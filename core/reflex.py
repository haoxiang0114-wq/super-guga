"""Model-free reflex arc: referent desktop actions that need no reasoning.

``match()`` is a pure parser.  It never imports UIA, never queries a window and
returns in well under a millisecond, so all three input paths can ask it before
the model.  ``execute()`` owns every side effect: it resolves exactly one target
window/element, refuses ambiguous or dangerous targets, sends at most one action
and reports honestly whatever could be verified afterwards.

Contract: foreground_single_step.  Only the app the user is pointing at is
operated, one action per utterance, never retried.  When Guga's own chat window
holds the foreground (the normal case for typed chat) the most recently active
real app window is restored first, so "点这块" still refers to the app the user
was just using; if that memory is stale or the target is not unique the command
goes back to the model (``need_model=True``) instead of guessing.
"""
import re
import threading
import time

from core.fast_commands import command_text

_OK_TAIL = r'(?:一下|一下子|一回|吧|啊|呀|呗|嘛|谢谢)*'
_QUESTION_WORDS = ('怎么', '咋样', '如何', '为什么', '为啥', '是什么', '什么意思', '哪个',
                   '哪一个', '哪里', '在哪', '能不能', '可不可以', '可以吗', '好吗',
                   '要不要', '是不是', '有没有', '会不会')
_COMPOUND = re.compile(r'(?:然后|接着|随后|并且|再帮我|再给我|再点|再输入|再按|再滚)')
_NEGATION = re.compile(r'^(?:不要|不用|别|不必|千万别|先别)')

_CLICK_HERE = re.compile(
    r'(?:帮我|替我|给我)?(?:点|点击|戳)(?:一下|一下子)?\s*'
    r'(这里|这儿|这块|这边|这个|那个|它|那里|那边|这|那)?\s*'
    r'(?:按钮|选项|链接|地方|区域|控件)?\s*' + _OK_TAIL)
_DOUBLE_HERE = re.compile(
    r'(?:帮我|替我)?(?:双击|双点|点两下)(?:一下)?\s*'
    r'(这里|这儿|这块|这边|这个|那个|它|那里|那边)?\s*'
    r'(?:按钮|选项|链接|地方|区域|控件)?\s*' + _OK_TAIL)
_ORDINAL_CLICK = re.compile(
    r'(?:帮我|替我)?(?:点|点击)(?:一下)?\s*'
    r'(?:第\s*([0-9零一二两三四五六七八九十]+)\s*(?:个|项|号)(?:按钮|选项|项|链接|结果)?'
    r'|中间(?:那个|这个|一个)?'
    r'|最后(?:一个|那个|一项)?)\s*' + _OK_TAIL)
_KEY_PHRASE = re.compile(
    r'(?:按|按下|按一下|敲|敲一下|敲击)(?:一下)?\s*'
    r'(回车键?|enter|return|esc|escape|退出键|取消键|tab键?|制表键?|退格键?|backspace)\s*'
    + _OK_TAIL, re.I)
_KEY_ALIASES = {'回车': 'enter', '回车键': 'enter', 'enter': 'enter', 'return': 'enter',
                'esc': 'esc', 'escape': 'esc', '退出键': 'esc', '取消键': 'esc',
                'tab': 'tab', 'tab键': 'tab', '制表键': 'tab', '制表': 'tab',
                '退格': 'backspace', '退格键': 'backspace', 'backspace': 'backspace'}
_HOTKEYS = {'保存': 'save', '保存一下': 'save', '保存文件': 'save',
            '复制': 'copy', '复制一下': 'copy',
            '粘贴': 'paste', '粘贴一下': 'paste'}
_SCROLL_DOWN = re.compile(
    r'(?:帮我|给我)?(?:往下|向下|下面|下边)(?:滚动|滚|滑|翻页|翻)(?:一下|一下子|一点)?'
    r'(?:(\d+|[一二两三四五六七八九十]+)\s*屏)?\s*' + _OK_TAIL)
_SCROLL_UP = re.compile(
    r'(?:帮我|给我)?(?:往上|向上|上面|上边)(?:滚动|滚|滑|翻页|翻)(?:一下|一下子|一点)?'
    r'(?:(\d+|[一二两三四五六七八九十]+)\s*屏)?\s*' + _OK_TAIL)
_SCROLL_EDGE = re.compile(
    r'(?:帮我)?(?:滚动?|翻页|翻)(?:到)?'
    r'(到底部?|到最下面|到最底下|到顶部?|到最上面|到最顶上)\s*' + _OK_TAIL)
_SCROLL_AMOUNT = re.compile(
    r'(?:帮我|给我)?(?:把|将)?(?:页面|窗口|网页|内容)?(?:滚动?|翻页|翻)\s*'
    r'(\d+|[一二两三四五六七八九十]+)\s*屏\s*' + _OK_TAIL)
_INPUT_DEICTIC = re.compile(
    r'(?:帮我|请|给我)?(?:在这里|在这儿|在这块|在这个输入框|在当前输入框|在这个框|在这'
    r'|在光标处|在光标位置|在光标那里)(?:里|内|中)?(?:输入|填写|键入|打字)[:：\s]*(.+)', re.S)
_INPUT_PLAIN = re.compile(r'(?:输入|填写|键入)[:：\s]*(.+)', re.S)
_PAYLOAD_SOURCE = re.compile(r'(?:输入|填写|键入)[:：]?\s*([\s\S]+)$')
_PAYLOAD_UNSAFE = re.compile(r'(?:然后|接着|随后|并且|再帮我|再给我|再点|再输入|再按|再滚)')
_WINDOW_STATES = (
    ('maximize', ('最大化', '最大化窗口', '窗口最大化', '放到最大')),
    ('minimize', ('最小化', '最小化窗口', '窗口最小化', '收到任务栏', '最小化到任务栏')),
    ('restore', ('还原', '还原窗口', '窗口还原', '恢复窗口', '取消最大化', '恢复原来大小', '恢复正常大小')),
    ('close', ('关闭这个窗口', '关掉这个窗口', '关闭当前窗口', '关掉当前窗口',
               '关闭窗口', '关掉窗口', '把这个窗口关掉')),
)
_NO = dict(matched=False, action='none')


