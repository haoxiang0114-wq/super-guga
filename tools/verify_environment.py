# -*- coding: utf-8 -*-
"""安装自检 — 确认咕嘎桌宠的运行环境是否完整可用

用法：
    .venv\\Scripts\\python.exe tools\\verify_environment.py
    .venv\\Scripts\\python.exe tools\\verify_environment.py --net --tts

检查范围：Python 版本、依赖包、Qt 平台插件、关键素材、配置项、.NET 桥接、
音频设备；加 --net 额外测试模型接口连通性，加 --tts 额外测试 Edge 语音合成。
只做只读检查，不写配置、不改仓库文件。
"""
import io
import os
import sys
import json
import platform
import importlib

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

# 必须早于 PyQt5 / onnxruntime：先占住系统运行库模块名，否则两者共存会崩溃
try:
    from launcher import _preload_msvc_runtime
    _preload_msvc_runtime()
except Exception:
    pass

OK = WARN = FAIL = 0


def line(tag, name, detail=''):
    global OK, WARN, FAIL
    if tag == 'ok':
        OK += 1
        mark = '  [OK]   '
    elif tag == 'warn':
        WARN += 1
        mark = '  [警告] '
    else:
        FAIL += 1
        mark = '  [失败] '
    print(f'{mark}{name}' + (f' — {detail}' if detail else ''))


def section(title):
    print(f'\n== {title} ==')


# ---------------- 1. Python ----------------
section('Python 解释器')
v = sys.version_info
line('ok' if v >= (3, 10) else 'fail', f'Python {v.major}.{v.minor}.{v.micro}',
     sys.executable if v >= (3, 10) else '需要 3.10 或更高版本')
line('ok' if v < (3, 15) else 'warn', f'平台 {platform.machine()} / {platform.system()}',
     'PyQt5 官方轮子目前不支持 3.15+' if v >= (3, 15) else '')
if 'Windows' not in platform.system():
    line('fail', '操作系统', '本项目只支持 Windows 10/11')

# ---------------- 2. 依赖 ----------------
section('依赖包')
REQUIRED = [
    ('PyQt5.QtCore', 'PyQt5'),
    ('PyQt5.QtWidgets', 'PyQt5'),
    ('PyQt5.QtMultimedia', 'PyQt5'),
    ('PIL', 'Pillow'),
    ('numpy', 'numpy'),
    ('requests', 'requests'),
    ('psutil', 'psutil'),
    ('sounddevice', 'sounddevice'),
    ('vosk', 'vosk'),
    ('clr', 'pythonnet'),
    ('win32api', 'pywin32'),
    ('edge_tts', 'edge-tts'),
    ('av', 'av'),
    ('OpenGL', 'PyOpenGL'),
    ('playwright', 'playwright'),
    ('onnxruntime', 'onnxruntime'),
    ('kaldi_native_fbank', 'kaldi-native-fbank'),
    ('pywebrtc_audio', 'pywebrtc-audio'),
    ('websocket', 'websocket-client'),
    ('yaml', 'PyYAML'),
]
for mod, pkg in REQUIRED:
    try:
        m = importlib.import_module(mod)
        ver = getattr(m, '__version__', '') or getattr(m, 'VERSION_STR', '')
        line('ok', pkg, str(ver) if ver else '已安装')
    except Exception as e:
        line('fail', pkg, f'导入 {mod} 失败：{e}')

OPTIONAL = [
    ('cv2', 'opencv-python', '视频/动作素材处理'),
    ('yt_dlp', 'yt-dlp', '音乐点歌'),
    ('gradio_client', 'gradio_client', '战棋 DeepBattler 服务'),
    ('torch', 'torch', '战棋策略模型训练'),
    ('rembg', 'rembg', '素材抠图（开发工具）'),
    ('audio_separator', 'audio-separator', '人声分离（开发工具）'),
]
for mod, pkg, why in OPTIONAL:
    try:
        importlib.import_module(mod)
        line('ok', f'{pkg}（可选）', why)
    except Exception:
        line('warn', f'{pkg}（可选）未安装', f'仅影响：{why}；主程序不受影响')

