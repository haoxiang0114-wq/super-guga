# -*- coding: utf-8 -*-
"""声明式软件适配注册表：一个软件一份 JSON，声明“怎么找、怎么起、怎么算就绪”。

与 ``core/local_tools.py`` 的区别：
- ``local_tools`` 的 APPS 是写死在代码里的能力表，探测靠 exe_globs + 开始菜单；
- 本模块把安装探测、启动参数、就绪判据、界面锚点、回执方式都搬进声明文件，
  启动时一次性加载并缓存探测结果；命中后直接给出 exe 路径，不再每次扫盘。

设计约束（对应真实用户环境）：
- 非默认盘：注册表 InstallLocation / DisplayIcon 与开始菜单快捷方式都能指向任意盘；
- 绿色版：用户指定路径（runtime/app_registry_user.json）、环境变量、受限深度目录扫描；
- 普通用户无管理员权限：注册表只读，读不到（Access Denied）时记 error，不假装“未安装”；
- 装在 Program Files 且不可写：本模块只读探测，不需要写目标目录；
- 首次运行未登录：声明里的 login 信号 + hint 会随探测/启动结果返回，供上层提示。

状态语义（不允许静默失败）：
- ready            已定位到真实存在的 exe
- not_installed    声明的所有来源都检查成功且均未命中
- unknown          有来源检查失败/被拒绝/超预算，无法断言未安装
- invalid_declaration  声明本身不合法
- unknown_app      没有对应声明

告警：本模块只做只读探测与显式启动，不使用像素坐标；启动默认 SW_SHOWMINNOACTIVE，
不抢用户前台。真实启动一次仅用于验证，不做后台反复拉起。
"""
from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from core import guga_paths
from core.adapters import app_reports

CONTRACT = 'guga.app_registry/1'
SCHEMA = 'guga.app_adapter/1'
DECLARATION_DIRNAME = 'apps'
APP_HOME_FOLDER = 'app_adapters'
DEFAULT_CACHE_TTL_READY = 6 * 3600
DEFAULT_CACHE_TTL_MISS = 600
DEFAULT_CACHE_TTL_UNKNOWN = 120

SOURCE_LABELS = {
    'user_override': '用户指定路径',
    'mcp_report': 'MCP 自报路径',
    'env': '环境变量',
    'common_path': '常见安装路径',
    'registry': '注册表卸载项',
    'app_paths': '注册表 App Paths',
    'start_menu': '开始菜单/桌面快捷方式',
    'path': '系统 PATH',
    'running_process': '正在运行的进程',
    'portable': '绿色版/自定义目录',
}

_UNINSTALL_KEY = r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'
_APP_PATHS_KEY = r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths'
_DEFAULT_LOCATION_VALUES = ('InstallLocation', 'InstallDir', 'InstallPath', 'DisplayIcon')
# 绿色版扫描只用于“常规来源全都没命中”的兜底，跳过系统/缓存类目录避免拖慢启动。
_SKIP_DIR_NAMES = {'windows', '$recycle.bin', 'system volume information', 'node_modules',
                   '.git', 'appdata', 'programdata', 'windows.old', 'recovery', 'perflogs',
                   '__pycache__', '.venv', 'venv', 'temp', 'tmp'}

_DEFAULTS = {
    'schema': SCHEMA,
    'aliases': [],
    'capabilities': [],
    'automation': 'ui_only',
    'adapter': '',
    'notes': '',
    'process_names': [],
    'install': {
        'user_override': True,
        'env': [],
        'common_paths': [],
        'registry': {
            'uninstall': [],
            'app_paths': [],
            'location_values': list(_DEFAULT_LOCATION_VALUES),
        },
        'start_menu': {'aliases': [], 'roots': []},
        'portable': {'roots': [], 'name_globs': [], 'depth': 2,
                     'budget_ms': 1200, 'max_entries': 600},
        'path_lookup': True,
        'detect_running': True,
    },
    'launch': {
        'args': [],
        'cwd': 'exe_dir',
        'timeout_seconds': 30,
        'show': 'min_no_activate',
        'single_instance': True,
    },
    'ready': {
        'window': {'title_keywords': [], 'process': '', 'class_keywords': []},
        'min_width': 280,
        'min_height': 180,
        'extra_settle_seconds': 0,
        'input_anchor': {'must_exist': False},
    },
    'anchors': {
        'input': {},
        'send': {},
        'done': {},
        'failure_codes': [],
    },
    'receipt': {'mode': 'ui_clipboard', 'notes': ''},
    'login': {'required': False, 'signals': [], 'hint': ''},
    'channel': {
        'primary': '', 'direction': '', 'transport': '', 'registration': '',
        'scope': '', 'server_name': 'guga', 'config_path': '', 'config_paths': [],
        'approval_file': '', 'approval_scheme': '',
        'config_format': {}, 'fallback': 'ui-auto', 'verified': {},
        'ui_steps': [], 'notes': '',
    },
    'download': {'url': '', 'hint': ''},
}

_LOCK = threading.RLock()
_DECL_CACHE = {'signature': None, 'decls': {}, 'invalid': {}, 'sources': {}}
_CACHE_MEM = {'signature': None, 'entries': {}}
_UNKNOWN_APP = dict(status='unknown_app', installed=None, exe='', ok=False)


# ---------------------------------------------------------------------------
# 声明文件位置与加载
# ---------------------------------------------------------------------------

def declaration_dirs():
    """声明目录：环境变量覆盖 > 源码/打包目录 > 用户可写目录。

    ``GUGA_APP_ADAPTERS`` 可指向一个目录，用于测试模拟或多实例；同 id 先到先得。
    """
    folders = []
    override = os.environ.get('GUGA_APP_ADAPTERS', '').strip()
    if override:
        folders.append(Path(override).expanduser())
    here = Path(__file__).resolve().parent / DECLARATION_DIRNAME
    folders.append(here)
    if getattr(sys, 'frozen', False):
        meipass = getattr(sys, '_MEIPASS', '')
        if meipass:
            folders.append(Path(meipass) / 'core' / 'adapters' / DECLARATION_DIRNAME)
            folders.append(Path(meipass) / DECLARATION_DIRNAME)
    folders.append(guga_paths.CONFIG.parent / APP_HOME_FOLDER)
    result = []
    seen = set()
    for folder in folders:
        try:
            key = str(folder.resolve()).casefold()
            if key in seen or not folder.is_dir():
                continue
            seen.add(key)
            result.append(folder)
        except Exception:
            continue
    return result


def _declaration_files():
    files = []
    seen_names = set()
    for folder in declaration_dirs():
        try:
            candidates = sorted(folder.glob('*.json'))
        except OSError:
            continue
        for path in candidates:
            if path.name.startswith('_'):
                continue
            if path.name.casefold() in seen_names:
                continue
            seen_names.add(path.name.casefold())
            files.append(path)
    return files


def _user_override_path():
    override = os.environ.get('GUGA_APP_REGISTRY_USER', '').strip()
    if override:
        return Path(override).expanduser()
    return guga_paths.RUNTIME / 'app_registry_user.json'


def _signature():
    """声明 + 用户覆盖的轻量指纹；文件增删改都会让缓存失效。"""
    parts = []
    for path in _declaration_files():
        try:
            stat = path.stat()
            parts.append([str(path), stat.st_size, stat.st_mtime_ns])
        except OSError:
            parts.append([str(path), -1, -1])
    user = _user_override_path()
    try:
        stat = user.stat()
        parts.append([str(user), stat.st_size, stat.st_mtime_ns])
    except OSError:
        parts.append([str(user), 0, 0])
    parts.append(['mcp_report', app_reports.signature()])
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:20]


