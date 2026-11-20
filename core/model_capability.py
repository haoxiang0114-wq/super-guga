# -*- coding: utf-8 -*-
"""模型准入标准 + 运行时能力探测。

这份代码就是“准入标准”的可执行版本：能力项、分档规则、已知模型画像都在常量里，
探测结论来自真实请求，不靠模型名猜。

用法：
    python -m core.model_capability            # 读 ai_config.json，用缓存/探测给出结论
    python -m core.model_capability --probe    # 强制重新探测（会发真实请求）
    python -m core.model_capability --json     # 输出机器可读结果
"""
from __future__ import annotations

import hashlib
import json
import platform
import socket
import time
from datetime import datetime
from pathlib import Path

from core import guga_paths
ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = guga_paths.RUNTIME / 'model_capability.json'
CACHE_TTL = 24 * 3600
# 缓存结构升级：旧缓存没有版本/探测环境/失败原因，按“无缓存”处理，必须重新确认。
CACHE_VERSION = 2

# 本轮真实请求计数：面板/自检共用，用来向用户交代“这一轮发了多少次请求、花了多少 token”。
STATS = {'requests': 0, 'prompt_tokens': 0, 'completion_tokens': 0}

# ---------------------------------------------------------------- 能力项与分档

CAPABILITIES = {
    'tool_call': '自发发起工具调用（不需要人提示“用工具”）',
    'forced_tool': '被要求时必须调用工具（tool_choice=required 真生效）',
    'recovery': '多步规划与按回执改道（失败后换路径，不复读同一句）',
    'vision': '视觉能力（截图能看懂；没有时只能 UIA/OCR 补位）',
    'structured': '输出稳定性（结构化 JSON、长上下文不丢约束）',
    'quota': '额度可用（拿不到额度的高端模型等于不可用）',
    'speed': '响应速度（单次请求 ≤ 20 秒）',
}

SCOPES = {
    'recommended': '推荐档：可跑跨软件多步界面任务（含视觉核对、失败改道）',
    'minimum': '受限档：仍有未通过/未验证的能力项，只限制对应动作；'
               '能直接产出产物的派单与单软件多步任务照常执行，缺视觉时限截图核对，'
               '按步核对即可，不因档位整体拒绝任务',
    'unsuitable': '不适合：额度不可用，或完全不会调用工具，界面任务必然失败',
}

TIER_NAMES = {'recommended': '推荐档', 'minimum': '受限档', 'unsuitable': '不适合'}

SHORT_NAMES = {
    'tool_call': '自发工具调用', 'forced_tool': '强制工具调用', 'recovery': '多步改道',
    'vision': '视觉', 'structured': '结构化输出', 'quota': '额度', 'speed': '速度',
}

# 本机 2026-11-07 的实测画像。grade: pass / degraded / fail / unknown
KNOWN_PROFILES = {
    'glm-4-flash': {
        'tool_call': ('degraded', '实测同一任务有时自发调工具、有时 0 调用，不稳定'),
        'forced_tool': ('fail', '实测 tool_choice=required 被忽略，仍返回纯文字'),
        'recovery': ('fail', '实测失败后会复读上一轮“这次未完成”，并重复同一条 locate'),
        'vision': ('fail', '带 image_url 返回 HTTP 400 code 1210（该模型只接受 text）'),
        'structured': ('unknown', '本轮未做 JSON 稳定性测试'),
        'quota': ('pass', '真实请求 HTTP 200，免费额度可用'),
        'speed': ('degraded', '单次约 8~15 秒'),
    },
    'glm-4.6': {
        'tool_call': ('unknown', '未测到：接口直接 429'),
        'forced_tool': ('unknown', '未测到'),
        'recovery': ('unknown', '未测到'),
        'vision': ('unknown', '未测到'),
        'structured': ('unknown', '未测到'),
        'quota': ('fail', 'HTTP 429 code 1113「余额不足或无可用资源包，请充值」'),
        'speed': ('unknown', '未测到'),
    },
}
KNOWN_PROFILES['glm-4.5v'] = dict(KNOWN_PROFILES['glm-4.6'])
KNOWN_PROFILES['glm-4.5'] = dict(KNOWN_PROFILES['glm-4.6'])

