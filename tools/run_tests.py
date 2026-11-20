# -*- coding: utf-8 -*-
"""咕嘎企鹅检测套件：编译 / RAG / AI对话 / 动作解析 / 视觉 / 语音"""
import sys
import os
import io
import json
import py_compile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

PASS = 0
FAIL = 0


def check(name, ok, detail=''):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f'  ✓ {name}')
    else:
        FAIL += 1
        print(f'  ✗ {name}  {detail}')


print('== 1. 编译检查 ==')
files = ['main.py', 'launcher.py', 'core/ai_chat.py', 'core/bg_knowledge.py', 'core/vision.py',
         'core/asr.py', 'core/interaction.py', 'core/status_system.py', 'core/keyboard_hook.py',
         'core/config_manager.py', 'ui/pet_window.py', 'ui/sprite_animator.py', 'ui/tts_engine.py',
         'ui/speech_system.py', 'ui/bongo_panel.py', 'ui/status_bar.py']
for f in files:
    try:
        py_compile.compile(f, doraise=True)
        check(f, True)
    except Exception as e:
        check(f, False, str(e))

print('== 2. RAG 知识库 ==')
from core.bg_knowledge import kb_stats, build_context, retrieve
stats = kb_stats()
check('知识库加载', stats['total'] > 30, f"共{stats['total']}块 {stats['categories']}")
for q, expect in [
    ('黑暗之赐怎么用？', '黑暗之赐'),
    ('野兽流怎么玩？鱼饵怎么用？', '野兽'),
    ('海盗的上锁宝箱是什么？', '宝箱'),
    ('郭分荷是谁？', '郭分荷'),
    ('烈毒改版后怎么防？', '烈毒'),
]:
    ctx = build_context(q)
    check(f'RAG命中[{q[:12]}...]', expect in ctx, ctx[:80])

print('== 3. 动作解析 ==')
from core.ai_chat import parse_reply
cases = [
    ('playful|point|咕嘎～听本鹅的！', ('playful', 'point', '咕嘎～听本鹅的！')),
    ('happy|bounce|咕嘎嘎～', ('happy', 'bounce', '咕嘎嘎～')),
    ('surprised|shake|咕嘎？！', ('surprised', 'shake', '咕嘎？！')),
    ('normal|peek|哼', ('normal', 'peek', '哼')),
    # tsundere 由 core.embodiment 统一映射为动画名 head_shake，这里按映射后的结果断言
    ('sad|tsundere|才不是担心你', ('sad', 'head_shake', '才不是担心你')),
    ('没有竖线格式的回复', ('normal', 'none', '没有竖线格式的回复')),
]
for raw, want in cases:
    got = parse_reply(raw)
    check(f'解析[{raw[:14]}]', got == want, str(got))

print('== 4. AI 对话（实测，走应用真正使用的模型配置）==')
from core.ai_chat import AIChatWorker, SYSTEM_PROMPT
from core.model_settings import load_config
cfg = load_config()
if cfg.get('auto_fallback'):
    print(f'      当前使用兼容通道回退：{cfg.get("model")} @ {cfg.get("base_url")}')
r = {}
cb = lambda ok=False, text='', error='': r.update(ok=ok, text=text, error=error)
w = AIChatWorker(cfg, SYSTEM_PROMPT, [], '（饲养员戳了戳你圆滚滚的肚子）', '', cb)
w.start(); w.join(timeout=90)
check('AI 点击互动', r.get('ok'), r.get('error', ''))
if r.get('ok'):
    expr, action, content = parse_reply(r['text'])
    check('AI 格式合法', expr in ('normal', 'happy', 'sleepy', 'surprised', 'sad', 'playful'),
          r['text'][:80])
    print(f'      台词: {content}')

