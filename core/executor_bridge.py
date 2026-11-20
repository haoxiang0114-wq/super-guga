# -*- coding: utf-8 -*-
"""执行器窗口能力桥 — 纯新增模块，不改本地既有实现。

做什么
----
本地 ``core/computer_control._attach_focus(hwnd)`` 只做
「AttachThreadInput + BringWindowToTop + SetForegroundWindow」一招，跨进程前台被
Windows 静默拒绝时就失败（窗口只在任务栏闪一下）。

本桥把它改成**本地先试、失败再兜底**：兜底用队友执行器里真机验证过的三段式
（Alt 伪造用户输入 → SwitchToThisWindow → 借用前台线程），并带 4 次重试。

铁律
----
1. 本地实现先跑；成功就直接返回，行为与现在完全一致。
2. 只有本地返回 False 才走兜底，且兜底失败仍返回 False。
3. 不安装 bridge 时，本模块与 ``core.executor_compat`` 不会被任何代码调用。
"""
import logging

logger = logging.getLogger(__name__)

_INSTALLED = False


def install():
    """给 computer_control._attach_focus 装兜底。幂等；返回是否本次安装。"""
    global _INSTALLED
    if _INSTALLED:
        return False
    try:
        import core.computer_control as computer_control
    except Exception:
        logger.exception("computer_control unavailable; executor bridge skipped")
        return False

    original = getattr(computer_control, "_attach_focus", None)
    if original is None or getattr(original, "_executor_bridge", False):
        _INSTALLED = True
        return False

    try:
        from core import executor_compat
    except Exception:
        logger.exception("executor_compat unavailable; executor bridge skipped")
        return False

    def _attach_focus(hwnd):
        try:
            if original(hwnd):
                return True
        except Exception:
            logger.exception("original _attach_focus failed")
        try:
            ok = bool(executor_compat.focus_window(int(hwnd), quiet=True))
            if ok:
                logger.info("foreground recovered by executor fallback (hwnd=%s)", hwnd)
            return ok
        except Exception:
            logger.exception("executor fallback focus failed")
            return False

    _attach_focus._executor_bridge = True
    computer_control._attach_focus = _attach_focus
    _INSTALLED = True
    logger.info("executor window-focus fallback installed")
    return True


def wake_from_tray(keywords):
    """把「关到托盘」的软件主窗口叫回来（队友真机验证过的实现）。

    返回恢复后的窗口标题；没有可恢复的返回 None。
    """
    try:
        from core import executor_compat
        return executor_compat._wake_from_tray(tuple(keywords or ()))
    except Exception:
        logger.exception("wake_from_tray failed")
        return None