SACRIFICE = {
    'tool_call': '模型不会主动动手，需要系统兜底补观察/补步，复杂任务成功率下降',
    'forced_tool': '强制调用无效，模型可能只回文字；系统能补一次只读观察，但无法替它点击/输入',
    'recovery': '失败后会重复同一路径，不能自己改道，必须靠外层每步纠偏',
    'vision': '看不了截图，只能靠 UIA 控件树和 OCR 文字补位；图标/颜色/布局类判断不可用',
    'structured': '长上下文可能丢约束，结构化字段偶尔不合规，需要解析层兜底',
    'quota': '接口不可用，等于没有这个模型',
    'speed': '单次响应慢，多步任务的等待时间会累加',
}

SUGGESTION = {
    'tool_call': '换有稳定工具调用能力的模型（且额度可用）；或把任务拆成单步，逐步人工确认',
    'forced_tool': '换遵守 tool_choice 的模型；小模型下只能依赖系统补观察，不能承诺“被要求必调工具”',
    'recovery': '换能按回执改道的模型；否则复合任务必须每步人工/AI 外环介入',
    'vision': '要视觉核对就换 glm-4.5v / glm-4.6（需可用额度），或配置 zhipu_api_key + vision_model 走截图问答',
    'structured': '换输出更稳的模型；或在解析层加重试与字段校验',
    'quota': '在智谱控制台充值或换 Key，也可以换到有额度的服务商',
    'speed': '换更快的模型/服务；或减少单次上下文与工具数量',
}

_PROBE_TOOLS = [
    {'type': 'function', 'function': {
        'name': 'computer', 'description': '操作本机窗口：列出/聚焦/观察/点击/输入',
        'parameters': {'type': 'object', 'properties': {
            'action': {'type': 'string', 'enum': ['windows', 'focus', 'observe', 'click', 'type']},
            'query': {'type': 'string'}, 'text': {'type': 'string'}},
            'required': ['action']}}},
    {'type': 'function', 'function': {
        'name': 'applications', 'description': '打开或列出本机已安装软件',
        'parameters': {'type': 'object', 'properties': {'name': {'type': 'string'}}, 'required': []}}},
]


def load_config(path=None):
    if path is None:
        return guga_paths.read_config()
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except Exception:
        return {}


def _fingerprint(cfg):
    raw = '%s|%s|%s' % (cfg.get('base_url', ''), cfg.get('model', ''),
                        hashlib.sha1(str(cfg.get('api_key', '')).encode()).hexdigest()[:16])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def provenance(probe='vision_probe:image_url(captcha)', cfg=None):
    """记录结论是哪台机器、什么时候、用什么方式测出来的（打包机与测试机不同）。"""
    cfg = cfg or {}
    return dict(host=socket.gethostname(), platform=platform.platform(),
                at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                probe=probe, base_url=str(cfg.get('base_url', '')),
                model=str(cfg.get('model', '')))


def _cache_read(cfg):
    try:
        data = json.loads(CACHE_PATH.read_text(encoding='utf-8'))
        if data.get('version') != CACHE_VERSION:
            return None
        if data.get('fingerprint') != _fingerprint(cfg):
            return None
        if time.time() - float(data.get('checked_at') or 0) >= CACHE_TTL:
            return None
        return data
    except Exception:
        return None


def _cache_write(cfg, measured, probe='mixed'):
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {'version': CACHE_VERSION, 'fingerprint': _fingerprint(cfg),
                   'checked_at': time.time(), 'model': str(cfg.get('model', '')),
                   'provenance': provenance(probe, cfg), 'measured': measured}
        CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass


def invalidate_cache():
    """配置改动后清空缓存，下一次自检必须重新确认。"""
    try:
        CACHE_PATH.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- 真实探测

def _post(cfg, payload, timeout):
    from core import model_endpoint
    # 与 SelfAgent._request 共用同一套地址归一化：用户粘贴完整端点或带查询串时，
    # 探测必须打到主链路真正会打的地址，否则面板结论和真实可用性会分叉。
    base = (model_endpoint.base_of(cfg.get('base_url'))
            or str(cfg.get('base_url', '')).rstrip('/'))
    r = model_endpoint.post(base + '/chat/completions', timeout=(4, timeout), json=payload,
                      headers={'Authorization': 'Bearer ' + str(cfg.get('api_key', '')),
                               'Content-Type': 'application/json'})
    STATS['requests'] += 1
    try:
        usage = (r.json() or {}).get('usage') or {}
        STATS['prompt_tokens'] += int(usage.get('prompt_tokens') or 0)
        STATS['completion_tokens'] += int(usage.get('completion_tokens') or 0)
    except Exception:
        pass
    return r


