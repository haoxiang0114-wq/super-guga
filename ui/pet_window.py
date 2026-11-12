# -*- coding: utf-8 -*-
"""咕嘎企鹅桌宠主窗口 — 透明悬浮（企鹅精灵图动画 + 视觉 + 语音）"""
import ctypes, os, sys, logging, random, math, threading

logging.basicConfig(
    filename=os.path.join(os.path.dirname(__file__), '..', 'pet_debug.log'),
    level=logging.WARNING, format='%(asctime)s %(message)s'
)

from PyQt5.QtCore import Qt, QPoint, QTimer, QRect, QElapsedTimer, pyqtSignal
from PyQt5.QtGui import (
    QPixmap, QBitmap, QIcon, QPainter, QColor, QFont,
    QFontMetrics, QPen, QPainterPath, QImage, QRadialGradient
)
from PyQt5.QtWidgets import (
    QWidget, QMenu, QSystemTrayIcon, QApplication, QFrame, QPlainTextEdit, QHBoxLayout, QLabel
)

from ui.bongo_panel import BongoPanel
from ui.sprite_animator import SpriteAnimator
from ui.tts_engine import TTSEngine
from ui.speech_system import get_speech, SPEECHES
from core.ai_chat import AIChatEngine, ANALYSIS_OVERRIDE, WINRATE_OVERRIDE, OUTLOOK_OVERRIDE
from core.keyboard_hook import KeyboardHook
from core.status_system import StatusSystem
from core.interaction import get_action, ACTIONS
from core.config_manager import ConfigManager
from core.embodiment import IDLE_GESTURES

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
SWP_NOSIZE = 1; SWP_NOMOVE = 2; SWP_NOACTIVATE = 16; SWP_SHOWWINDOW = 64
HWND_TOPMOST = -1; HWND_NOTOPMOST = -2


def _proxy_opponents(tier: int):
    """招募阶段对手不可见：从该酒馆等级【真实卡牌池】随机生成一个中期阵容，
    让胜率模拟有参考值（BobsBuddy 思路：对平均对手算胜率）"""
    from core.battle_sim import Minion
    from core import card_db
    import random as _rnd
    pool = card_db.minions_at_tier(max(1, tier))
    if not pool:
        pool = ['BG25_008', 'BG28_309', 'BG26_174']
    rng = _rnd.Random()
    team = []
    for i in range(7):
        cid = rng.choice(pool)
        db = card_db.lookup(cid)
        if not db:
            continue
        atk, hp = db['atk'], db['hp']
        # 中期阵容成长模拟：按酒馆等级给身材加成（参考该等级成型节奏）
        grow = {1: 0, 2: 0, 3: 1, 4: 2, 5: 4, 6: 6}.get(max(1, tier), 0)
        kw = {}
        if db.get('deathrattle'):
            kw['deathrattle'] = [db['deathrattle']]
        # 嘲讽/圣盾关键词从卡牌描述推导（简化：按身材比例随机给）
        r = rng.random()
        if r < 0.25 and atk >= 4:
            kw['taunt'] = True
        elif r < 0.4 and atk >= 5:
            kw['divine_shield'] = True
        team.append(Minion(name=db['name'], atk=atk + grow, hp=hp + grow, **kw))
    return team[:7]


