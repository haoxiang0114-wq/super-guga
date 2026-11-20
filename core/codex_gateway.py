"""Local desktop Codex app tools, without launching another model runtime.

The native app-tools pipe is private and version dependent. Discover its tool
schema at connection time; never patch the installed app or read login tokens.
The controller is a real, explicitly registered Codex conversation/turn.
"""
from __future__ import annotations

import json
import os
import re
import struct
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'runtime' / 'codex'
VERSION = '2026-11-13.codex-desktop.1'
MAX_FRAME = 8 * 1024 * 1024
ALLOWED_TOOLS = frozenset(('create_thread', 'send_message_to_thread', 'read_thread',
                          'wait_threads', 'navigate_to_codex_page', 'list_projects', 'list_threads',
                          'create_sidebar_section','move_thread_to_sidebar_section'))


class CodexError(RuntimeError):
    pass


class DeliveryUnknown(CodexError):
    """A write may already have reached the app; never resend automatically."""


def clean_error(value):
    text = re.sub(r'(?i)(bearer\s+|(?:token|api[_-]?key|password)[=:]\s*)[^\s,;]+',
                  r'\1[redacted]', str(value))
    return text[:600]


class NativePipe:
    """One bounded request per connection. No credentials or persistent handles."""

    def __init__(self, pipe_name=''):
        self.pipe_name = pipe_name

    def discover(self):
        if os.name != 'nt':
            raise CodexError('当前 Codex 桌面通道仅支持 Windows')
        names = [name for name in os.listdir(r'\\.\pipe')
                 if re.fullmatch(r'codex-app-tools-[a-zA-Z0-9-]+', name)]
        if self.pipe_name:
            if self.pipe_name not in names:
                raise CodexError('指定的 Codex 桌面窗口已关闭，请重新连接')
            return self.pipe_name
        if not names:
            raise CodexError('Codex 桌面应用尚未打开，请先打开应用')
        if len(names) != 1:
            raise CodexError('检测到多个 Codex 桌面实例，请先关闭多余实例或指定通道')
        return names[0]

    def request(self, method, params, timeout=20, mutation=False):
        import win32file
        import win32pipe
        name = self.discover()
        data = json.dumps(dict(jsonrpc='2.0', id=uuid.uuid4().hex,
                               method=method, params=params), ensure_ascii=False).encode('utf-8')
        if len(data) > MAX_FRAME:
            raise CodexError('任务内容超过 Codex 桌面通道的大小限制')
        handle = None
        sent = False
        try:
            handle = win32file.CreateFile('\\\\.\\pipe\\' + name,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE, 0, None,
                win32file.OPEN_EXISTING, 0, None)
            win32file.WriteFile(handle, struct.pack('<I', len(data)) + data)
            sent = True
            buffer = bytearray()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                _, available, _ = win32pipe.PeekNamedPipe(handle, 0)
                if available:
                    buffer.extend(win32file.ReadFile(handle, min(available, 65536))[1])
                if len(buffer) >= 4:
                    length = struct.unpack('<I', buffer[:4])[0]
                    if length > MAX_FRAME:
                        raise CodexError('Codex 桌面响应超过大小限制')
                    if len(buffer) >= length + 4:
                        response = json.loads(buffer[4:length + 4])
                        if response.get('error'):
                            raise CodexError(clean_error(response['error'].get('message', '桌面接口返回错误')))
                        return response.get('result', {})
                time.sleep(.025)
            raise TimeoutError('Codex 桌面通道响应超时')
        except CodexError:
            raise
        except Exception as exc:
            if mutation and sent:
                raise DeliveryUnknown('投递结果未确认，任务可能已送达；请先在 Codex 核对，勿重复派发') from exc
            raise CodexError(clean_error(exc)) from exc
        finally:
            if handle is not None:
                win32file.CloseHandle(handle)


