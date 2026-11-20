# -*- coding: utf-8 -*-
"""启动自检的真实探测层

原则（g 版硬要求）：
  · 每一项都必须**真调用一次**被检能力，拿到真实返回值或真实异常才算测过；
  · 做不到实测的项一律标“未测”，绝不因为文件/注册表存在就显示“通过”；
  · 每项结果四态: pass 通过 / degraded 降级(能用但有损) / fail 失败 / untested 未测；
  · 失败或降级都写清：缺什么、去哪补、补完要不要重启；界面可单项重试。

分级（决定程序能不能启动）:
  fatal  致命 —— 缺了程序起不来（只有真正确定时才用）
  warn   降级 —— 程序能起来，但某块能力不可用（给出可照做的安装路径）
  info   提示 —— 首次启动等正常状态，不需要用户处理
  ok     通过
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
LEVEL_RANK = {'ok': 0, 'untested': 1, 'info': 2, 'warn': 3, 'fatal': 4}
LEVEL_TEXT = {'ok': '通过', 'untested': '未测', 'info': '提示', 'warn': '降级', 'fatal': '致命'}
STATE_TEXT = {'pass': '通过', 'degraded': '降级', 'fail': '失败', 'untested': '未测'}


def _item(name, level, detail, hint='', check_id='', state=None, tested=True,
          value=None, error=None, missing='', where='', restart_required=False):
    if state is None:
        state = {'ok': 'pass', 'info': 'untested', 'warn': 'degraded',
                 'untested': 'untested', 'fatal': 'fail'}.get(level, 'untested')
    return dict(name=name, level=level, detail=detail, hint=hint, state=state,
                state_text=STATE_TEXT.get(state, state), tested=bool(tested),
                value=value, error=error, missing=missing, where=where,
                restart_required=bool(restart_required), check_id=check_id)


def _untested(name, detail, hint='', check_id=''):
    return _item(name, 'untested', detail, hint, check_id=check_id, state='untested',
                 tested=False)


def frozen() -> bool:
    return bool(getattr(sys, 'frozen', False))


def _base() -> Path:
    return Path(getattr(sys, '_MEIPASS', ROOT))


def _powershell() -> str:
    """直接用 System32 下的绝对路径，避免精简 PATH 环境找不到 powershell。"""
    root = os.environ.get('SystemRoot', r'C:\Windows')
    candidate = Path(root) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    return str(candidate) if candidate.is_file() else 'powershell'


def _ctx(ctx, key, default=None):
    return (ctx or {}).get(key, default) if isinstance(ctx, dict) else default


# ---------------------------------------------------------------- 单项检查

def scan_old_processes():
    """找旧执行程序/旧桌宠实例（排除自身）。返回 [(pid, 名称, 说明, psutil进程)]."""
    found = []
    try:
        import psutil
    except Exception:
        return found, False
    self_pid = os.getpid()
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if proc.info['pid'] == self_pid:
                continue
            name = (proc.info['name'] or '').lower()
            cmd = ' '.join(proc.info['cmdline'] or []).lower()
            hit = None
            if 'guga-agent' in name:
                hit = '旧版值守程序（会和新桌宠抢单/顶号）'
            elif 'guga-desktop' in name:
                hit = '另一个桌宠实例'
            elif 'guga_agent.py' in cmd and ('python' in name or 'pythonw' in name):
                hit = '脚本方式运行的旧值守程序'
            if hit:
                found.append((proc.info['pid'], proc.info['name'], hit, proc))
        except Exception:
            continue
    return found, True


def _old_process_check(ctx):
    procs, psutil_ok = _ctx(ctx, 'old') or (None, None)
    if procs is None:
        procs, psutil_ok = scan_old_processes()
    if not psutil_ok:
        return _item('旧进程扫描', 'warn', 'psutil 真实调用失败，无法确认是否残留旧值守程序',
                     '请确认任务管理器里没有 guga-agent.exe / 另一个 GUGA-Desktop.exe',
                     check_id='old_processes', state='degraded',
                     error='import psutil 或 process_iter 失败')
    if procs:
        names = '；'.join('#%d %s' % (pid, desc) for pid, _n, desc, _p in procs)
        return _item('旧进程扫描', 'warn',
                     '真实扫描到 %d 个需要清理的进程：%s' % (len(procs), names),
                     '勾选下方“结束旧进程”后继续；否则旧值守程序会和新桌宠抢单',
                     check_id='old_processes', state='degraded', value=len(procs))
    return _item('旧进程扫描', 'ok', 'psutil 真实扫描完成：没有残留的旧值守程序或重复实例',
                 check_id='old_processes', value=0)


def _os_check(ctx=None):
    try:
        version = sys.getwindowsversion()
    except Exception as exc:
        return _item('Windows 版本', 'warn', 'sys.getwindowsversion() 调用失败：%s' % type(exc).__name__,
                     '需要 Windows 10/11 x64', check_id='windows_version', state='degraded',
                     error=str(exc)[:200])
    arch = str(os.environ.get('PROCESSOR_ARCHITECTURE', '')).upper()
    if not arch:
        try:
            import platform
            arch = platform.machine().upper()
        except Exception:
            arch = ''
    label = 'Windows %s build %d（%s）' % (
        {10: '10/11', 6: '7/8'}.get(version.major, str(version.major)),
        version.build, arch or '未知架构')
    if version.major < 10 or (version.major == 10 and version.build < 17763):
        return _item('Windows 版本', 'fatal', label + '：低于最低要求',
                     '请升级到 Windows 10 1809 及以上（推荐 1903+ / Windows 11 x64）；'
                     '系统 OCR 与 UI 自动化在旧系统上不可用',
                     check_id='windows_version', state='fail', value=label)
    if arch and arch != 'AMD64' and arch != 'ARM64':
        return _item('Windows 版本', 'fatal', label + '：不是 64 位系统',
                     '本安装包只支持 64 位 Windows 10/11',
                     check_id='windows_version', state='fail', value=label)
    return _item('Windows 版本', 'ok', label + '，满足最低要求',
                 check_id='windows_version', value=label)


def _runtime_check(ctx=None):
    if not frozen():
        return _item('运行库（LoadLibrary）', 'info', '开发模式启动，使用当前 Python 自带的运行库',
                     '打包版会内置运行库，评委机器无需另装',
                     check_id='runtime', state='untested')
    import ctypes
    loaded, failed = [], []
    for name in ('VCRUNTIME140.dll', 'VCRUNTIME140_1.dll', 'MSVCP140.dll', 'ucrtbase.dll'):
        try:
            ctypes.WinDLL(name)
            loaded.append(name)
        except Exception as exc:
            failed.append('%s（%s）' % (name, type(exc).__name__))
    if failed:
        return _item('运行库（LoadLibrary）', 'fatal',
                     '真实 LoadLibrary 失败：%s' % '、'.join(failed),
                     '这是解压不完整/位数不匹配导致的：请重新解压完整压缩包，'
                     '不要把 exe 单独拷出来运行',
                     check_id='runtime', state='fail', missing='、'.join(failed),
                     where='重新解压完整压缩包（不要单独拷 exe）',
                     restart_required=True, error='、'.join(failed))
    return _item('运行库（LoadLibrary）', 'ok',
                 '真实 LoadLibrary 成功：%s' % '、'.join(loaded),
                 check_id='runtime', value=loaded)


def _imports_check(ctx=None):
    """真实 import，不查 find_spec；冻结进程内还要求 __file__ 位于 _MEIPASS。"""
    modules = ('PyQt5.QtWidgets', 'PyQt5.QtCore', 'PIL', 'numpy', 'pyautogui',
               'pyperclip', 'pygetwindow', 'psutil', 'win32gui', 'pywintypes',
               '_overlapped', 'win32clipboard', 'pythoncom', 'clr', 'sounddevice',
               'vosk', 'websocket', 'pptx', 'imageio_ffmpeg')
    critical = {'PyQt5.QtWidgets', 'PyQt5.QtCore', 'win32gui', 'pywintypes',
                '_overlapped', 'pythoncom', 'clr'}
    import importlib
    imported, failed, fatal_failed, outside = [], [], [], []
    meipass = str(getattr(sys, '_MEIPASS', '') or '')
    for module in modules:
        try:
            loaded = importlib.import_module(module)
            location = str(getattr(loaded, '__file__', '') or '')
            # 冻结包里 pythonnet 的 clr 没有真实文件（__file__='unknown'），
            # 不能因此误判成“来自安装包外”；内置/命名空间模块同样跳过路径校验。
            if (frozen() and meipass and location
                    and location.lower() not in ('unknown', 'built-in', '<frozen>')
                    and not location.startswith(meipass)
                    and not location.startswith('<')):
                outside.append('%s(%s)' % (module, location[:60]))
            imported.append(module)
        except Exception as exc:
            failed.append('%s（%s）' % (module, type(exc).__name__))
            if module in critical:
                fatal_failed.append(module)
    if fatal_failed:
        return _item('内置依赖（真实 import）', 'fatal',
                     '冻结进程内真实 import 失败：%s' % '、'.join(failed),
                     '安装包不完整（解压时被杀毒软件拦截过？）：重新解压完整压缩包；'
                     '仍失败请把整个目录加入杀毒软件白名单',
                     check_id='imports', state='fail', missing='、'.join(fatal_failed),
                     where='重新解压完整压缩包 / 加杀毒白名单', restart_required=True,
                     error='、'.join(failed))
    if outside:
        return _item('内置依赖（真实 import）', 'warn',
                     '模块真实 import 成功，但 %d 个来自安装包外：%s'
                     % (len(outside), '、'.join(outside)),
                     '请确认没有把系统 Python 的 site-packages 混进环境；重装干净安装包后重试',
                     check_id='imports', state='degraded', value=imported, error='、'.join(outside))
    if failed:
        return _item('内置依赖（真实 import）', 'warn',
                     '真实 import 成功 %d 个；失败：%s' % (len(imported), '、'.join(failed)),
                     '失败项对应的能力不可用（语音/文档/截图等）；重新解压完整压缩包，'
                     '并把整个目录加入杀毒软件白名单',
                     check_id='imports', state='degraded', value=len(imported),
                     error='、'.join(failed))
    return _item('内置依赖（真实 import）', 'ok',
                 '冻结进程内真实 import 通过 %d 个模块（含 pywin32/pythoncom/clr/pptx/ffmpeg）'
                 % len(imported), check_id='imports', value=len(imported))


def effective_dpi():
    """读主显示器真实缩放 DPI（120=125%，144=150%）。"""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        try:
            user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                try:
                    user32.SetProcessDPIAware()
                except Exception:
                    pass

        class _Point(ctypes.Structure):
            _fields_ = [('x', wintypes.LONG), ('y', wintypes.LONG)]

        hmon = user32.MonitorFromPoint(_Point(0, 0), 1)
        if hmon:
            try:
                dx, dy = ctypes.c_uint(), ctypes.c_uint()
                if ctypes.windll.shcore.GetDpiForMonitor(
                        hmon, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0 and dx.value:
                    return int(dx.value)
            except Exception:
                pass
        try:
            dpi = int(user32.GetDpiForSystem())
            if dpi:
                return dpi
        except Exception:
            pass
        hdc = user32.GetDC(0)
        try:
            dpi = int(ctypes.windll.gdi32.GetDeviceCaps(hdc, 88))
            if dpi:
                return dpi
        finally:
            user32.ReleaseDC(0, hdc)
    except Exception:
        pass
    return 0


def _display_check(ctx):
    dpi = _ctx(ctx, 'dpi') or effective_dpi()
    if not dpi:
        return _untested('屏幕缩放', 'GetDpiForMonitor/GetDeviceCaps 全部调用失败，未测到真实缩放',
                         '不影响启动；可在 设置 → 系统 → 显示 里人工确认缩放', 'display')
    percent = round(int(dpi) / 96 * 100)
    if percent == 100:
        return _item('屏幕缩放', 'ok', '真实 DPI %d = 100%%，点击定位精度最佳' % dpi,
                     check_id='display', value=dpi)
    return _item('屏幕缩放', 'warn', '真实 DPI %d = %d%%，不是 100%%' % (dpi, percent),
                 '演示真机操作前建议：设置 → 系统 → 显示 → 缩放设为 100%；'
                 '非 100% 时界面指挥的点击定位可能偏移（自检已按真实缩放识别）',
                 check_id='display', state='degraded', value=dpi)


def _config_check(ctx=None):
    from core import guga_paths
    if not guga_paths.writable():
        return _item('配置与运行目录', 'warn', '用户数据目录真实写入检查失败：%s' % guga_paths.describe(),
                     '请检查该目录的写入权限（或用环境变量 GUGA_HOME 指到其它可写目录）；'
                     '安装目录本身只读不影响运行',
                     check_id='config', state='degraded',
                     missing='可写的用户数据目录', where='设置 GUGA_HOME 到可写目录',
                     restart_required=True)
    path = guga_paths.migrate_legacy()
    migration = guga_paths.migration_status()
    if migration.get('status') == 'failed':
        return _item('配置与运行目录', 'warn', '旧配置迁移失败（不影响启动）',
                     '原因：%s；可启动后在配置向导里重新填，或手动把 %s 复制到 %s'
                     % (migration.get('message') or '未知错误',
                        migration.get('source') or '安装目录的 ai_config.json', path),
                     check_id='config', state='degraded',
                     missing='旧配置迁移', where=str(path))
    migrated_note = ('；已从安装目录迁移旧配置' if migration.get('status') == 'migrated' else '')
    if not path.is_file():
        return _item('配置与运行目录', 'info', '真实读取：还没有 ai_config.json（首次启动的正常状态）%s'
                     % migrated_note,
                     '关掉自检后会进入配置向导：选平台 → 填 API Key → 测试连接，全程图形化',
                     check_id='config', state='untested', value=str(path))
    try:
        cfg = json.loads(path.read_text(encoding='utf-8-sig'))
    except Exception as exc:
        return _item('配置与运行目录', 'warn', 'ai_config.json 真实读取失败（JSON 格式错误）：%s'
                     % type(exc).__name__,
                     '删除或改回正确格式；也可启动后在配置向导里重新填',
                     check_id='config', state='degraded', error=str(exc)[:200])
    key = str(cfg.get('api_key', '')).strip()
    url = str(cfg.get('base_url', '')).strip()
    model = str(cfg.get('model', '')).strip()
    if (not key or 'YOUR_' in key or not url.startswith('http')
            or 'YOUR_' in url or not model or 'YOUR_' in model):
        return _item('配置与运行目录', 'warn',
                     '真实读取到 ai_config.json，但对话 API 仍是占位值%s' % migrated_note,
                     '启动后在配置向导里填真实的 api_key / base_url / model；'
                     '不填则只能用到本地功能',
                     check_id='config', state='degraded', missing='api_key/base_url/model',
                     where='启动后的配置向导')
    cloud = cfg.get('cloud') or {}
    extra = '；云端监工已配置' if cloud.get('base_url') else '；未配置云端监工（可选）'
    return _item('配置与运行目录', 'ok', '真实读取 %s / %s%s%s' % (model, url, extra, migrated_note),
                 check_id='config', value=dict(model=model, base_url=url))


def _config_io_check(ctx=None):
    """配置写入回环：中文 + 空格路径写临时文件 → 读回 → 删除。"""
    import tempfile
    try:
        from core import guga_paths
        base = Path(guga_paths.RUNTIME) if guga_paths.writable() else Path(tempfile.gettempdir())
    except Exception:
        base = Path(tempfile.gettempdir())
    folder = None
    try:
        base.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix='咕嘎 自检 空格 ', dir=str(base)))
        target = folder / '配置 回环 测试.json'
        payload = {'中文键': '咕嘎 OCR 自检', 'path': str(target), '数字': 2026}
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        back = json.loads(target.read_text(encoding='utf-8'))
        equal = back == payload
        target.unlink()
        return _item('配置写入回环（中文/空格路径）', 'ok',
                     '真实写入→读回→删除成功：%s（内容一致=%s）' % (folder.name, equal),
                     check_id='config_io', value=str(folder))
    except Exception as exc:
        return _item('配置写入回环（中文/空格路径）', 'warn',
                     '真实写入失败：%s: %s' % (type(exc).__name__, str(exc)[:120]),
                     '检查该目录写入权限；或设置 GUGA_HOME 指向可写目录后重启咕嘎',
                     check_id='config_io', state='degraded',
                     missing='可写目录（中文/空格路径）', where=str(base),
                     restart_required=True, error=str(exc)[:200])
    finally:
        if folder is not None:
            try:
                import shutil
                shutil.rmtree(folder, ignore_errors=True)
            except Exception:
                pass


def _ffmpeg_check(ctx=None):
    try:
        import imageio_ffmpeg
        exe = Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception as exc:
        return _item('ffmpeg', 'warn', '真实调用 imageio_ffmpeg.get_ffmpeg_exe() 失败：%s'
                     % type(exc).__name__,
                     '视频导入、动作包处理会不可用；重新解压完整压缩包，'
                     '或源码运行时执行 pip install imageio-ffmpeg',
                     check_id='ffmpeg', state='degraded', error=str(exc)[:200])
    if not exe.is_file():
        return _item('ffmpeg', 'warn', '真实解析出的 ffmpeg 不存在：%s' % exe,
                     '重新解压完整压缩包，或执行 pip install imageio-ffmpeg',
                     check_id='ffmpeg', state='degraded', missing='ffmpeg.exe',
                     where='重新解压完整压缩包')
    try:
        out = subprocess.run([str(exe), '-version'], capture_output=True, timeout=8,
                             creationflags=_NO_WINDOW)
        first = (out.stdout or b'').decode('utf-8', errors='replace').splitlines()
        line = first[0].strip() if first else ''
        if out.returncode == 0 and line:
            return _item('ffmpeg', 'ok', '真实执行 ffmpeg -version 成功：%s' % line[:80],
                         check_id='ffmpeg', value=line[:80])
        raise RuntimeError('returncode=%s' % out.returncode)
    except Exception as exc:
        return _item('ffmpeg', 'warn', 'ffmpeg 真实执行失败：%s' % type(exc).__name__,
                     '视频导入、动作包处理会不可用；重新解压完整压缩包',
                     check_id='ffmpeg', state='degraded', error=str(exc)[:200])


def _vosk_model_dirs():
    """自检只认“随包”和“用户数据目录”里的模型，不再拿开发机的 .zcode 路径判过。"""
    rows = []
    package = _base() / 'models' / 'vosk-model-cn'
    rows.append(package)
    try:
        from core import guga_paths
        rows.append(Path(guga_paths.BASE) / 'models' / 'vosk-model-cn')
    except Exception:
        pass
    temp = Path(os.environ.get('TEMP', ''))
    rows.append(temp / 'vosk-model-cn')
    return rows


def _vosk_check(ctx=None):
    model_path = None
    for path in _vosk_model_dirs():
        if path.is_dir() and ((path / 'am').is_dir() or (path / 'conf').is_dir()):
            model_path = path
            break
    if model_path is None:
        return _item(
            '离线语音模型（未随包）', 'untested',
            '本安装包没有内置 vosk 中文模型，本次无法实测离线语音（不冒充通过）',
            '首次使用需联网下载约 40MB：运行 GUGA-Desktop.exe --download-vosk-model'
            '（默认走国内镜像 hf-mirror.com，实测约 15 秒；失败自动回退官方源），'
            '或手动下载 https://alphacephei.com/vosk/models/vosk-model-small-cn-0.3.zip '
            '解压到 %GUGA_HOME%\\models\\vosk-model-cn（默认 %LOCALAPPDATA%\\GUGA\\models\\vosk-model-cn）；'
            '下载完成后再点“重试”。不下载只影响“按住说话”的离线识别，不影响启动与文本对话',
            check_id='vosk', state='untested',
            missing='vosk-model-small-cn（约 40MB）',
            where='GUGA-Desktop.exe --download-vosk-model')
    try:
        from vosk import Model, KaldiRecognizer
        model = Model(str(model_path))
        recognizer = KaldiRecognizer(model, 16000)
        recognizer.AcceptWaveform(b'\0' * 3200)
        return _item('离线语音模型', 'ok',
                     '真实加载模型并初始化识别器成功：%s' % model_path,
                     check_id='vosk', value=str(model_path))
    except Exception as exc:
        return _item('离线语音模型', 'warn',
                     '真实加载失败：%s: %s' % (type(exc).__name__, str(exc)[:120]),
                     '离线语音不可用；删掉 %s 后运行 GUGA-Desktop.exe --download-vosk-model 重新下载，'
                     '或改用在线语音' % model_path,
                     check_id='vosk', state='degraded', missing='vosk 中文模型',
                     where='GUGA-Desktop.exe --download-vosk-model', error=str(exc)[:200])


def _mic_check(ctx=None):
    """真实打开录音流并读一小段，不再只看设备列表。"""
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        default = sd.default.device[0] if isinstance(sd.default.device, (list, tuple)) else 0
        name = '默认输入设备'
        try:
            info = devices[int(default)]
            name = info.get('name') or name
        except Exception:
            pass
        with sd.InputStream(samplerate=16000, channels=1, dtype='int16',
                            blocksize=320, device=default) as stream:
            data, overflowed = stream.read(480)
        frames = int(len(data))
        if frames < 480:
            raise RuntimeError('只读到 %d 帧' % frames)
        return _item('麦克风（真实开流）', 'ok',
                     '真实打开录音流并读到 %d 帧（%s%s）'
                     % (frames, name, '，曾溢出' if overflowed else ''),
                     check_id='mic', value=frames)
    except Exception as exc:
        return _item('麦克风（真实开流）', 'warn',
                     '真实打开录音流失败：%s: %s' % (type(exc).__name__, str(exc)[:120]),
                     '检查 设置 → 隐私和安全性 → 麦克风 → 允许桌面应用访问；'
                     '确认没有别的软件独占麦克风后重试该项',
                     check_id='mic', state='degraded', error=str(exc)[:200])


def _font_check(ctx=None):
    """真实渲染中文，确认不靠系统字体回退也能出字。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
        import numpy as np
        font_path = None
        for name in ('msyh.ttc', 'simhei.ttf', 'simsun.ttc'):
            candidate = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts' / name
            if candidate.is_file():
                font_path = candidate
                break
        if font_path is None:
            return _item('中文字体渲染', 'warn', '系统字体目录里找不到微软雅黑/黑体/宋体',
                         '截图问答和 OCR 测试图会退化为英文；'
                         '请在 设置 → 个性化 → 字体 里安装“微软雅黑”，装完重启咕嘎',
                         check_id='font', state='degraded', missing='微软雅黑/黑体',
                         where='设置 → 个性化 → 字体 → 微软雅黑', restart_required=True)
        image = Image.new('RGB', (600, 120), 'white')
        draw = ImageDraw.Draw(image)
        draw.text((10, 30), '咕嘎中文字体渲染测试', font=ImageFont.truetype(str(font_path), 34),
                  fill='black')
        pixels = np.asarray(image.convert('L'))
        ink = int((pixels < 100).sum())
        if ink < 200:
            raise RuntimeError('渲染后只有 %d 个墨点' % ink)
        return _item('中文字体渲染', 'ok',
                     '真实渲染中文成功：%s，墨点 %d' % (font_path.name, ink),
                     check_id='font', value=dict(font=font_path.name, ink=ink))
    except Exception as exc:
        return _item('中文字体渲染', 'warn',
                     '真实渲染失败：%s: %s' % (type(exc).__name__, str(exc)[:120]),
                     '截图问答/OCR 测试会受影响；安装微软雅黑后重启咕嘎',
                     check_id='font', state='degraded', missing='中文字体',
                     where='设置 → 个性化 → 字体', error=str(exc)[:200])


