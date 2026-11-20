"""Local-only credential setup and explicit realtime call controls."""
import json
import os
import math
import time
from pathlib import Path
from PyQt5.QtCore import Qt, QTimer, QRectF
from PyQt5.QtGui import QPainter, QColor, QPen, QPixmap, QFont
from PyQt5.QtWidgets import (QDialog, QFormLayout, QVBoxLayout, QHBoxLayout,
    QLineEdit, QComboBox, QCheckBox, QSpinBox, QPushButton, QLabel,
    QDialogButtonBox, QMessageBox, QWidget, QFrame, QScrollArea)
from core.realtime_voice import (
    CONFIG_PATH, DEFAULTS, SEED_MODEL, SEED_PROVIDER, SEED_URL, SEED_VOICE,
    connection_url, load_settings, with_environment_credentials,
)


QWEN_MODELS = ('qwen-audio-3.1-realtime-plus', 'qwen3.8-omni-flash-realtime',
               'qwen3.5-omni-plus-realtime', 'qwen3.5-omni-flash-realtime')


class VoiceSettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('咕嘎 · 实时语音配置')
        self.setMinimumWidth(560)
        try:
            with CONFIG_PATH.open(encoding='utf-8-sig') as f:
                self._config = json.load(f)
        except FileNotFoundError:
            self._config = {}
        cfg = dict(DEFAULTS, **self._config.get('realtime_voice', {}))
        self._active_provider = cfg['provider']
        self._profiles = dict(self._config.get('realtime_voice_profiles', {}))
        self._profiles[self._active_provider] = cfg
        layout = QVBoxLayout(self)
        note = QLabel('各平台使用独立的 API Key 和模型设置。Key 只保存在本机。')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.link = QLabel()
        self.link.setOpenExternalLinks(True)
        layout.addWidget(self.link)
        form = QFormLayout()
        layout.addLayout(form)
        self.provider = QComboBox()
        self.provider.addItem('千问 AI 平台（platform.qianwenai.com）', 'qianwen')
        self.provider.addItem('阿里云百炼', 'bailian')
        self.provider.addItem('火山引擎 · 豆包 Seed Realtime', SEED_PROVIDER)
        self.provider.addItem('自定义 · 兼容千问实时协议', 'custom')
        self.provider.setCurrentIndex(max(0, self.provider.findData(cfg.get('provider', 'qianwen'))))
        form.addRow('API 平台', self.provider)
        # The two platforms sell the same model names but are separate gateways
        # with separate accounts. Show the gateway that will actually be dialled so
        # "right model + right key" can never hide a wrong platform choice.
        self.endpoint = QLabel()
        self.endpoint.setWordWrap(True)
        self.endpoint.setStyleSheet('color:#5a6b7a;')
        form.addRow('实际连接', self.endpoint)
        self.url_label = QLabel('WebSocket 地址')
        self.url = QLineEdit(cfg.get('ws_url', ''))
        self.url.setPlaceholderText('wss://服务商域名/实时语音路径')
        form.addRow(self.url_label, self.url)
        self.key = QLineEdit(cfg.get('api_key', ''))
        self.key.setEchoMode(QLineEdit.Password)
        self.key.setPlaceholderText('所选平台的 API Key；也可用 DASHSCOPE_API_KEY 环境变量')
        form.addRow('API Key', self.key)
        self.workspace = QLineEdit(cfg.get('workspace_id', ''))
        self.workspace.setPlaceholderText('该Key所属的业务空间ID')
        self.workspace_label = QLabel('业务空间 ID')
        form.addRow(self.workspace_label, self.workspace)
        self.region = QComboBox()
        self.region.addItem('北京（中国内地）', 'cn-beijing')
        self.region.addItem('新加坡', 'ap-southeast-1')
        self.region.setCurrentIndex(max(0, self.region.findData(cfg.get('region'))))
        self.region_label = QLabel('地域')
        form.addRow(self.region_label, self.region)
        self.model = QComboBox()
        self.model.addItems(QWEN_MODELS)
        self.model.setCurrentText(cfg['model'])
        form.addRow('实时语音模型', self.model)
        self.voice = QComboBox()
        self.voice.setEditable(True)
        self.voice.addItems(['longanqian_v3.1', 'Tina', 'Cici', 'Momo', 'Serena', 'Ethan'])
        self.voice.setCurrentText(cfg['voice'])
        form.addRow('音色', self.voice)
        self.free_guard = QCheckBox('已确认当前平台、当前模型有“免费额度用尽即停”保护')
        self.free_guard.setChecked(cfg.get('free_quota_confirmed_model') == cfg['model'] and
                                   cfg.get('free_quota_confirmed_provider') == self.provider.currentData())
        form.addRow('只用赠送额度', self.free_guard)
        self.model.currentTextChanged.connect(lambda _: self.free_guard.setChecked(False))
        self.model.currentTextChanged.connect(self._model_changed)
        self.key.textChanged.connect(lambda _: self.free_guard.setChecked(False))
        self.quota_note = QLabel()
        self.quota_note.setWordWrap(True)
        layout.addWidget(self.quota_note)
        self.headphones = QCheckBox('使用耳机：允许边听边说和语音打断')
        self.headphones.setChecked(cfg['headphones'])
        form.addRow('收音模式', self.headphones)
        tip = QLabel('外放时，咕嘎说话期间暂停收音以避免回声；可用“打断”按钮。\n耳机模式没有软件回声消除，建议佩戴耳机。')
        tip.setWordWrap(True)
        layout.addWidget(tip)
        self.owner_only = QCheckBox('启用声纹门禁（可选，默认关闭）')
        self.owner_only.setChecked(cfg.get('owner_only', False))
        form.addRow('主人门禁', self.owner_only)
        owner_tip = QLabel('默认无需录入声纹，麦克风直接用于实时语音。只有手动启用门禁时才需录入；声纹不匹配会拦截该句话。')
        owner_tip.setWordWrap(True)
        layout.addWidget(owner_tip)
        self.input_device = QComboBox()
        self.output_device = QComboBox()
        for combo in (self.input_device, self.output_device):
            combo.addItem('系统默认设备', None)
        try:
            import sounddevice as sd
            for i, dev in enumerate(sd.query_devices()):
                if dev['max_input_channels']:
                    self.input_device.addItem(f"{i}: {dev['name']}", i)
                if dev['max_output_channels']:
                    self.output_device.addItem(f"{i}: {dev['name']}", i)
        except Exception:
            pass
        for key, combo in [('input_device', self.input_device), ('output_device', self.output_device)]:
            combo.setCurrentIndex(max(0, combo.findData(cfg.get(key))))
            form.addRow('麦克风' if key == 'input_device' else '扬声器', combo)
        self.silence = QSpinBox()
        self.turn_mode=QComboBox()
        self.turn_mode.addItem('即时回应（停顿最多600毫秒）','server_vad')
        self.turn_mode.addItem('长句交流（语义判断结束）','smart_turn_v2')
        self.turn_mode.setCurrentIndex(max(0,self.turn_mode.findData(cfg.get('turn_mode','server_vad'))))
        form.addRow('响应方式',self.turn_mode)
        self.silence.setRange(200, 6000)
        self.silence.setSuffix(' 毫秒')
        self.silence.setValue(cfg['silence_duration_ms'])
        form.addRow('停顿等待（长表达可调大）', self.silence)
        self.duration = QSpinBox()
        self.duration.setRange(0, 480)
        self.duration.setSpecialValueText('不限时（推荐）')
        self.duration.setValue(max(0, cfg['max_call_minutes']))
        self.duration.setSuffix(' 分钟')
        form.addRow('本次通话时限', self.duration)
        self.idle = QSpinBox()
        self.idle.setRange(5, 30)
        self.idle.setValue(max(5, min(30, cfg['idle_minutes'] or 30)))
        self.idle.setSuffix(' 分钟')
        form.addRow('无交谈自动结束（5—30分钟）', self.idle)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.provider.currentIndexChanged.connect(self._provider_changed)
        self.workspace.textChanged.connect(lambda _text: self._update_provider_fields())
        self.url.textChanged.connect(lambda _text: self.free_guard.setChecked(False))
        self._load_provider(cfg)

    def _provider_changed(self):
        self._profiles[self._active_provider] = self._provider_fields(self._active_provider)
        self._active_provider = self.provider.currentData()
        self._load_provider(self._profiles.get(self._active_provider, {}))

    def _model_changed(self, model):
        if self.provider.currentData() in ('qianwen', 'bailian'):
            self.voice.setCurrentText('longanqian_v3.1' if model.startswith('qwen-audio-') else 'Tina')

    def _load_provider(self, cfg):
        provider = self.provider.currentData()
        seed = provider == SEED_PROVIDER
        custom = provider == 'custom'
        self.model.clear()
        self.model.setEditable(custom)
        if not custom:
            self.model.addItems([SEED_MODEL] if seed else QWEN_MODELS)
        self.model.setCurrentText(cfg.get('model', SEED_MODEL if seed else '' if custom else QWEN_MODELS[0]))
        self.key.setText(cfg.get('api_key', ''))
        self.workspace.setText(cfg.get('workspace_id', ''))
        self.url.setText(cfg.get('ws_url', ''))
        self.region.setCurrentIndex(max(0, self.region.findData(cfg.get('region', 'cn-beijing'))))
        self.voice.clear()
        if seed:
            self.voice.addItem(SEED_VOICE)
        elif not custom:
            self.voice.addItems(['longanqian_v3.1', 'Tina', 'Cici', 'Momo', 'Serena', 'Ethan'])
        self.voice.setCurrentText(cfg.get('voice', SEED_VOICE if seed else 'Tina' if custom else self.voice.currentText()))
        self.free_guard.setChecked(bool(cfg.get('free_quota_confirmed_model') == self.model.currentText()
            and cfg.get('free_quota_confirmed_provider') == provider))
        self._update_provider_fields()

    def _provider_fields(self, provider):
        model = self.model.currentText().strip()
        return dict(provider=provider, api_key=self.key.text().strip(), model=model,
            workspace_id=self.workspace.text().strip() if provider == 'bailian' else '',
            ws_url=self.url.text().strip() if provider == 'custom' else '',
            region=self.region.currentData(), voice=self.voice.currentText().strip(),
            free_quota_confirmed_model=model if self.free_guard.isChecked() else '',
            free_quota_confirmed_provider=provider if self.free_guard.isChecked() else '')

    def _update_provider_fields(self):
        provider = self.provider.currentData()
        bailian = provider == 'bailian'
        self.url_label.setVisible(provider == 'custom')
        self.url.setVisible(provider == 'custom')
        self.endpoint.setVisible(provider != 'custom')
        self.link.setVisible(provider != 'custom')
        self.key.setPlaceholderText('填写当前平台签发的 API Key')
        self.quota_note.setText('确认所选模型启用免费额度用尽即停后，勾选上方设置。')
        for field in (self.workspace, self.workspace_label, self.region, self.region_label):
            field.setVisible(bailian)
        if bailian:
            self.link.setText('<a href="https://bailian.console.aliyun.com/">百炼控制台</a>')
            workspace = self.workspace.text().strip()
            region = self.region.currentData() or 'cn-beijing'
            host = (workspace + '.' + str(region) + '.maas.aliyuncs.com') if workspace else '需先填写业务空间 ID'
            self.endpoint.setText('wss://' + host + '  ·  阿里云百炼（用百炼的 Key，额度和账号在百炼控制台）')
        elif provider == SEED_PROVIDER:
            self.link.setText('<a href="https://docs.volcengine.com/docs/doubao-voice/seedrealtime?lang=zh">豆包语音接入文档</a>')
            self.endpoint.setText(SEED_URL)
            self.key.setPlaceholderText('豆包语音控制台 → API Key 管理')
            self.quota_note.setText('请在豆包语音控制台确认实时音视频通话仍处于试用状态、有剩余额度，且没有启用后付费。')
        elif provider == 'qianwen':
            self.link.setText('<a href="https://platform.qianwenai.com/">千问 AI 平台</a> · '
                              '<a href="https://platform.qianwenai.com/docs/resources/free-quota">免费额度说明</a>')
            self.endpoint.setText('wss://maas.qianwenaiapi.com  ·  千问 AI 平台（只需 Key，不需要业务空间）')

    def _save(self):
        provider = self.provider.currentData()
        fields = self._provider_fields(provider)
        cfg = dict(DEFAULTS, **fields)
        cfg.update(headphones=self.headphones.isChecked(),
                   silence_duration_ms=self.silence.value(),turn_mode=self.turn_mode.currentData(), max_call_minutes=self.duration.value(),
                   idle_minutes=self.idle.value(), input_device=self.input_device.currentData(),
                   output_device=self.output_device.currentData(), free_only=True,
                   owner_only=self.owner_only.isChecked(),
                   owner_threshold=self._config.get('realtime_voice', {}).get('owner_threshold'))
        try:
            connection_url(with_environment_credentials(cfg))
            # Re-read immediately before save to preserve unrelated current configuration.
            try:
                with CONFIG_PATH.open(encoding='utf-8-sig') as f:
                    current = json.load(f)
            except FileNotFoundError:
                current = {}
            previous = current.get('realtime_voice', {})
            profiles = current.setdefault('realtime_voice_profiles', {})
            if previous.get('provider') != provider and previous.get('provider'):
                profiles[previous['provider']] = previous
            profiles[provider] = fields
            current['realtime_voice'] = cfg
            tmp = CONFIG_PATH.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding='utf-8')
            os.replace(tmp, CONFIG_PATH)
        except Exception as exc:
            QMessageBox.warning(self, '配置未保存', str(exc))
            return
        self.accept()


