"""Native Qt workspace for Guga's conversation and task controls."""
from PyQt5.QtCore import Qt, QSize
from PyQt5.QtGui import QColor, QFont, QIcon, QKeySequence, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu,
    QPlainTextEdit, QPushButton, QShortcut, QSizeGrip, QSplitter, QStackedWidget,
    QTextBrowser, QToolButton, QTreeWidget, QVBoxLayout, QWidget,
)

from ui.companion_widgets import ElidedLabel, member_icon, ROOT
from ui.workspace_style import WORKSPACE_STYLE


class _WelcomeArt(QLabel):
    def paintEvent(self, event):
        image = self.pixmap()
        if not image or image.isNull():
            return
        dpr = image.devicePixelRatioF()
        width = min(self.width(), round(image.width() / dpr))
        height = min(self.height(), round(image.height() / dpr))
        scaled = image.scaled(round(width * dpr), round(height * dpr),
                              Qt.KeepAspectRatio, Qt.SmoothTransformation)
        scaled.setDevicePixelRatio(dpr)
        painter = QPainter(self)
        painter.drawPixmap(round((self.width() - scaled.width() / dpr) / 2),
                           round((self.height() - scaled.height() / dpr) / 2), scaled)


def _sidebar_icon():
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QPen(QColor('#3d5a69'), 2))
    painter.drawRoundedRect(3, 6, 25, 20, 4, 4)
    painter.drawLine(12, 6, 12, 26)
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return QIcon(pixmap)


def _button(text, tooltip, action=None, name=''):
    button = QPushButton(text)
    button.setObjectName(name)
    button.setToolTip(tooltip)
    button.setAccessibleName(tooltip)
    button.setAutoDefault(False)
    if action:
        button.clicked.connect(action)
    return button


def build(p):
    p.setWindowTitle('咕嘎')
    p.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
    p.setAttribute(Qt.WA_TranslucentBackground)
    p.setWindowIcon(QIcon(str(ROOT / 'resources/guga.ico')))
    p.resize(1060, 730)
    p.setMinimumSize(710, 520)
    p.setFont(QFont('Microsoft YaHei UI', 10))
    p.setObjectName('AgentWindow')
    p.setStyleSheet(WORKSPACE_STYLE)

    root = QVBoxLayout(p)
    root.setContentsMargins(4, 4, 4, 4)
    shell = QFrame(objectName='ChatShell')
    root.addWidget(shell)
    main = QVBoxLayout(shell)
    main.setContentsMargins(11, 8, 11, 5)
    main.setSpacing(5)
    main.addWidget(_header(p))
    p.splitter = QSplitter()
    p.splitter.setChildrenCollapsible(False)
    p.splitter.setHandleWidth(7)
    p.splitter.addWidget(_sidebar(p))
    p.splitter.addWidget(_conversation(p))
    p.splitter.setSizes([214, 808])
    p.splitter.setStretchFactor(0, 0)
    p.splitter.setStretchFactor(1, 1)
    main.addWidget(p.splitter, 1)
    footer = QHBoxLayout()
    footer.addStretch()
    footer.addWidget(QSizeGrip(p))
    main.addLayout(footer)
    QShortcut(QKeySequence('Ctrl+N'), p, activated=p.new_conversation)


def _header(p):
    header = QFrame()
    p._drag_header = header
    header.installEventFilter(p)
    row = QHBoxLayout(header)
    row.setContentsMargins(1, 0, 1, 2)
    row.setSpacing(5)
    p.history_button = _button('', '显示或隐藏侧栏', p._toggle_sidebar)
    p.history_button.setIcon(_sidebar_icon())
    p.history_button.setCheckable(True)
    p.history_button.setChecked(True)
    row.addWidget(p.history_button)
    brand = QLabel()
    brand.setPixmap(p.windowIcon().pixmap(25, 25))
    brand.setAttribute(Qt.WA_TransparentForMouseEvents)
    row.addWidget(brand)
    p.title = ElidedLabel('咕嘎 · 新对话')
    p.title.setObjectName('Title')
    p.title.installEventFilter(p)
    row.addWidget(p.title, 1)
    p.header_new = _button('＋', '新对话 · Ctrl+N', p.new_conversation)
    row.addWidget(p.header_new)
    p.header_new.hide()
    p.console_button = _button('模型接入', '连接聊天模型与视觉模型',
                               lambda: p.pet._open_model_connection(), 'SettingsEntry')
    row.addWidget(p.console_button)
    p.pin = _button('置顶', '对话窗口置顶')
    p.pin.setCheckable(True)
    p.pin.setChecked(True)
    p.pin.toggled.connect(p._pin)
    row.addWidget(p.pin)
    p.more_button = _button('···', '更多设置与工具', name='More')
    p.more_button.setMenu(_more_menu(p))
    row.addWidget(p.more_button)
    p.hide_button = _button('−', '收起对话', p.hide_to_pet)
    p.close_button = _button('×', '关闭窗口并结束通话', p.close, 'Close')
    row.addWidget(p.hide_button)
    row.addWidget(p.close_button)
    return header


