# -*- coding: utf-8 -*-
"""千问语音（Qwen Speech）— qwen3-asr-flash 识别 + qwen3-tts-flash 合成

只使用官方 HTTP 接口（requests），不额外引入 dashscope SDK：
- 识别：compatible-mode /chat/completions，内容为 data URI 音频；
        若该通道返回 404/400（平台未开放兼容层）则自动回退原生
        multimodal-generation 端点。
- 合成：/services/aigc/multimodal-generation/generation，返回 24 小时有效的
        音频链接或 base64，内容落盘（wav/mp3 自动识别后缀）。

密钥解析顺序（先到先用，占位符自动忽略）：
    qwen_speech.api_key  →  realtime_voice.api_key  →  dashscope_api_key
    →  环境变量 DASHSCOPE_API_KEY

命令行自检：
    python -m core.qwen_speech --check            只看配置与域名连通性
    python -m core.qwen_speech --check --live     额外做一次真实合成
    python -m core.qwen_speech --say 你好          合成一段语音并打印路径
    python -m core.qwen_speech --asr test.wav     识别本地音频文件
"""
import base64
import io
import json
import os
import socket
import tempfile
import time
import uuid
import wave
from pathlib import Path
from urllib.parse import urlsplit

CONFIG_PATH = Path(__file__).resolve().parents[1] / 'ai_config.json'

# 平台默认入口。provider=qianwen 走千问 AI 平台，bailian 走阿里云百炼。
PLATFORM_HOSTS = {
    'qianwen': 'https://maas.qianwenaiapi.com',
    'bailian': 'https://dashscope.aliyuncs.com',
}

DEFAULTS = dict(enabled=True, provider='', api_key='', base_url='', asr_model='qwen3-asr-flash',
                tts_model='qwen3-tts-flash', tts_voice='Cherry', language_type='Chinese',
                timeout=30)

# 文本上限：官方 qwen3-tts-flash 单次 600 字符
TEXT_LIMIT = 600
# 音频上限：官方输入 base64 后不超过 10MB，留出余量
AUDIO_LIMIT = 7 * 1024 * 1024

_PLACEHOLDERS = ('your_', 'sk-xxx', 'xxx', 'changeme')


class QwenSpeechError(RuntimeError):
    """千问语音调用失败（配置缺失、网络、平台返回错误）。"""


# ---------------- 配置 ----------------

def _clean(value):
    """过滤空值与模板占位符，避免拿着 YOUR_XXX 去请求平台。"""
    text = str(value or '').strip()
    if not text or text.lower().startswith(_PLACEHOLDERS):
        return ''
    return text


def read_raw(path=None):
    try:
        return json.loads(Path(path or CONFIG_PATH).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}


def speech_settings(raw=None):
    """合并 qwen_speech 与实时语音/全局密钥，返回可直接使用的配置。"""
    raw = read_raw() if raw is None else raw
    block = raw.get('qwen_speech') or {}
    if not isinstance(block, dict):
        block = {}
    settings = dict(DEFAULTS)
    for key, value in block.items():
        if key in settings:
            settings[key] = value
    realtime = raw.get('realtime_voice') or {}
    if not isinstance(realtime, dict):
        realtime = {}
    settings['provider'] = _clean(settings.get('provider')) or _clean(realtime.get('provider')) or 'bailian'
    settings['api_key'] = (_clean(settings.get('api_key')) or _clean(realtime.get('api_key'))
                           or _clean(raw.get('dashscope_api_key'))
                           or _clean(os.getenv('DASHSCOPE_API_KEY', '')))
    settings['workspace_id'] = _clean(block.get('workspace_id')) or _clean(realtime.get('workspace_id'))
    settings['region'] = _clean(block.get('region')) or _clean(realtime.get('region')) or 'cn-beijing'
    settings['enabled'] = bool(settings.get('enabled', True))
    try:
        settings['timeout'] = max(5, min(120, int(settings.get('timeout') or 30)))
    except (TypeError, ValueError):
        settings['timeout'] = 30
    settings['tts_voice'] = _clean(settings.get('tts_voice')) or 'Cherry'
    settings['asr_model'] = _clean(settings.get('asr_model')) or 'qwen3-asr-flash'
    settings['tts_model'] = _clean(settings.get('tts_model')) or 'qwen3-tts-flash'
    settings['language_type'] = _clean(settings.get('language_type')) or 'Chinese'
    return settings


