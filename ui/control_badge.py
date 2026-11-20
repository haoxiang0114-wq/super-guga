# -*- coding: utf-8 -*-
"""屏幕级「咕嘎接管标识」— 桌宠操控电脑时在屏幕顶部常驻的一枚信号。

设计约束（都来自真机执行链路，不是视觉偏好）：
  * 鼠标完全穿透、不抢焦点、不激活窗口：GUI 执行器靠 PyAutoGUI 真实点击与
    粘贴，浮层一旦吃掉鼠标或抢走焦点，就会制造「三次粘贴输入框无变化」。
  * 固定顶部居中、高度 60px：避开屏幕四角，不触发 PyAutoGUI fail-safe 急停；
    也不遮挡常见应用的输入区。
  * 接管期间常驻并显示实时计时与流动脉冲：屏幕上一直有东西在动，
    才能把「还在跑」和「死机在原地」区分开。
  * 空闲时不占屏；只有派单/接管/异常时才出现。

状态全部由 CompWindow 接真实信号驱动，组件自身不臆造任务进度：
  dispatch  已派单，等待执行方领单（常驻，超时会如实提示）
  control   本机执行方正在操控电脑（常驻，计时累加）
  done      一单跑完（4 秒后隐去）
  error     派单失败 / 执行异常 / 云端断线（4 秒后隐去）
"""
import math
import os
import time

from PyQt5.QtCore import Qt, QTimer, QPoint
from PyQt5.QtGui import (QPainter, QColor, QFont, QFontMetrics, QPen,
                         QFontDatabase)
from PyQt5.QtWidgets import QWidget, QApplication

BADGE_W, BADGE_H = 468, 60
TOP_GAP = 12

# 与桌宠胸口任务灯 / 任务台账同一套语义色
COLORS = {
    'dispatch': QColor('#4d8bff'),
    'control':  QColor('#e0a93b'),
    'done':     QColor('#57c271'),
    'error':    QColor('#e05252'),
}
LABELS = {
    'dispatch': '咕嘎已派单',
    'control':  '咕嘎正在操控电脑',
    'done':     '这一单跑完了',
    'error':    '这一单没跑通',
}


def _ensure_fonts():
    """精简/离屏环境下 Qt 字体库可能为空，此时标识会只剩灯没有字。

    仅在字体族为空时补装系统字体；正常 Windows 桌面不会触发。
    """
    try:
        if QFontDatabase().families():
            return
        for name in ('msyh.ttc', 'msyhbd.ttc', 'consola.ttf', 'simhei.ttf'):
            path = os.path.join(os.environ.get('WINDIR', r'C:\Windows'),
                                'Fonts', name)
            if os.path.isfile(path):
                QFontDatabase.addApplicationFont(path)
    except Exception:
        pass


def _elide(painter_font, text, width):
    fm = QFontMetrics(painter_font)
    return fm.elidedText(str(text), Qt.ElideRight, width)


