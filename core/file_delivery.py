# -*- coding: utf-8 -*-
"""对话产物交付：从回复文本/任务工作区收集文件，统一收进固定交付目录，
并返回聊天窗可直接渲染的条目（含缺失、复制失败、无产物三种状态）。

只处理已知产物后缀，绝不移动源文件；交付目录里的同名文件自动加 (2)(3)。
"""
import json
import re
import shutil
import time
from pathlib import Path

from core import guga_paths

ROOT = Path(__file__).resolve().parents[1]
DELIVERY_ROOT = (guga_paths.DELIVERY).resolve()
LOG_PATH = DELIVERY_ROOT / 'delivery-log.jsonl'
MAX_FILES = 30
MAX_BYTES = 500 * 1024 * 1024

ARTIFACT_EXT = {
    'pptx', 'ppt', 'docx', 'doc', 'xlsx', 'xls', 'pdf',
    'mp4', 'mov', 'webm', 'mkv', 'gif',
    'png', 'jpg', 'jpeg', 'webp', 'svg',
    'html', 'htm', 'csv', 'zip', 'md', 'txt', 'srt', 'ass',
}
_EXT = '|'.join(sorted(ARTIFACT_EXT, key=len, reverse=True))
_WIN = re.compile(r'[A-Za-z]:[\\/][^\r\n"<>|*?]*?\.(' + _EXT + r')(?![A-Za-z0-9_])', re.I)
_REL = re.compile(r'(?<![\w:/])((?:\.{1,2}[\\/]|runtime[\\/]|deliverables[\\/]|[^\s"\'<>|]+[\\/])'
                  r'[^\r\n"<>|*?]*?\.(' + _EXT + r'))(?![A-Za-z0-9_])', re.I)
_BARE = re.compile(r'(?<![A-Za-z0-9_/.:\\-])([\w\u4e00-\u9fff][\w\u4e00-\u9fff()（）\-]*\.(' + _EXT + r'))'
                   r'(?![A-Za-z0-9_])', re.I)
_QUOTED = re.compile(r'[`"“\'‘]([^\r\n`"”\'’]+?\.(' + _EXT + r'))[`"”\'’]', re.I)
INTENT = re.compile(r'文件|附件|导出|下载|保存成|保存为|pptx|ppt|word|docx|excel|xlsx|pdf|'
                    r'视频|mp4|html|网页|图片|海报|字幕|压缩包|zip|演示文稿|幻灯片|表格|文档|'
                    r'(?:做|写|生成|整理|导出)(?:一|个|份|张|段)?(?:报告|ppt|pdf|文档|表格|图片|'
                    r'视频|网页|海报|字幕)', re.I)


def wants_file(text=''):
    return bool(INTENT.search(str(text or '')))


def _clean(value):
    return str(value or '').strip().strip('`"\'“”‘’（）()[]<>，。；、:：')


def _trim(value):
    """去掉叙述粘在绝对路径前面的字（"已生成：C:\\x\\a.pptx" → "C:\\x\\a.pptx"）。"""
    value = _clean(value)
    match = re.search(r'[A-Za-z]:[\\/]', value)
    return value[match.start():] if match else value


def extract_paths(text):
    """从回复里抽出候选产物路径，保持出现顺序并去重。"""
    found = []

    def add(value):
        value = _trim(value)
        if not value:
            return
        folded = value.casefold()
        # 裸文件名可能只是某个完整路径的尾巴（"季度报告.pptx" 被切成 "度报告.pptx"）。
        if any(len(item) > len(value) and item.casefold().endswith(folded) for item in found):
            return
        if value not in found:
            found.append(value)

    for match in _WIN.finditer(text or ''):
        add(match.group(0))
    for match in _REL.finditer(text or ''):
        add(match.group(1))
    for match in _QUOTED.finditer(text or ''):
        add(match.group(1))
    for match in _BARE.finditer(text or ''):
        value = _clean(match.group(1))
        # 中文叙述常直接粘着文件名("已生成report.pptx")，取以 ASCII 开头的文件名尾巴
        tail = re.fullmatch(r'.*?([A-Za-z0-9_][\w\u4e00-\u9fff()（）\- ]*?\.(?:' + _EXT + r'))',
                            value, re.I)
        add(tail.group(1) if tail else value)
    return found


def _existing(raw, workspace=None):
    """返回 (real_or_None, primary_candidate)。"""
    value = str(raw or '').strip()
    if value.startswith('file:///'):
        value = value[8:].replace('/', '\\') if ':\\' in value[8:].replace('/', '\\') else value[8:]
    path = Path(value).expanduser()
    if path.is_absolute():
        return (path.resolve() if path.exists() else None), path
    relative = value.replace('/', '\\').lstrip('.\\')
    bases = []
    if relative.lower().startswith('deliverables\\'):
        relative = relative[len('deliverables\\'):]
        bases.append(DELIVERY_ROOT)
    elif relative.lower().startswith('delivery\\'):
        relative = relative[len('delivery\\'):]
        bases.append(DELIVERY_ROOT)
    if workspace:
        bases.append(Path(workspace))
    if ROOT not in bases:
        bases.append(ROOT)
    if DELIVERY_ROOT not in bases:
        bases.append(DELIVERY_ROOT)
    for base in bases:
        candidate = base / relative
        if candidate.exists():
            return candidate.resolve(), candidate
    primary = (Path(workspace) / relative) if workspace else ROOT / relative
    return None, primary