def api_root(settings=None):
    """服务根地址，例如 https://dashscope.aliyuncs.com/api/v1"""
    cfg = settings or speech_settings()
    base = _clean(cfg.get('base_url')).rstrip('/')
    if base:
        if base.endswith('/compatible-mode/v1'):
            base = base[:-len('/compatible-mode/v1')] + '/api/v1'
        if not base.endswith('/api/v1'):
            base += '/api/v1'
        return base
    provider = cfg.get('provider') or 'bailian'
    host = PLATFORM_HOSTS.get(provider, PLATFORM_HOSTS['bailian'])
    workspace = _clean(cfg.get('workspace_id'))
    region = _clean(cfg.get('region'))
    if provider == 'bailian' and workspace and region == 'ap-southeast-1':
        return f'https://{workspace}.ap-southeast-1.maas.aliyuncs.com/api/v1'
    return host + '/api/v1'


def compatible_root(settings=None):
    """OpenAI 兼容层根地址（识别走这里）。"""
    root = api_root(settings)
    if root.endswith('/api/v1'):
        return root[:-len('/api/v1')] + '/compatible-mode/v1'
    return root


def available(settings=None):
    cfg = settings or speech_settings()
    return bool(cfg.get('enabled') and cfg.get('api_key'))


def describe(settings=None):
    """给界面/自检用的一行说明（不含密钥）。"""
    cfg = settings or speech_settings()
    if not cfg.get('enabled'):
        return '千问语音：已关闭'
    if not cfg.get('api_key'):
        return '千问语音：未配置 API Key'
    return f"千问语音：{cfg['asr_model']} / {cfg['tts_model']} @ {urlsplit(api_root(cfg)).hostname}"


# ---------------- 音频工具 ----------------

def pcm_to_wav(pcm, rate=16000, channels=1, sample_width=2):
    """录音得到的裸 PCM 打包成内存 WAV（识别接口需要完整音频文件）。"""
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(sample_width)
        handle.setframerate(rate)
        handle.writeframes(bytes(pcm))
    return buffer.getvalue()


def _read_audio(audio):
    if isinstance(audio, (bytes, bytearray)):
        raw = bytes(audio)
    else:
        path = Path(audio)
        if not path.is_file():
            raise QwenSpeechError(f'音频文件不存在：{path}')
        raw = path.read_bytes()
    if not raw:
        raise QwenSpeechError('音频为空，没有可识别的内容')
    if len(raw) > AUDIO_LIMIT:
        raise QwenSpeechError(f'音频过大（{len(raw) / 1048576:.1f}MB），超过平台输入限制')
    return raw


def _mime_for(raw):
    if raw[:4] == b'RIFF':
        return 'audio/wav'
    if raw[:3] == b'ID3' or raw[:2] in (b'\xff\xfb', b'\xff\xf3'):
        return 'audio/mpeg'
    return 'audio/wav'


def _data_uri(raw, mime=None):
    mime = mime or _mime_for(raw)
    return 'data:%s;base64,%s' % (mime, base64.b64encode(raw).decode('ascii'))


def _headers(cfg):
    return {'Authorization': 'Bearer ' + cfg['api_key'],
            'Content-Type': 'application/json',
            'User-Agent': 'GugaDesktopPet/1.0'}


def _post(url, cfg, payload, timeout=None):
    try:
        import requests
    except ImportError:  # pragma: no cover - 依赖由一键启动脚本安装
        raise QwenSpeechError('缺少 requests 依赖，请重新运行一键启动.bat')
    try:
        return requests.post(url, headers=_headers(cfg), json=payload,
                             timeout=timeout or cfg.get('timeout', 30))
    except Exception as exc:
        raise QwenSpeechError(f'连接千问语音失败：{exc.__class__.__name__}') from exc