# ---------------- 3. Qt ----------------
section('Qt 图形环境')
try:
    from launcher import _prepare_qt
    has_plugins = _prepare_qt()
    line('ok' if has_plugins else 'warn', 'Qt 插件路径',
         '已定位 platforms 插件目录' if has_plugins else '未找到 platforms 插件目录')
    from PyQt5.QtCore import QT_VERSION_STR, PYQT_VERSION_STR
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    line('ok', 'Qt 运行时', f'Qt {QT_VERSION_STR} / PyQt {PYQT_VERSION_STR} / 平台 {app.platformName()}')
    from PyQt5.QtGui import QImage
    img = QImage(8, 8, QImage.Format_ARGB32)
    img.fill(0)
    line('ok' if not img.isNull() else 'fail', '图像渲染', 'ARGB 缓冲区可用')
except Exception as e:
    line('fail', 'Qt 图形环境', f'{type(e).__name__}: {e}')

try:
    from PyQt5.QtMultimedia import QMediaPlayer
    p = QMediaPlayer()
    line('ok', 'Qt 音频播放器', 'QMediaPlayer 可创建')
except Exception as e:
    line('warn', 'Qt 音频播放器', f'不可用：{e}；语音朗读会受影响')

# ---------------- 4. 素材与数据 ----------------
section('素材与数据')
for rel in ['resources/expressions/normal_transparent.png',
            'resources/expressions/happy_transparent.png',
            'resources/expressions/playful_transparent.png',
            'resources/expressions/sleepy_transparent.png',
            'resources/guga.ico',
            'data/cards_zhcn.json',
            'lib/HearthDb.dll',
            'lib/HearthMirror.dll',
            'knowledge/battlegrounds',
            'plugins/dsh-guga-desktop']:
    p = os.path.join(ROOT, rel)
    if os.path.isdir(p):
        n = len(os.listdir(p))
        line('ok' if n else 'fail', rel, f'{n} 项')
    elif os.path.isfile(p):
        size = os.path.getsize(p)
        line('ok' if size > 0 else 'fail', rel, f'{size/1024:.0f} KB')
    else:
        line('fail', rel, '缺失（源码没下载完整？）')

for rel in ['resources/stickers', 'resources/actions', 'resources/multiagent']:
    p = os.path.join(ROOT, rel)
    if os.path.isdir(p):
        n = len(os.listdir(p))
        line('ok' if n else 'warn', rel, f'{n} 个子项')
    else:
        line('warn', rel, '不存在，对应动画会退化')

# ---------------- 5. 配置 ----------------
section('模型配置 ai_config.json')
cfg_path = os.path.join(ROOT, 'ai_config.json')
cfg = {}
if not os.path.exists(cfg_path):
    line('fail', 'ai_config.json', '不存在，运行「首次配置.bat」或「一键启动.bat」生成')
else:
    try:
        cfg = json.load(open(cfg_path, encoding='utf-8'))
        line('ok', 'ai_config.json', '可解析')
    except Exception as e:
        line('fail', 'ai_config.json', f'JSON 格式错误：{e}')
        cfg = {}

def placeholder(val):
    return (not val) or str(val).strip() in ('', 'YOUR_CHAT_API_KEY', 'YOUR_VISION_API_KEY',
                                             'YOUR_REALTIME_VOICE_API_KEY', 'YOUR_WORKSPACE_ID') \
        or 'YOUR_' in str(val)

if cfg:
    chat_ready = not placeholder(cfg.get('api_key')) and not placeholder(cfg.get('base_url')) \
        and not placeholder(cfg.get('model'))
    line('ok' if chat_ready else 'warn', '聊天模型',
         f"{cfg.get('model')} @ {cfg.get('base_url')}" if chat_ready else '未填写 api_key / base_url / model，对话功能不可用')
    vision_ready = not placeholder(cfg.get('zhipu_api_key'))
    line('ok' if vision_ready else 'warn', '视觉模型（看屏幕/棋盘截图）',
         str(cfg.get('vision_model')) if vision_ready else '未填写 zhipu_api_key，看屏功能不可用')
    rv = cfg.get('realtime_voice') or {}
    rv_ready = not placeholder(rv.get('api_key')) and not placeholder(rv.get('model'))
    line('ok' if rv_ready else 'warn', '实时语音', 
         f"{rv.get('provider')} / {rv.get('model')}" if rv_ready else '未填写实时语音 api_key，长语音通话不可用')

# ---------------- 6. .NET 桥接（读牌桌/战棋） ----------------
section('.NET 桥接（战棋读牌）')
try:
    import clr  # noqa: F401
    dll = os.path.join(ROOT, 'lib', 'HearthDb.dll')
    if os.path.exists(dll):
        clr.AddReference(dll)
        line('ok', 'clr 加载 HearthDb.dll', 'pythonnet 桥接正常')
    else:
        line('warn', 'clr 加载 HearthDb.dll', 'DLL 缺失，战棋卡牌库不可用')