def stats_reset():
    for key in STATS:
        STATS[key] = 0
    return dict(STATS)


def stats():
    return dict(STATS)


def _usage_of(response):
    """把非 200 的真实原因归到额度项。"""
    try:
        body = response.json()
    except Exception:
        body = {}
    code = str((body.get('error') or {}).get('code') or body.get('code') or '')
    message = str((body.get('error') or {}).get('message') or body.get('message') or '')
    if response.status_code == 429 or code in ('1113', '1302', '1303'):
        return 'fail', 'HTTP %d %s%s' % (response.status_code, code, message[:60])
    if response.status_code in (401, 403):
        return 'fail', 'HTTP %d 鉴权失败（Key 无效或无权限）' % response.status_code
    return 'degraded', 'HTTP %d %s' % (response.status_code, message[:60])


def probe(cfg, budget=8.0):
    """真实请求探测。budget 秒内只做最关键的几项（聊天 + 强制工具 + 自发工具）。"""
    model = str(cfg.get('model', ''))
    out = {'model': model, 'checked_at': time.time(), 'source': 'probe'}
    if not cfg.get('api_key') or not str(cfg.get('base_url', '')).startswith('http'):
        out.update(quota=('unknown', '未配置 api_key/base_url'), tool_call=('unknown', '未探测'),
                   forced_tool=('unknown', '未探测'), vision=('unknown', '未探测'),
                   structured=('unknown', '未探测'), recovery=('unknown', '未探测'),
                   speed=('unknown', '未探测'))
        return out
    deadline = time.monotonic() + max(3.0, float(budget))

    def remaining():
        return max(1.5, deadline - time.monotonic())

    # 1) 连通 + 速度
    try:
        started = time.monotonic()
        r = _post(cfg, {'model': model, 'messages': [{'role': 'user', 'content': '回复“可用”两个字'}],
                        'max_tokens': 32, 'temperature': 0, 'stream': False}, remaining())
        elapsed = time.monotonic() - started
        if r.status_code == 200:
            text = ''
            try:
                text = (r.json()['choices'][0]['message'].get('content') or '').strip()
            except Exception:
                pass
            out['quota'] = ('pass', 'HTTP 200，返回%.0f字' % len(text)) if text else ('degraded', 'HTTP 200 但内容为空')
            out['speed'] = ('pass', '%.1f 秒/次' % elapsed) if elapsed <= 20 else ('degraded', '%.1f 秒/次' % elapsed)
        else:
            out['quota'] = _usage_of(r)
            out['speed'] = ('unknown', '请求失败，未测到')
    except Exception as exc:
        out['quota'] = ('fail', '网络/代理失败：%s' % type(exc).__name__)
        out['speed'] = ('unknown', '请求失败，未测到')
    if out['quota'][0] == 'fail':
        for key in ('tool_call', 'forced_tool', 'vision', 'structured', 'recovery'):
            out.setdefault(key, ('unknown', '额度/连通失败，未测到'))
        return out

    # 2) 自发工具调用
    try:
        r = _post(cfg, {'model': model, 'messages': [{'role': 'system', 'content': '你是本机执行智能体，必须用工具操作电脑。'},
                                                    {'role': 'user', 'content': '请聚焦 Trae 窗口。'}],
                        'tools': _PROBE_TOOLS, 'tool_choice': 'auto', 'max_tokens': 128,
                        'temperature': 0, 'stream': False}, remaining())
        calls = []
        if r.status_code == 200:
            calls = (r.json()['choices'][0]['message'].get('tool_calls') or [])
        out['tool_call'] = ('pass', '自发调用 %d 次' % len(calls)) if calls else ('fail', '只回文字，未发起工具调用')
        if r.status_code != 200:
            out['tool_call'] = ('unknown', 'HTTP %d' % r.status_code)
    except Exception as exc:
        out['tool_call'] = ('unknown', '探测失败：%s' % type(exc).__name__)
    if time.monotonic() >= deadline:
        out.setdefault('forced_tool', ('unknown', '预算不足，未测'))
        out.setdefault('structured', ('unknown', '预算不足，未测'))
        out.setdefault('recovery', ('unknown', '预算不足，未测'))
        out.setdefault('vision', _vision_static(cfg))
        return out

    # 3) 强制工具调用（不需要工具的问题，看它是否仍然调用）
    try:
        r = _post(cfg, {'model': model, 'messages': [{'role': 'user', 'content': '你好，请用一句话介绍你自己。'}],
                        'tools': _PROBE_TOOLS, 'tool_choice': 'required', 'max_tokens': 128,
                        'temperature': 0, 'stream': False}, remaining())
        if r.status_code == 200:
            calls = (r.json()['choices'][0]['message'].get('tool_calls') or [])
            out['forced_tool'] = ('pass', 'required 下调用 %d 次' % len(calls)) if calls else (
                'fail', 'required 下仍返回纯文字（tool_choice 被忽略）')
        elif r.status_code == 400:
            out['forced_tool'] = ('fail', 'HTTP 400 不支持 tool_choice=required')
        else:
            out['forced_tool'] = ('unknown', 'HTTP %d' % r.status_code)
    except Exception as exc:
        out['forced_tool'] = ('unknown', '探测失败：%s' % type(exc).__name__)
    # 4) 视觉：真实图片 + 验证码，不看模型名猜；单独配了视觉模型就测视觉目标
    if time.monotonic() < deadline:
        target = _vision_target(cfg)
        if target:
            out['vision'] = _vision_probe(target, str(target.get('model') or ''), remaining())
        else:
            out['vision'] = _vision_probe(cfg, model, remaining())
    else:
        out.setdefault('vision', _vision_static(cfg))
    # 5) 结构化输出 + 约束保持
    if time.monotonic() < deadline:
        out['structured'] = _structured_probe(cfg, model, remaining())
    else:
        out.setdefault('structured', ('unknown', '预算不足，未测'))
    # 6) 失败后是否改道
    if time.monotonic() < deadline:
        out['recovery'] = _recovery_probe(cfg, model, remaining())
    else:
        out.setdefault('recovery', ('unknown', '预算不足，未测'))
    return out