def _deep_merge(base, extra):
    result = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def validate(raw):
    """返回声明问题列表；空列表表示可用。"""
    errors = []
    if not isinstance(raw, dict):
        return ['声明必须是 JSON 对象']
    if str(raw.get('schema') or '') != SCHEMA:
        errors.append('schema 必须是 %s' % SCHEMA)
    if not str(raw.get('id') or '').strip():
        errors.append('缺少 id')
    if not str(raw.get('name') or '').strip():
        errors.append('缺少 name（显示名）')
    capabilities = raw.get('capabilities')
    if not isinstance(capabilities, list) or not capabilities:
        errors.append('capabilities 必须是非空数组（能力标签）')

    install = raw.get('install')
    if not isinstance(install, dict):
        errors.append('缺少 install（安装探测规则）')
    else:
        sources = []
        if install.get('common_paths'):
            sources.append('common_paths')
        if install.get('env'):
            sources.append('env')
        registry = install.get('registry') or {}
        if registry.get('uninstall') or registry.get('app_paths'):
            sources.append('registry')
        menu = install.get('start_menu') or {}
        if menu.get('aliases'):
            sources.append('start_menu')
        portable = install.get('portable') or {}
        if portable.get('roots'):
            sources.append('portable')
        if not sources:
            errors.append('install 至少要声明一种探测来源：registry / common_paths / '
                          'start_menu / portable / env')

    launch = raw.get('launch')
    if not isinstance(launch, dict) or not str(launch.get('exe_name') or '').strip():
        errors.append('launch.exe_name 必须写明可执行文件名')

    ready = raw.get('ready') or {}
    window = ready.get('window') or {}
    if not window.get('title_keywords') and not (window.get('process') or raw.get('process_names')):
        errors.append('ready.window 需要 title_keywords 或 process 之一')

    anchors = raw.get('anchors') or {}
    for key, label in (('input', '输入框锚点'), ('send', '发送动作'), ('done', '完成判据')):
        if not isinstance(anchors.get(key), dict) or not anchors.get(key):
            errors.append('anchors.%s 必须写明%s' % (key, label))
    if not isinstance(anchors.get('failure_codes'), list):
        errors.append('anchors.failure_codes 必须是数组')

    receipt = raw.get('receipt') or {}
    if not str(receipt.get('mode') or '').strip():
        errors.append('receipt.mode 必须写明回执/产物获取方式')

    channel = raw.get('channel')
    if channel is not None:
        if not isinstance(channel, dict):
            errors.append('channel 必须是对象')
        else:
            primary = str(channel.get('primary') or '')
            if primary not in ('mcp', 'ui-auto'):
                errors.append('channel.primary 只能是 mcp 或 ui-auto')
            direction = str(channel.get('direction') or '')
            if direction not in ('guga_server', 'guga_client', ''):
                errors.append('channel.direction 只能是 guga_server / guga_client')
            registration = str(channel.get('registration') or '')
            if registration not in ('file', 'ui', 'ui_json', ''):
                errors.append('channel.registration 只能是 file / ui / ui_json')
            if registration in ('ui', 'ui_json') and channel.get('config_path'):
                errors.append('channel.registration=%s 时不要写 config_path（官方未公布文件路径）'
                              % registration)
            if str(channel.get('fallback') or 'ui-auto') not in ('ui-auto', 'script', 'none'):
                errors.append('channel.fallback 只能是 ui-auto / script / none')
    return errors


def _load_declarations(force=False):
    signature = _signature()
    with _LOCK:
        if not force and _DECL_CACHE['signature'] == signature:
            return dict(_DECL_CACHE['decls'])
        decls = {}
        invalid = {}
        sources = {}
        for path in _declaration_files():
            try:
                raw = json.loads(path.read_text(encoding='utf-8-sig'))
            except (OSError, ValueError) as exc:
                stem = path.stem
                invalid[stem] = dict(id=stem, source=str(path),
                                     errors=['JSON 读取失败: %s' % str(exc)[:160]])
                continue
            errors = validate(raw)
            app_id = str((raw or {}).get('id') or path.stem).strip().casefold()
            if errors:
                invalid[app_id] = dict(id=app_id, source=str(path), errors=errors)
                continue
            decl = _deep_merge(_DEFAULTS, raw)
            decl['id'] = app_id
            if not decl.get('process_names'):
                decl['process_names'] = [decl['launch']['exe_name']]
            if not decl['install']['start_menu'].get('aliases'):
                decl['install']['start_menu']['aliases'] = list(
                    decl.get('aliases') or [decl['name']])
            if not decl['ready']['window'].get('title_keywords'):
                decl['ready']['window']['title_keywords'] = list(
                    decl.get('aliases') or [decl['name']])
            if not decl['ready']['window'].get('process'):
                decl['ready']['window']['process'] = decl['process_names'][0]
            decls[app_id] = decl
            sources[app_id] = str(path)
        _DECL_CACHE.update(signature=signature, decls=decls, invalid=invalid, sources=sources)
        _CACHE_MEM.update(signature=signature, entries={})
        return dict(decls)


def declarations(force=False):
    return _load_declarations(force=force)


def declaration(app_id):
    return _load_declarations().get(str(app_id or '').strip().casefold())


def declaration_errors():
    _load_declarations()
    return copy.deepcopy(_DECL_CACHE['invalid'])


def app_ids():
    return sorted(_load_declarations().keys())


def declaration_path(app_id):
    _load_declarations()
    return _DECL_CACHE['sources'].get(str(app_id or '').strip().casefold(), '')


# ---------------------------------------------------------------------------
# 用户覆盖（绿色版 / 异盘 / 装在特殊目录）
# ---------------------------------------------------------------------------

def _read_user_overrides():
    path = _user_override_path()
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _override_for(app_id):
    item = _read_user_overrides().get(str(app_id or '').strip().casefold())
    return item if isinstance(item, dict) else None


def set_override(app_id, exe, note=''):
    """显式指定某软件的 exe（绿色版/异盘）；只写用户目录，不碰声明文件。"""
    app_id = str(app_id or '').strip().casefold()
    path = _user_override_path()
    data = _read_user_overrides()
    data[app_id] = dict(exe=str(exe), note=str(note or ''), updated_at=time.strftime('%Y-%m-%dT%H:%M:%S'))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)
    invalidate()
    return dict(ok=True, path=str(path), app=app_id, exe=str(exe))


def clear_override(app_id):
    app_id = str(app_id or '').strip().casefold()
    path = _user_override_path()
    data = _read_user_overrides()
    existed = data.pop(app_id, None) is not None
    try:
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
    except OSError as exc:
        return dict(ok=False, error='write_failed', message=str(exc)[:160], app=app_id)
    invalidate()
    return dict(ok=True, app=app_id, removed=existed, path=str(path))


# ---------------------------------------------------------------------------
# 探测缓存
# ---------------------------------------------------------------------------

def cache_path():
    override = os.environ.get('GUGA_APP_REGISTRY_CACHE', '').strip()
    if override:
        return Path(override).expanduser()
    return guga_paths.RUNTIME / 'app_registry.json'


def _read_cache_file():
    path = cache_path()
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get('contract') != CONTRACT:
        return {}
    if data.get('signature') != _signature():
        return {}
    entries = data.get('entries')
    return entries if isinstance(entries, dict) else {}


def _cache_entries():
    with _LOCK:
        signature = _signature()
        if _CACHE_MEM['signature'] == signature:
            return dict(_CACHE_MEM['entries'])
        entries = _read_cache_file()
        _CACHE_MEM.update(signature=signature, entries=entries)
        return dict(entries)


def _write_cache_entries(entries):
    path = cache_path()
    payload = dict(contract=CONTRACT, signature=_signature(),
                   updated_at=time.strftime('%Y-%m-%dT%H:%M:%S'), entries=entries)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
    except OSError:
        # 只读环境（Program Files / 受限目录）下缓存写不进去也不能崩，探测结果照常返回。
        pass


def _cache_put(result):
    app_id = result.get('id')
    if not app_id or result.get('cached'):
        return
    with _LOCK:
        entries = _cache_entries()
        entry = {key: value for key, value in result.items() if key != 'cached'}
        entry['probed_at_epoch'] = time.time()
        entries[app_id] = entry
        _CACHE_MEM.update(signature=_signature(), entries=dict(entries))
        _write_cache_entries(entries)


