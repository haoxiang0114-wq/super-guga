# -*- coding: utf-8 -*-
"""M2: 管家语音播报适配 — 提醒到期 -> 管家口吻 -> 语音

松耦合设计: 本模块不 import 任何 UI/TTS, 只持有一个 speak_fn(text) 可调用.
实机装配(在 pet_window 或启动脚本里):
    from ui.tts_engine import TTSEngine
    tts = TTSEngine()
    announcer = ButlerAnnouncer(tts.speak)
    announcer.bind(reminder_service)
之后 reminder.tick() 到期即自动以管家口吻播报.

也支持 habit 主动建议播报: announce_suggestion(text).
"""
import logging

logger = logging.getLogger(__name__)


class ButlerAnnouncer:
    """把提醒/建议文本转成管家口吻并交给 speak_fn 播报."""

    def __init__(self, speak_fn, address='少爷'):
        self.speak_fn = speak_fn          # fn(text:str) -> None (由调用方注入 TTS)
        self.address = address
        self._spoke = 0

    # ==================== 绑定 ====================

    def bind(self, reminder_service):
        """接管 ReminderService 的到期回调(保留其原有 on_due, 串联执行)."""
        old_cb = getattr(reminder_service, 'on_due', None)
        outer = self

        def chained(reminder):
            try:
                outer.announce_reminder(reminder)
            finally:
                if callable(old_cb) and old_cb is not chained:
                    old_cb(reminder)
        reminder_service.on_due = chained
        return self

    # ==================== 文案 ====================

    def format_reminder(self, reminder):
        """{'text','when_text',...} -> 管家口吻的一句话."""
        text = str(reminder.get('text', '')).strip()
        when = str(reminder.get('when_text', '')).strip()
        head = '%s，' % self.address
        if when and when not in ('到时候', ''):
            return '%s您交代的时间到了（%s）——%s。请过目安排。' % (head, when, text)
        return '%s有件事到点了——%s。请过目安排。' % (head, text)

    def format_suggestion(self, suggestion_text):
        """习惯建议 -> 管家提议口吻."""
        return '%s，%s？' % (self.address, str(suggestion_text).strip().rstrip('。？?'))

    # ==================== 播报 ====================

    def announce_reminder(self, reminder):
        line = self.format_reminder(reminder)
        self._say(line)

    def announce_suggestion(self, suggestion_text):
        self._say(self.format_suggestion(suggestion_text))

    def _say(self, text):
        self._spoke += 1
        logger.info('butler announce: %s', text)
        if self.speak_fn:
            try:
                self.speak_fn(text)
            except Exception:
                logger.exception('speak_fn failed')

    @property
    def spoke_count(self):
        return self._spoke
