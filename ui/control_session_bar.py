# -*- coding: utf-8 -*-
"""桌宠旁边的「咕嘎正在控制电脑」标识条 + 全局中止热键。

全局热键主用 Ctrl+Alt+Q，被占用时自动顺延到备用 Ctrl+Shift+Q
（本机实测 Ctrl+Alt+Z 常被其它软件占用，故不选它）；
两个都注册失败时，标识条会明确提示改用条上的「中止」按钮。
标识条按钮本身始终可用，作为最后兜底。

界面约束：
  * 标识条贴在桌宠窗口侧面，不遮挡宠物本体，也不出现在屏幕中央；
  * 不抢焦点、不激活窗口（WindowDoesNotAcceptFocus + ShowWithoutActivating），
    但按钮本身可点，鼠标停在标识上才吃点击；
  * 只在控制会话进行中出现，结束/中止后短暂显示真实结果再消失；
  * 自身只按状态变化重绘（每秒最多一次计时刷新），不参与宠物动画主循环。

状态来源：core.control_session（界面写、调度读）。
"""
import ctypes
import ctypes.wintypes
import logging
import sys
import time

from PyQt5.QtCore import QTimer, Qt, QObject, pyqtSignal, QAbstractNativeEventFilter
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt5.QtWidgets import QWidget, QPushButton, QApplication

from core import control_session as cs

logger = logging.getLogger(__name__)

BAR_W, BAR_H = 344, 104
TERMINAL_H = 72
GAP = 6

ACCENT = {
    cs.RUNNING:  QColor('#e0a93b'),
    cs.PAUSING:  QColor('#f0c24a'),
    cs.PAUSED:   QColor('#4d8bff'),
    cs.STOPPING: QColor('#e05252'),
    cs.STOPPED:  QColor('#e05252'),
    cs.DONE:     QColor('#57c271'),
    cs.ERROR:    QColor('#e05252'),
}

HIDE_AFTER = {cs.STOPPED: 6.0, cs.DONE: 4.0, cs.ERROR: 6.0}


