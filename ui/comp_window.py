# -*- coding: utf-8 -*-
"""参赛版桌宠窗口 — 只保留产品本体: 管家 + 六员工召唤 + 对话 + 提醒

与原版 pet_window(开发原理验证, 含战棋/唱歌/多智能体等)完全隔离:
  - 形象: 当前为占位帧(企鹅), 队友素材到位后由 import_video.py 替换
  - 菜单: 召唤/召回 + 添加提醒 + 待办 + 退出, 无任何开发期功能
  - 对话: 点击桌宠打开输入条, 回复走 ButlerHub(员工知识库+人设)

被 comp_main.py 启动. 原版 main.py 不受影响.
"""
import json
import logging
import re
import threading
import time

from PyQt5.QtCore import Qt, QPoint, QTimer, pyqtSignal
from PyQt5.QtGui import QPainter, QColor, QFont
from PyQt5.QtWidgets import QWidget, QMenu, QLineEdit, QApplication

from ui.sprite_animator import SpriteAnimator, adaptive_display_width
from ui.tts_engine import TTSEngine
from core.ai_chat import (AIChatEngine, endpoint_label, model_config_error,
                          provider_label)
from ui.butler_attach import ButlerRuntime
from core.butler_cloud import (ButlerCloud, looks_like_device_task,
                               is_failed, is_aborted)
from core.local_agent import LocalAgent
from core import control_session
from core import guga_paths
from ui.task_panel import TaskPanel
from ui.control_badge import ControlBadge
from ui.control_session_bar import ControlSessionBar, ControlSessionHotkey

logger = logging.getLogger(__name__)

# 只有明确说“另一台/二号电脑/那台装了 XX 的机器”才走跨设备；识别不出来一律按本机。
_CROSS_DEVICE_RE = re.compile(
    r'另一台|另一台电脑|别的电脑|其他电脑|跨设备|远程(?:电脑|机器|设备)|'
    r'(?:[一二三四五六七八九十0-9]+\s*号|第[一二三四五六七八九十]+台)电脑|'
    r'那台[^，。！？]{0,20}(?:电脑|机器|设备|主机)', re.I)


def _cross_device_intent(text):
    from core.tool_router import detect_cross_device
    return bool(detect_cross_device(text)) or bool(_CROSS_DEVICE_RE.search(str(text or '')))


def _tool_present(device, wanted):
    """目标设备清单里是否装有 wanted（按 name/launch/aliases 模糊匹配，与云端一致）。"""
    value = str(wanted or '').casefold()
    for item in (device.get('tools') or []):
        fields = [item.get('name'), item.get('launch')] + list(item.get('aliases') or [])
        if any(value in str(field).casefold() for field in fields if field):
            return True
    return False


WIN_W, WIN_H = 320, 430          # 最大窗体尺寸(容纳 260px 显示宽的立绘)
BUBBLE_SECS = 8                  # 气泡停留秒数