class CodexGateway:
    def __init__(self, transport=None, context_path=None):
        self.transport = transport or NativePipe()
        self.context_path = Path(context_path or RUNTIME / 'controller.json')
        self._schemas = None
        self._compact_snapshots = True
        self._lock = threading.RLock()

    def configure(self, thread_id, turn_id, host_id='local'):
        if not thread_id or not turn_id:
            raise CodexError('连接需要真实的 Codex 对话和回合编号')
        self.context_path.parent.mkdir(parents=True, exist_ok=True)
        self.context_path.write_text(json.dumps(dict(thread_id=thread_id, turn_id=turn_id,
            host_id=host_id), ensure_ascii=False, indent=2), encoding='utf-8')

    def context(self):
        try:
            context = json.loads(self.context_path.read_text(encoding='utf-8'))
            if not context.get('thread_id') or not context.get('turn_id'):
                raise ValueError('missing context')
            return context
        except (OSError, ValueError, TypeError) as exc:
            raise CodexError('咕嘎尚未绑定 Codex 分发对话，请先完成桌面通道连接') from exc

    def schemas(self):
        with self._lock:
            if self._schemas is None:
                result = self.transport.request('tools/list', dict(threadStartKind='all'))
                self._schemas = {item['name']: item for item in result.get('tools', [])}
            return self._schemas

    def call(self, tool, arguments):
        if tool not in ALLOWED_TOOLS:
            raise CodexError('不支持的 Codex 任务操作')
        if tool not in self.schemas():
            self._schemas = None
            raise CodexError('当前 Codex 版本没有提供 ' + tool + '，请核对桌面版本')
        context = self.context()
        result = self.transport.request('tools/call', dict(callerSource='codex',
            callId='guga-' + uuid.uuid4().hex, namespace='codex_app',
            threadId=context['thread_id'], turnId=context['turn_id'],
            hostId=context.get('host_id') or 'local', tool=tool, arguments=arguments),
            timeout=35 if tool == 'create_thread' else 20,
            mutation=tool in ('create_thread', 'send_message_to_thread'))
        blocks = result.get('contentItems', result.get('content', []))
        texts = [item.get('text', '') for item in blocks if isinstance(item, dict) and 'text' in item]
        value = '\n'.join(texts)
        if result.get('success') is False or result.get('isError'):
            raise CodexError(clean_error(value or 'Codex 未受理本次操作'))
        try:
            return json.loads(value) if value else result
        except ValueError:
            return dict(message=value)

    def create_task(self, text, cwd=''):
        if isinstance(self.transport,NativePipe):
            try:self.transport.discover()
            except CodexError as exc:
                if '尚未打开' not in str(exc):raise
                import winreg
                try:
                    with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,r'codex\shell\open\command'):pass
                except OSError:raise CodexError('没有找到Codex桌面应用的启动入口，请先安装或打开应用') from None
                os.startfile('codex://')
                deadline=time.monotonic()+12
                while True:
                    try:self.transport.discover();break
                    except CodexError:
                        if time.monotonic()>=deadline:raise CodexError('Codex启动请求已发送，桌面通道尚未就绪；没有重复启动')
                        time.sleep(.25)
        text = str(text or '').strip()
        if not text or len(text) > 16000:
            raise CodexError('Codex 任务需要 1—16000 个字符')
        target = dict(type='projectless')
        if cwd:
            directory = Path(cwd).expanduser()
            if not directory.is_absolute() or not directory.is_dir():
                raise CodexError('项目目录必须是存在的绝对路径')
            wanted = os.path.normcase(os.path.normpath(str(directory.resolve())))
            projects = self.call('list_projects', {}).get('projects', [])
            matches = [p for p in projects if p.get('projectKind') == 'local' and
                       os.path.normcase(os.path.normpath(p.get('path') or '')) == wanted]
            if not matches:
                raise CodexError('这个目录尚未加入 Codex 项目，请先在 Codex 中添加该项目后再派发')
            target = dict(type='project', projectId=matches[0]['projectId'], environment=dict(type='local'))
        prompt = ('这是由咕嘎转交的任务。咕嘎负责分发、监测和向用户汇报，你负责执行。'
                  '请按原要求完成，每个可验证阶段简短说明实际进展；遇到需要用户处理的情况明确说明。'
                  '完成时说明结果及验证情况。\n\n用户任务：\n' + text)
        created = self.call('create_thread', dict(prompt=prompt, title='咕嘎 · ' + text.splitlines()[0][:55], target=target))
        if not created.get('threadId'):
            # Do not silently turn an asynchronous setup id into a valid thread.
            raise DeliveryUnknown('Codex 已开始创建任务，但尚未返回正式对话编号；请在应用内核对，勿重复派发')
        # Grouping happens after creation: a grouping failure must never replay
        # create_thread or lose the thread that already exists.
        try:
            if 'create_sidebar_section' in self.schemas():
                sections=self.call('list_threads',{}).get('sections',[])
                section=next((s for s in sections if s.get('name')=='咕嘎'),None)
                if section is None:section=self.call('create_sidebar_section',dict(name='咕嘎'))
                section_id=section.get('sectionId') or (section.get('section') or {}).get('sectionId')
                if not section_id:raise CodexError('创建分区未返回编号')
                self.call('move_thread_to_sidebar_section',dict(threadId=created['threadId'],sectionId=section_id))
                created['gugaSectionId']=section_id
        except Exception as exc:created['groupingWarning']=clean_error(exc)
        return created

    def messages(self,thread_id,host_id='local'):
        value=self.call('read_thread',dict(threadId=thread_id,hostId=host_id,turnLimit=10,
            includeOutputs=False,maxOutputCharsPerItem=18000))
        rows=[]
        for turn in reversed(value.get('turns') or []):
            for index,item in enumerate(turn.get('items') or []):
                kind=item.get('type')
                if kind=='userMessage':
                    content=item.get('content') or []
                    text='\n'.join(str(x.get('text') or '') for x in content if isinstance(x,dict)) if isinstance(content,list) else str(content)
                    role='user'
                elif kind=='agentMessage' and item.get('text'):
                    text=str(item['text']);role='assistant'
                else:continue
                rows.append(dict(id=str(item.get('id') or str(turn.get('id'))+'-'+str(index)),role=role,
                    text=text,turn_id=turn.get('id'),phase=item.get('phase')))
        return rows

    def send(self, thread_id, text, host_id='local'):
        if not str(text or '').strip() or len(text) > 16000:
            raise CodexError('追加要求需要 1—16000 个字符')
        return self.call('send_message_to_thread', dict(threadId=thread_id, hostId=host_id, prompt=text))

    def snapshot(self, thread_id, host_id='local', cursor=''):
        target = dict(threadId=thread_id, hostId=host_id)
        if cursor:
            target['afterCursor'] = cursor
        if self._compact_snapshots:
            try:
                return self.call('wait_threads', dict(targets=[target], timeoutMs=0))
            except CodexError:
                # Desktop wait subscriptions need a live calling turn in some
                # versions. A completed controller can still read actual turns.
                # Switch once; never repeatedly retry the rejected subscription.
                self._compact_snapshots = False
        return dict(readResult=self.read(thread_id, host_id), mode='read_thread')

    def read(self, thread_id, host_id='local'):
        return self.call('read_thread', dict(threadId=thread_id, hostId=host_id,
            turnLimit=1, includeOutputs=False, maxOutputCharsPerItem=2000))

    def open(self, thread_id):
        return self.call('navigate_to_codex_page', dict(threadId=thread_id))