def _error_text(response):
    try:
        data = response.json()
    except ValueError:
        return (response.text or '')[:200]
    if isinstance(data, dict):
        for key in ('message', 'error', 'code'):
            value = data.get(key)
            if isinstance(value, dict):
                value = value.get('message') or value.get('code')
            if value:
                return str(value)[:200]
    return f'HTTP {response.status_code}'


# ---------------- 语音识别 ----------------

def _content_text(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get('text') or item.get('transcription') or ''
                if isinstance(text, str):
                    parts.append(text)
        return ''.join(parts).strip()
    return ''


def _parse_asr(data):
    if not isinstance(data, dict):
        return ''
    output = data.get('output') if isinstance(data.get('output'), dict) else {}
    text = output.get('text')
    if isinstance(text, str) and text.strip():
        return text.strip()
    choices = data.get('choices') or output.get('choices') or []
    if choices:
        message = choices[0].get('message') or {}
        text = _content_text(message.get('content'))
        if text:
            return text
    return ''


def transcribe(audio, mime=None, settings=None, timeout=None):
    """把音频（bytes 或文件路径）转成文字；失败抛 QwenSpeechError。"""
    cfg = settings or speech_settings()
    if not cfg.get('enabled'):
        raise QwenSpeechError('千问语音已关闭')
    if not cfg.get('api_key'):
        raise QwenSpeechError('未配置千问 API Key')
    raw = _read_audio(audio)
    uri = _data_uri(raw, mime)

    compatible = compatible_root(cfg) + '/chat/completions'
    payload = {'model': cfg['asr_model'],
               'messages': [{'role': 'user',
                             'content': [{'type': 'input_audio', 'input_audio': {'data': uri}}]}],
               'stream': False,
               'asr_options': {'enable_itn': True, 'language': 'zh'}}
    response = _post(compatible, cfg, payload, timeout)
    if response.status_code < 400:
        text = _parse_asr(response.json() if response.content else {})
        if text:
            return text
    elif response.status_code in (401, 403):
        raise QwenSpeechError('千问 API Key 无效或无语音模型权限：' + _error_text(response))

    # 兼容层不可用（老平台/未开放）时走原生多模态端点
    native = api_root(cfg) + '/services/aigc/multimodal-generation/generation'
    payload = {'model': cfg['asr_model'],
               'input': {'messages': [{'role': 'user',
                                       'content': [{'type': 'input_audio',
                                                    'input_audio': {'data': uri}}]}]},
               'parameters': {'format': _mime_for(raw).split('/')[-1], 'sample_rate': '16000'}}
    second = _post(native, cfg, payload, timeout)
    if second.status_code >= 400:
        raise QwenSpeechError('语音识别失败：' + _error_text(second))
    text = _parse_asr(second.json() if second.content else {})
    if not text:
        raise QwenSpeechError('语音识别没有返回文字')
    return text


def transcribe_file(path, settings=None):
    return transcribe(path, settings=settings)


# ---------------- 语音合成 ----------------

def _trim(text, limit=TEXT_LIMIT):
    text = (text or '').strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for mark in ('。', '！', '？', '；', '，', '.', '!', '?', ';', ','):
        index = cut.rfind(mark)
        if index > limit * 0.6:
            return cut[:index + 1]
    return cut


def _audio_suffix(raw):
    if raw[:4] == b'RIFF':
        return '.wav'
    if raw[:3] == b'ID3' or raw[:2] in (b'\xff\xfb', b'\xff\xf3'):
        return '.mp3'
    return '.wav'


def synthesize(text, out_dir=None, settings=None, timeout=None):
    """把文字合成为本地音频文件，返回文件路径（后缀按真实格式）。"""
    cfg = settings or speech_settings()
    if not cfg.get('enabled'):
        raise QwenSpeechError('千问语音已关闭')
    if not cfg.get('api_key'):
        raise QwenSpeechError('未配置千问 API Key')
    spoken = _trim(text)
    if not spoken:
        raise QwenSpeechError('没有可朗读的文字')

    url = api_root(cfg) + '/services/aigc/multimodal-generation/generation'
    payload = {'model': cfg['tts_model'],
               'input': {'text': spoken,
                         'voice': cfg['tts_voice'],
                         'language_type': cfg['language_type']}}
    response = _post(url, cfg, payload, timeout)
    if response.status_code >= 400:
        raise QwenSpeechError('语音合成失败：' + _error_text(response))
    try:
        data = response.json()
    except ValueError:
        raise QwenSpeechError('语音合成返回格式异常')
    audio = (data.get('output') or {}).get('audio') or {}
    raw = b''
    encoded = audio.get('data') if isinstance(audio, dict) else ''
    if isinstance(encoded, str) and encoded:
        try:
            raw = base64.b64decode(encoded)
        except (ValueError, TypeError):
            raw = b''
    if not raw:
        link = audio.get('url') if isinstance(audio, dict) else ''
        if isinstance(link, str) and link:
            try:
                import requests
                fetched = requests.get(link, timeout=timeout or cfg.get('timeout', 30))
                if fetched.status_code < 400:
                    raw = fetched.content
            except Exception as exc:
                raise QwenSpeechError(f'下载合成音频失败：{exc.__class__.__name__}') from exc
    if not raw:
        raise QwenSpeechError('语音合成没有返回音频')

    folder = Path(out_dir or tempfile.gettempdir())
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ('guga_tts_qwen_%s%s' % (uuid.uuid4().hex[:8], _audio_suffix(raw)))
    path.write_bytes(raw)
    return str(path)


# ---------------- 自检 ----------------

def check(live=False, text='咕嘎，千问语音已经接通。'):
    """返回 [(层级, 项目, 说明)]，层级为 ok/warn/fail。"""
    cfg = speech_settings()
    rows = []
    rows.append(('ok' if cfg['enabled'] else 'warn', '开关',
                 '已启用' if cfg['enabled'] else '已关闭（qwen_speech.enabled = false）'))
    rows.append(('ok' if cfg['api_key'] else 'fail', 'API Key',
                 f"已配置（{len(cfg['api_key'])} 位）" if cfg['api_key']
                 else '未配置：填 qwen_speech.api_key，或沿用实时语音的密钥'))
    rows.append(('ok', '平台', cfg['provider']))
    rows.append(('ok', '识别模型', cfg['asr_model']))
    rows.append(('ok', '合成模型', '%s / %s' % (cfg['tts_model'], cfg['tts_voice'])))
    rows.append(('ok', '服务地址', api_root(cfg)))
    host = urlsplit(api_root(cfg)).hostname or ''
    try:
        with socket.create_connection((host, 443), timeout=6):
            rows.append(('ok', '域名连通', host + ':443 可连接'))
    except OSError as exc:
        rows.append(('fail', '域名连通', f'{host}:443 不可达（{exc.__class__.__name__}）'))
    if not cfg['api_key']:
        return rows
    if not live:
        rows.append(('warn', '真实调用', '加 --live 参数可做一次真实合成与识别'))
        return rows
    started = time.time()
    try:
        path = synthesize(text, settings=cfg)
        size = os.path.getsize(path)
        rows.append(('ok', '合成实测', f'{os.path.basename(path)} {size / 1024:.0f}KB'
                                      f' 用时 {time.time() - started:.1f}s'))
        try:
            heard = transcribe(path, settings=cfg)
            rows.append(('ok', '识别实测', f'回读「{heard[:30]}」'))
        except QwenSpeechError as exc:
            rows.append(('warn', '识别实测', str(exc)))
    except QwenSpeechError as exc:
        rows.append(('fail', '合成实测', str(exc)))
    return rows


def main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if '--asr' in argv:
        path = argv[argv.index('--asr') + 1]
        print(transcribe_file(path))
        return 0
    if '--say' in argv:
        text = argv[argv.index('--say') + 1]
        print(synthesize(text))
        return 0
    live = '--live' in argv
    print(describe())
    bad = 0
    for level, name, detail in check(live=live):
        bad += level == 'fail'
        print(f'[{level.upper():4s}] {name}: {detail}')
    return 1 if bad else 0


if __name__ == '__main__':
    raise SystemExit(main())