def _sidebar(p):
    p.sidebar = QFrame(objectName='Sidebar')
    p.sidebar.setMinimumWidth(192)
    p.sidebar.setMaximumWidth(325)
    side = QVBoxLayout(p.sidebar)
    side.setContentsMargins(11, 12, 11, 10)
    side.setSpacing(6)
    p.new_button = _button('＋  新对话', '新对话', p.new_conversation, 'NewChat')
    side.addWidget(p.new_button)
    p.project_button = _button('新建项目', '新建项目', p.new_project, 'SideLink')
    side.addWidget(p.project_button)

    heading = QHBoxLayout()
    heading.addWidget(QLabel('搭档', objectName='SectionTitle'), 1)
    heading.addWidget(_button('设置', '设置当前搭档的人设与记忆', lambda: p.open_companion_settings()))
    side.addLayout(heading)
    team = QHBoxLayout()
    team.setSpacing(3)
    p.member_buttons = {}
    for ident, name in (('001', '咕嘎'), ('002', '肥鱼'), ('003', 'Dora')):
        member = QToolButton(objectName='Member')
        member.setIcon(member_icon(ident))
        member.setIconSize(QSize(31, 31))
        member.setText(name)
        member.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        member.setCheckable(True)
        member.setToolTip('切换到' + name)
        member.clicked.connect(lambda checked=False, agent=ident: p.activate_agent(agent))
        team.addWidget(member, 1)
        p.member_buttons[ident] = member
    side.addLayout(team)

    side.addSpacing(6)
    p.tree = QTreeWidget()
    p.tree.setHeaderHidden(True)
    p.tree.setIndentation(12)
    p.tree.setTextElideMode(Qt.ElideRight)
    p.tree.setContextMenuPolicy(Qt.CustomContextMenu)
    p.tree.customContextMenuRequested.connect(p._context_menu)
    p.tree.itemClicked.connect(p._selected)
    p.tree.itemDoubleClicked.connect(p._rename_item)
    side.addWidget(p.tree, 1)
    p.workflow_button = _button('工作流', '编写和复用工作流',
                                lambda: p.open_companion_settings(tab=2), 'SideLink')
    p.archive_button = _button('已归档对话', '已归档对话', p._toggle_archive, 'SideLink')
    side.addWidget(p.workflow_button)
    side.addWidget(p.archive_button)
    return p.sidebar


def _conversation(p):
    content = QFrame(objectName='WorkspaceBody')
    width = QHBoxLayout(content)
    width.setContentsMargins(14, 5, 14, 10)
    canvas = QWidget()
    canvas.setMaximumWidth(900)
    width.addStretch(1)
    width.addWidget(canvas, 100)
    width.addStretch(1)
    layout = QVBoxLayout(canvas)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)

    settings = _options(p)
    top = QHBoxLayout()
    p.model_label = ElidedLabel('咕嘎')
    p.model_label.setObjectName('Muted')
    top.addWidget(p.model_label, 1)
    p.options_button = _button('对话选项', '展开执行方式、思考档位、联网和朗读设置')
    p.options_button.setCheckable(True)
    p.options_button.toggled.connect(settings.setVisible)
    top.addWidget(p.options_button)
    layout.addLayout(top)
    layout.addWidget(settings)
    settings.hide()

    p.conversation_pages = QStackedWidget()
    p.view = QTextBrowser()
    p.view.setOpenExternalLinks(False)
    p.view.anchorClicked.connect(p._log_link)
    p.conversation_pages.addWidget(p.view)
    p.welcome = QWidget()
    welcome = QVBoxLayout(p.welcome)
    welcome.setSpacing(9)
    welcome.addStretch(3)
    p.hero_art = _WelcomeArt()
    p.hero_art.setMinimumHeight(48)
    welcome.addWidget(p.hero_art)
    p.hero_title = QLabel(objectName='HeroTitle', alignment=Qt.AlignCenter, wordWrap=True)
    welcome.addWidget(p.hero_title)
    intro = QLabel('有问题想聊，或有事情要交给我？',
                   objectName='Muted', alignment=Qt.AlignCenter, wordWrap=True)
    welcome.addWidget(intro)
    welcome.addStretch(4)
    p.conversation_pages.addWidget(p.welcome)
    layout.addWidget(p.conversation_pages, 1)

    p.status = QLabel('就绪', objectName='Badge', wordWrap=True)
    layout.addWidget(p.status)
    p.attachment_bar = QFrame(objectName='Options')
    attachment = QHBoxLayout(p.attachment_bar)
    attachment.setContentsMargins(9, 1, 4, 1)
    p.attachment_label = QLabel('已附加截图 · 仅本轮使用', objectName='Muted')
    attachment.addWidget(p.attachment_label, 1)
    attachment.addWidget(_button('×', '移除截图', p._clear_attachment))
    layout.addWidget(p.attachment_bar)
    p.attachment_bar.hide()
    layout.addWidget(_composer(p))
    layout.addWidget(_voice_controls(p))
    return content


