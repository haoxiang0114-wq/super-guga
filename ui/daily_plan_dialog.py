"""Editable recurring checklist; all changes persist through DailyPlan."""
from datetime import timedelta
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QLineEdit, QCheckBox, QMessageBox, QInputDialog)
from core.daily_plan import format_plan


class DailyPlanDialog(QDialog):
    def __init__(self, panel):
        # A regular top-level window stays reachable even while the floating chat is hidden.
        super().__init__(None, Qt.Window)
        self.panel = panel
        self.plan = panel.runtime.daily_plan
        self.setWindowTitle('咕嘎 · 每日计划')
        self.resize(510, 580)
        self.setMinimumSize(420, 420)
        layout = QVBoxLayout(self)
        self.heading = QLabel()
        layout.addWidget(self.heading)
        tip = QLabel('每天自动重置勾选，往日记录保留。\n也可以在聊天框输入：设置每日计划：学习；运动')
        tip.setWordWrap(True)
        layout.addWidget(tip)
        self.list = QListWidget()
        self.list.itemChanged.connect(self._checked)
        self.list.currentItemChanged.connect(self._selected)
        layout.addWidget(self.list)
        self.title_input = QLineEdit()
        self.title_input.setPlaceholderText('添加每天要做的事…')
        self.title_input.setMaxLength(200)
        self.title_input.returnPressed.connect(self._add)
        layout.addWidget(self.title_input)
        week = QHBoxLayout()
        self.weekdays = []
        for n, label in enumerate(('一','二','三','四','五','六','日'), 1):
            check = QCheckBox('周'+label)
            check.setChecked(True)
            self.weekdays.append(check)
            week.addWidget(check)
        layout.addLayout(week)
        buttons = QHBoxLayout()
        for label, handler in (('添加',self._add),('修改选中项',self._edit),('删除选中项',self._delete),('最近7天',self._history)):
            button = QPushButton(label)
            button.setAutoDefault(False)
            button.setDefault(False)
            button.clicked.connect(handler)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.feedback = QLabel('')
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)
        self.timer = QTimer(self)
        self.timer.setInterval(30000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def refresh(self):
        state = self.plan.snapshot()
        selected = self.list.currentItem()
        ident = selected.data(Qt.UserRole) if selected else None
        today = {x['task_id']:x for x in state['items']}
        self.list.blockSignals(True)
        self.list.clear()
        self.heading.setText(state['day']+' · 今日完成 %d/%d'%(state['completed'], state['total']))
        for template in state['templates']:
            current = today.get(template['id'])
            suffix = '' if current else '（今天不安排）'
            item = QListWidgetItem(template['title']+suffix)
            item.setData(Qt.UserRole, template['id'])
            item.setData(Qt.UserRole+1, template)
            if current:
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if current['done'] else Qt.Unchecked)
            self.list.addItem(item)
            if template['id'] == ident:
                self.list.setCurrentItem(item)
        self.list.blockSignals(False)

    def _run(self, args):
        try:
            result = self.panel.runtime.execute('daily_plan', args)
            self.feedback.setText(result.get('message','已保存'))
            self.refresh()
            return True
        except (ValueError, RuntimeError) as exc:
            self.feedback.setText(str(exc))
            return False

    def _checked(self, item):
        self._run(dict(action='check' if item.checkState()==Qt.Checked else 'uncheck', task_id=str(item.data(Qt.UserRole))))

    def _days(self):
        return [n for n, check in enumerate(self.weekdays,1) if check.isChecked()]

    def _selected(self, item, previous):
        if item is not None:
            task=item.data(Qt.UserRole+1)
            for n, check in enumerate(self.weekdays,1):check.setChecked(n in task['weekdays'])

    def _add(self):
        if self._run(dict(action='add', title=self.title_input.text(), weekdays=self._days())):
            self.title_input.clear()

    def _edit(self):
        item = self.list.currentItem()
        if item is None:
            self.feedback.setText('请先选中一项。');return
        task = item.data(Qt.UserRole+1)
        title, ok = QInputDialog.getText(self,'修改每日事项','事项名称（下方周几勾选决定重复日期）',text=task['title'])
        if ok:
            self._run(dict(action='update',task_id=str(task['id']),new_title=title,weekdays=self._days()))

    def _delete(self):
        item = self.list.currentItem()
        if item is not None:
            self._run(dict(action='delete',task_id=str(item.data(Qt.UserRole))))

    def _history(self):
        # Materialize the requested days so uncompleted days also have a denominator.
        for offset in range(7):
            self.plan.snapshot((self.plan.today()-timedelta(days=offset)).isoformat())
        QMessageBox.information(self,'最近7天打卡',format_plan(self.plan.execute(dict(action='history',days=7))))

    def showEvent(self, event):
        self.refresh();self.timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)
