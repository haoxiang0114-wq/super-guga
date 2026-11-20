# -*- coding: utf-8 -*-
"""麦克风可用性分级探测.

为什么不用 Qt 的 QMediaDevices: Qt Multimedia 后端缺失/未初始化时会返回空列表,
把"有设备但打不开"误报成"没有设备". 这里以 Windows 自己的 MMDevices 注册表为准
(与"设置 → 系统 → 声音"同源), 再叠加隐私授权与依赖检查, 最后给出分级结论:

  ready             设备激活、授权允许、依赖齐全
  missing_dependency 有设备, 但缺 sounddevice/vosk 等运行库
  denied            隐私设置禁止了桌面应用使用麦克风
  disabled          设备存在但在"声音设置"里被禁用
  unplugged         设备存在但未插入/蓝牙断开
  no_device         系统里没有任何录音端点
  open_failed       设备看似可用, 实际打不开(通常被其它程序独占)
"""
import importlib.util
import os
import sys
import winreg


_CAPTURE = r'SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture'
_FRIENDLY = '{a45c254e-df1c-4efd-8020-67d146a850e0},2'
_CONSENT = r'Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone'

# MMDevice DeviceState: 1=激活 2=禁用 4=不存在 8=未插入, 其它高位值是无实体占位端点
_STATE_NAMES = {1: 'active', 2: 'disabled', 4: 'absent', 8: 'unplugged'}

_HINTS = {
    'ready': '',
    'missing_dependency': '语音运行库不全，请在终端执行：pip install sounddevice vosk websocket-client',
    'denied': 'Windows 隐私设置禁止了麦克风：设置 → 隐私和安全性 → 麦克风 → 打开“允许桌面应用访问麦克风”',
    'disabled': '麦克风被禁用了：设置 → 系统 → 声音 → 输入 → 选中设备 → 允许/启用该设备',
    'unplugged': '麦克风没插好或蓝牙已断开：检查连接后点一下重试',
    'no_device': '系统里没有录音设备：插好麦克风，或在“声音设置”里检查输入设备',
    'open_failed': '检测到麦克风但打不开，可能被其它录音软件独占：关掉它们后重试',
}


def _read_device_state(path):
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            return int(winreg.QueryValueEx(key, 'DeviceState')[0])
    except OSError:
        return 0


def _read_friendly(path):
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path + r'\Properties') as key:
            return str(winreg.QueryValueEx(key, _FRIENDLY)[0]).strip()
    except OSError:
        return ''


def capture_endpoints():
    """Windows 层录音端点列表: [{name, state, device_id}]，按 激活>禁用>未插入>不存在 排序."""
    found = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _CAPTURE) as root:
            index = 0
            while True:
                try:
                    device_id = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                path = _CAPTURE + '\\' + device_id
                state_code = _read_device_state(path)
                state = _STATE_NAMES.get(state_code)
                if state is None:
                    # 0x10000000 之上的高位是"无实体"占位端点(内部 AUX 等)
                    state = 'absent'
                name = _read_friendly(path) or '未命名录音设备'
                found.append({'name': name, 'state': state, 'device_id': device_id})
    except OSError:
        return []
    order = {'active': 0, 'disabled': 1, 'unplugged': 2, 'absent': 3}
    found.sort(key=lambda item: (order.get(item['state'], 9), item['name']))
    return found


def _consent():
    """HKCU 隐私授权: 'allow' / 'deny' / 'unknown'（含按 exe 的桌面应用授权）."""
    exe = os.path.basename(sys.executable).lower()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _CONSENT) as key:
            global_value = str(winreg.QueryValueEx(key, 'Value')[0]).strip().lower()
        if global_value == 'deny':
            return 'deny'
    except OSError:
        global_value = 'unknown'
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _CONSENT + r'\NonPackaged') as root:
            index = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                if exe and sub.lower().endswith(exe):
                    try:
                        with winreg.OpenKey(root, sub) as key:
                            value = str(winreg.QueryValueEx(key, 'Value')[0]).strip().lower()
                    except OSError:
                        value = ''
                    if value == 'deny':
                        return 'deny'
                    if value == 'allow':
                        return 'allow'
                    # 空值(还没被系统显式记录)不覆盖全局开关, 交给全局设置判断
                    break
    except OSError:
        pass
    return 'allow' if global_value == 'allow' else 'unknown'


def _deps():
    def has(module):
        try:
            return importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            return False
    return {'sounddevice': has('sounddevice'), 'vosk': has('vosk'),
            'websocket': has('websocket'), 'numpy': has('numpy')}


def default_input_name():
    """sounddevice 视角的默认输入设备名（仅供展示，不作判定依据）."""
    try:
        import sounddevice as sd
        dev = sd.query_devices(kind='input')
        return str(dev.get('name', ''))
    except Exception:
        return ''


def probe(require_vosk=True):
    """返回分级结论 dict: verdict/state/devices/consent/deps/hint.

    require_vosk=False 用于长语音(只依赖 sounddevice+websocket, 不需要离线模型).
    """
    devices = capture_endpoints()
    consent = _consent()
    deps = _deps()
    active = [d['name'] for d in devices if d['state'] == 'active']

    need = ['sounddevice'] + (['vosk'] if require_vosk else [])
    missing = [name for name in need if not deps.get(name)]
    if missing:
        verdict = 'missing_dependency'
    elif consent == 'deny':
        verdict = 'denied'
    elif active:
        verdict = 'ready'
    elif any(d['state'] == 'disabled' for d in devices):
        verdict = 'disabled'
    elif any(d['state'] == 'unplugged' for d in devices):
        verdict = 'unplugged'
    else:
        verdict = 'no_device'

    return {
        'verdict': verdict,
        'devices': devices,
        'active': active,
        'default_input': default_input_name(),
        'consent': consent,
        'deps': deps,
        'missing': missing,
        'hint': _HINTS.get(verdict, ''),
    }


def message(result):
    """把 probe() 结果压成一句用户能照着做的话."""
    verdict = (result or {}).get('verdict')
    hint = (result or {}).get('hint') or ''
    if verdict == 'ready':
        name = (result.get('default_input') or (result.get('active') or ['麦克风'])[0])
        return '麦克风就绪：%s' % name
    if verdict == 'missing_dependency':
        return '麦克风在，但%s' % hint
    return hint or '麦克风不可用'
