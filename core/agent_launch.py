# -*- coding: utf-8 -*-
"""把「对应的软件」真的拉起来：WorkBuddy / DSH / Codex。

为什么要有这一层：绑定/派单只保证"会话登记上了"，不保证那个软件真的在运行。
用户看到的现象就是"接上了，但屏幕上什么都没发生"。这里只做三件事——查在不在、
不在就拉起、拉不起来就说清楚原因。不猜、不假装成功。
"""
import os
import socket
import subprocess
import time
from pathlib import Path

DSH_HOST = '127.0.0.1'
DSH_PORT = 3080
DSH_URL = 'http://127.0.0.1:3080'


def _workbuddy_exe():
    try:
        from core.workbuddy_bridge import _resolved_exe
        exe = _resolved_exe()
        if exe:
            return exe
    except Exception:
        pass
    candidate = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'WorkBuddy' / 'WorkBuddy.exe'
    return str(candidate) if candidate.is_file() else ''


def _workbuddy_window():
    try:
        from core.workbuddy_bridge import _window
        return _window()
    except Exception:
        return None


def dsh_running(timeout=.35):
    try:
        with socket.create_connection((DSH_HOST, DSH_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def codex_ready():
    try:
        from core.codex_gateway import NativePipe
        return bool(NativePipe().discover())
    except Exception:
        return False


def ensure(backend, wait=0.0):
    """保证 backend 对应的软件在跑。返回 dict(ok, launched, message)。

    wait>0 时最多等这么久看窗口是否真的出现；默认不阻塞调用方。
    """
    backend = str(backend or '').lower()
    if backend == 'workbuddy':
        if _workbuddy_window():
            return dict(ok=True, launched=False, app=backend,
                        message='WorkBuddy 已经开着，直接用这个窗口')
        exe = _workbuddy_exe()
        if not exe:
            return dict(ok=False, launched=False, app=backend,
                        message='没找到 WorkBuddy.exe：先装桌面端，或把安装路径告诉我')
        try:
            # detached + 独立进程组：咕嘎自己退出时不会把 WorkBuddy 带走
            subprocess.Popen([exe], creationflags=0x00000008 | 0x00000200)
        except OSError as exc:
            return dict(ok=False, launched=False, app=backend,
                        message='WorkBuddy 启动失败：' + str(exc)[:120])
        deadline = time.monotonic() + max(0.0, float(wait))
        while wait and time.monotonic() < deadline:
            if _workbuddy_window():
                break
            time.sleep(.6)
        return dict(ok=True, launched=True, app=backend, message='已经拉起 WorkBuddy',
                    window=bool(_workbuddy_window()))
    if backend == 'dsh':
        if dsh_running():
            return dict(ok=True, launched=False, app=backend, url=DSH_URL,
                        message='DSH 已经在跑，用 ' + DSH_URL)
        return dict(ok=False, launched=False, app=backend, url=DSH_URL,
                    message='DSH 没在运行（%s:%d 没响应）：先启动 DSH，我就能把活派过去'
                            % (DSH_HOST, DSH_PORT))
    if backend == 'codex':
        ready = codex_ready()
        return dict(ok=ready, launched=False, app=backend,
                    message='Codex 桌面通道在线' if ready
                    else 'Codex 现在用不了（额度或没打开），先走 WorkBuddy / DSH')
    return dict(ok=False, launched=False, app=backend, message='不认识的执行器：' + backend)
