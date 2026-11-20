# -*- coding: utf-8 -*-
"""
咕嘎管家 · 桌面执行器 v0.7（真机控制）
KB2 剧本链：doubao-ask（提问）→ doubao-followup（追问）→ doubao-copy（取回答案）
  chain 模式一条龙执行，全程截图取证。
安全机制：
  - FAILSAFE 开启（鼠标甩到屏幕左上角可紧急中止）
  - 执行前保存剪贴板，结束后恢复
  - 新对话隔离（绝不污染少爷现有会话/画布任务）
  - 每一步都有画面校验，校验不过按 KB2 异常分支停手

v0.7 几何口径（治 Coordinate 'lower' is less than 'upper' 的根）：
  ① 窗口几何只有一个来源 win_rect()：GetWindowRect 单次快照 → 校验（正宽高/不小于 200/
     与屏幕有交集）→ 不合法按标题重找窗口 → 再不行退屏幕区域。
     绝不"夹一下就当能用"——夹完位置全错，比崩掉更坏。
  ② 截图只有一个入口 grab()：求交屏幕 + 正宽高 + 失败回退全屏 + 永不抛（FailSafe 除外）。
  ③ 视觉坐标按【实际截到的图】换算：窗口贴边被裁、窗口比 800 窄时，写死 800 或按标称
     宽换算都会整体偏移。

v0.12 跨机器口径（治"这台机器好好的，换台机器就认不出/点偏"）：
  ④ DPI 声明提前到 import pyautogui/PIL 之前，并以系统读回的真实级别为准（不谎报）。
  ⑤ 送视觉模型的图宽自适应 800→1600：固定 800 时 1920 屏满窗口正文只剩 5.8px、
     4K 只剩 2.9px，模型自然认不出按钮；这是"分辨率不一致就失效"的真根源，不是模型不行。
  ⑥ 两级定位 locate_deep：全窗找不着 → 只截底部输入带/左侧栏放大再看。面积更小、像素更清。
  ⑦ 所有固定像素（校验区 190/34/44、点击偏移 95、变化面积阈值 1200、工具栏图标偏移 14）
     统一乘显示器缩放系数——只在 96dpi 成立的东西，换个缩放就是错的。
"""
import os
import sys
import time
import io
import json
import re
import base64
import ctypes
import urllib.error
import urllib.request
# 以 `import guga_executor` 从 agent/ 目录导入时，仓库根不在 sys.path；
# 补上它才能复用 core 里的窗口前台/托盘实现（与 guga_agent.py 的做法一致）。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from core import win_focus as _win_focus  # noqa: E402

# 云端辅助（视觉/请示）因权限或额度被拒时，只在结果里记一条，不整单失败、不静默成功。
_AUX_NOTICES = []


def _note_aux(text):
    text = str(text or '').strip()
    if text and text not in _AUX_NOTICES:
        _AUX_NOTICES.append(text)
    return text


def aux_notice_text():
    return ' / '.join(_AUX_NOTICES)


def reset_aux_notices():
    del _AUX_NOTICES[:]


def _http_error_detail(exc):
    """403 响应体里的 code / error / degraded；读不出来就返回空。"""
    try:
        body = json.loads(exc.read().decode('utf-8') or '{}')
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return (str(body.get('code') or ''),
            str(body.get('error') or body.get('message') or ''),
            bool(body.get('degraded')),
            str(body.get('helper') or ''))


def _enable_dpi_awareness():
    """声明 DPI 感知。必须在 import pyautogui / PIL 之前调用——它们会缓存屏幕 DC，
    一旦在 unaware 状态下取过尺寸，后面再声明也改不回来（这是跨机器失效的隐蔽前提）。

    默认是"系统级感知"(1)：在 125%/150% 缩放的机器上，GetWindowRect 与 GetSystemMetrics 返回
    逻辑像素，而屏幕截图抓的是物理像素——两者混用会表现为"只截到窗口左上部分"+"点击整体偏移"，
    而且不报错。本机 100% 缩放时完全看不出来，换台演示机就翻车。

    返回**读回的真实级别**，不是"我调了就算成功"：exe 清单里若已声明过 DPI，
    SetProcessDpiAwareness 会抛 E_ACCESSDENIED，那时报 per-monitor 就是谎报。"""
    try:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)      # 2 = PER_MONITOR_DPI_AWARE
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
        # 无论设置成功与否，都以系统读回的值为准
        val = ctypes.c_int(-1)
        try:
            if ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(val)) != 0:
                raise OSError
        except Exception:
            val.value = int(ctypes.windll.user32.IsProcessDPIAware()) or 0
        return {0: "none（未感知·坐标必偏）", 1: "system（系统级·多屏缩放下会偏）", 2: "per-monitor"}.get(
            val.value, f"unknown({val.value})")
    except Exception as e:
        return f"none（{e}）"


DPI_MODE = _enable_dpi_awareness()   # ← 关键：早于下面所有第三方导入

import pyautogui          # noqa: E402  必须在 DPI 声明之后
import pyperclip          # noqa: E402
import pygetwindow as gw   # noqa: E402
from PIL import Image, ImageChops, ImageGrab   # noqa: E402

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.4

try:
    from core import input_guard          # 每次键鼠调用前先查暂停
    input_guard.install_pyautogui(pyautogui)
except Exception:
    input_guard = None

_DPI_WARNED = [False]


def dpi_report():
    """一行体检：尺寸三处是否一致。缩放没对齐时这里会立刻露馅，不用等点击歪了才发现。"""
    try:
        sw, sh = pyautogui.size()
        img = ImageGrab.grab(all_screens=True)
        ok = (sw, sh) == img.size
        return (f"DPI {DPI_MODE} · 系统 {sw}x{sh} · 截图 {img.width}x{img.height}"
                + ("（一致 ✓）" if ok else "（⚠ 不一致，坐标会偏，请把显示缩放设为 100% 或报告此日志）"))
    except Exception as e:
        return f"DPI {DPI_MODE} · 体检失败（{e}）"


def vis_report():
    """跨机器体检：这台机器上的窗口，送进视觉模型后文字还剩几像素。
    ≥7 可靠 · 5~7 勉强（靠区域放大兜） · <5 基本认不出，必须缩窗口或调缩放。"""
    try:
        sw, sh = pyautogui.size()
        img_w = _fit_vis_width(sw)
        dens = _vis_density(sw, img_w)
        verdict = "OK" if dens >= 7 else ("勉强（会自动区域放大兜底）" if dens >= 5 else "不足·请把软件窗口拖窄到 1500px 内")
        return (f"视觉 {sw}x{sh} · 送模型 {img_w}px · 满屏时正文 {dens:.1f}px → {verdict}"
                f" · 缩放 {_dpi_scale():.2f}x")
    except Exception as e:
        return f"视觉体检失败（{e}）"

EXEC_VERSION = "0.15"        # 随日志上报，便于下次"到底跑的是哪一版"一眼定位（本次事故的教训）

# 云端大脑地址只能由用户配置下发（桌面端启动执行器前写入 GUGA_CLOUD）；
# 不内置任何地址：没配就不发请求，本地启发式继续，绝不借用运营方端点。
API_BASE = os.environ.get("GUGA_CLOUD", "").rstrip("/")
TOKEN = ""          # 由 guga_agent 配对后注入——视觉请示要用执行方身份
CTX = None          # 当前正在执行的 {taskId, stId}——取证截图随任务上传网页证据墙


def _api(path, data, timeout=75):
    req = urllib.request.Request(API_BASE + path, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Agent-Token", TOKEN)
    # 云端 JSON POST 保留 POST 重定向，避免跳转后变 GET 回 405。
    try:
        from core import model_endpoint
        opener = model_endpoint.urlopen_post
    except Exception:
        opener = urllib.request.urlopen
    with opener(req, json.dumps(data).encode(), timeout=timeout) as r:
        return json.loads(r.read().decode())


# ───────────────────────── 窗口几何：唯一可信来源 ─────────────────────────

def _hwnd_rect(w):
    """一次性取窗口屏幕矩形（GetWindowRect 单次快照，与 pyautogui 截图同一坐标系）。
    不能分别读 w.left / w.width——pygetwindow 每次属性访问都是一次系统调用，
    窗口正在启动动画/被拖动时会读到不同帧，拼出来的矩形自相矛盾（负高宽就是这么来的）。"""
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = getattr(w, "_hWnd", None)
        if hwnd:
            r = wintypes.RECT()
            if ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
                return (int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top))
    except Exception:
        pass
    try:
        return (int(w.left), int(w.top), int(w.width), int(w.height))
    except Exception:
        return None


def _screen_bounds():
    """虚拟桌面边界 (x, y, w, h)。主屏不是全部：副屏放在主屏左侧时 x 为负，
    只认 pyautogui.size() 会把整块副屏裁没（多屏办公的常态）。"""
    try:
        import ctypes
        u = ctypes.windll.user32
        vx, vy = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
        vw, vh = u.GetSystemMetrics(78), u.GetSystemMetrics(79)
        if vw > 0 and vh > 0:
            return (vx, vy, vw, vh)
    except Exception:
        pass
    sw, sh = pyautogui.size()
    return (0, 0, sw, sh)


def _rect_ok(r):
    """矩形可信判据：宽高为正且不小于 200（滤掉启动动画/隐藏壳窗口）、与虚拟桌面有实质交集。"""
    if not r:
        return False
    x, y, cw, ch = r
    if cw < 200 or ch < 200:
        return False
    vx, vy, vw, vh = _screen_bounds()
    return x < vx + vw and y < vy + vh and x + cw > vx and y + ch > vy