def _vision_target(cfg):
    """单独配置的视觉目标；视觉 Base URL / 模型名 / Key 任一填写即视为已分开配置。"""
    base = str(cfg.get('vision_base_url') or '').strip()
    model = str(cfg.get('vision_model') or '').strip()
    key = str(cfg.get('vision_api_key') or '').strip()
    if not (base or model or key):
        return None
    out = dict(cfg)
    if base:
        from core.model_endpoint import base_of
        out['base_url'] = base_of(base) or base
    if model:
        out['model'] = model
    if key:
        out['api_key'] = key
    return out


def _vision_static(cfg):
    target = _vision_target(cfg)
    if target:
        # 主模型是纯文本时，截图理解本来就走单独配置的视觉模型；
        # 不能再用主模型的视觉结论把整条链路判成“看不了截图”。
        return ('degraded', '已单独配置视觉模型 %s，本次未实测'
                % (target.get('model') or '（跟随主模型）'))
    flag = cfg.get('supports_vision')
    if flag is not None:
        return ('pass', '配置显式声明 supports_vision=true') if bool(flag) else ('fail', '配置显式声明 supports_vision=false')
    model = str(cfg.get('model', '')).lower()
    if any(token in model for token in ('4.6v', '4.5v', '4o', 'vision', 'vl-', '-vl', 'gemini', 'claude')):
        return ('degraded', '按模型名推断可能有视觉，未实测')
    # 未实测的新模型不按名字判死刑：名字里没有视觉标记只能说明“未知”，
    # 否则实测通过也会被这条静态推断压回去。
    return ('unknown', '模型名未含视觉标记，未实测')