def _number(value):
    digits = dict(zip('零一二三四五六七八九', range(10)), 两=2)
    if value.isascii() and value.isdigit():
        return int(value)
    if value in digits:
        return digits[value]
    if value.count('十') == 1:
        left, right = value.split('十')
        if (not left or left in digits) and (not right or right in digits):
            return (digits[left] if left else 1) * 10 + (digits[right] if right else 0)
    return None


def _payload(source):
    """Recover literal field content; quotes mark data, never another command."""
    found = _PAYLOAD_SOURCE.search(str(source or ''))
    if not found:
        return None, False
    text = found.group(1).strip()
    quoted = len(text) >= 2 and (text[0], text[-1]) in (('“', '”'), ('"', '"'), ("'", "'"))
    if quoted:
        return text[1:-1], True
    return text, False


def _command(source, action, **fields):
    command = dict(matched=True, reflex=True, action=action, kind=action,
                   source_text=str(source or ''), silent=False)
    command.update(fields)
    return command


def _click_here(source, value):
    found = _CLICK_HERE.fullmatch(value)
    if not found:
        return None
    if not (found.group(1) or '一下' in value):
        return None
    return _command(source, 'click_here', target=found.group(1) or 'cursor')


def _double_click_here(source, value):
    found = _DOUBLE_HERE.fullmatch(value)
    if not found:
        return None
    if not (found.group(1) or '一下' in value):
        return None
    return _command(source, 'double_click_here', target=found.group(1) or 'cursor')


def _click_ordinal(source, value):
    found = _ORDINAL_CLICK.fullmatch(value)
    if not found:
        return None
    raw = found.group(1)
    if raw is not None:
        index = _number(raw)
        if index is None or not 1 <= index <= 60:
            return None
        return _command(source, 'click_ordinal', ordinal='index', index=index, target=str(index))
    if '中间' in value:
        return _command(source, 'click_ordinal', ordinal='middle', target='middle')
    return _command(source, 'click_ordinal', ordinal='last', target='last')


def _key(source, value):
    folded = value.casefold().replace(' ', '')
    if folded in ('回车', '回车键', 'enter', 'return'):
        return _command(source, 'key', key='enter', target='enter')
    if folded in ('ctrl+s', 'control+s', '按ctrl+s', '按control+s', '按一下ctrl+s'):
        return _command(source, 'key', key='save', target='save')
    if folded in ('ctrl+c', 'control+c', '按ctrl+c', '按control+c', '按一下ctrl+c'):
        return _command(source, 'key', key='copy', target='copy')
    if folded in ('ctrl+v', 'control+v', '按ctrl+v', '按control+v', '按一下ctrl+v'):
        return _command(source, 'key', key='paste', target='paste')
    hotkey = _HOTKEYS.get(value)
    if hotkey:
        return _command(source, 'key', key=hotkey, target=hotkey)
    found = _KEY_PHRASE.fullmatch(value)
    if not found:
        return None
    name = found.group(1).casefold().replace(' ', '')
    alias = _KEY_ALIASES.get(name)
    if not alias:
        return None
    return _command(source, 'key', key=alias, target=alias)


def _scroll(source, value):
    edge = _SCROLL_EDGE.fullmatch(value)
    if edge:
        word = edge.group(1)
        direction = 'bottom' if ('底' in word or '下' in word) else 'top'
        return _command(source, 'scroll', direction=direction, screens=0, target=direction)
    amount = _SCROLL_AMOUNT.fullmatch(value)
    if amount:
        screens = _number(amount.group(1))
        if screens is None or not 1 <= screens <= 20:
            return None
        return _command(source, 'scroll', direction='down', screens=screens,
                        target='down' + str(screens))
    for pattern, direction in ((_SCROLL_DOWN, 'down'), (_SCROLL_UP, 'up')):
        found = pattern.fullmatch(value)
        if not found:
            continue
        raw = found.group(1)
        screens = 0
        if raw:
            screens = _number(raw)
            if screens is None or not 1 <= screens <= 20:
                return None
        return _command(source, 'scroll', direction=direction, screens=screens,
                        target=direction + str(screens or 1))
    return None


def _input_text(source, value):
    if value.startswith(('输入法', '输入框')):
        return None
    found = _INPUT_DEICTIC.fullmatch(value) or _INPUT_PLAIN.fullmatch(value)
    if not found:
        return None
    text, quoted = _payload(source)
    if text is None:
        text = found.group(1).strip()
        if len(text) >= 2 and (text[0], text[-1]) in (('“', '”'), ('"', '"'), ("'", "'")):
            text, quoted = text[1:-1], True
    if not text or len(text) > 4000:
        return None
    if not quoted:
        text = re.sub(r'(?:吧|啊|呀)$', '', text).strip()
        if not text:
            return None
        if text[0] in '法框':
            return None
        if _PAYLOAD_UNSAFE.search(text):
            return None
    return _command(source, 'input_text', text=text, target=text[:80])


def _window_state(source, value):
    candidate = re.sub(r'^(?:把|将)\s*', '', value)
    for kind, phrases in _WINDOW_STATES:
        if candidate in phrases:
            return _command(source, 'window_state', kind=kind, target=kind)
    return None


_WF_CACHE = dict(at=0.0, ready=False, items=[])
_WF_TTL = 2.0