def _fmt(sec):
    sec = max(0, int(sec))
    return '%02d:%02d' % (sec // 60, sec % 60)


def describe(snap, hotkey=None):
    """把状态快照翻成标识条上的字。抽成纯函数，便于不依赖像素的测试。

    hotkey: ControlSessionHotkey.status() 的结果，或 None（未接线时按主键展示）。
    注册失败时必须让用户看到「热键不可用」，并指向条上的中止按钮。
    """
    state = snap.get('state') or cs.IDLE
    app = str(snap.get('app') or '').strip()
    step = str(snap.get('step') or '').strip()
    note = str(snap.get('note') or '').strip()
    total = int(snap.get('total') or 0)
    no = int(snap.get('step_no') or 0)
    hotkey_name = (hotkey or {}).get('key') or 'Ctrl+Alt+Q'
    hotkey_ok = True if hotkey is None else bool(hotkey.get('registered'))
    unavailable = '热键不可用，请用条上的中止按钮'

    if state == cs.RUNNING:
        title = '正在操作 ' + app if app else '正在操作电脑'
        if step:
            sub = ('第 %d 步 %s' % (no, step)) if no else step
            if total:
                sub += '  ·  共 %d 步' % total
        else:
            sub = '正在读取界面，准备下一步'
    elif state == cs.PAUSING:
        title = '正在暂停…'
        sub = '会在这一步结束后停手，鼠标位置保留'
    elif state == cs.PAUSED:
        title = '已暂停 · 已停手'
        sub = note or '可以继续，或者中止这次控制'
    elif state == cs.STOPPING:
        title = '正在中止…'
        sub = '收到中止请求，不再执行后续操作'
    elif state == cs.STOPPED:
        title = '已中止 · 不会继续操作'
        sub = note or ('执行器已收到中止指令' if snap.get('source') == 'hotkey' else '这次控制没有完成')
    elif state == cs.DONE:
        title = '控制已完成'
        sub = note or '本次电脑控制结束，已回到待命'
    elif state == cs.ERROR:
        title = '控制中断'
        sub = note or '控制已中断：原因与失败码见任务记录；可以说“刚才为什么失败”'
    else:
        return None

    if state == cs.RUNNING:
        hint = ('暂停：左侧按钮　·　中止：%s　·　已用时 ' % hotkey_name + _fmt(snap.get('elapsed') or 0)) if hotkey_ok \
            else (unavailable + '　·　已用时 ' + _fmt(snap.get('elapsed') or 0))
    elif state == cs.PAUSING:
        hint = ('中止：左侧按钮或 %s' % hotkey_name) if hotkey_ok else unavailable
    elif state == cs.PAUSED:
        hint = ('继续：左侧按钮　·　中止：%s' % hotkey_name) if hotkey_ok else ('继续：左侧按钮　·　' + unavailable)
    else:
        hint = '没有新的操作在执行'
    warn = bool(not hotkey_ok and state in (cs.RUNNING, cs.PAUSING, cs.PAUSED))
    return dict(state=state, title=title, sub=sub, hint=hint, hint_warn=warn,
                accent=ACCENT.get(state, ACCENT[cs.RUNNING]))


class ControlSessionBar(QWidget):
    """贴在桌宠侧面的控制标识条。由 CompWindow 创建，跟随宠物位置。"""

    def __init__(self, pet=None, hotkey=None):
        super().__init__()
        self._pet = pet
        self._hotkey = hotkey
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFixedSize(BAR_W, BAR_H)

        self._info = None
        self._last_revision = -1
        self._last_second = -1
        self._hide_at = 0.0
        self._hushed_revision = -1

        self._pause_btn = QPushButton('暂停', self)
        self._stop_btn = QPushButton('中止', self)
        for btn in (self._pause_btn, self._stop_btn):
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(
                'QPushButton{background:#2a3142;color:#dfe6f3;border:1px solid #454e66;'
                'border-radius:6px;font:600 11px "Microsoft YaHei UI";padding:2px 10px}'
                'QPushButton:hover{background:#3a4459}'
                'QPushButton:disabled{color:#6c7387;border-color:#333a4b}')
            btn.setFocusPolicy(Qt.NoFocus)
        self._pause_btn.clicked.connect(self._on_pause)
        self._stop_btn.clicked.connect(self._on_stop)
        self._pause_btn.hide()
        self._stop_btn.hide()

        self._timer = QTimer(self)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self.poll)
        self._timer.start()
        self.hide()

    # ---------------- 按钮 ----------------

    def _on_pause(self):
        snap = cs.session.snapshot()
        if snap['state'] == cs.PAUSED:
            cs.session.resume('user')
        else:
            cs.session.request_pause('user')
        self.poll()

    def _on_stop(self):
        cs.session.request_stop('user')
        self.poll()

    def set_hotkey(self, hotkey):
        """接入热键状态；注册结果变化后下一次 poll 就会反映到提示行。"""
        self._hotkey = hotkey
        self._last_revision = -1

    # ---------------- 状态同步 ----------------

    def poll(self):
        snap = cs.session.snapshot()
        state = snap['state']
        if state == cs.IDLE:
            self.hide()
            return
        if self._pet is not None and not self._pet.isVisible():
            self.hide()
            return

        now = time.time()
        if state in cs.TERMINAL:
            if self._hushed_revision != snap['revision']:
                self._hushed_revision = snap['revision']
                self._hide_at = now + HIDE_AFTER.get(state, 5.0)
            if now >= self._hide_at:
                self.hide()
                return

        second = int(snap.get('elapsed') or 0)
        if snap['revision'] != self._last_revision or second != self._last_second:
            self._last_revision = snap['revision']
            self._last_second = second
            hotkey = self._hotkey.status() if self._hotkey is not None else None
            self._apply(snap, hotkey)
        if self.isHidden():
            self.follow()
            self.show()
            self.raise_()
        else:
            self.follow()

    def _apply(self, snap, hotkey=None):
        self._info = describe(snap, hotkey)
        if not self._info:
            self.hide()
            return
        active_controls = snap['state'] in (cs.RUNNING, cs.PAUSING, cs.PAUSED)
        self.setFixedSize(BAR_W, BAR_H if active_controls else TERMINAL_H)
        self._pause_btn.setVisible(active_controls and snap['state'] != cs.PAUSING)
        self._pause_btn.setText('继续' if snap['state'] == cs.PAUSED else '暂停')
        self._stop_btn.setVisible(active_controls)
        self._pause_btn.setEnabled(snap['state'] in (cs.RUNNING, cs.PAUSED))
        # 中止按钮是热键失效时的最后兜底，任何情况下都不禁用。
        self._stop_btn.setEnabled(True)
        self._place_buttons()
        self.update()

    def _place_buttons(self):
        h = self.height()
        y = h - 30
        x = BAR_W - 12 - 52
        self._stop_btn.setGeometry(x, y, 52, 24)
        self._pause_btn.setGeometry(x - 8 - 52, y, 52, 24)

    def follow(self):
        """贴在桌宠左侧；左边放不下就换右侧；始终留在当前屏幕可用区域内。"""
        pet = self._pet
        if pet is None:
            return
        geo = pet.frameGeometry()
        screen = None
        try:
            screen = QApplication.screenAt(geo.center())
        except Exception:
            screen = None
        if screen is None:
            try:
                screen = QApplication.primaryScreen()
            except Exception:
                screen = None
        avail = screen.availableGeometry() if screen else geo
        x = geo.left() - self.width() - GAP
        if x < avail.left() + 4:
            x = geo.right() + GAP
        x = max(avail.left() + 4, min(x, avail.right() - self.width() - 4))
        y = geo.top() + 8
        y = max(avail.top() + 4, min(y, avail.bottom() - self.height() - 4))
        self.move(int(x), int(y))

    def info(self):
        """测试用：当前标识条该显示的文字。"""
        return dict(self._info or {})

    # ---------------- 绘制 ----------------

    def paintEvent(self, event):
        info = self._info
        if not info:
            return
        p = QPainter(self)
        try:
            self.paint_bar(p, info)
        finally:
            p.end()

    def paint_bar(self, p, info):
        p.setRenderHint(QPainter.Antialiasing, True)
        accent = info['accent']
        w, h = self.width(), self.height()
        p.setPen(QPen(QColor(58, 65, 82, 230), 1))
        p.setBrush(QColor(24, 28, 38, 238))
        p.drawRoundedRect(1, 1, w - 2, h - 3, 12, 12)
        p.setPen(Qt.NoPen)
        p.setBrush(accent)
        p.drawRoundedRect(1, 10, 4, h - 22, 2, 2)

        tall = h >= BAR_H
        title_y = 27 if tall else 24
        sub_y = 49 if tall else 46
        hint_y = 69 if tall else h - 10

        p.setBrush(QColor(accent))
        p.drawEllipse(14, title_y - 11, 10, 10)

        f_title = QFont('Microsoft YaHei UI', 10)
        f_title.setBold(True)
        p.setFont(f_title)
        p.setPen(QColor('#e8ecf4'))
        p.drawText(32, title_y, _elide(f_title, info['title'], w - 44))

        f_sub = QFont('Microsoft YaHei UI', 8)
        p.setFont(f_sub)
        p.setPen(QColor('#9aa3b5'))
        p.drawText(32, sub_y, _elide(f_sub, info['sub'], w - 44))

        if info['hint']:
            f_hint = QFont('Microsoft YaHei UI', 7)
            warn = bool(info.get('hint_warn'))
            if warn:
                f_hint.setBold(True)
            p.setFont(f_hint)
            p.setPen(QColor('#f0b45a' if warn else '#7f899c'))
            p.drawText(12, hint_y, _elide(f_hint, info['hint'], w - 24))


