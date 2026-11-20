# -*- coding: utf-8 -*-
"""发布包凭据剥离与泄露扫描（打包流程的强制闸门）。

三道闸：
1) 打包前 sanitize_config() 把 api_key / token / password 等字段置空，
   只保留 model、base_url 这类非敏感配置；
2) 包内禁止任何非示例配置：ai_config*.json 文件名不含 example 一律判失败
   （2026-11-08 事故：安装目录残留的旧 ai_config.json 被迁成用户 Key）；
3) 打包后 assert_clean() 逐条目扫描发布目录与 zip：文本按形态匹配，.pyc 与
   exe 内嵌 PYZ 解开后扫字符串常量，另可经 GUGA_BUILD_SECRETS 载入真实 Key
   做精确值比对。发现泄露就删除产物并抛 SystemExit，失败必须中止构建。

输出永远不回显 Key 本身，只报文件路径和原因。
"""
from __future__ import annotations

import json
import marshal
import os
import re
import shutil
import tempfile
import types
import zipfile
from pathlib import Path

_SENSITIVE_EXACT = {
    'api_key', 'apikey', 'api-key', 'zhipu_api_key', 'doubao_tts_api_key',
    'access_token', 'refresh_token', 'token', 'secret', 'client_secret',
    'password', 'authorization', 'private_key',
}
_SENSITIVE_SUFFIX = ('_api_key', '_access_token', '_secret', '_password')

_CHUNK = 4 * 1024 * 1024
_OVERLAP = 128

# 通用 Key 形态只在文本类文件里匹配；二进制（dll/exe/pyd）里随机字节会误报，
# 二进制只比对 ai_config.json 里的真实凭据值。
_TEXT_SUFFIXES = {
    '.json', '.txt', '.ini', '.cfg', '.conf', '.yaml', '.yml', '.env', '.md',
    '.py', '.ps1', '.bat', '.cmd', '.toml', '.xml', '.html', '.htm', '.js',
    '.css', '.csv', '.log', '.spec', '.properties', '.example',
}

_PATTERNS = (
    # 前面不能紧跟字母数字：避免把 vosk-model-xxx 这类路径当成 sk- Key。
    (re.compile(rb'(?i)(?<![A-Za-z0-9])sk-(?:proj-)?[A-Za-z0-9._\-]{20,}'), 'sk- 形态密钥'),
    (re.compile(rb'(?i)LTAI[0-9A-Za-z]{12,}'), '阿里云 AccessKey'),
    (re.compile(rb'(?i)ghp_[0-9A-Za-z]{20,}'), 'GitHub Token'),
    (re.compile(rb'(?i)AIza[0-9A-Za-z_\-]{20,}'), 'Google API Key'),
    (re.compile(rb'(?i)Bearer\s+[A-Za-z0-9._\-]{20,}'), 'Bearer 长串'),
    # 不含 or-：会误伤 SPDX 许可证 ID（or-pdf-font-exception-...）；OpenRouter
    # 的 sk-or-v1-... 已被上面的 sk- 规则覆盖。
    (re.compile(rb'(?i)\b(?:xai-|gsk_|hf_|glpat-|AKIA)[A-Za-z0-9._\-]{16,}'),
     '常见 API Key 前缀'),
    (re.compile(rb'(?i)["\']?api[_-]?key["\']?\s*[:=]\s*["\']([^"\'\r\n]{8,})["\']'),
     'api_key 带非空值'),
)


def is_live_config(name) -> bool:
    """ai_config*.json 且文件名不含 example —— 发布包内一律不允许出现。"""
    base = str(name or '').replace('\\', '/').rsplit('/', 1)[-1].lower()
    return base.startswith('ai_config') and base.endswith('.json') and 'example' not in base


def _pattern_reasons(blob, needles=()):
    for needle in needles:
        if needle and needle in blob:
            return ['发现本机配置中的真实凭据']
    reasons = []
    for pattern, label in _PATTERNS:
        match = pattern.search(blob)
        if not match:
            continue
        if label == 'api_key 带非空值':
            try:
                value = match.group(1).decode('utf-8', 'ignore')
            except Exception:
                value = ''
            if _is_placeholder(value):
                continue
        reasons.append('发现' + label)
    return reasons


def _code_strings(code, out):
    if isinstance(code, types.CodeType):
        for item in code.co_consts:
            if isinstance(item, str):
                out.append(item.encode('utf-8', 'ignore'))
            elif isinstance(item, bytes):
                out.append(item)
            else:
                _code_strings(item, out)


def _scan_code_blob(blob, findings, label, needles=()):
    """扫 .pyc / PYZ 模块里的字符串常量（Key 不会藏在代码逻辑里，只会在常量里）。"""
    body = blob[16:] if len(blob) > 16 else blob      # .pyc 头 16 字节
    try:
        code = marshal.loads(body)
    except Exception:
        return
    strings = []
    _code_strings(code, strings)
    for text in strings:
        reasons = _pattern_reasons(text, needles)
        if reasons:
            findings.append((label, reasons[0]))
            return


