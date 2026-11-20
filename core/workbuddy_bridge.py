"""WorkBuddy 无 API 桥：真实 UI 发需求 + 判定回答结束 + 全选复制回读全文。

前台约束：启动前若用户 2.5 秒内有键鼠输入，直接返回 foreground_busy，不抢焦点；
结束后恢复原前台窗口与鼠标位置。
"""
import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import zipfile
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import win32clipboard as clip
import win32con

ROOT = Path(__file__).resolve().parents[1]
from core import guga_paths
from core.failure_report import failure_payload, next_action_for
from core.paste_verify import ensure_plain_mode, sent_mode_check, chip_label
from core.paste_verify import input_verified  # noqa: F401  测试 test_paste_mode_guard 会 patch 本模块这个名字

# “已处理”是完成后的耗时标签（如“已处理 3m47s”），不是生成中状态；
# 把它算作 busy 会导致回答永远不判定结束，最后按超时收尾。
BUSY = ('深度思考中', '正在生成', '生成中', '正在思考', '停止生成', '正在调用',
        '等待模型响应', '正在上色', '正在读取', '正在执行', '正在解析')
# PPT 步骤的提问会被折叠成末段/被编辑器替换，不能按“页面看不到提问”直接判成别的会话。
_PPT_PROMPT_RE = re.compile(r'PPT|幻灯片|演示文稿|\.pptx', re.I)
EXE_CANDIDATES = (
    Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'WorkBuddy' / 'WorkBuddy.exe',
)


def _window():
    sys.path.insert(0, str(ROOT))
    from core.window_inventory import list_windows
    item = None
    for it in list_windows('workbuddy'):
        # 必须确认进程名是 WorkBuddy.exe——Edge 等浏览器窗口标题里可能含
        # "WorkBuddy" 字样（如 "WorkBuddy - 个人中心 - Microsoft Edge"），
        # matching_windows 在进程枚举为空时会 fallback 到 title 匹配，
        # 这里用进程名二次过滤彻底杜绝误匹配。
        pname = str(it.get('process_name') or '').casefold()
        if 'workbuddy' not in pname:
            continue
        # 窗口尺寸也得够大——WorkBuddy 主窗口最小也 >800×500
        rect = it.get('rect') or ()
        if len(rect) >= 4:
            ww, hh = rect[2] - rect[0], rect[3] - rect[1]
            if ww < 400 or hh < 300:
                continue
        item = it
        break
    if item:
        hwnd = int(item.get('hwnd') or 0)
        if hwnd:
            # WorkBuddy 有两个臭毛病：
            #   1) 主窗口藏起来（vis=0 iconic=0），调用方以为没窗口又去 _launch，
            #      但 launch 里用了 single_instance=true，反而更乱；
            #   2) 窗口缩小到任务栏图标大小（约 360×270），OCR/UIA 坐标全打空。
            # 所以这里除了把它拉出来，还必须保证窗口有一个可读的最小尺寸。
            try:
                import win32gui
                user32 = ctypes.windll.user32
                kernel32 = ctypes.windll.kernel32

                vis = win32gui.IsWindowVisible(hwnd)
                iconic = win32gui.IsIconic(hwnd)

                # 1. 抢前台（AttachThreadInput 让 SetForegroundWindow 生效）
                current_fg = win32gui.GetForegroundWindow()
                fg_tid = user32.GetWindowThreadProcessId(current_fg, None)
                my_tid = kernel32.GetCurrentThreadId()
                if fg_tid and my_tid and fg_tid != my_tid:
                    user32.AttachThreadInput(fg_tid, my_tid, True)

                # 2. 先恢复 + 最大化（SW_MAXIMIZE 比 SW_RESTORE 更能把缩在任务栏的窗口拉出来）
                win32gui.ShowWindow(hwnd, 9)   # SW_RESTORE
                win32gui.ShowWindow(hwnd, 3)   # SW_MAXIMIZE
                time.sleep(0.6)

                # 3. 前台抢占
                try:
                    win32gui.BringWindowToTop(hwnd)
                    win32gui.SetForegroundWindow(hwnd)
                except Exception:
                    pass

                # 4. 解绑线程
                if fg_tid and my_tid and fg_tid != my_tid:
                    user32.AttachThreadInput(fg_tid, my_tid, False)

                # 5. 验证尺寸：最大化后仍小于 600×400，强制 MoveWindow 到屏幕 75% 大小
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                ww, hh = right - left, bottom - top
                if ww < 600 or hh < 400:
                    screen_w = user32.GetSystemMetrics(0)
                    screen_h = user32.GetSystemMetrics(1)
                    target_w = int(screen_w * 0.75)
                    target_h = int(screen_h * 0.75)
                    new_left = max(0, int((screen_w - target_w) / 2))
                    new_top = max(0, int((screen_h - target_h) / 2))
                    win32gui.MoveWindow(hwnd, new_left, new_top, target_w, target_h, True)
                    time.sleep(0.4)
                    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                    ww, hh = right - left, bottom - top

                # 6. 再验证一下可见性和尺寸，写进 item 供调用方诊断
                item['_ww'] = ww
                item['_hh'] = hh
                item['_vis'] = vis
                item['_iconic'] = iconic
            except Exception:
                pass
    return item


def _resolved_exe():
    """启动路径优先级：用户指定 > MCP 自报 > 内置常见路径。

    与 app_registry 的探测顺序一致；自报路径失效时它会给出 warnings，
    这里忽略并继续走内置候选，保证一条脏记录不会让 WorkBuddy 再也起不来。
    """
    try:
        from core.adapters import app_registry
        found = app_registry.launch_exe('workbuddy')
        if found.get('ok'):
            return str(found['exe'])
    except Exception:
        pass
    return next((str(path) for path in EXE_CANDIDATES if path.is_file()), '')


def _launch(timeout=45):
    exe = _resolved_exe()
    if not exe:
        raise RuntimeError('找不到 WorkBuddy.exe')
    subprocess.Popen([str(exe)], creationflags=0x00000008 | 0x00000200)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        item = _window()
        if item:
            return item
        time.sleep(1.0)
    raise RuntimeError('WorkBuddy 启动后 %.0f 秒仍没有窗口' % timeout)


def _idle_ms():
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.UINT), ('dwTime', wintypes.DWORD)]
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
    return ctypes.windll.kernel32.GetTickCount() - info.dwTime


def _rect(hwnd):
    box = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(box))
    return box.left, box.top, box.right, box.bottom


def _click_ratio(hwnd, rx, ry):
    left, top, right, bottom = _rect(hwnd)
    point = (int(left + (right - left) * rx), int(top + (bottom - top) * ry))
    user = ctypes.windll.user32
    user.SetCursorPos(*point)
    time.sleep(0.1)
    user.mouse_event(2, 0, 0, 0, 0)
    user.mouse_event(4, 0, 0, 0, 0)
    time.sleep(0.22)


def _ocr_words(hwnd, region=None):
    """整窗 OCR，返回窗口比例坐标；region=(x,y,w,h) 用于只在侧栏等局部找字。"""
    from core.quick_look import grab_window
    from core.desktop_ocr_native import recognize
    frame=grab_window(hwnd)
    frame.thumbnail((1600,1100))
    width,height=frame.size
    receipt=recognize(frame)
    if not receipt.get('ok'):
        raise RuntimeError(receipt.get('message') or 'WorkBuddy 页面文字读取失败')
    words=[dict(text=word['text'],x=word['x']/width,y=word['y']/height,
                w=word['w']/width,h=word['h']/height)
           for line in receipt['data']['lines'] for word in line['words']]
    if region:
        x, y, w, h = region
        words = [item for item in words
                 if x <= float(item.get('x', 0)) <= x + w and y <= float(item.get('y', 0)) <= y + h]
    return words


def _ocr_lines(words):
    """把 OCR 单字按 y 归行再拼字符串，才能匹配 '.pptx'、'新建任务' 这类多字锚点。"""
    lines = []
    for word in sorted(words or [], key=lambda w: (round(float(w['y']), 3), float(w['x']))):
        y = float(word['y'])
        for line in lines:
            if abs(line['y'] - y) <= max(0.008, float(word.get('h', 0.02)) * 0.7):
                line['words'].append(word)
                break
        else:
            lines.append({'y': y, 'words': [word]})
    for line in lines:
        line['words'].sort(key=lambda w: float(w['x']))
        line['text'] = ''.join(str(w.get('text') or '') for w in line['words'])
    return lines


def _line_box(line):
    x0 = min(float(w['x']) for w in line['words'])
    x1 = max(float(w['x']) + float(w['w']) for w in line['words'])
    h = max(float(w.get('h', 0.02)) for w in line['words'])
    return x0, line['y'], x1, line['y'] + h


