# -*- coding: utf-8 -*-
"""运行期数据落点：一律放用户可写目录，不依赖安装目录。

评委常见做法是把包解压到 Program Files 或只读盘：安装目录写不进去时，
ai_config.json / runtime 缓存 / 交付物都不能失败，更不能因此崩溃。

路径解析顺序（同一个函数给所有读写配置的地方用）:
    1. %GUGA_HOME%              （测试/多实例显式覆盖）
    2. %LOCALAPPDATA%\\GUGA     （默认落点）
    3. %APPDATA%\\GUGA          （LOCALAPPDATA 不可写 / 未设置）
    4. %USERPROFILE%\\GUGA      （前两项都不可用时）
    5. 安装目录                  （最后兜底；只读时写入必须失败并给明确提示）

旧版本写在安装目录里的 ai_config.json 只做“检测”：首次启动会打码展示并询问是否沿用，
默认不复制、不写入任何 Key；只有用户明确选择“沿用”才复制并标记来源。
"""
import io
import json
import os
import sys
from pathlib import Path

APP_DIR = 'GUGA'
CONFIG_NAME = 'ai_config.json'


def _log(event, detail=''):
    try:
        from core.app_log import log
        log(event, detail)
    except Exception:
        pass


def install_dirs():
    """旧版本可能写配置的安装目录，按“最可能的落点”排序。

    PyInstaller onedir 下 core/ 在 _internal 里，旧配置通常就在 _internal/ 根；
    源码运行时则是项目根目录。
    """
    folders = []
    if getattr(sys, 'frozen', False):
        base = Path(sys.executable).resolve().parent
        candidates = [base / '_internal', base]
    else:
        root = Path(__file__).resolve().parents[1]
        candidates = [root, root.parent]
    for folder in candidates:
        try:
            if folder not in folders:
                folders.append(folder)
        except Exception:
            continue
    return folders


INSTALL_DIRS = install_dirs()
INSTALL_DIR = INSTALL_DIRS[0]


def _candidates():
    """按优先级返回 (来源标签, 目录)。同一目录只保留最先出现的一项。"""
    rows = []

    def add(label, path):
        try:
            folder = Path(os.path.expanduser(str(path)))
            key = str(folder).rstrip('\\/').lower()
            for _label, existing in rows:
                if str(existing).rstrip('\\/').lower() == key:
                    return
            if key:
                rows.append((label, folder))
        except Exception:
            pass

    override = os.environ.get('GUGA_HOME')
    if override:
        add('GUGA_HOME', override)
    local = os.environ.get('LOCALAPPDATA')
    if local:
        add('LOCALAPPDATA', Path(local) / APP_DIR)
    roaming = os.environ.get('APPDATA')
    if roaming:
        add('APPDATA', Path(roaming) / APP_DIR)
    home = os.environ.get('USERPROFILE') or os.path.expanduser('~')
    if home:
        add('用户主目录', Path(home) / APP_DIR)
    add('安装目录', INSTALL_DIR)
    return rows


def _probe(folder):
    """真的往目录里写一个探针文件，确认可写（存在不等于可写）。"""
    try:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ('.write-probe-%d' % os.getpid())
        probe.write_text('ok', encoding='utf-8')
        probe.unlink()
        return True
    except Exception:
        return False


def _resolve():
    rows = _candidates()
    for label, folder in rows:
        if _probe(folder):
            return folder, label, True
    # 全部不可写：落到最后的安装目录，后续写入会失败并给出明确提示
    label, folder = rows[-1] if rows else ('安装目录', INSTALL_DIR)
    return folder, label, False


BASE, BASE_SOURCE, BASE_WRITABLE = _resolve()
CONFIG = BASE / CONFIG_NAME
RUNTIME = BASE / 'runtime'
DELIVERY = BASE / 'deliverables'

# 写盘失败时只在本次进程内生效的配置（不假装已持久化，重启后需重填）
_session_config = None

# 最近一次旧配置迁移结果，供自检/界面提示；不阻塞启动
_last_migration = dict(status='none', source='', target=str(CONFIG), message='')


class ConfigWriteError(IOError):
    """配置写入失败。带上目标路径与原因，交给界面翻译成可照做的提示。"""

    def __init__(self, path, reason):
        self.path = str(path)
        self.reason = str(reason)
        super().__init__('写配置文件出错：%s' % self.reason)


class ApiKeyError(ValueError):
    """Key 形状不合法（例如中间夹了空白/换行）。不写盘，直接交给界面提示。"""


_ZERO_WIDTH = '\u200b\u200c\u200d\u2060\ufeff'


