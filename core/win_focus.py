# -*- coding: utf-8 -*-
"""Windows 窗口前台/托盘恢复的唯一实现。

来源：agent/guga_executor.py 的 ``_hwnd_of`` / ``focus_window`` / ``_wake_from_tray``，
判定与重试逻辑一行未改。差异只有两处：

1. 日志出口改成可注入的 ``log`` 回调：桌面执行器要 print 到自己的控制台，
   core 侧走 logging，两边各自保留原来的输出方式。
2. ``hwnd_of`` 同时接受窗口对象与原始 HWND（int / 纯数字串）。旧实现只认对象，
   而 ``core/executor_bridge`` 传的是 ``int(hwnd)``，于是兜底路径恒 False。

原先 core/executor_compat.py 与 agent/guga_executor.py 各存一份逐字相同的
拷贝（约 185 行），改一处漏一处；现在只此一份。
"""
import logging
import time

try:                                    # 输入护栏；缺失时降级为不拦截
    from core import input_guard
except Exception:                       # pragma: no cover
    input_guard = None


TRAY_NOISE = ("floatball", "launcher", "-siw", "crashhandler", "toolbar")


def _default_log(msg):
    logging.getLogger(__name__).info(str(msg))


def hwnd_of(w):
    """把「窗口对象 / 原始 HWND」统一成 int HWND；拿不到返回 None。

    窗口对象走属性探测：pygetwindow 各版本属性名不一样（_hWnd / _hwnd / hWnd / _handle）。
    原始 HWND（int / 纯数字字符串）直接放行 —— ``core/executor_bridge`` 传进来的正是
    ``int(hwnd)``。旧实现只认窗口对象，于是 ``hwnd_of(int)`` 恒为 None、
    ``focus_window`` 立刻 ``return False``，整条兜底路径是死代码（原有测试把
    ``focus_window`` 整个 mock 掉，所以一直没暴露）。
    """
    if w is None or isinstance(w, bool):
        return None
    if isinstance(w, int):
        return w or None
    if isinstance(w, str):
        text = w.strip()
        return int(text) if text.isdigit() else None
    for attr in ("_hWnd", "_hwnd", "hWnd", "_handle"):
        try:
            h = getattr(w, attr, None)
            if h:
                return int(h)
        except Exception:
            continue
    return None