def _pptx_check(ctx=None):
    import tempfile
    import zipfile
    try:
        from pptx import Presentation
        with tempfile.TemporaryDirectory(prefix='guga-pptx-') as folder:
            path = Path(folder) / '自检 演示.pptx'
            deck = Presentation()
            slide = deck.slides.add_slide(deck.slide_layouts[1])
            slide.shapes.title.text = '咕嘎自检'
            slide.placeholders[1].text = '中文 PPT 写入探针'
            deck.save(str(path))
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                if 'ppt/presentation.xml' not in names:
                    raise RuntimeError('缺少 ppt/presentation.xml')
            size = path.stat().st_size
        return _item('PPT 生成（python-pptx）', 'ok',
                     '真实生成并校验 PPTX：%d 字节，含 presentation.xml' % size,
                     check_id='pptx', value=size)
    except Exception as exc:
        return _item('PPT 生成（python-pptx）', 'warn',
                     '真实生成失败：%s: %s' % (type(exc).__name__, str(exc)[:120]),
                     'PPT 交付会失败；重新解压完整压缩包后重试',
                     check_id='pptx', state='degraded', error=str(exc)[:200])


def _ocr_check(ctx=None):
    """真实链路：-File 执行随包 windows_ocr.ps1 + 真中文图 + 真出字 + 重合率 ≥90%。

    失败判降级（不是致命）：程序照常启动，豆包派单降级走 UIA + 剪贴板。
    """
    from core import ocr_engine
    result = ocr_engine.probe(force=True)
    policy = result.get('execution_policy') or '未读取'
    policy_error = result.get('execution_policy_error') or ''
    state = result.get('state') or ('pass' if result.get('available') else 'fail')
    metrics = dict(language=result.get('language'), text=result.get('text'),
                   expected=result.get('expected'), accuracy=result.get('accuracy'),
                   accuracy_gate=result.get('accuracy_gate'), font_ok=result.get('font_ok'),
                   execution_policy=policy, execution_policy_error=policy_error,
                   elapsed_ms=result.get('elapsed_ms'), kind=result.get('kind'))
    if state == 'pass':
        return _item('系统 OCR（-File + 真识别中文）', 'ok',
                     '真实执行 -File windows_ocr.ps1 并识别出“%s”（语言 %s，'
                     '字符重合率 %.0f%% ≥ %.0f%%，耗时 %sms；执行策略 %s）'
                     % ((result.get('text') or '')[:40] or '空',
                        result.get('language') or '未知',
                        float(result.get('accuracy') or 0) * 100,
                        float(result.get('accuracy_gate') or 0.9) * 100,
                        result.get('elapsed_ms'), policy),
                     check_id='ocr', value=metrics)
    missing = result.get('missing') or 'Windows OCR'
    where = result.get('where') or ''
    restart = bool(result.get('restart_required'))
    kind = str(result.get('kind') or 'unknown')
    expected = (result.get('expected') or '')[:24]
    got = (result.get('text') or '空')[:24]
    accuracy = float(result.get('accuracy') or 0) * 100
    if state == 'degraded':
        detail = ('OCR 接口能调通，但真实识别未通过（%s）：自检图识别文本“%s”，'
                  '应识别出“%s”；通常是中文 OCR 语言包没装好或未生效。'
                  '重合率 %.0f%%，执行策略 %s'
                  % (kind, got, expected, accuracy, policy))
    else:
        detail = ('缺少中文 OCR 语言包或 OCR 组件（%s）：%s；自检图识别文本“%s”，'
                  '应识别出“%s”，重合率 %.0f%%，执行策略 %s'
                  % (kind, missing, got, expected, accuracy, policy))
    hint = ('缺什么：%s。去哪补：%s。补完%s。'
            '在此之前：豆包派单会降级走 UIA + 剪贴板（回执标注 ocr_unavailable），'
            '截图问答里的文字识别不可用；其余功能不受影响。原始错误：%s'
            % ('中文 OCR 语言包' if state == 'degraded' else missing,
               where, '重启咕嘎' if restart else '不用重启',
               (result.get('raw') or '')[:160]))
    return _item('系统 OCR（-File + 真识别中文）', 'warn', detail, hint, check_id='ocr',
                 state=state, missing=missing, where=where, restart_required=restart,
                 error=(result.get('raw') or '')[:300], value=metrics)


