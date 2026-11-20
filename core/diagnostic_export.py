# -*- coding: utf-8 -*-
"""一键导出诊断包：版本/自检/最近日志/任务台账摘要/失败码/证据目录清单。

原则：
  · 只读现有落点，不改业务状态；不打包 ai_config.json 原文；
  · 所有文本先脱敏再写入 zip（sk-* / api_key / Authorization / Bearer / 长十六进制）；
  · 证据目录只列文件名/大小/时间，截图按单个 <5MB 且总量受限可选打包；
  · 任何异常都返回结构化结果，不把异常抛给界面线程。
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
import time
import zipfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from core import guga_paths

DEFAULT_LOG_MINUTES = 30
SELFCHECK_CACHE_TTL = 3600
MAX_LOG_FILE_BYTES = 4 * 1024 * 1024
MAX_LOG_TEXT_CHARS = 2 * 1024 * 1024
MAX_SCREENSHOT_BYTES = 5 * 1024 * 1024
MAX_SCREENSHOT_TOTAL = 25 * 1024 * 1024
MAX_SCREENSHOTS = 40
MAX_EVIDENCE_FILES_PER_DIR = 400
MAX_TASK_RESULT_BYTES = 2 * 1024 * 1024
MAX_TASK_RESULTS = 40

SELFCHECK_CACHE_NAMES = (
    'selfcheck-last.json',
    'selfcheck.json',
    'guga-selfcheck.json',
    'guga_selfcheck_report.json',
)

_SCREENSHOT_EXTS = {'.png', '.jpg', '.jpeg', '.bmp', '.webp'}
_TS_RE = re.compile(r'(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})')
_JSON_CODE_RE = re.compile(r'"code"\s*:\s*"([a-z_][a-z0-9_]{2,})"', re.I)
_TEXT_CODE_RE = re.compile(r'\bcode\s*=\s*([a-z_][a-z0-9_]{2,})', re.I)
_SENSITIVE_KEY_RE = re.compile(
    r'(?i)^(api[_-]?key|authorization|bearer|access[_-]?token|refresh[_-]?token|secret)$')

_SECRET_RULES = (
    (re.compile(r'sk-[A-Za-z0-9_-]{6,}'), 'sk-***'),
    (re.compile(r'(?i)(["\']?api[_-]?key["\']?\s*[:=]\s*)(["\'])([^"\']{4,})(["\'])'),
     r'\1\2***\4'),
    (re.compile(r'(?i)(["\']?api[_-]?key["\']?\s*[:=]\s*)([^,\s;}]+)'), r'\1***'),
    (re.compile(r'(?i)(["\']?authorization["\']?\s*[:=]\s*)(?:bearer\s+)?'
                r'(["\'])([^"\']*)(["\'])'), r'\1\2***\4'),
    (re.compile(r'(?i)(["\']?authorization["\']?\s*[:=]\s*)(?:bearer\s+)?[^\s,;}]+'),
     r'\1***'),
    (re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}'), 'Bearer ***'),
    (re.compile(r'\b[A-Fa-f0-9]{32,}\b'), '***'),
)


# ---------------------------------------------------------------- 基础工具

def _now_text():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _iso_mtime(path):
    try:
        return datetime.fromtimestamp(Path(path).stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return ''


def _redact_text(text):
    value = str(text or '')
    for pattern, replacement in _SECRET_RULES:
        try:
            value = pattern.sub(replacement, value)
        except Exception:
            continue
    return value


def _json_safe(value, depth=0):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value
    if depth > 6:
        return str(value)[:800]
    if isinstance(value, dict):
        return {str(key): _json_safe(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item, depth + 1) for item in value]
    return str(value)[:1200]


def _redact_obj(value):
    if isinstance(value, dict):
        return {str(key): _redact_obj(item) for key, item in value.items()
                if not _SENSITIVE_KEY_RE.match(str(key))}
    if isinstance(value, (list, tuple)):
        return [_redact_obj(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _read_text_tail(path, limit=MAX_LOG_FILE_BYTES):
    """只读文件尾部；返回 (text, 是否截断)。日志只关心最近一段。"""
    path = Path(path)
    size = path.stat().st_size
    with path.open('rb') as handle:
        if size > limit:
            handle.seek(size - limit)
            raw = handle.read()
            text = raw.decode('utf-8', errors='replace')
            # 从中间截断时第一条可能不完整，丢掉它。
            return ('\n'.join(text.splitlines()[1:]), True)
        return (handle.read().decode('utf-8-sig', errors='replace'), False)


def _unique_arcname(arcname, used):
    name = str(arcname).replace('\\', '/').strip('/')
    if name not in used:
        used.add(name)
        return name
    folder, base = (name.rsplit('/', 1) + [''])[:2] if '/' in name else ('', name)
    stem, suffix = os.path.splitext(base)
    index = 2
    while True:
        candidate = '%s%s_%d%s' % (folder + '/' if folder else '', stem, index, suffix)
        if candidate not in used:
            used.add(candidate)
            return candidate
        index += 1


def _safe_join(*parts):
    chunks = []
    for part in parts:
        text = str(part or '').replace('\\', '/')
        chunks.extend(item for item in text.split('/') if item and item not in ('.', '..'))
    return '/'.join(chunks)


# ---------------------------------------------------------------- 版本 / 构建信息

def _root_dir():
    return Path(__file__).resolve().parents[1]


def _read_text(path, limit=20000):
    try:
        return Path(path).read_text(encoding='utf-8', errors='replace')[:limit]
    except Exception:
        return ''


def _git_sha(root):
    """源码模式读 .git/HEAD 与 refs；打包模式没有 .git，返回空。"""
    git_dir = Path(root) / '.git'
    if not git_dir.is_dir():
        return ''
    try:
        head = (git_dir / 'HEAD').read_text(encoding='utf-8', errors='replace').strip()
    except Exception:
        return ''
    if head.startswith('ref:'):
        ref = head[4:].strip()
        try:
            value = (git_dir / ref).read_text(encoding='utf-8', errors='replace').strip()
            if value:
                return value
        except Exception:
            pass
        try:
            packed = (git_dir / 'packed-refs').read_text(encoding='utf-8', errors='replace')
            for line in packed.splitlines():
                if line and not line.startswith(('#', '^')) and line.rsplit(' ', 1)[-1] == ref:
                    return line.split(' ', 1)[0]
        except Exception:
            pass
    return head if re.fullmatch(r'[0-9a-fA-F]{40}', head or '') else ''


def _build_info_paths():
    """包内固定位置 _internal/build-info.json：打包时写入版本/构建 SHA/内容摘要。"""
    rows = []
    if getattr(sys, 'frozen', False):
        meipass = str(getattr(sys, '_MEIPASS', '') or '')
        if meipass:
            rows.append(Path(meipass) / 'build-info.json')
            rows.append(Path(meipass).parent / '_internal' / 'build-info.json')
        rows.append(Path(sys.executable).resolve().parent / '_internal' / 'build-info.json')
    rows.append(_root_dir() / 'dist' / 'GUGA-Desktop' / '_internal' / 'build-info.json')
    return rows


def _load_build_info():
    for path in _build_info_paths():
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
        except Exception:
            continue
        if isinstance(data, dict) and data:
            return data, str(path)
    return {}, ''


def _sha256_file(path, chunk=1024 * 1024):
    digest = hashlib.sha256()
    with open(str(path), 'rb') as handle:
        while True:
            data = handle.read(chunk)
            if not data:
                break
            digest.update(data)
    return digest.hexdigest()


def _zip_candidates(name):
    """找原始 zip：桌面/下载/当前目录/安装包同级；找不到就返回空（不瞎填）。"""
    roots = [Path.home() / 'Desktop', Path.home() / 'Downloads', Path.cwd(),
             Path(sys.executable).resolve().parent]
    seen, rows = set(), []
    patterns = [name] if name else []
    patterns.append('GUGA-Desktop-win64-*.zip')
    for root in roots:
        for pattern in patterns:
            try:
                matches = list(root.glob(pattern))
            except Exception:
                continue
            for path in matches:
                try:
                    key = os.path.normcase(os.path.abspath(str(path)))
                    if key in seen or not path.is_file():
                        continue
                    seen.add(key)
                    rows.append(path)
                except Exception:
                    continue
    return rows


def _package_sha(name):
    """实测原始 zip 的 SHA256；同时核对同名 .sha256 sidecar。

    返回 (sha256, 来源说明, zip 路径, sidecar 是否一致/None, 实际 sidecar 值)。
    """
    candidates = _zip_candidates(name)
    if not candidates:
        return '', '未找到原始 zip（解压后只保留目录时无法反推压缩包 SHA）', '', None, ''
    if name:
        named = [p for p in candidates if p.name.casefold() == str(name).casefold()]
        if not named:
            others = '、'.join(p.name for p in candidates[:5]) or '无'
            return ('', '未找到包内声明的原始 zip：%s（找到的其它 zip：%s）'
                    % (name, others), '', None, '')
        candidates = named
    # 多个候选时优先“旁边有 sidecar”的，其次最近修改的。
    chosen = max(candidates, key=lambda p: (p.stat().st_mtime if p.exists() else 0))
    try:
        actual = _sha256_file(chosen)
    except Exception as exc:
        return '', '读取 zip 失败：%s: %s' % (type(exc).__name__, str(exc)[:120]), str(chosen), None, ''
    sidecar = Path(str(chosen) + '.sha256')
    sidecar_value, matches = '', None
    try:
        text = sidecar.read_text(encoding='utf-8', errors='replace').strip()
        first = text.split()[0] if text else ''
        if re.fullmatch(r'[0-9a-fA-F]{64}', first or ''):
            sidecar_value = first.lower()
            matches = sidecar_value == actual
    except Exception:
        pass
    return actual, '实测文件：%s' % chosen, str(chosen), matches, sidecar_value


def _version_info():
    version = str(os.environ.get('GUGA_VERSION') or os.environ.get('GUGA_BUILD_TAG') or '')
    version_source = '环境变量' if version else ''
    build_report = {}
    root = _root_dir()
    build_info, build_info_path = _load_build_info()

    if not version and build_info.get('version'):
        version = str(build_info.get('version'))
        version_source = build_info_path or '_internal/build-info.json'

    if not version:
        report_path = root / 'dist' / 'build-report.json'
        try:
            data = json.loads(report_path.read_text(encoding='utf-8'))
            build_report = {
                'built_at': data.get('built_at'),
                'python': data.get('python'),
                'pyinstaller': data.get('pyinstaller'),
                'exe_bytes': data.get('exe_bytes'),
                'folder_bytes': data.get('folder_bytes'),
            }
            version = str(data.get('version') or data.get('build_tag') or '')
            if version:
                version_source = 'dist/build-report.json'
        except Exception:
            pass

    if not version:
        notes = root / 'RELEASE-NOTES.md'
        text = _read_text(notes, 2000)
        match = re.search(r'（\s*(\d{8}[a-z]?)\s*）', text)
        if match:
            version = match.group(1)
            version_source = 'RELEASE-NOTES.md'
    if not version:
        version = '未知'
        version_source = '未找到版本常量或构建产物'

    sha = ''
    sha_source = ''
    env_sha = str(os.environ.get('GUGA_BUILD_SHA') or os.environ.get('GUGA_GIT_SHA') or '')
    if env_sha:
        sha, sha_source = env_sha, '环境变量'
    elif build_info.get('build_sha'):
        sha, sha_source = str(build_info['build_sha']), build_info_path or '_internal/build-info.json'
    elif not getattr(sys, 'frozen', False):
        sha = _git_sha(root)
        if sha:
            sha_source = '.git'

    if not build_report and isinstance(build_info.get('build_report'), dict):
        build_report = dict(build_info['build_report'])

    package_name = str(build_info.get('package_name') or '')
    package_sha, package_source, package_zip, sidecar_ok, sidecar_value = \
        _package_sha(package_name)
    content_sha = str(build_info.get('package_content_sha256') or '')
    content_sha_source = (build_info_path or '_internal/build-info.json') if content_sha else ''

    return dict(
        version=version,
        version_source=version_source,
        build_sha=sha or '未知',
        build_sha_source=sha_source or '未找到构建产物中的 SHA',
        package_sha256=package_sha or '未知',
        package_sha256_source=package_source,
        package_zip_path=package_zip,
        package_sha256_sidecar=sidecar_value,
        package_sha256_sidecar_matches=sidecar_ok,
        package_name=package_name,
        package_content_sha256=content_sha or '未知',
        package_content_sha256_source=content_sha_source,
        build_info_path=build_info_path,
        check_version=str(build_info.get('check_version') or ''),
        build_report=build_report,
    )


# ---------------------------------------------------------------- 自检结果

def _summary(items):
    """与 env_check.summarize 同一套聚合，避免两处算法漂移。"""
    try:
        from core import env_check
        return env_check.summarize(items)
    except Exception:
        pass
    states = {'pass': 0, 'degraded': 0, 'fail': 0, 'untested': 0}
    rows = list(items or [])
    for item in rows:
        data = item if isinstance(item, dict) else {}
        state = str(data.get('state') or 'untested')
        states[state if state in states else 'untested'] += 1
    counts = dict(states)
    counts.update(total=len(rows), state_total=sum(states.values()),
                  consistent=(sum(states.values()) == len(rows)))
    return counts


def _normalise_items(items):
    rows = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        rows.append(_redact_obj({
            'check_id': item.get('check_id') or '',
            'name': item.get('name') or '',
            'level': item.get('level') or 'info',
            'state': item.get('state') or 'untested',
            'detail': str(item.get('detail') or ''),
            'value': _json_safe(item.get('value')),
            'error': str(item.get('error') or ''),
        }))
    return rows


def _load_selfcheck_cache():
    candidates = [guga_paths.RUNTIME / name for name in SELFCHECK_CACHE_NAMES]
    try:
        candidates.extend(path for path in guga_paths.RUNTIME.glob('*.json')
                          if 'selfcheck' in path.name.casefold())
    except Exception:
        pass
    seen = set()
    rows = []
    for path in candidates:
        try:
            key = os.path.normcase(os.path.abspath(str(path)))
            if key in seen or not path.is_file():
                continue
            seen.add(key)
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            raw = data.get('items') if isinstance(data, dict) else None
            if not isinstance(raw, list) or not raw:
                continue
            items = _normalise_items(raw)
            rows.append(dict(
                source='cache',
                path=str(path),
                age_seconds=int(max(0, time.time() - path.stat().st_mtime)),
                checked_at=datetime.fromtimestamp(path.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                summary=_summary(items),
                items=items,
                note='',
            ))
        except Exception:
            continue
    rows.sort(key=lambda row: row['age_seconds'])
    return rows


def _run_live_selfcheck():
    try:
        from core import env_check
        started = time.perf_counter()
        items = list(env_check.iter_checks())
        return dict(
            source='live',
            path='',
            age_seconds=0,
            checked_at=_now_text(),
            summary=env_check.summarize(items),
            items=_normalise_items(items),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            note='',
        )
    except Exception as exc:
        return dict(source='error', path='', age_seconds=0, checked_at=_now_text(),
                    summary={}, items=[], note='', error='%s: %s' % (type(exc).__name__, str(exc)[:200]))


def _save_selfcheck_cache(payload):
    """真实跑一次后落一份缓存，下次导出优先复用，避免重复跑重活。"""
    try:
        target = guga_paths.RUNTIME / 'selfcheck-last.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        data = dict(schema='guga.selfcheck/1', updated=_now_text(),
                    summary=payload.get('summary') or {}, items=payload.get('items') or [])
        tmp = target.with_name(target.name + '.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(str(tmp), str(target))
    except Exception:
        pass


def _collect_selfcheck():
    cached = _load_selfcheck_cache()
    fresh = next((row for row in cached if row['age_seconds'] <= SELFCHECK_CACHE_TTL), None)
    if fresh is not None:
        return fresh
    live = _run_live_selfcheck()
    if live.get('items'):
        _save_selfcheck_cache(live)
        return live
    if cached:
        fallback = cached[0]
        fallback['note'] = '实时自检不可用，使用较早缓存（已超过 %d 分钟）' % (SELFCHECK_CACHE_TTL // 60)
        return fallback
    return live


# ---------------------------------------------------------------- 最近日志

def _parse_line_time(line):
    match = _TS_RE.search(line[:400])
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1).replace('T', ' '), '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None


def _recent_log_text(path, cutoff):
    text, source_truncated = _read_text_tail(path)
    lines = text.splitlines()
    parsed = [_parse_line_time(line) for line in lines]
    has_time = any(value is not None for value in parsed)
    truncated = bool(source_truncated)

    if has_time:
        chosen = []
        include = False
        for line, value in zip(lines, parsed):
            if value is not None:
                include = value >= cutoff
            if include:
                chosen.append(line)
        text = '\n'.join(chosen).strip()
    else:
        if datetime.fromtimestamp(Path(path).stat().st_mtime) < cutoff:
            return '', False, 0, truncated
        text = '\n'.join(lines[-500:]).strip()

    if len(text) > MAX_LOG_TEXT_CHARS:
        text = text[-MAX_LOG_TEXT_CHARS:]
        truncated = True
    if not text:
        return '', False, 0, truncated
    return text, len(text.splitlines()), len(text.encode('utf-8')), truncated


def _log_source_paths():
    paths = []
    try:
        from core import app_log
        primary = Path(app_log.log_path())
        for candidate in (primary, Path(str(primary) + '.1')):
            if candidate.is_file():
                paths.append(candidate)
    except Exception:
        pass
    try:
        paths.extend(sorted(path for path in guga_paths.RUNTIME.glob('*.log') if path.is_file()))
    except Exception:
        pass
    seen = set()
    unique = []
    for path in paths:
        key = os.path.normcase(os.path.abspath(str(path)))
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _collect_logs(minutes, used_arcnames, skipped, task_since=None):
    cutoff = datetime.now() - timedelta(minutes=max(1, int(minutes)))
    # 失败任务可能发生在 30 分钟窗口之前：把窗口强制扩到该任务开始时刻，
    # 否则 adapter_chain.log / startup.log 里真正的停因不会进包。
    if task_since and task_since < cutoff:
        cutoff = task_since
    window_minutes = max(1, int((datetime.now() - cutoff).total_seconds() // 60))
    rows = []
    payloads = []
    for path in _log_source_paths():
        try:
            text, lines, size_bytes, truncated = _recent_log_text(path, cutoff)
        except Exception as exc:
            skipped.append(dict(kind='log', name=str(path),
                                reason='读取失败：%s' % type(exc).__name__))
            continue
        if not text:
            skipped.append(dict(kind='log', name=str(path),
                                reason='最近 %d 分钟内没有日志行' % window_minutes))
            continue
        try:
            relative = path.relative_to(guga_paths.RUNTIME)
            arcname = _safe_join('logs', relative.as_posix())
        except Exception:
            arcname = _safe_join('logs', 'app', path.name)
        arcname = _unique_arcname(arcname, used_arcnames)
        data = _redact_text(text).encode('utf-8')
        payloads.append((arcname, data))
        rows.append(dict(source_path=str(path), arcname=arcname, bytes=size_bytes,
                         lines=lines, truncated=truncated, mtime=_iso_mtime(path),
                         window_minutes=window_minutes))
    return rows, payloads


def _parse_stamp(value):
    text = str(value or '').strip()
    if not text:
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def _task_window_start(ledger):
    """最近一次任务的起跑时刻：优先任务台账，其次证据目录里最新的 result.json。"""
    for row in ledger.get('recent') or []:
        started = _parse_stamp(row.get('started'))
        if started is not None:
            return started - timedelta(minutes=2)
        finished = _parse_stamp(row.get('finished'))
        if finished is not None:
            return finished - timedelta(minutes=30)
    newest = None
    for _label, folder in _evidence_candidates():
        try:
            paths = folder.rglob('result.json') if folder.is_dir() else []
        except Exception:
            continue
        for path in paths:
            try:
                stamp = datetime.fromtimestamp(path.stat().st_mtime)
            except OSError:
                continue
            if newest is None or stamp > newest:
                newest = stamp
    return (newest - timedelta(minutes=2)) if newest else None


def _collect_task_results(cutoff, used_arcnames, skipped):
    """强制带上失败任务的 result.json（适配器证据目录与 agent-jobs）。"""
    rows = []
    payloads = []
    if cutoff is None:
        return rows, payloads
    for label, folder in _evidence_candidates():
        if not folder.is_dir():
            continue
        try:
            paths = sorted(folder.rglob('result.json'))
        except Exception:
            continue
        for path in paths:
            try:
                stat = path.stat()
                if datetime.fromtimestamp(stat.st_mtime) < cutoff:
                    continue
                if stat.st_size > MAX_TASK_RESULT_BYTES:
                    skipped.append(dict(kind='task-result', name=str(path),
                                        reason='result.json 超过 2MB，只列清单'))
                    continue
                relative = path.relative_to(folder).as_posix()
                arcname = _unique_arcname(
                    _safe_join('task-results', label, relative), used_arcnames)
                text = path.read_text(encoding='utf-8', errors='replace')
                payloads.append((arcname, _redact_text(text).encode('utf-8')))
                rows.append(dict(source_path=str(path), arcname=arcname,
                                 bytes=stat.st_size, mtime=_iso_mtime(path)))
            except Exception as exc:
                skipped.append(dict(kind='task-result', name=str(path),
                                    reason='读取失败：%s' % type(exc).__name__))
            if len(rows) >= MAX_TASK_RESULTS:
                skipped.append(dict(kind='task-result', name=str(folder),
                                    reason='result.json 数量达到上限 %d，其余只列清单'
                                           % MAX_TASK_RESULTS))
                return rows, payloads
    return rows, payloads


# ---------------------------------------------------------------- 任务台账 / 失败码

def _task_ledger_summary():
    path = guga_paths.RUNTIME / 'task-ledger.json'
    if not path.is_file():
        return dict(present=False, path=str(path), total=0, status_counts={}, recent=[])
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except Exception as exc:
        return dict(present=True, path=str(path), error='%s: %s'
                    % (type(exc).__name__, str(exc)[:160]), total=0, status_counts={}, recent=[])
    records = data.get('records') if isinstance(data, dict) else {}
    if not isinstance(records, dict):
        records = {}
    rows = []
    for record in records.values():
        if not isinstance(record, dict):
            continue
        rows.append(_redact_obj(dict(
            task_id=record.get('task_id') or '',
            st_id=record.get('st_id') or '',
            attempt=record.get('attempt') or 1,
            name=str(record.get('name') or '')[:120],
            app=str(record.get('app') or '')[:80],
            status=record.get('status') or '',
            started=record.get('started') or '',
            finished=record.get('finished') or '',
            note=str(record.get('note') or '')[:240],
        )))
    rows.sort(key=lambda row: row.get('finished') or row.get('started') or '', reverse=True)
    return dict(present=True, path=str(path), updated=(data.get('updated') or ''),
                total=len(rows), status_counts=dict(Counter(row.get('status') or 'unknown' for row in rows)),
                recent=rows[:20])


def _failure_codes(log_texts):
    declared = []
    apps_dir = _root_dir() / 'core' / 'adapters' / 'apps'
    try:
        for path in sorted(apps_dir.glob('*.json')):
            if path.name.startswith('_'):
                continue
            try:
                data = json.loads(path.read_text(encoding='utf-8-sig'))
            except Exception:
                continue
            anchors = data.get('anchors') if isinstance(data.get('anchors'), dict) else {}
            codes = []
            for item in anchors.get('failure_codes') or []:
                if isinstance(item, dict) and item.get('code'):
                    codes.append(dict(code=str(item['code']),
                                      hint=str(item.get('hint') or '')[:200]))
                elif item:
                    codes.append(dict(code=str(item), hint=''))
            declared.append(dict(app_id=data.get('id') or path.stem,
                                 app_name=data.get('name') or '', codes=codes))
    except Exception:
        pass

    observed = set()
    for text in log_texts:
        for match in _JSON_CODE_RE.finditer(text or ''):
            observed.add(match.group(1))
        for match in _TEXT_CODE_RE.finditer(text or ''):
            observed.add(match.group(1))
    return dict(declared=declared, observed_recent=sorted(observed)[:200],
                note='declared 来自 core/adapters/apps/*.json；observed_recent 从本次日志提取')


# ---------------------------------------------------------------- 证据目录

def _evidence_candidates():
    runtime = guga_paths.RUNTIME
    rows = [
        ('doubao_evidence', runtime / 'doubao_evidence'),
        ('evidence', runtime / 'evidence'),
        ('workspace_outbox', runtime / 'workspace' / '.guga' / 'outbox'),
        ('workspace_answers', runtime / 'workspace' / '.guga' / 'answers'),
        ('workspace_inbox', runtime / 'workspace' / '.guga' / 'inbox'),
        ('workspace_mcp_connections', runtime / 'workspace' / '.guga' / 'mcp-connections'),
        ('agent_jobs', runtime / 'agent-jobs'),
    ]
    seen = set()
    unique = []
    for label, path in rows:
        key = os.path.normcase(os.path.abspath(str(path)))
        if key not in seen:
            seen.add(key)
            unique.append((label, path))
    try:
        for path in runtime.iterdir():
            if path.is_dir() and 'evidence' in path.name.casefold():
                key = os.path.normcase(os.path.abspath(str(path)))
                if key not in seen:
                    seen.add(key)
                    unique.append((path.name, path))
    except Exception:
        pass
    return unique


def _collect_evidence(used_arcnames, skipped):
    directories = []
    screenshot_rows = []
    total_files = 0
    for label, folder in _evidence_candidates():
        record = dict(label=label, path=str(folder), present=bool(folder.is_dir()),
                      files=[], truncated=False)
        if folder.is_dir():
            files = []
            try:
                iterator = sorted(folder.rglob('*'), key=lambda item: item.as_posix().casefold())
            except Exception:
                iterator = []
            for path in iterator:
                try:
                    if not path.is_file() or path.is_symlink():
                        continue
                    size = path.stat().st_size
                    relative = path.relative_to(folder).as_posix()
                    entry = dict(name=relative, bytes=size, mtime=_iso_mtime(path), included=False)
                    if path.name.casefold() == 'ai_config.json':
                        skipped.append(dict(kind='evidence', name=str(path),
                                            reason='配置文件不打包，也不列内容'))
                        continue
                    if path.suffix.casefold() in _SCREENSHOT_EXTS:
                        if size > MAX_SCREENSHOT_BYTES:
                            skipped.append(dict(kind='evidence', name=str(path),
                                                reason='截图超过 5MB，只列清单'))
                        else:
                            screenshot_rows.append((path.stat().st_mtime, label, folder,
                                                    path, relative, entry))
                    else:
                        skipped.append(dict(kind='evidence', name=str(path),
                                            reason='非截图文件，只列清单'))
                    files.append(entry)
                    if len(files) >= MAX_EVIDENCE_FILES_PER_DIR:
                        record['truncated'] = True
                        break
                except Exception:
                    continue
            record['files'] = files
            record['file_count'] = len(files)
            total_files += len(files)
        directories.append(record)

    screenshot_rows.sort(key=lambda row: row[0], reverse=True)
    payloads = []
    chosen = 0
    total_bytes = 0
    for mtime, label, folder, path, relative, entry in screenshot_rows:
        try:
            size = path.stat().st_size
        except Exception:
            continue
        if chosen >= MAX_SCREENSHOTS or total_bytes + size > MAX_SCREENSHOT_TOTAL:
            skipped.append(dict(kind='evidence', name=str(path),
                                reason='截图数量或总量达到上限，只列清单'))
            continue
        arcname = _unique_arcname(_safe_join('evidence', label, relative), used_arcnames)
        try:
            payloads.append((arcname, path.read_bytes()))
        except Exception as exc:
            skipped.append(dict(kind='evidence', name=str(path),
                                reason='读取截图失败：%s' % type(exc).__name__))
            continue
        entry['included'] = True
        entry['arcname'] = arcname
        chosen += 1
        total_bytes += size

    return directories, payloads, dict(files=total_files, screenshots_included=chosen,
                                       screenshot_bytes=total_bytes)


# ---------------------------------------------------------------- 导出主函数

def _desktop_dir():
    desktop = Path.home() / 'Desktop'
    return desktop if desktop.is_dir() else Path.home()


def _target_zip(dest_dir):
    folder = Path(dest_dir).expanduser() if dest_dir else _desktop_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    target = folder / ('咕嘎诊断_%s.zip' % stamp)
    index = 2
    while target.exists():
        target = folder / ('咕嘎诊断_%s_%d.zip' % (stamp, index))
        index += 1
    return target


def export_diagnostics(dest_dir=None, log_minutes=DEFAULT_LOG_MINUTES):
    """收集诊断信息并写到桌面/指定目录的 zip。异常也返回 dict，不抛给界面。"""
    skipped = []
    used_arcnames = {'manifest.json'}
    try:
        version = _version_info()
        selfcheck = _collect_selfcheck()
        # 先读任务台账，确定最近一次任务的起跑时刻；日志窗口与 result.json
        # 都以它为下限，避免“只导最近 30 分钟”把失败时段漏掉。
        ledger = _task_ledger_summary()
        task_since = _task_window_start(ledger)
        log_rows, log_payloads = _collect_logs(
            log_minutes, used_arcnames, skipped, task_since=task_since)
        result_rows, result_payloads = _collect_task_results(
            task_since, used_arcnames, skipped)
        log_texts = [data.decode('utf-8', errors='replace') for _arc, data in log_payloads]
        codes = _failure_codes(log_texts)
        directories, evidence_payloads, evidence_stats = _collect_evidence(
            used_arcnames, skipped)

        from core import app_log
        environment = dict(
            runtime=str(guga_paths.RUNTIME),
            config_path=str(guga_paths.CONFIG),
            config_exists=guga_paths.CONFIG.is_file(),
            base_source=guga_paths.BASE_SOURCE,
            base_writable=bool(guga_paths.BASE_WRITABLE),
            log_dir=str(Path(app_log.log_path()).parent) if app_log else '',
            log_controlled_by_base=True,
            guga_home=str(os.environ.get('GUGA_HOME') or ''),
            frozen=bool(getattr(sys, 'frozen', False)),
            python=sys.version.split()[0],
            platform=platform.platform(),
            executable=sys.executable,
            cwd=os.getcwd(),
        )

        included = ['manifest.json']
        payloads = list(log_payloads) + list(result_payloads) + list(evidence_payloads)
        included.extend(arcname for arcname, _data in payloads)
        manifest = dict(
            schema='guga.diagnostic/1',
            generated_at=_now_text(),
            log_minutes=int(log_minutes),
            task_window_start=(task_since.strftime('%Y-%m-%d %H:%M:%S')
                               if task_since else ''),
            version=version,
            environment=environment,
            selfcheck=selfcheck,
            logs=dict(files=log_rows, total_files=len(log_rows),
                      window_extended_to_task=bool(
                          task_since and (datetime.now() - task_since)
                          > timedelta(minutes=max(1, int(log_minutes))))),
            task_results=dict(files=result_rows, total_files=len(result_rows)),
            task_ledger=ledger,
            failure_codes=codes,
            evidence=dict(directories=directories, stats=evidence_stats),
            included=included,
            skipped=skipped,
            redaction='已去除密钥、鉴权头令牌与 32 位以上十六进制串；'
                      '未打包配置文件原文。',
        )
        manifest_bytes = json.dumps(manifest, ensure_ascii=False,
                                    indent=2).encode('utf-8')

        target = _target_zip(dest_dir)
        tmp = target.with_name(target.name + '.part')
        with zipfile.ZipFile(str(tmp), 'w', zipfile.ZIP_DEFLATED,
                             compresslevel=6, allowZip64=True) as archive:
            archive.writestr('manifest.json', manifest_bytes)
            for arcname, data in payloads:
                archive.writestr(arcname, data)
        os.replace(str(tmp), str(target))
        size = target.stat().st_size
        return dict(ok=True, zip_path=str(target), size=size, entries=1 + len(payloads),
                    included=included, skipped=skipped, manifest=manifest)
    except Exception as exc:
        try:
            if 'tmp' in locals() and tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return dict(ok=False, zip_path='', size=0, entries=0, included=[], skipped=skipped,
                    error='%s: %s' % (type(exc).__name__, str(exc)[:300]))