def _cache_get(app_id):
    entry = _cache_entries().get(app_id)
    if not isinstance(entry, dict):
        return None
    status = str(entry.get('status') or '')
    if status not in ('ready', 'not_installed', 'unknown'):
        return None
    age = time.time() - float(entry.get('probed_at_epoch') or 0)
    if status == 'ready':
        exe = str(entry.get('exe') or '')
        try:
            if exe and Path(exe).is_file():
                item = copy.deepcopy(entry)
                item.update(cached=True, elapsed_ms=0)
                return item
        except OSError:
            pass
        return None
    ttl_default = DEFAULT_CACHE_TTL_MISS if status == 'not_installed' else DEFAULT_CACHE_TTL_UNKNOWN
    ttl = float(entry.get('cache_ttl_seconds') or ttl_default)
    if age <= ttl:
        item = copy.deepcopy(entry)
        item.update(cached=True, elapsed_ms=0)
        return item
    return None


def invalidate():
    """清掉探测缓存；下次 probe 会重新只读探测。"""
    with _LOCK:
        _CACHE_MEM.update(signature=_signature(), entries={})
        try:
            cache_path().unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 探测基础工具
# ---------------------------------------------------------------------------

def _expand(value):
    text = str(value or '').strip().strip('"').strip("'")
    if not text:
        return None
    text = os.path.expanduser(os.path.expandvars(text))
    if re.search(r'%[^%]+%', text):
        return None
    return Path(text)


def _is_exe(path):
    if not path:
        return False
    try:
        return Path(path).suffix.lower() == '.exe' and Path(path).is_file()
    except OSError:
        return False


def _exe_from_value(value, exe_name):
    """把 InstallLocation / DisplayIcon / App Paths 值换算成真实 exe。"""
    text = str(value or '').strip().strip('"').strip()
    if not text:
        return None
    text = re.sub(r',\s*-?\d+\s*$', '', text)      # DisplayIcon 常见 "xxx.exe,0"
    path = _expand(text)
    if path is None:
        return None
    suffix = path.suffix.lower()
    if suffix == '.exe':
        return path if _is_exe(path) else None
    # 无扩展名按目录处理（InstallLocation 常见 "D:\JoyCode\"）；图标/图片取父目录。
    base = path if suffix == '' else path.parent
    candidate = base / exe_name
    return candidate if _is_exe(candidate) else None


def _flatten_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _fold_labels(source):
    return SOURCE_LABELS.get(source, source)


# ---------------------------------------------------------------------------
# 注册表（只读）
# ---------------------------------------------------------------------------

def _winreg():
    try:
        import winreg
        return winreg
    except ImportError:
        return None


def _hive(name):
    winreg = _winreg()
    if winreg is None:
        return None
    return {'HKCU': winreg.HKEY_CURRENT_USER, 'HKLM': winreg.HKEY_LOCAL_MACHINE}.get(
        str(name or '').upper())


def _view_flags(view):
    winreg = _winreg()
    if winreg is None:
        return 0
    if str(view) == '32':
        return getattr(winreg, 'KEY_WOW64_32KEY', 0)
    return getattr(winreg, 'KEY_WOW64_64KEY', 0)


def _views(rule):
    view = str(rule.get('view') or 'any').lower()
    if view in ('32', '64'):
        return (view,)
    return ('64', '32')


def _reg_open(hive, subkey, view):
    winreg = _winreg()
    return winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | _view_flags(view))


def _reg_value(key, name):
    winreg = _winreg()
    try:
        if name is None:
            return str(winreg.QueryValue(key, None) or '')
        value, _ = winreg.QueryValueEx(key, name)
        return str(value or '')
    except (OSError, ValueError):
        return ''


def _display_match(display, contains, equals):
    text = str(display or '').casefold().strip()
    if not text:
        return False
    if equals and text in equals:
        return True
    return bool(contains) and any(word in text for word in contains)


def _probe_registry(decl, exe_name, add_hit, errors, incomplete, tried):
    winreg = _winreg()
    if winreg is None:
        incomplete.append('registry')
        errors.append(dict(source='registry', code='winreg_unavailable',
                           detail='当前 Python 没有 winreg 模块，无法读取注册表'))
        return
    cfg = decl['install']['registry'] or {}
    rules = _flatten_list(cfg.get('uninstall'))
    app_paths = [str(x) for x in _flatten_list(cfg.get('app_paths')) if str(x).strip()]
    if not rules and not app_paths:
        return
    tried.append('registry')

    for rule in rules:
        if not isinstance(rule, dict):
            continue
        hive_name = str(rule.get('hive') or 'HKCU').upper()
        hive = _hive(hive_name)
        if hive is None:
            errors.append(dict(source='registry', code='unknown_hive', detail=hive_name))
            incomplete.append('registry')
            continue
        subkey = str(rule.get('key') or _UNINSTALL_KEY)
        contains = [x.casefold() for x in _flatten_list(rule.get('display_name_contains')) if str(x)]
        equals = [x.casefold() for x in _flatten_list(rule.get('display_name_equals')) if str(x)]
        if not contains and not equals:
            continue
        for view in _views(rule):
            opened = False
            try:
                root = _reg_open(hive, subkey, view)
                opened = True
            except FileNotFoundError:
                # 卸载项根键不存在是正常情况（某些精简系统），不算探测失败。
                continue
            except PermissionError as exc:
                errors.append(dict(source='registry', code='registry_denied',
                                   detail='%s\\%s 读取被拒绝（普通用户/组策略限制）：%s'
                                          % (hive_name, subkey, str(exc)[:120])))
                incomplete.append('registry')
                continue
            except OSError as exc:
                errors.append(dict(source='registry', code='registry_error',
                                   detail='%s\\%s：%s' % (hive_name, subkey, str(exc)[:120])))
                incomplete.append('registry')
                continue
            if not opened:
                continue
            index = 0
            enumerated = 0
            with root:
                while True:
                    try:
                        child_name = winreg.EnumKey(root, index)
                        index += 1
                    except OSError:
                        break
                    enumerated += 1
                    try:
                        child = _reg_open(hive, subkey + '\\' + child_name, view)
                    except (FileNotFoundError, PermissionError):
                        continue
                    except OSError:
                        continue
                    with child:
                        display = _reg_value(child, 'DisplayName')
                        if not _display_match(display, contains, equals):
                            continue
                        for value_name in cfg.get('location_values') or _DEFAULT_LOCATION_VALUES:
                            raw = _reg_value(child, value_name)
                            if not raw:
                                continue
                            candidate = _exe_from_value(raw, exe_name)
                            if candidate:
                                add_hit('registry', candidate,
                                        '%s\\%s : %s' % (hive_name, value_name, display))
                                return
            if enumerated == 0:
                # 根键能打开但没有子项：这是真实的“查过了、没有”，不是失败。
                continue

    for exe in app_paths:
        for hive_name in ('HKCU', 'HKLM'):
            hive = _hive(hive_name)
            if hive is None:
                continue
            for view in ('64', '32'):
                try:
                    key = _reg_open(hive, _APP_PATHS_KEY + '\\' + exe, view)
                except FileNotFoundError:
                    continue
                except PermissionError as exc:
                    errors.append(dict(source='app_paths', code='registry_denied',
                                       detail='%s\\App Paths\\%s：%s'
                                              % (hive_name, exe, str(exc)[:120])))
                    incomplete.append('app_paths')
                    continue
                except OSError:
                    continue
                with key:
                    for value_name in (None, 'Path', 'Executable'):
                        raw = _reg_value(key, value_name)
                        candidate = _exe_from_value(raw, exe)
                        if candidate:
                            add_hit('app_paths', candidate,
                                    '%s\\App Paths\\%s' % (hive_name, exe))
                            return


# ---------------------------------------------------------------------------
# 开始菜单 / 桌面快捷方式
# ---------------------------------------------------------------------------