def _win_ok(w):
    """窗口对象可用性判断。pygetwindow 的属性访问会调系统 API，
    枚举时正好有窗口关掉就抛异常——列表推导里裸写 w.width 会把这轮任务带崩。"""
    try:
        return w.width > 200 and w.height > 200
    except Exception:
        return False


# 托盘图标级小窗/框架窗：不是主界面，叫回来也没用，反而会误抓。
# （真机实测：元宝有 16x16 的 com.tencent.yuanbao-siw，智谱有 600x600 的 floatBallWindow，
#   Kimi 有 800x600 的 Kimi Launcher；它们的标题都可能含软件名。）
# 窗口前台/托盘恢复的实现在 core.win_focus（core/executor_compat.py 复用同一份，
# 不再各存一套）。这里保留执行器自己的 print 日志出口。
_TRAY_NOISE = _win_focus.TRAY_NOISE
_hwnd_of = _win_focus.hwnd_of


def focus_window(w, app="", quiet=False):
    """把目标窗口抬到最前并激活（重试策略见 core.win_focus.focus_window）。"""
    return _win_focus.focus_window(w, app=app, quiet=quiet, log=log)


def _wake_from_tray(kws):
    """把"关到托盘"的软件主窗口叫回来（实现见 core.win_focus.wake_from_tray）。"""
    return _win_focus.wake_from_tray(tuple(kws or ()), log=log)


def _carry_kws(src, dst):
    """把"这扇窗属于哪个软件"的关键词传给新窗口对象。
    丢了它，refresh_box 会退回默认的豆包关键词——非豆包任务第二轮就被切到豆包窗口去。"""
    try:
        k = getattr(src, "_guga_kws", None)
        if k and dst is not None:
            dst._guga_kws = k
    except Exception:
        pass
    return dst


def _refind(w):
    """按挂在窗口对象上的关键词重找窗口对象（不动传入的 w）。"""
    for kw in getattr(w, "_guga_kws", None) or ("豆包", "Doubao"):
        wins = [x for x in gw.getWindowsWithTitle(kw) if _win_ok(x)]
        if wins:
            return _carry_kws(w, wins[0])
    return None


def win_rect(w, deep=True):
    """全模块唯一的窗口几何来源。所有截图/点击换算只准从这里取 (x, y, w, h)。"""
    r = _hwnd_rect(w)
    if _rect_ok(r):
        return r
    if deep:
        try:
            nw = _refind(w)
        except Exception:
            nw = None
        if nw is not None:
            r2 = _hwnd_rect(nw)
            if _rect_ok(r2):
                log(f"窗口矩形不可用 {r} → 按标题重找得到 {r2}，已切用新矩形")
                return r2
    vx, vy, vw, vh = _screen_bounds()
    log(f"窗口矩形不可用 {r}，本步退回虚拟桌面区域 ({vx},{vy},{vw}x{vh})")
    return (vx, vy, vw, vh)


def refresh_box(w):
    """按标题重找窗口刷新矩形——窗口被移动/恢复/改变大小后旧矩形全错（点击/截图/换算全崩），
    干正事前先保鲜。找不到就拿回原对象（win_rect 兜底）。"""
    try:
        nw = _refind(w)
        if nw is not None:
            r_old, r_new = _hwnd_rect(w), _hwnd_rect(nw)
            if r_old != r_new:
                log(f"窗口矩形已漂移：{r_old} → {r_new}，已刷新")
            return _carry_kws(w, nw)
    except Exception as e:
        log(f"刷新窗口矩形失败（{e}），沿用旧矩形")
    return w


# ───────────────────────── 截图：唯一入口，永不炸任务 ─────────────────────────

def _sanitize(region):
    """region 与虚拟桌面求交，并保证正的宽高、至少 2px。不可用返回 None。"""
    if not region:
        return None
    try:
        x, y, cw, ch = [int(v) for v in region]
    except Exception:
        return None
    vx, vy, vw, vh = _screen_bounds()
    x1, y1 = max(vx, x), max(vy, y)
    x2, y2 = min(vx + vw, x + cw), min(vy + vh, y + ch)
    if x2 - x1 < 2 or y2 - y1 < 2:
        return None
    return (x1, y1, x2 - x1, y2 - y1)


def grab(region=None, gray=False):
    """安全截图：返回 (图, 实际使用的 region)。region 非法 → 回退全屏；截图失败 → (None, None)。
    直接走 ImageGrab(all_screens=True)+bbox：pyscreeze 是先抓主屏再 crop，副屏负坐标会截成黑块，
    负高宽才会抛 'Coordinate ... is less than ...'——这里绕开它，坐标口径与 Windows 点击一致。
    只放行 FailSafeException（鼠标甩左上角是少爷的紧急刹车，绝不能被"容错"吃掉）。"""
    try:
        pyautogui.failSafeCheck()
    except pyautogui.FailSafeException:
        raise
    r = _sanitize(region)
    try:
        if r:
            img = ImageGrab.grab(bbox=(r[0], r[1], r[0] + r[2], r[1] + r[3]), all_screens=True)
        else:
            img = ImageGrab.grab(all_screens=True)
            r = (0, 0, img.width, img.height)
        if (img.width, img.height) != (r[2], r[3]) and not _DPI_WARNED[0]:
            _DPI_WARNED[0] = True
            log(f"⚠ 截图 {img.width}x{img.height} 与请求区域 {r[2]}x{r[3]} 不一致——显示缩放没对齐，坐标会偏")
        return (img.convert("L") if gray else img), r
    except pyautogui.FailSafeException:
        raise
    except Exception as e:
        log(f"区域截图失败（{e}），回退 pyscreeze 全屏截图")
    try:
        img = pyautogui.screenshot()
        return (img.convert("L") if gray else img), (0, 0, img.width, img.height)
    except pyautogui.FailSafeException:
        raise
    except Exception as e:
        log(f"全屏截图也失败（{e}）")
        return None, None


def shot(region):
    """灰度截图（像素校验用）。可能返回 None——调用方必须容忍"这次没截到"。"""
    return grab(region, gray=True)[0]


VIS_MAX_W = 1600     # 送视觉模型的图宽上限（服务端 2.2MB base64 闸门内，1600 宽 JPEG 约 350KB）
VIS_MIN_W = 800      # 小窗口保底值，保持不变
VIS_TEXT_PX = 14.0   # 长侧边栏正文的典型逻辑高度，用来量化"文字还剩多少像素"


def _fit_vis_width(cw):
    """送模型的图宽：小窗不该浪费带宽，大窗必须放大。
    原来写死 800 的后果是跨分辨率失效的真根源——
    1920 屏满屏窗口(1936px)正文只剩 5.8px，4K(3800px)只剩 2.9px，模型认不出按钮形状；
    而 1366 小屏有 8.2px 一切正常。所以"测试机好好的、换台机器就瞎"不是玄学。"""
    return max(VIS_MIN_W, min(VIS_MAX_W, int(cw)))


def _vis_density(cw, img_w):
    """正文送进模型后还剩几像素——跨分辨率识别失效的量化指标（≥7 可靠，<5 基本认不出）。"""
    return VIS_TEXT_PX * img_w / max(1, cw)


_WIDTH_WARNED = [False]


def _shot_window(w, zone=None, max_w=None):
    """截窗（或窗口内某一区域）→ 等比缩到合适宽度 → JPEG base64。
    返回的 x0/y0/cw/ch 是【实际截到的区域】，视觉坐标必须按它换算。

    zone：可选 (rx, ry, rw, rh) 相对窗口的比例框，用于"区域聚焦"——
    全窗找不着时只截底部输入带/侧边栏放大去看，同样的模型看更少的面积、更清楚的像素。
    """
    x, y, cw, ch = win_rect(w)
    if zone:
        rx, ry, rw, rh = zone
        zx, zy = x + int(cw * rx), y + int(ch * ry)
        zw, zh = max(2, int(cw * rw)), max(2, int(ch * rh))
        x, y, cw, ch = zx, zy, zw, zh
    info = {"b64": "", "img_w": 0, "img_h": 0, "x0": x, "y0": y, "cw": cw, "ch": ch, "zone": zone}
    img, used = grab((x, y, cw, ch))
    if img is None:
        return info
    if used:
        info["x0"], info["y0"], info["cw"], info["ch"] = used
    tw = int(max_w or _fit_vis_width(info["cw"]))
    if img.width > tw:
        img = img.resize((tw, max(1, int(img.height * tw / img.width))), Image.LANCZOS)
    info["img_w"], info["img_h"] = img.width, img.height
    # 大窗口缩完还是糊时点一次名：这是"换台电脑就识别失效"的现场指标，不是噪音
    dens = _vis_density(info["cw"], img.width)
    info["density"] = round(dens, 1)
    if dens < 6 and not _WIDTH_WARNED[0]:
        _WIDTH_WARNED[0] = True
        log(f"⚠ 窗口宽 {info['cw']}px，缩到 {img.width}px 送模型后正文仅剩 {dens:.1f}px（<6 易认错）。"
            f"建议把软件窗口拖窄到 1500px 以内，或把系统缩放调高")
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=72)   # 60→72：文字边缘更锐，体积仍在闸门内
    info["b64"] = base64.b64encode(buf.getvalue()).decode()
    return info


