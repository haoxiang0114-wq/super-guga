# -*- coding: utf-8 -*-
"""打包版最小日志：windowed 构建没有 stderr，把启动关键节点与未捕获异常写到
用户目录（%USERPROFILE%\\.guga\\logs\\startup.log）。所有写入失败一律静默，
绝不影响启动；单文件超过 512KB 自动轮转一份。
"""
import os
import sys
import threading
import traceback
from datetime import datetime

MAX_BYTES = 512 * 1024
_lock = threading.Lock()


def log_dir():
    """日志与其它运行数据同落点：受 GUGA_HOME 控制，默认 %LOCALAPPDATA%\\GUGA\\logs。

    与 core.guga_paths.BASE 保持一致，避免“设了 GUGA_HOME，日志仍写死用户目录”
    导致隔离验收误判。guga_paths 不可用时退回旧落点，绝不阻断启动。
    """
    try:
        from core import guga_paths
        return os.path.join(str(guga_paths.BASE), 'logs')
    except Exception:
        base = os.environ.get('USERPROFILE') or os.path.expanduser('~') or os.getcwd()
        return os.path.join(base, '.guga', 'logs')


def log_path():
    return os.path.join(log_dir(), 'startup.log')


def log(event, detail=''):
    try:
        path = log_path()
        folder = os.path.dirname(path)
        os.makedirs(folder, exist_ok=True)
        try:
            if os.path.getsize(path) > MAX_BYTES:
                os.replace(path, path + '.1')
        except OSError:
            pass
        line = '%s [%s] %s\n' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                 event, str(detail)[:4000])
        with _lock:
            with open(path, 'a', encoding='utf-8') as handle:
                handle.write(line)
    except Exception:
        pass


def log_exc(event, exc_info=None):
    try:
        text = ''.join(traceback.format_exception(*(exc_info or sys.exc_info())))
    except Exception:
        text = 'traceback unavailable'
    log(event, text)


def install():
    """接管主线程与子线程的未捕获异常；任何失败都不抛出。"""
    try:
        def _hook(exc_type, exc, tb):
            try:
                log('uncaught', ''.join(traceback.format_exception(exc_type, exc, tb)))
            except Exception:
                pass
        sys.excepthook = _hook
    except Exception:
        pass
    try:
        def _thread_hook(args):
            try:
                log('thread-uncaught', ''.join(traceback.format_exception(
                    args.exc_type, args.exc_value, args.exc_traceback)))
            except Exception:
                pass
        threading.excepthook = _thread_hook
    except Exception:
        pass