def _workflow_index():
    """已保存工作流的触发词索引；最多每 2 秒重建一次，保证 match() 不碰磁盘热路径。"""
    now = time.monotonic()
    if _WF_CACHE['ready'] and now - _WF_CACHE['at'] < _WF_TTL:
        return _WF_CACHE['items']
    items = []
    try:
        from core.action_workflow import WorkflowStore
        for raw in WorkflowStore().list() or []:
            if not isinstance(raw, dict):
                continue
            slug = str(raw.get('slug') or raw.get('name') or '')
            name = str(raw.get('name') or slug)
            for phrase in raw.get('trigger_phrases') or []:
                text = command_text(str(phrase))
                if text and slug:
                    items.append((text, slug, name))
    except Exception:
        items = []
    _WF_CACHE.update(at=now, ready=True, items=items)
    return items


def match_workflow(value):
    """这句话是不是在点已保存工作流的名字/触发短语。"""
    if not value:
        return None
    for phrase, slug, name in _workflow_index():
        if phrase and (value == phrase or phrase in value):
            return dict(matched=True, action='workflow', slug=slug, name=name,
                        source_text=value, silent=False, priority='fast')
    return None


def match(text):
    """Parse one referent desktop action.  Pure, side-effect free and model-free."""
    source = str(text or '')
    try:
        value = command_text(source)
    except Exception:
        return dict(_NO)
    if not value or len(value) > 4000:
        return dict(_NO)
    # 已保存的长任务工作流优先级最高：一句话点它的名字就直接零模型回放。
    if not _NEGATION.match(value) and not any(word in value for word in _QUESTION_WORDS):
        saved = match_workflow(value)
        if saved:
            return saved
    if _NEGATION.match(value) or _COMPOUND.search(value):
        return dict(_NO)
    if any(word in value for word in _QUESTION_WORDS):
        return dict(_NO)
    # 网页动作走 CDP（能读 DOM，比 UIA 按坐标点更准），词表刻意收窄、解析不到就交回模型。
    try:
        from core.super_agent import browser_reflex
        browser = browser_reflex.match(value)
    except Exception:
        browser = None
    if browser and browser.get('matched'):
        return browser
    for rule in (_window_state, _double_click_here, _click_ordinal, _click_here,
                 _key, _scroll, _input_text):
        command = rule(source, value)
        if command:
            return command
    return dict(_NO)


class _NeedModel(RuntimeError):
    """The referent cannot be resolved safely; hand the turn back to the model."""


_AMBIGUOUS = '我没看清你指的是哪一块'
_DANGEROUS = ('发送', '删除', '卸载', '支付', '付款', '转账', '确认转账', '清空',
              '格式化', '关机', '重启', '注销', '退出登录', '重置', '确认支付',
              '立即支付', '提交订单', '确认删除', '密码', '验证码',
              'delete', 'uninstall', 'send', 'payment', 'transfer', 'shutdown',
              'restart', 'logout')
_CLICK_TYPES = {'Button', 'Hyperlink', 'MenuItem', 'TabItem', 'ListItem', 'TreeItem',
                'CheckBox', 'RadioButton', 'Edit', 'ComboBox', 'SplitButton'}
_CLICKABLE_PATTERNS = ('InvokePattern', 'SelectionItemPattern')
_EDIT_TYPES = ('Edit', 'ComboBox')
_SHELL_CLASSES = {'Progman', 'WorkerW', 'Shell_TrayWnd', 'Shell_SecondaryTrayWnd',
                  '#32769', 'SysListView32'}
_COMM_PROCESSES = {'wechat.exe', 'weixin.exe', 'qq.exe', 'dingtalk.exe', 'feishu.exe',
                   'lark.exe', 'telegram.exe', 'slack.exe', 'discord.exe', 'teams.exe',
                   'ms-teams.exe'}
_KEY_CODES = {'enter': (13,), 'esc': (27,), 'tab': (9,), 'backspace': (8,),
              'save': (17, 83), 'copy': (17, 67), 'paste': (17, 86)}
_KEY_LABELS = {'enter': '回车', 'esc': 'Esc', 'tab': 'Tab', 'backspace': '退格',
               'save': 'Ctrl+S', 'copy': 'Ctrl+C', 'paste': 'Ctrl+V'}
_WINDOW_MEMORY = dict(hwnd=0, at=0.0)
_WINDOW_MEMORY_TTL = 30.0
_LAST_TARGET = dict(hwnd=0, at=0.0, ref='', name='', type='', rect=None)
_LAST_TARGET_TTL = 30.0
_TRACKER = dict(thread=None, stop=None, users=0)
_TRACKER_LOCK = threading.Lock()


def remember_window(info, now=None):
    """Keep the last real app window so typed chat can still point at it."""
    if not isinstance(info, dict) or not info.get('hwnd'):
        return
    if info.get('own_app') or info.get('shell') or info.get('minimized'):
        return
    _WINDOW_MEMORY.update(hwnd=int(info['hwnd']),
                          at=float(now if now is not None else time.monotonic()))


def _remembered_hwnd(window):
    hwnd = int(_WINDOW_MEMORY.get('hwnd') or 0)
    if not hwnd or time.monotonic() - float(_WINDOW_MEMORY.get('at') or 0.0) > _WINDOW_MEMORY_TTL:
        return 0
    info = window.describe(hwnd)
    if not info or info.get('own_app') or info.get('shell') or info.get('minimized'):
        return 0
    return hwnd


def remember_target(info, item):
    if not isinstance(info, dict) or not isinstance(item, dict) or not item.get('ref'):
        return
    _LAST_TARGET.update(hwnd=int(info.get('hwnd') or 0), at=time.monotonic(),
                        ref=item['ref'], name=str(item.get('name') or ''),
                        type=str(item.get('type') or ''), rect=item.get('rect'))


def start_foreground_tracker(window=None):
    """Start one best-effort sampler so a focus move to Guga never loses the app."""
    with _TRACKER_LOCK:
        _TRACKER['users'] += 1
        if _TRACKER['thread'] and _TRACKER['thread'].is_alive():
            return False
        stop = threading.Event()
        _TRACKER['stop'] = stop

        def run():
            driver = window or _WindowsWindow()
            while not stop.wait(0.5):
                try:
                    info = driver.foreground()
                    if info and not info.get('own_app') and not info.get('shell'):
                        remember_window(info)
                except Exception:
                    pass

        thread = threading.Thread(target=run, name='guga-foreground-memory', daemon=True)
        _TRACKER['thread'] = thread
        thread.start()
        return True