def focus_window(w, app="", quiet=False, log=None):
    """把目标窗口抬到最前并激活。

    真机教训（2026-11-05，代价很大）：截图是"抓屏幕上那块矩形"，
    **被别的窗口盖住就抓到别人的界面**。实测元宝在 (352,104) 1216x839、
    Kimi 在 (352,120) 1216x808 —— 两个窗口几乎完全重叠，
    于是"截元宝"截出来的是 Kimi 的 KIMI 界面，而后续视觉定位会拿别人的界面去判定。

    SetForegroundWindow 跨进程有前台限制（会静默失败），所以借
    AttachThreadInput 挂到当前前台线程上，成功后再摘掉。
    返回 True 表示确认已成为前台窗口。"""
    _log = log or _default_log
    hwnd = hwnd_of(w)
    if not hwnd:
        return False
    if input_guard is not None:
        input_guard.note_target(hwnd)     # 登记目标窗口，恢复前要复核它还在且焦点回来
    try:
        import ctypes
        u = ctypes.windll.user32
        SW_RESTORE = 9
        for _attempt in range(4):
            if u.GetForegroundWindow() == hwnd:
                return True
            # 只有真最小化了才恢复；否则别乱动窗口状态
            try:
                if u.IsIconic(hwnd):
                    u.ShowWindow(hwnd, SW_RESTORE)
                    time.sleep(0.2)
            except Exception:
                pass
            # 第一招：Alt 键 + SetForegroundWindow。
            # Windows 只允许"当前拥有输入焦点"的进程改前台，后台进程硬切会被静默拒绝
            # （窗口只在任务栏闪一下）。先按一下 Alt 伪造一次用户输入，就能把门推开。
            # 实测：单按 Alt 再 SetForegroundWindow，智谱/豆包都能一次切过去。
            try:
                VK_MENU = 0x12
                KEYEVENTF_KEYUP = 0x2
                if input_guard is not None:
                    input_guard.assert_active()   # 暂停时连切窗口的按键也不发
                u.keybd_event(VK_MENU, 0, 0, 0)
                time.sleep(0.03)
                u.SetForegroundWindow(hwnd)
                time.sleep(0.03)
                u.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
            except Exception:
                pass
            time.sleep(0.3)
            if u.GetForegroundWindow() == hwnd:
                if not quiet:
                    _log(f"已把{app or '窗口'}抬到最前")
                return True
            # 第二招：SwitchToThisWindow。不受前台锁定影响，
            # 但它会把窗口"切换"过去，不动 Z 序以外的东西。
            try:
                u.SwitchToThisWindow(hwnd, True)
            except Exception:
                pass
            time.sleep(0.35)
            if u.GetForegroundWindow() == hwnd:
                if not quiet:
                    _log(f"已把{app or '窗口'}抬到最前（SwitchToThisWindow）")
                return True
            # 还不够：借当前前台线程的输入状态再抢一次（跨进程前台限制）
            fg = u.GetForegroundWindow()
            if fg:
                ftid = u.GetWindowThreadProcessId(fg, None)
                ttid = u.GetWindowThreadProcessId(hwnd, None)
                if ftid and ttid and ftid != ttid:
                    attached = False
                    try:
                        attached = bool(u.AttachThreadInput(ftid, ttid, True))
                        if attached:
                            u.BringWindowToTop(hwnd)
                            u.SetForegroundWindow(hwnd)
                            u.SetActiveWindow(hwnd)
                    except Exception:
                        pass
                    finally:
                        if attached:
                            try:
                                u.AttachThreadInput(ftid, ttid, False)
                            except Exception:
                                pass
                    time.sleep(0.3)
                    if u.GetForegroundWindow() == hwnd:
                        if not quiet:
                            _log(f"已把{app or '窗口'}抬到最前（借用前台线程）")
                        return True
        if not quiet:
            _log(f"⚠ {app or '窗口'}没能成为前台窗口，截图可能抓到别的窗口")
    except Exception as e:
        _log(f"前置窗口失败: {e}")
    return False


def wake_from_tray(kws, log=None):
    """把"关到托盘"的软件主窗口叫回来。

    真机教训（2026-11-05）：Kimi/元宝/智谱 点右上角关闭后**进程还在**，只是窗口变隐藏态。
    而 pygetwindow 只枚举可见窗口 → find_window 返回 None → 误报"软件没开"，
    用户看到的就是"我明明开着，它说没开"。
    探针实测 ShowWindow(SW_RESTORE)+SetForegroundWindow 能可靠恢复（元宝 1216x839 一次成功）。

    返回恢复后的窗口标题；没有可恢复的返回 None。"""
    _log = log or _default_log
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        SW_RESTORE = 9
        hits = []
        CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _lp):
            try:
                n = u.GetWindowTextLengthW(hwnd)
                if not n:
                    return True
                buf = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(hwnd, buf, n + 1)
                title = buf.value
                low = title.lower()
                if not any(str(k).lower() in low for k in kws):
                    return True
                if any(x in low for x in TRAY_NOISE):
                    return True
                if u.IsWindowVisible(hwnd):
                    return True          # 已经可见，交给 pygetwindow 那一路
                r = wintypes.RECT()
                u.GetWindowRect(hwnd, ctypes.byref(r))
                ww, hh = r.right - r.left, r.bottom - r.top
                if ww < 300 or hh < 300:  # 太小，不像主界面
                    return True
                hits.append((ww * hh, hwnd, title))
            except Exception:
                pass
            return True

        u.EnumWindows(CB(_cb), 0)
        if not hits:
            return None
        hits.sort(reverse=True)          # 面积最大优先：主窗比框架窗大
        _area, hwnd, title = hits[0]
        u.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.4)
        try:
            u.SetForegroundWindow(hwnd)
        except Exception:
            pass
        time.sleep(0.7)
        return title if u.IsWindowVisible(hwnd) else None
    except Exception as e:
        _log(f"托盘唤醒失败（不影响后续）: {e}")
        return None


__all__ = ["TRAY_NOISE", "hwnd_of", "focus_window", "wake_from_tray"]
