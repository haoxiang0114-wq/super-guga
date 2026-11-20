# -*- coding: utf-8 -*-
"""Windows OCR 的统一入口：真实探测 + 失败原因归类 + 可照做的修复说明。

约定：
  · 任何“OCR 能否用”的结论都必须来自真实调用（创建引擎 + 真识别一张图）；
  · 失败不抛裸 RuntimeError，而是 ``OcrUnavailableError``（code=ocr_unavailable），
    带 missing / where / next_action / raw（PowerShell 原始错误）；
  · 自检、豆包适配器、截图问答共用这里的探测，避免各处判断口径不一致。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)

WHERE_LANGUAGE = (
    '以管理员身份打开 PowerShell 执行：'
    'Add-WindowsCapability -Online -Name Language.OCR~~~zh-CN ；'
    '或在图形界面（Win10/11）：设置 → 应用 → 可选功能 → 添加功能 → 搜索'
    '“中文(简体) 光学字符识别”并安装。'
    '国内网络下载慢时用离线包：先取得 Language.OCR~~~zh-CN 的 .cab/.appx（同版本系统），'
    '再执行 DISM /Online /Add-Capability /CapabilityName:Language.OCR~~~zh-CN '
    '/Source:<离线包所在目录>（加 /LimitAccess 可强制不联网）；'
    '装完重启咕嘎（不用重启电脑）')
WHERE_WINRT = ('系统缺少 WinRT 组件：设置 → 应用 → 可选功能 → 添加“Windows 功能体验包 / '
               'Windows 媒体功能包”，装完重启电脑')
WHERE_SCRIPT = '安装包不完整：请重新解压完整压缩包，不要把 exe 单独拷出来运行'
WHERE_TIMEOUT = '关掉占用 CPU 的程序后在自检里点“重试”；仍失败就重启咕嘎'
WHERE_POWERSHELL = ('找得到 powershell.exe 才能用系统 OCR：设置 → 应用 → 可选功能 → '
                    '添加“Windows PowerShell 2.0 引擎”后重启电脑')
WHERE_SCRIPT_BLOCKED = ('系统禁止运行 PowerShell 脚本（-ExecutionPolicy Restricted/AllSigned 或被 AppLocker 拦截）：'
                        '以管理员身份打开 PowerShell 运行 Set-ExecutionPolicy -Scope CurrentUser RemoteSigned，'
                        '或让 IT 把安装目录加入白名单')
WHERE_FONT = '系统缺少中文字体：设置 → 个性化 → 字体 → 添加“微软雅黑 / 黑体”，或从另一台电脑复制 msyh.ttc'

NEXT_LANGUAGE = ('先按“缺什么”里的路径装好中文 OCR 语言包并重启咕嘎；'
                 '在此之前豆包派单会自动降级走 UIA + 剪贴板，截图问答里的文字识别不可用')
NEXT_WINRT = '先补齐 Windows 功能体验包并重启电脑；在此之前豆包派单会降级走 UIA + 剪贴板'
NEXT_SCRIPT = '重新解压完整压缩包（确保 tools/windows_ocr.ps1 在 exe 同级目录）后再试'
NEXT_TIMEOUT = '在启动自检里单独重试该项；仍失败请重启咕嘎后再派单'
NEXT_POWERSHELL = '按提示装好 PowerShell 后重启电脑，再在启动自检里重试'
NEXT_SCRIPT_BLOCKED = ('按“去哪补”放开脚本执行策略后，在启动自检里点“重试”验证 -File 能跑；'
                       '在此之前豆包派单会降级走 UIA + 剪贴板（回执标注 ocr_unavailable）')
NEXT_FONT = ('按“去哪补”装好中文字体后，在启动自检里点“重试”；'
             '在此之前中文识别准确率不可信，豆包派单会降级走 UIA + 剪贴板')
NEXT_ACCURACY = ('中文识别准确率不足：按“去哪补”安装“中文(简体) 光学字符识别”语言包并重启咕嘎；'
                 '在此之前豆包派单会降级走 UIA + 剪贴板，界面文字校验不可信')

ACCURACY_GATE = 0.90


class OcrUnavailableError(RuntimeError):
    """Windows OCR 不可用（可降级，不应打断复合任务）。"""

    code = 'ocr_unavailable'

    def __init__(self, missing, where, next_action, raw='', kind='unknown', restart_required=True):
        self.missing = missing
        self.where = where
        self.next_action = next_action
        self.raw = str(raw or '')[:600]
        self.kind = kind
        self.restart_required = bool(restart_required)
        super().__init__('Windows OCR 不可用（%s）：缺 %s。%s' % (kind, missing, where))

    def to_dict(self):
        return dict(code=self.code, missing=self.missing, where=self.where,
                    next_action=self.next_action, restart_required=self.restart_required,
                    kind=self.kind, raw=self.raw)

    def summary(self):
        return ('缺什么：%s。去哪补：%s。下一步：%s。'
                % (self.missing, self.where, self.next_action))


def _powershell() -> str:
    root = os.environ.get('SystemRoot', r'C:\Windows')
    candidate = Path(root) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    return str(candidate) if candidate.is_file() else 'powershell'


def script_path() -> Path:
    """允许用 GUGA_OCR_SCRIPT 指定替代脚本（复现/私有化部署用），默认随包脚本。"""
    custom = os.environ.get('GUGA_OCR_SCRIPT', '').strip()
    if custom:
        return Path(custom)
    return ROOT / 'tools' / 'windows_ocr.ps1'


def _decode(value) -> str:
    if isinstance(value, str):
        return value.strip()
    raw = value or b''
    for encoding in ('utf-8', 'gbk'):
        try:
            return raw.decode(encoding).strip()
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode('utf-8', errors='replace').strip()


def _classify(stderr: str, stdout: str, returncode: int):
    low = (stderr + '\n' + stdout).lower()
    if ('禁止运行脚本' in low or 'running scripts is disabled' in low
            or 'unauthorizedaccess' in low or 'execution policy' in low
            or 'executionpolicy' in low or 'cannot be loaded because running scripts' in low):
        return dict(kind='script_blocked', missing='PowerShell 脚本执行权限（-File 被策略拦截）',
                    where=WHERE_SCRIPT_BLOCKED, next_action=NEXT_SCRIPT_BLOCKED,
                    restart_required=False)
    if 'no windows ocr language available' in low or 'no_engine' in low:
        return dict(kind='no_language', missing='中文 OCR 语言包（Windows.Media.Ocr 没有可用识别器）',
                    where=WHERE_LANGUAGE, next_action=NEXT_LANGUAGE, restart_required=True)
    if ('cannot find path' in low or 'does not exist' in low or '找不到路径' in low
            or '系统找不到指定的文件' in low):
        return dict(kind='script_missing', missing='OCR 识别脚本 tools/windows_ocr.ps1',
                    where=WHERE_SCRIPT, next_action=NEXT_SCRIPT, restart_required=False)
    if 'windows ocr timeout' in low:
        return dict(kind='timeout', missing='OCR 识别响应（超时）', where=WHERE_TIMEOUT,
                    next_action=NEXT_TIMEOUT, restart_required=False)
    if ('system.runtime.windowsruntime' in low or 'unable to find type' in low
            or 'add-type' in low or 'winrt' in low or 'windows.foundation' in low):
        return dict(kind='winrt_missing', missing='WinRT 运行组件（Windows.Media.Ocr 投影）',
                    where=WHERE_WINRT, next_action=NEXT_WINRT, restart_required=True)
    if ('aggregateexception' in low or 'recognizeasync' in low or 'bitmapdecoder' in low):
        return dict(kind='recognition_failed', missing='OCR 识别运行组件（引擎能创建但识别失败）',
                    where=WHERE_WINRT, next_action=NEXT_WINRT, restart_required=True)
    if returncode:
        return dict(kind='unknown', missing='Windows OCR（未知原因）', where=WHERE_LANGUAGE,
                    next_action=NEXT_LANGUAGE, restart_required=False)
    return dict(kind='none', missing='', where='', next_action='', restart_required=False)


def run_on_image(image_path, script=None, timeout=15) -> dict:
    """对一张图跑一次真实 OCR。返回结构，不抛异常（调用方自己决定怎么处理）。"""
    path = Path(script) if script else script_path()
    started = time.perf_counter()
    result = dict(ok=False, data={}, raw='', returncode=None, script=str(path),
                  elapsed_ms=0, kind='none', missing='', where='', next_action='',
                  restart_required=False)
    if not path.is_file():
        result.update(kind='script_missing', missing='OCR 识别脚本 %s' % path.name,
                      where=WHERE_SCRIPT, next_action=NEXT_SCRIPT,
                      raw='脚本不存在：%s' % path)
        result['elapsed_ms'] = int((time.perf_counter() - started) * 1000)
        return result
    command = [_powershell(), '-NoProfile', '-NonInteractive', '-File', str(path), str(image_path)]
    try:
        output = subprocess.run(command, capture_output=True, timeout=timeout,
                                creationflags=_NO_WINDOW)
    except FileNotFoundError:
        result.update(kind='powershell_missing', missing='powershell.exe',
                      where=WHERE_POWERSHELL, next_action=NEXT_POWERSHELL,
                      raw='找不到 powershell.exe')
        result['elapsed_ms'] = int((time.perf_counter() - started) * 1000)
        return result
    except subprocess.TimeoutExpired:
        result.update(kind='timeout', missing='OCR 识别响应（超时）', where=WHERE_TIMEOUT,
                      next_action=NEXT_TIMEOUT, raw='识别超过 %ss 未返回' % timeout)
        result['elapsed_ms'] = int((time.perf_counter() - started) * 1000)
        return result
    except OSError as exc:
        result.update(kind='unknown', missing='Windows OCR（无法启动识别进程）',
                      where=WHERE_LANGUAGE, next_action=NEXT_LANGUAGE,
                      raw='%s: %s' % (type(exc).__name__, exc))
        result['elapsed_ms'] = int((time.perf_counter() - started) * 1000)
        return result
    result['returncode'] = output.returncode
    stdout = _decode(output.stdout)
    stderr = _decode(output.stderr)
    result['raw'] = (stderr or stdout)[-600:]
    if output.returncode:
        result.update(_classify(stderr, stdout, output.returncode))
        result['ok'] = False
        result['elapsed_ms'] = int((time.perf_counter() - started) * 1000)
        return result
    try:
        data = json.loads(stdout or '{}')
        if not isinstance(data, dict):
            raise ValueError('OCR 返回不是 JSON 对象')
    except ValueError as exc:
        result.update(kind='recognition_failed', missing='OCR 返回内容（JSON 解析失败）',
                      where=WHERE_WINRT, next_action=NEXT_WINRT,
                      raw=str(exc)[:200])
        result['elapsed_ms'] = int((time.perf_counter() - started) * 1000)
        return result
    text = str(data.get('text') or '').strip()
    if not text:
        # 引擎跑通但识别 0 行 / 0 字：这是“OCR 空文本”，不是引擎不可用。
        # 单独给 empty_text，上层据此报 ocr_empty，不与 adapter_exception 混码。
        result.update(kind='empty_text', missing='OCR 识别结果（0 行 / 0 字）',
                      where='目标窗口是否可见并停在对话页',
                      next_action='按 Ctrl+R 刷新或重启目标软件后再派；仍为空请导出诊断',
                      raw=stdout[-300:])
        result['elapsed_ms'] = int((time.perf_counter() - started) * 1000)
        return result
    result.update(ok=True, data=data, kind='none', missing='')
    result['elapsed_ms'] = int((time.perf_counter() - started) * 1000)
    return result


def error_from(result) -> OcrUnavailableError:
    return OcrUnavailableError(result.get('missing') or 'Windows OCR',
                               result.get('where') or WHERE_LANGUAGE,
                               result.get('next_action') or NEXT_LANGUAGE,
                               raw=result.get('raw') or '',
                               kind=result.get('kind') or 'unknown',
                               restart_required=bool(result.get('restart_required')))


_probe_lock = threading.Lock()
_probe_cache = {'at': 0.0, 'value': None}


def _test_image(folder: Path):
    """生成一张含中文的测试图：必须让引擎真识别，而不是只看引擎对象存在。

    返回 ``(图片路径, 期望文本, 是否用上中文字体, 字体名)``。缺中文字体时**不再退英文
    当作通过**——那会把“认不出中文”误判成“OCR 就绪”，调用方据此判降级。
    """
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new('RGB', (720, 220), 'white')
    draw = ImageDraw.Draw(image)
    font = None
    font_name = ''
    for name in ('msyh.ttc', 'msyhbd.ttc', 'simhei.ttf', 'simsun.ttc'):
        candidate = Path(os.environ.get('WINDIR', r'C:\\Windows')) / 'Fonts' / name
        if candidate.is_file():
            try:
                font = ImageFont.truetype(str(candidate), 40)
                font_name = name
                break
            except Exception:
                continue
    text = '咕嘎自检 OCR 2026'
    if font:
        draw.text((24, 70), text, font=font, fill='black')
    else:                         # 没有中文字体：仍画中文，让引擎暴露“认不出来”
        draw.text((24, 70), text, font=ImageFont.load_default(), fill='black')
    path = folder / 'probe.png'
    image.save(path, format='PNG')
    return path, text, bool(font), font_name


def _char_overlap(expected: str, got: str) -> float:
    """字符重合率（多重集合交集 / 较长者长度），用于容忍 1–2 个识别误差。"""
    from collections import Counter
    expected = re.sub(r'\s+', '', expected or '')
    got = re.sub(r'\s+', '', got or '')
    if not expected:
        return 0.0
    common = sum((Counter(expected) & Counter(got)).values())
    return round(common / max(len(expected), len(got), 1), 3)


def execution_policy_info():
    """真实读取当前 PowerShell 执行策略。

    返回 {policy: 可读状态值, error: 原始错误（仅失败时非空）}。
    value.execution_policy 只放状态，不把 stderr 混进去，排障看独立字段。
    """
    command = [_powershell(), '-NoProfile', '-NonInteractive', '-Command',
               'Get-ExecutionPolicy -List | ForEach-Object { "$($_.Scope)=$($_.ExecutionPolicy)" }']
    try:
        output = subprocess.run(command, capture_output=True, timeout=8,
                                creationflags=_NO_WINDOW)
        text = _decode(output.stdout).replace('\r', ' ').strip()
        if output.returncode or not text:
            first = _decode(output.stderr).replace('\r', ' ').split('\n')[0][:120]
            return dict(policy='未读取',
                        error=(first or 'Get-ExecutionPolicy 无输出')[:200])
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        if not lines:
            return dict(policy='未读取', error='Get-ExecutionPolicy 输出为空')
        # 只保留可读的作用域=策略；不重复、不截断成半句。
        return dict(policy='；'.join(lines)[:200], error='')
    except Exception as exc:
        return dict(policy='未读取', error='%s: %s' % (type(exc).__name__, str(exc)[:160]))


def execution_policy():
    """兼容旧调用：只返回可读状态值。"""
    return execution_policy_info()['policy']


def probe(force=False, ttl=300) -> dict:
    """真实探测：生成中文测试图 → 真实识别 → 返回引擎语言/识别文本/失败原文。

    结果缓存 ``ttl`` 秒；自检和豆包共用，避免每次派单都启动一次 PowerShell。
    """
    now = time.monotonic()
    with _probe_lock:
        cached = _probe_cache.get('value')
        if not force and cached and now - float(_probe_cache.get('at') or 0) < ttl:
            return dict(cached, cached=True)
    started = time.perf_counter()
    policy_info = execution_policy_info()
    try:
        with tempfile.TemporaryDirectory(prefix='guga-ocr-probe-') as folder:
            image, expect, font_ok, font_name = _test_image(Path(folder))
            outcome = run_on_image(image)
    except Exception as exc:
        outcome = dict(ok=False, kind='probe_failed', missing='自检测试图生成失败',
                       where='重新解压完整安装包后重试', next_action=NEXT_TIMEOUT,
                       raw='%s: %s' % (type(exc).__name__, exc), data={},
                       restart_required=False, elapsed_ms=0)
        expect, font_ok, font_name = '', False, ''
    got = str((outcome.get('data') or {}).get('text') or '')
    if str(outcome.get('kind') or '') == 'empty_text':
        # 自检图是已知中文内容：这里返回 0 行 0 字 = 引擎读不出中文（语言包缺失），
        # 归为“引擎不可用”，避免和“窗口本身没文字(ocr_empty)”混成一码。
        outcome = dict(outcome, kind='probe_empty',
                       missing='Windows OCR 语言包（自检图识别 0 行 / 0 字）',
                       where=WHERE_LANGUAGE, next_action=NEXT_LANGUAGE,
                       restart_required=True)
    accuracy = _char_overlap(expect, got) if outcome.get('ok') else 0.0
    if not outcome.get('ok'):
        # 接口能调通但自检图返回 0 行 0 字：是“装了但用不了/未生效”，判降级；
        # 引擎/语言包完全缺失（no_language、winrt_missing、脚本缺失等）才判失败。
        state = 'degraded' if str(outcome.get('kind') or '') == 'probe_empty' else 'fail'
    elif not font_ok:
        state = 'degraded'
    elif accuracy < ACCURACY_GATE:
        state = 'degraded'
    else:
        state = 'pass'
    value = dict(
        available=bool(outcome.get('ok')),
        state=state,
        language=str((outcome.get('data') or {}).get('language') or ''),
        text=got[:120],
        expected=expect,
        accuracy=accuracy,
        accuracy_gate=ACCURACY_GATE,
        font_ok=font_ok,
        font_name=font_name,
        execution_policy=policy_info.get('policy') or '未读取',
        execution_policy_error=policy_info.get('error') or '',
        kind=outcome.get('kind') or 'none',
        missing=outcome.get('missing') or '',
        where=outcome.get('where') or '',
        next_action=outcome.get('next_action') or '',
        restart_required=bool(outcome.get('restart_required')),
        raw=outcome.get('raw') or '',
        script=outcome.get('script') or str(script_path()),
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )
    if state == 'degraded' and not font_ok and outcome.get('ok'):
        value.update(kind='font_missing', missing='中文字体（微软雅黑/黑体）',
                     where=WHERE_FONT, next_action=NEXT_FONT, restart_required=False)
    elif state == 'degraded' and outcome.get('ok'):
        value.update(kind='low_accuracy',
                     missing='中文 OCR 语言包（识别准确率 %.0f%% < %.0f%%）'
                             % (accuracy * 100, ACCURACY_GATE * 100),
                     where=WHERE_LANGUAGE, next_action=NEXT_ACCURACY, restart_required=True)
    with _probe_lock:
        _probe_cache.update(at=time.monotonic(), value=value)
    return dict(value, cached=False)


def reset_probe():
    with _probe_lock:
        _probe_cache.update(at=0.0, value=None)


def available() -> bool:
    return bool(probe().get('available'))
