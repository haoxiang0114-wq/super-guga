"""Read-only discovery of known local agent installations and conversations."""
from dataclasses import dataclass
import shutil
import socket

from core.codex_gateway import CodexError, NativePipe, task_facts


@dataclass(frozen=True)
class LocalAgent:
    key: str
    name: str
    status: str
    can_browse: bool = False


@dataclass(frozen=True)
class AgentConversation:
    backend: str
    external_id: str
    title: str
    host_id: str = 'local'
    running: bool = False


def _running_names():
    try:
        import psutil
    except ImportError:
        return set()
    return {str(process.info.get('name') or '').casefold()
            for process in psutil.process_iter(['name'])}


def _dsh_listening():
    try:
        with socket.create_connection(('127.0.0.1', 3080), timeout=.35):
            return True
    except OSError:
        return False


def discover_agents():
    running = _running_names()
    agents = []
    try:
        codex_ready = bool(NativePipe().discover())
        codex_status = '桌面通道在线'
    except (CodexError, OSError) as exc:
        codex_ready = False
        codex_status = ('有多个桌面实例，请只留一个' if '多个' in str(exc)
                        else '已安装 · 请打开 Codex' if shutil.which('codex') or 'codex.exe' in running
                        else '未检测到')
    agents.append(LocalAgent('codex', 'Codex', codex_status, codex_ready))

    dsh_ready = _dsh_listening()
    agents.append(LocalAgent('dsh', 'DSH', '本机服务在线' if dsh_ready else
                            '已安装 · 请启动 DSH Web' if shutil.which('dsh') or shutil.which('dsh.ps1')
                            else '未检测到', dsh_ready))
    for command, name, processes in (
        ('workbuddy', 'WorkBuddy', {'workbuddy.exe'}),
        ('claude', 'Claude Code', {'claude.exe'}),
        ('cursor', 'Cursor', {'cursor.exe'}),
        ('opencode', 'OpenCode', {'opencode.exe', 'opencode'}),
    ):
        installed = bool(running & processes or shutil.which(command) or shutil.which(command + '.ps1'))
        if installed:
            agents.append(LocalAgent(command, name, '已检测到 · 待接入会话接口'))
    return agents


def list_conversations(backend, codex=None, dsh=None):
    if backend == 'codex':
        if codex is None:
            from core.codex_gateway import CodexGateway
            codex = CodexGateway()
        result = codex.call('list_threads', dict(mode='history', source='codex', hostId='local', limit=30))
        conversations = []
        for row in result.get('threads', [])[:30]:
            ident = str(row.get('threadId') or row.get('id') or '')
            status = row.get('status') or ''
            if isinstance(status, dict):
                status = status.get('type', '')
            if ident:
                conversations.append(AgentConversation('codex', ident, str(row.get('title') or '未命名对话'),
                    str(row.get('hostId') or 'local'), status in ('active', 'running', 'inProgress')))
        return conversations
    if backend == 'dsh':
        from core.super_agent.dsh_gateway import DshGateway, session_title
        client = dsh if dsh is not None else DshGateway(timeout=5)
        return [AgentConversation('dsh', ident, session_title(row) or '未命名对话',
                                 running=bool(row.get('running')))
                for row in client.session_summaries()[:30]
                if (ident := str(row.get('sessionId') or row.get('id') or ''))]
    raise ValueError('此智能体尚未接入会话接口')


def verify_conversation(conversation, codex=None, dsh=None):
    if conversation.backend == 'codex':
        if codex is None:
            from core.codex_gateway import CodexGateway
            codex = CodexGateway()
        facts = task_facts(codex.read(conversation.external_id, conversation.host_id))
        if facts['thread_id'] != conversation.external_id:
            raise ValueError('Codex 未确认选中的对话编号')
        return facts['state']
    if conversation.backend == 'dsh':
        if dsh is None:
            from core.super_agent.dsh_gateway import DshGateway
            dsh = DshGateway(timeout=5)
        for row in dsh.session_summaries():
            if str(row.get('sessionId') or row.get('id') or '') == conversation.external_id:
                return 'running' if row.get('running') else 'ready'
        raise ValueError('DSH 中已找不到这条对话')
    raise ValueError('此智能体尚未接入会话接口')