def _uia_check(ctx=None):
    """真实读一个控件：本窗 snapshot 或 AutomationElement.FromPoint。"""
    hwnd = int(_ctx(ctx, 'hwnd') or 0)
    try:
        from core.uia_service import shared_service
        service = shared_service()
        service.warmup()
    except Exception as exc:
        return _item('UI 自动化（UIA）', 'warn',
                     'UIA 真实初始化失败：%s: %s' % (type(exc).__name__, str(exc)[:140]),
                     '界面指挥会退回键鼠模式，部分软件可能点不准。'
                     'Win10/11 自带 .NET Framework 4.x；若被精简过，'
                     '请在“启用或关闭 Windows 功能”里勾选 .NET Framework 4.8',
                     check_id='uia', state='degraded',
                     missing='.NET Framework 4.x / UIAutomationClient',
                     where='启用或关闭 Windows 功能 → .NET Framework 4.8', restart_required=True,
                     error=str(exc)[:300])
    try:
        import win32gui
        if hwnd and win32gui.IsWindow(hwnd):
            snapshot = service.snapshot(hwnd, limit=40, time_budget=2.0)
            elements = snapshot.get('elements') or []
            named = [item for item in elements if item.get('name')]
            if named:
                sample = named[0]
                return _item('UI 自动化（UIA）', 'ok',
                             '真实读取本窗 %d 个控件（示例：%s / %s）'
                             % (len(elements), sample.get('type') or '?', sample.get('name') or ''),)
            point = _screen_center()
            found = service.from_point(*point)
            element = found.get('element') or {}
            if element.get('type') or element.get('name'):
                return _item('UI 自动化（UIA）', 'ok',
                             '本窗控件树为空；AutomationElement.FromPoint(%d,%d) 真实读到：%s / %s'
                             % (point[0], point[1], element.get('type') or '?',
                                element.get('name') or ''),)
            return _item('UI 自动化（UIA）', 'warn',
                         '真实调用完成，但本窗和屏幕中心都没有读到可访问控件',
                         '界面指挥会退回键鼠模式；确认“设置 → 辅助功能 → 视觉效果”里允许动画/无障碍',
                         check_id='uia', state='degraded')
        point = _screen_center()
        found = service.from_point(*point)
        element = found.get('element') or {}
        if not (element.get('type') or element.get('name')):
            return _untested('UI 自动化（UIA）',
                             '真实调用 FromPoint 成功，但该坐标没有可访问控件（无界面自检时正常）',
                             '在图形界面启动时会对本窗控件复测', 'uia')
        return _item('UI 自动化（UIA）', 'ok',
                     '真实 FromPoint(%d,%d) 读到：%s / %s'
                     % (point[0], point[1], element.get('type') or '?', element.get('name') or ''),)
    except Exception as exc:
        return _item('UI 自动化（UIA）', 'warn',
                     'UIA 真实读取失败：%s: %s' % (type(exc).__name__, str(exc)[:140]),
                     '界面指挥会退回键鼠模式；重试该项，仍失败请重启咕嘎',
                     check_id='uia', state='degraded', error=str(exc)[:300])


