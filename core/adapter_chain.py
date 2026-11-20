# -*- coding: utf-8 -*-
"""复合任务 → 适配器串联：A 工具产出 → 咕嘎校验 → 作为输入交给 B 工具 → 再校验。

为什么要有这一层：
  「用豆包生成提示词，再用它去 WorkBuddy 做 PPT」是两步串联，必须由两个软件各自
  真机执行；交给通用自主执行路径只会去抓前台窗口（常常抓到咕嘎自己），最后放弃。
  所以这里把复合句拆成按序的适配器步骤，每步过交付闸，任一步失败就停在该步。

约束：
  · 只用已注册适配器的软件（core.adapters.dispatch.adapter_for），没有通道就如实说；
  · 每步必须有真实回执（source/provenance 或 evidence），本机模型代写一律不算；
  · 上一步正文原样传给下一步，不经过任何模型改写；
  · 全过程写 runtime/adapter_chain.log，记录为什么停、停在哪一步。
"""
import os
import re
import threading
import time
from pathlib import Path

from core.handoff_prompt import HandoffError, compose

try:                       # OCR 不可用是可降级项，不能再被笼统归类为 adapter_exception
    from core.ocr_engine import OcrUnavailableError
except Exception:          # pragma: no cover
    OcrUnavailableError = ()

MAX_STEPS = 3
#: 外层看门狗：适配器 ask() 是同步调用，内部任何一处卡死都不能让界面永远停在
#: “等待具体操作回执”。上限 = 生成等待上限 + 注入/复制/滚动等固定开销余量。
ASK_WATCHDOG_SLACK = 240.0
ASK_WATCHDOG_MAX = 900.0
_STEP_VERBS = ('帮我用', '帮忙用', '给我用', '使用', '通过', '交给', '派给', '让', '叫', '请', '用')
_RELAY_HEAD = re.compile(r'^(?:用|拿|把|将)(?:上一步|上面|刚才|生成的|这份|这段)[^，,。；;]{0,14}?'
                         r'(?:去|来|给|交给|继续)?')
# 非末步的分句里，“，用生成的…”/“，然后让…”属于下一步的引导语，不能算本步要求
_RELAY_TAIL = re.compile(r'[，,;；]\s*(?=(?:用|拿|把|将|再|然后|接着|据此|以(?:上|这)))')


def _apps():
    """已注册适配器的软件：(app_id, name, aliases)。"""
    from core.adapters import app_registry
    rows = []
    for app_id, decl in (app_registry.declarations() or {}).items():
        if not str(decl.get('adapter') or '').strip():
            continue
        aliases = [str(item) for item in (decl.get('aliases') or ()) if str(item).strip()]
        name = str(decl.get('name') or app_id)
        if name not in aliases:
            aliases.append(name)
        rows.append((app_id, name, aliases))
    return rows


def _flat(text):
    """去掉空白并记住原始下标：'work Buddy' 也要能按 'workbuddy' 命中。"""
    chars, index = [], []
    for pos, char in enumerate(str(text or '')):
        if char.isspace():
            continue
        chars.append(char.lower())
        index.append(pos)
    return ''.join(chars), index


def _first_pos(flat, alias):
    needle = str(alias).replace(' ', '').lower()
    if not needle:
        return -1
    return flat.find(needle)


def _clean(segment, name, aliases, relay):
    """把“用豆包生成…”整成一句能直接交给该软件的话。"""
    value = str(segment or '').strip().strip('，,。;；:：')
    if relay:
        value = _RELAY_HEAD.sub('', value)
    for alias in sorted(list(aliases) + [name], key=len, reverse=True):
        value = re.sub(re.escape(alias), '', value, flags=re.I)
    value = value.strip('，,。;；:： ')
    for verb in _STEP_VERBS:
        if value.startswith(verb):
            value = value[len(verb):].lstrip('，,。;；:： ')
            break
    return value.strip() or str(segment or '').strip()


