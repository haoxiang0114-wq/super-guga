# -*- coding: utf-8 -*-
"""咕嘎控制暂停闸 —— 所有键鼠事件发出前的统一执行点。

分工（与 core/control_session.py 对齐）：
  * 界面层（标识条、聊天窗）负责写暂停/中止请求；
  * 本模块只读 `should_pause() / should_stop()`，并在真正发事件前拦截：
      - 中止：抛 Stopped 退出本轮，一次事件都不发；
      - 暂停：停在原地等待，一个事件都不发（不把手上这个动作做完），
        恢复后由调用方从安全点继续。
  * 第一次拦下时调用 `acknowledge_pause() / acknowledge_stop()` 留痕，
    让标识条从「暂停中/中止中」进入「已暂停/已中止」，不会一直转圈。

恢复时不盲打：`resume()` 会核对目标窗口仍在、焦点已回到目标；
窗口没了或切不回来就保持暂停并返回原因，让上层重新定位。
"""

import ctypes
import functools
import re
import threading
import time


class Paused(RuntimeError):
    """用户叫停。任务应记为「已暂停」，不能记为完成或失败。"""


class Stopped(RuntimeError):
    """用户中止本轮。任务记为已中止，不能记为完成。"""


global _own_paused, _own_reason, _pause_notified

_LOCK = threading.RLock()
_own_paused = False
_own_reason = ''
_pause_notified = False
_target = {'hwnd': 0, 'title': ''}
_session_cache = [None, False]
_LISTENERS = {'pause': None, 'resume': None}


def control():
    """core.control_session 的单例；模块不在时返回 None（不影响离线可用）。"""
    if not _session_cache[1]:
        try:
            from core import control_session
            _session_cache[0] = control_session.session
        except Exception:
            _session_cache[0] = None
        _session_cache[1] = True
    return _session_cache[0]


def _snapshot():
    sess = control()
    if sess is None:
        return {}
    try:
        return sess.snapshot()
    except Exception:
        return {}


def should_pause():
    sess = control()
    if sess is not None:
        snap = _snapshot()
        state = str(snap.get('state') or '')

        if state in ('stopping', 'stopped'):
            _drop_own_pause()
            return False
        if state in ('idle', 'done', 'error'):
            return _own_paused
        try:
            if sess.should_pause():
                return True
        except Exception:
            pass
    return _own_paused


def should_stop():
    sess = control()
    if sess is None:
        return False
    state = str(_snapshot().get('state') or '')

    if state in ('idle', 'done', 'error', 'stopped'):
        return False
    try:
        if sess.should_stop():
            _drop_own_pause()
            return True
    except Exception:
        pass
    return False


def _drop_own_pause():
    global _own_paused, _own_reason, _pause_notified
    with _LOCK:
        _own_paused = False
        _own_reason = ''
        _pause_notified = False


def is_paused():
    return should_pause()


def _acknowledge(stop=False):
    """第一次拦下时留痕；之后不重复改 revision。"""
    sess = control()
    if sess is None:
        return
    snap = _snapshot()
    try:
        if stop:
            if snap.get('state') != 'stopped':
                sess.acknowledge_stop('执行器已在安全点停手')
                return
        elif snap.get('state') != 'paused':
            sess.acknowledge_pause()
            return
    except Exception:
        pass


def blocked():
    """返回 '' / 'pause' / 'stop'。"""
    if should_stop():
        return 'stop'
    if should_pause():
        return 'pause'
    return ''


def assert_active():
    """发键鼠事件前的强制检查点。

    中止：回执后抛 Stopped，一次事件都不发；
    暂停：回执后停在原地等待，期间不发出任何事件；恢复（或中止）后才返回/抛出。
    """
    global _pause_notified
    state = blocked()
    if state == 'stop':
        _acknowledge(stop=True)
        _notify('stop', dict(reason='用户中止本轮操作'))
        raise Stopped('已中止控制：用户终止了本轮操作，控制权已还给用户')
    if state != 'pause':
        return
    _acknowledge()
    if not _pause_notified:
        _pause_notified = True
        _notify('pause', dict(reason=_own_reason or '用户叫停', target=target_state()))
    while True:
        if should_stop():
            _acknowledge(stop=True)
            _notify('stop', dict(reason='用户中止本轮操作'))
            raise Stopped('已中止控制：用户终止了本轮操作，控制权已还给用户')
        if not should_pause():
            return
        time.sleep(0.05)


def set_listeners(pause=None, resume=None):
    """给本地执行方挂暂停/恢复回调（用于同步云端台账）；传 None 取消。"""
    with _LOCK:
        _LISTENERS['pause'] = pause if callable(pause) else None
        _LISTENERS['resume'] = resume if callable(resume) else None


def _notify(kind, info):
    with _LOCK:
        listener = _LISTENERS.get(kind)
    if not callable(listener):
        return
    try:
        listener(dict(info))
    except Exception:
        pass


_STRIP = re.compile('[\\s,，。.!！?？;；:：、~～"\\\'“”‘’()（）\\[\\]【】<>《》…·—\\-]')
_PREFIX = '(?:请|麻烦|先|马上|赶紧|快|咕嘎|小嘎)*'
_PAUSE_RE = re.compile(
    '^' + _PREFIX
    + '(?:暂停|停下|停下来|停一下|停一停|别动|别动手|别动鼠标|别动电脑|别操作|别操作了|不要动|不要动了|不要操作|住手|停手|停止操作|停止控制)(?:一下|了|吧|啊|呀|呢|呗)*$')