def _uia_target_check(ctx=None):
    """对真实运行的目标软件（豆包/WorkBuddy/JoyCode）做 UIA 读取，而不是只测自检窗。"""
    targets = _candidate_windows()
    if not targets:
        return _untested('UI 自动化（目标软件可读）',
                         '豆包 / WorkBuddy / JoyCode 都没有在运行，无法实测目标软件控件树',
                         '打开目标软件后，在启动自检里点“重试”即可实测', 'uia_target')
    try:
        from core.uia_service import shared_service
        service = shared_service()
    except Exception as exc:
        return _item('UI 自动化（目标软件可读）', 'warn',
                     'UIA 服务真实初始化失败：%s' % type(exc).__name__,
                     '界面指挥会退回键鼠模式；重启咕嘎后重试',
                     check_id='uia_target', state='degraded', error=str(exc)[:200])
    reports = []
    best = None
    for hwnd, name, title in targets[:3]:
        try:
            snapshot = service.call('snapshot', hwnd, timeout=6.0, limit=80,
                                    time_budget=3.0)
            elements = snapshot.get('elements') or []
            editable = 0
            pattern_hits = 0
            for element in elements:
                kind = str(element.get('type') or '')
                rect = element.get('rect') or {}
                if kind in ('Edit', 'TextEdit', 'ComboBox', 'Document'):
                    try:
                        if float(rect.get('right', 0)) > float(rect.get('left', 0)):
                            editable += 1
                    except Exception:
                        pass
                for pattern in element.get('patterns') or []:
                    if pattern in ('ValuePattern', 'TextPattern'):
                        pattern_hits += 1
            row = {'app': name, 'title': title[:30], 'hwnd': hwnd,
                   'elements': len(elements), 'editable': editable,
                   'value_text': pattern_hits}
            reports.append(row)
            if best is None or (editable + pattern_hits) > (best['editable'] + best['value_text']):
                best = row
        except Exception as exc:
            reports.append({'app': name, 'title': title[:30], 'hwnd': hwnd,
                            'error': '%s: %s' % (type(exc).__name__, str(exc)[:80])})
    if not best:
        return _item('UI 自动化（目标软件可读）', 'warn',
                     '真实读取目标窗口全部失败：%s' % reports,
                     '目标软件只能用纯视觉模式；OCR 不可用时无法派单',
                     check_id='uia_target', state='degraded', value=reports)
    detail = ('真实读取 %s：%d 个控件，可编辑 %d 个，Value/TextPattern %d 个'
              % (best['title'], best['elements'], best['editable'], best['value_text']))
    if best['editable'] >= 1 or best['value_text'] >= 1:
        return _item('UI 自动化（目标软件可读）', 'ok', detail, check_id='uia_target',
                     value=reports)
    return _item('UI 自动化（目标软件可读）', 'warn',
                 detail + '；目标软件没有暴露可编辑控件/文本模式',
                 '豆包等只能走纯视觉模式；请打开目标软件的“辅助功能/无障碍”开关，'
                 'OCR 不可用时派单会受影响',
                 check_id='uia_target', state='degraded', value=reports)


