"""A single, owner-approved WeChat inbox for messages sent to File Transfer Assistant."""
from dataclasses import dataclass
import hashlib
import re
import threading
import time

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from core.agent_memory import safe_text
from core.chat_answer import AnswerWorker
from core.wechat_demo import _send_message, wechat_window


RECIPIENT = '文件传输助手'
PREFIX = '咕嘎回复'
_BLOCK = re.compile(r'扫码|二维码|QR\s*code|付款|支付|转账|收款|红包|验证码|密码|密钥|银行卡|身份证|\b(?:api[_ -]?key|cookie|token)\b|https?://', re.I)


def _ordinary_text(text):
    return bool(text and len(text) <= 900 and not _BLOCK.search(text)
                and safe_text(text) == text and not text.startswith(('[', '【文件】', '【图片】')))


@dataclass(frozen=True)
class InboxMessage:
    key: str
    text: str


def _chat_snapshot(computer, cancel):
    import win32gui

    hwnd = wechat_window(computer, cancel)
    if not hwnd or win32gui.IsIconic(hwnd):
        return None
    tree = computer.execute(dict(action='accessibility', hwnd=hwnd, limit=400), cancel)
    elements = [row for row in tree.get('elements', []) if not row.get('offscreen')]
    labels = [row for row in elements if row.get('id', '').endswith('.current_chat_name_label')]
    fields = [row for row in elements if row.get('id') == 'chat_input_field']
    if len(labels) != 1 or labels[0].get('name') != RECIPIENT or len(fields) != 1:
        return None
    records = []
    for row in elements:
        if not row.get('id', '').startswith('chat_message_list.'):
            continue
        rect = row.get('rect') or {}
        text = row.get('name', '')
        if not rect or row.get('type') != 'ListItem':
            continue
        if row.get('class_name') == 'mmui::ChatTextItemView':
            # Current desktop client exposes names as sender + colon + bubble text.
            match = re.match(r'^[^：:\n]{1,60}[:：]\s*(.+)$', text, re.S)
            payload = (match[1] if match else text).strip()
            own = re.search(r'(?:^|[:：]\s*)(' + PREFIX + r'[:：].*)$', text, re.S)
            if own:
                payload = own[1].strip()
        else:
            payload = ''
        # Class and exact label protect against treating list scrolling as a new message.
        digest = hashlib.sha256((row.get('class_name', '') + '\0' + text).encode()).hexdigest()
        records.append((rect['top'], InboxMessage(digest, payload)))
    records.sort(key=lambda pair: pair[0])
    return hwnd, fields[0].get('value') == '', tuple(message for _, message in records)


def send_reply(computer, hwnd, message, reply, cancel):
    snapshot = _chat_snapshot(computer, cancel)
    if (cancel.is_set() or not snapshot or snapshot[0] != hwnd or not snapshot[1]
            or not any(item.key == message.key for item in snapshot[2])):
        return dict(deferred=True)
    text = PREFIX + '：' + reply
    matches = lambda rows: sum(item.text == text for item in rows)
    previous = matches(snapshot[2])
    options = dict(chat={'hwnd': hwnd}, verify_current=True)
    receipt = _send_message(computer, RECIPIENT, text, cancel, **options)
    if not receipt.get('ok') and (receipt.get('send_clicked') or receipt.get('delivery_unknown')):
        current = _chat_snapshot(computer, cancel)
        if current and current[0] == hwnd and matches(current[2]) > previous:
            receipt = dict(receipt, ok=True, verified_message_visible=True, delivery_unknown=False)
    return receipt


