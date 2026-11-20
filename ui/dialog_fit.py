# -*- coding: utf-8 -*-
"""窗口尺寸适配：按屏幕可用区域收口，避免 150% 缩放下窗口比屏幕还高。

约定：
- 先用 availableGeometry() 算可用宽高，再留出边距给标题栏/任务栏；
- 任何窗口都不允许超过可用高度，超出部分由调用方放进 QScrollArea；
- 底栏按钮始终留在滚动区外，缩放到任何档位都能点到。
"""
from __future__ import annotations

from PyQt5.QtCore import QRect
from PyQt5.QtWidgets import QApplication

# 左右各留 24px，上下各留 40px；再额外扣 28px 给窗口标题栏。
SIDE_MARGIN = 24
VERT_MARGIN = 40
TITLE_BAR = 28


def available_size():
    screen = QApplication.primaryScreen()
    if screen is not None:
        rect = screen.availableGeometry()
        if rect.isValid() and rect.width() > 0 and rect.height() > 0:
            return rect
    return QRect(0, 0, 1280, 800)


def fit_size(desired_width, desired_height):
    """返回 (宽, 高)：不超过屏幕可用区域，同时保留最小可操作尺寸。"""
    rect = available_size()
    max_width = max(360, rect.width() - SIDE_MARGIN * 2)
    max_height = max(300, rect.height() - VERT_MARGIN * 2 - TITLE_BAR)
    return min(int(desired_width), max_width), min(int(desired_height), max_height)


def fit_dialog(dialog, width, height, min_width=360, min_height=300):
    """把窗口收进屏幕可用区域；返回实际尺寸，便于测试断言。

    关键点：必须显式 setMinimumSize。QLayout 默认约束会把窗口最小尺寸顶到
    布局自身的最小高度，内容多的窗口就会被撑回超屏高度，setMaximumSize 也会失效。
    """
    rect = available_size()
    max_w = max(240, rect.width() - SIDE_MARGIN * 2)
    max_h = max(200, rect.height() - VERT_MARGIN * 2 - TITLE_BAR)
    # 最小尺寸本身也不能超过可用区，否则小屏/高缩放下仍会溢出。
    min_width = min(max(int(min_width), 240), max_w)
    min_height = min(max(int(min_height), 200), max_h)
    dialog.setMinimumSize(min_width, min_height)
    dialog.setMaximumSize(max_w, max_h)
    w = min(max(int(width), min_width), max_w)
    h = min(max(int(height), min_height), max_h)
    dialog.resize(w, h)
    # 居中到可用区域，避免出现在任务栏下面。
    dialog.move(rect.left() + (rect.width() - dialog.width()) // 2,
                rect.top() + max(0, (rect.height() - dialog.height()) // 2))
    return dialog.width(), dialog.height()
