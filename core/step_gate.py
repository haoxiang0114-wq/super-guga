"""每步回验闸：工具返回后先校验，通过才允许进入下一步。

校验三类事实：产物存在且非空、界面状态符合（窗口/控件/焦点已证实）、
回执字段完整（不是“已请求”而是“已完成”）。不通过时把失败原因整理成
下一步的输入（next_input），由调用方注入对话，禁止直接进入下一工具。
"""
from __future__ import annotations

from pathlib import Path

# 必须真的落地产物的工具
ARTIFACT_TOOLS = {'ppt', 'video', 'media_workflow', 'pipeline', 'joycode'}
# 界面动作类工具：必须带回“已证实”的界面证据
UI_TOOLS = {'computer', 'applications', 'wechat', 'browser', 'ecosystem'}
# 会发键鼠事件的 computer 动作：执行前先查模态弹窗
MODAL_INPUT_ACTIONS = {'focus', 'click', 'double_click', 'move', 'drag', 'scroll',
                       'hotkey', 'type', 'fill', 'editor_select_tail'}
MODAL_DIALOG_CLASS = '#32770'
MODAL_REASON = ('目标窗口正被模态弹窗「%s」挡住，这一步没有发送任何输入；'
                '先 computer(action=dialog) 读取弹窗，再用 computer(action=uia, uia_operation=click, '
                'uia_name=...) 走 WM_COMMAND 点掉，复验主窗口恢复后再 observe 继续。'
                '不要把“打字没反应”当普通失败重试。')
# 产物路径候选字段
PATH_FIELDS = ('output', 'path', 'source_path', 'artifact_path', 'entry')


def _modal_state(args):
    """读目标窗口当前的模态事实；没有弹窗时返回 None。

    pid 必须取 GetWindowThreadProcessId 的 [1]（[0] 是线程 id）；owner 走 GW_OWNER。
    """
    try:
        import win32gui
        import win32process
        hwnd = int(args.get('hwnd') or 0)
        if not hwnd:
            hwnd = int(win32gui.GetForegroundWindow() or 0)
        if not hwnd or not win32gui.IsWindow(hwnd):
            return None
        if win32gui.GetClassName(hwnd) == MODAL_DIALOG_CLASS:
            return None                       # 正在操作弹窗本身，不算被挡
        # 实测签名：模态弹窗打开时主窗口 IsWindowEnabled=0；仍可用就只可能是非模态浮层，
        # 不能当 modal_blocked 拦，否则会让正常输入反复报“弹窗挡路”。
        if win32gui.IsWindowEnabled(hwnd):
            return None
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        found = []

        def visit(window, _):
            try:
                if (win32gui.IsWindowVisible(window)
                        and win32gui.GetClassName(window) == MODAL_DIALOG_CLASS
                        and win32process.GetWindowThreadProcessId(window)[1] == pid
                        and int(win32gui.GetWindow(window, 4) or 0) == hwnd):
                    found.append(window)
            except Exception:
                pass
            return True

        win32gui.EnumWindows(visit, None)
        if not found:
            return None
        return dict(main=hwnd, dialog=found[0],
                    title=str(win32gui.GetWindowText(found[0]) or '')[:80])
    except Exception:
        return None


def precheck(name, args):
    """动作前预检：computer 的按键/点击类动作若撞上模态弹窗，直接判这一步失败。"""
    name = str(name or '')
    args = args if isinstance(args, dict) else {}
    if name != 'computer' or str(args.get('action') or '') not in MODAL_INPUT_ACTIONS:
        return None
    state = _modal_state(args)
    if not state:
        return None
    reason = MODAL_REASON % (state['title'] or state['dialog'])
    return dict(passed=False, tool=name, action=str(args.get('action') or ''),
                check='modal_blocked',
                checks=[dict(ok=False, check='modal_blocked', dialog=state['dialog'],
                             title=state['title'], detail='模态弹窗未清')],
                failures=[dict(check='modal_blocked', reason=reason)],
                warnings=[], next_input='【模态预检拦截】' + reason, evidence_count=0)


def _paths(value, args):
    found = []
    nested = []
    for source in (value, args):
        if not isinstance(source, dict):
            continue
        for field in PATH_FIELDS:
            candidate = source.get(field)
            if isinstance(candidate, str) and candidate.strip():
                found.append(candidate.strip())
        if isinstance(source.get('artifact'), dict):
            nested.append(source['artifact'])
        result = source.get('result')
        if isinstance(result, dict):
            nested.append(result)
    for source in nested:
        for field in PATH_FIELDS:
            candidate = source.get(field)
            if isinstance(candidate, str) and candidate.strip():
                found.append(candidate.strip())
        for item in source.get('artifact_paths') or []:
            if isinstance(item, str) and item.strip():
                found.append(item.strip())
        if isinstance(source.get('artifact'), dict):
            inner = source['artifact']
            for field in PATH_FIELDS:
                candidate = inner.get(field)
                if isinstance(candidate, str) and candidate.strip():
                    found.append(candidate.strip())
    seen, unique = set(), []
    for item in found:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def _artifact_check(path):
    target = Path(path)
    if not target.exists():
        return dict(ok=False, check='artifact_exists', path=str(target), detail='产物路径不存在')
    if target.is_dir():
        children = [p for p in target.iterdir() if p.is_file()]
        return dict(ok=bool(children), check='artifact_nonempty', path=str(target),
                    size=sum(p.stat().st_size for p in children), files=len(children),
                    detail='目录产物' if children else '产物目录为空')
    size = target.stat().st_size
    return dict(ok=size > 0, check='artifact_nonempty', path=str(target), size=size,
                detail='空文件' if size == 0 else '文件已落地')