def stop_foreground_tracker():
    with _TRACKER_LOCK:
        _TRACKER['users'] = max(0, _TRACKER['users'] - 1)
        if _TRACKER['users'] or not _TRACKER['stop']:
            return
        _TRACKER['stop'].set()
        _TRACKER['thread'] = None
        _TRACKER['stop'] = None


class _WindowsWindow:
    """Read-only window facts; activation is the only state change it makes."""

    def foreground(self):
        import win32gui
        hwnd = int(win32gui.GetForegroundWindow() or 0)
        return self.describe(hwnd) if hwnd else {}

    def describe(self, hwnd):
        import os
        import win32gui
        import win32process
        try:
            hwnd = int(hwnd or 0)
            if not hwnd or not win32gui.IsWindow(hwnd):
                return {}
            pid = int(win32process.GetWindowThreadProcessId(hwnd)[1])
            class_name = str(win32gui.GetClassName(hwnd) or '')
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            return dict(hwnd=hwnd, pid=pid, title=str(win32gui.GetWindowText(hwnd) or '')[:200],
                        process=_process_name(pid), class_name=class_name,
                        rect=[int(left), int(top), int(right), int(bottom)],
                        own_app=pid == os.getpid(),
                        minimized=bool(win32gui.IsIconic(hwnd)),
                        shell=class_name in _SHELL_CLASSES or hwnd == int(win32gui.GetDesktopWindow()))
        except Exception:
            return {}

    def cursor(self):
        import ctypes
        import ctypes.wintypes
        point = ctypes.wintypes.POINT()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            return None
        return int(point.x), int(point.y)

    def root_at(self, x, y):
        import win32con
        import win32gui
        try:
            return int(win32gui.GetAncestor(win32gui.WindowFromPoint((int(x), int(y))),
                                            win32con.GA_ROOT) or 0)
        except Exception:
            return 0

    def activate(self, hwnd):
        try:
            from core.computer_control import _attach_focus
            return bool(_attach_focus(int(hwnd)))
        except Exception:
            return False

    def is_window(self, hwnd):
        import win32gui
        try:
            return bool(win32gui.IsWindow(int(hwnd)))
        except Exception:
            return False


class _WindowsInput:
    """One bounded input action; never retried here."""

    def click(self, x, y, expect_hwnd, count=1, guard=None):
        import ctypes
        import win32con
        import win32gui
        x, y = int(x), int(y)
        root = int(win32gui.GetAncestor(win32gui.WindowFromPoint((x, y)),
                                        win32con.GA_ROOT) or 0)
        if root != int(expect_hwnd):
            raise RuntimeError('目标位置被其他窗口挡住了，我没有点')
        if callable(guard):
            guard()
        user = ctypes.windll.user32
        if not user.SetCursorPos(x, y):
            raise RuntimeError('没能把鼠标移到目标位置，没有点')
        if callable(guard):
            guard()
        for index in range(max(1, min(int(count), 2))):
            user.mouse_event(2, 0, 0, 0, 0)
            user.mouse_event(4, 0, 0, 0, 0)
            if index == 0 and int(count) > 1:
                time.sleep(0.05)

    def scroll(self, x, y, delta, expect_hwnd, guard=None):
        import ctypes
        import win32con
        import win32gui
        x, y = int(x), int(y)
        root = int(win32gui.GetAncestor(win32gui.WindowFromPoint((x, y)),
                                        win32con.GA_ROOT) or 0)
        if root != int(expect_hwnd):
            raise RuntimeError('目标窗口被其他窗口挡住了，我没有滚')
        if callable(guard):
            guard()
        user = ctypes.windll.user32
        if not user.SetCursorPos(x, y):
            raise RuntimeError('没能把鼠标移到目标窗口，没有滚')
        if callable(guard):
            guard()
        user.mouse_event(0x0800, 0, 0, int(delta), 0)

    def keys(self, values):
        from core.computer_control import _send_keys
        _send_keys(list(values))


def _process_name(pid):
    try:
        import psutil
        return str(psutil.Process(pid).name()).casefold()
    except Exception:
        return ''


def _default_backend():
    from core.uia_service import shared_service
    return shared_service()


def _result(**fields):
    base = dict(ok=False, action_sent=False, verified=False, need_model=False)
    base.update(fields)
    return base


def _fingerprint(info):
    return (int(info.get('hwnd') or 0), int(info.get('pid') or 0),
            str(info.get('title') or ''), tuple(info.get('rect') or ()))


def _recheck(info, window):
    current = window.describe(info.get('hwnd'))
    if (not current or current.get('own_app') or current.get('shell') or current.get('minimized')
            or _fingerprint(current) != _fingerprint(info)):
        raise _NeedModel('目标窗口刚刚变了，这次没有继续操作；请再说一遍')


def _resolve_window(hwnd, window):
    foreground = window.foreground() or {}
    candidates = []
    if hwnd:
        candidates.append(('hint', int(hwnd)))
    external_foreground = bool(foreground) and not foreground.get('own_app') and not foreground.get('shell')
    if external_foreground:
        candidates.append(('foreground', int(foreground['hwnd'])))
    focused_on_own = bool(foreground) and (foreground.get('own_app') or foreground.get('shell'))
    if not foreground or focused_on_own:
        cursor_window = _cursor_window(window)
        if cursor_window:
            candidates.append(('cursor_window', cursor_window))
        remembered = _remembered_hwnd(window)
        if remembered:
            candidates.append(('memory', remembered))
    if _LAST_TARGET.get('hwnd'):
        candidates.append(('last_target', int(_LAST_TARGET['hwnd'])))
    seen = set()
    for source, candidate in candidates:
        candidate = int(candidate or 0)
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        info = window.describe(candidate)
        if not info or info.get('own_app') or info.get('shell') or info.get('minimized'):
            continue
        rect = info.get('rect') or []
        if len(rect) != 4 or int(rect[2]) <= int(rect[0]) or int(rect[3]) <= int(rect[1]):
            continue
        remember_window(info)
        return info, source
    raise _NeedModel('我没看清你指的是哪个窗口；请把鼠标放到目标上，或先切到要操作的程序')