def _probe_image():
    """生成带验证码的测试图，用来做真实视觉探测。"""
    try:
        import base64, io
        from PIL import Image, ImageDraw, ImageFont
        image = Image.new('RGB', (360, 140), 'white')
        draw = ImageDraw.Draw(image)
        try:
            font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 48)
        except Exception:
            font = ImageFont.load_default()
        draw.text((28, 44), 'GUGA7421', fill='black', font=font)
        buffer = io.BytesIO()
        image.save(buffer, format='PNG')
        return base64.b64encode(buffer.getvalue()).decode()
    except Exception:
        return ''


def _error_code(response):
    try:
        body = response.json()
    except Exception:
        return ''
    return str((body.get('error') or {}).get('code') or '')


def _vision_failure(response):
    """把带图请求的非 200 结果分成“确认不支持”和“未确认”两类。

    只有服务端明确说“内容类型只能是 text”这类业务错误，才算确认不支持视觉；
    401/403/404/408/429/5xx、网关错误、超时都只能算未确认，不能据此降级。
    """
    code = _error_code(response)
    message = ''
    try:
        body = response.json()
        message = str((body.get('error') or {}).get('message') or '')
    except Exception:
        pass
    text = (code + ' ' + message).lower()
    confirmed = ('1210' in code or 'content.type' in text or 'only text' in text
                 or 'text only' in text or '多模态' in text or 'image' in text and 'not support' in text)
    if response.status_code in (400, 415, 422) and confirmed:
        return ('fail', 'HTTP %d code %s：服务端明确拒绝图片输入' % (response.status_code, code or '-'))
    return ('unknown', '带图 HTTP %d code %s：未确认（可能是网关/鉴权/超时，不能判定无视觉）'
            % (response.status_code, code or '-'))


def _vision_request(cfg, model, remaining):
    image = _probe_image()
    if not image:
        raise RuntimeError('无法生成测试图片（Pillow 不可用）')
    r = _post(cfg, {'model': model, 'messages': [{'role': 'user', 'content': [
        {'type': 'text', 'text': '图中写的是什么？只回答图中的字母和数字。'},
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + image}}]}],
        'max_tokens': 32, 'temperature': 0, 'stream': False}, remaining)
    text = ''
    if r.status_code == 200:
        try:
            text = (r.json()['choices'][0]['message'].get('content') or '').strip()
        except Exception:
            pass
    return r, text


def _vision_probe(cfg, model, remaining):
    try:
        r, text = _vision_request(cfg, model, remaining)
    except Exception as exc:
        return ('unknown', '视觉探测未完成（%s），不能判定无视觉' % type(exc).__name__)
    if r.status_code == 200:
        compact = text.upper().replace(' ', '').replace('-', '')
        if '7421' in compact:
            return ('pass', '读出了图中验证码：%s' % text[:30])
        return ('degraded', '能接收图片但没读出验证码：%s' % (text[:30] or '空回复'))
    return _vision_failure(r)


def vision_test(cfg, budget=8.0):
    """给设置面板用的一次真实视觉测试：返回状态码、是否接受图片、是否读出图中内容。"""
    model = str(cfg.get('model', ''))
    started = time.monotonic()
    info = dict(model=model, base_url=str(cfg.get('base_url', '')),
                method='chat/completions + image_url(data:image/png;base64)',
                image='白色背景黑字验证码 GUGA7421', status=None, accepted=None,
                recognized=None, reply=None, error=None, elapsed=None)
    info.update(provenance('vision_test', cfg))
    try:
        r, text = _vision_request(cfg, model, max(2.0, float(budget)))
    except Exception as exc:
        info['error'] = '请求未完成：%s' % type(exc).__name__
    else:
        info['status'] = r.status_code
        info['elapsed'] = round(time.monotonic() - started, 2)
        if r.status_code == 200:
            compact = text.upper().replace(' ', '').replace('-', '')
            info['accepted'] = True
            info['recognized'] = '7421' in compact
            info['reply'] = text[:80]
        else:
            grade, detail = _vision_failure(r)
            info['accepted'] = False if grade == 'fail' else None
            info['error'] = detail
    info['elapsed'] = info['elapsed'] or round(time.monotonic() - started, 2)
    return info


