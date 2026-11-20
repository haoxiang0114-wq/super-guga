# -*- coding: utf-8 -*-
"""个人超级智能体 · DSH 网关客户端（咕嘎 → 运行中的 DSH web 会话）。

为什么需要它
------------
咕嘎原本只能通过 `core/dsh_worker.py` 起一个**新的** headless DSH 任务；
要指挥「已经打开的 DeepSeek Harness 会话」，官方没有对外 MCP/CLI，但本机
DSH web 网关（默认 http://127.0.0.1:3080）暴露了 Typert RPC：

    POST /api/<method>    带 operator cookie（由 GET /connect?token=<launchToken> 换得）
    信封：{"type":"client-request","rpcId":"<uuid>","method":"<method>",
           "payload":{"args":{...}}}

已实测可用（只读）：`session/list`、`session/projections`。
写操作 `session/prompt`（mode=queue）可向会话投递一条用户消息。

安全约束（重要）
----------------
- launch token / operator cookie = 本机 owner 级凭据：**只驻留内存，绝不写日志、不写记忆、不落盘**；
- 本模块默认只读；`prompt()` 是显式写操作，必须由调用方给出目标 session_id；
- 不作为后台自动行为：只在用户/工具明确要求「指挥这个会话」时调用。
"""
from __future__ import annotations

import http.client
import json
import os
import re
import sqlite3
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from pathlib import Path

try:
    from . import dsh_slot
except ImportError:  # 以裸模块方式导入时（脚本/测试）
    import dsh_slot  # type: ignore

HOST = '127.0.0.1'
PORT = 3080
COOKIE_TTL_SECONDS = 600.0

# 咕嘎派出去的会话必须落在「完全权限」档：
# 该 preset = {sandbox: danger-full-access, approval: never}。
# 否则默认 workspace-write + approval:ask —— 任务一碰工作区外的文件就会停下来等人点同意，
# 而咕嘎这条链路本来就不该有人在场。DSH 的 session/create 没有权限参数，
# 所以建完立刻用 commands/execute 执行 `/permission <preset>`（不经过模型回合）。
PERMISSION_PRESET = 'danger-full-access'

# 咕嘎拉起的会话必须落在 DSH 的「咕嘎区」里，而不是散在 DSH 默认目录成为游离会话。
# DSH 的 session/create 支持 workspaceId（与 cwd 互斥），没有它就只是「无项目会话」。
GUGA_ROOT = Path(__file__).resolve().parents[2]
GUGA_WORKSPACE_TITLE = '咕嘎'
WORKSPACE_STORE = Path.home()/'.dsh'/'storages'/'workspace.json'


class GatewayError(RuntimeError):
    pass


# 只有「咕嘎亲自投递过指令」的会话才算它这一轮要盯的会话；
# 其余在跑的会话（主人的、其他队友的）与它无关，不该抢播报。
#
# 归属落盘（契约第 5 节）：进程内 list/dict 仍是快路径，SQLite 是重启后的真相来源。
# 没有落盘时，咕嘎一重启就忘了「哪些会话是我派出去的」，既接不回来也守不住播报边界。
_OWNED = []
_OWNED_LIMIT = 16
_OWNERS = {}
_OWNED_LOADED = False       # 本进程是否已从盘里播种过内存缓存
_PREPARED = set()           # 已建过 owned_sessions 表的库文件（绝对路径）
_LAST_STORE_ERROR = ''      # 最近一次落盘失败原因；仅诊断用，不含凭据

TASKS_DB = Path(__file__).resolve().parents[2]/'runtime/personal-harness/tasks.sqlite3'


def _store_path(store=None) -> Path:
    """store 允许三种形态：None=默认库、路径、带 .path 的 PersonalHarness 实例。"""
    if store is None:
        override = os.environ.get('GUGA_TASKS_DB', '').strip()
        return Path(override) if override else TASKS_DB
    path = getattr(store, 'path', None)
    return Path(path if path is not None else store)