def _options(p):
    panel = QFrame(objectName='Options')
    layout = QGridLayout(panel)
    layout.setContentsMargins(8, 7, 8, 7)
    layout.setHorizontalSpacing(6)
    p.route_combo, p.effort_combo, p.search_combo = QComboBox(), QComboBox(), QComboBox()
    options = (
        (p.route_combo, 'chat/route', 'auto', '任务分流',
         (('自动分流', 'auto'), ('只问答', 'chat'), ('执行任务', 'task'))),
        (p.effort_combo, 'chat/effort', 'quick', '问答思考档位',
         (('快速回答', 'quick'), ('轻度思考', 'low'), ('深度思考', 'high'), ('极限思考', 'max'))),
        (p.search_combo, 'chat/search', 'auto', '问答联网策略',
         (('联网自动', 'auto'), ('强制联网', 'always'), ('关闭联网', 'never'))),
    )
    for column, (combo, key, default, label, choices) in enumerate(options):
        for title, value in choices:
            combo.addItem(title, value)
        combo.setAccessibleName(label)
        combo.setToolTip(label)
        combo.setCurrentIndex(max(0, combo.findData(p.pet._config._s.value(key, default))))
        combo.currentIndexChanged.connect(
            lambda index, select=combo, setting=key: p.pet._config._s.setValue(setting, select.currentData()))
        layout.addWidget(combo, 0, column)
    p.read_aloud = QCheckBox('朗读')
    layout.addWidget(p.read_aloud, 0, 3)
    p.executor_combo = QComboBox()
    for title, value in (('自动分工', 'auto'), ('咕嘎处理任务', 'self'), ('Codex 执行任务', 'codex'),
                         ('DSH 执行任务', 'dsh'), ('WorkBuddy 执行任务', 'workbuddy')):
        p.executor_combo.addItem(title, value)
    p.executor_combo.setAccessibleName('任务执行方式')
    p.executor_combo.setToolTip('普通问答由咕嘎回答。执行任务时使用此设置。')
    p.executor_combo.setCurrentIndex(max(0, p.executor_combo.findData(p.pet._config._s.value('team/executor', 'auto'))))
    p.executor_combo.currentIndexChanged.connect(lambda _: p.pet._set_executor_mode(p.executor_combo.currentData()))
    layout.addWidget(p.executor_combo, 1, 0, 1, 2)
    p.executor_open = _button('打开执行器', '打开当前对话绑定的执行器', p.open_executor)
    p.executor_open.hide()
    p.executor_history = _button('执行器记录', '查看执行器同步的对话记录', p.toggle_executor_view)
    p.executor_history.setCheckable(True)
    p.executor_history.hide()
    layout.addWidget(p.executor_open, 1, 2)
    layout.addWidget(p.executor_history, 1, 3)
    return panel


