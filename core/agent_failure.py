"""Evidence-based failure reports that remain available when the model is down."""
import re
from urllib.parse import urlsplit

import requests

FAILURE_CONTRACT = '''
【执行与失败协议】
先区分用户想达到的目标、实际观察到的状态、可用工具。关闭一个页面、切换窗口、读写单个文件是日常操作，不因口语重复、代词或两三步操作就变成复杂任务。
每次操作前确认目标；操作后用目标特有的证据验证。关闭标签页必须核对该标签消失，不能用关闭整个浏览器代替，也不能仅凭快捷键已发出就说完成。
工具失败后读取 error/code/verification 和回执：明确是目标不明、控件不可用、权限拒绝、工具超时、模型连接或结果未验证。先选另一种有证据支持的方法；相同失败条件最多一次改法，不盲目重复点击、关闭、发送、删除，不为了掩盖失败升级到DSH。
最终无法完成时，用自然中文说明：目标尚未完成；失败发生在哪一步；确切证据和已完成/未完成的操作；已尝试的恢复；可执行的下一步。原因不确定就说尚不能确定，禁止把连接异常归咎于用户软件、权限或不存在的接口。不要只说“失败/稍后重试”。
解释是可核对的简短决策依据，不输出隐藏思维链。模型服务本身断开时，宿主会根据工具回执生成说明，不能依赖再次调用已断开的模型才能解释失败。
'''


def exception_evidence(exc, base_url='', *, stage='model_request', elapsed_ms=0, request_index=0):
    """Keep type/errno/status only: upstream messages may echo keys or proxy URLs."""
    queue=[exc];seen=set();names=[];codes=[]
    while queue and len(seen)<16:
        item=queue.pop(0)
        if not isinstance(item,BaseException) or id(item) in seen:continue
        seen.add(id(item));names.append(type(item).__name__)
        for attr in ('errno','winerror'):
            value=getattr(item,attr,None)
            if isinstance(value,int):codes.append(value)
        queue.extend([item.__cause__,item.__context__,getattr(item,'reason',None)])
        queue.extend(x for x in item.args if isinstance(x,BaseException))
    kinds=set(names)
    if isinstance(exc,requests.exceptions.ProxyError) or 'ProxyError' in kinds:
        code='proxy';reason='模型请求在代理连接层失败';next_step='核对咕嘎进程使用的代理及代理服务状态'
    elif isinstance(exc,requests.exceptions.SSLError) or any('SSL' in x for x in kinds):
        code='tls';reason='模型请求的 TLS 安全连接失败';next_step='核对系统时间、证书和代理 TLS 配置，不关闭证书校验'
    elif 'NameResolutionError' in kinds or 'gaierror' in kinds:
        code='dns';reason='模型服务域名解析失败';next_step='核对网络和该模型服务域名的 DNS 解析'
    elif isinstance(exc,requests.ConnectTimeout):
        code='connect_timeout';reason='连接模型服务超时，尚未收到 HTTP 响应';next_step='检查模型服务可达性，再重试未完成的步骤'
    elif isinstance(exc,requests.ReadTimeout):
        code='read_timeout';reason='模型请求已发出，但读取响应超时';next_step='恢复连接后先核对现场，再继续未完成的步骤'
    elif isinstance(exc,requests.ConnectionError):
        code='connection';reason='模型连接被中断，现有异常不足以确定远端根因';next_step='检查模型服务与网络状态；不能据此判断浏览器连接失败'
    elif isinstance(exc,requests.HTTPError):
        status=getattr(getattr(exc,'response',None),'status_code',None)
        code='http';reason='模型服务返回 HTTP '+str(status)
        next_step=('核对该服务的凭据和模型权限' if status in (401,403) else
                   '核对套餐额度或限流状态' if status in (402,429) else '核对模型服务状态和请求格式')
    else:
        code='executor';reason='执行器异常：'+type(exc).__name__;next_step='根据任务记录核对最后一步，保留已完成的操作'
    return dict(code=code,stage=stage,reason=reason,next_step=next_step,
        exception_types=names,os_error_codes=sorted(set(codes)),
        http_status=getattr(getattr(exc,'response',None),'status_code',None),
        service_host=urlsplit(base_url).hostname,elapsed_ms=round(elapsed_ms,1),request_index=request_index)


def explain_failure(error, *, diagnostic=None, operations=0, events=(), blockers=(),last_tool_failure=None):
    diagnostic=diagnostic or {}
    last_tool_failure=last_tool_failure or {}
    # 执行器给了结构化失败码时走统一四段式（失败码/阶段/原始异常/下一步）。
    # 否则气泡里只剩一句自由文本，用户和日志都拿不到可核对的码——20261114 注入签名
    # 不匹配（expected LP_INPUT instance instead of INPUT_Array_122）就是这样被吞成
    # “未归类异常”的。没有 code 时保持原有叙述式解释。
    code=str(last_tool_failure.get('code') or '').strip()
    if code:
        from core.failure_report import format_failure,next_action_for
        app=last_tool_failure.get('app') or ''
        raw=last_tool_failure.get('raw') or last_tool_failure.get('error') or str(error or '')
        action=last_tool_failure.get('next_action') or next_action_for(code,app)
        report=format_failure(code,last_tool_failure.get('stage') or '',raw,action,app=app)
        if operations:
            successes=sum(e.get('ok') is True for e in events)
            report+='\n【执行情况】已调用工具%d次，其中%d次返回成功；这些回执不等于整个目标完成。'%(operations,successes)
        return report
    reason=diagnostic.get('reason') or last_tool_failure.get('error') or str(error or '执行器没有提供明确原因')
    # Non-transport errors are already redacted by the task executor.
    reason=re.sub(r'\s+',' ',reason).strip()[:240]
    if not operations:
        impact='尚未调用任何操作工具，没有执行关闭、点击或文件修改。'
    else:
        successes=sum(e.get('ok') is True for e in events)
        impact=f'已调用工具{operations}次，其中{successes}次返回成功；这些回执不等于整个目标完成。'
        if events:impact+='最后一个工具是'+str(events[-1].get('tool','未知工具'))+'。'
    next_step=diagnostic.get('next_step') or (
        '这不是目标找不到，而是动作名不受支持：要按键盘改用 press(keys=enter)，'
        '要点按改用 click，然后 observe 核对画面是否真的变了' if '未知屏幕操作' in reason else
        '请先切到要操作的目标页面，我再重新观察后继续' if last_tool_failure.get('code') in
        ('target_ambiguous','target_not_foreground','target_missing','tab_unavailable') else
        '先重新观察目标并核对最后一步，再处理尚未完成的部分')
    return f'这次未完成。{reason.rstrip("。")}。{impact}下一步：{next_step}。'


# ---- [teammate-merge] legacy compat re-exports (additive) ----
from core.legacy_compat import (  # noqa: E402,F401
    proxy_snapshot,
)
