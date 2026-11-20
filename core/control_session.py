# -*- coding: utf-8 -*-
"""咕嘎控制电脑的会话状态：界面层写入，调度层只读。

分工（避免两边抢写同一个事实）：
  * 界面层负责「开始/结束、当前软件与步骤、用户暂停/中止请求」，
    并在用户操作后立刻改状态，供桌面标识条显示。
  * 调度层只读取 `should_pause() / should_stop() / is_paused()`，
    在自己的安全检查点停手；需要留痕时调用 `acknowledge_pause() /
    acknowledge_stop()`，不改写当前步骤与软件。

所有写入走同一把锁，状态只增不减（revision 单调递增），
界面用轮询快照判断是否需要重绘，不依赖 Qt 跨线程信号。
"""
import threading
import time

IDLE = 'idle'
RUNNING = 'running'
PAUSING = 'pausing'        # 已收到暂停请求，执行器还没到安全点
PAUSED = 'paused'          # 执行器已停手，现场保留
STOPPING = 'stopping'      # 已收到中止请求，执行器还没停手
STOPPED = 'stopped'        # 明确中止：不是「已完成」
DONE = 'done'
ERROR = 'error'

TERMINAL = (STOPPED, DONE, ERROR)
ACTIVE = (RUNNING, PAUSING, PAUSED, STOPPING)


