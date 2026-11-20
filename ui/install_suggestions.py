# -*- coding: utf-8 -*-
"""自检通过后的“建议安装”弹窗

只推荐咕嘎真实适配过的三项外部工具，且用 runtime/local_tools.json 的真实
探测结果过滤：已装 / 正在运行的一律不再推荐。

- 每条写清“装了能做什么 / 不装会少哪块能力 / 装在哪”
- 可以关闭；勾选“以后不再提示”后写入 runtime/install_suggestions.json
- 不触碰渲染主循环，也不改任何业务配置
"""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel,
                             QPushButton, QScrollArea, QVBoxLayout, QWidget)

from ui.dialog_fit import fit_dialog


class InstallSuggestions(QDialog):
    def __init__(self, missing, installed_names=()):
        super().__init__()
        self.setWindowTitle('咕嘎 · 建议安装')
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self._tools = list(missing)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 12)
        root.setSpacing(8)

        title = QLabel('咕嘎已经可用。再装这几个工具，它才能把活派出去')
        title.setStyleSheet('font-size:15px; font-weight:bold; color:#1a1c20;')
        root.addWidget(title)

        sub = '咕嘎本体会正常启动；下面这些是外部施工方，装了才解锁对应派单能力。'
        if installed_names:
            sub += '（已检测到 %s，不再重复推荐）' % '、'.join(installed_names)
        label = QLabel(sub)
        label.setWordWrap(True)
        label.setStyleSheet('color:#4a5568; font-size:12px;')
        root.addWidget(label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        holder = QWidget()
        box = QVBoxLayout(holder)
        box.setContentsMargins(0, 0, 4, 0)
        box.setSpacing(8)
        for tool in self._tools:
            box.addWidget(self._card(tool))
        box.addStretch(1)
        scroll.setWidget(holder)
        root.addWidget(scroll, 1)

        self._no_more = QCheckBox('以后不再提示（可在 runtime\\install_suggestions.json 里删除以恢复）')
        self._no_more.setStyleSheet('color:#6b7280;font-size:12px;')
        root.addWidget(self._no_more)

        btns = QHBoxLayout()
        later = QPushButton('稍后再说')
        later.setStyleSheet(
            'QPushButton{background:transparent;color:#4a5568;padding:8px 16px;'
            'border:1px solid #d5dae2;border-radius:6px;}'
            'QPushButton:hover{background:#f3f5f8;}')
        later.clicked.connect(self._later)
        start = QPushButton('知道了，开始使用 →')
        start.setStyleSheet(
            'QPushButton{background:#2f7cf6;color:white;padding:8px 20px;'
            'border-radius:6px;font-weight:bold;}'
            'QPushButton:hover{background:#2468d9;}')
        start.clicked.connect(self._start)
        btns.addStretch(1)
        btns.addWidget(later)
        btns.addWidget(start)
        root.addLayout(btns)

        fit_dialog(self, 660, 640, min_width=480, min_height=360)

    def _card(self, tool):
        frame = QFrame()
        frame.setStyleSheet('QFrame{background:#fafbfc;border:1px solid #e6e9ef;'
                            'border-radius:8px;}')
        column = QVBoxLayout(frame)
        column.setContentsMargins(12, 10, 12, 10)
        column.setSpacing(4)

        head = QHBoxLayout()
        name = QLabel(tool['name'])
        name.setStyleSheet('font-size:13px;font-weight:bold;color:#1a1c20;')
        head.addWidget(name)
        badge = QLabel(tool.get('badge', '推荐'))
        color, background = (('#c0392b', '#fdecea') if tool.get('badge') == '必装'
                             else ('#2f6f4f', '#eaf5ef'))
        badge.setStyleSheet('color:%s;background:%s;border-radius:4px;'
                            'padding:2px 6px;font-size:11px;font-weight:bold;'
                            % (color, background))
        head.addWidget(badge)
        head.addStretch(1)
        column.addLayout(head)

        column.addWidget(self._line('装了能做什么', tool.get('gains', ''), '#1d7a3e'))
        column.addWidget(self._line('不装会少什么', tool.get('missing', ''), '#b36b00'))
        column.addWidget(self._line('怎么装', tool.get('install', ''), '#6b7280'))
        return frame

    @staticmethod
    def _line(prefix, text, color):
        label = QLabel('%s：%s' % (prefix, text))
        label.setWordWrap(True)
        label.setStyleSheet('color:%s;font-size:12px;' % color)
        return label

    def _later(self):
        self.reject()

    def _start(self):
        if self._no_more.isChecked():
            try:
                from core import tool_advice
                tool_advice.dismiss()
            except Exception:
                pass
        self.accept()


def maybe_show(parent=None):
    """自检通过后调用：没装齐工具、没选“不再提示”、且 24 小时内没弹过才弹。

    返回是否弹出。任何异常都直接放弃弹窗，绝不影响启动。
    """
    try:
        from core import tool_advice
        if not tool_advice.should_show():
            return False
        missing = tool_advice.missing_tools()
        installed = tool_advice.installed_names()
    except Exception:
        return False
    if not missing:
        return False
    dialog = InstallSuggestions(missing, installed)
    if parent is not None:
        dialog.setWindowModality(Qt.ApplicationModal)
    try:
        dialog.exec_()
    finally:
        try:
            from core import tool_advice
            tool_advice.mark_shown()
        except Exception:
            pass
    return True