class ControlBadge(QWidget):
    """置顶、鼠标穿透的接管标识。用法: badge.set_state('control', detail=...)"""

    def __init__(self):
        super().__init__()
        _ensure_fonts()
        # Tool 不占任务栏；WindowDoesNotAcceptFocus + ShowWithoutActivating 防止抢焦点
        # 不用原生 WindowTransparentForInput：WA_TransparentForMouseEvents 在事件
        # 分发层就跳过鼠标，足够穿透，且避免 Windows/offscreen 平台窗口差异。
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFixedSize(BADGE_W, BADGE_H)

        self._state = ''
        self._detail = ''
        self._progress = ''
        self._t0 = 0.0
        self._elapsed = 0.0
        self._running_timer = False
        self._hide_at = 0.0
        self._alpha = 0.0

        self._tick = QTimer(self)
        self._tick.setInterval(100)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()

        self._reposition()

    # ---------------- 对外接口 ----------------

    def set_state(self, state, detail='', progress=''):
        """由 CompWindow 的真实信号调用。detail=当前子任务/原因，progress=子任务进度。"""
        if state not in COLORS:
            return
        prev = self._state
        self._state = state
        self._detail = detail or ''
        self._progress = progress or ''

        if state == 'control' and prev != 'control':
            # 真正开始操控电脑才从零计时
            self._t0 = time.time()
            self._elapsed = 0.0
            self._running_timer = True
        elif state == 'dispatch' and prev == '':
            self._t0 = time.time()
            self._elapsed = 0.0
            self._running_timer = True
        elif state in ('done', 'error'):
            self._running_timer = False
            self._hide_at = time.time() + 4.0
        else:
            # dispatch 态持续存在时计时继续走（用来暴露「一直没人领单」）
            pass

        self._alpha = 1.0
        self._reposition()
        self.show()
        self.raise_()
        self.update()

    def set_progress(self, progress, detail=None):
        """云端台账轮询到子任务变化时刷新，不改变状态与计时。"""
        if not self._state:
            return
        self._progress = progress or ''
        if detail:
            self._detail = detail
        self.update()

    def clear(self):
        self._state = ''
        self._running_timer = False
        self.hide()

    # ---------------- 内部 ----------------

    def _reposition(self):
        screen = QApplication.primaryScreen()
        if not screen:
            return
        g = screen.availableGeometry()
        x = g.left() + (g.width() - BADGE_W) // 2
        y = g.top() + TOP_GAP
        self.move(QPoint(x, y))

    def _on_tick(self):
        now = time.time()
        if self._running_timer:
            self._elapsed = now - self._t0
        if self._hide_at and now >= self._hide_at:
            self._hide_at = 0.0
            self.clear()
            return
        if self._state:
            # 每分钟校正一次位置，应对分辨率/显示器变化
            if int(now) % 60 == 0 and int(now) != getattr(self, '_last_pos_check', -1):
                self._last_pos_check = int(now)
                self._reposition()
            self.update()

    def _sub_line(self):
        if self._state == 'error':
            return self._detail or '请查看任务台账里的失败原因'
        if self._state == 'done':
            return self._detail or '等云端验收结果，随后回到待命'
        if self._state == 'control':
            # 进度放最前，保证任何宽度下都不会被省略号吃掉
            base = (self._progress + '   ·   ') if self._progress else ''
            return base + (self._detail or '正在按拆解出的步骤操作')
        # dispatch
        base = self._detail or '等待执行方领单'
        if self._elapsed >= 60:
            return '已等待超过 1 分钟 · 执行方可能未在线（台账可查）'
        return base

    @staticmethod
    def _fmt(sec):
        sec = max(0, int(sec))
        return '%02d:%02d' % (sec // 60, sec % 60)

    def paintEvent(self, event):
        if not self._state:
            return
        p = QPainter(self)
        try:
            self.paint_badge(p)
        finally:
            p.end()

    def paint_badge(self, p):
        """绘制本体。抽成独立方法，便于直接画到 QImage 做实渲验证。"""
        p.setRenderHint(QPainter.Antialiasing, True)
        accent = COLORS[self._state]

        # 卡片：与任务台账同色系深色半透明
        card = QColor(24, 28, 38, 238)
        p.setPen(QPen(QColor(58, 65, 82, 230), 1))
        p.setBrush(card)
        p.drawRoundedRect(1, 1, BADGE_W - 2, BADGE_H - 3, 14, 14)

        # 左侧竖色条：状态最远距离也可辨
        p.setPen(Qt.NoPen)
        p.setBrush(accent)
        p.drawRoundedRect(1, 12, 4, BADGE_H - 27, 2, 2)

        # 状态灯：呼吸光晕 + 实心芯（control 态持续跳动 = 程序还活着）
        t = time.time()
        pulse = (1.0 + math.sin(t * 3.2)) / 2.0
        glow = QColor(accent)
        glow.setAlpha(int(46 + 62 * pulse))
        p.setBrush(glow)
        p.drawEllipse(14, int(BADGE_H / 2) - 12, 24, 24)
        core = QColor(accent)
        p.setBrush(core)
        p.drawEllipse(21, int(BADGE_H / 2) - 5, 10, 10)

        # 主标题
        f_title = QFont('Microsoft YaHei UI', 10)
        f_title.setBold(True)
        p.setFont(f_title)
        p.setPen(QColor('#e8ecf4'))
        p.drawText(48, 24, _elide(f_title, LABELS[self._state], 300))

        # 副行：当前子任务 / 原因 / 等待提示
        f_sub = QFont('Microsoft YaHei UI', 8)
        p.setFont(f_sub)
        sub_color = QColor('#ff8f8f') if (self._state == 'dispatch' and self._elapsed >= 60) \
            else QColor('#9aa3b5')
        p.setPen(sub_color)
        p.drawText(48, 43, _elide(f_sub, self._sub_line(), 300))

        # 右侧计时：常驻跳动的数字，是「没死机」最直接的证据
        f_time = QFont('Consolas', 12)
        f_time.setBold(True)
        p.setFont(f_time)
        p.setPen(accent)
        p.drawText(BADGE_W - 92, 30, self._fmt(self._elapsed))
        f_tip = QFont('Microsoft YaHei UI', 7)
        p.setFont(f_tip)
        p.setPen(QColor('#7f899c'))
        p.drawText(BADGE_W - 92, 46, '已用时' if self._running_timer else '用时')

        # 底边流动亮带：亮度沿整条底边推进，任何时刻都完整可见
        # （用位移的短线会周期性跑到画布外，看起来像残缺）
        if self._state in ('control', 'dispatch'):
            p.setPen(Qt.NoPen)
            n, step = 30, (BADGE_W - 20) / 30.0
            phase = (t * 0.32) % 1.0
            for i in range(n):
                d = abs(((i / float(n)) - phase + 1.0) % 1.0)
                d = min(d, 1.0 - d)
                a = int(120 * max(0.0, 1.0 - d * 5.0))
                if a <= 0:
                    continue
                c = QColor(accent)
                c.setAlpha(a)
                p.setBrush(c)
                p.drawRoundedRect(int(10 + i * step), BADGE_H - 5,
                                  max(4, int(step) - 3), 2, 1, 1)