AUX_DEGRADED_HINTS = {
    'CLOUD_NOT_ENTITLED': '云端辅助未开通，已用本地模式继续',
    'CLOUD_QUOTA_EXCEEDED': '云端额度用完，已用本地模式继续',
}


def degraded_hint(code):
    """code 只用于提示文案；不写进任务状态，也不作为失败原因。"""
    return AUX_DEGRADED_HINTS.get(str(code or ''),
                                  '云端辅助不可用，已用本地模式继续')


def _note_degraded(helper, code):
    """记一条降级日志（哪个 helper、哪个 code、什么时候），并返回给结果的提示。"""
    stamp = time.strftime('%Y-%m-%d %H:%M:%S')
    log('云端辅助降级：helper=%s code=%s at=%s' % (helper, code or '-', stamp))
    return _note_aux(degraded_hint(code))


def _vision(b64, question, timeout=75):
    """视觉通道。失败返回 ''（调用方必须自带回退路径——视觉是增强，不是依赖）。

    精确规则：HTTP 403 且 body.degraded === true 且 body.helper === "vision"
    → 不重试、不判任务失败，跳过本步截图理解，走执行器本地启发式回退。
    """
    if not b64:
        return ""
    try:
        r = _api("/api/agent/vision", {"image": b64, "question": question}, timeout)
    except urllib.error.HTTPError as e:
        code, _text, degraded, helper = _http_error_detail(e)
        if e.code == 403 and degraded and helper == 'vision':
            note = _note_degraded('vision', code)
            log('%s；跳过本步截图理解，走本地启发式回退' % note)
            return ""
        log(f"视觉通道暂不可用（{e}），走位置/剪贴板兜底")
        return ""
    except Exception as e:
        log(f"视觉通道暂不可用（{e}），走位置/剪贴板兜底")
        return ""
    if not isinstance(r, dict):
        return ""
    return (r.get("text") or "").strip() if r.get("ok") else ""


def see(w, question, timeout=75):
    """把当前窗口截图给云端视觉模型看，返回回答文本。"""
    return _vision(_shot_window(w)["b64"], question, timeout)


def locate(w, desc, zone=None):
    """让视觉模型指出窗口（或 zone 区域内）某元素中心坐标 → 换算成真实屏幕坐标。失败返回 None。
    换算基准是实际截到的图与实际截取区域，不是标称窗口宽；x/y 分别换算，
    避免"图被夹过、宽高比例不一致"时纵向偏移（等比缩放时两者相等，属双保险）。"""
    info = _shot_window(w, zone=zone)
    if not info["b64"]:
        log("截图取不到，无法视觉定位")
        return None
    scope = "这块区域截图" if zone else "这张应用窗口截图"
    ans = _vision(info["b64"],
                  f'在{scope}里找到「{desc}」，只输出 JSON：{{"x":中心横坐标,"y":中心纵坐标}}，'
                  f'坐标基于该截图的像素（图宽 {info["img_w"]}）。找不到就输出 {{"x":-1,"y":-1}}')
    m = re.search(r"\{[^{}]*\}", ans)
    if not m:
        return None
    try:
        p = json.loads(m.group(0))
        x, y = int(p.get("x", -1)), int(p.get("y", -1))
    except Exception:
        return None
    if x < 0 or y < 0:
        return None
    # 模型偶尔会按"图宽"以外的尺度答（比如直接给 0~1 归一化或超界）；
    # 越界就按比例归一，别把点击落到窗口外去
    if x > info["img_w"] or y > info["img_h"]:
        # 区域裁片特有：模型盯着裁片看、答的却是整窗坐标（实测裁片 304 高答 y=878）。
        # 若坐标落在整窗范围内，按整窗绝对坐标直收——比放弃这次定位强得多。
        try:
            fx, fy, fcw, fch = win_rect(w)
            if 0 < x <= fcw and 0 < y <= fch:
                log(f"区域定位坐标按整窗尺度解读（{x},{y}）")
                return (fx + x, fy + y)
        except Exception:
            pass
    if x > info["img_w"] * 1.5 or y > info["img_h"] * 1.5:
        log(f"视觉坐标越界（{x},{y} vs 图 {info['img_w']}x{info['img_h']}），本次放弃")
        return None
    sx = info["cw"] / float(info["img_w"] or 1)
    sy = info["ch"] / float(info["img_h"] or 1)
    return (info["x0"] + int(x * sx), info["y0"] + int(y * sy))


def locate_deep(w, desc, zones):
    """两级定位：先看全窗，找不着就按 zones 逐块放大再看。
    为什么需要：大分辨率机器上全窗缩到 1600 后小图标只剩几个像素，归因于"模型不行"是错的——
    同样的模型看一块 800x400 的放大图，认「输入框」几乎不会错。
    zones 传窗口相对比例框，如底部输入带 (0, 0.55, 1, 0.45)。"""
    p = locate(w, desc)
    if p:
        return p
    for z in zones or ():
        log(f"全窗没定位到「{desc[:16]}」，放大到区域 {z} 再看一次")
        p = locate(w, desc, zone=z)
        if p:
            log(f"区域放大定位成功 → {p}")
            return p
    return None


def _expand(p):
    return os.path.expandvars(p)


# 多 AI 软件档案（v0.5）：服务端派单会带 app=显示名，按档案找窗口/启动 exe。
# 视觉定位/读回复本来就与具体软件无关，只有"找窗口"和"启动"是软件相关的。
APP_PROFILES = {
    "豆包": {
        "win": ("豆包", "Doubao"),
        "exe": (r"%LOCALAPPDATA%\Doubao\Application\app\Doubao.exe",
                r"%LOCALAPPDATA%\ByteDance\Doubao\app\Doubao.exe"),
    },
    "WorkBuddy": {
        "win": ("WorkBuddy",),
        "exe": (r"%LOCALAPPDATA%\Programs\WorkBuddy\WorkBuddy.exe",
                r"%PROGRAMFILES%\WorkBuddy\WorkBuddy.exe"),
    },
    # 以下四款的路径与入口位置来自 2026-11-05 返工版素材包（队友机真机实测）。
    # 路径里的用户名一律换成环境变量，否则换台机器就全失效。
    "腾讯元宝": {
        "win": ("元宝", "Yuanbao"),
        # 真机校准（2026-11-05）：本机装在 D:\Yuanbao（不在 Program Files）。
        # 两套都留着——换台机器可能又装回默认位置，存在即用。
        "exe": (r"D:\Yuanbao\yuanbao.exe",
                r"%PROGRAMFILES%\Tencent\Yuanbao\yuanbao.exe",
                r"%LOCALAPPDATA%\Tencent\Yuanbao\yuanbao.exe"),
        # 附件入口：输入框**左侧**「+」→ 浮层菜单「本地文件」。
        # 菜单点开才出现，不是常驻；点菜单外即关闭。
        "attach": {"side": "left", "menu_item": "本地文件",
                   # 实测白名单：只认 Office/PDF/文本，塞 .zip/.md 会被系统框直接过滤掉
                   "filter": ("xls", "xlsx", "ppt", "pptx", "doc", "docx", "pdf", "txt")},
        # 真机教训（2026-11-05 实测×3）：视觉模型连续把「AI创作」误当「新建对话」（相邻两行），
        # 点进图片创作页（"开始你的创作吧"），页面看着空但其实不是对话。
        # 对策：不再让模型区分相邻行——锚定大字「AI 创作」，点它**上一行**（固定行距）即新建对话。
        "btn_anchor": {"desc": "左侧栏里的「AI 创作」菜单项文字（带剪刀或画笔图标的那行）",
                       "dy": -0.044},   # 上移约一行 = 新建对话
        "new_chat_hint": "当前元宝是否已进入对话界面（中央是对话输入框，可打字提问；"
                         "而**不是**显示「开始你的创作吧」或「描述你想生成的图片」的图片创作页）？",
    },
    "Kimi": {
        # ⚠ 已移出适配名单（2026-11-06 用户决定）：Kimi 桌面版需付费会员才能启动。
        # 档案保留只为历史追溯；find_window/launch 见 blocked 即拒绝，绝不指挥它。
        "blocked": "需要付费会员才能使用，咕嘎已停止适配",
        "win": ("Kimi",),
        # 真机校准：本机 D:\Kimi\Kimi.exe（默认位置 %LOCALAPPDATA%\Programs\Kimi 反而没有）
        "exe": (r"D:\Kimi\Kimi.exe",
                r"%LOCALAPPDATA%\Programs\Kimi\Kimi.exe",
                r"%PROGRAMFILES%\Kimi\Kimi.exe"),
        # 附件入口：左侧「+」→「添加文件与图片」，或直接 Ctrl+U 唤系统框。
        # 实测过滤为 All Files(*.*)，不挑后缀——喂非 Office 文件优先走 Kimi。
        "attach": {"side": "left", "menu_item": "添加文件与图片", "hotkey": "ctrl+u",
                   "filter": ()},
        # 任务导向：新建后出现「[任务]」标题属正常，判空只看有没有问答气泡
        "new_chat_hint": "当前 Kimi 是否已进入一个新建的空任务页（对话区没有任何问答消息；"
                         "左侧列表出现「[任务]」字样是正常命名，不算历史消息）？",
    },
    "DeepSeek": {
        "win": ("DeepSeek",),
        "exe": (),
        # 桌面版实测**无可见 GUI 窗口**（便携 Harness，多进程但 MainWindowTitle 全空），
        # 窗口级自动化在这条路上走不通；界面只能走网页版。见 gui=False。
        "gui": False,
        "web": "https://chat.deepseek.com",
        # 附件入口在输入框**右侧**（回形针）——与其余三款的左侧「+」方向相反。
        "attach": {"side": "right", "menu_item": "回形针", "filter": ()},
    },
    "智谱清言": {
        "win": ("清言", "ChatGLM", "智谱"),
        # 真机校准：本机 D:\chatglm\智谱清言.exe
        "exe": (r"D:\chatglm\智谱清言.exe",
                r"%LOCALAPPDATA%\ZP\chatglm\智谱清言.exe",
                r"%PROGRAMFILES%\chatglm\智谱清言.exe"),
        # 附件入口：左侧「+」→「上传文件或图片」。
        # 坑：输入框**未聚焦**时点「+」毫无反应（实测前后像素差 0.00%），必须先点输入框。
        "attach": {"side": "left", "menu_item": "上传文件或图片",
                   "focus_input_first": True, "filter": ()},
        "new_chat_hint": "当前智谱清言是否已进入一个新建的空任务页（对话区没有任何问答消息；"
                         "任务视图里显示「[任务]」是正常命名，不算历史消息）？",
    },
    "企业微信": {"win": ("企业微信", "WXWork"), "exe": (), "blocked": "隐私"},   # 登记但禁止
    "微信": {"win": ("微信", "WeChat"), "exe": (), "blocked": "隐私"},          # 登记但禁止
    "QQ": {"win": ("QQ",), "exe": (), "blocked": "隐私"},                    # 登记但禁止
    "记事本": {"win": ("记事本", "Notepad"), "exe": ("notepad.exe",)},
    "计算器": {"win": ("计算器", "Calculator"), "exe": ()},
    "浏览器": {"win": ("Chrome", "Edge", "浏览器"), "exe": ()},
    "WPS": {"win": ("WPS",), "exe": ()},
    "Word": {"win": ("Word",), "exe": ()},
    "Excel": {"win": ("Excel",), "exe": ()},
    "剪映": {"win": ("剪映", "JianyingCapCut"), "exe": ()},
    "飞书": {"win": ("飞书", "Feishu", "Lark"), "exe": ()},
    "钉钉": {"win": ("钉钉", "DingTalk"), "exe": ()},
    "腾讯会议": {"win": ("腾讯会议", "wemeet", "voov"), "exe": ()},
}


