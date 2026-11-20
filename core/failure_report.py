# -*- coding: utf-8 -*-
"""统一失败报告：失败码 / 阶段 / 原始异常 / 下一步。

适配器（JoyCode、WorkBuddy、豆包）失败时必须给出可核对的真实码，而不是
“（错误）”“执行器报告失败”这类无信息占位。本模块只做归类、脱敏和拼装，
不执行任何界面操作，便于适配器与执行器共用同一口径。
"""
import re

STAGES = ('启动', '定位工作区', 'inject', '提交指令', '等待产物', '校验交付')

# 旧码 -> 统一码：适配器内部的分级码仍然保留在 raw_code 里，对外只报统一码。
_ALIASES = {
    'input_not_found': 'panel_not_open',
    'click_miss': 'panel_not_open',
    'occluded': 'panel_not_open',
    'modal_blocked': 'panel_not_open',
    'typed_in_editor': 'typed_in_editor',
    'input_text_mismatch': 'typed_in_editor',
    'paste_failed': 'not_submitted',
    'input_not_cleared': 'not_submitted',
    'stuck_ppt_page': 'not_submitted',
    'empty_answer': 'artifact_incomplete',
    'answer_timeout': 'artifact_incomplete',
    'no_receipt': 'artifact_incomplete',
    'empty_receipt': 'artifact_incomplete',
    'send_not_confirmed': 'not_submitted',
    'skill_chip_present': 'not_submitted',
    'wrong_skill': 'not_submitted',
    'readback_mismatch': 'typed_in_editor',
    'capture_empty': 'artifact_incomplete',
    'generate_not_started': 'not_submitted',
    'blocked_by_dialog': 'panel_not_open',
    'exception': 'adapter_exception',
}