def _receipt_fields(value):
    receipt = value.get('receipt') if isinstance(value.get('receipt'), dict) else None
    source = receipt or (value.get('result') if isinstance(value.get('result'), dict) else value)
    status = str(source.get('status') or '')
    artifacts = source.get('artifact_paths') or []
    missing = []
    if not status:
        missing.append('status')
    if not isinstance(artifacts, list) or not artifacts:
        missing.append('artifact_paths')
    if not str(source.get('self_check') or '').strip():
        missing.append('self_check')
    return dict(ok=not missing, check='receipt_fields', status=status,
                artifacts=len(artifacts) if isinstance(artifacts, list) else 0,
                missing=missing, detail='回执字段完整' if not missing else '回执缺字段：' + '、'.join(missing))


def _ui_check(name, action, value):
    result = value.get('result') if isinstance(value.get('result'), dict) else {}
    payload = dict(value, **result)
    checks = []
    if name == 'applications' and action == 'open':
        ok = bool(payload.get('verified_running') or payload.get('selection_required')
                  or payload.get('reused_existing'))
        checks.append(dict(ok=ok, check='window_state',
                           detail='窗口已出现/已复用' if ok else '启动请求已发出但未观察到窗口'))
    elif name == 'computer':
        verification = payload.get('verification') if isinstance(payload.get('verification'), dict) else {}
        if action in ('click', 'double_click', 'type', 'fill', 'hotkey', 'scroll', 'drag', 'uia'):
            verified = bool(payload.get('verified') or verification.get('verified'))
            checks.append(dict(ok=verified, check='input_verified',
                               detail='已回读动作结果' if verified else '动作已发送但没有回读证据'))
            if verification:
                checks.append(dict(ok=verification.get('verified') is not False,
                                   check='verification_detail', detail=str(verification)[:120]))
        elif action in ('observe', 'accessibility', 'uia_read', 'verify', 'locate', 'dialog'):
            ok = bool(payload.get('observation_id') or payload.get('hierarchy') or payload.get('controls')
                      or payload.get('ratio') or payload.get('title') or payload.get('texts'))
            checks.append(dict(ok=ok, check='observation_payload', detail='已拿到界面事实' if ok else '观察结果为空'))
    elif name == 'wechat':
        if action == 'send':
            ok = bool(payload.get('verified_message_visible') or payload.get('verified'))
            checks.append(dict(ok=ok, check='message_visible',
                               detail='已回读消息' if ok else '发送结果未回读，不能当成已送达'))
    elif name == 'browser':
        if action in ('fill_form', 'click', 'press', 'upload', 'download'):
            ok = bool(payload.get('verified') or payload.get('snapshot_id'))
            checks.append(dict(ok=ok, check='page_verified',
                               detail='已拿到新快照/已验证' if ok else '页面动作后缺少新快照'))
        elif action == 'snapshot':
            ok = bool(payload.get('snapshot_id') or payload.get('controls'))
            checks.append(dict(ok=ok, check='snapshot_payload',
                               detail='快照可用' if ok else '快照为空'))
    elif name == 'ecosystem':
        ok = not bool(payload.get('isError'))
        checks.append(dict(ok=ok, check='mcp_result',
                           detail='MCP 返回正常' if ok else 'MCP 报告 isError'))
    return checks


