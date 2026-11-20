# -*- coding: utf-8 -*-
"""MCP 通道：咕嘎做 server，WorkBuddy / 豆包 / JoyCode 做 client。

三家工具都是 MCP 客户端，方向统一为 ``guga_server``：
咕嘎在本地起一个 stdio MCP server（``core/adapters/guga_mcp_server.py``），
目标软件按各自格式把这条命令注册进去，连上来领任务、回报进度、交回执。

本模块只负责“通道”这一层，不派单、不点界面：
- 生成可直接粘贴的配置片段（命令、参数、工作区、编码）；
- 返回该软件的配置文件位置（豆包没有本地文件，返回界面操作指引）；
- 检测是否已经添加过，已添加就显示“已接入”，不反复催；
- 写入/备份配置（仅在用户点“复制并写入”时调用）；
- 真实拉起一次 MCP 握手，确认软件侧能连上。

声明文件里的 ``channel`` 块是唯一事实来源；本模块不硬编码各家路径。
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from core import guga_paths
from core.adapters import app_registry, app_reports

CONTRACT = 'guga.mcp_channels/1'
SERVER_NAME = 'guga'
DEFAULT_ORDER = ('workbuddy', 'doubao', 'joycode')
BACKUP_SUFFIX = '.bak-guga-'
IMPORT_CHECK_TTL = 600

_PYTHON_CACHE = {'checked_at': 0, 'command': '', 'error': ''}


# ---------------------------------------------------------------------------
# 工作区与解释器
# ---------------------------------------------------------------------------

def default_workspace() -> Path:
    """咕嘎工作区：优先 GUGA_WORKSPACE；其次 cwd / exe 目录向上找带 .guga / .joycode 的目录。

    都没找到时落到用户可写目录：打包进 Program Files 时安装目录只读，
    JoyCode/豆包按工作区写任务与回执会直接失败。
    """
    override = os.environ.get('GUGA_WORKSPACE', '').strip()
    if override:
        return Path(override).expanduser().resolve()
    here = Path(__file__).resolve()
    roots = [Path.cwd()]
    if getattr(sys, 'frozen', False) and sys.executable:
        roots.insert(0, Path(sys.executable).resolve().parent)
    for root in roots:
        for parent in [root] + list(root.parents)[:6]:
            if (parent / '.guga').is_dir() or (parent / '.joycode').is_dir():
                return parent
    for parent in here.parents[:6]:
        if (parent / '.guga').is_dir() or (parent / '.joycode').is_dir():
            return parent
    try:
        from core import guga_paths
        return guga_paths.RUNTIME / 'workspace'
    except Exception:  # pragma: no cover
        base = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA') \
            or os.path.expanduser('~')
        return Path(base) / 'GUGA' / 'runtime' / 'workspace'


def _python_candidates():
    candidates = []
    override = os.environ.get('GUGA_MCP_PYTHON', '').strip()
    if override:
        candidates.append((override, '环境变量 GUGA_MCP_PYTHON'))
    if not getattr(sys, 'frozen', False) and sys.executable:
        candidates.append((sys.executable, '当前解释器'))
    for name in ('python.exe', 'python3.exe', 'python', 'python3'):
        found = shutil.which(name)
        if found:
            candidates.append((found, '系统 PATH'))
    result = []
    seen = set()
    for path, source in candidates:
        try:
            key = str(Path(path).resolve()).casefold()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        result.append((str(Path(path)), source))
    return result


def python_command(force=False) -> dict:
    """找一个能 ``import mcp`` 的 Python；找不到就明确返回错误，不静默降级。"""
    now = time.time()
    if not force and _PYTHON_CACHE['command'] and \
            now - float(_PYTHON_CACHE['checked_at'] or 0) <= IMPORT_CHECK_TTL:
        return dict(ok=True, command=_PYTHON_CACHE['command'], source=_PYTHON_CACHE.get('source', ''))
    last_error = ''
    for path, source in _python_candidates():
        try:
            completed = subprocess.run(
                [path, '-c', 'import mcp; print(mcp.__file__)'],
                capture_output=True, text=True, timeout=15,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.SubprocessError) as exc:
            last_error = '%s：%s' % (path, str(exc)[:140])
            continue
        if completed.returncode == 0:
            _PYTHON_CACHE.update(checked_at=now, command=path, source=source, error='')
            return dict(ok=True, command=path, source=source)
        last_error = '%s：缺少 mcp 包或无法启动（%s）' % (
            path, (completed.stderr or '').strip().splitlines()[-1:] or '')
    _PYTHON_CACHE.update(checked_at=now, command='', source='', error=last_error or '没有找到 Python')
    return dict(ok=False, command='', source='',
                error='找不到能 import mcp 的 Python。请在目标机器的 Python 里 pip install mcp，'
                      '或设置环境变量 GUGA_MCP_PYTHON 指向该解释器。')


def server_script() -> dict:
    """MCP server 脚本的绝对路径；打包后优先用 _MEIPASS 里的副本。"""
    here = Path(__file__).resolve().parent
    candidates = [here / 'guga_mcp_server.py']
    meipass = getattr(sys, '_MEIPASS', '')
    if meipass:
        candidates.append(Path(meipass) / 'core' / 'adapters' / 'guga_mcp_server.py')
        candidates.append(Path(meipass) / 'guga_mcp_server.py')
    for path in candidates:
        if path.is_file():
            return dict(ok=True, path=str(path))
    return dict(ok=False, path='',
                error='找不到 guga_mcp_server.py；请把 core/adapters/guga_mcp_server.py 一起打包')


def frozen_mode() -> bool:
    """冻结态判断；``GUGA_MCP_FORCE_FROZEN=1`` 仅供源码环境验证打包形态片段。"""
    if getattr(sys, 'frozen', False):
        return True
    return os.environ.get('GUGA_MCP_FORCE_FROZEN', '').strip().lower() in ('1', 'true', 'yes')


def frozen_exe() -> Path:
    """冻结态下指向的 exe 自身；源码环境验证时可用 GUGA_MCP_EXE 指定打包产物。"""
    override = os.environ.get('GUGA_MCP_EXE', '').strip()
    if override:
        return Path(override).expanduser()
    if getattr(sys, 'frozen', False) and sys.executable:
        return Path(sys.executable)
    root = Path(__file__).resolve().parents[2]
    for relative in (('dist', 'GUGA-Desktop', 'GUGA-Desktop.exe'),
                     ('dist', 'GUGA-Desktop.exe')):
        candidate = root.joinpath(*relative)
        if candidate.is_file():
            return candidate
    return root / 'dist' / 'GUGA-Desktop' / 'GUGA-Desktop.exe'


def server_command(app_id='', workspace=None, force=False) -> dict:
    """按 frozen / 源码两种形态给出启动命令。

    - frozen：直接 ``GUGA-Desktop.exe --guga-mcp``，不依赖外部 Python；
    - 源码：``python.exe <repo>/core/adapters/guga_mcp_server.py``。
    """
    ws = Path(workspace).expanduser() if workspace else default_workspace()
    if frozen_mode():
        # 真实打包运行时只认“自己这个 exe”，避免环境变量把配置指到别处。
        exe = Path(sys.executable) if getattr(sys, 'frozen', False) and sys.executable \
            else frozen_exe()
        if not exe.is_file():
            return dict(ok=False, mode='frozen', command='', args=[], env={},
                        error='打包入口不存在：%s（请先构建 dist/GUGA-Desktop/GUGA-Desktop.exe）' % exe)
        return dict(ok=True, mode='frozen', command=str(exe), args=['--guga-mcp'],
                    env=_workspace_entry(ws, app_id), source='打包入口自身')
    python = python_command(force=force)
    script = server_script()
    if not python.get('ok'):
        return dict(ok=False, mode='source', command='', args=[], env={},
                    error=python.get('error') or 'python_unavailable')
    if not script.get('ok'):
        return dict(ok=False, mode='source', command='', args=[], env={},
                    error=script.get('error') or 'server_script_missing')
    return dict(ok=True, mode='source', command=python['command'], args=[script['path']],
                env=_workspace_entry(ws, app_id), source=python.get('source', ''))


# ---------------------------------------------------------------------------
# 声明里的 channel 块
# ---------------------------------------------------------------------------

def _channel(app_id) -> dict:
    decl = app_registry.declaration(app_id) or {}
    channel = decl.get('channel') if isinstance(decl.get('channel'), dict) else {}
    return channel


def _substitute(text, workspace: Path) -> str:
    raw = str(text or '')
    raw = raw.replace('{workspace}', str(workspace)).replace('{project_root}', str(Path(__file__).resolve().parents[2]))
    raw = os.path.expandvars(raw)
    return raw


def config_targets(app_id, workspace=None) -> list:
    """声明的配置文件位置（按优先级）；豆包没有本地文件，返回空数组。"""
    channel = _channel(app_id)
    ws = Path(workspace).expanduser() if workspace else default_workspace()
    values = []
    if channel.get('config_path'):
        values.append(str(channel['config_path']))
    values.extend(str(item) for item in (channel.get('config_paths') or []) if str(item))
    result = []
    seen = set()
    for value in values:
        expanded = _substitute(value, ws)
        if not expanded:
            continue
        key = expanded.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(Path(expanded))
    return result


def _workspace_entry(workspace=None, app_id='') -> dict:
    """配置里只放与机器无关的项。

    ``GUGA_WORKSPACE`` 不再写进复制片段：它会带上生成配置那台机器的绝对路径，
    换机器/换解压目录后就成了死路径。工作区由 server 启动时自己解析
    （``GUGA_WORKSPACE`` 环境变量仍可由用户显式覆盖）。
    """
    env = {'PYTHONIOENCODING': 'utf-8'}
    if app_id:
        env['GUGA_CLIENT_ID'] = str(app_id)
    return env


def mcp_entry(app_id, workspace=None) -> dict:
    """给文件型 client（WorkBuddy / JoyCode）的 mcpServers 条目。"""
    command = server_command(app_id, workspace=workspace)
    if not command.get('ok'):
        return {'error': command.get('error') or 'server_unavailable'}
    return {
        'command': command['command'],
        'args': list(command['args']),
        'env': dict(command['env']),
        'disabled': False,
    }


def form_entry(app_id, workspace=None) -> dict:
    """豆包“自定义连接器”表单字段：HTTP 优先，STDIO 作备选。

    豆包本机客户端直连 ``http://127.0.0.1:<端口>/mcp``（实测保存即通），
    所以默认给 HTTP 表单；同样保留 STDIO 的命令/参数，HTTP 不可用时按备选填。
    端口取端点真实状态（冲突时会回退，实际端口写进这里）。
    """
    stdio = mcp_entry(app_id, workspace)
    if stdio.get('error'):
        return stdio
    endpoint = http_endpoint()
    port = endpoint.get('port') or endpoint.get('requested_port') or 18760
    url = str(endpoint.get('url') or ('http://127.0.0.1:%s/mcp' % port))
    return {
        'name': SERVER_NAME,
        'transport': 'HTTP',
        'url': url,
        'headers': {},
        'port': endpoint.get('port'),
        'requested_port': endpoint.get('requested_port'),
        'endpoint_state': str(endpoint.get('state') or ''),
        'endpoint_state_text': str(endpoint.get('state_text') or ''),
        'mode_hint': '豆包需在「工作」模式下才会调用该连接器；对话模式、/ 和 @ 搜不到。',
        'fallback': {
            'transport': 'stdio',
            'command': stdio['command'],
            'params': list(stdio['args']),
            'env': dict(stdio['env']),
        },
    }


def http_endpoint() -> dict:
    """HTTP MCP 端点的真实状态（监听中 / 未启动 / 端口冲突 / 启动失败）。"""
    try:
        from core import mcp_http
        return mcp_http.status()
    except Exception as exc:
        return dict(state='error', state_text='启动失败', port=None, requested_port=18760,
                    host='127.0.0.1', url='', detail='%s: %s' % (type(exc).__name__, str(exc)[:160]),
                    hint='右键「导出诊断」把日志发回；或先用 STDIO 连接器。', record={})


_SECRET_FIELDS = ('api_key', 'zhipu_api_key', 'access_token', 'refresh_token',
                  'secret', 'password', 'client_secret')


def _secret_values() -> list:
    """本机配置里所有模型/账号凭据的值，用于确保复制片段绝不带 Key。"""
    values = []
    try:
        data = json.loads((Path(guga_paths.BASE) / 'ai_config.json').read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return values
    stack = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                if isinstance(value, (dict, list)):
                    stack.append(value)
                elif str(key).casefold() in _SECRET_FIELDS and str(value or '').strip():
                    values.append(str(value).strip())
        elif isinstance(item, list):
            stack.extend(value for value in item if isinstance(value, (dict, list)))
    return values


def has_model_key(text) -> bool:
    """复制片段里是否混进了模型 Key（明文/字段注入都算）。"""
    raw = str(text or '')
    if re.search(r'sk-[A-Za-z0-9]{16,}', raw):
        return True
    return any(value and value in raw for value in _secret_values())


_DEV_MARKERS = (
    ('rengongzhineng', '开发机工作区 rengongzhineng'),
    ('guga-competition', '源码目录 guga-competition'),
    ('python.exe', '开发机解释器 python.exe'),
    ('python3.exe', '开发机解释器 python3.exe'),
)


def dev_artifacts(text) -> list:
    """复制/写入前扫描开发机痕迹：这些路径换台电脑必然失效。

    正在运行的 exe 自身先被替换成占位符：解压/构建目录就算在开发仓库下，
    命令行指向“当前这个 exe”也是合法的，不能误判。
    """
    raw = str(text or '')
    self_exe = ''
    try:
        if getattr(sys, 'frozen', False) and sys.executable:
            self_exe = str(Path(sys.executable).resolve())
    except OSError:
        self_exe = ''
    if not self_exe:
        self_exe = os.environ.get('GUGA_MCP_EXE', '').strip()
    if self_exe:
        raw = re.sub(re.escape(self_exe), '<self-exe>', raw, flags=re.I)
    raw = raw.casefold().replace('/', '\\')
    found = [label for marker, label in _DEV_MARKERS if marker in raw]
    if re.search(r'[a-z]:\\[^\s"\\]*python', raw) and '开发机解释器' not in '、'.join(found):
        found.append('开发机 Python 绝对路径')
    return found


def _dev_guard_message(problems) -> str:
    return ('配置里检测到开发机痕迹（%s），已拒绝复制/写入。'
            '换机器会直接启动失败；打包版只会用运行中的 GUGA-Desktop.exe --guga-mcp，'
            '源码环境请勿把配置带到别的电脑。' % '、'.join(problems))


def ui_fields_text(entry) -> str:
    """豆包连接器要逐项填的字段，用“字段：值”而不是 JSON；HTTP 优先，附 STDIO 备选。"""
    entry = entry if isinstance(entry, dict) else {}
    transport = str(entry.get('transport') or 'stdio')
    if transport.upper() == 'HTTP':
        fallback = entry.get('fallback') or {}
        env = fallback.get('env') or {}
        lines = [
            '首选：HTTP（豆包需在「工作」模式下才会调用该连接器）',
            '服务器名称：%s' % (entry.get('name') or SERVER_NAME),
            '传输类型：HTTP',
            '服务器 URL：%s' % (entry.get('url') or ''),
            'Headers：留空（不要把模型 Key 填进来）',
            '',
            '备选：STDIO（HTTP 端点未启动或端口冲突时改选这个）',
            '传输类型：STDIO',
            '命令：%s' % (fallback.get('command') or ''),
            '参数：%s' % ' '.join(str(item) for item in (fallback.get('params') or [])),
        ]
        if env:
            lines.append('环境变量：%s' % '；'.join('%s=%s' % (key, value)
                                                      for key, value in env.items()))
        return '\n'.join(lines)
    env = entry.get('env') or {}
    lines = [
        '服务器名称：%s' % (entry.get('name') or SERVER_NAME),
        '传输类型：%s' % transport,
        '命令：%s' % (entry.get('command') or ''),
        '参数：%s' % ' '.join(str(item) for item in (entry.get('params') or [])),
    ]
    if env:
        lines.append('环境变量：%s' % '；'.join('%s=%s' % (key, value)
                                                  for key, value in env.items()))
    return '\n'.join(lines)


def guidance(app_id) -> dict:
    """卡片下方的“粘到哪里 / 怎么点”说明，按软件的真实形态给，不套用固定菜单。"""
    channel = _channel(app_id)
    decl = app_registry.declaration(app_id) or {}
    name = str(decl.get('name') or app_id)
    if not channel:
        report = app_reports.valid_report(app_id)
        if report.get('valid'):
            name = str((report.get('record') or {}).get('name') or name)
        return dict(
            kind='generic', app=app_id, name=name,
            where='%s 自己的 MCP 设置（不同版本入口可能不一样）' % name,
            steps=[
                '打开 %s，在它自己的设置/菜单里找「MCP」「MCP 服务器」「扩展」这类入口。' % name,
                '把「复制配置」拿到的 mcpServers 条目合并进去，保存后重启该软件。',
                '重启后回到本窗口点「验证连接」；连上会显示 5 个工具（含 report_app）。',
                '不同版本入口不同，找不到就用复制文本问软件内置帮助，不要凭记忆找固定菜单。',
            ])
    if str(channel.get('registration') or 'file') == 'ui':
        endpoint = http_endpoint()
        return dict(
            kind='connector', app=app_id, name=name,
            where='豆包 → 插件·技能·伙伴 → 插件 → 右上角 + 添加 → 新建自定义连接器',
            http_endpoint=endpoint,
            steps=[
                '登录豆包，并确认账号有个人订阅或团队/企业额度（否则进不了豆包工作）。',
                '确认咕嘎已启动：本机 HTTP 端点是 %s（%s）。' % (
                    endpoint.get('url') or '未启动', endpoint.get('state_text') or '未知'),
                '打开「插件·技能·伙伴」→「插件」→ 右上角「+ 添加」→「新建自定义连接器」。',
                '传输类型选 HTTP，服务器名称 guga，服务器 URL 填 %s，Headers 留空。'
                % (endpoint.get('url') or 'http://127.0.0.1:18760/mcp'),
                '保存后进入豆包「工作」模式的任务输入框；该连接器只在「工作」模式生效，'
                '对话模式、/ 和 @ 都搜不到。「自定义连接器仅支持在本地电脑中使用」。',
                '官方没有独立的连通状态/工具清单页面，只能看「插件 → 管理」；是否真的连上以咕嘎'
                '「验证连接」的真实握手为准。HTTP 端点不可用时，改用同卡片里的 STDIO 备选。',
            ])
    if str(channel.get('registration') or 'file') == 'ui_json':
        entry = str(channel.get('official_entry') or '官方 MCP 界面')
        steps = [str(item) for item in (channel.get('ui_steps') or []) if str(item)]
        return dict(
            kind='ui_json', app=app_id, name=name,
            where='%s → %s' % (name, entry),
            official_entry=entry,
            success_signal=str(channel.get('official_success_signal') or ''),
            restart_required=bool(channel.get('restart_required')),
            steps=steps or [
                '打开 %s 的「%s」。' % (name, entry),
                '把「复制配置」得到的完整 JSON 粘贴进去（本地程序必须含 "type": "stdio"）。',
                '官方是否接受以界面提示为准；官方未要求重启。',
                '回到咕嘎点「验证连接」做真实握手，拿到 5 个工具才算真的连上，否则一律标“未验证”。',
            ])
    if str(channel.get('registration') or 'file') != 'file':
        return dict(
            kind='connector', app=app_id, name=name,
            where='%s 官方 MCP 界面' % name,
            steps=[
                '打开 %s，按官方文档的 MCP 入口添加。' % name,
                '把卡片里的字段/JSON 按官方格式粘贴，保存后以官方界面提示为准。',
                '回到咕嘎点「验证连接」；只有真实握手成功才显示已连接，其余一律“未验证”。',
            ])
    path = str(channel.get('config_path') or '')
    where = ('%s（若没有就新建）' % _substitute(path, default_workspace())) if path \
        else '%s 自己的设置里' % name
    steps = [str(item) for item in (channel.get('ui_steps') or []) if str(item)]
    entry = str(channel.get('official_entry') or '')
    if entry:
        where = '%s；官方界面入口：%s' % (where, entry)
    return dict(
        kind='settings', app=app_id, name=name, where=where,
        official_entry=entry,
        restart_required=bool(channel.get('restart_required')),
        restart_note=str(channel.get('restart_note') or ''),
        steps=steps or [
            '打开 %s，在它自己的设置或菜单里找「MCP」「MCP 服务器」「扩展」这类入口；不同版本位置和叫法可能不一样。' % name,
            '把「复制配置」拿到的 mcpServers 条目合并进去，保存后重启该软件。',
            '找不到入口时，先点「复制配置」拿到完整文本，再在软件内置帮助里搜“MCP”或问它的客服，不要凭记忆找固定菜单。',
            '重启后回到本窗口点「验证连接」：只检测到配置的标“已检测到配置”，真实握手拿到 5 个工具才标“已连接”。',
        ])


def snippet(app_id, workspace=None) -> dict:
    """生成可直接粘贴的片段。文件型给完整 mcpServers JSON；豆包给表单字段。"""
    app_id = str(app_id or '').strip().casefold()
    channel = _channel(app_id)
    if not channel:
        entry = mcp_entry(app_id, workspace)
        if entry.get('error'):
            return dict(ok=False, kind='generic', text='', entry=entry, message=entry['error'])
        payload = {SERVER_NAME: entry}
        text = json.dumps({'mcpServers': payload}, ensure_ascii=False, indent=2)
        problems = dev_artifacts(text)
        if problems:
            return dict(ok=False, kind='generic', text='', entry=entry,
                        message=_dev_guard_message(problems))
        if has_model_key(text):
            return dict(ok=False, kind='generic', text='', entry=entry,
                        message='片段里检测到模型 Key，已拒绝显示；MCP 连接只用本地命令/参数，'
                                '不应包含任何模型凭据。')
        name = str((app_reports.valid_report(app_id).get('record') or {}).get('name') or app_id)
        return dict(ok=True, kind='generic', text=text, entry=entry, registration='file',
                    message='把这段条目合并进 %s 自己的 MCP 设置；连上后让软件调用 report_app。'
                            '该软件没有内置通道声明，所以只能复制配置，无法自动写它的配置文件。' % name)
    if str(channel.get('registration') or 'file') == 'ui':
        entry = form_entry(app_id, workspace)
        if entry.get('error'):
            return dict(ok=False, kind='ui', text='', entry=entry, message=entry['error'])
        text = ui_fields_text(entry)
        problems = dev_artifacts(text)
        if problems:
            return dict(ok=False, kind='ui', text='', entry=entry,
                        message=_dev_guard_message(problems))
        if has_model_key(text):
            return dict(ok=False, kind='ui', text='', entry=entry,
                        message='片段里检测到模型 Key，已拒绝显示；MCP 连接只用本地命令，'
                                '不应包含任何模型凭据。请检查 ai_config.json。')
        return dict(ok=True, kind='ui', text=text, entry=entry, registration='ui',
                    http_endpoint=http_endpoint(),
                    message='豆包首选 HTTP：入口是「插件·技能·伙伴 → 插件 → 右上角 + 添加 → '
                            '新建自定义连接器」，传输类型选 HTTP，服务器 URL 填 %s，Headers 留空。'
                            '该连接器只在豆包「工作」模式生效，对话模式、/ 和 @ 都搜不到；'
                            '官方没有独立的连通状态/工具清单页面（只能看「插件 → 管理」）。'
                            'HTTP 端点不可用时改用同一份文本里的 STDIO 备选（传输类型 STDIO + '
                            '命令/参数/环境变量）。能否真的连上以咕嘎「验证连接」的真实握手为准；'
                            '未验证前不要写“已接入”。MCP 只做发现，派单仍走界面自动化。'
                            '详见《豆包连接器配置教程.txt》。'
                            % (entry.get('url') or 'http://127.0.0.1:18760/mcp'))
    if str(channel.get('registration') or 'file') == 'ui_json':
        entry = mcp_entry(app_id, workspace)
        if entry.get('error'):
            return dict(ok=False, kind='json', text='', entry=entry, message=entry['error'])
        entry = dict(type=str(channel.get('config_format', {}).get('type') or 'stdio'), **entry)
        payload = {str(channel.get('config_format', {}).get('top_key') or 'mcpServers'):
                   {str(channel.get('server_name') or SERVER_NAME): entry}}
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        problems = dev_artifacts(text)
        if problems:
            return dict(ok=False, kind='json', text='', entry=entry,
                        message=_dev_guard_message(problems))
        if has_model_key(text):
            return dict(ok=False, kind='json', text='', entry=entry,
                        message='片段里检测到模型 Key，已拒绝显示；MCP 连接只用本地命令/参数，'
                                '不应包含任何模型凭据。请检查 ai_config.json 或本机环境变量。')
        return dict(ok=True, kind='json', text=text, entry=entry, registration='ui_json',
                    message='不要把这段 JSON 直接写进 {workspace}/.joycode/mcp.json —— '
                            '官方只公布了界面入口，没有公布文件系统路径。请打开 JoyCode →'
                            '「MCP市场 → 全局配置文件 → +MCP配置」，把上面的 JSON 整段粘贴进去'
                            '（本地程序 type 必填 "stdio"，官方支持 stdio/SSE/streamableHttp）。'
                            '保存后以右下角「添加成功，正在更新」为准；官方未要求重启。'
                            '回到咕嘎「验证连接」真实握手，拿到 5 个工具才显示已连接。')
    entry = mcp_entry(app_id, workspace)
    if entry.get('error'):
        return dict(ok=False, kind='file', text='', entry=entry, message=entry['error'])
    payload = {str(channel.get('config_format', {}).get('top_key') or 'mcpServers'):
               {str(channel.get('server_name') or SERVER_NAME): entry}}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    problems = dev_artifacts(text)
    if problems:
        return dict(ok=False, kind='file', text='', entry=entry,
                    message=_dev_guard_message(problems))
    if has_model_key(text):
        return dict(ok=False, kind='file', text='', entry=entry,
                    message='片段里检测到模型 Key，已拒绝显示；MCP 连接只用本地命令/参数，'
                            '不应包含任何模型凭据。请检查 ai_config.json 或本机环境变量。')
    top = str(channel.get('config_format', {}).get('top_key') or 'mcpServers')
    return dict(ok=True, kind='file', text=text, entry=entry, registration='file',
                message='把下面条目合并进 %s 的 %s；连上后让软件调用 report_app 自报安装路径，'
                        '咕嘎写入 %%LOCALAPPDATA%%\\GUGA\\app_reports.json，以后直接按这条路径启动，'
                        '不用再猜。MCP 只做发现，派单仍走界面自动化。' % (app_id, top))


# ---------------------------------------------------------------------------
# 配置文件读写与“是否已添加”
# ---------------------------------------------------------------------------

def _read_config(path: Path) -> dict:
    try:
        raw = path.read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        return dict(ok=True, exists=False, data=None, error='')
    except OSError as exc:
        return dict(ok=False, exists=True, data=None, error='读取失败：%s' % str(exc)[:160])
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return dict(ok=False, exists=True, data=None, error='JSON 解析失败：%s' % str(exc)[:160])
    if not isinstance(data, dict):
        return dict(ok=False, exists=True, data=None, error='配置文件顶层不是 JSON 对象')
    return dict(ok=True, exists=True, data=data, error='')


def _normalize_args(items):
    result = []
    for item in (items or []):
        value = str(item)
        result.append(value.casefold() if os.name == 'nt' else value)
    return result


def _missing_config_exe(entry) -> str:
    """配置条目里的 command 指向一个已不存在的绝对 exe 时返回该路径。

    解压目录改名/移动后，客户端还在按旧路径启动，用户重启多少次都连不上；
    这里必须识别成“配置失效，需要重新复制”，不能显示成“重启软件即可”。
    """
    command = str((entry or {}).get('command') or '').strip().strip('"')
    if not command or not command.lower().endswith('.exe'):
        return ''
    try:
        path = Path(command)
    except OSError:
        return ''
    if path.is_absolute() and not path.is_file():
        return str(path)
    return ''


def entry_matches(current, expected) -> bool:
    """只比对真正影响连接的关键字段（command / args / 显式给出的 env）。"""
    if not isinstance(current, dict) or not isinstance(expected, dict):
        return False
    if str(current.get('command') or '').casefold() != str(expected.get('command') or '').casefold():
        return False
    if _normalize_args(current.get('args')) != _normalize_args(expected.get('args')):
        return False
    current_env = current.get('env') or {}
    expected_env = expected.get('env') or {}
    if not isinstance(current_env, dict):
        return False
    expected_ws = str(expected_env.get('GUGA_WORKSPACE') or '').strip()
    current_ws = str(current_env.get('GUGA_WORKSPACE') or '').strip()
    if expected_ws and current_ws.casefold() != expected_ws.casefold():
        return False
    return not bool(current.get('disabled'))


CONNECTION_FRESH_SECONDS = 900


def connection_witness(app_id, workspace=None) -> dict:
    """读 server 留下的连接证据：哪个软件真的连上来了、进程是否还活着。"""
    ws = Path(workspace).expanduser() if workspace else default_workspace()
    path = ws / '.guga' / 'mcp-connections' / (str(app_id) + '.json')
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        mtime = path.stat().st_mtime
    except (OSError, ValueError):
        return dict(exists=False, alive=False, path=str(path), pid=None,
                    started_at='', age_seconds=None)
    if not isinstance(data, dict):
        return dict(exists=False, alive=False, path=str(path), pid=None,
                    started_at='', age_seconds=None)
    pid = data.get('pid')
    alive = False
    if isinstance(pid, int):
        try:
            import psutil
            alive = psutil.pid_exists(pid)
        except Exception:
            alive = (time.time() - mtime) < 120
    return dict(exists=True, alive=alive, path=str(path), pid=pid,
                started_at=str(data.get('started_at') or ''), client_id=data.get('client_id'),
                client_name=str(data.get('client_name') or ''),
                client_version=str(data.get('client_version') or ''),
                transport=str(data.get('transport') or ''),
                last_handshake_at=str(data.get('last_handshake_at') or ''),
                handshake_count=data.get('handshake_count'),
                age_seconds=round(time.time() - mtime, 1), mtime_epoch=mtime,
                mode=str(data.get('mode') or ''))


def _client_tool_cache(app_id, workspace=None) -> dict:
    """客户端持久化的工具清单（WorkBuddy 的 mcp-tool-list.json）。

    WorkBuddy 只在启动时短暂连上 server 拉取工具清单，随后关掉子进程；
    因此“已接入”不能只看连接进程是否存活，还要看清单里有没有这五个工具，
    并且清单时间要晚于配置写入时间（否则只是旧缓存，仍需重启）。
    """
    channel = _channel(app_id)
    raw = str(channel.get('tool_cache') or '')
    if not raw:
        return {}
    ws = Path(workspace).expanduser() if workspace else default_workspace()
    path = Path(_substitute(raw, ws))
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        stat = path.stat()
    except (OSError, ValueError):
        return dict(path=str(path), exists=False, tools=[], complete=False)
    text = json.dumps(data, ensure_ascii=False).lower()
    tools = [name for name in ('report_app', 'get_task', 'report_progress',
                               'submit_artifact', 'ask_guga')
             if name in text]
    return dict(path=str(path), exists=True, tools=tools, complete=len(tools) == 5,
                mtime=time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(stat.st_mtime)),
                mtime_epoch=stat.st_mtime)


def verify_record(app_id) -> dict:
    """最近一次「验证连接」的真实握手结果（由 verify_connection 写入）。"""
    try:
        path = Path(guga_paths.RUNTIME) / 'mcp_verify.json'
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        record = data.get(str(app_id or '').casefold())
        return record if isinstance(record, dict) else {}
    except Exception:
        return {}


def _record_verify(app_id, result, url='') -> dict:
    """把最近一次真实握手结果写进 runtime/mcp_verify.json，供面板区分“通道就绪/已接入”。

    豆包是 HTTP 连接器：咕嘎自己连自己只能证明“端点活着 + 工具对”，
    不能证明豆包客户端来过。两者分开记，面板才不会把自检当成豆包。
    """
    record = dict(
        ok=bool((result or {}).get('ok')),
        status=str((result or {}).get('status') or ''),
        tools=list((result or {}).get('tools') or []),
        message=str((result or {}).get('message') or '')[:600],
        url=str(url or ''),
        method='http' if url else 'stdio',
        verified_at=time.strftime('%Y-%m-%dT%H:%M:%S'),
    )
    try:
        path = Path(guga_paths.RUNTIME) / 'mcp_verify.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        data[str(app_id or '').casefold()] = record
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
    except Exception:
        pass
    return record


def channel_status(app_id, workspace=None) -> dict:
    """一个软件的通道现状：通道类型、配置位置、是否已接入、是否只是写了配置待重启。"""
    app_id = str(app_id or '').strip().casefold()
    decl = app_registry.declaration(app_id) or {}
    channel = _channel(app_id)
    ws = Path(workspace).expanduser() if workspace else default_workspace()
    base = dict(
        contract=CONTRACT, app=app_id, name=str(decl.get('name') or app_id),
        supported=bool(channel), workspace=str(ws),
        direction=str(channel.get('direction') or ''), transport=str(channel.get('transport') or ''),
        registration=str(channel.get('registration') or ''),
        fallback=str(channel.get('fallback') or 'ui-auto'),
        server_name=str(channel.get('server_name') or SERVER_NAME),
        config_path='', config_paths=[], config_exists=False, added=None,
        conflict=False, entry={}, snippet='', action='', ui_instructions=[],
        download=copy.deepcopy(decl.get('download') or {}),
        verified=copy.deepcopy(channel.get('verified') or {}),
        connection=connection_witness(app_id, ws), connected=False, state='',
        tool_cache={}, http_endpoint={},
        message='', notes=str(channel.get('notes') or ''),
    )
    base['connected'] = bool(base['connection'].get('alive'))
    if not channel:
        report = app_reports.valid_report(app_id)
        if report.get('valid'):
            record = report.get('record') or {}
            base['name'] = str(record.get('name') or app_id)
            base['exe'] = str(report.get('exe') or '')
            generated = snippet(app_id, workspace=ws)
            base['snippet'] = generated.get('text', '')
            base['entry'] = generated.get('entry') or {}
            base['action'] = 'copy_config'
            base['state'] = 'report_only'
            base['message'] = '已记录自报路径（%s）。这类软件没有内置通道声明，' \
                              '只能复制配置到它自己的 MCP 设置里；连上后点「验证连接」确认。' % base['exe']
        else:
            base['message'] = '没有通道声明'
            base['action'] = 'none'
            base['state'] = 'unknown_app'
        return base

    generated = snippet(app_id, workspace=ws)
    base['snippet'] = generated.get('text', '')
    base['action'] = 'copy_config' if generated.get('registration') == 'file' else 'ui_manage'
    # 文件型通道必须能生成配置；HTTP 型（豆包）不用配置文件，配置片段失败只影响“复制”，
    # 不影响“是否已真实握手”的判定，所以不能在这里提前 return 把状态打成 server_unavailable。
    if not generated.get('ok') and str(channel.get('registration') or 'file') == 'file':
        base['message'] = generated.get('message') or '配置片段生成失败'
        base['state'] = 'server_unavailable'
        return base

    targets = config_targets(app_id, workspace=ws)
    base['config_paths'] = [str(path) for path in targets]
    base['config_path'] = str(targets[0]) if targets else ''
    top_key = str(channel.get('config_format', {}).get('top_key') or 'mcpServers')

    if str(channel.get('registration') or 'file') != 'file':
        base['added'] = None
        base['action'] = 'ui_manage'
        base['entry'] = generated.get('entry') or {}
        base['ui_instructions'] = [str(item) for item in (channel.get('ui_steps') or []) if str(item)]
        base['official_entry'] = str(channel.get('official_entry') or '')
        base['official_success_signal'] = str(channel.get('official_success_signal') or '')
        base['restart_required'] = bool(channel.get('restart_required'))
        base['http_endpoint'] = http_endpoint()
        base['verify'] = verify_record(app_id)
        name = base['name']
        entry = base['official_entry'] or '官方 MCP 界面'
        client = (base['http_endpoint'] or {}).get('client') or {}
        cname = str((client or {}).get('name') or '').strip()
        client_at = str((client or {}).get('at') or '')
        client_tools_at = str((base['http_endpoint'] or {}).get('client_tools_listed_at') or '')
        witness_at = str((base['connection'] or {}).get('last_handshake_at') or '')
        low_name = cname.casefold()
        # k 版修复：以前 `real_client = bool(cname)` —— 当豆包发空 clientInfo.name 时
        # cname='' 导致 real_client=False，真实握手匹配分支全部跳过，panel 显示 endpoint_ready
        # 而不是已接入。改成只要 mcp_http.json 的 client 字段存在（不管 name 空不空、
        # 是不是 self-probe）就算 real_client；self-probe 的排除移到后面分支。
        client_exists = bool(client) and isinstance(client, dict)
        real_client = client_exists and not (
            'selfcheck' in low_name or 'self-check' in low_name
            or low_name.startswith('guga-probe'))
        # 真实握手数据源是 mcp_http.json 的 client 字段。_record_client_handshake 只有在
        # clientInfo.name 能解析成已知 app_id 时才额外写 .guga/mcp-connections/{app_id}.json；
        # 若豆包发的名字不在别名表里（例如 'Doubao Windows'），连接证据文件就不会生成，
        # 面板会误判成“未验证”。这里用 mcp_http.json 的 client 名做回退匹配，保证“真握过手”
        # 一定能在面板上反映出来。
        client_unidentified = False
        client_matches = False
        if real_client:
            from core.adapters.app_registry import resolve_app
            matched_id = resolve_app(cname)
            if not matched_id and any(k in low_name for k in ('doubao', '豆包')):
                matched_id = 'doubao'
            client_matches = matched_id in ('', app_id)
            # HTTP 端点只用于豆包连接器：客户端没自报可识别名称时，仍按真实握手记录，
            # 但明确标记“未识别”，不能静默当成已确认的豆包。
            unidentified = not matched_id
            if client_matches and (
                    not witness_at or client_at >= witness_at or not base['connected']):
                base['connection'] = {
                    'transport': 'http',
                    'client_name': cname,
                    'pid': client.get('pid'),
                    'last_handshake_at': client_at or base['http_endpoint'].get('evidence_updated_at'),
                    'handshake_count': 1,
                    'alive': bool(client_tools_at),
                    'fallback_from_http_evidence': True,
                }
                client_unidentified = unidentified
                # 三个条件缺一不可：initialize 收到、tools/list 真的枚举过。
                base['connected'] = bool(client_tools_at)
        if base['connected']:
            base['state'] = 'connected'
            if str(base['connection'].get('transport') or '') == 'http' or client:
                detail = base['connection'].get('client_name') or client.get('name') or 'HTTP 客户端'
                seen = base['connection'].get('last_handshake_at') or base['connection'].get('started_at') or ''
                if client_unidentified:
                    base['client_unidentified'] = True
                    base['message'] = ('已接入：检测到 MCP 客户端「%s」完成真实握手并枚举了工具'
                                       '（最近 %s）。该客户端没有自报为豆包，但豆包 HTTP 端点当前只提供给'
                                       '豆包连接器使用，因此按已接入记录；若你同时在用别的 MCP 客户端，'
                                       '请以豆包界面里的连接状态为准。'
                                       % (detail, seen or '时间未知'))
                else:
                    base['message'] = ('已接入：%s 完成真实 MCP 握手并枚举了工具（客户端 %s，最近 %s，累计 %s 次）。'
                                       '这是客户端 initialize + tools/list 的实时证据，不是“界面里看到条目”。'
                                       % (name, detail, seen or '时间未知',
                                          base['connection'].get('handshake_count') or 1))
            else:
                base['message'] = ('已连接：%s 真实连上咕嘎（连接进程 %s）。这是握手证据，'
                                   '比“界面出现条目”更硬；未验证前不要写“已接入”。'
                                   % (name, base['connection'].get('pid')))
        elif real_client and client_matches and not client_tools_at:
            # 已经收到客户端 initialize，只是还没调用 tools/list。不能笼统写“未接入”。
            base['state'] = 'client_handshake_no_tools'
            base['client_unidentified'] = bool(client_unidentified)
            base['message'] = ('已收到 MCP 客户端「%s」的 initialize（最近 %s），但还没有 tools/list，'
                               '所以还不能判定“已接入”。请在豆包里发起一次调用或重新打开连接器；'
                               '看到 5 个工具后这里会自动变成“已接入”。'
                               % (cname, client_at or '时间未知'))
        elif base['connection'].get('exists'):
            base['state'] = 'connector_pending'
            base['message'] = ('曾握手成功：%s 之前连上过咕嘎（最近 %s），但当前 HTTP 服务已重启或'
                               '那次连接进程已退出。让豆包再发起一次调用，或点「验证连接」即可恢复。'
                               % (name, base['connection'].get('last_handshake_at')
                                  or base['connection'].get('started_at') or '时间未知'))
        elif ((base.get('verify') or {}).get('ok')
              and str((base['http_endpoint'] or {}).get('state') or '') == 'listening'):
            # 端点真实握手通过（咕嘎自己连自己），但还没有等来豆包的 initialize。
            # 以前这里只写“未验证”，用户会以为配置白做了；现在如实区分“通道就绪/客户端没来”。
            base['state'] = 'endpoint_ready'
            tools = (base.get('verify') or {}).get('tools') or []
            base['message'] = ('通道已就绪：HTTP 端点 %s 真实握手通过（%s 个工具），'
                               '但还没有收到豆包客户端（clientInfo）的 initialize。'
                               '请在豆包「%s」里确认连接器已启用并发起一次调用；'
                               '收到第一次真实握手后这里会自动变成“已接入”，不用重启咕嘎。'
                               % ((base['http_endpoint'] or {}).get('url') or '（端口未就绪）',
                                  len(tools) or '5', entry))
        else:
            base['state'] = 'ui_manage'
            signal = base['official_success_signal']
            endpoint = base['http_endpoint']
            base['message'] = ('未验证：%s 还没有看到 guga 条目。请打开「%s」按卡片添加；'
                               '%s；官方未要求重启。加完点「验证连接」，'
                               '只有真实握手拿到 5 个工具才算连接。'
                               % (name, entry,
                                  ('以界面提示「%s」为准' % signal) if signal else '以官方界面提示为准'))
            if not generated.get('ok') and endpoint.get('state') != 'listening':
                # 既生不成可复制的连接器字段、端点也没起：直说“服务端不可用”，
                # 不要把空白卡片当“按卡片添加”。
                base['state'] = 'server_unavailable'
                base['message'] = (generated.get('message') or '配置片段生成失败') + \
                    '\nHTTP 端点当前：%s（%s）。请先启动咕嘎（或用打包版 GUGA-Desktop.exe --guga-mcp-http）。' % (
                        endpoint.get('state_text') or '未知', endpoint.get('detail') or '')
            elif endpoint.get('state') != 'listening':
                base['message'] += ('\nHTTP 端点当前：%s（%s）。豆包教程里的 URL 要等端点启动后才是最终端口；'
                                    '先启动咕嘎，或执行 GUGA-Desktop.exe --guga-mcp-http 排查。'
                                    % (endpoint.get('state_text') or '未知',
                                       endpoint.get('detail') or ''))
        return base

    expected = generated.get('entry') or {}
    first_error = ''
    for path in targets:
        read = _read_config(path)
        base['config_exists'] = base['config_exists'] or bool(read['exists'])
        if not read['ok']:
            first_error = first_error or '%s：%s' % (path, read['error'])
            continue
        servers = (read['data'] or {}).get(top_key) if isinstance(read['data'], dict) else None
        if not isinstance(servers, dict):
            continue
        if SERVER_NAME not in servers and str(channel.get('server_name') or '') not in servers:
            continue
        name = SERVER_NAME if SERVER_NAME in servers else str(channel.get('server_name'))
        current = servers.get(name)
        missing_exe = _missing_config_exe(current)
        if missing_exe:
            base['added'] = True
            base['entry'] = current if isinstance(current, dict) else {}
            base['config_path'] = str(path)
            base['action'] = 'copy_config'
            base['state'] = 'config_exe_missing'
            base['message'] = ('配置里的咕嘎程序已经不存在：%s。\n'
                               '软件被移动、改名或换了盘符后旧配置会一直启动失败——'
                               '重启目标软件也没用，请重新复制配置覆盖原条目。' % missing_exe)
            return base
        if entry_matches(current, expected):
            base['added'] = True
            base['entry'] = current
            base['config_path'] = str(path)
            cache = _client_tool_cache(app_id, ws)
            base['tool_cache'] = cache
            loaded_after_config = False
            try:
                config_mtime = path.stat().st_mtime
            except OSError:
                config_mtime = 0
            if cache.get('complete'):
                loaded_after_config = float(cache.get('mtime_epoch') or 0) >= config_mtime - 1
            connection = base['connection']
            witness_after_config = bool(connection.get('exists')) and \
                float(connection.get('mtime_epoch') or 0) >= config_mtime - 1
            if base['connected']:
                base['state'] = 'connected'
                base['message'] = '已接入：%s 已连上咕嘎（连接进程 %s）' % (
                    app_id, connection.get('pid'))
            elif loaded_after_config:
                base['state'] = 'connected'
                base['message'] = '已接入：%s 已加载 5 个工具（工具清单 %s，晚于配置写入）' % (
                    app_id, cache.get('mtime'))
            elif witness_after_config:
                base['state'] = 'connected'
                base['message'] = '已接入：%s 已连过咕嘎（最近连接 %s，进程可能已按需关闭）' % (
                    app_id, connection.get('started_at'))
            else:
                base['state'] = 'restart_required'
                note = str(channel.get('restart_note') or ('重启 %s' % app_id))
                base['message'] = '已检测到配置，未验证连接：%s 里已有 guga 条目，但还没看到软件连上；' \
                                  '%s，再点「验证连接」，通过才算真的接上。' % (path, note)
            return base
        base['added'] = False
        base['conflict'] = True
        base['entry'] = current if isinstance(current, dict) else {}
        base['config_path'] = str(path)
        base['state'] = 'config_conflict'
        base['message'] = '%s 里已有同名条目但命令/参数不一致，未覆盖；可点“写入配置”以本机路径更新' % path
        return base

    base['added'] = False
    base['state'] = 'not_configured'
    base['message'] = first_error or ('尚未接入：%s' % (str(targets[0]) if targets else '没有配置路径'))
    return base


def _backup(path: Path) -> str:
    if not path.is_file():
        return ''
    stamp = time.strftime('%Y%m%d-%H%M%S')
    backup = path.with_name(path.name + BACKUP_SUFFIX + stamp)
    shutil.copy2(path, backup)
    return str(backup)


def _approval_path(app_id, workspace=None) -> Path | None:
    """WorkBuddy 等客户端要求“信任本地命令”后才启动；声明里给出审批文件位置。"""
    channel = _channel(app_id)
    value = str(channel.get('approval_file') or '')
    if not value:
        return None
    return Path(_substitute(value, Path(workspace).expanduser() if workspace else default_workspace()))


def approval_token(entry: dict) -> str:
    """复刻 WorkBuddy 的 trustMaterial：command|args排序|env键排序|cwd。"""
    args = ','.join(sorted(str(item) for item in (entry.get('args') or [])))
    env_keys = ','.join(sorted(str(key) for key in (entry.get('env') or {}).keys()))
    return '|'.join([str(entry.get('command') or ''), args, env_keys])


def approve(app_id, workspace=None, entry=None) -> dict:
    """把 guga 条目写进客户端的信任清单（等同于用户在软件里点“允许”）。"""
    app_id = str(app_id or '').strip().casefold()
    path = _approval_path(app_id, workspace)
    if path is None:
        return dict(ok=False, status='not_supported', message='该软件不需要本地信任清单')
    if entry is None:
        entry = mcp_entry(app_id, workspace=workspace)
    if not isinstance(entry, dict) or entry.get('error'):
        return dict(ok=False, status='no_entry', message='没有可授权的 MCP 条目')
    channel = _channel(app_id)
    name = str(channel.get('server_name') or SERVER_NAME)
    digest = hashlib.sha256(approval_token(entry).encode('utf-8')).hexdigest()
    key = digest + '::' + name
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig')) if path.is_file() else {}
    except (OSError, ValueError) as exc:
        return dict(ok=False, status='approval_invalid', path=str(path),
                    message='信任清单读取失败：%s' % str(exc)[:160])
    if not isinstance(data, dict):
        return dict(ok=False, status='approval_invalid', path=str(path),
                    message='信任清单不是 JSON 对象')
    existed = key in data
    data[key] = int(time.time() * 1000)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        backup = _backup(path)
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
    except OSError as exc:
        return dict(ok=False, status='write_failed', path=str(path),
                    message='信任清单写入失败：%s' % str(exc)[:160])
    return dict(ok=True, status='unchanged' if existed else 'approved',
                path=str(path), key=key, backup=backup,
                message='已加入 %s 的信任清单，重启后会自动启动 guga' % app_id)


def apply(app_id, workspace=None, force=False, target=None) -> dict:
    """把 guga 条目写入配置文件（先备份；同名不一致时默认不覆盖，force=True 才更新）。

    ``target`` 可指定某个作用域的配置文件（例如 JoyCode 的用户级配置）；
    客户端要求信任清单时（WorkBuddy），写入后一并加入，等同于用户点“允许”。
    """
    app_id = str(app_id or '').strip().casefold()
    channel = _channel(app_id)
    if not channel:
        return dict(ok=False, status='unknown_app', message='没有通道声明')
    if str(channel.get('registration') or 'file') != 'file':
        decl = app_registry.declaration(app_id) or {}
        name = str(decl.get('name') or app_id)
        entry = str(channel.get('official_entry') or '官方 MCP 界面')
        return dict(ok=False, status='ui_managed',
                    message='%s 没有可写的本地配置；请按官方入口「%s」粘贴片段添加'
                            % (name, entry), action='ui_manage')
    status = channel_status(app_id, workspace=workspace)
    targets = [Path(path) for path in status.get('config_paths') or []]
    if target:
        chosen = Path(target).expanduser()
    elif targets:
        chosen = targets[0]
    else:
        return dict(ok=False, status='no_config_path', message='声明里没有可写的配置路径')
    expected = mcp_entry(app_id, workspace=workspace)
    if expected.get('error'):
        return dict(ok=False, status='server_unavailable', message=expected['error'])
    problems = dev_artifacts(json.dumps(expected, ensure_ascii=False))
    if problems:
        return dict(ok=False, status='dev_path', message=_dev_guard_message(problems))
    read = _read_config(chosen)
    if read['exists'] and not read['ok']:
        return dict(ok=False, status='config_invalid', path=str(chosen), message=read['error'])
    if status.get('added') and status.get('config_path') == str(chosen):
        return dict(ok=True, status='unchanged', path=str(chosen), entry=expected,
                    approval=approve(app_id, workspace=workspace, entry=expected),
                    message='已接入，无需重复写入')
    if status.get('conflict') and not force:
        return dict(ok=False, status='conflict', path=str(chosen),
                    message='已有同名但参数不同的条目；确认后再用 force=True 覆盖')
    data = copy.deepcopy(read['data']) if isinstance(read.get('data'), dict) else {}
    top_key = str(channel.get('config_format', {}).get('top_key') or 'mcpServers')
    servers = data.get(top_key)
    servers = copy.deepcopy(servers) if isinstance(servers, dict) else {}
    name = str(channel.get('server_name') or SERVER_NAME)
    existed = name in servers
    servers[name] = expected
    data[top_key] = servers
    backup = _backup(chosen)
    try:
        chosen.parent.mkdir(parents=True, exist_ok=True)
        temporary = chosen.with_suffix(chosen.suffix + '.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(chosen)
    except OSError as exc:
        return dict(ok=False, status='write_failed', path=str(chosen),
                    message='写入失败：%s' % str(exc)[:160])
    return dict(ok=True, status='updated' if existed else 'added',
                path=str(chosen), backup=backup, entry=expected,
                approval=approve(app_id, workspace=workspace, entry=expected),
                message='已写入 %s（%s 后生效；首次连接需在「MCP 服务管理 → 我的 MCP」点「信任」）'
                        % (chosen, str(_channel(app_id).get('restart_note') or '重启目标软件')))


def remove(app_id, workspace=None) -> dict:
    """撤掉 guga 条目（同样先备份）；用于回滚验证或用户手动关闭。"""
    app_id = str(app_id or '').strip().casefold()
    channel = _channel(app_id)
    if not channel or str(channel.get('registration') or 'file') != 'file':
        return dict(ok=False, status='not_file_registration', message='该软件没有本地配置可撤')
    name = str(channel.get('server_name') or SERVER_NAME)
    top_key = str(channel.get('config_format', {}).get('top_key') or 'mcpServers')
    changed = []
    for path in config_targets(app_id, workspace=workspace):
        read = _read_config(path)
        if not read['ok'] or not isinstance(read.get('data'), dict):
            continue
        servers = read['data'].get(top_key)
        if not isinstance(servers, dict) or name not in servers:
            continue
        backup = _backup(path)
        servers.pop(name)
        try:
            temporary = path.with_suffix(path.suffix + '.tmp')
            temporary.write_text(json.dumps(read['data'], ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(path)
        except OSError as exc:
            return dict(ok=False, status='write_failed', message=str(exc)[:160], path=str(path))
        changed.append(dict(path=str(path), backup=backup))
    return dict(ok=True, status='removed' if changed else 'not_present',
                changed=changed,
                message=('已从 %d 个配置移除 guga 条目' % len(changed)) if changed else '配置里没有 guga 条目')


def open_config_dir(app_id, workspace=None) -> dict:
    """打开配置目录；目录不存在时明确返回原因，不弹假成功。"""
    app_id = str(app_id or '').strip().casefold()
    channel = _channel(app_id)
    if not channel or str(channel.get('registration') or 'file') != 'file':
        return dict(ok=False, status='ui_managed',
                    message='豆包没有本地配置目录；请打开豆包设置里的连接器页面')
    targets = config_targets(app_id, workspace=workspace)
    if not targets:
        return dict(ok=False, status='no_config_path', message='声明里没有配置路径')
    target = targets[0]
    folder = target.parent
    if not folder.is_dir():
        return dict(ok=False, status='missing_dir', path=str(folder),
                    message='配置目录还不存在：%s（先启动一次 %s 再回来）' % (folder, app_id))
    try:
        os.startfile(str(folder))
    except (OSError, AttributeError) as exc:
        return dict(ok=False, status='open_failed', path=str(folder), message=str(exc)[:160])
    return dict(ok=True, status='opened', path=str(folder), message='已打开 %s' % folder)


# ---------------------------------------------------------------------------
# 真实握手（软件侧“能连上”的证据）
# ---------------------------------------------------------------------------

def _handshake_result(names, probe) -> dict:
    text = ''
    for item in (getattr(probe, 'content', None) or []):
        text += str(getattr(item, 'text', '') or '')
    required = ('report_app', 'get_task', 'report_progress', 'submit_artifact', 'ask_guga')
    missing = [name for name in required if name not in names]
    if missing:
        return dict(ok=False, status='tool_mismatch', tools=names, get_task_text=text[:600],
                    message='握手成功但工具清单不符：缺少 %s（应有 5 个，含 report_app）'
                            % '、'.join(missing))
    return dict(ok=True, status='connected', tools=names, get_task_text=text[:600],
                message='握手成功，工具：%s' % ', '.join(names))


def _async_probe_http(url: str, timeout: float) -> dict:
    """真实连一次本机 HTTP 端点：initialize + list_tools + get_task。"""
    from mcp import ClientSession, types
    from mcp.client.streamable_http import streamable_http_client

    async def run():
        async with streamable_http_client(url) as (read, write):
            # 自检客户端必须自报名字：服务端用 clientInfo.name 区分“咕嘎自检”和
            # “豆包真实客户端”。不自报就会被当成豆包本人，面板立刻误显示“已接入”。
            async with ClientSession(
                    read, write,
                    client_info=types.Implementation(name='guga-selfcheck', version='1.0')) as session:
                await session.initialize()
                tools = await session.list_tools()
                probe = await session.call_tool('get_task', {}, read_timeout_seconds=timeout)
                return [tool.name for tool in tools.tools], probe

    try:
        names, probe = asyncio.run(asyncio.wait_for(run(), timeout))
    except Exception as exc:
        return dict(ok=False, status='handshake_failed',
                    message='%s: %s' % (type(exc).__name__, str(exc)[:200]))
    return _handshake_result(names, probe)


def _async_probe(entry: dict, workspace: Path, timeout: float) -> dict:
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    env = {key: str(value) for key, value in dict(os.environ).items()}
    env.update({str(k): str(v) for k, v in (entry.get('env') or {}).items()})
    env['GUGA_WORKSPACE'] = str(workspace)
    params = StdioServerParameters(command=str(entry['command']),
                                   args=[str(item) for item in entry.get('args') or []],
                                   env=env)

    async def run():
        async with Client(params) as client:
            tools = await client.list_tools()
            names = [tool.name for tool in tools.tools]
            probe = await client.call_tool('get_task', {}, read_timeout_seconds=timeout)
            return names, probe

    try:
        names, probe = asyncio.run(asyncio.wait_for(run(), timeout=timeout))
    except Exception as exc:
        return dict(ok=False, status='handshake_failed',
                    message='%s: %s' % (type(exc).__name__, str(exc)[:200]))
    return _handshake_result(names, probe)


def verify_connection(app_id=None, entry=None, workspace=None, timeout=25) -> dict:
    """按配置里的真实命令拉起一次 MCP server，做 initialize + list_tools + get_task。"""
    ws = Path(workspace).expanduser() if workspace else default_workspace()
    if entry is None:
        status = channel_status(app_id, workspace=ws)
        entry = status.get('entry') if isinstance(status.get('entry'), dict) else {}
        if not entry:
            generated = snippet(app_id, workspace=ws)
            entry = generated.get('entry') or {}
    if isinstance(entry, dict) and str(entry.get('transport') or '').upper() == 'HTTP':
        url = str(entry.get('url') or '')
        if not url:
            return dict(ok=False, status='no_entry',
                        message='HTTP 条目缺少 url（请先启动咕嘎，让端点写入真实端口）')
        result = _async_probe_http(url, float(timeout))
        if app_id:
            _record_verify(app_id, result, url)
        return result
    if not isinstance(entry, dict) or not entry.get('command'):
        return dict(ok=False, status='no_entry', message='没有可用的 MCP 条目（先接入或设置 GUGA_MCP_PYTHON）')
    if not entry.get('args') and entry.get('params'):
        entry = dict(entry, args=list(entry.get('params') or []))
    if not entry.get('args'):
        return dict(ok=False, status='no_entry',
                    message='条目缺少 args（请检查配置里的启动参数是否为 --guga-mcp）')
    result = _async_probe(entry, ws, float(timeout))
    if app_id:
        _record_verify(app_id, result, '')
    return result


# ---------------------------------------------------------------------------
# 首次接入教程数据（界面层只负责渲染）
# ---------------------------------------------------------------------------

def onboarding(apps=None, workspace=None, force_probe=False) -> dict:
    """给首次启动的接入教程：装了哪些、怎么接、接没接上。"""
    ws = Path(workspace).expanduser() if workspace else default_workspace()
    targets = [str(item).strip().casefold() for item in (apps or DEFAULT_ORDER) if str(item).strip()]
    command = server_command(workspace=ws, force=force_probe)
    source_python = python_command(force=force_probe)
    script = server_script()
    items = []
    for app_id in targets:
        decl = app_registry.declaration(app_id)
        if not decl:
            continue
        probe = app_registry.probe(app_id, force=force_probe)
        status = channel_status(app_id, workspace=ws)
        download = copy.deepcopy(decl.get('download') or {})
        installed = probe.get('status') == 'ready'
        if not installed:
            action = 'download_first'
        elif status.get('state') == 'connected':
            action = 'already_added'
        elif status.get('state') == 'restart_required':
            action = 'restart_required'
        elif status.get('action') == 'ui_manage':
            action = 'ui_manage'
        elif not status.get('added'):
            action = 'add_config'
        else:
            action = 'restart_required'
        items.append(dict(
            app=app_id, name=str(decl.get('name') or app_id),
            installed=installed, probe_status=probe.get('status'),
            exe=probe.get('exe', ''), capabilities=list(decl.get('capabilities') or []),
            channel=status, action=action, state=status.get('state', ''),
            download=download, probe_message=probe.get('message', ''),
        ))
    ready = [item for item in items if item['installed']]
    connected = [item for item in items if item['state'] == 'connected']
    return dict(
        contract=CONTRACT, workspace=str(ws),
        server=dict(mode=command.get('mode'), ok=bool(command.get('ok')),
                   command=command.get('command', ''), args=list(command.get('args') or []),
                   source=command.get('source', ''), error=command.get('error', ''),
                   python=source_python, script=script),
        apps=items,
        summary=dict(total=len(items), installed=len(ready), added=len(connected),
                     connected=len(connected),
                     restart=sum(1 for item in items if item['state'] == 'restart_required'),
                     ui_managed=sum(1 for item in items if item['action'] == 'ui_manage')),
        instructions=[
            'MCP 只负责“发现”：软件连上来后调用 report_app 自报 exe 路径，咕嘎记住，以后直接启动。',
            '指挥（派单）仍走界面自动化，不会因为接了 MCP 就改成 MCP 派单。',
            'WorkBuddy：在它自己的设置里添加 MCP（入口和叫法随版本变化）；复制配置后按官方提示保存。',
            'JoyCode：官方只有界面入口「MCP市场 → 全局配置文件 → +MCP配置」，未公布文件路径；本工具不再往 {workspace}/.joycode/mcp.json 写文件，只生成含 "type": "stdio" 的可粘贴 JSON。',
            '豆包：官方入口是「插件·技能·伙伴 → 插件 → 右上角 + 添加 → 新建自定义连接器」。首选 HTTP：服务器名称 guga、传输类型 HTTP、服务器 URL http://127.0.0.1:<实际端口>/mcp、Headers 留空；端点不可用时改填同一份文本里的 STDIO 备选。连接器只在「工作」模式生效，对话模式和 /、@ 都搜不到；详见《豆包连接器配置教程.txt》。',
            '三态口径：已检测到配置 / 界面已显示 guga / 未验证；只有真实握手拿到 5 个工具才显示“已连接”，其余不得写“已接入”。',
            '复制出来的片段只含本地命令/参数，不含任何模型 API Key，也不含 Python 路径或开发目录。',
            '配置里的命令就是当前解压出来的 GUGA-Desktop.exe 自身（参数 --guga-mcp）；换解压目录后重新复制一次即可。',
        ],
        generated_at=time.strftime('%Y-%m-%dT%H:%M:%S'),
    )


def nudge(apps=None, workspace=None, force_probe=False) -> dict:
    """启动时的主动提醒数据：哪些已安装但还没接入（含待重启），哪些已接入，哪些没装。"""
    data = onboarding(apps=apps, workspace=workspace, force_probe=force_probe)
    items = data.get('apps') or []
    pending = []
    for item in items:
        if not item.get('installed'):
            continue
        if item.get('action') == 'already_added' or item.get('state') == 'connected':
            continue
        pending.append(dict(
            app=item.get('app'), name=item.get('name'), state=item.get('state') or '',
            action=item.get('action'),
            message=(item.get('channel') or {}).get('message') or ''))
    return dict(
        contract=data.get('contract'), workspace=data.get('workspace'),
        apps=items, pending=pending,
        installed=sum(1 for item in items if item.get('installed')),
        connected=sum(1 for item in items
                      if item.get('action') == 'already_added' or item.get('state') == 'connected'),
        not_installed=sum(1 for item in items if not item.get('installed')))


if __name__ == '__main__':
    data = onboarding(force_probe=True)
    print(json.dumps(data, ensure_ascii=False, indent=2))