def _cursor_window(window):
    try:
        point = window.cursor()
        if not point:
            return 0
        info = window.describe(window.root_at(point[0], point[1]))
        if not info or info.get('own_app') or info.get('shell') or info.get('minimized'):
            return 0
        return int(info['hwnd'])
    except Exception:
        return 0


def _rect_values(rect):
    try:
        values = [float(rect[key]) for key in ('left', 'top', 'right', 'bottom')]
    except (KeyError, TypeError, ValueError):
        return None
    if values[2] <= values[0] or values[3] <= values[1]:
        return None
    return values


def _inside_window(rect, window_rect, tolerance=4):
    values = _rect_values(rect)
    if not values or not window_rect:
        return False
    left, top, right, bottom = values
    return (left >= window_rect[0] - tolerance and top >= window_rect[1] - tolerance
            and right <= window_rect[2] + tolerance and bottom <= window_rect[3] + tolerance)


def _contains_point(rect, x, y):
    values = _rect_values(rect)
    return bool(values and values[0] <= x < values[2] and values[1] <= y < values[3])


def _area(rect):
    values = _rect_values(rect)
    return (values[2] - values[0]) * (values[3] - values[1]) if values else 1e18


def _entry_usable(item):
    return (isinstance(item, dict) and bool(item.get('ref'))
            and item.get('enabled') is True and item.get('offscreen') is not True
            and item.get('password') is not True)


def _interactive(item):
    """Only controls whose control type means "user can operate it".

    Qt and Chromium report InvokePattern on plain Text/Group/Window objects, so a
    pattern alone must never make a paragraph clickable; the type is the gate.
    """
    return _entry_usable(item) and item.get('type') in _CLICK_TYPES


def _element_at_cursor(backend, window, info):
    """Return (entry, how) or (None, reason).  Never guesses outside the cursor."""
    point = window.cursor()
    if not point:
        return None, 'no_cursor'
    x, y = int(point[0]), int(point[1])
    rect = info.get('rect') or [0, 0, 0, 0]
    if not (int(rect[0]) <= x < int(rect[2]) and int(rect[1]) <= y < int(rect[3])):
        return None, 'outside'
    if int(window.root_at(x, y) or 0) != int(info['hwnd']):
        return None, 'covered'
    getter = getattr(backend, 'element_from_point', None)
    if callable(getter):
        try:
            value = getter(x, y)
        except Exception:
            value = None
        if isinstance(value, dict):
            if int(value.get('hwnd') or 0) not in (0, int(info['hwnd'])):
                return None, 'other_window'
            chain = [value.get('element')] + list(value.get('chain') or [])
            for item in chain:
                if _interactive(item) and _inside_window(item.get('rect'), rect):
                    return item, 'cursor'
            return None, 'non_actionable'
    try:
        snapshot = backend.snapshot(int(info['hwnd']), limit=240, time_budget=1.0)
    except Exception:
        return None, 'unavailable'
    best = None
    for item in (snapshot or {}).get('elements') or []:
        if not _interactive(item) or not _contains_point(item.get('rect'), x, y):
            continue
        if best is None or _area(item.get('rect')) < _area(best.get('rect')):
            best = item
    if best:
        return best, 'cursor_snapshot'
    return None, 'non_actionable'


def _focused_element(backend, window, info):
    foreground = window.foreground() or {}
    if int(foreground.get('hwnd') or 0) != int(info['hwnd']):
        return None
    try:
        value = backend.focused(int(info['hwnd']))
    except Exception:
        return None
    item = (value or {}).get('element') if isinstance(value, dict) else None
    return item if _entry_usable(item) else None


def _last_target(backend, info):
    if int(_LAST_TARGET.get('hwnd') or 0) != int(info['hwnd']):
        return None
    if time.monotonic() - float(_LAST_TARGET.get('at') or 0.0) > _LAST_TARGET_TTL:
        return None
    ref = _LAST_TARGET.get('ref')
    if not ref:
        return None
    try:
        value = backend.read(int(info['hwnd']), ref=ref)
    except Exception:
        return None
    item = (value or {}).get('element') if isinstance(value, dict) else None
    return item if _entry_usable(item) else None


def _resolve_click_target(backend, window, info):
    item, reason = _element_at_cursor(backend, window, info)
    if item:
        return item, 'cursor'
    if reason in ('non_actionable', 'other_window'):
        raise _NeedModel('鼠标下面不是可点的控件，我没敢乱点；请指着按钮再说一次')
    focused = _focused_element(backend, window, info)
    if focused:
        return focused, 'focused'
    last = _last_target(backend, info)
    if last:
        return last, 'last_target'
    raise _NeedModel('我没看清你指的是哪一块；把鼠标移到目标上再说一次，或告诉我它的名字')


def _editable_items(backend, info):
    try:
        snapshot = backend.snapshot(int(info['hwnd']), limit=240)
    except Exception:
        return []
    items = []
    for item in (snapshot or {}).get('elements') or []:
        if not _entry_usable(item) or item.get('type') not in _EDIT_TYPES:
            continue
        items.append(item)
    return items


