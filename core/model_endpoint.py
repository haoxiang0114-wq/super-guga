# -*- coding: utf-8 -*-
"""模型接口地址归一化与 HTTP 失败自救说明。

所有请求模型的地方都按 OpenAI 兼容约定拼 ``base_url.rstrip('/') + '/chat/completions'``。
用户最常见的填错方式是把完整端点（``.../chat/completions``）或网页/控制台地址
直接粘进 ``base_url``，拼出来就是双段路径，服务端会返回 404/405。这里统一做归一化，
并把 405 变成能照着改的说明，而不是只说“模型返回 405”。
"""
from __future__ import annotations

import copy
from urllib.parse import urljoin, urlsplit, urlunsplit

CHAT_PATH = '/chat/completions'
_REDIRECT_STATUS = (301, 302, 303, 307, 308)


def urlopen_post(request, body, timeout=25):
    """urllib POST，但重定向后仍保持 POST。

    云端接口（登录/派单/版本上报）用 urllib 发 JSON。urllib 默认在 301/302/303
    后把 POST 改成 GET；云端只要做一次 http→https 或补斜杠跳转，就可能回 405。
    这里统一保留原方法、请求体和头，避免把“云端 405”误判成账号或权限问题。
    """
    import urllib.request

    class _KeepPost(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if code in (301, 302, 303) and req.get_method() not in ('GET', 'HEAD'):
                return urllib.request.Request(
                    newurl, data=req.data, headers=dict(req.header_items()),
                    origin_req_host=req.origin_req_host, unverifiable=True,
                    method=req.get_method())
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = urllib.request.build_opener(_KeepPost())
    return opener.open(request, body, timeout=timeout)

# 用户可能整段粘贴的端点后缀，按长度从长到短剥离。
_ENDPOINT_SUFFIXES = (
    '/chat/completions',
    '/completions',
    '/responses',
    '/embeddings/multimodal',
    '/embeddings',
)

# 常见厂商只填域名时补上它自己的兼容根路径；只补已知域名，不改用户写的路径。
KNOWN_HOST_PATHS = {
    'api.openai.com': '/v1',
    'api.deepseek.com': '/v1',
    'api.moonshot.cn': '/v1',
    'api.siliconflow.cn': '/v1',
    'openrouter.ai': '/api/v1',
    'dashscope.aliyuncs.com': '/compatible-mode/v1',
    'maas.qianwenaiapi.com': '/compatible-mode/v1',
    'open.bigmodel.cn': '/api/paas/v4',
    'ark.cn-beijing.volces.com': '/api/v3',
}

# 本机（2026-11-09）实测通过的清单；证据文件 build/model-probe-k.json。
# 只列真跑过的，没跑过的在说明里明确标“未实测”。
VERIFIED_ENDPOINT = 'https://maas.qianwenaiapi.com/compatible-mode/v1'
VERIFIED_MODELS = (
    dict(model='qwen3.8-max-0902', vision=True, status=200, note='文本/视觉均 200，识图返回“红色”'),
    dict(model='qwen3.8-flash', vision=True, status=200, note='文本/视觉均 200，识图返回“红色”'),
    dict(model='qwen3-vl-plus', vision=True, status=200, note='专用视觉模型，文本/视觉均 200'),
    dict(model='qwen-vl-ocr', vision=True, status=200, note='OCR 专用视觉模型，本机 200'),
)
UNVERIFIED_ENDPOINTS = (
    dict(base_url='https://dashscope.aliyuncs.com/compatible-mode/v1',
         note='阿里云百炼标准兼容地址；本机无 Key，未实测'),
    dict(base_url='https://open.bigmodel.cn/api/paas/v4',
         note='智谱标准兼容地址；本机无 Key，未实测'),
)


def _clean(value):
    return str(value or '').strip().strip('"').strip("'").strip()


def normalize_base_url(value):
    """把用户填的 base_url 归一化成可拼 ``/chat/completions`` 的根地址。

    返回 ``dict(ok, base, original, changed, removed_suffix, added_path, error)``；
    不联网、不改配置文件，只在调用方决定是否回写。
    """
    raw = _clean(value)
    if not raw:
        return dict(ok=False, base='', original='', changed=False,
                    error='没有填写模型接口地址（base_url）')
    if not raw.lower().startswith(('http://', 'https://')):
        raw = 'https://' + raw
    parts = None
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        # 例如 https://[invalid：用户手填的地址可能不是合法 URL。
        # 归一化函数是所有模型调用的公共入口，不能把 ValueError 抛到业务层，
        # 否则一次误填会让整条任务以“未归类异常”崩掉，而不是收到可照改的提示。
        return dict(ok=False, base='', original=raw, changed=False,
                    error='模型接口地址格式不合法（%s）' % (str(exc)[:80] or '无法解析'))
    if not parts.netloc:
        return dict(ok=False, base='', original=raw, changed=False,
                    error='接口地址不完整：%s' % raw[:120])
    path = parts.path.rstrip('/')
    removed = ''
    for suffix in _ENDPOINT_SUFFIXES:
        if path.casefold().endswith(suffix):
            path = path[: -len(suffix)].rstrip('/')
            removed = suffix
            break
    added = ''
    host = (parts.hostname or '').casefold()
    if path in ('', '/') and host in KNOWN_HOST_PATHS:
        path = KNOWN_HOST_PATHS[host]
        added = path
    base = urlunsplit((parts.scheme or 'https', parts.netloc, path, '', ''))
    changed = bool(removed or added or parts.query or parts.fragment or
                   (raw != base))
    return dict(ok=True, base=base, original=raw, changed=changed,
                removed_suffix=removed, added_path=added, error='')


def base_of(value):
    """只取归一化后的地址；不可用时返回空串。"""
    return normalize_base_url(value).get('base') or ''


def request_url(value):
    """咕嘎实际会 POST 的完整地址（用于报错与自检）。"""
    base = base_of(value)
    return (base + CHAT_PATH) if base else ''


def _proxy_configured():
    """进程当前是否会走环境变量或 Windows 系统代理。

    requests 默认 ``trust_env=True``。企业网关/安全软件代理有时会对
    ``POST /chat/completions`` 直接回 405，而同一地址直连却是 200；这类 405
    在独立验证（不经过代理）时永远复现不出来。
    """
    import os
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                 'http_proxy', 'https_proxy', 'all_proxy'):
        if str(os.environ.get(name) or '').strip():
            return True
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\CurrentVersion\Internet Settings') as key:
            enabled, _ = winreg.QueryValueEx(key, 'ProxyEnable')
            server, _ = winreg.QueryValueEx(key, 'ProxyServer')
        return bool(int(enabled or 0) and str(server or '').strip())
    except Exception:
        return False