# 隐私阻断（2026-11-05 拍板）：个人/办公通信软件一律不接管——微信、企业微信、QQ 都不做。
# 不是"做不了"，是"不该做"——读屏式自动化必须截整窗，聊天里必然是私人消息、
# 群文件、转账、家庭照片，全部会被一并读走。服务端已拦，这里是第二道闸：
# 万一是旧版服务端或手工派单，本机也不动。
PRIVACY_BLOCK = {
    "企业微信": "办公通信软件，读屏会读到与任务无关的同事私聊和内部文件",
    "wxwork": "办公通信软件（同上）",
    "wecom": "办公通信软件（同上）",
    "微信": "个人通信软件，读屏会读到与任务无关的私人聊天和文件",
    "wechat": "个人通信软件（同上）",
    "weixin": "个人通信软件（同上）",
    "qq": "个人通信软件，读屏会读到与任务无关的私人聊天、群文件和转账",
}


def _privacy_guard(app):
    """命中隐私名单就直接抛错——带理由，让台账说清"为什么不做"，而不是静默跳过。
    按关键词长度倒序匹配：否则 "微信" 会先命中 "企业微信"，报错文案就指错软件了。"""
    name = str(app or "").strip().lower()
    if not name:
        return
    for k, why in sorted(PRIVACY_BLOCK.items(), key=lambda kv: -len(kv[0])):
        if k.lower() in name:
            raise RuntimeError(
                f"咕嘎不接管「{app}」：{why}。跨应用搬文件请直接指定文件和目标 AI 软件，不经过聊天窗口")


def _profile(app):
    """取软件档案；**未登记的软件不再退回豆包**（旧逻辑会把 Kimi 的任务派到豆包窗口上）。
    未登记时用软件名本身当窗口关键词，走通用视觉路径。"""
    prof = APP_PROFILES.get(app)
    if prof:
        return prof, True
    return {"win": (app,), "exe": ()}, False


def find_window(app="豆包"):
    """按关键词找窗口；未登记软件用名字模糊匹配全部窗口标题。
    关键词挂在窗口对象上（_guga_kws），refresh_box 按同款关键词保鲜矩形。"""
    _privacy_guard(app)          # 第二道闸：服务端拦不到的手工派单也不动
    prof, _known = _profile(app)
    if prof.get("blocked"):
        log(f"{app} 已移出适配名单（{prof['blocked']}），咕嘎不指挥它")
        return None
    # 已实测"桌面版没有可见窗口"的软件（如 DeepSeek 便携 Harness）：直接说清楚，
    # 不要让它落到下面的模糊匹配里瞎转一圈再返回 None——那会被误判成"软件没开"。
    if prof.get("gui") is False:
        raise RuntimeError(
            f"{app} 桌面版没有可见主窗口（实测多进程但窗口标题为空），无法窗口级接管；"
            f"界面请走网页版 {prof.get('web', '')}，或改用豆包/元宝/Kimi/智谱")
    for kw in prof["win"]:
        wins = [w for w in gw.getWindowsWithTitle(kw) if _win_ok(w)]
        if wins:
            w = wins[0]
            try:
                w._guga_kws = prof["win"]
            except Exception:
                pass
            log(f"找到{app}窗口「{w.title}」")
            focus_window(w, app, quiet=True)   # 找到 ≠ 在最前，重叠时必须抬起来，否则截图抓到别人
            return w
    # 可见窗口一个都没找到 —— 很可能是被"关到托盘"了（进程还在，窗口隐藏）。
    # 先叫回来再找一次，别急着报"软件没开"。
    woke = _wake_from_tray(prof["win"])
    if woke:
        log(f"{app} 主窗口在托盘里（隐藏态），已叫回「{woke}」")
        for kw in prof["win"]:
            wins = [w for w in gw.getWindowsWithTitle(kw) if _win_ok(w)]
            if wins:
                w = wins[0]
                try:
                    w._guga_kws = prof["win"]
                except Exception:
                    pass
                log(f"托盘唤醒后找到{app}窗口「{w.title}」")
                focus_window(w, app, quiet=True)
                return w

    # 通用兜底：标题里含软件名（忽略空格/连字符/大小写）就算——软件窗口标题常带文档名/会话名
    def _norm(s):
        return re.sub(r"[\s\-_·—]+", "", str(s)).lower()
    target = _norm(app)
    if target:
        for w in gw.getAllWindows():
            try:
                if _win_ok(w) and target in _norm(w.title):
                    try:
                        w._guga_kws = (app,)
                    except Exception:
                        pass
                    log(f"按标题模糊匹配到{app}窗口「{w.title}」")
                    focus_window(w, app, quiet=True)
                    return w
            except Exception:
                continue
    return None


def _exe_from_running(kws):
    """从**正在运行的进程**反查 exe 全路径。

    真机教训（2026-11-05）：本机 Kimi/元宝/智谱 全被装到 D 盘非默认位置
    （D:\\Kimi、D:\\Yuanbao、D:\\chatglm），档案里的默认路径三条全落空，
    而且这三款都没在注册表 App Paths 里登记 → 注册表兜底也救不了。
    进程反查是唯一不依赖"猜安装位置"的办法。

    挑主程序的规则：先把 CrashHandler / Helper / webbridge 这类附属进程排掉，
    再优先精确同名（yuanbao.exe 优于 YuanbaoCrashHandler.exe）。"""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        arr = (wintypes.DWORD * 8192)()
        need = wintypes.DWORD()
        if not psapi.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(need)):
            return None
        cnt = need.value // ctypes.sizeof(wintypes.DWORD)
        kws_l = [str(k).lower() for k in kws]
        exact = []
        loose = []
        for i in range(cnt):
            pid = arr[i]
            if not pid:
                continue
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                continue
            try:
                size = wintypes.DWORD(1024)
                buf = ctypes.create_unicode_buffer(1024)
                if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    continue
                p = buf.value
                base = os.path.basename(p).lower()
                if not base.endswith(".exe"):
                    continue
                if not any(k in base or k in p.lower() for k in kws_l):
                    continue
                if any(x in base for x in ("crash", "helper", "update", "webbridge",
                                           "uninstall", "elevate", "launcher", "-cu")):
                    continue
                stem = base[:-4]
                if stem in kws_l or any(stem == k for k in kws_l):
                    exact.append(p)
                else:
                    loose.append(p)
            except Exception:
                pass
            finally:
                k32.CloseHandle(h)
        for p in exact + loose:
            if os.path.exists(p):
                return p
    except Exception as e:
        log(f"进程反查 exe 失败: {e}")
    return None