@contextmanager
def _db(store=None):
    path = _store_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=5)
    db.row_factory = sqlite3.Row
    try:
        key = str(path.resolve())
        if key not in _PREPARED:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS owned_sessions ('
                           'session_id TEXT PRIMARY KEY, conversation_id TEXT, '
                           'bound_at REAL, source TEXT)')
            _PREPARED.add(key)
        yield db
    finally:
        db.close()


def _note_error(exc) -> None:
    global _LAST_STORE_ERROR
    _LAST_STORE_ERROR = _no_secret(str(exc))[:200]


def _upsert(session_id: str, conversation_id, source: str, store=None) -> bool:
    """写穿到 SQLite。写失败只记诊断，不打断内存快路径——一次磁盘故障不该让投递失败。"""
    try:
        with _db(store) as db, db:
            db.execute(
                'INSERT INTO owned_sessions(session_id, conversation_id, bound_at, source) '
                'VALUES (?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET '
                'conversation_id=COALESCE(excluded.conversation_id, owned_sessions.conversation_id), '
                'bound_at=excluded.bound_at, source=excluded.source',
                (session_id, conversation_id, time.time(), source))
        return True
    except (sqlite3.Error, OSError) as exc:
        _note_error(exc)
        return False


def _load_once(store=None) -> None:
    """本进程第一次读归属时从盘里播种最近 N 条；之后读全走内存。"""
    global _OWNED_LOADED
    if _OWNED_LOADED:
        return
    _OWNED_LOADED = True
    try:
        with _db(store) as db:
            rows = db.execute('SELECT session_id, conversation_id FROM owned_sessions '
                              'ORDER BY bound_at DESC LIMIT ?', (_OWNED_LIMIT,)).fetchall()
    except (sqlite3.Error, OSError) as exc:
        _note_error(exc)
        return
    for row in reversed(rows):      # 旧→新，保持 _OWNED 尾部最新的语义
        sid = str(row['session_id'] or '').strip()
        if not sid:
            continue
        if sid in _OWNED:
            _OWNED.remove(sid)
        _OWNED.append(sid)
        conversation_id = str(row['conversation_id'] or '').strip()
        if conversation_id:
            _OWNERS.setdefault(sid, conversation_id)
    while len(_OWNED) > _OWNED_LIMIT:      # 先写后读时内存里已有条目，保持上限不破
        _OWNED.pop(0)
    while len(_OWNERS) > _OWNED_LIMIT:
        _OWNERS.pop(next(iter(_OWNERS)))


def remember_owned(session_id: str, store=None) -> None:
    sid = str(session_id or '').strip()
    if not sid:
        return
    if sid in _OWNED:
        _OWNED.remove(sid)
    _OWNED.append(sid)
    while len(_OWNED) > _OWNED_LIMIT:
        _OWNED.pop(0)
    _upsert(sid, None, 'owned', store)


def owned_sessions(store=None) -> set:
    _load_once(store)
    return set(_OWNED)


# 咕嘎自己的对话 ↔ DSH 会话 的归属映射（用于"只盯自己拉起的那个对话"和事后验收取证）。
def remember_owner(session_id: str, conversation_id: str, store=None) -> None:
    session_id = str(session_id or '').strip()
    conversation_id = str(conversation_id or '').strip()
    if not session_id or not conversation_id:
        return
    _OWNERS[session_id] = conversation_id
    while len(_OWNERS) > _OWNED_LIMIT:
        _OWNERS.pop(next(iter(_OWNERS)))
    _upsert(session_id, conversation_id, 'owner', store)


def owner_of(session_id: str, store=None) -> str:
    sid = str(session_id or '').strip()
    if not sid:
        return ''
    _load_once(store)
    value = _OWNERS.get(sid, '')
    if value:
        return value
    # 内存没有就查一次盘（别的进程写过，或调用方清过内存缓存）。
    try:
        with _db(store) as db:
            row = db.execute('SELECT conversation_id FROM owned_sessions WHERE session_id=?',
                             (sid,)).fetchone()
    except (sqlite3.Error, OSError) as exc:
        _note_error(exc)
        return ''
    value = str((row['conversation_id'] if row else '') or '').strip()
    if value:
        _OWNERS[sid] = value
    return value