# 统一码 + 仍保留细分语义的适配器码 -> (阶段, 默认下一步)
_GUIDE = {
    'launcher_not_found': ('启动',
        '设置环境变量 GUAGA_JOYCODE_LAUNCHER 指向 JoyCode.exe 的完整路径'
        '（PowerShell：setx GUAGA_JOYCODE_LAUNCHER "D:\\JoyCode\\JoyCode.exe"），'
        '重开咕嘎后再派单。'),
    'workspace_not_found': ('定位工作区',
        '在咕嘎弹出的工程选择框里选中目标软件当前打开的工程目录（JoyCode 任务写到该目录的 '
        '.guga/inbox），选完后点“重新派单”。'),
    'panel_not_open': ('提交指令',
        '在软件窗口里点右侧“对话 / 新任务”面板，再点一下底部输入框让光标进入，然后重新派单。'),
    'typed_in_editor': ('提交指令',
        '把焦点移出代码编辑器，点对话面板底部输入框确认光标在那里，清空残留文字后重派。'),
    'not_submitted': ('提交指令',
        '在输入框里确认文字真的出现，再按一次回车；若界面显示“停止生成 / 正在生成”，'
        '先点“停止生成”等这一轮结束再派。'),
    'skill_chip_present': ('提交指令',
        '目标软件输入框挂着技能标签（带 × 的 chip），这次没有发送。'
        '点掉标签后的 × 或点“对话”切回普通对话，确认标签消失后重新派单。'),
    'wrong_skill': ('提交指令',
        '目标软件把这次要求当成生图/幻灯片技能执行了（界面显示“正在生成图片/幻灯片”），'
        '已立即中止、不重试。先点掉输入框里技能标签后的 × 切回普通对话，再重新派单；'
        '这不是 OCR 问题，也不是模型问题。'),
    'artifact_incomplete': ('等待产物',
        '等页面不再显示“生成中”、产物文件大小连续两次不变后再重试，不要用中途版本。'),
    'answer_in_document_card': ('等待产物',
        '目标软件把正文放进了在线文档卡片，对话里只有摘要，卡片自身的复制入口也没取到正文。'
        '先要求它“直接在对话里用纯文本输出全文”，或手动点开卡片复制正文后再派单；'
        '这不是 OCR 问题，也不是模型问题。'),
    'artifact_stale': ('校验交付',
        '确认产物修改时间晚于本次任务开始时间；到任务目录里删掉旧文件后重新派单。'),
    'login_required': ('启动',
        '在软件里点“登录”，扫码或输入账号完成登录并保持窗口打开，然后重新派单。'),
    'window_blank': ('启动',
        '按 Ctrl+R 刷新窗口；仍白屏就在任务栏右键关闭后重新打开软件，再派单。'),
    'window_not_found': ('启动',
        '确认目标软件已安装并手动启动一次；窗口出现后保持打开，再让咕嘎派单。'),
    'window_not_ready': ('启动', '等目标软件窗口加载完（能看见对话页）再派单。'),
    'target_window_abnormal': ('启动',
        '目标软件窗口本身不可用：白屏/渲染异常、大部分在屏幕外，或停在非对话页。'
        '这不是 OCR 或适配器异常，不用重装语言包——关掉该软件后重新打开，'
        '确认能看到对话页和输入框再重新派单。'),
    'empty_prompt': ('提交指令', '先写出要交给软件的具体需求，再派单。'),
    'duplicate_task': ('提交指令',
        '等上一单出回执（done / failed / blocked）后再派，不重复写任务文件。'),
    'trigger_failed': ('提交指令',
        '打开目标软件右侧对话面板，手动输入 /guga-run <task_id> 并回车，再让咕嘎读回执。'),
    'foreground_busy': ('提交指令', '停手 3 秒（别再动键鼠）后再派一次。'),
    'busy_generating': ('提交指令',
        '等界面不再显示“停止生成”后再派；不要重复下发。'),
    'previous_task_running': ('等待产物',
        '等上一单结束（或点“停止生成”）后再派，本次没有提交。'),
    'question_unanswered': ('等待产物',
        '在提问卡片上手动选一项，或点“停止生成”后重新派单。'),
    'workbuddy_quota': ('等待产物',
        'WorkBuddy 当前模型免费额度受限：等额度重置后再派，或换一个可用模型/工具。'),
    'page_mismatch': ('等待产物',
        '关掉 WorkBuddy 里另一个会话或切回本单，再重新派单；本次没有交付。'),
    'prompt_truncated': ('提交指令',
        '粘贴后输入框被清空或截断，已中止、没有提交空要求：重新点输入框粘贴一次；'
        '仍不足就缩短内容或分段派单。'),
    'requirement_not_landed': ('等待产物',
        'WorkBuddy 的 PPT 流程只取到了末段指令，提示词正文没进“要求”：本次不计入交付；'
        '重派时把正文并进同一条要求里，不要在末尾单列“本步要求”。'),
    'generation_cancelled': ('等待产物', '在 WorkBuddy 里重新发起这一单，别复用被取消的会话。'),
    'workbuddy_error': ('等待产物',
        '在 WorkBuddy 里重新问一次；若仍返回“未知错误”，换新会话再派。'),
    'cancelled': ('等待产物', '重新派单，本次已被取消。'),
    'bridge_exception': ('提交指令',
        '先手动在目标软件窗口里发一次确认窗口可用，再重新派单。'),
    'window_lost': ('等待产物', '重新打开目标软件窗口并保持可见，再派单。'),
    'blocked_by_window': ('提交指令', '移开遮挡目标软件窗口的其他窗口，再派单。'),
    'timeout': ('等待产物', '确认软件没有卡在“生成中”，再重试或换工具。'),
    'not_installed': ('启动',
        '先安装对应桌面端（豆包：www.doubao.com/download/desktop；WorkBuddy：比赛指定安装包），'
        '装好后重开咕嘎再派单。'),
    'app_not_found': ('启动',
        '确认软件名称是否准确（例如“打开计算器”“打开豆包”）；已安装但没被识别时，'
        '到桌面咕嘎「接入面板 → 新增软件」填写软件名称和可执行文件路径，再重新派单。'
        '软件没安装就先安装。'),
    'access_disabled': ('启动',
        '到桌面咕嘎「软件权限」勾选“允许咕嘎完整访问本机”（或把目标软件加入授权列表），'
        '再重新派单；未开启完整访问时只能打开已授权的软件。'),
    'launch_failed': ('启动',
        '手动双击桌面图标启动一次目标软件，等窗口出现后保持打开，再让咕嘎派单。'),
    'ocr_unavailable': ('等待产物',
        '等目标软件把正文渲染出来；仍读不到就重启软件，或在设置里确认 Windows OCR 可用。'),
    'ocr_empty': ('等待产物',
        'OCR 引擎可用但这次识别到 0 行 / 0 字：确认目标软件窗口已渲染出正文（不是空页/加载中）'
        '并让它保持可见，再重新派单；仍为空请到桌面咕嘎「自检」重跑“系统 OCR”，'
        '并右键「导出诊断」把 zip 发给开发者。'),
    'adapter_exception': ('提交指令',
        '先重启目标软件再派一次；仍报同样的错，把日志里的异常类型和首行发给开发者。'),
    'injection_failed': ('inject',
        '检查目标输入框是否真的拿到焦点：先在软件窗口里点一下底部输入框，'
        '把目标窗口切到前台后再重试注入；仍失败就重启该软件再派单，'
        '并把这条原始异常发给我们。这不是模型或 OCR 问题。'),
    'chain_exception': ('提交指令',
        '先单独跑第一步（用对应软件适配器），确认可用后再重跑整条串联。'),
}

