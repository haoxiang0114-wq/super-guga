"""Desktop board for intentionally advancing one Bilibili step at a time."""
import threading

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QDialog, QLabel, QListWidget, QPushButton, QVBoxLayout

from core.bilibili_preparation import STEPS


class BilibiliPreparationDialog(QDialog):
    completed = pyqtSignal(object)

    def __init__(self, workflow, lock, animate=None, parent=None):
        super().__init__(parent, Qt.Window)
        self.workflow, self.lock, self.animate = workflow, lock, animate
        self.working = False
        self.cancel = threading.Event()
        self.setWindowTitle('咕嘎 · B 站投稿准备')
        self.resize(530, 345)
        root = QVBoxLayout(self)
        root.addWidget(QLabel('B 站投稿准备'))
        self.steps = QListWidget()
        self.steps.setSelectionMode(self.steps.NoSelection)
        root.addWidget(self.steps)
        self.status = QLabel('每次确认只执行下一步。准备结束后由你选择视频文件。')
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.next_button = QPushButton()
        self.next_button.setAutoDefault(False)
        self.next_button.clicked.connect(self.advance)
        root.addWidget(self.next_button)
        self.completed.connect(self.finish_step)
        self.sync()

    def sync(self):
        self.steps.clear()
        for i, title in enumerate(STEPS):
            mark = '✓' if i < self.workflow.step else '→' if i == self.workflow.step else '○'
            self.steps.addItem(f'{mark}  {title}')
        done = self.workflow.step >= len(STEPS)
        title = '准备完成' if done else '确认 · ' + STEPS[self.workflow.step]
        self.next_button.setText(title)
        self.next_button.setEnabled(not done and not self.working)

    def advance(self):
        if self.working or self.workflow.step >= len(STEPS):
            return
        self.working = True
        self.cancel = threading.Event()
        self.sync()
        self.status.setText('正在执行当前一步，等待界面回执…')
        if self.animate:
            self.animate('working')
        cancel = self.cancel

        def work():
            if not self.lock.acquire(False):
                result = dict(ok=False, message='桌面正被另一项任务使用。')
            else:
                try:
                    result = self.workflow.advance(cancel)
                except Exception as exc:
                    result = dict(ok=False, message=str(exc)[:200])
                finally:
                    self.lock.release()
            self.completed.emit(result)

        threading.Thread(target=work, name='guga-bilibili-preparation', daemon=True).start()

    def finish_step(self, result):
        self.working = False
        self.status.setText(str(result.get('message') or '未取得有效回执。'))
        if self.animate:
            self.animate('result' if self.workflow.step == len(STEPS) else 'waiting')
        self.sync()

    def done(self, result):
        self.cancel.set()
        super().done(result)