def _exe_from_registry(app):
    """Windows App Paths 注册表：软件自己登记的启动入口，比猜安装目录可靠得多。"""
    try:
        import winreg
        exts = [app, app + ".exe"]
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for name in exts:
                try:
                    with winreg.OpenKey(root, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{name}") as k:
                        p = winreg.QueryValueEx(k, "")[0]
                        if p and os.path.exists(p):
                            return p
                except OSError:
                    continue
    except Exception:
        pass
    return None


def _lnk_from_startmenu(app):
    """开始菜单快捷方式：几乎每款软件都在这儿留一个 .lnk，按名字匹配最省事。"""
    roots = [os.path.join(os.environ.get("ProgramData", ""), r"Microsoft\Windows\Start Menu\Programs"),
             os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs")]
    target = re.sub(r"[\s\-_]+", "", app).lower()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if not f.lower().endswith(".lnk"):
                    continue
                if target and target in re.sub(r"[\s\-_]+", "", f[:-4]).lower():
                    return os.path.join(dirpath, f)
    return None


def launch(app="豆包"):
    """启动软件：档案路径 → 注册表 App Paths → 开始菜单快捷方式。
    通用三连的意义：任何软件只要装过，就能被派单拉起，不用逐个写路径。"""
    _privacy_guard(app)
    prof, _known = _profile(app)
    if prof.get("blocked"):
        log(f"{app} 已移出适配名单（{prof['blocked']}），咕嘎不启动它")
        return None
    exe = None
    for p in prof["exe"]:
        p2 = _expand(p)
        if os.path.exists(p2) or p2.lower().endswith("notepad.exe"):
            exe = p2
            break
    if not exe:
        exe = _exe_from_running(prof["win"])
        if exe:
            log(f"进程反查命中{app}真实路径: {exe}")
    if not exe:
        exe = _exe_from_registry(app)
        if exe:
            log(f"注册表 App Paths 命中{app}: {exe}")
    lnk = None
    if not exe:
        lnk = _lnk_from_startmenu(app)
        if lnk:
            log(f"开始菜单快捷方式命中{app}: {lnk}")
    if not exe and not lnk:
        log(f"未在本机探测到{app}的启动入口（注册表/开始菜单/已知路径都没有）")
        return None
    log(f"窗口未找到，启动: {exe or lnk}")
    import subprocess
    try:
        if exe:
            subprocess.Popen([exe])
        else:
            os.startfile(lnk)   # .lnk 必须交给 shell 解析
    except Exception as e:
        log(f"启动失败（{e}）")
        return None
    for _ in range(30):
        time.sleep(2)
        w = find_window(app)
        if w:
            return w
    log(f"60 秒内未等到{app}窗口")
    return None
def _evidence_dir():
    """取证目录必须落在用户可写位置：评委可能把程序装在 Program Files 或只读解压目录。"""
    try:
        from core import guga_paths
        return str(guga_paths.RUNTIME / "evidence")
    except Exception:
        pass
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, "GUGA", "runtime", "evidence")


EVIDENCE_DIR = _evidence_dir()
try:
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
except OSError:
    import tempfile
    EVIDENCE_DIR = os.path.join(tempfile.gettempdir(), "GUGA-evidence")
    try:
        os.makedirs(EVIDENCE_DIR, exist_ok=True)
    except OSError:
        pass   # 最坏情况只影响取证截图，绝不能因此让执行器 import 失败


def log(msg):
    print(f"[咕嘎执行器 {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def focus(w):
    try:
        hwnd = _hwnd_of(w)
        if hwnd and input_guard is not None:
            input_guard.note_target(hwnd)
        if w.isMinimized:
            w.restore()
        w.activate()
    except Exception as e:
        log(f"activate 失败（{e}），用 alt-tab 兜底")
    time.sleep(1)
    # 激活/还原会改矩形：立刻按标题保鲜一次，后续几何全用它
    return refresh_box(w)


def diff_ok(a, b):
    if a is None or b is None:
        return False
    return ImageChops.difference(a, b).getbbox() is not None


def new_chat(w, app="豆包"):
    """步骤0（真机教训）：先开新对话隔离少爷的现有会话。
    视觉定位「新对话按钮」→ 点击 → 视觉验证真开了一个空对话，没开成功换备选坐标重试。
    比例坐标那套（4.7%/17%）是豆包专属的；别的软件绝不瞎点——宁可不开新对话
    （继续在原会话提问），也不能在陌生界面乱点（可能点到菜单、关闭、发送历史消息）。
    注意：视觉找到了按钮但验证没通过，对非豆包同样不能退到比例坐标（上一版在这里漏了）。"""
    _prof, _ = _profile(app)
    # 优先走锚点反推（大字定位 + 固定偏移）：相邻菜单行模型分不清，锚点法确定性高
    _anch = _prof.get("btn_anchor")
    p = None
    if _anch:
        try:
            pa = locate(w, _anch["desc"])
            if pa:
                x, y, cw, ch = win_rect(w)
                p = (pa[0], pa[1] + int(ch * _anch["dy"]))
                log(f"锚点「{_anch['desc'][:14]}…」@{pa} → 反推目标 {p}")
        except Exception as e:
            log(f"锚点定位异常（{e}），退回直接定位")
    if not p:
        _btn_desc = _prof.get("new_chat_btn") or "「新对话」或「新建对话」按钮（通常是侧边栏顶部带 + 号的按钮）"
        p = locate_deep(w, _btn_desc,
                        [(0, 0, 0.34, 0.45)])   # 找不到就只放大左侧栏再看，别让全窗小图标糊掉
    # 判空措辞要按客户端类型改：Kimi/智谱是"任务"导向，新建出来的是任务页，
    # 列表里显示「[任务]xxx」是正常的，不是历史消息残留。用原来那句"全新空对话"
    # 会让这两款永远判否 → 每次都走跳过隔离的分支，会话一直叠在上一个任务里。
    _empty_q = _prof.get("new_chat_hint") or \
        f"当前{app}是否已切换到一个全新的空对话（对话区没有历史消息）？"
    if p:
        log(f"视觉定位「新对话」按钮 {p}，隔离少爷的现有会话")
        pyautogui.click(p[0], p[1])
        time.sleep(1.5)
        if verify(w, _empty_q):
            evidence(w, "step_new_chat")
            return
        if app != "豆包":
            log(f"{app} 的「新对话」点了但没验证通过，跳过隔离（不再按比例坐标盲点），直接在当前会话提问")
            evidence(w, "step_new_chat_skip")
            return
        log("新对话疑似没点开，按比例坐标重试一次")
    elif app != "豆包":
        log(f"视觉没定位到{app}的「新对话」按钮，跳过隔离（不在陌生界面瞎点），直接在当前会话提问")
        evidence(w, "step_new_chat_skip")
        return
    x, y, cw, ch = win_rect(w)
    nx, ny = x + int(cw * 0.047), y + int(ch * 0.17)
    log(f"点击「新对话」({nx},{ny})，隔离少爷的现有会话")
    pyautogui.click(nx, ny)
    time.sleep(1.5)
    evidence(w, "step_new_chat")


def click_input(w, app="豆包"):
    """KB2 步骤1：点击底部输入框正中。视觉定位优先，失败退回比例坐标。
    真机教训：偏低会误触功能按钮栏；空输入框点击可能无画面变化——
    真正的校验交给 send_text 的"粘贴后文字进入"检查，这里只负责点。
    返回实际点击的屏幕坐标（粘贴校验区必须以它为中心，写死比例区会校验到空气）。
    比例坐标兜底只给豆包用：未知软件的界面上盲点一下可能点到"删除/关闭"，宁可如实报错。"""
    p = locate_deep(w, "底部的对话输入框（用户打字的地方，不含功能按钮栏）",
                    [(0, 0.55, 1, 0.45)])   # 底部放大再看：输入框是最关键的一个点，值得多花一次视觉
    if p:
        log(f"视觉定位输入框 {p}")
        pyautogui.click(p[0], p[1])
    elif app == "豆包":
        x, y, cw, ch = win_rect(w)
        bx = x + int(cw * 0.42)
        by = y + ch - int(95 * _dpi_scale(w))   # 固定 95px 只在 96dpi 成立
        p = (bx, by)
        log(f"视觉没定位到，按比例坐标点输入框 ({bx},{by})")
        pyautogui.click(bx, by)
    else:
        raise RuntimeError(f"视觉没定位到{app}的输入框——不在未识别的界面上盲点（避免误触删除/关闭），请人工确认后重试")
    time.sleep(0.8)
    return p


_DPI_SCALE = [1.0]


def _dpi_scale(w=None):
    """当前窗口所在显示器的缩放系数（96dpi = 1.0）。

    为什么需要：校验区、点击偏移这些是**固定像素**，只在 96dpi 成立。
    150% 缩放的机器上同样的界面元素物理尺寸是 1.5 倍，190px 的校验区只盖住中间 2/3，
    文字贴在左边就"看不见变化"→ 判成假发送 → 反复重贴。这跟分辨率失效是同一个根：
    把 UI 尺寸当常量。"""
    if w is not None:
        try:
            hwnd = getattr(w, "_hWnd", None)
            if hwnd:
                d = ctypes.windll.user32.GetDpiForWindow(int(hwnd))
                if d:
                    _DPI_SCALE[0] = max(1.0, min(3.0, d / 96.0))
                    return _DPI_SCALE[0]
        except Exception:
            pass
    return _DPI_SCALE[0]


def _clip_zone(pt, rect, half_w=190, up=34, down=44):
    """以点击点为中心裁一块校验区，全部夹在窗口范围内。rect=(x, y, w, h)。
    治本两条：①点击点不在窗口矩形内（窗口框漂移/最小化恢复后坐标过期）时，
    退回窗口底部输入区锚定——绝不产出 x2<x1/y2<y1 的颠倒矩形崩掉 pyscreeze；
    ②结果再夹一次屏幕范围，双保险；③区域尺寸随 DPI 缩放，不假设 96dpi。"""
    if not rect:
        return None
    s = _dpi_scale()
    half_w, up, down = int(half_w * s), int(up * s), int(down * s)
    edge, band = int(36 * s), int(70 * s)
    x, y, cw, ch = rect
    inside = x <= pt[0] <= x + cw and y <= pt[1] <= y + ch
    if not inside:
        log(f"点击点 {pt} 已跑出窗口矩形 {rect}，窗口框漂移——校验区退回窗口底部输入区")
        x1 = x + int(cw * 0.18)
        x2 = x + int(cw * 0.82)
        y2 = y + ch - edge
        y1 = y2 - band
    else:
        x1 = max(x, pt[0] - half_w)
        y1 = max(y, pt[1] - up)
        x2 = min(x + cw, pt[0] + half_w)
        y2 = min(y + ch, pt[1] + down)
    if x2 - x1 < 40 * s or y2 - y1 < 20 * s:   # 理论到不了：再小就退窗口底部输入带
        x1, x2 = x + 10, x + cw - 10
        y1, y2 = y + ch - int(110 * s), y + ch - edge
    return _sanitize((x1, y1, x2 - x1, y2 - y1))


def _bottom_strip(rect):
    """窗口底部输入带（校验区算不出来时的兜底）。比"退整窗"强：
    整窗包含侧边栏/动画，任何风吹草动都会被当成"字贴进去了"，那是假发送的温床。"""
    if not rect:
        return None
    s = _dpi_scale()
    x, y, cw, ch = rect
    h = int(74 * s)
    return _sanitize((x + int(cw * 0.18), y + ch - int(110 * s), int(cw * 0.64), h))


def send_text(w, text, app="豆包"):
    """KB2 步骤2-3：粘贴 → 文字进入校验 → Enter 发送。粘贴未进入自动重试 3 次（同一坐标重点）。
    三次都"看不见字"时先让视觉模型看一眼输入框是否其实已有大段文字（治校验区假阴性），
    视觉也否认才判定假发送停手——宁可多问一句，不冤枉一次真粘贴。"""
    w = refresh_box(w)   # 窗口被移动/恢复过的话，旧矩形会让定位、点击、校验区全错
    _dpi_scale(w)        # 先按这扇窗所在屏刷新缩放系数，后面的固定像素尺寸都乘它
    pt = click_input(w, app)
    zone = _clip_zone(pt, win_rect(w)) or _bottom_strip(win_rect(w))
    keep = ""
    try:
        keep = pyperclip.paste() or ""     # 发送后要还回去，见函数末尾
    except Exception:
        pass
    typed = False
    for attempt in range(3):
        if attempt:                       # 重试重点同一坐标（换视觉重定位只会拿到漂移坐标，越点越散）
            pyautogui.click(pt[0], pt[1])
            time.sleep(0.5)
        before = shot(zone)
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.9)
        after = shot(zone)
        typed = diff_ok(before, after)
        log(f"第 {attempt + 1} 次粘贴，输入框文字校验: {'有字 ✓' if typed else '未检测到变化 ⚠，重贴'}")
        if typed:
            break
    if not typed:
        ans = see(w, "当前输入框里是否已经有一大段文字（刚粘贴进去的提问内容）？只回答：是 或 否。")
        typed = ans.startswith("是")
        log(f"像素校验三次未见变化，视觉复核输入框：{'有字，放行发送' if typed else '确认没贴进去'}")
    if not typed:
        raise RuntimeError("三次粘贴后输入框仍无变化（像素+视觉双重确认），疑似假发送，按 KB2 异常分支停手")

    pyautogui.press("enter")
    log(f"已发送：「{text[:50]}…」" if len(text) > 50 else f"已发送：「{text}」")
    time.sleep(2)
    # 发完立刻还原剪贴板：否则 read_reply 的剪贴板兜底会把刚发出去的提问当成回复
    # （2026-11-07 干净环境实测：107 字的【总目标】被当 200 字作文交付）。
    try:
        pyperclip.copy(keep)
    except Exception:
        pass
    return typed


def _diff_area(a, b):
    """两图差异的面积（像素²）。流式回答里闪烁光标/小动画只贡献小面积差异，
    用面积阈值把它们当"没变化"，否则"连续 8 秒无变化"永远等不到。"""
    if a is None or b is None:
        return 0
    bbox = ImageChops.difference(a, b).getbbox()
    if not bbox:
        return 0
    return (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])


