# -*- coding: utf-8 -*-
"""咕嘎 × DSH 一体化启动：打开咕嘎时确保本机 DSH 在跑。

规则：
  * 3080 已经在监听 → 直接复用，不重复拉起（用户可能已开着 DSH）。
  * 没在监听 → 拉起本机已安装的 DSH web，并等到端口就绪。
  * 任何失败都只记日志，绝不阻断桌宠启动。

对外只有两个入口：`ensure_dsh_web()`（同步，返回结果字典）和
`ensure_dsh_web_async()`（后台线程，启动链路用这个）。
"""
import os
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

DSH_PORT = 3080
DSH_HOST = '127.0.0.1'
READY_TIMEOUT = 45.0
_LOG = Path(__file__).resolve().parents[1] / 'dsh_bootstrap.log'


def _log(line):
    """把结果写进 dsh_bootstrap.log；日志失败不影响主流程。"""
    try:
        with open(_LOG, 'a', encoding='utf-8') as handle:
            handle.write('%s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), line))
    except OSError:
        pass


def port_open(port=DSH_PORT, host=DSH_HOST, timeout=0.4):
    """3080 是否已经有服务在监听。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def dsh_cli():
    """本机安装的 DSH 命令行入口；没装返回 None。"""
    cli = Path.home() / '.local' / 'bin' / 'node_modules' / '@deepseek-ai' / 'dsh' / 'lib' / 'bin.js'
    return cli if cli.is_file() else None


def ensure_dsh_web(port=DSH_PORT, timeout=READY_TIMEOUT, open_window=True):
    """确保 DSH web 在 port 上就绪。

    open_window=True 时由 DSH 自己打开界面窗口（咕嘎一启动，DSH 跟着出来）；
    返回 {'started': bool, 'reason': str, 'port': int}。
    """
    if port_open(port):
        _log('already-running port=%d' % port)
        return {'started': False, 'reason': 'already-running', 'port': port}

    cli = dsh_cli()
    if cli is None:
        _log('dsh-not-installed')
        return {'started': False, 'reason': 'dsh-not-installed', 'port': port}

    node = shutil.which('node')
    if not node:
        _log('node-not-found')
        return {'started': False, 'reason': 'node-not-found', 'port': port}

    args = [node, str(cli), 'web']
    if not open_window:
        args.append('--no-open')

    flags = 0
    if os.name == 'nt':
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'DETACHED_PROCESS', 0)

    try:
        logs = Path.home() / '.dsh' / 'logs'
        logs.mkdir(parents=True, exist_ok=True)
        launch_log = logs / ('launch-guga-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.log')
        with launch_log.open('xb') as output:
            subprocess.Popen(
                args, cwd=str(Path.home()), stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, creationflags=flags, close_fds=True,
            )
    except OSError as error:
        _log('spawn-failed %s' % error)
        return {'started': False, 'reason': 'spawn-failed: %s' % error, 'port': port}

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_open(port):
            _log('launched port=%d' % port)
            return {'started': True, 'reason': 'launched', 'port': port}
        time.sleep(0.5)

    _log('timeout after %.0fs' % timeout)
    return {'started': False, 'reason': 'timeout', 'port': port}


def ensure_dsh_web_async(**kwargs):
    """后台线程版：启动链路调用它，永不抛异常、永不阻塞 Qt 启动。"""
    def _run():
        try:
            ensure_dsh_web(**kwargs)
        except Exception as error:  # noqa: BLE001 - 启动期任何异常都不能冒泡
            _log('unexpected %r' % (error,))

    thread = threading.Thread(target=_run, name='guga-dsh-bootstrap', daemon=True)
    thread.start()
    return thread