def _post_once(url, *, trust_env, **kwargs):
    """单次 POST（不自动跟随重定向）；trust_env=False 时完全绕开代理。"""
    import requests
    if trust_env:
        return requests.post(url, allow_redirects=False, **kwargs)
    session = requests.Session()
    session.trust_env = False
    try:
        return session.post(url, allow_redirects=False, **kwargs)
    finally:
        session.close()


def _post_preserving(url, *, trust_env, max_redirects, kwargs):
    """按 Location 继续 POST，返回 (response, hops)。"""
    response = _post_once(url, trust_env=trust_env, **kwargs)
    hops = []
    for _ in range(max(0, int(max_redirects))):
        if getattr(response, 'status_code', None) not in _REDIRECT_STATUS:
            break
        location = str(response.headers.get('Location') or '').strip()
        if not location:
            break
        target = urljoin(str(getattr(response, 'url', '') or url), location)
        hops.append(dict(status=int(response.status_code),
                         from_url=str(getattr(response, 'url', '') or url),
                         to_url=target))
        try:
            response.close()
        except Exception:
            pass
        response = _post_once(target, trust_env=trust_env, **kwargs)
    return response, hops


def post(url, *, max_redirects=2, trust_env=True, bypass_proxy_on_405=True, **kwargs):
    """POST 模型接口，重定向后继续用 POST，不交给 requests 自动改方法。

    requests 默认在 301/302/303 之后把 POST 改成 GET。模型网关只要做一次
    重定向（补结尾斜杠、http→https、CDN 跳转），GET 打到只接受 POST 的
    ``/chat/completions`` 就会返回 405。这类 405 与 Key、模型名、请求体都无关，
    独立用 POST 验证网关时也永远复现不出来。

    这里显式 ``allow_redirects=False``，按 Location 继续 POST，最多跳
    ``max_redirects`` 次；返回最后一次响应，实际跳转记录挂在
    ``response.guga_redirects`` 上供失败诊断使用。

    若最终仍是 405 且进程配置了代理，会**绕过代理再发一次**（同样的 POST、
    同样的重定向保留逻辑），结果挂在 ``response.guga_proxy_bypass``。这样
    “代理把 POST 改写导致 405”的机器不用用户手工改 NO_PROXY 也能跑通。

    ``trust_env``/``bypass_proxy_on_405`` 供诊断探针使用：探针要分别测
    “直连”和“系统代理”两条链路，不能在这个函数里自动绕代理，否则两条链路
    会被抹平成同一条，诊断结论失效。
    """
    kwargs.pop('allow_redirects', None)
    response, hops = _post_preserving(
        url, trust_env=trust_env, max_redirects=max_redirects, kwargs=kwargs)
    if (getattr(response, 'status_code', None) == 405 and trust_env
            and bypass_proxy_on_405 and _proxy_configured()):
        try:
            response.close()
        except Exception:
            pass
        retry, retry_hops = _post_preserving(
            url, trust_env=False, max_redirects=max_redirects, kwargs=kwargs)
        try:
            retry.guga_redirects = hops + [dict(status=405, from_url=url,
                                               to_url=url, proxy_bypass=True)] + retry_hops
            retry.guga_proxy_bypass = True
        except Exception:
            pass
        return retry
    try:
        response.guga_redirects = hops
    except Exception:
        pass
    return response