def _screen_center():
    try:
        import ctypes
        user32 = ctypes.windll.user32
        return int(user32.GetSystemMetrics(0) // 2), int(user32.GetSystemMetrics(1) // 2)
    except Exception:
        return 640, 360


def _clipboard_check(ctx=None):
    token = ('咕嘎剪贴板自检 2026-%s\n第二行：中文与空格 都要一致\n%s'
             % (int(time.time()), '长文本' * 80))
    try:
        import win32clipboard as clip
        import win32con
        previous = ''
        try:
            clip.OpenClipboard()
            try:
                if clip.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                    previous = clip.GetClipboardData(win32con.CF_UNICODETEXT) or ''
            finally:
                clip.CloseClipboard()
        except Exception:
            previous = ''
        opened = False
        for _ in range(5):
            try:
                clip.OpenClipboard()
                opened = True
                break
            except Exception:
                time.sleep(0.05)
        if not opened:
            raise RuntimeError('OpenClipboard 重试 5 次仍被占用')
        try:
            clip.EmptyClipboard()
            clip.SetClipboardData(win32con.CF_UNICODETEXT, token)
        finally:
            clip.CloseClipboard()
        time.sleep(0.05)
        clip.OpenClipboard()
        try:
            back = clip.GetClipboardData(win32con.CF_UNICODETEXT) if \
                clip.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT) else ''
        finally:
            clip.CloseClipboard()
        try:
            if previous:
                clip.OpenClipboard()
                try:
                    clip.EmptyClipboard()
                    clip.SetClipboardData(win32con.CF_UNICODETEXT, previous)
                finally:
                    clip.CloseClipboard()
        except Exception:
            pass
        if back == token:
            return _item('剪贴板（Unicode 回环）', 'ok',
                         '真实写入并读回一致：%d 字符（含中文/换行/长文本）' % len(back),
                         check_id='clipboard', value=len(back))
        return _item('剪贴板（Unicode 回环）', 'warn',
                     '真实写入后读回不一致：期望 %d 字符，实际 %d 字符'
                     % (len(token), len(back)),
                     '豆包派单的粘贴/复制会失败；关掉占用剪贴板的软件（远程桌面/剪贴板历史/DLP）后重试',
                     check_id='clipboard', state='degraded', value=len(back))
    except Exception as exc:
        return _item('剪贴板（Unicode 回环）', 'warn',
                     '真实写入失败：%s: %s' % (type(exc).__name__, str(exc)[:120]),
                     '豆包派单的粘贴/复制会失败；关闭剪贴板管理工具后重试该项',
                     check_id='clipboard', state='degraded', error=str(exc)[:200])


def _temp_check(ctx=None):
    import shutil
    import tempfile
    temp = Path(tempfile.gettempdir())
    try:
        temp.mkdir(parents=True, exist_ok=True)
        probe = temp / ('咕嘎 temp 探针-%d.bin' % os.getpid())
        probe.write_bytes(b'x' * (1024 * 1024))
        size = probe.stat().st_size
        probe.unlink()
        free_gb = round(shutil.disk_usage(str(temp)).free / 1024 ** 3, 1)
        if size != 1024 * 1024:
            raise RuntimeError('写入大小不符')
        if free_gb < 2:
            return _item('%TEMP% 可写与空间', 'warn',
                         '真实写入 1MB 成功，但剩余空间只有 %.1fGB：%s' % (free_gb, temp),
                         '清理磁盘或把 TEMP 指到空间充足的目录后重启咕嘎',
                         check_id='temp', state='degraded', value=free_gb,
                         missing='2GB 以上剩余空间', where=str(temp), restart_required=True)
        return _item('%TEMP% 可写与空间', 'ok',
                     '真实写入 1MB→读回大小→删除成功；路径 %s；剩余 %.1fGB'
                     % (temp, free_gb), check_id='temp', value=free_gb)
    except Exception as exc:
        return _item('%TEMP% 可写与空间', 'fatal',
                     '真实写入 1MB 失败：%s: %s' % (type(exc).__name__, str(exc)[:120]),
                     '截图和识字无法落盘：清理磁盘，或用系统属性 → 环境变量把 TEMP 指到可写目录后重启咕嘎',
                     check_id='temp', state='fail', missing='可写的 %TEMP%',
                     where='系统属性 → 高级 → 环境变量 → TEMP', restart_required=True,
                     error=str(exc)[:200])


def _long_path_check(ctx=None):
    import shutil
    import tempfile
    root = Path(tempfile.gettempdir()) / ('guga-long-%d' % os.getpid())
    deep = root
    try:
        while len(str(deep)) < 290:
            deep = deep / '目录_长路径_测试_abcdefghij'
        target = deep / 'probe.txt'
        deep.mkdir(parents=True, exist_ok=True)
        target.write_text('ok', encoding='utf-8')
        back = target.read_text(encoding='utf-8')
        length = len(str(target))
        if back != 'ok':
            raise RuntimeError('读回内容不一致')
        return _item('长路径（>260 字符）', 'ok',
                     '真实创建/写入/读回成功：路径 %d 字符' % length,
                     check_id='long_path', value=length)
    except Exception as exc:
        return _item('长路径（>260 字符）', 'warn',
                     '真实创建失败（%s）：路径 %d 字符' % (type(exc).__name__, len(str(target))),
                     '请把安装包/工作目录放到更短路径（如 C:\\GUGA）；'
                     '或让 IT 开启“启用 Win32 长路径”策略',
                     check_id='long_path', state='degraded',
                     missing='长路径支持或更短的安装目录', where='C:\\GUGA',
                     restart_required=True, error=str(exc)[:200])
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _screenshot_check(ctx=None):
    """真实整屏截图（ImageGrab all_screens）并检查黑屏/纯色。"""
    try:
        from PIL import ImageGrab
        import numpy as np
        shot = ImageGrab.grab(all_screens=True)
        width, height = shot.size
        array = np.asarray(shot.convert('RGB'))
        if array.size == 0:
            raise RuntimeError('截图内容为空')
        colors = len(np.unique(array.reshape(-1, 3), axis=0))
        dark = float((array.max(axis=2) < 12).mean())
        bright = float((array.min(axis=2) > 245).mean())
        if width * height < 640 * 480 or colors < 20 or dark > 0.98 or bright > 0.98:
            return _item('整屏截图（非黑/非纯色）', 'warn',
                         '真实截屏 %dx%d，颜色 %d 种，近黑 %.1f%%，近白 %.1f%%'
                         % (width, height, colors, dark * 100, bright * 100),
                         '锁屏/远程会话未连接桌面时会出现；解锁或断开远程后重试该项',
                         check_id='screenshot', state='degraded',
                         value=dict(size=[width, height], colors=colors))
        return _item('整屏截图（非黑/非纯色）', 'ok',
                     '真实整屏截图 %dx%d：颜色 %d 种，近黑 %.1f%%（非全黑）'
                     % (width, height, colors, dark * 100),
                     check_id='screenshot',
                     value=dict(size=[width, height], colors=colors, dark=round(dark, 3)))
    except Exception as exc:
        return _item('整屏截图（非黑/非纯色）', 'warn',
                     '真实截屏失败：%s: %s' % (type(exc).__name__, str(exc)[:120]),
                     '豆包/界面指挥的截图会失败；解锁桌面或退出远程会话后重试该项',
                     check_id='screenshot', state='degraded', error=str(exc)[:200])


def _candidate_windows():
    """真实枚举可视顶层窗口，按进程名筛出目标软件。返回 [(hwnd, 进程名, 标题)]."""
    import win32gui
    import win32process
    import psutil
    targets = ('doubao', 'workbuddy', 'joycode')
    found = []
    for hwnd, title in _all_windows(win32gui):
        try:
            if str(title).strip() == '':
                continue
            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
            name = (psutil.Process(pid).name() or '').lower()
        except Exception:
            continue
        for target in targets:
            if target in name:
                found.append((hwnd, name, str(title)))
                break
    return found