def _scan_pyz(path, findings, label, needles=()):
    try:
        from PyInstaller.archive.readers import ZlibArchiveReader
        pyz = ZlibArchiveReader(str(path))
    except Exception as exc:
        findings.append((label, 'PYZ 无法解包：%s' % type(exc).__name__))
        return
    for module in list(getattr(pyz, 'toc', {}) or {}):
        try:
            code = pyz.extract(module)
        except Exception:
            continue
        strings = []
        _code_strings(code, strings)
        for text in strings:
            reasons = _pattern_reasons(text, needles)
            if reasons:
                findings.append((label, 'PYZ.%s：%s' % (module, reasons[0])))
                return


def _scan_exe_pyz(exe, findings, label, needles=()):
    """从 exe 内嵌归档取出 PYZ.pyz 再解包；取不到就明确失败，不静默跳过。"""
    try:
        from PyInstaller.archive.readers import CArchiveReader
        reader = CArchiveReader(str(exe))
    except Exception as exc:
        findings.append((label, 'exe 归档无法打开：%s' % type(exc).__name__))
        return
    toc = list(getattr(reader, 'toc', {}) or {})
    names = [name for name in toc if str(name).lower().endswith('.pyz')]
    if not names:
        findings.append((label, '未找到内嵌 PYZ 归档'))
        return
    for name in names:
        try:
            blob = reader.extract(name)
        except Exception as exc:
            findings.append((label, 'PYZ 提取失败：%s' % type(exc).__name__))
            continue
        handle = tempfile.NamedTemporaryFile(suffix='.pyz', delete=False)
        try:
            handle.write(blob)
            handle.close()
            _scan_pyz(handle.name, findings, label, needles)
        finally:
            try:
                os.unlink(handle.name)
            except OSError:
                pass


def load_secret_file(raw):
    """GUGA_BUILD_SECRETS 指向的真实 Key 文件：JSON 取敏感字段，纯文本按行。"""
    value = str(raw or '').strip()
    if not value:
        return set()
    path = Path(value).expanduser()
    if not path.is_file():
        raise SystemExit('GUGA_BUILD_SECRETS 指向的文件不存在：%s' % path)
    text = path.read_text(encoding='utf-8-sig', errors='ignore')
    try:
        data = json.loads(text)
        found = collect_secrets(data)
        if found:
            return found
    except ValueError:
        pass
    return {line.strip() for line in text.splitlines()
            if len(line.strip()) >= 8 and not line.strip().startswith('#')}


def is_sensitive(name) -> bool:
    key = str(name or '').lower()
    return key in _SENSITIVE_EXACT or key.endswith(_SENSITIVE_SUFFIX)


def _is_placeholder(value) -> bool:
    text = str(value or '').strip()
    if not text:
        return True
    # 真实 Key 全是 ASCII；文档里的中文占位（"你的对话 API Key"）不算凭据。
    if any('\u4e00' <= char <= '\u9fff' for char in text):
        return True
    upper = text.upper()
    if upper.startswith('YOUR') or text.startswith('<') or '***' in text:
        return True
    if upper in {'REDACTED', 'CHANGEME', 'CHANGE_ME', 'PLACEHOLDER'}:
        return True
    # xxxxxxxx / 00000000 之类的占位串
    return set(text) <= {'*', '.', 'x', 'X', '0', '-'}


def sanitize_config(value):
    """递归把敏感字段置空；结构保留，避免运行时读配置时 KeyError。"""
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                clean[key] = sanitize_config(item)
            elif is_sensitive(key) and not _is_placeholder(item):
                clean[key] = ''
            else:
                clean[key] = item
        return clean
    if isinstance(value, list):
        return [sanitize_config(item) for item in value]
    return value


def collect_secrets(value, found=None):
    """收集配置里所有非空、非占位的敏感值（只用于比对，不打印）。"""
    found = set() if found is None else found
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                collect_secrets(item, found)
            elif is_sensitive(key) and not _is_placeholder(item):
                found.add(str(item).strip())
    elif isinstance(value, list):
        for item in value:
            collect_secrets(item, found)
    return found


def _chunks(path):
    with path.open('rb') as handle:
        prev = b''
        while True:
            block = handle.read(_CHUNK)
            if not block:
                if prev:
                    yield prev
                return
            yield prev + block
            prev = block[-_OVERLAP:]


def _entry_suffix(name):
    return Path(str(name).replace('\\', '/')).suffix.lower()


