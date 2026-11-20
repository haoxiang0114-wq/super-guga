"""Discover local agents and attach one explicitly selected conversation."""
import threading

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout,
)

from core.codex_gateway import clean_error
from core.local_agent_catalog import discover_agents, list_conversations, verify_conversation


class LocalAgentCatalogDialog(QDialog):
    finished_work = pyqtSignal(str, object, object)

    def __init__(self, on_bind, parent=None):
        super().__init__(parent, Qt.Window)
        self.on_bind = on_bind
        self.working = False
        self._cancelled = False
        self.setWindowTitle('咕嘎 · 本机智能体')
        self.resize(740, 460)
        self.setStyleSheet("""
            QDialog { background: #f5f7fa; color: #212b39; }
            QLabel { color: #314155; }
            QListWidget { background: white; border: 1px solid #c8d5e2;
                          border-radius: 8px; padding: 5px; outline: none; }
            QListWidget::item { padding: 8px 6px; }
            QListWidget::item:selected { background: #e0ebff; color: #173e82; }
            QPushButton { background: white; border: 1px solid #c8d5e2;
                          border-radius: 7px; padding: 8px 14px; }
            QPushButton#bind { background: #254d91; border-color: #254d91; color: white; }
            QPushButton:disabled, QPushButton#bind:disabled { background: #ebeff4;
                          border-color: #d5dee8; color: #768398; }
        """)
        column = QVBoxLayout(self)
        column.setContentsMargins(22, 20, 22, 18)
        heading = QLabel('本机智能体')
        font = heading.font()
        font.setPointSize(19)
        font.setBold(True)
        heading.setFont(font)
        column.addWidget(heading)

        panels = QHBoxLayout()
        self.agents = QListWidget()
        self.agents.setAccessibleName('检测到的智能体')
        self.conversations = QListWidget()
        self.conversations.setAccessibleName('可绑定的已有对话')
        for text, widget, size in (('智能体', self.agents, 2), ('已有对话', self.conversations, 3)):
            panel = QVBoxLayout()
            panel.addWidget(QLabel(text))
            panel.addWidget(widget)
            panels.addLayout(panel, size)
        column.addLayout(panels, 1)

        self.status = QLabel('选择智能体后，点「查看已有对话」。')
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        column.addWidget(self.status)
        buttons = QHBoxLayout()
        self.scan_button = QPushButton('重新检测')
        self.list_button = QPushButton('查看已有对话')
        self.bind_button = QPushButton('绑定并在咕嘎打开')
        self.bind_button.setObjectName('bind')
        buttons.addWidget(self.scan_button)
        buttons.addStretch()
        buttons.addWidget(self.list_button)
        buttons.addWidget(self.bind_button)
        column.addLayout(buttons)
        self.scan_button.clicked.connect(self.scan)
        self.list_button.clicked.connect(self.load_conversations)
        self.bind_button.clicked.connect(self.bind_selected)
        self.agents.currentItemChanged.connect(self._agent_changed)
        self.conversations.currentItemChanged.connect(self._sync_buttons)
        self.finished_work.connect(self._finish)
        self._sync_buttons()

    @staticmethod
    def _selection(widget):
        current = widget.currentItem()
        return current.data(Qt.UserRole) if current is not None else None

    @staticmethod
    def _add(widget, label, data):
        item = QListWidgetItem(label)
        item.setData(Qt.UserRole, data)
        widget.addItem(item)

    def _sync_buttons(self):
        agent = self._selection(self.agents)
        available = not self.working
        self.agents.setEnabled(available)
        self.conversations.setEnabled(available)
        self.scan_button.setEnabled(available)
        self.list_button.setEnabled(available and agent is not None and agent.can_browse)
        self.bind_button.setEnabled(available and self._selection(self.conversations) is not None)

    def _agent_changed(self):
        self.conversations.clear()
        self._sync_buttons()

    def _work(self, operation, message, run):
        if self.working:
            return
        self.working = True
        self._cancelled = False
        self.status.setText(message)
        self._sync_buttons()

        def worker():
            try:
                value = run()
            except Exception as exc:
                self.finished_work.emit(operation, None, clean_error(exc))
            else:
                self.finished_work.emit(operation, value, None)

        threading.Thread(target=worker, name='guga-agent-catalog', daemon=True).start()

    def scan(self):
        self.conversations.clear()
        self._work('scan', '正在检测本机智能体…', discover_agents)

    def load_conversations(self):
        agent = self._selection(self.agents)
        if agent is not None and agent.can_browse:
            self.conversations.clear()
            self._work('list', '正在读取已有对话列表…', lambda: list_conversations(agent.key))

    def bind_selected(self):
        selection = self._selection(self.conversations)
        if selection is not None:
            self._work('bind', '正在核对选中的对话…',
                       lambda: (selection, verify_conversation(selection)))

    def _finish(self, operation, value, error):
        self.working = False
        if self._cancelled:
            self._sync_buttons()
            return
        if error:
            self.status.setText(error)
        elif operation == 'scan':
            self.agents.clear()
            for agent in value:
                self._add(self.agents, f'{agent.name} · {agent.status}', agent)
            if value:
                self.agents.setCurrentRow(0)
            self.status.setText('选择智能体后，点「查看已有对话」。')
        elif operation == 'list':
            for conversation in value:
                suffix = ' · 执行中' if conversation.running else ''
                self._add(self.conversations, conversation.title + suffix, conversation)
            self.status.setText('选择要绑定的对话。' if value else '暂无可绑定的对话。')
        elif operation == 'bind' and self.isVisible():
            try:
                self.on_bind(*value)
            except Exception as exc:
                self.status.setText(clean_error(exc))
            else:
                self.accept()
        self._sync_buttons()

    def done(self, result):
        self._cancelled = self.working
        super().done(result)
