"""One desktop step; no task planner, replay, privilege change or activation.

Both legacy browser_step and desktop_step commands are accepted. UIAService owns
bounded provider calls and value readback. ``input_target(hwnd, search=False)`` is
the desktop analogue of page_input, supplied by the backend integration.

Optional target_resolver(hwnd, target) returns source='visual', an absolute rect
{left, top, right, bottom}, window_fingerprint={hwnd, pid, process, rect:[l,t,r,b],
title}, observed_at (monotonic), frame_digest and name. For target='', it returns
elements instead of a single rect. Every visual click also requires
desktop_targeting.validate_target(hwnd, result), or an injected target_validator.
These hooks must be bounded and must never send input themselves.
"""
import copy
import math
import os
import re
import threading
import time

from core.browser_interaction import parse_step


_BROWSERS = {'msedge.exe', 'chrome.exe', 'firefox.exe', 'doubao.exe', 'brave.exe',
             'chromium.exe', 'opera.exe', 'vivaldi.exe'}
_BROWSER_ONLY = {'back', 'forward', 'refresh', 'next_tab', 'previous_tab',
                 'new_tab', 'address_bar'}
_KEYS = {'enter': [13], 'escape': [27], 'esc': [27], 'tab': [9],
         'find': [17, 70], 'select_all': [17, 65]}
_LOCAL_ACTIONS = {'fill', 'append', 'clear', 'fill_named', 'click', 'focus', 'click_index',
                  'scroll_down', 'scroll_up', 'list_targets', 'list_options'}
_SUPPORTED_ACTIONS = _BROWSER_ONLY | set(_KEYS) | _LOCAL_ACTIONS
_GENERIC_INPUTS = {'输入框', '当前输入框', '这个输入框', '搜索框', '搜索输入框',
                   '搜索', '搜索输入', '当前', '这个', '输入'}
_CLICK_TYPES = {'Button', 'Hyperlink', 'MenuItem', 'TabItem', 'ListItem',
                'TreeItem', 'CheckBox', 'RadioButton', 'Edit', 'ComboBox'}
_SHELL_CLASSES = {'Progman', 'WorkerW', 'Shell_TrayWnd', 'Shell_SecondaryTrayWnd',
                  '#32769', 'SysListView32'}
_FP_KEYS = ('hwnd', 'pid', 'process', 'rect', 'title')
_COMPOUND = re.compile(r'(?:然后|接着|随后|并且|再帮我)(?:再|请|帮我)?\s*'
                       r'(?:点击|点|输入|填写|按|滚动|拖动|双击|打开|发送)')


def _unquote(text):
    if len(text) >= 2 and (text[0], text[-1]) in (('“', '”'), ('"', '"'), ("'", "'")):
        return text[1:-1], True
    return text, False


def _payload(action, raw):
    """Recover literal content from the unnormalised utterance, including spaces."""
    pattern = (r'(?:在|往|向).+?(?:输入框|框)(?:里|内|中)?(?:输入|填写)'
               if action == 'fill_named' else r'(?:追加输入|输入|键入|填写)')
    found = re.search(pattern + r'[:：]?([\s\S]+)$', raw)
    return _unquote(found.group(1).strip())[0] if found else None


def _input_method_question(raw):
    raw = re.sub(r'^(?:(?:你帮我|请你?|帮我|麻烦你?)[，,\s]*)+', '', raw.strip())
    return bool(re.match(r'(?:(?:在|往|向).+?(?:输入框|框)(?:里|内|中)?)?'
                         r'输入法(?:怎么|如何|怎样|能否|为什么|是什么)', raw))


def _unsafe_input(raw):
    # Quoted content is data even when it happens to describe another action.
    found = re.search(r'(?:追加输入|输入|键入|填写)[:：]?([\s\S]+)$', raw)
    if not found:
        return False
    content = found.group(1)
    _, quoted = _unquote(content.strip())
    return not quoted and bool(_COMPOUND.search(content))


def _number(value):
    if value.isascii() and value.isdigit():
        return int(value)
    digits = dict(zip('零一二三四五六七八九', range(10)), 两=2)
    if value in digits:
        return digits[value]
    if value.count('十') == 1:
        left, right = value.split('十')
        if (not left or left in digits) and (not right or right in digits):
            return (digits[left] if left else 1) * 10 + (digits[right] if right else 0)
    return None


