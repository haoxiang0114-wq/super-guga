# -*- coding: utf-8 -*-
"""队友功能兼容层 — 纯新增模块，本地既有代码不引用本模块。

背景：队友仓库 (lhh20060218-sketch/guga-desktop) 基于旧版咕嘎，其新增的
管家/派单模块引用了旧版内部 API。本模块用 AST 原样移植了这些函数的原始
实现（未改写逻辑），供新增模块导入，从而避免改动本地已更新的同名模块。

移植来源与函数：
  core/computer_control.py : wheel_delta / wheel_notches / _ensure_foreground
                             （含 _attach_focus / _root / WHEEL_NOTCH / _WHEEL_INTENTS）
  core/ai_chat.py          : provider_label / endpoint_label / model_config_error
                             （含 _PROVIDER_HOSTS）
  core/agent_failure.py    : proxy_snapshot
  ui/sprite_animator.py    : adaptive_display_width

对现有能力的影响：无。本模块不被本地既有模块导入；移植函数语义与队友旧版一致。
"""
import os
import re
import time
from urllib.parse import urlsplit

try:                                    # 队友新增模块，缺失时降级
    from core import guga_paths
except Exception:                       # pragma: no cover
    guga_paths = None


def _read_config():
    """旧版 guga_paths.read_config 的等价入口，缺失时返回空配置。"""
    if guga_paths is None:
        return {}
    try:
        return guga_paths.read_config() or {}
    except Exception:
        return {}



# ==================== ported from core/computer_control.py ====================
WHEEL_NOTCH = 120

_WHEEL_INTENTS = {
    "later": -WHEEL_NOTCH, "down": -WHEEL_NOTCH, "forward": -WHEEL_NOTCH,
    "next": -WHEEL_NOTCH, "bottom": -WHEEL_NOTCH,
    "往下": -WHEEL_NOTCH, "向下": -WHEEL_NOTCH, "更后": -WHEEL_NOTCH,
    "earlier": WHEEL_NOTCH, "up": WHEEL_NOTCH, "back": WHEEL_NOTCH,
    "previous": WHEEL_NOTCH, "top": WHEEL_NOTCH,
    "往上": WHEEL_NOTCH, "向上": WHEEL_NOTCH, "更早": WHEEL_NOTCH,
}

def wheel_delta(intent):
    """滚动意图 → ``MOUSEEVENTF_WHEEL`` 的 dwData。

    ``later``/``down``（往下翻、看更后）返回 -120；``earlier``/``up`` 返回 +120。
    """
    key = str(intent or "").strip().lower()
    if key not in _WHEEL_INTENTS:
        raise ValueError("未知滚动意图: %r（只支持 later/down 或 earlier/up）" % (intent,))
    return _WHEEL_INTENTS[key]

def wheel_notches(intent, notches=1):
    """把意图展开成每格一次的 delta 列表，便于调用方逐格发送。"""
    count = max(1, int(notches))
    return [wheel_delta(intent)] * count

def _attach_focus(hwnd):
    """Windows foreground handoff, bounded to the requested existing window."""
    import ctypes,win32gui,win32process,win32api
    user=ctypes.windll.user32;attached=[]
    try:
        target_thread=win32process.GetWindowThreadProcessId(hwnd)[0]
        foreground=win32gui.GetForegroundWindow()
        foreground_thread=win32process.GetWindowThreadProcessId(foreground)[0] if foreground else 0
        current=win32api.GetCurrentThreadId()
        if not target_thread:return False
        for thread in set((target_thread,foreground_thread))-{0,current}:
            if user.AttachThreadInput(current,thread,True):attached.append(thread)
        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
        return win32gui.GetForegroundWindow()==hwnd
    except Exception:return False
    finally:
        for thread in reversed(attached):user.AttachThreadInput(current,thread,False)

def _root(hwnd):
    """顶层窗口句柄。Electron/Chromium 应用（WorkBuddy、JoyCode、豆包、Trae）
    内部是子窗口，点击后前台句柄会变成父窗口；直接比原句柄会误判“前台已改变”。"""
    import win32gui
    try:
        return int(win32gui.GetAncestor(int(hwnd), 2) or hwnd)
    except Exception:
        return int(hwnd or 0)

def _ensure_foreground(hwnd,timeout=.7):
    """Best-effort focus recovery for cross-app automation.

    Windows may hand focus to another window between steps (the pet, a toast,
    or the user's own click).  Give the target one bounded chance to come back
    instead of failing the whole action immediately.
    """
    import win32gui
    hwnd=int(hwnd);deadline=time.monotonic()+max(0,float(timeout));last=0.0
    while True:
        if win32gui.GetForegroundWindow()==hwnd:return True
        if not win32gui.IsWindow(hwnd):return False
        now=time.monotonic()
        if now-last>=.15:
            last=now
            try:
                win32gui.ShowWindow(hwnd,9)
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                _attach_focus(hwnd)
        if time.monotonic()>=deadline:break
        time.sleep(.05)
    return win32gui.GetForegroundWindow()==hwnd