def forget_owned(session_id: str, store=None) -> None:
    """显式撤销归属（会话被关掉/不再由咕嘎负责）：内存与盘一起删。"""
    sid = str(session_id or '').strip()
    if not sid:
        return
    if sid in _OWNED:
        _OWNED.remove(sid)
    _OWNERS.pop(sid, None)
    try:
        with _db(store) as db, db:
            db.execute('DELETE FROM owned_sessions WHERE session_id=?', (sid,))
    except (sqlite3.Error, OSError) as exc:
        _note_error(exc)


def owned_store_error() -> str:
    """最近一次归属落盘失败的原因（空串=正常）；给诊断用，不含凭据。"""
    return _LAST_STORE_ERROR


def _no_secret(text: str) -> str:
    """错误信息里永远不带 cookie/token 原文。"""
    cleaned = re.sub(r'(dsh-auth[^=]*=)[^;,\s]+', r'\1<redacted>', str(text))
    cleaned = re.sub(r'(token=)[^\s&,;]+', r'\1<redacted>', cleaned, flags=re.I)
    return cleaned


def _norm_path(value) -> str:
    """比较目录用：统一分隔符与大小写，避免同一个目录认成两个项目。"""
    text = str(value or '').strip()
    if not text:
        return ''
    try:
        return os.path.normcase(os.path.normpath(text))
    except (OSError, ValueError):
        return text.lower()


