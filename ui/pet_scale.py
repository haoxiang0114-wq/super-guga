# -*- coding: utf-8 -*-
"""桌宠大小的纯计算规则 —— 不依赖 Qt，方便无头测试。

滚轮调大小、右键「咕嘎大小…」对话框、多智能体小窗共用同一套边界：
40% 是下限（再小就糊成一团），100% 是原图上限（超过必然拉伸失真）。
"""

MIN_SCALE = 0.40
MAX_SCALE = 1.00
WHEEL_STEP = 0.05     # 一格滚轮 = 5%
NOTCH = 120           # Windows 一格滚轮的角度增量
PIXEL_NOTCH = 50      # 触摸板给的是像素增量，50px 当一格


def clamp_scale(value, minimum=MIN_SCALE, maximum=MAX_SCALE):
    """任何来源的尺寸都过这道闸：解析不了就回原尺寸，绝不返回 None。"""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return maximum
    return max(minimum, min(maximum, value))


def wheel_notches(angle_delta=0, pixel_delta=0):
    """把滚轮增量换算成「格」。角度增量优先，触摸板像素增量兜底。"""
    if angle_delta:
        return angle_delta / float(NOTCH)
    if pixel_delta:
        return pixel_delta / float(PIXEL_NOTCH)
    return 0.0


def next_scale(current, notches, step=WHEEL_STEP, minimum=MIN_SCALE, maximum=MAX_SCALE):
    """向上滚（notches > 0）变大，向下滚变小，结果对齐到 step 的整数倍。"""
    current = clamp_scale(current, minimum, maximum)
    if not notches:
        return current
    steps = int(round(notches))
    if not steps:
        # 触摸板的小增量也要有反应，但一次只走一格，避免手指一抖就缩一半
        steps = 1 if notches > 0 else -1
    bound = int(round((maximum - minimum) / step))
    level = int(round((current - minimum) / step)) + steps
    level = max(0, min(bound, level))
    return round(minimum + level * step, 4)


def percent_text(scale):
    """给徽标用的百分比文本：0.72 → '72%'。"""
    return '%d%%' % round(clamp_scale(scale) * 100)


def anchored_top_left(anchor, local, old_size, new_size):
    """缩放时把指针底下的那一点钉住，返回新的窗口左上角。

    anchor: 指针的全局坐标；local: 指针在窗口内的坐标（缩放前量的）；
    不做这一步的话，往下滚缩小会把窗口从光标底下抽走，连滚两格就断。
    """
    anchor_x, anchor_y = anchor
    old_w = max(1.0, float(old_size[0]))
    old_h = max(1.0, float(old_size[1]))
    fx = min(1.0, max(0.0, float(local[0]) / old_w))
    fy = min(1.0, max(0.0, float(local[1]) / old_h))
    return (int(round(anchor_x - fx * new_size[0])),
            int(round(anchor_y - fy * new_size[1])))