def _sig_area():
    """判定"画面真的动了"的最小变化面积。光标闪烁/微动画在这个量级以下。
    面积随 DPI 平方缩放：150% 缩放下同一处闪烁的像素数是 1.0 时的 2.25 倍，
    写死 1200 会让光标闪烁被当成"回答开始输出"，白等一整个超时。"""
    s = _dpi_scale()
    return int(1200 * s * s)


def wait_answer(w, max_wait=120):
    """KB2 步骤4：等回答开始（画面动）→ 显著画面连续 8 秒不动 = 流式结束。
    面积阈值滤掉光标闪烁类伪变化；返回 False 只代表"没等到稳定"，调用方仍应尝试读回复。"""
    x, y, cw, ch = win_rect(w)
    _dpi_scale(w)        # 变化面积阈值要按这扇窗所在屏缩放，否则光标闪烁会被当成"开始输出"
    region = (x, y + int(ch * 0.25), cw, int(ch * 0.6))
    started = False
    prev = shot(region)
    if prev is None:
        log("回答区截不到图，跳过像素等待（交给读回复裁决）")
        return False
    stable = 0
    for i in range(max_wait // 2):
        time.sleep(2)
        cur = shot(region)
        if cur is None:
            log("回答区截图间歇失败，本轮跳过")
            continue
        sig = _diff_area(prev, cur) > _sig_area()   # 光标闪烁 ≈几十~几百 px²（随 DPI 缩放），直接无视
        prev = cur
        if not started:
            if sig:
                started = True
                log(f"检测到回答开始输出（{(i + 1) * 2} 秒）")
            continue
        if sig:
            stable = 0
        else:
            stable += 1
            if stable >= 4:
                log(f"回答画面连续 8 秒无显著变化，判定输出结束（共 {(i + 1) * 2} 秒）")
                return True
    return False


def _flat(txt):
    return ''.join(str(txt or '').split())


def is_prompt_echo(got, sent):
    """读回的内容是不是刚发出去的提问本身。

    真因（2026-11-07 干净环境实测）：send_text 把提问写进了剪贴板，回答还没出来时
    read_reply 的剪贴板兜底读到的是提问文本，于是 107 字的【总目标】被当成
    “200 字作文”的产物交付。产物必须是对方产出的回复，而不是自己发出去的题面。"""
    a, b = _flat(got), _flat(sent)
    if not a or not b:
        return False
    if a == b:
        return True
    return b[:20] in a or a.startswith(b[:12])


def copy_answer(w, old_clip, sent=""):
    """doubao-copy 路径A：悬停让工具栏浮现 → 差异定位工具栏 → 点最左复制图标 → 剪贴板校验。
    兜底：三连击选中段落 + Ctrl+C。返回取到的文本（失败返回 ''）。"""
    x, y, cw, ch = win_rect(w)
    chat_zone = (x + int(cw * 0.18), y + int(ch * 0.30), int(cw * 0.55), int(ch * 0.45))
    hover_x = chat_zone[0] + chat_zone[2] // 2
    hover_y = chat_zone[1] + int(chat_zone[3] * 0.55)

    base = shot(chat_zone)
    pyautogui.moveTo(hover_x, hover_y)
    time.sleep(1.0)
    after = shot(chat_zone)
    bbox = ImageChops.difference(base, after).getbbox() if (base is not None and after is not None) else None
    if bbox:
        # 差异带 = 浮现的工具栏。复制通常是工具栏最左侧图标（偏移随 DPI 缩放）。
        tx = chat_zone[0] + bbox[0] + int(14 * _dpi_scale(w))
        ty = chat_zone[1] + (bbox[1] + bbox[3]) // 2
        log(f"工具栏差异带 {bbox}，点击最左图标 ({tx},{ty})")
        pyautogui.click(tx, ty)
        time.sleep(1.0)
        got = (pyperclip.paste() or "").strip()
        if is_prompt_echo(got, sent):
            log("剪贴板里是我们刚发出去的提问，不是回答——判为未取到回复")
            got = ""
        if got and got != old_clip:      # 原来是 ≥20 字：把"好的""是"这种正常短答案判成失败
            log(f"剪贴板校验通过（{len(got)} 字），工具栏复制成功")
            return got
        log("工具栏点击未取到文本，转兜底方案")

    # 兜底：三连击选中回答内一段 → Ctrl+C
    log("兜底：三连击选段 + Ctrl+C")
    pyautogui.click(hover_x, hover_y, clicks=3, interval=0.15)
    time.sleep(0.4)
    pyautogui.hotkey("ctrl", "c")
    time.sleep(0.8)
    got = (pyperclip.paste() or "").strip()
    if is_prompt_echo(got, sent):
        log("兜底复制拿到的仍是提问本身，不是回复")
        got = ""
    if got and got != old_clip:
        log(f"兜底复制成功（{len(got)} 字）")
        return got
    log("复制失败（KB2 异常分支：回报少爷）")
    return ""


def verify(w, question):
    """动作后验证（借鉴 UI-TARS 的 verify 循环）：截图问视觉模型"这一下点成了吗"，只答 是/否。"""
    ans = see(w, question + " 只回答：是 或 否。")
    ans = re.sub(r'<think>[\s\S]*?</think>', '', ans or '').strip()   # 思考泄漏会让 startswith('是') 误判
    ok = ans.startswith("是")
    log(f"动作验证：{'✓' if ok else '✗'}（视觉回答：{ans[:30]}）")
    return ok


def evidence(w, tag):
    """取证截图：本地存档 + 上传网页证据墙。截图失败只记日志，绝不影响任务成败
    ——证据是旁证，不是任务的一部分。"""
    fp = os.path.join(EVIDENCE_DIR, f"{tag}.png")
    img, _ = grab(win_rect(w))
    if img is not None:
        try:
            img.save(fp)
            log(f"证据截图已存: {fp}")
        except Exception as e:
            log(f"证据截图存盘失败（{e}），不影响任务")
    if TOKEN and CTX:
        try:
            _api("/api/agent/evidence", {"taskId": CTX.get("taskId"), "stId": CTX.get("stId"),
                                         "tag": tag, "image": _shot_window(w)["b64"]}, timeout=30)
        except Exception as e:
            log(f"证据上传失败（不影响执行）: {e}")
    return fp


def _usable_reply(txt):
    """视觉转写算不算"读到了回复"。原来卡 20 字，把"好的""方案 A"这类正常短答案也判失败，
    白白退到剪贴板路径（还会抢鼠标）。现在放开长度，只挡"看不清/无法识别"这类无效回答。"""
    t = (txt or "").strip()
    if len(t) < 2:
        return False
    if len(t) <= 14 and any(b in t for b in ("无法识别", "看不清", "看不到", "没有看到",
                                             "抱歉", "无法读取", "不清晰", "图片中")):
        return False
    return True


def read_reply(w, old_clip, tag, idx, app="豆包", sent=""):
    """读 AI 软件的最新回复。视觉转写优先（不动鼠标、不怕工具栏不浮现），
    看不清再退回悬停点复制/三连击兜底。返回文本（失败 ''）。
    sent 是本轮刚发出去的提问：读回内容若与它相同/以它开头，说明拿到的是提问回显，
    不是回复——一律判未取到（这是 2026-11-07 假交付的直接原因）。"""
    txt = see(w, f'完整转写{app}对话区里最新一条 AI 回复的正文，输出纯文本/Markdown，'
                 '不要界面按钮文字，不要你自己的评论。若最新回复是在向我提问，也原样完整转写。')
    if _usable_reply(txt) and not is_prompt_echo(txt, sent):
        evidence(w, f"{tag}_{idx}_vision")
        log(f"视觉转写成功（{len(txt)} 字），不动鼠标直接读到了")
        return txt
    if _usable_reply(txt):
        log("视觉转写拿到的是本轮提问本身（提问回显），不算回复，改走剪贴板路径")
    log("视觉转写不可用，回退剪贴板复制（悬停最新回复）")
    # 最新回复在对话区底部：悬停/兜底复制都取低处（旧版中区会悬停到旧消息）
    x, y, cw, ch = win_rect(w)
    zone = (x + int(cw * 0.18), y + int(ch * 0.55), int(cw * 0.55), int(ch * 0.30))
    hx = zone[0] + zone[2] // 2
    hy = zone[1] + int(zone[3] * 0.6)
    got = ""
    try:
        base = shot(zone)
        pyautogui.moveTo(hx, hy)
        time.sleep(1.0)
        after = shot(zone)
        bbox = ImageChops.difference(base, after).getbbox() if (base is not None and after is not None) else None
        if bbox:
            tx = zone[0] + bbox[0] + 14
            ty = zone[1] + (bbox[1] + bbox[3]) // 2
            log(f"回复区工具栏差异带 {bbox}，点最左复制图标 ({tx},{ty})")
            pyautogui.click(tx, ty)
            time.sleep(1.0)
            got = pyperclip.paste() or ""
            if len(got) < 20 or got == old_clip or is_prompt_echo(got, sent):
                got = ""
    except Exception as e:
        log(f"工具栏复制异常（{e}），转兜底")
        got = ""
    if not got:
        log("兜底：三连击选段 + Ctrl+C（取最新回复）")
        pyautogui.click(hx, hy, clicks=3, interval=0.15)
        time.sleep(0.4)
        pyautogui.hotkey("ctrl", "c")
        time.sleep(0.8)
        got = pyperclip.paste() or ""
        if len(got) < 20 or got == old_clip or is_prompt_echo(got, sent):
            got = ""
    evidence(w, f"{tag}_{idx}_copy")
    return got


def ask_round(w, text, old_clip, tag, idx, app="豆包"):
    """单轮真机对话：视觉定位输入框 → 粘贴提问 → 等流式结束 → 读回复（视觉优先）。
    返回 (ok, reply)。回复会交给大脑判读（deliverable / followup），绝不盲交。
    等待超时不再直接判死：稳定判定可能被动画干扰永远不收敛，读回复才是真正的裁决。
    点击只由 send_text 做一次（之前这里多点一次，既抢鼠标又让视觉坐标漂移）。"""
    w = refresh_box(w)   # 每轮开工先保鲜矩形：等回答/取证/读回复的区域换算全靠它
    send_text(w, text, app)
    if not wait_answer(w):
        log(f"第 {idx} 轮等待超时——先让视觉读一次回复再判死（可能只是画面动画干扰了稳定判定）")
    evidence(w, f"{tag}_{idx}_ask")
    got = read_reply(w, old_clip, tag, idx, app, sent=text)
    # 读回提问回显/空 = 回答还没出来：再等 4 秒重读一次（绝不把题面当产物交付）
    if not got:
        log(f"第 {idx} 轮没读到回复，4 秒后再读一次")
        time.sleep(4)
        got = read_reply(w, old_clip, tag, idx, app, sent=text)
    if got:
        log(f"已读到{app}回复（{len(got)} 字），交给大脑判读")
    else:
        log(f"未能从{app}取到本轮回复（已排除提问回显，不拿题面冒充产物）")
    return bool(got), got


def run_chain(q1=None, q2=None):
    """完整链路：ask → followup → copy。可被 guga_agent 程序化调用，返回结果 dict。"""
    q1 = q1 or (sys.argv[1] if len(sys.argv) > 1 else "用一句话介绍一下你自己")
    q2 = q2 or (sys.argv[2] if len(sys.argv) > 2 else "针对你刚才的回答，再补充一个具体例子")
    tag = time.strftime("doubao-chain_%H%M%S")
    result = {"chain": ["doubao-ask", "doubao-followup", "doubao-copy"], "steps": []}
    old_clip = ""
    try:
        old_clip = pyperclip.paste() or ""
    except Exception:
        pass
    try:
        w = find_window() or launch()
        w = focus(w)
        result["steps"].append({"step": "唤出窗口", "ok": True})

        # ── doubao-ask ──
        new_chat(w)
        result["steps"].append({"step": "ask:新对话隔离", "ok": True})
        send_text(w, q1)
        ok1 = wait_answer(w)
        evidence(w, f"{tag}_1_ask")
        result["steps"].append({"step": "ask:提问+等回答", "ok": ok1})
        if not ok1:
            raise RuntimeError("ask:第一轮回答超时")

        # ── doubao-followup（同一会话，不开新对话）──
        send_text(w, q2)
        ok2 = wait_answer(w)
        evidence(w, f"{tag}_2_followup")
        result["steps"].append({"step": "followup:同会话追问+等回答", "ok": ok2})
        if not ok2:
            raise RuntimeError("followup:第二轮回答超时")

        # ── doubao-copy ──
        text = copy_answer(w, old_clip, sent=q2)
        evidence(w, f"{tag}_3_copy")
        result["steps"].append({"step": "copy:取回答案", "ok": bool(text), "chars": len(text)})
        result["copied_head"] = text[:120]
        result["copied"] = text
        result["success"] = bool(text)
    except Exception as e:
        result["success"] = False
        result["error"] = str(e)
        log(f"执行异常: {e}")
    finally:
        try:
            pyperclip.copy(old_clip)   # 恢复剪贴板
        except Exception:
            pass

    out = os.path.join(EVIDENCE_DIR, f"{tag}_result.json")
    io.open(out, "w", encoding="utf-8").write(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


# ==================== KB6 开屏感知 & KB7 文件喂入（2026-11-05） ====================
# 知识来源: knowledge/sense/*-sense.md（界面状态辨认要点）与 knowledge/flows/*-io.md（文件入口实测）
# 原则: 感知是增强不是依赖——文档读不到/视觉失败时一律回退原流程, 绝不让知识库故障挡住执行。

_KB_SENSE_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'knowledge', 'sense'))
_APP_SLUGS = [('豆包', 'doubao'), ('kimi', 'kimi'), ('元宝', 'yuanbao'),
              ('智谱', 'zhipu'), ('清言', 'zhipu')]