def normalize_config(config):
    """返回归一化后的配置副本；保留调用方原始 dict 不被意外修改。"""
    data = copy.deepcopy(config or {})
    # 视觉模型单独配置时也会被粘贴成完整端点，必须和主模型走同一套归一化，
    # 否则「重新探测档位」会拼出 .../chat/completions/chat/completions 并误报 404/405。
    for key in ('base_url', 'vision_base_url'):
        if data.get(key):
            fixed = normalize_base_url(data.get(key))
            if fixed.get('ok'):
                data[key] = fixed['base']
    fallback = data.get('fallback')
    if isinstance(fallback, dict) and fallback.get('base_url'):
        fixed = normalize_base_url(fallback.get('base_url'))
        if fixed.get('ok'):
            fallback['base_url'] = fixed['base']
    return data


def verified_lines():
    """三行可直接照抄的“已验证”示例（只含本机真跑过 200 的）。"""
    models = [item['model'] for item in VERIFIED_MODELS]
    lines = []
    for item in VERIFIED_MODELS[:3]:
        lines.append('%s  +  %s（%s）' % (
            VERIFIED_ENDPOINT, item['model'],
            '支持视觉，本机实测 200' if item['vision'] else '本机实测 200'))
    if len(VERIFIED_MODELS) > 3:
        lines.append('同一地址还实测通过：%s' % '、'.join(models[3:]))
    return lines


def unverified_lines():
    """合规但本机没 Key、未实测的地址；单独列，不能混进“已验证可用”。"""
    return ['%s（%s）' % (item['base_url'], item['note']) for item in UNVERIFIED_ENDPOINTS]


def _auth_shape(detail):
    """把 401/403 响应体归类：invalid / permission / quota / unknown。

    只看已打码、已截断的 detail，绝不读取或记录 Authorization 头。
    """
    low = str(detail or '').casefold()
    if any(token in low for token in (
            'invalid api key', 'invalid_api_key', 'invalid key', 'incorrect api key',
            'invalid token', 'token is invalid', 'unauthorized', 'authentication',
            'unauthenticated', '无效', '未授权', '认证失败')):
        return 'invalid'
    if any(token in low for token in (
            'quota', 'balance', 'insufficient', 'arrears', 'exceeded',
            'billing', 'payment required', '额度', '余额', '欠费', '配额')):
        return 'quota'
    if any(token in low for token in (
            'permission', 'not allowed', 'not authorized', 'forbidden', 'no access',
            'model_not_found', 'model not found', 'does not exist', 'not have access',
            '权限', '无权', '未开通', '模型不存在')):
        return 'permission'
    return 'unknown'


_AUTH_TITLES = {
    'invalid': '模型凭据无效（HTTP %d）',
    'permission': '模型没有该模型/接口权限（HTTP %d）',
    'quota': '模型额度不足或账号余额不足（HTTP %d）',
    'unknown': '模型凭据或权限不足（HTTP %d）',
}