class VoiceVisual(QWidget):
    """Small, audio-reactive penguin visual using existing desktop pet artwork."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(176, 176)
        self.level = 0.0
        self.phase = 0.0
        self.mode = 'connecting'
        self.pet = QPixmap(str(Path(__file__).resolve().parents[1] / 'resources/expressions/normal_transparent.png'))
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

    def _tick(self):
        self.phase += 0.13
        self.level *= 0.86
        self.update()

    def set_level(self, level):
        self.level = max(self.level, level)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        color = QColor('#7baad2' if self.mode != 'speaking' else '#8e82cb')
        if self.mode in ('muted', 'ended'):
            color = QColor('#a8b6c3')
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor('#eaf3fa'))
        painter.drawEllipse(QRectF(19, 15, 138, 138))
        for i in range(3):
            radius = 67 + i * 8 + self.level * 6
            ring = QColor(color)
            ring.setAlpha(115 - i * 30)
            painter.setPen(QPen(ring, 1.2))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QRectF(88-radius, 83-radius, radius*2, radius*2))
        if not self.pet.isNull():
            pix = self.pet.scaled(110, 115, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            bob = math.sin(self.phase) * 2 if self.mode not in ('ended', 'muted') else 0
            painter.drawPixmap(int(88-pix.width()/2), int(82-pix.height()/2+bob), pix)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        for i in range(15):
            strength = self.level
            if self.mode == 'speaking':
                strength = max(strength, 0.4 + math.sin(self.phase+i*0.65)*0.18)
            if self.mode in ('muted', 'ended'):
                strength = 0
            height = 3 + strength * 19 * (0.45 + abs(math.sin(i*0.75+self.phase)))
            painter.drawRoundedRect(QRectF(37+i*7, 162-height/2, 3, height), 1.5, 1.5)


class ConversationView(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setObjectName('Conversation')
        self.body = QWidget()
        self.body.setObjectName('ConversationBody')
        self.rows = QVBoxLayout(self.body)
        self.rows.setContentsMargins(16, 12, 16, 12)
        self.rows.setSpacing(12)
        self.rows.setAlignment(Qt.AlignTop)
        self.empty = QLabel('说声「咕嘎，你好」开始聊天\n你的话和咕嘎的回答会出现在这里。')
        self.empty.setObjectName('EmptyConversation')
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setMinimumHeight(145)
        self.rows.addWidget(self.empty)
        self.setWidget(self.body)
        self._items = []

    def append_message(self, who, text):
        self.empty.hide()
        row = QWidget()
        outer = QHBoxLayout(row)
        outer.setContentsMargins(0, 0, 0, 0)
        bubble = QFrame()
        bubble.setObjectName('UserBubble' if who == '饲养员' else 'PetBubble')
        inner = QVBoxLayout(bubble)
        inner.setContentsMargins(14, 10, 14, 12)
        inner.setSpacing(5)
        label = QLabel(who + '  ·  ' + time.strftime('%H:%M'))
        label.setObjectName('BubbleName')
        inner.addWidget(label)
        content = QLabel(text)
        content.setTextFormat(Qt.PlainText)
        content.setWordWrap(True)
        content.setTextInteractionFlags(Qt.TextSelectableByMouse)
        content.setMinimumWidth(180)
        content.setMaximumWidth(620)
        inner.addWidget(content)
        if who == '饲养员':
            outer.addStretch(1)
            outer.addWidget(bubble)
        else:
            outer.addWidget(bubble)
            outer.addStretch(1)
        self.rows.addWidget(row)
        self._items.append((row, who, text))
        if len(self._items) > 100:
            old, _, _ = self._items.pop(0)
            self.rows.removeWidget(old)
            old.deleteLater()
        QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(self.verticalScrollBar().maximum()))

    def appendPlainText(self, text):
        self.append_message('通话提示', text)

    def toPlainText(self):
        return '\n'.join(f'{who}：{text}' for _, who, text in self._items)


class VoiceCallDialog(QDialog):
    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.setWindowTitle('咕嘎 · 持续语音通话')
        self.setObjectName('VoiceCallPanel')
        self.resize(700, 770)
        self.setMinimumSize(600, 630)
        self.setWindowFlags((self.windowFlags() | Qt.WindowStaysOnTopHint) & ~Qt.WindowContextHelpButtonHint)
        self.setFont(QFont('Microsoft YaHei UI', 10))
        self.setStyleSheet('''
            QDialog#VoiceCallPanel { background: #f3f7fa; color: #253b50; }
            QLabel { color: #253b50; background: transparent; }
            QLabel#Eyebrow { color: #7b8e9e; font-size: 10px; letter-spacing: 2px; }
            QLabel#Heading { font-size: 23px; font-weight: 700; }
            QLabel#Subtle, QLabel#BubbleName { color: #7890a4; font-size: 11px; }
            QLabel#Badge { color: #2c846e; background: #e4f3ec; border-radius: 10px; padding: 5px 10px; font-size: 11px; }
            QFrame#Hero { background: white; border: 1px solid #e2eaf1; border-radius: 18px; }
            QLabel#HeroTitle { font-size: 20px; font-weight: 600; }
            QLabel#Status { color: #6688a4; font-size: 12px; }
            QLabel#Hint { color: #8595a5; font-size: 11px; }
            QFrame#Stat { background: white; border: 1px solid #e2eaf1; border-radius: 12px; }
            QLabel#StatValue { font-size: 23px; font-weight: 700; color: #406582; }
            QLabel#StatTitle { color: #7a8c9c; font-size: 11px; }
            QScrollArea#Conversation, QWidget#ConversationBody { background: #f9fbfd; border: none; }
            QFrame#TranscriptCard { background: #f9fbfd; border: 1px solid #e2eaf1; border-radius: 15px; }
            QFrame#UserBubble { background: #e5eff8; border-radius: 12px; }
            QFrame#PetBubble { background: white; border: 1px solid #e4ebf1; border-radius: 12px; }
            QLabel#EmptyConversation { color: #93a5b4; font-size: 13px; }
            QPushButton { border: 1px solid #d8e4ee; border-radius: 12px; background: white; color: #456580; min-height: 42px; padding: 0 17px; font-size: 12px; }
            QPushButton:hover { background: #eaf2f9; border-color: #b9cee0; }
            QPushButton:checked { background: #e5ebf1; color: #768899; }
            QPushButton#EndCall { background: #de7c83; color: white; border: none; }
            QPushButton#EndCall:hover { background: #cd6972; }
            QPushButton#Details { min-height: 20px; border: none; background: transparent; color: #5f87aa; font-size: 11px; padding: 0; }
            QScrollBar:vertical { width: 5px; background: transparent; margin: 4px; }
            QScrollBar::handle:vertical { background: #c6d5e2; border-radius: 2px; min-height: 25px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        ''')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(14)
        top = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.setSpacing(4)
        eyebrow = QLabel('GUGA  /  VOICE COMPANION')
        eyebrow.setObjectName('Eyebrow')
        title_col.addWidget(eyebrow)
        heading = QLabel('和咕嘎聊一会儿')
        heading.setObjectName('Heading')
        title_col.addWidget(heading)
        top.addLayout(title_col)
        top.addStretch()
        self.badge = QLabel('●  仅用免费额度')
        self.badge.setObjectName('Badge')
        top.addWidget(self.badge)
        layout.addLayout(top)

        hero = QFrame()
        hero.setObjectName('Hero')
        hero_row = QHBoxLayout(hero)
        hero_row.setContentsMargins(14, 8, 18, 8)
        self.visual = VoiceVisual()
        hero_row.addWidget(self.visual)
        hero_col = QVBoxLayout()
        hero_col.setSpacing(8)
        self.hero_title = QLabel('正在和咕嘎连线')
        self.hero_title.setObjectName('HeroTitle')
        hero_col.addWidget(self.hero_title)
        self.status = QLabel('准备连接…')
        self.status.setObjectName('Status')
        self.status.setWordWrap(True)
        hero_col.addWidget(self.status)
        try:
            cfg = getattr(engine, '_cfg', {}) or load_settings()
        except (OSError, ValueError):
            cfg = DEFAULTS
        self.mode_hint = QLabel('耳机模式 · 可以直接用语音打断' if cfg.get('headphones') else '外放模式 · 咕嘎说话时暂停收音')
        self.mode_hint.setObjectName('Hint')
        self.mode_hint.setWordWrap(True)
        hero_col.addWidget(self.mode_hint)
        hero_col.addStretch()
        hero_row.addLayout(hero_col, 1)
        layout.addWidget(hero)

        stats = QHBoxLayout()
        stats.setSpacing(10)
        self.elapsed = self._stat_card(stats, '通话时长', '00:00')
        self.last_tokens = self._stat_card(stats, '最近一轮 · Token', '—')
        self.total_tokens = self._stat_card(stats, '本次累计 · Token', '0')
        layout.addLayout(stats)
        meter = QHBoxLayout()
        self.usage_hint = QLabel('等待第一轮回复 · 使用服务端实际计数')
        self.usage_hint.setObjectName('Subtle')
        meter.addWidget(self.usage_hint, 1)
        self.details_button = QPushButton('查看用量明细')
        self.details_button.setObjectName('Details')
        self.details_button.setCheckable(True)
        meter.addWidget(self.details_button)
        layout.addLayout(meter)
        self.usage_details = QLabel('完成一轮对话后显示输入、输出与音频明细。')
        self.usage_details.setObjectName('Subtle')
        self.usage_details.setWordWrap(True)
        self.usage_details.hide()
        self.details_button.toggled.connect(self.usage_details.setVisible)
        layout.addWidget(self.usage_details)

        conversation_card = QFrame()
        conversation_card.setObjectName('TranscriptCard')
        card_layout = QVBoxLayout(conversation_card)
        card_layout.setContentsMargins(0, 12, 0, 4)
        conversation_top = QHBoxLayout()
        conversation_top.setContentsMargins(16, 0, 16, 0)
        conversation_top.addWidget(QLabel('这次的对话'))
        conversation_top.addStretch()
        self.turns = QLabel('0 轮')
        self.turns.setObjectName('Subtle')
        conversation_top.addWidget(self.turns)
        card_layout.addLayout(conversation_top)
        self.transcript = ConversationView()
        card_layout.addWidget(self.transcript, 1)
        layout.addWidget(conversation_card, 1)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.mute = QPushButton('静音麦克风')
        self.mute.setCheckable(True)
        self.mute.toggled.connect(self._mute)
        row.addWidget(self.mute)
        interrupt = QPushButton('打断咕嘎')
        interrupt.clicked.connect(engine.interrupt)
        row.addWidget(interrupt)
        end = QPushButton('结束通话')
        end.setObjectName('EndCall')
        end.clicked.connect(self.close)
        row.addWidget(end)
        layout.addLayout(row)
        footer = QLabel('普通互动仍用原来的 Edge TTS  ·  仅本窗口使用实时语音\n账号剩余额度以平台权益页为准；关闭窗口即停止收音。')
        footer.setObjectName('Subtle')
        footer.setAlignment(Qt.AlignCenter)
        footer.setWordWrap(True)
        layout.addWidget(footer)
        self._started_at = time.monotonic()
        self._clock = QTimer(self)
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self._update_clock)
        self._clock.start()
        engine.status.connect(self._status)
        engine.transcript.connect(self._append)
        engine.audio_level.connect(self.visual.set_level)
        engine.speaking.connect(self._speaking)
        engine.usage_ready.connect(self._usage)
        engine.failed.connect(self._error)
        engine.finished.connect(self._finished)

    def _stat_card(self, row, title, value):
        card = QFrame()
        card.setObjectName('Stat')
        col = QVBoxLayout(card)
        col.setContentsMargins(14, 10, 14, 10)
        col.setSpacing(4)
        caption = QLabel(title)
        caption.setObjectName('StatTitle')
        col.addWidget(caption)
        number = QLabel(value)
        number.setObjectName('StatValue')
        col.addWidget(number)
        row.addWidget(card, 1)
        return number

    def _update_clock(self):
        elapsed = int(time.monotonic() - self._started_at)
        self.elapsed.setText(f'{elapsed//60:02d}:{elapsed%60:02d}')

    def _status(self, message):
        self.status.setText(message)
        if self.mute.isChecked():
            self.hero_title.setText('麦克风已静音')
            self.visual.mode = 'muted'
        elif '正在思考' in message:
            self.hero_title.setText('咕嘎在想怎么回答')
        elif '持续通话中' in message or '正在听' in message:
            if self.engine.is_speaking:
                self._speaking(True)
            else:
                self.hero_title.setText('本鹅在听，你慢慢说')
                self.visual.mode = 'listening'
        elif '连接' in message or '重连' in message:
            self.hero_title.setText('正在和咕嘎连线')
            self.visual.mode = 'connecting'

    def _speaking(self, on):
        self.visual.mode = 'speaking' if on else ('muted' if self.mute.isChecked() else 'listening')
        self.hero_title.setText('咕嘎正在回答你' if on else ('麦克风已静音' if self.mute.isChecked() else '本鹅在听，你慢慢说'))

    def _usage(self, snapshot):
        record = snapshot.get('last_round', {})
        usage = record.get('usage')
        self.turns.setText(str(snapshot.get('rounds', 0)) + ' 轮')
        self.total_tokens.setText(f"{snapshot.get('reported_total_tokens', 0):,}")
        self.last_tokens.setText(f"{usage['total_tokens']:,}" if usage else '—')
        if usage:
            self.usage_hint.setText(f"第 {record.get('round', 0)} 轮 · 输入 {usage.get('input_tokens')} / 输出 {usage.get('output_tokens')} Token")
            def show(value):
                return str(value) if value is not None else '未返回'
            self.usage_details.setText('最近一轮服务端用量\n'
                f"输入：文本 {show(usage.get('input_text_tokens'))} / 音频 {show(usage.get('input_audio_tokens'))}\n"
                f"输出：文本 {show(usage.get('output_text_tokens'))} / 音频 {show(usage.get('output_audio_tokens'))}\n"
                '包含系统提示和历史上下文；本次累计不是账号剩余额度。')
        else:
            self.usage_hint.setText('该轮被打断或服务端未返回用量 · 不按零消耗计算')
            self.usage_details.setText('服务端未返回该轮用量，无法准确计算；请查看平台权益或用量页面。')
        missing = snapshot.get('unreported_rounds', 0)
        if missing:
            self.total_tokens.setText('≥ ' + self.total_tokens.text())
            self.usage_details.setText(self.usage_details.text() + f'\n另有 {missing} 轮用量未返回，累计仅包含已报告的轮次。')

    def _mute(self, on):
        self.engine.mute(on)
        self.mute.setText('开启麦克风' if on else '静音麦克风')

    def _append(self, who, text):
        self.transcript.append_message(who, text)

    def _error(self, message):
        self.status.setText(message)
        self.hero_title.setText('暂时没有连上咕嘎')
        self.visual.mode = 'ended'
        self.transcript.appendPlainText('连接提示：' + message)

    def _finished(self):
        self._clock.stop()
        self.visual.mode = 'ended'
        self.visual.timer.stop()
        self.mute.setEnabled(False)
        self.hero_title.setText('下次再和本鹅聊吧')
        if not self.engine.last_error:
            self.status.setText('通话已结束 · 麦克风已释放')
        self.transcript.appendPlainText('通话已结束。需要继续时，从桌宠菜单重新开始。')

    def closeEvent(self, event):
        self.engine.stop()
        self._clock.stop()
        self.visual.timer.stop()
        event.accept()