def detect(text):
    """识别“A 产出 → B 消费”的复合任务；不是复合返回 None。

    返回 {raw, steps:[{index, app_id, name, instruction, relay}]}。
    """
    raw = str(text or '').strip()
    if not raw:
        return None
    flat, index = _flat(raw)
    hits = []
    for app_id, name, aliases in _apps():
        best = -1
        for alias in aliases:
            pos = _first_pos(flat, alias)
            if pos >= 0 and (best < 0 or pos < best):
                best = pos
        if best >= 0:
            hits.append(dict(app_id=app_id, name=name, aliases=aliases, flat_pos=best))
    hits.sort(key=lambda item: item['flat_pos'])
    ordered = []
    for hit in hits:
        if ordered and ordered[-1]['app_id'] == hit['app_id']:
            continue                      # 同一软件重复提及只算一步
        ordered.append(hit)
    if len(ordered) < 2:
        return None
    steps = []
    for i, hit in enumerate(ordered[:MAX_STEPS]):
        start = index[hit['flat_pos']]
        end = len(raw) if i + 1 >= len(ordered[:MAX_STEPS]) else index[ordered[i + 1]['flat_pos']]
        segment = raw[start:end]
        if i < len(ordered[:MAX_STEPS]) - 1:
            segment = _RELAY_TAIL.split(segment)[0]
        steps.append(dict(index=i + 1, app_id=hit['app_id'], name=hit['name'],
                          instruction=_clean(segment, hit['name'], hit['aliases'], i > 0),
                          raw_segment=segment.strip()))
    return dict(raw=raw, steps=steps)


def gate(result):
    """交付闸：有正文 + 有真实回执 + 没用本机模型代写。返回 (ok, code, hint)。"""
    if not isinstance(result, dict):
        return False, 'adapter_bad_result', '适配器返回格式不对'
    if not result.get('ok'):
        code = str(result.get('code') or 'adapter_failed')
        hint = str(result.get('hint') or result.get('message') or '目标软件没有完成这一步')
        # 失败卡片/hint 与聊天回复共用同一套禁用话术闸：不把操作推回用户。
        try:
            from core.reply_guard import sanitize_hint
            hint, _hits = sanitize_hint(hint)
        except Exception:
            pass
        return False, code, hint
    body = str(result.get('text') or '').strip()
    evidence = result.get('evidence') if isinstance(result.get('evidence'), dict) else {}
    source = result.get('source') if isinstance(result.get('source'), dict) else {}
    artifact = str(evidence.get('artifact') or result.get('pptx') or '').strip()
    file_ok = bool(artifact) and artifact.lower().endswith('.pptx') and Path(artifact).is_file()
    # PPT 这类“成品是文件”的步骤：界面切到编辑器后抓不到对话正文，
    # 但真实 .pptx 已落地并有 sha256 回执，这同样是可验收的交付，不算空正文。
    if not body and not file_ok:
        return False, 'empty_text', '目标软件没有返回正文'
    if evidence.get('local_model_used') is True or str(source.get('channel') or '') == 'local_model':
        return False, 'local_model_used', '这份产出是本机模型写的，不是目标软件真机产出'
    receipt = str(source.get('provenance') or source.get('call_id')
                  or evidence.get('receipt_id') or evidence.get('dir') or '').strip()
    if not receipt and not file_ok:
        return False, 'no_receipt', '没有真实回执（来源/证据缺失）'
    return True, '', ''


def _submit_summary(meta):
    """接收回执里的提交证据：回读长度、要求校验结果，供判定“提示词真进了要求框”。"""
    if not isinstance(meta, dict):
        return {}
    keys = ('readback_ok', 'readback_len', 'expected_len', 'sent_wall',
            'requirement_check', 'recovered', 'attempts')
    return {key: meta.get(key) for key in keys if meta.get(key) not in (None, '')}


def _log(event, **fields):
    """唯一日志落点：与适配器的 composer/inject/done/timeout 共用 adapter_chain.log。"""
    try:
        from core import run_log
        run_log.emit(event, **fields)
    except Exception:
        pass


def _save_step(index, app_id, body):
    try:
        from core import guga_paths
        folder = Path(guga_paths.DELIVERY) / 'adapter-chain'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ('%s-step%d-%s.txt' % (time.strftime('%Y%m%d-%H%M%S'), index, app_id))
        path.write_text(body, encoding='utf-8')
        return str(path)
    except Exception:
        return ''


