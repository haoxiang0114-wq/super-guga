"""Small desktop entry that survives hiding the pet; owns no task or audio."""
from PyQt5.QtCore import Qt, QPoint
from PyQt5.QtWidgets import QWidget, QPushButton, QHBoxLayout, QMenu, QApplication
from ui.desktop_position import move_into_work_area


class DesktopLauncher(QWidget):
    def __init__(self, pet):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.pet = pet
        self._press = None
        self._dragged = False
        self.setWindowTitle('咕嘎 · 悬浮入口')
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet('''
            QPushButton {background:#fffdf8;color:#35344b;border:1px solid #ddddea;
                border-radius:13px;padding:9px;font:12px "Microsoft YaHei UI";}
            QPushButton:hover {background:#eef2ff;}
            QMenu {background:white;color:#30334b;border:1px solid #ddddea;padding:5px;}
            QMenu::item {padding:8px 16px;} QMenu::item:selected {background:#eef2ff;}
        ''')
        row = QHBoxLayout(self); row.setContentsMargins(2, 2, 2, 2); row.setSpacing(2)
        self.chat = QPushButton('咕嘎'); self.chat.setToolTip('点击打开对话；拖动移动悬浮入口')
        self.chat.installEventFilter(self); self.chat.clicked.connect(self.open_chat)
        row.addWidget(self.chat)
        self.more = QPushButton('···'); self.more.setToolTip('截图、语音与显示桌宠')
        self.more.clicked.connect(self.show_menu); row.addWidget(self.more)
        self.menu = QMenu(self)
        self.menu.addAction('💬 打开对话', self.open_chat)
        settings = self.menu.addMenu('🖥️ 桌面管理台')
        settings.addAction('模型与语音设置…', pet._open_desktop_console)
        settings.addAction('💭 陪伴与碎碎念…', pet._show_companion_settings)
        voice = self.menu.addMenu('🎙️ 语音与说话')
        voice.addAction('🎙 点击说话 · 录音后发送', pet._open_voice_recording)
        voice.addAction('📞 实时语音 · 使用模型额度', pet._open_agent_voice)
        voice.addAction('⚙ 实时语音配置…', pet._configure_realtime_voice)
        self.menu.addAction('📋 每日计划', pet._show_daily_plan)
        tools = self.menu.addMenu('🔧 工具')
        tools.addAction('📷 截图提问', lambda: pet._screen_tools.begin('region_ask'))
        tools.addAction('🔍 框选识字 / 解释', lambda: pet._screen_tools.begin('explain'))
        self.selection_action = tools.addAction('全局划词工具条')
        self.selection_action.setCheckable(True)
        self.selection_action.setChecked(pet._screen_tools.selection_enabled)
        self.selection_action.toggled.connect(pet._screen_tools.set_selection_enabled)
        pet._screen_tools.selection_enabled_changed.connect(self.selection_action.setChecked)
        self.silent_action = self.menu.addAction('🔇 静默咕嘎声音')
        self.silent_action.triggered.connect(pet._toggle_silent)
        self.sync_silent(getattr(pet, '_silent', False))
        self.menu.addSeparator()
        self.menu.addAction('🐧 显示桌宠', lambda: pet.set_pet_hidden(False))
        self.adjustSize()
        settings = pet._config._s
        screen = QApplication.screenAt(pet.frameGeometry().center()) or QApplication.primaryScreen()
        if screen:
            area = screen.availableGeometry()
            self.move(settings.value('desktop/launcher_x', area.right()-self.width()-24, type=int),
                      settings.value('desktop/launcher_y', area.center().y(), type=int))
        self.keep_on_screen()

    def keep_on_screen(self):
        move_into_work_area(self)

    def save_position(self):
        settings = self.pet._config._s
        settings.setValue('desktop/launcher_x', self.x())
        settings.setValue('desktop/launcher_y', self.y())

    def open_chat(self):
        if self._dragged:
            self._dragged = False
            return
        self.chat.setText('咕嘎')
        self.chat.setToolTip('点击打开对话；拖动移动悬浮入口')
        self.adjustSize()
        self.pet._open_agent_chat()

    def notify_text(self, text):
        self.chat.setText('咕嘎 · 消息')
        self.chat.setToolTip(str(text)[:500] + '\n点击打开对话；拖动移动悬浮入口')
        self.adjustSize(); self.keep_on_screen()

    def sync_silent(self, silent):
        self.silent_action.setText('🔊 恢复咕嘎声音' if silent else '🔇 静默咕嘎声音')

    def show_menu(self):
        self.menu.popup(self.mapToGlobal(QPoint(0, self.height())))

    def contextMenuEvent(self, event):
        self.menu.popup(event.globalPos()); event.accept()

    def eventFilter(self, obj, event):
        from PyQt5.QtCore import QEvent
        if obj is self.chat:
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._press = event.globalPos()-self.pos(); self._dragged = False
            elif event.type() == QEvent.MouseMove and self._press is not None and event.buttons() & Qt.LeftButton:
                target = event.globalPos()-self._press
                if self._dragged or (target-self.pos()).manhattanLength() > QApplication.startDragDistance():
                    self._dragged = True; self.move(target); self.keep_on_screen()
            elif event.type() == QEvent.MouseButtonRelease and self._press is not None:
                self._press = None
                self.save_position()
        return super().eventFilter(obj, event)

    def closeEvent(self, event):
        # The tray's explicit Quit remains the process lifetime owner.
        event.ignore()