class ControlSession:
    """一个进程内唯一的控制会话。线程安全，snapshot() 返回不可变副本。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._finished = {}      # key → 终态：同一子任务进了终态后不许被新的 begin 冲回 running
        self._reset()
        self._revision = 0

    def _reset(self):
        self.state = IDLE
        self.app = ''
        self.step = ''
        self.step_no = 0
        self.total = 0
        self.task = ''
        self.key = ''
        self.note = ''
        self.source = ''
        self.started = 0.0
        self.pause_requested = False
        self.stop_requested = False
        self.pause_at = 0.0
        self.stop_at = 0.0

    def _touch(self):
        self._revision += 1
        return self._revision

    # ---------------- 写入（界面/任务层） ----------------

    def begin(self, app='', step='', detail='', index=None, total=None, task='', key=''):
        """开始一轮电脑控制。app=正在操作的软件，step=当前步骤，total=总步数(可选)。

        key=子任务唯一键（taskId|stId|attempt）。该键已到过终态（done/stopped/error）时
        拒绝重新开始并返回 None——避免云端重复派同一单把界面从「已完成/已中止」冲回「进行中」。
        """
        with self._lock:
            key = str(key or task or '')
            if key and self._finished.get(key):
                return None
            self._reset()
            self.key = key
            self.app = str(app or '')
            self.step = str(step or detail or '')
            self.step_no = int(index) if index else (1 if self.step else 0)
            self.total = int(total or 0)
            self.task = str(task or '')
            self.started = time.time()
            self.state = RUNNING
            return self._touch()

    def update(self, app=None, step=None, detail=None, index=None, total=None, source=''):
        """刷新当前软件/步骤。步骤文字变化时自动递增「第 N 步」。"""
        with self._lock:
            if self.state not in ACTIVE:
                return None
            if app:
                self.app = str(app)
            text = str(step if step is not None else (detail or ''))
            if text:
                if self.step and text != self.step and not index:
                    self.step_no += 1
                elif not self.step_no:
                    self.step_no = 1
                self.step = text
            if index:
                self.step_no = int(index)
            if total:
                self.total = int(total)
            self.source = str(source or self.source)
            return self._touch()

    def request_pause(self, source='user'):
        """用户要求暂停。返回 False 表示当前没有可暂停的控制。"""
        with self._lock:
            if self.state in (IDLE, DONE, ERROR, STOPPED):
                return False
            if self.state == STOPPING:
                return True
            self.pause_requested = True
            self.pause_at = time.time()
            self.source = str(source or self.source)
            if self.state == RUNNING:
                self.state = PAUSING
            return self._touch()

    def resume(self, source='user'):
        """撤销暂停请求并继续，只对暂停中/已暂停有效。"""
        with self._lock:
            if self.state not in (PAUSING, PAUSED):
                return False
            self.pause_requested = False
            self.pause_at = 0.0
            self.source = str(source or self.source)
            self.state = RUNNING
            return self._touch()

    def request_stop(self, source='user'):
        """中止整轮控制。之后无论执行器回报什么，都不会被显示成「已完成」。"""
        with self._lock:
            if self.state in (IDLE, DONE, ERROR, STOPPED):
                return False
            self.stop_requested = True
            self.pause_requested = True
            self.stop_at = time.time()
            self.source = str(source or self.source)
            self.state = STOPPING
            return self._touch()

    # ---------------- 调度层回执（不改写步骤） ----------------

    def acknowledge_pause(self):
        with self._lock:
            if not self.pause_requested or self.stop_requested:
                return False
            self.state = PAUSED
            return self._touch()

    def notify_takeover(self, note=''):
        """调度层发现用户自己接管了鼠标/键盘时调用：直接进「已暂停」，不等人回执。"""
        with self._lock:
            if self.state in (IDLE, DONE, ERROR, STOPPED):
                return False
            self.pause_requested = True
            self.pause_at = time.time()
            self.source = 'takeover'
            self.note = str(note or '你接管了鼠标，咕嘎已经停手')
            self.state = PAUSED
            return self._touch()

    def acknowledge_stop(self, note=''):
        with self._lock:
            if not self.stop_requested:
                return False
            self.state = STOPPED
            self.note = str(note or self.note)
            self.pause_requested = False
            return self._touch()

    def finish(self, ok=True, note='', key=''):
        """一轮结束。已被中止时固定落在 stopped，绝不出现「已完成」。

        到终态后按 key 记账：同一子任务再派一次也不会被 begin 冲回 running（幂等）。
        """
        with self._lock:
            if self.state == IDLE:
                return None
            self.note = str(note or '')
            if self.stop_requested:
                self.state = STOPPED
                self.pause_requested = False
            elif ok:
                self.state = DONE
            else:
                self.state = ERROR
            done_key = str(key or self.key or '')
            if done_key:
                self._finished[done_key] = self.state
            return self._touch()

    def clear(self):
        with self._lock:
            self._reset()
            return self._touch()

    # ---------------- 只读（调度层/界面） ----------------

    def should_pause(self):
        return bool(self.pause_requested)

    def should_stop(self):
        return bool(self.stop_requested)

    paused = should_pause
    stop = should_stop

    def is_paused(self):
        return self.state == PAUSED

    def active(self):
        return self.state in ACTIVE

    def snapshot(self):
        with self._lock:
            return dict(state=self.state, app=self.app, step=self.step, step_no=self.step_no,
                        total=self.total, task=self.task, note=self.note, source=self.source,
                        started=self.started, elapsed=(time.time() - self.started if self.started else 0.0),
                        pause_requested=self.pause_requested, stop_requested=self.stop_requested,
                        revision=self._revision)


session = ControlSession()


TOOL_APP = {
    'computer': '电脑界面', 'applications': '本机软件', 'wechat': '微信', 'browser': '浏览器',
    'joycode': 'JoyCode', 'pipeline': '外部工具', 'music_performance': '音乐',
    'battleground': '酒馆战棋', 'local_computer': '本机文件', 'ecosystem': '生态软件',
}
CONTROL_TOOLS = set(TOOL_APP)


def is_control_tool(name):
    """这一步是否真的在动本机界面/软件——只有这些工具才该亮起控制标识。"""
    return str(name or '') in CONTROL_TOOLS


_APP_KEYS = ('app', 'application', 'software', 'process_name', 'window', 'title',
             'recipient', 'to', 'name', 'query', 'target')


def describe_tool_app(name, arguments):
    """从一次工具调用里读出正在操作的软件名，给标识条用。取不到就返回工具的大类名。"""
    key = str(name or '')
    fallback = TOOL_APP.get(key, '')
    if not isinstance(arguments, dict):
        return fallback
    for field in _APP_KEYS:
        value = arguments.get(field)
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text or len(text) > 40:
            continue
        return text[:28]
    return fallback