# 同一失败码在不同软件里的阶段可能不同（豆包“找不到窗口”属于启动阶段）。
_APP_GUIDE = {
    ('豆包', 'window_lost'): ('启动', '重新打开豆包窗口并保持可见，再派单。'),
    ('豆包', 'window_blank'): ('启动',
        '在豆包窗口按 Ctrl+R；仍白屏就重启豆包后再派单。'),
    ('豆包', 'ocr_empty'): ('等待产物',
        '豆包窗口没有可识别的文字（OCR 结果 0 行 0 字，不是适配器异常）：'
        '在豆包窗口按 Ctrl+R 刷出对话页并保持可见后重派；仍为空请导出诊断。'),
    ('豆包', 'target_window_abnormal'): ('启动',
        '豆包窗口不可用（白屏/渲染异常、大部分在屏幕外，或停在非对话页）：'
        '重启豆包，确认能正常显示对话页与输入框后再派单；这不是 OCR 缺失。'),
}

_TYPE_HEAD = re.compile(
    r'^([A-Za-z_][\w.]*(?:Error|Exception|Failure|Timeout))[：:]\s*(.*)$')
_SECRET = re.compile(
    r'(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|'
    r'password|secret)\s*[:=]\s*)(["\']?)([^"\'\s,;]+)(\2)')
_AUTH = re.compile(r'(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?([A-Za-z0-9._\-]+)')
_BEARER = re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._\-]+')
_KEYS = re.compile(r'\b(?:oc_sk_[A-Za-z0-9_-]{16,}|sk-[A-Za-z0-9_-]{20,})\b')


