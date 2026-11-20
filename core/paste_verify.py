"""所有界面适配器共用的「输入 → 回读 → 重试 → 恢复剪贴板」实现。

背景：粘贴走系统剪贴板。用户在派单过程中自己复制内容会把要粘的文本冲掉，
出现「只粘进去一半」「粘成别人的内容」「输入框残留被追加」；
旧实现里豆包、WorkBuddy、JoyCode 各自写了不同的回读/重试，口径不一致。

这个模块只负责通用流程，不关心目标软件是谁：
    清空输入框 → 确认框内为空 → 写剪贴板 → 粘贴/键入 → 冲突检查
    → 回读 → 归一化整体比对 → 不一致则重试（最多 attempts 次）
    → 无论成功失败都恢复用户原剪贴板。

调用方只需提供一个 driver（三个回调）：
    clear()      清空输入框（全选删除等）
    write(text)  把文本写进输入框（Ctrl+V 粘贴或键入；剪贴板由本模块写）
    read()       读回输入框当前文本
"""

import re
import sys
import time

try:                       # 暂停/中止闸门与键鼠共用一套实现
    from core import input_guard
except Exception:          # 独立运行（无 core 包）时不阻断
    input_guard = None

MIN_ATTEMPTS = 1
MAX_ATTEMPTS = 3
DEFAULT_ATTEMPTS = 3

_INVISIBLE = re.compile(r'[\ufeff\u200b\u200c\u200d\u00ad]')

# ---- 技能/模式污染防护（豆包 / WorkBuddy / JoyCode 共用） ----
# 20261110u 真机现场：豆包输入框挂着技能标签「帮我写作 ×」，正文注入与回读都对
# （expected_len=45 / got_len=45），但提交后被当成生图指令，页面显示「正在生成图片」，
# 文字量不增反降（grew=-66），旧实现只看字数 → 连续重试三次后误报 send_not_confirmed。
# 所以：输入前必须先回到普通对话态；发送后先认“走错模式”的标志，再谈字数增长。

#: 走错模式的界面标志：出现任意一条就立刻中止，不再重试。
WRONG_MODE_MARKERS = (
    '正在生成图片', '正在生成幻灯片', '正在生成视频', '正在生成演示',
    '正在绘制', '生成图片：', '生成幻灯片：',
)

#: 输入框里会挂成“技能标签”的名字（标签后紧跟 × / ✕ 才算激活，常驻快捷栏不算）。
SKILL_CHIP_LABELS = (
    '帮我写作', '图像生成', '生成图片', 'PPT 生成', 'PPT生成',
    '视频生成', '实时转写', '深度研究', 'AI 搜索', 'AI搜索',
)

_CHIP_CLOSERS = ('×', '✕', '✖')

_CHIP_PATTERN = re.compile(
    r'(%s)\s*[×✕✖]' % '|'.join(re.escape(label) for label in SKILL_CHIP_LABELS))


def _gate():
    """清空/粘贴/回读前先过暂停中止闸门：暂停时原地等，中止时抛 Stopped。"""
    if input_guard is not None:
        input_guard.assert_active()


def normalize(text):
    """回读比对用的归一化：去 BOM/零宽字符、统一换行、去行尾与首尾空白。"""
    value = _INVISIBLE.sub('', str(text or ''))
    value = value.replace('\r\n', '\n').replace('\r', '\n')
    return '\n'.join(line.rstrip() for line in value.split('\n')).strip()


def _clip_length(text):
    return len(normalize(text))


def _difference(got, expected, span=20):
    """返回首尾差异片段，写进日志和“缺什么”，定位差异在哪。"""
    left, right = normalize(got), normalize(expected)
    index = 0
    limit = min(len(left), len(right))
    while index < limit and left[index] == right[index]:
        index += 1
    head = ('第 %d 字起不一致：期望“%s”，实际“%s”'
            % (index + 1, right[index:index + span], left[index:index + span]))
    tail = ('尾部期望“%s”，实际“%s”' % (right[-span:], left[-span:]))
    return head, tail


def chip_label(text):
    """从一行界面文字里认出“激活的技能标签”（标签 + ×）。普通快捷栏没有 ×，不算。"""
    match = _CHIP_PATTERN.search(str(text or ''))
    return match.group(1) if match else ''