def ensure_vision(cfg=None, budget=3.0, use_cache=True):
    """只在视觉结论未知时补一次真实探测，并把结果并入缓存（带版本与探测环境）。"""
    cfg = cfg or load_config()
    if use_cache:
        data = _cache_read(cfg)
        if data and isinstance(data.get('measured'), dict) and 'vision' in data['measured']:
            return tuple(data['measured']['vision'])
    if not cfg.get('api_key') or not str(cfg.get('base_url', '')).startswith('http'):
        return ('unknown', '未配置 api_key/base_url')
    value = _vision_probe(cfg, str(cfg.get('model', '')), max(1.5, float(budget)))
    data = _cache_read(cfg) or {'measured': {}}
    measured = dict(data.get('measured') or {})
    measured['vision'] = value
    _cache_write(cfg, measured, probe='vision_probe:image_url(captcha)')
    return value


def _structured_probe(cfg, model, remaining):
    prompt = ('只输出一个 JSON 对象，不要解释、不要 markdown 代码块：'
              '{"task":"<一句话任务名>","steps":["<步骤1>","<步骤2>"],"tag":"GUGA-7421"}。'
              'steps 至少两项，tag 必须原样输出。')
    try:
        r = _post(cfg, {'model': model, 'messages': [{'role': 'user', 'content': prompt}],
                        'max_tokens': 200, 'temperature': 0, 'stream': False}, remaining)
        if r.status_code != 200:
            return ('unknown', 'HTTP %d %s' % (r.status_code, _error_code(r)))
        text = ''
        try:
            text = (r.json()['choices'][0]['message'].get('content') or '').strip()
        except Exception:
            pass
        cleaned = text.strip().strip('`')
        if cleaned.lower().startswith('json'):
            cleaned = cleaned[4:].lstrip()
        data = json.loads(cleaned)
        if not isinstance(data, dict):
            raise ValueError('返回的不是 JSON 对象')
        if not isinstance(data.get('task'), str) or not data.get('task').strip():
            raise ValueError('task 字段缺失')
        steps = data.get('steps')
        if not isinstance(steps, list) or len(steps) < 2 or not all(isinstance(x, str) for x in steps):
            raise ValueError('steps 不足两项')
        if str(data.get('tag')) != 'GUGA-7421':
            raise ValueError('tag 约束丢失')
        return ('pass', 'JSON 合法且字段/约束完整')
    except Exception as exc:
        detail = str(exc)[:60] if isinstance(exc, ValueError) else type(exc).__name__
        return ('fail', '%s：%s' % (detail, (locals().get('text') or '')[:40]))


def _recovery_probe(cfg, model, remaining):
    """两回合：第一回合必然失败，看第二回合是否换路径而不是原样重试。"""
    tools = [{'type': 'function', 'function': {
        'name': 'computer', 'description': '操作本机窗口；focus 聚焦不存在的窗口会失败',
        'parameters': {'type': 'object', 'properties': {
            'action': {'type': 'string', 'enum': ['focus', 'observe', 'windows']},
            'query': {'type': 'string'}}, 'required': ['action']}}}]
    messages = [{'role': 'user', 'content': '请聚焦 Trae 窗口。'}]
    try:
        r1 = _post(cfg, {'model': model, 'messages': messages, 'tools': tools,
                         'tool_choice': 'required', 'max_tokens': 128, 'temperature': 0,
                         'stream': False}, remaining)
        if r1.status_code != 200:
            return ('unknown', 'HTTP %d %s' % (r1.status_code, _error_code(r1)))
        message = r1.json()['choices'][0]['message']
        calls = message.get('tool_calls') or []
        if not calls:
            return ('unknown', '模型未发起工具调用（required 被忽略），无法测改道')
        first = (calls[0]['function']['name'], calls[0]['function'].get('arguments') or '')
        messages.append({'role': 'assistant', 'content': message.get('content') or '', 'tool_calls': calls})
        messages.append({'role': 'tool', 'tool_call_id': calls[0]['id'],
                         'content': json.dumps({'ok': False, 'error': '找不到窗口 Trae：本机没有该进程',
                                                'hint': '先列出窗口或换用实际存在的软件'}, ensure_ascii=False)})
        r2 = _post(cfg, {'model': model, 'messages': messages, 'tools': tools,
                         'tool_choice': 'auto', 'max_tokens': 200, 'temperature': 0,
                         'stream': False}, remaining)
        if r2.status_code != 200:
            return ('unknown', '第二回合 HTTP %d' % r2.status_code)
        message2 = r2.json()['choices'][0]['message']
        calls2 = message2.get('tool_calls') or []
        if calls2:
            second = (calls2[0]['function']['name'], calls2[0]['function'].get('arguments') or '')
            if second != first:
                return ('pass', '失败后改道：%s %s' % (second[0], str(second[1])[:60]))
            return ('fail', '失败后原样重试同一条调用：%s %s' % (second[0], str(second[1])[:60]))
        text = str(message2.get('content') or '')
        if text and ('Trae' in text or '不存在' in text or '找不到' in text):
            return ('degraded', '未再调用工具，但说明了阻塞：%s' % text[:60])
        return ('fail', '失败后未改道：%s' % (text[:60] or '空回复'))
    except Exception as exc:
        return ('unknown', '改道探测失败：%s' % type(exc).__name__)