def parse_desktop_step(value, source_text=''):
    """Reuse browser parsing, adding desktop targets, lists, numbers and keys."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = source_text or value
    common = dict(matched=True, action='desktop_step', kind='desktop', silent=True,
                  priority='fast', source_text=raw)

    def command(action, **fields):
        return dict(common, step_action=action, browser_action=action, target='', **fields)

    def unsupported(reason):
        return command('unsupported', error=reason)

    text = value.strip()
    if text in ('停止操作','停止这一步','取消这一步','停止当前操作','取消当前操作'):
        return command('cancel_step')
    if _input_method_question(text) or _input_method_question(raw):
        return None
    if text in ('列出可点选项', '看看能点什么', '列出可点击选项', '看看可以点什么'):
        return command('list_targets')
    number = re.fullmatch(r'(?:点击|点一下|点)\s*(?:第)?([0-9零一二两三四五六七八九十]+)\s*(?:项|号|个(?:按钮|选项)?)', text)
    if number and _number(number.group(1)) is not None:
        return command('click_index', index=_number(number.group(1)))
    key = re.sub(r'^(?:按一下|按下|按|使用)', '', text).replace(' ', '').lower()
    aliases = {'esc': 'escape', 'escape': 'escape', 'esc键': 'escape',
               '取消键': 'escape', '退出键': 'escape',
               'tab': 'tab', 'tab键': 'tab', '制表': 'tab', '制表键': 'tab',
               '切换焦点': 'tab', '切到下一个输入框': 'tab',
               'ctrl+f': 'find', 'control+f': 'find', '查找': 'find', '查找内容': 'find',
               '打开查找': 'find', '查找快捷键': 'find',
               'ctrl+a': 'select_all', 'control+a': 'select_all', '全选': 'select_all',
               '全选内容': 'select_all', '回车键': 'enter', 'enter': 'enter'}
    if key in aliases:
        return command(aliases[key])
    if re.match(r'^(?:双击|双点|拖动|拖拽|右击|右键点击|长按)', text):
        return unsupported('通用本地单步暂不支持双击、拖动、右击或长按；本次未发送操作')
    if _unsafe_input(raw):
        return unsupported('输入法问句或未加引号的复合输入不是单步填写，请逐步操作或给文字加引号')

    # Strip scope qualifiers only after a click/focus verb, never inside input.
    scoped = re.sub(r'^((?:点击|点一下|点选|点开|按一下|点)(?:一下)?)'
                    r'(?:窗口|软件|程序|应用|屏幕)(?:里|内|中|上)(?:的)?', r'\1', text)
    focus = re.fullmatch(r'(?:聚焦|焦点移到|将焦点移到)(.+)', scoped)
    parsed = parse_step(scoped, raw)
    if focus and not parsed:
        parsed = parse_step('点击' + focus.group(1), raw)
        if parsed:
            parsed['browser_action'] = 'focus'
    if not parsed:
        return None
    parsed = dict(parsed, **common)
    action = parsed['browser_action']
    parsed['step_action'] = action
    if action in ('fill', 'append', 'fill_named'):
        literal = _payload(action, raw)
        if literal is not None:
            parsed['text' if action == 'fill_named' else 'target'] = literal
    return parsed


def window_fingerprint(hwnd):
    """Public fingerprint contract shared with desktop_targeting (no input)."""
    import psutil
    import win32gui
    import win32process
    hwnd = int(hwnd)
    pid = win32process.GetWindowThreadProcessId(hwnd)[1]
    return dict(hwnd=hwnd, pid=pid, process=psutil.Process(pid).name().lower(),
                rect=list(win32gui.GetWindowRect(hwnd)), title=win32gui.GetWindowText(hwnd))


class DesktopInteraction:
    OPTION_TTL = 30.0
    OPTION_LIMIT = 60

    def __init__(self, uia=None, browser=None, target_resolver=None, target_validator=None):
        if uia is None:
            from core.uia_service import shared_service
            uia = shared_service()
        self.uia = uia
        self.browser = browser
        self.target_resolver = target_resolver
        self.target_validator = target_validator
        self.lock = threading.Lock()
        self._options = None
        self._generation = 0
        self._can_execute = None

    def cancel(self):
        self._generation += 1
        self._options = None
        return dict(ok=True,action_sent=False,verified=False,
            message='已停止尚未发出的操作；已发出的操作请看画面核对',silent=True)

    def warmup(self):
        return self.uia.warmup()

    def prewarm(self):
        def run():
            try:
                self.warmup()
            except Exception:
                pass
        thread = threading.Thread(target=run, name='guga-desktop-prewarm', daemon=True)
        thread.start()
        return thread

    def close(self):
        self._options = None
        self.uia.close()

    @staticmethod
    def _guard(hwnd=None):
        import psutil
        import win32gui
        active = int(win32gui.GetForegroundWindow() or 0)
        if not active or (hwnd is not None and int(hwnd) != active):
            raise RuntimeError('前台窗口已变化，请切到目标程序；本次未发送操作')
        kind = win32gui.GetClassName(active)
        if active == win32gui.GetDesktopWindow() or kind in _SHELL_CLASSES:
            raise RuntimeError('不操作桌面或任务栏，请切到常规程序；本次未发送操作')
        identity = window_fingerprint(active)
        if identity['pid'] == os.getpid():
            raise RuntimeError('不操作自身窗口；本次未发送操作')
        identity.update(class_name=kind, process_started=psutil.Process(identity['pid']).create_time())
        if int(win32gui.GetForegroundWindow() or 0) != active:
            raise RuntimeError('读取期间前台窗口已变化；本次未发送操作')
        return identity

    def _recheck(self, identity):
        if self._can_execute and not self._can_execute():
            raise RuntimeError('这一步已取消或通话已结束，没有继续操作')
        if self._guard(identity['hwnd']) != identity:
            raise RuntimeError('窗口身份、标题或位置已变化；本次未发送操作')

    @staticmethod
    def _element(result, hwnd):
        if not isinstance(result, dict) or result.get('hwnd', hwnd) != hwnd:
            raise RuntimeError('UIA返回了错误窗口，未发送操作')
        item = result.get('element')
        if not isinstance(item, dict) or not item.get('ref') or not item.get('type'):
            raise RuntimeError('UIA目标缺少可核对的引用或类型，未发送操作')
        if item.get('hwnd', hwnd) != hwnd:
            raise RuntimeError('UIA目标属于其他窗口，未发送操作')
        return item

    @staticmethod
    def _usable(item, for_input=False):
        if item.get('enabled') is not True:
            raise RuntimeError('控件不可用，未发送操作')
        if item.get('offscreen') is not False:
            raise RuntimeError('控件不可见，未发送操作')
        if item.get('password') is not False:
            raise RuntimeError('密码控件或安全状态未知，不进入通用操作路径')
        if for_input and (item.get('type') not in ('Edit', 'ComboBox') or
                          item.get('readonly') or item.get('read_only')):
            raise RuntimeError('目标不是可填写的普通输入框，未发送操作')

    @staticmethod
    def _rect(rect, identity):
        if not isinstance(rect, dict):
            raise RuntimeError('目标没有有效屏幕区域，未发送操作')
        try:
            values = [float(rect[k]) for k in ('left', 'top', 'right', 'bottom')]
        except (KeyError, TypeError, ValueError):
            raise RuntimeError('目标屏幕区域无效，未发送操作') from None
        l, t, r, b = values
        wl, wt, wr, wb = identity['rect']
        if (not all(math.isfinite(v) for v in values) or r <= l or b <= t or
                l < wl or t < wt or r > wr or b > wb):
            raise RuntimeError('目标区域无效或超出目标窗口，未发送操作')
        return dict(zip(('left', 'top', 'right', 'bottom'), values))

    def _mouse_click(self, rect, identity):
        import ctypes
        import win32gui
        import win32con
        x, y = int((rect['left'] + rect['right']) / 2), int((rect['top'] + rect['bottom']) / 2)
        # Overlay/another window at this point must not receive the click.
        root = win32gui.GetAncestor(win32gui.WindowFromPoint((x, y)), win32con.GA_ROOT)
        if root != identity['hwnd']:
            raise RuntimeError('目标被其他窗口遮挡，未发送鼠标操作')
        user = ctypes.windll.user32
        if not user.SetCursorPos(x, y):
            raise RuntimeError('无法移动鼠标到目标位置，未发送点击')
        self._recheck(identity)
        user.mouse_event(2, 0, 0, 0, 0)
        user.mouse_event(4, 0, 0, 0, 0)

    @staticmethod
    def _send_keys(keys):
        from core.computer_control import _send_keys
        _send_keys(keys)

    def _scroll(self, identity, down):
        import ctypes
        import win32gui
        import win32con
        l, t, r, b = identity['rect']
        x, y = (l + r) // 2, (t + b) // 2
        if win32gui.GetAncestor(win32gui.WindowFromPoint((x, y)), win32con.GA_ROOT) != identity['hwnd']:
            raise RuntimeError('窗口滚动位置被遮挡，未发送操作')
        user = ctypes.windll.user32
        if not user.SetCursorPos(x, y):
            raise RuntimeError('无法移动鼠标到目标窗口，未发送滚动')
        self._recheck(identity)
        user.mouse_event(0x0800, 0, 0, -480 if down else 480, 0)

    @staticmethod
    def _missing(exc):
        from core.uia_backend import UIAUnavailable
        # Never infer absence from an arbitrary exception, ambiguity or timeout.
        return (isinstance(exc, (UIAUnavailable, NotImplementedError)) or
                str(exc) in ('UIA目标不存在或不可见，未发送操作',
                             '控件不支持该UIA操作', 'UIA目标未找到'))

    def _resolve_visual(self, identity, target):
        resolver = self.target_resolver
        if resolver is None:
            try:
                from core.desktop_targeting import resolve_target
                resolver = resolve_target
            except ImportError:
                raise RuntimeError('UIA明确未找到目标或不支持；未接入视觉定位能力，未发送操作') from None
        self._recheck(identity)
        result = resolver(identity['hwnd'], target)
        self._recheck(identity)
        self._check_visual(identity, result)
        return result

    def _check_visual(self, identity, result):
        if not isinstance(result, dict) or result.get('source') != 'visual':
            raise RuntimeError('视觉定位未返回明确目标，未发送操作')
        if result.get('window_fingerprint') != {key: identity[key] for key in _FP_KEYS}:
            raise RuntimeError('视觉目标的窗口指纹已变化，未发送操作')
        observed = result.get('observed_at')
        age = time.monotonic() - observed if isinstance(observed, (int, float)) else math.inf
        if not math.isfinite(age) or age < 0 or age >= self.OPTION_TTL or not result.get('frame_digest'):
            raise RuntimeError('视觉快照已过期或缺少画面凭据，未发送操作')

    def _validate_visual(self, identity, result):
        self._check_visual(identity, result)
        validator = self.target_validator
        if validator is None:
            try:
                from core.desktop_targeting import validate_target
                validator = validate_target
            except ImportError:
                raise RuntimeError('未接入视觉画面复核能力，未发送操作') from None
        if validator(identity['hwnd'], result) is False:
            raise RuntimeError('视觉画面复核未通过，未发送操作')
        self._recheck(identity)

    def _list(self, identity):
        self._options = None
        started = time.monotonic()
        hwnd = identity['hwnd']
        try:
            result = self.uia.snapshot(hwnd, limit=240)
        except Exception as exc:
            if not self._missing(exc):
                raise
            result = dict(elements=[])
        self._recheck(identity)
        if not isinstance(result, dict) or result.get('hwnd', hwnd) != hwnd:
            raise RuntimeError('UIA快照属于其他窗口，未保存编号')
        items = [copy.deepcopy(item) for item in result.get('elements', [])
                 if isinstance(item, dict) and item.get('ref') and
                 (item.get('type') in _CLICK_TYPES or
                  any(p in item.get('patterns', []) for p in ('InvokePattern', 'SelectionItemPattern')))]
        source = 'uia'
        if not items:
            visual = self._resolve_visual(identity, '')
            items = []
            for element in visual.get('elements', [])[:self.OPTION_LIMIT]:
                if not isinstance(element, dict) or not element.get('name'):
                    continue
                item = copy.deepcopy(element)
                item.update(source='visual', visual_snapshot=copy.deepcopy(visual))
                item['visual_snapshot'].pop('elements', None)
                if element.get('target_signature'):
                    item['visual_snapshot']['target_signature']=element['target_signature']
                # Visual controls cannot assert accessibility/security flags.
                for field in ('enabled', 'offscreen', 'password'):
                    item.setdefault(field, None)
                items.append(item)
            source = 'visual'
        items = items[:self.OPTION_LIMIT]
        if not items:
            raise RuntimeError('当前窗口没有可明确定位的可点选项，未保存编号')
        self._recheck(identity)
        self._options = dict(identity=copy.deepcopy(identity), at=started, items=items)
        labels = []
        options = []
        for index, item in enumerate(items, 1):
            name = item.get('name') or '未命名'
            kind={'Edit':'输入框','ComboBox':'下拉框','Button':'按钮','MenuItem':'菜单项',
                'TabItem':'标签','ListItem':'列表项','CheckBox':'勾选框','RadioButton':'选择项',
                'Hyperlink':'链接','TreeItem':'列表项','VisualTarget':'画面中的选项'}.get(item.get('type'),'选项')
            notes=[]
            if item.get('enabled') is False:notes.append('暂时不可用')
            if item.get('offscreen') is True:notes.append('不在当前画面内')
            if item.get('password') is True:notes.append('密码框，不代填')
            labels.append(f"{index}. {name}（{kind}"+('；'+'；'.join(notes) if notes else '')+'）')
            # Never include field values/password content in the bubble or cache.
            options.append(dict(index=index, name=name, type=item.get('type', ''),
                                **{k: item.get(k) for k in ('enabled', 'offscreen', 'password')}))
            item.pop('value', None)
        message = '可点选项（30秒内有效，操作后失效）：\n' + '\n'.join(labels)
        if result.get('truncated') or len(items) == self.OPTION_LIMIT:
            message += '\n列表可能未列全，请使用精确名称。'
        return dict(ok=True, action_sent=False, verified=False, source=source,
                    options=options, message=message, bubble=message, silent=False)

    def _indexed(self, command, identity):
        cached = self._options
        if not cached or not 0 <= time.monotonic() - cached['at'] < self.OPTION_TTL:
            raise RuntimeError('选项编号已过期，请重新列出可点选项')
        if cached['identity'] != identity:
            raise RuntimeError('编号所属窗口已变化，请重新列出可点选项')
        index = command.get('index', command.get('target'))
        if type(index) is not int or not 1 <= index <= len(cached['items']):
            raise RuntimeError('选项编号不存在，请重新查看清单')
        old = cached['items'][index - 1]
        if old.get('source') == 'visual':
            result = dict(old['visual_snapshot'], name=old['name'], rect=old.get('rect'))
            for field in ('enabled', 'offscreen', 'password'):
                if old.get(field) is (False if field == 'enabled' else True):
                    raise RuntimeError('编号控件不可用、屏外或为密码控件，未发送操作')
            return None, result
        self._usable(old)
        item = self._element(self.uia.read(identity['hwnd'], ref=old['ref'],
                                         name=old.get('name', ''), control_type=old['type']), identity['hwnd'])
        if any(item.get(k) != old.get(k) for k in ('ref', 'name', 'type', 'rect')):
            raise RuntimeError('编号控件的位置、名称或类型已变化，请重新列出选项')
        self._usable(item)
        return item, None

    def _named(self, identity, target, for_input=False):
        hwnd = identity['hwnd']
        if not isinstance(target, str) or not target or len(target) > 120:
            raise ValueError('需要明确且不超过120字的目标名称')
        # Exact names always win; do not guess a generic field on ambiguity.
        try:
            return self._element(self.uia.read(hwnd, name=target, include_offscreen=True,
                                 **({'control_type': 'Edit'} if for_input else {})), hwnd), None
        except Exception as exc:
            if not self._missing(exc):
                raise
            missing = exc
        if target in _GENERIC_INPUTS:
            if identity['process'] in _BROWSERS:
                result = self.uia.page_input(hwnd, search='搜索' in target)
            else:
                method = getattr(self.uia, 'input_target', None)
                if method is None:
                    raise RuntimeError('当前UIAService未接入input_target，未发送操作')
                result = method(hwnd, search='搜索' in target)
            return self._element(result, hwnd), None
        if for_input:
            # Visual coordinates cannot provide UIA value readback or password status.
            raise RuntimeError(f'{missing}；视觉坐标不支持安全填写与回读，未发送操作')
        return None, self._resolve_visual(identity, target)

    def _activate_browser(self):
        """Bring an existing browser window forward when Guga itself is foreground.

        Typing in Guga's own chat leaves Guga as the foreground window, so a
        browser step would otherwise be refused as "own window". Focus is only
        moved when the user is not working in another application.
        """
        import win32con
        import win32gui
        import win32process
        try:
            foreground = int(win32gui.GetForegroundWindow() or 0)
            if not foreground:
                return None
            if win32process.GetWindowThreadProcessId(foreground)[1] != os.getpid():
                return None
            from core.window_inventory import list_windows
            windows = [window for window in list_windows('浏览器', 50)
                       if int(window.get('pid', 0)) != os.getpid()]
        except Exception:
            return None
        if not windows:
            return None
        selected = next((window for window in windows if window.get('foreground')), windows[0])
        hwnd = int(selected['hwnd'])
        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            return None
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if int(win32gui.GetForegroundWindow() or 0) == hwnd:
                return hwnd
            time.sleep(0.03)
        return None

    def execute(self, command, hwnd=None):
        started = time.perf_counter()

        def finish(result):
            return dict(result, route='local_desktop_step',
                        dispatch_ms=round((time.perf_counter() - started) * 1000, 2))

        if not self.lock.acquire(blocking=False):
            return finish(dict(ok=False, action_sent=False, code='desktop_busy', silent=False,
                               error='上一项界面操作尚未结束，本次未发送操作'))
        flags = {'attempted': False, 'is_list': False}
        try:
            action, identity = self._prepare(command, hwnd, flags)
            hwnd = identity['hwnd']
            if flags['is_list']:
                return finish(dict(self._list(identity), hwnd=hwnd))
            from core.wechat_input_scope import require_wechat_input_scope
            require_wechat_input_scope(hwnd)
            if action in _BROWSER_ONLY:
                return finish(self._browser_step(identity, action, command, hwnd, flags))
            verified = False
            source = 'uia'
            if action in _KEYS:
                self._recheck(identity)
                flags['attempted'] = True
                self._send_keys(_KEYS[action])
                message = '已发送快捷键'
            elif action in ('scroll_down', 'scroll_up'):
                self._recheck(identity)
                flags['attempted'] = True
                self._scroll(identity, action == 'scroll_down')
                message = '已发送滚动'
            elif action in ('fill', 'append', 'clear', 'fill_named'):
                self._fill_step(identity, action, command, hwnd, flags)
                verified = True
                message = '已填写并回读确认'
            else:
                verified, source, message = self._pointer_step(identity, action, command, hwnd, flags)
            return finish(dict(ok=True, action_sent=True, verified=verified, source=source,
                               hwnd=hwnd, message=message, silent=True))
        except Exception as exc:
            error = str(exc).replace('UIA', '界面').replace('窗口指纹', '窗口').replace('ref', '按钮记录')
            return finish(dict(ok=False, code='desktop_step_failed', silent=False,
                               action_sent=None if flags['attempted'] else False, error=error[:500]))
        finally:
            # Even a rejected/uncertain action consumes numbers; never reuse stale intent.
            if not flags['is_list']:
                self._options = None
            self._can_execute = None
            self.lock.release()

    def _prepare(self, command, hwnd, flags):
        """Validate the step and resolve its window before any input is sent."""
        generation = command.get('_step_generation', self._generation) if isinstance(command, dict) else self._generation
        external_check = command.get('_can_execute') if isinstance(command, dict) else None
        self._can_execute = lambda: self._generation == generation and (external_check is None or external_check())
        if not self._can_execute():
            raise RuntimeError('这一步已取消，没有发送操作')
        from core.desktop_access import DesktopAccess
        if not DesktopAccess().enabled:
            raise RuntimeError('完整访问未开启，本次未发送操作')
        if not isinstance(command, dict):
            raise ValueError('需要已解析的单步命令')
        action = command.get('step_action') or command.get('browser_action')
        flags['is_list'] = action in ('list_targets', 'list_options')
        if action == 'unsupported':
            raise ValueError(command.get('error') or '不支持该本地单步操作，本次未发送操作')
        if action not in _SUPPORTED_ACTIONS:
            raise ValueError('不支持该本地单步操作，本次未发送操作')
        if action in ('fill', 'append', 'fill_named') and (
                _input_method_question(command.get('source_text', '')) or
                _unsafe_input(command.get('source_text', ''))):
            raise ValueError('输入法问句或复合输入不是单步填写，本次未发送操作')
        if command.get('kind') == 'browser':
            activated = self._activate_browser()
            if activated:
                hwnd = activated
        return action, self._guard(hwnd)

    def _browser_step(self, identity, action, command, hwnd, flags):
        """Delegate a browser-only action to BrowserInteraction after a recheck."""
        if identity['process'] not in _BROWSERS:
            raise RuntimeError('该动作只适用于浏览器；当前是其他程序，未发送浏览器快捷键')
        if self.browser is None:
            from core.browser_interaction import BrowserInteraction
            self.browser = BrowserInteraction()
        self._recheck(identity)
        flags['attempted'] = True
        result = self.browser.execute(dict(command, browser_action=action), hwnd=hwnd)
        if not isinstance(result, dict):
            raise RuntimeError('浏览器执行回执不明，请先核对，未自动重试')
        return dict(result, silent=bool(result.get('ok')), hwnd=hwnd)

    def _fill_step(self, identity, action, command, hwnd, flags):
        """Write text into the focused (or named) input and verify the readback."""
        if action == 'fill_named':
            item, _ = self._named(identity, command.get('target'), for_input=True)
            text = command.get('text')
        else:
            item = self._element(self.uia.focused(hwnd), hwnd)
            if item.get('focused') is not True:
                raise RuntimeError('当前焦点不是目标输入框，未发送操作')
            text = '' if action == 'clear' else command.get('target')
        self._usable(item, for_input=True)
        if action != 'clear':
            literal = _payload(action, command.get('source_text', ''))
            if literal is not None:
                text = literal
        if not isinstance(text, str):
            raise ValueError('输入内容必须为文字')
        if action == 'append':
            if item.get('value_truncated') or not isinstance(item.get('value'), str):
                raise RuntimeError('无法完整回读原有内容，未执行追加输入')
            text = item['value'] + text
        if len(text) > 4000:
            raise ValueError('输入内容超过4000字，本次未发送操作')
        # A second focused read binds focus-only input; never select another edit.
        if action != 'fill_named':
            current = self._element(self.uia.focused(hwnd), hwnd)
            if current['ref'] != item['ref'] or current.get('focused') is not True:
                raise RuntimeError('输入焦点已变化，未发送操作')
        self._recheck(identity)
        flags['attempted'] = True
        receipt = self.uia.act(hwnd, 'set_value', ref=item['ref'], value=text)
        if not isinstance(receipt, dict) or receipt.get('verified') is not True or receipt.get('fallback'):
            raise RuntimeError('填写已尝试但回读未匹配，结果不确定；请先核对，未自动重试')

    def _pointer_step(self, identity, action, command, hwnd, flags):
        """Click or focus one element, with validated visual coordinates as fallback."""
        if action == 'click_index':
            item, visual = self._indexed(command, identity)
        else:
            item, visual = self._named(identity, command.get('target'))
        source = 'uia'
        if visual is not None:
            if action == 'focus':
                raise RuntimeError('视觉坐标无法确认输入焦点，未发送操作')
            rect = self._rect(visual.get('rect'), identity)
            self._validate_visual(identity, visual)
            flags['attempted'] = True
            self._mouse_click(rect, identity)
            return False, 'visual', '已点中画面里的目标；请看画面确认结果'
        self._usable(item, for_input=action == 'focus')
        operation = 'focus' if action == 'focus' or item['type'] in ('Edit', 'ComboBox') else 'invoke'
        self._recheck(identity)
        flags['attempted'] = True
        receipt = self.uia.act(hwnd, operation, ref=item['ref'])
        if not isinstance(receipt, dict):
            raise RuntimeError('UIA回执不明，请先核对，未自动重试')
        if receipt.get('fallback'):
            # This receipt explicitly means UIA has NOT sent an action.
            if operation != 'invoke' or receipt.get('input_sent') is not False:
                raise RuntimeError('UIA动作结果不确定，未发送第二次操作')
            flags['attempted'] = False
            rect = self._rect(receipt.get('rect'), identity)
            self._recheck(identity)
            flags['attempted'] = True
            self._mouse_click(rect, identity)
        elif receipt.get('input_sent') is not True and receipt.get('verified') is not True:
            raise RuntimeError('UIA未确认动作是否发送，结果不确定；请先核对，未自动重试')
        verified = receipt.get('verified') is True
        if operation == 'focus' and not verified:
            raise RuntimeError('聚焦已尝试但未确认，结果不确定；请先核对，未自动重试')
        return verified, source, '已选中输入框' if operation == 'focus' else '已点击；请看画面确认结果'