def _click_text(hwnd, token, region=None, timeout=3.0):
    """按 OCR 找到含 token 的词并点它中心；窗口尺寸变化不影响，点不到返回 False。

    OCR 常把中文按钮拆成单字（如"新建任务"拆成"新""建""任""务"），
    需要把同一行内 x 相邻的 word 按顺序拼接后再匹配 token。
    """
    norm_token = re.sub(r'\W', '', str(token or ''))
    if not norm_token:
        return False
    deadline = time.monotonic() + max(0, timeout)
    while time.monotonic() < deadline:
        words = _ocr_words(hwnd, region)
        # 单 word 直接命中
        target = next((item for item in words
                       if norm_token in re.sub(r'\W', '', str(item.get('text') or ''))), None)
        if target is None and len(norm_token) > 1:
            # 同行相邻 word 拼接后命中：按 y 聚行，按 x 排序后拼接
            rows = {}
            for item in words:
                y_key = round(float(item.get('y', 0)), 2)
                rows.setdefault(y_key, []).append(item)
            for row in rows.values():
                row.sort(key=lambda w: float(w.get('x', 0)))
                text = ''.join(re.sub(r'\W', '', str(w.get('text') or '')) for w in row)
                if norm_token in text:
                    # 找到 token 起始位置对应的 word，点击该词中心
                    idx = text.find(norm_token)
                    cum = 0
                    for w in row:
                        w_text = re.sub(r'\W', '', str(w.get('text') or ''))
                        if cum <= idx < cum + len(w_text):
                            target = w
                            break
                        cum += len(w_text)
                    if target is None and row:
                        target = row[len(row) // 2]
                    break
        if target:
            left, top, right, bottom = _rect(hwnd)
            point = (int(left + (target['x'] + target['w'] / 2) * (right - left)),
                     int(top + (target['y'] + target['h'] / 2) * (bottom - top)))
            user = ctypes.windll.user32
            user.SetCursorPos(*point)
            time.sleep(0.1)
            user.mouse_event(2, 0, 0, 0, 0)
            user.mouse_event(4, 0, 0, 0, 0)
            time.sleep(0.35)
            return True
        time.sleep(0.6)
    return False


def _read_clip():
    for _ in range(6):
        try:
            clip.OpenClipboard()
            try:
                return clip.GetClipboardData(win32con.CF_UNICODETEXT) or ''
            finally:
                clip.CloseClipboard()
        except Exception:
            time.sleep(0.12)
    return ''


def _grab(hwnd):
    from core.computer_control import _send_keys
    _click_ratio(hwnd, 0.620, 0.240)
    _send_keys([17, 65])
    time.sleep(0.2)
    _send_keys([17, 67])
    time.sleep(0.55)
    return _read_clip()


def _copy_page(hwnd):
    """不点正文，直接全选复制当前页面文字。

    刚从产物页切回对话页时，(0.62,0.24) 很可能正压在对话顶部的产物卡片上，
    点一下就会把 PPT 编辑页重新打开；所以这种场景只用键盘复制，不点正文。
    """
    from core.computer_control import _send_keys
    _send_keys([17, 65])
    time.sleep(0.2)
    _send_keys([17, 67])
    time.sleep(0.55)
    return _read_clip()


def _paste(hwnd, prompt, cancel=None):
    """整段写入当前输入框；不点击窗口，依赖输入框本来就有焦点。返回是否发出粘贴。"""
    from core.computer_control import ComputerControl
    sent = ComputerControl._paste_multiline(prompt, hwnd, cancel or threading.Event())
    time.sleep(0.6)
    return sent


def _clear_composer():
    """焦点在输入框时清空残留文字；焦点在正文时这两下无副作用。"""
    from core.computer_control import _send_keys
    _send_keys([17, 65])
    time.sleep(0.15)
    _send_keys([46])


def _click_composer(hwnd):
    """把焦点打进输入框：直接点击干净页中央偏下的输入框位置 (0.50, 0.60)。

    **V5.9.3 干净页布局**：Ctrl+N 后页面是 Canvas 自绘，布局是
    标题"WorkBuddy, 我帮你" → 标签(办公/开发/设计) → 预填 prompt 输入框。
    点击 (0.50, 0.60) 正好命中预填 prompt 输入框。

    **简化原则**：probe 脚本（tools/_probe_wb_v2.py）验证过——
    Ctrl+N → 直接点击 (0.50, 0.60) → 清空 → 粘贴 → Enter → **立即 BUSY**。
    不做任何多余的 Esc、Tab 等键盘操作——那些反而会把焦点搞乱。
    """
    # 只做点击，不做键盘操作——干净利落把焦点打进输入框
    _click_ratio(hwnd, 0.50, 0.60)
    time.sleep(0.4)
    return False


def _norm_text(text):
    # 回读必须剔除不可见字符：WorkBuddy 的 ValuePattern 会在正文里插 \ufeff，
    # 不剔除时“粘贴成功”会被判定成回读不一致，于是永远不按回车。
    value = re.sub(r'[\ufeff\u200b\u200c\u200d\u00ad]', '', str(text or ''))
    return re.sub(r'\s+', '', value)


def _artifact_sig():
    info = _artifact_progress()
    return (info.get('pptx'), info.get('pptx_size'), info.get('slide_files'))


def _clear_clip():
    """先清空剪贴板：输入框为空时 Ctrl+C 不写剪贴板，否则会读到上一次的旧内容。"""
    for _ in range(6):
        try:
            clip.OpenClipboard()
            try:
                clip.EmptyClipboard()
            finally:
                clip.CloseClipboard()
            return True
        except Exception:
            time.sleep(0.12)
    return False


def _read_composer(hwnd):
    """点回输入框并全选复制，读回输入框当前内容（空输入框返回空串）。"""
    from core.computer_control import _send_keys
    _click_composer(hwnd)
    _clear_clip()
    _send_keys([17, 65])
    time.sleep(0.18)
    _send_keys([17, 67])
    time.sleep(0.5)
    return _read_clip()


# ---- UIA 直控 composer -------------------------------------------------------
# WorkBuddy 是 Electron/Chromium：点占位文字常常只把焦点给到正文（Ctrl+A 选中整页），
# 但 Chromium 会把 contenteditable 暴露成 Edit，ValuePattern 可读可写。
# 实测：SetFocus() 抛“目标元素无法接收焦点”，点该控件矩形中心则 HasKeyboardFocus=true。
_UIA = {'backend': None, 'error': '', 'last': ''}
_SENTINEL = '__GUGA__'


def _uia_safe_call(func, timeout=2.0):
    """用守护线程跑 UIA 调用，主线程在 timeout 后返回 None 继续走 OCR fallback。

    WorkBuddy 是 Chromium canvas 自绘，UIA FindFirst/FromPoint 偶尔永久挂起
    （COM 调用等不到响应）——普通 timeout 保护只能装饰，因为 deadline 检查
    都在 COM 调用返回之后才执行。这里用 daemon 线程包一层，主线程直接超时
    放弃，daemon 线程随 Python 主进程退出。
    """
    result = [None]
    ok = [False]
    exc = [None]
    def worker():
        try:
            result[0] = func()
            ok[0] = True
        except Exception as e:
            exc[0] = e
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        _UIA['last'] = 'uia_timeout_after_%.1fs' % timeout
        return None
    if exc[0]:
        _UIA['last'] = '%s: %s' % (type(exc[0]).__name__, str(exc[0])[:100])
        return None
    return result[0]


def _uia():
    """进程内复用 UIAutomation 后端：程序集只加载一次，之后 FindFirst 约 2~3 秒。"""
    if _UIA['backend'] is not None:
        return _UIA['backend']
    if _UIA['error']:
        return None
    try:
        from core.uia_backend import UIABackend
        _UIA['backend'] = UIABackend()
    except Exception as exc:
        _UIA['error'] = '%s: %s' % (type(exc).__name__, str(exc)[:120])
        return None
    return _UIA['backend']


def _uia_composer(hwnd, timeout=8.0):
    """定位 composer 控件；返回 dict(backend, hwnd, ref, rect, focused) 或 None。

    优先用 UIA FromPoint 在窗口下半区做网格采样：工作区里可能有多个 Edit
    （会话内联编辑、搜索框），FindFirst 拿到的第一个不一定是 composer；
    但 composer 是唯一“又宽又高”的 Edit，按面积就能认出来。
    采样拿不到时退回 FindFirst。
    """
    # 快速短路：Chromium/CEF 自绘窗口（Chrome_WidgetWin_1）的 UIA 树里
    # 要么没有 Edit 控件（WorkBuddy），要么 FindFirst 永久挂起。
    # 这类窗口直接走 OCR fallback，不浪费时间碰 COM。
    try:
        import win32gui as _wg
        cls = _wg.GetClassName(int(hwnd))
        if cls == 'Chrome_WidgetWin_1':
            _UIA['last'] = 'chromium_window_skip'
            return None
    except Exception:
        pass
    backend = _uia()
    if backend is None:
        _UIA['last'] = _UIA['error'] or 'uia_unavailable'
        return None
    deadline = time.monotonic() + max(1.0, float(timeout))
    last = ''
    while time.monotonic() < deadline:
        try:
            found = _uia_composer_at_point(backend, hwnd)
        except Exception as exc:
            found = None
            last = 'from_point: %s' % (str(exc)[:100])
        if found:
            _UIA['last'] = ''
            _log('uia_composer', {'result': 'from_point', 'ref': found['ref']})
            return dict(backend=backend, hwnd=int(hwnd), ref=found['ref'],
                        rect=found['rect'], focused=bool(found['element'].get('focused')))
        try:
            found = backend.first_edit(int(hwnd), timeout=2.0)
            rect = found.get('rect') or {}
            width = float(rect.get('right', 0)) - float(rect.get('left', 0))
            height = float(rect.get('bottom', 0)) - float(rect.get('top', 0))
            fl, ft, fr, fb = _rect(hwnd)
            fww, fhh = fr - fl, fb - ft
            area_ratio = (width * height) / (fww * fhh) if fww and fhh else 0
            elem = found.get('element') or {}
            kf = elem.get('keyboard_focusable')
            if width >= 240 and height >= 28 and area_ratio < 0.60 and kf:
                _UIA['last'] = ''
                _log('uia_composer', {'result': 'first_edit', 'ref': found['ref'], 'area_ratio': area_ratio})
                return dict(backend=backend, hwnd=int(hwnd), ref=found['ref'], rect=rect,
                            focused=bool(elem.get('focused')))
            elif width >= 240 and height >= 28:
                last = 'Edit 面积占比 %.2f 或不可聚焦，跳过（不是 composer）' % area_ratio
            else:
                last = '找到的 Edit 太小（%.0fx%.0f），不是 composer' % (width, height)
        except Exception as exc:
            last = '%s: %s' % (type(exc).__name__, str(exc)[:120])
        time.sleep(0.3)
    _UIA['last'] = last or 'input_not_found'
    _log('uia_composer', {'result': None, 'last': _UIA['last']})
    return None


def _uia_point_class():
    if not _UIA.get('point'):
        from core.uia_backend import UIABackend  # noqa: F401  确保程序集已加载
        from System.Windows import Point
        _UIA['point'] = Point
    return _UIA['point']


#: Chromium contenteditable 在 UIA 树里可能暴露成 Edit/Document/Pane/Group/Text 等多种类型；
#: 只要面积达标、有 TextPattern 或 keyboard_focusable，都有可能是 composer。
_UIA_COMPOSER_TYPES = frozenset(('Edit', 'Document', 'Pane', 'Group', 'Text', 'Custom'))


def _is_composer_candidate(item, left, top, right, bottom):
    """从 snapshot 元素 dict 判断是否像 composer：位置 + 面积 + 可编辑特征。

    **关键约束**：面积不能超过窗口的 60%——Chromium canvas 自绘会暴露整窗大小的
    Document/Pane/Custom 控件，它们不是 composer。
    """
    rect = item.get('rect') or {}
    rl = float(rect.get('left', 0))
    rt = float(rect.get('top', 0))
    rr = float(rect.get('right', 0))
    rb = float(rect.get('bottom', 0))
    w, h = rr - rl, rb - rt
    if w < 240 or h < 28:
        return False
    ww = right - left
    hh = bottom - top
    if ww > 0 and (rl - left) / ww < 0.30:
        return False
    if ww > 0 and w / ww > 0.92:       # 宽度占 92%+ → 整窗，排除
        return False
    if hh > 0 and h / hh > 0.80:       # 高度占 80%+ → 整窗，排除
        return False
    if (not item.get('text_pattern')
            and not item.get('keyboard_focusable')
            and 'value' not in item
            and 'SetValuePattern' not in (item.get('patterns') or [])):
        return False
    return True


def _uia_composer_at_point(backend, hwnd):
    """网格采样 + 整树 snapshot 两路找 composer。

    第一路：FromPoint 网格采样，接受 Chromium 常见的多种控件类型。
    第二路（fallback）：FromPoint 全 miss 时用整树 snapshot，扫所有元素里面积达标 +
    可编辑特征的最大那个。
    """
    Point = _uia_point_class()
    left, top, right, bottom = _rect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None
    AE = backend.AutomationElement
    walker = backend.walker

    def _from_element(element):
        try:
            kind_full = str(element.Current.ControlType.ProgrammaticName)
            kind = kind_full.replace('ControlType.', '')
            box = element.Current.BoundingRectangle
            w, h = float(box.Width), float(box.Height)
        except Exception:
            return None
        if kind not in _UIA_COMPOSER_TYPES:
            return None
        if w < 240 or h < 28:
            return None
        # rect 必须在窗口可视区域内
        win_w = right - left
        win_h = bottom - top
        area_ratio = (w * h) / (win_w * win_h) if win_w and win_h else 0
        if area_ratio >= 0.60:
            return None
        if w > win_w * 0.95:
            return None
        # 必须有可编辑特征：键盘可聚焦 或 有 TextPattern
        try:
            kf = element.Current.IsKeyboardFocusable
        except Exception:
            kf = False
        if not kf:
            return None
        item, _ = backend._entry(element, hwnd)
        if item:
            backend.elements[item['ref']] = (element, time.monotonic(), int(hwnd))
            _log('uia_from_point_hit', {'type': kind, 'w': int(w), 'h': int(h),
                                        'area_ratio': round(area_ratio, 2),
                                        'rect': item.get('rect'),
                                        'kf': kf})
            return dict(ref=item['ref'], rect=item.get('rect') or {}, element=item)
        return None

    # ---- 第一路：FromPoint 网格采样，接受扩展类型 ----
    for yi in range(13, -1, -1):
        y = int(top + height * (0.30 + 0.05 * yi))
        for xf in (0.58, 0.72, 0.86):
            x = int(left + width * xf)
            try:
                element = AE.FromPoint(Point(x, y))
            except Exception:
                continue
            for _ in range(6):
                if element is None:
                    break
                hit = _from_element(element)
                if hit:
                    return hit
                try:
                    element = walker.GetParent(element)
                except Exception:
                    break

    # ---- 第二路：整树 snapshot fallback ----
    try:
        snap = backend.snapshot(int(hwnd), limit=400, time_budget=3.0)
    except Exception:
        return None
    if not snap or not snap.get('elements'):
        return None
    candidates = []
    for item in snap['elements']:
        if not _is_composer_candidate(item, left, top, right, bottom):
            continue
        if item.get('offscreen'):
            continue
        rect = item.get('rect') or {}
        area = (float(rect.get('right', 0)) - float(rect.get('left', 0))) *                (float(rect.get('bottom', 0)) - float(rect.get('top', 0)))
        candidates.append((area, item))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    _, best = candidates[0]
    ref = best.get('ref')
    if ref and ref in backend.elements:
        return dict(ref=ref, rect=best.get('rect') or {}, element=best)
    try:
        again = backend._find(int(hwnd), ref=ref) if ref else None
    except Exception:
        again = None
    if again:
        return dict(ref=ref, rect=again.get('rect') or {}, element=again)
    return None

def _uia_focus(hwnd, timeout=6.0):
    """点 composer 第一行文字把键盘焦点打进编辑器，并用 HasKeyboardFocus 校验。

    点的是控件真实矩形里的文字行（top+16），不是矩形中心：composer 高 80px 左右，
    文字只占顶部一行，点下半截的空白常常只把焦点给到外层容器。
    """
    bridge = _uia_safe_call(lambda: _uia_composer(hwnd, timeout=4.0), timeout=5.0)
    if bridge is None:
        return None
    rect = bridge['rect']
    point = (int(float(rect['left']) + 40), int(float(rect['top']) + 16))
    user = ctypes.windll.user32
    user.SetCursorPos(*point)
    time.sleep(0.12)
    user.mouse_event(2, 0, 0, 0, 0)
    user.mouse_event(4, 0, 0, 0, 0)
    deadline = time.monotonic() + max(0.8, float(timeout))
    while time.monotonic() < deadline:
        time.sleep(0.15)
        try:
            item = bridge['backend'].read(bridge['hwnd'], ref=bridge['ref'])['element']
        except Exception:
            again = _uia_composer(hwnd, timeout=2.0)
            if again is None:
                return None
            bridge = again
            continue
        if item.get('focused'):
            bridge['how'] = 'click'
            return bridge
    _UIA['last'] = 'composer 点击后仍没有键盘焦点'
    return None


def _uia_value(bridge):
    try:
        return bridge['backend'].value(bridge['hwnd'], bridge['ref'])
    except Exception:
        return None


def _composer_blank(text):
    """空输入框时 UIA 读到的是占位文字（“今天帮你做些什么？…”），按空白处理。"""
    norm = _norm_text(text)
    return (not norm) or (len(norm) < 60 and ('今天帮你' in norm or '做些什么' in norm))


def _composer_state(hwnd, target, bridge=None):
    """empty / holds_prompt / focus_elsewhere / unknown。"""
    text = _uia_value(bridge) if bridge is not None else None
    if text is None and bridge is not None:
        again = _uia_composer(hwnd, timeout=3.0)      # 引用失效（页面重渲染）
        if again is not None:
            text = _uia_value(again)
    if text is None:
        text = _read_composer(hwnd)
    norm = _norm_text(text)
    if _composer_blank(text):
        return 'empty'
    if norm == target:
        return 'holds_prompt'
    if len(norm) > max(60, len(target) * 2):
        return 'focus_elsewhere'      # 复制到了整页正文，说明焦点不在输入框
    return 'unknown'


def _press_paste(hwnd, cancel=None):
    """只按 Ctrl+V：剪贴板由 core.paste_verify 统一写入并负责恢复。"""
    import win32gui
    from core.computer_control import _send_keys
    for _ in range(3):
        if cancel is not None and cancel.is_set():
            return False
        if win32gui.GetForegroundWindow() == hwnd:
            break
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        time.sleep(0.35)
    if win32gui.GetForegroundWindow() != hwnd:
        return False
    _send_keys([17, 86])
    time.sleep(0.6)
    return True


def _log(event, payload=None):
    """提交过程日志（结构性字段，不记用户正文）：runtime/workbuddy_submit.log。

    之前这里的回调引用了未定义的 _log，调用时抛 NameError 又被上层吞掉，
    等于提交日志一直是空的；现在补上落盘实现。
    """
    try:
        folder = Path(guga_paths.RUNTIME)
        folder.mkdir(parents=True, exist_ok=True)
        row = dict(time=time.strftime('%Y-%m-%d %H:%M:%S'), event=str(event))
        if isinstance(payload, dict):
            row.update(payload)
        with open(folder / 'workbuddy_submit.log', 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')
    except Exception:
        pass


class _ComposerDriver:
    """WorkBuddy 输入框驱动：清空 / 粘贴 / 回读，交给 core.paste_verify 统一重试。"""

    def __init__(self, hwnd, bridge, cancel):
        self.hwnd = hwnd
        self.bridge = bridge
        self.cancel = cancel

    def clear(self):
        from core.computer_control import _send_keys
        # bridge=None 说明在 OCR fallback 路径——先点回输入框再清空，
        # 不然 Ctrl+A 会选中整页而不是输入框内容。
        if not self.bridge:
            _click_composer(self.hwnd)
            time.sleep(0.3)
        _send_keys([17, 65])
        time.sleep(0.15)
        _send_keys([46])
        time.sleep(0.2)

    def write(self, text):
        _press_paste(self.hwnd, self.cancel)

    def read(self):
        if self.bridge:
            value = _uia_value(self.bridge)
            if value is None:
                fresh = _uia_composer(self.hwnd, timeout=3.0)
                value = _uia_value(fresh) if fresh else ''
            return value or ''
        return _read_composer(self.hwnd)

    # ---- 技能/模式探测（判定在 core.paste_verify，本适配器只提供探针） ----
    def mode(self):
        """WorkBuddy 输入框若被挂了「标签 + ×」的技能 chip，就先别发。

        WorkBuddy 没有豆包那种常驻技能栏，只有真的挂上 chip 才会出现 ×，
        所以按“标签紧跟 ×”认，不会把普通文字误判成技能态。
        """
        text = ''
        try:
            text = self.read() or ''
        except Exception:
            text = ''
        label = chip_label(text)
        if label:
            return dict(plain=False, label=label)
        return dict(plain=True, label='')

    def clear_mode(self, label=''):
        # WorkBuddy 的 chip 在 Chromium 内部，没有稳定的可点句柄：宁可中止也不盲点。
        raise RuntimeError('WorkBuddy 输入框里的技能标签「%s」无法自动移除' % (label or '?'))


def _submit(hwnd, prompt, cancel, attempts=3, prior_growing=False):
    """提交期间全程保护用户剪贴板（页面复制也会占用剪贴板）。"""
    from core.paste_verify import ClipboardGuard
    guard = ClipboardGuard().save()
    try:
        return _submit_locked(hwnd, prompt, cancel, attempts=attempts,
                              prior_growing=prior_growing)
    finally:
        guard.restore()


def _submit_locked(hwnd, prompt, cancel, attempts=3, prior_growing=False):
    """提交必须四步都成立：焦点在输入框 → 哨兵/回读一致 → 回车 → 输入框清空且有生成信号。

    WorkBuddy 是 Electron/Chromium：坐标点占位文字常常只把焦点给到正文（Ctrl+A 选中整页），
    而 ValuePattern.SetValue 又不会触发应用自己的 input 事件（发送键一直禁用）。
    所以组合成：UIA 定位控件并用它的真实矩形点第一行文字 → 真实粘贴写哨兵 → UIA 回读确认
    → 选中覆盖成真实提问 → 回车。UIA 不可用时退回坐标点击 + 剪贴板 + 回读。
    失败码区分 input_not_found / paste_failed / input_not_cleared。
    """
    from core.computer_control import _send_keys
    target = _norm_text(prompt)
    meta = dict(attempts=0, focus='', sentinel='', readback_ok=False, readback='',
                cleared='', busy_seen=False, artifact_changed=False, page_changed=False,
                prior_growing=prior_growing, uia_error='')
    before = _artifact_sig()

    # === Chromium canvas 自绘（WorkBuddy V5.9.3）：直接走 probe 精确流程 ===
    # 不能先跑 _copy_page（Ctrl+A 高亮会留痕干扰 Enter），也不能跑 for 循环里的
    # _uia_focus（虽然立即 None，但 recover 后又 _copy_page 一次）
    try:
        import win32gui as _wg
        _is_chromium = (_wg.GetClassName(int(hwnd)) == 'Chrome_WidgetWin_1')
    except Exception:
        _is_chromium = False

    if _is_chromium:
        meta['uia_error'] = 'chromium_direct_path'
        meta['focus'] = 'clipboard'
        meta['attempts'] = 1
        _log('submit_stage', {'stage': 'chromium_path_start', 'prompt_len': len(prompt)})
        # === 完全照搬 probe 脚本 (tools/_probe_wb_v2.py) 精确流程 ===
        # 已验证：Ctrl+N→2.5s→点击(0.50,0.60)→清空→粘贴→Ctrl+End→Enter → t=3s BUSY
        # 任何多余操作（Esc×2、额外等、ClipboardGuard vs 直接写剪贴板）都可能干扰
        # 1. Ctrl+N 干净页 + 等 2.5s（probe 精确）
        steps = _recover_new_task(hwnd)
        if steps: meta['recovered'] = steps
        _log('submit_stage', {'stage': 'chromium_after_recover', 'steps': steps})
        # _recover_new_task 里已经等了 2.5s，不再额外等
        rows=_ocr_lines(_ocr_words(hwnd,region=(.20,.18,.75,.62)))
        prompt_rows=[row for row in rows if '帮你做些' in _norm_text(row['text'])]
        if len(prompt_rows)!=1 or _wg.GetForegroundWindow()!=hwnd:
            raise RuntimeError('没看到当前 WorkBuddy 新任务的输入框，尚未提交任务')
        left,top,right,bottom=_line_box(prompt_rows[0])
        _click_ratio(hwnd,left+min(.025,(right-left)/4),(top+bottom)/2)
        time.sleep(.3)
        meta['blank_composer_observed'] = True
        # 4. 剪贴板写 prompt → Ctrl+V（probe 用直接 OpenClipboard）
        try:
            import win32clipboard as _clip
            import win32con as _wc
            _clip.OpenClipboard(); _clip.EmptyClipboard()
            _clip.SetClipboardData(_wc.CF_UNICODETEXT, prompt)
            _clip.CloseClipboard()
        except Exception:
            try: _clip.CloseClipboard()
            except Exception: pass  # 尽力关闭剪贴板；关闭失败不应打断回退到 ClipboardGuard
            # 回退到 ClipboardGuard
            from core.paste_verify import ClipboardGuard
            _guard = ClipboardGuard(); _guard.save()
            _guard.write_text(prompt); _guard.restore()
        time.sleep(0.2)
        if _wg.GetForegroundWindow()!=hwnd or cancel is not None and cancel.is_set():
            raise RuntimeError('WorkBuddy 未保持前台，尚未粘贴提示词')
        _send_keys([17, 86]); time.sleep(1.0)
        if _wg.GetForegroundWindow()!=hwnd or cancel is not None and cancel.is_set():
            raise RuntimeError('WorkBuddy 未保持前台，尚未提交任务')
        if not _clear_clip():
            raise RuntimeError('WorkBuddy 输入回读不可用，尚未提交任务')
        _send_keys([17, 65]); time.sleep(.18)
        _send_keys([17, 67]); time.sleep(.4)
        if _norm_text(_read_clip())!=target or _wg.GetForegroundWindow()!=hwnd:
            raise RuntimeError('WorkBuddy 没有读回完整提示词，尚未提交任务')
        meta['readback_ok'] = True
        meta['input_records'] = [{'mode': 'probe_exact_replica'}]
        # 5. Ctrl+End → Enter（probe 精确）
        _send_keys([17, 35]); time.sleep(0.1)
        if _wg.GetForegroundWindow()!=hwnd or cancel is not None and cancel.is_set():
            raise RuntimeError('WorkBuddy 未保持前台，尚未提交任务')
        sent_at = time.perf_counter()
        sent_wall = time.time()
        meta['sent_wall'] = sent_wall
        _log('submit_stage', {'stage': 'chromium_before_enter'})
        _send_keys([13])
        _log('submit_stage', {'stage': 'chromium_after_enter'})
        # 6. Enter 后轮询（最多 60 秒）
        deadline = time.perf_counter() + 60.0
        page = ''
        prompt_acknowledged = False
        while time.perf_counter() < deadline:
            if cancel is not None and cancel.is_set():
                return page,sent_at,meta
            time.sleep(1.0)
            page = _grab(hwnd)
            # BUSY 词检测（最可靠）
            if any(word in page for word in BUSY):
                meta['busy_seen'] = True
                return page, sent_at, meta
            # 产物变化（pptx 落盘）
            now = _artifact_sig()
            if now != before and now and now[0] is not None:
                meta['artifact_changed'] = True
                return page, sent_at, meta
            # prompt 出现在页面 = 消息发出（WorkBuddy 可能没 BUSY 就中断/快答）
            if not prompt_acknowledged and page and target in _norm_text(page):
                prompt_acknowledged = True
                meta['page_changed'] = True
                # 等 5 秒看看会不会出现 BUSY/artifact
            if prompt_acknowledged and (time.perf_counter() - sent_at) > 10.0:
                # prompt 已确认发出，等了 10 秒还没 BUSY/artifact——提交成功但没生成
                return page, sent_at, meta
        return page or '', sent_at, meta
    # === 非 Chromium（豆包桌面版）：原来的 for 循环 ===
    for attempt in range(1, attempts + 1):
        meta['attempts'] = attempt
        bridge = _uia_focus(hwnd)
        if not bridge and attempt == 1:
            # 停在上一单的 PPT 编辑页/旧任务页时没有输入框：先自动切回新建任务页再重定位。
            steps = _recover_new_task(hwnd)
            if steps:
                meta['recovered'] = steps
                time.sleep(0.5)
                bridge = _uia_focus(hwnd)
                base_page = _norm_text(_copy_page(hwnd))
        if bridge:
            meta['focus'] = 'uia'
            driver = _ComposerDriver(hwnd, bridge, cancel)
            # 输入前模式检查（共用层）：挂着技能标签时先移除并回读确认，移不掉就一个字都不发。
            mode = ensure_plain_mode(driver, log=lambda tag, payload: _log(tag, payload))
            meta['mode'] = mode.get('mode')
            if not mode['ok']:
                meta['error'] = mode['code']
                meta['missing'] = mode['missing']
                return '', time.perf_counter(), meta
            # UIA 只负责定位/回读；正文必须走真实粘贴，因为 ValuePattern.SetValue
            # 不会触发应用自己的 input 事件（发送按钮一直禁用、回车也不提交），
            # 而 SetValue('') 清空会丢光标，所以清空改用“选中后删除”。
            # 清空 → 真实粘贴 → 回读 → 不一致重试统一交给共用输入层
            # （core.paste_verify.input_verified），口径与豆包、JoyCode 一致：
            # 不再自己写哨兵/判回读（哨兵法只认“哨兵在不在”，半截粘贴会被当成成功）。
            # 回读不一致时 input_verified 内部已重试 attempts 次，这里按它的失败码收口，
            # 不再二次重试（wrong_skill 路径必须只按一次回车）。
            verify = input_verified(prompt, driver, attempts=attempts,
                                    log=lambda tag, payload: _log(tag, payload))
            meta['input_records'] = verify.get('records') or []
            meta['input_attempts'] = verify.get('attempts')
            meta['conflicts'] = verify.get('conflicts', 0)
            meta['trust_paste'] = bool(verify.get('trust_paste'))
            if not verify.get('ok'):
                meta['sentinel'] = 'failed'
                meta['readback'] = str(verify.get('last_text') or '')[-60:]
                meta['error'] = verify.get('code') or 'input_verify_failed'
                meta['missing'] = verify.get('missing') or ''
                return '', time.perf_counter(), meta
            meta['sentinel'] = 'ok'
            meta['paste_sent'] = True
            meta['readback_ok'] = True
        else:
            # === Chromium canvas 自绘路径：bridge=None ===
            # probe 脚本 (tools/_probe_wb_v2.py) 精确验证过的流程：
            # Ctrl+N → Esc×2 → 点 (0.50, 0.60) → Esc→Ctrl+A→Del→Esc → 剪贴板写→Ctrl+V→等1.5s
            # → Ctrl+End→Enter → 立即 BUSY DETECTED
            # 必须完全照搬，不能有多余的 paste_verify / _copy_page / _uia_focus 干扰
            meta['uia_error'] = _UIA['last']
            meta['focus'] = 'clipboard'
            from core.paste_verify import ClipboardGuard
            # Step 1: Esc×2 清场
            _send_keys([27]); time.sleep(0.12)
            _send_keys([27]); time.sleep(0.12)
            # Step 2: 点击输入框位置
            _click_ratio(hwnd, 0.50, 0.60)
            time.sleep(0.3)
            # Step 3: 清空预填 prompt
            _send_keys([27]); time.sleep(0.15)
            _send_keys([17, 65]); time.sleep(0.2)
            _send_keys([46]); time.sleep(0.3)
            _send_keys([27]); time.sleep(0.15)
            meta['prefill_cleared'] = True
            # Step 4: 剪贴板写 prompt → Ctrl+V 粘贴
            guard = ClipboardGuard()
            guard.save()
            try:
                guard.write_text(prompt)
                time.sleep(0.2)
                _send_keys([17, 86])          # Ctrl+V
                time.sleep(1.5)
            finally:
                guard.restore()
            meta['trust_paste'] = True
            meta['readback_ok'] = True
            meta['input_records'] = [{'mode': 'probe_exact'}]
            # 直接 break 出 for 循环，进入 Enter 提交
            break
        _send_keys([17, 35])                           # 把光标收回到末尾，再回车
        time.sleep(0.2)
        sent_at = time.perf_counter()
        sent_wall = time.time()          # 产物时间过滤必须用墙钟；perf_counter 是开机秒数
        meta['sent_wall'] = sent_wall
        _send_keys([13])
        deadline = time.perf_counter() + 24.0
        page = ''
        while time.perf_counter() < deadline:
            time.sleep(1.0)
            page = _grab(hwnd)
            # 发送后模式校验（共用层）：走错模式（正在生成图片/幻灯片…）的回合没有文字增量，
            # 必须在比字数/判 busy 之前中止，且不允许重试。
            wrong = sent_mode_check(page, log=lambda tag, payload: _log(tag, payload))
            if not wrong['ok']:
                meta['error'] = 'wrong_skill'
                meta['missing'] = wrong['missing']
                meta['wrong_marker'] = wrong['marker']
                return page, sent_at, meta
            state = _composer_state(hwnd, target, bridge)
            meta['cleared'] = state
            if any(word in page for word in BUSY):
                meta['busy_seen'] = True
                return page, sent_at, meta
            # 回读已确认过的提问 + 输入框随后清空 = 消息确实发出去了。
            # 不能额外要求页面里出现本次提问的开头：长提问会被 WorkBuddy 折叠成
            # 最后一两行，按首 12 字匹配必然落空，于是误判 not_submitted 并重复提交。
            if meta['readback_ok'] and state == 'empty':
                meta['submitted_cleared'] = True
                return page, sent_at, meta
            now = _artifact_sig()
            if now != before:
                meta['artifact_changed'] = True
                return page, sent_at, meta
            # 输入框已清空且整页内容发生变化：新提问已经进入对话（快答时没有“生成中”字样）
            if state in ('empty', 'focus_elsewhere') and _norm_text(page) != base_page:
                meta['page_changed'] = True
                return page, sent_at, meta
        state = _composer_state(hwnd, target, bridge)
        meta['cleared'] = state
        if state == 'holds_prompt':
            continue                                   # 文字还在输入框：确实没发出去，允许重试
        if meta['readback_ok'] and state == 'empty':
            meta['submitted_cleared'] = True           # 粘进去的字被回车送走了
            return page, sent_at, meta
        # 输入框已清空、页面上已经能看到本次提问的开头：消息确实发出去了，
        # 只是暂时还没有“生成中/已处理”字样（WorkBuddy 在静默思考，不能当没发出去）。
        head = _norm_text(prompt)[:12]
        if state in ('empty', 'focus_elsewhere') and head and head in _norm_text(page):
            meta['page_changed'] = True
            return page, sent_at, meta
        return '', time.perf_counter(), meta
    if not meta['readback_ok']:
        # 仍停在 PPT 编辑页：不管是否尝试过自动切回，都要按“停在上一次 PPT 页面”报，
        # 不能退化成含糊的 input_not_found。
        state_now = _ppt_editor_state(hwnd)
        if state_now:
            meta['error'] = 'stuck_ppt_page'
            meta['stuck_page'] = state_now
        else:
            meta['error'] = 'input_not_found' if meta.get('uia_error') else 'paste_failed'
    else:
        meta['error'] = 'input_not_cleared'
    return '', time.perf_counter(), meta


def extract_answer(page, prompt):
    """从整页文本里截出回答正文：提问结尾锚点之后、模型页脚之前。"""
    anchor = prompt.strip()[-18:]
    index = page.rfind(anchor)
    body = page[index + len(anchor):] if index >= 0 else page
    body = body.split('DeepSeek-V4.1-Flash')[0].replace('\ufeff', '').replace('\u200b', '')
    marks = list(re.finditer(r'\r?\n\d{1,2}:\d{2}\r?\n', body))
    if marks and len(body) - marks[-1].start() <= 500:        # 只截尾部页脚，别碰开头的时间戳
        body = body[:marks[-1].start()]
    lines = [line.strip() for line in body.splitlines()]
    drop = lambda line: (not line or line == 'WorkBuddy' or re.match(r'^\d{1,2}:\d{2}$', line)
                         or re.fullmatch(r'已(?:处理|完成)\s*\d+(?:\.\d+)?\s*(?:s|秒)?',line))
    while lines and drop(lines[0]):
        lines.pop(0)
    while lines and drop(lines[-1]):
        lines.pop()
    return '\n'.join(lines)


def looks_done(page):
    """回答已结束：尾部不再有生成中状态词，且出现了“已处理 + 耗时”或独立时间戳行。

    产物卡片（如“xxx.pptx 加载中...”）会排在时间戳之后，只认尾部 300 字的时间戳会漏判，
    所以同时接受完成态标签“已处理”。
    """
    tail = page[-120:]
    if any(word in tail for word in BUSY):
        return False
    return bool(re.search(r'已(?:处理|完成)\s*\d', page)
                or re.search(r'\r?\n\d{1,2}:\d{2}\r?\n', page[-600:]))


def _window_title(hwnd):
    user = ctypes.windll.user32
    try:
        length = user.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value or ''
    except Exception:
        return ''


def _pptx_editor(hwnd):
    """WorkBuddy 生成完 PPT 会切到内置编辑器：窗口标题变成 “xxx.pptx”。

    此时对话正文被编辑器视图替换，Ctrl+A 复制抓不到对话文字（实测整页只回 0 字），
    但磁盘上的 .pptx 已经真实落地，交付物应该以文件为准。
    """
    title = _window_title(hwnd)
    return title if title.lower().endswith('.pptx') or '.pptx' in title.lower() else ''


def _latest_pptx(since):
    """WorkBuddy 默认产物目录里，本步之后新出现的最新 .pptx。"""
    base = Path.home() / 'WorkBuddy'
    if not base.is_dir():
        return ''
    found = []
    try:
        for path in base.rglob('*.pptx'):
            try:
                if path.is_file() and path.stat().st_mtime >= float(since) - 10:
                    found.append(path)
            except OSError:
                continue
    except OSError:
        return ''
    return str(max(found, key=lambda item: item.stat().st_mtime)) if found else ''


def _pptx_pages(deck):
    """.pptx 页数：数 ppt/slides/slideN.xml，不依赖 python-pptx。"""
    try:
        with zipfile.ZipFile(deck) as zf:
            return len([n for n in zf.namelist()
                        if re.match(r'ppt/slides/slide\d+\.xml$', n)])
    except Exception:
        return 0


_PROOF_STOP = {'PPT', 'PPTX', 'WORKBUDDY', 'HTTP', 'HTTPS', 'HTML', 'JSON', 'EXCEL',
               'WORD', 'PDF', 'TODO', 'API', 'CPU', 'GPU', 'TXT', 'ZIP'}
_PROMPT_WRAPPER = ('请严格按下面这份', '以下是上一步', '本步要求', '必须产出可下载',
                   '输出：必须产出', '（只要', '不要反问')


def _prompt_body(prompt):
    """去掉咕嘎自己加的模板行，只留上一步正文：校验“要求里有没有正文”必须用正文关键词。"""
    lines = [line.strip() for line in str(prompt or '').splitlines()]
    body = [line for line in lines
            if line and not any(line.startswith(mark) for mark in _PROMPT_WRAPPER)]
    return '\n'.join(body) or str(prompt or '')


def _proof_tokens(prompt):
    """正文里可辨识的关键词；用于证明提示词确实进了“要求”，而不是只进了末段指令。"""
    body = _prompt_body(prompt)
    tokens = []
    for match in re.findall(r'[A-Za-z][A-Za-z0-9\-\.]{3,}', body):
        token = match.strip('.-')
        if len(token) < 4 or token.upper() in _PROOF_STOP:
            continue
        if token not in tokens:
            tokens.append(token)
        if len(tokens) >= 8:
            break
    for match in re.findall(r'[\u4e00-\u9fff]{4,8}', _norm_text(body)):
        if match not in tokens:
            tokens.append(match)
        if len(tokens) >= 12:
            break
    return tokens


def _proof_in(text, tokens):
    flat = _norm_text(text).upper()
    for token in tokens:
        if _norm_text(token).upper() in flat:
            return token
    return ''


def _pptx_text(deck, limit=200000):
    """.pptx 正文：把 slide XML 的文本节点拼起来，用于核对内容是否按提示词生成。"""
    try:
        with zipfile.ZipFile(deck) as zf:
            parts = []
            for name in zf.namelist():
                if re.match(r'ppt/slides/slide\d+\.xml$', name):
                    xml = zf.read(name).decode('utf-8', 'ignore')
                    parts.append(re.sub(r'<[^>]+>', ' ', xml))
            return ' '.join(parts)[:limit]
    except Exception:
        return ''


def _slidep_quiet(deck, seconds=15.0):
    """WorkBuddy 逐页写入时 .slidep/commands.jsonl 会持续更新；静默够久才认写完。

    优先检查 commands.jsonl（每页写入都会追加），slidep.log 只在 create 时写一次。
    """
    slidep_dir = Path(deck).parent / '.slidep'
    for name in ('commands.jsonl', 'slidep.log'):
        log = slidep_dir / name
        try:
            if log.exists():
                if (time.time() - log.stat().st_mtime) >= seconds:
                    return True
                return False  # 有活跃文件且不够静默
        except OSError:
            pass
    return True


def _close_pptx_tab(hwnd):
    """PPT 编辑页没有对话输入框：关掉顶部编辑标签，回到对话页再提交。

    标签标题会截断成“…”（OCR 得到“．“”两个点），关闭 X 紧贴在标题右侧约 0.008；
    点完用底部“第 N 页/共 M 页”是否消失来验证，点偏了依次往右补 0.008 再试。
    """
    try:
        words = _ocr_words(hwnd, region=(0, 0.05, 0.9, 0.10))
    except Exception:
        return False
    lines = [line for line in _ocr_lines(words) if 0.05 <= line['y'] <= 0.15]
    title = next((line for line in lines if '.pptx' in line['text'].lower()), None)
    if title is None:
        # 截断的标题只显示“xxx…”，没有 .pptx 后缀；必须同时看到省略号才认标签。
        ell = any('…' in line['text'] or '...' in line['text'] or '．' in line['text']
                  for line in lines)
        title = next((line for line in lines if 'ppt' in line['text'].lower()), None)
        if not ell or title is None:
            return False
    right = max(_line_box(line)[2] for line in lines
                if abs(line['y'] - title['y']) <= 0.02)
    x0, y, x1, bottom = _line_box(title)
    for offset in (0.008, 0.016, 0.024):
        # _line_box 的第 4 个值是下边界，不是高度；这里必须用 (上+下)/2 才是文字中线。
        _click_ratio(hwnd, min(0.92, right + offset), (y + bottom) / 2)
        for _ in range(2):
            time.sleep(1.3)
            try:
                footer = _norm_text(''.join(
                    str(item.get('text') or '')
                    for item in _ocr_words(hwnd, region=(0, 0.86, 1, 1))))
            except Exception:
                continue                      # OCR 失败不能算“已关闭”，继续等/重试
            if not re.search(r'第\d+[页贞][/／]共\d+[页贞]', footer):
                return True
    return False


def _has_pptx_tab(hwnd):
    """窗口顶部是否还挂着 xxx.pptx 编辑标签（不点击，只判断）。"""
    try:
        words = _ocr_words(hwnd, region=(0, 0.03, 0.85, 0.12))
    except Exception:
        return False
    return any('.pptx' in line['text'].lower() for line in _ocr_lines(words))


def _ppt_editor_state(hwnd):
    """当前是否停在 PPT 编辑页：标题、顶部标签、底部“第 N 页/共 M 页”三种证据。"""
    if _pptx_editor(hwnd):
        return 'editor_title'
    if _has_pptx_tab(hwnd):
        return 'pptx_tab'
    try:
        text = _norm_text(''.join(
            str(x.get('text') or '') for x in _ocr_words(hwnd, region=(0, 0.86, 1, 1))))
    except Exception:
        text = ''
    if re.search(r'第\d+[页贞][/／]共\d+[页贞]', text):
        return 'editor_statusbar'
    return ''


def _home_page(hwnd):
    """当前是否在 WorkBuddy 主页面（带“今天帮你做些什么？”输入框的新任务页）。"""
    try:
        text = _norm_text(''.join(
            row['text'] for row in _ocr_lines(_ocr_words(hwnd,region=(.18,.18,.78,.60)))))
    except Exception:
        text = ''
    return ('今天帮你' in text) or ('做些什么' in text)


def _card_lines(hwnd):
    """反问卡片的候选行：只取每行最右侧的文字簇，避开左侧栏/正文混排。"""
    try:
        words = _ocr_words(hwnd)
    except Exception:
        return []
    rows = []
    for line in _ocr_lines(words):
        if not (0.26 <= line['y'] <= 0.94):
            continue
        cells = sorted(line['words'], key=lambda item: float(item.get('x', 0)))
        cluster = [cells[-1]]
        for item in reversed(cells[:-1]):
            gap = float(cluster[0].get('x', 0)) - (float(item.get('x', 0)) + float(item.get('w', 0)))
            if gap > 0.08:
                break
            cluster.insert(0, item)
        x0 = min(float(item['x']) for item in cluster)
        x1 = max(float(item['x']) + float(item.get('w', 0)) for item in cluster)
        high = max(float(item.get('h', 0.02)) for item in cluster)
        rows.append(dict(text=''.join(str(item.get('text') or '') for item in cluster),
                         x0=x0, x1=x1, y=line['y'], bottom=line['y'] + high))
    return rows


def _question_only(hwnd):
    """当前反问卡片的提问原文（带问号的那一行，剔除左侧栏混排前缀）。"""
    try:
        words = _ocr_words(hwnd)
    except Exception:
        return ''
    hits = []
    for line in _ocr_lines(words):
        if not (0.26 <= line['y'] <= 0.90):
            continue
        text = ''.join(str(item.get('text') or '') for item in line['words']
                       if float(item.get('x', 0)) >= 0.30)
        if not text:
            text = line['text']
        if '？' in text or '?' in text:
            hits.append(text)
    return max(hits, key=len) if hits else ''


def _answer_question(hwnd):
    """识别反问卡片并自动选项：含“推荐”→ 第一项 → “跳过”。

    返回证据 dict(question, picked, at)；没有可点选项时返回 None。
    """
    rows = _card_lines(hwnd)
    if not rows:
        return None
    options = [row for row in rows
               if re.match(r'^[１２３４５６123456]', row['text'])
               or any(word in row['text'] for word in ('推荐', '跳过', '其他补充'))]
    pick = next((row for row in options if '推荐' in row['text']), None)
    if pick is None:
        pick = next((row for row in options if re.match(r'^[１1]', row['text'])), None)
    if pick is None:
        pick = next((row for row in rows if '跳过' in row['text']), None)
    if pick is None:
        return None
    question = _question_only(hwnd)
    _click_ratio(hwnd, (pick['x0'] + pick['x1']) / 2, (pick['y'] + pick['bottom']) / 2)
    time.sleep(1.5)
    return dict(question=question, picked=pick['text'],
                at=time.strftime('%H:%M:%S'))


def _recover_new_task(hwnd):
    """强制回到干净的新任务页（带大输入框的对话页）。

    WorkBuddy V5.9.3 是 Chromium Canvas 自绘，没有独立输入框控件，
    干净页由 Ctrl+N 直接创建——**不需要前置 Esc 或后置 Esc**，
    多余的 Esc 反而会破坏 Ctrl+N 的效果（可能触发菜单关闭或焦点转移）。
    干净页里输入框会带有预填的推荐 prompt（如"根据最近30天..."），
    后续在 submit 的 clipboard 路径里会显式清空。
    """
    from core.computer_control import _send_keys
    _send_keys([17, 78])  # Ctrl+N 新建任务
    time.sleep(2.5)       # 等待页面切换完成
    return 'ctrl_n'


def _artifact_progress():
    """最新 WorkBuddy 任务目录的真实产物进度：.slide 数量、pptx 大小与修改时间。

    只看界面状态词会把“已处理但产物还在长”误判成结束，所以进度以文件为准。
    """
    base = Path.home() / 'WorkBuddy'
    if not base.is_dir():
        return {}
    try:
        latest = max((d for d in base.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime)
    except (OSError, ValueError):
        return {}
    info = dict(task_dir=str(latest))
    try:
        for child in latest.iterdir():
            if not child.is_dir():
                continue
            slides = list((child / 'slides').glob('*.slide')) if (child / 'slides').is_dir() else []
            decks = [p for p in child.glob('*.pptx')]
            if slides or decks:
                info['slide_files'] = len(slides)
                if decks:
                    deck = max(decks, key=lambda p: p.stat().st_mtime)
                    info['pptx'] = str(deck)
                    info['pptx_size'] = deck.stat().st_size
                    info['pptx_at'] = time.strftime('%H:%M:%S', time.localtime(deck.stat().st_mtime))
                break
    except OSError:
        pass
    return info


def capture(prompt, out_root=None, timeout=900, cancel=None, idle_gate_ms=2500, on_poll=None, require_new=False):
    """真实 UI 抓取：返回 answer 全文与任务包路径；不做任何降级生成。"""
    # 本进程若不是 DPI 感知的，GetWindowRect 会被系统按缩放虚拟化
    # （实测 1005x750 被读成 804x600，所有比例点击整体偏移，粘贴/回车全打空）。
    # 在线程级别切到 per-monitor aware，保证截图、点击、窗口矩形用同一套物理像素。
    try:
        ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
    from core.computer_control import _ensure_foreground
    if cancel is not None and cancel.is_set():
        return dict(ok=False, error='cancelled', message='任务已取消')
    idle = _idle_ms()
    if idle < idle_gate_ms:
        return dict(ok=False, error='foreground_busy', idle_ms=idle,
                    message='用户正在操作（%d ms 前有输入），暂不接管 WorkBuddy 前台' % idle)
    started = time.perf_counter()
    item = _window() or _launch()
    hwnd = item['hwnd']
    _log('capture_stage', {'stage': 'window_or_launch', 'hwnd': hwnd, 'elapsed_ms': int((time.perf_counter() - started) * 1000)})
    user = ctypes.windll.user32
    previous = user.GetForegroundWindow()
    cursor = wintypes.POINT()
    user.GetCursorPos(ctypes.byref(cursor))
    started = time.perf_counter()
    try:
        if previous != hwnd:
            _ensure_foreground(hwnd, 3)
        _log('capture_stage', {'stage': 'foreground', 'elapsed_ms': int((time.perf_counter() - started) * 1000)})
        recovered = ''
        if require_new:
            if cancel is not None and cancel.is_set():
                return dict(ok=False, error='cancelled', message='任务已取消')
            recovered = _recover_new_task(hwnd)
            current = _window()
            if not current or current['hwnd'] != hwnd or user.GetForegroundWindow() != hwnd or not _home_page(hwnd):
                return dict(ok=False, error='new_chat_not_verified', message='没有确认 WorkBuddy 新任务页，未提交')
        # 只有“确实找不到输入框”时才去关标签/切页：PPT 面板停靠在对话页右侧时，
        # 底部状态栏也会被识别成编辑页，误按 Ctrl+W 有把整个窗口关掉的风险。
        pe_state = _ppt_editor_state(hwnd)
        _log('capture_stage', {'stage': 'ppt_editor_state', 'result': pe_state, 'elapsed_ms': int((time.perf_counter() - started) * 1000)})
        # UIA FindFirst/FromPoint 在 Chromium canvas 里可能永久挂起——
        # 用守护线程强制 5s 超时，超时就走 OCR fallback（_click_composer）。
        uia_comp = _uia_safe_call(lambda: _uia_composer(hwnd, timeout=4.0), timeout=5.0)
        _log('capture_stage', {'stage': 'uia_composer', 'found': bool(uia_comp),
                                'uia_last': _UIA.get('last', '')[:80],
                                'elapsed_ms': int((time.perf_counter() - started) * 1000)})
        if pe_state and uia_comp is None:
            # 上一单产出 PPT 后 WorkBuddy 停在编辑页：那里没有新建任务输入框，
            # 先自动关标签/切回新建任务页，再抓页面基线。
            recovered = _recover_new_task(hwnd)
            _log('capture_stage', {'stage': 'recover_new_task', 'result': recovered, 'elapsed_ms': int((time.perf_counter() - started) * 1000)})
            _ensure_foreground(hwnd, 2)
        baseline = _copy_page(hwnd) if recovered else _grab(hwnd)
        _log('capture_stage', {'stage': 'baseline_grab', 'baseline_len': len(baseline), 'baseline_tail': baseline[-60:], 'elapsed_ms': int((time.perf_counter() - started) * 1000)})
        _log('capture_stage', {'stage': 'baseline_check',
                                'has_wo_bang_ni': '我帮你' in baseline,
                                'elapsed_ms': int((time.perf_counter() - started) * 1000)})
        if '我帮你' not in baseline:
            _log('capture_stage', {'stage': 'click_new_start', 'elapsed_ms': int((time.perf_counter() - started) * 1000)})
        if not require_new and '我帮你' not in baseline and _click_text(hwnd, '新建', region=(0, 0, 0.35, 0.40)):
            time.sleep(1.2)                       # 新任务首屏渲染完，输入框自动获得焦点
            _ensure_foreground(hwnd, 2)
            baseline = _copy_page(hwnd) if recovered else _grab(hwnd)
            _log('capture_stage', {'stage': 'baseline_after_new', 'baseline_len': len(baseline), 'elapsed_ms': int((time.perf_counter() - started) * 1000)})
        _norm_baseline_chk = _norm_text(baseline)
        _busy_words_hit = [w for w in ('停止生成', '正在生成', '深度思考中') if w in _norm_baseline_chk]
        _log('capture_stage', {'stage': 'prev_task_check', 'busy_words_hit': _busy_words_hit, 'elapsed_ms': int((time.perf_counter() - started) * 1000)})
        # 上一单若还在生成，界面上会留着“停止生成”，这时优先报“等着”，不要误判输入框问题。
        if any(word in _norm_text(baseline) for word in ('停止生成', '正在生成', '深度思考中')):
            return dict(ok=False, error='previous_task_running', prior_task_running=True,
                        window=_rect(hwnd),
                        message='上一单仍在生成（界面还显示“停止生成”），本次没有提交：等它结束再派。')
        # 先看上一单是否还在写产物：这种情况下的文件增长不能算成本次提交
        _log('capture_stage', {'stage': 'artifact_sig_before', 'elapsed_ms': int((time.perf_counter() - started) * 1000)})
        prior_sig = _artifact_sig()
        _log('capture_stage', {'stage': 'artifact_sig_after', 'prior_sig': str(prior_sig)[:120], 'elapsed_ms': int((time.perf_counter() - started) * 1000)})
        time.sleep(3.0)
        prior_growing = _artifact_sig() != prior_sig
        _log('capture_stage', {'stage': 'before_submit', 'prior_growing': prior_growing, 'prompt_len': len(prompt), 'elapsed_ms': int((time.perf_counter() - started) * 1000)})
        page, sent_at, meta = _submit(hwnd, prompt, cancel, prior_growing=prior_growing)
        _log('capture_stage', {'stage': 'after_submit', 'got_page': bool(page), 'busy_seen': meta.get('busy_seen'), 'error': meta.get('error', ''), 'elapsed_ms': int((time.perf_counter() - started) * 1000)})
        # submit 里如果重启了 WorkBuddy，会把新 hwnd 放进 meta['hwnd']
        if meta.get('hwnd') and int(meta['hwnd']) != int(hwnd):
            hwnd = int(meta['hwnd'])
            _log('capture_stage', {'stage': 'hwnd_updated', 'new_hwnd': hwnd})
        if not page:
            code = meta.get('error') or 'not_submitted'
            # 上一单仍在写产物时，所有“提交没确认”都优先报上一单在跑，而不是输入框问题。
            if prior_growing and code in ('not_submitted', 'input_not_found',
                                          'paste_failed', 'stuck_ppt_page'):
                code = 'previous_task_running'
            if code == 'previous_task_running':
                hint = '上一单还在生成：等它结束（或点“停止生成”）后再派，本次没有提交。'
            elif code == 'stuck_ppt_page':
                hint = ('WorkBuddy 停在上一次的 PPT 页面，我没能自动切回新建任务页：'
                        '请点窗口左上角第二个图标（回主页），再在“今天帮你做些什么？”输入框里重派。')
            else:
                hint = ('请手动点 WorkBuddy 输入框，粘贴文字后确认框内出现该文字，再按回车；'
                        '若上一单仍在生成，先等它结束再发。')
            return dict(ok=False, error=code, submit=meta, prior_task_running=prior_growing,
                        message='提问未确认发出（%s）：回读一致=%s，回车后输入框=%s，'
                                '生成中状态=%s，产物变化=%s。%s%s'
                                % (code, meta.get('readback_ok'), meta.get('cleared'),
                                   meta.get('busy_seen'), meta.get('artifact_changed'),
                                   ('缺什么：%s。' % meta['missing']) if meta.get('missing') else '',
                                   hint),
                        page_chars=len(baseline), window=_rect(hwnd))
        if prior_growing and not meta.get('busy_seen'):
            meta['note'] = '产物在增长但未出现新的生成中状态，可能是上一单仍在跑'
        # 整页复制会把“当前显示的那一屏”全抓回来：如果窗口里同时有别的会话在跑，
        # 抓到的可能是别人的问答。用本次提问做归属锚点，页面里看不到它就不认。
        # 长提问会被气泡折叠：开头、中间、结尾各取一段，任一段可见即认定是这一单。
        flat_prompt = _norm_text(prompt)
        if len(flat_prompt) > 16:
            anchors = (flat_prompt[:12], flat_prompt[len(flat_prompt) // 2: len(flat_prompt) // 2 + 12],
                       flat_prompt[-20:])
        else:
            anchors = (flat_prompt,)
        anchors = tuple(item for item in anchors if item)
        misses = 0
        polls, same, previous_hash, text = [], 0, '', page
        deadline = sent_at + timeout
        pptx_step = bool(_PPT_PROMPT_RE.search(prompt))
        while time.perf_counter() < deadline:
            if cancel is not None and cancel.is_set():
                return dict(ok=False, error='cancelled', message='任务已取消')
            began = time.perf_counter()
            text = _grab(hwnd)
            cost = int((time.perf_counter() - began) * 1000)
            # === PPT 步骤的文件检测（不管窗口标题是不是 .pptx） ===
            # WorkBuddy V5.9.3 生成 PPT 后可能不自动切到 PPT editor，
            # 但磁盘上 .pptx 已经落地——直接以文件为准判完成。
            if pptx_step:
                deck = _latest_pptx(meta.get('sent_wall') or time.time())
                if deck:
                    try:
                        size = os.path.getsize(deck)
                    except OSError:
                        size = 0
                    signature = (deck, size)
                    stable = (int(meta.get('_deck_stable') or 0) + 1
                              if size and meta.get('_deck_sig') == signature else 0)
                    meta['_deck_sig'] = signature
                    meta['_deck_stable'] = stable
                    meta['_deck_size'] = size
                    # 连续 3 次采样（间隔 3 秒）大小不变 + commands.jsonl 静默 ≥15 秒
                    if stable >= 2 and _slidep_quiet(deck, 15):
                        meta['editor_deck'] = deck
                        meta['deck_stable_ms'] = int((time.perf_counter() - sent_at) * 1000)
                        break
                    # 还没稳定但已有 pptx：先跳过下面的 page_mismatch 等检测
                    # 让 while 循环继续等文件稳定
                    time.sleep(3.0)
                    continue
            # === 非 PPT editor 的正常检测 ===
            if _pptx_editor(hwnd) and len(_norm_text(text)) < 40:
                deck = _latest_pptx(meta.get('sent_wall') or time.time())
                if deck:
                    try:
                        size = os.path.getsize(deck)
                    except OSError:
                        size = 0
                    signature = (deck, size)
                    polls.append(dict(at_ms=int((time.perf_counter() - sent_at) * 1000),
                                      chars=len(text), editor=_pptx_editor(hwnd),
                                      deck=deck, deck_size=size, grab_ms=cost, stable=same))
                    stable = (int(meta.get('_deck_stable') or 0) + 1
                              if size and meta.get('_deck_sig') == signature else 0)
                    meta['_deck_sig'] = signature
                    meta['_deck_stable'] = stable
                    meta['_deck_size'] = size
                    # 连续 3 次采样（首次 + 2 次相同，间隔 3 秒）大小不变，
                    # 且逐页日志 .slidep/slidep.log 静默 ≥15 秒，才认这一版写完。
                    if stable >= 2 and _slidep_quiet(deck, 15):
                        meta['editor_deck'] = deck
                        meta['deck_stable_ms'] = int((time.perf_counter() - sent_at) * 1000)
                        break
                    time.sleep(3.0)
                    continue
            if '免费使用量已超出' in text or '频率限制' in text:
                reset = re.search(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', text)
                return dict(ok=False, error='workbuddy_quota',
                            message='WorkBuddy 当前模型免费额度受限，%s 后才重置'
                            % (reset.group(0) if reset else '未知时间'),
                            quota_reset=reset.group(0) if reset else '',
                            page_chars=len(text), polls=polls[-5:])
            busy = [word for word in BUSY if word in text]
            flat_page = _norm_text(text)
            anchored = any(item in flat_page for item in anchors)
            if not anchored:
                misses += 1
                # 连续 4 次（约 6 秒）都看不到本次提问：当前显示的不是这一单，
                # 立刻停手报 page_mismatch，别把别人的问答当成自己的交付。
                # PPT 步骤例外：长提问会被折叠/被编辑器替换，只要本次之后有新 .pptx 落地，
                # 就以文件为准继续等写完，最后再核对“要求里有没有正文关键词”。
                if not busy and misses >= (12 if pptx_step else 4):
                    if pptx_step and _latest_pptx(meta.get('sent_wall') or time.time()):
                        pass
                    else:
                        return dict(ok=False, error='page_mismatch',
                                    message=('WorkBuddy 页面里看不到本次提问（%s…），'
                                             '当前显示的是另一个会话；已停止抓取，没有交付。'
                                             % prompt[:20]),
                                    page_chars=len(text), polls=polls[-5:],
                                    artifact=_artifact_progress())
            else:
                misses = 0
            digest = hashlib.md5(text.encode('utf-8')).hexdigest()
            same = same + 1 if digest == previous_hash else 0
            previous_hash = digest
            elapsed = int((time.perf_counter() - sent_at) * 1000)
            progress = _artifact_progress()
            info = dict(at_ms=elapsed, chars=len(text), busy=busy, stable=same, grab_ms=cost,
                        anchored=anchored,
                        tail=text[-80:].replace('\r', ' ').replace('\n', ' '), artifact=progress)
            polls.append(info)
            if on_poll:
                try:
                    on_poll(info)
                except Exception:
                    pass
            # 反问卡片可能在任何阶段弹出（包括“深度思考中”之后才出大纲确认卡），
            # 所以每轮都查，不绑定 busy / 未 busy。卡片挡住正文时复制可能是 0 字，
            # 再用 OCR 补一次判断，避免整页复制抓不到“等待你的回答”。
            asking = '等待你的回答' in text or '待确认' in text
            if not asking and len(_norm_text(text)) < 40:
                try:
                    asking = bool(_question_only(hwnd))
                except Exception:
                    asking = False
            if asking:
                qa = meta.get('qa') or []
                if len(qa) >= 8:
                    return dict(ok=False, error='question_unanswered',
                                message=('WorkBuddy 连续追问未能自动应答，最后一题是：%s，'
                                         '请手动选择后重试'
                                         % (_question_only(hwnd) or meta.get('qa_last') or '')),
                                submit=meta, qa=qa, polls=polls[-5:], page_chars=len(text))
                picked = _answer_question(hwnd)
                if picked:
                    last_q = qa[-1].get('question') if qa else ''
                    if picked['question'] and picked['question'] == last_q:
                        same = 0           # 界面还没翻到下一问，别重复点同一项
                        continue
                    picked['round'] = len(qa) + 1
                    picked['state'] = '生成中' if busy else '待确认'
                    meta['qa'] = qa + [picked]
                    meta['qa_last'] = picked['question'] or picked['picked']
                    meta['clarified'] = True
                    same = 0
                    previous_hash = ''
                    deadline = time.perf_counter() + timeout   # 前面的等待不吃掉后面的生成时间
                    time.sleep(1.8)        # 每轮之间等界面刷新
                    continue
            if anchored and same >= 2 and not busy and looks_done(text):
                break
            time.sleep(1.4)
        else:
            return dict(ok=False, error='answer_timeout', message='等待 WorkBuddy 回答超时',
                        polls=polls[-20:], page_chars=len(text), artifact=_artifact_progress())
        settle = _grab(hwnd)
        if '用户已取消' in settle:
            return dict(ok=False, error='generation_cancelled',
                        message='WorkBuddy 显示“用户已取消”，本次没有回答',
                        page_chars=len(settle), polls=polls[-5:], window=_rect(hwnd))
        answer = extract_answer(settle, prompt)
        flat = _norm_text(answer)
        # PPT 任务：产物可能是“编辑器视图 + 已落地的 .pptx”，正文抓不到不算失败。
        deck = ''
        if _pptx_editor(hwnd) or re.search(r'PPT|幻灯片|演示文稿', prompt, re.I):
            deck = _latest_pptx(meta.get('sent_wall') or time.time())
        short_answer_verified=(bool(flat) and meta.get('readback_ok') and looks_done(settle)
                               and _norm_text(prompt) in _norm_text(settle)
                               and not re.fullmatch(r'(?:WorkBuddy|暂无响应|已完成|重新生成|复制)',flat))
        if len(flat) < 40 and not short_answer_verified:
            if not deck or not Path(deck).is_file():
                return dict(ok=False, error='empty_answer',
                            message='抓到的正文只有 %d 字（%r），判定为界面残留而非回答'
                                    % (len(flat), answer[:30]),
                            page_chars=len(settle), polls=polls[-5:], window=_rect(hwnd))
            try:
                size = Path(deck).stat().st_size
                file_digest = hashlib.sha256(Path(deck).read_bytes()).hexdigest()
            except OSError:
                size, file_digest = 0, ''
            answer = ('已生成 PPT 文件：%s（%d 字节，sha256 %s）'
                      % (deck, size, file_digest[:16]))
            flat = _norm_text(answer)
            meta['artifact_only'] = True
        # 交付前再校验：mtime 晚于本次任务开始。
        # 不再校验 pages 和 sampled size——deck_stable>=2 + slidep_quiet(15)
        # 已充分证明 PPT 写完，后续微调（lint/重新保存）不会再影响大小。
        if deck and Path(deck).is_file():
            try:
                stat = Path(deck).stat()
            except OSError:
                stat = None
            started_wall = float(meta.get('sent_wall') or 0)
            stale = stat is not None and started_wall and stat.st_mtime < started_wall - 2
            if stale:
                message = ('找到的 PPT 不是本次任务产出的：修改时间 %s 早于本次任务开始 %s'
                           % (time.strftime('%H:%M:%S', time.localtime(stat.st_mtime)),
                              time.strftime('%H:%M:%S', time.localtime(started_wall))))
                return dict(ok=False, error='artifact_stale', message=message,
                            pptx=deck, page_chars=len(settle), polls=polls[-5:],
                            artifact=_artifact_progress())
        # 证明“提示词确实进了要求”：要求或成品里必须能对上正文关键词。
        # 只取到末段“生成PPT + 规则”时这里必然对不上，不能拿无关的 PPT 冒充交付。
        if deck and Path(deck).is_file():
            tokens = _proof_tokens(prompt)
            hit = _proof_in(settle, tokens)
            proof_source = 'page' if hit else ''
            if not hit and tokens:
                hit = _proof_in(_pptx_text(deck), tokens)
                proof_source = 'deck' if hit else ''
            meta['requirement_check'] = dict(ok=bool(hit), token=hit, source=proof_source,
                                             tokens=tokens[:8], page_chars=len(settle))
            if tokens and not hit:
                missing = ('PPT 要求里的提示词正文（关键词 %s 未出现）'
                           % '、'.join(tokens[:4]))
                return dict(ok=False, error='requirement_not_landed',
                            message=('PPT 文件生成了，但要求页和成品里都看不到提示词正文的关键词'
                                     '（%s），等于拿空要求生成；本次不计入交付。'
                                     % '、'.join(tokens[:4])),
                            pptx=deck, submit=meta, missing=missing,
                            page_chars=len(settle), polls=polls[-5:],
                            artifact=_artifact_progress())
        folder = Path(out_root or guga_paths.RUNTIME / 'agent-tasks') / (
            'workbuddy-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'prompt.txt').write_text(prompt, encoding='utf-8')
        (folder / 'page.txt').write_text(settle, encoding='utf-8')
        (folder / 'answer.txt').write_text(answer, encoding='utf-8')
        if meta.get('qa'):
            # 自动替他选的每一项都留证据：问题原文、选中项、轮次、时间。
            (folder / 'qa.json').write_text(
                json.dumps(meta['qa'], ensure_ascii=False, indent=2), encoding='utf-8')
        package = dict(task_id=folder.name, source='workbuddy-ui',
                       created=datetime.now().isoformat(timespec='seconds'),
                       prompt_path=str(folder / 'prompt.txt'), answer_path=str(folder / 'answer.txt'),
                       answer_chars=len(answer), page_chars=len(settle),
                       generate_ms=int((time.perf_counter() - sent_at) * 1000),
                       elapsed_ms=int((time.perf_counter() - started) * 1000), polls=polls)
        (folder / 'package.json').write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding='utf-8')
        if len(answer) < 300 and ('发生未知错误' in answer or '暂无响应' in answer):
            return dict(ok=False, error='workbuddy_error', folder=str(folder),
                        message='WorkBuddy 返回未知错误，没有可用回答', answer_path=str(folder / 'answer.txt'),
                        page_chars=len(settle), polls=polls[-5:])
        return dict(ok=True, **package, folder=str(folder), answer=answer, submit=meta,
                    qa=meta.get('qa') or [], artifact=_artifact_progress(), pptx=deck)
    finally:
        user.SetCursorPos(cursor.x, cursor.y)
        if previous and previous != hwnd:
            _ensure_foreground(previous, 1)


# 失败码 -> 用户可行动的一句话。桥接层只报事实，绝不自己补正文。
_FAIL_HINTS = {
    'foreground_busy': '你正在操作（2.5 秒内有键鼠输入），我没抢前台；停手后再发一次就行。',
    'not_submitted': '提问没确认发出：在 WorkBuddy 输入框里确认文字真的出现，再按回车。',
    'new_chat_not_verified': 'WorkBuddy 新任务页没有核对成功，提交已暂停。',
    'paste_failed': '文字没进 WorkBuddy 输入框：先点一下输入框再重试。',
    'input_not_found': '没找到 WorkBuddy 的输入框：可能停在任务/PPT 页面；已尝试自动切回新建任务页，仍失败请点窗口左上角第二个图标（回主页）后重派。',
    'stuck_ppt_page': 'WorkBuddy 停在上一次的 PPT 页面，我没能自动切回新建任务页：请点窗口左上角第二个图标（回主页），再在“今天帮你做些什么？”输入框里重派。',
    'input_not_cleared': '回车后输入框还留着原文：请在 WorkBuddy 里手动发送一次。',
    'previous_task_running': '上一单还在生成：等它结束（或点“停止生成”）后再派。',
    'question_unanswered': 'WorkBuddy 连续追问，咕嘎没能自动答完：请手动在提问卡片上选一项后重试。',
    'workbuddy_quota': 'WorkBuddy 当前模型免费额度受限：点右上角头像 → 设置 → 模型，换一个可用模型，或等额度重置后再派。',
    'answer_timeout': '等 WorkBuddy 回答超时：确认界面没有卡在“生成中”；卡住就点“停止生成”，再重新派单。',
    'workbuddy_error': 'WorkBuddy 返回未知错误：点窗口左上角第二个图标（回主页），换新会话重新派单。',
    'generation_cancelled': 'WorkBuddy 里这一轮被取消了（界面显示“用户已取消”）：重新在输入框提问并发送。',
    'cancelled': '任务已取消：重新派单，本次没有生成任何产物。',
    'empty_answer': 'WorkBuddy 页面没有抓到正文：确认回答已经出结果；没有就点“重新生成”或重新提问。',
    'artifact_incomplete': 'PPT 文件还在写入（页数不足或大小仍在变）：等它写完再重试，不要用中途版本。',
    'artifact_stale': '找到的 PPT 不是本次任务产出的（修改时间早于任务开始）：确认任务目录后重试。',
    'page_mismatch': 'WorkBuddy 窗口里显示的是另一个会话（看不到本次提问）：关掉/切回这一单再派，本次没有交付。',
    'prompt_truncated': '提交前回读发现输入框内容为空或被截断，已中止，没有提交空要求：重新点输入框粘贴一次；仍不足就分段派单。',
    'input_verify_failed': '输入框回读与要发的正文不一致，已自动清空重试 3 次仍未过校验，本次没有回车：点一下 WorkBuddy 输入框后重派。',
    'clipboard_conflict': '粘贴时剪贴板被其他操作改写，重写重试后仍未通过校验，本次没有提交（用户原剪贴板已恢复）：停手后重派。',
    'requirement_not_landed': 'PPT 流程只取到了末段指令，提示词正文没进“要求”：已判定本次无效；重派时把正文并进同一条要求里，别让它单独落到末段。',
    'bridge_exception': 'WorkBuddy 桥接异常：先手动在窗口里发一次，确认窗口可用。',
}


def _wait_idle(limit=8.0, gate_ms=2500):
    """用户刚敲完回车就派单时，等键盘静下来再接管前台，避免直接撞 foreground_busy。"""
    deadline = time.monotonic() + max(0.0, limit)
    while time.monotonic() < deadline:
        if _idle_ms() >= gate_ms:
            return True
        time.sleep(0.4)
    return False


class Adapter:
    """给 ``core.adapters.dispatch`` 的统一适配器入口。

    只是把 :func:`capture` 的结果映射成桌面端约定的契约：
      成功 {ok, text, source{label,provenance}, evidence{dir,local_model_used,...}, notice}
      失败 {ok:False, code, hint}
    正文只可能来自 WorkBuddy 窗口的剪贴板回读，没有任何模型代写退路。
    """

    name = 'WorkBuddy'
    tool = 'workbuddy'

    def ask(self, prompt, timeout_seconds=None, retry=1, prefer_mcp=False, cancel=None, require_new=False, **_kw):
        prompt = str(prompt or '').strip()
        if not prompt:
            return failure_payload(
                'empty_prompt', 'WorkBuddy 适配器收到空 prompt，没有可提交的内容',
                app=self.name, hint='提示词为空，没有可提交的内容。')
        # 桌宠的通用超时是 180 秒，对“写周报/写文档”这种生成太短；
        # 按声明里的 max_wait_seconds（900）给足，调用方显式给更长时才覆盖。
        timeout = max(900, int(float(timeout_seconds or 0)))
        _wait_idle()
        capture_options = dict(timeout=timeout)
        if cancel is not None:
            capture_options['cancel'] = cancel
        if require_new:
            capture_options['require_new'] = True
        try:
            result = capture(prompt, **capture_options)
        except Exception as exc:
            result = dict(ok=False, error='bridge_exception',
                          message='%s: %s' % (type(exc).__name__, str(exc)[:160]))
        if not result.get('ok') and result.get('error') == 'foreground_busy' and retry:
            _wait_idle(limit=12.0)
            try:
                result = capture(prompt, **capture_options)
            except Exception as exc:
                result = dict(ok=False, error='bridge_exception',
                              message='%s: %s' % (type(exc).__name__, str(exc)[:160]))
        if not result.get('ok'):
            raw_code = str(result.get('error') or result.get('code') or 'failed')
            hint = _FAIL_HINTS.get(raw_code) or next_action_for(raw_code, self.name)
            message = str(result.get('message') or '').strip()
            raw = message or str(result.get('raw') or '') or ('WorkBuddy 返回 %s' % raw_code)
            return failure_payload(
                raw_code, raw, hint, app=self.name, hint=hint,
                missing=str(result.get('missing') or ''),
                submit=result.get('submit') or {},
                evidence=dict(dir=str(result.get('folder') or ''),
                              origin='workbuddy_desktop_window',
                              local_model_used=False,
                              raw_error=raw_code,
                              polls=result.get('polls') or []),
                message=message or hint)
        answer = str(result.get('answer') or '').strip()
        deck = str(result.get('pptx') or '').strip()
        if deck and not Path(deck).is_file():
            deck = ''
        if not answer and deck:
            answer = '已生成 PPT 文件：%s' % deck
        if not answer:
            return failure_payload(
                'empty_answer',
                'WorkBuddy 页面没有读到可用正文，也没有落地可核对的 PPT 文件',
                app=self.name, hint=_FAIL_HINTS['empty_answer'],
                evidence=dict(dir=str(result.get('folder') or ''),
                              origin='workbuddy_desktop_window',
                              local_model_used=False))
        digest = hashlib.sha256(answer.encode('utf-8')).hexdigest()
        file_digest = ''
        if deck:
            try:
                file_digest = hashlib.sha256(Path(deck).read_bytes()).hexdigest()
            except OSError:
                deck = ''
        task_id = str(result.get('task_id') or '')
        # PPT 文件交付时，来源回执绑在文件本身（路径 + sha256），不是绑在一段文字上。
        provenance = ('file:%s' % file_digest[:16]) if file_digest else ('ui_capture:%s' % digest[:16])
        return dict(
            ok=True,
            text=answer,
            notice=('未走接口：WorkBuddy 没有对外的文本 API，'
                    '本次是在它的窗口里真实提交并读回正文。' +
                    ('PPT 文件由 WorkBuddy 生成，回执按文件 sha256 记录。' if deck else '')),
            source=dict(tool=self.tool, name=self.name, channel='ui_automation',
                        label='WorkBuddy（界面自动化，非接口调用）',
                        provenance=provenance,
                        call_id=task_id),
            evidence=dict(dir=str(result.get('folder') or ''),
                          receipt_id=task_id,
                          origin='workbuddy_desktop_window',
                          local_model_used=False,
                          text_chars=len(answer),
                          text_sha256=digest,
                          artifact=deck,
                          artifact_sha256=file_digest,
                          elapsed_ms=result.get('elapsed_ms'),
                          generate_ms=result.get('generate_ms'),
                          submit=result.get('submit') or {}),
            elapsed_ms=result.get('elapsed_ms'),
        )