def _all_windows(win32gui):
    rows = []

    def visit(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            rows.append((hwnd, win32gui.GetWindowText(hwnd)))
        return True

    win32gui.EnumWindows(visit, None)
    return rows


def _printwindow_check(ctx=None):
    """PrintWindow(PW_RENDERFULLCONTENT) 真实渲染目标/本窗，检查是否全黑/全白。"""
    import ctypes
    import win32gui
    import numpy as np
    from ctypes import wintypes
    from PIL import Image
    candidates = []
    hwnd = int(_ctx(ctx, 'hwnd') or 0)
    if hwnd and win32gui.IsWindow(hwnd):
        candidates.append((hwnd, '自检窗'))
    candidates.extend((row[0], row[1]) for row in _candidate_windows()[:2])
    if not candidates:
        return _untested('PrintWindow（窗口自绘截图）',
                         '没有可渲染的窗口（无界面自检且目标软件未运行时正常）',
                         '图形界面启动时会对自检窗复测', 'printwindow')
    hwnd, title = candidates[0]
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width, height = right - left, bottom - top
        if width < 200 or height < 200:
            raise RuntimeError('窗口尺寸异常 %dx%d' % (width, height))
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        screen = user32.GetDC(0)
        memory = gdi32.CreateCompatibleDC(screen)
        bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
        try:
            gdi32.SelectObject(memory, bitmap)
            ok = user32.PrintWindow(hwnd, memory, 2)
            if not ok:
                raise RuntimeError('PrintWindow 返回 0')

            class _BIH(ctypes.Structure):
                _fields_ = [('biSize', wintypes.DWORD), ('biWidth', wintypes.LONG),
                            ('biHeight', wintypes.LONG), ('biPlanes', wintypes.WORD),
                            ('biBitCount', wintypes.WORD), ('biCompression', wintypes.DWORD),
                            ('biSizeImage', wintypes.DWORD), ('biXPelsPerMeter', wintypes.LONG),
                            ('biYPelsPerMeter', wintypes.LONG), ('biClrUsed', wintypes.DWORD),
                            ('biClrImportant', wintypes.DWORD)]

            header = _BIH()
            header.biSize = ctypes.sizeof(_BIH)
            header.biWidth = width
            header.biHeight = -height
            header.biPlanes = 1
            header.biBitCount = 32
            buffer = (ctypes.c_ubyte * (width * height * 4))()
            if not gdi32.GetDIBits(memory, bitmap, 0, height, buffer,
                                   ctypes.byref(header), 0):
                raise RuntimeError('GetDIBits 失败')
            shot = Image.frombuffer('RGB', (width, height), buffer, 'raw', 'BGRX', 0, 1)
            array = np.asarray(shot)
            colors = len(np.unique(array.reshape(-1, 3), axis=0))
            dark = float((array.max(axis=2) < 12).mean())
            white = float((array.min(axis=2) > 245).mean())
            if colors < 3 and (dark > 0.98 or white > 0.98):
                raise RuntimeError('渲染结果只有 %d 种颜色（全黑/全白）' % colors)
            return _item('PrintWindow（窗口自绘截图）', 'ok',
                         '真实 PrintWindow %s：%dx%d，颜色 %d 种，近黑 %.1f%%'
                         % (title[:24], width, height, colors, dark * 100),
                         check_id='printwindow', value=dict(title=title[:24], colors=colors,
                                                            dark=round(dark, 3)))
        finally:
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(memory)
            user32.ReleaseDC(0, screen)
    except Exception as exc:
        return _item('PrintWindow（窗口自绘截图）', 'warn',
                     '真实 PrintWindow 失败（%s）：%s' % (title[:24], type(exc).__name__),
                     '窗口自绘模式退化为置顶截图；关掉遮挡/最小化后重试该项',
                     check_id='printwindow', state='degraded', error=str(exc)[:200])


def _sendinput_check(ctx=None):
    """真实调用 SendInput 发一个无效但合法的空事件，检查返回值。"""
    import ctypes
    from ctypes import wintypes

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG),
                    ('mouseData', wintypes.DWORD), ('dwFlags', wintypes.DWORD),
                    ('time', wintypes.DWORD), ('dwExtraInfo', ctypes.POINTER(ctypes.c_ulong))]

    class INPUT(ctypes.Structure):
        class _I(ctypes.Union):
            _fields_ = [('mi', MOUSEINPUT)]
        _anonymous_ = ('_I',)
        _fields_ = [('type', wintypes.DWORD), ('_I', _I)]

    try:
        # 用私有 DLL 实例承载这份 INPUT 签名：ctypes.windll.user32 是进程级缓存对象，
        # 改它的 argtypes 会污染派单侧 computer_control._send_keys（现场 q 包豆包第 1/2 步
        # 就是这样抛 ArgumentError 的）。自检不能改任何全局签名。
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        user32.SendInput.restype = wintypes.UINT
        item = INPUT()
        item.type = 0                      # INPUT_MOUSE + dx=dy=0 + flags=0：不产生实际输入
        item.mi.dwFlags = 0
        accepted = int(user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(INPUT)))
        if accepted == 1:
            return _item('SendInput 被系统接受', 'ok',
                         '真实调用 SendInput 返回 1（无副作用空事件）', check_id='sendinput',
                         value=accepted)
        return _item('SendInput 被系统接受', 'warn',
                     '真实调用 SendInput 返回 %d（被 UIPI/安全软件拦截）' % accepted,
                     '键鼠指挥会失效：退出安全软件的“防注入”或让 IT 放行咕嘎；'
                     '以管理员身份运行时也会拦普通权限窗口',
                     check_id='sendinput', state='degraded', value=accepted)
    except Exception as exc:
        return _item('SendInput 被系统接受', 'warn',
                     '真实调用 SendInput 抛错：%s: %s' % (type(exc).__name__, str(exc)[:120]),
                     '键鼠指挥会失效；重启咕嘎后重试该项',
                     check_id='sendinput', state='degraded', error=str(exc)[:200])


def _window_enum_check(ctx=None):
    try:
        import win32gui
        import win32process
        own_pid = os.getpid()
        rows = []
        total = [0]

        def visit(hwnd, _):
            total[0] += 1
            try:
                if win32gui.IsWindowVisible(hwnd):
                    _pid = win32process.GetWindowThreadProcessId(hwnd)[1]
                    if _pid == own_pid:
                        rows.append((hwnd, win32gui.GetWindowText(hwnd)))
            except Exception:
                pass
            return True

        win32gui.EnumWindows(visit, None)
        hwnd = int(_ctx(ctx, 'hwnd') or 0)
        if hwnd and win32gui.IsWindow(hwnd) and hwnd not in [row[0] for row in rows]:
            rows.append((hwnd, win32gui.GetWindowText(hwnd)))
        targets = _candidate_windows()
        detail = ('真实枚举 %d 个顶层窗口；本进程 %d 个；目标软件 %d 个%s'
                  % (total[0], len(rows), len(targets),
                     ('（%s）' % '、'.join('%s:%s' % (name, title[:16])
                                       for _h, name, title in targets[:3]) if targets else '')))
        if not rows:
            return _untested('窗口枚举（EnumWindows）', detail + '；本进程没有可见窗口',
                             '图形界面启动时会拿到自检窗本身', 'window_enum')
        return _item('窗口枚举（EnumWindows）', 'ok', detail, check_id='window_enum',
                     value=dict(total=total[0], own=len(rows), targets=len(targets)))
    except Exception as exc:
        return _item('窗口枚举（EnumWindows）', 'warn',
                     'EnumWindows 真实调用失败：%s' % type(exc).__name__,
                     '界面指挥找不到目标窗口；重启咕嘎后重试该项',
                     check_id='window_enum', state='degraded', error=str(exc)[:200])