def http_failure(status, base_url, model='', detail=''):
    """按 HTTP 状态码给出可自救的说明；405 必须讲清“改哪个字段、改成什么”。"""
    status = int(status or 0)
    base = normalize_base_url(base_url)
    url = (base.get('base') + CHAT_PATH) if base.get('ok') else ''
    attempted = dict(method='POST', url=url or str(base_url or '')[:200],
                     model=str(model or ''))
    if status in (401, 403):
        auth_shape = _auth_shape(detail)
        head = _AUTH_TITLES.get(auth_shape, '模型凭据或权限不足（HTTP %d）') % status
    else:
        auth_shape = ''
        head = '接口地址与协议不匹配（HTTP %d Method Not Allowed）' % status \
            if status == 405 else {
                301: '模型接口发生重定向（HTTP 301）',
                302: '模型接口发生重定向（HTTP 302）',
                303: '模型接口发生重定向（HTTP 303）',
                307: '模型接口发生重定向（HTTP 307）',
                308: '模型接口发生重定向（HTTP 308）',
                400: '模型拒绝了请求参数（HTTP 400）',
                401: '模型凭据无效（HTTP 401）',
                403: '模型凭据或权限不足（HTTP 403）',
                404: '模型接口地址不存在（HTTP 404）',
                429: '模型服务限流或额度用尽（HTTP 429）',
            }.get(status, '模型服务返回 HTTP %d' % status)
    examples = verified_lines()
    if status == 405:
        next_step = (
            '请检查 base_url 是否为 OpenAI 兼容的接口根地址（形如 https://.../v1），'
            '不要填网页/控制台地址，也不要直接粘贴 .../chat/completions（咕嘎会自动补）。'
            '本次实际请求：POST %s。%s'
            '405 还可能来自两处与 Key 无关的原因：①地址被网关重定向后，旧版会自动改用 GET，'
            '而 /chat/completions 只接受 POST（本版已改为保留 POST 重发，所以升级后仍 405 说明不是这条）；'
            '②系统代理/安全软件拦截并把请求改写。可到「诊断」里查看代理快照，或临时关闭系统代理后重试。' % (
                attempted['url'] or '（地址为空）',
                ' 已自动把粘贴的完整端点还原为根地址，若仍 405，请换下面验证过的地址。'
                if base.get('removed_suffix') else ''))
        fixes = examples
    elif status in _REDIRECT_STATUS:
        next_step = ('接口把请求重定向到了别处，且保留 POST 重发后仍未成功。请把 base_url 改成最终地址'
                     '（通常是重定向目标去掉 /chat/completions）。本次实际请求：POST %s。' % (
                         attempted['url'] or '（地址为空）'))
        fixes = examples
    elif status == 404:
        next_step = ('接口返回“地址不存在”。请核对 base_url 的层级：常见是漏了 /v1 或 '
                     '/compatible-mode/v1，或把控制台/网页地址当成 API 地址。本次实际请求：'
                     'POST %s。' % (attempted['url'] or '（地址为空）'))
        fixes = examples
    elif status in (401, 403):
        if auth_shape == 'invalid':
            next_step = ('Key 本身被拒绝（不是地址问题）：打开「模型设置」核对 Key 是否完整、'
                         '是否属于当前服务商，点「验证 Key（直连/系统代理）」现场复验；'
                         '仍 401 就到服务商控制台确认该 Key 没有删除/过期/被重置。')
        elif auth_shape == 'permission':
            next_step = ('Key 已通过但权限不足：到服务商控制台确认账号已开通该模型，'
                         '模型 id 必须与服务商列出的完全一致；或换用有权限的模型 id。'
                         '地址不用改。')
        elif auth_shape == 'quota':
            next_step = ('额度或余额不足：到服务商控制台确认免费额度是否用尽、账户是否欠费，'
                         '充值或换有额度的账号后，再点「模型设置 → 验证 Key」复验。')
        else:
            next_step = ('Key/权限/额度都无法从响应体归类：先点「模型设置 → 验证 Key'
                         '（直连/系统代理）」复验，再把下面的服务端原文发给开发者。')
            if detail:
                next_step += '服务端原文（已脱敏）：%s' % str(detail)[:240]
        fixes = []
    elif status == 429:
        next_step = '额度或限流问题：检查套餐余额与频率限制，稍后重试。'
        fixes = []
    elif status == 400:
        next_step = '参数或模型不兼容：%s。若提示 url error/unsupported，请改为 OpenAI 兼容根地址；' \
                    '若提示模型不存在，请换模型 id。' % (str(detail or '')[:160] or '核对模型 id 与服务商')
        fixes = examples
    else:
        next_step = '核对模型服务状态和请求格式。'
        fixes = []
    return dict(code='model_http_%d' % status, title=head, next_step=next_step,
                examples=fixes, unverified=unverified_lines(), attempted=attempted,
                base_url=base.get('base') or '', base_original=base.get('original') or '',
                auto_fixed=bool(base.get('changed')),
                removed_suffix=base.get('removed_suffix') or '')