_RESUME_RE = re.compile(
    '^(?:请|麻烦|可以|好了|那就|现在)?(?:继续|接着|恢复)(?:吧|了|一下|任务|操作|控制|干活|做|来|干|弄)*$')


def parse_control_command(text):
    """短句叫停/继续识别：只认完整短句，避免把长句里的词误当指令。"""
    value = _STRIP.sub('', str(text or ''))
    if not value or len(value) > 24:
        return ''
    if _PAUSE_RE.match(value):
        return 'pause'
    if _RESUME_RE.match(value):
        return 'resume'
    return ''


def pause(reason=''):
    """置暂停。界面会话在跑就同步写它的 pause_requested。"""
    global _own_paused, _own_reason
    with _LOCK:
        _own_paused = True
        _own_reason = str(reason or '用户叫停')[:80]
    sess = control()
    if sess is not None:
        try:
            sess.request_pause('user')
        except Exception:
            pass
    return True


def resume(force=False):
    """恢复前确认目标窗口仍在、焦点已回到目标；否则保持暂停并说明原因。"""
    global _own_paused, _own_reason, _pause_notified
    snap = _snapshot()
    if snap.get('stop_requested') or snap.get('state') in ('stopping', 'stopped'):
        return dict(ok=False, reason='stopped',
                    message='本轮已经中止，不能继续；请重新下达指令。')
    state = target_state()
    refocused = False
    if state['hwnd']:
        if not state['exists']:
            return dict(ok=False, reason='target_gone', target=state,
                        message='目标窗口已经关闭，不能盲打；请重新指定要操作的窗口。')
        if not state['foreground']:
            if force or _refocus(state['hwnd']):
                refocused = True
            else:
                return dict(ok=False, reason='focus_changed', target=state,
                            message='目标窗口焦点不在了，且没能切回去；先重新定位目标窗口再继续。')
    with _LOCK:
        _own_paused = False
        _own_reason = ''
        _pause_notified = False
    sess = control()
    if sess is not None:
        try:
            sess.resume('user')
        except Exception:
            pass
    _notify('resume', dict(target=state, refocused=refocused))
    return dict(ok=True, target=state, refocused=refocused,
                message='已恢复控制；目标窗口与焦点都已重新确认。')


def note_target(hwnd, title=''):
    try:
        hwnd = int(hwnd or 0)
    except (TypeError, ValueError):
        hwnd = 0
    if not hwnd:
        return
    with _LOCK:
        _target['hwnd'] = hwnd
        if title:
            _target['title'] = str(title)[:80]


def target():
    with _LOCK:
        return dict(_target)


def target_state():
    item = target()
    hwnd = item['hwnd']
    if not hwnd:
        return dict(hwnd=0, title=item['title'], exists=None, foreground=None)
    try:
        user = ctypes.windll.user32
        exists = bool(user.IsWindow(hwnd))
        foreground = bool(exists and user.GetForegroundWindow() == hwnd)
    except Exception:
        exists, foreground = None, None
    return dict(hwnd=hwnd, title=item['title'], exists=exists, foreground=foreground)


def _refocus(hwnd):
    try:
        user = ctypes.windll.user32
        if not user.IsWindow(hwnd):
            return False
        if user.IsIconic(hwnd):
            user.ShowWindow(hwnd, 9)
        user.SetForegroundWindow(hwnd)
        time.sleep(0.1)
        return user.GetForegroundWindow() == hwnd
    except Exception:
        return False


def reset():
    """测试/退出用：清本模块状态，不回写界面会话。"""
    global _own_paused, _own_reason, _pause_notified
    with _LOCK:
        _own_paused = False
        _own_reason = ''
        _pause_notified = False
        _target['hwnd'] = 0
        _target['title'] = ''


_MOUSE_KEYS = ('click', 'doubleClick', 'tripleClick', 'rightClick', 'middleClick',
               'moveTo', 'moveRel', 'dragTo', 'dragRel', 'mouseDown', 'mouseUp',
               'scroll', 'hscroll', 'vscroll', 'hkey', 'hotkey', 'press',
               'keyDown', 'keyUp')

_TYPE_KEYS = ('typewrite', 'write')


def install_pyautogui(module=None):
    """把 pyautogui 的公开输入函数换成先查暂停再发送的版本（幂等）。"""
    if module is None:
        try:
            import pyautogui as module
        except Exception:
            return False
    if getattr(module, '_guga_input_gated', False):
        return True
    for name in _MOUSE_KEYS:
        original = getattr(module, name, None)
        if not callable(original):
            continue
        setattr(module, name, _wrap(original))
    for name in _TYPE_KEYS:
        original = getattr(module, name, None)
        if not callable(original):
            continue
        setattr(module, name, _wrap_typing(original))
    module._guga_input_gated = True
    return True


def _wrap(original):
    @functools.wraps(original)
    def gated(*args, **kwargs):
        assert_active()
        return original(*args, **kwargs)
    return gated


def _wrap_typing(original):
    @functools.wraps(original)
    def gated(message, *args, **kwargs):
        assert_active()
        if not isinstance(message, str):
            return original(message, *args, **kwargs)
        for char in message:
            assert_active()
            original(char, *args, **kwargs)
    return gated