def _composer(p):
    p.composer = QFrame(objectName='Composer')
    layout = QVBoxLayout(p.composer)
    layout.setContentsMargins(8, 6, 7, 6)
    layout.setSpacing(2)
    p.editor = QPlainTextEdit()
    p.editor.setPlaceholderText('给咕嘎发消息')
    p.editor.setAccessibleName('消息输入框')
    p.editor.setToolTip('Enter 发送 · Shift+Enter 换行')
    p.editor.setFixedHeight(64)
    p.editor.installEventFilter(p)
    layout.addWidget(p.editor)

    from ui.voice_record_bar import VoiceRecordBar
    p.voice_record_bar = VoiceRecordBar(p)
    p.voice_record_bar.started.connect(p.start_bubble_short_voice)
    p.voice_record_bar.submitted.connect(lambda: p.finish_bubble_short_voice(False))
    p.voice_record_bar.cancelled.connect(lambda: p.finish_bubble_short_voice(True))
    layout.addWidget(p.voice_record_bar)
    p.voice_record_bar.hide()

    controls = QHBoxLayout()
    controls.setSpacing(1)
    tools = QToolButton(objectName='SmallTools', text='工具')
    tools.setPopupMode(QToolButton.InstantPopup)
    menu = QMenu(tools)
    menu.addAction('优化提示词', p.optimize_composer)
    menu.addAction('框选截图', lambda: p._screen_tool('region_ask'))
    menu.addAction('框选识字', lambda: p._screen_tool('explain'))
    p.explain_button = menu.addAction('划词工具条')
    p.explain_button.setCheckable(True)
    p.explain_button.setChecked(p.pet._screen_tools.selection_enabled)
    p.explain_button.toggled.connect(p.pet._screen_tools.set_selection_enabled)
    p.pet._screen_tools.selection_enabled_changed.connect(p.explain_button.setChecked)
    tools.setMenu(menu)
    controls.addWidget(tools)
    p.short_voice_button = _button('说话', '录音后发送', p.toggle_short_voice)
    controls.addWidget(p.short_voice_button)
    p.voice_button = _button('实时语音', '使用实时语音模型通话')
    p.voice_button.setCheckable(True)
    p.voice_button.toggled.connect(p.toggle_voice)
    controls.addWidget(p.voice_button)
    p.log_button = _button('执行记录', '展开操作摘要与工具记录')
    p.log_button.setCheckable(True)
    p.log_button.toggled.connect(p._toggle_logs)
    controls.addWidget(p.log_button)
    controls.addStretch()
    p.stop_button = _button('停止', '停止当前任务或回答', p.stop_turn)
    p.stop_button.setEnabled(False)
    controls.addWidget(p.stop_button)
    p.send_button = _button('↑', '发送消息 · Enter', p.send, 'Primary')
    p.send_button.setFixedSize(36, 32)
    controls.addWidget(p.send_button)
    layout.addLayout(controls)
    return p.composer


def _voice_controls(p):
    p.voice_controls = QFrame()
    row = QHBoxLayout(p.voice_controls)
    row.setContentsMargins(3, 0, 3, 0)
    p.mute_button = _button('静音', '静音麦克风')
    p.mute_button.setCheckable(True)
    p.mute_button.setEnabled(False)
    p.mute_button.toggled.connect(lambda on: p.voice.mute(on) if p.voice_active and p.voice else None)
    row.addWidget(p.mute_button)
    p.interrupt_button = _button('打断', '打断咕嘎当前回答',
                                 lambda: p.voice.interrupt() if p.voice_active and p.voice else None)
    p.interrupt_button.setEnabled(False)
    row.addWidget(p.interrupt_button)
    p.voice_share_button = _button('共享屏幕', '开启或停止通话的屏幕共享')
    p.voice_share_button.setCheckable(True)
    p.voice_share_button.toggled.connect(p.toggle_call_screen_share)
    row.addWidget(p.voice_share_button)
    row.addStretch()
    p.voice_controls.hide()
    return p.voice_controls


def _more_menu(p):
    menu = QMenu(p)
    menu.addAction('软件设置…', p.pet._open_desktop_console)
    menu.addAction('软件权限…', p.open_permissions)
    menu.addAction('长语音模型…', p.open_voice_settings)
    menu.addMenu(p.pet._ensure_management_menu())
    menu.addSeparator()
    menu.addAction('本机智能体与会话绑定…', p.pet._show_local_agents)
    menu.addAction('B 站投稿准备…', p.pet._show_bilibili_preparation)
    menu.addAction('项目黑板 · 方案预审…', p.pet._show_project_review)
    menu.addAction('恢复上次未发出的要求', p.recover_pending_draft)
    menu.addAction('对话更名…', p.rename_active)
    menu.addAction('快捷键说明', p._shortcut_help)
    return menu
