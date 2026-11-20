"""User-owned second confirmation for a specific recipient and exact message."""
import threading

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTextEdit, QVBoxLayout,
)


class WeChatConfirmationDialog(QDialog):
    completed = pyqtSignal(str, object)

    def __init__(self, agent, desktop_lock, parent=None):
        super().__init__(parent, Qt.Window)
        self.agent = agent
        self.desktop_lock = desktop_lock
        self.token = None
        self.cancel = threading.Event()
        self.working = False
        self.setWindowTitle('咕嘎 · 微信发送确认')
        self.resize(570, 370)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        header = QLabel('微信发送确认')
        font = header.font()
        font.setPointSize(18)
        font.setBold(True)
        header.setFont(font)
        root.addWidget(header)
        form = QFormLayout()
        self.recipient = QLineEdit()
        self.recipient.setMaxLength(80)
        self.message = QTextEdit()
        self.message.setAcceptRichText(False)
        form.addRow('收件人', self.recipient)
        form.addRow('发送正文', self.message)
        root.addLayout(form, 1)
        self.status = QLabel('先定位联系人。核对收件人和正文后，再确认发送。')
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        buttons = QHBoxLayout()
        self.locate_button = QPushButton('定位联系人')
        self.current_button = QPushButton('核对当前会话')
        self.send_button = QPushButton('确认发送')
        self.cancel_button = QPushButton('取消')
        buttons.addWidget(self.cancel_button)
        buttons.addStretch()
        buttons.addWidget(self.locate_button)
        buttons.addWidget(self.current_button)
        buttons.addWidget(self.send_button)
        root.addLayout(buttons)
        for button in (self.locate_button, self.current_button, self.send_button, self.cancel_button):
            button.setAutoDefault(False)
        self.recipient.textChanged.connect(self._invalidate)
        self.message.textChanged.connect(self._invalidate)
        self.locate_button.clicked.connect(self.locate)
        self.current_button.clicked.connect(lambda: self.locate(current=True))
        self.send_button.clicked.connect(self.confirm)
        self.cancel_button.clicked.connect(self.reject)
        self.completed.connect(self._finish)
        self._sync()

    def _invalidate(self):
        if self.token:
            self.token = None
            self.status.setText('收件人或正文已修改，请重新定位并确认。')
        self._sync()

    def _sync(self):
        available = not self.working
        self.recipient.setReadOnly(not available)
        self.message.setReadOnly(not available)
        ready = available and bool(self.recipient.text().strip()) and bool(self.message.toPlainText().strip())
        self.locate_button.setEnabled(ready)
        self.current_button.setEnabled(ready)
        self.send_button.setEnabled(available and bool(self.token))

    def _work(self, operation, label, action):
        if self.working:
            return
        self.cancel = threading.Event()
        self.working = True
        self.status.setText(label)
        self._sync()
        cancel = self.cancel

        def run():
            if not self.desktop_lock.acquire(False):
                result = dict(ok=False, error='桌面正被另一项任务使用')
            else:
                try:
                    result = action(cancel)
                except Exception as exc:
                    result = dict(ok=False, error=str(exc)[:220])
                finally:
                    self.desktop_lock.release()
            self.completed.emit(operation, result)

        threading.Thread(target=run, name='guga-wechat-confirmation', daemon=True).start()

    def locate(self, current=False):
        self.token = None
        name, text = self.recipient.text().strip(), self.message.toPlainText().strip()
        self._work('locate', '正在定位微信会话…',
                   lambda cancel: self.agent.prepare_confirmation(name, text, cancel, current=current))

    def confirm(self):
        token, self.token = self.token, None
        if token:
            self._work('send', '正在重新核对微信会话与正文，等待发送结果…',
                       lambda cancel: self.agent.confirm(token, cancel))

    def _finish(self, operation, result):
        self.working = False
        if not self.cancel.is_set():
            if operation == 'locate' and result.get('ok'):
                self.token = result.get('confirmation_id')
                self.status.setText('已核对会话标题。请确认屏幕上的收件人和正文，确认有效期 2 分钟。')
                self.raise_()
                self.activateWindow()
            elif result.get('ok') and result.get('verified_message_visible'):
                self.status.setText('✓ 已在此微信会话中回读到消息。')
            else:
                candidates = result.get('candidates') or []
                suffix = '\n识别到的候选：' + '、'.join(candidates) if candidates else ''
                self.status.setText((result.get('error') or result.get('message') or '操作未完成') + suffix)
        self._sync()

    def done(self, result):
        self.cancel.set()
        self.token = None
        super().done(result)
