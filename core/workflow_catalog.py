"""Persistent, inspectable workflow definitions and outcome counters."""
from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from core.agent_memory import safe_text

ROOT = Path(__file__).resolve().parents[1]


class WorkflowCatalog:
    def __init__(self, path=None):
        self.path = Path(path or ROOT/'runtime/workflows.sqlite3')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS workflows(
                    id TEXT PRIMARY KEY, title TEXT, trigger TEXT, instructions TEXT,
                    state TEXT, success INTEGER DEFAULT 0, failure INTEGER DEFAULT 0,
                    last_summary TEXT DEFAULT '', updated REAL);
                CREATE TABLE IF NOT EXISTS runs(
                    id TEXT PRIMARY KEY, workflow TEXT, ok INTEGER, summary TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS goal_stats(
                    goal_key TEXT PRIMARY KEY, goal TEXT, successes INTEGER, failures INTEGER,
                    last_summary TEXT, updated REAL);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def define(self, trigger, instructions, title='', state='active'):
        trigger = safe_text(trigger).strip()[:120]
        instructions = self._instructions(instructions)
        if not trigger or not instructions:
            raise ValueError('工作流需要触发词和步骤')
        if state not in ('active','candidate','disabled'):raise ValueError('未知工作流状态')
        ident = 'custom-'+uuid.uuid4().hex[:12]
        with self.connect() as db:
            db.execute('INSERT INTO workflows VALUES (?,?,?,?,?,?,?,?,?)',
                (ident, title.strip()[:120] or trigger, trigger, instructions,
                 state, 0, 0, '', time.time()))
        return self.get(ident)

    @staticmethod
    def _instructions(value):
        text=str(value or '').strip()
        if len(text)>32000:raise ValueError('工作流最多 32000 字，请分成可检查的阶段')
        if any(safe_text(text[i:i+8000])!=text[i:i+8000].strip() for i in range(0,len(text),8000)):
            raise ValueError('工作流中不能包含密钥，请使用配置引用')
        return text

    def update(self,ident,trigger,instructions,title=''):
        self.get(ident)
        trigger=safe_text(trigger).strip()[:120]
        instructions=self._instructions(instructions)
        if not trigger or not instructions:raise ValueError('工作流需要触发词和步骤')
        with self.connect() as db:
            db.execute('UPDATE workflows SET title=?,trigger=?,instructions=?,updated=? WHERE id=?',
                       (safe_text(title)[:120] or trigger,trigger,instructions,time.time(),ident))
        return self.get(ident)

    def get(self, ident):
        with self.connect() as db:
            row = db.execute('SELECT * FROM workflows WHERE id=?', (ident,)).fetchone()
        if not row:
            raise ValueError('工作流不存在')
        return dict(row)

    def match(self, text):
        value = str(text or '').strip()
        if not value:
            return None
        with self.connect() as db:
            rows = db.execute("SELECT * FROM workflows WHERE state='active' ORDER BY updated DESC")
            for row in rows:
                if row['trigger'] in value:
                    return dict(row)
        return None

    def list(self, include_builtins=True):
        rows = []
        if include_builtins:
            from core.daily_workflows import BUILTINS
            rows.extend({**flow, 'builtin': True} for flow in BUILTINS)
        with self.connect() as db:
            rows.extend(dict(row, builtin=False) for row in db.execute(
                'SELECT * FROM workflows ORDER BY updated DESC'))
        return rows

    def disable(self, ident):
        with self.connect() as db:
            if db.execute("UPDATE workflows SET state='disabled',updated=? WHERE id=?",
                          (time.time(), ident)).rowcount != 1:
                raise ValueError('工作流不存在')
        return True

    def activate(self, ident):
        with self.connect() as db:
            if db.execute("UPDATE workflows SET state='active',updated=? WHERE id=?",
                          (time.time(), ident)).rowcount != 1:
                raise ValueError('工作流不存在')
        return self.get(ident)

    def record_run(self, workflow, ok, summary=''):
        workflow = dict(workflow or {})
        ident = workflow.get('id')
        if not ident:
            return
        if workflow.get('builtin'):
            return
        summary = safe_text(summary)[:1200]
        with self.connect() as db:
            column = 'success' if ok else 'failure'
            db.execute(f'UPDATE workflows SET {column}={column}+1,last_summary=?,updated=? WHERE id=?',
                       (summary, time.time(), ident))
            db.execute('INSERT INTO runs VALUES (?,?,?,?,?)',
                       (uuid.uuid4().hex, ident, int(bool(ok)), summary, time.time()))

    def observe_local_success(self, goal, summary='', tools=()):
        """Promote repeated successful jobs to a reviewable local recipe."""
        goal = safe_text(goal).strip()[:2000]
        if not goal:
            return None
        key = ' '.join(goal.casefold().split())[:300]
        now = time.time()
        with self.connect() as db:
            old = db.execute('SELECT * FROM goal_stats WHERE goal_key=?', (key,)).fetchone()
            count = int(old['successes']) + 1 if old else 1
            db.execute('''INSERT INTO goal_stats VALUES (?,?,?,?,?,?)
                ON CONFLICT(goal_key) DO UPDATE SET successes=successes+1,last_summary=excluded.last_summary,updated=excluded.updated''',
                (key, goal, 1, 0, safe_text(summary)[:1000], now))
            existing = db.execute("SELECT id FROM workflows WHERE state!='disabled' AND trigger=?",
                                  (goal[:120],)).fetchone()
        if count < 2 or existing:
            return None
        unique_tools = [str(x) for x in tools if x][-20:]
        instructions = ('这是同类任务连续成功后的候选流程。执行前重新观察当前界面，不重放旧的点击。\n'
                        '目标：' + goal + '\n最近成功摘要：' + safe_text(summary)[:1200] +
                        ('\n已验证过的工具顺序：' + ' → '.join(unique_tools) if unique_tools else ''))
        return self.define(goal[:120], instructions, title='候选：'+goal[:80], state='candidate')