def _resolve_editable(backend, window, info):
    item, _reason = _element_at_cursor(backend, window, info)
    if item and item.get('type') in _EDIT_TYPES:
        return item, 'cursor'
    focused = _focused_element(backend, window, info)
    if focused and focused.get('type') in _EDIT_TYPES:
        return focused, 'focused'
    last = _last_target(backend, info)
    if last and last.get('type') in _EDIT_TYPES:
        return last, 'last_target'
    edits = _editable_items(backend, info)
    if len(edits) == 1:
        return edits[0], 'unique_edit'
    if len(edits) > 1:
        raise _NeedModel('这个窗口里有多个输入框，我不知道填哪一个；请先点一下它，或说出它的名称')
    raise _NeedModel('我没有找到可以输入的输入框；请先把光标点进要填的地方')


def _interactive_items(backend, info):
    try:
        snapshot = backend.snapshot(int(info['hwnd']), limit=240)
    except Exception:
        raise _NeedModel('这个窗口我现在读不到控件，这次没有操作') from None
    items = []
    seen = set()
    for item in (snapshot or {}).get('elements') or []:
        if not _entry_usable(item) or item['ref'] in seen or not _interactive(item):
            continue
        seen.add(item['ref'])
        items.append(item)
    return items


def _cached_ordinal(desktop, index, window):
    """Reuse the numbered list the user already saw ("列出可点选项").

    The cache belongs to the existing desktop-step executor, so the numbers stay
    identical between "列出可点选项" and "点第2个" even when the focus moved to
    Guga's own chat window in between.
    """
    cached = getattr(desktop, '_options', None) if desktop is not None else None
    if not isinstance(cached, dict):
        return None
    ttl = float(getattr(desktop, 'OPTION_TTL', 30.0))
    if not 0 <= time.monotonic() - float(cached.get('at') or 0.0) < ttl:
        return None
    items = cached.get('items') or []
    if not 1 <= index <= len(items):
        return None
    item = items[index - 1]
    if (not isinstance(item, dict) or item.get('source') == 'visual'
            or not item.get('ref') or item.get('type') not in _CLICK_TYPES):
        return None
    hwnd = int((cached.get('identity') or {}).get('hwnd') or 0)
    info = window.describe(hwnd) if hwnd and hasattr(window, 'describe') else {}
    if not info or info.get('own_app') or info.get('shell') or info.get('minimized'):
        return None
    return info, item


def _resolve_ordinal(backend, info, command, window=None, desktop=None):
    if command.get('ordinal') == 'index' and desktop is not None and window is not None:
        cached = _cached_ordinal(desktop, int(command.get('index') or 0), window)
        if cached:
            cached_info, cached_item = cached
            try:
                fresh = (backend.read(int(cached_info['hwnd']), ref=cached_item['ref']) or {}).get('element')
            except Exception:
                fresh = None
            if isinstance(fresh, dict) and _interactive(fresh):
                return fresh, 'cached_list_%d' % int(command.get('index') or 0), cached_info
    items = _interactive_items(backend, info)
    if not items:
        raise _NeedModel('这个窗口里我没有找到可点的控件')
    kind = command.get('ordinal')
    if kind == 'middle':
        index = (len(items) + 1) // 2
    elif kind == 'last':
        index = len(items)
    else:
        index = int(command.get('index') or 0)
    if not 1 <= index <= len(items):
        raise _NeedModel('这个窗口没有第 %d 个可点选项；我只看到 %d 个' % (index, len(items)))
    return items[index - 1], 'ordinal_%d' % index, info


def _guard_entry(item):
    if not isinstance(item, dict) or not item.get('ref'):
        raise _NeedModel(_AMBIGUOUS)
    if item.get('enabled') is not True:
        raise _NeedModel('这个控件现在不可用，我没有操作')
    if item.get('offscreen') is not False:
        raise _NeedModel('这个控件不在当前画面里，我没有操作')
    if item.get('password') is True:
        raise _NeedModel('这是密码控件，我不碰')
    label = (str(item.get('name') or '') + ' ' + str(item.get('id') or '')).casefold()
    for word in _DANGEROUS:
        if word.casefold() in label:
            raise _NeedModel('这个控件是「' + word + '」一类，我不会替你操作；请你自己确认，或让我交给大脑判断')


def _label(item):
    name = str(item.get('name') or '').strip()
    return name[:24] if name else ({'Edit': '输入框', 'ComboBox': '下拉框'}.get(item.get('type'), '这个控件'))


def _ensure_foreground(info, window):
    current = window.foreground() or {}
    if int(current.get('hwnd') or 0) == int(info['hwnd']):
        return
    if not window.activate(int(info['hwnd'])):
        raise _NeedModel('目标窗口没能切到前台，这次没有操作')
    current = window.foreground() or {}
    if int(current.get('hwnd') or 0) != int(info['hwnd']):
        raise _NeedModel('目标窗口没能切到前台，这次没有操作')
    _recheck(info, window)


def _target_fields(item):
    return dict(ref=item.get('ref'), name=str(item.get('name') or '')[:80],
                type=str(item.get('type') or ''))


