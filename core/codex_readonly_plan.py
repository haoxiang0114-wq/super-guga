"""Produce a real design turn in a tool-disabled, read-only Codex session."""
import json
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time

from core.codex_gateway import CodexError, clean_error
from core.project_review import plan_request


class PlanInterrupted(CodexError):
    def __init__(self, message, thread_id):
        super().__init__(message)
        self.thread_id = thread_id


class _PlannerConnection:
    def __init__(self, process, deadline, on_event=None):
        self.process = process
        self.deadline = deadline
        self.inbox = queue.Queue()
        self.next_id = 0
        self.notifications = []
        self.on_event = on_event
        threading.Thread(target=self._read, name='guga-codex-plan-receipts', daemon=True).start()

    def _read(self):
        for line in self.process.stdout:
            try:
                self.inbox.put(json.loads(line))
            except (TypeError, ValueError):
                continue
        self.inbox.put(None)

    def send(self, message):
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + '\n')
        self.process.stdin.flush()

    def receive(self):
        try:
            row = self.inbox.get(timeout=max(0, self.deadline - time.monotonic()))
        except queue.Empty as exc:
            raise CodexError('只读方案达到等候上限。请先核对原任务，勿重复创建。') from exc
        if row is None:
            raise CodexError('Codex 方案会话已经断开')
        if 'id' in row and 'method' in row:
            # Never approve tool, input, or filesystem requests.
            self.send(dict(id=row['id'], error=dict(code=-32000, message='Read-only plan refuses tools')))
        elif self.on_event and 'method' in row:
            self.on_event(row['method'], row.get('params') or {})
        return row

    def call(self, method, params):
        self.next_id += 1
        request_id = self.next_id
        self.send(dict(id=request_id, method=method, params=params))
        while True:
            row = self.receive()
            if row.get('id') == request_id and 'method' not in row:
                if row.get('error'):
                    raise CodexError(clean_error(row['error'].get('message', 'Codex 未接受只读请求')))
                return row.get('result') or {}
            if 'method' in row:
                self.notifications.append(row)

    def wait_for_turn(self, thread_id, turn_id):
        def completed(row):
            params = row.get('params') or {}
            return (row.get('method') == 'turn/completed' and params.get('threadId') == thread_id
                    and (params.get('turn') or {}).get('id') == turn_id)
        if any(completed(row) for row in self.notifications):
            return
        while not completed(self.receive()):
            pass


def generate_codex_plan(goal, timeout=85, thread_id='', revision=''):
    executable = shutil.which('codex') or shutil.which('codex.exe')
    if not executable:
        raise CodexError('没有找到 Codex 命令，请先安装或在 Codex 计划模式里提交')
    command = [executable, 'app-server', '--listen', 'stdio://',
               '--disable', 'shell_tool', '--disable', 'unified_exec',
               '--disable', 'apps', '--disable', 'skill_mcp_dependency_install',
               '-c', 'mcp_servers={}', '-c', 'web_search=disabled']
    cwd = str(Path(tempfile.gettempdir()).resolve())
    process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, encoding='utf-8', errors='replace',
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        client = _PlannerConnection(process, time.monotonic() + timeout)
        client.call('initialize', dict(clientInfo=dict(name='guga-plan', title='Guga Read-only Plan',
                                                      version='2.2.0')))
        client.send(dict(method='initialized'))
        parameters = dict(cwd=cwd, sandbox='read-only', approvalPolicy='never',
            developerInstructions='仅产出方案正文。禁止任何工具调用、命令、写文件或发布。用中文简明输出。')
        if thread_id:
            parameters['threadId'] = thread_id
        result = client.call('thread/resume' if thread_id else 'thread/start', parameters)
        existing_id = str((result.get('thread') or {}).get('id') or '')
        # Do not send the prompt if the server chose any other permissions.
        actual = result.get('sandbox') or result.get('sandboxPolicy') or {}
        if isinstance(actual, dict):
            actual = actual.get('type')
        if (not existing_id or thread_id and existing_id != thread_id
                or actual not in ('readOnly', 'read-only') or result.get('approvalPolicy') != 'never'):
            raise CodexError('Codex 未确认只读权限，方案请求未发出')
        thread_id = existing_id
        prompt = plan_request(goal)
        if revision.strip():
            prompt += '\n\n修改要求：\n' + revision.strip()
        turn = client.call('turn/start', dict(threadId=thread_id, approvalPolicy='never',
            effort='low', sandboxPolicy=dict(type='readOnly', networkAccess=False),
            input=[dict(type='text', text=prompt, text_elements=[])]))
        turn_id = str((turn.get('turn') or {}).get('id') or '')
        if not turn_id:
            raise CodexError('Codex 未返回方案回合编号')
        client.wait_for_turn(thread_id, turn_id)
        snapshot = client.call('thread/read', dict(threadId=thread_id, includeTurns=True))
        thread = snapshot.get('thread') or {}
        turns = [item for item in thread.get('turns', []) if item.get('id') == turn_id]
        return thread_id, dict(thread=thread, turns=turns)
    except CodexError as exc:
        if thread_id:
            raise PlanInterrupted(str(exc), thread_id) from exc
        raise
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        process.stdin.close()
        process.stdout.close()