def _sense_doc(app):
    """读 KB6 感知文档(去截图引用行, 控长度)。读不到返回空串——调用方走原流程。"""
    low = (app or '').lower()
    slug = 'generic'
    for k, s in _APP_SLUGS:
        if k in (app or '') or k in low:
            slug = s
            break
    path = os.path.join(_KB_SENSE_DIR, ('generic' if slug == 'generic' else slug) + '-sense.md')
    try:
        with io.open(path, encoding='utf-8') as f:
            txt = f.read()
        lines = [l for l in txt.splitlines() if 'imgs/' not in l]   # 截图引用对模型无意义
        return '\n'.join(lines)[:6000]
    except Exception as e:
        log(f"KB6 感知文档读不到（{os.path.basename(path)}: {e}），开屏感知降级跳过")
        return ''


def sense_screen(w, app=""):
    """开屏感知: 截全窗 + KB6 辨认要点 → 视觉模型判状态。
    返回 {state, ambiguous, note, question}; 失败返回 unknown(不抛异常, 不挡流程)。"""
    doc = _sense_doc(app)
    if not doc:
        return {'state': 'unknown', 'ambiguous': False, 'note': '无KB6文档', 'question': ''}
    try:
        info = _shot_window(w)
    except Exception as e:
        log(f"开屏感知截图失败（{e}）")
        return {'state': 'unknown', 'ambiguous': False, 'note': '截图失败', 'question': ''}
    q = ('这是「%s」的窗口截图，请对照《界面状态辨认要点》判断当前状态。\n'
         'state 只允许: empty_chat / ongoing_chat / workspace_picker / history_sidebar / dialog_popup / unknown。\n'
         '只回 JSON（不要多余文字）: {"state":"...","ambiguous":true或false,'
         '"note":"一句话依据","question":"若 state=dialog_popup 给出该点的关闭按钮文字; 否则空串"}\n\n'
         '《界面状态辨认要点》：\n%s') % (app or '目标软件', doc)
    ans = _vision(info['b64'], q, timeout=90)
    ans = re.sub(r'<think>[\s\S]*?</think>', '', ans or '')   # 视觉模型偶发思考泄漏，只留结论
    try:
        m = re.search(r'\{[^{}]*\}', ans, re.S)
        d = json.loads(m.group(0))
        d.setdefault('state', 'unknown')
        d.setdefault('ambiguous', False)
        d.setdefault('note', '')
        d.setdefault('question', '')
        return d
    except Exception:
        return {'state': 'unknown', 'ambiguous': False, 'note': (ans or '')[:120], 'question': ''}