def _action_click(command, backend, window, input_driver, hwnd, desktop=None):
    info, source = _resolve_window(hwnd, window)
    _recheck(info, window)
    double = command.get('action') == 'double_click_here'
    if command.get('action') == 'click_ordinal':
        item, how, resolved_info = _resolve_ordinal(backend, info, command, window, desktop)
        if resolved_info is not info:
            # A fresh "列出可点选项" list names the window it was read from.
            info = resolved_info
            _recheck(info, window)
    else:
        item, how = _resolve_click_target(backend, window, info)
    _guard_entry(item)
    if int((window.foreground() or {}).get('hwnd') or 0) != int(info['hwnd']):
        _ensure_foreground(info, window)
    _recheck(info, window)
    kind = item.get('type')
    if not double and kind in _EDIT_TYPES:
        receipt = backend.act(int(info['hwnd']), 'focus', ref=item['ref'])
        if not isinstance(receipt, dict) or receipt.get('verified') is not True:
            return _result(ok=False, action_sent=receipt.get('input_sent') if isinstance(receipt, dict) else False,
                           verified=False, evidence='focus_unconfirmed', target=_target_fields(item),
                           message='我试着选中「' + _label(item) + '」，但没有得到确认；请先核对')
        remember_target(info, item)
        return _result(ok=True, action_sent=True, verified=True, evidence='keyboard_focus',
                       target=_target_fields(item), source=how,
                       message='已选中「' + _label(item) + '」输入框')
    if not double and any(pattern in _CLICKABLE_PATTERNS for pattern in (item.get('patterns') or [])):
        receipt = backend.act(int(info['hwnd']), 'invoke', ref=item['ref'])
        if not isinstance(receipt, dict):
            raise _NeedModel('界面没有给出明确的点击回执，这次没有继续')
        if receipt.get('fallback'):
            return _mouse_click(backend, window, input_driver, info, item, how, False)
        verified, evidence = _verify_invoke(backend, window, info, item, receipt)
        remember_target(info, item)
        message = '已点击「' + _label(item) + '」'
        if not verified:
            message += '；没有回读到结果，请看一眼画面'
        return _result(ok=True, action_sent=True, verified=verified, evidence=evidence,
                       target=_target_fields(item), source=how, message=message)
    return _mouse_click(backend, window, input_driver, info, item, how, double)


def _verify_invoke(backend, window, info, item, receipt):
    if receipt.get('verified') is True:
        return True, 'uia_confirmed'
    hwnd = int(info['hwnd'])
    time.sleep(0.12)
    try:
        again = backend.read(hwnd, ref=item['ref'])
    except Exception:
        if int((window.foreground() or {}).get('hwnd') or 0) == hwnd:
            return True, 'target_gone'
        return False, 'foreground_changed'
    current = (again or {}).get('element') if isinstance(again, dict) else None
    if int((window.foreground() or {}).get('hwnd') or 0) != hwnd:
        return False, 'foreground_changed'
    if not isinstance(current, dict) or current.get('ref') != item.get('ref'):
        return True, 'target_changed'
    return False, 'target_unchanged'


def _send_input(call, *args, **kwargs):
    """Bound one OS input call; a refused/stale desktop becomes a model hand-off."""
    try:
        return call(*args, **kwargs)
    except _NeedModel:
        raise
    except RuntimeError as exc:
        raise _NeedModel(str(exc) or '没能把操作发出去，这次没有继续') from None