class InboxTail:
    def __init__(self):
        self.hwnd = None
        self.previous = None

    def advance(self, snapshot):
        if snapshot is None:
            return []
        hwnd, _, rows = snapshot
        before = self.previous
        if hwnd != self.hwnd:
            before = None
        self.hwnd, self.previous = hwnd, rows
        if before is None or before == rows:
            return []
        if not before:
            return [row for row in rows if row.text and not row.text.startswith(PREFIX)]
        # Live accessibility virtualizes earlier bubbles as the list scrolls.
        # Only accept a suffix/prefix match that still includes the last seen row.
        old = [row.key for row in before]
        new = [row.key for row in rows]
        candidates = [size for size in range(min(len(old), len(new)), 0, -1)
                      if old[-size:] == new[:size]]
        if not candidates or candidates[0] == len(new):
            return []
        return [row for row in rows[candidates[0]:]
                if row.text and not row.text.startswith(PREFIX)]


class FileAssistant(QObject):
    checked = pyqtSignal(int, object)
    sent = pyqtSignal(int, object)

    def __init__(self, panel, desktop_lock, memory):
        super().__init__(panel)
        self.panel, self.desktop_lock, self.memory = panel, desktop_lock, memory
        self.timer = QTimer(self, interval=1700)
        self.timer.timeout.connect(self._tick)
        self.checked.connect(self._observed)
        self.sent.connect(self._sent)
        self.answer = AnswerWorker(self)
        self.answer.completed.connect(self._answered)
        self.enabled = False
        self._generation = 0
        self._cancel = threading.Event()
        self._tail = InboxTail()
        self._pending = None
        self._snapshot = None
        self._reading = self._sending = False
        self._queue = []
        self.received = self.replied = self.skipped = 0
        self.state = '已停止'
        self.last_outcome = ''
        self.cid = None

    def status(self):
        return dict(enabled=self.enabled, state=self.state, received=self.received,
                    replied=self.replied, skipped=self.skipped, last_outcome=self.last_outcome)

    def _conversation(self):
        store = self.panel.store
        cid = store.preference('wechat_file_assistant_conversation')
        if cid:
            try:
                if store.conversation(cid)['agent_id'] == '001':
                    return cid
            except ValueError:
                pass
        cid = store.create_conversation('微信 · 文件传输助手', agent_id='001')
        store.preference('wechat_file_assistant_conversation', cid)
        return cid

    def enable(self, persist=True):
        if persist:
            self.panel.store.preference('wechat_file_assistant_enabled', '1')
        if self.enabled:
            return self.state
        self.cid = self._conversation()
        self._generation += 1
        self._cancel = threading.Event()
        self._tail, self._snapshot, self._queue = InboxTail(), None, []
        self.enabled = True
        self.state = '正在等文件传输助手会话'
        self.timer.start()
        self._tick()
        return '文件传输助手自动回复已开启。微信保持打开这一个会话，手机新发的普通文字会进入咕嘎对话。'

    def restore(self):
        if self.panel.store.preference('wechat_file_assistant_enabled') == '1':
            self.enable(persist=False)

    def stop(self, persist=True):
        if persist:
            self.panel.store.preference('wechat_file_assistant_enabled', '0')
        self.enabled = False
        self._generation += 1
        self.timer.stop()
        self._cancel.set()
        self.answer.cancel()
        self._queue.clear()
        self._pending = None
        self._snapshot = None
        self.state = '已停止'
        return '文件传输助手自动回复已停止。'

    def _busy(self):
        return self.panel.busy or self.panel.voice_active or self.panel.runtime.dsh.status().get('running')

    def _tick(self):
        if not self.enabled or self._reading or self._sending or self._busy():
            return
        generation, cancel = self._generation, self._cancel
        self._reading = True
        def work():
            try:
                data = _chat_snapshot(self.panel.agent.computer, cancel)
            except (OSError, RuntimeError, ValueError, KeyError):
                data = None
            self.checked.emit(generation, data)
        threading.Thread(target=work, name='guga-wechat-inbox', daemon=True).start()

    def _observed(self, generation, snapshot):
        self._reading = False
        if generation != self._generation or not self.enabled:
            return
        self._snapshot = snapshot
        if snapshot is None:
            self.state = '等待微信显示文件传输助手'
            return
        for message in self._tail.advance(snapshot):
            if not _ordinary_text(message.text):
                self.skipped += 1
                continue
            self.received += 1
            if len(self._queue) < 6:
                self._queue.append(message)
        if self._pending and self._pending.get('reply'):
            self._send_if_ready()
        elif not self._pending and self._queue:
            try:
                self._answer(self._queue.pop(0))
            except (OSError, RuntimeError, ValueError, KeyError):
                self._pending = None
                self.state = '本条拟回复未开始'
        elif not self._pending:
            self.state = '正在等待手机新消息'

    def _answer(self, message):
        self._pending = dict(message=message, started=time.monotonic())
        self.state = '正在拟回复'
        store = self.panel.store
        store.append(self.cid, 'user', message.text)
        self.memory.record('001', 'user', message.text, self.cid)
        context = self.memory.context('001', message.text, self.cid)
        context += '\n这是与你主人在文件传输助手中的聊天。用一段口语回复，控制在 80 个汉字以内，不加称谓标签和链接。'
        self.answer.start(self.cid, store.messages(self.cid, 18), message.text,
                          memory_context=context, profile=self.panel.memory.profile('001'), search_mode='off')

    def _answered(self, result):
        if not self.enabled or not self._pending or result.get('generation') != self.answer.generation:
            return
        if not result.get('ok'):
            self.state = self.last_outcome = '模型回复失败，本条未发送'
            self._pending = None
            return
        text = ' '.join(result['text'].split())[:110].strip()
        if not _ordinary_text(text):
            self.skipped += 1
            self._pending = None
            self.state = '含敏感内容，已跳过'
            return
        self._pending['reply'] = text
        self._send_if_ready()

    def _send_if_ready(self):
        if self._sending or not self._pending or not self._snapshot or self._busy():
            return
        pending = self._pending
        if time.monotonic() - pending['started'] > 180:
            self.skipped += 1
            self._pending = None
            self.state = self.last_outcome = '回复等待过久，本条已跳过'
            return
        if not self._snapshot[1]:
            self.state = '等待电脑微信输入框清空'
            return
        generation, cancel = self._generation, self._cancel
        expected_hwnd = self._snapshot[0]
        self._sending = True
        def work():
            if not self.desktop_lock.acquire(False):
                self.sent.emit(generation, dict(deferred=True))
                return
            try:
                computer = self.panel.agent.computer
                now = _chat_snapshot(computer, cancel)
                from core.workbuddy_bridge import _idle_ms
                import win32gui
                human_active = win32gui.GetForegroundWindow() != expected_hwnd and _idle_ms() < 1800
                if (cancel.is_set() or human_active or not now or now[0] != expected_hwnd or not now[1]
                        or not any(x.key == pending['message'].key for x in now[2])):
                    receipt = dict(deferred=True)
                else:
                    receipt = send_reply(computer, now[0], pending['message'], pending['reply'], cancel)
            except (OSError, RuntimeError, ValueError, KeyError) as exc:
                detail = safe_text(str(exc))[:105]
                receipt = dict(ok=False, error='微信回复未得到发送回执。' + detail)
            finally:
                self.desktop_lock.release()
            self.sent.emit(generation, receipt)
        threading.Thread(target=work, name='guga-wechat-reply', daemon=True).start()

    def _sent(self, generation, receipt):
        self._sending = False
        if generation != self._generation or not self.enabled or not self._pending:
            return
        if receipt.get('deferred'):
            return
        pending, self._pending = self._pending, None
        if receipt.get('ok') and receipt.get('verified_message_visible'):
            self.panel.store.append(self.cid, 'assistant', pending['reply'])
            self.memory.record('001', 'assistant', pending['reply'], self.cid)
            self.replied += 1
            self.state = self.last_outcome = '已在文件传输助手中看到回复'
        else:
            self.state = self.last_outcome = str(receipt.get('error') or receipt.get('message') or '回复待核对')[:120]