def _elide(font, text, width):
    return QFontMetrics(font).elidedText(str(text), Qt.ElideRight, max(40, width))


class ControlSessionHotkey(QObject, QAbstractNativeEventFilter):
    """全局中止热键：主用 Ctrl+Alt+Q，被占用时顺延到备用 Ctrl+Shift+Q。

    只有 RegisterHotKey 真正返回成功才算可用；两个都失败时 registered=False，
    由标识条提示用户改用条上的「中止」按钮。
    """

    triggered = pyqtSignal(str, bool)
    HOTKEY_ID = 0x6A73
    PRIMARY = 'Ctrl+Alt+Q'
    FALLBACK = 'Ctrl+Shift+Q'
    VK_Q = 0x51
    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_NOREPEAT = 0x4000
    CANDIDATES = ((PRIMARY, VK_Q, MOD_CONTROL | MOD_ALT),
                  (FALLBACK, VK_Q, MOD_CONTROL | MOD_SHIFT))

    def __init__(self, parent=None):
        super().__init__(parent)
        self.registered = False
        self.combo_name = None
        self.attempts = []
        self.heartbeat = QTimer(self)
        self.heartbeat.setSingleShot(True)
        self.heartbeat.timeout.connect(self.fire)
        self._register()

    def _register_once(self, mods, vk):
        return bool(ctypes.windll.user32.RegisterHotKey(None, self.HOTKEY_ID, mods, vk))

    def _register(self):
        """按 主键 → 备用键 的顺序实际注册；记录每次尝试，绝不假装成功。"""
        self.registered = False
        self.combo_name = None
        self.attempts = []
        if sys.platform != 'win32':
            logger.warning('当前系统不支持全局中止热键，请用控制标识条上的中止按钮')
            return
        try:
            for name, vk, mods in self.CANDIDATES:
                for extra in (self.MOD_NOREPEAT, 0):
                    try:
                        ok = self._register_once(mods | extra, vk)
                    except Exception as exc:
                        self.attempts.append(dict(name=name, norepeat=bool(extra), ok=False,
                                                  error=type(exc).__name__))
                        continue
                    self.attempts.append(dict(name=name, norepeat=bool(extra), ok=ok))
                    if ok:
                        self.registered = True
                        self.combo_name = name
                        break
                if self.registered:
                    break
        except Exception as exc:
            self.attempts.append(dict(name='exception', ok=False, error=type(exc).__name__))
        if self.registered:
            app = QApplication.instance()
            if app is not None:
                app.installNativeEventFilter(self)
            if self.combo_name == self.PRIMARY:
                logger.info('中止热键已注册：%s', self.combo_name)
            else:
                logger.warning('%s 被占用，已改用备用中止热键 %s', self.PRIMARY, self.combo_name)
        else:
            logger.warning('中止热键注册失败：%s 与 %s 均不可用，请用控制标识条上的中止按钮',
                           self.PRIMARY, self.FALLBACK)

    def status(self):
        return dict(registered=bool(self.registered), key=self.combo_name,
                    primary=self.PRIMARY, fallback=self.FALLBACK, attempts=list(self.attempts))

    def nativeEventFilter(self, event_type, message):
        try:
            msg = ctypes.wintypes.MSG.from_address(int(message))
            if msg.message == 0x0312 and msg.wParam == self.HOTKEY_ID:
                self.heartbeat.start(0)
                return True, 0
        except Exception:
            pass
        return False, 0

    def fire(self):
        accepted = cs.session.request_stop('hotkey')
        self.triggered.emit('hotkey', bool(accepted))

    def stop(self):
        if self.registered:
            try:
                ctypes.windll.user32.UnregisterHotKey(None, self.HOTKEY_ID)
            except Exception:
                pass
            self.registered = False
            self.combo_name = None
            app = QApplication.instance()
            if app is not None:
                app.removeNativeEventFilter(self)