def clean_api_key(value):
    """洗净 Key：去 BOM/零宽字符与首尾空白；中间仍有空白则返回错误。

    返回 ``(cleaned, error)``。error 非空时 cleaned 为空串，调用方不得发出请求。
    这里不打印 Key 本身，错误文案只带长度，避免日志/文案泄漏凭据。
    """
    text = str(value or '')
    cleaned = ''.join(ch for ch in text if ch not in _ZERO_WIDTH).strip()
    if any(ch.isspace() for ch in cleaned):
        return '', ('API Key 中间含有空白或换行（清理后长度 %d 字符）；'
                    '请重新复制完整 Key，粘贴时不要带入换行或空格' % len(cleaned))
    return cleaned, ''


def key_fingerprint(value, source='main'):
    """报错用凭据指纹：长度 + 前四 + 后四 + 来源（不含完整 Key）。"""
    key = str(value or '')
    head = key[:4] if key else '—'
    tail = key[-4:] if len(key) > 4 else '—'
    return dict(length=len(key), head=head, tail=tail, source=str(source or 'main'))


def fingerprint_text(value, source='main'):
    """把凭据指纹拼成一句可直接展示的中文。"""
    item = key_fingerprint(value, source)
    return '本次凭据：长度 %d · 前四 %s … 后四 %s · 来源 %s' % (
        item['length'], item['head'], item['tail'], item['source'])


def ensure():
    """建目录；失败也不抛（只读/受限环境下由调用方按缺省值降级）。"""
    ok = True
    for folder in (BASE, RUNTIME, DELIVERY):
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except Exception:
            ok = False
    return ok


def writable():
    """当前落点是否真的可写（拿一个探针文件试）。"""
    return _probe(BASE)


_LEGACY_PROMPT = RUNTIME / 'legacy_config_prompt.json'


def legacy_paths():
    """安装目录里可能存在的真实配置；示例模板永远不参与迁移。"""
    return [folder / CONFIG_NAME for folder in INSTALL_DIRS
            if '.example.' not in (folder / CONFIG_NAME).name.lower()]


def _read_json(path):
    try:
        with io.open(str(path), encoding='utf-8-sig') as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def legacy_candidate():
    """只检测、不写入：返回待用户确认的安装目录旧配置（含打码所需字段）。

    用户目录已有配置，或没有旧配置，或只有 ai_config.example.json 时返回空。
    """
    if CONFIG.exists():
        return {}
    for legacy in legacy_paths():
        try:
            if not legacy.is_file() or legacy.resolve() == CONFIG.resolve():
                continue
            data = _read_json(legacy)
            if not data:
                continue
            return dict(path=str(legacy), config=data,
                        api_key=str(data.get('api_key') or ''),
                        base_url=str(data.get('base_url') or ''),
                        model=str(data.get('model') or ''))
        except Exception:
            continue
    return {}


def legacy_prompt_dismissed(source):
    data = _read_json(_LEGACY_PROMPT)
    return str(source) in set(data.get('dismissed') or [])


def dismiss_legacy(source):
    """用户选择“重新填写”：记住这个来源，下次启动不再反复弹。"""
    data = _read_json(_LEGACY_PROMPT)
    rows = list(data.get('dismissed') or [])
    if str(source) not in rows:
        rows.append(str(source))
    try:
        write_config(dict(dismissed=rows[-20:]), path=_LEGACY_PROMPT)
    except Exception:
        pass
    return True


def adopt_legacy(source):
    """用户在弹窗里明确选择“沿用”后才复制；默认路径绝不复制。

    复制后标记 key_source=legacy_inherited，设置页会如实显示 Key 来源。
    """
    src = Path(str(source or ''))
    if not src.is_file() or '.example.' in src.name.lower():
        return dict(ok=False, message='旧配置不存在，或这是示例模板（模板永不参与迁移）')
    data = _read_json(src)
    if not data:
        return dict(ok=False, message='旧配置读不出来或不是对象')
    data['key_source'] = 'legacy_inherited'
    try:
        ensure()
        write_config(data)
    except Exception as exc:
        return dict(ok=False, message=str(exc))
    _log('config-adopt', '%s → %s' % (src, CONFIG))
    # 返回真正落盘后的配置（Key 已洗净），而不是迁移前的原始 dict。
    return dict(ok=True, target=str(CONFIG), config=read_config())


def migrate_legacy():
    """旧配置只检测、不复制；返回最终配置路径（可能不存在）。

    默认“不沿用”：安装目录里的旧 Key 绝不静默写进用户目录。
    必须由界面调用 adopt_legacy() 且用户明确同意后才复制。
    """
    global _last_migration
    info = dict(status='none', source='', target=str(CONFIG), message='')
    if CONFIG.exists():
        info['status'] = 'already'
    else:
        candidate = legacy_candidate()
        if candidate:
            info.update(status='pending_confirm', source=candidate['path'],
                        message='安装目录有旧配置，等待用户确认；默认不沿用。')
        else:
            info['status'] = 'missing'
    _last_migration = info
    if info['status'] == 'pending_confirm':
        _log('config-migrate-pending', info['source'])
    return CONFIG