def _in_delivery(path):
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == DELIVERY_ROOT or DELIVERY_ROOT in resolved.parents


def _scan_workspace(workspace, since):
    if not workspace:
        return []
    base = Path(workspace)
    if not base.is_dir():
        return []
    found = []
    try:
        for path in base.rglob('*'):
            try:
                if not path.is_file() or path.suffix.lower().lstrip('.') not in ARTIFACT_EXT:
                    continue
                if since and path.stat().st_mtime < since - 2:
                    continue
                found.append(path)
            except OSError:
                continue
    except OSError:
        return []
    return sorted(found, key=lambda p: p.stat().st_mtime)[:MAX_FILES]


def _scan_deliveries(since):
    if not DELIVERY_ROOT.is_dir():
        return []
    found = []
    try:
        for path in DELIVERY_ROOT.rglob('*'):
            try:
                if not path.is_file() or path.suffix.lower().lstrip('.') not in ARTIFACT_EXT:
                    continue
                if path.name.startswith('delivery-'):
                    continue
                if since and path.stat().st_mtime < since - 2:
                    continue
                found.append(path)
            except OSError:
                continue
    except OSError:
        return []
    return sorted(found, key=lambda p: p.stat().st_mtime)[:MAX_FILES]


def _copy_unique(src):
    stem, suffix = src.stem, src.suffix
    target = DELIVERY_ROOT / src.name
    number = 2
    while target.exists():
        if target.resolve() == src.resolve():
            return target
        target = DELIVERY_ROOT / ('%s (%d)%s' % (stem, number, suffix))
        number += 1
    shutil.copy2(str(src), str(target))
    return target


def _entry(path, source=None):
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return dict(name=path.name, path=str(path), size=int(size), status='ok',
                source=str(source or path))


def _missing(raw):
    return dict(name=Path(_clean(raw)).name or _clean(raw), path=_clean(raw), status='missing', size=0)


def deliver(text='', user_text='', workspace=None, since=None, conversation=''):
    """收集并交付本轮产物。异常不外抛：交付失败只降级成 status=error。"""
    DELIVERY_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        candidates = extract_paths(text)
        seen = set()
        files, missing, failed, oversized = [], [], [], []

        def resolve(raw):
            path, primary = _existing(raw, workspace)
            if path is None:
                return None, primary
            return path, primary

        for raw in candidates:
            path, primary = resolve(raw)
            if path is None or not path.is_file():
                missing.append(_missing(raw))
                continue
            key = str(path).casefold()
            if key in seen:
                continue
            seen.add(key)
            if _in_delivery(path):
                files.append(_entry(path, path))
                continue
            try:
                if path.stat().st_size > MAX_BYTES:
                    oversized.append(dict(name=path.name, path=str(path), size=path.stat().st_size,
                                          status='too_large'))
                    continue
                dest = _copy_unique(path)
                files.append(_entry(dest, path))
            except Exception as exc:
                failed.append(dict(name=path.name, path=str(path), status='failed',
                                   error=type(exc).__name__ + ': ' + str(exc)[:120]))

        # 只有知道本轮开始时间才按时间扫目录；否则会把历史产物当成这一轮的交付。
        if since is not None:
            for path in _scan_workspace(workspace, since) + _scan_deliveries(since):
                path = Path(path)
                key = str(path.resolve()).casefold()
                if key in seen:
                    continue
                seen.add(key)
                try:
                    if _in_delivery(path):
                        files.append(_entry(path, path))
                    elif path.stat().st_size > MAX_BYTES:
                        oversized.append(dict(name=path.name, path=str(path),
                                              size=path.stat().st_size, status='too_large'))
                    else:
                        files.append(_entry(_copy_unique(path), path))
                except Exception as exc:
                    failed.append(dict(name=path.name, path=str(path), status='failed',
                                       error=type(exc).__name__ + ': ' + str(exc)[:120]))

        files = files[:MAX_FILES]
        if files and missing:
            status = 'partial'
        elif files:
            status = 'ok'
        elif missing:
            status = 'missing'
        elif failed or oversized:
            status = 'failed'
        else:
            status = 'none' if wants_file(user_text) else 'empty'
        result = dict(version=1, root=str(DELIVERY_ROOT), status=status,
                      files=files, missing=missing[:MAX_FILES], failed=failed, oversized=oversized,
                      at=time.strftime('%Y-%m-%d %H:%M:%S'), conversation=conversation)
        try:
            with LOG_PATH.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(result, ensure_ascii=False) + '\n')
        except OSError:
            pass
        return result
    except Exception as exc:
        return dict(version=1, root=str(DELIVERY_ROOT), status='error', files=[], missing=[],
                    failed=[dict(name='交付登记', path='', status='failed',
                                 error=type(exc).__name__ + ': ' + str(exc)[:160])],
                    oversized=[], at=time.strftime('%Y-%m-%d %H:%M:%S'), conversation=conversation)


def human_size(size):
    size = float(size or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return ('%d %s' % (size, unit)) if unit == 'B' else ('%.1f %s' % (size, unit))
        size /= 1024.0
    return '0 B'