def wrong_mode_marker(text):
    """页面文字里是否出现“走错模式”的标志，返回命中的那一条。"""
    value = str(text or '')
    for marker in WRONG_MODE_MARKERS:
        if marker in value:
            return marker
    return ''


def ensure_plain_mode(driver, log=None):
    """输入前模式检查：输入框挂着技能标签就先移除，移除后回读确认回到普通态。

    ``driver`` 只需提供两个探测回调（适配器各自实现，本函数统一判定）：
        mode()            -> dict(plain=bool, label=str)  当前位置是否是技能态
        clear_mode(label) -> 移除该标签/切回普通对话（移除不掉就抛异常）

    返回 ``dict(ok, code, mode, label, changed, missing)``；移不掉时
    ``code='skill_chip_present'``，调用方必须中止、一个字都不许输入、不许发送。
    """
    probe = getattr(driver, 'mode', None)
    if not callable(probe):
        return dict(ok=True, code='', mode='plain', label='', changed=False, missing='')
    try:
        state = probe() or {}
    except Exception as exc:                     # 探测失败不阻断正常输入
        out = dict(ok=True, code='', mode='unknown', label='', changed=False,
                   missing='', detail='%s: %s' % (type(exc).__name__, exc))
        _emit(log, 'mode_guard', out)
        return out
    label = str(state.get('label') or '')
    if state.get('plain', True) or not label:
        out = dict(ok=True, code='', mode='plain', label='', changed=False, missing='')
        _emit(log, 'mode_guard', out)            # 普通态也落一条，便于确认检查真的跑过
        return out
    clear = getattr(driver, 'clear_mode', None)
    detail = ''
    changed = False
    if callable(clear):
        try:
            clear(label)
            changed = True
        except Exception as exc:
            detail = '%s: %s' % (type(exc).__name__, exc)
    try:
        after = probe() or {}
    except Exception as exc:
        after = {}
        detail = detail or ('%s: %s' % (type(exc).__name__, exc))
    still = str(after.get('label') or '')
    if still and not after.get('plain', True):
        missing = ('输入框仍挂着技能标签「%s」（本轮会被目标软件当成技能调用而不是对话，'
                   '20261110u 现场就是这样被渲染成“生成图片：…”的）。'
                   '请到目标软件输入框点掉标签后的 ×，切回普通对话后重新派单。'
                   '这不是 OCR 问题，也不是模型问题。' % still)
        out = dict(ok=False, code='skill_chip_present', mode='skill', label=still,
                   changed=changed, missing=missing)
        if detail:
            out['detail'] = detail
        _emit(log, 'mode_guard', out)
        return out
    out = dict(ok=True, code='', mode='plain', label=label, changed=changed, missing='')
    _emit(log, 'mode_guard', out)
    return out


def sent_mode_check(page_text, log=None):
    """发送后模式校验：出现“正在生成图片/幻灯片”这类标志就判 wrong_skill，立即中止。

    这类回合根本没有文字增量（旧实现只看字数 → 误判没发出去 → 重试三次），
    所以必须在比字数之前先看模式。
    """
    marker = wrong_mode_marker(page_text)
    if not marker:
        return dict(ok=True, code='', marker='')
    missing = ('目标软件把这次要求当成「%s」执行了（界面出现“%s”），'
               '不是文字对话回合，本次不能算发送成功，已立即中止、不重试。'
               '请先移除输入框里的技能标签（点标签后的 ×）或切回普通对话，再重新派单。'
               '这不是 OCR 问题，也不是模型问题。' % (marker.rstrip('：'), marker))
    out = dict(ok=False, code='wrong_skill', marker=marker, missing=missing,
               where='send', next_action='移除技能标签后重派')
    _emit(log, 'sent_mode', out)
    return out