# ---------------------------------------------------------------- 结论

def _profile_evidence(cfg):
    model = str(cfg.get('model', ''))
    base = dict(KNOWN_PROFILES.get(model) or {})
    evidence = {}
    for key in CAPABILITIES:
        if key in base:
            evidence[key] = base[key]
        elif key == 'vision':
            evidence[key] = _vision_static(cfg)
        elif key == 'quota':
            evidence[key] = ('unknown', '未探测')
        else:
            evidence[key] = ('unknown', '未探测')
    return evidence


def _merge(static, measured):
    evidence = dict(static)
    for key, value in (measured or {}).items():
        if key in CAPABILITIES and isinstance(value, (list, tuple)) and len(value) == 2:
            # 探测说“没测到”时不能覆盖已知的失败结论，否则会把短板洗成未知。
            if value[0] == 'unknown' and evidence.get(key, ('unknown', ''))[0] != 'unknown':
                continue
            # 已知短板不能被一次偶然成功洗白：实测画像来自多次复现，更可信。
            # 已知 fail 是复现过的问题，单次探测的 pass/degraded 都不足以推翻它。
            if (evidence.get(key, ('unknown', ''))[0] in ('fail', 'degraded')
                    and value[0] in ('pass', 'degraded')):
                continue
            evidence[key] = tuple(value)
    return evidence


def tier_of(evidence):
    """只有“即便给它工具也必然办不成”的缺项才拦任务。

    - 额度不可用：任何请求都发不出去，必然失败；
    - 既不自发调工具、强制要求也不调：界面任务没有任何执行入口，必然失败；
    其余缺项（强制工具无效、视觉未验证、结构化不稳、速度慢）只影响对应动作，
    不能据此整体判定“不能干活”。
    """
    if evidence['quota'][0] == 'fail':
        return 'unsuitable'
    if evidence['tool_call'][0] == 'fail' and evidence['forced_tool'][0] == 'fail':
        return 'unsuitable'
    core = ('tool_call', 'forced_tool', 'recovery', 'vision', 'structured', 'quota', 'speed')
    if all(evidence[key][0] == 'pass' for key in core):
        return 'recommended'
    return 'minimum'


def assess(cfg=None, measured=None, use_cache=True, probe_if_missing=True, budget=8.0):
    cfg = cfg or load_config()
    model = str(cfg.get('model', ''))
    cached = None
    if use_cache and not measured:
        data = _cache_read(cfg)
        if data:
            cached = data.get('measured') or {}
    if measured is None:
        measured = cached
    if measured is None and probe_if_missing:
        measured = probe(cfg, budget=budget)
        _cache_write(cfg, measured, probe='probe:7项真实请求')
    evidence = _merge(_profile_evidence(cfg), measured)
    tier = tier_of(evidence)
    missing = []
    for key in CAPABILITIES:
        grade, detail = evidence[key]
        if grade != 'pass':
            missing.append(dict(capability=key, label=CAPABILITIES[key], grade=grade, detail=detail,
                                sacrifice=SACRIFICE[key], suggestion=SUGGESTION[key]))
    checked_at = (measured or {}).get('checked_at')
    return dict(model=model, tier=tier, tier_name=TIER_NAMES[tier], scope=SCOPES[tier],
                evidence=evidence, missing=missing, cached=bool(cached), checked_at=checked_at,
                probe_source=('缓存' if cached else '实时请求' if measured else '未探测'),
                provenance=provenance('probe:7项真实请求' if measured else 'none', cfg),
                fingerprint=_fingerprint(cfg))