# ==================== ported from core/ai_chat.py ====================
_PROVIDER_HOSTS = {
    'api.deepseek.com': 'DeepSeek 官方',
    'open.bigmodel.cn': '智谱 GLM',
    'dashscope.aliyuncs.com': '通义千问',
    'maas.qianwenaiapi.com': '千问 MaaS 网关',
    'opencode.ai': 'OpenCode',
}

def provider_label(cfg=None) -> str:
    """当前服务商显示名；没配地址时明确写“未选择服务商”。"""
    cfg = cfg if isinstance(cfg, dict) else guga_paths.read_config()
    base = str(cfg.get('base_url') or '').strip()
    if not base:
        return '未选择服务商'
    host = urlsplit(base).hostname or ''
    custom = str(cfg.get('provider') or '').strip()
    return custom or _PROVIDER_HOSTS.get(host, host or base)

def endpoint_label(cfg=None) -> str:
    cfg = cfg if isinstance(cfg, dict) else guga_paths.read_config()
    return str(cfg.get('base_url') or '').strip() or '未填写接口地址'

def model_config_error(cfg=None) -> str:
    """返回可直接展示给用户的中文原因；空串表示三项都齐、可以发请求。"""
    cfg = cfg if isinstance(cfg, dict) else guga_paths.read_config()
    key = str(cfg.get('api_key') or '').strip()
    base = str(cfg.get('base_url') or '').strip()
    model = str(cfg.get('model') or '').strip()
    if not key:
        return ('未配置模型：没有 API Key。对话、完整对话、截图分析不可用；'
                '请点「模型设置」自己填写。接入 MCP 只负责发现软件路径，不等于模型可用。')
    if not base.startswith('http'):
        return ('未选择服务商：已填 API Key，但没有接口地址（base_url）。'
                '咕嘎不会把 Key 发到任何默认服务商，请到「模型设置」选择平台或填写地址。')
    if not model:
        return '未选择模型名：请到「模型设置」填写模型 id（例如 deepseek-chat）。'
    return ''


# ==================== ported from core/agent_failure.py ====================
def proxy_snapshot():
    """记录进程实际会走的代理（requests 默认 trust_env=True，代理也能返回 405）。"""
    found = {}
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'):
        value = str(os.environ.get(name) or '').strip()
        if value:
            found[name] = re.sub(r'(?i)(://)[^/@]+@', r'\1***@', value)[:160]
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Windows\CurrentVersion\Internet Settings') as key:
            enabled, _ = winreg.QueryValueEx(key, 'ProxyEnable')
            server, _ = winreg.QueryValueEx(key, 'ProxyServer')
        if int(enabled or 0) and server:
            found['WindowsProxy'] = str(server)[:160]
    except Exception:
        pass
    return found


# ==================== ported from ui/sprite_animator.py ====================
def adaptive_display_width(min_w=140, max_w=205, height_ratio=0.249):
    """按主屏可用高度自适应形象显示宽（对齐 dsh-pet 观感再降一档）.

    dsh-pet 实测: 角色显示高约 244px(@1080p)。这里显示宽占屏约 17.3%
    (1080p 可用 1040 → 180)；新素材画布 458x660 按显示框等比留白，
    角色身高约 207px，比 dsh 再小一档，边缘参差在小画布上被重采样糊平。
    1080p → 180; 2K → 205 封顶; 768 小屏 → 140 兜底.

    2026-11-07 修"同分辨率换缩放就变尺寸": 比例项本身是物理像素，
    但 140/205 的上下限原来夹在逻辑像素上——同屏 100%/125%/150% 会得到
    180/180/210(1080p)、205/249/249(2K) 三种物理大小。改为统一按物理
    像素夹，再用屏幕 DPR 换回逻辑宽，保证同一分辨率下观感不变.
    """
    try:
        from PyQt5.QtGui import QGuiApplication
        scr = QGuiApplication.primaryScreen()
        if scr is None:
            return max_w
        dpr = max(1.0, float(scr.devicePixelRatio() or 1.0))
        h = scr.availableGeometry().height()
        phys_w = round(h * dpr * height_ratio / 1.44)
        phys_w = max(min_w, min(max_w, phys_w))
        return max(1, round(phys_w / dpr))
    except Exception:
        return max_w



# ---- 兼容别名：队友旧版模块内的同名调用点 ----
_legacy_read_config = _read_config