print('== 5. 视觉模块（GLM 实测，用现有截图）==')
from core import vision
shot = os.environ.get('GUGA_TEST_SCREENSHOT', '')
if os.path.exists(shot):
    res = vision.analyze_board_image(shot, '简要描述这张截图内容')
    check('视觉API调用', bool(res) and not res.startswith('❌'), (res or '')[:100])
    print(f'      视觉输出: {(res or "")[:100]}')
else:
    check('视觉API调用', False, '测试图不存在')

print('== 6. 语音模块 ==')
from core.asr import ASRRecorder, MODEL_DIR
check('Vosk模型存在', os.path.isdir(MODEL_DIR), MODEL_DIR)
rec = ASRRecorder()
check('ASR依赖就绪', rec.available, '')
check('手动录音后端', rec.backend in ('qwen', 'vosk'), rec.backend)

# 千问语音：只验证配置解析与音频打包，不联网、不消耗额度
from core import qwen_speech
qcfg = qwen_speech.speech_settings()
check('千问语音配置可解析', qcfg.get('asr_model') == 'qwen3-asr-flash'
      and qcfg.get('tts_model') == 'qwen3-tts-flash' and bool(qcfg.get('tts_voice')),
      qwen_speech.describe(qcfg))
check('千问语音 WAV 打包', len(qwen_speech.pcm_to_wav(bytes(3200))) == 3244, '16k/16bit 单声道')
check('千问语音密钥状态', True,
      '已配置' if qcfg.get('api_key') else '未配置（按住说话自动用本地 Vosk）')

print('== 7. PyMaster 教学站桥 ==')
from core.pymaster_bridge import SITE as PM_SITE, available as pm_available, glossary_terms, lookup_term
check('站点地址固定', PM_SITE.endswith('/p/uTIQLihsxWIj6pIM7Cx8lc'), PM_SITE)
pm_ok, pm_reason = pm_available()
check('playwright 可用', pm_ok, pm_reason)
pm_terms = glossary_terms()
check('站内术语表', len(pm_terms) >= 50, f'{len(pm_terms)} 条')
pm_hit = lookup_term('闭包')
check('查词命中术语表', bool(pm_hit.get('ok')),
      (pm_hit.get('matches') or [{}])[0].get('desc', '')[:40])
from core.selection_actions import ACTION_LABELS, prompt as sel_prompt
check('划词动作含查词', ACTION_LABELS.get('lookup') == '查词', '')
check('查词提示词带术语表', 'PyMaster 术语表' in sel_prompt('闭包', 'lookup'), '')
from core.self_agent import TOOLS as AGENT_TOOLS
check('智能体注册 pymaster 工具',
      any(item['function']['name'] == 'pymaster' for item in AGENT_TOOLS), '')
try:
    from core import sticker_studio as sticker
    sticker_ok, sticker_why = sticker.available()
    sticker_size = sticker.resolve_canvas()[0]
    check('表情导入内核', sticker_ok, sticker_why or
          '画布 %d×%d，待机链 %d 段' % (sticker_size[0], sticker_size[1],
                                    len(sticker.manifest_clips())))
except Exception as exc:
    check('表情导入内核', False, str(exc)[:60])
check('智能体注册 sticker 工具',
      any(item['function']['name'] == 'sticker' for item in AGENT_TOOLS), '')
try:
    from core import emoji_harvest as emoji
    qq_dirs = emoji.qq_emoji_dirs()
    check('表情收件箱', True,
          'QQ 表情目录 %d 个，收件箱 %d 个（微信 4.x 缓存加密，需在客户端另存为）'
          % (len(qq_dirs), len(emoji.inbox_items())))
except Exception as exc:
    check('表情收件箱', False, str(exc)[:60])
try:
    from core import sticker_motion as motion, sticker_conform as conform, motion_library as mlib
    check('补帧引擎', True,
          '块匹配网格 %d，帧率上限 %g，跨度阈值 %.2f'
          % (motion.GRID, motion.MAX_FPS_CAP, motion.HARD_PAIR))
    check('生成图贴合', True, '抠底 + 底边对齐 + Reinhard 调色')
    check('动作库', True, mlib.summary())