class CompWindow(QWidget):
    """参赛版: 极简桌宠窗体(管家中枢)."""

    adapter_done = pyqtSignal(dict)     # 适配器结果回主线程（错误/中止支持, 不跨线程碰 UI）
    device_inventory_ready = pyqtSignal(list, list, str)  # 设备清单/所需软件/错误(跨设备派单)

    def __init__(self):
        super().__init__()
        # --- 无边框/透明/置顶/工具窗(不占任务栏) ---
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        # --- 表现层: 占位立绘(素材到位后整体替换) ---
        # 2026-11-06 治"太大": 显示宽按主屏高度自适应(1080p≈216, 小屏 150 兜底)
        self._anim = SpriteAnimator(str(_res_dir()), display_width=adaptive_display_width())
        # 窗体随形象尺寸走(底部输入框/气泡/默认落点全部引用实例属性)
        self._win_w = max(220, self._anim.width + 60)
        self._win_h = max(320, self._anim.height + 110)
        self.setFixedSize(self._win_w, self._win_h)
        self._anim_offset = QPoint(10, 60)
        self._pet_scale = 1.0

        # 模型状态要有可见入口：不只藏在右键菜单，桌宠右上角常驻两个小按钮
        # ——「模型」（缺 Key 时红色写明未配置）和「接入」（随时打开连接器面板）。
        from PyQt5.QtWidgets import QPushButton
        self._bubble_persistent = False
        self._model_btn = QPushButton('模型', self)
        self._model_btn.setCursor(Qt.PointingHandCursor)
        self._model_btn.clicked.connect(self.open_model_settings)
        self._connector_btn = QPushButton('接入', self)
        self._connector_btn.setCursor(Qt.PointingHandCursor)
        self._connector_btn.setToolTip('连接器 / 接入状态：查看 WorkBuddy、豆包、JoyCode 是否真的接上咕嘎，'
                                       '也可以新增软件。MCP 只负责发现路径，不等于模型可用。')
        self._connector_btn.setStyleSheet(
            'QPushButton{color:#f6f7fb;background:rgba(46,52,68,160);border:1px solid rgba(255,255,255,90);'
            'border-radius:9px;padding:2px 7px;font-size:11px;}'
            'QPushButton:hover{background:rgba(70,120,220,210);}')
        self._connector_btn.clicked.connect(self.open_connectors)
        self._refresh_model_state()

        # --- 大脑/语音 ---
        self._ai = AIChatEngine(self)
        self._ai.reply_ready.connect(self._on_reply)
        self._ai.failed.connect(self._on_fail)
        self._tts = TTSEngine()
        # --- 点名软件的适配器直连（结果从工作线程回主线程再动界面） ---
        self.adapter_done.connect(self._on_adapter_done)
        self._adapter_busy = False
        # --- 跨设备派单（设备清单从后台线程回主线程再弹选择框） ---
        self.device_inventory_ready.connect(self._on_device_inventory)
        self._cross_pending = None
        self._device_dialog = None

        # --- 管家系统(召唤/检索链/提醒/报幕/菜单注入) ---
        self._butler = ButlerRuntime(self)

        # --- 台账面板(先建, 云端信号回调要写状态行) ---
        self._panel = TaskPanel()
        self._panel.hide()
        # 离线台账: 先显示上次落盘的云端快照, 免得没网/没登录时面板一片空白
        self._tasks_snapshot = []
        self._load_tasks_cache()

        # --- 屏幕级接管标识: 只在派单/操控/异常时出现, 空闲不占屏 ---
        self._badge = ControlBadge()

        # --- 控制会话标识条(贴在宠物侧面) + 全局中止热键(主 Q / 备用 Z) ---
        self._control_bar = ControlSessionBar(self)
        self._control_hotkey = ControlSessionHotkey(self)
        self._control_bar.set_hotkey(self._control_hotkey)
        self._control_hotkey.triggered.connect(self._on_control_hotkey)
        self._last_bar_follow = 0.0

        # --- 云端大脑(监工闭环: 拆解/派单/台账/验收; 未配置则静默跳过) ---
        self._cloud = ButlerCloud(self)
        self._cloud.login_ok.connect(self._on_cloud_login)
        self._cloud.login_fail.connect(
            lambda e: (self.show_bubble('少爷，云端大脑连不上：%s' % e[:60]),
                       self._panel.set_status('云端未登录 · 右键「☁ 云端账号（可选）…」检查地址/账号/密码'),
                       self._badge.set_state('error', '云端未登录 · ' + e[:60])))
        self._cloud.dispatch_ok.connect(self._on_task_created)
        self._cloud.dispatch_fail.connect(
            lambda e: (self.show_bubble('派单没成：%s' % e[:80]),
                       self._panel.set_status('派单失败 · 详情见台账'),
                       self._badge.set_state('error', '派单失败 · ' + e[:60])))
        self._cloud.poll_error.connect(
            lambda e: (self._panel.set_status('云端断线 · 自动重试中'),
                       self._badge.set_state('error', '云端断线 · 自动重试中')))
        self._cloud.state_updated.connect(self._on_state)
        self._cloud.announce.connect(
            lambda line: (self.show_bubble(line), self._tts.speak(line)))
        self._cloud.start()

        # --- 内嵌本机执行方（合一第二步: 桌宠自己领单干活, 无需独立值守程序） ---
        self._agent = None

        # --- 气泡与重绘 ---
        # 2026-11-06 治"形象不动": 参赛版只开了重绘定时器, 从未启动精灵动画器的
        # 帧推进定时器(_anim.start()), 导致 _clip 永远停在第 0 帧 —— 每 33ms
        # 重画同一张静态图 = 看起来"不动/卡死". 动画器自己按源帧率推进并回调
        # 重绘, 不再额外挂一个重复的 33ms 定时器.
        self._bubble_text = ''
        self._bubble_until = 0.0
        self._anim.on_update = self.update
        self._anim.start()

        # --- 拖动 ---
        self._drag_off = None

        # --- 待机: 管家占位循环(素材到位后自动被新素材覆盖) ---
        QTimer.singleShot(300, self._start_idle)
        self.move_to_default()

    # ==================== 素材/启动 ====================

    def _start_idle(self):
        # 2026-11-07: butler_idle 是 24fps 无缝循环素材, 作为常态待机底座.
        # doze/heart 仍保留为一次性开场动画的兜底.
        try:
            if 'idle:idle' in self._anim.clips:
                self._anim.play_clip('idle:idle', loop=True)
                logger.info('comp idle started: looping idle:idle')
                return
        except Exception:
            logger.exception('idle:idle failed, fallback to doze')
        # 2026-11-06 治"不动": doze 是 one-shot 审查单, 旧代码 loop=True 实际
        # 被闸门否定——播 5s 就永久静止. 改为一次性开场(播完由 idle 动画链
        # 自动接续随机下一条), 不再伪装循环.
        try:
            if 'idle:doze' in self._anim.clips:
                self._anim.play_clip('idle:doze')
                logger.info('comp idle started: one-shot idle:doze, chain takes over')
                return
        except Exception:
            logger.exception('idle:doze failed, fallback to armor doze')
        try:
            res = self._butler.mm._resolver
            if res and res.load_into(self._anim, 'doze', namespace='idle',
                                     clip_key='idle:comp_butler'):
                self._anim.play_clip('idle:comp_butler', loop=True)
                logger.info('comp idle started: armor doze fallback')
        except Exception:
            logger.exception('idle start failed')

    def open_agent_window(self):
        """v1 完整对话/复合任务窗口（复用原引擎，形象与气泡仍由参赛版窗口承载）."""
        if not self._require_model('完整对话'):
            return
        try:
            if getattr(self, '_agent_window', None) is None:
                from ui.comp_pet_proxy import CompPetProxy
                from ui.agent_chat import AgentChatWindow
                self._pet_proxy = CompPetProxy(self)
                self._agent_window = AgentChatWindow(self._pet_proxy)
            self._agent_window.show()
            self._agent_window.raise_()
            self._agent_window.activateWindow()
        except Exception as e:
            logger.exception('open agent window failed')
            self.show_bubble('完整对话窗打不开：%s' % str(e)[:80])

    def screenshot_analyze(self):
        """v1 截屏助教入口：框选屏幕 → 附件进完整对话窗 → 直接提问分析."""
        if not self._require_model('截图分析'):
            return
        try:
            self.open_agent_window()
            if getattr(self, '_screen_tools', None) is None:
                from ui.screen_tools import ScreenTools
                self._screen_tools = ScreenTools(self._pet_proxy, register=False)
            self._screen_tools.begin('region_ask')
        except Exception as e:
            logger.exception('screenshot tool failed')
            self.show_bubble('截图工具起不来：%s' % str(e)[:80])

    def open_bg_agent(self):
        """v1 战棋决策实验室（原面板自身独立，直接复用）."""
        try:
            from ui.bg_agent_panel import BattlegroundAgentPanel
            if getattr(self, '_bg_agent_panel', None) is None:
                self._bg_agent_panel = BattlegroundAgentPanel(self)
                self._bg_agent_panel.speak_requested.connect(self.show_bubble)
            self._bg_agent_panel.show()
            self._bg_agent_panel.raise_()
            self._bg_agent_panel.activateWindow()
        except Exception as e:
            logger.exception('open bg agent failed')
            self.show_bubble('战棋决策实验室打不开：%s' % str(e)[:80])

    def open_bg_live(self):
        try:
            from ui.bg_live_panel import BattlegroundLivePanel
            if getattr(self, '_bg_live_panel', None) is None:
                self._bg_live_panel = BattlegroundLivePanel(self)
            self._bg_live_panel.show()
            self._bg_live_panel.raise_()
            self._bg_live_panel.activateWindow()
        except Exception as e:
            logger.exception('open bg live failed')
            self.show_bubble('战棋实战面板打不开：%s' % str(e)[:80])

    def review_bg_logs(self):
        """v1 日志分析/HDT 胜率记录（自包含，直接复用原解析器）."""
        from pathlib import Path
        from PyQt5.QtWidgets import QDialog, QVBoxLayout, QTextBrowser, QLabel
        from core.bg_combat_review import parse_hdt_records, render_review
        folder = Path.home() / 'Desktop/游戏/HDT/对战记录'
        files = list(folder.glob('*.txt')) if folder.exists() else []
        dialog = QDialog(self)
        dialog.setWindowTitle('咕嘎 · 日志分析 / HDT 胜率记录')
        dialog.resize(980, 650)
        layout = QVBoxLayout(dialog)
        view = QTextBrowser()
        layout.addWidget(view)
        if files:
            source = max(files, key=lambda p: p.stat().st_mtime)
            if source.stat().st_size <= 16 * 1024 * 1024:
                report = parse_hdt_records(source.read_text(encoding='utf-8-sig'))
                from datetime import datetime
                layout.addWidget(QLabel('来源：' + source.name + '；最后写入：' +
                                        datetime.fromtimestamp(source.stat().st_mtime).strftime('%m-%d %H:%M:%S') +
                                        '。记录可能滞后，未自动对齐当前战斗。'))
                view.setMarkdown(render_review(report))
            else:
                view.setPlainText('记录超过本次读取预算，请选择较小的按日记录。')
        else:
            view.setPlainText('尚未发现本机 HDT 对战记录；不会用假想对手胜率代替插件数据。')
        self._bg_review_dialog = dialog
        dialog.show()

    def move_to_default(self):
        screen = QApplication.primaryScreen()
        if screen:
            g = screen.availableGeometry()
            self.move(g.right() - self._win_w - 40, g.bottom() - self._win_h - 20)

    # ==================== ButlerRuntime 需要的接口 ====================

    def _ensure_pet_menu(self):
        menu = getattr(self, '_comp_menu', None)
        if menu is not None:
            return menu
        menu = QMenu(None)
        self._comp_menu = menu
        menu.addAction('💬 对话(点击形象也可)').triggered.connect(self.open_input)
        menu.addSeparator()
        # v1 完整智能体: 复合任务(多步骤/DSH/跨软件工具链)走这套原引擎,
        # 参赛版不再只有"简单文本任务".
        menu.addAction('🧠 完整对话 · 复杂任务').triggered.connect(self.open_agent_window)
        menu.addAction('⚙️ 模型设置（换模型/端点/Key，测连接）').triggered.connect(self.open_model_settings)
        # 云端账号入口常驻：不依赖 _cloud.enabled，首次配置/清空都从这里进。
        menu.addAction('☁ 云端账号（可选）…').triggered.connect(self.open_cloud_account)
        menu.addAction('🩺 导出诊断（发给我们）').triggered.connect(self.open_diagnostic_export)
        menu.addAction('🔌 连接器 / 接入状态（MCP）').triggered.connect(self.open_connectors)
        menu.addAction('📷 截图分析').triggered.connect(self.screenshot_analyze)
        menu.addAction('🎤 语音输入（点一下开始/再点结束）').triggered.connect(self.voice_input)
        bg = menu.addMenu('🎮 战棋（v1 移植）')
        bg.addAction('决策实验室').triggered.connect(self.open_bg_agent)
        bg.addAction('日志分析 / 胜率记录').triggered.connect(self.review_bg_logs)
        bg.addAction('实战操作面板').triggered.connect(self.open_bg_live)
        fun = menu.addMenu('🐧 逗一逗')
        fun.addAction('困倦打盹').triggered.connect(
            lambda: self._anim.play_clip('idle:doze', loop=False) if 'idle:doze' in self._anim.clips else None)
        fun.addAction('双手比心').triggered.connect(
            lambda: self._anim.play_clip('idle:heart', loop=False) if 'idle:heart' in self._anim.clips else None)
        fun.addAction('轻轻摆动').triggered.connect(lambda: self._anim.trigger_action('dance'))
        menu.addSeparator()
        if self._cloud.enabled:
            menu.addAction('🗣️ 派个任务…').triggered.connect(self.open_dispatch)
            menu.addAction('📋 任务台账').triggered.connect(self._panel.toggle)
        menu.addAction('❌ 退出').triggered.connect(QApplication.instance().quit)
        return menu

    def open_model_settings(self):
        """右键菜单/右上角按钮入口：换模型/端点/Key、测试连接，保存后立刻生效。"""
        from ui.model_settings import open_model_settings
        return open_model_settings(self, on_saved=self._apply_model_config)

    def open_cloud_account(self):
        """常驻入口：云端账号可选，填完立即重新登录；不填不影响本地任务。"""
        from ui.first_run import CloudAccountDialog
        return CloudAccountDialog(self, on_saved=self._on_cloud_account_saved).exec_()

    def _on_cloud_account_saved(self, cloud):
        """保存/清除后重载 ButlerCloud，并重建右键菜单（派单/台账项按新状态出现）。"""
        try:
            self._cloud.reload()
        except Exception:
            logger.exception('cloud reload failed')
        menu = getattr(self, '_comp_menu', None)
        if menu is not None:
            try:
                menu.deleteLater()
            except Exception:
                pass
            self._comp_menu = None
        if cloud:
            self.show_bubble('云端账号已保存，正在重新登录；派单/跨设备入口随后出现。')
        else:
            self.show_bubble('云端账号已清除；本地聊天、打开软件、截图分析不受影响。')

    def open_diagnostic_export(self):
        """右键菜单入口：后台收集诊断并打包，完成后提示 zip 路径。"""
        from ui.model_settings import run_diagnostic_export
        return run_diagnostic_export(self)

    def open_connectors(self):
        """常驻入口：查看/新增软件接入，随时可再打开（不再只靠一次性提醒）。"""
        try:
            from ui.mcp_onboarding_dialog import show_mcp_onboarding
            show_mcp_onboarding(parent=self)
        except Exception as exc:
            logger.exception('open connectors failed')
            self.show_bubble('接入面板打不开：%s' % str(exc)[:80])

    def _refresh_model_state(self):
        """右上角常驻显示当前服务商/端点；缺配置就红色写明不可用功能。"""
        cfg = guga_paths.read_config() or {}
        problem = model_config_error(cfg)
        if problem:
            self._model_btn.setText('未配置模型')
            self._model_btn.setStyleSheet(
                'QPushButton{color:#fff;background:rgba(198,40,40,220);border:1px solid rgba(255,255,255,120);'
                'border-radius:9px;padding:2px 7px;font-size:11px;font-weight:600;}'
                'QPushButton:hover{background:rgba(220,60,60,240);}')
            self._model_btn.setToolTip(problem + '\n点击打开模型设置。')
        else:
            provider = provider_label(cfg)
            self._model_btn.setText('模型：%s' % provider)
            self._model_btn.setStyleSheet(
                'QPushButton{color:#f6f7fb;background:rgba(46,140,90,190);border:1px solid rgba(255,255,255,90);'
                'border-radius:9px;padding:2px 7px;font-size:11px;}'
                'QPushButton:hover{background:rgba(60,170,110,220);}')
            self._model_btn.setToolTip('当前服务商：%s\n端点：%s\n模型：%s\n点击修改或测试连接。'
                                       % (provider, endpoint_label(cfg), cfg.get('model') or '未填写'))
            if self._bubble_persistent:
                self._bubble_persistent = False
                self._bubble_until = 0
        for btn in (self._model_btn, self._connector_btn):
            btn.adjustSize()
        self._model_btn.move(max(6, self._win_w - self._model_btn.width() - 8), 6)
        self._connector_btn.move(max(6, self._win_w - self._connector_btn.width() - 8),
                                 self._model_btn.geometry().bottom() + 4)
        self._model_btn.raise_()
        self._connector_btn.raise_()

    def _require_model(self, feature):
        """完整对话/截图分析前明确拦一下，别让用户点了以后只看到半英文报错。"""
        problem = model_config_error(guga_paths.read_config())
        if not problem:
            return True
        from PyQt5.QtWidgets import QMessageBox
        self._refresh_model_state()
        self.show_notice('%s不可用：%s' % (feature, problem))
        box = QMessageBox(self)
        box.setWindowTitle('未配置模型')
        box.setIcon(QMessageBox.Warning)
        box.setText('%s暂时不可用。' % feature)
        box.setInformativeText(problem + '\n\n接入 MCP 只让咕嘎知道软件装在哪，不等于模型可用。')
        go = box.addButton('去配置模型', QMessageBox.AcceptRole)
        box.addButton('先用本地功能', QMessageBox.RejectRole)
        box.exec_()
        if box.clickedButton() is go:
            self.open_model_settings()
        return False

    def _apply_model_config(self, cfg):
        window = getattr(self, '_agent_window', None)
        if window is not None and getattr(window, 'agent', None) is not None:
            window.agent.config.clear()
            window.agent.config.update(cfg)
        if getattr(self, '_ai', None) is not None:
            self._ai._cfg = dict(cfg or {})   # 让桌宠自己的对话立即用上新配置
        self._refresh_model_state()

    def _say_with_expression(self, text, expression='normal', action=None):
        self.show_bubble(text)

    # ==================== 交互 ====================

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_off = e.globalPos() - self.frameGeometry().topLeft()
        elif e.button() == Qt.RightButton:
            self._ensure_pet_menu().exec_(e.globalPos())

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.open_input()

    def mouseMoveEvent(self, e):
        if self._drag_off is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPos() - self._drag_off)
            now = time.monotonic()
            bar = getattr(self, '_control_bar', None)
            if bar is not None and bar.isVisible() and now - self._last_bar_follow > 0.1:
                self._last_bar_follow = now
                bar.follow()

    def mouseReleaseEvent(self, e):
        self._drag_off = None

    # ==================== 对话 ====================

    def open_input(self):
        if getattr(self, '_input', None) is not None:
            self._input.raise_()
            self._input.activateWindow()
            return
        box = QLineEdit(self)
        box.setPlaceholderText('少爷请吩咐…(Enter 发送, Esc 关闭)')
        box.setStyleSheet("""
            QLineEdit { background: rgba(255,255,255,235); color: #202124;
                        border: 1px solid #b8bcc7; border-radius: 10px;
                        padding: 6px 12px; font-size: 13px;
                        font-family: "Microsoft YaHei UI"; }""")
        box.setFixedSize(self._win_w - 40, 34)
        box.move(20, self._win_h - 44)
        box.returnPressed.connect(lambda: self._send(box))
        box.installEventFilter(self)
        box.show()
        box.raise_()
        box.activateWindow()
        box.setFocus()
        self._input = box
        self.show_bubble('少爷，请讲。')

    def eventFilter(self, obj, event):
        from PyQt5.QtCore import QEvent
        if obj is getattr(self, '_input', None) and event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Escape:
                self._close_input()
                return True
        return super().eventFilter(obj, event)

    def _close_input(self):
        box = getattr(self, '_input', None)
        if box is not None:
            box.deleteLater()
            self._input = None

    def _send(self, box):
        text = box.text().strip()
        if not text:
            return
        self._close_input()
        self._handle_text(text)

    def _handle_text(self, text):
        """文本统一入口(输入条/语音转写都走这里), 保持派单与对话同一套路由."""
        text = str(text or '').strip()
        if not text:
            return
        # 叫停/继续优先于一切路由：命中即执行，不确认、不交给模型
        from core import input_guard
        command = input_guard.parse_control_command(text)
        if command == 'pause':
            if (self._agent is not None and self._agent.active) or input_guard.is_paused():
                input_guard.pause('用户叫停')
                self.show_bubble('已暂停，控制权还给你。要恢复就说「继续」。')
            else:
                self.show_bubble('现在没有正在进行的控制，不需要暂停。')
            self._tts.speak('已暂停，控制权还给你。')
            return
        if command == 'resume':
            if input_guard.is_paused():
                state = input_guard.resume()
                line = state.get('message') or ('已恢复控制。' if state.get('ok') else '还没能恢复控制。')
                self.show_bubble(line)
                self._tts.speak(line)
            else:
                self.show_bubble('当前没有暂停，控制正常。')
            return
        # 简单“打开软件”走本地匹配：不派任务、不调模型、没有 Key 也能用。
        from core import fast_commands
        fast = fast_commands.match(text)
        if fast.get('matched') and fast.get('action') == 'open_app' and fast.get('kind') == 'app':
            import threading
            def work():
                answer = fast_commands.execute(fast)
                def show():
                    line = answer.get('message') or answer.get('error') or '已处理'
                    self.show_bubble(line)
                    self._tts.speak(line)
                QTimer.singleShot(0, show)
            threading.Thread(target=work, name='guga-open-app', daemon=True).start()
            return
        # 跨设备: 仅当用户明确说“另一台/二号电脑/那台装了 XX 的机器”才走；
        # 识别不出来或普通任务一律按本机，绝不默认跨设备。
        if _cross_device_intent(text):
            self._start_cross_device(text)
            return
        # 意图路由: 话里出现目标软件(豆包/Kimi/记事本…) = 要咕嘎指挥那个软件真机干活,
        # 转派单链路——绝不能让咕嘎自己的 API 代写冒充(那等于演剧本).
        # 点名工具时先查本机有没有装, 没装就界面里说清缺什么/去哪装, 不允许直答代写.
        from core import tool_mandate
        # 复合任务（A 软件产出 → B 软件消费）必须拆成适配器串联，按序真机执行；
        # 否则第二个软件不会被当作目标，最后落进通用自主执行去抓前台窗口。
        from core import adapter_chain
        chain = adapter_chain.detect(text)
        if chain:
            self._run_adapter_chain(chain)
            return
        mandate = tool_mandate.detect(text)
        if mandate:
            state = tool_mandate.availability(mandate)
            if not state.get('installed'):
                line = tool_mandate.install_message(mandate)
                self.show_bubble(line)
                self._tts.speak(line)
                return
            # 声明里带适配器的软件：直接交给适配器真机执行（MCP 优先、UI 自动化兜底），
            # 不经云端也不经模型——这条路径不存在“模型自己写成品”的可能。
            try:
                from core.adapters.dispatch import adapter_for
                app_id, adapter = adapter_for(mandate.get('name'))
            except Exception:
                logger.exception('adapter lookup failed')
                self._reject_no_adapter(mandate, '')
                return
            if adapter is not None:
                self._run_adapter_task(mandate, text, adapter, app_id)
                return
            # 点名了、本机也装了，但没有可用适配通道：明确拒绝并给失败码。
            # 绝不再降级成云端拆解派单——那会让“点名工具”变成“模型/云端代写”。
            if app_id:
                self._reject_no_adapter(mandate, app_id)
                return
            if self._cloud.enabled:
                self._dispatch_from_chat(text)
                return
            self._start_local_task(text, mandate)
            return
        if self._cloud.enabled and looks_like_device_task(text):
            self._dispatch_from_chat(text)
            return
        # 云端没配时, 需要动真机的任务也不能退化成"咕嘎自己写"——交给本机执行方.
        from core.execution_contract import requires_execution
        if requires_execution(text):
            if self._cloud.enabled:
                self._dispatch_from_chat(text)
                return
            self._start_local_task(text)
            return
        self.show_bubble('……')
        self._ai.ask(text)

    # ==================== 点名软件：适配器直连 ====================

    def _reject_no_adapter(self, mandate, app_id):
        """本机已装但没有任何可用通道：如实拒绝，绝不改走云端拆解/模型代写。"""
        from core import tool_mandate
        app = str(mandate.get('name') or app_id or '该工具')
        try:
            from core.adapters import dispatch
            detail = dispatch.load_error(app_id)
        except Exception:
            detail = ''
        reason = 'no_adapter：本机已装%s，但没有可用的本机通道' % app
        line = tool_mandate.failure_message(
            {'name': app}, reason,
            next_step='告诉我换工具，或等本机通道修好后再派；这次不会拆成云端任务。')
        self.show_bubble(line)
        self._tts.speak('没能调起%s，不会拿模型代写冒充。' % app)
        self._badge.set_state('error', '本机没有 %s 通道 · no_adapter' % app)
        logger.warning('mandate %s 已安装但没有可用适配器：%s', app_id, detail or '无 adapter 入口')

    def _run_adapter_task(self, mandate, text, adapter, app_id):
        """把点名任务交给目标软件的适配器真机执行；全程不给模型代写的入口。"""
        app = str(mandate.get('name') or app_id)
        try:
            from agent.guga_agent import _adapter_prompt
            prompt = _adapter_prompt({'goal': text, 'name': text, 'acceptance': ''}, app)
        except Exception:
            prompt = text
        if self._adapter_busy:
            # 上一个还在跑：如果就是同一 goal + 同一工程，明确说“不重复派单”，
            # 与适配器侧的幂等拦截保持同一口径，不让用户以为只是“忙”。
            dup = ''
            try:
                dup = adapter.pending_duplicate(prompt, log_block=True) or ''
            except Exception:
                dup = ''
            if dup:
                line = '同一目标已经在执行（%s），这次不重复派单。' % dup
            else:
                line = '上一个真机任务还在跑，跑完再接新的。'
            self.show_bubble(line)
            self._tts.speak(line)
            return
        self._adapter_busy = True
        line = '交给%s了——正在它窗口里真机执行，不是我自己代写。' % app
        self.show_bubble(line)
        self._tts.speak(line)
        self._badge.set_state('run', '咕嘎正在操控电脑 · %s' % app)

        def work():
            try:
                res = adapter.ask(prompt, timeout_seconds=180, retry=1, prefer_mcp=True)
            except TypeError:
                res = adapter.ask(prompt)
                res = res if isinstance(res, dict) else {
                    'ok': False, 'code': 'exception',
                    'raw': 'TypeError: 适配器返回 %s，不是 dict'
                           % type(res).__name__}
            except Exception as exc:
                res = {'ok': False, 'code': 'exception',
                       'raw': '%s: %s' % (type(exc).__name__, str(exc)[:160]),
                       'hint': '适配器异常：%s' % str(exc)[:120]}
            res = dict(res or {})
            res['_app'] = app
            # 工作区识别失败时要在主线程弹选择框，这里把重派所需上下文带回主线程。
            res['_adapter'] = adapter
            res['_mandate'] = mandate
            res['_text'] = text
            res['_app_id'] = app_id
            self.adapter_done.emit(res)

        threading.Thread(target=work, daemon=True).start()

    def _run_adapter_chain(self, chain):
        """A 软件产出 → 咕嘎校验 → B 软件消费：按序真机跑，任一步失败就停在那一步。"""
        steps = list(chain.get('steps') or [])
        label = ' → '.join(str(item.get('name') or '') for item in steps)
        if self._adapter_busy:
            line = '上一个真机任务还在跑，跑完再接新的。'
            self.show_bubble(line)
            self._tts.speak(line)
            return
        self._adapter_busy = True
        self._badge.set_state('busy', '串联：%s' % label)
        self.show_bubble('这是 %d 步串联：%s，我按顺序真机跑。' % (len(steps), label))

        def work():
            from core import adapter_chain
            try:
                outcome = adapter_chain.run(chain)
            except Exception as exc:
                logger.exception('adapter chain failed')
                outcome = dict(ok=False, steps=[], code='chain_exception',
                               error='串联没跑起来：%s' % str(exc)[:160])
            outcome = dict(outcome or {})
            outcome['_chain'] = True
            outcome['label'] = label
            self.adapter_done.emit(outcome)

        threading.Thread(target=work, daemon=True).start()

    def _on_chain_done(self, res):
        label = str(res.get('label') or '串联')
        steps = res.get('steps') or []
        summary = str(res.get('summary') or '')
        if res.get('ok'):
            text = str(res.get('text') or '').strip()
            path = self._save_product(text, label.replace(' → ', '-'))
            line = '串联完成：%s；%s' % (label, summary)
            self.show_bubble(line)
            self._tts.speak('串联完成了：%s' % summary)
            self._show_product_dialog(label, text, '%s（各步真机产出）' % label, summary, path)
            return
        error = str(res.get('error') or '').strip() or '串联任务失败'
        self.show_bubble(error)
        self._tts.speak(error)
        if steps:
            failed = [item for item in steps if item.get('status') != 'done']
            if failed:
                logger.info('adapter chain stopped at step %s (%s): %s',
                            failed[-1].get('index'), failed[-1].get('name'), error)

    def _adapter_receipt_ok(self, res):
        """交付闸：必须有真实工具调用回执（来源 + 证据），只凭 ok/正文不算.

        适配器若误报 ok=True 但没来源/证据，或明确标明用了本机模型，
        一律不进交付态——宁可报失败，也不能拿模型代写冒充真机产物。
        """
        if not res.get('ok') or not str(res.get('text') or '').strip():
            return False
        evidence = res.get('evidence') or {}
        source = res.get('source') or {}
        if isinstance(evidence, dict) and evidence.get('local_model_used') is True:
            return False
        if isinstance(source, dict) and str(source.get('channel') or '') == 'local_model':
            return False
        receipt = ''
        if isinstance(source, dict):
            receipt = source.get('provenance') or source.get('call_id') or ''
        if not receipt and isinstance(evidence, dict):
            receipt = evidence.get('dir') or evidence.get('receipt_id') or ''
        receipt = receipt or res.get('receipt') or res.get('call_id') or ''
        return bool(receipt)

    def _on_adapter_done(self, res):
        self._adapter_busy = False
        self._badge.set_state('idle', '')
        if res.get('_chain'):
            self._on_chain_done(res)
            return
        app = res.get('_app') or '豆包'
        code = str(res.get('code') or '')
        if code == 'workspace_not_found':
            self._pick_workspace_and_retry(res)
            return
        if code == 'duplicate_task':
            line = str(res.get('failure') or res.get('hint') or res.get('message')
                       or '同一目标已有未完成的任务，这次不重复派单。')
            self.show_bubble(line)
            self._tts.speak(line)
            return
        if self._adapter_receipt_ok(res):
            text = str(res['text']).strip()
            source = res.get('source') or {}
            label = source.get('label') or ('%s（来源标签缺失）' % app)
            notice = str(res.get('notice') or '').strip()
            path = self._save_product(text, app)
            line = '「%s」写好 %d 字 · 来源：%s' % (app, len(text), label)
            self.show_bubble(line)
            self._tts.speak('%s 写好了，%d 字，来源%s' % (app, len(text), label))
            self._show_product_dialog(app, text, label, notice, path)
            return
        code = str(res.get('code') or '')
        if not code:
            code = ('no_receipt' if res.get('ok') and str(res.get('text') or '').strip()
                    else 'failed')
        hint = str(res.get('hint') or res.get('message') or '')
        if not hint:
            hint = ('没有任何真实工具调用回执（来源/证据缺失），本次不交付'
                    if code == 'no_receipt' else '没有返回正文')
        from core.failure_report import format_failure, stage_for
        failure = str(res.get('failure') or '').strip()
        if not failure:
            failure = format_failure(
                code, res.get('stage') or stage_for(code),
                res.get('raw') or res.get('message') or hint,
                res.get('next_action') or hint, app=app)
        line = failure
        self.show_bubble(line)
        self._tts.speak('%s 没完成（%s）：%s' % (app, code,
                         str(res.get('next_action') or hint).split('。')[0][:60]))

    def _pick_workspace_and_retry(self, res):
        """找不到 JoyCode 当前工程：让用户选一次再原单重派，绝不静默写默认目录。"""
        from PyQt5.QtWidgets import QFileDialog
        app = res.get('_app') or 'JoyCode'
        candidates = res.get('candidates') or []
        default = ''
        if candidates:
            first = candidates[0]
            default = first.get('path') if isinstance(first, dict) else str(first)
        if not default:
            default = str(res.get('default') or '')
        self.show_bubble('没找到 JoyCode 当前工程，请选择。')
        chosen = QFileDialog.getExistingDirectory(
            self, '请选择 JoyCode 当前工程（任务写到该目录的 .guga/inbox）',
            default or '', QFileDialog.ShowDirsOnly)
        if not chosen:
            line = '没找到 JoyCode 当前工程，请选择；这次没有派单，也没有写错位置。'
            self.show_bubble(line)
            self._tts.speak(line)
            return
        adapter = res.get('_adapter')
        if adapter is None or not hasattr(adapter, 'use_workspace'):
            self.show_bubble('选好了工程，但当前通道不支持指定工作区；这次没有派单。')
            return
        try:
            adapter.use_workspace(chosen)
        except Exception as exc:
            self.show_bubble('无法使用这个目录：%s' % str(exc)[:80])
            return
        self.show_bubble('已选中 %s，重新派单。' % chosen)
        self._run_adapter_task(res.get('_mandate') or {'name': app},
                               res.get('_text') or '', adapter, res.get('_app_id') or '')

    def _save_product(self, text, app):
        try:
            from core import guga_paths
            guga_paths.ensure()
            name = 'guga-%s-%s.txt' % (app, time.strftime('%Y%m%d-%H%M%S'))
            path = guga_paths.DELIVERY / name
            path.write_text(text, encoding='utf-8')
            return str(path)
        except Exception:
            logger.exception('save adapter product failed')
            return ''

    def _show_product_dialog(self, app, text, label, notice, path):
        from PyQt5.QtWidgets import QDialog, QLabel, QTextBrowser, QVBoxLayout
        dlg = QDialog(self)
        dlg.setWindowTitle('咕嘎 · %s 的产物（真机执行）' % app)
        dlg.resize(760, 560)
        lay = QVBoxLayout(dlg)
        head = QLabel('来源：%s%s' % (label, ('\n已保存：%s' % path) if path else ''))
        head.setWordWrap(True)
        lay.addWidget(head)
        if notice:
            note = QLabel(notice)
            note.setWordWrap(True)
            lay.addWidget(note)
        view = QTextBrowser()
        view.setPlainText(text)
        lay.addWidget(view)
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        self._product_dialog = dlg
        dlg.show()
        dlg.raise_()

    # ==================== 语音输入 ====================

    def voice_input(self):
        """按键语音: 点一下开始录音, 再点一次结束并离线识别."""
        if getattr(self, '_voice', None) is not None and self._voice.active:
            self._voice.stop()
            self.show_bubble('正在识别…')
            return
        try:
            from core import audio_status
            state = audio_status.probe()
        except Exception:
            logger.exception('audio probe failed')
            state = {'verdict': 'unknown'}
        if state.get('verdict') != 'ready':
            self.show_bubble(audio_status.message(state))
            return
        try:
            if getattr(self, '_voice', None) is None:
                from core.short_voice import ShortVoice
                self._voice = ShortVoice(self)
                self._voice.state.connect(self._on_voice_state)
                self._voice.transcript.connect(self._on_voice_text)
                self._voice.failed.connect(self._on_voice_fail)
            self._voice.start()
        except Exception as exc:
            logger.exception('voice start failed')
            self.show_bubble('语音起不来：%s' % str(exc)[:80])

    def _on_voice_state(self, state):
        text = {'preparing': '正在准备离线识别…',
                'recording': '🔴 录音中…说完再点一次',
                'transcribing': '正在识别…'}.get(state, '')
        if text:
            self.show_bubble(text)

    def _on_voice_text(self, text):
        self.show_bubble('听到：' + text[:60])
        self._handle_text(text)

    def _on_voice_fail(self, message):
        self.show_bubble('语音没成：%s' % str(message)[:80])

    def _start_local_task(self, text, mandate=None):
        """云端大脑没配时，需要动真机的任务也不能让咕嘎代写：交给本机执行方。

        完整对话窗里挂着工具桥（applications/computer/…），由模型真实调用并向
        用户展示每一步；点名工具的任务在 AgentChatWindow.send 里还会再过一道
        “必须有工具回执才允许交付”的闸门。
        """
        try:
            self.open_agent_window()
            window = getattr(self, '_agent_window', None)
            if window is None:
                raise RuntimeError('对话窗没有创建成功')
            self.show_bubble('这单要动真机，交给本机执行方——过程你看得到。')
            window.send(text, force_agent=True)
        except Exception as exc:
            line = ('本机执行方没能起来：%s。下一步：点桌宠右键「完整对话 · 复杂任务」'
                    '手动打开后重试。' % str(exc)[:100])
            self.show_bubble(line)
            self._tts.speak(line)

    def _dispatch_from_chat(self, text):
        """对话里的任务请求 → 云端拆解派单(优先本机执行方)."""
        if _cross_device_intent(text):
            self._start_cross_device(text)
            return
        if not self._cloud.logged_in:
            self._panel.set_status('云端未登录 · 本单未派出（右键「☁ 云端账号（可选）…」）')
            self.show_bubble('少爷，这单要真机执行，但云端大脑还没连上——点桌宠右键'
                             '「☁ 云端账号（可选）…」，填服务地址、账号、密码，'
                             '点「测试登录」通过后再派。')
            try:
                self._tts.speak('少爷，云端大脑没连上，这单派不出去。')
            except Exception:
                pass
            return
        if self._agent is not None and not self._agent.active:
            self.show_bubble('本机执行方还没就绪（配对中或失败）。这单我会照常派出，'
                             '若云端拒绝会告诉你原因。')
        self.show_bubble('少爷，这是要动真机的活——我来拆解派单，过程您在本机亲眼看到。')
        try:
            self._tts.speak('收到，这是真机任务，我来派单并监工。')
        except Exception:
            pass
        self._cloud.dispatch(text)

    # ==================== 跨设备派单 ====================

    def _start_cross_device(self, text):
        """跨设备：拉设备清单 → 用户亲手选一台 → 带 device + requiredTools 派单。

        契约坑（cloud/API-CONTRACT.md）：device 只认 agent.id 或设备名，
        绝不传 device_id；requiredTools 只在同时传 device 时生效。
        """
        if not self._cloud.enabled or not self._cloud.logged_in:
            line = ('这是跨设备任务，但云端账号没连上——跨设备必须走云端，'
                    '不会换成本机，也不会由我自己代写。')
            self.show_bubble(line)
            try:
                self._tts.speak(line)
            except Exception:
                pass
            self._panel.set_status('跨设备未派 · 云端未登录（右键「☁ 云端账号（可选）…」）')
            return
        required = self._cross_required_tools(text)
        self._cross_pending = (str(text), required)
        self.show_bubble('这是跨设备任务，我先拉账号下的设备清单——您选一台，我不自动乱选。')
        self._panel.set_status('跨设备 · 正在拉设备清单')

        def work():
            try:
                result = self._cloud.devices_inventory()
            except Exception as exc:
                logger.exception('devices inventory failed')
                self.device_inventory_ready.emit([], required, str(exc)[:120])
                return
            self.device_inventory_ready.emit(list(result.get('devices') or []),
                                             required, '')

        threading.Thread(target=work, daemon=True).start()

    def _cross_required_tools(self, text):
        """本次跨设备必须装的软件：显式点名的 + 能力推导，去重最多 10 个。"""
        names = []
        value = str(text or '').casefold()
        try:
            from core.adapters import app_registry
            for decl in app_registry.declarations().values():
                labels = [decl.get('name')] + list(decl.get('aliases') or [])
                for label in labels:
                    label = str(label or '').strip()
                    if len(label) >= 2 and label.casefold() in value:
                        names.append(str(decl.get('name') or label))
                        break
        except Exception:
            logger.exception('cross required tools scan failed')
        try:
            from core.tool_router import required_tools
            names.extend(required_tools(text))
        except Exception:
            logger.exception('cross required tools derive failed')
        result = []
        for name in names:
            if name and name not in result:
                result.append(name)
        return result[:10]

    def _on_device_inventory(self, devices, required, error):
        if error:
            line = ('设备清单拉不到：%s。下一步：确认云端账号在线后重试；'
                    '这类任务不会降级成本机代写。' % error)
            self.show_bubble(line)
            try:
                self._tts.speak(line)
            except Exception:
                pass
            self._panel.set_status('跨设备未派 · 设备清单拉取失败')
            return
        devices = list(devices or [])
        if not devices:
            line = ('账号下没找到可用设备（在线或刚上报过清单的都算）。'
                    '下一步：确认另一台电脑上的咕嘎本体已启动并配对；'
                    '找不到就本机做，或先去那台机器启动咕嘎。')
            self.show_bubble(line)
            try:
                self._tts.speak(line)
            except Exception:
                pass
            self._panel.set_status('跨设备未派 · 没有可用设备')
            return
        self._show_device_choice(devices, list(required or []))

    def _show_device_choice(self, devices, required):
        """设备选择框：设备名 / 在线 / 已装工具 / 是否满足 requiredTools，用户自选。"""
        from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                                     QTableWidget, QTableWidgetItem, QPushButton,
                                     QAbstractItemView, QHeaderView)
        text, required = self._cross_pending or ('', required)
        dlg = QDialog(self)
        dlg.setWindowTitle('跨设备派单 · 选择执行设备')
        dlg.resize(760, 420)
        lay = QVBoxLayout(dlg)
        need = '、'.join(required) or '未指定（不校验软件）'
        head = QLabel('本次所需：%s。只选一台在线设备，我不会自动替您选。' % need)
        head.setWordWrap(True)
        lay.addWidget(head)
        table = QTableWidget(len(devices), 4)
        table.setHorizontalHeaderLabels(['设备', '在线', '已装工具', '本次所需'])
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        for row, device in enumerate(devices):
            online = bool(device.get('online'))
            name = str(device.get('device') or device.get('id') or '未命名设备')
            label = name + ('（话里提到）' if name and name in text else '')
            name_item = QTableWidgetItem(label)
            name_item.setData(Qt.UserRole, device)
            table.setItem(row, 0, name_item)
            status_item = QTableWidgetItem('在线' if online else '离线')
            table.setItem(row, 1, status_item)
            tools = device.get('tools') or []
            if not device.get('hasInventory'):
                tools_text = '未上报清单'
            else:
                tools_text = '、'.join(str(t.get('name') or t.get('launch') or '')
                                      for t in tools[:6]) or '无'
                total = int(device.get('toolsCount') or len(tools))
                if total > 6:
                    tools_text += ' 等 %d 个' % total
            table.setItem(row, 2, QTableWidgetItem(tools_text))
            missing = [tool for tool in required if not _tool_present(device, tool)]
            if not device.get('hasInventory'):
                fit = '未知（没清单）'
            elif missing:
                fit = '缺：' + '、'.join(missing)
            elif required:
                fit = '满足'
            else:
                fit = '不校验'
            table.setItem(row, 3, QTableWidgetItem(fit))
            if not online:
                for column in range(4):
                    item = table.item(row, column)
                    item.setFlags(item.flags() & ~Qt.ItemIsSelectable)
        table.resizeRowsToContents()
        lay.addWidget(table)
        hint = QLabel('离线设备不可选；「缺：X」表示该设备清单里没有本次所需软件。')
        hint.setWordWrap(True)
        lay.addWidget(hint)
        buttons = QHBoxLayout()
        ok = QPushButton('派到这台')
        ok.setEnabled(False)
        cancel = QPushButton('取消')
        buttons.addStretch(1)
        buttons.addWidget(ok)
        buttons.addWidget(cancel)
        lay.addLayout(buttons)

        def chosen():
            items = table.selectedItems()
            if not items:
                return None
            return table.item(items[0].row(), 0).data(Qt.UserRole)

        def sync():
            device = chosen()
            ok.setEnabled(bool(device and device.get('online')))

        table.itemSelectionChanged.connect(sync)
        table.cellDoubleClicked.connect(lambda row, column: accept())
        ok.clicked.connect(lambda: accept())
        cancel.clicked.connect(dlg.close)

        def accept():
            device = chosen()
            if not device or not device.get('online'):
                return
            dlg.close()
            self._dispatch_to_device(text, device, required)

        dlg.device_table = table
        dlg.device_count_label = head
        self._device_dialog = dlg
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _dispatch_to_device(self, text, device, required):
        name = str(device.get('device') or device.get('id') or '目标设备')
        # 契约：只认 agent.id 或设备名；device_id 只用于清单归账，传了会 DEVICE_OFFLINE。
        target = str(device.get('id') or device.get('device') or '')
        tools = [str(t) for t in (required or []) if str(t).strip()][:10]
        missing = [tool for tool in tools if not _tool_present(device, tool)]
        if missing:
            line = ('「%s」清单里缺 %s——按您选的这台仍派一次，'
                    '云端会校验；若拒绝我会告诉你哪台有。' % (name, '、'.join(missing)))
        else:
            line = ('派给「%s」了%s——等它领单，过程看台账。'
                    % (name, ('（所需：%s）' % '、'.join(tools)) if tools else ''))
        self.show_bubble(line)
        try:
            self._tts.speak(line)
        except Exception:
            pass
        self._panel.set_status('跨设备派单中 · %s' % name)
        self._badge.set_state('dispatch', '跨设备派单 · %s' % name)
        self._cloud.dispatch(text, device=target, required_tools=tools)

    def _on_reply(self, content, expression, action):
        self.show_bubble(content)

    def _on_fail(self, error):
        """缺模型时给常驻、可照做的提示；其他失败保持原气泡。"""
        problem = model_config_error(guga_paths.read_config())
        if problem:
            logger.warning('model unavailable: %s', problem)
            self._refresh_model_state()
            self.show_notice(problem + '\n点右上角红色「未配置模型」去填；接入 MCP 不等于模型可用。')
            return
        self.show_bubble('( connection: %s )' % str(error or '')[:80])

    # ==================== 云端监工（合一的管家一面） ====================

    def _on_cloud_login(self, username):
        self._panel.set_status('已连接 · 账号 %s' % username)
        self.show_bubble('少爷，云端大脑连上了（%s）。说个目标，我去派活监工。' % username)
        self._play_task_anim('hello')
        self.refresh_state()
        self._start_local_agent()

    def _start_local_agent(self):
        """登录成功后自动把本机注册为执行方（合一: 桌宠既是主人端又是执行端）."""
        if self._agent is not None:
            return
        try:
            self._agent = LocalAgent(self._cloud, self)
            self._agent.paired.connect(self._on_agent_paired)
            self._agent.pair_fail.connect(
                lambda e: self.show_bubble('本机执行方没配成：%s（不影响派单给其他设备）' % e[:60]))
            self._agent.job_started.connect(self._on_agent_job)
            self._agent.job_paused.connect(self._on_agent_paused)
            self._agent.job_finished.connect(self._on_agent_done)
            self._agent.job_skipped.connect(self._on_agent_skipped)
            self._agent.announce.connect(self._on_agent_announce)
            self._agent.start()
        except Exception:
            logger.exception('local agent start failed')

    def _play_task_anim(self, name):
        """事件动画直触（accept/reject/celebrating/error/hello…），clip 不在则静默。"""
        clip = 'task:' + name
        if clip in self._anim.clips:
            self._anim.play_clip(clip, loop=False)

    def _on_agent_paired(self, device):
        self.show_bubble('本机执行方已就位（%s）。派给本机的活，我自己就能干。' % device)
        try:
            self._tts.speak('少爷，本机执行方就位，派给我的活我自己干。')
        except Exception:
            pass

    def _on_agent_job(self, job):
        app = str(job.get('app') or '豆包')
        # 幂等键与台账一致：同一子任务已到终态时，begin 会拒绝把界面冲回进行中
        self._job_key = '%s|%s|%s' % (job.get('taskId') or '', job.get('stId') or '',
                                      job.get('attempt') or 1)
        begun = control_session.session.begin(
            app=app, step=str(job.get('name') or '执行子任务'),
            task=str(job.get('taskId') or ''), key=self._job_key)
        if begun is None:
            self.show_bubble('这份子任务已到终态（已完成/已终止），重复派单未再执行。')
            self._panel.set_status('重复派单已终止 · 本次未再执行')
            self._badge.set_state('done', '已终止 · 不重复执行')
            logger.info('duplicate job refused by control_session: %s', self._job_key)
            return
        self.show_bubble('领到单了：「%s」——开始指挥本机%s干活。'
                         % (job.get('name', '子任务'), app))
        self._badge.set_state(
            'control',
            '「%s」· %s' % (job.get('name', '子任务'), app))
        self._anim.set_work_state('working', str(job.get('taskId') or 'main'))
        try:
            self._tts.speak('接到子任务 %s，我开始指挥本机%s。'
                            % (job.get('name', ''), app))
        except Exception:
            pass

    def _on_control_hotkey(self, source, accepted):
        if not accepted:
            self.show_bubble('现在没有咕嘎正在进行的电脑控制。')
        else:
            self.show_bubble('已中止电脑控制，咕嘎不会再动鼠标。')

    def _on_agent_paused(self, note):
        """叫停后的终态：保留暂停态供「继续」，不按失败收尾、不伪造完成。"""
        self.show_bubble(note or '已暂停，控制权还给你。')
        self._badge.set_state('control', '已暂停 · 等你说「继续」')
        self._play_task_anim('pause' if 'pause' in self._anim.clips else 'error')
        self._anim.set_work_state('idle')
        try:
            self._tts.speak('已暂停，控制权还给你。要恢复就说继续。')
        except Exception:
            pass

    def _on_agent_done(self, job, ok, note):
        control_session.session.finish(bool(ok), str(note or '')[:80],
                                       key=getattr(self, '_job_key', ''))
        self._mark_subtask(job, 'done' if ok else 'error')   # 与云端口径一致: error=失败
        self.show_bubble(note or ('执行结束' if ok else '执行失败'))
        self._badge.set_state('done' if ok else 'error',
                             (note or '执行结束')[:70])
        # 即时事件动画: 成功庆祝 / 出错挠头, 然后回空闲（不等 TaskAnimationDirector 的 1.2s 起步延迟）
        self._play_task_anim('celebrating' if ok else 'error')
        self._anim.set_work_state('idle')
        if not ok:
            try:
                self._tts.speak('少爷，这一单没干成，原因我已如实上报。')
            except Exception:
                pass

    # ---------- 本地台账快照（离线也更新、也可看） ----------

    def _tasks_cache_path(self):
        return guga_paths.RUNTIME / 'cloud-tasks.json'

    def _load_tasks_cache(self):
        try:
            path = self._tasks_cache_path()
            if not path.exists():
                return
            tasks = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(tasks, list) and tasks:
                self._tasks_snapshot = tasks
                self._panel.set_tasks(tasks)
                self._panel.set_status('离线台账快照 · 云端未连接时显示最近进度')
        except Exception:
            logger.exception('load cloud task cache failed')

    def _save_tasks_cache(self, tasks):
        try:
            path = self._tasks_cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix('.tmp')
            tmp.write_text(json.dumps(tasks, ensure_ascii=False, indent=1), encoding='utf-8')
            tmp.replace(path)
            return True
        except Exception as exc:
            logger.warning('task cache save failed: %s', exc)
            self._panel.set_status('本地台账写入失败：%s' % str(exc)[:60])
            return False

    def _mark_subtask(self, job, status):
        """本机执行完立刻把这条子任务写进本地台账（不等云端下一拍轮询）。"""
        try:
            tasks = list(getattr(self, '_tasks_snapshot', []) or [])
            tid = str(job.get('taskId') or '')
            sid = str(job.get('stId') or '')
            changed = False
            for t in tasks:
                if str(t.get('id') or '') != tid:
                    continue
                for s in t.get('subtasks') or []:
                    if str(s.get('id') or '') == sid:
                        s['status'] = status
                        changed = True
            if changed:
                self._tasks_snapshot = tasks
                self._panel.set_tasks(tasks)
                self._save_tasks_cache(tasks)
        except Exception:
            logger.exception('mark subtask failed')

    def _on_agent_skipped(self, note):
        """重复派单被本机台账拦下：界面必须明确显示「已终止」，不能悄悄再跑一遍。"""
        text = '已终止：%s' % (note or '重复派单未再执行')
        self.show_bubble(text)
        self._panel.set_status('重复派单已终止 · 未再执行')
        self._badge.set_state('done', '已终止 · ' + str(note or '')[:50])
        try:
            self._tts.speak('这份子任务已经做过，我不重复执行。')
        except Exception:
            pass

    def _on_agent_announce(self, text):
        """执行方要说的真话（比如本地台账写不进去）直接冒出来，不静默。"""
        if text:
            self.show_bubble(text)
            self._panel.set_status(str(text)[:80])

    def open_dispatch(self):
        """菜单"派个任务": 输入目标 → 云端拆解派单 → 台账可见."""
        if not self._cloud.logged_in:
            self.show_bubble('少爷，先在 ai_config.json 里配好 cloud 账号，我才能连云端大脑。')
            return
        from PyQt5.QtWidgets import QInputDialog
        goal, ok = QInputDialog.getText(
            self, '派个任务', '告诉咕嘎目标（如: 把本周实验数据整理成周报）:')
        if not ok or not goal.strip():
            return
        goal = goal.strip()
        if _cross_device_intent(goal):
            self._start_cross_device(goal)
            return
        self.show_bubble('少爷，接单了。我拆拆看……')
        self._cloud.dispatch(goal)

    def _on_task_created(self, task):
        subs = task.get('subtasks') or []
        # 派单成功这一刻就把任务并进台账并立刻渲染：只改顶部计数会出现
        # “顶部 2 个子任务、列表还没有任务”的空白期（下一次 /api/state 轮询前）。
        try:
            tid = str(task.get('id') or '')
            tasks = [t for t in (self._tasks_snapshot or [])
                     if str(t.get('id') or '') != tid]
            tasks.insert(0, task)
            self._tasks_snapshot = tasks
            self._panel.set_tasks(tasks)
            self._save_tasks_cache(tasks)
        except Exception:
            logger.exception('optimistic task insert failed')
        self._panel.set_status('派单中 · %d 个子任务，进度看台账' % len(subs))
        self._badge.set_state('dispatch',
                             '已拆成 %d 个子任务 · 等待执行方领单' % len(subs),
                             '子任务 0/%d' % len(subs))
        names = '、'.join(s.get('name', '') for s in subs[:3])
        self.show_bubble('少爷，拆解完了：%d 个子任务（%s%s）——已派单，进度在台账里盯。'
                         % (len(subs), names, '…' if len(subs) > 3 else ''))
        self._play_task_anim('accept')
        self._anim.set_work_state('waiting', str(task.get('id') or 'main'))
        try:
            self._tts.speak('少爷，任务已拆成 %d 个子任务并派单，我来监工。' % len(subs))
        except Exception:
            pass

    def _on_state(self, tasks):
        """台账刷新: 面板与状态行一起更新, 任何情况下都不留过期文案."""
        self._tasks_snapshot = list(tasks or [])
        self._panel.set_tasks(tasks)
        if self._tasks_snapshot:      # 空列表不清本地副本, 免得断网时把离线台账抹掉
            self._save_tasks_cache(self._tasks_snapshot)   # 本地副本: 离线/断网也能看到进度
        # 异常检测: 新出现 error/failed/中止 的子任务 → 播对应动画与气泡（差分，每个子任务只报一次）
        try:
            bad_now = {}
            for t in tasks or []:
                for s in t.get('subtasks') or []:
                    if is_aborted(s):
                        state = 'aborted'
                    elif is_failed(s):
                        state = 'error'
                    else:
                        continue
                    bad_now[str(s.get('id') or s.get('name'))] = (state, s.get('name') or '子任务')
            seen = getattr(self, '_seen_bad', None)
            if seen is None:
                self._seen_bad = bad_now      # 首刷只建基线, 历史异常不播动画
            else:
                fresh = [k for k in bad_now if k not in seen]
                if fresh and self._cloud.logged_in:
                    state, name = bad_now[fresh[0]]
                    self._play_task_anim('error' if state == 'aborted' else 'reject')
                    self.show_bubble('「%s」%s' % (
                        name, '已中止（用户终止，可重新派单）' if state == 'aborted'
                        else '执行失败，详情见台账'))
                self._seen_bad = bad_now
        except Exception:
            pass
        if tasks and self._badge.isVisible():
            subs = tasks[0].get('subtasks') or []
            if subs:
                done = sum(1 for s in subs if s.get('status') == 'done')
                self._badge.set_progress('子任务 %d/%d' % (done, len(subs)))
        if self._cloud.logged_in:
            self._panel.set_status('已连接 · 账号 %s · %d 条任务'
                                   % (self._cloud.user or '?', len(tasks)))
        else:
            self._panel.set_status('未连接云端大脑 · 检查 cloud 配置')

    def refresh_state(self):
        if self._cloud.logged_in:
            self._cloud.refresh_state()

    # ==================== 气泡/绘制 ====================

    def show_bubble(self, text):
        self._bubble_text = str(text)
        self._bubble_persistent = False
        self._bubble_until = time.time() + BUBBLE_SECS

    def show_notice(self, text):
        """常驻提示：不自动消失，直到配置补齐或被下一条普通气泡替换。"""
        self._bubble_text = str(text)
        self._bubble_persistent = True
        self._bubble_until = time.time() + 24 * 3600

    def paintEvent(self, e):
        # 2026-11-06: 绘制虚函数里漏出的 Python 异常会被 PyQt 直接 abort 进程
        # (桌宠表现为"突然消失"). 单帧画坏了就空一帧, 下一帧继续, 不拖走整个进程.
        try:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing, True)
            self._anim.draw(p, self._anim_offset.x(), self._anim_offset.y())
            # 气泡(管家/员工说话)
            if self._bubble_text and time.time() < self._bubble_until:
                self._draw_bubble(p)
            elif self._bubble_text:
                self._bubble_text = ''
        except Exception:
            logger.exception('paint failed')

    def _draw_bubble(self, p):
        text = self._bubble_text
        font = QFont('Microsoft YaHei UI', 10)
        p.setFont(font)
        fm = p.fontMetrics()
        max_w = self._win_w - 60
        # 简易换行
        lines, line = [], ''
        for ch in text:
            if fm.horizontalAdvance(line + ch) > max_w or ch == '\n':
                lines.append(line)
                line = '' if ch == '\n' else ch
            else:
                line += ch
        if line:
            lines.append(line)
        # 行数上限按窗口可用高度自适应（365px 高 ≈ 14 行）：拒绝代写这类长文案
        # 若被截成 7 行，最关键的承诺句会消失。文案侧已把承诺提到最前，
        # 这里再放开到窗口实际能容纳的行数，宁可占满气泡也不砍承诺（P2-NOAI-TRUNC）。
        max_lines = max(7, min(15, (self._win_h - 30) // max(1, fm.height())))
        if len(lines) > max_lines:
            lines = lines[:max_lines - 1] + ['…']
        if not lines:
            return
        bw = max(fm.horizontalAdvance(t) for t in lines) + 28
        bh = fm.height() * len(lines) + 20
        bx, by = (self._win_w - bw) // 2, 6
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 242))
        p.drawRoundedRect(bx, by, bw, bh, 12, 12)
        p.setPen(QColor('#202124'))
        ty = by + 10 + fm.ascent()
        for t in lines:
            p.drawText(bx + 14, ty, t)
            ty += fm.height()


def _res_dir():
    from pathlib import Path
    return Path(__file__).resolve().parent.parent / 'resources'