def _start_menu_roots(config):
    roots = []
    for item in _flatten_list(config.get('roots')):
        path = _expand(item)
        if path:
            roots.append((path, True))
    if not roots:
        appdata = _expand('%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs')
        programdata = _expand('%PROGRAMDATA%\\Microsoft\\Windows\\Start Menu\\Programs')
        desktop = _expand('%USERPROFILE%\\Desktop')
        public_desktop = _expand('%PUBLIC%\\Desktop')
        if appdata:
            roots.append((appdata, True))
        if programdata:
            roots.append((programdata, True))
        if desktop:
            roots.append((desktop, False))
        if public_desktop:
            roots.append((public_desktop, False))
    return roots


def _escape_ps(text):
    return str(text).replace("'", "''")


def _resolve_shortcuts_powershell(paths):
    quoted = ','.join("'%s'" % _escape_ps(path) for path in paths)
    script = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "$sh=New-Object -ComObject WScript.Shell;$r=@();"
        "foreach($p in @(%s)){try{$t=$sh.CreateShortcut($p).TargetPath;"
        "if($t){$r+=@{lnk=$p;target=$t}}}catch{}};"
        "ConvertTo-Json -Compress -InputObject @($r)" % quoted
    )
    try:
        completed = subprocess.run(['powershell', '-NoProfile', '-NonInteractive',
                                    '-ExecutionPolicy', 'Bypass', '-Command', script],
                                   capture_output=True, text=True, encoding='utf-8',
                                   timeout=25,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or not (completed.stdout or '').strip():
        return None
    try:
        data = json.loads(completed.stdout.strip())
    except ValueError:
        return None
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return None
    result = {}
    for item in data:
        if isinstance(item, dict) and item.get('lnk'):
            result[str(item['lnk'])] = str(item.get('target') or '')
    return result


def _resolve_shortcuts(paths):
    """优先进程内 COM（快）；win32com 不可用时退回一次 PowerShell 批量解析。"""
    if not paths:
        return {}
    try:
        import pythoncom
        import win32com.client
    except Exception:
        return _resolve_shortcuts_powershell(paths)
    initialized = False
    try:
        pythoncom.CoInitialize()
        initialized = True
    except Exception:
        pass
    result = {}
    try:
        shell = win32com.client.Dispatch('WScript.Shell')
        for path in paths:
            try:
                target = str(shell.CreateShortcut(str(path)).TargetPath or '')
            except Exception:
                target = ''
            result[str(path)] = target
    except Exception:
        return _resolve_shortcuts_powershell(paths)
    finally:
        # 先释放 Dispatch 引用，再 CoUninitialize，避免退出时释放 IUnknown 的 Win32 警告。
        shell = None
        if initialized:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
    return result


def _probe_start_menu(decl, exe_name, add_hit, errors, incomplete):
    config = decl['install']['start_menu'] or {}
    aliases = [str(a).casefold().strip() for a in _flatten_list(config.get('aliases'))]
    if not aliases:
        return
    roots = _start_menu_roots(config)
    existing = [(path, recursive) for path, recursive in roots if path.is_dir()]
    if not existing:
        incomplete.append('start_menu')
        errors.append(dict(source='start_menu', code='no_start_menu',
                           detail='开始菜单/桌面目录都不存在，无法检查快捷方式'))
        return
    shortcuts = []
    for root, recursive in existing:
        try:
            iterator = root.rglob('*.lnk') if recursive else root.glob('*.lnk')
            for lnk in iterator:
                stem = lnk.stem.casefold()
                if any(alias in stem for alias in aliases):
                    shortcuts.append(lnk)
        except OSError as exc:
            errors.append(dict(source='start_menu', code='enumerate_failed',
                               detail='%s：%s' % (root, str(exc)[:120])))
            incomplete.append('start_menu')
    if not shortcuts:
        return
    resolved = _resolve_shortcuts([str(path) for path in shortcuts])
    if resolved is None:
        incomplete.append('start_menu')
        errors.append(dict(source='start_menu', code='resolve_failed',
                           detail='快捷方式目标解析失败（COM 与 PowerShell 均不可用）'))
        return
    for lnk, target in resolved.items():
        if not target:
            errors.append(dict(source='start_menu', code='shortcut_broken',
                               detail='%s 解析不到目标' % lnk))
            continue
        candidate = _exe_from_value(target, exe_name)
        if candidate:
            add_hit('start_menu', candidate, lnk)


# ---------------------------------------------------------------------------
# 绿色版 / 自定义目录（受限深度 + 时间预算）
# ---------------------------------------------------------------------------

def _portable_probe(decl, exe_name, add_hit, errors, incomplete):
    config = decl['install']['portable'] or {}
    roots = []
    for item in _flatten_list(config.get('roots')):
        path = _expand(item)
        if path:
            roots.append(path)
    if not roots:
        return 0
    patterns = [str(x) for x in _flatten_list(config.get('name_globs')) if str(x)]
    if not patterns:
        patterns = [exe_name]
    depth = max(1, min(int(config.get('depth') or 2), 4))
    budget_ms = max(200, int(config.get('budget_ms') or 1200))
    max_entries = max(50, int(config.get('max_entries') or 600))
    deadline = time.monotonic() + budget_ms / 1000.0
    scanned = 0
    hits = []
    timed_out = False
    for root in roots:
        if not root.is_dir():
            continue
        base_parts = len(root.parts)
        try:
            walker = os.walk(str(root), topdown=True, followlinks=False)
        except OSError:
            continue
        for dirpath, dirnames, filenames in walker:
            level = len(Path(dirpath).parts) - base_parts
            if level >= depth:
                dirnames[:] = []
            dirnames[:] = [name for name in dirnames
                           if name.casefold() not in _SKIP_DIR_NAMES]
            scanned += 1
            if scanned > max_entries or time.monotonic() > deadline:
                timed_out = True
                break
            for name in filenames:
                if any(_match_glob(name, pattern) for pattern in patterns):
                    path = Path(dirpath) / name
                    try:
                        if path.is_file():
                            hits.append(path)
                    except OSError:
                        continue
        if timed_out:
            break
    for path in sorted(hits, key=lambda p: (len(p.parts), str(p).casefold())):
        add_hit('portable', path, '绿色版目录扫描（已扫描 %d 个目录）' % scanned)
        return scanned
    if timed_out:
        incomplete.append('portable')
        errors.append(dict(source='portable', code='budget_exceeded',
                           detail='绿色版目录扫描超出预算（已扫描 %d 个目录，可能仍有未检查目录）'
                                  % scanned))
    return scanned


def _match_glob(name, pattern):
    text = str(name or '')
    return fnmatch.fnmatch(text.casefold(), str(pattern).casefold())


# ---------------------------------------------------------------------------
# 运行中的进程
# ---------------------------------------------------------------------------

def _process_names(decl):
    names = [str(n).casefold() for n in _flatten_list(decl.get('process_names')) if str(n)]
    exe_name = str(decl['launch'].get('exe_name') or '')
    if exe_name:
        names.append(exe_name.casefold())
    return set(names)


def _running_processes(decl):
    names = _process_names(decl)
    result = []
    try:
        import psutil
    except Exception:
        return result
    try:
        iterator = psutil.process_iter(['pid', 'name', 'exe'])
    except Exception:
        return result
    for proc in iterator:
        try:
            info = proc.info
            if str(info.get('name') or '').casefold() not in names:
                continue
            result.append(dict(pid=int(info.get('pid')), exe=str(info.get('exe') or '')))
        except (psutil.Error, OSError, ValueError):
            continue
    return result


def _windows_snapshot():
    try:
        import psutil
        import win32gui
        import win32process
    except ImportError:
        return []
    windows = []
    process_cache = {}

    def visit(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
            if not title:
                return
            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
            if pid not in process_cache:
                name = ''
                try:
                    name = psutil.Process(pid).name()
                except Exception:
                    pass
                process_cache[pid] = name
            try:
                rect = win32gui.GetWindowRect(hwnd)
            except OSError:
                rect = (0, 0, 0, 0)
            windows.append(dict(hwnd=hwnd, title=title, pid=pid,
                                process=process_cache.get(pid, ''),
                                class_name=win32gui.GetClassName(hwnd),
                                minimized=bool(win32gui.IsIconic(hwnd)),
                                rect=rect))
        except Exception:
            return
    try:
        win32gui.EnumWindows(visit, None)
    except Exception:
        return []
    return windows


def _window_config(decl):
    return decl['ready']['window'] or {}


def _window_matches(window, decl, pid=None):
    if pid and int(window.get('pid') or 0) != int(pid):
        return False
    cfg = _window_config(decl)
    keywords = [str(k).casefold() for k in _flatten_list(cfg.get('title_keywords')) if str(k)]
    processes = [str(p).casefold() for p in
                 (_flatten_list(cfg.get('process')) + list(decl.get('process_names') or []))
                 if str(p)]
    class_keywords = [str(k).casefold() for k in _flatten_list(cfg.get('class_keywords')) if str(k)]
    title = str(window.get('title') or '').casefold()
    process = str(window.get('process') or '').casefold()
    class_name = str(window.get('class_name') or '').casefold()
    if keywords and any(word in title for word in keywords):
        return True
    if processes and process in processes:
        return True
    if class_keywords and any(word in class_name for word in class_keywords):
        return True
    return False


def _find_window(decl, pid=None, include_minimized=False):
    for window in _windows_snapshot():
        if not _window_matches(window, decl, pid=pid):
            continue
        if window.get('minimized') and not include_minimized:
            continue
        left, top, right, bottom = window.get('rect') or (0, 0, 0, 0)
        width, height = max(0, right - left), max(0, bottom - top)
        if not window.get('minimized') and width < int(decl['ready'].get('min_width') or 1):
            continue
        if not window.get('minimized') and height < int(decl['ready'].get('min_height') or 1):
            continue
        return dict(window, width=width, height=height)
    return None


def _window_texts(hwnd, limit=200, seconds=2.0):
    """只读抓窗口内可见文本（用于登录/同意页判定）；失败返回 None，不误判。"""
    try:
        from core.uia_backend import UIABackend
        snapshot = UIABackend().perceive(int(hwnd), limit=limit)
    except Exception:
        return None
    items = snapshot.get('elements') if isinstance(snapshot, dict) else None
    if not isinstance(items, list):
        return None
    return [str(item.get('name') or '') + ' ' + str(item.get('value') or '')
            for item in items if isinstance(item, dict)]


def _login_state(decl, window):
    login = decl.get('login') or {}
    if not login.get('required'):
        return None
    signals = [str(s) for s in _flatten_list(login.get('signals')) if str(s)]
    if not signals:
        return None
    title = str((window or {}).get('title') or '')
    for signal in signals:
        if signal.casefold() in title.casefold():
            return dict(kind='login_required', signal=signal, hint=str(login.get('hint') or ''),
                        evidence='window_title')
    if str(login.get('detect') or 'title_then_uia') == 'title_only':
        return None
    texts = _window_texts((window or {}).get('hwnd'))
    if not texts:
        return None
    joined = ' '.join(texts)
    for signal in signals:
        if signal.casefold() in joined.casefold():
            return dict(kind='login_required', signal=signal, hint=str(login.get('hint') or ''),
                        evidence='uia_text')
    return None


# ---------------------------------------------------------------------------
# 探测主流程
# ---------------------------------------------------------------------------

def _base_result(decl, app_id):
    return dict(
        contract=CONTRACT, id=app_id,
        name=str((decl or {}).get('name') or app_id),
        capabilities=list((decl or {}).get('capabilities') or []),
        automation=str((decl or {}).get('automation') or ''),
        adapter=str((decl or {}).get('adapter') or ''),
        channel=copy.deepcopy((decl or {}).get('channel') or {}),
        download=copy.deepcopy((decl or {}).get('download') or {}),
        declaration=declaration_path(app_id),
    )


def _probe_uncached(decl, app_id):
    started = time.monotonic()
    exe_name = str(decl['launch']['exe_name'])
    hit = {}
    errors = []
    warnings = []
    incomplete = []
    tried = []
    checked = {'common_path': 0, 'portable_entries': 0}
    allowed_names = {exe_name.casefold()} | {
        str(name).casefold() for name in _flatten_list(decl.get('process_names')) if str(name)}
    running = _running_processes(decl) if decl['install'].get('detect_running') else []

    def add_hit(source, path, detail=''):
        if hit or not _is_exe(path):
            return
        candidate = Path(path)
        # 自动来源（注册表/快捷方式等）只接受声明的可执行文件名，防止同名匹配到别的软件。
        if source in ('registry', 'app_paths', 'start_menu', 'portable', 'path') and \
                candidate.name.casefold() not in allowed_names:
            errors.append(dict(source=source, code='name_mismatch',
                               detail='命中的可执行文件与声明不一致：%s（期望 %s）'
                                      % (candidate.name, exe_name)))
            return
        hit.update(path=str(candidate), source=source, detail=str(detail))

    # ① 用户显式指定（绿色版/异盘最可靠）
    if decl['install'].get('user_override'):
        override = _override_for(app_id)
        if override:
            tried.append('user_override')
            value = str(override.get('exe') or '').strip()
            if _is_exe(value):
                add_hit('user_override', value, str(override.get('note') or ''))
            else:
                errors.append(dict(source='user_override', code='override_missing',
                                   detail='用户指定路径不存在或不是 exe：%s' % value[:200]))

    # ② MCP 自报路径（软件连上咕嘎时由 report_app 写入；只用于发现，不改变指挥方式）
    if not hit:
        report = app_reports.valid_report(app_id)
        if report.get('exists'):
            tried.append('mcp_report')
            record = report.get('record') or {}
            if report.get('valid'):
                add_hit('mcp_report', report.get('exe'),
                        '%s %s 自报于 %s' % (str(record.get('name') or app_id)[:60],
                                             str(record.get('version') or '').strip(),
                                             str(record.get('reported_at') or '')[:19]))
            else:
                warnings.append(dict(
                    source='mcp_report', code=report.get('reason') or 'report_invalid',
                    detail='自报路径已失效，继续按声明探测：%s' % report.get('message')))

    # ③ 环境变量（例如 GUGA_JOYCODE_LAUNCHER）
    for variable in _flatten_list(decl['install'].get('env')):
        variable = str(variable or '').strip()
        if not variable:
            continue
        tried.append('env:%s' % variable)
        value = os.environ.get(variable, '').strip()
        if not value:
            continue
        candidate = _exe_from_value(value, exe_name)
        if candidate:
            add_hit('env', candidate, variable)
        else:
            errors.append(dict(source='env', code='env_invalid',
                               detail='%s 指向的路径不存在或不是可执行文件：%s'
                                      % (variable, value[:200])))

    # ③ 常见安装路径（纯文件系统 stat，最快）
    if not hit:
        paths = _flatten_list(decl['install'].get('common_paths'))
        if paths:
            tried.append('common_path')
        for item in paths:
            checked['common_path'] += 1
            candidate = _exe_from_value(str(item), exe_name)
            if candidate:
                add_hit('common_path', candidate, str(item))
                break

    # ④ 注册表卸载项 + App Paths（能覆盖装在任意盘的正常安装）
    if not hit:
        _probe_registry(decl, exe_name, add_hit, errors, incomplete, tried)

    # ⑤ 开始菜单 / 桌面快捷方式（能覆盖装在任意盘的安装）
    if not hit:
        _probe_start_menu(decl, exe_name, add_hit, errors, incomplete)

    # ⑥ 系统 PATH（少见的命令行安装）
    if not hit and decl['install'].get('path_lookup'):
        tried.append('path')
        found = shutil.which(exe_name) if exe_name else None
        if found:
            add_hit('path', found, exe_name)

    # ⑦ 已在运行的进程（绿色版常从这里拿到真实 exe）
    if not hit and running:
        tried.append('running_process')
        for item in running:
            if item.get('exe') and _is_exe(item['exe']):
                add_hit('running_process', item['exe'], 'pid=%s' % item.get('pid'))
                break

    # ⑧ 绿色版/自定义目录（最慢，最后做；带时间预算）
    if not hit:
        tried.append('portable')
        checked['portable_entries'] = _portable_probe(
            decl, exe_name, add_hit, errors, incomplete)

    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    if hit:
        status = 'ready'
        installed = True
        message = '已定位 %s：%s（来源：%s）' % (
            decl.get('name'), hit['path'], _fold_labels(hit['source']))
        if hit.get('detail'):
            message += '｜证据：%s' % str(hit['detail'])[:200]
    elif errors or incomplete:
        status = 'unknown'
        installed = None
        details = '；'.join('%s：%s' % (_fold_labels(item.get('source')), item.get('detail'))
                          for item in errors[:3]) or '部分来源未能完成检查'
        message = ('无法确认 %s 是否安装（%s）。已检查的来源未命中；'
                   '可设置环境变量 GUGA_APP_REGISTRY_USER 或在用户覆盖文件里写明 exe 路径。'
                   % (decl.get('name'), details))
    else:
        status = 'not_installed'
        installed = False
        checked_labels = [_fold_labels(item) for item in
                          ('user_override', 'mcp_report', 'env', 'common_path', 'registry',
                           'start_menu', 'path', 'portable')]
        message = ('未检测到 %s。已只读检查：%s，均未命中；安装后可重新探测。'
                   % (decl.get('name'), '、'.join(checked_labels)))
        download = decl.get('download') or {}
        if download.get('url') or download.get('hint'):
            message += '下载/安装指引：%s' % (str(download.get('url') or download.get('hint'))[:240])

    if warnings:
        message += ('；已忽略 %d 条失效自报路径（%s）'
                    % (len(warnings), str(warnings[0].get('detail') or '')[:160]))

    result = _base_result(decl, app_id)
    result.update(
        ok=(status == 'ready'), status=status, installed=installed,
        exe=hit.get('path', ''), source=hit.get('source', ''),
        source_label=_fold_labels(hit['source']) if hit else '',
        detail=hit.get('detail', ''),
        running=bool(running), running_pid=(running[0]['pid'] if running else 0),
        running_exe=(running[0]['exe'] if running else ''),
        tried=tried, errors=errors, warnings=warnings, incomplete=incomplete, checked=checked,
        elapsed_ms=elapsed_ms, cached=False,
        login=dict(decl.get('login') or {}),
        probed_at=time.strftime('%Y-%m-%dT%H:%M:%S'),
        message=message,
    )
    if status == 'not_installed':
        result['cache_ttl_seconds'] = DEFAULT_CACHE_TTL_MISS
    elif status == 'unknown':
        result['cache_ttl_seconds'] = DEFAULT_CACHE_TTL_UNKNOWN
    return result


def _error_result(app_id, status, message, decl=None):
    result = _base_result(decl, app_id) if decl else dict(contract=CONTRACT, id=app_id, name=app_id)
    result.update(ok=False, status=status, installed=None, exe='', source='',
                  message=message, probed_at=time.strftime('%Y-%m-%dT%H:%M:%S'),
                  errors=[], warnings=[], incomplete=[], tried=[], elapsed_ms=0, cached=False)
    return result


def _report_declaration(app_id, report):
    """给“新增软件”手填的自报路径生成一份最小声明，让 probe / launch_exe 也能用。"""
    record = report.get('record') or {}
    exe = str(report.get('exe') or '')
    name = str(record.get('name') or app_id)
    decl = _deep_merge(copy.deepcopy(_DEFAULTS), {
        'id': app_id,
        'name': name,
        'aliases': [name],
        'capabilities': list(record.get('capabilities') or ['ui_automation']),
        'process_names': [Path(exe).name],
        'install': {
            'common_paths': [exe],
            'path_lookup': False,
            'detect_running': False,
            'registry': {'uninstall': [], 'app_paths': []},
            'start_menu': {'aliases': [], 'roots': []},
            'portable': {'roots': [], 'name_globs': [], 'depth': 0,
                         'budget_ms': 120, 'max_entries': 20},
        },
        'launch': {'exe_name': Path(exe).name},
        'ready': {'window': {'title_keywords': [name], 'process': Path(exe).name}},
        'channel': {'primary': 'ui-auto', 'direction': '', 'transport': '',
                    'registration': '', 'fallback': 'ui-auto'},
    })
    decl['id'] = app_id
    return decl


def probe(app_id, force=False, use_cache=True):
    """只读探测一个软件；命中返回 ready + 真实 exe，失败明确区分未安装/未知。"""
    app_id = str(app_id or '').strip().casefold()
    started = time.monotonic()
    decl = declaration(app_id)
    report_only = False
    if not decl:
        invalid = declaration_errors().get(app_id)
        if invalid:
            return _error_result(app_id, 'invalid_declaration',
                                 '声明不合法：%s' % '；'.join(invalid.get('errors') or []))
        report = app_reports.valid_report(app_id)
        if report.get('valid'):
            decl = _report_declaration(app_id, report)
            report_only = True
        else:
            return _error_result(app_id, 'unknown_app',
                                 '没有找到 %s 的适配声明；可用 id：%s'
                                 % (app_id, '、'.join(app_ids()) or '无'))
    if not report_only and not force and use_cache:
        cached = _cache_get(app_id)
        if cached:
            cached['elapsed_ms'] = round((time.monotonic() - started) * 1000, 1)
            return cached
    try:
        result = _probe_uncached(decl, app_id)
    except Exception as exc:
        result = _error_result(app_id, 'error', '探测异常：%s: %s'
                               % (type(exc).__name__, str(exc)[:200]), decl)
        result['elapsed_ms'] = round((time.monotonic() - started) * 1000, 1)
    _cache_put(result)
    result['elapsed_ms'] = round((time.monotonic() - started) * 1000, 1)
    return result


def resolve(app_id, force=False):
    """``probe`` 的语义化别名：给路由层直接拿 exe 路径。"""
    return probe(app_id, force=force)


def exe_path(app_id, force=False):
    result = probe(app_id, force=force)
    return str(result.get('exe') or '') if result.get('status') == 'ready' else ''


def launch_exe(app_id, force=True):
    """适配器真正拉起软件时取路径：走统一优先级（用户指定 > MCP 自报 > 声明探测）。

    与 ``exe_path`` 的区别：把 warnings 一并带回，便于适配器记录“自报路径已失效”
    后继续用自己的兜底；只有通过存在性/可执行校验的路径才会返回 ok=True。
    """
    result = probe(app_id, force=force)
    exe = str(result.get('exe') or '')
    ok = _is_exe(exe)
    return dict(ok=ok, exe=exe if ok else '',
                source=str(result.get('source') or '') if ok else '',
                status=result.get('status'), warnings=result.get('warnings') or [],
                message=str(result.get('message') or '')[:240])


def _normalize_name(value):
    """软件名归一化：忽略引号/空格/大小写，去掉 .exe 与“软件/客户端”等后缀。"""
    text = str(value or '').strip().casefold()
    for junk in ('"', "'", '“', '”', '‘', '’'):
        text = text.replace(junk, '')
    text = re.sub(r'\s+', '', text)
    if text.endswith('.exe'):
        text = text[:-4]
    for suffix in ('软件', '客户端', '桌面端', 'desktop', 'app'):
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[:-len(suffix)]
            break
    return text


def resolve_app(text):
    """用户说的软件名/别名/进程名 → 已登记的 app_id；找不到返回空串。"""
    value = _normalize_name(text)
    if not value:
        return ''
    try:
        table = declarations()
    except Exception:
        table = {}
    for app_id, decl in table.items():
        names = [app_id, decl.get('name')] + list(decl.get('aliases') or [])
        names += list(decl.get('process_names') or [])
        names += [str(item).replace('\\', '/').rsplit('/', 1)[-1]
                  for item in ((decl.get('install') or {}).get('common_paths') or [])]
        if any(_normalize_name(item) == value for item in names):
            return app_id
    try:
        for app_id, record in (app_reports.read_all().get('apps') or {}).items():
            names = [app_id, record.get('name'), record.get('exe')]
            if any(_normalize_name(item) == value for item in names):
                return app_id
    except Exception:
        pass
    return ''


def _focus_window(hwnd):
    """把已有窗口切到前台并取消最小化；返回是否成功。

    Windows 会拦“后台进程抢前台”（SetForegroundWindow 静默失败）。这里按
    restore → BringWindowToTop → 临时 AttachThreadInput 到当前前台线程的顺序重试，
    仍失败就返回 False，由调用方如实告诉用户“从任务栏点开”，不假装已切到前台。
    """
    try:
        import win32con
        import win32gui
    except Exception:
        return False
    try:
        hwnd = int(hwnd)
        if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
            return False
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        else:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        if win32gui.GetForegroundWindow() == hwnd:
            return True
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        if win32gui.GetForegroundWindow() == hwnd:
            return True
        try:
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
        except Exception:
            pass
        try:
            import win32api
            import win32process
            foreground = win32gui.GetForegroundWindow()
            target_thread, _ = win32process.GetWindowThreadProcessId(hwnd)
            current_thread = win32api.GetCurrentThreadId()
            foreground_thread = win32process.GetWindowThreadProcessId(foreground)[0] if foreground else 0
            attached = []
            for thread_id in {foreground_thread, target_thread} - {0, current_thread}:
                try:
                    win32process.AttachThreadInput(thread_id, current_thread, True)
                    attached.append(thread_id)
                except Exception:
                    pass
            try:
                win32gui.SetForegroundWindow(hwnd)
            finally:
                for thread_id in attached:
                    try:
                        win32process.AttachThreadInput(thread_id, current_thread, False)
                    except Exception:
                        pass
        except Exception:
            pass
        return bool(win32gui.GetForegroundWindow() == hwnd)
    except Exception:
        return False


def _open_unavailable(app_id, decl, resolved, name):
    download = (decl or {}).get('download') or {}
    url = str(download.get('url') or '')
    hint = str(download.get('hint') or '')
    status = str(resolved.get('status') or '')
    if status == 'unknown_app':
        message = ('咕嘎里还没有登记“%s”的启动方式。请到桌面咕嘎「接入面板 → 新增软件」，'
                   '填软件名称和可执行文件路径后再对我说“打开%s”。' % (name, name))
    elif status in ('not_installed', 'missing', 'invalid_declaration'):
        message = '没有在本机找到 %s 的可执行文件。' % name
        if url:
            message += '下载地址：%s。' % url
        if hint:
            message += hint + '。'
        message += ('装好后请到桌面咕嘎「接入面板 → 新增软件」填软件名称和可执行文件路径，'
                    '或把安装目录告诉我；然后再试“打开%s”。' % name)
    else:
        message = ('没能确认 %s 是否已安装（%s）。请到桌面咕嘎「接入面板 → 新增软件」'
                   '手动填写可执行文件路径，或检查是否被安全软件拦截了路径探测。'
                   % (name, resolved.get('message') or status or '路径探测不可用'))
        if url:
            message += '官方下载地址：%s。' % url
    return dict(ok=False, status=status or 'unknown', code='app_not_found',
                app=app_id, name=name, message=message, error=message,
                install_hint=hint, download_url=url,
                probe=resolved)


def open_app(app_id, verify=False, timeout=None):
    """简单“打开软件”：只拉起或切前台，不派任务、不进界面自动化。

    路径解析复用 ``launch_exe()``（用户指定 / MCP 自报 / 声明探测依次兜底）；
    已在运行就不起第二个实例，把已有窗口切到前台；找不到就返回能照做的提示。
    """
    app_id = str(app_id or '').strip().casefold()
    started = time.monotonic()
    resolved = launch_exe(app_id)
    decl = declaration(app_id)
    if not decl:
        report = app_reports.valid_report(app_id)
        if report.get('valid'):
            decl = _report_declaration(app_id, report)
    name = str((decl or {}).get('name') or app_id)
    if not resolved.get('ok'):
        return _open_unavailable(app_id, decl, resolved, name)
    exe = resolved['exe']

    if decl:
        existing = _find_window(decl, include_minimized=True)
        if existing:
            focused = _focus_window(existing.get('hwnd'))
            return dict(
                ok=True, status='already_running', app=app_id, name=name, exe=exe,
                path_source=resolved.get('source'), pid=existing.get('pid'),
                hwnd=existing.get('hwnd'), window_title=existing.get('title'),
                focused=focused, started=False,
                message=('%s 已在运行，已把窗口切到前台（没有重复启动）' % name if focused else
                         '%s 已在运行，但系统拒绝自动切换前台；请从任务栏点开它的窗口' % name),
                elapsed_ms=round((time.monotonic() - started) * 1000, 1))

    args = [str(exe)] + [str(item) for item in _flatten_list((decl or {}).get('launch', {}).get('args'))]
    try:
        process = subprocess.Popen(
            args, cwd=_launch_cwd(decl, exe) if decl else str(Path(exe).parent),
            startupinfo=_startup_info(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        return dict(ok=False, status='launch_failed', code='launch_failed', app=app_id,
                    name=name, exe=exe, error='启动失败：%s' % str(exc)[:200],
                    message='启动失败：%s。请在「接入面板 → 新增软件」核对可执行文件路径。'
                            % str(exc)[:200],
                    elapsed_ms=round((time.monotonic() - started) * 1000, 1))

    # 0.8 秒快检：立刻非 0 退出说明路径/依赖有问题，不能假装已打开。
    time.sleep(0.8)
    code = process.poll()
    if code not in (None, 0):
        return dict(ok=False, status='exited_early', code='launch_failed', app=app_id,
                    name=name, exe=exe, pid=process.pid,
                    message='%s 启动后立即退出（exit=%s）。请检查安装是否完整，'
                            '或在「接入面板 → 新增软件」重新指定可执行文件。' % (name, code),
                    elapsed_ms=round((time.monotonic() - started) * 1000, 1))

    window = None
    if verify:
        deadline = time.monotonic() + max(2.0, float(timeout or 8))
        while time.monotonic() < deadline:
            window = _find_window(decl, pid=process.pid) if decl else None
            if window:
                break
            existing = _find_window(decl) if decl else None
            if existing:
                window = existing
                break
            if process.poll() not in (None, 0):
                break
            time.sleep(0.3)
    return dict(ok=True, status='ready' if window else 'started', app=app_id, name=name,
                exe=exe, path_source=resolved.get('source'), pid=process.pid,
                hwnd=(window or {}).get('hwnd') or 0, window_title=(window or {}).get('title') or '',
                verified=bool(window), started=True,
                message=('%s 已启动（已看到窗口）' % name if window else
                         '%s 已发起启动，暂未观察到窗口（可能仍在加载）' % name),
                elapsed_ms=round((time.monotonic() - started) * 1000, 1))


def preload(ids=None, force=False):
    """启动时同步预热（只读探测，不启动软件）。建议在后台线程调用。"""
    targets = list(ids or app_ids())
    return [probe(app_id, force=force) for app_id in targets]


def warmup(ids=None, force=False):
    """后台线程预热；返回线程对象，调用方可以选择 join 或直接忽略。"""
    thread = threading.Thread(target=_safe_preload, args=(ids, force),
                              name='guga-app-registry-warmup', daemon=True)
    thread.start()
    return thread


def _safe_preload(ids, force):
    try:
        preload(ids=ids, force=force)
    except Exception:
        pass


def diagnose(force=False):
    """全部声明的探测结果；给自检面板/排障用。"""
    apps = []
    for app_id in app_ids():
        try:
            apps.append(probe(app_id, force=force))
        except Exception as exc:
            apps.append(_error_result(app_id, 'error', str(exc)[:200]))
    invalid = declaration_errors()
    return dict(contract=CONTRACT, cache=str(cache_path()),
                declarations=declaration_dirs(), apps=apps,
                invalid_declarations=invalid,
                summary=dict(
                    ready=sum(1 for item in apps if item.get('status') == 'ready'),
                    not_installed=sum(1 for item in apps if item.get('status') == 'not_installed'),
                    unknown=sum(1 for item in apps if item.get('status') == 'unknown'),
                    invalid=len(invalid),
                ),
                updated_at=time.strftime('%Y-%m-%dT%H:%M:%S'))


# ---------------------------------------------------------------------------
# 启动与就绪判定
# ---------------------------------------------------------------------------

def _startup_info():
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    # SW_SHOWNOACTIVATE：正常显示窗口但不抢用户前台。
    # （SW_SHOWMINNOACTIVE 会把窗口最小化，后续 UIA/点击无法工作，故不用。）
    startup.wShowWindow = 4
    return startup


def _launch_cwd(decl, exe):
    value = str(decl['launch'].get('cwd') or 'exe_dir')
    if value in ('exe_dir', '', 'default'):
        return str(Path(exe).parent)
    path = _expand(value)
    return str(path) if path and path.is_dir() else str(Path(exe).parent)


def _close_window(hwnd):
    try:
        import win32con
        import win32gui
        win32gui.PostMessage(int(hwnd), win32con.WM_CLOSE, 0, 0)
        return True
    except Exception:
        return False


def _input_anchor_ok(decl, window):
    """可选的输入框锚点校验；UIA 不可用时返回 None（未确认），不当作失败。"""
    anchor = decl['ready'].get('input_anchor') or {}
    if not anchor.get('must_exist'):
        return None
    texts = _window_texts(window.get('hwnd'))
    if texts is None:
        return None
    words = [str(w).casefold() for w in _flatten_list(anchor.get('name_contains')) if str(w)]
    if not words:
        return True
    joined = ' '.join(texts).casefold()
    return any(word in joined for word in words)


def launch(app_id, extra_args=None, verify=True, timeout=None, close_after_probe=False):
    """显式启动一个已声明的软件并判定就绪。

    - 已在运行（有可见窗口）：直接返回 already_running，不重复拉起；
    - 启动用 SW_SHOWMINNOACTIVE，不抢用户前台；
    - 只关闭本次自己拉起且 ``close_after_probe=True`` 的实例，绝不动用户已有窗口；
    - 首次运行未登录时返回 login_required + 可照做的 hint。
    """
    started = time.monotonic()
    app_id = str(app_id or '').strip().casefold()
    decl = declaration(app_id)
    if not decl:
        return _error_result(app_id, 'unknown_app',
                             '没有 %s 的适配声明' % app_id)
    result = probe(app_id)
    if result.get('status') != 'ready':
        return dict(probe=result, ok=False, status=result.get('status'),
                    installed=result.get('installed'), exe='', app=app_id,
                    message=result.get('message'), elapsed_ms=result.get('elapsed_ms'))
    exe = result['exe']

    existing = _find_window(decl)
    if existing:
        return dict(ok=True, status='already_running', app=app_id, exe=exe,
                    pid=existing.get('pid'), hwnd=existing.get('hwnd'),
                    window_title=existing.get('title'), started=False,
                    message='%s 已在运行，直接复用当前窗口（未重复拉起）' % decl.get('name'),
                    elapsed_ms=round((time.monotonic() - started) * 1000, 1))
    minimized = _find_window(decl, include_minimized=True)
    if minimized and minimized.get('minimized'):
        return dict(ok=False, status='running_minimized', app=app_id, exe=exe,
                    pid=minimized.get('pid'), hwnd=minimized.get('hwnd'),
                    window_title=minimized.get('title'), started=False,
                    message='%s 的窗口已最小化；最小化窗口无法进行 UIA/点击，'
                            '请用户在任务栏恢复窗口后再派单（未重复拉起）' % decl.get('name'),
                    elapsed_ms=round((time.monotonic() - started) * 1000, 1))

    args = [str(exe)] + [str(a) for a in _flatten_list(decl['launch'].get('args'))] + \
        [str(a) for a in _flatten_list(extra_args)]
    try:
        process = subprocess.Popen(args, cwd=_launch_cwd(decl, exe),
                                   startupinfo=_startup_info(),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        return dict(ok=False, status='launch_failed', app=app_id, exe=exe,
                    error='launch_failed', message='启动失败：%s' % str(exc)[:200],
                    elapsed_ms=round((time.monotonic() - started) * 1000, 1))
    result_launch = dict(app=app_id, exe=exe, pid=process.pid, started=True)
    if not verify:
        result_launch.update(ok=True, status='started', message='已拉起 %s' % decl.get('name'),
                             elapsed_ms=round((time.monotonic() - started) * 1000, 1))
        return result_launch

    wait_seconds = float(timeout or decl['launch'].get('timeout_seconds') or 30)
    deadline = time.monotonic() + max(3.0, wait_seconds)
    window = None
    signal = 'window_timeout'
    while time.monotonic() < deadline:
        window = _find_window(decl, pid=process.pid)
        if window:
            signal = 'window'
            break
        # 有些单实例软件会把请求转给已有进程后自己退出；借用已有窗口继续判。
        existing = _find_window(decl)
        if existing:
            window = existing
            signal = 'existing_instance_window'
            break
        code = process.poll()
        if code is not None and code != 0:
            break
        time.sleep(0.35)
    if window:
        # 少量界面有二次渲染，给一点稳定时间（默认 0）。
        extra = float(decl['ready'].get('extra_settle_seconds') or 0)
        if extra > 0:
            time.sleep(min(extra, 5.0))
        login = _login_state(decl, window)
        anchor = _input_anchor_ok(decl, window)
        anchor_failed = anchor is False
        result_launch.update(
            ok=(login is None and not anchor_failed),
            status=('login_required' if login else
                    ('input_anchor_missing' if anchor_failed else 'ready')),
            hwnd=window.get('hwnd'), window_title=window.get('title'),
            window_pid=window.get('pid'),
            window_size=[window.get('width'), window.get('height')],
            ready_signal=signal, login=login, input_anchor=anchor,
            message=(login.get('hint') or '首次运行可能需要登录/同意后才能接收任务')
            if login else ('就绪但没有找到输入框锚点，请检查窗口内是否停在其他页面'
                           if anchor_failed else
                           '%s 已就绪（%s，%.0f 秒）'
                           % (decl.get('name'), signal,
                              time.monotonic() - started)),
            elapsed_ms=round((time.monotonic() - started) * 1000, 1))
    else:
        code = process.poll()
        minimized = _find_window(decl, include_minimized=True)
        if minimized and minimized.get('minimized'):
            status = 'window_minimized'
            message = '启动后窗口处于最小化状态，无法进行 UIA/点击；请用户在任务栏恢复窗口'
        elif code is not None:
            status = 'exited_early'
            message = '启动后进程提前退出（exit=%s）' % code
        else:
            status = 'window_timeout'
            message = '启动后 %.0f 秒仍没有可见窗口' % wait_seconds
        result_launch.update(ok=False, status=status,
                             hwnd=(minimized or {}).get('hwnd') or 0, window_title='',
                             ready_signal=signal, login=None,
                             message=message,
                             elapsed_ms=round((time.monotonic() - started) * 1000, 1))
    # 只关闭本次自己拉起的那个窗口（pid 必须与我们启动的进程一致），
    # 借用的已有实例窗口绝不动，避免把用户正在用的软件关掉。
    if (close_after_probe and result_launch.get('hwnd')
            and result_launch.get('window_pid') == process.pid):
        _close_window(result_launch['hwnd'])
        for _ in range(16):
            if process.poll() is not None:
                break
            time.sleep(0.5)
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
    return result_launch