def _scan_directory(root, needles, findings, stats):
    for path in Path(root).rglob('*'):
        if not path.is_file():
            continue
        stats['entries'] += 1
        label = str(path)
        if is_live_config(path.name):
            findings.append((label, '发布包内禁止出现非示例配置（ai_config*.json）'))
            continue
        suffix = path.suffix.lower()
        if suffix == '.pyc':
            try:
                _scan_code_blob(path.read_bytes(), findings, label, needles)
            except OSError:
                pass
        elif suffix == '.pyz':
            _scan_pyz(path, findings, label, needles)
        if path.name.lower() == 'guga-desktop.exe':
            _scan_exe_pyz(path, findings, label, needles)
        text_like = suffix in _TEXT_SUFFIXES
        try:
            for block in _chunks(path):
                if any(needle in block for needle in needles):
                    findings.append((label, '发现本机配置中的真实凭据'))
                    break
                if text_like:
                    reasons = _pattern_reasons(block)
                    if reasons:
                        findings.append((label, reasons[0]))
                        break
        except OSError:
            continue


def _scan_blob_via_temp(blob, suffix, scanner, findings, label, needles=()):
    """zip 条目没有磁盘路径：落一个临时文件交给解包器，用完即删。"""
    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        handle.write(blob)
        handle.close()
        scanner(handle.name, findings, label, needles)
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass


def _scan_zip(archive, needles, findings, stats):
    try:
        handle = zipfile.ZipFile(archive)
    except (OSError, zipfile.BadZipFile) as exc:
        findings.append((str(archive), 'zip 无法打开：%s' % type(exc).__name__))
        return
    with handle:
        for info in handle.infolist():
            if info.is_dir():
                continue
            stats['entries'] += 1
            name = info.filename
            if is_live_config(name):
                findings.append((name, '发布包内禁止出现非示例配置（ai_config*.json）'))
                continue
            suffix = _entry_suffix(name)
            try:
                data = handle.read(info)
            except (OSError, RuntimeError, zipfile.BadZipFile):
                continue
            if suffix == '.pyc':
                _scan_code_blob(data, findings, name, needles)
            elif suffix == '.pyz':
                _scan_blob_via_temp(data, '.pyz', _scan_pyz, findings, name, needles)
            elif name.lower().endswith('guga-desktop.exe'):
                _scan_blob_via_temp(data, '.exe', _scan_exe_pyz, findings, name, needles)
            if any(needle in data for needle in needles):
                findings.append((name, '发现本机配置中的真实凭据'))
                continue
            if suffix in _TEXT_SUFFIXES:
                reasons = _pattern_reasons(data)
                if reasons:
                    findings.append((name, reasons[0]))


def scan_all(paths, secrets=()):
    """目录与 zip 逐条目扫描；PAK/PYZ/pyc 解包后扫常量。返回结构化结果。"""
    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    if isinstance(secrets, str):
        secrets = [secrets]
    needles = [str(s).strip().encode() for s in secrets
               if str(s).strip() and len(str(s).strip()) >= 8]
    findings = []
    stats = {'entries': 0, 'needle_count': len(needles)}
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            _scan_directory(path, needles, findings, stats)
        elif path.is_file() and path.suffix.lower() == '.zip':
            _scan_zip(path, needles, findings, stats)
        elif path.is_file():
            label = str(path)
            if is_live_config(path.name):
                findings.append((label, '发布包内禁止出现非示例配置（ai_config*.json）'))
                continue
            stats['entries'] += 1
            try:
                for block in _chunks(path):
                    if any(needle in block for needle in needles):
                        findings.append((label, '发现本机配置中的真实凭据'))
                        break
                    if path.suffix.lower() in _TEXT_SUFFIXES:
                        reasons = _pattern_reasons(block)
                        if reasons:
                            findings.append((label, reasons[0]))
                            break
            except OSError:
                continue
    return dict(
        checked_at=__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        ok=not findings,
        paths=[str(p) for p in paths],
        entries=stats['entries'],
        needle_count=stats['needle_count'],
        findings=[{'path': str(path), 'reason': reason} for path, reason in findings],
    )


def scan(paths, secrets=()):
    """兼容旧调用：返回 [(路径, 原因)]。"""
    return [(item['path'], item['reason']) for item in scan_all(paths, secrets)['findings']]


def assert_clean(paths, secrets=(), purge=True, result=None):
    """扫描通过才返回；发现泄露则删除产物并中止构建。失败必须抛错，不能只警告。"""
    data = scan_all(paths, secrets)
    if result is not None:
        result.update(data)
    if data['ok']:
        print('[release] 凭据扫描通过：%d 个条目 / %d 个比对针，未发现真实 Key'
              % (data['entries'], data['needle_count']))
        return data
    print('[release] 凭据扫描失败，发现 %d 处问题：' % len(data['findings']))
    for item in data['findings'][:12]:
        print('  - %s：%s' % (item['path'], item['reason']))
    if len(data['findings']) > 12:
        print('  ...（其余 %d 处省略）' % (len(data['findings']) - 12))
    if purge:
        for raw in paths:
            path = Path(raw)
            try:
                shutil.rmtree(path) if path.is_dir() else path.unlink()
                print('[release] 已删除带凭据的产物：%s' % path)
            except OSError as exc:
                print('[release] 删除产物失败（%s），请手工删除后再发布' % type(exc).__name__)
    raise SystemExit('发布包包含真实凭据或非示例配置，构建已中止')