class ClipboardGuard:
    """保存/恢复用户剪贴板（格式 + 内容），避免回读和粘贴污染用户数据。"""

    def __init__(self):
        self._saved = []
        self._saved_ok = False

    # ---- 快照 ----
    def save(self):
        try:
            import win32clipboard as clip
            clip.OpenClipboard()
            try:
                fmt = 0
                while True:
                    fmt = clip.EnumClipboardFormats(fmt)
                    if not fmt:
                        break
                    try:
                        value = clip.GetClipboardData(fmt)
                    except Exception:
                        continue
                    if isinstance(value, (bytes, str)) and len(value) <= 10_000_000:
                        self._saved.append((fmt, value))
                self._saved_ok = True
            finally:
                clip.CloseClipboard()
        except Exception:
            self._saved, self._saved_ok = [], False
        return self

    def restore(self):
        if not self._saved_ok:
            return False
        for _ in range(6):
            try:
                import win32clipboard as clip
                clip.OpenClipboard()
                try:
                    clip.EmptyClipboard()
                    for fmt, value in self._saved:
                        try:
                            clip.SetClipboardData(fmt, value)
                        except Exception:
                            continue
                finally:
                    clip.CloseClipboard()
                return True
            except Exception:
                time.sleep(0.1)
        return False

    # ---- 读写文本 ----
    @staticmethod
    def read_text():
        for _ in range(6):
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
                time.sleep(0.1)
        return ''

    @staticmethod
    def write_text(text):
        for _ in range(6):
            try:
                import win32clipboard as clip
                import win32con
                clip.OpenClipboard()
                try:
                    clip.EmptyClipboard()
                    clip.SetClipboardData(win32con.CF_UNICODETEXT, str(text))
                finally:
                    clip.CloseClipboard()
                return True
            except Exception:
                time.sleep(0.1)
        return False

    def __enter__(self):
        return self.save()

    def __exit__(self, *exc):
        self.restore()
        return False


def input_verified(expected, driver, attempts=DEFAULT_ATTEMPTS, clipboard=None, log=None):
    """清空 → 粘贴 → 回读 → 重试 → 恢复剪贴板，返回结构化结果。

    ``driver`` 需要提供 clear() / write(text) / read() 三个回调；
    ``clipboard`` 默认使用 ClipboardGuard（测试可注入 mock）；
    ``log(tag, payload)`` 可选，用于把每次尝试落进适配器日志。

    失败时 ``code`` 为 ``input_verify_failed``；若最后一次失败是剪贴板被
    第三方改动，则为 ``clipboard_conflict``。任何情况下都恢复用户原剪贴板。
    """
    text = str(expected or '')
    limit = max(MIN_ATTEMPTS, min(MAX_ATTEMPTS, int(attempts or DEFAULT_ATTEMPTS)))
    guard = clipboard if clipboard is not None else ClipboardGuard()
    records = []
    conflicts = 0
    last_got = ''
    #: 是否真的读回过文本。只有“读不回任何文本”（自绘 canvas 等无 UIA 回读能力）
    #: 才允许兜底信任粘贴；读回了却和期望不一致，是真实失败，不能报成功。
    readback_seen = False
    guard.save()
    try:
        if not normalize(text):
            return dict(ok=False, code='input_verify_failed', attempts=0, records=[],
                        conflicts=0, missing='本步要发送的文本为空，未执行粘贴')
        # 输入前先确认目标软件在普通对话态：挂着技能标签时注入得再准也会被当成技能调用，
        # 移不掉就一个键都不发（skill_chip_present），不再走“注入成功但回合不对”的老路。
        mode = ensure_plain_mode(driver, log=log)
        if not mode['ok']:
            return dict(ok=False, code='skill_chip_present', attempts=0, records=[],
                        conflicts=0, missing=mode['missing'], text='', mode=mode)
        for attempt in range(1, limit + 1):
            record = dict(attempt=attempt, expected_len=len(text))
            _gate()
            driver.clear()
            _gate()
            residue = driver.read()
            last_got = residue
            record['got_len'] = _clip_length(residue)
            if normalize(residue):
                readback_seen = True
                record.update(cleared=False, detail='清空后输入框仍有残留文本')
                records.append(record)
                _emit(log, 'input_guard', record)
                continue
            record['cleared'] = True
            _gate()
            guard.write_text(text)
            driver.write(text)
            clipboard_now = guard.read_text()
            if normalize(clipboard_now) != normalize(text):
                conflicts += 1
                record.update(conflict=True, clipboard_len=_clip_length(clipboard_now),
                              detail='剪贴板被第三方改动（期望 %d 字，实际 %d 字），已重写重试'
                                     % (len(text), _clip_length(clipboard_now)))
                records.append(record)
                _emit(log, 'input_guard_conflict', record)
                continue
            _gate()
            got = driver.read()
            last_got = got
            if normalize(got):
                readback_seen = True
            record['got_len'] = _clip_length(got)
            if normalize(got) == normalize(text):
                record['matched'] = True
                records.append(record)
                _emit(log, 'input_guard', record)
                return dict(ok=True, code='', attempts=attempt, records=records,
                            conflicts=conflicts, text=got, mode=mode)
            head, tail = _difference(got, text)
            record.update(head_diff=head, tail_diff=tail)
            records.append(record)
            _emit(log, 'input_guard', record)
        last = records[-1] if records else {}
        # === 兜底信任 ===：只在“读不回任何文本”时成立。
        # 目标软件不支持 UIA 回读（Chromium canvas 自绘等）时回读永远是空串，
        # 不能因此判失败，就相信 Ctrl+V 已经成功——这个 fallback 让 WorkBuddy/豆包/
        # Notion 这类自绘软件不会死在“回读校验”上。
        # 但读回了文本却和期望不一致，是真实失败（截断/残留/粘错位置），
        # 必须报 input_verify_failed，不能拿“剪贴板没被改动”当成粘贴成功。
        if conflicts == 0 and records and not readback_seen:
            _emit(log, 'input_guard_trust', dict(
                conflicts=conflicts, attempts=len(records),
                reason='readback_unavailable_trust_paste'))
            return dict(ok=True, code='', attempts=len(records), records=records,
                        conflicts=conflicts, text='', mode=mode,
                        trust_paste=True, reason='readback_unavailable_trust_paste')
        if last.get('conflict'):
            code = 'clipboard_conflict'
            missing = ('剪贴板在粘贴后 %d 次被第三方改动（%s）；用户原剪贴板已恢复，'
                       '但本步没有发送成功' % (conflicts, last.get('detail') or ''))
        else:
            code = 'input_verify_failed'
            missing = ('输入框回读 %d 次都与本步文本不一致（期望 %d 字）；%s；%s'
                       % (len(records), len(text),
                          last.get('head_diff') or '未读回任何文本',
                          last.get('tail_diff') or '尾部无法比对'))
        return dict(ok=False, code=code, attempts=len(records), records=records,
                    conflicts=conflicts, missing=missing, text='',
                    last_text=last_got)
    finally:
        guard.restore()