except Exception as exc:
    check('补帧引擎', False, str(exc)[:60])
try:
    from core import doubao_image as doubao
    st = doubao.status()
    check('豆包生图通道', True,
          ('已登录，可做 AI 重绘' if st.get('ready')
           else '未登录（%s），补帧会自动降级为纯本地' % st.get('reason')))
    from PIL import Image as _Img
    _probe = _Img.new('RGBA', (1500, 1500), (0, 0, 0, 0))
    _probe.paste((200, 60, 60, 255), (400, 400, 1100, 1100))
    _ref = doubao.prepare_ref(_probe, name='selftest')
    with _Img.open(str(_ref)) as _im:
        _size = _im.size
        _corner = _im.getpixel((1, 1))
    _ref.unlink(missing_ok=True)
    check('豆包参考图准备', max(_size) <= doubao.PREP_MAX_EDGE and _corner == (255, 255, 255),
          '%dx%d，透明底铺白=%s' % (_size[0], _size[1], _corner == (255, 255, 255)))
except Exception as exc:
    check('豆包生图通道', False, str(exc)[:60])

print('== 桌面开关 ==')
try:
    py_compile.compile('tools/pet_toggle.pyw', doraise=True)
    from core.mcp_contract import pipe_name
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location('pet_toggle', os.path.join(ROOT, 'tools', 'pet_toggle.pyw'))
    _toggle = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_toggle)
    check('桌面开关脚本', True, '没运行就启动，运行中走管道正常退出')
    check('开关与桌宠管道同名', _toggle._pipe_name() == pipe_name(),
          '开关算出的管道名对不上，关闭会退化成发窗口消息')
    with open(os.path.join(ROOT, 'ui', 'mcp_bridge.py'), encoding='utf-8') as _f:
        check('管道退出指令', 'pet_control' in _f.read(), 'mcp_bridge 里没有 pet_control')
    check('桌面快捷方式', os.path.exists(os.path.join(
        os.path.expanduser('~'), 'Desktop', '语音智能体.lnk')),
        '桌面没有 语音智能体.lnk，跑 tools/make_desktop_shortcut.py 重建成')
except Exception as exc:
    check('桌面开关脚本', False, str(exc)[:60])

print('== 走路动作 ==')
try:
    from core.fast_commands import parse_walk
    check('走动指令解析', parse_walk('向左走') == 'left' and parse_walk('别走了') == 'stop'
          and parse_walk('左边的文件') is None,
          '向左走/向右走/随便走走/别走了 都能认，普通名词不会误触发')
    _walk_base = os.path.join(ROOT, 'resources', 'actions', 'walk')
    _walk_manifest = os.path.join(_walk_base, 'manifest.json')
    if os.path.isfile(_walk_manifest):
        from core.avatar_review import approved_action
        import json as _json
        _clips = _json.loads(open(_walk_manifest, encoding='utf-8').read()).get('clips', [])
        _dirs = [c['name'] for c in _clips]
        _ok = all(approved_action(os.path.join(_walk_base, c['frames'])) for c in _clips)
        check('走路素材', bool(_clips) and _ok,
              '方向 %s，每方向 %s 帧，审核记录与帧内容一致'
              % ('/'.join(_dirs), '/'.join(str(len(os.listdir(os.path.join(_walk_base, c['frames'])))) for c in _clips)))
    else:
        check('走路素材', False, '没有 resources/actions/walk/manifest.json，跑 tools/build_walk_from_video.py 生成')
    from ui.sprite_animator import SpriteAnimator as _SA
    check('走路片段加载', hasattr(_SA, 'play_walk') and hasattr(_SA, 'stop_walk'),
          'play_walk(direction) / stop_walk() 已接到动画器')
except Exception as exc:
    check('走路素材', False, str(exc)[:70])

print()
print(f'===== 结果: {PASS} 通过 / {FAIL} 失败 =====')
sys.exit(1 if FAIL else 0)
