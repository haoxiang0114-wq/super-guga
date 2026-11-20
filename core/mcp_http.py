# -*- coding: utf-8 -*-
"""咕嘎 MCP 的 streamable HTTP 端点：状态、选端口、随手启动。

豆包自定义连接器只接受 HTTP / STDIO；本机客户端直连 127.0.0.1。
本模块只负责“端点状态与进程管理”，工具集与 stdio 共用
``core.adapters.guga_mcp_server``，绝不另写一套工具。

安全约束：
  · 只绑 127.0.0.1，永远不绑 0.0.0.0；
  · 端口从 18760 起固定递增回退，实际端口写进 status 文件和复制片段；
  · 启动失败/端口耗尽都写明确状态，接入面板据此显示。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

HOST = '127.0.0.1'
DEFAULT_PORT = 18760
MAX_TRIES = 20
_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
_ROOT = Path(__file__).resolve().parents[1]


def _paths():
    try:
        from core import guga_paths
        return guga_paths.RUNTIME, guga_paths.CONFIG
    except Exception:
        base = Path(os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA') or Path.home())
        return base / 'GUGA' / 'runtime', base / 'GUGA' / 'ai_config.json'


def status_path() -> Path:
    runtime, _config = _paths()
    return Path(runtime) / 'mcp_http.json'


def enabled() -> bool:
    """默认开启；环境变量或 ai_config.json 的 mcp_http_enabled 可关。"""
    flag = str(os.environ.get('GUGA_MCP_HTTP', '')).strip().lower()
    if flag in ('0', 'false', 'off', 'no'):
        return False
    _runtime, config = _paths()
    try:
        data = json.loads(Path(config).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return True
    return bool(data.get('mcp_http_enabled', True))


def port_available(port: int, host: str = HOST) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=0.3):
            return False          # 已有人在监听 = 被占用
    except OSError:
        return True


def candidate_port(start: int = DEFAULT_PORT, tries: int = MAX_TRIES) -> int | None:
    for port in range(int(start), int(start) + int(tries)):
        if port_available(port):
            return port
    return None


def _is_listening(port: int) -> bool:
    try:
        with socket.create_connection((HOST, int(port)), timeout=0.3):
            return True
    except OSError:
        return False


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:
        return False


def read_record() -> dict:
    try:
        data = json.loads(status_path().read_text(encoding='utf-8-sig'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def configured_start() -> int:
    """起始端口：ai_config.json 的 mcp_http_port，默认 18760。"""
    _runtime, config = _paths()
    try:
        data = json.loads(Path(config).read_text(encoding='utf-8-sig'))
        return int(data.get('mcp_http_port') or DEFAULT_PORT)
    except (OSError, ValueError, TypeError):
        return DEFAULT_PORT


def status() -> dict:
    """返回可直接展示的端点状态：listening / not_started / conflict / error。

    ``client`` / ``probe`` 提到顶层：接入面板据此判断“是否真有客户端来过”，
    不再需要钻到 ``record`` 里取，避免“握手写进 mcp_http.json 但面板读不到”。
    """
    record = read_record()
    requested = int(record.get('requested_port') or configured_start())
    port = record.get('port')
    state = str(record.get('state') or '')
    client = record.get('client') if isinstance(record.get('client'), dict) else {}
    probe = record.get('probe') if isinstance(record.get('probe'), dict) else {}
    evidence = dict(client=client, probe=probe,
                    evidence_updated_at=record.get('evidence_updated_at') or '',
                    tools_listed_at=record.get('tools_listed_at') or '',
                    client_tools_listed_at=record.get('client_tools_listed_at') or '')
    if state == 'conflict':
        return dict(state='conflict',
                    state_text='端口冲突（%d–%d 全被占用）' % (requested, requested + MAX_TRIES - 1),
                    port=None, requested_port=requested, host=HOST, url='',
                    detail='没有可用端口，豆包 HTTP 连接器无法启动。',
                    hint='关掉占用 %d–%d 的程序，或在 ai_config.json 里把 '
                         'mcp_http_port 改成别的起始端口后重启咕嘎。'
                         % (requested, requested + MAX_TRIES - 1), record=record, **evidence)
    if state == 'error':
        return dict(state='error', state_text='启动失败',
                    port=port, requested_port=requested, host=HOST, url=record.get('url') or '',
                    detail=str(record.get('error') or 'MCP HTTP 端点启动失败')[:300],
                    hint='右键「导出诊断」把日志发回；或先用 STDIO 连接器。', record=record, **evidence)
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 0
    if not port:
        fallback = candidate_port(requested)
        return dict(state='not_started', state_text='未启动',
                    port=fallback, requested_port=requested, host=HOST,
                    url=('http://%s:%s/mcp' % (HOST, fallback)) if fallback else '',
                    detail='HTTP 端点还没启动（咕嘎主程序启动时会自动拉起）。',
                    hint='启动咕嘎后会自动监听；也可用 GUGA-Desktop.exe --guga-mcp-http 单独排查。',
                    record=record, **evidence)
    if _is_listening(port) and _pid_alive(record.get('pid')):
        return dict(state='listening', state_text='监听中',
                    port=port, requested_port=requested, host=HOST,
                    url='http://%s:%d/mcp' % (HOST, port),
                    detail='豆包需在「工作」模式下才会调用该连接器；对话模式、/ 和 @ 搜不到。',
                    hint='', record=record, **evidence)
    fallback = candidate_port(requested)
    return dict(state='not_started', state_text='未启动',
                port=port, requested_port=requested, host=HOST,
                url='http://%s:%d/mcp' % (HOST, port),
                detail='状态文件在，但端口没有进程监听（上次可能异常退出）。',
                hint='重新启动咕嘎会自动拉起；或执行 GUGA-Desktop.exe --guga-mcp-http。',
                record=record, **evidence)


def _child_command() -> list:
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--guga-mcp-http']
    entry = _ROOT / 'tools' / 'comp_entry.py'
    return [sys.executable, str(entry), '--guga-mcp-http']


def start(wait_seconds: float = 4.0) -> dict:
    """若未启动则拉起子进程；返回最终状态（不阻塞主界面）。"""
    current = status()
    if current['state'] == 'listening':
        return current
    if not enabled():
        current.update(state='disabled', state_text='已关闭',
                       detail='ai_config.json 里 mcp_http_enabled=false 或环境变量 GUGA_MCP_HTTP=0。',
                       hint='需要豆包 HTTP 接入时把它改回 true 再重启咕嘎。')
        return current
    try:
        subprocess.Popen(_child_command(), close_fds=True,
                         creationflags=_NO_WINDOW,
                         env=dict(os.environ, PYTHONIOENCODING='utf-8'))
    except Exception as exc:
        current.update(state='error', state_text='启动失败',
                       detail='%s: %s' % (type(exc).__name__, str(exc)[:180]),
                       hint='右键「导出诊断」把日志发回；或先用 STDIO 连接器。')
        return current
    deadline = time.monotonic() + max(0.5, float(wait_seconds))
    while time.monotonic() < deadline:
        time.sleep(0.2)
        latest = status()
        if latest['state'] in ('listening', 'conflict', 'error'):
            return latest
    return status()


def stop() -> dict:
    """只结束确认是本模块拉起的 --guga-mcp-http 进程。"""
    record = read_record()
    pid = record.get('pid')
    if not _pid_alive(pid):
        return dict(ok=False, error='没有存活的 HTTP 端点进程')
    try:
        import psutil
        proc = psutil.Process(int(pid))
        argv = ' '.join(proc.cmdline())
        if '--guga-mcp-http' not in argv:
            return dict(ok=False, error='进程参数不匹配，拒绝结束：%s' % argv[:120])
        proc.terminate()
        proc.wait(timeout=4)
        return dict(ok=True, pid=int(pid))
    except Exception as exc:
        return dict(ok=False, error='%s: %s' % (type(exc).__name__, str(exc)[:160]))