#: 写日志失败时的模块级记录：方便诊断导出与单测直接读取，不再静默吞掉。
EMIT_ERRORS = []


def _record_emit_error(log, tag, errors):
    """记录“写日志本身失败”，并尽量再写一条 log_emit_error。"""
    row = dict(tag=tag, errors=[str(item)[:200] for item in errors[:2]],
               at=time.strftime('%Y-%m-%d %H:%M:%S'))
    EMIT_ERRORS.append(row)
    if len(EMIT_ERRORS) > 50:
        del EMIT_ERRORS[:len(EMIT_ERRORS) - 50]
    if callable(log):
        for call in (lambda: log('log_emit_error', row),
                     lambda: log('log_emit_error', **row)):
            try:
                call()
                return
            except Exception:
                continue
    try:
        sys.stderr.write('[paste_verify] 写日志失败 tag=%s：%s\n'
                         % (tag, row['errors'][0] if row['errors'] else ''))
    except Exception:
        pass


def _emit(log, tag, payload):
    """兼容两种日志回调形态：``log(tag, payload)`` 与 ``log(tag, **payload)``。

    20261110v 真机现场：豆包侧时间线回调是 ``step(name, **fields)``，旧实现固定用
    位置参调用 → ``TypeError`` 被 ``except: pass`` 吞掉，mode_guard 一条都没落盘。
    现在两种形态都试；都失败时计数并记一条 log_emit_error，绝不再静默。
    """
    if not callable(log):
        return
    data = dict(payload or {})
    errors = []
    for call in (lambda: log(tag, data), lambda: log(tag, **data)):
        try:
            call()
            return
        except TypeError as exc:          # 签名不匹配：换另一种形态再试
            errors.append('%s: %s' % (type(exc).__name__, exc))
        except Exception as exc:          # 其他异常：换形态也无济于事，直接记
            errors.append('%s: %s' % (type(exc).__name__, exc))
            break
    _record_emit_error(log, tag, errors)