def _one_line(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def _redact(value):
    text = _one_line(value)
    # 连 "Authorization:" 这个头名一起抹掉：日志/文案里不出现该头的任何痕迹。
    text = _AUTH.sub('[REDACTED]', text)
    text = _BEARER.sub('Bearer [REDACTED]', text)
    text = _SECRET.sub(lambda m: m.group(1) + m.group(2) + '[REDACTED]' + m.group(2), text)
    return _KEYS.sub('[REDACTED]', text)


def redact_raw(value):
    """把原始异常/ stderr 压成一行并脱敏，最长 200 字。"""
    return _redact(value)[:200]


def redact_body(value, limit=240):
    """上游响应体：先脱敏再截断（绝不落 Authorization/Key），供失败诊断使用。"""
    return _redact(value)[:max(1, int(limit or 240))]


def normalize_code(code):
    """旧分级码 -> 统一失败码；未知码保持原样（由 format_failure 标未归类）。"""
    raw = str(code or '').strip()
    return _ALIASES.get(raw, raw)


def classify_exception(exc):
    """把输入注入层的签名不匹配异常归成结构化失败。

    现场：
      ``ArgumentError: argument 2: TypeError: expected LP_INPUT instance
      instead of INPUT_Array_122``

    ctypes 参数签名与传入数组类型不一致（典型是 SendInput 被别处改过
    ``argtypes``），既不是模型、也不是 OCR 或窗口问题。返回空 dict 表示
    不是注入层异常，调用方继续走原有分类。
    """
    try:
        text = '%s: %s' % (type(exc).__name__, exc)
    except Exception:
        text = type(exc).__name__
    low = text.lower()
    try:
        import ctypes
        is_argument_error = isinstance(exc, ctypes.ArgumentError)
    except Exception:
        is_argument_error = False
    signature_mismatch = ('expected lp_' in low
                          and ('input' in low or 'array' in low))
    if not (is_argument_error or signature_mismatch or 'input_array_' in low):
        return {}
    return dict(
        code='injection_failed',
        stage='inject',
        raw=text[:400],
        next_action=('检查目标输入框是否真的拿到焦点：先在软件窗口里点一下底部输入框，'
                     '把目标窗口切到前台后再重试注入；仍失败就重启该软件再派单，'
                     '并把这条原始异常发给我们。这不是模型或 OCR 问题。'),
        exception_type=type(exc).__name__,
    )


def stage_for(code, app=None):
    app_name = str(app or '')
    if app_name.startswith('豆包') or 'doubao' in app_name.lower():
        override = _APP_GUIDE.get(('豆包', normalize_code(code)))
        if override:
            return override[0]
    guide = _GUIDE.get(normalize_code(code))
    return guide[0] if guide else '提交指令'


def next_action_for(code, app=None):
    app_name = str(app or '')
    if app_name.startswith('豆包') or 'doubao' in app_name.lower():
        override = _APP_GUIDE.get(('豆包', normalize_code(code)))
        if override:
            return override[1]
    guide = _GUIDE.get(normalize_code(code))
    if guide:
        return guide[1]
    return ('按上面的原始异常先修正一次再重试；若仍出现同一条，'
            '桌面咕嘎右键「导出诊断」把 zip 发给开发者（里面含失败原文、版本号和日志）。')


def _exception_type(raw):
    match = _TYPE_HEAD.match(raw)
    if match:
        return match.group(1)
    found = re.search(r'\b[A-Za-z_][\w.]*(?:Error|Exception|Failure|Timeout)\b', raw)
    return found.group(0) if found else '未知异常类型'


def _first_message(raw):
    match = _TYPE_HEAD.match(raw)
    if match:
        return match.group(2)[:80]
    return raw[:80]


def _code_label(code, raw):
    raw_code = str(code or '').strip()
    canonical = normalize_code(raw_code)
    if raw_code and canonical in _GUIDE:
        return canonical
    return '未归类：%s: %s' % (_exception_type(raw), _first_message(raw) or '没有异常消息')


def format_failure(code, stage, raw, next_action, app=None):
    """严格四段：失败码 / 阶段 / 原始异常 / 下一步。"""
    raw_text = redact_raw(raw) or '未提供：执行器没有返回异常或 stderr'
    if raw_text == '执行器报告失败':
        raw_text = '未提供：旧版执行器没有记录失败码或异常摘要'
    label = _code_label(code, raw_text)
    if app:
        label = '%s·%s' % (str(app).strip(), label)
    stage_text = str(stage or '').strip()
    if stage_text not in STAGES:
        stage_text = stage_for(code, app)
    action = _redact(next_action) or next_action_for(code, app)
    return ('【失败码】%s\n【阶段】%s\n【原始异常】%s\n【下一步】%s'
            % (label, stage_text, raw_text, action))


def failure_payload(code, raw, next_action='', app=None, **extra):
    """适配器失败返回的统一字段；调用方可再补 candidates / evidence 等。"""
    canonical = normalize_code(code) or str(code or '')
    stage = stage_for(canonical, app)
    action = _redact(next_action) or next_action_for(canonical, app)
    raw_text = redact_raw(raw) or '未提供：执行器没有返回异常或 stderr'
    if raw_text == '执行器报告失败':
        raw_text = '未提供：旧版执行器没有记录失败码或异常摘要'
    item = dict(ok=False, error=canonical, code=canonical, raw_code=str(code or ''),
                stage=stage, raw=raw_text, next_action=action, message=raw_text,
                failure=format_failure(canonical, stage, raw_text, action, app=app))
    item.update(extra)
    return item