def _mouse_click(backend, window, input_driver, info, item, how, double):
    rect = _rect_values(item.get('rect'))
    if not rect or not _inside_window(item.get('rect'), info.get('rect')):
        raise _NeedModel('这个控件的屏幕位置无效，我没有点')
    x = int((rect[0] + rect[2]) // 2)
    y = int((rect[1] + rect[3]) // 2)
    before = int((window.foreground() or {}).get('hwnd') or 0)

    def guard():
        _recheck(info, window)

    _send_input(input_driver.click, x, y, int(info['hwnd']), count=2 if double else 1, guard=guard)
    verified = False
    deadline = time.monotonic() + 0.4
    while time.monotonic() < deadline:
        if int((window.foreground() or {}).get('hwnd') or 0) == int(info['hwnd']):
            verified = True
            break
        time.sleep(0.03)
    evidence = 'foreground_moved_to_target' if verified and before != int(info['hwnd']) else 'click_sent'
    if not verified:
        try:
            backend.read(int(info['hwnd']), ref=item['ref'])
        except Exception:
            verified = True
            evidence = 'target_gone'
    remember_target(info, item)
    message = ('已双击「' if double else '已点击「') + _label(item) + '」'
    if not verified:
        message += '；没有回读结果，请看一眼画面'
    return _result(ok=True, action_sent=True, verified=verified, evidence=evidence,
                   target=_target_fields(item), source=how, message=message)


def _action_input(command, backend, window, input_driver, hwnd):
    text = str(command.get('text') or '')
    if not text or len(text) > 4000:
        raise _NeedModel('我没有听清要输入的内容')
    info, source = _resolve_window(hwnd, window)
    _recheck(info, window)
    item, how = _resolve_editable(backend, window, info)
    _guard_entry(item)
    if item.get('type') not in _EDIT_TYPES:
        raise _NeedModel('目标不是普通输入框，我没有填')
    _ensure_foreground(info, window)
    _recheck(info, window)
    fresh = (backend.read(int(info['hwnd']), ref=item['ref']) or {}).get('element') or item
    _guard_entry(fresh)
    if fresh.get('type') not in _EDIT_TYPES:
        raise _NeedModel('目标不是普通输入框，我没有填')
    receipt = backend.act(int(info['hwnd']), 'set_value', ref=fresh['ref'], value=text[:4000])
    if not isinstance(receipt, dict) or receipt.get('fallback'):
        raise _NeedModel('这个输入框不支持安全填写，我没有继续')
    if receipt.get('verified') is not True:
        return _result(ok=False, action_sent=receipt.get('input_sent'),
                       verified=False, evidence='value_mismatch', target=_target_fields(fresh),
                       message='已经尝试填写，但没有回读到同样的内容；请先核对')
    remember_target(info, fresh)
    shown = text[:24] + ('…' if len(text) > 24 else '')
    return _result(ok=True, action_sent=True, verified=True, evidence='value_equals',
                   target=_target_fields(fresh), source=how,
                   message='已输入「' + shown + '」并回读确认')


def _action_key(command, backend, window, input_driver, hwnd):
    key = str(command.get('key') or '')
    codes = _KEY_CODES.get(key)
    if not codes:
        raise _NeedModel('这个按键我还不认识')
    info, source = _resolve_window(hwnd, window)
    _recheck(info, window)
    if key == 'enter' and str(info.get('process') or '') in _COMM_PROCESSES:
        raise _NeedModel('「' + str(info.get('title') or '这个窗口')[:24] + '」像是聊天软件，按回车可能会把消息发出去；请你自己发，或让我交给大脑判断')
    _ensure_foreground(info, window)
    _recheck(info, window)
    _send_input(input_driver.keys, codes)
    title = str(info.get('title') or '')[:24] or '当前窗口'
    return _result(ok=True, action_sent=True, verified=False, evidence='keys_sent',
                   source=source, target=dict(title=str(info.get('title') or '')[:80]),
                   message='已在「' + title + '」按' + _KEY_LABELS.get(key, key))


def _action_scroll(command, backend, window, input_driver, hwnd):
    info, source = _resolve_window(hwnd, window)
    _recheck(info, window)
    _ensure_foreground(info, window)
    _recheck(info, window)
    direction = str(command.get('direction') or 'down')
    screens = int(command.get('screens') or 0)
    rect = info.get('rect') or [0, 0, 0, 0]
    x = int((int(rect[0]) + int(rect[2])) // 2)
    y = int((int(rect[1]) + int(rect[3])) // 2)

    def guard():
        _recheck(info, window)

    if direction in ('bottom', 'top'):
        _send_input(input_driver.keys, (17, 35) if direction == 'bottom' else (17, 36))
        evidence = 'keys_sent'
        message = '已滚到' + ('底部' if direction == 'bottom' else '顶部')
    else:
        amount = -(1200 if screens <= 0 else 1200 * screens) if direction == 'down' else (1200 if screens <= 0 else 1200 * screens)
        _send_input(input_driver.scroll, x, y, amount, int(info['hwnd']), guard=guard)
        evidence = 'wheel_sent'
        message = '已向' + ('下' if direction == 'down' else '上') + '滚动' + (('%d屏' % screens) if screens else '')
    return _result(ok=True, action_sent=True, verified=False, evidence=evidence, source=source,
                   message=message + '；没有回读滚动位置')


def _action_window_state(command, window):
    kind = str(command.get('kind') or '')
    if kind not in ('maximize', 'minimize', 'restore', 'close'):
        raise _NeedModel('这个窗口动作我还不认识')
    from core.fast_commands import execute as execute_fast
    value = execute_fast(dict(matched=True, action='window_control', kind=kind, target=''))
    if not isinstance(value, dict):
        raise _NeedModel('窗口动作没有返回结果')
    return _result(ok=bool(value.get('ok')), action_sent=value.get('action_sent'),
                   verified=bool(value.get('verified')),
                   evidence=value.get('evidence') or value.get('code'),
                   error=value.get('error'),
                   message=str(value.get('message') or value.get('error') or '窗口动作已完成'))


def _run_workflow(command, started):
    """回放一个已保存的长任务工作流；全程零模型调用。"""
    slug = str(command.get('slug') or '')
    label = str(command.get('name') or slug or '这个流程')
    if not slug:
        raise _NeedModel('没有指定要回放的工作流')
    from core.action_workflow import play_workflow
    report = play_workflow(slug) or {}
    steps = report.get('steps') or []
    ok = bool(report.get('ok') if 'ok' in report else report.get('success'))
    first_bad = next((step for step in steps if isinstance(step, dict) and step.get('ok') is False), None)
    if ok:
        message = '照你上次教的「%s」跑完了，%d 步都核验过了' % (label, len(steps))
    else:
        where = (first_bad or {}).get('index') or (first_bad or {}).get('step') or '?'
        why = (first_bad or {}).get('error') or report.get('error') or '原因不明'
        message = '「%s」在第 %s 步没做成：%s。我先停在这儿，没有继续往下点' % (label, where, str(why)[:120])
    return dict(ok=ok, action_sent=True, verified=ok, workflow=True, message=message,
                workflow_slug=slug, workflow_steps=len(steps),
                workflow_total_ms=report.get('total_ms'),
                elapsed_ms=round((time.perf_counter() - started) * 1000, 2))


def execute(command, *, backend=None, hwnd=None, window=None, input_driver=None, desktop=None):
    """Perform one already-parsed reflex action, then report what was verified.

    ``desktop`` is the existing DesktopInteraction instance when the caller has
    one, only so a numbered click reuses the exact list the user just saw.
    """
    started = time.perf_counter()
    command = dict(command or {})
    try:
        if not command.get('matched'):
            raise _NeedModel('这条不是反射动作')
        action = command.get('action')
        if action == 'workflow':
            return _run_workflow(command, started)
        if action.startswith('browser_'):
            from core.super_agent import browser_reflex
            result = browser_reflex.execute(command)
            result.setdefault('action', action)
            return result
        if action == 'window_state':
            result = _action_window_state(command, window)
        else:
            backend = backend or _default_backend()
            window = window or _WindowsWindow()
            input_driver = input_driver or _WindowsInput()
            if action in ('click_here', 'click_ordinal', 'double_click_here'):
                result = _action_click(command, backend, window, input_driver, hwnd, desktop)
            elif action == 'input_text':
                result = _action_input(command, backend, window, input_driver, hwnd)
            elif action == 'key':
                result = _action_key(command, backend, window, input_driver, hwnd)
            elif action == 'scroll':
                result = _action_scroll(command, backend, window, input_driver, hwnd)
            else:
                raise _NeedModel('这个反射动作还没有实现')
    except _NeedModel as exc:
        result = dict(ok=False, action_sent=False, verified=False, need_model=True,
                      message=str(exc) or _AMBIGUOUS)
    except Exception as exc:
        # A reflex must never raise into the UI thread; an unreadable desktop is
        # reported as "let the model handle it", never as a fake success.
        result = dict(ok=False, action_sent=False, verified=False, need_model=True,
                      error='反射动作没有完成：' + type(exc).__name__,
                      message='界面这边我没有看清或没有做成，这次没有继续操作')
    result = dict(result, action=command.get('action'), source_text=command.get('source_text', ''),
                  dispatch_ms=round((time.perf_counter() - started) * 1000, 2))
    return result