def _window_focus_check(ctx=None):
    """真实 WindowFromPoint + 前台窗口归属 + GetWindowRect，验证“能定位并激活窗口”。"""
    try:
        import win32gui
        import win32process
        import psutil
        hwnd = int(_ctx(ctx, 'hwnd') or 0)
        if not hwnd or not win32gui.IsWindow(hwnd):
            candidates = _candidate_windows()
            hwnd = candidates[0][0] if candidates else 0
        if not hwnd:
            return _untested('窗口定位与前台归属',
                             '没有可测试的窗口（无界面自检且目标软件未运行时正常）',
                             '图形界面启动时会对自检窗复测', 'window_focus')
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if right - left < 200 or bottom - top < 200:
            raise RuntimeError('窗口尺寸异常 %dx%d' % (right - left, bottom - top))
        import ctypes
        from ctypes import wintypes
        point = wintypes.POINT((left + right) // 2, (top + bottom) // 2)
        hit = int(ctypes.windll.user32.WindowFromPoint(point) or 0)
        root = win32gui.GetAncestor(hit, 2) if hit else 0
        foreground = win32gui.GetForegroundWindow()
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        try:
            name = psutil.Process(pid).name()
        except Exception:
            name = '未知进程'
        visible = bool(win32gui.IsWindowVisible(hwnd))
        detail = ('WindowFromPoint 命中 hwnd=%s（根窗口 %s）；前台 hwnd=%s；'
                  '目标窗口 pid=%s(%s) 可见=%s 尺寸 %dx%d'
                  % (hit, root, foreground, pid, name, visible,
                     right - left, bottom - top))
        if hit and (root == hwnd or hit == hwnd):
            return _item('窗口定位与前台归属', 'ok', detail, check_id='window_focus',
                         value=dict(hwnd=hwnd, foreground=foreground, pid=pid))
        return _item('窗口定位与前台归属', 'warn', detail + '；坐标命中的不是目标窗口（被遮挡？）',
                     '派单前请把目标窗口从遮挡处移开；点“重试”可再测',
                     check_id='window_focus', state='degraded', value=dict(hwnd=hwnd))
    except Exception as exc:
        return _item('窗口定位与前台归属', 'warn',
                     '真实定位失败：%s: %s' % (type(exc).__name__, str(exc)[:120]),
                     '界面指挥无法定位目标窗口；重启咕嘎后重试该项',
                     check_id='window_focus', state='degraded', error=str(exc)[:200])


def _mcp_handshake_check(ctx=None):
    """真实拉起自身 --guga-mcp 子进程，走 initialize + tools/list。"""
    import threading
    command = [sys.executable, str(ROOT / 'tools' / 'comp_entry.py'), '--guga-mcp']
    if frozen():
        command = [sys.executable, '--guga-mcp']
    env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONPATH=str(ROOT))
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, encoding='utf-8',
                                   cwd=str(ROOT), creationflags=_NO_WINDOW, env=env)
    except Exception as exc:
        return _item('MCP 握手（自身 --guga-mcp）', 'warn',
                     '真实拉起失败：%s: %s' % (type(exc).__name__, str(exc)[:120]),
                     '接入面板会显示“未验证”；重新解压完整压缩包后重试',
                     check_id='mcp', state='degraded', error=str(exc)[:200])

    lines, err_lines = [], []

    def pump(stream, sink):
        try:
            for line in stream:
                sink.append(line)
        except Exception:
            pass

    threading.Thread(target=pump, args=(process.stdout, lines), daemon=True).start()
    threading.Thread(target=pump, args=(process.stderr, err_lines), daemon=True).start()

    def send(payload):
        process.stdin.write(json.dumps(payload) + '\n')
        process.stdin.flush()

    def wait_reply(magic, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            while lines:
                try:
                    reply = json.loads(lines.pop(0))
                except ValueError:
                    continue
                if reply.get('id') == magic:
                    return reply
            time.sleep(0.05)
        stderr = (''.join(err_lines) or '').strip().splitlines()
        raise RuntimeError('%s 秒内没有响应%s' % (seconds, ('；stderr: ' + stderr[-1][:120]) if stderr else ''))

    try:
        send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
              'params': {'protocolVersion': '2024-11-05', 'capabilities': {},
                         'clientInfo': {'name': 'guga-selfcheck', 'version': '1.0'}}})
        reply = wait_reply(1, 15)
        if 'error' in reply:
            raise RuntimeError('initialize 返回错误：%s' % reply['error'])
        send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        send({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}})
        reply = wait_reply(2, 10)
        if 'error' in reply:
            raise RuntimeError('tools/list 返回错误：%s' % reply['error'])
        tools = (reply.get('result') or {}).get('tools') or []
        names = [str(tool.get('name')) for tool in tools]
        if len(names) >= 5:
            return _item('MCP 握手（自身 --guga-mcp）', 'ok',
                         'initialize + tools/list 真实通过：%d 个工具（%s）'
                         % (len(names), '、'.join(names)),
                         check_id='mcp', value=names)
        return _item('MCP 握手（自身 --guga-mcp）', 'warn',
                     '握手成功但只列出 %d 个工具（期望 5 个）：%s'
                     % (len(names), '、'.join(names) or '无'),
                     '接入面板可能不完整；重新解压完整压缩包后重试',
                     check_id='mcp', state='degraded', value=names)
    except Exception as exc:
        return _item('MCP 握手（自身 --guga-mcp）', 'warn',
                     '真实握手失败：%s: %s' % (type(exc).__name__, str(exc)[:160]),
                     '软件接入会显示未验证；点“重试”，仍失败请重启咕嘎或重新解压安装包',
                     check_id='mcp', state='degraded', error=str(exc)[:240])
    finally:
        try:
            process.terminate()
            process.wait(timeout=2)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


def _model_endpoint_check(ctx=None):
    """只做 DNS/TCP 轻探测，绝不带 Key 发正文。"""
    try:
        from core import model_capability as mc
        cfg = mc.load_config()
    except Exception as exc:
        return _item('模型端点连通性（DNS/TCP）', 'warn',
                     '真实读取配置失败：%s' % type(exc).__name__,
                     '在右键「模型设置」里重新填 base_url 后重试',
                     check_id='model_endpoint', state='degraded', error=str(exc)[:200])
    url = str(cfg.get('base_url') or '').strip()
    if not url.startswith('http'):
        return _untested('模型端点连通性（DNS/TCP）',
                         '还没有配置 base_url（首次启动的正常状态），未做连通性探测',
                         '配置模型后再重试该项', 'model_endpoint')
    parts = urlsplit(url)
    host = parts.hostname or ''
    if not host:
        return _item('模型端点连通性（DNS/TCP）', 'warn', 'base_url 里解析不到主机名：%s' % url[:80],
                     '在右键「模型设置」里改写成完整地址（含 https://）',
                     check_id='model_endpoint', state='degraded', missing='主机名',
                     where='右键 → 模型设置 → base_url')
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    started = time.perf_counter()
    try:
        socket.create_connection((host, port), timeout=3.0).close()
        elapsed = int((time.perf_counter() - started) * 1000)
        return _item('模型端点连通性（DNS/TCP）', 'ok',
                     'DNS 解析 + TCP 连接 %s:%s 真实成功（%sms，未发送任何 Key/正文）'
                     % (host, port, elapsed),
                     check_id='model_endpoint', value=dict(host=host, port=port, ms=elapsed))
    except Exception as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        return _item('模型端点连通性（DNS/TCP）', 'warn',
                     'DNS/TCP 真实连接失败：%s:%s（%sms，%s）'
                     % (host, port, elapsed, type(exc).__name__),
                     '检查网络/代理后重试；模型不可用不影响本地界面自动化，但对话会失败',
                     check_id='model_endpoint', state='degraded',
                     missing='可连通的模型端点', where=url[:100], error=str(exc)[:200])


def _tools_check(ctx=None):
    try:
        from core import tool_advice
        missing = tool_advice.missing_tools()
        installed = list(tool_advice.installed_names())
    except Exception as exc:
        return _item('外部软件', 'warn', '真实探测失败：%s' % type(exc).__name__,
                     '不影响启动；如需派单，请手动确认 JoyCode / WorkBuddy / 豆包 是否安装',
                     check_id='external_software', state='degraded', error=str(exc)[:200])
    # 与接入面板共用同一份真实握手证据：豆包是 HTTP 连接器，安装路径探测不到
    # 不代表没接入；只要客户端真的 initialize 过，就不能再报“未发现/未接入”。
    connected = {}
    handshaken = {}
    try:
        from core.adapters import mcp_channels
        for app_id, label in (('doubao', '豆包'), ('workbuddy', 'WorkBuddy'), ('joycode', 'JoyCode')):
            try:
                status = mcp_channels.channel_status(app_id)
            except Exception:
                continue
            state = status.get('state')
            if state == 'connected':
                connected[label] = status
            elif state == 'client_handshake_no_tools':
                # 已收到客户端 initialize、但还没 tools/list。接入面板会如实写
                # “已握手、还不能判定已接入”，自检不能反过来说“缺少/未接入”。
                handshaken[label] = status
    except Exception:
        pass
    known = dict(connected)
    known.update(handshaken)
    if known:
        missing = [tool for tool in missing if tool.get('name') not in known]
        for label in connected:
            if not any(label in str(name) for name in installed):
                installed.append(label + '（MCP 真实握手）')
        for label in handshaken:
            if not any(label in str(name) for name in installed):
                installed.append(label + '（MCP 已握手，未枚举工具）')
    names = '、'.join('%s%s' % (tool['name'], '（必装）' if tool['badge'] == '必装' else '')
                      for tool in missing)
    detail = '静态预判（只查文件/注册表，不算实测）：发现安装痕迹 %s；%s' % (
        '、'.join(installed) or '无',
        ('缺少 ' + names) if names else '没有发现缺失项')
    if connected:
        detail += '；MCP 真实握手已确认：%s' % '、'.join(connected)
        unidentified = [label for label, status in connected.items()
                        if status.get('client_unidentified')]
        if unidentified:
            detail += ('；其中 %s 的客户端没有自报为可识别名称，按“HTTP 端点当前只给豆包使用”'
                       '记录为已接入；若同时连接了其他 MCP 客户端，请以豆包界面为准'
                       % '、'.join(unidentified))
    if handshaken:
        detail += ('；%s 已收到 MCP initialize 但尚未 tools/list，暂不判定为已接入；'
                   '请在软件里发起一次调用，看到 5 个工具后会自动变为已接入'
                   % '、'.join(handshaken))
    hint = ('自检不启动第三方软件，因此只能算“未测”；派单时会真启动并核对窗口。'
            + ('；'.join('%s：%s' % (tool['name'], tool['install']) for tool in missing)))
    return _untested('外部软件', detail, hint, 'external_software')


