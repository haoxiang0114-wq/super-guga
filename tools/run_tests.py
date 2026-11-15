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

print('== 4. AI 对话（DS API 实测）==')
from core.ai_chat import AIChatWorker, SYSTEM_PROMPT
cfg = json.load(open('ai_config.json', encoding='utf-8'))
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

print()
print(f'===== 结果: {PASS} 通过 / {FAIL} 失败 =====')
sys.exit(1 if FAIL else 0)
