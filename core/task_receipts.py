"""Small public facts from nested executor receipts, without images or raw logs."""
import re


def payload(result):
    value = result if isinstance(result, dict) else {}
    for _ in range(5):
        if value.get('ok') is False:return value
        child = value.get('result')
        if not isinstance(child, dict):break
        value = child
    return value


def succeeded(result):
    return isinstance(result, dict) and result.get('ok') is not False and payload(result).get('ok') is not False


def navigation_only(prompt):
    text = str(prompt or '')
    return bool(re.search(r'打开|进入|跳转|切到', text)) and not re.search(
        r'然后|接着|之后|并|再|记住|记下|投稿(?!页)|上传|填写|输入|下载|搜索|保存|发送|提交|完成.*任务', text)


def public_fact(name, args, result):
    data = payload(result); action = str((args or {}).get('action') or '')
    if not succeeded(result):
        from core.agent_memory import safe_text
        return dict(action=action, tool=name, ok=False,
                    error=safe_text(data.get('error') or result.get('error') or '操作没有完成')[:240])
    fact = dict(tool=name, action=action, ok=True)
    if name == 'browser':
        fact.update(url=str(data.get('url_observed') or data.get('url_requested') or '')[:400],
                    title=str(data.get('title') or '')[:120],
                    navigation_verified=bool(data.get('navigation_verified')),
                    launch_requested=bool(data.get('launch_requested')))
    return fact
