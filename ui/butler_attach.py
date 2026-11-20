# -*- coding: utf-8 -*-
"""M3: 实机装配 — 把管家系统接进 PetWindow(占位素材即可跑通)

装配内容(全部非侵入, PetWindow 零改动):
  1. ModeManager + ArmorAssetResolver, 绑定 pet 的 SpriteAnimator
     —— 右键菜单"👔 管家与员工"可召唤/召回, 召唤动画播完自动接 idle
  2. ButlerHub.attach(pet._ai)
     —— 对话自动按当前在岗者检索知识库 + 人设注入(称呼少爷)
  3. ReminderService(持久化 data/butler_reminders.json)
     + ButlerAnnouncer(pet._tts.speak) —— 到期语音播报
  4. 员工切换 TTS 报幕("少爷, 学者已召唤上岗")
  5. 右键菜单附"添加提醒…"

用法(main.py): attach_butler(window)
"""
import logging
from pathlib import Path

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QInputDialog

from core.mode_manager import ModeManager
from core.butler_hub import ButlerHub
from core.butler_reminder import ReminderService
from core.butler_announcer import ButlerAnnouncer
from ui.asset_interface import ArmorAssetResolver

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parent.parent
RES = REPO / 'resources'
PACKS = REPO / 'armor_packs'

# 菜单显示顺序: 召回管家 + 六员工
STAFF = (
    ('butler',    '召回管家 · 回中枢'),
    ('scholar',   '召唤学者 · 教育'),
    ('tactician', '召唤军师 · 金融'),
    ('steward',   '召唤事务官 · 消费'),
    ('navigator', '召唤领航员 · 出海'),
    ('medic',     '召唤医师 · 医疗'),
    ('artisan',   '召唤工匠 · 制造'),
)


class ButlerRuntime:
    """管家系统的实机运行时(PetWindow 的伴生对象)."""

    def __init__(self, pet):
        self.pet = pet
        self.mm = ModeManager(packs_dir=str(PACKS),
                              animator=pet._anim,
                              resolver=ArmorAssetResolver(str(RES)))
        self.hub = ButlerHub(self.mm, repo_root=str(REPO)).attach(pet._ai)
        self.reminders = ReminderService()            # data/butler_reminders.json
        self.announcer = ButlerAnnouncer(pet._tts.speak).bind(self.reminders)
        self._last_mode = self.mm.current_id
        self.mm.mode_changed.connect(self._announce_switch)

        # 提醒巡检: 主线程 QTimer, 30s 一拍
        self._tick_timer = QTimer(pet)
        self._tick_timer.setInterval(30 * 1000)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start()

        # 菜单在 _ensure_pet_menu(启动后 1.2s)构建, 等它建好再追加
        QTimer.singleShot(1700, self._inject_menu)
        logger.info('butler runtime attached (mode=%s)', self.mm.current_id)

    # ==================== 召唤/召回 ====================

    def summon(self, mode_id):
        """召唤员工/召回管家; 返回是否受理."""
        return self.mm.switch(mode_id)

    def _announce_switch(self, mode_id):
        """员工上岗/退场 TTS 报幕(占位期也保留仪式感)."""
        if mode_id == self._last_mode:
            return
        leaving = self._last_mode
        self._last_mode = mode_id
        pack = self.mm.get_pack(mode_id)
        name = (pack.display_name if pack else mode_id)
        if mode_id == 'butler':
            line = '%s，%s已退场，我回来了。有事随时吩咐。' % (
                getattr(pack, 'address', '少爷'), self._name_of(leaving))
        else:
            line = '%s，%s已召唤上岗，请吩咐。' % (
                getattr(pack, 'address', '少爷'), name)
        try:
            self.pet._tts.speak(line)
        except Exception:
            logger.exception('tts announce failed')

    def _name_of(self, mode_id):
        pack = self.mm.get_pack(mode_id)
        return (pack.display_name if pack else mode_id)

    # ==================== 提醒 ====================

    def _tick(self):
        try:
            self.reminders.tick()
        except Exception:
            logger.exception('reminder tick failed')

    def add_reminder_dialog(self):
        """右键菜单: 口语时间 + 事项, 走 parse_when 解析."""
        text, ok = QInputDialog.getText(
            self.pet, '添加提醒', '时间和事项（如: 明天9点 开周会 / +30m 喝水）:')
        if not ok or not text.strip():
            return
        parts = text.strip().split(None, 1)
        if len(parts) == 2:
            when_text, item = parts
        else:
            when_text, item = '+30m', parts[0]
        try:
            r = self.reminders.add(when_text, item)
        except ValueError as exc:
            self.pet._say_with_expression('少爷，这个时间我没看懂：%s' % exc,
                                          'surprised')
            return
        try:
            self.pet._tts.speak('%s，记下了——%s，%s提醒您。' % (
                '少爷', r['text'], r.get('when_text', when_text)))
        except Exception:
            pass

    # ==================== 菜单 ====================

    def _inject_menu(self):
        try:
            menu = self.pet._ensure_management_menu()
        except Exception:
            logger.exception('pet menu unavailable')
            return
        staff = menu.addMenu('👔 专业员工与提醒')
        for mode_id, label in STAFF:
            staff.addAction(label).triggered.connect(
                lambda checked=False, m=mode_id: self.summon(m))
        staff.addSeparator()
        staff.addAction('⏰ 添加提醒…').triggered.connect(self.add_reminder_dialog)
        n = self.reminders.list_pending()
        staff.addAction('📋 待办提醒(%d 条)' % len(n)).triggered.connect(
            self._show_reminders)
        logger.info('butler menu injected')

    def _show_reminders(self):
        pending = self.reminders.list_pending()
        if not pending:
            self.pet._say_with_expression('少爷，目前没有待办提醒。', 'normal')
            return
        lines = ['%s %s' % (r.get('when_text', ''), r['text']) for r in pending]
        self.pet._say_with_expression('待办有：%s' % '；'.join(lines[:4]), 'normal')


def attach_butler(pet):
    """装配入口: main.py 里 attach_butler(window)."""
    try:
        pet._butler = ButlerRuntime(pet)
        return pet._butler
    except Exception:
        logger.exception('butler attach failed (continuing without)')
        return None
