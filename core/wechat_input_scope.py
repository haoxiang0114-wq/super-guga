"""Reserve WeChat input for the verified contact / confirmation workflow."""
from contextlib import contextmanager
import threading


_scope = threading.local()
WECHAT_PROCESSES = frozenset(('wechat.exe', 'weixin.exe'))


@contextmanager
def verified_wechat_input(hwnd):
    previous = getattr(_scope, 'windows', ())
    _scope.windows = (*previous, int(hwnd))
    try:
        yield
    finally:
        _scope.windows = previous


def require_wechat_input_scope(hwnd):
    import psutil
    import win32process

    _, pid = win32process.GetWindowThreadProcessId(int(hwnd))
    try:
        name = psutil.Process(pid).name().casefold()
    except (OSError, psutil.Error) as exc:
        raise RuntimeError('无法核对前台程序，未发送输入') from exc
    if name in WECHAT_PROCESSES and int(hwnd) not in getattr(_scope, 'windows', ()):
        raise RuntimeError('微信输入需要走专用微信工具，由它核对聊天对象和发送结果')