class PetWindow(QWidget):
    RES_DIR = os.path.join(os.path.dirname(__file__), '..', 'resources')
    # 视觉分析结果信号（后台线程 → 主线程，避免跨线程操作 Qt 界面）
    _vision_done = pyqtSignal(str)
    # 战棋分数识别结果信号（独立于助教 _vision_done, 避免冲突）
    _bg_rating_done = pyqtSignal(str)

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self._config = ConfigManager()
        from core.agent_memory import AgentMemoryStore
        self._agent_memory = AgentMemoryStore()
        from ui.multi_agent_window import MultiAgentManager
        self._multi_agent=MultiAgentManager(self,self._agent_memory)

        # 窗口属性
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setMouseTracking(True)

        # Appearance is independent of AI credentials and task execution.
        # Both backends have the same logical canvas; switching keeps bubbles.
        self._anim3d = None
        self._anim2d = SpriteAnimator(self.RES_DIR, display_width=250)
        self._anim = self._anim2d
        self._appearance_error = ''
        self._set_avatar_mode('illustration' if '--avatar-2d' in sys.argv else self._config.avatar_mode,
                              persist=False)
        self._iw = self._anim.width
        self._ih = self._anim.height
        self._pet_scale = self._config.pet_scale
        self._anim.on_update = self.update
        self._anim.start()
        from ui.pet_physics import DesktopPetPhysics
        self._pet_physics=DesktopPetPhysics(self,self._anim2d,lambda:self._config.save_pos(self.x(),self.y()))
        self._gaze_timer = QTimer(self)
        self._gaze_timer.setInterval(160)
        self._gaze_timer.timeout.connect(self._update_avatar_gaze)
        self._gaze_timer.start()

        # --- TTS 语音引擎 ---
        self._tts = TTSEngine()
        self._tts._player.setVolume(self._config._s.value('agent/volume', 80, type=int))
        self.setWindowOpacity(self._config.opacity)
        self._tts.speak_started.connect(self._on_tts_started)
        self._tts.speak_finished.connect(self._on_tts_finished)

        # --- 整体弹跳偏移 ---
        self._anim_offset = QPoint(0, 0)
        self._anim_start = QElapsedTimer()
        self._anim_type = ""
        self._anim_duration = 300
        self._bounce_timer = QTimer(self)
        self._bounce_timer.timeout.connect(self._bounce_tick)
        self._bounce_timer.setInterval(16)

        # --- 说话嘴巴控制 ---
        self._talk_timer = QTimer(self)
        self._talk_timer.setInterval(80)
        self._talk_timer.timeout.connect(self._talk_tick)

        # --- 气泡 ---
        self._bubble_text = ""
        self._bubble_timer = QTimer(self)
        self._bubble_timer.setSingleShot(True)
        self._bubble_timer.timeout.connect(self._hide_bubble)
        self._chat_panel = None
        self._agent_panel = None
        self._desktop_hidden = False
        self._floating_launcher = None
        self._pending_voice_gesture = None
        from ui.speech_bubble import PetSpeechBubble
        self._agent_bubble = PetSpeechBubble(self)
        self._agent_bubble.clicked.connect(self._on_bubble_clicked)
        self._agent_bubble.end_call.connect(self._end_agent_voice)
        self._agent_bubble.talk_started.connect(self._start_bubble_talk)
        self._agent_bubble.talk_stopped.connect(self._finish_bubble_talk)
        self._chat_view = None
        self._chat_history = []
        self._input_mode = "chat"
        self._tutor_pending_text = None   # 等待屏幕分析的问题
        self._tutor_vision_done = False   # 屏幕分析是否完成
        self._chat_title = None
        self._chat_drag_pos = QPoint()

        # --- 鼠标 ---
        self._drag_pos = QPoint()
        self._is_drag = False

        # --- UI ---
        self._init_sub_widgets()
        self._init_tray()
        # Warm the native QMenu after startup so the first right-click does not
        # pay stylesheet/DWM window creation latency.
        self._pet_menu = None
        QTimer.singleShot(1200, self._ensure_pet_menu)
        # The full AI workspace is lazy-loaded on the first chat/voice action.
        # Startup only creates the lightweight pet, tray and local animation.

        # --- 核心 ---
        self._status = StatusSystem(self)
        self._status.mood_changed.connect(self._on_mood_changed)
        self._status.start()

        self._hook = KeyboardHook(self)
        self._hook.key_pressed.connect(self._on_key)
        self._hook.start()

        # --- AI 大脑（DeepSeek，酒馆战棋大师 + 娇憨调皮人设）---
        self._ai = AIChatEngine(self)
        self._ai.reply_ready.connect(self._on_ai_reply)
        self._ai.failed.connect(self._on_ai_fail)
        from ui.mcp_bridge import SelfMCPBridge
        self._self_mcp = SelfMCPBridge(self)
        self._vision_done.connect(self._on_vision_result)
        self._bg_rating_done.connect(self._on_bg_rating_done)

        # --- 悬停查分(右键开关): 轮询鼠标悬停的玩家, 显示其分数 ---
        self._hover_rating_on = False
        self._last_hovered_id = 0
        self._hover_timer = QTimer(self)
        self._hover_timer.setInterval(500)
        self._hover_timer.timeout.connect(self._poll_hovered_rating)

        # --- 打字/语音直接交流（无对话框）---
        self._inline_input = None      # 迷你输入条（企鹅下方）
        self._voice_mode = False       # 按住说话模式
        self._asr = None
        self._recording = False
        self._voice_record_timer = QTimer(self)
        self._voice_record_timer.setSingleShot(True)
        self._voice_record_timer.timeout.connect(self._stop_voice)
        self._realtime_voice = None
        self._voice_call_panel = None
        # _vision_done 只连助教槽; 战棋看牌单独走 _see_board 回调, 避免互相覆盖

        # --- 背景亮度自适应（深色桌面给黑猫加光晕，浅色桌面加深阴影）---
        # 截屏放到独立线程执行，避免无桌面环境（远程/锁屏）下 ImageGrab 挂起 UI
        self._bg_luminance = 0.5
        self._bg_sampling = False
        self._bg_fail_count = 0
        self._bg_timer = QTimer(self)
        self._bg_timer.setInterval(60000)
        self._bg_timer.timeout.connect(self._sample_background)
        self._bg_timer.start()

        self._save_timer = QTimer(self)
        # --- 自动观战：轮询日志，战斗开始自动给胜率+建议（及时性核心） ---
        self._watch_timer = QTimer(self)
        self._watch_timer.setInterval(30000)
        self._watch_timer.timeout.connect(self._auto_watch)
        self._watch_timer.start()
        self._watch_phase = ''       # 上次观测到的阶段: ''/'recruit'/'combat'
        self._watch_last_analyze = 0.0  # 上次自动分析时间戳
        self._watch_analyzing = False
        self._save_timer.setInterval(60000)
        self._save_timer.timeout.connect(self._save_state)
        self._save_timer.start()

        # --- 启动 ---
        self.setFixedSize(round(self._iw*self._pet_scale), round(self._ih*self._pet_scale))
        screen = QApplication.primaryScreen()
        if screen:
            g = screen.geometry()
            self.move(int(g.width() * 0.7), int(g.height() * 0.4))
        self.show()
        self.raise_()
        self.activateWindow()
        self._force_top()
        self._click_count = 0

        self._load_state()
        from ui.screen_tools import ScreenTools
        self._screen_tools=ScreenTools(self,register=QApplication.platformName()!='offscreen')
        if self._config._s.value('desktop/pet_hidden', False, type=bool):
            self.set_pet_hidden(True)

        QTimer.singleShot(200, self._force_top)
        # 咕嘎企鹅启动问候
        QTimer.singleShot(1500, lambda: self._say_with_expression(
            "咕嘎～本鹅来了！冰面很滑，小心别摔了！", "happy", "bounce"))

    # ==================== 子控件 ====================

    def _init_sub_widgets(self):
        self._bongo = BongoPanel(self)
        self._bongo.hide()
        self._bongo_on = False

    def _set_pet_scale(self,value):
        self._pet_scale=max(.4,min(1.,float(value)))
        self._config.pet_scale=self._pet_scale
        bottom=self.geometry().bottom();center=self.geometry().center().x()
        w=round(self._iw*self._pet_scale);h=round(self._ih*self._pet_scale)
        if self._bongo_on:
            self._bongo.move(max(0,(w-self._bongo.width())//2),h)
            w=max(w,self._bongo.width());h+=self._bongo.height()
        self.setFixedSize(w,h);self.move(center-w//2,bottom-h+1)
        screen=self.screen()
        if screen:
            area=screen.availableGeometry()
            self.move(max(area.left(),min(self.x(),area.right()-self.width()+1)),max(area.top(),min(self.y(),area.bottom()-self.height()+1)))
        if getattr(self,'_agent_bubble',None):self._agent_bubble.follow_pet()
        self.update()

    def _show_size_dialog(self):
        from PyQt5.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QLabel,QSlider,QPushButton
        dialog=QDialog(self);dialog.setWindowTitle('咕嘎大小');dialog.resize(320,130)
        box=QVBoxLayout(dialog);label=QLabel();box.addWidget(label)
        slider=QSlider(Qt.Horizontal);slider.setRange(40,100);slider.setValue(round(self._pet_scale*100));box.addWidget(slider)
        def changed(value):
            label.setText(str(value)+'% · 当前原尺寸为最大值');self._set_pet_scale(value/100)
        slider.valueChanged.connect(changed);label.setText(str(slider.value())+'% · 当前原尺寸为最大值')
        row=QHBoxLayout();reset=QPushButton('恢复原尺寸');reset.clicked.connect(lambda:slider.setValue(100));row.addWidget(reset)
        done=QPushButton('完成');done.clicked.connect(dialog.accept);row.addWidget(done);box.addLayout(row);dialog.exec_()

    def _show_companion_settings(self):
        from PyQt5.QtWidgets import (QDialog,QVBoxLayout,QCheckBox,QSpinBox,QDialogButtonBox,
                                     QHBoxLayout,QPushButton)
        def describe(seconds):
            seconds=int(seconds)
            if seconds<60:return '%d 秒'%seconds
            if seconds%60:return '%d 分 %d 秒'%(seconds//60,seconds%60)
            return '%d 分钟'%(seconds//60)
        dialog=QDialog(self);dialog.setWindowTitle('陪伴与碎碎念');dialog.setMinimumWidth(400)
        box=QVBoxLayout(dialog);settings=self._config._s
        enabled=QCheckBox('允许咕嘎主动搭话（碎碎念）')
        enabled.setChecked(settings.value('multiagent/proactive_enabled',True,type=bool))
        box.addWidget(enabled)
        box.addWidget(QLabel('两次主动搭话的最短间隔（10 秒 — 60 分钟）'))
        row=QHBoxLayout();interval=QSpinBox();interval.setRange(10,3600);interval.setSingleStep(5)
        interval.setSuffix(' 秒')
        interval.setValue(max(10,min(3600,settings.value('multiagent/companion_interval_seconds',30,type=int))))
        row.addWidget(interval)
        for text,value in (('30 秒',30),('2 分钟',120),('5 分钟',300),('10 分钟',600)):
            quick=QPushButton(text);quick.clicked.connect(lambda _checked=False,v=value:interval.setValue(v))
            row.addWidget(quick)
        box.addLayout(row)
        hint=QLabel();hint.setWordWrap(True);box.addWidget(hint)
        def refresh():
            seconds=interval.value()
            hint.setText('大约每 '+describe(seconds)+' 最多主动说一次；30 秒左右比较自然，'
                         '拉长到 5 分钟以上几乎只剩关键时刻会开口。')
        interval.valueChanged.connect(lambda _value:refresh());refresh()
        interval.setEnabled(enabled.isChecked());enabled.toggled.connect(interval.setEnabled)
        note=QLabel('取消勾选后，咕嘎完全不会主动找你聊天；你主动发起的对话和长语音不受影响。')
        note.setWordWrap(True);box.addWidget(note)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject);box.addWidget(buttons)
        if dialog.exec_()!=QDialog.Accepted:return
        seconds=interval.value()
        settings.setValue('multiagent/proactive_enabled',enabled.isChecked())
        settings.setValue('multiagent/companion_interval_seconds',seconds);settings.sync()
        panel=getattr(self,'_agent_panel',None)
        if panel is not None:panel.proactive.configure(enabled.isChecked(),seconds)
        self._show_agent_bubble('碎碎念已关闭。' if not enabled.isChecked()
                                else '好，本鹅每隔 '+describe(seconds)+' 最多主动说一次。')

    def _init_tray(self):
        self._tray = QSystemTrayIcon(self)
        # 托盘图标：用 idle 表情缩放
        icon_pm = QPixmap(32, 32)
        icon_pm.fill(Qt.transparent)
        ip = QPainter(icon_pm)
        ip.setRenderHint(QPainter.Antialiasing)
        idle_path = os.path.join(self.RES_DIR, 'expressions', 'normal_transparent.png')
        if not os.path.exists(idle_path):
            idle_path = os.path.join(self.RES_DIR, 'expressions', 'normal.png')
        if os.path.exists(idle_path):
            small = QPixmap(idle_path).scaled(
                28, 28, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            ip.drawPixmap(2, 2, small)
        ip.end()
        self._tray.setIcon(QIcon(icon_pm))
        self._tray.setToolTip("咕嘎企鹅桌宠")
        m = QMenu()
        m.addAction("显示/隐藏桌宠（保留悬浮入口）").triggered.connect(
            lambda: self.set_pet_hidden(not self._desktop_hidden))
        m.addSeparator()
        m.addAction("💬 展开完整对话（聊天/项目）").triggered.connect(self._open_agent_chat)
        m.addAction('桌面管理台 · 模型与语音设置').triggered.connect(self._open_desktop_console)
        m.addAction("🎙 点击说话 · 录音后发送").triggered.connect(self._open_voice_recording)
        m.addAction("实时语音 · 使用实时模型额度").triggered.connect(self._open_agent_voice)
        team=m.addMenu("👥 多智能体")
        team.addAction("召唤 002 · 大肥鱼").triggered.connect(lambda:self._multi_agent.summon('002'))
        team.addAction("召唤 003 · Dora").triggered.connect(lambda:self._multi_agent.summon('003'))
        team.addAction("同时召唤大肥鱼和 Dora").triggered.connect(self._multi_agent.summon_pair)
        team.addAction("让咕嘎与大肥鱼讨论…").triggered.connect(lambda:self._multi_agent.start_discussion(('001','002')))
        m.addAction("⚙ 实时语音配置").triggered.connect(self._configure_realtime_voice)
        m.addAction("💭 陪伴与碎碎念…").triggered.connect(self._show_companion_settings)
        m.addAction("☑ 每日计划…").triggered.connect(self._show_daily_plan)
        m.addAction("♟ 战棋决策实验室").triggered.connect(self._open_bg_agent)
        m.addAction("🎮 战棋实战操作").triggered.connect(self._open_bg_live)
        m.addAction("退出").triggered.connect(self._quit)
        self._tray.setContextMenu(m)
        self._tray.show()

    # ==================== 绘制 ====================

    def _set_avatar_mode(self, mode, persist=True):
        """Switch without restarting voice, task workers, or the pet window."""
        # The reference-shell prototype changes the approved silhouette in
        # side views. Keep it out of normal launches and saved preferences.
        if mode == '3d' and '--experimental-avatar-3d' not in sys.argv:
            mode = 'illustration'
        if persist and getattr(self, '_avatar_demo_running', False):
            # A deliberate user choice cancels scheduled demo callbacks.
            self._avatar_demo_token = None
            self._avatar_demo_running = False
            self._last_avatar_demo = {'state':'cancelled','reason':'manual_appearance_change'}
        old = self._anim
        target = self._anim2d
        if mode == '3d':
            try:
                if self._anim3d is None:
                    from ui.avatar_renderer import GLPenguinRenderer
                    candidate = GLPenguinRenderer(display_width=250)
                    if not candidate.available:
                        error = candidate.error or '没有生成有效图像'
                        candidate.close()
                        raise RuntimeError(error)
                    self._anim3d = candidate
                target = self._anim3d
                self._appearance_error = ''
            except Exception as exc:
                self._appearance_error = str(exc)
                logging.warning('3D avatar fallback: %s', exc)
        if target is not old:
            target.set_expression(old.expression)
            target.sunglasses = old.sunglasses
            old_clock = old.motion.time if hasattr(old, 'motion') else old._time
            new_clock = target.motion.time if hasattr(target, 'motion') else target._time
            remaining = max(0, old._sunglasses_until - old_clock)
            target._sunglasses_until = new_clock + remaining if old._sunglasses_until else 0
            target.dancing = old.dancing
            target.mouth_open = old.mouth_open
            old.stop()
            self._anim = target
        self._anim.on_update = self.update
        self._anim.start()
        if persist:
            self._config.avatar_mode = '3d' if target is self._anim3d else 'illustration'
        self.update()

    def _update_avatar_gaze(self):
        if not self.isVisible():
            return
        from PyQt5.QtGui import QCursor
        local = self.mapFromGlobal(QCursor.pos())
        # Far-away pointer lets the character settle, avoiding permanent strain.
        x = (local.x() - self.width() / 2) / max(1, self.width() * 1.5)
        y = (local.y() - self.height() * .35) / max(1, self.height() * 1.5)
        if abs(x) > 3 or abs(y) > 3:
            x = y = 0
        self._anim.look_at(x, y)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.scale(self._pet_scale,self._pet_scale)

        ox, oy = self._anim_offset.x(), self._anim_offset.y()

        # 绘制企鹅（3D 渲染器 or 2D 精灵；不加环境光晕矩形，避免"透明方框"）
        if self._anim3d is not None:
            img = self._anim3d.paint_image()
            if img is not None:
                p.drawImage(ox, oy, img)
            else:
                # 3D 出图失败（GL 上下文丢失等）：立即回退 2D，企鹅不能消失
                try:
                    self._anim3d.stop()
                except Exception:
                    pass
                # Retain voice/accessory state; dispose broken GL after painting.
                failed = self._anim3d
                self._set_avatar_mode('illustration', persist=False)
                self._anim3d = None
                QTimer.singleShot(0, failed.close)
                self._anim.draw(p, ox, oy)
        else:
            self._anim.draw(p, ox, oy)

        # 对话气泡
        if self._bubble_text:
            self._draw_bubble(p)

    def _draw_ambient_glow(self, p):
        """根据窗口背后桌面亮度画光晕：深色背景 → 白色柔光让黑猫显形"""
        lum = self._bg_luminance
        if lum >= 0.45:
            return
        a = int((0.45 - lum) * 300)  # 越暗越亮
        a = min(a, 80)
        grad = QRadialGradient(self._iw / 2, self._ih * 0.45, max(self._iw, self._ih) * 0.8)
        grad.setColorAt(0, QColor(255, 255, 255, 0))
        grad.setColorAt(0.5, QColor(255, 255, 255, a))
        grad.setColorAt(1, QColor(255, 255, 255, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(grad)
        p.drawRect(0, 0, self._iw, self._ih)

    def _sample_background(self):
        """异步截取窗口背后桌面小块区域计算平均亮度（0~1），失败保持上次值。
        防卡死: 连续失败后退避, 避免锁屏/远程会话下线程堆积。"""
        if self._bg_sampling or not self.isVisible() or self.isMinimized():
            return
        if 3 <= self._bg_fail_count < 12:
            self._bg_fail_count += 1
            return
        self._bg_sampling = True
        threading.Thread(target=self._grab_luminance, daemon=True).start()

    def _grab_luminance(self):
        try:
            from PIL import ImageGrab
            cx = self.x() + self._iw // 2
            cy = self.y() + self._ih // 2
            img = ImageGrab.grab(bbox=(cx - 60, cy - 60, cx + 60, cy + 60))
            px = img.convert("L")
            hist = px.histogram()
            total = sum(hist)
            if total:
                self._bg_luminance = sum(i * c for i, c in enumerate(hist)) / (total * 255)
            self._bg_fail_count = 0
        except Exception:
            self._bg_fail_count = min(self._bg_fail_count + 1, 12)
        finally:
            self._bg_sampling = False

    def _draw_bubble(self, p):
        font = QFont("Microsoft YaHei", 10)
        fm = QFontMetrics(font)
        max_w = min(self._iw - 16, 400)
        line_h = fm.height() + 6
        max_lines = 5

        # 按宽度自动换行
        lines = []
        for para in self._bubble_text.split("\n"):
            cur = ""
            for ch in para:
                if cur and fm.horizontalAdvance(cur + ch) > max_w:
                    lines.append(cur)
                    cur = ch
                else:
                    cur += ch
            lines.append(cur)
        lines = lines[:max_lines]
        if len(lines) < 1:
            lines = [""]

        bw = max(min(max(fm.horizontalAdvance(l) for l in lines) + 24, max_w + 24), 60)
        bh = len(lines) * line_h + 12
        bx = (self._iw - bw) // 2
        by = 5

        # 气泡背景（浅灰 + 深色边框，深色背景时用高对比配色）
        dark_bg = self._bg_luminance < 0.4
        if dark_bg:
            bg_color = QColor(30, 30, 40, 230)
            text_color = QColor(240, 240, 245)
            border_color = QColor(255, 255, 255, 60)
        else:
            bg_color = QColor(245, 245, 250, 240)
            text_color = QColor(40, 40, 60)
            border_color = QColor(60, 60, 80, 220)
        path = QPainterPath()
        path.addRoundedRect(bx, by, bw, bh, 12, 12)
        p.fillPath(path, bg_color)
        pen = QPen(border_color, 1.2)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(bx, by, bw, bh, 12, 12)

        p.setPen(text_color)
        p.setFont(font)
        for i, line in enumerate(lines):
            p.drawText(QRect(bx + 8, by + 4 + i * line_h, bw - 16, line_h),
                       Qt.AlignLeft | Qt.AlignVCenter, line)

    # ==================== 鼠标事件 ====================

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._on_user_interaction()
            if self._voice_mode:
                # Toggle on release, so holding the mouse is never required.
                return
            self._drag_pos = e.globalPos()
            self._pet_physics.press(e.globalPos())
            self._is_drag = False
            self.grabMouse()

    def mouseMoveEvent(self, e):
        if self._voice_mode:return
        if e.buttons() & Qt.LeftButton:
            delta = e.globalPos() - self._drag_pos
            if delta.manhattanLength() > 3:
                self._is_drag = True
                self._anim.set_dragging(True, delta.x() * 30)
                self._pet_physics.sample(e.globalPos())
                self.move(self.pos() + delta)
                self._drag_pos = e.globalPos()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            if self._voice_mode:
                if self._recording:self._stop_voice()
                else:self._start_voice()
                return
            self.releaseMouse()
            self._anim.set_dragging(False)
            self._pet_physics.release(self._is_drag)
            if not self._is_drag:
                self._click()
            self._is_drag = False

    def mouseDoubleClickEvent(self, e):
        if self._voice_mode:return
        self._click()

    def enterEvent(self, e):
        self.setCursor(Qt.PointingHandCursor)

    def leaveEvent(self, e):
        self.setCursor(Qt.ArrowCursor)

    # ==================== 右键 ====================

    def _ensure_pet_menu(self):
        """Create the popup once and keep it alive between right-clicks.

        Rebuilding a styled QMenu on every click made the first opening slow
        and could leave a stale black popup owned by the translucent pet
        window. A single top-level opaque menu is cheap to reopen.
        """
        menu = getattr(self, '_pet_menu', None)
        if menu is not None:
            return menu
        from PyQt5.QtGui import QPalette
        menu = QMenu(None)
        self._pet_menu = menu
        menu.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint |
                            Qt.NoDropShadowWindowHint | Qt.WindowStaysOnTopHint)
        menu.setAttribute(Qt.WA_TranslucentBackground, False)
        menu.setWindowOpacity(1.0)
        menu.setToolTipsVisible(True)
        pal = menu.palette()
        pal.setColor(QPalette.Window, QColor('#fbfbfd'))
        pal.setColor(QPalette.WindowText, QColor('#202124'))
        pal.setColor(QPalette.Text, QColor('#202124'))
        pal.setColor(QPalette.ButtonText, QColor('#202124'))
        pal.setColor(QPalette.Highlight, QColor('#e6edff'))
        pal.setColor(QPalette.HighlightedText, QColor('#111827'))
        menu.setPalette(pal)
        menu.setAutoFillBackground(True)
        menu.setAttribute(Qt.WA_StyledBackground, True)
        menu.setStyleSheet("""
            QMenu { background-color: #fbfbfd; color: #202124;
                    border: 1px solid #b8bcc7; padding: 5px;
                    font-family: "Microsoft YaHei UI"; font-size: 12px; }
            QMenu::item { background: transparent; padding: 7px 28px 7px 15px;
                          border-radius: 4px; }
            QMenu::item:selected { background-color: #e6edff; color: #111827; }
            QMenu::item:disabled { color: #9298a4; }
            QMenu::separator { height: 1px; background: #d6dae3; margin: 4px 8px; }
            QMenu::icon { padding-left: 6px; }
        """)
        battle = menu.addMenu('战旗')
        self._menu_netcut = battle.addAction("⚡ 一键拔线（每局最多 9 次，F1）")
        self._menu_netcut.triggered.connect(self._netcut)
        menu.addAction("💬 展开完整对话").triggered.connect(self._open_agent_chat)
        menu.addAction('桌面管理台 · 模型与语音设置').triggered.connect(self._open_desktop_console)
        menu.addAction("隐藏桌宠 · 保留悬浮入口").triggered.connect(lambda:self.set_pet_hidden(True))
        menu.addAction("🎙 点击说话 · 录音后发送").triggered.connect(self._open_voice_recording)
        menu.addAction("实时语音 · 使用实时模型额度").triggered.connect(self._open_agent_voice)
        menu.addAction("💭 陪伴与碎碎念…").triggered.connect(self._show_companion_settings)
        menu.addAction("☑ 每日计划…").triggered.connect(self._show_daily_plan)
        team = menu.addMenu("👥 多智能体")
        team.addAction("召唤 002 · 大肥鱼").triggered.connect(lambda:self._multi_agent.summon('002'))
        team.addAction("召唤 003 · Dora").triggered.connect(lambda:self._multi_agent.summon('003'))
        team.addAction("同时召唤大肥鱼和 Dora").triggered.connect(self._multi_agent.summon_pair)
        team.addAction("让咕嘎与大肥鱼讨论…").triggered.connect(lambda:self._multi_agent.start_discussion(('001','002')))
        team.addAction("展开完整对话 · 当前同伴").triggered.connect(self._open_agent_chat)
        menu.addAction('截图提问').triggered.connect(lambda:self._screen_tools.begin('region_ask'))
        menu.addAction('解释').triggered.connect(lambda:self._screen_tools.begin('explain'))
        selection = menu.addAction('全局划词工具条')
        selection.setCheckable(True);selection.setChecked(self._screen_tools.selection_enabled)
        selection.toggled.connect(self._screen_tools.set_selection_enabled)
        self._screen_tools.selection_enabled_changed.connect(selection.setChecked)
        menu.addAction('咕嘎大小…').triggered.connect(self._show_size_dialog)
        stage = menu.addMenu("🎵 音乐与动作")
        appearance = stage.addMenu("形象 · 可随时切换")
        self._menu_avatar_3d = appearance.addAction("3D 开发预览（已暂停）")
        self._menu_avatar_3d.setCheckable(True)
        self._menu_avatar_3d.setEnabled('--experimental-avatar-3d' in sys.argv)
        self._menu_avatar_3d.setToolTip('原版贴图优先；侧背壳预览不用于日常桌宠。')
        self._menu_avatar_3d.triggered.connect(lambda: self._set_avatar_mode('3d'))
        self._menu_avatar_2d = appearance.addAction("原版咕嘎 · 默认形象")
        self._menu_avatar_2d.setCheckable(True)
        self._menu_avatar_2d.triggered.connect(lambda: self._set_avatar_mode('illustration'))
        demo = stage.addAction("3D 动作演示（已暂停，保留原画）")
        demo.setEnabled('--experimental-avatar-3d' in sys.argv)
        demo.triggered.connect(self._show_avatar_actions)
        natural = stage.addMenu('自然动作 · 视频')
        for label, gesture in (('困倦打盹','idle_doze'), ('双手比心','idle_heart'),
                               ('抱臂嘟嘴','idle_pout'), ('仰头大笑','idle_laugh')):
            natural.addAction(label).triggered.connect(
                lambda _checked=False, name=gesture: self._perform_gesture(name))
        expressions = stage.addMenu('表情')
        for label, expression in (('开心', 'happy'), ('傲娇', 'playful'), ('惊讶', 'surprised'),
                                  ('困了', 'sleepy'), ('委屈', 'sad'), ('日常', 'normal')):
            expressions.addAction(label).triggered.connect(
                lambda checked=False, expr=expression: self._anim.set_expression(expr))
        stage.addAction("暗中观察").triggered.connect(lambda: self._perform_gesture("sneak_peek"))
        stage.addAction("暂停唱歌，聊一会儿").triggered.connect(
            lambda: self._ensure_agent_panel().send("暂停唱歌"))
        stage.addAction("继续唱歌").triggered.connect(
            lambda: self._ensure_agent_panel().send("继续唱歌"))
        self._menu_sunglasses = stage.addAction("戴上墨镜")
        self._menu_sunglasses.triggered.connect(
            lambda: self._perform_gesture('remove_sunglasses' if self._anim.sunglasses else 'sunglasses'))
        self._menu_dance = stage.addAction("轻轻摆动")
        self._menu_dance.triggered.connect(
            lambda: self._perform_gesture('stop_dance' if self._anim.dancing else 'dance'))
        stage.addAction("唱《晴天》").triggered.connect(
            lambda: self._ensure_agent_panel().send('唱晴天'))
        stage.addAction("停止唱歌").triggered.connect(
            lambda: self._ensure_agent_panel().runtime.music.stop())
        talk_menu = menu.addMenu("🎙 原语音功能")
        talk_menu.addAction("千问长语音 · 屏幕辅助与任务执行").triggered.connect(self._open_realtime_voice)
        talk_menu.addAction("千问语音设置").triggered.connect(self._configure_realtime_voice)
        battle.addAction("📋 日志分析").triggered.connect(self._review_bg_logs)
        battle.addAction("🎮 战局分析 / 实战操作").triggered.connect(self._open_bg_live)
        # Force style/layout/native popup allocation while idle, so the first
        # real opening only positions and paints the already-warm menu.
        menu.ensurePolished()
        menu.adjustSize()
        menu.winId()
        return menu

    def contextMenuEvent(self, e):
        self._on_user_interaction()
        menu = self._ensure_pet_menu()
        # Cached labels update without rebuilding the menu or reading Power.log.
        self._menu_netcut.setText(
            f"⚡ 一键拔线（本局 {int(getattr(self, '_netcut_count', 0) or 0)}/9，F1）")
        self._menu_sunglasses.setText(
            "摘下墨镜" if self._anim.sunglasses else "戴上墨镜")
        self._menu_dance.setText(
            "停止摇摆" if self._anim.dancing else "轻轻摆动")
        self._menu_avatar_3d.setChecked(self._anim is self._anim3d)
        self._menu_avatar_2d.setChecked(self._anim is self._anim2d)
        menu.popup(e.globalPos())
        e.accept()

    # ==================== 交互 ====================

    def _on_user_interaction(self):
        """Only explicit human input wakes a held sleepy expression."""
        panel=getattr(self,'_agent_panel',None)
        if panel is not None:
            import time
            panel._last_user_activity=time.monotonic()
        return self._anim2d.user_interaction()

    def hideEvent(self,event):
        anim=getattr(self,'_anim',None);gaze=getattr(self,'_gaze_timer',None)
        if anim is not None:anim.stop()
        if gaze is not None:gaze.stop()
        super().hideEvent(event)

    def showEvent(self,event):
        super().showEvent(event)
        anim=getattr(self,'_anim',None);gaze=getattr(self,'_gaze_timer',None)
        if anim is not None:anim.start()
        if gaze is not None:gaze.start()

    def _click(self):
        self._anim2d.tap()
        self._on_user_interaction()
        self._click_count += 1
        self._status.happy = min(100.0, self._status.happy + 3)
        # 招募阶段点击企鹅 = 讨论 → 未来阵容展望（轨道⑤；日志检测到在招募阶段才触发）
        try:
            from core import log_reader
            _b = log_reader.read_current_board()
            if _b and _b.get('active') and _b.get('mine') and not _b.get('theirs'):
                self._lineup_outlook()
                return
        except Exception:
            pass
        if self._ai.enabled:
            # AI 互动：戳一下企鹅，它会用 DS 大脑回话（表情+调侃）
            self._ai.ask("（饲养员用手指轻轻戳了戳你圆滚滚的肚子，想逗你玩。你被他戳醒了，吐槽他一句，再咕嘎一声。）")
        else:
            line = get_speech("click")
            self._say_with_expression(line.text, line.expression, line.action)
        QTimer.singleShot(3000, lambda: self._anim.update_mood(self._status.mood))

    # ==================== 阵容展望（招募阶段讨论） ====================

    def _lineup_outlook(self):
        """招募阶段点击讨论 → 未来阵容展望：
        规则引擎先给摘要（方向/缺的核心/成型模板/建议），再让 AI 军师展开"""
        import time as _time
        now = _time.time()
        # 冷却：25 秒内不重复展望（点击很容易误触，防刷屏）
        last = getattr(self, '_outlook_last_t', 0)
        if now - last < 25:
            self._say_with_expression(
                "咕嘎～刚展望过啦，等 25 秒再点，让本鹅消化一下！", "tsundere", "tsundere")
            return
        from core import log_reader
        board = log_reader.read_current_board()
        if board is None:
            self._say_with_expression("咕嘎？没找到炉石日志，先开一把酒馆战棋吧！", "sad", "shake")
            return
        if not board.get('active'):
            self._say_with_expression(
                "咕嘎？你根本没在玩嘛，开一把酒馆战棋，本鹅才能展望你的未来！", "tsundere", "tsundere")
            return
        if board.get('theirs'):
            self._say_with_expression(
                "咕嘎？正在战斗中呢，等回到招募阶段再点本鹅展望！", "tsundere", "peek")
            return
        if not board.get('mine'):
            self._say_with_expression(
                "咕嘎？场上还没有随从，先买点东西再来展望！", "tsundere", "tsundere")
            return
        self._say_with_expression("咕嘎～让本鹅琢磨琢磨你的未来…", "normal", "point")
        from core.lineup_planner import project_lineup, format_outlook
        from core.log_reader import format_board
        try:
            outlook = project_lineup(board)
            outlook_text = format_outlook(outlook)
        except Exception as e:
            print(f"[阵容展望] 失败: {e}")
            self._say_with_expression("咕嘎？展望引擎卡住了，再试一次！", "sad", "shake")
            return
        self._outlook_last_t = now
        self._say_with_expression(f"咕嘎～本鹅展望完毕：{outlook_text}", "surprised", "point")
        self._ai.ask(
            f"饲养员在招募阶段点了你，想听对未来阵容的展望。当前局面（来自游戏日志，精确数据）：\n"
            f"{format_board(board)}\n"
            f"本鹅规则引擎的展望：{outlook_text}\n"
            f"请以军师身份展望他的未来阵容：当前方向对不对、接下来优先买什么/刷什么、该不该升酒馆、"
            f"第几回合成型、成型后强度如何、何时考虑转型。",
            rag_query=f"阵容展望 {outlook.get('direction', '')} 未来阵容 成型 转型",
            system_override=OUTLOOK_OVERRIDE)

    # ==================== AI 对话 ====================

    def _ensure_agent_panel(self):
        if self._agent_panel is None:
            from ui.agent_chat import AgentChatWindow
            self._agent_panel = AgentChatWindow(self,memory=getattr(self,'_agent_memory',None))
        return self._agent_panel

    def set_pet_hidden(self, hidden):
        """Change only the desktop surface, keeping jobs and voice alive."""
        hidden = bool(hidden)
        self._desktop_hidden = hidden
        self._config._s.setValue('desktop/pet_hidden', hidden)
        if hidden:
            from ui.desktop_launcher import DesktopLauncher
            if self._floating_launcher is None:self._floating_launcher = DesktopLauncher(self)
            self.hide();self._agent_bubble.hide();self._hide_inline()
            self._floating_launcher.keep_on_screen();self._floating_launcher.show()
        else:
            if self._floating_launcher:self._floating_launcher.hide()
            self.show();self._force_top()

    def _open_agent_chat(self):
        self._ensure_agent_panel()
        self._agent_panel.show()
        self._agent_panel.raise_()
        self._agent_panel.activateWindow()

    def _open_desktop_console(self):
        from ui.desktop_console import DesktopConsole
        if getattr(self,'_desktop_console',None) is None:self._desktop_console=DesktopConsole(self)
        self._desktop_console.show();self._desktop_console.raise_();self._desktop_console.activateWindow()

    def _show_daily_plan(self):
        panel=self._ensure_agent_panel()
        from ui.daily_plan_dialog import DailyPlanDialog
        if getattr(self,'_daily_plan_dialog',None) is None:
            self._daily_plan_dialog=DailyPlanDialog(panel)
        self._daily_plan_dialog.show()
        self._daily_plan_dialog.raise_()
        self._daily_plan_dialog.activateWindow()

    def _open_agent_voice(self):
        panel = self._ensure_agent_panel()
        # The long-voice entry is an ambient action: never reveal the full
        # conversation window just because setup or voiceprint validation failed.
        panel.hide()
        panel.voice_button.setChecked(True)
        QTimer.singleShot(120, lambda: self._finish_agent_voice_launch(panel))

    def _open_voice_recording(self):
        panel = self._ensure_agent_panel()
        if panel.isVisible():panel.toggle_short_voice();return
        if self._desktop_hidden:
            self._open_agent_chat();panel.toggle_short_voice();return
        self._show_agent_bubble('点击说话开始录音，录好后点 ↑ 发送，×取消。',panel.active_id)
        self._agent_bubble._begin_talk()

    def _finish_agent_voice_launch(self, panel):
        # A user may click the bubble during the short startup delay.
        # Respect that explicit reopen instead of hiding the conversation again.
        if panel.isVisible():
            return
        if panel.voice_active:
            # toggle_voice already keeps the panel hidden and creates the
            # listening bubble. Do not overwrite a fast live reply 120 ms later.
            return
        panel.hide()
        message = panel.status.text() or '长语音未启动，请检查语音配置'
        self._show_agent_bubble(message, panel.active_id)

    def _end_agent_voice(self):
        if self._agent_panel:
            self._agent_panel.voice_button.setChecked(False)

    def _on_bubble_clicked(self):
        """Record a draft through the same click/send bar used in chat.

        It must never open the full chat window: that steals focus and fights
        the live microphone. The full conversation has explicit menu entries.
        """
        self._on_user_interaction()
        panel = self._agent_panel
        if panel is not None:
            if panel.voice_active:
                return
            if panel.busy and panel._run_kind=='answer':
                return
        self._open_voice_recording()

    def _open_bubble_chat(self):
        """Compatibility entry: explicit full conversation, never a bubble click."""
        self._on_user_interaction()
        self._open_agent_chat()

    def _show_agent_bubble(self, text, conversation_id=None):
        self._bubble_text = ''
        self._bubble_timer.stop()
        self._agent_bubble.show_text(text,conversation_id)
        self.update()

    def _say_agent_reply(self,text,expression=None,action='none',speak=False,conversation_id=None):
        """One desktop bubble for agent speech; optional TTS does not draw another card."""
        from core.embodiment import spoken_reply
        inferred_expression,inferred_action,clean=spoken_reply(text)
        if inferred_action!='none':action=inferred_action
        if inferred_expression!='normal':expression=inferred_expression
        self._show_agent_bubble(clean,conversation_id)
        self._apply_agent_style(expression,action)
        if speak and not self._realtime_active():self._tts.speak(clean)

    def _apply_agent_style(self,expression=None,action='none'):
        if expression and expression!='none':self._anim.set_expression(expression)
        if action and action!='none':self._perform_gesture(action)

    def _start_bubble_talk(self):
        panel=self._ensure_agent_panel()
        if not panel.start_bubble_short_voice():
            self._agent_bubble.set_voice_state('idle')

    def _finish_bubble_talk(self,cancel=False):
        if self._agent_panel is not None:self._agent_panel.finish_bubble_short_voice(bool(cancel))

    def _on_ai_reply(self, text, expression, action):
        self._say_with_expression(text, expression, action)
        # 助教模式: 对话追加到面板(确保面板已创建)
        if self._tts._tutor_mode:
            self._ensure_chat_panel()
            if self._chat_history and self._chat_history[-1][0] == "系统":
                self._chat_history.pop()
            self._chat_append("咕嘎", text)
            self._show_chat_panel()
            # 连续对话: AI 回复后自动重新打开输入条, 让用户继续追问/答题
            # 保留当前 _input_mode: tutor=提问模式(截屏), quiz_answer=答题模式(不截屏)
            from PyQt5.QtCore import QTimer
            _mode = self._input_mode if self._input_mode in ('tutor', 'quiz_answer') else 'tutor'
            QTimer.singleShot(400, lambda: self._open_input(_mode))

    def _on_ai_fail(self, error):
        print(f"[AI] 请求失败({error})，回落台词库")
        line = get_speech(random.choice(["click", "idle", "tease"]))
        self._say_with_expression(line.text, line.expression, line.action)

    # ==================== 打字输入条（无对话框） ====================

    def _ensure_input(self):
        """企鹅下方的迷你输入条（一次性创建）"""
        if self._inline_input is None:
            bar = QFrame(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
            bar.setStyleSheet("""
                QFrame { background: rgba(25,25,35,0.95); border: 1px solid rgba(255,255,255,0.15);
                         border-radius: 10px; }
            """)
            lay = QHBoxLayout(bar)
            lay.setContentsMargins(8, 6, 8, 6)
            self._input_edit = QPlainTextEdit()
            self._input_edit.setFixedSize(320, 64)
            self._input_edit.setPlaceholderText("和咕嘎说话…（Enter 发送，Esc 关闭）")
            self._input_edit.setStyleSheet("background: transparent; color: #eee; border: none;")
            lay.addWidget(self._input_edit)
            self._input_hint = QLabel("💬")
            self._input_hint.setStyleSheet("color: #8a8aaa; font-size: 11px;")
            lay.addWidget(self._input_hint)
            from PyQt5.QtWidgets import QPushButton
            close_input = QPushButton('关闭', bar)
            close_input.clicked.connect(self._hide_inline)
            lay.addWidget(close_input)
            self._inline_input = bar

            # Enter发送 / Ctrl+Enter换行 / Esc关闭 — 用 eventFilter 拦截,
            # 因为 QPlainTextEdit 原生吞掉 Return 键导致 QShortcut 无效
            self._input_edit.installEventFilter(self)
            self._inline_input_bar = bar
        return self._inline_input

    def eventFilter(self, obj, event):
        from PyQt5.QtCore import QEvent
        # --- 输入条按键处理 ---
        if obj is getattr(self, '_input_edit', None) and event.type() == QEvent.KeyPress:
            from PyQt5.QtCore import Qt
            k = event.key()
            mods = event.modifiers()
            if k in (Qt.Key_Return, Qt.Key_Enter) and not (mods & Qt.ControlModifier):
                self._send_inline()
                return True
            if k in (Qt.Key_Return, Qt.Key_Enter) and (mods & Qt.ControlModifier):
                self._input_edit.insertPlainText("\n")
                return True
            if k == Qt.Key_Escape:
                self._hide_inline()
                return True
        # --- 面板 Esc 关闭 ---
        if obj is getattr(self, '_chat_panel', None) and event.type() == QEvent.KeyPress:
            from PyQt5.QtCore import Qt
            if event.key() == Qt.Key_Escape:
                self._hide_chat_panel()
                return True
        # --- 面板标题栏拖拽 ---
        if obj is getattr(self, '_chat_title', None):
            from PyQt5.QtCore import Qt
            etype = event.type()
            if etype == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._chat_drag_pos = event.globalPos() - self._chat_panel.frameGeometry().topLeft()
                return True
            if etype == QEvent.MouseMove and event.buttons() & Qt.LeftButton:
                self._chat_panel.move(event.globalPos() - self._chat_drag_pos)
                return True
        return super().eventFilter(obj, event)

    def _open_input(self, mode: str = 'chat'):
        bar = self._ensure_input()
        self._input_edit.setPlainText("")
        self._input_mode = mode
        if mode == 'tutor':
            self._input_edit.setPlaceholderText("问本鹅 Python/大模型问题...(Enter 发送)")
        elif mode == 'quiz_answer':
            self._input_edit.setPlaceholderText("写出你的答案, 本鹅来点评...(Enter 发送)")
        else:
            self._input_edit.setPlaceholderText("和咕嘎说话...(Enter 发送, Esc 关闭)")
        bar.move(self.x() + (self.width() - bar.width()) // 2, self.y() + self.height() + 4)
        bar.show()
        bar.raise_()
        self._input_edit.setFocus()

    def _send_inline(self):
        text = self._input_edit.toPlainText().strip()
        self._hide_inline()
        if not text:
            return
        self._on_user_interaction()
        if self._input_mode == 'tutor':
            self._tutor_ask(text)
        elif self._input_mode == 'quiz_answer':
            # 答题模式: 不截屏, 直接问AI(用助教人设点评答案)
            from core.tutor import TUTOR_SYSTEM
            self._ensure_chat_panel()
            self._chat_append("饲养员", text)
            self._chat_append("系统", "咕嘎点评中...~")
            self._show_chat_panel()
            self._ai.ask(text, system_override=TUTOR_SYSTEM)
        else:
            self._tts.set_tutor_mode(False)
            self._ask_with_ctx(text)

    # ==================== 助教大对话面板 ====================

    def _ensure_chat_panel(self):
        if self._chat_panel is not None:
            return self._chat_panel
        from PyQt5.QtWidgets import QFrame, QVBoxLayout, QTextEdit, QLabel, QShortcut, QPushButton
        from PyQt5.QtGui import QKeySequence
        from PyQt5.QtCore import Qt, QPoint
        panel = QFrame(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        panel.setStyleSheet("""
            QFrame { background: rgba(25,25,40,0.97); border: 2px solid rgba(120,180,255,0.5);
                     border-radius: 14px; }
            QLabel { color: #8acaff; font-size: 12px; font-weight: bold; padding: 2px; }
            QTextEdit { background: transparent; color: #eef; border: none;
                        font-size: 13px; selection-background-color: #446; }
        """)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(12, 8, 12, 10)
        lay.setSpacing(4)
        title = QLabel("📚 咕嘎助教对话  (Esc 关闭 · 拖动标题移动)")
        title.setStyleSheet("color: #8acaff; font-size: 12px; font-weight: bold; padding: 2px;")
        title_row = QHBoxLayout()
        title_row.addWidget(title, 1)
        close_panel = QPushButton('关闭', panel)
        close_panel.setStyleSheet('color: #eef; padding: 4px 10px; border: 1px solid #668; border-radius: 5px;')
        close_panel.clicked.connect(self._hide_chat_panel)
        title_row.addWidget(close_panel)
        lay.addLayout(title_row)
        view = QTextEdit()
        view.setReadOnly(True)
        view.setFixedSize(420, 260)
        lay.addWidget(view)
        self._chat_panel = panel
        self._chat_view = view
        self._chat_title = title
        # 给标题栏安装事件过滤器实现拖拽
        title.installEventFilter(self)
        QShortcut(QKeySequence(Qt.Key_Escape), panel, self._hide_chat_panel)
        return panel

    def _show_chat_panel(self):
        panel = self._ensure_chat_panel()
        pw = panel.width()
        ph = panel.height()
        px = self.x() + (self.width() - pw) // 2
        py = self.y() - ph - 8
        if py < 4:
            py = self.y() + self.height() + 8
        panel.move(px, py)
        panel.show()
        panel.raise_()

    def _hide_chat_panel(self):
        if self._chat_panel is not None:
            self._chat_panel.hide()

    def _chat_append(self, who, text):
        self._chat_history.append((who, text))
        if self._chat_view is None:
            return
        # 超过30条时清空重建, 否则增量append避免卡顿
        if len(self._chat_history) > 30:
            self._chat_history = self._chat_history[-30:]
            self._chat_view.clear()
            for w, t in self._chat_history:
                self._chat_append_line(w, t)
        else:
            self._chat_append_line(who, text)
        bar = self._chat_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _chat_append_line(self, who, text):
        color = {"饲养员": "#8affaa", "咕嘎": "#cce0ff"}.get(who, "#888")
        size = '11px' if who not in ("饲养员", "咕嘎") else '13px'
        bold = f"<b>{who}:</b> " if who in ("饲养员", "咕嘎") else ""
        html = f'<p style="color:{color}; font-size:{size}; margin:2px 0;">{bold}{self._esc_html(text)}</p>'
        self._chat_view.append(html)

    @staticmethod
    def _esc_html(s):
        return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('\n', '<br>')

    def _tutor_ask(self, text):
        """助教问答: 截屏分析 + 用户问题 → AI 回答
        用 pyqtSignal 做跨线程回调(比 QTimer.singleShot 可靠),
        加 8 秒超时保护: 屏幕分析超时则直接问 AI(不加屏幕内容)
        """
        from core.tutor import TUTOR_SYSTEM, SCREEN_TUTOR_PROMPT
        from core import vision
        from PyQt5.QtCore import QTimer
        self._ensure_chat_panel()
        self._chat_append("饲养员", text)
        self._chat_append("系统", "咕嘎正在看屏幕并思考...(8秒超时)~")
        self._show_chat_panel()
        self._tutor_pending_text = text
        self._tutor_vision_done = False
        # 8秒超时: 如果屏幕分析还没回来, 直接问AI
        QTimer.singleShot(8000, self._tutor_timeout)
        # 后台截屏分析, 回调通过 pyqtSignal 回主线程
        def _on_screen(screen_text):
            # 在后台线程里, 用类级 pyqtSignal 安全地跨线程投递到主线程
            self._vision_done.emit(screen_text)
        vision.analyze_in_thread(None, _on_screen, SCREEN_TUTOR_PROMPT)

    def _on_vision_result(self, screen_text):
        """主线程: 收到屏幕分析结果"""
        from core.tutor import TUTOR_SYSTEM
        if self._tutor_vision_done:
            return  # 超时已触发, 丢弃
        self._tutor_vision_done = True
        if self._chat_history and self._chat_history[-1][0] == "系统":
            self._chat_history.pop()
        combined = f"【屏幕看到】{screen_text}\n\n【饲养员的问题】{self._tutor_pending_text}"
        self._chat_append("系统", "咕嘎思考中...")
        self._ai.ask(combined, system_override=TUTOR_SYSTEM)

    def _tutor_timeout(self):
        """主线程: 屏幕分析超时, 直接问AI"""
        from core.tutor import TUTOR_SYSTEM
        if self._tutor_vision_done:
            return
        self._tutor_vision_done = True
        if self._chat_history and self._chat_history[-1][0] == "系统":
            self._chat_history.pop()
        self._chat_append("系统", "(屏幕分析超时, 直接回答) 咕嘎思考中...")
        self._ai.ask(self._tutor_pending_text, system_override=TUTOR_SYSTEM)

    def _ask_with_ctx(self, text):
        """Ordinary inline chat shares the companion model, memory and history."""
        import re
        if not re.search(r'炉石|酒馆|战[棋旗绩]|对局|胜率',text):
            return self._ensure_agent_panel().send(text)
        if re.search(r'(打开|启动|进入).*(炉石|酒馆战[棋旗])', text):
            self._open_bg_live()
            self._bg_live_panel.open_game()
            self._bg_live_panel.start(1)
            return
        if re.search(r'(帮我|替我|代).*(下棋|操作|打酒馆|打战棋)', text):
            self._open_bg_live()
            self._bg_live_panel.start(12)
            return
        ctx = ''
        try:
            from core import match_history
            ctx = match_history.chat_context(text)
        except Exception:
            pass
        if ctx:
            self._ai.ask(
                f"{ctx}\n饲养员说：{text}\n如果他在问战绩相关，结合上面的最近战绩"
                f"调侃他（可爱不刻薄，自称本鹅）。", rag_query=text)
        else:
            self._ai.ask(text, rag_query=text)

    def _hide_inline(self):
        if self._inline_input is not None:
            self._inline_input.hide()

    # ==================== 按住说话（无对话框） ====================

    def _play_sticker_action(self, name):
        """播放 GIF 表情包动作"""
        self._anim.play_sticker(name)
        self._say_with_expression(f"咕嘎～{name}！", "happy", "none")

    def _toggle_voice(self, checked):
        if checked and self._agent_panel and self._agent_panel.voice_active:
            return
        if checked and self._realtime_active():
            return
        self._voice_mode = bool(checked)
        if self._voice_mode:
            self._say_with_expression("咕嘎～点一下本鹅开始录音，再点一下发送！", "normal", "none")
        else:
            self._stop_voice()
            self._say_with_expression("咕嘎～语音模式关了。", "normal", "none")

    def _start_voice(self):
        if self._agent_panel and self._agent_panel.voice_active:
            return
        if self._realtime_active():
            return
        if self._recording:
            return
        from core.asr import ASRRecorder
        if self._asr is None:
            self._asr = ASRRecorder()
        if not self._asr.available:
            hint = getattr(self._asr, 'hint', '语音模块没就绪')
            self._say_with_expression(f"咕嘎？{hint}，先用打字吧。", "sad", "none")
            return
        self._recording = True
        self._asr.start()
        self._bubble_text = "🔴 录音中…再点一下发送（最长30秒）"
        self._voice_record_timer.start(30000)
        self._bubble_timer.start(60000)
        self.update()

    def _stop_voice(self):
        if not self._recording:
            return
        self._recording = False
        self._voice_record_timer.stop()
        self._bubble_timer.stop()
        text = self._asr.stop() if self._asr else ''
        if self._asr and self._asr.error:
            self._say_with_expression(f"咕嘎？{self._asr.error}", "sad", "shake")
            return
        if not text:
            st = self._asr.stats() if self._asr else {}
            if not st.get('has_audio'):
                # 麦克风没收到声音：多半是设备/音量问题
                dev = getattr(self._asr, 'device', None)
                self._say_with_expression(
                    "咕嘎？没收到声音，检查一下麦克风吧（录音"
                    f"{st.get('seconds', 0)}秒，设备{'#' + str(dev) if dev is not None else '默认'}）",
                    "sad", "shake")
            else:
                self._say_with_expression("咕嘎？没听清，再说一次？", "surprised", "peek")
            return
        self._on_user_interaction()
        self._ask_with_ctx(text)

    def _realtime_active(self):
        engine = getattr(self, '_realtime_voice', None)
        if not engine or not engine.active:
            return False
        # A reconnecting thread is not an audible call: let local TTS speak and
        # let the UI show "reconnecting" instead of pretending the call is fine.
        return bool(getattr(engine, 'connected', True))

    def _configure_realtime_voice(self):
        from ui.voice_dialog import VoiceSettingsDialog
        if self._realtime_active():
            if self._voice_call_panel:
                self._voice_call_panel.raise_()
            elif self._agent_panel:
                self._open_agent_chat()
                self._agent_panel.status.setText('请先结束长语音通话，再修改语音配置')
            return
        VoiceSettingsDialog(self).exec_()

    def _open_realtime_voice(self):
        """Keep the historical menu alias on the same screen-aware agent path."""
        self._open_agent_voice()

    def _on_user_transcript(self, who, text):
        if who == '饲养员' and text.strip():
            self._on_user_interaction()

    def _on_realtime_status(self, message):
        self._bubble_text = message
        self._bubble_timer.start(10000)
        self.update()
        # Last status only: no microphone audio, transcript, token or headers.
        try:
            import json
            from pathlib import Path
            path = Path(__file__).resolve().parents[1] / 'realtime_status.log'
            path.write_text(json.dumps({'status': message}, ensure_ascii=False), encoding='utf-8')
        except OSError:
            pass

    def _on_realtime_speaking(self, on):
        self._agent_bubble.set_speaking(on)
        if on:
            if self._pending_voice_gesture:
                gesture=self._pending_voice_gesture;self._pending_voice_gesture=None
                self._perform_gesture(gesture)
            self._on_tts_started('')
        else:
            self._on_tts_finished()

    def _say(self, text: str, expression: str = None, action: str = "none"):
        self._say_with_expression(text, expression, action)

    # 动作 → 表情 强制映射（保证动作与表情永远匹配）
    GESTURE_EXPR = {
        'point': 'playful',      # 指点江山 → 傲娇脸（装严肃指点）
        'peek': 'normal',        # 偷偷看我 → 日常脸
        'tsundere': 'playful',   # 傲娇摇头 → 傲娇脸
        'bounce': 'happy',       # 开心蹦 → 开心
        'shake': 'surprised',    # 受惊抖 → 惊讶
    }

    def _say_with_expression(self, text: str, expression: str = None, action: str = "none"):
        from core.embodiment import spoken_reply
        inferred_expression,inferred_action,text=spoken_reply(text)
        if inferred_action!='none':action=inferred_action
        if inferred_expression!='normal':expression=inferred_expression
        # 显示气泡
        self._bubble_text = text
        self._bubble_timer.start(4000)
        self.update()

        # 动作决定表情（强制匹配）
        if action in self.GESTURE_EXPR:
            expression = self.GESTURE_EXPR[action]

        # 切换表情
        if expression and expression != "none":
            self._anim.set_expression(expression)

        # 触发动作（bounce/shake/point/peek/tsundere 由动画器处理）
        if action and action != "none":
            self._anim.trigger_action(action)

        # 播放 TTS 语音
        if not self._realtime_active():
            self._tts.speak(text)

    def _perform_gesture(self,name):
        # Gestures do not create speech or replace the current reply bubble.
        self._anim2d.trigger_action(name)
        if self._anim is not self._anim2d:
            self._anim.trigger_action('tsundere' if name=='head_shake' else name)
        self.update()

    def _show_avatar_actions(self):
        """Finite local demo; no microphone, API calls, music or task side effects."""
        if '--experimental-avatar-3d' not in sys.argv:
            self._last_avatar_demo = {'state':'disabled','reason':'preserve_original_artwork'}
            return
        if getattr(self, '_avatar_demo_running', False):
            return
        old_mode = '3d' if self._anim is self._anim3d else 'illustration'
        old_glasses, old_dance = self._anim.sunglasses, self._anim.dancing
        self._set_avatar_mode('3d', persist=False)
        if self._anim is not self._anim3d:
            self._show_agent_bubble('当前 3D 动作渲染不可用，已保留原版形象。')
            return
        self._avatar_demo_running = True
        self._last_avatar_demo = {'state':'running','original_mode':old_mode}
        token = self._avatar_demo_token = object()
        def schedule(ms, callback):
            QTimer.singleShot(ms, lambda: callback() if getattr(self, '_avatar_demo_token', None) is token else None)
        self._perform_gesture('stop_dance')
        self._perform_gesture('spin')
        schedule(2600, lambda: self._perform_gesture('sneak_peek'))
        schedule(6000, lambda: self._perform_gesture('wave'))
        def dance():
            self._perform_gesture('sunglasses')
            self._perform_gesture('dance')
        schedule(8200, dance)
        def finish():
            self._perform_gesture('dance' if old_dance else 'stop_dance')
            self._perform_gesture('sunglasses' if old_glasses else 'remove_sunglasses')
            self._avatar_demo_running = False
            self._avatar_demo_token = None
            self._set_avatar_mode(old_mode, persist=False)
            self._last_avatar_demo = {'state':'completed','restored_mode':old_mode}
        schedule(15000, finish)

    def _queue_voice_gesture(self,name):
        if name in ('sunglasses','remove_sunglasses','dance','stop_dance','sneak_peek') + IDLE_GESTURES or (self._realtime_active() and self._realtime_voice.is_speaking):
            self._perform_gesture(name)
        else:
            self._pending_voice_gesture=name

    def _on_tts_started(self, text: str):
        panel=getattr(self,'_agent_panel',None)
        if panel:self._multi_agent.speaking(panel.active_agent_id,True)
        self._talk_timer.start()
        self._talk_count = 0

    def _on_tts_finished(self):
        panel=getattr(self,'_agent_panel',None)
        if panel:self._multi_agent.speaking(panel.active_agent_id,False)
        self._talk_timer.stop()
        self._anim.mouth_open = 0.0
        self.update()

    def _talk_tick(self):
        self._talk_count = getattr(self, '_talk_count', 0) + 1
        if self._tts.is_speaking or (self._realtime_active() and self._realtime_voice.is_speaking):
            if self._talk_count % 3 < 2:
                self._anim.mouth_open = random.uniform(0.2, 0.6)
            else:
                self._anim.mouth_open = 0.1
        else:
            self._anim.mouth_open = 0.0
            self._talk_timer.stop()

    def _hide_bubble(self):
        self._bubble_text = ""
        self.update()

    def _say_scene(self, scene: str):
        line = get_speech(scene)
        self._say_with_expression(line.text, line.expression, line.action)

    def _on_mood_changed(self, mood: str):
        self._tray.setToolTip(f"咕嘎企鹅桌宠 {mood}")
        self._anim.update_mood(mood)

    def _on_key(self, vk):
        # KeyboardHook.key_pressed 是 pyqtSignal, 跨线程自动 QueuedConnection
        # 所以 _on_key 已经在主线程, 可直接操作 UI
        self._bongo.press_key(vk)
        if not self._bongo_on:
            self._show_bongo()
        # 数字键: 1=请教助教 2=助教出题 3=语音 4=拔线 5=摇摆
        shortcuts = {0x31: 'ask_tutor', 0x32: 'quiz_user', 0x33: 'voice_chat',
                     0x34: 'netcut', 0x35: 'dance'}
        if vk in shortcuts:
            self._act(shortcuts[vk])
        if vk == 0x70:  # F1 → 一键拔线(冗余)
            self._netcut()

    def _show_bongo(self):
        self._bongo_on = True
        self._bongo.move(max(0, (round(self._iw*self._pet_scale) - self._bongo.width()) // 2),
                         round(self._ih*self._pet_scale))
        self._bongo.show()
        w = max(round(self._iw*self._pet_scale), self._bongo.width())
        self.setFixedSize(w, round(self._ih*self._pet_scale) + self._bongo.height())

    def _act(self, name):
        a = get_action(name)

        # 看我的棋盘 → 视觉流程
        if name == 'see_board':
            self._see_board()
            return

        # Python/大模型助教 → 请教
        if name == 'ask_tutor':
            self._say_with_expression("咕嘎~想问什么? 本鹅耐心讲解~", "happy", "peek")
            self._tts.set_tutor_mode(True)
            self._ensure_chat_panel()
            self._show_chat_panel()
            self._open_input('tutor')
            return

        # Python/大模型助教 → 出题
        if name == 'quiz_user':
            self._tts.set_tutor_mode(True)
            self._input_mode = 'quiz_answer'  # 标记: 接下来用户输入是答题(不截屏)
            self._ensure_chat_panel()
            self._chat_append("咕嘎", "本鹅出题中...~")
            self._show_chat_panel()
            from core.tutor import TUTOR_QUIZ
            self._say_with_expression("咕嘎~本鹅出题考考你!", "playful", "point")
            self._ai.ask("请出一道 Python/大模型中级工程师思考题, 然后等饲养员回答.",
                         system_override=TUTOR_QUIZ)
            return

        # 一键拔线
        if name == 'netcut':
            self._netcut()
            return

        # 语音对话
        if name == 'voice_chat':
            self._toggle_voice(not getattr(self, '_voice_mode', False))
            return

        # 指点江山 → 军师模式（先出动作，再让 AI 给建议）
        if name == 'advise':
            self._say_with_expression(a.messages[0], a.animation, a.gesture)
            self._ai.ask("饲养员请你指点他一下酒馆战棋。用指点江山的姿态，给他一条当前版本最实用的建议（结合第14赛季机制）。",
                         rag_query="酒馆战棋 指点 建议 第14赛季")
            self._status.happy = min(100.0, self._status.happy + a.happy_delta)
            return

        scene_map = {
            'feed': 'feed', 'play': 'play', 'pet': 'pet',
            'sleep': 'sleep', 'dance': 'play', 'peek': 'idle', 'tsundere': 'tease',
        }
        scene = scene_map.get(name, 'idle')
        line = get_speech(scene)
        # 用动作定义的 gesture 播放动画
        gesture = a.gesture if a.gesture != 'none' else line.action
        self._say_with_expression(line.text, a.animation, gesture)

        QTimer.singleShot(4000, lambda: self._anim.update_mood(self._status.mood))

        self._status.hunger = max(0, min(100, self._status.hunger + a.hunger_delta))
        self._status.happy = max(0, min(100, self._status.happy + a.happy_delta))
        self._status.energy = max(0, min(100, self._status.energy + a.energy_delta))
        self._status.sleeping = (name == 'sleep') and (not self._status.sleeping)

    # ==================== 一键拔线 ====================

    def _netcut_selftest(self):
        """拔线自检：企鹅(管理员)内执行完整链路，结果写日志文件"""
        from core.netcut import self_test
        report = self_test()
        try:
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'netcut_selftest.log')
            with open(path, 'w', encoding='utf-8') as f:
                f.write(report + '\n')
            print('[拔线自检]\n' + report)
        except Exception:
            pass
        # 气泡简短反馈
        first = report.splitlines()[0] if report else ''
        if 'SetTcpEntry 返回: 0' in report:
            self._say_with_expression("咕嘎～自检通过！连接已断开，看游戏是否自动重连～",
                                      "playful", "bounce")
        elif '未运行' in report:
            self._say_with_expression("咕嘎？炉石没开，开一局再自检！", "sad", "shake")
        else:
            self._say_with_expression(
                f"咕嘎？自检有问题，详见 netcut_selftest.log（{first}）",
                "sad", "shake")

    def _netcut(self, _checked=False, board=None):
        """强制断开炉石对局连接（跳战斗动画的时间技巧），需管理员权限"""
        import time as _time
        from core.netcut_budget import DisconnectBudget
        now = _time.time()
        # 重连保护期：拔线后 30 秒内不执行，避免打断重连（不稳定主因）
        reconnecting = getattr(self, '_netcut_reconnect_until', 0)
        if now < reconnecting:
            self._say_with_expression(
                "咕嘎～正在重连中，别急着拔！等本鹅说重连成功再拔～",
                "surprised", "peek")
            return
        # 冷却：15 秒内只允许拔一次
        last = getattr(self, '_last_netcut_t', 0)
        if now - last < 15:
            self._say_with_expression(
                "咕嘎～刚拔过线，等 15 秒再拔！拔太频繁会被服务器判负的！",
                "sad", "shake")
            return
        # 时机检测：只在战斗中拔（对手可见 = 战斗已开始，结果已定）
        from core import log_reader
        board = board or log_reader.read_current_board()
        budget = DisconnectBudget()
        game_key = board.get('game_key', '') if board else ''
        if game_key and game_key != getattr(self, '_netcut_game_key', None):
            self._netcut_game_key = game_key
            self._netcut_reconnect_until = 0
            self._last_netcut_t = 0
        state = budget.status(game_key)
        self._netcut_count = state['count']
        if state['exhausted']:
            self._say_with_expression(
                "咕嘎～这局已经拔了 9 次了！已经达到硬上限，稳一点！",
                "sad", "shake")
            return
        if now < getattr(self, '_netcut_reconnect_until', 0) or state['cooldown_remaining'] > 0:
            self._say_with_expression("咕嘎～重连保护中，等几秒再拔～", "surprised", "peek")
            return
        if board and board.get('active') and board.get('theirs'):
            pass  # 战斗中，OK
        elif board and board.get('active') and not board.get('theirs'):
            self._say_with_expression(
                "咕嘎～现在还在招募阶段，对手没开打，拔了也没用！等战斗开始再按！",
                "tsundere", "tsundere")
            return
        else:
            self._say_with_expression(
                "咕嘎？没检测到对局，开一把酒馆战棋再拔线！", "sad", "shake")
            return
        combat_id = f"{board.get('turn', '?')}:{len(board.get('theirs', []))}"
        try:
            result = budget.cut(game_key, combat_id)
        except RuntimeError as exc:
            self._say_with_expression(f"咕嘎？{exc}", "sad", "shake")
            return
        if result.get('ok'):
            n, msg = result['closed'], result['source']
            self._last_netcut_t = now
            self._netcut_count = result['count']
            self._netcut_reconnect_until = now + 30  # 重连保护期
            src = f"（按{msg}定位）" if msg else ""
            self._say_with_expression(
                f"咕嘎～已断开 {n} 条对局连接{src}（本局第 {result['count']} 次，剩余 {result['remaining']} 次）！正在重连，别乱点～",
                "playful", "bounce")
            self._watch_reconnect(0)
            return
        msg = result.get('error', '')
        if '管理员权限' in (msg or '') or 'NOT_ELEVATED' in (msg or ''):
            self._say_with_expression(
                "咕嘎？企鹅没管理员权限，拔不了线！桌面有个"
                "「修复拔线权限-右键管理员运行.bat」，右键管理员运行一次，"
                "重启电脑后就能拔线了！", "sad", "shake")
        elif msg:
            self._say_with_expression(f"咕嘎？{msg}", "sad", "shake")
        else:
            self._say_with_expression(f"咕嘎？{msg or '未找到可断开的对局连接'}", "sad", "shake")

    def _watch_reconnect(self, attempt: int):
        """拔线后轮询日志：确认重连成功（每 3 秒，最多 8 次）
        重连成功信号：日志恢复活跃（游戏已连回服务器继续写日志）"""
        import os as _os
        from core import log_reader
        try:
            b = log_reader.read_current_board()
            if b is None:
                # 日志暂时不可读（重连瞬间）→ 继续等
                if attempt < 8:
                    QTimer.singleShot(3000, lambda: self._watch_reconnect(attempt + 1))
                return
            if b.get('active') is False and b.get('reason') == 'log_stale':
                # 日志没动 = 还在重连中/游戏暂停写日志 → 继续等
                if attempt < 8:
                    QTimer.singleShot(3000, lambda: self._watch_reconnect(attempt + 1))
                return
            # 日志活跃（无论招募/战斗）→ 重连成功
            self._netcut_reconnect_until = 0
            phase = '回到招募' if not b.get('theirs') else '回到战斗中'
            self._say_with_expression(
                f"咕嘎～重连成功，{phase}！本局还能再拔"
                f"{9 - getattr(self, '_netcut_count', 0)} 次～",
                "happy", "bounce")
            return
        except Exception:
            pass
        if attempt < 8:
            QTimer.singleShot(3000, lambda: self._watch_reconnect(attempt + 1))
        else:
            self._say_with_expression(
                "咕嘎？重连有点慢…再等几秒，别点退出。",
                "surprised", "peek")

    # ==================== 自动观战（战斗开始自动分析） ====================

    def _auto_watch(self):
        """空闲30秒检查一次；检测到对局后提高到8秒；绝不自动拔线。"""
        if self._realtime_active():
            return
        import time as _time
        # 正在人工分析或 AI 忙时跳过
        if self._watch_analyzing:
            return
        if self._ai.is_busy():
            return
        try:
            from core import log_reader
            board = log_reader.read_current_board()
            if board is None:
                self._watch_timer.setInterval(30000)
                return
            # 对局结束 → 记录战绩（只记结果，最近5场）+ 调侃播报（仅状态转换时一次）
            if board.get('reason') == 'game_over':
                if self._watch_phase != 'gameover':
                    self._watch_phase = 'gameover'
                    self._on_game_over(board)
                return
            if not board.get('active'):
                self._watch_phase = ''
                self._watch_timer.setInterval(30000)
                return
            self._watch_timer.setInterval(8000)
            in_combat = bool(board.get('theirs'))
            phase = 'combat' if in_combat else 'recruit'
            if phase == 'combat':
                self._last_combat_t = _time.time()
            self._watch_phase = phase
        except Exception:
            pass

    # ==================== 战绩记忆（最近5场，只记结果） ====================

    def _on_game_over(self, board):
        """对局结束（自动观战检测到）：记录战绩 → 按最近战绩调侃播报"""
        from core import match_history
        ps = str(board.get('playstate') or '').upper()
        try:
            place = int(board.get('place') or 0)
        except (TypeError, ValueError):
            place = 0
        if place <= 0 and ps == 'FINAL_GAMEOVER':
            # Unknown result: never pollute persistent history with a guessed
            # win/loss record.
            return
        if place == 1 or ps == 'WON':
            result = 'win'
        elif ps == 'CONCEDED':
            result = 'concede'
        else:
            result = 'loss'
        h = match_history.record_result(result=result, place=place or board.get('place'),
                                        game_key=board.get('game_key'),
                                        turns=board.get('turn'))
        s = match_history.stats(h)
        roast = match_history.build_roast(h) or "咕嘎～这局打完了！"
        expr = 'happy' if result == 'win' else 'sad'
        action = 'bounce' if result == 'win' else 'shake'
        self._say_with_expression(roast, expr, action)
        print(f"[战绩] 记录:{result} 名次:{place or board.get('place')} 最近{s['total']}场:"
              f"{match_history.format_line(h)}")

    def _show_history(self):
        """右键「最近战绩」：播报最近5场 + 调侃"""
        from core import match_history
        h = match_history.recent()
        if not h:
            self._say_with_expression(
                "咕嘎？还没记录过战绩，先去打完一把酒馆战棋！", "tsundere", "tsundere")
            return
        s = match_history.stats(h)
        roast = match_history.build_roast(h) or ''
        avg = f"，平均第{s['avg_place']}名" if s.get('avg_place') else ''
        self._say_with_expression(
            f"咕嘎～最近{len(h)}场战绩：{match_history.format_line(h)}"
            f"（吃鸡{s['wins']}次{avg}）！{roast}", "playful", "point")

    # ==================== 看棋盘（日志优先，视觉兜底） ====================

    def _show_bg_ratings(self):
        """读内存看分数: HearthMirror.dll 即时读取自己的战棋分数+8人大厅
        比视觉快10倍(零截屏零API), 但只能读自己的分数(炉石内存不存对手MMR)
        """
        self._say_with_expression("咕嘎～本鹅读内存看看分数！", "happy", "point")
        threading.Thread(target=self._read_ratings_bg, daemon=True).start()

    def _read_ratings_bg(self):
        """后台线程: 读内存拿分数 + API查对手分数, 通过信号回主线程"""
        from core import memory_reader, leaderboard_api
        import json as _json
        my_rating = memory_reader.get_my_rating()
        lobby = memory_reader.get_lobby_info()
        # 查每个对手的API分数
        if lobby:
            for p in lobby:
                try:
                    p['api_score'] = leaderboard_api.lookup_score(p['name'])
                except Exception:
                    p['api_score'] = None
        # 信号回主线程
        self._bg_rating_done.emit(
            _json.dumps({'my_rating': my_rating, 'lobby': lobby}))

    def _on_bg_rating_done(self, text):
        """主线程槽: 内存读取+API查询完成 -> 直接播报8人分数"""
        import json as _json
        try:
            data = _json.loads(text)
        except Exception:
            self._say_with_expression(
                "咕嘎？读内存失败了，炉石是不是没开？", "sad", "shake")
            return
        my_rating = data.get('my_rating')
        lobby = data.get('lobby')
        if my_rating is None:
            self._say_with_expression(
                "咕嘎？读不到分数，炉石没开或不在对局里！", "sad", "shake")
            return
        if not lobby:
            self._say_with_expression(
                f"咕嘎～你的战棋分数: {my_rating}！但不在对局中。",
                "normal", "point")
            return
        # 构建8人分数播报(直接显示, 不走AI润色, 更快更准)
        lines = [f"你的战棋分数: {my_rating}", "本局8人分数:"]
        on_board = 0
        for i, p in enumerate(lobby):
            name = p['name']
            api_score = p.get('api_score')
            me_tag = " (你)" if p.get('is_me') else ""
            if api_score:
                lines.append(f"  {i+1}. {name}{me_tag} = {api_score}分")
                on_board += 1
            else:
                lines.append(f"  {i+1}. {name}{me_tag} = 未上榜")
        if on_board == 0:
            lines.append("本局无人上榜(分数都较低或缓存过期)")
        # 直接显示在大气泡(不走AI, 避免延迟)
        self._say_with_expression("\n".join(lines), "normal", "point")

    def _toggle_hover_rating(self):
        """开关: 悬停查分"""
        self._hover_rating_on = not self._hover_rating_on
        if self._hover_rating_on:
            # 先加载DLL(可能需要几秒)
            self._say_with_expression("咕嘎～悬停查分开启！加载内存模块...", "happy", "bounce")
            threading.Thread(target=self._init_hover_rating_bg, daemon=True).start()
        else:
            self._hover_timer.stop()
            self._last_hovered_id = 0
            self._say_with_expression("咕嘎～悬停查分已关闭。", "normal", "none")

    def _init_hover_rating_bg(self):
        """后台线程: 初始化DLL加载 + 预热排行榜API缓存, 成功后启动轮询定时器"""
        from core import memory_reader, leaderboard_api
        # 1. 加载磁盘缓存(秒级)
        leaderboard_api.load_cache_from_disk()
        ok = memory_reader._ensure_loaded()
        if not ok:
            err = memory_reader.get_load_error() or "未知错误"
            QTimer.singleShot(0, lambda: self._say_with_expression(
                f"咕嘎？内存模块加载失败: {err}", "sad", "shake"))
            return
        # 2. 检查API缓存是否需要更新(后台拉取, 不阻塞)
        cache = leaderboard_api.get_cache()
        if cache is None:
            QTimer.singleShot(0, lambda: self._say_with_expression(
                "咕嘎～内存就绪！正在拉取国服排行榜(约5秒)...", "happy", "point"))
            def _on_lb_done(c):
                if c:
                    QTimer.singleShot(0, lambda: self._say_with_expression(
                        f"咕嘎～排行榜就绪！{c['total']}人数据, 悬停玩家即可看分数～",
                        "happy", "bounce"))
                    QTimer.singleShot(0, self._hover_timer.start)
                else:
                    QTimer.singleShot(0, lambda: self._say_with_expression(
                        "咕嘎？排行榜拉取失败, 但仍可看自己的分数", "sad", "shake"))
                    QTimer.singleShot(0, self._hover_timer.start)
            leaderboard_api.fetch_in_background(_on_lb_done)
        else:
            QTimer.singleShot(0, self._hover_timer.start)
            QTimer.singleShot(0, lambda: self._say_with_expression(
                f"咕嘎～就绪！排行榜缓存{cache['total']}人, 悬停玩家即可看分数～",
                "happy", "point"))
            # 后台悄悄刷新(过期才拉)
            leaderboard_api.fetch_in_background()

    def _poll_hovered_rating(self):
        """定时器槽: 轮询鼠标悬停的玩家实体ID(放后台线程避免阻塞UI)"""
        if not self._hover_rating_on:
            return
        threading.Thread(target=self._poll_hover_bg, daemon=True).start()

    def _poll_hover_bg(self):
        """后台线程: 读悬停实体ID, 变化时显示8人分数气泡"""
        from core import memory_reader, leaderboard_api
        eid = memory_reader.get_hovered_entity_id()
        if eid is None:
            eid = 0
        if eid == self._last_hovered_id:
            return  # 没变化, 不处理
        self._last_hovered_id = eid
        if eid == 0:
            return  # 停止悬停, 不显示(气泡自动消失)
        # 有悬停: 读大厅8人 + 自己分数 + API匹配每人分数
        my_rating = memory_reader.get_my_rating()
        lobby = memory_reader.get_lobby_info()
        if lobby:
            for p in lobby:
                try:
                    p['api_score'] = leaderboard_api.lookup_score(p['name'])
                except Exception:
                    p['api_score'] = None
        info = {
            'entity_id': eid,
            'my_rating': my_rating,
            'lobby': lobby,
        }
        QTimer.singleShot(0, lambda: self._show_hover_rating(info))

    def _show_hover_rating(self, info):
        """主线程: 显示悬停时8人分数气泡(实体ID无法精确映射到单个玩家,
        显示当前8人各自的分数列表, 让用户对照悬停位置)"""
        my_rating = info.get('my_rating')
        lobby = info.get('lobby')
        if my_rating is None:
            return  # 读不到分数, 不显示
        if not lobby:
            self._say_with_expression(
                f"你的战棋分数: {my_rating}", "normal", "point")
            return
        # 构建8人分数列表(按内存顺序, 通常对应游戏内排行榜位置)
        lines = []
        for i, p in enumerate(lobby):
            name = p['name']
            api_score = p.get('api_score')
            me_tag = " (你)" if p.get('is_me') else ""
            if api_score:
                lines.append(f"{i+1}.{name}{me_tag}={api_score}")
            else:
                lines.append(f"{i+1}.{name}{me_tag}=未上榜")
        text = " | ".join(lines)
        self._say_with_expression(text, "normal", "point")

    def _review_bg_logs(self):
        from pathlib import Path
        from PyQt5.QtWidgets import QDialog,QVBoxLayout,QTextBrowser,QLabel
        from core.bg_combat_review import parse_hdt_records,render_review
        folder=Path.home()/'Desktop/游戏/HDT/对战记录'
        files=list(folder.glob('*.txt')) if folder.exists() else []
        dialog=QDialog(self)
        dialog.setWindowTitle('咕嘎 · 日志分析 / HDT 胜率记录')
        dialog.resize(980,650)
        layout=QVBoxLayout(dialog)
        view=QTextBrowser()
        layout.addWidget(view)
        if files:
            source=max(files,key=lambda p:p.stat().st_mtime)
            if source.stat().st_size<=16*1024*1024:
                report=parse_hdt_records(source.read_text(encoding='utf-8-sig'))
                from datetime import datetime
                layout.addWidget(QLabel('来源：'+source.name+'；最后写入：'+datetime.fromtimestamp(source.stat().st_mtime).strftime('%m-%d %H:%M:%S')+
                    '。记录可能滞后，未自动对齐当前战斗。'))
                view.setMarkdown(render_review(report))
            else:
                view.setPlainText('记录超过本次读取预算，请选择较小的按日记录。')
        else:
            view.setPlainText('尚未发现本机 HDT 对战记录；不会用假想对手胜率代替插件数据。')
        self._bg_review_dialog=dialog
        dialog.show()

    def _open_bg_live(self):
        from ui.bg_live_panel import BattlegroundLivePanel
        if not getattr(self, '_bg_live_panel', None):
            self._bg_live_panel = BattlegroundLivePanel(self)
        self._bg_live_panel.show()
        self._bg_live_panel.raise_()
        self._bg_live_panel.activateWindow()

    def _open_bg_agent(self):
        from ui.bg_agent_panel import BattlegroundAgentPanel
        if not getattr(self, '_bg_agent_panel', None):
            self._bg_agent_panel = BattlegroundAgentPanel(self)
            self._bg_agent_panel.speak_requested.connect(
                lambda text: self._say_with_expression(text, "normal", "point"))
        self._bg_agent_panel.show()
        self._bg_agent_panel.raise_()
        self._bg_agent_panel.activateWindow()

    def _see_board(self):
        """① power.log 日志解析（零API零429，最精确）② 失败才走 GLM 视觉"""
        from core import log_reader
        board = log_reader.read_current_board()
        if board is None:
            self._say_with_expression("咕嘎？没找到炉石日志，先开一把酒馆战棋吧！", "sad", "shake")
            return
        if not board.get('active'):
            # 日志太久没更新 = 没在玩
            self._say_with_expression(
                f"咕嘎？日志 {board.get('stale_min', '?')} 分钟没动了，你根本没在玩嘛！"
                f"开一把酒馆战棋，本鹅就能看牌面啦～", "tsundere", "tsundere")
            return
        if board.get('mine'):
            self._on_board_from_log(board)
            return
        self._say_with_expression("咕嘎～日志没读到，让本鹅看看你的屏幕…", "surprised", "point")
        from core import vision
        from PyQt5.QtCore import QTimer
        def _cb_board(text):
            QTimer.singleShot(0, lambda: self._on_board_seen(text))
        vision.analyze_in_thread(None, _cb_board)

    def _on_board_from_log(self, board):
        """日志局面 → 胜率模拟+解读 → 对手阵容分析 → DS 军师分析（不调用任何视觉API）
        战斗阶段：实时胜率解读（谁先倒/最大威胁/胜负手）+ 对手流派分析
        招募阶段：假想对手参考胜率 + 己方阵容方向展望"""
        from core.log_reader import format_board
        from core.battle_sim import win_rate, format_rate, minion_from_log, analyze_matchup, format_matchup_insight
        board_text = format_board(board)
        self._say_with_expression("咕嘎～日志读到牌面了！", "normal", "point")
        rate_text = ''
        insight = ''
        opp_text = ''
        outlook = ''
        mine = [minion_from_log(m) for m in board['mine']]
        theirs = [minion_from_log(m) for m in board['theirs']]
        if mine and theirs:
            # 战斗中：对手可见 → 真实胜率 + 结构化解读（谁先倒/最大威胁/胜负手）
            r = win_rate(mine, theirs, n=600)
            rate_text = format_rate(r)
            try:
                m = analyze_matchup(mine, theirs, n=600)
                insight = format_matchup_insight(m)
            except Exception as e:
                print(f"[胜率解读] 失败: {e}")
            # 对手阵容分析（流派/核心/威胁/弱点）
            try:
                from core.comp_analysis import summarize_opponent
                opp_text = summarize_opponent(theirs)
            except Exception as e:
                print(f"[对手分析] 失败: {e}")
            speak = f"咕嘎～模拟了 600 场：{rate_text}"
            if insight:
                speak += f" {insight}"
            self._say_with_expression(speak, "surprised", "point")
        elif mine:
            # 招募阶段：对手不可见 → 用"当前酒馆等级真实卡池常见阵容"做假想对手
            r = win_rate(mine, _proxy_opponents(board.get('tier', 4)), n=600)
            rate_text = format_rate(r)
            # 招募阶段顺带展望己方阵容方向（喂给军师作上下文）
            try:
                from core.lineup_planner import project_lineup, format_outlook
                outlook = format_outlook(project_lineup(board))
            except Exception as e:
                print(f"[阵容展望] 失败: {e}")
            self._say_with_expression(
                f"咕嘎～还没开打看不到对手，本鹅拿{board.get('tier', 4)}级真实卡池"
                f"算了算：{rate_text}（参考）", "surprised", "point")
        prompt_tail = "请以军师身份分析：当前局面核心矛盾、最优先的1-3个动作（买/刷/升/转/站位）、下一步关键节奏。"
        self._ai.ask(
            f"饲养员让你分析他的酒馆战棋对局（来自游戏日志，精确数据）：\n{board_text}\n"
            + (f"本鹅的战斗模拟器预测：{rate_text}\n" if rate_text else "")
            + (f"本鹅的模拟解读：{insight}\n" if insight else "")
            + (f"对手阵容分析：{opp_text}\n" if opp_text else "")
            + (f"本鹅的阵容展望：{outlook}\n" if outlook else "")
            + prompt_tail,
            rag_query=board_text,
            system_override=WINRATE_OVERRIDE if (insight or opp_text) else ANALYSIS_OVERRIDE)

    def _on_board_seen(self, vision_text):
        if not vision_text or vision_text.startswith('❌'):
            self._say_with_expression(f"咕嘎？{vision_text or '什么都没看到'}",
                                      "sad", "shake")
            return
        # 中间反馈：让用户知道在分析中
        self._say_with_expression("咕嘎看到牌面了，本鹅正在分析…", "normal", "point")
        # 胜率预测：从视觉结果提取随从 JSON → 蒙特卡洛模拟 + 结构化解读
        rate_text = ''
        insight = ''
        opp_text = ''
        try:
            import json as _json
            import re
            from core.battle_sim import Minion, win_rate, format_rate, analyze_matchup, format_matchup_insight
            m = re.search(r'```json\s*(\{.*?\})\s*```', vision_text, re.S)
            if m:
                data = _json.loads(m.group(1))
                mine = [Minion(**x) for x in data.get('minions', []) if x.get('atk')]
                theirs = [Minion(**x) for x in data.get('opponents', []) if x.get('atk')]
                if mine and theirs:
                    r = win_rate(mine, theirs, n=600)
                    rate_text = format_rate(r)
                    try:
                        mm = analyze_matchup(mine, theirs, n=600)
                        insight = format_matchup_insight(mm)
                    except Exception as e:
                        print(f"[胜率解读] 失败: {e}")
                    try:
                        from core.comp_analysis import summarize_opponent
                        opp_text = summarize_opponent(theirs)
                    except Exception as e:
                        print(f"[对手分析] 失败: {e}")
                    speak = f"咕嘎～模拟了 600 场：{rate_text}"
                    if insight:
                        speak += f" {insight}"
                    self._say_with_expression(speak, "surprised", "point")
        except Exception as e:
            print(f"[胜率模拟] 失败: {e}")
        self._ai.ask(
            f"饲养员让你看他的酒馆战棋棋盘，你截屏看到的局面：\n{vision_text}\n"
            + (f"本鹅的战斗模拟器预测：{rate_text}\n" if rate_text else "")
            + (f"本鹅的模拟解读：{insight}\n" if insight else "")
            + (f"对手阵容分析：{opp_text}\n" if opp_text else "")
            + f"请以军师身份分析：当前局面核心矛盾、最优先的1-3个动作（买/刷/升/转/站位）、下一步关键节奏。",
            rag_query=vision_text,
            system_override=WINRATE_OVERRIDE if (insight or opp_text) else ANALYSIS_OVERRIDE)

    # ==================== 整体弹跳 ====================

    def _play_anim(self, atype):
        self._anim_type = atype
        self._anim_start.start()
        if not self._bounce_timer.isActive():
            self._bounce_timer.start()

    def _bounce_tick(self):
        elapsed = self._anim_start.elapsed()
        if elapsed > self._anim_duration:
            self._anim_offset = QPoint(0, 0)
            self._bounce_timer.stop()
            self._anim_type = ""
            self.update()
            return
        t = elapsed / self._anim_duration
        if self._anim_type == "bounce":
            progress = math.sin(t * math.pi)
            y = -5 * progress
            self._anim_offset = QPoint(0, int(y))
        elif self._anim_type == "shake":
            amp = 4 * (1 - t)
            x = amp * math.sin(t * math.pi * 4)
            self._anim_offset = QPoint(int(x), 0)
        self.update()

    # ==================== 工具 ====================

    def _force_top(self):
        try:
            hwnd = int(self.winId())
            ctypes.windll.user32.SetWindowPos(
                hwnd, HWND_TOPMOST if self._config._s.value('agent/topmost', True, type=bool) else HWND_NOTOPMOST, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, style | 0x00000080)
            # The user must be able to record and screenshot the companion.
            ctypes.windll.user32.SetWindowDisplayAffinity(hwnd,0)
        except Exception as e:
            pass

    def _reset_pos(self):
        scr = QApplication.primaryScreen()
        if scr:
            g = scr.geometry()
            self.move((g.width() - self.width()) // 2,
                     (g.height() - self.height()) // 2)
        self._say("咕嘎～回到原位了。")

    def _load_state(self):
        self._status.set_all(**self._config.load_status())

    def _save_state(self):
        self._config.save_pos(self.x(), self.y())
        s = self._status.snapshot()
        self._config.save_status(s["hunger"], s["happy"], s["energy"])

    def _quit(self):
        self._voice_record_timer.stop()
        if self._floating_launcher:self._floating_launcher.hide()
        self._pet_physics.stop()
        if getattr(self,'_screen_tools',None):self._screen_tools.stop()
        if getattr(self,'_multi_agent',None):self._multi_agent.close()
        self._agent_bubble.hide()
        self._self_mcp.close()
        if self._agent_panel:
            self._agent_panel.shutdown()
        if self._realtime_voice:
            self._realtime_voice.stop()
        self._save_state()
        for _t in (self._bg_timer, self._watch_timer, self._save_timer,
                   self._bounce_timer, self._talk_timer, self._bubble_timer,
                   self._hover_timer, self._gaze_timer):
            try:
                _t.stop()
            except Exception:
                pass
        try:
            self._tts.stop()
            self._anim.stop()
            if self._anim3d is not None:
                self._anim3d.close()
            self._hook.stop()
            self._status.stop()
        except Exception:
            pass
        try:
            self._tray.hide()
        except Exception:
            pass
        QApplication.quit()
        # 强制退出: pythonnet/CLR 后台线程会阻止进程退出, 必须强制
        import os as _os
        _os._exit(0)

    def closeEvent(self, e):
        # 点X/Alt+F4 直接退出(不再只隐藏导致进程残留)
        self._quit()
