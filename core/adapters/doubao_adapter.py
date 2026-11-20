"""豆包（Doubao 桌面端）适配器：MCP 通道优先，界面自动化兜底。

设计约束（与本仓库既有约定保持一致）：
- 声明文件：``core/adapters/apps/doubao.json``（schema=guga.app_adapter/1）。
  本模块只读取声明，不修改它；字段命名与声明一一对应（install / launch / ready /
  anchors / receipt / login / channel / anchors.failure_codes）。
- 通道顺序：MCP -> UI 自动化。MCP 需要显式配置一个可调用的豆包 MCP 端点
  （``core/adapters/doubao_mcp.json`` 或环境变量 ``GUGA_DOUBAO_MCP_URL``）。
  豆包桌面端的“连接器”是客户端方向（豆包调用外部 MCP），本机没有对外提供
  MCP Server，因此默认情况下 MCP 通道会明确返回 ``mcp_unavailable``，
  此时才回落到界面自动化，并在结果里标注“走的是界面自动化，不是接口调用”。
- 绝不用咕嘎自己的模型代写：任何失败都返回 ``ok=False`` + 明确失败码，
  正文只可能来自豆包窗口剪贴板。

独立自测：
    python -m core.adapters.doubao_adapter --probe
    python -m core.adapters.doubao_adapter --prompt "用豆包写一篇200字作文"
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import psutil
import win32clipboard as _clip
import win32con
import win32gui
import win32process

try:  # 与桌宠共用 OCR / 键鼠原语；独立运行时保持可用
    from core.computer_control import (ComputerControl, _ensure_foreground, _send_keys,
                                       wheel_notches)
    from core import ocr_engine
except Exception:  # pragma: no cover
    ComputerControl = None
    _ensure_foreground = None
    _send_keys = None
    ocr_engine = None

    def wheel_notches(intent, notches=1):
        # 独立运行兜底：与 computer_control.wheel_delta 同一实测符号
        return [-120 if str(intent).strip().lower() in
                ("later", "down", "forward", "next", "bottom", "往下", "向下", "更后")
                else 120] * max(1, int(notches))

from core.paste_verify import input_verified, ensure_plain_mode, sent_mode_check, chip_label

SCHEMA = "guga.doubao_adapter/1"
ROOT = Path(__file__).resolve().parents[2]
DECLARATION_PATH = Path(__file__).with_name("apps") / "doubao.json"
MCP_CONFIG_PATH = Path(__file__).with_name("doubao_mcp.json")
try:  # 证据目录必须落在用户可写位置：安装目录被装进 Program Files 时只读
    from core import guga_paths
    DEFAULT_EVIDENCE_DIR = guga_paths.RUNTIME / "doubao_evidence"
except Exception:  # pragma: no cover - 独立运行时（无 core 包）兜底
    _base = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or Path.home())
    DEFAULT_EVIDENCE_DIR = _base / "GUGA" / "runtime" / "doubao_evidence"
PROTOCOL_URL = "doubaoapp://chat"

# 稳定性参数：本机实测豆包窗口偶发白屏/渲染进程退出，关闭 GPU 合成后稳定。
STABILITY_ARGS = ["--disable-gpu", "--disable-gpu-compositing"]

RETRYABLE = {
    "window_not_found", "window_blank", "window_lost", "input_not_found",
    "readback_mismatch", "send_not_confirmed", "blocked_by_dialog",
    "blocked_by_window", "capture_empty", "answer_in_document_card",
    "timeout", "generate_not_started",
    "ocr_unavailable", "ocr_empty",
    # 滚动结构判定的两种异常状态：各自独立成码，不再混进 capture_empty。
    "scroll_stuck", "scroll_incomplete", "scroll_wrong_target",
    # 注入前焦点/控件同源校验：互不合并，避免“写到别处”被当成普通回读不一致。
    "target_not_focused", "composer_not_found", "input_written_elsewhere",
    # 单轮抓屏/OCR 超时：中断本轮轮询，不再越过总 deadline。
    "capture_timeout",
    # 复制读取（20261110ac）：操作栏定位/复制失败单独成码，不再混成 capture_empty。
    "no_action_bar", "reply_unreadable", "reply_in_card",
}
# 这些失败码一旦发生在 OCR 不可用的机器上，真实原因就是 OCR 不可用，
# 对外必须报 ocr_unavailable（不能只丢一个笼统的 adapter_exception）。
# 注意：send_not_confirmed 不在其中——UIA 降级模式下同样能判断有没有出现新回合，
# 把它改写成 ocr_unavailable 会把“消息没发出去”误导成“去装 OCR 语言包”。
OCR_DEPENDENT_CODES = {
    "window_blank", "input_not_found", "readback_mismatch",
    "capture_empty", "timeout", "generate_not_started",
}
# 目标窗口本身不可用（白屏/渲染异常/几乎全在屏外/停在非对话页）：给专门码，
# 文案要求用户“重启该软件”，并附上实测到的页面地址与窗口状态。
TARGET_WINDOW_CODE = "target_window_abnormal"
_NON_CHAT_URL_HINTS = ('/skills', '/setting', '/plugin', '/discover', '/mall')

# 取正文的每一档复制路径都要有硬超时：20261110t 真机现场出现过 UIA 复制路径
# 卡在阻塞调用上、整个取正文线程不再返回（既不出结果也不报错），链路只能被强杀。
# 单档超时到了就放弃该档进入下一档；整体超时到了就不再开新档，统一返回 capture_empty。
CAPTURE_PATH_TIMEOUT = {
    "action_bar": 25.0,
    "uia_button": 25.0,
    "ocr_anchor": 20.0,
    "ocr_more_menu": 20.0,
    "select_all": 15.0,
}
CAPTURE_TOTAL_TIMEOUT = 75.0

#: 视口自适应阈值：客户区小于屏幕工作区的这些比例就视为“小窗口”。
#: 取值理由：豆包长回答至少需要 ~60% 宽、~65% 高才不会被挤成两三行/屏，
#: 低于这个比例时“视口里看不到”会频繁被误当成“内容没生成完”，反复重发。
VIEWPORT_MIN_WIDTH_RATIO = 0.60
VIEWPORT_MIN_HEIGHT_RATIO = 0.65
#: 放大失败时按工作区比例设置的目标尺寸：留出任务栏/边框，客户区仍高于阈值。
VIEWPORT_TARGET_RATIO = 0.92

#: 滚动到底的三个结构状态（互斥）：分别对应不同结论与失败码，不合并成 capture_empty。
SCROLL_AT_BOTTOM = "at_bottom"          # 已到底（ScrollPattern 100% / 箭头消失 / 文本稳定）
SCROLL_STILL_MOVING = "scrolling"       # 仍在滚动中（轮数或预算用尽时视口还在变化）
SCROLL_STUCK = "scroll_stuck"           # 滚不动但未到底（异常：UIA 明说没到底/箭头还在）
#: 滚轮没落到正文容器上（三个正文落点都无效）：与“已到底”“滚不动”分开报。
SCROLL_WRONG_TARGET = "scroll_wrong_target"
#: 同一个落点连续无效多少轮就换正文区另一个位置。
SCROLL_POINTER_INVALID_ROUNDS = 2
#: 兜底判定“已到底”所需的连续稳定轮数（视口文本哈希不变）。
SCROLL_STABLE_ROUNDS = 3

#: 点“复制”前先写进剪贴板的哨兵：读回它说明点击没落到复制按钮上，剪贴板没变化。
COPY_SENTINEL = "__GUGA_COPY_SENTINEL__"

#: 剪贴板恢复时允许出现的系统派生格式（设置文本格式后 Windows 会自动派生这些）。
_TEXT_DERIVED_FORMATS = frozenset((
    win32con.CF_TEXT, 7,                       # CF_OEMTEXT
    win32con.CF_UNICODETEXT, 16,               # CF_LOCALE
))

#: 能确认“确实点到了回答下方复制按钮/复制菜单项”的取正文路径。
#: document_card 是点卡片自身的复制入口，同样属于“来源可确认”。
COPY_BUTTON_PATHS = ("action_bar", "uia_button", "ocr_anchor", "ocr_more_menu", "document_card")

#: 回答操作栏的几何特征（20261110ac 实测）：一排 24×24 Button，同一 top（±4px），
#: 位于回答正文末行下方 0~60px；最左那个就是“复制”，name 为空串。
ACTION_BAR_MIN_BUTTONS = 3
ACTION_BAR_TOLERANCE = 4
ACTION_BAR_MIN_SIZE = 18
ACTION_BAR_MAX_SIZE = 62
ACTION_BAR_MAX_GAP = 70

#: 豆包把长正文放进“在线文档卡片”时界面上出现的标记（20261110w 现场：
#: 对话里只有约 100 字摘要，18 页正文在卡片里，还弹出“暂不支持当前场景打开”）。
DOCUMENT_CARD_MARKERS = ("文档卡片", "查看文档", "打开文档", "文档预览",
                         "暂不支持当前场景打开")
DOCUMENT_CARD_WEAK_MARKERS = ("在线文档",)
DOCUMENT_CARD_OVERLAY_MARKERS = ("暂不支持当前场景打开", "暂不支持打开")
#: 卡片自身复制入口的候选文案。
DOCUMENT_CARD_COPY_NAMES = ("复制", "复制全文", "复制内容", "复制文档", "复制文本")

#: 降级放行的最低正文长度与结构特征种类数（OCR 覆盖不足时才用，见 _degrade_reason）。
DEGRADE_MIN_CHARS = 120
DEGRADE_MIN_STRUCTURE = 2

_STRUCTURE_PATTERNS = (
    ("标题", re.compile(r"^#{1,6}\s+\S", re.M)),
    ("编号", re.compile(r"^\s*\d+[.、)）]\s*\S", re.M)),
    ("列表", re.compile(r"^\s*[-*•·]\s+\S", re.M)),
    ("章节", re.compile(r"^\s*第[一二三四五六七八九十百零\d]+[章节页部分]", re.M)),
    ("中文序号", re.compile(r"^\s*[一二三四五六七八九十]+[、.]\s*\S", re.M)),
    ("加粗", re.compile(r"^\s*(?:\*\*|__).+(?:\*\*|__)\s*$", re.M)),
)

#: 回答块 OCR 区域里要剔除的界面词（复制/重新生成/分享等操作行）。
_ANSWER_REGION_NOISE = (
    "复制", "重新生成", "重新回答", "继续生成", "停止生成", "点赞", "踩",
    "分享", "更多", "下载", "发送", "新对话", "对话", "工作", "豆包",
    "内容由AI生成", "内容由 AI 生成", "回到底部", "回到底", "到底部",
)


class BlockedByWindow(RuntimeError):
    """目标坐标被别的窗口占着，点了就会点到别人身上。"""
    code = "blocked_by_window"


class CaptureTimeout(RuntimeError):
    """单轮截图/识别超过预算：中断本轮并按 capture_timeout 收口。"""
    code = "capture_timeout"


class ComposerNotFound(RuntimeError):
    """按同一个 UIA 控件回读不到输入框内容：宁可报错，也不拿别处文本充数。"""
    code = "composer_not_found"

DEFAULT_DECLARATION = {
    "schema": "guga.app_adapter/1",
    "id": "doubao",
    "name": "豆包",
    "process_names": ["Doubao.exe"],
    "launch": {"exe_name": "Doubao.exe", "args": [], "timeout_seconds": 25, "single_instance": True},
    "ready": {"min_width": 320, "min_height": 240, "extra_settle_seconds": 0.5},
    "anchors": {
        "input": {
            "name_contains": ["发消息", "输入", "提问", "问点什么"],
            "geometry": {"band": "bottom", "min_width_ratio": 0.3},
            "retry": {"focus_attempts": 3},
        },
        "send": {"keys": ["ENTER"], "prefer": "key"},
        "done": {"poll_seconds": 1.0, "max_wait_seconds": 300, "settle_samples": 2},
        "failure_codes": [],
    },
    "login": {"required": True, "signals": ["登录", "扫码", "验证码", "同意并继续", "Sign in"],
              "hint": "豆包首次运行需要在窗口里登录（扫码或手机号）；登录完成后窗口会显示输入框。"},
    "channel": {"primary": "mcp", "fallback": "ui-auto"},
}


# --------------------------------------------------------------------------- 通用工具

def _now_iso():
    return datetime.now().isoformat(timespec="milliseconds")


def _norm(text):
    return re.sub(r"\s+", "", text or "")


def _key(text):
    """结构比对用的键：去掉空白和所有标点/符号，只留中文、字母、数字。

    20261110ab 现场：Windows OCR 把用户气泡里的“生成PPT提示词，PPT主题”读成
    “生成PPT提示词PPT主题”（全角逗号直接丢了），按 _norm 比对永远对不上，
    于是发送已成功也被判成 send_not_confirmed，白重试一步。标点不参与比对。
    """
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", text or "")


def _sha(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _send_unconfirmed_hint(send):
    """发送未确认的如实说明：只说“未确认的事实 + 我下一步做什么”。

    20261110z 的旧文案写着“到豆包窗口点一下输入框手动发一次”，把操作推回用户，
    与聊天回复共用的话术闸冲突。这里改成不依赖用户动手的说法。
    """
    return ("没有确认到新的用户气泡（气泡指纹 %d→%d、输入框仍留原文=%s、按钮态=%s）；"
            "缺什么：一条新的用户消息或“停止生成”状态。"
            "下一步：我会重新定位输入框再发这一句；仍确认不到就停在这里报 "
            "send_not_confirmed，不再重复发送。"
            % (int(send.get("bubble_before") or 0),
               int(send.get("bubble_after") or 0),
               bool(send.get("still_in_composer")),
               send.get("button_state") or "unknown"))


def _rect_box(rect):
    """把 UIA 元素 rect 归一成 (left, top, right, bottom)；无效返回 None。

    ``uia_service.snapshot()`` 返回的 rect 是字典
    ``{"left":...,"top":...,"right":...,"bottom":...}``；历史上这里按序列索引，
    ``len(dict) == 4`` 骗过了长度检查，随后 ``rect[2]`` 抛 ``KeyError: 2``。
    这里同时兼容字典与 list/tuple，字段缺失或尺寸非正一律视为无效。
    """
    if isinstance(rect, dict):
        try:
            box = (float(rect["left"]), float(rect["top"]),
                   float(rect["right"]), float(rect["bottom"]))
        except (KeyError, TypeError, ValueError):
            return None
    elif isinstance(rect, (list, tuple)) and len(rect) == 4:
        try:
            box = tuple(float(value) for value in rect)
        except (TypeError, ValueError):
            return None
    else:
        return None
    left, top, right, bottom = box
    if right <= left or bottom <= top:
        return None
    return box


@contextmanager
def _dpi_aware():
    """截图坐标与鼠标坐标统一到物理像素，避免混合 DPI 下点偏。"""
    user = ctypes.windll.user32
    setter = getattr(user, "SetThreadDpiAwarenessContext", None)
    previous = None
    if setter:
        setter.restype = ctypes.c_void_p
        setter.argtypes = [ctypes.c_void_p]
        previous = setter(ctypes.c_void_p(-4))
    try:
        yield
    finally:
        if setter and previous:
            setter(previous)


def _read_clipboard():
    for _ in range(10):
        try:
            _clip.OpenClipboard()
            try:
                if _clip.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                    return _clip.GetClipboardData(win32con.CF_UNICODETEXT) or ""
                return ""
            finally:
                _clip.CloseClipboard()
        except Exception:
            time.sleep(0.08)
    return ""


#: 跨线程共享的快照盒。20261110w 真机现场：复制动作跑在 doubao-capture-* 工作线程里，
#: 旧实现用 threading.local()，主线程写下的 ``box`` 在 worker 里根本看不到，
#: 于是 worker 既不快照也不标 tried，主线程 finally 判定“没写过”→ 跳过恢复，
#: 还谎报 restore_ok=true/skipped=true，用户剪贴板被真实污染。
_CLIP_LOCK = threading.RLock()
_CLIP_SESSION = {"box": None}
#: 工作线程启动时显式注入同一个快照盒实例：即使主线程已经结束会话，
#: 晚返回的 worker 仍能用自己的引用完成恢复，不会把污染留在剪贴板里。
_CLIP_THREAD = threading.local()


def _clip_session_begin():
    """开一次剪贴板会话：主线程与工作线程共用同一个普通 dict（不用 threading.local）。"""
    box = dict(tried=False, wrote=False, dirty=False, owner=None,
               rows=[], formats=[], all_formats=[], unreadable=[],
               error="", restore=None)
    with _CLIP_LOCK:
        _CLIP_SESSION["box"] = box
    return box


def _clip_session_end(box):
    with _CLIP_LOCK:
        if _CLIP_SESSION.get("box") is box:
            _CLIP_SESSION["box"] = None


def _clip_session_box():
    """当前会话盒：主线程和 doubao-capture-* 工作线程拿到的是同一份 dict。

    工作线程优先用启动时注入的实例（``_CLIP_THREAD.box``），这样主线程发出
    ``_clip_session_end`` 之后晚结束的 worker 仍然能恢复自己写过的剪贴板。
    """
    injected = getattr(_CLIP_THREAD, "box", None)
    if isinstance(injected, dict):
        return injected
    with _CLIP_LOCK:
        return _CLIP_SESSION.get("box")


def _write_clipboard(text):
    # 只有真的动剪贴板时才快照用户原内容（单元测试里复制路径被 mock 时不会碰真实剪贴板）。
    box = _clip_session_box()
    if isinstance(box, dict):
        with _CLIP_LOCK:
            if not box.get("tried"):
                box["tried"] = True
                box.update(_snapshot_clipboard())
    for _ in range(8):
        try:
            _clip.OpenClipboard()
            _clip.EmptyClipboard()
            _clip.SetClipboardData(win32con.CF_UNICODETEXT, text)
            _clip.CloseClipboard()
            if isinstance(box, dict):
                with _CLIP_LOCK:
                    box["wrote"] = True
                    box["dirty"] = True
                    box["owner"] = threading.get_ident()
            return True
        except Exception:
            time.sleep(0.08)
    return False


def _clipboard_formats():
    """读当前剪贴板格式列表；读不到返回 None（而不是空列表，避免误判“已清空”）。"""
    formats = []
    try:
        _clip.OpenClipboard()
        try:
            fmt = 0
            while True:
                fmt = _clip.EnumClipboardFormats(fmt)
                if not fmt:
                    break
                formats.append(int(fmt))
        finally:
            _clip.CloseClipboard()
    except Exception:
        return None
    return formats


def _snapshot_clipboard():
    """取正文前保存用户剪贴板全量格式（含 HTML/Chromium 私有格式），用于 finally 恢复。

    ``all_formats`` 是恢复后比对的基准；``rows`` 是真正能读出来的内容。
    读不出来的格式记进 ``unreadable``：恢复时不会假装成功，返回结果会据此告警。
    """
    snapshot = {"ok": False, "rows": [], "formats": [], "all_formats": [],
                "unreadable": [], "error": ""}
    try:
        _clip.OpenClipboard()
        try:
            fmt = 0
            while True:
                fmt = _clip.EnumClipboardFormats(fmt)
                if not fmt:
                    break
                fmt = int(fmt)
                snapshot["all_formats"].append(fmt)
                try:
                    value = _clip.GetClipboardData(fmt)
                except Exception:
                    snapshot["unreadable"].append(fmt)
                    continue          # 读不出来的格式不纳入快照，恢复时也不假装成功
                if isinstance(value, (bytes, str)) and len(value) <= 10_000_000:
                    snapshot["rows"].append((fmt, value))
                else:
                    snapshot["unreadable"].append(fmt)
        finally:
            _clip.CloseClipboard()
        snapshot["formats"] = [int(fmt) for fmt, _ in snapshot["rows"]]
        snapshot["ok"] = True
    except Exception as exc:
        snapshot["error"] = "%s: %s" % (type(exc).__name__, exc)
    return snapshot


def _restore_clipboard(snapshot, attempts=4):
    """恢复用户原剪贴板；带重试与“恢复后格式集合 == 快照格式集合”的实测核对。

    20261110v 真机现场：豆包“复制”留下 7 个格式（HTML/Chromium 私有格式），
    旧实现没有 finally 恢复，污染留存。这里恢复后按格式集合核对，
    返回 ``restore_ok / attempts / leftover_formats / missing_formats / error`` 供日志告警。
    """
    rows = list((snapshot or {}).get("rows") or [])
    if not (snapshot or {}).get("ok"):
        return dict(restore_ok=False, attempts=0, leftover_formats=[],
                    missing_formats=[], skipped=False,
                    error=(snapshot or {}).get("error") or "剪贴板快照失败，未尝试恢复")
    expected = set(int(fmt) for fmt in ((snapshot or {}).get("all_formats")
                                        or (snapshot or {}).get("formats") or []))
    last_error = ""
    total = max(1, int(attempts or 4))
    for attempt in range(1, total + 1):
        try:
            _clip.OpenClipboard()
            try:
                _clip.EmptyClipboard()
                for fmt, value in rows:
                    try:
                        _clip.SetClipboardData(fmt, value)
                    except Exception as exc:
                        last_error = "格式 %s 恢复失败：%s: %s" % (
                            fmt, type(exc).__name__, exc)
            finally:
                _clip.CloseClipboard()
        except Exception as exc:
            last_error = "%s: %s" % (type(exc).__name__, exc)
            time.sleep(0.12)
            continue
        current = _clipboard_formats()
        if current is None:
            last_error = "恢复后读不到剪贴板格式"
            time.sleep(0.12)
            continue
        current_set = set(int(fmt) for fmt in current)
        # 文本格式会被 Windows 自动派生（CF_TEXT/CF_OEMTEXT/CF_LOCALE），
        # 只要快照里本来有文本，这些派生格式不算残留。
        if expected & _TEXT_DERIVED_FORMATS:
            expected_cmp = expected - _TEXT_DERIVED_FORMATS
            current_cmp = current_set - _TEXT_DERIVED_FORMATS
        else:
            expected_cmp, current_cmp = expected, current_set
        leftover = sorted(current_cmp - expected_cmp)
        missing = sorted(expected_cmp - current_cmp)
        if not leftover and not missing:
            return dict(restore_ok=True, attempts=attempt, leftover_formats=[],
                        missing_formats=[], skipped=False, error=last_error)
        last_error = last_error or ("恢复后格式与快照不一致：多 %s，缺 %s"
                                    % (leftover, missing))
        time.sleep(0.1)
    current_set = set(_clipboard_formats() or [])
    if expected & _TEXT_DERIVED_FORMATS:
        expected_cmp = expected - _TEXT_DERIVED_FORMATS
        current_cmp = current_set - _TEXT_DERIVED_FORMATS
    else:
        expected_cmp, current_cmp = expected, current_set
    return dict(restore_ok=False, attempts=total,
                leftover_formats=sorted(current_cmp - expected_cmp),
                missing_formats=sorted(expected_cmp - current_cmp),
                skipped=False, error=last_error)


def _restore_session_clipboard(box, attempts=4, only_owner=True):
    """在“真正动过剪贴板的那个线程”的 finally 里恢复，并回写实测结果。

    - 没写过：返回 ``skipped=True``，不碰剪贴板；
    - 已经恢复过且之后没有再写：直接返回上次结果；
    - ``only_owner=True`` 且当前线程不是最后一次写入的线程：返回 ``None``，
      把恢复机会留给拥有剪贴板的那个线程（不在主线程抢着动）。
    """
    if not isinstance(box, dict):
        return dict(restore_ok=False, attempts=0, leftover_formats=[],
                    missing_formats=[], skipped=True,
                    error="没有剪贴板会话盒，未尝试恢复")
    with _CLIP_LOCK:
        if not box.get("wrote"):
            return dict(restore_ok=True, attempts=0, leftover_formats=[],
                        missing_formats=[], skipped=True,
                        reason="本轮未写入剪贴板")
        if not box.get("dirty") and box.get("restore") is not None:
            return box["restore"]
        if only_owner and box.get("owner") not in (None, threading.get_ident()):
            return None
        restore = _restore_clipboard(box, attempts=attempts)
        restore["restored_in"] = threading.current_thread().name
        restore["skipped"] = False
        box["dirty"] = False
        box["restore"] = restore
    return restore



def _clip_changed(text):
    """复制是否真的改变了剪贴板：哨兵原样返回 / 太短都算没复制到。"""
    value = str(text or "")
    return bool(value) and value != COPY_SENTINEL and len(value) >= 12


#: 豆包空输入框的占位文案（TextPattern 会把它们当正文读出来）。
#: 只剔除整行都是占位提示的行，避免把用户输入的一整行误删。
#: 20261110aa 现场：空框读回的是“发消息或按住空格说话...”占位（13 字），
#: 旧清单没这版文案 → input_verified 误判“清空后仍有残留”，三次都不写入，
#: 报 input_verify_failed。这里补上该版本文案（按子串匹配覆盖“...”变体）。
COMPOSER_PLACEHOLDERS = ("发消息或创建任务", "使用技能", "@ 添加资料", "@添加资料",
                         "发送消息", "输入消息", "按住空格说话", "发消息或按住空格")


def _strip_composer_placeholder(text):
    lines = [line for line in str(text or "").splitlines()
             if not any(marker in line for marker in COMPOSER_PLACEHOLDERS)]
    return "\n".join(lines).strip()


class _ComposerDriver:
    """豆包输入框驱动：点击聚焦 → 清空 → Ctrl+V → 同源 TextPattern 回读。"""

    def __init__(self, adapter, hwnd, anchor, ref=None, service=None, element=None):
        self.adapter = adapter
        self.hwnd = hwnd
        self.anchor = anchor or {"x": 0.55, "y": 0.87}
        # 同源绑定：写入/回读都针对这一个 UIA element（ref 由 hwnd+RuntimeId 派生）。
        self.ref = ref or ""
        self.service = service
        self.element = element or {}

    def clear(self):
        self.adapter._click(self.hwnd, max(float(self.anchor.get("x", 0.55)), 0.5),
                            float(self.anchor.get("y", 0.87)))
        self.adapter._send([17, 65])
        time.sleep(0.15)
        self.adapter._send([46])
        time.sleep(0.3)

    def write(self, text):
        self.adapter._send([17, 86])
        time.sleep(0.8)

    def read(self):
        """只按注入时绑定的同一个 UIA 控件回读，绝不回退到前台窗口文本/整窗 OCR。

        20261110y 现场：旧实现 Ctrl+A/Ctrl+C 读的是“当前焦点窗口”的文本，
        键落到咕嘎自己的输入条时就把用户原句（48 字）当成豆包输入框残留，
        3 次都“有残留”从而拒绝发送。取不到同一控件就抛 ComposerNotFound。

        20261110z 实测：豆包输入框是 Group + tiptap ProseMirror，没有
        ValuePattern，只有 TextPattern；因此这里优先按同一个 ref 读 TextPattern。
        """
        if not (self.ref and self.service):
            raise ComposerNotFound("没有绑定豆包输入框控件，无法同源回读（不回退到前台窗口/整窗 OCR）")
        try:
            if self.element.get("text_pattern"):
                text = self.service.text_of(self.hwnd, self.ref).get("text") or ""
                return _strip_composer_placeholder(text)
            if "ValuePattern" in (self.element.get("patterns") or []):
                return self.service.value(self.hwnd, self.ref)
            text = self.service.text_of(self.hwnd, self.ref).get("text") or ""
            return _strip_composer_placeholder(text)
        except Exception as exc:
            raise ComposerNotFound("输入框控件已失效，无法同源回读：%s" % str(exc)[:120]) from None

    # ---- 技能/模式探测（判定逻辑在 core.paste_verify，适配器只提供探针） ----
    def _chip_lines(self):
        """输入框条带里“标签 + ×”的行：常驻快捷栏（对话/PPT 生成/图像生成…）没有 ×。"""
        _, words, _ = self.adapter._capture_words(self.hwnd)
        rows = []
        for line in _group_lines(words):
            if float(line.get("y") or 0) < 0.72:
                continue
            label = chip_label(line.get("text"))
            if label:
                rows.append(dict(label=label, line=line))
        return rows

    def mode(self):
        rows = self._chip_lines()
        if not rows:
            return dict(plain=True, label="")
        return dict(plain=False, label=rows[0]["label"])

    def clear_mode(self, label=""):
        """点掉技能标签后面的 ×；点完由 ensure_plain_mode 再回读一次确认。"""
        rows = self._chip_lines()
        hit = next((row for row in rows if not label or row["label"] == label), None)
        if not hit:
            raise RuntimeError("没有定位到技能标签「%s」" % (label or "?"))
        x0, y0, x1, y1 = _line_box(hit["line"])
        self.adapter._click(self.hwnd, min(0.985, x1 - 0.003), (y0 + y1) / 2)
        time.sleep(0.5)


def _grab_window_sync(hwnd):
    """抓窗口自身渲染内容（PrintWindow），别的窗口盖在上面也不会混进来。

    区域截图会把他人的界面一起 OCR 进来，这是“看起来调用了”的典型假象来源，
    所以优先走窗口自绘；失败/全黑时再退回区域截图。
    """
    import win32ui
    from PIL import Image

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise ValueError("窗口没有可见区域")
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    try:
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)
        rendered = ctypes.windll.user32.PrintWindow(
            hwnd, save_dc.GetSafeHdc(), 2)  # PW_RENDERFULLCONTENT
        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)
        image = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]),
                                 bits, "raw", "BGRX", 0, 1)
    finally:
        try:
            win32gui.DeleteObject(bitmap.GetHandle())
        except Exception:
            pass
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)
    if not rendered:
        raise RuntimeError("PrintWindow 失败")
    return image.convert("RGB")


def _grab_window(hwnd, timeout=3.0):
    """带超时的窗口抓屏代理：PrintWindow 卡住时抛 CaptureTimeout，而不是无限阻塞。

    20261110z 现场：豆包渲染进程卡死时 PrintWindow 不返回，整条 ask() 跟着挂住，
    界面一直显示“等待具体操作回执”。这里用工作线程 + join 硬收口。
    """
    box = {}

    def worker():
        try:
            box["value"] = _grab_window_sync(hwnd)
        except BaseException as exc:          # 线程内异常原样带回调用线程
            box["error"] = exc

    thread = threading.Thread(target=worker, name="guga-grab-window", daemon=True)
    thread.start()
    thread.join(max(0.2, float(timeout)))
    if thread.is_alive():
        raise CaptureTimeout("PrintWindow 超过 %.1f 秒未返回（窗口可能卡死），已放弃本轮抓屏"
                             % float(timeout))
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _group_lines(words):
    """OCR 把中文拆成单字，先按 y 归行再拼字符串，才能匹配多字锚点。"""
    lines = []
    for word in sorted(words or [], key=lambda w: (round(float(w["y"]), 3), float(w["x"]))):
        y = float(word["y"])
        for line in lines:
            if abs(line["y"] - y) <= max(0.008, float(word.get("h", 0.02)) * 0.7):
                line["words"].append(word)
                break
        else:
            lines.append({"y": y, "words": [word]})
    for line in lines:
        line["words"].sort(key=lambda w: float(w["x"]))
        line["text"] = "".join(str(w.get("text") or "") for w in line["words"])
    lines.sort(key=lambda line: line["y"])
    return lines


def _line_box(line):
    x0 = min(float(w["x"]) for w in line["words"])
    x1 = max(float(w["x"]) + float(w["w"]) for w in line["words"])
    y = line["y"]
    h = max(float(w.get("h", 0.02)) for w in line["words"])
    return x0, y, x1, y + h


def _load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


# --------------------------------------------------------------------------- MCP 通道

class McpChannel:
    """最小 MCP 客户端（stdio / streamable-http）。

    只做“调用一个文本生成工具”，不做工具编排。配置来源：
    ``core/adapters/doubao_mcp.json`` 或环境变量 ``GUGA_DOUBAO_MCP_URL``。
    """

    def __init__(self, config_path=MCP_CONFIG_PATH, timeout=4.0):
        self.config_path = Path(config_path)
        self.timeout = timeout
        config = _load_json(self.config_path, None) or {}
        env_endpoint = os.environ.get("GUGA_DOUBAO_MCP_URL") or ""
        endpoint = env_endpoint or config.get("endpoint") or ""
        self.config = dict(config)
        self.config["endpoint"] = endpoint
        if env_endpoint:
            # 环境变量是最强覆盖：显式给了端点就说明本次要试 MCP，
            # 不能被配置文件里默认的 enabled=false 挡住。
            self.config["enabled"] = True
            self.config["transport"] = "http"
        self.config.setdefault("enabled", bool(endpoint))
        self.config.setdefault("transport", "http" if endpoint else "stdio")
        self._proc = None
        self._session_id = ""
        self._rpc_id = 0
        self._lock = threading.Lock()

    # -- 生命周期
    def close(self):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                pass
        self._proc = None

    def _available_config(self):
        if not self.config.get("enabled"):
            return False, "not_configured"
        if self.config.get("transport") == "http":
            return bool(self.config.get("endpoint")), "no_endpoint"
        return bool(self.config.get("command")), "no_command"

    def probe(self):
        """初始化 + tools/list；返回 {available, reason, tools, server}。"""
        ok, reason = self._available_config()
        if not ok:
            return {"available": False, "reason": reason}
        try:
            init = self._rpc("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "guga", "version": "1.0"},
            }, timeout=self.timeout)
            self._notify("notifications/initialized", {})
            tools = self._rpc("tools/list", {}, timeout=self.timeout).get("result", {}).get("tools", [])
            server = init.get("result", {}).get("serverInfo", {})
            return {"available": True, "server": server, "tools": [t.get("name") for t in tools],
                    "transport": self.config.get("transport")}
        except Exception as exc:
            return {"available": False, "reason": "unreachable", "detail": str(exc)[:240]}

    def call_text(self, prompt):
        self._rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "guga", "version": "1.0"}}, timeout=self.timeout)
        self._notify("notifications/initialized", {})
        tools = self._rpc("tools/list", {}, timeout=self.timeout).get("result", {}).get("tools", [])
        names = [str(t.get("name") or "") for t in tools]
        tool = self.config.get("tool") or next(
            (n for n in names if re.search(r"chat|ask|generate|completion|text|write", n, re.I)), "")
        if not tool:
            raise RuntimeError("MCP 服务没有文本生成工具：%s" % (names or "空"))
        arg_name = self.config.get("prompt_argument") or "prompt"
        result = self._rpc("tools/call", {"name": tool, "arguments": {arg_name: prompt}},
                           timeout=float(self.config.get("call_timeout_seconds") or 60))
        payload = result.get("result") or {}
        texts = []
        for item in payload.get("content") or []:
            if isinstance(item, dict) and item.get("type") in (None, "text"):
                texts.append(str(item.get("text") or ""))
        if not texts and isinstance(payload.get("text"), str):
            texts.append(payload["text"])
        text = "\n".join(t for t in texts if t).strip()
        if not text:
            raise RuntimeError("MCP 工具返回空正文")
        return {"text": text, "tool": tool, "server": self.config.get("server_name") or "doubao"}

    # -- 传输
    def _rpc(self, method, params, timeout=None):
        self._rpc_id += 1
        message = {"jsonrpc": "2.0", "id": self._rpc_id, "method": method, "params": params or {}}
        if self.config.get("transport") == "http":
            reply = self._http(message, timeout or self.timeout)
            if "error" in reply:
                raise RuntimeError(reply["error"])
            return reply
        return self._stdio(message, timeout or self.timeout)

    def _notify(self, method, params):
        message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        try:
            if self.config.get("transport") == "http":
                self._http(message, 2.0, expect_reply=False)
            else:
                self._stdio(message, 2.0, expect_reply=False)
        except Exception:
            pass

    def _stdio(self, message, timeout, expect_reply=True):
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                env = dict(os.environ)
                env.update({str(k): str(v) for k, v in (self.config.get("env") or {}).items()})
                command = [str(self.config.get("command"))] + [str(a) for a in (self.config.get("args") or [])]
                self._proc = subprocess.Popen(
                    command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    env=env, text=True, encoding="utf-8", errors="replace",
                    cwd=str(self.config.get("cwd") or ROOT))
            line = json.dumps(message, ensure_ascii=False) + "\n"
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
            if not expect_reply:
                return {}
            deadline = time.time() + max(0.5, timeout)
            target = message.get("id")
            while time.time() < deadline:
                raw = self._proc.stdout.readline()
                if not raw:
                    raise RuntimeError("MCP stdio 服务已退出")
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    reply = json.loads(raw)
                except Exception:
                    continue
                if reply.get("id") == target:
                    return reply
            raise TimeoutError("MCP stdio 调用超时")

    def _http(self, message, timeout, expect_reply=True):
        import urllib.request

        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        headers.update({str(k): str(v) for k, v in (self.config.get("headers") or {}).items()})
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        body_bytes = json.dumps(message).encode("utf-8")
        request = urllib.request.Request(
            str(self.config.get("endpoint")), data=body_bytes,
            headers=headers, method="POST")
        # MCP HTTP 也可能被网关 http→https 重定向，requests 默认 302 后会把 POST 改成 GET，
        # 只接受 POST 的 MCP 端点立刻回 405。这里保留 POST 重发。
        from core.model_endpoint import urlopen_post
        with urlopen_post(request, body_bytes, timeout=timeout) as response:
            session = response.headers.get("Mcp-Session-Id")
            if session:
                self._session_id = session
            body = response.read().decode("utf-8", "replace")
            if not expect_reply:
                return {}
            content_type = (response.headers.get("Content-Type") or "").lower()
            if "text/event-stream" in content_type:
                for block in body.split("\n\n"):
                    data = "\n".join(line[5:] for line in block.splitlines() if line.startswith("data:"))
                    if not data.strip():
                        continue
                    try:
                        reply = json.loads(data)
                    except Exception:
                        continue
                    if reply.get("id") == message.get("id"):
                        return reply
                raise RuntimeError("MCP SSE 没有返回匹配的响应")
            reply = json.loads(body) if body.strip() else {}
            if isinstance(reply, list):
                reply = next((r for r in reply if r.get("id") == message.get("id")), {})
            return reply


# --------------------------------------------------------------------------- UI 通道

class DoubaoAdapter:
    """豆包适配器。一次 ask() 走完 MCP -> UI 的通道选择与结果契约。"""

    name = "豆包"
    tool = "doubao"

    def __init__(self, declaration_path=DECLARATION_PATH, mcp_config_path=MCP_CONFIG_PATH,
                 evidence_dir=DEFAULT_EVIDENCE_DIR, allow_launch=True, logger=None):
        self.declaration_path = Path(declaration_path)
        self.declaration = _load_json(self.declaration_path, None) or dict(DEFAULT_DECLARATION)
        self.mcp = McpChannel(mcp_config_path)
        self.evidence_root = Path(evidence_dir)
        self.allow_launch = allow_launch
        self.logger = logger
        self.cc = ComputerControl() if ComputerControl else None
        # 最近一次读屏：OCR 引擎成功执行但返回 0 行 0 字时置 True。
        # 用来把“OCR 空文本”与“适配器异常/白屏”分开报码，避免都压成 adapter_exception。
        self._last_ocr_empty = False
        # 发送状态机：sent_confirmed 之后任何路径都不许再注入/回车（只重试滚动/读取）。
        self._submit_state = ""
        # 本轮视口是否处于“小视口模式”（放大被拒或无效）。
        self._small_viewport = False

    # -- 对外入口 ---------------------------------------------------------
    def _obs(self, event, **fields):
        """把关键事件写进 adapter_chain.log（与链层同一个落点）。

        20261110z 现场：这些事件只在 result.json 的 timeline 里，链日志只有两行，
        验收时等于没有。四类必须落盘：composer / inject / done / timeout。
        """
        try:
            from core import run_log
            row = dict(tool=getattr(self, "tool", "doubao"))
            row.update({key: value for key, value in fields.items() if value is not None})
            run_log.emit(event, **row)
        except Exception:
            pass

    def probe(self):
        """只读探测：安装、进程、窗口、登录/空窗、MCP 可用性。"""
        with _dpi_aware():
            window = self._find_window()
            info = {
                "ok": True,
                "tool": self.tool,
                "installed": bool(self._resolve_exe()),
                "exe": self._resolve_exe(),
                "running": self._process_running(),
                "window": window,
                "blank": None,
                "login_required": None,
                "mcp": self.mcp.probe(),
                "declaration": {"path": str(self.declaration_path), "schema": self.declaration.get("schema"),
                                "automation": self.declaration.get("automation", "ui_only")},
                "channel_order": ["mcp", "ui_automation"],
            }
            if window:
                self._restore_window(window["hwnd"])
                self._ocr_state()
                frame, words, text = self._capture_words(window["hwnd"], save_path=None)
                info["blank"] = self._is_blank(frame, text)
                info["login_required"] = self._login_required(text, words)
                info["ocr_unavailable"] = bool(getattr(self, "_ocr_unavailable", False))
            else:
                info["ocr_unavailable"] = not bool(self._ocr_state().get("available"))
            return info

    def ask(self, prompt, timeout_seconds=None, retry=1, prefer_mcp=True):
        """让豆包真实产出正文。

        返回统一契约：
          成功 {ok:True, text, source{...}, evidence{...}, notice}
          失败 {ok:False, code, hint, channel, attempts, evidence{...}}
        """
        prompt = str(prompt or "").strip()
        call_id = "doubao-%s-%s" % (datetime.now().strftime("%Y%m%d%H%M%S"), uuid.uuid4().hex[:4])
        evidence_dir = self.evidence_root / call_id
        started = time.time()
        result = {
            "ok": False, "tool": self.tool, "name": self.name, "call_id": call_id,
            "started_at": _now_iso(), "channel": None, "text": "",
            "source": {}, "evidence": {"dir": str(evidence_dir)}, "attempts": [],
        }
        if not prompt:
            return self._fail(result, "empty_prompt", "提示词为空", evidence_dir)

        # 1) MCP 优先
        if prefer_mcp:
            mcp_state = self.mcp.probe()
            result["mcp"] = mcp_state
            if mcp_state.get("available"):
                try:
                    payload = self.mcp.call_text(prompt)
                    text = payload["text"]
                    result.update({
                        "ok": True, "text": text, "channel": "mcp",
                        "source": {
                            "tool": "doubao", "name": self.name, "channel": "mcp",
                            "label": "豆包（MCP 接口）", "call_id": call_id,
                            "started_at": result["started_at"],
                            "duration_ms": int((time.time() - started) * 1000),
                            "provenance": "mcp_tool_call:%s" % payload.get("tool"),
                        },
                        "elapsed_ms": int((time.time() - started) * 1000),
                    })
                    result["evidence"].update({
                        "mcp_tool": payload.get("tool"), "text_sha256": _sha(text),
                        "text_chars": len(_norm(text)),
                    })
                    return result
                except Exception as exc:
                    result["mcp"] = dict(mcp_state, call_error=str(exc)[:240])
            else:
                result["mcp"] = mcp_state

        # 2) UI 自动化兜底
        result["channel"] = "ui_automation"
        result["notice"] = ("未走接口：豆包桌面端没有对外 MCP Server（其“连接器”是让豆包调用外部 MCP），"
                            "本次走的是界面自动化，不是接口调用。")
        attempts = 0
        last_code = None
        last_attempt = {}
        # 每轮派单重置：submit_state 一旦变 sent_confirmed，后续只允许重试滚动/读取。
        self._submit_state = ""
        self._small_viewport = False
        while attempts <= max(0, int(retry)):
            attempts += 1
            attempt_dir = evidence_dir / ("attempt%d" % attempts)
            sent_ok = (getattr(self, "_submit_state", "") == "sent_confirmed"
                       or bool((last_attempt.get("evidence") or {}).get("send", {}).get("ok")))
            try:
                if sent_ok and last_code in ("timeout", "capture_empty",
                                             "scroll_stuck", "scroll_incomplete",
                                             "scroll_wrong_target"):
                    attempt = self._ui_capture_retry(prompt, attempt_dir, timeout_seconds)
                else:
                    attempt = self._ui_attempt(prompt, attempt_dir, timeout_seconds)
            except BlockedByWindow as exc:
                attempt = {"ok": False, "code": "blocked_by_window",
                           "hint": "豆包窗口被别的窗口压着且抬不到最前：%s；关掉遮挡窗口后重试。" % exc,
                           "evidence": {"dir": str(attempt_dir)}, "timeline": []}
            attempt["attempt"] = attempts
            result["attempts"].append(attempt)
            if attempt.get("ok"):
                result.update({
                    "ok": True, "text": attempt["text"], "channel": "ui_automation",
                    "source": {
                        "tool": "doubao", "name": self.name, "channel": "ui_automation",
                        "label": "豆包（界面自动化，非接口调用）", "call_id": call_id,
                        "started_at": result["started_at"],
                        "duration_ms": int((time.time() - started) * 1000),
                        "provenance": attempt["provenance"],
                    },
                    "evidence": dict(result["evidence"], **attempt.get("evidence", {})),
                    "elapsed_ms": int((time.time() - started) * 1000),
                })
                # 成功也要在调用根目录留一份 result.json：copied_from / copy_paths
                # 是这次到底走通了哪一级复制锚点的唯一书面证据。
                self._dump_log(evidence_dir, result)
                self._release_topmost()
                return result
            last_attempt = attempt
            last_code = attempt.get("code")
            if last_code not in RETRYABLE:
                break

        result["code"] = last_code or "failed"
        if getattr(self, "_ocr_unavailable", False) and result["code"] in OCR_DEPENDENT_CODES:
            result["code"] = "ocr_unavailable"
        if getattr(self, "_ocr_unavailable", False):
            error = getattr(self, "_ocr_error", None) or {}
            result["ocr_unavailable"] = True
            result["evidence"]["ocr_unavailable"] = True
            result["evidence"]["ocr_error"] = error
            # 只有“确实因 OCR 读不到”的码才补 OCR 缺什么/去哪补；
            # 目标窗口异常、发送未确认等真实停因不能被 OCR 说明顶掉。
            if result["code"] in OCR_DEPENDENT_CODES or result["code"] == "ocr_unavailable":
                result["missing"] = error.get("missing") or "Windows OCR 语言包"
                result["where"] = error.get("where") or "设置 → 应用 → 可选功能"
                result["next_action"] = error.get("next_action") or "装完重启咕嘎"
                result["restart_required"] = True
        last = result["attempts"][-1] if result["attempts"] else {}
        if last_code == result["code"]:
            # 真实停因没有被改写：保留该次尝试里带实测证据的文案
            # （目标窗口的页面地址 / 窗口 rect / 屏幕，或发送未确认的具体状态）。
            for field in ("hint", "missing", "where", "next_action"):
                if last.get(field):
                    result[field] = last[field]
            if last.get("restart_required"):
                result["restart_required"] = True
        if isinstance(last.get("evidence"), dict):
            result["evidence"].update(last["evidence"])
        result["hint"] = result.get("hint") or self._hint_for(result["code"], last)
        result["elapsed_ms"] = int((time.time() - started) * 1000)
        result["message"] = "没能调起豆包完成这次任务；不会用咕嘎自己的模型代写。"
        self._release_topmost()
        self._dump_log(evidence_dir, result)
        return result

    # -- UI 主流程 --------------------------------------------------------
    def _ui_attempt(self, prompt, evidence_dir, timeout_seconds):
        """对外入口：整段 UI 流程跑完后，把窗口恢复到操作前的 rect / 状态。

        用户可能故意把豆包放成小窗口；放大只是本轮操作的临时手段，
        必须在 ``finally`` 里还原，不能永久改人家的布局。
        """
        guard = {"hwnd": 0, "info": None, "step": None, "evidence": None}
        try:
            return self._ui_attempt_inner(prompt, evidence_dir, timeout_seconds, guard)
        finally:
            if guard.get("hwnd"):
                restored = self._restore_viewport(guard["hwnd"], guard["info"],
                                                  guard.get("step"))
                if isinstance(guard.get("evidence"), dict):
                    guard["evidence"]["viewport_restore"] = restored

    def _ui_attempt_inner(self, prompt, evidence_dir, timeout_seconds, guard=None):
        evidence_dir.mkdir(parents=True, exist_ok=True)
        evidence = {"dir": str(evidence_dir)}
        record = {"ok": False, "evidence": evidence, "timeline": []}

        def step(event, **fields):
            record["timeline"].append(dict(ts=_now_iso(), step=event, **fields))

        with _dpi_aware():
            window = self._find_window()
            if not window:
                opened = self._ensure_window()
                window = opened
                step("open_window", window=window)
            if not window:
                state = getattr(self, "_launch_state", "")
                if state == "not_installed":
                    record.update(code="not_installed",
                                  hint="没装豆包：请先安装豆包桌面端（www.doubao.com/download/desktop），"
                                       "装好并登录后再派。")
                    return record
                if state == "launch_failed":
                    record.update(code="launch_failed",
                                  hint="装了豆包但没能打开窗口：请手动启动一次豆包，"
                                       "确认能正常打开并登录后再派。")
                    return record
                record.update(code="window_not_found",
                              hint="没有找到豆包窗口；请确认已安装豆包桌面端。")
                return record

            hwnd = window["hwnd"]
            self._restore_window(hwnd)
            self._raise_window(hwnd)
            focused = win32gui.GetForegroundWindow() == hwnd
            step("focus", hwnd=hwnd, focused=focused, rect=win32gui.GetWindowRect(hwnd))
            if not focused:
                time.sleep(0.4)
                self._raise_window(hwnd)
                focused = win32gui.GetForegroundWindow() == hwnd
            if not focused:
                record.update(code="window_not_found", hint="豆包窗口无法切到前台，请手动点一下豆包窗口后重试。")
                return record

            # 操作前先测视口：客户区太小就先放大（记原始 rect/状态，结束后 restore）。
            viewport = self._adapt_viewport(hwnd, step)
            evidence["viewport"] = viewport
            if isinstance(guard, dict):
                guard.update(hwnd=hwnd, info=viewport, step=step, evidence=evidence)
            if viewport.get("small_viewport"):
                step("viewport_small_mode", adjusted=viewport.get("adjusted"),
                     reason=viewport.get("reason"))

            ocr_state = self._ocr_state()
            if self._ocr_unavailable:
                record["degraded"] = {"code": "ocr_unavailable",
                                      "missing": ocr_state.get("missing") or "Windows OCR",
                                      "where": ocr_state.get("where") or "",
                                      "next_action": ocr_state.get("next_action") or "",
                                      "raw": (ocr_state.get("raw") or "")[:240]}
                step("ocr_unavailable", kind=ocr_state.get("kind"),
                     missing=ocr_state.get("missing"), mode="uia+clipboard")
                evidence["ocr_unavailable"] = True
                evidence["ocr_error"] = record["degraded"]
            frame, words, page_text = self._capture_words(hwnd, evidence_dir / "01_window.png")
            step("capture_window", chars=len(_norm(page_text)), mode="uia" if self._ocr_unavailable else "ocr")
            if self._ocr_unavailable:
                abnormal = self._target_window_abnormal(hwnd, frame)
                if abnormal:
                    step("target_window_abnormal", **abnormal)
                    record.update(
                        code=TARGET_WINDOW_CODE,
                        hint=("豆包窗口当前不可用：%s。这不是 OCR 或适配器异常。"
                              "缺什么：一个正常渲染、停在对话页的豆包窗口。"
                              "下一步：关掉豆包（任务栏右键 → 关闭窗口 / 退出），重新打开后"
                              "确认能看到对话页和输入框，再重新派单。"
                              % abnormal["detail"]),
                        missing="正常渲染的豆包对话窗口",
                        where="重启豆包：关闭窗口后重新打开（必要时任务栏退出再启动）",
                        next_action="重启豆包后重新派单",
                        restart_required=True)
                    evidence["target_window"] = abnormal
                    record["evidence"] = evidence
                    return record
                if not _norm(page_text):
                    record.update(code="ocr_unavailable",
                                  hint=("Windows OCR 不可用（%s），UIA 也没读到豆包页面文本。"
                                          "缺什么：%s。去哪补：%s。下一步：先按路径补语言包并重启咕嘎；"
                                          "若已装语言包，请确认豆包窗口可见且没被最小化，再重试。"
                                          % (ocr_state.get("missing") or "OCR 引擎不可用",
                                             ocr_state.get("missing") or "Windows OCR 语言包",
                                             ocr_state.get("where") or "设置 → 应用 → 可选功能")))
                    evidence["ocr_unavailable"] = True
                    record["evidence"] = evidence
                    return record
                step("uia_text_ok", chars=len(_norm(page_text)))
            elif self._is_blank(frame, page_text):
                step("blank_detected",
                     reason=("frame_blank" if self._frame_blank(frame)
                             else ("ocr_empty" if self._last_ocr_empty else "short_text")))
                self._recover_blank()
                frame, words, page_text = self._capture_words(hwnd, evidence_dir / "02_after_recover.png")
                if self._is_blank(frame, page_text):
                    if not self._frame_blank(frame) and self._last_ocr_empty:
                        # OCR 引擎可用但识别结果 0 行 0 字：这是“OCR 空文本”，
                        # 不是适配器异常，必须与 adapter_exception 分开报。
                        record.update(code="ocr_empty", hint=(
                            "OCR 引擎可用，但对豆包窗口的识别结果是 0 行 / 0 字，"
                            "不是适配器异常。缺什么：可识别的界面文字。下一步：确认豆包窗口"
                            "停在对话页且没有最小化，按 Ctrl+R 刷新后重派；仍为空请到"
                            "「设置 → 自检」重跑“系统 OCR”，并右键「导出诊断」把 zip 发给开发者。"))
                        evidence["ocr_empty"] = True
                    else:
                        record.update(code="window_blank",
                                      hint="豆包窗口白屏；已自动重启一次仍白屏，请在豆包窗口按 Ctrl+R 或重启豆包。")
                    record["evidence"] = evidence
                    return record
                step("blank_recovered")

            modal = self._dismiss_modal(hwnd, words)
            if modal:
                step("dismiss_modal", clicked=modal)
                frame, words, page_text = self._capture_words(hwnd, evidence_dir / "03_after_modal.png")

            if self._login_required(page_text, words):
                record.update(code="login_required", hint=self._login_hint())
                evidence["login_signals"] = self._login_signals(page_text)
                return record

            if not self._ocr_unavailable:
                self._start_clean_chat(hwnd, words, evidence_dir, step)
            if self._ocr_unavailable:
                anchor = self._uia_input_anchor(hwnd)
                if not anchor:
                    anchor = {"x": 0.55, "y": 0.87, "text": "geometry_bottom"}
                step("input_anchor", anchor=anchor.get("text"), at=[round(anchor["x"], 3), round(anchor["y"], 3)],
                     mode="uia")
            else:
                anchor = self._find_input_anchor(hwnd, words, evidence_dir)
            if not anchor:
                record.update(code="input_not_found",
                              hint=("没找到豆包输入框；缺什么：一个可见的对话输入框。"
                                    "下一步：我会重新枚举控件再定位一次，仍找不到就报 input_not_found。"))
                return record

            # 硬规则：发送一旦确认，任何重试路径都不许再注入/回车，只允许重试滚动/读取。
            reused = getattr(self, "_submit_state", "") == "sent_confirmed"
            if reused:
                step("submit_state", state="sent_confirmed", action="skip_inject")
                inject = {"ok": True, "reused": True, "attempts": 0, "mode": "reused"}
            else:
                inject = self._inject(hwnd, anchor, prompt, evidence_dir, step)
            evidence["inject"] = inject
            if not inject.get("ok"):
                record.update(code=inject.get("code", "readback_mismatch"),
                              hint=inject.get("hint") or (
                                  "写入后同源回读不一致；未确认。下一步：重新定位同一个控件再写一次，"
                                  "仍不一致就报 readback_mismatch。"))
                for field in ("missing", "where", "next_action"):
                    if inject.get(field):
                        record[field] = inject[field]
                return record

            if reused:
                send = {"ok": True, "reused": True, "submit_state": "sent_confirmed",
                        "signal": "reused", "confirm_signal": "reused",
                        "turns_before": None, "turns_after": None,
                        "baseline_chars": 0, "chars": 0}
            else:
                send = self._send_and_confirm(
                    hwnd, prompt, evidence_dir, step,
                    ref=inject.get("ref"), composer_verified=bool(inject.get("matched")))
            evidence["send"] = send
            if not send.get("ok"):
                if send.get("code") == "wrong_skill":
                    record.update(code="wrong_skill", hint=send.get("hint") or send.get("missing") or "",
                                  missing=send.get("missing") or "",
                                  where=send.get("where") or "send",
                                  next_action=send.get("next_action") or "移除技能标签后重派")
                    return record
                record.update(code="send_not_confirmed",
                              hint=_send_unconfirmed_hint(send))
                return record

            limit = float(timeout_seconds or self._done_setting("max_wait_seconds", 120))
            done = self._wait_done(hwnd, prompt, limit, evidence_dir, step,
                                   baseline=send.get("chars", 0),
                                   baseline_before=send.get("baseline_chars", 0))
            evidence["generate"] = done
            if not done.get("ok"):
                record.update(code=done.get("code", "timeout"),
                              hint=done.get("hint") or "等豆包回复超时。")
                return record

            capture = self._capture_answer(hwnd, prompt, evidence_dir, step)
            evidence["capture"] = capture
            if not capture.get("ok"):
                record.update(code=capture.get("code", "capture_empty"),
                              hint=capture.get("hint") or "没抓到豆包正文。")
                for field in ("missing", "where", "next_action"):
                    if capture.get(field):
                        record[field] = capture[field]
                return record

            answer = capture["answer"]
            degraded = bool(getattr(self, "_ocr_unavailable", False))
            record.update(
                ok=True, text=answer,
                done_signal=done.get("done_signal"),
                confirm_signal=send.get("confirm_signal"),
                confirm_elapsed_ms=send.get("confirm_elapsed_ms"),
                done_elapsed_ms=done.get("done_elapsed_ms"),
                provenance=("uia_text:%s" if degraded else "clipboard:%s") % capture["clip_sha256"],
                evidence={
                    "dir": str(evidence_dir),
                    "window": window,
                    "page_sha256": done["page_sha256"],
                    "capture_sha256": capture["clip_sha256"],
                    "capture_chars": len(_norm(answer)),
                    "prompt_echo_found": capture["prompt_echo_found"],
                    "copied_from": capture.get("copied_from"),
                    "copy_paths": capture.get("copy_paths"),
                    "visible_in_page": capture.get("visible_in_page"),
                    "ocr_unavailable": degraded,
                    "capture_mode": capture.get("mode") or "ocr+clipboard",
                    "degraded": record.get("degraded"),
                    "local_model_used": False,
                    "origin": "doubao_desktop_window",
                    "screenshots": [str(p.name) for p in sorted(evidence_dir.glob("*.png"))],
                },
            )
            self._dump_log(evidence_dir, dict(record, prompt=prompt, timeline=record["timeline"]))
            return record

    # -- 只补抓（不重发）---------------------------------------------------
    def _ui_capture_retry(self, prompt, evidence_dir, timeout_seconds):
        """对外入口：只重试读取；结束后同样把窗口恢复到操作前 rect/状态。"""
        guard = {"hwnd": 0, "info": None, "step": None, "evidence": None}
        try:
            return self._ui_capture_retry_inner(prompt, evidence_dir, timeout_seconds, guard)
        finally:
            if guard.get("hwnd"):
                self._restore_viewport(guard["hwnd"], guard["info"], guard.get("step"))

    def _ui_capture_retry_inner(self, prompt, evidence_dir, timeout_seconds, guard=None):
        """上一次已经确认发出去了，但抓取失败：只重新等/复制，不再发一遍提问。"""
        evidence_dir.mkdir(parents=True, exist_ok=True)
        record = {"ok": False, "mode": "capture_only", "evidence": {"dir": str(evidence_dir)},
                  "timeline": []}

        def step(event, **fields):
            record["timeline"].append(dict(ts=_now_iso(), step=event, **fields))

        with _dpi_aware():
            window = self._find_window()
            if not window:
                record.update(code="window_lost", hint="豆包窗口不见了；重新打开后再试。")
                return record
            hwnd = window["hwnd"]
            self._restore_window(hwnd)
            self._raise_window(hwnd)
            if win32gui.GetForegroundWindow() != hwnd:
                record.update(code="window_not_found", hint="豆包窗口无法切到前台。")
                return record
            step("submit_state", state=getattr(self, "_submit_state", "") or "unconfirmed",
                 action="retry_read_only")
            viewport = self._adapt_viewport(hwnd, step)
            record["evidence"]["viewport"] = viewport
            if isinstance(guard, dict):
                guard.update(hwnd=hwnd, info=viewport, step=step,
                             evidence=record["evidence"])
            self._ocr_state()
            frame, words, text = self._capture_words(hwnd, evidence_dir / "01_retry_window.png")
            if self._ocr_unavailable and not _norm(text):
                error = getattr(self, "_ocr_error", None) or {}
                record.update(code="ocr_unavailable",
                              hint="OCR 不可用且 UIA 读不到页面文本；缺什么：%s。去哪补：%s。"
                              % (error.get("missing") or "Windows OCR 语言包",
                                 error.get("where") or "设置 → 应用 → 可选功能"))
                return record
            if not self._ocr_unavailable and self._is_blank(frame, text):
                record.update(code="window_blank", hint="豆包窗口白屏，请重启豆包。")
                return record
            if self._dismiss_modal(hwnd, words):
                frame, words, text = self._capture_words(hwnd, evidence_dir / "02_retry_after_modal.png")
            limit = float(timeout_seconds or self._done_setting("max_wait_seconds", 120))
            done = self._wait_done(hwnd, prompt, limit, evidence_dir, step)
            record["evidence"]["generate"] = done
            if not done.get("ok"):
                record.update(code=done.get("code", "timeout"),
                              hint=done.get("hint") or "等豆包回复超时。")
                return record
            capture = self._capture_answer(hwnd, prompt, evidence_dir, step)
            record["evidence"]["capture"] = capture
            record["evidence"]["window"] = window
            record["evidence"]["page_sha256"] = done.get("page_sha256")
            if not capture.get("ok"):
                record.update(code=capture.get("code", "capture_empty"),
                              hint=capture.get("hint") or "没抓到豆包正文。")
                for field in ("missing", "where", "next_action"):
                    if capture.get(field):
                        record[field] = capture[field]
                return record
            record.update(
                ok=True, text=capture["answer"],
                provenance=("uia_text:%s" if getattr(self, "_ocr_unavailable", False) else "clipboard:%s")
                % capture["clip_sha256"],
                evidence=dict(record["evidence"], **{
                    "capture_sha256": capture["clip_sha256"],
                    "capture_chars": len(_norm(capture["answer"])),
                    "prompt_echo_found": capture["prompt_echo_found"],
                    "copied_from": capture.get("copied_from"),
                    "copy_paths": capture.get("copy_paths"),
                    "visible_in_page": capture.get("visible_in_page"),
                    "ocr_unavailable": bool(getattr(self, "_ocr_unavailable", False)),
                    "capture_mode": capture.get("mode") or "ocr+clipboard",
                    "local_model_used": False,
                    "origin": "doubao_desktop_window",
                    "screenshots": [p.name for p in sorted(evidence_dir.glob("*.png"))],
                }),
            )
            self._dump_log(evidence_dir, dict(record, prompt=prompt, timeline=record["timeline"]))
            return record

    # -- 窗口 -------------------------------------------------------------
    def _decl(self, *keys, default=None):
        node = getattr(self, "declaration", None) or {}
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def _process_names(self):
        names = [n.lower() for n in (self.declaration.get("process_names") or ["Doubao.exe"])]
        return names

    def _process_running(self):
        names = set(self._process_names())
        for proc in psutil.process_iter(["name"]):
            try:
                if (proc.info.get("name") or "").lower() in names:
                    return True
            except Exception:
                continue
        return False

    def _resolve_exe(self):
        # 启动优先级与 app_registry 一致：用户指定 > MCP 自报 > 声明里的环境变量/常见目录。
        # 自报路径失效时 launch_exe 返回 ok=False，这里继续走下面的兜底，不会被一条脏记录卡死。
        try:
            from core.adapters import app_registry
            found = app_registry.launch_exe('doubao')
            if found.get('ok'):
                return str(found['exe'])
        except Exception:
            pass
        for env_name in self._decl("install", "env", default=[]) or []:
            candidate = os.environ.get(env_name)
            if candidate and Path(candidate).exists():
                return candidate
        for raw in self._decl("install", "common_paths", default=[]) or []:
            candidate = Path(os.path.expandvars(raw))
            if candidate.exists():
                return str(candidate)
        single = self._decl("install", "single_instance")
        if single and Path(single).exists():
            return str(single)
        return ""

    def _find_window(self):
        min_w = int(self._decl("ready", "min_width", default=320))
        min_h = int(self._decl("ready", "min_height", default=240))
        keywords = [str(k).lower() for k in (self._decl("ready", "window", "title_keywords", default=["豆包"])
                                            or [])]
        names = set(self._process_names())
        rows = []

        def visit(hwnd, _):
            try:
                if not win32gui.IsWindow(hwnd):
                    return True
                cls = win32gui.GetClassName(hwnd)
                if "Chrome_WidgetWin" not in cls:
                    return True
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                try:
                    pname = (psutil.Process(pid).name() or "").lower()
                except Exception:
                    return True
                if pname not in names:
                    return True
                rect = win32gui.GetWindowRect(hwnd)
                width, height = rect[2] - rect[0], rect[3] - rect[1]
                if width < min_w or height < min_h:
                    return True
                title = win32gui.GetWindowText(hwnd)
                score = width * height + (1_000_000 if any(k in title.lower() for k in keywords) else 0)
                rows.append({"hwnd": hwnd, "pid": pid, "title": title, "class": cls,
                             "rect": list(rect), "visible": bool(win32gui.IsWindowVisible(hwnd)),
                             "iconic": bool(win32gui.IsIconic(hwnd)), "score": score})
            except Exception:
                pass
            return True

        win32gui.EnumWindows(visit, None)
        rows.sort(key=lambda row: (-row["score"], not row["visible"]))
        for row in rows:
            row.pop("score", None)
        return rows[0] if rows else None

    def _restore_window(self, hwnd):
        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.8)
            if not win32gui.IsWindowVisible(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
            win32gui.BringWindowToTop(hwnd)
        except Exception:
            pass

    # -- 视口自适应（咕嘎此前没有“窗口视口大小”概念）------------------------
    @staticmethod
    def _client_size(hwnd):
        """客户区尺寸（不含标题栏/边框）：豆包正文可见高度只取决于它。"""
        try:
            left, top, right, bottom = win32gui.GetClientRect(hwnd)
            return max(0, int(right - left)), max(0, int(bottom - top))
        except Exception:
            rect = win32gui.GetWindowRect(hwnd)
            return max(0, int(rect[2] - rect[0])), max(0, int(rect[3] - rect[1]))

    @staticmethod
    def _work_area():
        """屏幕工作区（含 DPI 缩放后的物理像素）：任务栏/停靠栏之外可用区域。"""
        try:
            from ctypes import wintypes
            rect = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                width = int(rect.right - rect.left)
                height = int(rect.bottom - rect.top)
                if width > 0 and height > 0:
                    return width, height
        except Exception:
            pass
        user = ctypes.windll.user32
        return (int(user.GetSystemMetrics(0)) or 1920, int(user.GetSystemMetrics(1)) or 1080)

    @staticmethod
    def _is_maximized(hwnd):
        try:
            return (int(win32gui.GetWindowPlacement(hwnd)[1])
                    == win32con.SW_SHOWMAXIMIZED)
        except Exception:
            return False

    @classmethod
    def _window_state(cls, hwnd):
        try:
            if win32gui.IsIconic(hwnd):
                return "minimized"
            if cls._is_maximized(hwnd):
                return "maximized"
        except Exception:
            pass
        return "normal"

    @staticmethod
    def _apply_window_rect(hwnd, rect):
        left, top, right, bottom = [int(value) for value in rect]
        win32gui.SetWindowPos(
            hwnd, win32con.HWND_TOP, left, top, max(1, right - left), max(1, bottom - top),
            win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)

    def _enlarge_window(self, hwnd, work_area):
        """先最大化；不行再按工作区比例设尺寸。返回 ``(ok, reason)``。"""
        errors = []
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
            time.sleep(0.45)
            client = self._client_size(hwnd)
            if (client[0] >= int(work_area[0] * VIEWPORT_MIN_WIDTH_RATIO)
                    and client[1] >= int(work_area[1] * VIEWPORT_MIN_HEIGHT_RATIO)):
                return True, "maximize"
            errors.append("maximize:客户区仍小于阈值")
        except Exception as exc:
            errors.append("maximize:%s" % type(exc).__name__)
        try:
            left, top, _right, _bottom = win32gui.GetWindowRect(hwnd)
            width = min(int(work_area[0] * VIEWPORT_TARGET_RATIO), int(work_area[0]))
            height = min(int(work_area[1] * VIEWPORT_TARGET_RATIO), int(work_area[1]))
            self._apply_window_rect(hwnd, (left, top, left + width, top + height))
            time.sleep(0.45)
            return True, "resize"
        except Exception as exc:
            errors.append("resize:%s" % type(exc).__name__)
        return False, "enlarge_failed:" + ";".join(errors)

    def _adapt_viewport(self, hwnd, step=None):
        """操作前测量客户区 / 工作区 / 比例；太小就先放大，并记录原始 rect 与窗口状态。

        ``adjusted=False`` 表示放大被拒或无效：后续一律按“小视口模式”判定，
        不再把“视口里看不到”当成“内容没生成完”（20261110x 真机根因）。
        """
        before_rect = list(win32gui.GetWindowRect(hwnd))
        before_state = self._window_state(hwnd)
        work_area = self._work_area()
        client = self._client_size(hwnd)
        ratio = {"w": round(client[0] / float(work_area[0] or 1), 3),
                 "h": round(client[1] / float(work_area[1] or 1), 3)}
        too_small = (ratio["w"] < VIEWPORT_MIN_WIDTH_RATIO
                     or ratio["h"] < VIEWPORT_MIN_HEIGHT_RATIO)
        info = dict(before_rect=before_rect, before_state=before_state,
                    client_size=list(client), work_area=list(work_area), ratio=ratio,
                    after_rect=list(before_rect), after_client_size=list(client),
                    after_ratio=dict(ratio), adjusted=False, small_viewport=False,
                    reason="ok")
        if too_small:
            ok, reason = self._enlarge_window(hwnd, work_area)
            after_client = self._client_size(hwnd)
            after_ratio = {"w": round(after_client[0] / float(work_area[0] or 1), 3),
                           "h": round(after_client[1] / float(work_area[1] or 1), 3)}
            still_small = (after_ratio["w"] < VIEWPORT_MIN_WIDTH_RATIO
                           or after_ratio["h"] < VIEWPORT_MIN_HEIGHT_RATIO)
            info.update(after_rect=list(win32gui.GetWindowRect(hwnd)),
                        after_client_size=list(after_client), after_ratio=after_ratio,
                        adjusted=bool(ok), reason=reason)
            info["small_viewport"] = bool(still_small)
            if ok and still_small:
                info["reason"] = "resize_not_effective"
        self._small_viewport = bool(info["small_viewport"])
        if step:
            step("viewport", **info)
        return info

    def _restore_viewport(self, hwnd, info, step=None):
        """把窗口恢复成操作前的 rect / 窗口状态（用户可能故意放成小窗口）。"""
        if not isinstance(info, dict) or not info.get("adjusted"):
            return dict(restored=False, restore_ok=True, reason="未调整视口，无需恢复")
        before = list(info.get("before_rect") or [])
        before_state = info.get("before_state") or "normal"
        try:
            if before_state == "maximized":
                win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
                time.sleep(0.3)
            elif before_state == "minimized":
                win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
            else:
                if win32gui.IsIconic(hwnd) or self._is_maximized(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                self._apply_window_rect(hwnd, before)
                time.sleep(0.25)
            after = list(win32gui.GetWindowRect(hwnd))
            ok = (before_state != "normal"
                  or all(abs(a - b) <= 8 for a, b in zip(after, before)))
            out = dict(restored=True, restore_ok=bool(ok), before_rect=before,
                       after_rect=after, before_state=before_state)
        except Exception as exc:
            out = dict(restored=False, restore_ok=False, before_rect=before,
                       before_state=before_state,
                       error="%s: %s" % (type(exc).__name__, exc))
        if step:
            step("viewport_restore", **out)
        return out

    def _launch(self, extra_args=None):
        exe = self._resolve_exe()
        args = list(self._decl("launch", "args", default=[]) or [])
        args += [a for a in (extra_args or []) if a not in args]
        if exe:
            try:
                command = [exe] + [str(a) for a in args]
                subprocess.Popen(command, cwd=os.path.dirname(exe) or None,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                return True
            except Exception:
                pass
        return False

    def _show_via_protocol(self):
        try:
            os.startfile(PROTOCOL_URL)  # noqa: S606 - 走系统注册的 doubaoapp:// 协议
            return True
        except Exception:
            try:
                subprocess.Popen(["cmd", "/c", "start", "", PROTOCOL_URL],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                return True
            except Exception:
                return False

    def _wait_window(self, seconds):
        deadline = time.time() + max(1.0, seconds)
        while time.time() < deadline:
            window = self._find_window()
            if window:
                return window
            time.sleep(0.5)
        return None

    @staticmethod
    def _protocol_registered():
        """系统是否注册了 doubaoapp:// 协议；没装豆包时决定要不要尝试唤起。"""
        try:
            import winreg
        except Exception:
            return False
        for root, path in ((winreg.HKEY_CURRENT_USER, r'Software\Classes\doubaoapp'),
                           (winreg.HKEY_CLASSES_ROOT, 'doubaoapp')):
            try:
                with winreg.OpenKey(root, path):
                    return True
            except OSError:
                continue
        return False

    def _ensure_window(self):
        """返回窗口；失败原因写进 ``self._launch_state``，供上层给不同提示。

        - ``existing``：已有窗口，复用（不会重新拉起）
        - ``launch_disabled``：调用方明确禁止拉起
        - ``not_installed``：找不到 exe 且没有 doubaoapp 协议，立即失败，不空等
        - ``launch_failed``：装了但 exe/协议都没能拉出窗口
        """
        self._launch_state = 'existing'
        window = self._find_window()
        if window:
            return window
        if not self.allow_launch:
            self._launch_state = 'launch_disabled'
            return None
        installed = bool(self._resolve_exe())
        if not installed and not self._protocol_registered():
            self._launch_state = 'not_installed'
            return None
        if not self._process_running() and installed:
            args = [] if os.environ.get("GUGA_DOUBAO_GPU") == "on" else STABILITY_ARGS
            self._launch(args)
        self._show_via_protocol()
        timeout = float(self._decl("launch", "timeout_seconds", default=25))
        window = self._wait_window(timeout)
        if not window:
            self._show_via_protocol()
            window = self._wait_window(8)
        self._launch_state = 'existing' if window else 'launch_failed'
        return window

    def _recover_blank(self):
        """白屏恢复：用协议再唤起一次（实测可让渲染进程重新出图）。"""
        self._show_via_protocol()
        time.sleep(2.5)

    # -- OCR / 截图 -------------------------------------------------------
    def _ocr_state(self):
        """真实探测 OCR；不可用就记下来，后续全部降级走 UIA + 剪贴板。"""
        if ocr_engine is None:
            self._ocr_unavailable = True
            self._ocr_error = None
            return {"available": False, "missing": "Windows OCR 模块缺失",
                    "where": "重新解压完整压缩包", "raw": "core.ocr_engine 导入失败"}
        state = ocr_engine.probe()
        self._ocr_unavailable = not bool(state.get("available"))
        self._ocr_error = None if state.get("available") else state
        return state

    def _uia_text(self, hwnd):
        """UIA TextPattern 读页面文本；读不到返回空串，不伪造。"""
        try:
            from core.uia_service import shared_service
            payload = shared_service().text(hwnd, limit=8000)
            return str(payload.get("text") or "")
        except Exception:
            return ""

    def _uia_input_anchor(self, hwnd):
        """用 UIA 找输入框并换算成窗口相对坐标；找不到返回 None。"""
        try:
            from core.uia_service import shared_service
            snapshot = shared_service().snapshot(hwnd, limit=80, time_budget=2.5)
        except Exception:
            return None
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width = max(1, right - left)
        height = max(1, bottom - top)
        for element in snapshot.get("elements") or []:
            if str(element.get("type")) not in ("Edit", "TextEdit", "Document"):
                continue
            box = _rect_box(element.get("rect"))
            if box is None:
                continue
            rect_left, rect_top, rect_right, rect_bottom = box
            if rect_right <= 0 or rect_bottom <= 0:
                continue
            if rect_bottom > height * 0.9:
                continue
            cx = (rect_left + rect_right) / 2.0
            cy = (rect_top + rect_bottom) / 2.0
            return {"x": (cx - left) / width, "y": (cy - top) / height,
                    "ref": element.get("ref"), "text": "uia_edit",
                    "value": element.get("value") or ""}
        return None

    def _uia_page_url(self, hwnd):
        """读 UIA Document 的地址；读不到返回空串（只用于诊断，不参与成功判定）。"""
        try:
            from core.uia_service import shared_service
            snapshot = shared_service().snapshot(hwnd, limit=40, time_budget=2.0)
        except Exception:
            return ""
        for element in snapshot.get("elements") or []:
            if str(element.get("type")) != "Document":
                continue
            value = str(element.get("value") or "").strip()
            if value.lower().startswith(("http://", "https://", "chrome://", "file://")):
                return value
        return ""

    def _target_window_abnormal(self, hwnd, frame):
        """UIA 降级时判断目标窗口是否不可用；返回证据 dict，正常返回 None。

        三个信号：截图接近纯色（白屏/渲染异常）、窗口大部分在屏幕外、
        页面明确停在非对话页。文案会带上实测到的窗口 rect / 屏幕 / 页面地址。
        """
        problems = []
        blank = bool(frame is not None and self._frame_blank(frame))
        if blank:
            problems.append("窗口截图接近纯色（白屏或渲染异常）")
        ratio = 1.0
        rect = None
        screen = None
        try:
            import win32api
            rect = tuple(int(value) for value in win32gui.GetWindowRect(hwnd))
            left, top, right, bottom = rect
            vx = int(win32api.GetSystemMetrics(76))
            vy = int(win32api.GetSystemMetrics(77))
            vw = int(win32api.GetSystemMetrics(78))
            vh = int(win32api.GetSystemMetrics(79))
            screen = (vx, vy, vx + vw, vy + vh)
            area = max(1, (right - left) * (bottom - top))
            ix = max(0, min(right, vx + vw) - max(left, vx))
            iy = max(0, min(bottom, vy + vh) - max(top, vy))
            ratio = (ix * iy) / float(area)
            if ratio < 0.6:
                problems.append("窗口有 %d%% 在屏幕外（窗口 rect=%s，屏幕=%s）"
                                % (int(round((1 - ratio) * 100)), rect, screen))
        except Exception:
            pass
        url = self._uia_page_url(hwnd)
        low = url.lower()
        if low and any(hint in low for hint in _NON_CHAT_URL_HINTS):
            problems.append("页面停在 %s，不是对话页" % url)
        if not problems:
            return None
        return dict(detail="；".join(problems), url=url, rect=rect, screen=screen,
                    blank=blank, onscreen_ratio=round(ratio, 3))

    def _grab_region(self, hwnd, timeout=3.0):
        """区域截图也走超时代理：目标窗口卡死时不要无限阻塞本轮抓屏。"""
        box = {}

        def worker():
            try:
                box["value"] = self.cc._grab(hwnd)
            except BaseException as exc:
                box["error"] = exc

        thread = threading.Thread(target=worker, name="guga-grab-region", daemon=True)
        thread.start()
        thread.join(max(0.2, float(timeout)))
        if thread.is_alive():
            raise CaptureTimeout("区域截图超过 %.1f 秒未返回（窗口可能卡死）" % float(timeout))
        if "error" in box:
            raise box["error"]
        return box.get("value") or (None, None)

    def _capture_words_uia(self, hwnd, save_path=None, error=None):
        """OCR 不可用时的替代读屏：UIA 文本 + 截图留证（words 为空）。"""
        if error is not None:
            self._ocr_error = error if isinstance(error, dict) else None
            self._ocr_unavailable = True
        self._last_ocr_empty = False
        text = self._uia_text(hwnd)
        frame = None
        try:
            if win32gui.GetForegroundWindow() != hwnd:
                self._raise_window(hwnd)
            frame, _ = self._grab_region(hwnd)
        except Exception:
            frame = None
        if save_path and frame is not None:
            try:
                frame.save(str(save_path))
            except Exception:
                pass
        return frame, [], text

    def _capture_words(self, hwnd, save_path=None):
        """先试窗口自绘（不受遮挡影响），不行就抬到最前再区域截图。

        OCR 不可用时不再抛 RuntimeError：改走 UIA TextPattern 读文本，
        并记录 ``_ocr_unavailable``，让回执标注 ocr_unavailable 后继续。
        """
        if not self.cc:
            raise RuntimeError("缺少 ComputerControl，无法截图")
        if getattr(self, "_ocr_unavailable", False):
            return self._capture_words_uia(hwnd, save_path)
        frame = words = text = None
        try:
            if getattr(self, "_printwindow_ok", True):
                try:
                    candidate = _grab_window(hwnd)
                    if not self._frame_blank(candidate):
                        result = self.cc._ocr_words(candidate) or {}
                        if len(_norm(result.get("text") or "")) >= 6:
                            frame = candidate
                            words = result.get("words") or []
                            text = result.get("text") or ""
                    if frame is None:
                        self._printwindow_ok = False
                except Exception:
                    self._printwindow_ok = False
            if frame is None:
                if win32gui.GetForegroundWindow() != hwnd:
                    self._raise_window(hwnd)
                frame, _ = self._grab_region(hwnd)
                result = self.cc._ocr_words(frame) or {}
                words, text = result.get("words") or [], result.get("text") or ""
        except Exception as exc:
            if ocr_engine is not None and isinstance(exc, ocr_engine.OcrUnavailableError):
                info = exc.to_dict()
                if str(info.get("kind") or "") == "empty_text":
                    # 引擎跑通了但识别结果 0 行 0 字：这是“OCR 空文本”，
                    # 不是引擎不可用，交给上层报 ocr_empty（不与 adapter_exception 混码）。
                    self._last_ocr_empty = True
                    self._ocr_unavailable = False
                    self._ocr_error = None
                    frame = None
                    try:
                        if win32gui.GetForegroundWindow() != hwnd:
                            self._raise_window(hwnd)
                        frame, _ = self._grab_region(hwnd)
                        if save_path and frame is not None:
                            frame.save(str(save_path))
                    except Exception:
                        frame = None
                    return frame, [], ""
                self._ocr_error = info
                return self._capture_words_uia(hwnd, save_path, error=info)
            raise
        if save_path:
            try:
                frame.save(str(save_path))
            except Exception:
                pass
        self._last_ocr_empty = not _norm(text or "")
        return frame, words, text

    def _capture_words_timed(self, hwnd, save_path=None, timeout=None):
        """单轮抓屏/识别的独立 deadline：超时抛 CaptureTimeout，不越过总 deadline。

        20261110z 现场：``_wait_done`` 只在轮与轮之间看 deadline，单轮
        ``_capture_words`` 卡在 PrintWindow/OCR 上时整轮不再返回。
        """
        if timeout is None:
            timeout = self._decl("anchors", "done", "capture_timeout_seconds", default=12.0)
        budget = max(0.5, float(timeout or 12.0))
        box = {}

        def worker():
            try:
                box["value"] = self._capture_words(hwnd, save_path)
            except BaseException as exc:
                box["error"] = exc

        thread = threading.Thread(target=worker, name="guga-capture-words", daemon=True)
        thread.start()
        thread.join(budget)
        if thread.is_alive():
            raise CaptureTimeout("单轮截图/识别超过 %.1f 秒未返回（窗口可能卡死/白屏）"
                                 % budget)
        if "error" in box:
            raise box["error"]
        return box.get("value") or (None, [], "")

    @staticmethod
    def _frame_blank(frame):
        try:
            colors = frame.convert("RGB").getcolors(maxcolors=64)
            return colors is not None and len(colors) <= 4
        except Exception:
            return False

    def _is_blank(self, frame, text):
        if self._frame_blank(frame):
            return True
        return len(_norm(text)) < 6

    def _raise_window(self, hwnd):
        """把豆包抬到最前并置顶，避免点击落到压在它上面的窗口里。"""
        self._topmost_hwnd = hwnd
        try:
            win32gui.SetWindowPos(
                hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
        except Exception:
            pass
        try:
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            if _ensure_foreground:
                _ensure_foreground(hwnd, 1.0)
        time.sleep(0.2)

    def _release_topmost(self):
        hwnd = getattr(self, "_topmost_hwnd", 0)
        if not hwnd:
            return
        try:
            win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
        except Exception:
            pass
        self._topmost_hwnd = 0

    @staticmethod
    def _point_is_target(hwnd, x, y):
        from ctypes import wintypes
        point = wintypes.POINT(int(x), int(y))
        target = int(ctypes.windll.user32.WindowFromPoint(point) or 0)
        if not target:
            return False
        try:
            return (int(win32gui.GetAncestor(target, 2))
                    == int(win32gui.GetAncestor(hwnd, 2)))
        except Exception:
            return False

    def _click(self, hwnd, rx, ry):
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        x = int(left + (right - left) * float(rx))
        y = int(top + (bottom - top) * float(ry))
        for _ in range(3):
            if self._point_is_target(hwnd, x, y):
                break
            self._raise_window(hwnd)
            time.sleep(0.25)
        else:
            # 该坐标被别的窗口占着：不点，交给上层报 blocked_by_window。
            raise BlockedByWindow("(%d,%d) 被其它窗口遮挡" % (x, y))
        user = ctypes.windll.user32
        user.SetCursorPos(x, y)
        time.sleep(0.12)
        user.mouse_event(2, 0, 0, 0, 0)
        user.mouse_event(4, 0, 0, 0, 0)
        time.sleep(0.32)
        return x, y

    def _click_line(self, hwnd, line, dx=0.0, dy=0.012):
        x0, y0, x1, y1 = _line_box(line)
        return self._click(hwnd, (x0 + x1) / 2 + dx, y0 + (y1 - y0) / 2 + dy)

    def _send(self, keys):
        if not _send_keys:
            raise RuntimeError("缺少键盘原语")
        _send_keys(keys)

    def _press_send(self, hwnd):
        """回车发送前先把焦点抬回豆包。

        实测 11:31 那单：文字已经粘进输入框，回车却没发出去——那一刻前台被
        别的窗口抢走，SendInput 的回车落到了别人窗口上。这里每次发送前
        有界地把豆包抬回前台；抬不回来也不硬发，交给上层按没确认处理。
        """
        if _ensure_foreground:
            try:
                _ensure_foreground(hwnd, 1.0)
            except Exception:
                pass
        self._send([13])

    # -- 页面状态 ---------------------------------------------------------
    def _login_signals(self, text):
        signals = self._decl("login", "signals", default=[]) or []
        return [s for s in signals if s and s in (text or "")]

    def _login_hint(self):
        return self._decl("login", "hint") or "豆包需要登录后才能接收任务。"

    def _login_required(self, page_text, words):
        if self._find_input_anchor_no_click(page_text, words):
            return False
        return bool(self._login_signals(page_text))

    def _dismiss_modal(self, hwnd, words):
        markers = ("Unhandled exception", "Failed to execute script", "Traceback",
                   "错误", "发生错误", "警告", "风险提示")
        labels = ("Close", "关闭", "确定", "知道了", "我知道了", "取消")
        for _ in range(3):
            lines = _group_lines(words)
            text = "\n".join(line["text"] for line in lines)
            if not any(marker in text for marker in markers):
                return ""
            target = None
            for line in lines:
                if any(label in line["text"] for label in labels):
                    target = line
                    break
            if target is None:
                # 兜底：点弹窗常见的关闭位置
                self._click(hwnd, 0.78, 0.90)
            else:
                self._click_line(hwnd, target)
            time.sleep(0.9)
            _, words, _ = self._capture_words(hwnd)
        return "closed" if words else ""

    def _start_clean_chat(self, hwnd, words, evidence_dir, step):
        lines = _group_lines(words)
        text = "\n".join(line["text"] for line in lines)
        if "有什么我能帮你的吗" in _norm(text) and self._find_input_anchor_no_click(text, words):
            step("page_ready", mode="clean_chat")
            return
        # 工作任务的失败弹窗 / 本地电脑模式：优先点“新对话”回到普通对话
        for _ in range(2):
            target = None
            for line in lines:
                if "新对话" in line["text"]:
                    target = line
                    break
            if not target:
                break
            self._click_line(hwnd, target)
            time.sleep(2.4)
            _, words, text = self._capture_words(hwnd, evidence_dir / "04_after_new_chat.png")
            lines = _group_lines(words)
            text = "\n".join(line["text"] for line in lines)
            step("new_chat", chars=len(_norm(text)))
            if "有什么我能帮你的吗" in _norm(text) and self._find_input_anchor_no_click(text, words):
                return
            modal = self._dismiss_modal(hwnd, words)
            if modal:
                _, words, text = self._capture_words(hwnd)
                lines = _group_lines(words)
                text = "\n".join(line["text"] for line in lines)

    def _find_input_anchor_no_click(self, page_text, words):
        tokens = self._decl("anchors", "input", "name_contains", default=["发消息", "输入", "提问"]) or []
        for line in _group_lines(words):
            if line["y"] < 0.55:
                continue
            clean = re.sub(r"\W", "", line["text"])
            for token in tokens:
                if token and token in clean:
                    x0, y0, x1, y1 = _line_box(line)
                    return {"x": (x0 + x1) / 2, "y": y0 + (y1 - y0) / 2 + 0.012, "text": line["text"]}
        if "发消息" in (page_text or "") or "问点什么" in (page_text or ""):
            return {"x": 0.55, "y": 0.87, "text": "geometry"}
        return None

    def _find_input_anchor(self, hwnd, words, evidence_dir):
        anchor = self._find_input_anchor_no_click("\n".join(l["text"] for l in _group_lines(words)), words)
        if anchor:
            return anchor
        # 声明里的 geometry 兜底：底部、宽度 >= 30%
        band = (self._decl("anchors", "input", "geometry", "band") or "bottom")
        if band == "bottom":
            self._capture_words(hwnd, evidence_dir / "05_anchor_fallback.png")
            return {"x": 0.55, "y": 0.87, "text": "geometry_bottom"}
        return None

    def _focus_composer(self, hwnd, step=None, attempts=3):
        """注入前确认：目标窗口在前台，且同一个输入控件真的持有键盘焦点。

        20261110y 现场：回读 48 字恰好是用户原句去空格（咕嘎自己的输入条），
        豆包输入框截图却是空的——键鼠落到了别的窗口。这里用 UIA 取输入控件，
        先有界地把豆包抬回前台，再 SetFocus 并回读 HasKeyboardFocus；
        不合格就有限次重试，仍不合格返回 ``target_not_focused``，一个键都不发。

        返回 ``{ok, ref, element, service}`` 或 ``{ok: False, code, missing}``。
        """
        from core.uia_service import shared_service
        service = shared_service()
        last = dict(code="target_not_focused",
                    missing="豆包窗口不在前台或输入框没有键盘焦点，未写入任何键")
        for attempt in range(1, max(1, int(attempts)) + 1):
            try:
                foreground = int(ctypes.windll.user32.GetForegroundWindow() or 0)
            except Exception:
                foreground = 0
            if foreground != int(hwnd) and _ensure_foreground:
                try:
                    _ensure_foreground(hwnd, 1.0)
                except Exception:
                    pass
                try:
                    foreground = int(ctypes.windll.user32.GetForegroundWindow() or 0)
                except Exception:
                    foreground = 0
            if foreground != int(hwnd):
                last = dict(code="target_not_focused",
                            missing="豆包窗口不在前台（前台 hwnd=%s，目标 hwnd=%s），未写入任何键"
                                    % (foreground, hwnd))
                if step:
                    step("inject_focus", attempt=attempt, target_hwnd=int(hwnd),
                         foreground_hwnd=foreground, has_keyboard_focus=False,
                         ok=False, reason="foreground_mismatch")
                continue
            found = None
            # 先用真实控件特征（Group/Document + tiptap ProseMirror + 可聚焦 + TextPattern），
            # 再退回 page_input（Edit 唯一）与 first_edit；三条都是显式方法，不再靠 getattr 试。
            for probe, call in (
                    ("composer", lambda: service.composer(hwnd)),
                    ("page_input", lambda: service.page_input(hwnd)),
                    ("first_edit", lambda: service.first_edit(hwnd))):
                try:
                    found = call()
                    break
                except Exception as exc:
                    last = dict(code="composer_not_found",
                                missing="UIA 定位不到豆包输入框（%s）：%s"
                                        % (probe, str(exc)[:120]))
            element = (found or {}).get("element") or {}
            ref = element.get("ref")
            if not ref:
                if step:
                    step("inject_focus", attempt=attempt, target_hwnd=int(hwnd),
                         foreground_hwnd=foreground, ok=False, reason="composer_not_found")
                continue
            try:
                act = service.act(hwnd, "focus", ref=ref)
            except Exception as exc:
                last = dict(code="composer_not_found",
                            missing="输入框 SetFocus 失败：%s" % str(exc)[:120])
                if step:
                    step("inject_focus", attempt=attempt, ref=ref, ok=False,
                         reason="focus_failed", detail=str(exc)[:120])
                continue
            control = act.get("control") or {}
            same = (not control.get("ref")) or control.get("ref") == ref
            focused = bool(act.get("verified")) and same
            if step:
                step("inject_focus", attempt=attempt, target_hwnd=int(hwnd),
                     foreground_hwnd=foreground,
                     has_keyboard_focus=bool(act.get("verified")),
                     ref=ref, runtime_id=element.get("runtime_id"),
                     name=element.get("name"), automation_id=element.get("id"),
                     type=element.get("type"), class_name=element.get("class_name"),
                     keyboard_focusable=element.get("keyboard_focusable"),
                     text_pattern=element.get("text_pattern"),
                     source=(found or {}).get("source"),
                     ok=focused,
                     reason="" if focused else ("element_changed" if not same
                                                else "keyboard_focus_false"))
            if focused:
                return dict(ok=True, ref=ref, element=element, service=service,
                            source=(found or {}).get("source"), attempts=attempt)
            last = dict(code="target_not_focused",
                        missing="豆包输入框没有拿到键盘焦点（SetFocus 后 HasKeyboardFocus=false），"
                                "未写入任何键")
        return dict(ok=False, **last)

    @staticmethod
    def _looks_written_elsewhere(got, expected, clipboard_text=""):
        """回读与期望不一致、却和别处文本（剪贴板/咕嘎输入条）高度相似 → 写错地方。

        只做保守判定：完全相等，或回读整段是别处文本的一段（>=6 字）。
        """
        got, want = _norm(got), _norm(expected)
        if not got or got == want:
            return False
        clip = _norm(clipboard_text)
        if clip and (got == clip or (len(got) >= 6 and got in clip)):
            return True
        return False

    def _inject(self, hwnd, anchor, prompt, evidence_dir, step):
        # 注入顺序：① UIA 定位同一个输入控件并确认前台+键盘焦点（不合格一个键都不发）；
        # ② ValuePattern 同源写入 + 同源回读；③ 不支持 ValuePattern 才用剪贴板粘贴，
        # 但回读仍只读同一个 element——绝不退到“前台窗口文本/整窗 OCR”做输入校验。
        started = time.monotonic()

        def obs(mode, expected, got, attempts, matched, ref=None):
            self._obs('inject', stage='write', mode=mode, ref=ref,
                      expected=expected, got=got, attempts=attempts,
                      matched=bool(matched),
                      elapsed_ms=int((time.monotonic() - started) * 1000))

        driver = _ComposerDriver(self, hwnd, anchor)
        # 输入前模式检查走共用层：输入框挂着技能标签时，注入得再准也会被当成技能调用。
        guard = ensure_plain_mode(driver, log=lambda tag, payload: step(tag, **payload))
        if not guard["ok"]:
            return {"ok": False, "code": guard["code"], "missing": guard["missing"],
                    "attempts": 0, "where": "inject", "next_action": "移除技能标签后重派",
                    "hint": guard["missing"]}
        try:
            clipboard_before = _read_clipboard()
        except Exception:
            clipboard_before = ""
        focus_attempts = int(self._decl("anchors", "input", "retry", "focus_attempts", default=3) or 3)
        focused = self._focus_composer(hwnd, step, attempts=max(1, focus_attempts))
        if not focused.get("ok"):
            code = focused.get("code") or "target_not_focused"
            return {"ok": False, "code": code, "missing": focused.get("missing") or "",
                    "attempts": 0, "where": "inject", "next_action": "切到豆包窗口后重派",
                    "hint": ("注入前没确认到豆包输入框的键盘焦点（%s）。一个键都没有发，"
                             "文本不会被写到别的窗口；请把豆包窗口切到前台后重派。"
                             % (focused.get("missing") or code))}
        ref = focused["ref"]
        service = focused["service"]
        element = focused.get("element") or {}
        self._obs('composer', stage='locate', ref=ref,
                  source=focused.get("source"), attempts=focused.get("attempts"),
                  class_name=element.get("class_name"),
                  keyboard_focusable=element.get("keyboard_focusable"),
                  text_pattern=element.get("text_pattern"), ok=True)
        has_value_pattern = "ValuePattern" in (element.get("patterns") or [])
        back = None
        if has_value_pattern:
            try:
                service.act(hwnd, "set_value", ref=ref, value=prompt)
                back = service.value(hwnd, ref)
            except Exception as exc:
                back = None
                step("inject", attempt="uia_set_value", ok=False,
                     ref=ref, detail=str(exc)[:120])
        if back is not None:
            equal = _norm(back) == _norm(prompt)
            step("inject", attempt="uia_set_value", equal=equal, length=len(back),
                 sha=_sha(back)[:16], ref=ref)
            if equal:
                obs("uia_value", len(prompt), len(back), "uia_set_value", True, ref=ref)
                return {"ok": True, "attempts": "uia_set_value", "text": back,
                        "sha256": _sha(back), "mode": "uia_value", "ref": ref,
                        "matched": True}
            obs("uia_value", len(prompt), len(back), "uia_set_value", False, ref=ref)
            if self._looks_written_elsewhere(back, prompt, clipboard_before):
                return {"ok": False, "code": "input_written_elsewhere",
                        "missing": ("回读到的文本（%d 字）与期望（%d 字）不一致，"
                                    "且与本机别处文本（剪贴板/咕嘎输入条）高度相似："
                                    "文本被写到了别的窗口或控件" % (len(back), len(prompt))),
                        "attempts": 0, "where": "inject",
                        "next_action": "把豆包窗口切到前台后重派",
                        "hint": ("这次没有往别处继续写，也没有按回车：回读不是豆包输入框的内容。"
                                 "缺什么：豆包输入框里的本步文本。下一步：切到豆包窗口后重派。")}
        bound = _ComposerDriver(self, hwnd, anchor, ref=ref, service=service,
                                element=element)
        try:
            verify = input_verified(
                prompt, bound,
                attempts=max(1, focus_attempts),
                log=lambda tag, payload: step(tag, **payload))
        except ComposerNotFound as exc:
            return {"ok": False, "code": "composer_not_found",
                    "missing": str(exc)[:200], "attempts": 0, "where": "inject",
                    "next_action": "切到豆包窗口后重派",
                    "hint": ("注入后按同一个输入框控件回读不到内容（%s）；没有拿前台窗口/整窗 "
                             "OCR 文本充数，也没有按回车。下一步：切到豆包窗口后重派。" % str(exc)[:120])}
        if verify["ok"]:
            back = verify.get("text") or ""
            obs("clipboard", len(prompt), len(back), verify["attempts"], True, ref=ref)
            return {"ok": True, "attempts": verify["attempts"], "text": back,
                    "sha256": _sha(back), "mode": "clipboard", "ref": ref,
                    "matched": True}
        obs("clipboard", len(prompt), len(verify.get("last_text") or ""),
            verify["attempts"], False, ref=ref)
        try:
            foreground_now = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        except Exception:
            foreground_now = 0
        last_text = verify.get("last_text") or ""
        if (foreground_now != int(hwnd)
                or self._looks_written_elsewhere(last_text, prompt, clipboard_before)):
            return {"ok": False, "code": "input_written_elsewhere",
                    "missing": ("回读与期望不一致（期望 %d 字，回读 %d 字），且%s："
                                "文本被写到了别的窗口或控件"
                                % (len(prompt), len(last_text),
                                   ("前台已不是豆包（hwnd=%s）" % foreground_now)
                                   if foreground_now != int(hwnd)
                                   else "回读内容与本机别处文本高度相似")),
                    "attempts": verify["attempts"], "where": "inject",
                    "next_action": "把豆包窗口切到前台后重派",
                    "hint": ("这次没有重复下发：回读不是豆包输入框的内容。缺什么：豆包输入框里的"
                             "本步文本。下一步：切到豆包窗口后重派。")}
        return {"ok": False, "code": verify["code"], "missing": verify["missing"],
                "attempts": verify["attempts"], "ref": ref,
                "hint": ("输入框回读与要发的正文不一致（期望 %d 字，回读 %d 字），"
                         "已清空重试 %d 次仍未通过校验，本次没有按回车发送。"
                         "下一步：我会重新定位同一个输入控件再写一次；仍对不上就停在这里"
                         "报 input_verify_failed，不重复发送。"
                         % (len(_norm(prompt)), len(_norm(last_text)), verify["attempts"]))}

    def _composer_has_prompt(self, words, prompt):
        """输入框（底部带）里是否还留着要发的提问。

        OCR 对长句会滚出视区/打乱标点，所以“已发送”不能靠整页里找提问回显，
        只能看输入框本身是否清空 + 页面是否增长。
        """
        needle = _key(prompt)[:8]
        if not needle:
            return False
        for line in _group_lines(words):
            if line["y"] < 0.76:
                continue
            if needle in _key(line["text"]):
                return True
        return False

    @staticmethod
    def _generating(words, page_text):
        """是否仍在生成：OCR 有词框就用词框，降级时用 UIA 文本直接找“停止生成”。"""
        for line in _group_lines(words):
            if "停止生成" in line["text"] or line["text"].strip() in ("停止", "停止回答"):
                return True
        text = _norm(page_text)
        return "停止生成" in text or "停止回答" in text

    def _turn_markers(self, hwnd):
        """结构信号：回答动作节点（复制/重新生成/点赞/分享）的计数。

        20261110w 现场：attempt1 因页面滚动/浮层导致 `grew=-40`，被误判成
        send_not_confirmed 又多派了一次提问。每个新回答气泡都会带一组动作节点，
        所以“计数相对基线增加”比 OCR 字数稳，滚动和浮层不会让计数变小。

        返回 ``None`` 表示结构信号不可用（UIA 快照失败/无元素），此时才退回字数。
        """
        try:
            from core.uia_service import shared_service
            snapshot = shared_service().snapshot(hwnd, limit=400, time_budget=2.5)
        except Exception:
            return None
        elements = snapshot.get("elements") or []
        if not elements:
            return None
        names = ("复制", "重新生成", "重新回答", "点赞", "踩", "分享")
        count = 0
        for element in elements:
            name = str(element.get("name") or "").strip()
            if not name:
                continue
            if name in names or "重新生成" in name or "重新回答" in name:
                count += 1
        return count

    @staticmethod
    def _fingerprint(prompt):
        """本步正文的指纹：开头 14 字 + 结尾 12 字（去空白、去标点）。

        用户气泡可能因为 OCR 把长句折行/漏字/丢标点，所以用“开头+结尾”两头比对，
        不要求整段相等；开头 14 字已足够唯一。
        """
        flat = _key(prompt)
        if len(flat) < 8:
            return flat, flat
        head = flat[:14]
        tail = flat[-12:] if len(flat) >= 20 else flat[-8:]
        return head, tail

    def _bubble_hits(self, words, prompt):
        """对话区（输入框以上，y<0.76）里本步正文的命中次数。

        返回 ``(head_count, tail_count, first_y)``。用次数而不是“有没有”：
        同一句在本轮之前可能已经发过，只有次数增加才算新增一条用户气泡。
        """
        head, tail = self._fingerprint(prompt)
        if not head:
            return 0, 0, None
        lines = [line for line in _group_lines(words) if line["y"] < 0.76]
        upper = "".join(_key(line["text"]) for line in lines)
        first_y = None
        for line in lines:
            if head in _key(line["text"]):
                first_y = round(line["y"], 3)
                break
        return upper.count(head), (upper.count(tail) if tail else 0), first_y

    def _send_button_state(self, hwnd, words=None, generating=False):
        """发送按钮态：stop=已变成“停止生成”，send=仍是发送键，unknown=读不到。"""
        if generating:
            return "stop"
        if not words:
            return "unknown"
        try:
            from core.uia_service import shared_service
            snapshot = shared_service().snapshot(hwnd, limit=300, time_budget=1.2)
        except Exception:
            return "unknown"
        names = [str(item.get("name") or "").strip()
                 for item in snapshot.get("elements") or []]
        if any("停止生成" in name or "停止回答" in name for name in names):
            return "stop"
        if any(name in ("发送", "Send") for name in names):
            return "send"
        return "unknown"

    @staticmethod
    def _send_confirmation(signals):
        """发送确认：四信号任一强证据成立即确认。

        设计原则：Enter 按下去之后豆包 UI 最直观的反馈就是输入框清空。
        这个信号和豆包回复多少字完全无关——哪怕豆包只回一个"好"，Enter 也是发出去了。
        字数增长只是额外佐证，不是前提条件。

        signals: dict(bubble_before, bubble_after, tail_after, composer_before,
                      still_in_composer, button_state, generating, grew)
        返回 ``(confirmed, confirm_signal, detail)``。
        """
        before = int(signals.get("bubble_before") or 0)
        after = int(signals.get("bubble_after") or 0)
        grew = int(signals.get("grew") or 0)
        # 信号 1（最强）：对话区出现了我们的用户气泡
        if after > before:
            return (True, "user_bubble",
                    "对话区新增用户气泡：正文指纹命中 %d→%d（尾部命中 %d 次）；"
                    "字数变化 %+d 仅作辅助"
                    % (before, after, int(signals.get("tail_after") or 0), grew))
        # 信号 2：发送前 inject 已确认写入 + 发送后输入框清空（最严铁证链）
        # 20261110t 真机修：不设字数门槛——豆包回复一个"好"也是真回复，字数增长
        # 只是额外佐证。但「发送前框里确实有本步正文」是前提，不能省：没有这个
        # 前提，发送后的空输入框什么也证明不了（可能压根没写进去，或焦点不在框里）。
        if signals.get("composer_before") and not signals.get("still_in_composer"):
            return (True, "input_cleared",
                    "输入框已清空回占位文案（发送前同源回读已确认写入；"
                    "发送后判空走 %s）；页面字数变化 %+d 仅作说明，不是确认门槛"
                    % (signals.get("composer_probe") or "ocr", grew))
        # 信号 3：豆包 UIA 按钮态变成"停止生成"
        if str(signals.get("button_state")) == "stop":
            return (True, "send_button_state", "发送按钮已变成「停止生成」")
        # 信号 4：豆包已经在生成回复了（即使输入框还没完全清空的极端情况）
        if signals.get("generating") and grew >= 0:
            return (True, "generating",
                    "豆包已开始生成回复，页面字数 %+d" % grew)
        return (False, "",
                "四信号都未成立：气泡 %d→%d、输入框仍留原文=%s、按钮态=%s、generating=%s；"
                "字数变化 %+d 仅作辅助"
                % (before, after, bool(signals.get("still_in_composer")),
                   signals.get("button_state") or "unknown",
                   bool(signals.get("generating")), grew))

    def _composer_state_uia(self, hwnd, ref):
        """用 UIA 读同一个输入控件，判断本步正文还在不在框里。

        返回 ``(state, chars)``，state ∈ ``empty`` / ``has_text`` / ``unknown``。
        为什么不能只看 OCR：20261110ab 现场 Windows OCR 把输入框里 105 字读成
        “PPT扌是示i司生巧生成提示词…”，既判不出“还在框里”，也判不出“已清空”。
        UIA TextPattern 是同一个控件的同源回读，阈值只判空/非空，稳定得多。
        """
        if not ref:
            return "unknown", 0
        try:
            from core.uia_service import shared_service
            got = shared_service().text_of(hwnd, ref)
        except Exception:
            return "unknown", 0
        if not got or got.get("ok") is False:
            return "unknown", 0
        text = _strip_composer_placeholder(got.get("text") or "")
        if not text:
            return "empty", 0
        return "has_text", len(_key(text))

    def _send_and_confirm(self, hwnd, prompt, evidence_dir, step, ref=None,
                          composer_verified=False):
        started = time.monotonic()
        # 硬规则：一旦确认发出过，任何入口都不允许再写一遍/再回车。
        if getattr(self, "_submit_state", "") == "sent_confirmed":
            step("resend_rejected", submit_state="sent_confirmed",
                 reason="本轮已确认发送过，禁止重复下发（只允许重试滚动/读取）")
            self._obs('send', stage='resend_rejected', confirm_signal='reused', attempts=0)
            return {"ok": True, "reused": True, "submit_state": "sent_confirmed",
                    "signal": "reused", "confirm_signal": "reused",
                    "turns_before": None, "turns_after": None,
                    "baseline_chars": 0, "chars": 0}
        # 发送前基线：对话区里本步正文已有的气泡数（同句历史气泡会被计进去）。
        try:
            _, before_words, before_text = self._capture_words_timed(
                hwnd, evidence_dir / "06_before_send.png")
        except CaptureTimeout as exc:
            step("capture_timeout", where="before_send", detail=str(exc)[:120])
            self._obs('timeout', stage='before_send', detail=str(exc)[:160],
                      elapsed_ms=int((time.monotonic() - started) * 1000))
            return dict(ok=False, code="capture_timeout", baseline_chars=0, chars=0,
                        missing="发送前抓屏超时：%s" % str(exc)[:80],
                        where="send", next_action="检查豆包窗口是否卡死或白屏后重派")
        baseline = len(_norm(before_text))
        bubble_before, _, _ = self._bubble_hits(before_words, prompt)
        # 发送前“框里有本步正文”以刚刚通过校验的写入为准：写入链路已经同源回读
        # 105/105，OCR 读不准小字号输入框，不能拿它当否定证据。
        composer_before = bool(composer_verified) or self._composer_has_prompt(before_words, prompt)
        turns_before = self._turn_markers(hwnd)
        self._press_send(hwnd)
        time.sleep(1.2)
        last = dict(ok=False, baseline_chars=baseline, chars=baseline,
                    confirm_signal="", bubble_before=bubble_before,
                    bubble_after=bubble_before, turns_before=turns_before)
        resend_used = False

        def composer_after_send(words):
            """发送后输入框是否已空：UIA 同源回读优先，读不到才退回 OCR。"""
            state, uia_chars = self._composer_state_uia(hwnd, ref)
            if state == "has_text":
                return True, "uia", uia_chars
            if state == "empty":
                return False, "uia", 0
            return self._composer_has_prompt(words, prompt), "ocr", 0

        def verdict(words, text, attempt):
            """按“用户气泡 / 输入框清空 / 按钮态”判定这一轮是否已发出。"""
            page = _norm(text)
            generating = self._generating(words, text)
            still_in_composer, composer_src, composer_chars = composer_after_send(words)
            bubble_after, tail_after, bubble_y = self._bubble_hits(words, prompt)
            grew = len(page) - baseline
            # 气泡/输入框已能给出结论时不再扫 UIA，省一轮结构查询。
            button = "unknown"
            if bubble_after <= bubble_before and (not composer_before or still_in_composer):
                button = self._send_button_state(hwnd, words, generating)
            signals = dict(bubble_before=bubble_before, bubble_after=bubble_after,
                           tail_after=tail_after, composer_before=composer_before,
                           still_in_composer=still_in_composer, button_state=button,
                           composer_probe=composer_src,
                           generating=generating, grew=grew)
            confirmed, used, detail = self._send_confirmation(signals)
            info = dict(attempt=attempt, baseline=baseline, chars=len(page), grew=grew,
                        composer_cleared=not still_in_composer,
                        composer_probe=composer_src, composer_chars=composer_chars,
                        generating=generating,
                        bubble_before=bubble_before, bubble_after=bubble_after,
                        bubble_y=bubble_y, button_state=button,
                        confirm_signal=used or "",
                        confirm_elapsed_ms=int((time.monotonic() - started) * 1000),
                        detail=detail)
            step("send", confirmed=confirmed, signal=used or "", **info)
            return dict(page=page, text=text, info=info, confirmed=confirmed,
                        signal=used, generating=generating,
                        still_in_composer=still_in_composer)

        def confirmed_result(out, attempt):
            info = out["info"]
            self._submit_state = "sent_confirmed"
            self._obs('send', stage='confirm', confirm_signal=info["confirm_signal"],
                      attempts=attempt, expected=len(prompt), got=info["chars"],
                      bubble_before=info["bubble_before"],
                      bubble_after=info["bubble_after"],
                      elapsed_ms=info["confirm_elapsed_ms"])
            return {"ok": True, "attempt": attempt, "baseline_chars": baseline,
                    "chars": info["chars"], "generating": info["generating"],
                    "confirm_signal": info["confirm_signal"],
                    "confirm_elapsed_ms": info["confirm_elapsed_ms"],
                    "signal": info["confirm_signal"],
                    "bubble_before": info["bubble_before"],
                    "bubble_after": info["bubble_after"],
                    "turns_before": turns_before, "turns_after": None,
                    "submit_state": "sent_confirmed"}

        for attempt in range(1, 4):
            try:
                frame, words, text = self._capture_words_timed(
                    hwnd, evidence_dir / ("07_sent_%d.png" % attempt))
            except CaptureTimeout as exc:
                step("capture_timeout", where="send_poll", attempt=attempt,
                     detail=str(exc)[:120])
                self._obs('timeout', stage='send_poll', attempts=attempt,
                          detail=str(exc)[:160],
                          elapsed_ms=int((time.monotonic() - started) * 1000))
                return dict(ok=False, code="capture_timeout", baseline_chars=baseline,
                            chars=0, missing="发送后抓屏超时：%s" % str(exc)[:80],
                            where="send", next_action="检查豆包窗口是否卡死或白屏后重派")
            out = verdict(words, text, attempt)
            wrong = sent_mode_check(text, log=lambda tag, payload: step(tag, **payload))
            if not wrong["ok"]:
                # 走错模式（正在生成图片/幻灯片…）：这类回合没有文字增量，重试只会更糟。
                return {"ok": False, "code": "wrong_skill", "attempt": attempt,
                        "baseline_chars": baseline, "chars": out["info"]["chars"],
                        "marker": wrong["marker"], "missing": wrong["missing"],
                        "where": wrong["where"], "next_action": wrong["next_action"]}
            last = dict(ok=False, baseline_chars=baseline, chars=out["info"]["chars"],
                        confirm_signal=out["info"]["confirm_signal"],
                        bubble_before=bubble_before,
                        bubble_after=out["info"]["bubble_after"],
                        detail=out["info"]["detail"],
                        still_in_composer=out["still_in_composer"],
                        turns_before=turns_before)
            if out["confirmed"]:
                return confirmed_result(out, attempt)
            # 补发前必须先核对上一轮是不是其实已经产生气泡：有就直接判确认，不再按回车。
            if out["info"]["bubble_after"] > bubble_before:
                return confirmed_result(out, attempt)
            if out["still_in_composer"] and not resend_used:
                # 只有“确实没有新气泡 且 输入框仍留原文”才允许这一次补发。
                resend_used = True
                step("resend", attempt=attempt, reason="no_bubble_composer_keeps_text",
                     channel="enter", submit_state="unconfirmed")
                self._obs('send', stage='resend', reason='no_bubble_composer_keeps_text',
                          channel='enter', attempts=attempt)
                self._press_send(hwnd)
            time.sleep(1.8)
        # 回车没确认且没补发过：点输入框右侧发送箭头（仍只算唯一一次补发）。
        if not resend_used and last.get("still_in_composer"):
            try:
                _, words_now, text_now = self._capture_words_timed(
                    hwnd, evidence_dir / "07_pre_click.png")
            except CaptureTimeout as exc:
                step("capture_timeout", where="send_pre_click", detail=str(exc)[:120])
                self._obs('timeout', stage='send_pre_click', detail=str(exc)[:160],
                          elapsed_ms=int((time.monotonic() - started) * 1000))
                return dict(ok=False, code="capture_timeout", baseline_chars=baseline,
                            chars=0, missing="补发前抓屏超时：%s" % str(exc)[:80],
                            where="send", next_action="检查豆包窗口是否卡死或白屏后重派")
            pre = verdict(words_now, text_now, "pre_click")
            if pre["confirmed"]:
                return confirmed_result(pre, "pre_click")
            resend_used = True
            step("resend", attempt="click", reason="no_bubble_composer_keeps_text",
                 channel="click", submit_state="unconfirmed")
            self._obs('send', stage='resend', reason='no_bubble_after_enter',
                      channel='click')
            try:
                self._click(hwnd, 0.947, 0.936)
                step("send_click", ok=True)
                time.sleep(1.4)
                try:
                    frame, words, text = self._capture_words_timed(
                        hwnd, evidence_dir / "07_sent_click.png")
                except CaptureTimeout as exc:
                    step("capture_timeout", where="send_click", detail=str(exc)[:120])
                    self._obs('timeout', stage='send_click', detail=str(exc)[:160],
                              elapsed_ms=int((time.monotonic() - started) * 1000))
                    return dict(ok=False, code="capture_timeout", baseline_chars=baseline,
                                chars=0, missing="点发送后抓屏超时：%s" % str(exc)[:80],
                                where="send",
                                next_action="检查豆包窗口是否卡死或白屏后重派")
                out = verdict(words, text, "click")
                wrong = sent_mode_check(text, log=lambda tag, payload: step(tag, **payload))
                if not wrong["ok"]:
                    return {"ok": False, "code": "wrong_skill", "attempt": "click",
                            "baseline_chars": baseline, "chars": out["info"]["chars"],
                            "marker": wrong["marker"], "missing": wrong["missing"],
                            "where": wrong["where"], "next_action": wrong["next_action"]}
                last = dict(ok=False, baseline_chars=baseline, chars=out["info"]["chars"],
                            confirm_signal=out["info"]["confirm_signal"],
                            bubble_before=bubble_before,
                            bubble_after=out["info"]["bubble_after"],
                            detail=out["info"]["detail"],
                            still_in_composer=out["still_in_composer"],
                            turns_before=turns_before)
                if out["confirmed"]:
                    return confirmed_result(out, "click")
            except Exception as exc:
                step("send_click", ok=False, detail=str(exc)[:80])
        step("send_giveup", reason="no_confirm_signal", confirm_signal="none")
        self._obs('send', stage='giveup', confirm_signal='none', attempts=3,
                  elapsed_ms=int((time.monotonic() - started) * 1000),
                  bubble_before=bubble_before,
                  bubble_after=last.get("bubble_after"))
        last["ok"] = False
        return last

    def _wait_done(self, hwnd, prompt, limit, evidence_dir, step, baseline=0,
                   baseline_before=None):
        """等豆包生成完：done.any 三信号任一成立即完成（不再要求 >=120 字）。

        信号：stop_button_gone（生成中标志消失）/ artifact_new（新文档卡片出现）/
        stable_2s（整页文本连续 2 秒无变化）。生成中（OCR 仍见“停止生成”）时不判完成。
        长度只用于“页面是否为空/是否变化”，不再作为完成门槛。
        """
        poll = float(self._decl("anchors", "done", "poll_seconds", default=1.0) or 1.0)
        stable_seconds = float(self._decl("anchors", "done", "stable_seconds", default=2.0) or 2.0)
        # 声明里的 done.any 是完成信号的权威来源：没声明的信号不参与判定。
        declared = self._decl("anchors", "done", "any", default=None)
        enabled = {"action_bar", "stop_button_gone", "answer_text_stable_2s", "artifact_new"}
        if isinstance(declared, (list, tuple)) and declared:
            names = set()
            for item in declared:
                key = str(item or "").strip().lower()
                if key in ("action_bar", "actionbar", "operation_bar"):
                    names.add("action_bar")
                elif key == "stop_button_gone":
                    names.add("stop_button_gone")
                elif key in ("answer_text_stable_2s", "answer_text_stable", "stable_2s"):
                    names.add("answer_text_stable_2s")
                elif key in ("artifact_new", "new_card", "artifact"):
                    names.add("artifact_new")
            enabled = names or enabled
        deadline = time.monotonic() + max(10.0, limit)
        started_at = time.monotonic()
        before = int(baseline_before if baseline_before is not None else baseline)
        last_len = None
        last_text = None
        changed_at = time.monotonic()
        saw_stop = False
        card_markers = None
        self._bar_during_generate = None
        samples = []
        while time.monotonic() < deadline:
            time.sleep(poll)
            try:
                frame, words, text = self._capture_words_timed(
                    hwnd, evidence_dir / "poll.png")
            except CaptureTimeout as exc:
                step("poll_timeout", detail=str(exc)[:120])
                self._obs('timeout', stage='poll', detail=str(exc)[:160],
                          elapsed_ms=int((time.monotonic() - started_at) * 1000))
                return {"ok": False, "code": "capture_timeout", "done_signal": "timeout",
                        "samples": samples,
                        "missing": "单轮截图/识别超时（%s），已中断本轮轮询" % str(exc)[:80],
                        "next_action": "检查豆包窗口是否卡死或白屏后重派"}
            page = _norm(text)
            generating = self._generating(words, text)
            length = len(page)
            now = time.monotonic()
            samples.append({"t": round(now, 2), "chars": length,
                            "generating": generating, "sha": _sha(page)[:12]})
            step("poll", chars=length, generating=generating)
            bar = self._find_action_bar(hwnd, time_budget=2.5)
            bar_present = bool(bar.get("present"))
            samples[-1]["action_bar"] = bar_present
            if generating and self._bar_during_generate is None:
                # 生成中必须留一条“操作栏缺席”的快照证据（把假设变成实测）。
                self._bar_during_generate = dict(
                    present=bar_present, candidates=int(bar.get("candidates") or 0),
                    top=(bar.get("rect") or {}).get("top"), chars=length)
                step("action_bar_generate", **self._bar_during_generate)
                self._obs('done', stage='generate', done_signal='none',
                          action_bar=bar_present, chars=length,
                          detail='生成中操作栏快照')
            wrong = sent_mode_check(text, log=lambda tag, payload: step(tag, **payload))
            if not wrong["ok"]:
                return {"ok": False, "code": "wrong_skill", "missing": wrong["missing"],
                        "where": wrong["where"], "next_action": wrong["next_action"],
                        "marker": wrong["marker"], "samples": samples}
            if not page and len(samples) >= 3:
                frame2, _, text2 = self._capture_words(hwnd)
                if self._is_blank(frame2, text2):
                    code = ("ocr_empty"
                            if (not self._frame_blank(frame2) and self._last_ocr_empty)
                            else "window_blank")
                    return {"ok": False, "code": code, "samples": samples}
            if generating:
                saw_stop = True
            if last_text is None or page != last_text:
                last_text = page
                changed_at = now
            last_len = length
            markers = sum(1 for marker in DOCUMENT_CARD_MARKERS
                          if marker in str(text or ""))
            if card_markers is None:
                card_markers = markers
            artifact_new = markers > card_markers
            changed_since_before = (before > 0 and length != before)
            started = saw_stop or artifact_new or changed_since_before or length != samples[0]["chars"]
            stable_2s = (started and last_text and
                         (now - changed_at) >= stable_seconds)
            signal = ""
            if not generating:
                if bar_present and "action_bar" in enabled:
                    signal = "action_bar"
                elif saw_stop and "stop_button_gone" in enabled:
                    signal = "stop_button_gone"
                elif artifact_new and "artifact_new" in enabled:
                    signal = "artifact_new"
                elif stable_2s and "answer_text_stable_2s" in enabled:
                    signal = "stable_2s"
            if signal:
                page_sha = _sha(page)
                step("done", done_signal=signal, chars=length, generating=False,
                     markers=markers)
                elapsed_ms = int((time.monotonic() - started_at) * 1000)
                self._obs('done', stage='wait', done_signal=signal, chars=length,
                          elapsed_ms=elapsed_ms, samples=len(samples),
                          chars_seq=[s["chars"] for s in samples[-6:]],
                          sha_seq=[s.get("sha", "") for s in samples[-6:]])
                return {"ok": True, "done_signal": signal, "samples": samples,
                        "done_elapsed_ms": elapsed_ms,
                        "page_sha256": page_sha, "chars": length,
                        "action_bar": bar,
                        "bar_during_generate": self._bar_during_generate,
                        "stop_seen": saw_stop, "artifact_new": artifact_new,
                        "stable_seconds": stable_seconds}
        step("done", done_signal="timeout", chars=last_len or 0)
        self._obs('done', stage='wait', done_signal='timeout', chars=last_len or 0,
                  elapsed_ms=int((time.monotonic() - started_at) * 1000),
                  samples=len(samples),
                  chars_seq=[s["chars"] for s in samples[-6:]],
                  sha_seq=[s.get("sha", "") for s in samples[-6:]])
        return {"ok": False, "code": "timeout", "done_signal": "timeout",
                "samples": samples,
                "bar_during_generate": self._bar_during_generate,
                "hint": ("豆包在限定时间内没有出现完成信号（操作栏出现/停止按钮消失/新产物/"
                         "文本稳定 2 秒）。下一步：我会重抓一屏确认豆包是否仍在生成，"
                         "仍无信号就报 timeout，不编造已完成。")}

    def _copy_anchor(self, words):
        """回答操作行的锚点 -> 复制图标的窗口相对坐标。

        OCR 常把“费”认成数字（例如“免0”），所以只认单字“免”；
        识别不到时退到同一行的“今天 HH:MM”时间戳。
        """
        marks = [w for w in (words or [])
                 if str(w.get("text")) == "免" and 0.3 < float(w["y"]) < 0.86
                 and float(w["x"]) > 0.25]
        if marks:
            hit = max(marks, key=lambda w: float(w["y"]))
            return float(hit["x"]) - 0.155, float(hit["y"]) + float(hit.get("h", 0.02)) / 2
        stamps = [w for w in (words or [])
                  if str(w.get("text")) == "今" and 0.3 < float(w["y"]) < 0.86
                  and float(w["x"]) > 0.4]
        if stamps:
            hit = max(stamps, key=lambda w: float(w["y"]))
            return float(hit["x"]) - 0.25, float(hit["y"]) + float(hit.get("h", 0.02)) / 2
        return None

    def _copy_via_answer_button(self, hwnd, words, evidence_dir, step):
        """点回答操作行最左侧的“复制”图标。

        豆包的对话区点击会把焦点交回输入框，Ctrl+A 只能选中空输入框，
        所以必须走自带的复制按钮；点“…”菜单是二级兜底。
        """
        anchor = self._copy_anchor(words)
        if not anchor:
            return ""
        base, y = anchor
        for dx in (0.0, -0.012, 0.012):
            _write_clipboard(COPY_SENTINEL)
            self._click(hwnd, base + dx, y)
            time.sleep(0.8)
            copied = _read_clipboard()
            step("copy_button", dx=dx, length=len(copied))
            # 一句话回答（如“一句话 PPT 提示词”）只有十几个字，不能按长回答的 80 字门槛拒绝；
            # 复制内容是否可信由 _answer_visible_in_page 与页面 OCR 比对决定。
            if _clip_changed(copied):
                return copied
        return ""

    def _copy_via_more_menu(self, hwnd, words, evidence_dir, step):
        anchor = self._copy_anchor(words)
        if not anchor:
            return ""
        base, y = anchor
        for dx in (0.223, 0.213, 0.233):
            self._click(hwnd, base + dx, y)
            time.sleep(0.7)
            _, menu_words, menu_text = self._capture_words(hwnd, evidence_dir / "09b_more_menu.png")
            target = None
            for item in _group_lines(menu_words):
                text = item["text"].strip()
                if len(text) <= 8 and text in ("复制", "复制文本", "Copy", "复制回答"):
                    target = item
                    break
            step("more_menu", dx=dx, found=bool(target), chars=len(_norm(menu_text)))
            if target:
                _write_clipboard(COPY_SENTINEL)
                self._click_line(hwnd, target)
                time.sleep(0.8)
                copied = _read_clipboard()
                step("more_menu_copy", length=len(copied))
                if _clip_changed(copied):
                    return copied
            self._send([27])
            time.sleep(0.3)
        return ""

    @staticmethod
    def _uia_copy_buttons(hwnd):
        """UIA 结构里找回答区末尾的“复制 / 更多”按钮，换算成窗口相对坐标。

        这是复制锚点的第一级：Chromium/Electron 会给工具条按钮暴露 Name（aria-label），
        不依赖 OCR 把“免”或“今 HH:MM”认出来，因此长回答滚到底后也能稳定命中。
        """
        try:
            from core.uia_service import shared_service
            snapshot = shared_service().snapshot(hwnd, limit=240, time_budget=2.5)
        except Exception:
            return {}
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width, height = max(1, right - left), max(1, bottom - top)
        copies, mores = [], []
        for element in snapshot.get("elements") or []:
            if element.get("offscreen"):
                continue
            box = _rect_box(element.get("rect"))
            if box is None:
                continue
            x0, y0, x1, y1 = box
            cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            if not (left <= cx <= right and top <= cy <= bottom):
                continue
            name = str(element.get("name") or "").strip()
            if not name:
                continue
            rel_y = (cy - top) / height
            if rel_y < 0.25:          # 回答操作行在对话区靠下，顶部的按钮不算
                continue
            hit = {"x": (cx - left) / width, "y": rel_y,
                   "name": name[:40], "type": str(element.get("type") or "")}
            if ("复制" in name or name.lower() == "copy") and "链接" not in name:
                copies.append(hit)
            elif "更多" in name or name.lower() == "more":
                mores.append(hit)
        return {"copy": max(copies, key=lambda item: item["y"]) if copies else None,
                "more": max(mores, key=lambda item: item["y"]) if mores else None}

    def _find_action_bar(self, hwnd, step=None, with_chips=False, time_budget=5.0):
        """按几何位置找最新一条回答下方的操作栏（不按中文名匹配）。

        20261110ac 真机实测：复制按钮 name 为空串，只有 ``type=Button``、24×24、
        与同排按钮同一 ``top``（±4px）、横排 ≥3 个、位于回答正文末行下方 0~60px。
        返回 ``present/ref/rect/buttons/candidates/chips``。
        """
        try:
            from core.uia_service import shared_service
            snap = shared_service().snapshot(hwnd, limit=650, time_budget=time_budget)
        except Exception as exc:
            return {"present": False, "candidates": 0, "buttons": [], "chips": [],
                    "error": "%s: %s" % (type(exc).__name__, exc)}
        elements = snap.get("elements") or []
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        _width, height = max(1, right - left), max(1, bottom - top)

        composer_top = None
        for element in elements:
            cls = str(element.get("class_name") or "").lower()
            if ("prosemirror" in cls or "tiptap" in cls) and element.get("rect"):
                composer_top = float(element["rect"]["top"])
                break
        if composer_top is None:
            composer_top = top + height * 0.93

        bottoms = []
        for element in elements:
            if not element.get("text_pattern") or not element.get("rect"):
                continue
            box = _rect_box(element.get("rect"))
            if box is None:
                continue
            x0, y0, x1, y1 = box
            if (x1 - x0) < 80 or not (8 <= (y1 - y0) <= 60):
                continue
            if y0 < top + height * 0.30 or y1 > composer_top:
                continue
            bottoms.append(y1)
        answer_bottom = max(bottoms) if bottoms else None

        buttons = []
        for element in elements:
            if str(element.get("type")) != "Button" or not element.get("rect"):
                continue
            if element.get("enabled") is False:
                continue
            box = _rect_box(element.get("rect"))
            if box is None:
                continue
            x0, y0, x1, y1 = box
            if not (ACTION_BAR_MIN_SIZE <= (x1 - x0) <= ACTION_BAR_MAX_SIZE
                    and ACTION_BAR_MIN_SIZE <= (y1 - y0) <= 34):
                continue
            buttons.append(element)

        groups = []
        for element in sorted(buttons, key=lambda item: float(item["rect"]["top"])):
            row_top = float(element["rect"]["top"])
            for group in groups:
                if abs(group["top"] - row_top) <= ACTION_BAR_TOLERANCE:
                    group["items"].append(element)
                    break
            else:
                groups.append({"top": row_top, "items": [element]})

        bars = []
        for group in groups:
            items = sorted(group["items"], key=lambda item: float(item["rect"]["left"]))
            chain = [items[0]]
            for element in items[1:]:
                gap = float(element["rect"]["left"]) - float(chain[-1]["rect"]["right"])
                if gap <= ACTION_BAR_MAX_GAP:
                    chain.append(element)
                else:
                    if len(chain) >= ACTION_BAR_MIN_BUTTONS:
                        bars.append({"top": group["top"], "items": chain})
                    chain = [element]
            if len(chain) >= ACTION_BAR_MIN_BUTTONS:
                bars.append({"top": group["top"], "items": chain})

        if answer_bottom is not None:
            rows_all = list(bottoms)
        else:
            rows_all = []
        # 先不看正文末行（建议追问条也是文本行，会把末行顶到最下面）：
        # 取输入框上方最靠下的那一排 ≥3 个方块按钮，再用“这排上方 0~60px 有正文末行”复核。
        bars = [bar for bar in bars
                if bar["top"] < composer_top - 10 and bar["top"] > top + height * 0.30]
        bars.sort(key=lambda bar: (bar["top"], len(bar["items"])))
        result = {"present": False, "candidates": len(bars), "buttons": [], "chips": [],
                  "answer_bottom": answer_bottom, "composer_top": composer_top}
        chosen_bar = None
        for candidate_bar in reversed(bars):
            above = [bottom for bottom in rows_all
                     if bottom <= candidate_bar["top"] + 6]
            if not above:
                continue
            nearest = max(above)
            if candidate_bar["top"] - 60 <= nearest <= candidate_bar["top"] + 6:
                chosen_bar = candidate_bar
                result["answer_bottom"] = nearest
                break
        if chosen_bar is None:
            if step:
                step("action_bar_probe", present=False, candidates=len(bars),
                     answer_bottom=answer_bottom, composer_top=composer_top)
            return result
        bar = chosen_bar
        chosen = bar["items"][0]
        rect = _rect_box(chosen.get("rect"))
        result.update(
            present=True,
            ref=chosen.get("ref"),
            name=str(chosen.get("name") or ""),
            rect=dict(left=rect[0], top=rect[1], right=rect[2], bottom=rect[3]),
            buttons=[{"ref": item.get("ref"), "name": str(item.get("name") or "")[:20],
                      "rect": _rect_box(item.get("rect"))} for item in bar["items"]])
        if with_chips:
            result["chips"] = self._suggestion_chips(hwnd, elements, bar, composer_top)
        if step:
            step("action_bar_probe", present=True, candidates=len(bars),
                 top=bar["top"], buttons=len(bar["items"]),
                 rect=result["rect"], chips=len(result["chips"]))
        return result

    def _suggestion_chips(self, hwnd, elements, bar, composer_top):
        """建议追问条：操作栏下方、单行 18px、左边界对齐、间距 46px 的短文本。"""
        bar_bottom = max((_rect_box(item.get("rect")) or (0, 0, 0, 0))[3]
                         for item in bar["items"])
        cand = []
        for element in elements:
            if not element.get("text_pattern") or not element.get("rect"):
                continue
            box = _rect_box(element.get("rect"))
            if box is None:
                continue
            x0, y0, x1, y1 = box
            if not (16 <= (y1 - y0) <= 20) or (x1 - x0) < 120:
                continue
            if not (bar_bottom < y0 < composer_top - 5):
                continue
            cand.append((x0, y0, element))
        if len(cand) < 2:
            return []
        base_x = min(x0 for x0, _, _ in cand)
        aligned = [item for item in cand if abs(item[0] - base_x) <= 8]
        aligned.sort(key=lambda item: item[1])
        gaps = [round(aligned[i + 1][1] - aligned[i][1], 1) for i in range(len(aligned) - 1)]
        if len(aligned) >= 2 and not any(abs(gap - 46) <= 6 for gap in gaps):
            return []
        texts = []
        try:
            from core.uia_service import shared_service
        except Exception:
            return []
        service = None
        try:
            service = shared_service()
        except Exception:
            service = None
        if service is None:
            return []
        for _, _, element in aligned[:4]:
            try:
                text = (service.text_of(hwnd, element["ref"], limit=200).get("text") or "").strip()
            except Exception:
                continue
            if text:
                texts.append(text[:60])
        return texts

    def _copy_via_action_bar(self, hwnd, evidence_dir, step):
        """主路径：点最新回答操作栏最左按钮（复制）→ 读剪贴板。

        先用 InvokePattern；失败退回物理左键（点击前先把鼠标移开 ≥0.4s，
        否则悬停提示会滞后，无法确认按钮语义）。剪贴板快照/还原由
        ``_capture_answer`` 的会话盒统一负责。
        """
        bar = self._find_action_bar(hwnd, step=step, with_chips=True)
        self._last_action_bar = bar
        if not bar.get("present"):
            step("action_bar", present=False, candidates=bar.get("candidates", 0),
                 error=bar.get("error") or "")
            return ""
        rect = bar["rect"]
        _write_clipboard(COPY_SENTINEL)
        started = time.time()
        invoked = False
        detail = ""
        try:
            from core.uia_service import shared_service
            answer = shared_service().act(hwnd, "invoke", ref=bar.get("ref"))
            invoked = bool((answer or {}).get("ok", True))
            detail = str(answer)[:150]
        except Exception as exc:
            detail = "%s: %s" % (type(exc).__name__, exc)
        step("action_bar_invoke", ok=invoked, detail=detail,
             rect=rect, name=bar.get("name") or "")
        if not invoked:
            time.sleep(0.45)
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            rx = (rect["left"] - left) / max(1, right - left)
            ry = (rect["top"] - top) / max(1, bottom - top)
            try:
                self._click(hwnd, rx + 0.01, ry + 0.01)
            except BlockedByWindow:
                step("action_bar_click", ok=False, detail="按钮被其它窗口遮挡")
                return ""
            step("action_bar_click", ok=True, rect=rect)
        copied = ""
        while time.time() - started < 3.0:
            time.sleep(0.05)
            value = _read_clipboard()
            if value and value != COPY_SENTINEL:
                copied = value
                break
        elapsed = int((time.time() - started) * 1000)
        bar["copy"] = {"chars": len(copied), "elapsed_ms": elapsed,
                       "head40": copied[:40], "tail40": copied[-40:],
                       "clipboard_changed": bool(copied)}
        step("action_bar_copy", chars=len(copied), elapsed_ms=elapsed,
             head40=copied[:40], tail40=copied[-40:])
        return copied

    def _copy_via_uia_button(self, hwnd, evidence_dir, step):
        """第一级：点 UIA 结构里的“复制”；没有就直接点“更多”，再从菜单里选“复制”。"""
        targets = self._uia_copy_buttons(hwnd)
        hit = targets.get("copy")
        if hit:
            _write_clipboard(COPY_SENTINEL)
            self._click(hwnd, hit["x"], hit["y"])
            time.sleep(0.8)
            copied = _read_clipboard()
            # 字段名用 target：step 的内部参数叫 event，name 会与旧签名撞车。
            step("copy_uia_button", target=hit["name"], length=len(copied))
            if _clip_changed(copied):
                return copied
        more = targets.get("more")
        if not more:
            return ""
        self._click(hwnd, more["x"], more["y"])
        time.sleep(0.7)
        menu = self._uia_copy_buttons(hwnd)
        item = menu.get("copy")
        step("copy_uia_more", found=bool(item), item=(item or {}).get("name", ""))
        if not item:
            self._send([27])
            time.sleep(0.3)
            return ""
        _write_clipboard(COPY_SENTINEL)
        self._click(hwnd, item["x"], item["y"])
        time.sleep(0.8)
        copied = _read_clipboard()
        step("copy_uia_more_item", length=len(copied))
        return copied if _clip_changed(copied) else ""

    def _copy_via_select_all(self, hwnd, evidence_dir, step):
        """第三级：在消息容器里 Ctrl+A 选中、Ctrl+C 复制。

        前两级都依赖豆包自己的按钮；界面改版/按钮被遮挡时，整页选中复制也能拿到
        正文（多出的界面词由 ``_extract_answer`` 按提示词回显切掉）。
        """
        _write_clipboard(COPY_SENTINEL)
        try:
            self._click(hwnd, 0.55, 0.38)
        except BlockedByWindow:
            step("select_all_copy", ok=False, detail="消息区被遮挡")
            return ""
        time.sleep(0.25)
        self._send([0x11, 0x41])        # Ctrl+A
        time.sleep(0.25)
        self._send([0x11, 0x43])        # Ctrl+C
        time.sleep(0.7)
        copied = _read_clipboard()
        step("select_all_copy", length=len(copied))
        return copied if _clip_changed(copied) else ""

    @staticmethod
    def _document_card_state(words, page_text=""):
        """识别“在线文档卡片 / 内置预览浮层”。

        20261110w 现场：豆包把 18 页正文放进“在线文档卡片”，对话气泡里只剩约 100 字摘要，
        还弹出“暂不支持当前场景打开”。四档复制都只拿到界面文案，最终被误报成 capture_empty。

        只用卡片专属文案做信号：普通对话正文里出现“文档”两个字不算（负例见单测）。
        """
        page = _norm(page_text)
        flat_lines = [_norm(item.get("text")) for item in (words or [])
                      if str(item.get("text") or "").strip()]

        def _hit(marker):
            return marker in page or any(marker in line for line in flat_lines)

        signals = [marker for marker in DOCUMENT_CARD_MARKERS if _hit(marker)]
        weak = [marker for marker in DOCUMENT_CARD_WEAK_MARKERS if _hit(marker)]
        overlay = any(_hit(marker) for marker in DOCUMENT_CARD_OVERLAY_MARKERS)
        action = any(_hit(marker) for marker in ("查看文档", "打开文档", "文档预览", "预览"))
        # “在线文档”单独出现可能只是正文描述，必须同时有打开/预览动作或浮层才算卡片。
        present = bool(signals) or bool(weak and (overlay or action))
        return {"present": present, "signals": signals + weak if present else [],
                "overlay": overlay}

    def _dismiss_card_overlay(self, hwnd, evidence_dir, step):
        """关掉“暂不支持当前场景打开”这类内置预览浮层：先 Esc，再找关闭按钮。"""
        attempts = []
        state = {"overlay": True}
        for _ in range(2):
            try:
                self._send([27])
                attempts.append("esc")
            except Exception as exc:
                attempts.append("esc_error:%s" % str(exc)[:40])
            time.sleep(0.45)
            _frame, words, text = self._capture_words(hwnd, evidence_dir / "09c_card_overlay.png")
            state = self._document_card_state(words, text)
            if not state["overlay"]:
                break
        if state["overlay"]:
            hit = None
            try:
                from core.uia_service import shared_service
                snapshot = shared_service().snapshot(hwnd, limit=240, time_budget=2.0)
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                width, height = max(1, right - left), max(1, bottom - top)
                for element in snapshot.get("elements") or []:
                    name = str(element.get("name") or "").strip()
                    if name not in ("关闭", "×", "Close", "关闭预览", "关闭文档"):
                        continue
                    box = _rect_box(element.get("rect"))
                    if box is None:
                        continue
                    cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
                    if left <= cx <= right and top <= cy <= bottom:
                        hit = ((cx - left) / width, (cy - top) / height)
                        break
            except Exception:
                hit = None
            if hit:
                try:
                    self._click(hwnd, hit[0], hit[1])
                    attempts.append("close_button")
                    time.sleep(0.5)
                except Exception as exc:
                    attempts.append("close_error:%s" % str(exc)[:40])
        _frame, words, text = self._capture_words(hwnd, evidence_dir / "09d_card_after_close.png")
        state = self._document_card_state(words, text)
        closed = not state["overlay"]
        step("document_card_overlay", closed=closed, attempts=attempts)
        return closed, words, text

    def _copy_via_document_card(self, hwnd, evidence_dir, step):
        """点“在线文档卡片”自身的复制入口取正文（优先 UIA，其次 OCR 卡片底部）。"""
        targets = []
        try:
            from core.uia_service import shared_service
            snapshot = shared_service().snapshot(hwnd, limit=300, time_budget=2.5)
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            width, height = max(1, right - left), max(1, bottom - top)
            for element in snapshot.get("elements") or []:
                name = str(element.get("name") or "").strip()
                if name not in DOCUMENT_CARD_COPY_NAMES:
                    continue
                box = _rect_box(element.get("rect"))
                if box is None:
                    continue
                cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
                if not (left <= cx <= right and top <= cy <= bottom):
                    continue
                targets.append(((cx - left) / width, (cy - top) / height, name))
        except Exception:
            targets = []
        targets.sort(key=lambda item: item[1], reverse=True)   # 卡片底部的复制优先
        for rx, ry, name in targets[:4]:
            _write_clipboard(COPY_SENTINEL)
            try:
                self._click(hwnd, rx, ry)
            except BlockedByWindow:
                step("document_card_copy", target=name, ok=False, detail="卡片区域被遮挡")
                continue
            time.sleep(0.9)
            copied = _read_clipboard()
            step("document_card_copy", target=name, length=len(copied))
            if _clip_changed(copied):
                return copied
        # UIA 没暴露卡片复制按钮时，退到 OCR：取卡片最底部一行“复制/复制全文”。
        _, words, _ = self._capture_words(hwnd, evidence_dir / "09e_card_copy.png")
        lines = [line for line in _group_lines(words)
                 if str(line.get("text") or "").strip() in DOCUMENT_CARD_COPY_NAMES
                 or ("复制" in str(line.get("text") or "")
                     and len(str(line.get("text") or "").strip()) <= 8)]
        lines.sort(key=lambda line: float(line.get("y") or 0), reverse=True)
        for line in lines[:3]:
            _write_clipboard(COPY_SENTINEL)
            try:
                self._click_line(hwnd, line)
            except BlockedByWindow:
                continue
            time.sleep(0.9)
            copied = _read_clipboard()
            step("document_card_copy_ocr", length=len(copied))
            if _clip_changed(copied):
                return copied
        return ""

    @staticmethod
    def _answer_region(words, prompt=""):
        """把 OCR 词表限定到“回答块”，返回 region_text / rect / lines。

        20261110v 现场：OCR 扫的是整窗（侧栏、顶栏、输入框、操作行都在），回答只是其中
        一块；拿“复制正文”去和整窗文字比重合率必然偏低（ratio=0.134）。这里先用词坐标
        圈出回答块（去掉顶栏、侧栏、输入框与“复制/重新生成”等操作行，并用提示词回显
        切掉问题气泡），再在这个区域内算覆盖。
        """
        lines = _group_lines(words)
        rows = []
        noise = set(_norm(item) for item in _ANSWER_REGION_NOISE)
        for line in lines:
            text = str(line.get("text") or "").strip()
            if not text:
                continue
            try:
                x0, y0, x1, y1 = _line_box(line)
            except Exception:
                continue
            if y0 < 0.12 or y1 > 0.86:        # 顶栏 / 输入框条带
                continue
            if x0 < 0.18:                      # 左侧会话栏
                continue
            flat = _norm(text)
            if flat in noise:
                continue
            # 操作行（“复制 重新生成 分享”）可能被 OCR 拼成一行：短且命中关键词就丢掉
            if len(flat) <= 14 and any(word in text for word in
                                       ("复制", "重新生成", "分享", "点赞", "下载")):
                continue
            rows.append(dict(text=text, x0=x0, y0=y0, x1=x1, y1=y1))
        tail = _norm(prompt)[-12:]
        if tail:
            for row in list(rows):
                if tail in _norm(row["text"]) and row["y0"] < 0.75:
                    rows = [item for item in rows if item["y0"] > row["y1"] - 0.002]
                    break
        if not rows:
            return None
        region_text = "\n".join(row["text"] for row in rows)
        rect = [round(min(r["x0"] for r in rows), 3), round(min(r["y0"] for r in rows), 3),
                round(max(r["x1"] for r in rows), 3), round(max(r["y1"] for r in rows), 3)]
        return {"text": region_text, "rect": rect, "lines": len(rows)}

    @staticmethod
    def _answer_visible_in_page(answer, page, region_text=None):
        """来源校验：剪贴板正文必须能在同一时刻的窗口 OCR 文本里找到。

        给了 ``region_text``（回答块区域）时按“该区域被复制正文覆盖了多少”算：
        长回答只有最后一屏在视口里，用“正文被页面覆盖”会天然偏低，方向反过来才有意义。
        没给区域时保留旧的整窗口径（短回答直接查整段是否出现）。
        """
        from collections import Counter
        flat_answer = _norm(answer)
        if region_text is not None:
            flat_region = _norm(region_text)
            if not flat_answer or len(flat_region) < 12:
                return {"ratio": 0.0, "windows": 0, "region": "answer_block", "chars": 0}
            if len(flat_region) < 40:
                hit = flat_region in flat_answer
                return {"ratio": 1.0 if hit else 0.0, "windows": 2 if hit else 0,
                        "region": "answer_block", "chars": len(flat_region)}
            pool = Counter(flat_answer)
            matched = 0
            for ch in flat_region:
                if pool[ch] > 0:
                    pool[ch] -= 1
                    matched += 1
            windows = sum(1 for i in range(0, len(flat_region) - 8, 8)
                          if flat_region[i:i + 8] in flat_answer)
            return {"ratio": round(matched / len(flat_region), 3), "windows": windows,
                    "region": "answer_block", "chars": len(flat_region)}
        flat_page = _norm(page)
        if not flat_answer or len(flat_page) < 12:
            return {"ratio": 0.0, "windows": 0, "region": "page", "chars": len(flat_page)}
        if len(flat_answer) < 40:
            # 短回答（一句话提示词等）凑不出两个 8 字窗口，改用“整段能在页面里找到”。
            hit = flat_answer in flat_page
            return {"ratio": 1.0 if hit else 0.0, "windows": 2 if hit else 0,
                    "region": "page", "chars": len(flat_page)}
        pool = Counter(flat_page)
        matched = 0
        for ch in flat_answer:
            if pool[ch] > 0:
                pool[ch] -= 1
                matched += 1
        windows = sum(1 for i in range(0, len(flat_answer) - 8, 8)
                      if flat_answer[i:i + 8] in flat_page)
        return {"ratio": round(matched / len(flat_answer), 3), "windows": windows,
                "region": "page", "chars": len(flat_page)}

    @staticmethod
    def _structure_hits(answer):
        """正文的结构特征（标题/编号/列表/章节/中文序号/加粗），返回命中的种类名。"""
        text = str(answer or "")
        return [name for name, pattern in _STRUCTURE_PATTERNS if pattern.search(text)]

    @classmethod
    def _degrade_reason(cls, method, answer, check):
        """OCR 覆盖不足时的降级放行判定；三条件缺一不可，不满足返回空串。

        条件：① 来源可确认（点到了回答下方“复制”按钮或复制菜单项）；
              ② 长度不是残片（>= DEGRADE_MIN_CHARS）；
              ③ 结构像正文（至少 DEGRADE_MIN_STRUCTURE 类结构特征）。
        """
        if method not in COPY_BUTTON_PATHS:
            return ""
        chars = len(_norm(answer))
        if chars < DEGRADE_MIN_CHARS:
            return ""
        kinds = cls._structure_hits(answer)
        if len(kinds) < DEGRADE_MIN_STRUCTURE:
            return ""
        return ("OCR 覆盖不足降级放行：ratio=%.3f / windows=%d / region=%s，"
                "但复制来源=%s（点了回答下方「复制」按钮并拿到剪贴板变化）、"
                "正文 %d 字、结构命中 %d 类（%s）。"
                % (float(check.get("ratio") or 0), int(check.get("windows") or 0),
                   check.get("region") or "page", method, chars,
                   len(kinds), "、".join(kinds)))

    def _viewport_reason(self, method, answer, check, scroll_state=""):
        """小视口/滚动受限时的放行：复制到完整正文且结构像正文就不再要求“视口可见”。

        20261110x 根因：豆包窗口小，长回答只显示开头，旧逻辑把“视口里看不到”
        当成“内容没生成完”反复重发。正文主路径是整篇复制，视口只用于定位。
        """
        if not (getattr(self, "_small_viewport", False)
                or scroll_state in (SCROLL_STUCK, SCROLL_STILL_MOVING,
                                    SCROLL_WRONG_TARGET)):
            return ""
        if method not in tuple(COPY_BUTTON_PATHS) + ("uia_text",):
            return ""
        chars = len(_norm(answer))
        if chars < DEGRADE_MIN_CHARS:
            return ""
        kinds = self._structure_hits(answer)
        if len(kinds) < DEGRADE_MIN_STRUCTURE:
            return ""
        return ("小视口/滚动受限放行：视口只显示开头（ratio=%.3f / windows=%d / region=%s），"
                "但复制来源=%s、正文 %d 字、结构命中 %d 类（%s），按完整正文接受、不判空。"
                % (float(check.get("ratio") or 0), int(check.get("windows") or 0),
                   check.get("region") or "page", method, chars,
                   len(kinds), "、".join(kinds)))

    @staticmethod
    def _bottom_marker_visible(words, page_text):
        """回答区右下角的“回到底部”悬浮箭头是否还在。

        箭头可能是图标（没有文字），所以这只是一个信号；真正判定还要看
        “再滚一轮页面是否完全不动”（帧差 + 文本长度），两者都不依赖 OCR 词组。
        """
        text = _norm(page_text)
        for marker in ("回到底部", "回到底", "到底部"):
            if marker in text:
                return True
        for line in _group_lines(words):
            flat = _norm(line["text"])
            if "回到底部" in flat or "回到底" in flat:
                return True
        return False

    @staticmethod
    def _frame_signature(frame):
        """把窗口截图缩成 64x64 灰度指纹：滚动位置变化时整块变，闪烁光标几乎不影响。"""
        try:
            return frame.convert("L").resize((64, 64)).tobytes()
        except Exception:
            return b""

    @staticmethod
    def _frame_delta(sig_a, sig_b):
        """两帧指纹的平均灰度差；缺一帧时返回 255（视为完全不同）。"""
        if not sig_a or not sig_b or len(sig_a) != len(sig_b):
            return 255.0
        total = sum(abs(a - b) for a, b in zip(sig_a, sig_b))
        return total / float(len(sig_a))

    def _uia_scroll_state(self, hwnd):
        """UIA ScrollPattern 读数（只读）：拿不到就返回 ``available=False``，由哈希兜底。"""
        try:
            from core.uia_service import shared_service
            payload = shared_service().scroll_state(hwnd)
        except Exception as exc:
            return {"available": False,
                    "error": "%s: %s" % (type(exc).__name__, str(exc)[:80])}
        if not isinstance(payload, dict) or not payload.get("available"):
            return {"available": False, "error": "no_scroll_pattern"}
        return payload

    @classmethod
    def _scroll_targets(cls, hwnd, words=None, page_text=""):
        """滚轮落点：必须先把光标放进回答正文区，再发滚轮。

        20261110y 现场缺陷：滚轮只作用于鼠标光标所在的容器。旧实现把光标放在
        窗口固定点（可能落在侧栏/输入框/窗口外），正文没动，却被当成“已到底/滚不动”。
        这里按回答区矩形算三个落点（正中 / 偏上 1/3 / 偏下 1/3），水平取正文块中心，
        避开右下的“复制/重新生成”操作行与右侧滚动条；只移动光标、不点击，
        所以不会误触链接或按钮。OCR 没圈出正文块时退回窗口右侧中段。
        """
        region = cls._answer_region(words or [], "")
        rect = (region or {}).get("rect")
        source = "answer_region"
        if not rect:
            rect = [0.30, 0.18, 0.92, 0.80]
            source = "fallback"
        x0, y0, x1, y1 = [float(value) for value in rect]
        cx = (x0 + x1) / 2.0
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width, height = max(1, right - left), max(1, bottom - top)
        screen_rect = [int(left + width * x0), int(top + height * y0),
                       int(left + width * x1), int(top + height * y1)]
        points = []
        for tag, ratio in (("center", 0.5), ("upper", 0.34), ("lower", 0.66)):
            ny = y0 + (y1 - y0) * ratio
            points.append({"tag": tag, "source": source, "target_rect": screen_rect,
                           "region": [round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)],
                           "center": [int(left + width * cx), int(top + height * ny)]})
        return points

    def _scroll_to_bottom(self, hwnd, evidence_dir=None, step=None,
                          max_rounds=12, deadline_seconds=35.0, notches=6,
                          words=None):
        """循环滚动到底：按结构判，不按“看得见”；滚轮前先把光标移到正文区。

        每轮先 ``SetCursorPos`` 到回答正文区的落点再发滚轮，滚完校验是否真的动了
        （视口文本哈希变化 或 ScrollPattern 百分比变化）；同一落点连续
        ``SCROLL_POINTER_INVALID_ROUNDS`` 轮无效就换下一个落点，三个落点都无效 →
        ``scroll_wrong_target``（与 at_bottom / scroll_stuck 分开报）。
        结束（含异常）把光标移回操作前坐标，记 ``pointer_restore``。

        判定优先级：
          ① UIA ScrollPattern 的 VerticalScrollPercent 到 100 → 已到底；
          ② 回答区“回到底部”箭头从有到无 → 已到底；
          ③ 已经真的滚动过、且连续 ``SCROLL_STABLE_ROUNDS`` 轮视口文本哈希不变 → 已到底；
             但 UIA 明说没到底、或箭头还在 → 判“滚不动但未到底”（异常）。
        轮数与整体预算都有硬上限，不会无限滚动。
        """
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        x = int(left + (right - left) * 0.55)
        y = int(top + (bottom - top) * 0.5)
        for _ in range(3):
            if self._point_is_target(hwnd, x, y):
                break
            self._raise_window(hwnd)
            time.sleep(0.25)
        else:
            return {"scrolled": False, "rounds": 0, "reason": "window_blocked",
                    "state": "window_blocked", "code": "blocked_by_window",
                    "words": [], "page_text": "", "marker": False, "stable": False}

        user = ctypes.windll.user32
        try:
            pointer_before = [int(value) for value in win32gui.GetCursorPos()]
        except Exception:
            pointer_before = None
        pointer_moved = False

        started = time.time()
        prev_hash = prev_sig = None
        stable_rounds = ineffective = rounds = 0
        saw_marker = ever_effective = False
        words_now, page_text, marker, stable = list(words or []), "", False, False
        state, reason = SCROLL_STILL_MOVING, "budget_exhausted"
        uia = self._uia_scroll_state(hwnd)
        targets = self._scroll_targets(hwnd, words_now, "")
        target_index = 0
        switches = 0
        if step:
            step("scroll_targets", count=len(targets), source=targets[0]["source"],
                 rect=targets[0]["target_rect"], region=targets[0]["region"],
                 points=[point["center"] for point in targets])
        try:
            while rounds < max(1, int(max_rounds)) and (time.time() - started) < deadline_seconds:
                # 滚动前先按 UIA 结构信号直接判到底，避免多余滚动。
                if uia.get("available") and uia.get("at_bottom"):
                    state, reason = SCROLL_AT_BOTTOM, "uia_scroll_pattern"
                    break
                point = targets[min(target_index, len(targets) - 1)]
                px, py = [int(value) for value in point["center"]]
                moved_from = pointer_before if pointer_moved else []
                user.SetCursorPos(px, py)
                pointer_moved = True
                time.sleep(0.1)
                if step:
                    step("scroll_pointer", target_rect=point["target_rect"],
                         center=[px, py], moved_from=moved_from,
                         point=point["tag"], source=point["source"],
                         target_index=target_index)
                if not self._point_is_target(hwnd, px, py):
                    self._raise_window(hwnd)
                    time.sleep(0.2)
                if not self._point_is_target(hwnd, px, py):
                    # 落点被别的窗口压着：不发滚轮（会滚到别人窗口上），按无效记。
                    rounds += 1
                    ineffective += 1
                    if step:
                        step("scroll_pointer_blocked", center=[px, py], round=rounds,
                             ineffective=ineffective)
                    if ineffective >= SCROLL_POINTER_INVALID_ROUNDS:
                        target_index, ineffective = target_index + 1, 0
                        switches += 1
                        if target_index >= len(targets):
                            state, reason = SCROLL_WRONG_TARGET, "all_targets_blocked"
                            break
                        if step:
                            step("scroll_pointer_switch", target_index=target_index,
                                 point=targets[target_index]["tag"],
                                 center=targets[target_index]["center"], switches=switches)
                    continue
                rounds += 1
                # 只在这里取 delta：滚轮方向全仓由 core.computer_control.wheel_delta 决定。
                # 20261110ac 实测：-120 内容上移（看到更后），+120 内容下移（看到更早）。
                for value in wheel_notches("later", notches):
                    user.mouse_event(0x0800, 0, 0, value, 0)
                    time.sleep(0.06)
                time.sleep(0.35)
                path = (evidence_dir / ("09_scroll_%02d.png" % rounds)) if evidence_dir else None
                frame, words_now, page_text = self._capture_words(hwnd, path)
                flat = _norm(page_text)
                sig = self._frame_signature(frame)
                marker = self._bottom_marker_visible(words_now, page_text)
                saw_marker = saw_marker or marker
                text_hash = _sha(flat)
                delta = self._frame_delta(prev_sig, sig) if prev_sig else 0.0
                percent_before = uia.get("percent") if uia.get("available") else None
                # 首轮没有上一帧可比：不计无效，只当基线。
                judged = prev_hash is not None or percent_before is not None
                moved = bool(prev_hash is not None
                             and (text_hash != prev_hash or delta > 2.0))
                stable = bool(prev_hash is not None and text_hash == prev_hash
                              and delta <= 2.0)
                stable_rounds = stable_rounds + 1 if stable else 0
                uia = self._uia_scroll_state(hwnd)
                percent_after = uia.get("percent") if uia.get("available") else None
                if (percent_before is not None and percent_after is not None
                        and percent_after != percent_before):
                    moved = True
                if moved:
                    ever_effective = True
                    ineffective = 0
                elif judged:
                    ineffective += 1
                if step:
                    step("scroll_round", round=rounds, point=point["tag"], marker=marker,
                         stable=stable, stable_rounds=stable_rounds, moved=bool(moved),
                         ineffective=ineffective, hash=text_hash[:16],
                         delta=round(delta, 2), chars=len(flat),
                         uia_available=bool(uia.get("available")),
                         uia_percent=uia.get("percent"),
                         uia_at_bottom=uia.get("at_bottom"))
                prev_hash, prev_sig = text_hash, sig
                if uia.get("available") and uia.get("at_bottom"):
                    state, reason = SCROLL_AT_BOTTOM, "uia_scroll_pattern"
                    break
                if saw_marker and not marker:
                    state, reason = SCROLL_AT_BOTTOM, "marker_gone"
                    break
                if stable_rounds >= SCROLL_STABLE_ROUNDS and ever_effective:
                    if marker:
                        state, reason = SCROLL_STUCK, "marker_still_visible"
                    elif uia.get("available") and not uia.get("at_bottom"):
                        state, reason = SCROLL_STUCK, "stable_but_uia_not_bottom"
                    else:
                        state, reason = SCROLL_AT_BOTTOM, "page_stable"
                    break
                if ineffective >= SCROLL_POINTER_INVALID_ROUNDS:
                    target_index, ineffective = target_index + 1, 0
                    switches += 1
                    if target_index >= len(targets):
                        state, reason = SCROLL_WRONG_TARGET, "all_targets_ineffective"
                        break
                    if step:
                        step("scroll_pointer_switch", target_index=target_index,
                             point=targets[target_index]["tag"],
                             center=targets[target_index]["center"], switches=switches)
            else:
                if rounds == 0:
                    state, reason = SCROLL_STUCK, "no_round"
                elif stable_rounds >= SCROLL_STABLE_ROUNDS and ever_effective:
                    if marker:
                        state, reason = SCROLL_STUCK, "marker_still_visible"
                    elif uia.get("available") and not uia.get("at_bottom"):
                        state, reason = SCROLL_STUCK, "stable_but_uia_not_bottom"
                    else:
                        state, reason = SCROLL_AT_BOTTOM, "page_stable"
                else:
                    state, reason = SCROLL_STILL_MOVING, "budget_exhausted"
        finally:
            if pointer_before and pointer_moved:
                try:
                    user.SetCursorPos(int(pointer_before[0]), int(pointer_before[1]))
                except Exception:
                    pass
                if step:
                    try:
                        pointer_after = [int(value) for value in win32gui.GetCursorPos()]
                    except Exception:
                        pointer_after = None
                    step("pointer_restore", before=pointer_before, after=pointer_after,
                         restored=bool(pointer_after == pointer_before))
        return {"scrolled": True, "rounds": rounds, "reason": reason, "state": state,
                "words": words_now, "page_text": page_text,
                "marker": marker, "stable": bool(stable_rounds >= SCROLL_STABLE_ROUNDS),
                "pointer": {"before": pointer_before,
                            "targets": [point["center"] for point in targets],
                            "switches": switches},
                "uia": {key: uia.get(key) for key in ("available", "percent", "view",
                                                      "at_bottom", "container", "error")
                        if key in uia},
                "elapsed_ms": int((time.time() - started) * 1000)}


    def _capture_answer_uia(self, hwnd, prompt, evidence_dir, step):
        """OCR 不可用时的取正文：先走操作栏复制（不依赖 OCR），再退 UIA 页面文本。"""
        self._last_action_bar = {}
        copied = ""
        try:
            copied = self._copy_via_action_bar(hwnd, evidence_dir, step)
        except Exception as exc:
            step("action_bar_error", detail="%s: %s" % (type(exc).__name__, exc))
        if copied and len(_norm(copied)) >= 20:
            answer = self._extract_answer(
                copied, prompt, chips=(self._last_action_bar or {}).get("chips"))
            step("capture_uia_text", chars=len(_norm(copied)),
                 answer_chars=len(_norm(answer)), mode="action_bar")
            for name, payload in (("10_uia_page.txt", copied), ("11_answer.txt", answer)):
                try:
                    with open(evidence_dir / name, "w", encoding="utf-8") as fh:
                        fh.write(payload or "")
                except Exception:
                    pass
            return {"ok": True, "answer": answer, "clip_sha256": _sha(copied),
                    "prompt_echo_found": _norm(prompt)[:8] in _norm(copied),
                    "copied_from": "action_bar",
                    "visible_in_page": {"ratio": 1.0, "windows": 1},
                    "action_bar": self._last_action_bar, "mode": "action_bar"}
        page = self._uia_text(hwnd)
        answer = self._extract_answer(page, prompt)
        echo = bool(_norm(prompt)[:8]) and _norm(prompt)[:8] in _norm(page)
        step("capture_uia_text", chars=len(_norm(page)), answer_chars=len(_norm(answer)),
             prompt_echo=echo, mode="uia_text")
        for name, payload in (("10_uia_page.txt", page), ("11_answer.txt", answer)):
            try:
                with open(evidence_dir / name, "w", encoding="utf-8") as fh:
                    fh.write(payload or "")
            except Exception:
                pass
        if answer and len(_norm(answer)) >= 20:
            return {"ok": True, "answer": answer, "clip_sha256": _sha(page),
                    "prompt_echo_found": echo, "copied_from": "uia_text",
                    "visible_in_page": {"ratio": 1.0, "windows": 1}, "mode": "uia_text"}
        error = getattr(self, "_ocr_error", None) or {}
        return {"ok": False, "code": "ocr_unavailable",
                "hint": ("Windows OCR 不可用（%s），UIA 读到的页面文本里也没凑出完整回答。"
                         "缺什么：%s。去哪补：%s。下一步：按路径补装语言包并重启咕嘎；"
                         "若语言包已装，请确认豆包窗口停在这一轮回答上再重试。"
                         % (error.get("missing") or "OCR 引擎不可用",
                            error.get("missing") or "Windows OCR 语言包",
                            error.get("where") or "设置 → 应用 → 可选功能"))}

    @staticmethod
    def _leftover_capture_threads():
        """仍在跑的取正文工作线程数：挂住不返回的档会留在这里，便于发现线程泄漏。"""
        current = threading.current_thread()
        return sum(1 for item in threading.enumerate()
                   if item is not current and item.is_alive()
                   and item.name.startswith("doubao-capture-"))

    def _run_capture_path(self, name, func, args, budget, index=0, total=0):
        """在 daemon 线程里跑一档复制路径，最多等 budget 秒。

        返回 ``(info, text)``：info 直接进 step 时间线（path/started/elapsed/result），
        text 为这一档拿到的正文。超时/异常都只影响这一档，绝不阻塞调用方。
        """
        budget = max(0.0, float(budget))
        started = _now_iso()
        base = dict(path=name, index=index, total=total, budget=round(budget, 2))
        if budget <= 0:
            base.update(ok=False, chars=0, started=started, elapsed=0.0,
                        result="skipped", detail="整体超时，未进入该档")
            return base, ""
        box = {}

        def runner():
            # 线程启动时显式注入主线程建好的同一个快照盒实例（普通 dict + 锁，跨线程共享）。
            _CLIP_THREAD.box = _clip_session_box()
            try:
                box["text"] = func(*args) or ""
            except BlockedByWindow:
                box["blocked"] = True
            except BaseException as exc:      # 单档异常只丢这一档，不打断整条取正文
                box["error"] = "%s: %s" % (type(exc).__name__, exc)
            finally:
                # 恢复必须在真正动过剪贴板的这个线程里做：谁写谁恢复，不依赖主线程。
                _restore_session_clipboard(_CLIP_THREAD.box, only_owner=True)
                _CLIP_THREAD.box = None

        worker = threading.Thread(target=runner, name="doubao-capture-%s" % name,
                                  daemon=True)
        begin = time.time()
        worker.start()
        worker.join(budget)
        elapsed = round(time.time() - begin, 2)
        if worker.is_alive():
            base.update(ok=False, chars=0, started=started, elapsed=elapsed,
                        result="timeout",
                        detail="单档超时 %.1fs，已放弃该档并进入下一档" % budget)
            return base, ""
        text = str(box.get("text") or "")
        if box.get("blocked"):
            base.update(result="blocked", detail="窗口被遮挡")
        elif box.get("error"):
            base.update(result="error", detail=str(box.get("error")))
        else:
            base.update(result="ok" if text else "empty", detail="")
        base.update(ok=bool(text), chars=len(text), started=started, elapsed=elapsed)
        return base, text

    def _capture_answer(self, hwnd, prompt, evidence_dir, step):
        """取正文入口：一旦动过剪贴板，无论成败都在 finally 里恢复用户原内容。

        快照盒跨线程共享：真正写剪贴板的是 ``doubao-capture-*`` 工作线程，恢复由该线程
        在自己的 ``finally`` 里完成；主线程这里只做兜底（写入线程没来得及恢复时），
        并且 ``restore_ok`` 一律来自恢复后的实测格式集合比对，不再靠 ``tried`` 猜。
        """
        box = _clip_session_begin()
        try:
            result = self._capture_answer_locked(hwnd, prompt, evidence_dir, step)
        finally:
            restore = _restore_session_clipboard(box, only_owner=False)
            if restore is None:
                restore = dict(restore_ok=False, attempts=0, leftover_formats=[],
                               missing_formats=[], skipped=False,
                               error="复制线程未完成恢复，剪贴板可能仍是本轮内容")
            _clip_session_end(box)
            step("clipboard_restore", restore_ok=bool(restore["restore_ok"]),
                 attempts=int(restore["attempts"]),
                 leftover_formats=restore.get("leftover_formats") or [],
                 missing_formats=restore.get("missing_formats") or [],
                 skipped=bool(restore.get("skipped")),
                 restored_in=restore.get("restored_in") or "",
                 error=restore.get("error") or "")
            if not restore["restore_ok"]:
                step("clipboard_restore_warning", restore_ok=False,
                     leftover_formats=restore.get("leftover_formats") or [],
                     missing_formats=restore.get("missing_formats") or [],
                     error=restore.get("error") or "")
        result["clipboard_restore"] = restore
        if not restore["restore_ok"]:
            result["clipboard_restore_warning"] = dict(
                restore_ok=False,
                leftover_formats=restore.get("leftover_formats") or [],
                missing_formats=restore.get("missing_formats") or [],
                attempts=int(restore["attempts"]),
                error=restore.get("error") or "")
        return result

    def _capture_answer_locked(self, hwnd, prompt, evidence_dir, step):
        self._last_action_bar = {}
        if getattr(self, "_ocr_unavailable", False):
            return self._capture_answer_uia(hwnd, prompt, evidence_dir, step)
        _, before_words, before_text = self._capture_words(hwnd, evidence_dir / "09_before_copy.png")
        scroll = self._scroll_to_bottom(hwnd, evidence_dir, step, words=before_words)
        words = scroll.get("words") or []
        after_text = scroll.get("page_text") or ""
        page_text = "\n".join(value for value in (before_text, after_text) if value)
        step("scroll_bottom", scrolled=scroll.get("scrolled"), rounds=scroll.get("rounds"),
             reason=scroll.get("reason"), state=scroll.get("state"),
             marker=scroll.get("marker"), stable=scroll.get("stable"),
             small_viewport=bool(getattr(self, "_small_viewport", False)),
             chars=len(_norm(page_text)))
        scroll_state = str(scroll.get("state") or "")
        page = _norm(page_text)
        region = self._answer_region(words, prompt)
        step("answer_region", rect=(region or {}).get("rect"),
             lines=(region or {}).get("lines", 0),
             chars=len(_norm((region or {}).get("text") or "")),
             mode="answer_block" if region else "page")

        # 产出形态识别：豆包可能把正文放进“在线文档卡片”，对话里只剩摘要。
        # 先关掉“暂不支持当前场景打开”浮层，再把卡片自身的复制入口排到第一档。
        card = self._document_card_state(words, page_text)
        step("document_card", present=card["present"], signals=card["signals"],
             overlay=card["overlay"])
        if card["overlay"]:
            closed, fresh_words, fresh_text = self._dismiss_card_overlay(
                hwnd, evidence_dir, step)
            if closed and fresh_words:
                words = fresh_words
                after_text = fresh_text or after_text
                page_text = "\n".join(value for value in (before_text, after_text) if value)
                page = _norm(page_text)
                region = self._answer_region(words, prompt)

        # 三级兜底：UIA 结构按钮 -> OCR 锚点（复制按钮 / 更多菜单）-> 消息容器内 Ctrl+A/Ctrl+C。
        # 每一档都跑在 daemon 工作线程里并带 join(timeout)：挂住只丢这一档，不拖死整条链路。
        # 识别到文档卡片时，最前面加一档“点卡片自身的复制”，避免卡片正文取不到。
        paths = []
        candidates = []
        plan = []
        if card["present"]:
            plan.append(("document_card", self._copy_via_document_card,
                         (hwnd, evidence_dir, step)))
        plan.extend((
            ("action_bar", self._copy_via_action_bar, (hwnd, evidence_dir, step)),
            ("uia_button", self._copy_via_uia_button, (hwnd, evidence_dir, step)),
            ("ocr_anchor", self._copy_via_answer_button, (hwnd, words, evidence_dir, step)),
            ("ocr_more_menu", self._copy_via_more_menu, (hwnd, words, evidence_dir, step)),
            ("select_all", self._copy_via_select_all, (hwnd, evidence_dir, step)),
        ))
        total = float(CAPTURE_TOTAL_TIMEOUT)
        deadline = time.time() + total
        timeouts = 0
        skipped = 0
        last_timeout = (0, "")
        for index, (name, func, args) in enumerate(plan, start=1):
            remaining = deadline - time.time()
            if remaining < 0.05:          # 整体预算耗尽：不再开新档，直接记 skipped
                info = dict(path=name, index=index, total=len(plan), ok=False, chars=0,
                            started=_now_iso(), elapsed=0.0, budget=0.0,
                            result="skipped", detail="整体超时，未进入该档")
                text = ""
            else:
                budget = min(float(CAPTURE_PATH_TIMEOUT.get(name, 20.0)), remaining)
                info, text = self._run_capture_path(name, func, args, budget,
                                                    index=index, total=len(plan))
            step("copy_path", **info)
            paths.append({key: info[key] for key in
                          ("path", "index", "ok", "chars", "result", "started",
                           "elapsed", "budget", "detail") if key in info})
            if info.get("result") == "timeout":
                timeouts += 1
                last_timeout = (index, name)
            elif info.get("result") == "skipped":
                skipped += 1
            if text:
                candidates.append((name, text))
                break
        last_path = paths[-1]["path"] if paths else ""
        last_index = int(paths[-1]["index"]) if paths else 0

        checks = []
        bar = getattr(self, "_last_action_bar", {}) or {}
        chips = bar.get("chips") or []
        page_key = _key(page_text)
        for method, text in candidates:
            answer = self._extract_answer(text, prompt, chips=chips)
            check = self._answer_visible_in_page(
                answer, page, (region or {}).get("text"))
            visible = check["ratio"] >= 0.6 or check["windows"] >= 2
            degrade_reason = "" if visible else self._degrade_reason(method, answer, check)
            if not degrade_reason:
                degrade_reason = self._viewport_reason(
                    method, answer, check, scroll_state=scroll_state)
            if degrade_reason:
                visible = True
            head_ok = bool(_key(answer)[:20]) and _key(answer)[:20] in page_key
            tail_ok = bool(_key(answer)[-20:]) and _key(answer)[-20:] in page_key
            checks.append({"path": method, "chars": len(_norm(answer)),
                           "ratio": check["ratio"], "windows": check["windows"],
                           "region": check.get("region") or "page",
                           "visible_in_page": visible,
                           "head20_in_page": head_ok, "tail20_in_page": tail_ok,
                           "degrade_reason": degrade_reason})
            step("capture_verify", method=method, chars=len(_norm(answer)),
                 ratio=check["ratio"], windows=check["windows"],
                 region=check.get("region") or "page",
                 visible_in_page=visible, degrade_reason=degrade_reason,
                 head20_in_page=head_ok, tail20_in_page=tail_ok,
                 sha=_sha(text)[:16])
            if not visible:
                continue
            with open(evidence_dir / "10_clipboard.txt", "w", encoding="utf-8") as fh:
                fh.write(text)
            with open(evidence_dir / "11_answer.txt", "w", encoding="utf-8") as fh:
                fh.write(answer)
            return {"ok": True, "answer": answer, "clip_sha256": _sha(text),
                    "prompt_echo_found": _norm(prompt)[:8] in _norm(text),
                    "copied_from": method, "visible_in_page": check,
                    "answer_region": region, "action_bar": bar,
                    "copy_paths": paths, "copy_checks": checks, "scroll": scroll,
                    "capture_timeouts": timeouts,
                    "leftover_threads": self._leftover_capture_threads()}

        last_copy = candidates[-1][1] if candidates else ""
        with open(evidence_dir / "10_clipboard_failed.txt", "w", encoding="utf-8") as fh:
            fh.write(last_copy or "")
        if card["present"]:
            # 识别到文档卡片且没取到正文：这是产出形态问题，不是 OCR/模型问题，
            # 必须给独立失败码，不能让上层误以为“回复没复制出来”去反复重取。
            detail = ("卡片里是正文，对话气泡只有摘要；卡片自身复制入口没有取到正文。"
                      if not candidates else
                      "卡片里是正文，对话气泡只有摘要；取到的 %d 字只是摘要或不完整。"
                      % len(_norm(candidates[-1][1])))
            missing = ("豆包把正文放进了“在线文档卡片”，%s（卡片信号：%s）"
                       % (detail, "、".join(card["signals"])))
            return {"ok": False, "code": "reply_in_card",
                    "missing": missing, "document_card": card,
                    "action_bar": bar, "copy_paths": paths, "copy_checks": checks,
                    "scroll": scroll, "capture_timeouts": timeouts,
                    "leftover_threads": self._leftover_capture_threads(),
                    "hint": ("豆包把正文放进了文档卡片，需在对话内输出或手动复制。缺什么：%s。"
                             "下一步：在豆包对话里明确要求“直接在对话里用纯文本输出全文”，"
                             "或手动点开卡片复制正文后再派单。"
                             "这不是 OCR 问题，也不是模型问题。" % missing)}
        if not candidates and bar.get("present"):
            # 操作栏找到了（说明回答已生成完），但复制按钮没产出可用文本：
            # 与“没有回复/没生成完”严格区分，单独成码，不再落 capture_empty。
            copy_info = bar.get("copy") or {}
            missing = ("操作栏已出现（候选 %d 排，选中 rect=%s，按钮 %d 个），"
                       "但复制按钮没有给出可用文本（复制 %d 字，耗时 %s ms，"
                       "首 40=%r，尾 40=%r）"
                       % (int(bar.get("candidates") or 0), bar.get("rect"),
                          len(bar.get("buttons") or []), int(copy_info.get("chars") or 0),
                          copy_info.get("elapsed_ms"), copy_info.get("head40") or "",
                          copy_info.get("tail40") or ""))
            return {"ok": False, "code": "reply_unreadable", "missing": missing,
                    "action_bar": bar, "copy_paths": paths, "copy_checks": checks,
                    "scroll": scroll, "capture_timeouts": timeouts,
                    "leftover_threads": self._leftover_capture_threads(),
                    "hint": ("豆包这一条回答的操作栏已出现，但复制没拿到正文。缺什么：%s。"
                             "下一步：只重试读取这条回答的复制按钮（不会重复下发），"
                             "连续两次仍为空就停在这里报 reply_unreadable。"
                             % missing)}
        if (not candidates and "present" in bar and not bar.get("present")
                and scroll_state not in (SCROLL_STUCK, SCROLL_STILL_MOVING,
                                         SCROLL_WRONG_TARGET)):
            # 最新一条回答下方没有操作栏：回答可能还在生成、或界面形态变了。
            missing = ("最新一条回答下方没有找到操作栏（24×24 同排 Button ≥%d 个，"
                       "位于正文末行下方 0~60px；候选 %d 排）"
                       % (ACTION_BAR_MIN_BUTTONS, int(bar.get("candidates") or 0)))
            return {"ok": False, "code": "no_action_bar", "missing": missing,
                    "action_bar": bar, "copy_paths": paths, "copy_checks": checks,
                    "scroll": scroll, "capture_timeouts": timeouts,
                    "leftover_threads": self._leftover_capture_threads(),
                    "hint": ("没有在豆包最新回答下方找到操作栏（回复可能还在生成，"
                             "或界面形态与探测时不同）。缺什么：%s。"
                             "下一步：先确认“停止生成”消失，再重试读取；"
                             "不会重复下发同一条消息。" % missing)}
        if not candidates and scroll_state in (SCROLL_STUCK, SCROLL_STILL_MOVING,
                                               SCROLL_WRONG_TARGET):
            # 滚动状态与“没复制到正文”分开报：滚不动/还在滚/滚轮没落到正文区不是同一类
            # 问题，上层据此只重试滚动或重试读取，不会再重新下发同一句话。
            wrong = scroll_state == SCROLL_WRONG_TARGET
            stuck = scroll_state == SCROLL_STUCK
            code = ("scroll_wrong_target" if wrong else
                    "scroll_stuck" if stuck else "scroll_incomplete")
            label = ("滚轮没落到回答正文区（三个正文落点都没带动内容）" if wrong else
                     "滚不动但未到底" if stuck else "仍在滚动、预算已用尽")
            missing = ("回答区%s（滚动 %d 轮，原因 %s，UIA=%s，视口最后 %d 字）；"
                       "复制路径没有取到正文，不能当成 capture_empty。"
                       % (label, int(scroll.get("rounds") or 0), scroll.get("reason") or "?",
                          "可用" if (scroll.get("uia") or {}).get("available") else "不可用",
                          len(page)))
            return {"ok": False, "code": code, "missing": missing,
                    "scroll": scroll, "copy_paths": paths, "copy_checks": checks,
                    "capture_timeouts": timeouts,
                    "leftover_threads": self._leftover_capture_threads(),
                    "hint": ("豆包有回复但没取到正文，且滚动状态异常（%s）。缺什么：%s。"
                             "下一步：只重试滚动/读取（不会重复下发），或在豆包窗口手动滚到"
                             "本轮回答末尾后重试。这不是 OCR 问题，也不是模型问题。"
                             % (label, missing))}
        if not candidates:
            if timeouts or skipped:
                reason = ("三条复制路径均超时" if not skipped else
                          "三条复制路径均未取到正文（%d 档单档超时、%d 档整体超时后未进入）"
                          % (timeouts, skipped))
                missing = ("%s，最后一条是第 %d 档 %s"
                           "（整体上限 %.0fs；每档 started/elapsed/result 见 copy_path 记录）"
                           % (reason, last_index, last_path, total))
            else:
                missing = ("正文复制锚点：UIA 结构里没找到回答区末尾的「复制/更多」按钮，"
                           "OCR 词表里也没有「免」或「今 HH:MM」，"
                           "消息容器内 Ctrl+A/Ctrl+C 也没取到剪贴板文字")
        else:
            worst = max(checks or [{"chars": 0, "ratio": 0.0, "windows": 0}],
                        key=lambda item: item["chars"])
            missing = ("能与回答区 OCR 对上的正文（已复制到 %d 字，但字符重合率 %.2f、"
                       "连续 8 字命中 %d、区域=%s，低于放行线；"
                       "复制来源不满足降级放行条件）"
                       % (int(worst["chars"]), float(worst["ratio"]),
                          int(worst["windows"]), worst.get("region") or "page"))
            if timeouts:
                missing += ("；另有 %d 档复制超时（最后一条是第 %d 档 %s）"
                            % (timeouts, last_timeout[0], last_timeout[1]))
        extra = ""
        if timeouts:
            extra = ("超时的那一档不会再继续点界面（已放弃），可先在豆包窗口手动确认正文完整，"
                     "再点重试；若仍超时，最后一条是第 %d 档 %s。" % (last_timeout[0], last_timeout[1]))
        return {"ok": False, "code": "capture_empty", "missing": missing,
                "copy_paths": paths, "copy_checks": checks, "scroll": scroll,
                "capture_timeouts": timeouts,
                "leftover_threads": self._leftover_capture_threads(),
                "hint": ("豆包有回复，但没能取到可核对的正文。缺什么：%s。%s"
                         "下一步：在豆包窗口滚到这一轮回答末尾，点回答下方的「复制」按钮一次，"
                         "确认剪贴板里真的有正文后再派单重试。" % (missing, extra))}

    def _extract_answer(self, copied, prompt, chips=None):
        text = (copied or "").replace("\r\n", "\n").replace("\r", "\n")
        target = _norm(prompt)
        # 用提示词的回显做切点：优先用提示词最后 12 个非空白字符
        tail = target[-12:]
        flat = _norm(text)
        index = flat.find(tail)
        if index >= 0:
            # 把扁平位置映射回原文位置
            count = 0
            for pos, char in enumerate(text):
                if not char.isspace():
                    if count == index + len(tail):
                        text = text[pos:]
                        break
                    count += 1
        noise = ("复制", "重新生成", "重新回答", "继续生成", "停止生成", "点赞", "踩", "分享",
                 "内容由 AI 生成", "内容由AI生成", "以上内容由AI生成", "收起", "展开", "编辑",
                 "删除", "更多", "下载", "发送", "新对话", "对话", "工作", "豆包")
        chip_keys = {_key(item) for item in (chips or []) if _key(item)}
        lines = []
        for raw in text.split("\n"):
            line = raw.strip()
            if not line:
                continue
            if line in noise or any(line == n for n in noise):
                continue
            # 建议追问条（单行 18px、固定左边界、间距 46px）不并入正文。
            if chip_keys and _key(line) in chip_keys:
                continue
            # 回答下方的推荐问题/第二轮提问：正文已经收够就到此为止，
            # 避免把点击推荐问题触发的第二次生成混进结果。
            if len("".join(lines)) >= 40 and re.match(
                    r"^(以《|在作文中|再写|换成|继续|帮我写|写一篇|加一些|润色|修改)", line):
                break
            lines.append(line)
        cleaned = "\n".join(lines).strip()
        # 去掉首尾残留的界面词
        cleaned = re.sub(r"^(复制|重新生成|重新回答)\s*", "", cleaned)
        cleaned = re.sub(r"\s*(复制|重新生成|重新回答|内容由\s*AI\s*生成)\s*$", "", cleaned)
        return cleaned or (copied or "").strip()

    # -- 结果兜底 ---------------------------------------------------------
    def _done_setting(self, key, default):
        value = self._decl("anchors", "done", key, default=default)
        return default if value in (None, "") else value

    def _hint_for(self, code, attempt):
        if code == "ocr_unavailable":
            error = getattr(self, "_ocr_error", None) or {}
            return ("Windows OCR 不可用（%s）。缺什么：%s。去哪补：%s（补完重启咕嘎，不用重启电脑）。"
                    "下一步：%s。在此之前豆包派单会继续走 UIA + 剪贴板降级，"
                    "截图问答里的文字识别不可用；点“重试”可再走一遍降级流程。"
                    % (error.get("kind") or "engine_unavailable",
                       error.get("missing") or "Windows OCR 语言包",
                       error.get("where") or "设置 → 应用 → 可选功能 → 添加“中文(简体) 光学字符识别”",
                       error.get("next_action") or "按上面的路径装好后重启咕嘎"))
        for item in self._decl("anchors", "failure_codes", default=[]) or []:
            if item.get("code") == code:
                return item.get("hint")
        mapping = {
            "wrong_skill": ("这次要求被豆包当成技能/生图执行了（输入框挂着技能标签，页面出现"
                            "“正在生成图片/幻灯片”）。缺什么：普通对话态。去哪补：豆包输入框里"
                            "点掉标签后的 ×，切回普通对话。下一步：重新派单。"
                            "这不是 OCR 问题，也不是模型问题，已中止且不会重试。"),
            "skill_chip_present": ("豆包输入框挂着技能标签且没移除成功，本次一个字都没发送。"
                                   "缺什么：普通对话态。去哪补：手动点掉标签后的 ×（或点“对话”）。"
                                   "下一步：切回普通对话后重新派单。"
                                   "这不是 OCR 问题，也不是模型问题。"),
            "not_installed": "没装豆包：先安装豆包桌面端并登录，再派单。",
            "launch_failed": "装了豆包但没能打开窗口：手动启动一次，确认能打开并登录后再派。",
            "window_not_found": "没找到豆包窗口，确认豆包桌面端已安装并可正常打开。",
            "window_blank": "豆包窗口白屏，按 Ctrl+R 或重启豆包后再试。",
            "input_not_found": "没找到输入框，请把豆包切到“对话”页并手动点一下输入框。",
            "readback_mismatch": "粘贴后回读不一致，通常是焦点没在输入框；点一下输入框再重试。",
            "login_required": self._login_hint(),
            "blocked_by_dialog": "豆包窗口被弹窗挡住，请关闭弹窗后重试。",
            "blocked_by_window": "豆包窗口被别的窗口压着，自动抬起失败；关掉遮挡的窗口后重试。",
            "capture_empty": "回复没复制出来，点一下回答正文后重试。",
            "scroll_stuck": ("回答区滚不动但没到末尾（UIA 明说没到底，或“回到底部”箭头还在）："
                             "缺什么：滚动到底后的完整正文。下一步：把豆包窗口放大/手动滚到本轮"
                             "回答末尾后重试；只会重试滚动/读取，不会重复下发。"
                             "这不是 OCR 问题，也不是模型问题。"),
            "scroll_incomplete": ("滚动预算/轮数用尽时页面仍在变化：长回答还没滚完。"
                                  "下一步：把豆包窗口放大后重试；只会重试滚动/读取，不会重复下发。"
                                  "这不是 OCR 问题，也不是模型问题。"),
            "scroll_wrong_target": ("滚轮没带动回答正文：滚轮只作用于光标所在容器，"
                                    "这次三个正文落点（正中/偏上/偏下）都没让内容动。"
                                    "缺什么：滚动到底后的完整正文。下一步：把豆包窗口放大、"
                                    "在正文上手动滚一下后重试；只会重试滚动/读取，不会重复下发。"
                                    "这不是 OCR 问题，也不是模型问题。"),
            "answer_in_document_card": ("豆包把正文放进了在线文档卡片，对话里只有摘要。"
                                        "缺什么：对话内可直接复制的纯文本全文。"
                                        "下一步：要求豆包“直接在对话里用纯文本输出全文”，"
                                        "或手动点开卡片复制正文后再派单。"
                                        "这不是 OCR 问题，也不是模型问题。"),
            "ocr_empty": ("OCR 引擎可用但识别结果是 0 行 / 0 字（不是适配器异常）："
                          "确认豆包窗口停在对话页且未最小化，按 Ctrl+R 刷新后重派；"
                          "仍为空请到「设置 → 自检」重跑“系统 OCR”并导出诊断。"),
            "timeout": "等待豆包回复超时，检查网络后重试。",
            "send_not_confirmed": ("文字已经打进去，但回车/点发送后没有出现新回合："
                                   "多半是页面没在对话页或有弹窗挡住。到豆包窗口点一下输入框手动发一次；"
                                   "仍不行就先重启豆包再派单。"),
            "target_not_focused": ("注入前没确认到豆包输入框的键盘焦点（窗口不在前台或控件没拿到焦点）："
                                   "一个键都没有发，文本不会被写到别的窗口。把豆包窗口切到前台后重派；"
                                   "这不是 OCR 问题，也不是模型问题。"),
            "composer_not_found": ("按同一个 UIA 控件回读不到豆包输入框（控件失效/页面结构变了）："
                                   "没有拿前台窗口或整窗 OCR 文本充数，也没有按回车。"
                                   "把豆包窗口切到前台后重派；仍失败请重启豆包。"),
            "input_written_elsewhere": ("回读到的不是豆包输入框里的内容（与本机别处文本高度相似，"
                                        "或前台已不是豆包）：文本被写到了别的窗口或控件。"
                                        "缺什么：豆包输入框里的本步文本。下一步：把豆包窗口切到前台后重派；"
                                        "这次没有重复下发，也没有按回车。"),
            TARGET_WINDOW_CODE: ("豆包窗口本身不可用（白屏/渲染异常、大部分在屏幕外，或停在非对话页）："
                                 "这不是 OCR 或适配器异常。请重启豆包后再派单。"),
        }
        return (attempt or {}).get("hint") or mapping.get(code) or "豆包调用失败，请重试。"

    def _fail(self, result, code, hint, evidence_dir):
        result.update(code=code, hint=hint, elapsed_ms=0)
        self._dump_log(evidence_dir, result)
        return result

    def _dump_log(self, evidence_dir, payload):
        try:
            evidence_dir.mkdir(parents=True, exist_ok=True)
            with open(evidence_dir / "result.json", "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
        except Exception:
            pass


def ask_doubao(prompt, **kwargs):
    """给调度层的一行式入口（不修改 tool_router，由调用方按需接入）。"""
    return DoubaoAdapter().ask(prompt, **kwargs)


def _main(argv):
    import argparse

    parser = argparse.ArgumentParser(description="豆包适配器自测")
    parser.add_argument("--probe", action="store_true", help="只探测窗口/登录/MCP 状态")
    parser.add_argument("--prompt", default="", help="要发给豆包的任务")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--no-mcp", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    adapter = DoubaoAdapter()
    if args.probe or not args.prompt:
        payload = adapter.probe()
    else:
        payload = adapter.ask(args.prompt, timeout_seconds=args.timeout, prefer_mcp=not args.no_mcp)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") or args.probe else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