def text_of(info):
    """拼成一段可直接展示的中文说明。"""
    info = info or {}
    parts = [info.get('title') or '模型请求失败', info.get('next_step') or '']
    examples = info.get('examples') or []
    if examples:
        parts.append('已验证可用：' + '；'.join(examples))
    unverified = info.get('unverified') or []
    if unverified:
        parts.append('其他兼容地址（本机无 Key、未实测）：' + '；'.join(unverified))
    return '。'.join(item.rstrip('。') for item in parts if item) + '。'


def probe(config=None, *, timeout=30, max_tokens=8, channel='main'):
    """用咕嘎自己的配置真发一次 POST，分别测“直连 / 跟随系统代理”。

    命令入口（--model-probe-json）与设置页「验证 Key」共用这一份逻辑：
    - 不抛异常、不打印 Key；返回 dict 可直接写 JSON 或填到界面；
    - attempts 每项只含模式、状态码、耗时、跳转与打码后的响应片段；
    - 401/403 会附 http_failure 的三类诊断（Key 无效 / 无模型权限 / 额度不足）。
    """
    import time

    from core import guga_paths
    from core.agent_failure import proxy_snapshot
    from core.failure_report import redact_body

    cfg = dict(config or guga_paths.read_config() or {})
    original = str(cfg.get('base_url') or '')
    fixed = normalize_base_url(original)
    base = fixed.get('base') or ''
    url = (base + CHAT_PATH) if base else ''
    key, key_error = guga_paths.clean_api_key(cfg.get('api_key'))
    record = dict(config_path=str(guga_paths.CONFIG), base_url_original=original,
                  base_url_normalized=base, auto_fixed=bool(fixed.get('changed')),
                  removed_suffix=fixed.get('removed_suffix') or '',
                  url=url, method='POST', model=str(cfg.get('model') or ''),
                  key_present=bool(key), key_fingerprint=guga_paths.key_fingerprint(key, channel),
                  proxy=proxy_snapshot(), attempts=[], ok=False)
    if key_error:
        record['error'] = key_error
        record['next_action'] = '重新复制完整 Key，粘贴时不要带入换行/空格'
        return record
    if not key:
        record['error'] = 'ai_config.json 里没有 api_key，无法发起真实请求'
        record['next_action'] = '在「模型设置」填写 API Key 后重试'
        return record
    if not url:
        record['error'] = 'base_url 不可用：%s' % (fixed.get('error') or original)
        record['next_action'] = '在「模型设置」核对 Base URL（形如 https://.../v1）'
        return record

    headers = {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}
    body = {'model': cfg.get('model'), 'max_tokens': max(1, int(max_tokens or 8)),
            'stream': False,
            'messages': [{'role': 'user', 'content': '回复 OK'}]}
    modes = (('direct', False), ('system_proxy', True))
    for mode, trust in modes:
        started = time.monotonic()
        response = None
        try:
            # 与主链路同一套“重定向后仍用 POST”的发送逻辑；两条链路必须各自独立测量，
            # 不能让 post() 自动绕代理，否则 405 的来源会被抹平。
            response = post(url, headers=headers, json=body,
                            timeout=(6, int(timeout or 60)),
                            trust_env=trust, bypass_proxy_on_405=False)
            record['attempts'].append(dict(
                mode=mode, status=response.status_code, url=response.url or url,
                elapsed_s=round(time.monotonic() - started, 2),
                redirects=[dict(status=hop.get('status'), to_url=hop.get('to_url'))
                           for hop in (getattr(response, 'guga_redirects', None) or [])],
                body=redact_body(response.text or '', 240)))
        except Exception as exc:
            record['attempts'].append(dict(
                mode=mode, status=None, url=url,
                elapsed_s=round(time.monotonic() - started, 2),
                error='%s: %s' % (type(exc).__name__, redact_body(str(exc), 200))))
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
    ok = [item for item in record['attempts'] if item.get('status') == 200]
    record['ok'] = bool(ok)
    if not ok:
        bad = next((item for item in record['attempts']
                    if item.get('status') in (400, 401, 403, 404, 405, 429)), None)
        if bad:
            info = http_failure(bad['status'], base, model=cfg.get('model'),
                                detail=bad.get('body') or '')
            record['diagnosis'] = info
            record['diagnosis_text'] = text_of(info)
            statuses = {item.get('status') for item in record['attempts']}
            if record['proxy'] and len(statuses) > 1:
                record['diagnosis_text'] += (
                    '。直连与跟随代理的结果不同，代理很可能就是失败来源；'
                    '请把该域名加入 NO_PROXY 或暂时关闭代理。')
    return record