except Exception as e:
    line('warn', '.NET 桥接', f'{type(e).__name__}: {e}（只影响战棋读牌，其他功能正常）')

# ---------------- 7. 音频设备 ----------------
section('音频设备')
try:
    import sounddevice as sd
    devs = sd.query_devices()
    ins = [d for d in devs if d['max_input_channels'] > 0]
    outs = [d for d in devs if d['max_output_channels'] > 0]
    line('ok' if ins else 'warn', '麦克风输入', f'{len(ins)} 个设备' if ins else '没有可用输入设备，语音对话不可用')
    line('ok' if outs else 'warn', '扬声器输出', f'{len(outs)} 个设备' if outs else '没有可用输出设备')
    if ins:
        line('ok', '默认输入', ins[0]['name'][:40])
except Exception as e:
    line('warn', '音频设备', f'{type(e).__name__}: {e}')

# ---------------- 8. 本地语音识别模型 ----------------
section('本地语音识别（Vosk 中文模型）')
try:
    from core.asr import MODEL_DIR
    # 小模型是扁平结构（final.mdl + 若干 .fst），部分大模型才有 am/conf 子目录
    has_model = os.path.isdir(MODEL_DIR) and (
        os.path.exists(os.path.join(MODEL_DIR, 'final.mdl'))
        or os.path.isdir(os.path.join(MODEL_DIR, 'am')))
    line('ok' if has_model else 'warn', '模型目录',
         MODEL_DIR if has_model else f'{MODEL_DIR} 缺少 final.mdl，按住说话不可用')
    if has_model:
        line('ok' if MODEL_DIR.isascii() else 'warn', '路径可被 Vosk 加载',
             '路径为纯 ASCII' if MODEL_DIR.isascii() else '含非 ASCII 字符，Vosk 引擎会加载失败')
except Exception as e:
    line('warn', '模型目录', f'{type(e).__name__}: {e}')

# ---------------- 9. 可选：网络 ----------------
if '--net' in sys.argv:
    section('模型接口连通性')
    import requests
    base = (cfg.get('base_url') or '').rstrip('/')
    if base and not placeholder(base):
        try:
            r = requests.get(base + '/models', timeout=8,
                             headers={'Authorization': 'Bearer ' + str(cfg.get('api_key', ''))})
            line('ok' if r.status_code < 500 else 'warn', f'聊天接口 {base}',
                 f'HTTP {r.status_code}')
        except Exception as e:
            line('warn', f'聊天接口 {base}', f'{type(e).__name__}: {e}')
    for host in ['https://open.bigmodel.cn', 'https://dashscope.aliyuncs.com']:
        try:
            r = requests.head(host, timeout=8)
            line('ok', f'可达 {host}', f'HTTP {r.status_code}')
        except Exception as e:
            line('warn', f'不可达 {host}', str(e)[:80])

# ---------------- 9. 可选：TTS ----------------
if '--tts' in sys.argv:
    section('Edge 语音合成')
    try:
        import asyncio
        import tempfile
        import edge_tts

        async def synth():
            out = os.path.join(tempfile.gettempdir(), 'guga_verify_tts.mp3')
            await edge_tts.Communicate('咕嘎，环境自检通过', 'zh-CN-XiaoyiNeural').save(out)
            return out

        path = asyncio.run(asyncio.wait_for(synth(), timeout=25))
        size = os.path.getsize(path)
        line('ok' if size > 1000 else 'warn', 'edge-tts 合成', f'{size/1024:.0f} KB -> {path}')
    except Exception as e:
        line('warn', 'edge-tts 合成', f'{type(e).__name__}: {e}（需要联网到微软语音服务）')

# ---------------- 汇总 ----------------
print('\n' + '=' * 46)
print(f'通过 {OK} 项 / 警告 {WARN} 项 / 失败 {FAIL} 项')
if FAIL:
    print('结论：环境不完整，请先按上面「失败」项修复。')
elif WARN:
    print('结论：主程序可运行；带「警告」的功能需要补充配置或安装可选依赖。')
else:
    print('结论：环境完整，所有检查项通过。')
print('=' * 46)
sys.exit(1 if FAIL else 0)