def _focus_app(name):
    """把目标软件窗口恢复到前台：上一步的软件常还压在它上面（甚至被最小化）。

    用 _attach_focus (AttachThreadInput) 绕过 Windows 前台窗口限制，
    避免 SetForegroundWindow 静默失败导致后续 inject 找不到焦点。
    """
    try:
        import win32con
        import win32gui
        from core.window_inventory import list_windows
        from core.computer_control import _attach_focus
    except Exception:
        return False
    rows = [row for row in list_windows(name) if '咕嘎' not in str(row.get('title') or '')]
    if not rows:
        return False
    hwnd = int(rows[0].get('hwnd') or 0)
    if not hwnd:
        return False
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    except Exception:
        pass
    return _attach_focus(hwnd)


_TRANSIENT = ('bridge_exception', 'focus_changed', 'window_changed', 'window_switched')
_PPT_RE = re.compile(r'PPT|幻灯片|演示文稿|slides?', re.I)
_PPT_TEXT_RE = re.compile(r'提示词|文案|大纲|提纲|脚本')


def _wants_pptx(text):
    """这一步要的是“PPT 文件”，不是“PPT 提示词/大纲”这类文字。"""
    value = str(text or '')
    if _PPT_TEXT_RE.search(value):
        return False
    return bool(_PPT_RE.search(value))


def _find_pptx(since):
    """目标软件默认产物目录里，本步之后新出现的 .pptx（只认最新的一个）。"""
    roots = []
    profile = os.environ.get('USERPROFILE', '').strip()
    if profile:
        roots.append(Path(profile) / 'WorkBuddy')
    found = []
    for root in roots:
        try:
            if not root.is_dir():
                continue
            found.extend(path for path in root.rglob('*.pptx')
                         if path.stat().st_mtime >= float(since) - 10)
        except Exception:
            continue
    return str(max(found, key=lambda path: path.stat().st_mtime)) if found else ''


def _prompt_for(step, previous):
    """本步提示词统一由 core.handoff_prompt.compose 组装，适配器不各写一套。"""
    index = int(step.get('index') or 1)
    if previous is None and index > 1:
        # 第 2 步起却拿不到上一步正文，说明交接链断了：不能退化成只发模板。
        raise HandoffError('handoff_missing', '上一步交付的正文',
                           '第 %d 步没有拿到上一步的正文（交接链断了）' % index)
    return compose(step.get('instruction'), previous,
                   wants_file=_wants_pptx(step.get('instruction')),
                   plain_text=str(step.get('app_id') or '').strip().lower() == 'doubao')


def _sentence(value):
    """去掉结尾句号，避免和外层模板的“。”拼成“。。”。"""
    return str(value or '').strip().rstrip('。').strip()


_MISSING_RE = re.compile(r'缺什么[：:]\s*([^。]+)')


def _missing_of(receipt):
    """气泡里的“缺什么”必须是具体缺失项：没有独立字段时从 hint 的“缺什么：…”里取。"""
    value = str(receipt.get('missing') or '').strip()
    if value:
        return value
    match = _MISSING_RE.search(str(receipt.get('hint') or ''))
    return match.group(1).strip() if match else ''


def ask_watchdog_seconds(timeout_seconds):
    """外层看门狗上限：生成等待 + 固定开销余量，封顶 ASK_WATCHDOG_MAX。

    WorkBuddy 生成 PPT 通常需要 3-8 分钟，加上 submit retry 等固定开销。
    默认给 ASK_WATCHDOG_MAX（900s），确保长 PPT 生成能完成。
    豆包 adapter 内部 30s 内返回，外层 watchdog 更大不会造成问题。
    """
    try:
        wait = float(timeout_seconds or (ASK_WATCHDOG_MAX - ASK_WATCHDOG_SLACK))
    except (TypeError, ValueError):
        wait = float(ASK_WATCHDOG_MAX - ASK_WATCHDOG_SLACK)
    return min(ASK_WATCHDOG_MAX, max(60.0, wait) + ASK_WATCHDOG_SLACK)


