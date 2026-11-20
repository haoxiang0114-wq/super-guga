"""Driver and speech configuration, with references to existing DSH credentials."""
import json,os
from pathlib import Path
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
CONFIG=ROOT/'ai_config.json'


def read_raw(path=CONFIG):
    try:return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError,ValueError):return {}


def dsh_driver(home=None):
    import yaml
    home=Path(home or Path.home())
    path=home/'.dsh/profiles/web/cordis.patch.yml'
    rows=yaml.safe_load(path.read_text(encoding='utf-8')) or []
    selected=next((r.get('config',{}) for r in rows if isinstance(r,dict) and r.get('id')=='agent-default-model'),{})
    provider=selected.get('provider','');model=selected.get('model','')
    providers={}
    for row in rows:
        if isinstance(row,dict):providers.update((row.get('config') or {}).get('providers') or {})
    source=providers.get(provider) or {}
    base=source.get('baseURL') or ('https://api.deepseek.com' if provider=='deepseek' else '')
    ref=source.get('apiKeyEnv') or ('DEEPSEEK_API_KEY' if provider=='deepseek' else '')
    if not base or not ref or not model:raise ValueError('本地 DSH 尚未配置可读取的驱动模型，请在管理台手动设置')
    return dict(base_url=base,model=model,api_key_ref=ref,driver_source='dsh')


def resolve_config(raw,home=None):
    cfg=dict(raw);home=Path(home or Path.home())
    if cfg.get('driver_source')=='dsh':cfg.update(dsh_driver(home))
    ref=cfg.get('api_key_ref')
    if ref:
        import yaml
        try:stored=(yaml.safe_load((home/'.dsh/.credentials.yaml').read_text(encoding='utf-8')) or {}).get('refs',{})
        except (OSError,ValueError):stored={}
        cfg['api_key']=stored.get(ref) or os.environ.get(ref,'')
    if cfg.get('driver_source')!='dsh' and _chat_unusable(cfg):
        fallback=_qwen_chat(raw)
        if fallback:
            cfg['configured_base_url']=cfg.get('base_url','')
            cfg['configured_model']=cfg.get('model','')
            cfg.update(fallback)
            cfg['auto_fallback']=True
    return cfg


QWEN_MAAS_CHAT='https://maas.qianwenaiapi.com/compatible-mode/v1'
DEFAULT_QWEN_CHAT_MODEL='deepseek-v4.1-flash-1017'


def placeholder(value):
    """占位符不是密钥：example 配置里的 YOUR_xxx 一律当没填。"""
    text=str(value or '').strip()
    if not text:return True
    low=text.lower()
    return (low.startswith('your_') or low.startswith('sk-your') or
            'your_openai_compatible_endpoint' in low or low.startswith('your-'))


def _chat_unusable(cfg):
    return placeholder(cfg.get('api_key')) or placeholder(cfg.get('base_url')) or not str(cfg.get('model','')).strip()


def _qwen_chat(raw):
    """聊天模型没配时，借用千问语音那把 Key 的兼容通道（同一把 Key 已能调文本模型）。

    这样用户只填一个千问 Key，查词、答疑、桌宠对话就都能跑；想指定别的模型，
    在 ai_config.json 里写 qwen_chat.model，或者干脆填自己的 base_url/api_key。
    """
    block=raw.get('qwen_chat') if isinstance(raw.get('qwen_chat'),dict) else {}
    if block.get('enabled') is False:return None
    if os.environ.get('GUGA_QWEN_CHAT','').strip().lower() in ('0','off','false','no'):return None
    try:
        from core import qwen_speech
        key=str(qwen_speech.speech_settings().get('api_key') or '')
    except Exception:
        return None
    if placeholder(key):return None
    return dict(base_url=str(block.get('base_url') or QWEN_MAAS_CHAT).rstrip('/'),
                api_key=key,model=str(block.get('model') or DEFAULT_QWEN_CHAT_MODEL),
                driver_source='qwen-maas')


def load_config(path=CONFIG):
    return resolve_config(read_raw(path))


def vision_config(cfg):
    """Use the configured visual endpoint, including the older Zhipu setup."""
    from core.model_endpoint import base_of
    target = dict(cfg)
    base = str(cfg.get('vision_base_url') or '').strip()
    model = str(cfg.get('vision_model') or '').strip()
    key = str(cfg.get('vision_api_key') or '').strip()
    legacy_key = str(cfg.get('zhipu_api_key') or '').strip()
    zhipu = 'https://open.bigmodel.cn/api/paas/v4'
    if (not key and legacy_key and (not model or model.startswith('glm-'))
            and (not base or base_of(base) == zhipu)):
        return dict(target, base_url=zhipu,
                    model=model or 'glm-4.6v-flash', api_key=legacy_key)
    main_base = cfg.get('base_url') or ''
    inherit = not base or (base_of(base) and base_of(base) == base_of(main_base))
    return dict(target, base_url=base or main_base, model=model or cfg.get('model', ''),
                api_key=key or (cfg.get('api_key', '') if inherit else ''))


def public_settings():
    try:
        cfg=load_config();endpoint=urlsplit(cfg.get('base_url',''))
        return dict(driver=dict(source=cfg.get('driver_source','custom'),model=cfg.get('model',''),host=endpoint.hostname,
                    credential_configured=bool(cfg.get('api_key'))),speech=dict(provider=cfg.get('tts_provider') or 'edge',
                    voice=cfg.get('edge_voice') or 'zh-CN-XiaoyiNeural'),manual_voice_independent=True)
    except Exception:return dict(driver=dict(credential_configured=False),speech=dict(provider='edge'),manual_voice_independent=True)


def save_settings(updates,path=CONFIG):
    cfg=read_raw(path);cfg.update(updates)
    if cfg.get('driver_source')=='dsh':
        cfg.update(dsh_driver());cfg['api_key']=''
    if cfg.get('driver_source')!='dsh':
        endpoint=urlsplit(str(cfg.get('base_url','')))
        if endpoint.scheme not in ('http','https') or not endpoint.netloc or endpoint.username:
            raise ValueError('模型地址需要完整的 http:// 或 https:// 服务地址')
        if not str(cfg.get('model','')).strip():raise ValueError('请填写模型名称')
    target=Path(path);temporary=target.with_name(target.name+'.pending')
    temporary.write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding='utf-8')
    os.replace(temporary,target)
    return resolve_config(cfg)


def test_driver(config):
    import requests
    from core.chat_answer import request_payload
    cfg=resolve_config(config)
    if not cfg.get('api_key'):raise ValueError('驱动模型没有可用密钥；请在管理台配置或选择本地 DSH')
    payload=request_payload(cfg,[dict(role='user',content='只回复：连接成功')],'quick')
    payload.update(stream=False,max_tokens=64);payload.pop('stream_options',None)
    response=requests.post(cfg['base_url'].rstrip('/')+'/chat/completions',
        headers={'Authorization':'Bearer '+cfg['api_key'],'Content-Type':'application/json','User-Agent':'GugaDesktopPet/1.0','x-opencode-session':'guga-console-check'},
        json=payload,timeout=(5,18))
    try:
        if response.status_code!=200:
            explanation='密钥无效或已失效' if response.status_code==401 else '服务未接受请求'
            raise RuntimeError(f'HTTP {response.status_code}：{explanation}。未更改实时语音或朗读配置。')
        text=(response.json().get('choices') or [{}])[0].get('message',{}).get('content','')
        if not text:raise RuntimeError('服务未返回回答正文')
        return '连接成功 · '+str(cfg.get('model',''))
    finally:response.close()