def close_popup(w, app="", hint=""):
    """关弹窗: 优先按 KB6 给的字样定位, 找不到按通用措辞。成功返回 True。"""
    descs = []
    if hint:
        descs.append("弹窗上写着「%s」的按钮" % hint)
    descs.append("弹窗/卡片上的「暂不添加」「暂不」「跳过」「我知道了」「关闭」按钮（绝不要点「立即添加」「设为默认」「开启」这类确认按钮）")
    for desc in descs:
        p = locate(w, desc)
        if p:
            pyautogui.click(p[0], p[1])
            time.sleep(1.0)
            evidence(w, "popup_closed")
            log("弹窗已处理: %s" % desc[:30])
            return True
    log("弹窗关闭按钮没定位到，不动（宁可留着也不乱点）")
    return False


# ---- KB7 文件喂入（入口全部来自 flows/*-io.md 实测记录；豆包入口未定位故无配置） ----
_ATTACH_PROFILES = {
    'kimi': {'type': 'hotkey', 'keys': ('ctrl', 'u'),
             'desc': 'Ctrl+U 直呼系统文件框（kimi-io 实测入口）'},
    '元宝': {'type': 'plus_menu', 'menu': '本地文件',
             'desc': '「+」→「本地文件」（yuanbao-io 实测入口）'},
    '智谱': {'type': 'plus_menu', 'menu': '上传文件或图片',
             'desc': '「+」浮层 →「上传文件或图片」（zhipu-io 实测入口）'},
}


def _wait_file_dialog(timeout=6.0):
    """等系统文件选择对话框(类名 #32770)出现。"""
    try:
        user32 = ctypes.windll.user32
    except Exception:
        return None
    t0 = time.time()
    while time.time() - t0 < timeout:
        h = user32.FindWindowExW(None, None, '#32770', None)
        if h and user32.IsWindowVisible(h):
            return h
        time.sleep(0.3)
    return None


def attach_file(w, app, path):
    """把本地文件喂给 {app}。返回 (ok, msg)。五步校验: 入口→文件框→路径→上屏→取证。"""
    prof = None
    try:
        _p, _known = _profile(app)
        a = _p.get('attach') or {}
        if a.get('hotkey'):
            keys = tuple(a['hotkey'].split('+'))
            prof = {'type': 'hotkey', 'keys': keys, 'desc': '快捷键 %s（档案实测入口）' % a['hotkey']}
        elif a.get('menu_item'):
            prof = {'type': 'plus_menu', 'menu': a['menu_item'],
                    'desc': '「+」→「%s」（档案实测入口）' % a['menu_item']}
    except Exception:
        pass
    if not prof:
        _app_low = (app or '').lower()
        for k, v in _ATTACH_PROFILES.items():
            if k in _app_low:
                prof = v
                break
    if not prof:
        return False, "%s 的文件喂入入口未在 KB7 建档（豆包入口实测未定位），本步需降级" % (app or '该软件')
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        return False, "要喂的文件不存在: %s" % path
    try:
        # 窗口句柄保鲜 + 前台锁定：最多试 3 轮
        # 注意 focus_window 返回 True/False 布尔（不是窗口对象），保鲜用 focus()；
        # focus_window 内部已含 Alt+SetFG/SwitchToThisWindow/借前台线程三招抢前台，
        # 返回 True 即 GetForegroundWindow==目标 hwnd，无需再自校验。
        for attempt in range(3):
            w = _refind(w) or find_window(app) or w
            w = focus(w)                      # 激活并按标题保鲜矩形，返回窗口对象
            if not w:
                time.sleep(1.2)
                continue
            if focus_window(w, app):
                break
            time.sleep(1.0)
        else:
            return False, "喂文件中止：3 轮都没能把目标窗口带到前台（不盲发快捷键）"
        if prof['type'] == 'hotkey':
            pyautogui.hotkey(*prof['keys'])
        else:
            def _in_win(pt):
                try:
                    x, y, cw, ch = win_rect(w)
                    return bool(x <= pt[0] <= x + cw and y <= pt[1] <= y + ch)
                except Exception:
                    return True   # 几何拿不到时不拦截，交给后续校验兜底
            # 元宝窗口会自己漂移（实测 y 从 141 漂到 37）：定位点落到窗外 = 按旧几何算的，重截重定位
            # 「+」是小图标——用 locate_deep 放大输入框区域找，比全窗找可靠得多（视觉 5px 字会糊）
            _plus_desc = "底部对话输入框内部左下角的「+」加号按钮（「发送消息」占位文字左边，紧贴输入框左下边缘）"
            p = locate_deep(w, _plus_desc, [(0.0, 0.62, 1.0, 0.38)])
            if p and not _in_win(p):
                log("「+」定位落在窗口外（窗口漂移），刷新几何重定位")
                w = focus(w)
                p = locate_deep(w, _plus_desc, [(0.0, 0.62, 1.0, 0.38)])
            if not p or not _in_win(p or (0, 0)):
                # 兜底锚点：小图标定位不稳，改用大字「发送消息」占位文字锚定输入框，
                # 「+」就挂在其左侧同一行（实测间距约为窗宽的 2%）
                pt = locate(w, "底部输入框里的「发送消息」占位文字（灰色的提示文字）")
                if pt and _in_win(pt):
                    try:
                        x, y, cw, ch = win_rect(w)
                        p = (max(pt[0] - int(cw * 0.02), x + 8), pt[1])
                        log(f"「+」改用文字锚点偏移定位: {p}")
                    except Exception:
                        pass
            if not p or not _in_win(p or (0, 0)):
                return False, "「+」按钮定位失败或落在窗口外，中止（不盲点）"
            pyautogui.click(p[0], p[1])
            time.sleep(0.9)
            p2 = locate_deep(w, "菜单里的「%s」选项" % prof['menu'], [(0.0, 0.3, 1.0, 0.55)])
            if not p2:
                evidence(w, "attach_popup")   # 留证：浮层里到底有什么，便于校准菜单项文案
                pyautogui.press('esc')   # 收起误开的浮层，不留脏状态
                return False, "「+」浮层里没找到「%s」（浮层内容见证据 attach_popup）" % prof['menu']
            pyautogui.click(p2[0], p2[1])
        dlg = _wait_file_dialog(6.0)
        if not dlg:
            evidence(w, "attach_no_dialog")
            return False, "触发了入口但没等到系统文件选择框"
        import pyperclip
        old = ""
        try:
            old = pyperclip.paste() or ""
        except Exception:
            pass
        pyperclip.copy(path)
        time.sleep(0.2)
        pyautogui.hotkey('ctrl', 'v')
        time.sleep(0.5)
        pyautogui.press('enter')
        time.sleep(2.5)
        try:
            if old:
                pyperclip.copy(old)
        except Exception:
            pass
        verdict = see(w, "窗口里是否已经出现名为「%s」的附件/文件卡片（或其上传进度/缩略图）？只答：是 或 否"
                      % os.path.basename(path))
        evidence(w, "attach_done" if '是' in (verdict or '') else "attach_missing")
        if '是' in (verdict or ''):
            log("文件已喂入 %s（%s）" % (app, prof['desc']))
            return True, "文件已上屏"
        return False, "文件框已确认，但窗口里没看到附件卡片（模型答：%s）" % (verdict or '')[:40]
    except Exception as e:
        return False, "喂文件异常: %s" % e


if __name__ == "__main__":
    run_chain()