def check(name, args, value):
    """单步回验。返回 passed / checks / failures / next_input。"""
    name = str(name or '')
    args = args if isinstance(args, dict) else {}
    value = value if isinstance(value, dict) else {}
    action = str(args.get('action') or '')
    checks, failures, warnings = [], [], []

    ok_flag = value.get('ok')
    # 有的工具（任务受理、演出类）不返回 ok，只返回 accepted/started；没有明确失败就不算失败。
    ok = ok_flag is not False
    checks.append(dict(ok=ok, check='tool_ok',
                       detail=('工具报告成功' if ok_flag is True else
                               '工具报告失败：' + str(value.get('error') or '无错误码')[:160]
                               if ok_flag is False else '工具未返回 ok 字段，按未明确的成功处理')))
    if not ok:
        failures.append(dict(check='tool_ok',
                             reason=str(value.get('error') or '工具没有成功回执')[:200]))

    if name in ARTIFACT_TOOLS:
        for path in _paths(value, args):
            receipt = _artifact_check(path)
            checks.append(receipt)
            if not receipt['ok']:
                failures.append(dict(check=receipt['check'], path=path, reason=receipt['detail']))
        if name == 'pipeline':
            # 跨工具链的结果在 folder/ 与 verify 里，和普通产物的路径字段不同。
            payload = value.get('result') if isinstance(value.get('result'), dict) else {}
            folder = payload.get('folder') or value.get('folder')
            verify = payload.get('verify') if isinstance(payload.get('verify'), dict) else value.get('verify')
            if isinstance(verify, dict):
                entry = verify.get('entry')
                if isinstance(entry, str) and entry.strip():
                    receipt = _artifact_check(entry)
                    checks.append(receipt)
                    if not receipt['ok']:
                        failures.append(dict(check=receipt['check'], path=entry, reason=receipt['detail']))
                checks.append(dict(ok=bool(verify.get('ok')), check='site_render',
                                   detail='浏览器核验通过' if verify.get('ok') else '浏览器核验未通过'))
                if not verify.get('ok'):
                    failures.append(dict(check='site_render',
                                         reason='产物在浏览器里未通过核验（空白或渲染失败）'))
            elif isinstance(folder, str) and folder.strip():
                receipt = _artifact_check(folder)
                checks.append(receipt)
                if not receipt['ok']:
                    failures.append(dict(check=receipt['check'], path=folder, reason=receipt['detail']))
            # WorkBuddy 抓回正文为空时，整条链就没有意义，必须拦在这一步。
            if payload:
                chars = payload.get('answer_chars')
                if isinstance(chars, int):
                    ok_chars = chars > 0
                    checks.append(dict(ok=ok_chars, check='prompt_captured',
                                       detail='抓回正文 %d 字' % chars if ok_chars else '抓回正文为空'))
                    if not ok_chars:
                        failures.append(dict(check='prompt_captured', reason='WorkBuddy 回答正文为空'))
            # 回执缺失不等于产物未验证：产物已独立核验时只记警告，不拦交付。
            if isinstance(payload.get('receipt'), dict):
                receipt = _receipt_fields(payload)
                checks.append(receipt)
                if not receipt['ok']:
                    failures.append(dict(check='receipt_fields', reason=receipt['detail']))
            elif payload:
                warnings.append(dict(check='receipt_present', detail='JoyCode 未写回执，以产物核验为准'))
        if name == 'joycode' and action in ('dispatch', 'receipt'):
            receipt = _receipt_fields(value)
            checks.append(receipt)
            if not receipt['ok']:
                failures.append(dict(check='receipt_fields', reason=receipt['detail']))

    # 弹窗清不掉：这是 manual_required，不是普通输入失败；不许当作“打字没反应”继续重试
    if name == 'computer':
        error = str(value.get('error') or '')
        if (value.get('code') == 'modal_blocked' or value.get('manual_required')
                or error.startswith('manual_required')):
            detail = error[:200] or '模态弹窗未清，交还人工'
            checks.append(dict(ok=False, check='modal_blocked', detail=detail))
            failures.append(dict(check='modal_blocked', reason=detail))

    if name in UI_TOOLS and ok:
        ui_checks = _ui_check(name, action, value)
        checks.extend(ui_checks)
        failures.extend(dict(check=item['check'], reason=item['detail'])
                        for item in ui_checks if not item['ok'])

    passed = not failures
    if passed:
        next_input = ''
    else:
        reasons = '；'.join(str(item.get('reason') or item.get('detail') or '') for item in failures)
        next_input = ('这一步没有通过回验（' + reasons + '）。先核对现状并修正这一环，'
                      '拿到证据后才能进入下一步；不能把未验证的中间结果当作已完成往下继续。')
    return dict(passed=passed, tool=name, action=action, checks=checks,
                failures=failures, warnings=warnings, next_input=next_input,
                evidence_count=sum(1 for item in checks if item.get('ok')))


def gate_sequence(requirement, verdicts):
    """复合任务收尾前复核：任何一步没过就不允许交付。"""
    bad = [item for item in verdicts if not item.get('passed')]
    return dict(delivered=not bad, blocked_steps=[item.get('tool') for item in bad],
                reason='' if not bad else '有 %d 步未通过回验，不能交付' % len(bad),
                next_input='' if not bad else bad[-1].get('next_input', ''))


class StepGate:
    """有状态的闸门：记录本任务的每一步结论，供收尾复核。"""

    def __init__(self):
        self.verdicts = []

    def check(self, name, args, value):
        verdict = check(name, args, value)
        self.verdicts.append(verdict)
        return verdict

    def precheck(self, name, args):
        """动作前预检（目前覆盖 computer 的模态弹窗）；命中返回失败结论，不写入历史。"""
        return precheck(name, args)

    def record(self, verdict):
        """把外部（预检）结论并入本任务闸门历史。"""
        self.verdicts.append(verdict)
        return verdict

    def sequence(self, requirement=''):
        return gate_sequence(requirement, self.verdicts)

    def reset(self):
        self.verdicts = []