def mask_key(value):
    """界面展示用：只露后四位，任何地方都不打印完整 Key。"""
    text = str(value or '').strip()
    if not text:
        return '未填写'
    return '••••' + text[-4:]


def key_source(cfg=None):
    """Key 来源：本机填写 / 从旧配置继承 / 云端下发。"""
    cfg = cfg if isinstance(cfg, dict) else read_config()
    marker = str(cfg.get('key_source') or '')
    if marker == 'legacy_inherited':
        return '从旧配置继承'
    if marker == 'cloud_issued':
        return '云端下发'
    if marker == 'local_input':
        return '本机填写'
    return '本机填写' if str(cfg.get('api_key') or '').strip() else '未填写'


def migration_status():
    return dict(_last_migration)


def prepare():
    """启动早期统一入口：建目录 + 迁移旧配置。返回用户目录是否可写。"""
    ok = ensure()
    migrate_legacy()
    return ok


def _normalize_model_fields(data, strict=False):
    """归一化 base_url，并洗净所有 Key（含 fallback / legacy 旧配置）。

    strict=True 用于写盘：内部仍有空白时抛 ApiKeyError，绝不写入坏 Key；
    读盘走 strict=False：只记录 api_key_error 供界面/请求层提示，不阻断启动。
    """
    data = dict(data or {})
    data.pop('api_key_error', None)
    try:
        from core.model_endpoint import normalize_config
        data = normalize_config(data)
    except Exception:
        data = dict(data or {})
    for field, label in (('api_key', '主模型 API Key'), ('vision_api_key', '视觉模型 API Key')):
        if field in data:
            cleaned, error = clean_api_key(data.get(field))
            if error:
                if strict:
                    raise ApiKeyError('%s：%s' % (label, error))
                data['api_key_error'] = '%s：%s' % (label, error)
            else:
                data[field] = cleaned
    fallback = data.get('fallback')
    if isinstance(fallback, dict) and 'api_key' in fallback:
        cleaned, error = clean_api_key(fallback.get('api_key'))
        if error:
            if strict:
                raise ApiKeyError('备用模型 API Key：%s' % error)
            data['api_key_error'] = '备用模型 API Key：%s' % error
        else:
            fallback['api_key'] = cleaned
    return data


def read_config(default=None, path=None):
    """统一读取入口：优先本次进程内临时配置，其次用户目录，最后给缺省值。"""
    if path is None and _session_config is not None:
        return dict(_session_config)
    target = Path(path or CONFIG)
    try:
        with io.open(str(target), encoding='utf-8-sig') as handle:
            data = json.load(handle)
        return _normalize_model_fields(data if isinstance(data, dict) else dict(default or {}))
    except Exception:
        return _normalize_model_fields(dict(default or {}))


def write_config(cfg, path=None):
    """原子写入配置；失败抛 ConfigWriteError（界面负责提示，不得让异常裸奔）。

    写入前把 base_url / fallback.base_url 归一化：用户整段粘贴
    ``.../chat/completions`` 或网页地址时，落盘的就是可用的根地址。
    """
    global _session_config
    target = Path(path or CONFIG)
    # 写盘路径（含 adopt_legacy 旧配置迁移）必须先洗净 Key；坏 Key 直接拒绝写入。
    payload_cfg = _normalize_model_fields(dict(cfg or {}), strict=True)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    payload = json.dumps(payload_cfg, ensure_ascii=False, indent=2)
    tmp = target.with_name(target.name + '.tmp')
    try:
        tmp.write_text(payload, encoding='utf-8')
        os.replace(str(tmp), str(target))
        with io.open(str(target), encoding='utf-8-sig') as handle:
            back = json.load(handle)
        if not isinstance(back, dict):
            raise IOError('保存后回读内容不是配置对象')
    except Exception as exc:
        try:
            tmp.unlink()
        except Exception:
            pass
        raise ConfigWriteError(target, exc)
    if path is None:
        _session_config = None
    return target


def remember_config(cfg):
    """写盘失败时的兜底：配置只在本次进程生效，桌宠仍能正常启动。"""
    global _session_config
    _session_config = _normalize_model_fields(dict(cfg or {}))
    return _session_config


def describe():
    """给界面/自检用的一句话：数据放哪、可不可写。"""
    state = '可写' if writable() else '不可写，请检查用户目录权限'
    return '%s（%s · %s）' % (BASE, BASE_SOURCE, state)
