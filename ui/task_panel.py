# -*- coding: utf-8 -*-
"""任务台账面板 — 桌宠上的"监工现场"

合一架构里管家一面的可视化: 评委在桌宠旁打开它, 就能看到
  目标 → 子任务拆解 → 执行者 → 实时状态 → 验收结果/打回理由

数据来源: core/butler_cloud.ButlerCloud 轮询 /api/state 的任务列表.
只读展示(操作都在桌宠对话/云端后台), 保持面板简单可靠.
"""
import time

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QTextBrowser, QPushButton)

PANEL_W, PANEL_H = 380, 520

ST_BADGE = {
    'pending':  ('#8a93a6', '排队'),
    'running':  ('#3d8bff', '进行中'),
    'review':   ('#f5a623', '待验收'),
    'returned': ('#f5a623', '返工待重派'),
    'done':     ('#37c26e', '已完成'),
    'error':    ('#ff5b5b', '失败'),
    'failed':   ('#ff5b5b', '失败'),
    'aborted':  ('#ff9f43', '已中止'),
    'paused':   ('#8a93a6', '暂停'),
}


class TaskPanel(QWidget):
    """无边框深色台账卡片. 用法: panel = TaskPanel(); panel.set_tasks(tasks); panel.show()"""

    def __init__(self, title='咕嘎 · 任务台账'):
        super().__init__()
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint
                            | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(PANEL_W, PANEL_H)
        self._drag = None
        self._title = title

        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)

        # 卡片主体
        card = QWidget()
        card.setObjectName('card')
        card.setStyleSheet("""
            QWidget#card { background: rgba(24,28,38,242);
                           border: 1px solid #3a4152; border-radius: 12px; }
            QLabel { color: #e8ecf4; font-family: "Microsoft YaHei UI"; }
            QTextBrowser { background: transparent; color: #cfd6e4;
                           border: none; font-family: "Microsoft YaHei UI";
                           font-size: 12px; }
            QPushButton { color: #9aa3b5; background: transparent;
                          border: none; font-size: 14px; }
            QPushButton:hover { color: #ffffff; }
        """)
        root.addWidget(card)

        cv = QVBoxLayout(card)
        cv.setContentsMargins(14, 10, 14, 12)

        # 标题行
        head = QVBoxLayout()
        top = QHBoxLayout()
        self._lbl_title = QLabel(title)
        self._lbl_title.setStyleSheet('font-size: 14px; font-weight: bold;')
        btn_close = QPushButton('✕')
        btn_close.setFixedSize(24, 20)
        btn_close.clicked.connect(self.hide)
        top.addWidget(self._lbl_title)
        top.addStretch(1)
        top.addWidget(btn_close)
        self._lbl_sub = QLabel('未连接云端大脑')
        self._lbl_sub.setStyleSheet('color:#8a93a6; font-size: 11px;')
        head.addLayout(top)
        head.addWidget(self._lbl_sub)
        cv.addLayout(head)

        # 台账正文
        self._view = QTextBrowser()
        self._view.setOpenExternalLinks(False)
        cv.addWidget(self._view, 1)

        self._foot = QLabel('同一账号在任何设备登录, 看到的都是这份台账')
        self._foot.setStyleSheet('color:#5d6575; font-size: 10px;')
        cv.addWidget(self._foot)

    # ---------- 数据 ----------

    def set_status(self, text):
        self._lbl_sub.setText(text)

    def set_tasks(self, tasks):
        if not tasks:
            self._view.setHtml(_empty_html())
            return
        parts = []
        for task in tasks[:10]:
            parts.append(_task_html(task))
        self._view.setHtml(''.join(parts))

    # ---------- 窗体行为 ----------

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPos() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPos() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None

    def toggle(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()
            self.activateWindow()


# ==================== HTML 渲染 ====================

def _esc(s):
    return (str(s or '').replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;'))


def _empty_html():
    return ('<div style="color:#5d6575; margin-top:40px; text-align:center;">'
            '还没有任务<br><br>对桌宠说一句目标，<br>比如「把本周实验数据整理成周报」</div>')


def _task_html(task):
    goal = _esc(task.get('goal', ''))
    overall = _overall(task)
    color, word = ST_BADGE.get(overall, ('#8a93a6', overall))
    demo = ' <span style="color:#8a93a6;">[演示剧本]</span>' if task.get('demo') else ''
    rows = []
    for st in (task.get('subtasks') or []):
        raw = st.get('status', 'pending')
        shown = 'aborted' if raw in ('error', 'failed', 'aborted') and _aborted(st) else raw
        c, w = ST_BADGE.get(shown, ('#8a93a6', '?'))
        prog = int(st.get('progress') or 0)
        rows.append(
            '<tr>'
            '<td style="padding:2px 6px 2px 0;">{name}</td>'
            '<td style="padding:2px 4px; color:#5d6575; font-size:11px;">{worker}</td>'
            '<td style="padding:2px 2px; color:{c}; font-size:11px; white-space:nowrap;">{w}</td>'
            '</tr>'
            '<tr><td colspan="3" style="padding:0 0 4px 0;">'
            '<div style="background:#2a3040; border-radius:3px; height:4px;">'
            '<div style="background:{c}; width:{prog}%; height:4px; border-radius:3px;"></div>'
            '</div>'
            '{check}'
            '</td></tr>'.format(
                name=_esc(st.get('name', '')),
                worker=_esc(st.get('worker', ''))[:14],
                c=c, w=w, prog=prog,
                check=('<div style="color:#8a93a6; font-size:11px; padding-bottom:2px;">'
                       '验收：%s</div>' % _esc(st.get('checkResult'))
                       if st.get('checkResult') else '')))
    ago = _ago(task.get('createdAt'))
    return (
        '<div style="border:1px solid #2a3040; border-radius:8px; padding:8px 10px; margin-bottom:8px;">'
        '<div style="margin-bottom:4px;"><b style="color:#e8ecf4;">{goal}</b>{demo} '
        '<span style="float:right; color:{oc}; font-size:11px;">{oword}</span></div>'
        '<div style="color:#5d6575; font-size:10px; margin-bottom:6px;">{ago} · {n} 个子任务</div>'
        '<table width="100%" cellpadding="0" cellspacing="0">{rows}</table>'
        '</div>'.format(goal=goal, demo=demo, oc=color, oword=word,
                        ago=ago, n=len(task.get('subtasks') or []), rows=''.join(rows)))


def _aborted(sub):
    """用户中止：服务端可能只给 error+aborted 标记，备注前缀兜底。"""
    sub = sub or {}
    if sub.get('aborted'):
        return True
    note = str(sub.get('note') or sub.get('checkResult') or sub.get('feedback') or '')
    return note.startswith(('已中止', '用户终止', 'Stopped', '中止'))


def _overall(task):
    subs = task.get('subtasks') or []
    sts = [s.get('status', 'pending') for s in subs]
    if not sts:
        return 'pending'
    bad = [s for s in subs if s.get('status') in ('error', 'failed', 'aborted')]
    if bad:
        return 'aborted' if all(_aborted(s) for s in bad) else 'error'
    if all(s == 'done' for s in sts):
        return 'done'
    if any(s in ('review', 'returned') for s in sts):
        return 'review'
    if any(s == 'running' for s in sts):
        return 'running'
    return 'pending'


def _ago(ts):
    try:
        diff = max(0, time.time() - float(ts) / 1000.0)
    except Exception:
        return ''
    if diff < 60:
        return '刚刚'
    if diff < 3600:
        return '%d 分钟前' % (diff // 60)
    if diff < 86400:
        return '%d 小时前' % (diff // 3600)
    return '%d 天前' % (diff // 86400)
