"""咕嘎 MCP Server（stdio）：软件路径发现（自报）+ 兼容任务总线。

定位：MCP 的核心职责是“发现/自报路径”，不承担指挥；咕嘎的派单仍走界面自动化（视觉 + UIA）。
四个任务总线工具是**旧工作流的兼容通道**（外部 worker 主动取任务/交回执用），
咕嘎自己的派单不会调用它们——保留是为了不破坏已接入的 worker，不代表 MCP 变成指挥通道。

五个工具：
  report_app      软件自报身份与安装路径（写 %LOCALAPPDATA%\\GUGA\\app_reports.json，只用于发现）
  get_task        兼容：取一件待办任务（.guga/inbox/<task_id>.json）
  report_progress 兼容：回报中间进展（.guga/outbox/<task_id>.progress.json）
  submit_artifact 兼容：提交最终回执（.guga/outbox/<task_id>.json）
  ask_guga        兼容：需要判断时提问（.guga/questions/<question_id>.json，可等待 .guga/answers/）

任务协议：{task_id, goal, acceptance, deadline, callback}
回执协议：{task_id, status, artifact_paths, self_check}
提问协议：{task_id, question_id, question, options, asked_at, status}
回答协议：{task_id, question_id, answer, answered_by, answered_at}

只依赖标准库和 mcp SDK，不导入咕嘎引擎；工作区可用 GUGA_WORKSPACE 覆盖。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP


class MCPServer(FastMCP):
    """把 mcp SDK 的 FastMCP 适配成本文件使用的接口。

    本文件按「MCPServer(name, title, instructions) + run(transport, host=, port=, …)」
    写，而 SDK 1.x 的 FastMCP 有四处差异：构造器没有 title；host / port /
    streamable_http_path / stateless_http / json_response 只能走构造期 settings；
    run() 只收 transport；lowlevel server 叫 _mcp_server。
    差异全部收在这一层，下面几百行的工具定义保持原样。
    """

    def __init__(self, name=None, title=None, instructions=None, **kwargs):
        super().__init__(name=name, instructions=instructions, **kwargs)
        # FastMCP 没有 title 入参；留在实例上，供排查时确认这是哪个 server。
        self.title = title or name or ''

    def run(self, transport='stdio', mount_path=None, **options):
        for key, value in options.items():
            if not hasattr(self.settings, key):
                raise TypeError('FastMCP 不支持运行参数 %r' % (key,))
            setattr(self.settings, key, value)
        return super().run(transport, mount_path)

    @property
    def _lowlevel_server(self):
        """旧写法沿用的名字，指向 SDK 1.x 的 lowlevel server。"""
        return self._mcp_server

# 源码态直接运行本文件时，保证能导入 core.adapters.app_reports（打包态同样可用）。
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from core.adapters import app_reports
    _REPORT_IMPORT_ERROR = ''
except Exception as exc:      # 路径发现不可用时不拖垮任务总线
    app_reports = None
    _REPORT_IMPORT_ERROR = '%s: %s' % (type(exc).__name__, str(exc)[:160])

TASK_ID = re.compile(r'[A-Za-z0-9._-]{1,80}')
STATUS_VALUES = ('done', 'blocked', 'failed')
PROGRESS_VALUES = ('running', 'blocked', 'failed')


def _workspace() -> Path:
    override = os.environ.get('GUGA_WORKSPACE', '').strip()
    if override:
        return Path(override).expanduser().resolve()
    here = Path(__file__).resolve()
    for parent in here.parents[:6]:
        if (parent / '.joycode').is_dir() or (parent / '.guga').is_dir():
            return parent
    # 本模块刻意不导入咕嘎引擎，这里只用环境变量推导用户可写目录；
    # 打包进 Program Files 时安装目录只读，任务/回执不能写在那里。
    base = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA') \
        or os.path.expanduser('~')
    return Path(base) / 'GUGA' / 'runtime' / 'workspace'


WORKSPACE = _workspace()
INBOX = WORKSPACE / '.guga' / 'inbox'
OUTBOX = WORKSPACE / '.guga' / 'outbox'
QUESTIONS = WORKSPACE / '.guga' / 'questions'
ANSWERS = WORKSPACE / '.guga' / 'answers'
CONNECTIONS = WORKSPACE / '.guga' / 'mcp-connections'
CLIENT_ID = re.sub(r'[^A-Za-z0-9._-]', '', os.environ.get('GUGA_CLIENT_ID', ''))[:40]


def _record_connection() -> None:
    """连上就留证据：哪个软件（GUGA_CLIENT_ID）、哪个进程、什么时候。

    接入教程和验证脚本靠这份文件区分“只写了配置”和“软件真的连上来了”。
    """
    try:
        connections = _connections_dir()
        connections.mkdir(parents=True, exist_ok=True)
        record = dict(
            client_id=CLIENT_ID or 'unknown',
            pid=os.getpid(),
            started_at=time.strftime('%Y-%m-%dT%H:%M:%S'),
            argv=sys.argv[:6],
            workspace=str(WORKSPACE),
            mode='frozen' if getattr(sys, 'frozen', False) else 'source',
            source='guga.task_bus/1',
        )
        path = connections / ((CLIENT_ID or 'unknown') + '.json')
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
    except Exception:
        pass


_CONNECTIONS_CACHE = {'dir': None}


def _connections_dir() -> Path:
    """握手证据目录：必须和接入面板（mcp_channels.default_workspace）解析到同一处。

    server 与 GUI 的 __file__ 基准不同（打包态是 _MEIPASS），各自推导会分叉：
    server 写 A 目录、面板读 B 目录，就出现“握手成功但显示未接入”。
    """
    cached = _CONNECTIONS_CACHE.get('dir')
    if cached is not None:
        return cached
    result = CONNECTIONS
    try:
        from core.adapters import mcp_channels
        result = Path(mcp_channels.default_workspace()) / '.guga' / 'mcp-connections'
    except Exception:
        result = CONNECTIONS
    _CONNECTIONS_CACHE['dir'] = result
    return result


_CLIENT_ALIASES = {
    'doubao': 'doubao', '豆包': 'doubao', 'doubao-app': 'doubao',
    'workbuddy': 'workbuddy', 'work buddy': 'workbuddy',
    'joycode': 'joycode', 'joy code': 'joycode', 'joycoder': 'joycode',
}


def _client_app_id(name: str) -> str:
    """把 MCP 客户端自报的名字归一化到已登记的 app_id。

    豆包是 HTTP 连接器，握手时只带 clientInfo.name（可能是 doubao / 豆包 /
    Doubao Desktop），不会带 GUGA_CLIENT_ID，所以不能只按 stdio 环境变量认。
    """
    text = str(name or '').strip()
    if not text:
        return ''
    try:
        from core.adapters import app_registry
        app_id = app_registry.resolve_app(text)
        if app_id:
            return app_id
    except Exception:
        pass
    folded = text.casefold()
    if folded in _CLIENT_ALIASES:
        return _CLIENT_ALIASES[folded]
    # resolve_app 对 'Doubao Windows' 这类带后缀的名字不一定能命中（_normalize_name
    # 只剥 desktop/客户端/桌面端 等后缀），这里再做一次子串兜底，确保豆包发任何变体
    # 名都能落到 doubao，连接证据文件一定会写，面板不会再误判“未接入”。
    low = folded
    for known_id, aliases in (
        ('doubao', ('doubao', '豆包')),
        ('workbuddy', ('workbuddy', '工作搭子', '工作伙伴')),
        ('joycode', ('joycode', '字节豆包编程', '豆包编程')),
    ):
        if any(a in low for a in aliases):
            return known_id
    return ''


def _http_record_path() -> Path:
    try:
        from core import guga_paths
        runtime = Path(guga_paths.RUNTIME)
    except Exception:
        base = Path(os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA')
                    or os.path.expanduser('~'))
        runtime = base / 'GUGA' / 'runtime'
    runtime.mkdir(parents=True, exist_ok=True)
    return runtime / 'mcp_http.json'


def _read_http_evidence() -> dict:
    """读取 runtime/mcp_http.json；失败时返回空 dict，绝不阻断握手。"""
    try:
        path = _http_record_path()
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _update_http_evidence(**fields) -> None:
    """把“真实客户端握手/工具枚举”合并进 runtime/mcp_http.json，不覆盖监听状态。"""
    try:
        path = _http_record_path()
        try:
            record = json.loads(path.read_text(encoding='utf-8-sig'))
        except Exception:
            record = {}
        if not isinstance(record, dict):
            record = {}
        record.update(fields)
        record['evidence_updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
    except Exception:
        pass


def _is_self_probe(name: str) -> bool:
    text = str(name or '').casefold()
    return 'selfcheck' in text or 'self-check' in text or text.startswith('guga-probe')


def _record_client_handshake(name, version='', protocol='') -> None:
    """HTTP 客户端完成 initialize 后写证据；这是“已接入”的唯一可信来源。

    pid 写的是 HTTP server 自身：server 活着说明这次握手仍有效；server 重启后
    旧证据自动失效。仅“添加了连接器/只在界面看到 guga”不算。
    """
    text = str(name or '').strip()
    now = time.strftime('%Y-%m-%dT%H:%M:%S')
    if not text:
        # 客户端自报的 clientInfo.name 为空串也必须写 client 字段，否则 tools/list
        # observer 读到 mcp_http.json 里没有 client，会走 else 分支只写 tools_listed_at
        # 而不写 client_tools_listed_at，panel 的 channel_status 判定不到"已接入"。
        # HTTP 端点当前只给豆包连接器用，app_id 固定为 doubao。
        _update_http_evidence(client=dict(name='', app_id='doubao',
                                          version=str(version or ''),
                                          protocol=str(protocol or ''), at=now,
                                          empty_name=True))
        # k 版修复：不要 return，继续往下走让 connections dir 也写一份证据，
        # 不然 connection_witness 读不到，panel 会显示 endpoint_ready 而不是已接入。
        # 但 text 已空，跳过 _is_self_probe / _client_app_id 分支，直接写 connections dir。
        app_id = 'doubao'
        unidentified = True
        # 内联一份 connections dir 写入（因为后面的通用代码也依赖 text 非空）
        try:
            connections = _connections_dir()
            connections.mkdir(parents=True, exist_ok=True)
            path = connections / (app_id + '.json')
            previous = {}
            try:
                previous = json.loads(path.read_text(encoding='utf-8-sig'))
            except Exception:
                previous = {}
            count = int((previous or {}).get('handshake_count') or 0) + 1
            record = dict(
                client_id=app_id, client_name='（未识别客户端 · empty_name）',
                client_version=str(version or ''),
                pid=os.getpid(), server_pid=os.getpid(), transport='http',
                source='guga.mcp_http/1', protocol_version=str(protocol or ''),
                connected_at=(previous or {}).get('connected_at') or now,
                last_handshake_at=now, last_seen_at=now, handshake_count=count,
                client_unidentified=True, empty_name=True,
                workspace=str(WORKSPACE),
                mode='frozen' if getattr(sys, 'frozen', False) else 'source',
                argv=sys.argv[:6],
            )
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(path)
        except Exception:
            pass
        return
    if _is_self_probe(text):
        _update_http_evidence(probe=dict(name=text, version=str(version or ''),
                                        protocol=str(protocol or ''), at=now))
        return
    app_id = _client_app_id(text)
    _update_http_evidence(client=dict(name=text, version=str(version or ''),
                                      app_id=app_id, protocol=str(protocol or ''), at=now))
    unidentified = not app_id
    if unidentified:
        # 豆包 / 部分 MCP 客户端不会把 clientInfo.name 报成可识别名称（可能是
        # 'MCP Client'、'Coze'、空串之外的任意值）。HTTP 端点当前只用于豆包连接器，
        # 真实握手不能因为名字不在别名表里就丢掉；这里落到 doubao 证据文件并标记
        # client_unidentified，面板据此显示“已接入（客户端名称未识别）”。
        app_id = 'doubao'
    try:
        connections = _connections_dir()
        connections.mkdir(parents=True, exist_ok=True)
        path = connections / (app_id + '.json')
        previous = {}
        try:
            previous = json.loads(path.read_text(encoding='utf-8-sig'))
        except Exception:
            previous = {}
        count = int((previous or {}).get('handshake_count') or 0) + 1
        record = dict(
            client_id=app_id, client_name=text, client_version=str(version or ''),
            pid=os.getpid(), server_pid=os.getpid(), transport='http',
            source='guga.mcp_http/1', protocol_version=str(protocol or ''),
            connected_at=(previous or {}).get('connected_at') or now,
            last_handshake_at=now, last_seen_at=now, handshake_count=count,
            client_unidentified=unidentified,
            workspace=str(WORKSPACE),
            mode='frozen' if getattr(sys, 'frozen', False) else 'source',
            argv=sys.argv[:6],
        )
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
    except Exception:
        pass


def _install_http_observer() -> None:
    """在 HTTP server 上挂一个只读中间件：记录 initialize / tools/list 的真实发生。

    旧实现只在 server 启动时写一次 unknown.json，客户端握手完全不落证据，
    导致“豆包握手成功但面板显示未接入”。
    """
    lowlevel = server._lowlevel_server

    async def observer(ctx, call_next):
        try:
            method = str(getattr(ctx, 'method', '') or '')
            params = getattr(ctx, 'params', None) or {}
            if method == 'initialize':
                info = params.get('clientInfo') if isinstance(params, dict) else None
                info = info if isinstance(info, dict) else {}
                _record_client_handshake(info.get('name') or '', info.get('version') or '',
                                         params.get('protocolVersion') or '')
            elif method == 'tools/list':
                now = time.strftime('%Y-%m-%dT%H:%M:%S')
                last = _read_http_evidence().get('client') or {}
                name = str((last or {}).get('name') or '') if isinstance(last, dict) else ''
                # 只有真实客户端（非 guga-selfcheck）枚举工具，才算“已接入”的第三个条件；
                # 自检的 tools/list 单独记，不能拿来给豆包凑数。
                # k 版修复：空 clientInfo.name 时 name=''（falsy），旧逻辑会走 else 分支
                # 只写 tools_listed_at 不写 client_tools_listed_at。改成只要 client 字段
                # 存在且不是 self-probe，就更新 client_tools_listed_at，不管 name 是否为空。
                client_exists = bool(last) and isinstance(last, dict)
                is_probe = bool(name) and _is_self_probe(name)
                if client_exists and not is_probe:
                    _update_http_evidence(client_tools_listed_at=now)
                else:
                    _update_http_evidence(tools_listed_at=now)
        except Exception:
            pass
        return await call_next(ctx)

    try:
        lowlevel.middleware.append(observer)
    except Exception:
        pass


def _check_id(task_id: str) -> str:
    value = str(task_id or '').strip()
    if not TASK_ID.fullmatch(value):
        raise ValueError('task_id 只允许字母、数字、点、下划线和连字符，长度 1-80')
    return value


def _task_path(task_id: str) -> Path:
    return INBOX / (_check_id(task_id) + '.json')


def _receipt_path(task_id: str) -> Path:
    return OUTBOX / (_check_id(task_id) + '.json')


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError('读取失败：' + path.name + '：' + str(exc))
    if not isinstance(value, dict):
        raise ValueError('文件不是 JSON 对象：' + path.name)
    return value


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def _pending_tasks() -> list[Path]:
    if not INBOX.is_dir():
        return []
    paths = [p for p in INBOX.glob('*.json') if p.is_file()]
    paths.sort(key=lambda p: (p.stat().st_mtime, p.name))
    return [p for p in paths if not _receipt_path(p.stem).exists()]


server = MCPServer(
    name='guga',
    title='咕嘎接入服务（发现 + 任务总线）',
    instructions=(
        '本 server 有两类能力，用途严格分开：'
        '1) report_app 只用于“发现/自报路径”：软件第一次连上咕嘎时调用一次，'
        '把软件标识、显示名、本机 exe 绝对路径、版本和能力告诉咕嘎；咕嘎写入用户目录，'
        '以后直接按这条路径启动，不再靠注册表/开始菜单猜测。它不改变咕嘎的派单方式。'
        '2) get_task / report_progress / submit_artifact / ask_guga 是兼容保留的旧任务总线：'
        '给已经按旧协议写好的 worker 主动取任务、回报进展、提交回执、提问用；'
        '咕嘎自己的派单不调用这四个工具，也不要求新接入的软件使用它们——'
        '只要调用 report_app 自报一次路径即可。'
        '咕嘎的实际指挥仍由界面自动化（视觉 + UIA）执行，不因为接了 MCP 就改成 MCP 派单。'
        '任务文件在 .guga/inbox，回执在 .guga/outbox，提问在 .guga/questions；'
        '不要手工改动这两个目录以外的文件。'
        'submit_artifact 的 status 只能是 done / blocked / failed；blocked 或 failed 时必须说明卡点。'
    ),
)


@server.tool(
    name='report_app',
    description=(
        '软件自报安装位置（只用于路径发现，不改变咕嘎的派单方式）。'
        '软件第一次连上咕嘎时调用一次：传软件标识（如 workbuddy / doubao / joycode）、显示名、'
        '本机可执行文件绝对路径、版本和能力标签。咕嘎会校验文件真实存在且为 .exe，'
        '原子写入 %LOCALAPPDATA%\\GUGA\\app_reports.json 并回读校验；'
        '以后启动该软件直接用这条路径，不用再猜安装目录。'
        '路径移动或升级后重新调用一次即可；传错路径会被拒绝，不会写脏记录。'
    ),
)
def report_app(app_id: str, name: str, exe_path: str, version: str = '',
               capabilities: list[str] | None = None) -> dict:
    if app_reports is None:
        return {'ok': False, 'code': 'report_unavailable',
                'error': '路径发现不可用：' + (_REPORT_IMPORT_ERROR or '未知原因')}
    return app_reports.write_report(app_id, name, exe_path, version=version,
                                    capabilities=capabilities, client_id=CLIENT_ID,
                                    pid=os.getpid())


@server.tool(
    name='get_task',
    description=(
        '兼容通道（咕嘎自己的派单不调用本工具）：取下一条待办任务。'
        '不传 task_id 时返回最早一条尚未提交回执的任务；'
        '全部完成后返回 task=null。任务字段：task_id, goal, acceptance, deadline, callback。'
    ),
)
def get_task(task_id: str = '') -> dict:
    if task_id:
        task = _read_json(_task_path(task_id))
        if task is None:
            return {'ok': False, 'task': None, 'error': '任务不存在：' + task_id}
        receipt = _receipt_path(task_id)
        return {'ok': True, 'task': task, 'already_submitted': receipt.exists()}
    pending = _pending_tasks()
    if not pending:
        return {'ok': True, 'task': None, 'remaining': 0}
    task = _read_json(pending[0])
    return {'ok': True, 'task': task, 'remaining': len(pending)}


@server.tool(
    name='report_progress',
    description=(
        '兼容通道（旧 worker 回报用，咕嘎派单不依赖它）：回报任务中间进展，不产生最终回执。'
        'status 取 running / blocked / failed；'
        '同一任务多次调用会追加到 .guga/outbox/<task_id>.progress.json 的 events 列表（最多保留 50 条）。'
    ),
)
def report_progress(task_id: str, summary: str, status: str = 'running', self_check: str = '') -> dict:
    key = _check_id(task_id)
    if status not in PROGRESS_VALUES:
        return {'ok': False, 'error': 'status 必须是 ' + ' / '.join(PROGRESS_VALUES)}
    if not str(summary or '').strip():
        return {'ok': False, 'error': 'summary 不能为空'}
    path = OUTBOX / (key + '.progress.json')
    data = _read_json(path) or {'task_id': key, 'status': status, 'summary': '', 'events': []}
    events = list(data.get('events') or [])
    events.append(dict(status=status, summary=str(summary)[:2000],
                       self_check=str(self_check or '')[:2000], time=time.strftime('%Y-%m-%dT%H:%M:%S')))
    data.update(task_id=key, status=status, summary=str(summary)[:2000],
                self_check=str(self_check or '')[:2000], events=events[-50:])
    _write_json(path, data)
    return {'ok': True, 'task_id': key, 'status': status, 'events': len(data['events'])}


@server.tool(
    name='submit_artifact',
    description=(
        '兼容通道（旧 worker 交回执用，咕嘎派单不依赖它）：提交任务最终回执，'
        '写入 .guga/outbox/<task_id>.json，字段为 task_id, status, artifact_paths, self_check。'
        'artifact_paths 支持绝对路径或相对工作区路径；status 只能是 done / blocked / failed。'
    ),
)
def submit_artifact(task_id: str, artifact_paths: list[str], self_check: str,
                    status: str = 'done') -> dict:
    key = _check_id(task_id)
    if status not in STATUS_VALUES:
        return {'ok': False, 'error': 'status 必须是 ' + ' / '.join(STATUS_VALUES)}
    if not isinstance(artifact_paths, list) or not all(isinstance(item, str) for item in artifact_paths):
        return {'ok': False, 'error': 'artifact_paths 必须是字符串数组'}
    if not str(self_check or '').strip():
        return {'ok': False, 'error': 'self_check 不能为空'}
    resolved = []
    for item in artifact_paths:
        value = str(item).strip()
        if not value:
            continue
        candidate = Path(value)
        resolved.append(str((WORKSPACE / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()))
    receipt = dict(task_id=key, status=status, artifact_paths=resolved, self_check=str(self_check)[:4000])
    _write_json(_receipt_path(key), receipt)
    return {'ok': True, 'receipt': receipt}


@server.tool(
    name='ask_guga',
    description=(
        '兼容通道（旧 worker 使用，咕嘎派单不走这里）：遇到需要判断的事情'
        '（方案取舍、范围变化、验收标准不清）时提问，不要自己猜。'
        '问题写入 .guga/questions/<question_id>.json；timeout_seconds 大于 0 时最多等待 60 秒读取 '
        '.guga/answers/<question_id>.json，读到回答返回 answered，否则返回 pending（稍后再查）。'
        'options 是可选项列表，可为空表示开放作答。'
    ),
)
def ask_guga(task_id: str, question: str, options: list[str] | None = None,
             timeout_seconds: int = 0) -> dict:
    key = _check_id(task_id)
    text = str(question or '').strip()
    if not text:
        return {'ok': False, 'error': 'question 不能为空'}
    option_list = [str(item)[:300] for item in (options or []) if str(item).strip()]
    digest = hashlib.sha1((key + '|' + text).encode('utf-8')).hexdigest()[:8]
    question_id = key + '-' + digest
    requested_at = time.strftime('%Y-%m-%dT%H:%M:%S')
    document = dict(task_id=key, question_id=question_id, question=text[:4000],
                    options=option_list, asked_at=requested_at, status='pending')
    _write_json(QUESTIONS / (question_id + '.json'), document)
    wait_seconds = max(0, min(int(timeout_seconds or 0), 60))
    deadline = time.monotonic() + wait_seconds
    answer_path = ANSWERS / (question_id + '.json')
    while True:
        try:
            answer = _read_json(answer_path)
        except ValueError as exc:
            return {'ok': False, 'question_id': question_id,
                    'error': '回答文件损坏：' + str(exc)[:200]}
        if isinstance(answer, dict):
            return {'ok': True, 'question_id': question_id, 'status': 'answered',
                    'question': text[:4000], 'answer': answer.get('answer', ''),
                    'answered_by': answer.get('answered_by', 'guga'),
                    'answered_at': answer.get('answered_at', '')}
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    return {'ok': True, 'question_id': question_id, 'status': 'pending',
            'question': text[:4000], 'options': option_list,
            'question_path': str(QUESTIONS / (question_id + '.json')),
            'answer_path': str(answer_path),
            'message': '已提交给咕嘎，尚未回答；稍后用 question_id 再查或让咕嘎写 answers 文件。'}


def _write_http_record(state: str, port, requested, error: str = '') -> None:
    """把 HTTP 端点的真实端口/状态落盘，接入面板与验收脚本都读它。"""
    try:
        path = _http_record_path()
    except Exception:
        return
    record = dict(pid=os.getpid(), host='127.0.0.1', port=port, requested_port=int(requested),
                  url=('http://127.0.0.1:%s/mcp' % port) if port else '',
                  state=state, started_at=time.strftime('%Y-%m-%dT%H:%M:%S'),
                  mode='frozen' if getattr(sys, 'frozen', False) else 'source',
                  argv=sys.argv[:5], error=str(error or '')[:400],
                  client=None, probe=None, tools_listed_at='')
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def _register_http_compat() -> None:
    """补齐新版协议里豆包会用到的服务器方法。

    server/discover 由 SDK 原生实现；subscriptions/listen 原生没有服务器实现，
    这里返回 complete（本服务没有资源变更通知），避免豆包在握手阶段被卡住。
    """
    from mcp import types
    lowlevel = server._lowlevel_server

    async def _listen(_ctx, _params):
        return types.SubscriptionsListenResult()

    try:
        lowlevel.add_request_handler('subscriptions/listen',
                                     types.SubscriptionsListenRequestParams, _listen)
    except Exception:
        pass


def _prepare_http_runtime() -> None:
    """windowed 构建下 sys.stdout/stderr 是 None。

    uvicorn 默认配色 formatter 会在 ``dictConfig`` 里调 ``sys.stdout.isatty()``，
    直接抛 ``Unable to configure formatter 'default'``（f 版真实踩过）。
    这里补一个丢弃式 stdio，并把 formatter 换成纯文本，彻底不依赖终端。
    """
    import io
    if sys.stdout is None:
        sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding='utf-8')
    if sys.stderr is None:
        sys.stderr = io.TextIOWrapper(io.BytesIO(), encoding='utf-8')
    try:
        import uvicorn.config as ucfg
        ucfg.LOGGING_CONFIG['formatters'] = {
            'default': {'()': 'logging.Formatter', 'format': '%(levelname)s %(message)s'},
            'access': {'()': 'logging.Formatter',
                       'format': '%(levelname)s %(client_addr)s - "%(request_line)s" %(status_code)s'},
        }
    except Exception:
        pass


def http_main() -> None:
    """streamable HTTP 端点：只绑 127.0.0.1，端口 18760 起冲突自动 +1。"""
    from core import mcp_http
    requested = int(os.environ.get('GUGA_MCP_HTTP_PORT') or mcp_http.DEFAULT_PORT)
    try:
        from core import guga_paths
        config = json.loads(Path(guga_paths.CONFIG).read_text(encoding='utf-8-sig'))
        requested = int(config.get('mcp_http_port') or requested)
    except Exception:
        pass
    port = mcp_http.candidate_port(requested)
    if not port:
        _write_http_record('conflict', None, requested)
        raise SystemExit(2)
    _write_http_record('listening', port, requested)
    INBOX.mkdir(parents=True, exist_ok=True)
    OUTBOX.mkdir(parents=True, exist_ok=True)
    QUESTIONS.mkdir(parents=True, exist_ok=True)
    ANSWERS.mkdir(parents=True, exist_ok=True)
    # 不在启动时伪造“已连接”证据：只有客户端真的 initialize 才写 <app_id>.json。
    _install_http_observer()
    _register_http_compat()
    _prepare_http_runtime()
    try:
        server.run('streamable-http', host='127.0.0.1', port=port,
                   streamable_http_path='/mcp', stateless_http=False,
                   json_response=False)
    except BaseException as exc:
        cause = getattr(exc, '__cause__', None)
        error = '%s: %s' % (type(exc).__name__, str(exc)[:250])
        if cause is not None:
            error += ' <= %s: %s' % (type(cause).__name__, str(cause)[:160])
        _write_http_record('error', port, requested, error)
        raise


def main() -> None:
    if '--guga-mcp-http' in sys.argv:
        http_main()
        return
    INBOX.mkdir(parents=True, exist_ok=True)
    OUTBOX.mkdir(parents=True, exist_ok=True)
    QUESTIONS.mkdir(parents=True, exist_ok=True)
    ANSWERS.mkdir(parents=True, exist_ok=True)
    _record_connection()
    server.run('stdio')


if __name__ == '__main__':
    main()