def _model_check(ctx=None):
    try:
        from core import model_capability as mc
    except Exception as exc:
        return _item('模型准入', 'warn', '能力自检模块真实导入失败：%s' % type(exc).__name__,
                     '不影响启动；但模型能力未知，界面任务可能中途失败',
                     check_id='model_tier', state='degraded', error=str(exc)[:200])
    cfg = mc.load_config()
    model = str(cfg.get('model', '')).strip()
    if not model or 'YOUR_' in model or not str(cfg.get('api_key', '')).strip():
        return _item('模型准入', 'info', '真实读取配置：还没有配置对话模型（首次启动的正常状态）',
                     '配置真实模型后会自动判定档位：推荐档 / 最低档 / 不适合',
                     check_id='model_tier', state='untested')
    try:
        result = mc.assess(cfg, use_cache=True, probe_if_missing=False)
    except Exception as exc:
        return _item('模型准入', 'warn', '模型能力评估失败：%s' % type(exc).__name__,
                     '不影响启动；可运行 python -m core.model_capability --probe 复查',
                     check_id='model_tier', state='degraded', error=str(exc)[:200])
    prov = result.get('provenance') or {}
    if result.get('probe_source') == '未探测':
        return _untested('模型准入',
                         '%s 能力未在本次自检中复测（本机无有效缓存）' % (result['model'] or '模型'),
                         '启动自检不发请求以免扣费；在右键「模型设置 → 测试连接 / 重新探测档位」里确认',
                         'model_tier')
    return _untested('模型准入',
                     '%s 属%s；结论来自缓存 %s · %s（本次未重新请求）'
                     % (result['model'], result['tier_name'], prov.get('host') or '本机',
                        prov.get('at') or '时间未知'),
                     '要重新实测请用右键「模型设置 → 重新探测档位」', 'model_tier')


# ---------------------------------------------------------------- 注册表

# 自检项版本：打包时会断言“包内 --selfcheck-json 的项数/版本 == 源码这里的值”，
# 防止再出现“源码改了、包里还是旧自检”的事故。前缀必须与包版本尾字母一致
# （20261110s 对应 s-1，20261110s2 对应 s2-1，20261110s3 对应 s3-2，
#  20261110s4 对应 s4-1，20261110t 对应 t-1，20261110u 对应 u-1，
#  20261110v 对应 v-1，20261110w 对应 w-1，20261110x 对应 x-1，20261110y 对应 y-1，
#  20261110z 对应 z-1，20261110aa 对应 aa-1，20261110ab 对应 ab-1，
#  20261110ac 对应 ac-1），构建闸门会校验。
CHECK_VERSION = 'ac-1'

_CHECKS = (
    ('old_processes', '旧进程扫描', _old_process_check, False),
    ('windows_version', 'Windows 版本', _os_check, False),
    ('runtime', '运行库', _runtime_check, False),
    ('imports', '内置依赖（真实 import）', _imports_check, True),
    ('display', '屏幕缩放', _display_check, False),
    ('config', '配置与运行目录', _config_check, False),
    ('config_io', '配置写入回环（中文/空格路径）', _config_io_check, True),
    ('ffmpeg', 'ffmpeg（真执行 -version）', _ffmpeg_check, True),
    ('vosk', '离线语音模型（真加载）', _vosk_check, True),
    ('mic', '麦克风（真实开流）', _mic_check, True),
    ('font', '中文字体（真实渲染）', _font_check, True),
    ('ocr', '系统 OCR（-File + 真识别中文）', _ocr_check, True),
    ('uia', 'UI 自动化（UIA）', _uia_check, True),
    ('uia_target', 'UI 自动化（目标软件可读）', _uia_target_check, True),
    ('clipboard', '剪贴板（写入→读回）', _clipboard_check, True),
    ('screenshot', '截图（非全黑像素）', _screenshot_check, True),
    ('printwindow', '后台截图 PrintWindow', _printwindow_check, True),
    ('window_enum', '窗口枚举（EnumWindows）', _window_enum_check, True),
    ('window_focus', '窗口定位与前台归属', _window_focus_check, True),
    ('sendinput', '模拟输入 SendInput', _sendinput_check, False),
    ('temp', '%TEMP% 可写与空间', _temp_check, False),
    ('long_path', '长路径（>260 字符）', _long_path_check, True),
    ('pptx', 'PPT 生成（python-pptx）', _pptx_check, True),
    ('mcp', 'MCP 握手（自身 --guga-mcp）', _mcp_handshake_check, True),
    ('model_endpoint', '模型端点连通性（DNS/TCP）', _model_endpoint_check, True),
    ('external_software', '外部软件', _tools_check, True),
    ('model_tier', '模型准入', _model_check, True),
)

_CHECK_MAP = {check_id: (name, func) for check_id, name, func, _slow in _CHECKS}
_SLOW_IDS = {check_id for check_id, _name, _func, slow in _CHECKS if slow}


def check_count() -> int:
    return len(_CHECKS)


def _run_one(check_id, ctx):
    name, func = _CHECK_MAP[check_id]
    started = time.perf_counter()
    try:
        item = func(ctx)
    except Exception as exc:
        item = _item(name, 'warn', '检查函数真实抛错：%s: %s' % (type(exc).__name__, str(exc)[:140]),
                     '该项结果未知；点“重试”，仍失败请把本行发给开发者',
                     check_id=check_id, state='degraded', error=str(exc)[:300])
    item['check_id'] = item.get('check_id') or check_id
    item['name'] = item.get('name') or name
    item['elapsed_ms'] = int((time.perf_counter() - started) * 1000)
    return item


def run_single(check_id, context=None):
    """失败项单独重试：只跑这一项，返回同结构结果。"""
    return _run_one(check_id, context or {})


def iter_checks(display_dpi=None, old_processes=None, window_hwnd=0):
    """逐项产出检查结果（dict）。慢项并行跑，其余立即返回。"""
    context = dict(dpi=display_dpi, old=old_processes, hwnd=int(window_hwnd or 0))
    pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix='guga-check')
    futures = {check_id: pool.submit(_run_one, check_id, context)
               for check_id in _CHECK_MAP if check_id in _SLOW_IDS}
    try:
        for check_id, name, func, slow in _CHECKS:
            if slow:
                try:
                    yield futures[check_id].result(timeout=30)
                except Exception as exc:
                    yield _item(name, 'warn', '检查超时或失败：%s' % type(exc).__name__,
                                '该项结果未知；点“重试”单独复查',
                                check_id=check_id, state='degraded', error=str(exc)[:200])
            else:
                yield _run_one(check_id, context)
    finally:
        pool.shutdown(wait=False)


def summarize(items):
    """summary 只按 items 的 state 聚合；level 计数放在 levels 子字典里，两套不混。

    硬要求：len(items) == 各 state 之和；不成立时 consistent=False，
    --selfcheck-json 会以非 0 退出，构建闸门也会拒绝产出。
    """
    states = {'pass': 0, 'degraded': 0, 'fail': 0, 'untested': 0}
    levels = {'ok': 0, 'info': 0, 'warn': 0, 'fatal': 0, 'untested': 0}
    rows = list(items or [])
    for one in rows:
        data = one if isinstance(one, dict) else {}
        state = str(data.get('state') or 'untested')
        if state not in states:
            state = 'untested'
        states[state] += 1
        level = str(data.get('level') or 'info')
        if level not in levels:
            level = 'info'
        levels[level] += 1
    total = len(rows)
    state_total = sum(states.values())
    level_total = sum(levels.values())
    counts = dict(states)
    counts.update(total=total, state_total=state_total, level_total=level_total,
                  consistent=(state_total == total and level_total == total),
                  levels=levels)
    return counts


def has_fatal(items):
    return any(one.get('level') == 'fatal' for one in items or [])
