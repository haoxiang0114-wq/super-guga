"""User-controlled plan review that never dispatches on import or on close."""
from dataclasses import replace
import threading

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QInputDialog, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from core.project_review import plan_request


class ProjectReviewDialog(QDialog):
    plan_loaded = pyqtSignal(object, str)
    plan_generated = pyqtSignal(object, object, str)

    def __init__(self, load_plan, dispatch, save_decision, open_original, bind_generated=None,
                 open_result=None, parent=None):
        super().__init__(parent, Qt.Window)
        self.load_plan, self.dispatch = load_plan, dispatch
        self.save_decision, self.open_original = save_decision, open_original
        self.bind_generated = bind_generated
        self.open_result = open_result
        self.plan = None
        self.working = False
        self.active_project = None
        self._interrupted_thread = ''
        self.setWindowTitle('咕嘎 · 项目黑板')
        self.resize(770, 530)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        title = QLabel('项目黑板 · 方案预审')
        font = title.font()
        font.setBold(True)
        font.setPointSize(18)
        title.setFont(font)
        root.addWidget(title)
        self.goal = QPlainTextEdit()
        self.goal.setPlaceholderText('项目需求，例如：设计一个三关桌面解谜游戏')
        self.goal.setMaximumHeight(86)
        root.addWidget(self.goal)
        toolbar = QHBoxLayout()
        self.copy_button = QPushButton('复制方案需求')
        self.generate_button = QPushButton('启动只读方案')
        self.original_button = QPushButton('打开 Codex 原对话')
        self.load_button = QPushButton('读取最新方案')
        for button in (self.generate_button, self.copy_button, self.original_button, self.load_button):
            toolbar.addWidget(button)
        root.addLayout(toolbar)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText('绑定 Codex 对话，在计划模式完成设计，再读取方案。')
        root.addWidget(self.preview, 1)
        self.status = QLabel('通过后才会创建关联的开发会话。')
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        decisions = QHBoxLayout()
        self.stop_button = QPushButton('终止')
        self.revise_button = QPushButton('返回修改')
        self.approve_button = QPushButton('通过并下发开发')
        for button in (self.stop_button, self.revise_button, self.approve_button):
            decisions.addWidget(button)
        root.addLayout(decisions)
        outputs = QHBoxLayout()
        self.execution_buttons = []
        for key, label in (('executor', '打开开发会话'), ('preview', '游戏预览'), ('folder', '打开项目目录')):
            button = QPushButton(label)
            button.clicked.connect(lambda _, target=key: self.show_result(target))
            button.setEnabled(False)
            outputs.addWidget(button)
            self.execution_buttons.append((key, button))
        if self.open_result:
            root.addLayout(outputs)
        self.copy_button.clicked.connect(self.copy_request)
        self.generate_button.clicked.connect(self.generate)
        self.original_button.clicked.connect(self.show_original)
        self.load_button.clicked.connect(self.fetch_plan)
        self.stop_button.clicked.connect(self.stop_review)
        self.revise_button.clicked.connect(self.revise)
        self.approve_button.clicked.connect(self.approve)
        self.goal.textChanged.connect(self._clear_plan)
        self.plan_loaded.connect(self.show_plan)
        self.plan_generated.connect(self.show_generated)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        self.sync()

    def _clear_plan(self):
        self.plan = None
        self._interrupted_thread = ''
        self.preview.clear()
        self.sync()

    def sync(self):
        ready = not self.working
        has_goal = bool(self.goal.toPlainText().strip())
        self.goal.setReadOnly(self.working)
        self.load_button.setEnabled(ready and has_goal)
        self.copy_button.setEnabled(ready and has_goal)
        self.generate_button.setVisible(self.bind_generated is not None)
        self.generate_button.setEnabled(ready and has_goal)
        self.generate_button.setText('续接只读方案' if self._interrupted_thread else '启动只读方案')
        self.revise_button.setEnabled(ready and self.plan is not None)
        self.approve_button.setEnabled(ready and self.plan is not None and not self.plan.issues)

    def copy_request(self):
        QApplication.clipboard().setText(plan_request(self.goal.toPlainText()))
        self.status.setText('方案需求已复制，可在绑定的 Codex 对话里用计划模式提交。')

    def show_original(self):
        try:
            plan = self.plan or self.active_project
            self.open_original(plan.thread_id if plan else self._interrupted_thread)
        except Exception as exc:
            self.status.setText(str(exc)[:250])

    def fetch_plan(self):
        try:
            read = self.load_plan(self.goal.toPlainText().strip())
        except Exception as exc:
            self.status.setText(str(exc)[:250])
            return
        self.working = True
        self.status.setText('正在读取已绑定的 Codex 方案…')
        self.sync()

        def run():
            try:
                self.plan_loaded.emit(read(), '')
            except Exception as exc:
                self.plan_loaded.emit(None, str(exc)[:250])

        threading.Thread(target=run, name='guga-project-review', daemon=True).start()

    def generate(self, revision=''):
        from core.codex_readonly_plan import PlanInterrupted, generate_codex_plan
        from core.project_review import inspect_plan

        goal, original = self.goal.toPlainText().strip(), self._interrupted_thread
        self.working = True
        self.status.setText('Codex 正在只读规划。开发须等黑板批准。')
        self.sync()

        def run():
            try:
                thread, raw = generate_codex_plan(goal, thread_id=original, revision=revision if isinstance(revision, str) else '')
                plan = inspect_plan('', thread, goal, raw)
            except PlanInterrupted as exc:
                self.plan_generated.emit(None, exc.thread_id, str(exc)[:250])
            except Exception as exc:
                self.plan_generated.emit(None, original, str(exc)[:250])
            else:
                self.plan_generated.emit(plan, thread, '')

        threading.Thread(target=run, name='guga-project-plan', daemon=True).start()

    def show_generated(self, plan, thread, error):
        self._interrupted_thread = thread if error else ''
        if plan is not None:
            try:
                plan = replace(plan, conversation_id=self.bind_generated(plan))
            except Exception as exc:
                self._interrupted_thread = thread
                self.show_plan(None, '方案已生成，绑定未确认：' + str(exc)[:170])
                return
        self.show_plan(plan, error)

    def show_plan(self, plan, error):
        self.working = False
        self.plan = plan
        if error:
            self.status.setText(error)
        else:
            self.preview.setPlainText(plan.text)
            self.status.setText('\n'.join(plan.issues) or '自动预检通过，等你确认是否开始开发。')
        self.sync()

    def revise(self):
        if self.plan:
            plan = self.plan
            issues = '\n'.join(plan.issues)
            notes, accepted = QInputDialog.getMultiLineText(self, '返回修改', '补充修改要求', issues)
            if not accepted or not notes.strip():
                return
            try:
                self.save_decision(plan, 'revise')
            except Exception as exc:
                self.status.setText(str(exc)[:250])
                return
            self._clear_plan()
            if self.bind_generated is not None:
                self._interrupted_thread = plan.thread_id
                self.generate(notes)
            else:
                QApplication.clipboard().setText(plan_request(plan.goal) + '\n需要修改：' + notes)
                self.status.setText('已记录返回修改，并复制修改要求。')

    def stop_review(self):
        if self.working:
            self.status.setText('只读请求正在收尾，请等回执后处理。')
            return
        if self.plan:
            try:
                self.save_decision(self.plan, 'stopped')
            except Exception as exc:
                self.status.setText(str(exc)[:250])
                return
        self._clear_plan()
        self.status.setText('方案评审已终止。')

    def approve(self):
        plan, self.plan = self.plan, None
        self.sync()
        if plan is None or plan.issues:
            return
        self.active_project = plan
        try:
            self.dispatch(plan)
        except Exception as exc:
            self.status.setText(str(exc)[:250])
        else:
            self.status.setText('已提交开发请求，进展以开发会话回执为准。')
            self._sync_result_buttons('dispatching')

    def update_execution(self, event):
        if self.active_project is None or event.get('cid') != self.active_project.conversation_id:
            return
        stage = event.get('type')
        if stage in ('progress', 'completed', 'error', 'stopped'):
            text = str(event.get('text') or '')[:350]
            label = {'progress': '开发中', 'completed': '开发已回传结果',
                     'error': '开发需要核对', 'stopped': '开发已停止'}[stage]
            self.status.setText(label + (f' · {text}' if text else ''))
            self._sync_result_buttons(stage)

    def _sync_result_buttons(self, stage):
        for key, button in self.execution_buttons:
            button.setEnabled(stage == 'completed' or (key != 'preview' and stage in ('progress', 'error', 'stopped')))

    def show_result(self, action):
        if not self.active_project or not self.open_result:
            return
        try:
            self.open_result(self.active_project, action)
        except Exception as exc:
            self.status.setText(str(exc)[:250])
