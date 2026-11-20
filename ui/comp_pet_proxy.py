# -*- coding: utf-8 -*-
"""参赛版窗口 ←→ v1 完整智能体（AgentChatWindow）的最小宿主适配层。

背景: v1(heimao-zhuochong) 的复合任务能力(DSH/工具链/跨应用)全部挂在
PetWindow 上; 参赛版 CompWindow 为了干净产品形态只保留气泡+派单, 于是
"多步骤复合任务"在新版里没有入口。这里不复制 AgentChatWindow, 而是给它
一个最小宿主代理: 形象/语音/气泡转发到 CompWindow, 其余 v1 专属部件用
空实现兜底, 这样复合任务引擎可以原样复用, 且不破坏参赛版现有能力。
"""
from PyQt5.QtCore import QSettings, QObject
from PyQt5.QtWidgets import QWidget


class _TeamShim:
    """单宠物形态下 v1 多智能体接口的空实现(召唤/讨论不参与比赛演示)."""
    companions = {}

    def work_state(self, *a, **k): pass
    def user_interaction(self, *a, **k): pass
    def say(self, *a, **k): pass
    def gesture(self, *a, **k): pass
    def set_expression(self, *a, **k): pass
    def summon(self, *a, **k): pass
    def summon_pair(self, *a, **k): pass
    def start_discussion(self, *a, **k): pass
    def status(self): return {}


class _StatusShim(QObject):
    def snapshot(self):
        return {'hunger': 100, 'mood': 100, 'energy': 100, 'mode': 'comp'}


class _BubbleShim:
    """参赛版气泡是 CompWindow 自己画的, 这里只满足截屏/语音流程的显隐调用."""
    def set_voice_state(self, *a, **k): pass
    def set_call_active(self, *a, **k): pass
    def set_call_reconnecting(self, *a, **k): pass
    def isVisible(self): return True
    def show(self): pass
    def hide(self): pass
    def follow_pet(self): pass


class _ScreenToolsShim:
    """截图分析/划词解释依赖 v1 的热键与选区钩子; 参赛版先给出明确提示."""
    selection_enabled = False
    hotkey_errors = []
    last_selection_status = 'unavailable'

    def __init__(self, comp):
        self.comp = comp

    def set_selection_enabled(self, on):
        self.selection_enabled = bool(on)

    def begin(self, mode='ask'):
        self.comp.show_bubble('截图分析在完整版窗口里可用；当前参赛版先按文字指令执行。')


class _ConfigShim:
    """AgentChatWindow/chat_shell 只用到 _s.value(...) 与少量动态属性."""
    def __init__(self):
        self._s = QSettings('Guga', 'GugaComp')
        self.opacity = 1.0


class CompPetProxy(QWidget):
    """把 AgentChatWindow 需要的 PetWindow 接口转发到 CompWindow."""

    def __init__(self, comp):
        super().__init__(comp)
        self.comp = comp
        self.hide()
        self._tts = comp._tts
        self._anim = comp._anim
        self._pet_scale = getattr(comp, '_pet_scale', 1.0)
        self._config = _ConfigShim()
        self._screen_tools = _ScreenToolsShim(comp)
        self._agent_bubble = _BubbleShim()
        self._multi_agent = _TeamShim()
        self._status = _StatusShim(self)
        self._voice_mode = False
        self._recording = False
        self._realtime_voice = None
        self._voice_call_panel = None
        self._pending_voice_gesture = None

    @property
    def _agent_panel(self):
        return getattr(self.comp, '_agent_window', None)

    def _ensure_agent_panel(self):
        self.comp.open_agent_window()
        return self.comp._agent_window

    # ---- 形象/语音/气泡 ----
    @property
    def _anim2d(self): return self.comp._anim

    @property
    def _anim3d(self): return self.comp._anim

    def _show_agent_bubble(self, text, *a, **k):
        self.comp.show_bubble(str(text or ''))

    def _say_agent_reply(self, text, *a, **k):
        self.comp.show_bubble(str(text or ''))

    def _apply_agent_style(self, expression=None, action=None):
        if expression:
            try: self.comp._anim.set_expression(expression)
            except Exception: pass

    def _perform_gesture(self, name):
        name = str(name or '').strip()
        try:
            if name in ('idle_doze', 'doze') and 'idle:doze' in self.comp._anim.clips:
                self.comp._anim.play_clip('idle:doze', loop=False)
            elif name in ('idle_heart', 'heart') and 'idle:heart' in self.comp._anim.clips:
                self.comp._anim.play_clip('idle:heart', loop=False)
            elif name in ('dance', 'sway'):
                self.comp._anim.trigger_action('dance')
            elif name:
                self.comp._anim.trigger_action(name)
            else:
                self.comp._anim.trigger_action('bounce')
        except Exception:
            pass

    def _act(self, name):
        if name == 'dance': self._perform_gesture('dance')
        elif name == 'sleep': self.comp._anim.set_expression('sleepy')
        elif name in ('feed', 'play', 'pet'):
            self.comp._anim.set_expression('happy')
            self._perform_gesture('bounce')

    # ---- v1 语音接口: 长语音复用完整对话窗自带的"唤醒"按钮 ----
    def _realtime_active(self):
        panel = self._agent_panel
        return bool(panel and getattr(panel, 'voice_active', False))

    def _open_agent_voice(self):
        panel = self._ensure_agent_panel()
        button = getattr(panel, 'voice_button', None)
        if button is None:
            self.comp.show_bubble('这个版本没有长语音按钮。')
            return
        panel.show()
        panel.raise_()
        button.setChecked(True)
    def _end_agent_voice(self): pass
    def _queue_voice_gesture(self, name): self._perform_gesture(name)
    def _on_realtime_speaking(self, *a, **k): pass
    def _on_user_transcript(self, *a, **k): pass
    def _on_user_interaction(self, *a, **k): pass
    def _configure_realtime_voice(self): self.comp.show_bubble('实时语音配置请在完整版窗口使用。')

    # ---- 位置/外观 ----
    def _save_state(self): pass
    def _reset_pos(self):
        try: self.comp.move_to_default()
        except Exception: pass
    def _force_top(self):
        try:
            self.comp.raise_()
        except Exception: pass
    def _show_size_dialog(self): self.comp.show_bubble('大小调节请在完整版窗口使用。')
    _bongo = None
    _iw = 0
    _ih = 0
    def _show_bongo(self): pass

    # ---- 战棋入口(逻辑在 v1 面板里, 此处只做入口提示) ----
    def _open_bg_agent(self): self.comp.show_bubble('战棋决策实验室请在完整版窗口使用。')
    def _open_bg_live(self): self.comp.show_bubble('战棋实战操作请在完整版窗口使用。')