def find_guga_workspace(path=None, store=None) -> str:
    """在 DSH 的项目存储里找指向咕嘎目录的项目 id；找不到返回空串（只读，不创建）。"""
    target = _norm_path(path or GUGA_ROOT)
    if not target:
        return ''
    try:
        data = json.loads(Path(store or WORKSPACE_STORE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return ''
    workspaces = ((data or {}).get('tables') or {}).get('workspaces') or {}
    if not isinstance(workspaces, dict):
        return ''
    for workspace_id, item in workspaces.items():
        if isinstance(item, dict) and _norm_path(item.get('path')) == target:
            return str(workspace_id)
    return ''


def discover_launch_token() -> str:
    """从 DSH 启动日志末行读取 launch token；只读，不缓存到磁盘。"""
    logs = Path.home() / '.dsh' / 'logs'
    if not logs.is_dir():
        return ''
    files = sorted(logs.glob('launch-*.log'), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in files[:4]:
        try:
            text = path.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
        for line in reversed(text.splitlines()):
            match = re.search(r'[?&]token=([A-Za-z0-9._~%-]{10,})', line)
            if match:
                return urllib.parse.unquote(match.group(1))
    return ''


class DshGateway:
    def __init__(self, host: str = HOST, port: int = PORT, timeout: float = 10.0):
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self._cookie = ''
        self._cookie_at = 0.0
        self._token = ''

    # ------------------------------------------------------------------ 鉴权
    def _connect(self, force: bool = False):
        if force:
            self._token = ''
            self._cookie = ''
        token = self._token or discover_launch_token()
        if not token:
            raise GatewayError('找不到本机 DSH launch token：请确认 `dsh web` 正在运行且 ~/.dsh/logs 有启动日志')
        self._token = token
        path = '/connect?token=' + urllib.parse.quote(token, safe='') + '&interactions=none'
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            conn.request('GET', path)
            response = conn.getresponse()
            response.read()
            cookie = ''
            for key, value in response.getheaders():
                if key.lower() != 'set-cookie':
                    continue
                pair = value.split(';', 1)[0].strip()
                if pair.lower().startswith('dsh-auth'):
                    cookie = pair
            if response.status not in (200, 301, 302, 303, 307, 308) or not cookie:
                raise GatewayError('DSH /connect 未返回 operator cookie（HTTP %s）' % response.status)
            self._cookie = cookie
            self._cookie_at = time.monotonic()
        finally:
            conn.close()

    def _ensure_cookie(self):
        if not self._cookie or time.monotonic() - self._cookie_at > COOKIE_TTL_SECONDS:
            self._connect()

    # ------------------------------------------------------------------ RPC
    def _post_once(self, method: str, args: dict, timeout: float | None) -> tuple:
        """发一次请求，返回 (HTTP 状态, 响应字节)。"""
        rpc_id = str(uuid.uuid4())
        body = json.dumps({
            'type': 'client-request',
            'rpcId': rpc_id,
            'method': method,
            'payload': {'args': args},
        }, ensure_ascii=False).encode('utf-8')
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout or self.timeout)
        try:
            conn.request('POST', '/api/' + method, body=body,
                         headers={'content-type': 'application/json', 'cookie': self._cookie})
            response = conn.getresponse()
            return rpc_id, response.status, response.read()
        finally:
            conn.close()

    def rpc(self, method: str, args: dict, timeout: float | None = None, _retry: bool = True) -> dict:
        self._ensure_cookie()
        rpc_id, status, raw = self._post_once(method, args, timeout)
        if status == 401 and _retry:
            # DSH 重启 / cookie 过期 / launch token 轮换：强制重新发现 token 并重试一次。
            self._connect(force=True)
            return self.rpc(method, args, timeout=timeout, _retry=False)
        if status == 401:
            raise GatewayError('DSH 网关拒绝了请求（401）：launch token 与 operator cookie 均已失效，且重试失败')
        try:
            data = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise GatewayError('DSH 网关返回非 JSON（HTTP %s）' % status)
        if data.get('type') != 'server-response':
            raise GatewayError('DSH 网关响应类型异常：%s' % _no_secret(data.get('type')))
        echoed = data.get('rpcId')
        if echoed and echoed != rpc_id:
            raise GatewayError('DSH 网关 rpcId 回显不一致，拒绝使用该响应')
        result = data.get('result') or {}
        if not result.get('ok'):
            error = result.get('error') or {}
            raise GatewayError('DSH 网关调用失败（%s）：%s' % (error.get('code'), _no_secret(error.get('message'))))
        return result.get('value') or {}

    # ------------------------------------------------------------------ 只读
    def current_session_id(self) -> str:
        """最值得当作「当前会话」的一个：优先正在跑的、最近更新的。"""
        items = self.session_summaries()
        if not items:
            return ''
        running = [item for item in items if item.get('running')]
        pool = running or [item for item in items if not item.get('blank') and item.get('agentAvailable')]
        pool = pool or items
        pool = sorted(pool, key=lambda item: float(item.get('updatedAt') or 0), reverse=True)
        return str(pool[0].get('sessionId') or '')

    def list_sessions(self) -> dict:
        """列出会话（含 running/title/cwd 等投影摘要）。"""
        return self.rpc('session/list', {'_request': {}})

    def session_summaries(self) -> list:
        value = self.list_sessions()
        return value.get('items') or value.get('sessions') or []

    def find_session(self, session_id: str = '') -> dict | None:
        wanted = str(session_id or '').strip()
        items = self.session_summaries()
        if wanted:
            for item in items:
                candidate = str(item.get('sessionId') or item.get('id') or '')
                if candidate == wanted or candidate.endswith(wanted) or wanted.endswith(candidate):
                    return item
            return None
        return items[0] if items else None

    def projections(self, session_id: str) -> dict:
        return self.rpc('session/projections', {'request': {'sessionId': session_id}})

    def session_view(self, session_id: str) -> dict:
        """把 projections 归一成咕嘎可用的一小份视图（running 取自 session/list 顶层）。"""
        raw = self.projections(session_id)
        item = self.find_session(session_id) or {}
        values = raw.get('values') or {}
        todos = values.get('todos') or {}
        if isinstance(todos, dict):
            todo_items = todos.get('items') or []
        else:
            todo_items = todos
        plan = values.get('plan') or {}
        turn_outline = values.get('turnOutline') or []
        if isinstance(turn_outline, dict):
            turn_outline = turn_outline.get('items') or []
        return dict(
            session_id=session_id,
            kind=raw.get('kind'),
            as_of_seq=raw.get('asOfSeq'),
            title=values.get('title') or session_title(item),
            running=bool(item.get('running')),
            agent_available=bool(item.get('agentAvailable')),
            working_directory=values.get('workingDirectory') or str(item.get('cwd') or ''),
            token_usage=values.get('tokenUsage') or {},
            context_pressure=values.get('contextPressure') or {},
            todos=todo_items,
            plan=plan,
            turn_outline=turn_outline,
        )

    # ------------------------------------------------------------------ 写
    def prompt(self, session_id: str, text: str, mode: str = 'queue', request_id: str = '') -> dict:
        """向在线会话投递一条用户消息（写操作）。requestId 幂等，重试安全。"""
        session_id = str(session_id or '').strip()
        text = str(text or '').strip()
        if not session_id or not text:
            raise ValueError('prompt 需要明确的 session_id 与 text（不允许猜目标会话）')
        mode = str(mode or 'queue').strip()
        if mode not in ('queue', 'steer'):
            raise ValueError('mode 只支持 queue（下一轮）或 steer（插话），拒绝未知取值')
        if len(text) > 8000:
            raise ValueError('text 超过 8000 字符；拒绝静默截断，请拆成多条投递')
        resolved = self.resolve_session_id(session_id)
        request = {
            'requestId': request_id or str(uuid.uuid4()),
            'sessionId': resolved,
            'mode': mode,
            'content': [{'type': 'text', 'text': text}],
            'clientTimeZone': 'Asia/Shanghai',
        }
        result = self.rpc('session/prompt', {'request': request}, timeout=20.0)
        # 投递成功才认领：这是咕嘎自己拉起的对话，后续只盯它。
        remember_owned(resolved)
        return result

    def cancel(self, session_id: str) -> dict:
        """Stop only the explicitly named turn; preserve the session and inbox."""
        sid = str(session_id or '').strip()
        if not sid:
            raise GatewayError('cancel 需要明确的 session_id，不允许猜目标会话')
        return self.rpc('session/cancel', {'request': {'sessionId': sid}})

    def resolve_session_id(self, session_id: str) -> str:
        """把任意形态（裸 uuid / session- 前缀 / 后缀）归一到列表里的原样 id。"""
        wanted = str(session_id or '').strip()
        if not wanted:
            return ''
        items = self.session_summaries()
        exact = session_id_from_focused(wanted, items)
        if exact:
            return exact
        if not exact and wanted in {str(item.get('sessionId') or '') for item in items}:
            return wanted
        raise GatewayError('目标会话不在 DSH 会话列表里，拒绝投递：' + wanted[:80])

    # ------------------------------------------------------------------ 开新对话
    def ensure_workspace(self, path=None, title: str = GUGA_WORKSPACE_TITLE) -> str:
        """解析（必要时创建）指向咕嘎目录的 DSH 项目，返回 workspaceId。

        `workspace/create` 对同一目录是幂等的；建好后把标题改成「咕嘎」，
        这样 DSH 侧边栏里是一个明确的咕嘎区，而不是目录名 guga-penguin-pet。
        任何失败都返回空串——找不到项目不该阻断新建会话。
        """
        target = str(path or GUGA_ROOT)
        try:
            existing = find_guga_workspace(target)
            if existing:
                return existing
            value = self.rpc('workspace/create', {'request': {'path': target}}, timeout=15.0)
            workspace = (value or {}).get('workspace') or {}
            workspace_id = str(workspace.get('workspaceId') or '').strip()
            if not workspace_id:
                return ''
            if title and str(workspace.get('title') or '') != title:
                try:
                    self.rpc('workspace/rename',
                             {'request': {'workspaceId': workspace_id, 'title': title}},
                             timeout=15.0)
                except GatewayError:
                    pass    # 改不了标题不影响会话归属
            return workspace_id
        except GatewayError:
            return ''

    def create_session(self, conversation_id: str = '', preset: str = '',
                       workspace_id: str = '', strict_grouping: bool = True) -> dict:
        """新建一个空会话，返回真实 sessionId，并登记为「咕嘎自己拉起的对话」。

        走的是 3080 同一个内部 RPC 面（`session/create`，实测可用）。
        会话必须加入「咕嘎区」：`strict_grouping=True` 时拿不到 workspaceId 就**报错不建**。
        以前这里是静默降级（`ensure_workspace()` 失败返回空串照样建），结果 181 个会话
        全掉进「未分组」——失败必须响亮，不能变成侧边栏里的一堆垃圾。
        不返回会话编号就报错，绝不假装已经建好。
        """
        request = {'clientTimeZone': 'Asia/Shanghai'}
        if preset:
            request['agentPreset'] = str(preset)[:80]
        workspace_id = str(workspace_id or '').strip() or self.ensure_workspace()
        if not workspace_id and strict_grouping:
            raise GatewayError(
                '无法把新会话挂到「咕嘎」分组（workspace/create 失败或标题改不动）；'
                '拒绝新建会话——否则它会掉进「未分组」污染侧边栏。'
                '先确认 3080 上的 dsh web 正常，再重试派单。')
        if workspace_id:
            request['workspaceId'] = workspace_id
        result = self.rpc('session/create', {'request': request}, timeout=20.0)
        result = result if isinstance(result, dict) else {}
        session_id = str(result.get('sessionId') or result.get('session_id') or '').strip()
        if not session_id:
            raise GatewayError('DSH 受理了新建会话但没有返回会话编号；不假装已经建好')
        remember_owned(session_id)
        if conversation_id:
            remember_owner(session_id, conversation_id)
        # 立刻切到完全权限并复核。不切的话默认是 workspace-write + approval:ask，
        # 任务一碰工作区外的文件就会停下来等人点同意——那正是咕嘎最不该发生的事。
        permission = self.ensure_full_access(session_id)
        return dict(ok=True, session_id=session_id,
                    agent_preset=str(result.get('agentPreset') or ''),
                    conversation_id=str(conversation_id or ''),
                    workspace_id=workspace_id,
                    permission=permission,
                    full_access=bool(permission.get('ok')))

    # ------------------------------------------------------------------ 权限
    def permission_of(self, session_id: str) -> dict:
        """读会话当前权限档位（投影里的 `values.permissions.currentValue`）。"""
        session_id = str(session_id or '').strip()
        if not session_id:
            return {}
        raw = self.rpc('session/projections', {'request': {'sessionId': session_id}})
        values = (raw or {}).get('values') or {}
        permissions = values.get('permissions')
        return permissions if isinstance(permissions, dict) else {}

    def ensure_full_access(self, session_id: str) -> dict:
        """把会话切到完全权限档，并**实测复核**。

        返回 {ok, before, after, preset, error}。复核不一致就如实 ok=False——
        权限没到位的话，任务迟早会卡在"等人点同意"上，那比直接报错更坑。
        """
        session_id = str(session_id or '').strip()
        if not session_id:
            return dict(ok=False, error='没有会话编号')
        before = str((self.permission_of(session_id) or {}).get('currentValue') or '')
        if before == PERMISSION_PRESET:
            return dict(ok=True, before=before, after=before, preset=PERMISSION_PRESET, already=True)
        try:
            self.rpc('commands/execute', {
                'agentId': session_id,
                'line': '/permission ' + PERMISSION_PRESET,
                'submittedAttachments': [],
            }, timeout=20.0)
        except Exception as exc:  # noqa: BLE001
            return dict(ok=False, before=before, after='', preset=PERMISSION_PRESET,
                        error='切换权限失败：' + _no_secret(str(exc))[:200])
        after = str((self.permission_of(session_id) or {}).get('currentValue') or '')
        ok = after == PERMISSION_PRESET
        return dict(ok=ok, before=before, after=after, preset=PERMISSION_PRESET,
                    error='' if ok else '权限切换后复核不一致')

    def session_running(self, session_id: str) -> bool:
        """会话是否还在跑一轮（单槽闸门的探活口）。

        探不到就当「还在跑」：宁可让下一个任务多排一会儿，也不并发抢同一个工作区。
        """
        session_id = str(session_id or '').strip()
        if not session_id:
            return False
        try:
            view = self.session_view(session_id)
        except Exception:  # noqa: BLE001
            return True
        return bool((view or {}).get('running'))

    def start_task(self, text: str, conversation_id: str = '', mode: str = 'queue',
                   ticket_id: str = '') -> dict:
        """开一个新对话并立刻投第一条指令——咕嘎托管 DSH 的最小闭环。

        单槽闸门（2026-11-14 事故后加的硬规则）：同一时刻只允许一个咕嘎会话在跑。
        已有会话在跑时**不新建**，返回 ``busy=True`` 和当前占用者，由调用方排队。
        以前这里无条件 ``create_session``，外层循环把它放大成半小时 122 个会话，
        全部落在「未分组」并互相抢目录。
        """
        slot = dsh_slot.status(probe=self.session_running)
        if slot.get('active'):
            return dict(ok=False, busy=True, accepted=False, session_id='',
                        active_session_id=slot.get('session_id') or '',
                        active_ticket=slot.get('ticket_id') or '',
                        error='已有咕嘎会话在跑（%s），本次排队，不新建会话' % slot.get('reason'))
        created = self.create_session(conversation_id=conversation_id)
        session_id = created['session_id']
        claimed = dsh_slot.claim(session_id, ticket_id=ticket_id, probe=self.session_running)
        if not claimed.get('ok'):
            return dict(ok=False, busy=True, accepted=False, session_id=session_id,
                        active_session_id=claimed.get('active_session_id') or '',
                        error='抢单槽失败，本次不投递：' + str(claimed.get('reason') or ''))
        try:
            self.prompt(session_id, text, mode=mode)
        except Exception as exc:  # noqa: BLE001
            # 会话建了但指令没进去：如实报告并带回会话编号，方便人工收尾；不自动重投。
            dsh_slot.release(session_id)
            return dict(ok=False, session_id=session_id, accepted=False,
                        error='新会话已建立，但第一条指令没有投递成功：' + _no_secret(str(exc))[:200])
        return dict(ok=True, session_id=session_id, accepted=True, slot=True)

    def finish_task(self, session_id: str = '') -> dict:
        """任务结束放槽。咕嘎在对账/交付后必须调用，否则下一个任务会一直排队。"""
        return dsh_slot.release(session_id)

    # ------------------------------------------------------------------ 健康
    def health(self) -> dict:
        try:
            items = self.session_summaries()
        except Exception as exc:  # noqa: BLE001
            return dict(ok=False, error=str(exc)[:200], host=self.host, port=self.port)
        return dict(ok=True, host=self.host, port=self.port, sessions=len(items),
                    cookie_cached=bool(self._cookie), token=bool(self._token))


def session_title(item: dict) -> str:
    """从 session/list 条目里取标题（标题在 projections.values.title）。"""
    projections = item.get('projections') if isinstance(item.get('projections'), dict) else {}
    values = projections.get('values') if isinstance(projections.get('values'), dict) else {}
    return str(values.get('title') or item.get('title') or '')[:100]


def session_id_from_focused(focused: str, summaries: list) -> str:
    """把 dsh_link 的 focused_session（可能是 session-<uuid>）映回网关 sessionId。"""
    focused = str(focused or '').strip()
    if not focused:
        return ''
    bare = focused[len('session-'):] if focused.startswith('session-') else focused
    for item in summaries:
        candidate = str(item.get('sessionId') or item.get('id') or '')
        if candidate == focused or candidate == bare or candidate.endswith(bare):
            return candidate
    return ''
