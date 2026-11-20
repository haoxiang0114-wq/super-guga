# -*- coding: utf-8 -*-
"""执行器窗口能力兼容层 —— 转发到 core.win_focus 的单一实现。

历史：本模块曾是从队友 agent/guga_executor.py AST 原样移植的一份拷贝，
两边逻辑逐字相同（约 185 行），改一处必然漏一处；现已抽到
``core/win_focus.py``，这里只保留对外名字和 core 侧的 logging 出口。

本地既有代码不直接引用本模块；由 core/executor_bridge.py 在启动时挂成
``core.computer_control._attach_focus`` 的兜底路径（本地先试，失败才用这套）。
对现有能力的影响：无。不安装 bridge 时本模块不会被任何代码调用。
"""
import logging

from core.win_focus import TRAY_NOISE as _TRAY_NOISE
from core.win_focus import focus_window as _focus_window
from core.win_focus import hwnd_of as _hwnd_of
from core.win_focus import wake_from_tray as _wake_from_tray_impl

logger = logging.getLogger(__name__)


def log(msg):
    """队友模块里的 log() 等价实现。"""
    logger.info(str(msg))


def focus_window(w, app="", quiet=False):
    """把目标窗口抬到最前并激活（实现见 core.win_focus）。"""
    return _focus_window(w, app=app, quiet=quiet, log=log)


def _wake_from_tray(kws):
    """把"关到托盘"的软件主窗口叫回来（实现见 core.win_focus）。"""
    return _wake_from_tray_impl(tuple(kws or ()), log=log)


__all__ = ["focus_window", "_wake_from_tray", "_hwnd_of", "_TRAY_NOISE"]
