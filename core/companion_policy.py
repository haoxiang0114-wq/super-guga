"""User-selected agent roles. Pure routing; never opens an executor itself."""
import re


def read_policy(settings):
    executor=str(settings.value('team/executor','auto') or 'auto').strip().lower()
    if executor not in ('auto','self','codex','dsh','workbuddy'):executor='auto'
    return dict(enabled=settings.value('team/enabled',False,type=bool),
        executor=executor,
        reviewer='codex',auto_review=settings.value('team/auto_review',False,type=bool))


def task_role(text):
    value=re.sub(r'^(?:请|帮我|麻烦|你来|先|给我|做一下|把这个|这个)[，,\s]*','',str(text).strip())
    # A request to implement and then test belongs to the executor; the returned work is audited separately.
    if re.match(r'(?:修复|修改|实现|开发|编写|安装|搭建|升级|优化)',value):return 'executor'
    if re.search(r'审计|代码审查|独立验收|架构(?:设计|方案|规划)|设计.{0,8}架构|运行.{0,5}测试|跑.{0,3}测试|测试.{0,8}(?:代码|项目|功能)|检查.{0,8}代码',value):return 'reviewer'
    return 'executor'


def selected_backend(text,policy,fallback,workflow=None):
    value=str(text)
    denied=set()
    negative=r'(?:不要|不用|无需|禁止|不许|不准|不能|不再|别|不)(?:再|去|主动)?\s*'
    verbs=r'(?:交给|使用|调用|让|用|通过)'
    # A negated instruction is not authorization to launch that executor.
    for clause in re.split(r'[，,。；;！!\n]',value):
        if re.search(negative+verbs+r'?\s*(?:codex|dsh)(?![a-z])',clause,re.I):
            denied.update(n.lower() for n in re.findall(r'(codex|dsh)(?![a-z])',clause,re.I))
    for explicit in re.finditer(verbs+r'\s*(codex|dsh)(?![a-z])',value,re.I):
        prefix=value[max(0,explicit.start()-12):explicit.start()]
        name=explicit.group(1).lower()
        if not re.search(negative+r'$',prefix,re.I) and name not in denied:
            return name
    if workflow:
        preferred = workflow.get('executor')
        return preferred if preferred in ('codex', 'dsh') and preferred not in denied else 'self'
    # Keep the current conversation (including a selected worker) through
    # completion. Settings choose the first executor, not a replacement.
    if fallback in ('codex','dsh'):return 'self' if fallback in denied else fallback
    backend=policy.get(task_role(text)) if policy.get('enabled') else policy.get('executor')
    if backend == 'auto':
        from core.task_router import _is_code_task
        return 'codex' if _is_code_task(value) and 'codex' not in denied else 'self'
    return backend if backend in ('self','codex','dsh') and backend not in denied else 'self'


def workbuddy_prompt(text, policy):
    value = str(text or '').strip()
    explicit = re.match(
        r'^(?:(?:咕嘎[，,、\s]+)?(?:请|帮我|麻烦你)?\s*(?:交给|派给|叫|让|使用|用|问)\s*'
        r'(?:work\s*buddy|工作搭子|工作伙伴))(?![a-z0-9])[\s，,：:]*([\s\S]+)$', value, re.I)
    if explicit:
        body = explicit[1].strip()
        if len(body) > 2 and not re.match(r'^(?:是什么|怎么|如何|不要|别)', body):
            return body
    if policy.get('executor') in ('auto','workbuddy') or policy.get('enabled') and policy.get('executor') == 'self':
        # Pick the dedicated document executor for well-defined owner tasks.
        if re.match(r'^(?:请|帮我|给我)?(?:制作|生成|做)(?:一份|一个|一套)?.{0,24}(?:PPT|幻灯片|演示文稿)',
                    value, re.I) and not re.search(r'不要|不用|别用|codex|dsh|豆包', value, re.I):
            return value
    return ''


def role_instructions(text,policy):
    if not policy.get('enabled'):return ''
    if task_role(text)=='reviewer':
        return '\n【咕嘎分工】你负责审计、架构和测试。以实际文件、行为和检查回执为依据，说明未验证项。'
    return '\n【咕嘎分工】你负责执行。保留现有改动，按原需求完成；返回改动文件、检查命令、退出码和交付路径，供独立审计核对。'