def runtime_notice(result):
    """把准入结论翻译成执行智能体必须遵守的降级约束（注入 system prompt）。"""
    evidence = result.get('evidence') or {}
    tier = result.get('tier')
    if result.get('probe_source') == '未探测':
        return ('当前模型能力尚未验证（本机没有有效缓存）。启动自检不发网络请求以免扣费；'
                '请在「模型设置」里点「测试连接」或「重新探测档位」确认后再下界面任务。'
                '“未验证”不等于“不支持”：不要据此宣称模型没有视觉，也不要按降级模式操作。')
    if tier == 'recommended':
        return ''
    parts = []
    if tier == 'unsuitable':
        parts.append('当前模型未通过准入：只能做文本问答，不要承诺执行界面任务；'
                     '必须操作电脑时先如实说明模型能力不足。')
    else:
        parts.append('当前模型有未通过/未验证的能力项，但不等于不能执行任务：'
                     '下面列出的缺项只限制对应动作，能做的事照常做；复合任务按步核对即可，'
                     '不要因为档位直接拒绝，也不要把产物动作推回给用户。')
    vision_grade = evidence.get('vision', ('unknown', ''))[0]
    if vision_grade == 'fail':
        parts.append('已确认本模型看不了截图：不要以画面内容为依据；先用 accessibility/uia_read 取控件，'
                     '取不到再用 locate/ocr 取文字，点击用 0..1 相对比例坐标。')
    elif vision_grade == 'unknown':
        parts.append('本模型视觉能力“未确认”（不是确认不支持）：探测超时/网络错/网关 4xx 都可能造成，'
                     '不要据此宣称模型没有视觉；先按文字/控件事实操作，'
                     '需要看图时在「模型设置 → 测试连接」里确认，或配 zhipu_api_key + vision_model 补视觉。')
    elif vision_grade == 'degraded':
        parts.append('本模型能接收图片但识图不稳定：截图只作参考，精确文字/控件仍以 accessibility 或 ocr 为准。')
    if evidence.get('forced_tool', ('unknown', ''))[0] != 'pass' or \
            evidence.get('tool_call', ('unknown', ''))[0] != 'pass':
        parts.append('本模型不保证“被要求必调工具”：每轮先调用只读观察或工具取得证据，不能只回文字就宣布完成。'
                     '能直接产出产物的派单（如 WorkBuddy 生成 .pptx）不受这一项影响。')
    if evidence.get('recovery', ('unknown', ''))[0] != 'pass':
        parts.append('失败后禁止原样重试：先 observe 核对焦点和实际状态，再换路径；同一路径连续失败就停下说明卡点。')
    if evidence.get('structured', ('unknown', ''))[0] != 'pass':
        parts.append('工具参数必须严格符合 schema，不确定的字段宁可不填，不要编造 hwnd/ID/坐标。')
    if evidence.get('speed', ('unknown', ''))[0] != 'pass':
        parts.append('响应较慢：减少单次上下文和工具数量，避免无意义重试。')
    return '\n'.join(parts)


def explain(result, compact=False):
    """给用户看的结论：属于哪档、缺什么、牺牲什么、建议换什么。"""
    lines = ['模型 %s → %s' % (result['model'] or '(未配置)', result['tier_name'])]
    lines.append('能支撑：' + result['scope'])
    if result['missing']:
        lines.append('缺项：')
        for item in result['missing']:
            lines.append('  · %s [%s] %s' % (item['label'], item['grade'], item['detail']))
            lines.append('    代价：' + item['sacrifice'])
            if not compact:
                lines.append('    建议：' + item['suggestion'])
    else:
        lines.append('七项能力全部通过。')
    return '\n'.join(lines)


def main(argv=None):
    import sys
    argv = argv if argv is not None else sys.argv[1:]
    cfg = load_config()
    force = '--probe' in argv
    budget = 8.0 if not force else 30.0
    result = assess(cfg, use_cache=not force, probe_if_missing=True, budget=budget)
    if '--json' in argv:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(explain(result))
        print('（探测来源：%s）' % ('缓存' if result['cached'] else '实时请求'))
    return result


if __name__ == '__main__':
    main()
