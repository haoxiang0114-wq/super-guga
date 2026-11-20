"""The desktop settings page for known abilities, installed work and repository plugins."""
import threading

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from core.plugin_market import PluginMarket, _version


class PluginMarketPage(QWidget):
    refreshed = pyqtSignal(object)

    def __init__(self, pet, experience, market=None):
        super().__init__()
        self.pet, self.experience = pet, experience
        self.market = market or PluginMarket()
        self._refreshing = False
        self._local_loaded = False
        self.features = {
            'hearthstone': ('炉石酒馆战棋', '实时炉石日志、阵容与对局。', pet._open_bg_live),
            'battleground-advisor': ('战棋军师', '打开战棋智能体面板。', pet._open_bg_agent),
            'desktop-ocr': ('桌面识别', '从应用窗口读取文字。', lambda: experience.open_settings('桌面识别')),
            'companion-memory': ('学习与长期记忆', '整理咕嘎的记忆及工作流。', lambda: pet._ensure_agent_panel().open_companion_settings()),
            'executor-settings': ('智能体分工', '选择咕嘎、Codex 或 DSH 执行。', lambda: experience.open_settings('智能体分工')),
        }
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        title = QLabel('咕嘎插件市场', objectName='PageTitle')
        header.addWidget(title, 1)
        self.update_button = QPushButton('更新仓库', objectName='Primary')
        self.update_button.clicked.connect(self.refresh)
        header.addWidget(self.update_button)
        layout.addLayout(header)
        self.status = QLabel('从仓库发现插件，也可以使用此电脑上的技能和工作流。', objectName='Muted')
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        layout.addWidget(self.scroll, 1)
        self.refreshed.connect(self._updated)
        self._render()

    def showEvent(self, event):
        super().showEvent(event)
        self._render(load_local=True)

    @staticmethod
    def _card(title, description, label, callback, parent, enabled=True):
        frame = QFrame()
        frame.setObjectName('PluginCard')
        frame.setStyleSheet('QFrame#PluginCard{background:#f3f7f8;border:1px solid #e0e8eb;border-radius:9px;}')
        row = QHBoxLayout(frame)
        text = QLabel(title + '\n' + description)
        text.setTextFormat(Qt.PlainText)
        text.setWordWrap(True)
        row.addWidget(text, 1)
        action = QPushButton(label)
        action.setEnabled(enabled)
        action.clicked.connect(lambda: callback())
        row.addWidget(action)
        parent.addWidget(frame)

    def _render(self, load_local=None):
        if load_local is not None:
            self._local_loaded = load_local
        panel = getattr(self.pet, '_agent_panel', None)
        root = QWidget()
        body = QVBoxLayout(root)
        body.setContentsMargins(0, 2, 4, 4)
        if self._local_loaded:
            selector = QComboBox()
            selector.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            selector.setMinimumContentsLength(14)
            from core.connected_knowledge import installed_skills
            for skill in installed_skills().get('Codex', []):
                selector.addItem('Codex · ' + skill['name'], ('local-skill', skill['name']))
            if panel is not None:
                for work in panel.runtime.workflows.list()[:30]:
                    if work.get('state') != 'disabled':
                        selector.addItem('工作流 · ' + work['title'], ('workflow', work.get('instructions') or work['title']))
            if selector.count():
                line = QHBoxLayout()
                line.addWidget(selector, 1)
                use = QPushButton('载入对话')
                use.clicked.connect(lambda: self._existing(selector.currentData()))
                line.addWidget(use)
                body.addLayout(line)
        available, installed = self.market.available(), self.market.installed()
        if available or installed:
            body.addWidget(QLabel('仓库插件'))
            for ident in sorted(set(available) | set(installed)):
                package = available.get(ident) or installed[ident]
                current = installed.get(ident)
                action = '安装' if not current else '更新' if _version(package) > _version(current) else (
                    '打开' if current['kind'] == 'feature' else '载入对话')
                description = package['description']
                if current:
                    description += '  ·  已安装 ' + current['version']
                    if action == '更新':
                        description += '  ·  可更新至 ' + package['version']
                callback = (lambda key=ident: self._install(key)) if action in ('安装', '更新') else (
                    lambda row=current: self._open(row))
                self._card(package['name'], description, action, callback, body)
        body.addWidget(QLabel('内置功能'))
        for title, desc, callback in self.features.values():
            self._card(title, desc, '打开', callback, body)
        body.addStretch()
        self.scroll.setWidget(root)

    def refresh(self):
        if self._refreshing:
            return
        self._refreshing = True
        self.update_button.setEnabled(False)
        self.status.setText('正在读取仓库目录…')
        def work():
            try:
                result = self.market.refresh()
            except (OSError, ValueError, RuntimeError) as exc:
                result = dict(error=str(exc)[:160])
            self.refreshed.emit(result)
        threading.Thread(target=work, name='guga-plugin-market', daemon=True).start()

    def _updated(self, result):
        self._refreshing = False
        self.update_button.setEnabled(True)
        if result.get('error'):
            self.status.setText('更新失败，保留已有目录。' + result['error'])
        elif result['published']:
            self.status.setText(f"仓库目录已更新，发现 {result['count']} 个插件。"
                                + (f"另有 {result['invalid']} 个包格式有误。" if result['invalid'] else ''))
        else:
            self.status.setText('远端还没有社区插件目录。当前可安装此版本自带的示例包。')
        self._render()

    def _install(self, ident):
        try:
            package = self.market.install(ident)
        except (OSError, ValueError) as exc:
            self.status.setText('安装未完成。' + str(exc)[:150])
            return
        self._render()
        self.status.setText(f"已安装 {package['name']} {package['version']}。")

    def _draft(self, text):
        panel = self.pet._ensure_agent_panel()
        if panel.editor.toPlainText().strip():
            self.status.setText('咕嘎对话里有草稿。处理草稿后再载入。')
            return
        panel.editor.setPlainText(text)
        panel.editor.moveCursor(panel.editor.textCursor().End)
        panel.show()
        from ui.desktop_position import keep_face_clear
        keep_face_clear(panel, self.pet)
        panel.raise_()
        panel.activateWindow()
        panel.editor.setFocus()
        self.status.setText('已载入对话，补上本次目标即可发送。')
        self.window().hide()

    def _existing(self, item):
        kind, value = item or ('', '')
        if kind == 'local-skill':
            self._draft('交给 Codex，使用本机技能 $' + value + ' 完成本次任务。\n本次目标：')
        elif kind == 'workflow':
            self._draft('请按我保存的工作流处理。\n' + value + '\n本次目标：')

    def _open(self, package):
        if package['kind'] == 'feature':
            self.features[package['entry']][2]()
        elif package['kind'] == 'codex-skill':
            self._draft('交给 Codex，按「' + package['name'] + '」完成本次任务。\n'
                        + package['body'] + '\n本次目标：')
        else:
            self._draft('请按「' + package['name'] + '」工作流处理。\n'
                        + package['body'] + '\n本次目标：')