def task_facts(read_result):
    """Only explicit task/turn states establish completion, never a quiet screen."""
    thread = read_result.get('thread') or {}
    turns = read_result.get('turns') or []
    turn = turns[0] if turns else {}
    raw = turn.get('status') or ''
    status = thread.get('status') or {}
    thread_state = status.get('type', '') if isinstance(status, dict) else str(status)
    flags = status.get('activeFlags', []) if isinstance(status, dict) else []
    state = {'completed': 'completed', 'failed': 'failed', 'interrupted': 'stopped',
             'inProgress': 'running', 'in_progress': 'running'}.get(raw, 'unknown')
    if thread_state == 'active' and state != 'running':
        state = 'running'  # the newest turn may not yet be present in this page
    if any(flag in ('waitingOnApproval', 'waitingOnUserInput', 'waitingForApproval', 'waitingForUserInput') for flag in flags):
        state = 'needs_input'
    commentary, final, activity = '', '', ''
    for item in turn.get('items') or []:
        kind = item.get('type')
        if kind == 'agentMessage':
            if item.get('phase') in ('final_answer', 'final'):
                final = str(item.get('text') or '')
            elif item.get('text'):
                commentary = str(item['text'])
        elif kind == 'commandExecution' and item.get('status') == 'inProgress':
            activity = '正在运行命令或检查'
        elif kind == 'fileChange' and item.get('status') == 'inProgress':
            activity = '正在修改文件'
    error = turn.get('error')
    if isinstance(error, dict):
        error = error.get('message') or str(error)
    return dict(thread_id=thread.get('id', ''), turn_id=turn.get('id', ''), state=state,
                summary=commentary or activity, text=final, error=clean_error(error or ''),
                turn_status=raw,validation_receipts=[dict(type='commandExecution',exit_code=item.get('exitCode'),
                    command=str(item.get('command') or '')[:500]) for item in turn.get('items') or []
                    if item.get('type')=='commandExecution' and item.get('status')=='completed'])