def _ask_guarded(ask_once, seconds):
    """在独立线程里跑 ask()，超时返回 None（线程是 daemon，不阻塞退出）。"""
    box = {}

    def worker():
        try:
            box["value"] = ask_once()
        except BaseException as exc:          # 线程内异常原样带回
            box["error"] = exc

    thread = threading.Thread(target=worker, name="guga-adapter-ask", daemon=True)
    thread.start()
    thread.join(max(0.05, float(seconds)))
    if thread.is_alive():
        return None
    if "error" in box:
        raise box["error"]
    return box.get("value")


def run(chain, on_step=None, timeout_seconds=None):
    """按序执行适配器步骤。返回最终交付或“第几步、缺什么、下一步”结构。"""
    from core.adapters.dispatch import adapter_for, load_error
    steps = list((chain or {}).get('steps') or [])
    total = len(steps)
    receipts = []
    started = time.perf_counter()
    previous = None
    degraded_notes = []

    def notify(no, name, phase, summary=''):
        if not callable(on_step):
            return
        try:
            on_step(no, total, name, phase, summary)
        except Exception:
            pass

    for step in steps:
        no = int(step.get('index') or 1)
        name = str(step.get('name') or '')
        notify(no, name, 'start', '第 %d/%d 步：%s 开始处理' % (no, total, name))
        app_id, adapter = adapter_for(step.get('app_id') or name)
        if adapter is None:
            detail = load_error(app_id) if app_id else '这个软件没有注册适配器'
            receipt = dict(index=no, app_id=app_id, name=name, status='failed',
                           code='no_adapter', elapsed_ms=0, text_chars=0, artifact='')
            receipts.append(receipt)
            _log('step_failed', step=no, total=total, app=name, code='no_adapter', detail=detail)
            notify(no, name, 'failed', detail)
            return dict(ok=False, code='step_failed', step=no, total=total, app=name,
                        steps=receipts,
                        error=('第 %d/%d 步（%s）没有可用的本机通道：%s。'
                               '缺什么：本机没装或没有适配器。下一步：装好并登录%s后重试。'
                               % (no, total, name, detail, name)))
        try:
            prompt = _prompt_for(step, previous)
        except HandoffError as exc:
            # 上游正文缺失/过短/没进最终 prompt：中止本步并报独立失败码，
            # 绝不静默退化成「只发模板指令」。
            reason = {'handoff_missing': '上一步没有交出正文',
                      'handoff_too_short': '上一步交付的正文过短',
                      'handoff_not_embedded': '本步提示词组装回读失败'}.get(
                          exc.code, '上游交接内容不可用')
            hint = exc.detail or reason
            receipt = dict(index=no, app_id=app_id, name=name, status='failed',
                           code=exc.code, hint=hint, elapsed_ms=0, text_chars=0,
                           artifact='', missing=exc.missing, handoff_code=exc.code)
            receipts.append(receipt)
            _log('step_failed', step=no, total=total, app=name, code=exc.code,
                 detail=hint[:200], missing=exc.missing)
            notify(no, name, 'failed', hint)
            if exc.code == 'handoff_not_embedded':
                next_action = ('这是提示词组装异常（上游正文没进最终 prompt），已拒绝发送；'
                               '请导出诊断包上报，不要把模板当成正文重派')
            else:
                next_action = ('重新执行第 %d 步并确认它交回正文后，再派第 %d 步（%s）；'
                               '我不会退化成只发模板指令'
                               % (max(1, no - 1), no, name))
            return dict(ok=False, code=exc.code, step=no, total=total, app=name,
                        failure_code=exc.code, stage='组装本步提示词',
                        steps=receipts,
                        error=('第 %d/%d 步（%s）失败：%s。缺什么：%s。下一步：%s。'
                               % (no, total, name, _sentence(reason),
                                  _sentence(exc.missing), _sentence(next_action))),
                        missing=exc.missing, where='', next_action=next_action)
        _log('step_prompt', step=no, total=total, app=name,
             handoff_chars=(len(str((previous or {}).get('text') or '')) if previous else 0),
             prompt_chars=len(prompt))

        def ask_once():
            _focus_app(name)
            try:
                return adapter.ask(prompt, timeout_seconds=timeout_seconds,
                                   retry=1, prefer_mcp=True)
            except TypeError:
                return adapter.ask(prompt)
            except Exception as exc:
                if OcrUnavailableError and isinstance(exc, OcrUnavailableError):
                    return dict(ok=False, code='ocr_unavailable',
                                hint=exc.summary(),
                                missing=getattr(exc, 'missing', ''),
                                where=getattr(exc, 'where', ''),
                                next_action=getattr(exc, 'next_action', ''),
                                restart_required=bool(getattr(exc, 'restart_required', True)))
                return dict(ok=False, code='adapter_exception',
                            hint='%s: %s' % (type(exc).__name__, str(exc)[:160]))

        step_started = time.perf_counter()
        step_wall = time.time()
        watchdog = ask_watchdog_seconds(timeout_seconds)

        def timeout_stop():
            """适配器内部任何一处卡死都在这里收口：不让界面永远“等待具体操作回执”。"""
            receipt = dict(index=no, app_id=app_id, name=name, status='failed',
                           code='step_timeout', elapsed_ms=int(watchdog * 1000),
                           text_chars=0, artifact='',
                           hint='执行器超过 %.0f 秒没有回执' % watchdog,
                           missing='这一步的可执行回执（执行超时）',
                           where='', next_action='检查%s窗口是否卡死/白屏后重派第 %d 步'
                                                  % (name, no))
            receipts.append(receipt)
            _log('step_failed', step=no, total=total, app=name, code='step_timeout',
                 detail='执行超过 %.0f 秒无回执' % watchdog, elapsed_ms=receipt['elapsed_ms'])
            notify(no, name, 'failed', receipt['hint'])
            return dict(ok=False, code='step_failed', failure_code='step_timeout',
                        step=no, total=total, app=name, steps=receipts,
                        error=('第 %d/%d 步（%s）失败：执行器超过 %.0f 秒没有回执（step_timeout）。'
                               '缺什么：这一步的可执行回执。下一步：检查%s窗口是否卡死/白屏，'
                               '切到前台后重派第 %d 步；这一步没有完成，未执行/未确认，'
                               '我不会替它编造结果。'
                               % (no, total, name, watchdog, name, no)),
                        missing=receipt['missing'], where='',
                        next_action=receipt['next_action'])

        result = _ask_guarded(ask_once, watchdog)
        if result is None:
            return timeout_stop()
        ok, code, hint = gate(result)
        if not ok and code in _TRANSIENT:
            # 前台被上一步的窗口抢走/被最小化：恢复目标窗口后原样重试一次，不算换目标。
            _log('step_retry', step=no, total=total, app=name, code=code, detail=hint[:120])
            result = _ask_guarded(ask_once, watchdog)
            if result is None:
                return timeout_stop()
            ok, code, hint = gate(result)
        elapsed_ms = int((time.perf_counter() - step_started) * 1000)
        if not ok:
            next_action = str(result.get('next_action') or '').strip()
            if next_action:
                try:
                    from core.reply_guard import sanitize_hint
                    next_action, _hits = sanitize_hint(next_action)
                except Exception:
                    pass
            receipt = dict(index=no, app_id=app_id, name=name, status='failed', code=code,
                           hint=hint, elapsed_ms=elapsed_ms, text_chars=0, artifact='',
                           missing=result.get('missing') or '',
                           where=result.get('where') or '',
                           next_action=next_action,
                           restart_required=bool(result.get('restart_required')))
            # 适配器没给独立 missing 字段时，从 hint 的“缺什么：…”里取具体缺失项，
            # 不能把失败码当成缺失项（用户看到的应是“缺什么”而不是“capture_empty”）。
            detail_missing = _missing_of(receipt)
            if detail_missing:
                receipt['missing'] = detail_missing
            receipts.append(receipt)
            _log('step_failed', step=no, total=total, app=name, code=code,
                 detail=hint[:200], elapsed_ms=elapsed_ms,
                 missing=receipt['missing'], where=receipt['where'])
            notify(no, name, 'failed', hint)
            missing = (receipt['missing'] or 'Windows OCR 语言包 / WinRT 组件'
                       if code == 'ocr_unavailable'
                       else (receipt['missing'] or code))
            where = receipt['where'] or (
                '设置 → 应用 → 可选功能 → 添加功能 → 搜索“中文(简体) 光学字符识别”'
                if code == 'ocr_unavailable' else '')
            restart = ('补完重启咕嘎（不用重启电脑）' if code == 'ocr_unavailable'
                       else ('补完重启咕嘎' if receipt['restart_required'] else '不用重启'))
            next_action = receipt['next_action'] or (
                '装好语言包并重启咕嘎后，回车重派同一句；在此之前豆包会降级走 UIA + 剪贴板，'
                '截图问答里的文字识别不可用' if code == 'ocr_unavailable'
                else '处理完%s这一项后再重派同一句，我不会绕开它自己写' % name)
            reason = ('Windows OCR 不可用（%s）' % hint if code == 'ocr_unavailable' else hint)
            if code == 'ocr_unavailable':
                # 对外失败码就是 ocr_unavailable，不再套一层无信息的 step_failed。
                error_text = ('第 %d/%d 步（%s）失败：Windows OCR 不可用。缺什么：%s。去哪装：%s。'
                              '%s。下一步：%s。'
                              % (no, total, name,
                                 _sentence(missing) or 'Windows OCR 语言包 / WinRT 组件',
                                 _sentence(where) or '设置 → 应用 → 可选功能 → 添加“中文(简体) 光学字符识别”',
                                 _sentence(restart), _sentence(next_action)))
            else:
                where_text = ('；去哪装：%s' % _sentence(where)) if _sentence(where) else ''
                error_text = ('第 %d/%d 步（%s）失败：%s（失败码：%s）。缺什么：%s%s。%s。下一步：%s。'
                              % (no, total, name, _sentence(reason), code,
                                 _sentence(missing), where_text, _sentence(restart),
                                 _sentence(next_action))).strip()
            return dict(ok=False, code=code, step=no, total=total, app=name,
                        failure_code=code, stage='提交指令' if no == 1 else '等待产物',
                        steps=receipts, error=error_text, missing=receipt['missing'],
                        where=receipt['where'], next_action=receipt['next_action'])
        body = str(result.get('text') or '').strip()
        artifact = _save_step(no, app_id or name, body)
        source = result.get('source') if isinstance(result.get('source'), dict) else {}
        evidence_dir = str((result.get('evidence') or {}).get('dir') or '') \
            if isinstance(result.get('evidence'), dict) else ''
        deck = ''
        evidence_artifact = ''
        if isinstance(result.get('evidence'), dict):
            evidence_artifact = str(result['evidence'].get('artifact') or '').strip()
        if evidence_artifact.lower().endswith('.pptx') and Path(evidence_artifact).is_file():
            deck = evidence_artifact          # 适配器已把文件当交付物并给了 sha256 回执
        else:
            deck = _find_pptx(step_wall)
        if not deck and evidence_dir:
            try:
                decks = sorted(Path(evidence_dir).rglob('*.pptx'),
                               key=lambda p: p.stat().st_mtime, reverse=True)
                deck = str(decks[0]) if decks else ''
            except Exception:
                deck = ''
        if _wants_pptx(step['instruction']) and not deck:
            # 文件常比界面“生成完成”晚落地（实测 10:49 提交、10:55 才出 .pptx）：
            # 有界再等 90 秒，每 5 秒看一次，等不到才算失败。
            deadline = time.time() + 90
            while not deck and time.time() < deadline:
                time.sleep(5)
                deck = _find_pptx(step_wall)
        if _wants_pptx(step['instruction']) and not deck:
            # 口口声声“生成PPT”却只回一段大纲：按失败处理，不拿文字冒充文件。
            hint = '只给了文字大纲，没有产出 .pptx 文件'
            receipt = dict(index=no, app_id=app_id, name=name, status='failed',
                           code='missing_artifact', hint=hint, elapsed_ms=elapsed_ms,
                           text_chars=len(body), artifact=artifact, deck='')
            receipts.append(receipt)
            _log('step_failed', step=no, total=total, app=name, code='missing_artifact',
                 detail=hint, elapsed_ms=elapsed_ms)
            notify(no, name, 'failed', hint)
            return dict(ok=False, code='step_failed', step=no, total=total, app=name,
                        steps=receipts,
                        error=('第 %d/%d 步（%s）失败：%s。缺什么：PPT 文件（.pptx）。'
                               '下一步：在 %s 窗口里明确要求“直接生成可下载的 .pptx 文件”后，'
                               '再重派这一句；我不会拿大纲冒充成品。'
                               % (no, total, name, hint, name)))
        evidence = result.get('evidence') if isinstance(result.get('evidence'), dict) else {}
        ocr_down = bool(result.get('ocr_unavailable') or evidence.get('ocr_unavailable'))
        receipt = dict(index=no, app_id=app_id, name=name, status='done', code='',
                       elapsed_ms=elapsed_ms, text_chars=len(body), artifact=artifact,
                       deck=deck,
                       submit=_submit_summary(result.get('submit')
                                              or (result.get('evidence') or {}).get('submit')),
                       confirm_signal=result.get('confirm_signal') or '',
                       done_signal=result.get('done_signal') or '',
                       source=dict(label=source.get('label') or '',
                                   provenance=source.get('provenance') or '',
                                   call_id=source.get('call_id') or '',
                                   channel=source.get('channel') or ''),
                       evidence=dict(dir=str(evidence.get('dir') or '')))
        if ocr_down:
            # OCR 不可用但适配器已用 UIA + 剪贴板读出正文：记为降级并继续，
            # 不把整单当失败（用户要求“不得中断复合任务”）。
            receipt['status'] = 'degraded'
            receipt['code'] = 'ocr_unavailable'
            receipt['ocr_unavailable'] = True
            receipt['capture_mode'] = evidence.get('capture_mode') or 'uia+clipboard'
            degraded_notes.append('第 %d 步 %s：OCR 不可用，已降级走 UIA + 剪贴板（%s）'
                                  % (no, name, receipt['capture_mode']))
            _log('step_degraded', step=no, total=total, app=name, code='ocr_unavailable',
                 capture_mode=receipt['capture_mode'])
        receipts.append(receipt)
        _log('step_done', step=no, total=total, app=name, elapsed_ms=elapsed_ms,
             text_chars=len(body), artifact=artifact,
             confirm_signal=result.get('confirm_signal') or '',
             confirm_ms=result.get('confirm_elapsed_ms'),
             done_signal=result.get('done_signal') or '',
             provenance=str(source.get('provenance') or '')[:80])
        notify(no, name, 'done', '第 %d/%d 步完成，%d 字' % (no, total, len(body)))
        previous = dict(name=name, text=body, artifact=artifact)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    label = ' → '.join('%s(%s)' % (item['name'], item.get('source', {}).get('label') or '真机')
                       for item in receipts)
    deck = next((item.get('deck') for item in reversed(receipts) if item.get('deck')), '')
    _log('chain_done', steps=total, elapsed_ms=elapsed_ms, deck=deck)
    summary = '；'.join('第 %d 步 %s：%d 字 / %d 秒' % (item['index'], item['name'],
                                                      item['text_chars'],
                                                      int(item['elapsed_ms'] / 1000))
                        for item in receipts)
    if deck:
        summary += '；PPT 文件：%s' % deck
    if degraded_notes:
        summary += '；OCR 不可用，已降级：%s' % '；'.join(degraded_notes)
    return dict(ok=True, code='', text=previous['text'], steps=receipts,
                artifact=previous['artifact'], label=label, summary=summary,
                deck=deck, elapsed_ms=elapsed_ms,
                degraded=bool(degraded_notes),
                degraded_reasons=degraded_notes,
                ocr_unavailable=bool(degraded_notes))
