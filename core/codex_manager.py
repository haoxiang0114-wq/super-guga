"""Guga distributes desktop Codex tasks and observes only its own threads."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from core.codex_gateway import CodexGateway, CodexError, DeliveryUnknown, RUNTIME, VERSION, clean_error, task_facts

TERMINAL = frozenset(('completed', 'failed', 'stopped'))
WATCHING = frozenset(('running', 'starting', 'needs_input', 'unknown', 'stop_requested'))


def snapshot_facts(result):
    if isinstance(result.get('readResult'), dict):
        return dict(task_facts(result['readResult']), cursor='', changed=True)
    polls = result.get('polls') or []
    if not polls:
        raise CodexError('Codex 没有返回任务快照')
    row = polls[0]
    turn = row.get('latestTurn') or {}
    message = row.get('latestAssistantMessage') or {}
    item = dict(type='agentMessage', text=message.get('text', '') if message.get('turnId') == turn.get('id') else '',
                phase=message.get('phase'))
    facts = task_facts(dict(thread=row.get('thread') or {}, turns=[dict(turn, items=[item])]))
    marker = row.get('latestToolMarker') or {}
    if not facts['summary'] and facts['state'] == 'running':
        kind = str(marker.get('type') or '')
        facts['summary'] = {'commandExecution': '正在运行命令或检查',
                            'fileChange': '正在修改文件', 'webSearch': '正在查资料'}.get(kind, '')
    facts['cursor'] = row.get('cursor') or ''
    facts['changed'] = row.get('changed', True)
    return facts


class CodexManager(QObject):
    update = pyqtSignal(dict)

    def __init__(self, parent=None, gateway=None, path=None, start_timer=True):
        super().__init__(parent)
        self.gateway = gateway or CodexGateway()
        self.path = Path(path or RUNTIME / 'tasks.sqlite3')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._task_locks = {}
        self._inflight = set()
        self._monitoring = False
        self._closed = False
        with self._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS codex_tasks ('
                       'job_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, '
                       'created REAL NOT NULL, data TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS codex_conversation ON codex_tasks(conversation_id,created)')
            # An interrupted write is ambiguous, never automatically replayed.
            for (payload,) in db.execute('SELECT data FROM codex_tasks').fetchall():
                record = json.loads(payload)
                if record.get('state') == 'dispatching':
                    record.update(state='delivery_unknown', error='上次派发中断，请先在 Codex 核对，勿重复派发')
                    db.execute('UPDATE codex_tasks SET data=? WHERE job_id=?',
                               (json.dumps(record, ensure_ascii=False), record['job_id']))
        self.timer = QTimer(self)
        self.timer.setInterval(12000)
        self.timer.timeout.connect(self._schedule_monitor)
        if start_timer:
            self.timer.start()
            QTimer.singleShot(1500, self._schedule_monitor)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _task_lock(self, key):
        with self._lock:
            return self._task_locks.setdefault(key, threading.RLock())

    def _save(self, record):
        with self._lock, self._connect() as db:
            record['updated'] = time.time()
            db.execute('INSERT INTO codex_tasks VALUES (?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET data=excluded.data',
                       (record['job_id'], record['conversation_id'], record['created'],
                        json.dumps(record, ensure_ascii=False)))

    def list(self):
        with self._lock, self._connect() as db:
            return [json.loads(row[0]) for row in db.execute(
                'SELECT data FROM codex_tasks ORDER BY created DESC LIMIT 50')]

    def bound(self, conversation_id):
        with self._lock, self._connect() as db:
            row = db.execute('SELECT data FROM codex_tasks WHERE conversation_id=? ORDER BY created DESC LIMIT 1',
                             (str(conversation_id),)).fetchone()
            return json.loads(row[0]) if row else {}

    def conversation_for_thread(self, thread_id, host_id):
        with self._lock, self._connect() as db:
            for payload, in db.execute('SELECT data FROM codex_tasks ORDER BY created DESC'):
                row = json.loads(payload)
                if thread_id in (row.get('thread_id'), row.get('plan_thread_id')) and (row.get('host_id') or 'local') == host_id:
                    return row['conversation_id']
        return ''

    def bind_thread(self, conversation_id, agent_id, thread_id, host_id, state):
        """Persist a verified existing thread without sending a prompt."""
        with self._lock:
            existing = self.conversation_for_thread(thread_id, host_id)
            if existing:
                return existing
            if self.bound(conversation_id):
                raise CodexError('此咕嘎对话已有 Codex 绑定')
            self._save(dict(job_id=uuid.uuid4().hex, conversation_id=conversation_id, agent_id=agent_id,
                thread_id=thread_id, host_id=host_id, state=state, created=time.time(),
                summary='已绑定现有 Codex 对话', text='', goal='', watch_started=time.time()))
            return conversation_id

    def implement_plan(self, plan, prompt, workspace):
        from core.codex_project_execution import implement_project

        cid = plan.conversation_id
        workspace = Path(workspace)
        with self._lock:
            record = self.bound(cid)
            if record.get('thread_id') != plan.thread_id or record.get('write_uncertain'):
                raise CodexError('原方案会话绑定已变化或存在未核对的投递')

        def dispatch():
            record.update(state='dispatching', goal=plan.goal, cwd=str(workspace), error='', text='',
                          plan_thread_id=plan.thread_id)
            self._save(record)

            def notify(method, parameters):
                if method == 'guga/implementationThread':
                    record['thread_id'] = parameters['threadId']
                    self._save(record)
                elif method == 'turn/started':
                    record.update(state='running', summary='Codex 正在独立开发会话实施已批准的方案')
                    self._save(record)
                    self._emit('codex_started', record)
                elif method == 'item/completed':
                    item = parameters.get('item') or {}
                    stage = {'fileChange': '原型或资源已更新', 'commandExecution': '验证命令已返回'}.get(item.get('type'))
                    if stage:
                        record['summary'] = stage
                        self._save(record)
                        self._emit('codex_progress', record)

            try:
                facts = task_facts(implement_project(plan.thread_id, prompt, workspace, on_event=notify))
            except Exception as exc:
                # An interrupted worker may have left files behind. Keep the
                # approval and original thread, and let the user inspect it.
                record.update(state='delivery_unknown', error=clean_error(exc))
                self._save(record)
                self._emit('codex_failed', record)
                return
            if facts.get('state') == 'completed' and not any(workspace.iterdir()):
                facts.update(state='failed', error='Codex 已回复，项目目录没有出现成果文件')
            record.update(facts, delivered_turn_id=facts.get('turn_id', ''))
            self._save(record)
            self._emit('codex_completed' if record['state'] == 'completed' else 'codex_failed', record)

        return self._launch(cid, dispatch)

    def _emit(self, kind, record, **extra):
        if not self._closed:
            self.update.emit(dict(type=kind, **{k: record.get(k, '') for k in (
                'conversation_id', 'agent_id', 'thread_id', 'host_id', 'job_id', 'turn_id', 'state',
                'summary', 'text', 'error','validation_receipts')}, **extra))

    def _launch(self, key, action):
        with self._lock:
            if self._closed:
                raise CodexError('咕嘎正在关闭，未投递任务')
            if key in self._inflight:
                raise CodexError('这条 Codex 消息正在投递，请稍候，勿重复发送')
            self._inflight.add(key)

        def work():
            try:
                with self._task_lock(key):
                    action()
            except Exception as exc:
                record = self.bound(key)
                if record:
                    record.update(error=clean_error(exc))
                    if record.get('state') == 'dispatching':
                        record['state'] = 'delivery_unknown' if isinstance(exc, DeliveryUnknown) else 'failed'
                    self._save(record)
                else:
                    record = dict(conversation_id=key, error=clean_error(exc), state='failed')
                self._emit('codex_failed', record)
            finally:
                with self._lock:
                    self._inflight.discard(key)
        threading.Thread(target=work, name='guga-codex-dispatch', daemon=True).start()
        return dict(ok=True, accepted=True, message='正在向 Codex 桌面对话投递，受理和完成以回执为准')

    def start(self, text, conversation_id, agent_id='001', cwd='', new=False):
        if not str(text or '').strip() or len(text) > 16000:
            raise CodexError('Codex 任务需要 1—16000 个字符')
        if not conversation_id:
            raise CodexError('Codex 任务需要所属咕嘎对话')
        conversation_id = str(conversation_id)
        # Check the binding and reserve dispatch atomically. Do not wait on
        # the task lock here: its worker may be doing a slow desktop request.
        with self._lock:
            return self._start_locked(text, conversation_id, agent_id, cwd, new)

    def _start_locked(self, text, conversation_id, agent_id, cwd, new):
        previous = self.bound(conversation_id)
        if previous:
            if previous.get('state') == 'delivery_unknown' or previous.get('write_uncertain'):
                raise CodexError(previous.get('error') or '请先核对上次派发结果')
            if previous.get('thread_id'):
                if new:
                    raise CodexError('当前咕嘎对话已有 Codex 绑定；需要独立任务时请新建咕嘎对话')
                return self.send(text, conversation_id, agent_id)
        if previous and previous.get('state') in WATCHING | {'dispatching', 'delivery_unknown'}:
            raise CodexError('本对话已有待处理 Codex 任务，请另开咕嘎对话派发新任务')
        record = dict(job_id=uuid.uuid4().hex, conversation_id=str(conversation_id),
            agent_id=str(agent_id), thread_id='', host_id='local', state='dispatching',
            goal=text, cwd=cwd, created=time.time(), summary='', text='', error='', cursor='',
            failures=0, last_report=0, delivered_turn_id='')

        def dispatch():
            self._save(record)
            created = self.gateway.create_task(text, cwd)
            record.update(thread_id=created['threadId'], host_id=created.get('hostId') or 'local',
                          state='starting', summary='Codex 已接收任务，正在执行', watch_started=time.time())
            self._save(record)
            self._emit('codex_started', record)
            try:
                self.gateway.open(record['thread_id'])
            except Exception as exc:
                # Creation already succeeded; opening a window must never resend it.
                record['open_error'] = clean_error(exc)
                self._save(record)
            self._refresh(record)
        return self._launch(str(conversation_id), dispatch)

    def send(self, text, conversation_id, agent_id='001', stop=False):
        record = self.bound(conversation_id)
        if not record.get('thread_id'):
            raise CodexError('当前咕嘎对话尚未绑定 Codex 任务')
        if not str(text or '').strip() or len(text) > 16000:
            raise CodexError('追加要求需要 1—16000 个字符')
        if record.get('state') == 'delivery_unknown' or record.get('write_uncertain'):
            raise CodexError('上次消息是否送达还未确认，请先在 Codex 对话核对')

        def send():
            record = self.bound(conversation_id)
            try:
                self.gateway.send(record['thread_id'], text, record.get('host_id') or 'local')
            except DeliveryUnknown:
                record['write_uncertain'] = True
                self._save(record)
                raise
            record.update(state='stop_requested' if stop else 'starting',
                summary='已发送停止要求，等待 Codex 确认' if stop else '追加要求已送到原 Codex 对话',
                text='', error='', cursor='', failures=0, watch_started=time.time(), last_report=0)
            self._save(record)
            self._emit('codex_progress', record)
        return self._launch(str(conversation_id), send)

    def execute(self, args, conversation_id='', agent_id='001'):
        action = str(args.get('action') or 'status')
        record = self.bound(conversation_id)
        target = str(args.get('thread_id') or '')
        if target and target != record.get('thread_id'):
            raise CodexError('只能操作当前咕嘎对话亲自派发的 Codex 任务')
        if action == 'list':
            return dict(ok=True, tasks=[r for r in self.list() if r.get('agent_id') == str(agent_id)])
        if action == 'status':
            if record.get('thread_id'):
                self._schedule_monitor(conversation_id)
            return dict(ok=True, task=record, monitoring=not self._closed, version=VERSION)
        if action == 'start':
            return self.start(args.get('text') or args.get('task', ''), conversation_id, agent_id,
                              args.get('cwd') or '', bool(args.get('new')))
        if action == 'send':
            return self.send(args.get('text') or '', conversation_id, agent_id)
        if action == 'stop':
            return self.send('请立即停止当前任务，不再启动新的操作。简短说明已经停止或仍在收尾的真实情况。',
                             conversation_id, agent_id, stop=True)
        if action == 'open':
            if not record.get('thread_id'):
                raise CodexError('当前咕嘎对话尚未绑定 Codex 任务')
            return self._launch(str(conversation_id), lambda: self.gateway.open(record['thread_id']))
        raise CodexError('未知 Codex 任务操作')

    def _schedule_monitor(self, conversation_id=''):
        with self._lock:
            if self._closed or self._monitoring:
                return
            self._monitoring = True

        def monitor():
            try:
                rows = [self.bound(conversation_id)] if conversation_id else self.list()
                latest = set()
                for record in rows:
                    cid = record.get('conversation_id')
                    if cid in latest:
                        continue
                    latest.add(cid)
                    if (record.get('thread_id') and
                            (record.get('state') in WATCHING or conversation_id) and
                            cid not in self._inflight):
                        self._refresh(record)
            finally:
                with self._lock:
                    self._monitoring = False
        threading.Thread(target=monitor, name='guga-codex-monitor', daemon=True).start()

    def _refresh(self, record):
        with self._task_lock(record['conversation_id']):
            latest = self.bound(record['conversation_id'])
            if latest.get('job_id') != record.get('job_id'):
                return
            self._refresh_locked(latest)

    def _refresh_locked(self, record):
        if self._closed:
            return
        previous = dict(record)
        try:
            snapshot = self.gateway.snapshot(record['thread_id'], record.get('host_id') or 'local', record.get('cursor') or '')
            facts = snapshot_facts(snapshot)
            # A pending follow-up may not have appeared in the snapshot yet.
            if (record.get('state') in ('starting', 'stop_requested') and
                    facts['turn_id'] == record.get('delivered_turn_id') and
                    facts['state'] in TERMINAL):
                return
            if facts['state'] == 'completed':
                details=task_facts(self.gateway.read(record['thread_id'], record.get('host_id') or 'local'))
                if details.get('turn_id')==facts.get('turn_id'):facts.update(details)
            for key in ('state', 'summary', 'text', 'error', 'cursor', 'turn_id','validation_receipts'):
                if key in facts:
                    record[key] = facts[key]
            record['failures'] = 0
            if record['state'] in TERMINAL:
                if facts['turn_id'] and facts['turn_id'] != record.get('delivered_turn_id'):
                    record['delivered_turn_id'] = facts['turn_id']
                    self._save(record)
                    self._emit('codex_completed' if record['state'] == 'completed' else 'codex_failed', record)
                    return
            elif record['state'] == 'needs_input' and previous.get('state') != 'needs_input':
                record['summary'] = record.get('summary') or 'Codex 正在等待你的确认或补充，请打开任务对话处理'
                record['last_report'] = time.time()
                self._save(record)
                self._emit('codex_progress', record)
                return
            elif record.get('summary') and record['summary'] != previous.get('reported_summary') and time.time() - record.get('last_report', 0) >= 60:
                record.update(last_report=time.time(), reported_summary=record['summary'])
                self._save(record)
                self._emit('codex_progress', record)
                return
            self._save(record)
        except Exception as exc:
            record.update(failures=int(record.get('failures') or 0) + 1, error=clean_error(exc))
            if record['failures'] >= 3:
                record.update(state='monitor_disconnected', summary='Codex 进度连接已断开，任务状态未知；查询进度可重新连接')
                self._emit('codex_failed', record)
            self._save(record)

    def close(self):
        self._closed = True
        self.timer.stop()
