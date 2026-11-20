"""JoyCode 界面自动化薄封装：激活窗口 → 定位输入框 → 输入并断言 → 提交 → 状态回读。

设计约束：
- 感知原语全部复用现有实现（``core.window_inventory`` / ``core.computer_control`` /
  ``core.uia_service``），本文件只做编排，不复制第二套 UIA/OCR/输入实现。
- 定位顺序：UIA 焦点元素优先；拿不到再按窗口区域 OCR 视觉定位。
- 所有坐标都由运行时窗口矩形与 OCR 结果按比例推导；不写死像素坐标。
- 输入内容用剪贴板回读做精确断言；不一致就清空重来，避免输入法/焦点污染。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from core.paste_verify import ClipboardGuard as _ClipboardGuard
from core.paste_verify import input_verified, sent_mode_check, chip_label

# 前台抢占预算：同一进程内最多每 30 秒主动激活一次，重复触发会被拒绝。
_FOCUS_LOCK = threading.Lock()
_LAST_ACTIVATE = [0.0]
# 相同命令去重：模型可能对同一个 task_id 反复 prompt，重复发送只会反复抢前台。
_RECENT_TRIGGERS = {}
_TRIGGER_TTL = 150.0
# 用户最近有键盘/鼠标输入时不允许抢占焦点。
_USER_BUSY_SECONDS = 3.0

# 右侧 JoyCode 面板（相对窗口的比例区域）
PANEL_REGION = dict(x=0.55, y=0.08, w=0.45, h=0.90)
# 面板已打开的视觉标记：只收面板/输入区专属词，避免编辑器正文（"指令""上下文"）误判。
PANEL_MARKERS = ('添加上下文', '最近任务', '问题修复', '代码洪流', '已深度思考', '停止生成')
PLACEHOLDER_TOKENS = ('添加上下文',)
GENERATING_TOKENS = ('思考中', '生成中', '正在生成', '正在思考', '停止生成', '中断')
ERROR_TOKENS = ('错误', '失败', '重试', '未授权', '无权限', '超时', '异常', '失败原因')
# 输入位置证明：面板区与编辑器区互斥，用于识别“键入打进了编辑器”。
# 编辑器文本从侧边栏右侧就开始了（约窗口宽度的 0.13），不是 0.30；
# 用 0.02 起覆盖整个编辑区（含侧边栏，侧边栏文本不含任务命令/探针），右界 0.86 避开 JoyCode 面板。
# 编辑器区右界必须停在面板左缘（约 0.72）。面板宽度会随用户拖拽变化，
# 写死旧值（0.84/0.86）会把面板文字误判成编辑器正文。
EDITOR_REGION = dict(x=0.02, y=0.10, w=0.70, h=0.82)


def _log(event, detail=''):
    """把提交路径（回车/发送按钮、每次回读、最终是否清空）落到 app_log。

    打包版没有 stderr，日志是现场唯一能回溯的证据；写失败一律静默，不影响派单。
    """
    try:
        from core.app_log import log
        if isinstance(detail, (dict, list)):
            detail = json.dumps(detail, ensure_ascii=False)
        log(event, detail)
    except Exception:
        pass

# 输入框点击点（窗口比例坐标，不写死像素）：
# 主选点由当前画面的模型芯片/提示行动态推导；这两个比例是 OCR 锚点全失效时的兜底。
# 实测（剪贴板回读证明焦点落在输入框）：会话页 (0.820, 0.870) 在 1500x950 与
# 1200x800 两种窗口尺寸下均命中；首页 (0.720, 0.430) 在 1500x950 的真实派单中命中。
# 明细见 JOYCODE-GUGA.md「输入框点击落点实测」。
COMPOSER_HOME_CLICK = dict(x=0.720, y=0.430)
COMPOSER_CHAT_CLICK = dict(x=0.820, y=0.870)
# 首页输入框的相对高度会随窗口尺寸漂移：0.43 在 1500x950 命中，
# 1705x898 时输入框整体下移到约 0.47~0.56，0.43 落进“代码洪流”欢迎语（被判成 panel）。
COMPOSER_HOME_EXTRA = (dict(x=0.750, y=0.500), dict(x=0.720, y=0.520),
                       dict(x=0.790, y=0.480))
COMPOSER_CHAT_EXTRA = (dict(x=0.820, y=0.920), dict(x=0.800, y=0.880))
# 页面判别：会话页的输入提示贴在底部，首页的提示行在中部。
HOME_PAGE_TOKENS = ('代码洪流', '一念即成', '问问咕嘎', '交给我一件事')
CHAT_HINT_TOKENS = ('添加上下文', '快捷指令', '切换历史输入')
# 回读文本长度上限：空/短输入框 ≤300 字；整页选中 ≥600 字，编辑器整篇更长。
COMPOSER_MAX_CHARS = 300


class JoyCodeUIError(RuntimeError):
    """带分级码的界面自动化错误，供咕嘎按类型决定是重试、改道还是请人确认。"""

    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code = code
        self.detail = detail or {}

    def as_dict(self):
        return dict(error=self.code, message=str(self), detail=self.detail)


def _clean(value):
    return re.sub(r'\W', '', str(value or '')).casefold()


def _band(words):
    """按阅读顺序把 OCR 词拼成一条无标点字符串，抵消 OCR 把中文拆成单字的问题。"""
    ordered = sorted(words, key=lambda w: (round(float(w.get('y', 0)), 2), float(w.get('x', 0))))
    return ''.join(_clean(w.get('text', '')) for w in ordered)


class _ComposerTypingDriver:
    """JoyCode 输入框驱动：清空 → 键入 → 剪贴板回读，重试与剪贴板恢复走共用实现。"""

    def __init__(self, ui):
        self.ui = ui

    def clear(self):
        self.ui._clear_composer()

    def write(self, text):
        self.ui._act('type', text=text)
        time.sleep(0.35)

    def read(self):
        return self.ui._read_composer()

    # ---- 技能/模式探测（判定在 core.paste_verify，本适配器只提供探针） ----
    def mode(self):
        """输入框若挂着「标签 + ×」的技能 chip，就先别发（JoyCode 正常不会有）。"""
        try:
            label = chip_label(self.read() or '')
        except Exception:
            label = ''
        if label:
            return dict(plain=False, label=label)
        return dict(plain=True, label='')

    def clear_mode(self, label=''):
        # JoyCode 的 chip 没有稳定的可点位置：宁可中止也不盲点。
        raise RuntimeError('JoyCode 输入框里的技能标签「%s」无法自动移除' % (label or '?'))


class JoyCodeUI:
    def __init__(self, computer=None, launcher=None, workspace=None, window_timeout=45.0, cancel=None):
        self._computer = computer
        self.launcher = launcher
        self.workspace = str(workspace) if workspace else None
        self.window_timeout = max(5.0, float(window_timeout))
        self.cancel = cancel
        self._hwnd = 0

    # ---- 基础原语 ----
    @property
    def computer(self):
        if self._computer is None:
            from core.computer_control import ComputerControl
            self._computer = ComputerControl()
        return self._computer

    def _check_cancel(self):
        if self.cancel is not None and getattr(self.cancel, 'is_set', lambda: False)():
            raise JoyCodeUIError('cancelled', '操作已被取消')

    def _execute(self, action, **arguments):
        self._check_cancel()
        if not arguments.get('hwnd'):
            arguments['hwnd'] = int(getattr(self, '_hwnd', 0))
        return self.computer.execute(dict(action=action, **arguments))

    def _observe(self):
        return self._execute('observe')

    # ---- 模态弹窗闸 ----
    @staticmethod
    def _process_id(hwnd):
        try:
            import win32process
            return int(win32process.GetWindowThreadProcessId(int(hwnd))[1] or 0)
        except Exception:
            return 0

    def _owned_dialogs(self, hwnd):
        """列出属于该窗口的可见原生弹窗（``#32770``，owner/parent 指向主窗口）。"""
        import win32gui
        pid = self._process_id(hwnd)
        found = []

        def visit(window, _):
            try:
                if not win32gui.IsWindowVisible(window) or win32gui.GetClassName(window) != '#32770':
                    return True
                owner = int(win32gui.GetWindow(window, 4) or 0)
                parent = int(win32gui.GetParent(window) or 0)
                if pid and self._process_id(window) == pid and \
                        (owner == int(hwnd) or parent == int(hwnd) or not owner):
                    found.append(window)
            except Exception:
                pass
            return True

        win32gui.EnumWindows(visit, None)
        return found

    @staticmethod
    def _dialog_buttons(dialog):
        import win32gui
        buttons = []

        def visit(child, _):
            try:
                if win32gui.GetClassName(child) == 'Button':
                    text = str(win32gui.GetWindowText(child) or '').strip()
                    if text:
                        buttons.append((child, text))
            except Exception:
                pass
            return True

        win32gui.EnumChildWindows(dialog, visit, None)
        return buttons

    @staticmethod
    def _dialog_texts(dialog):
        """UIA 能读到弹窗正文；读不到就退回标题，绝不阻塞。"""
        try:
            from core.uia_service import shared_service
            snapshot = shared_service().snapshot(dialog, limit=40, time_budget=1.5)
            return [str(item.get('name') or '')[:160] for item in snapshot.get('elements', [])
                    if str(item.get('name') or '').strip()][:10]
        except Exception:
            return []

    def _dismiss_dialog(self, dialog, button):
        """后台点掉原生弹窗（Win32 消息不依赖前台）。"""
        import win32con
        import win32gui
        control_id = int(win32gui.GetDlgCtrlID(button) or 0)
        win32gui.SendMessage(dialog, win32con.WM_COMMAND,
                             (win32con.BN_CLICKED << 16) | control_id, button)
        time.sleep(0.6)

    def modal_guard(self, hwnd=None):
        """每一步写操作前的闸：屏幕被 JoyCode 模态挡住时先处理，处理不掉就中止。

        实测弹窗是原生 ``#32770``，owner 指向主窗口，主窗口会被禁用；
        但 ``GetLastActivePopup(main)`` 返回主窗口自己，所以不能靠它判断。
        """
        import win32gui
        hwnd = int(hwnd or getattr(self, '_hwnd', 0) or 0)
        if not hwnd or not win32gui.IsWindow(hwnd):
            return dict(modal=False, dialogs=[])
        for _ in range(3):
            dialogs = self._owned_dialogs(hwnd)
            if not dialogs:
                return dict(modal=False, dialogs=[])
            dialog = dialogs[0]
            buttons = self._dialog_buttons(dialog)
            texts = self._dialog_texts(dialog)
            detail = dict(hwnd=dialog, title=win32gui.GetWindowText(dialog),
                          buttons=[text for _, text in buttons], texts=texts)
            dismiss = [child for child, text in buttons
                       if text.replace('&', '').replace('(', '').replace(')', '').strip()
                       in ('确定', '确定O', 'OK', '关闭', 'Close')]
            if not dismiss:
                raise JoyCodeUIError('modal_blocked',
                                     'JoyCode 弹窗挡住界面，且没有可识别的确定/关闭按钮，请人工关闭后重试',
                                     dict(detail, manual_required=True))
            self._dismiss_dialog(dialog, dismiss[0])
            if win32gui.IsWindowVisible(dialog):
                raise JoyCodeUIError('modal_blocked',
                                     'JoyCode 弹窗点击确定后仍存在，请人工关闭后重试',
                                     dict(detail, manual_required=True))
        if self._owned_dialogs(hwnd):
            raise JoyCodeUIError('modal_blocked',
                                 'JoyCode 弹窗反复出现，自动关闭 3 次仍存在，请人工处理',
                                 dict(hwnd=self._owned_dialogs(hwnd)[0], manual_required=True))
        return dict(modal=True, dismissed=True)

    def _act(self, action, **arguments):
        """每个动作前重新观察，回执里带画面证据；观察会被消费，不能复用。

        ``computer.execute('observe')`` 在窗口不在最前时会自行把窗口拉到前台，这里先挡住：
        一旦用户把焦点切走，就地中止，绝不为了继续自动化而抢回前台。
        """
        hwnd = int(arguments.get('hwnd') or getattr(self, '_hwnd', 0) or 0)
        if hwnd:
            # 先过模态闸：界面被弹窗挡住时打字是空操作，继续往下只会干等超时。
            self.modal_guard(hwnd)
            if not self._foreground_self(hwnd):
                raise JoyCodeUIError('foreground_busy', '用户已切走焦点，自动输入中止',
                                     dict(hwnd=hwnd, action=action))
        observation = self._observe()
        arguments.setdefault('verify', False)
        return self._execute(action, observation_id=observation['observation_id'], **arguments)

    # ---- 窗口 ----
    @staticmethod
    def _area(item):
        rect = item.get('rect') or (0, 0, 0, 0)
        return max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])

    def window(self):
        """返回当前已经存在的 JoyCode 主窗口；没有则 None。"""
        from core.window_inventory import list_windows
        candidates = [w for w in list_windows('joycode')
                      if str(w.get('process_name', '')).casefold() == 'joycode.exe' and w.get('title')]
        if not candidates:
            return None
        main = max(candidates, key=self._area)
        return dict(hwnd=int(main['hwnd']), title=str(main.get('title') or ''),
                    minimized=bool(main.get('minimized')),
                    foreground=bool(main.get('foreground')))

    @staticmethod
    def _foreground_self(hwnd):
        """只读判断目标窗口是否已经在最前；不触发任何切换。"""
        try:
            import win32gui
            return int(win32gui.GetForegroundWindow() or 0) == int(hwnd or 0)
        except Exception:
            return False

    @staticmethod
    def _minimized(hwnd):
        try:
            import win32gui
            return bool(win32gui.IsIconic(int(hwnd)))
        except Exception:
            return False

    @staticmethod
    def _user_idle_seconds():
        """用户最后一次键鼠输入距今秒数；用于判断“用户正在用电脑”。"""
        try:
            import ctypes
            from ctypes import wintypes

            class _LASTINPUTINFO(ctypes.Structure):
                _fields_ = [('cbSize', wintypes.UINT), ('dwTime', wintypes.DWORD)]

            info = _LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
                return 0.0
            return max(0.0, (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0)
        except Exception:
            return 0.0

    def ensure_window(self, activate=False):
        """复用已存在的窗口，绝不重复抢前台。

        ``activate=True`` 只在“窗口原本不存在（冷启动）”或“窗口被最小化”时允许唤起一次；
        窗口已经存在且只是不在最前时，保持只读，交给调用方在真正需要输入时再申请。
        """
        existing = self.window()
        if existing:
            hwnd = existing['hwnd']
            self._hwnd = hwnd
            restored = False
            if activate and self._minimized(hwnd):
                try:
                    self._activate(hwnd, allow_steal=True, reason='restore-minimized')
                    restored = True
                except JoyCodeUIError:
                    restored = False
            return dict(existing, launched=False, activated=restored)

        launch = self._launch_detached()
        deadline = time.monotonic() + self.window_timeout
        while time.monotonic() < deadline:
            self._check_cancel()
            found = self.window()
            if found and self._ready(found, launch):
                hwnd = found['hwnd']
                self._hwnd = hwnd
                activated = False
                if activate:
                    try:
                        self._activate(hwnd, allow_steal=True, reason='cold-launch')
                        activated = True
                    except JoyCodeUIError:
                        activated = False
                return dict(found, launched=True, activated=activated, launch=launch)
            time.sleep(0.8)
        raise JoyCodeUIError('window_not_ready',
                             'JoyCode 窗口在 %.0f 秒内没有就绪' % self.window_timeout,
                             dict(launch=launch, launcher=self.launcher))

    def _ready(self, window, launch):
        """刚启动时先出现一个只有 ``JoyCode`` 标题的加载窗口；标题带上工程名才算可用。"""
        title = str(window.get('title') or '').strip()
        if ' - ' in title and title.endswith('- JoyCode'):
            if self.workspace:
                name = Path(self.workspace).name
                return bool(name) and name.casefold() in title.casefold()
            return True
        # 没有工程名的裸窗口：加载通常 10-20 秒，超过宽限期就当作可用。
        elapsed = time.monotonic() - float(launch.get('_started') or 0)
        return elapsed >= 20

    def _activate(self, hwnd, allow_steal=False, reason=''):
        """唯一的窗口唤起入口，带三道硬约束。

        1. 已在最前：直接跳过，不做任何 Windows 调用。
        2. 未获授权（``allow_steal=False``）：只记录 hwnd，绝不切换。
        3. 用户 3 秒内有键鼠输入，或 30 秒内刚激活过：拒绝并抛 ``foreground_busy``。
        """
        hwnd = int(hwnd)
        self._hwnd = hwnd
        if self._foreground_self(hwnd):
            return dict(focused=True, activated=False, skipped='already-foreground', reason=reason)
        if not allow_steal:
            return dict(focused=False, activated=False, skipped='steal-disabled', reason=reason)
        idle = self._user_idle_seconds()
        if idle < _USER_BUSY_SECONDS:
            raise JoyCodeUIError('foreground_busy',
                                 '用户正在操作其他窗口，已拒绝抢占焦点（%s）' % reason,
                                 dict(hwnd=hwnd, idle_seconds=round(idle, 2),
                                      reason=reason, manual_required=True))
        now = time.monotonic()
        with _FOCUS_LOCK:
            cooldown = 30.0 - (now - _LAST_ACTIVATE[0])
            if cooldown > 0:
                raise JoyCodeUIError('foreground_busy',
                                     '前台激活过于频繁，%.0f 秒后再试（%s）' % (cooldown, reason),
                                     dict(hwnd=hwnd, cooldown_seconds=round(cooldown, 1),
                                          reason=reason, manual_required=True))
            _LAST_ACTIVATE[0] = now
        try:
            result = self._execute('focus', hwnd=hwnd)
        except JoyCodeUIError:
            raise
        except Exception as exc:
            raise JoyCodeUIError('window_not_ready', '无法把 JoyCode 窗口切到前台：%s' % exc,
                                 dict(hwnd=hwnd, reason=reason)) from None
        if not result.get('focused'):
            raise JoyCodeUIError('window_not_ready', 'Windows 拒绝切换前台窗口',
                                 dict(hwnd=hwnd, reason=reason))
        return dict(result, activated=True, reason=reason)

    def _launcher_path(self):
        if self.launcher:
            return self.launcher
        override = os.environ.get('GUGA_JOYCODE_LAUNCHER', '').strip()
        if override:
            return override
        for candidate in (Path(r'D:\JoyCode\JoyCode.exe'), Path(r'D:\JoyCode\bin\joycode.cmd')):
            if candidate.is_file():
                return str(candidate)
        return ''

    def _launch_detached(self):
        """WMI 启动：进程挂在 WmiPrvSE 下，生命周期不受本次命令/Job 影响。"""
        import shutil
        launcher = self._launcher_path()
        if not launcher:
            raise JoyCodeUIError('window_not_ready', '找不到 JoyCode 启动器，请设置 GUGA_JOYCODE_LAUNCHER')
        target = self.workspace or ''
        if launcher.lower().endswith(('.cmd', '.bat')):
            command_line = 'cmd.exe /c "%s"%s' % (launcher, (' "%s"' % target) if target else '')
        else:
            command_line = '"%s"%s' % (launcher, (' "%s"' % target) if target else '')
        literal = "'" + command_line.replace("'", "''") + "'"
        script = ('Invoke-CimMethod -ClassName Win32_Process -MethodName Create '
                  '-Arguments @{CommandLine=' + literal + '} | '
                  'Select-Object -ExpandProperty ProcessId')
        powershell = shutil.which('powershell.exe') or 'powershell.exe'
        record = dict(command_line=command_line, method='wmi', _started=time.monotonic())
        try:
            output = subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-Command', script],
                                    capture_output=True, text=True, timeout=30,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            pid = (output.stdout or '').strip().splitlines()[-1:] or ['']
            pid = int(pid[0]) if pid[0].isdigit() else 0
            if output.returncode == 0 and pid:
                record['pid'] = pid
                return record
            record['wmi_error'] = (output.stderr or output.stdout or '').strip()[:300]
        except Exception as exc:
            record['wmi_error'] = type(exc).__name__
        # 兜底：直接启动。若调用方在 Windows Job 内，进程可能在命令结束时被回收，
        # 因此这里只作为 WMI 不可用时的降级路径。
        try:
            subprocess.Popen([launcher, target] if target else [launcher])
            record['method'] = 'popen'
        except Exception as exc:
            raise JoyCodeUIError('window_not_ready', '启动 JoyCode 失败：%s' % exc, record) from None
        return record

    # ---- 视觉定位 ----
    def _ocr_region(self, region=None):
        """区域 OCR，并把词坐标换算回整窗比例。

        复用 ComputerControl 的图像抓取与 Windows OCR 原语；不用 execute('ocr')
        是因为它对词数做了 120 上限，长对话里会把底部输入框截掉。
        """
        region = dict(region or PANEL_REGION)
        hwnd = int(getattr(self, '_hwnd', 0))
        frame, _ = self.computer._grab(hwnd)
        width, height = frame.size
        box = (max(0, int(width * region['x'])), max(0, int(height * region['y'])),
               min(width, int(width * (region['x'] + region['w']))),
               min(height, int(height * (region['y'] + region['h']))))
        data = self.computer._ocr_words(frame.crop(box))
        words = []
        for word in data.get('words') or []:
            item = dict(word)
            item['x'] = round(region['x'] + float(word.get('x', 0)) * region['w'], 4)
            item['y'] = round(region['y'] + float(word.get('y', 0)) * region['h'], 4)
            item['w'] = round(float(word.get('w', 0)) * region['w'], 4)
            item['h'] = round(float(word.get('h', 0)) * region['h'], 4)
            words.append(item)
        return dict(text=str(data.get('text') or ''), words=words, region=region)

    def _ocr_panel(self):
        return self._ocr_region(PANEL_REGION)

    def _model_tokens(self):
        candidates = []
        try:
            settings = Path(os.getenv('APPDATA', '')) / 'JoyCode' / 'User' / 'settings.json'
            model = str(json.loads(settings.read_text(encoding='utf-8-sig')).get('JoyCode.config.chatModel') or '')
            if model:
                candidates.append(model.casefold())
                candidates.append(model.split('.')[0].casefold())
        except Exception:
            pass
        for extra in ('qwen', 'flash', 'deepseek', '模型'):
            if extra not in candidates:
                candidates.append(extra)
        return [token for token in candidates if len(token) >= 3]

    def _panel_words(self):
        try:
            ocr = self._ocr_panel()
        except Exception:
            return []
        return [w for w in (ocr.get('words') or [])
                if 0.5 <= float(w.get('x', 0)) <= 0.99 and 0.04 <= float(w.get('y', 0)) <= 0.995]

    def _panel_text(self):
        return '\n'.join(str(w.get('text') or '') for w in self._panel_words())

    def _panel_kind(self, words=None):
        """按输入提示行高度/首页文案判别当前是首页还是会话页。"""
        words = list(words if words is not None else self._panel_words())
        hints = [w for w in words
                 if any(_clean(token) in _clean(w.get('text')) for token in CHAT_HINT_TOKENS)]
        if hints:
            # 会话页输入框贴底；首页提示行在中部。同一窗口尺寸下至少差 0.2 个窗高。
            return 'chat' if max(float(w.get('y', 0)) for w in hints) >= 0.72 else 'home'
        band = _band(words)
        if any(_clean(token) in band for token in HOME_PAGE_TOKENS):
            return 'home'
        if any(float(w.get('y', 0)) > 0.78 for w in words):
            return 'chat'
        return 'home'

    def _click_candidates(self, kind, words=None):
        """输入框候选点击点，按可信度排序；全部是窗口比例坐标。

        首选当前画面的 OCR 锚点：模型芯片上移 0.055 个窗高、右移 1.8 个芯片宽度，
        实测落在输入框空白处；提示行则下移 0.035、右移 0.10。锚点全失效才退回
        两种窗口尺寸实测过的比例常量（见 JOYCODE-GUGA.md）。
        """
        words = list(words or [])
        points = []

        def add(point):
            if not 0.55 <= point['x'] <= 0.98 or not 0.04 <= point['y'] <= 0.97:
                return
            if any(abs(point['x'] - item['x']) < 0.01 and abs(point['y'] - item['y']) < 0.01
                   for item in points):
                return
            points.append(point)

        chip = [w for w in words
                if any(token in _clean(w.get('text')) for token in self._model_tokens())]
        if chip:
            anchor = max(chip, key=lambda w: float(w.get('y', 0)))
            add(dict(x=round(float(anchor['x']) + float(anchor.get('w', 0)) * 1.8, 4),
                     y=round(float(anchor['y']) - 0.055, 4), source='ocr-chip',
                     anchor=dict(text=anchor.get('text'), x=anchor.get('x'), y=anchor.get('y'),
                                 w=anchor.get('w'), h=anchor.get('h'))))
        hints = [w for w in words
                 if any(_clean(token) in _clean(w.get('text')) for token in CHAT_HINT_TOKENS)]
        if hints:
            anchor = max(hints, key=lambda w: float(w.get('y', 0)))
            add(dict(x=round(float(anchor['x']) + 0.10, 4),
                     y=round(float(anchor['y']) + 0.035, 4), source='ocr-hint',
                     anchor=dict(text=anchor.get('text'), x=anchor.get('x'), y=anchor.get('y'),
                                 w=anchor.get('w'), h=anchor.get('h'))))
        primary = COMPOSER_CHAT_CLICK if kind == 'chat' else COMPOSER_HOME_CLICK
        secondary = COMPOSER_HOME_CLICK if kind == 'chat' else COMPOSER_CHAT_CLICK
        add(dict(primary, source='ratio-%s' % kind))
        add(dict(secondary, source='ratio-other'))
        for point in (COMPOSER_CHAT_EXTRA if kind == 'chat' else COMPOSER_HOME_EXTRA):
            add(dict(point, source='ratio-%s-extra' % kind))
        return points

    def panel_open(self):
        if not getattr(self, '_hwnd', None):
            return False
        try:
            ocr = self._ocr_panel()
        except Exception:
            return False
        words = [w for w in (ocr.get('words') or []) if float(w.get('x', 0)) > 0.5]
        band = _band(words)
        # 面板标题栏是“面板已开”的硬标记：长对话滚到底时输入框提示与模型芯片
        # 都可能 OCR 不到，但标题栏一直在。
        header = [w for w in words
                  if float(w.get('y', 0)) < 0.16 and 'joycode' in _clean(w.get('text'))]
        if header:
            return True
        # 生成中/已提交时标题类标记会滚出视野，但输入框提示或“停止生成”仍在，
        # 这些同样是“面板已打开”的证据，避免误判为关闭而把面板切换掉。
        tokens = (PANEL_MARKERS + PLACEHOLDER_TOKENS + GENERATING_TOKENS
                  + tuple(self._model_tokens()))
        return any(_clean(token) in band for token in tokens)

    def _panel_occlusion(self):
        """检查面板区域是否被别的窗口盖住：逐点取最顶层窗口并比对进程 id。

        截屏走的是屏幕区域抓取，别的窗口压在 JoyCode 上时 OCR 会读到对方的内容，
        把“面板已开”判成“面板没开”（进而误按开关）。这里先做纯 Win32 遮挡检测，
        挡住就明确失败，不进入任何 OCR 决策。
        """
        import win32gui
        import win32process
        hwnd = int(getattr(self, '_hwnd', 0) or 0)
        if not hwnd or not win32gui.IsWindow(hwnd):
            return None
        try:
            pid = int(win32process.GetWindowThreadProcessId(hwnd)[1] or 0)
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        except Exception:
            return None
        width = max(1, right - left)
        height = max(1, bottom - top)
        blocked = []
        for rx, ry in ((0.62, 0.12), (0.88, 0.30), (0.82, 0.60), (0.85, 0.88)):
            x = left + int(width * rx)
            y = top + int(height * ry)
            window = win32gui.WindowFromPoint((x, y))
            if not window:
                continue
            root = win32gui.GetAncestor(window, 2)     # GA_ROOT
            try:
                root_pid = int(win32process.GetWindowThreadProcessId(root)[1] or 0)
            except Exception:
                continue
            if root_pid != pid and win32gui.IsWindowVisible(root):
                blocked.append(dict(ratio=[rx, ry], window=root, pid=root_pid,
                                    title=str(win32gui.GetWindowText(root) or '')[:80]))
        return blocked or None

    def ensure_panel(self):
        """只读确认面板：确认不了就失败，绝不按键切换。

        ``Ctrl+Alt+B`` 是“辅助侧边栏”开关而不是“打开”，在 OCR 误判时会把本来
        开着的面板按关，这正是 ``input_not_found`` 的现场根因，因此已移除自动切换。
        """
        blocked = self._panel_occlusion()
        if blocked:
            raise JoyCodeUIError('occluded',
                                 'JoyCode 面板区域被其他窗口遮挡，已中止（未发送任何快捷键）',
                                 dict(blocked=blocked, panel_region=PANEL_REGION,
                                      manual_required=True,
                                      hint='请露出 JoyCode 窗口（关掉或移开遮挡窗口）后重试'))
        if self.panel_open():
            self._panel_seen = time.monotonic()
            return dict(opened=False, method='already-visible')
        # 刚确认过打开（例如上一次发送成功）：webview 生成中会暂时读不到标记，
        # 此时按“最近已验证”直接复用，不再按键。
        if time.monotonic() - getattr(self, '_panel_seen', 0) < 600:
            return dict(opened=False, method='recently-verified')
        ocr = {}
        try:
            ocr = self._ocr_panel()
        except Exception:
            pass
        raise JoyCodeUIError('input_not_found',
                             '看不到 JoyCode 对话面板；为避免把已开面板按关，不再自动按 Ctrl+Alt+B，'
                             '请人工确认面板已打开后重试',
                             dict(panel_region=PANEL_REGION,
                                  words=len((ocr.get('words') or [])),
                                  ocr=str(ocr.get('text') or '')[:300],
                                  manual_required=True,
                                  hint='请在 JoyCode 右侧打开对话面板后再触发'))

    # ---- 输入与断言 ----
    @staticmethod
    def _clipboard_text():
        try:
            import win32clipboard as clip
            import win32con
            clip.OpenClipboard()
            try:
                if clip.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                    return str(clip.GetClipboardData(win32con.CF_UNICODETEXT) or '')
                return ''
            finally:
                clip.CloseClipboard()
        except Exception:
            return ''

    def _read_composer(self):
        """Ctrl+A / Ctrl+C 回读当前焦点里的文本。

        先清空剪贴板：输入框为空时 Ctrl+C 不会写剪贴板，不清就会读到上一次的旧内容，
        把“空输入框”误判成“有残留”。剪贴板由调用方的 ``_ClipboardGuard`` 恢复。
        """
        with _ClipboardGuard():
            self._clear_clipboard()
            self._act('hotkey', keys='ctrl+a')
            self._act('hotkey', keys='ctrl+c')
            time.sleep(0.12)
            return self._clipboard_text()

    def _focus_kind(self):
        """判断当前键盘焦点在哪：输入框 / 页面 / 编辑器 / 读不到。

        判别依据是回读长度与内容，不用 OCR：OCR 会漏识别短探针，误判为“没有焦点”。
        """
        text = self._read_composer()
        cleaned = _clean(text)
        if not cleaned:
            return dict(kind='composer', chars=0, sample='')
        if len(cleaned) <= COMPOSER_MAX_CHARS:
            return dict(kind='composer', chars=len(cleaned), sample=str(text)[:60])
        if any(_clean(token) in cleaned for token in PANEL_MARKERS + PLACEHOLDER_TOKENS):
            return dict(kind='panel', chars=len(cleaned), sample=str(text)[:60])
        return dict(kind='editor', chars=len(cleaned), sample=str(text)[:60])

    def _region_band(self, region):
        try:
            words = self._ocr_region(region).get('words') or []
        except Exception:
            return ''
        return _band(words)

    @staticmethod
    def _markers(text):
        cleaned = _clean(text)
        if not cleaned:
            return []
        if len(cleaned) <= 8:
            return [cleaned]
        # 首段（如 guga-run）会出现在“最近任务”历史里，只用中段和尾段，避免假阳性。
        mid = len(cleaned) // 2
        return [cleaned[mid:mid + 8], cleaned[-8:]]

    def _fresh_band(self, region):
        """清掉 OCR 缓存再取条带；位置证明必须基于当前画面，不能复用旧缓存。"""
        try:
            cache = getattr(self.computer, '_ocr_cache', None)
            if cache:
                cache.clear()
        except Exception:
            pass
        return self._region_band(region)

    def _visible_fresh(self, region, text):
        band = self._fresh_band(region)
        return any(marker in band for marker in self._markers(text))

    def _undo_editor(self, times=3):
        """确认键入落进编辑器后，撤销这几步；只在有正面视觉证据时调用。"""
        for _ in range(max(1, times)):
            self._act('hotkey', keys='ctrl+z')
            time.sleep(0.25)

    def _click_input(self, point):
        self._act('click', coordinate_space='ratio', x=point['x'], y=point['y'])

    def focus_composer(self, attempts=2):
        """按窗口比例真实点击输入框，用剪贴板回读证明焦点。

        点不到输入框抛 ``click_miss``（附候选点与每轮焦点证据），绝不盲打；
        焦点落到编辑器时也只读判别，不输入任何字符。
        """
        history = []
        last = {}
        for attempt in range(1, attempts + 1):
            # 没有正面证据时绝不按比例点击：面板没开/被遮挡时那个点会落进编辑器。
            if (not self.panel_open()
                    and time.monotonic() - getattr(self, '_panel_seen', 0) >= 600):
                raise JoyCodeUIError('input_not_found',
                                     '面板未确认可见，拒绝按比例点击（避免点到编辑器）',
                                     dict(history=history, manual_required=True,
                                          hint='请确认 JoyCode 对话面板已打开且未被遮挡后再触发'))
            words = self._panel_words()
            # 生成中（底部出现“停止生成”按钮）不定位、不点击：此时界面状态在变，
            # 归一化坐标容易落到消息区，宁可多等一轮也不乱点。
            if any('停止生成' in _clean(w.get('text'))
                   for w in words if float(w.get('y', 0)) >= 0.72):
                raise JoyCodeUIError('busy_generating',
                                     'JoyCode 仍在生成上一轮，未点击输入框',
                                     dict(history=history, manual_required=True,
                                          hint='等这一轮生成结束后再派单'))
            kind = self._panel_kind(words)
            for point in self._click_candidates(kind, words):
                for round_index in (1, 2):
                    self._click_input(point)
                    # Electron webview 第一次点击往往只激活视图，第二击才落到 DOM。
                    time.sleep(0.28)
                    state = self._focus_kind()
                    history.append(dict(attempt=attempt, point=point, round=round_index,
                                        focus=state['kind'], chars=state['chars']))
                    last = dict(point=point, kind=state['kind'], chars=state['chars'])
                    if state['kind'] == 'composer':
                        return dict(point=point, page=kind, focus=state, history=history)
        raise JoyCodeUIError('click_miss', '连续点击输入框都没有拿到焦点（最后焦点在%s）' % last.get('kind'),
                             dict(history=history, last=last, manual_required=True,
                                  hint='请人工点一下 JoyCode 面板底部的输入框，再让咕嘎重试'))

    def _clear_composer(self):
        """清空输入框；先确认焦点仍在输入框才删，免得误删编辑器内容。"""
        for index in range(3):
            state = self._focus_kind()
            if state['kind'] != 'composer':
                return dict(clean=False, reason='focus_moved', kind=state['kind'], rounds=index + 1)
            # 上一轮 _focus_kind 的 Ctrl+A 已选中全部残留，这里只删。
            self._act('hotkey', keys='delete')
            time.sleep(0.15)
            if not _clean(self._read_composer()):
                return dict(clean=True, rounds=index + 1)
        return dict(clean=False, reason='residue', rounds=3)

    def submit_command(self, text, attempts=2, submit=True):
        """默认派单路径：真实点击 → 回读断言 → 清空残留 → 键入 → 剪贴板精确断言 → 回车。

        清不干净就整轮重来，绝不在输入框含残留内容时按回车；键入后回读必须与命令
        逐字一致才允许回车。失败按阶段抛分级码（``click_miss`` / ``input_text_mismatch``）。
        """
        if not getattr(self, '_hwnd', None):
            raise JoyCodeUIError('window_not_ready', '还没有激活 JoyCode 窗口')
        text = str(text or '')
        if not text.strip():
            raise ValueError('submit_command 需要非空命令文本')
        history = []
        with _ClipboardGuard():
            for attempt in range(1, attempts + 1):
                self._check_cancel()
                focus = self.focus_composer()   # 点不到就抛 click_miss，不会继续
                history.append(dict(attempt=attempt, stage='focus', point=focus['point'],
                                    page=focus['page'], chars=focus['focus']['chars']))
                clear = self._clear_composer()
                history.append(dict(attempt=attempt, stage='clear', clean=clear['clean'],
                                    reason=clear.get('reason'), rounds=clear['rounds']))
                if not clear['clean']:
                    continue
                verify = input_verified(text, _ComposerTypingDriver(self), attempts=3)
                got = verify.get('text') or ''
                history.append(dict(attempt=attempt, stage='type', matched=bool(verify['ok']),
                                    read_back=got[:200],
                                    input_attempts=verify.get('attempts'),
                                    conflicts=verify.get('conflicts')))
                if not verify['ok']:
                    if self._visible_fresh(EDITOR_REGION, text):
                        self._undo_editor(3)
                    raise JoyCodeUIError(
                        verify['code'],
                        verify.get('missing') or '输入框回读与命令不一致，已中止（未按回车）',
                        dict(history=history, text=text, records=verify.get('records')))
                if self._visible_fresh(EDITOR_REGION, text):
                    # 剪贴板回读会被“编辑器里已有的同一段命令”骗过（上一轮打歪留下的残留）。
                    # 正文区只要看得见这段命令，就判定这轮打进了编辑器：撤销、重来，不按回车。
                    self._undo_editor(3)
                    history.append(dict(attempt=attempt, stage='typed-in-editor',
                                        point=focus['point']))
                    _log('joycode-submit', dict(route='type', stage='typed-in-editor',
                                                command=text, point=focus['point'],
                                                read_back=got[:160]))
                    continue
                if not submit:
                    return dict(verified=True, submitted=False, text=text,
                                point=focus['point'], history=history)
                # 回读用 Ctrl+A 选中了全文，先折叠光标再回车，避免回车替换掉选中内容。
                self._act('hotkey', keys='end')
                time.sleep(0.15)
                self._clear_clipboard()
                self._act('hotkey', keys='enter')
                time.sleep(1.2)
                read_back = self._read_composer()
                # 发送后模式校验（共用层）：走错模式时没有文字增量，重试只会更糟，立即中止。
                wrong = sent_mode_check(self._panel_text(),
                                        log=lambda tag, payload: _log('joycode-submit', payload))
                if not wrong['ok']:
                    raise JoyCodeUIError(
                        'wrong_skill', wrong['missing'],
                        dict(history=history, text=text, marker=wrong['marker'],
                             where=wrong['where'], next_action=wrong['next_action']))
                editor_hit = self._visible_fresh(EDITOR_REGION, text)
                still = bool(editor_hit) or read_back.strip() == text.strip()
                route = 'enter'
                if still and read_back.strip() == text.strip():
                    # 实测 JoyCode 面板：字符进了输入框，回车却给编辑器加了一行，
                    # 命令仍留在输入框。此时改点输入框右下角的发送按钮，再按
                    # “输入框清空 + 面板出现命令”双重条件确认发出。
                    enter_readback = read_back
                    button = self._send_point(focus['point'])
                    self._click_input(button)
                    time.sleep(1.6)
                    after = self._read_composer()
                    trace = self._visible_fresh(PANEL_REGION, text)
                    if not after.strip() and not trace:
                        time.sleep(1.2)                  # 生成区渲染慢，再取一次画面证据
                        trace = self._visible_fresh(PANEL_REGION, text)
                    generated = bool(self.status().get('generating'))
                    route = 'button'
                    read_back = after
                    still = bool(after.strip())
                    history.append(dict(attempt=attempt, stage='submit-button', point=button,
                                        enter_readback=enter_readback[:160], route=route,
                                        read_back=after[:160], cleared=not still,
                                        trace=bool(trace), generating=generated))
                    _log('joycode-submit', dict(route=route, command=text, point=button,
                                                enter_readback=enter_readback[:160],
                                                button_readback=after[:160],
                                                cleared=not still, trace=bool(trace),
                                                generating=generated,
                                                editor_hit=bool(editor_hit)))
                    if not still and (trace or generated):
                        self._panel_seen = time.monotonic()
                        return dict(verified=True, submitted=True, text=text, source='button',
                                    point=button, route=route, trace=bool(trace),
                                    history=history)
                    if not still:
                        # 输入框空了却没有任何“已发出”的画面证据：不认，下一轮重来。
                        history.append(dict(attempt=attempt, stage='submit-unconfirmed',
                                            point=button))
                        _log('joycode-submit', dict(route=route, stage='unconfirmed',
                                                    command=text, point=button))
                        continue
                else:
                    history.append(dict(attempt=attempt, stage='submit', route=route,
                                        still_in_composer=still, editor_hit=bool(editor_hit),
                                        read_back=read_back[:160]))
                    _log('joycode-submit', dict(route=route, command=text,
                                                enter_readback=read_back[:160],
                                                cleared=not bool(read_back.strip()),
                                                editor_hit=bool(editor_hit),
                                                still=bool(still)))
                    if not still:
                        self._panel_seen = time.monotonic()
                        return dict(verified=True, submitted=True, text=text, source='click',
                                    point=focus['point'], route=route, history=history)
                if editor_hit:
                    self._undo_editor(3)
        last = history[-1] if history else {}
        if last.get('stage') in ('submit', 'submit-button'):
            raise JoyCodeUIError('input_text_mismatch',
                                 '命令已键入，但回车和发送按钮都没把它发出去（输入框仍有内容）',
                                 dict(history=history, text=text, status=self.status()))
        if last.get('stage') == 'clear':
            raise JoyCodeUIError('input_text_mismatch',
                                 '输入框残留内容清不干净，已放弃发送（未按回车）',
                                 dict(history=history, text=text))
        raise JoyCodeUIError('input_text_mismatch',
                             '连续 %d 轮键入后回读都与命令不一致，已中止（避免污染编辑器）' % attempts,
                             dict(history=history, text=text,
                                  hint='可能是输入法拦截或窗口被抢占；可转人工确认'))

    def _composer_region(self, point):
        """由定位点推导输入框条带；比例坐标，随窗口/布局变化。"""
        y = float(point.get('y', 0.5))
        return dict(x=0.60, y=max(0.02, y - 0.015), w=0.40, h=0.16)

    @staticmethod
    def _send_point(point):
        """由输入框定位点推导右下角发送按钮的比例坐标。

        实测：输入区定位点 (0.83, 0.54) 时，发送按钮在 (0.955, 0.592)，
        即右移 0.126、下移 0.056 个窗宽/窗高；两个偏移都跟输入区一起随窗口缩放。
        """
        base_x = float(point.get('x', 0.83))
        base_y = float(point.get('y', 0.54))
        return dict(x=round(min(0.985, base_x + 0.126), 4),
                    y=round(min(0.975, base_y + 0.056), 4),
                    source='send-button', anchor=dict(x=base_x, y=base_y))

    def status(self, point=None):
        """OCR 判断界面是否在生成、是否报错、输入框是否已清空。"""
        ocr = self._ocr_panel()
        band = _band(ocr.get('words') or [])
        if point:
            strip = self._ocr_region(self._composer_region(point))
            strip_band = _band(strip.get('words') or [])
        else:
            strip, strip_band = None, ''
        placeholder_visible = any(_clean(token) in strip_band or _clean(token) in band
                                  for token in PLACEHOLDER_TOKENS)
        return dict(composer_empty=placeholder_visible,
                    generating=any(_clean(token) in band for token in GENERATING_TOKENS),
                    error=any(_clean(token) in band for token in ERROR_TOKENS),
                    composer_band=strip_band[:200],
                    text=str(ocr.get('text') or '')[:400])

    @staticmethod
    def _clear_clipboard():
        try:
            import win32clipboard as clip
            clip.OpenClipboard()
            try:
                clip.EmptyClipboard()
                return True
            finally:
                clip.CloseClipboard()
        except Exception:
            return False

    # ---- 对外入口 ----
    def trigger(self, text, submit=True):
        """把命令送进 JoyCode。只有这一步允许申请一次前台，且受全部硬约束。

        相同命令 150 秒内重复触发直接返回 ``skipped=True``，不再激活、不再重发；
        用户正在别的窗口操作时抛 ``foreground_busy`` 并保留人工确认路径。

        默认路径是“按窗口比例真实点击输入框 → 回读断言 → 清空残留 → 键入 →
        剪贴板精确断言 → 回车”。快捷键（F6/Tab/Ctrl+Alt+I）会插进编辑器缩进或
        绕回编辑器，已不再作为派单路径使用。
        """
        text = str(text or '')
        if not text.strip():
            raise ValueError('trigger 需要非空命令文本')
        key = _clean(text)
        now = time.monotonic()
        with _FOCUS_LOCK:
            recent = _RECENT_TRIGGERS.get(key)
            if recent and now - recent < _TRIGGER_TTL:
                self._check_cancel()
                return dict(ok=True, skipped=True, submitted=True, text=text,
                            reason='recently-triggered', hwnd=int(getattr(self, '_hwnd', 0) or 0),
                            hint='相同命令刚发送过，本次跳过重复激活与重复输入')
        # 只读复用窗口：不在这里激活；冷启动/最小化恢复才允许唤起一次。
        window = self.ensure_window(activate=True)
        self._hwnd = int(window['hwnd'])
        # 真正需要键盘输入时才申请前台一次；用户忙或刚激活过会被拒绝。
        focus = self._activate(self._hwnd, allow_steal=True, reason='trigger')
        panel = self.ensure_panel()
        result = self.submit_command(text, submit=submit)
        with _FOCUS_LOCK:
            _RECENT_TRIGGERS[key] = time.monotonic()
        result.update(hwnd=self._hwnd, launched=window.get('launched'),
                      activated=bool(focus.get('activated')), panel=panel,
                      window_title=window.get('title'))
        result['ok'] = bool(result.get('submitted')) if submit else True
        if not submit:
            result['submitted'] = False
        return result

    def manual_hint(self, task_id):
        return ('自动触发没有完成。请打开 JoyCode 右侧对话面板，手动输入 /guga-run %s 并回车；'
                '执行完后回执会写到 .guga/outbox/%s.json，再让咕嘎读回执即可。' % (task_id, task_id))
