"""跨工具链：WorkBuddy 出提示词 → 抓回全文 → JoyCode 生成代码 → 本地核验产物。

失败就如实返回，不降级成本机模型自己生成；WorkBuddy/JoyCode 的前台激活沿用各自已有约束。
"""
import json
import re
import subprocess
import time
import uuid
from pathlib import Path

from core import workbuddy_bridge, guga_paths

WORKSPACE = guga_paths.DELIVERY               # JoyCode 工作区根目录（用户可写）
DELIVERABLES = guga_paths.DELIVERY
BROWSERS = (
    Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'),
    Path(r'C:\Program Files\Microsoft\Edge\Application\msedge.exe'),
    Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe'),
    Path(r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'),
)


def prompt_for(requirement, product='website'):
    """交给 WorkBuddy 的元需求：要它输出可整段交给代码生成工具的提示词。"""
    shape = ('单页静态网站，入口为 index.html，CSS/JS 内联或同目录相对引用，'
             '不使用构建工具或本地服务器') if product == 'website' else '可直接运行的多文件代码'
    return ('请只输出文本，不要执行任何文件或网页操作，也不要创建任务文件。\n'
            '你是一名提示词工程专家。用户需求：%s\n'
            '请输出一段可以直接整段交给代码生成工具的中文提示词，要求：\n'
            '1) 明确产物形态：%s；\n'
            '2) 说明页面/模块结构、视觉风格、交互细节、响应式要求；\n'
            '3) 写明必须包含的真实文案要点，禁止占位符和空白页；\n'
            '4) 给出可验收标准，便于下一步核验。' % (requirement, shape))


def acceptance_for(folder, product='website'):
    relative = folder.relative_to(WORKSPACE).as_posix()
    if product != 'website':
        return '在工作区根目录创建 %s/ 并产出可直接运行的代码；完成后在 self_check 中列出文件与验证结果。' % relative
    return ('在工作区根目录创建目录 %s/，产出一个可直接双击打开的静态单页网站：\n'
            '1) 入口必须是 %s/index.html，CSS/JS 内联或同目录相对引用，不依赖构建工具、本地服务器或外网 CDN；\n'
            '2) 页面必须有产品名、价值主张、至少 3 个内容区块和行动按钮，文案完整真实，禁止空白页、占位符和“待补充”；\n'
            '3) 具备基本响应式布局（窄屏不横向溢出）；\n'
            '4) 完成后在回执 self_check 中逐项说明：创建了哪些文件、index.html 是否存在、页面是否非空白；\n'
            '5) index.html 一写完就收尾：立即用 guga 的 submit_artifact（或 write_result.py）交回执，'
            '不要在交回执前反复做样式微调。'
            % (relative, relative))


def find_browser():
    for path in BROWSERS:
        if path.is_file():
            return str(path)
    return ''


def verify_site(entry, timeout=90):
    """用真实 Edge 通过 CDP 打开入口页：读 DOM/正文 + 截图，非空白才算通过。"""
    import base64
    import tempfile
    from PIL import Image, ImageStat
    result = dict(ok=False, entry=str(entry), exists=entry.is_file(),
                  size=entry.stat().st_size if entry.is_file() else 0)
    if not result['exists']:
        result.update(error='entry_missing')
        return result
    browser = find_browser()
    if not browser:
        result.update(error='browser_missing')
        return result
    import random
    import requests
    import websocket
    port = random.randint(9400, 9800)
    profile = tempfile.mkdtemp(prefix='guga-verify-')
    proc = subprocess.Popen([browser, '--remote-debugging-port=%d' % port, '--user-data-dir=' + profile,
                             '--no-first-run', '--no-default-browser-check', '--window-size=1366,768',
                             '--window-position=-2400,-2400', entry.as_uri()],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=8)
    started = time.perf_counter()
    shot = entry.parent / 'verify.png'
    try:
        target = None
        for _ in range(int(timeout * 2.5)):
            try:
                pages = requests.get('http://127.0.0.1:%d/json/list' % port, timeout=1).json()
                target = next((item for item in pages if item.get('type') == 'page'), None)
                if target:
                    break
            except (requests.RequestException, ValueError):
                pass
            time.sleep(0.4)
        if not target:
            result.update(error='browser_start_timeout')
            return result
        ws = websocket.create_connection(target['webSocketDebuggerUrl'], timeout=15)
        # CDP 一挂上就读 DOM 常常读到还在加载的空壳，先等 readyState 完成再取值。
        for _ in range(160):
            ws.send(json.dumps({'id': 90, 'method': 'Runtime.evaluate',
                                'params': {'expression': 'document.readyState', 'returnByValue': True}}))
            state = ''
            for _ in range(6):
                message = json.loads(ws.recv())
                if message.get('id') == 90:
                    state = (message.get('result') or {}).get('result', {}).get('value') or ''
                    break
            if state == 'complete':
                time.sleep(1.0)                   # 让首屏脚本/入场动画跑完再截图
                break
            time.sleep(0.25)
        expression = ('JSON.stringify({ready:document.readyState,title:document.title,'
                      'text:document.body?document.body.innerText:"",'
                      'sections:document.querySelectorAll("section,article,.section").length,'
                      'buttons:document.querySelectorAll("a,button").length,'
                      'height:document.body?document.body.scrollHeight:0,'
                      'bg:getComputedStyle(document.body).backgroundColor})')
        ws.send(json.dumps({'id': 1, 'method': 'Runtime.evaluate',
                            'params': {'expression': expression, 'returnByValue': True}}))
        meta = {}
        for _ in range(30):
            message = json.loads(ws.recv())
            if message.get('id') == 1:
                meta = json.loads((message.get('result') or {}).get('result', {}).get('value') or '{}')
                break
        # 滚动入场动画（.reveal 初始 opacity:0）会让整页截图只剩首屏和页脚，
        # 截图前把入场类补上，还原用户滚动到底后实际看到的样子。
        ws.send(json.dumps({'id': 3, 'method': 'Runtime.evaluate',
                            'params': {'expression': 'document.querySelectorAll(".reveal")'
                                      '.forEach(function(e){e.classList.add("in")});1',
                                       'returnByValue': True}}))
        for _ in range(10):
            message = json.loads(ws.recv())
            if message.get('id') == 3:
                break
        time.sleep(0.6)
        ws.send(json.dumps({'id': 2, 'method': 'Page.captureScreenshot',
                            'params': {'format': 'png', 'captureBeyondViewport': True}}))
        for _ in range(40):
            message = json.loads(ws.recv())
            if message.get('id') == 2:
                shot.write_bytes(base64.b64decode((message['result'] or {})['data']))
                break
        ws.close()
    except (OSError, ValueError, KeyError, websocket.WebSocketException) as exc:
        result.update(error='browser_failed', message=str(exc)[:200])
        return result
    finally:
        proc.kill()
        try:
            import shutil
            shutil.rmtree(profile, ignore_errors=True)
        except OSError:
            pass
    text = re.sub(r'\s+', ' ', str(meta.get('text') or '')).strip()
    colors = 0
    gray = 0
    if shot.is_file():
        image = Image.open(shot).convert('RGB')
        colors = len(image.getcolors(maxcolors=200000) or [])
        gray = ImageStat.Stat(image.convert('L')).stddev[0]
    result.update(browser=Path(browser).name, screenshot=str(shot), title=meta.get('title'),
                  ready_state=meta.get('ready'), dom_chars=len(text), text=text[:120],
                  sections=meta.get('sections'), links=meta.get('buttons'),
                  page_height=meta.get('height'), background=meta.get('bg'),
                  unique_colors=colors, gray_stddev=round(gray, 2),
                  render_ms=int((time.perf_counter() - started) * 1000),
                  ok=bool(text) and len(text) >= 40 and colors > 200 and gray > 8
                  and meta.get('ready') == 'complete')
    return result


def _composer_points(hwnd):
    """输入框候选点击点，按可信度排序。

    占位文字相对输入框的位置会随面板布局（编辑器宽窄、是否展开"最近任务"）上下变化，
    单一固定偏移不可靠；调用方用剪贴板焦点探针逐个验证，点错最多复制到正文不会误发消息。
    """
    from core.workbuddy_bridge import _ocr_words, _rect
    left, top, right, bottom = _rect(hwnd)
    width, height = right - left, bottom - top
    points = []
    try:
        words = _ocr_words(hwnd, (0.5, 0.3, 0.5, 0.68))
        hits = [item for item in words
                if any(token in re.sub(r'\W', '', str(item.get('text') or ''))
                       for token in ('添', '上下', '快捷', '询问'))]
        if hits:
            target = max(hits, key=lambda item: float(item.get('y', 0)))
            cx = int(left + (target['x'] + target['w'] / 2) * width)
            cy = int(top + (float(target['y']) + float(target['h']) / 2) * height)
            for ratio in (0.02, 0.0, -0.04, -0.085):   # 占位文字可能在框内任意高度，双向试探
                points.append((cx, cy + int(ratio * height)))
    except Exception:
        pass
    points.append((int(left + width * 0.91), int(top + height * 0.60)))
    points.append((int(left + width * 0.885), int(top + height * 0.485)))
    seen, out = set(), []
    for point in points:
        if point not in seen:
            seen.add(point)
            out.append(point)
    return out


def _say(task_id):
    """旁路 JoyCode 自带 send：点占位文字聚焦输入框 → 粘贴 → 剪贴板回读一致才回车。"""
    import ctypes
    import win32clipboard as clip
    import win32con
    from core.window_inventory import list_windows
    from core.computer_control import _ensure_foreground, _send_keys
    item = next((w for w in list_windows('joycode')
                 if str(w.get('process_name', '')).casefold() == 'joycode.exe' and not w.get('minimized')), None)
    if not item:
        return dict(ok=False, error='window_missing', message='JoyCode 窗口不存在或已最小化')
    hwnd = int(item['hwnd'])
    idle = workbuddy_bridge._idle_ms()
    if idle < 2500:
        return dict(ok=False, error='foreground_busy', idle_ms=idle,
                    message='用户正在操作（%d ms 前有输入），暂不接管 JoyCode 前台' % idle)
    user = ctypes.windll.user32
    previous = user.GetForegroundWindow()
    if not _ensure_foreground(hwnd, 5):
        return dict(ok=False, error='not_foreground', message='JoyCode 无法置前')
    time.sleep(0.4)
    points = _composer_points(hwnd)
    if not points:
        return dict(ok=False, error='composer_not_found', message='OCR 找不到对话输入框占位文字')

    def _read():
        try:
            clip.OpenClipboard()
            value = str(clip.GetClipboardData())
            clip.CloseClipboard()
            return value
        except Exception:
            return ''

    def _put(value):
        clip.OpenClipboard()
        clip.EmptyClipboard()
        clip.SetClipboardData(win32con.CF_UNICODETEXT, value)
        clip.CloseClipboard()

    focused = None
    for point in points:
        user.SetCursorPos(*point)
        time.sleep(0.1)
        user.mouse_event(2, 0, 0, 0, 0)
        user.mouse_event(4, 0, 0, 0, 0)
        time.sleep(0.45)
        _put('GUGA_FOCUS_PROBE')
        _send_keys([17, 65]); _send_keys([17, 67])
        time.sleep(0.35)
        # 输入框为空时复制不到东西，剪贴板保持探针；复制到整页至少几百字，说明焦点在正文。
        if len(_read()) <= 400:
            focused = point
            break
    if focused is None:
        return dict(ok=False, error='input_not_focused', tried=len(points),
                    message='候选点击点都未聚焦对话框输入框')
    _send_keys([46])                              # 焦点已确认在输入框，清空才安全
    text = '/guga-run ' + task_id
    _put(text)
    _send_keys([17, 86])
    time.sleep(0.5)
    _send_keys([17, 65]); _send_keys([17, 67])
    time.sleep(0.35)
    if text not in _read().replace('\r', ''):
        return dict(ok=False, error='input_mismatch', expected=text, got=_read()[:120])
    _send_keys([13])
    time.sleep(0.6)
    if previous and previous != hwnd:              # 不把用户原来的前台窗口一直压着
        _ensure_foreground(previous, 2)
    return dict(ok=True, source='fallback', text=text, point=list(focused))


def run(requirement, acceptance=None, product='website', timeout=900, cancel=None,
        out_root=None, why=''):
    """同步执行整条链；返回分段耗时与核验明细。"""
    started = time.perf_counter()
    marks, folder_name = {}, 'cross-tool-' + time.strftime('%Y%m%d-%H%M%S')
    folder = (Path(out_root) if out_root else DELIVERABLES / folder_name)
    folder.mkdir(parents=True, exist_ok=True)
    marks['workbuddy_ms'] = 0
    try:
        capture = workbuddy_bridge.capture(prompt_for(requirement, product),
                                           out_root=guga_paths.RUNTIME / 'agent-tasks',
                                           cancel=cancel)
    except Exception as exc:
        detail = str(exc)[:180]
        if 'WorkBuddy' in detail:
            return dict(ok=False, stage='workbuddy', error='workbuddy_unavailable',
                        message='本机没有可用的 WorkBuddy，跨工具链无法开工。'
                                '请安装并登录 WorkBuddy 桌面端（默认路径 '
                                '%LOCALAPPDATA%\\Programs\\WorkBuddy\\WorkBuddy.exe），'
                                '装好后重试；其他功能不受影响。',
                        detail=detail, decision_basis=why, timings=marks)
        return dict(ok=False, stage='workbuddy', error='workbuddy_failed',
                    message='WorkBuddy 环节失败：%s' % detail, timings=marks)
    if not capture.get('ok'):
        return dict(ok=False, stage='workbuddy', error=capture.get('error'),
                    message=capture.get('message') or 'WorkBuddy 抓取失败',
                    quota_reset=capture.get('quota_reset'), folder=capture.get('folder'),
                    answer_path=capture.get('answer_path'),
                    decision_basis=why, timings=marks)
    marks['workbuddy_ms'] = capture['elapsed_ms']
    answer = capture['answer']
    if len(answer) < 300:
        return dict(ok=False, stage='workbuddy', error='answer_too_short',
                    message='抓回正文只有 %d 字，不能作为派单正文' % len(answer),
                    timings=marks, answer_path=capture['answer_path'])
    from core.adapters.joycode_adapter import JoyCodeAdapter
    from core.window_inventory import list_windows
    # 不指定默认目录：自动识别 JoyCode 当前打开的工程（打包版同样生效）
    adapter = JoyCodeAdapter(poll_interval=1.0)
    joycode_hint = ('请安装 JoyCode 并登录；默认装在 D:\\JoyCode，装在别处时设置环境变量 '
                    'GUGA_JOYCODE_LAUNCHER 指向 JoyCode.exe。装好后重试；其他功能不受影响。')
    try:
        if not list_windows('joycode'):
            adapter.open_ide()
    except Exception as exc:
        return dict(ok=False, stage='joycode', error='joycode_unavailable',
                    message='本机没有可用的 JoyCode，代码类任务无法派单。' + joycode_hint,
                    detail=str(exc)[:180], answer_path=capture['answer_path'],
                    decision_basis=why, timings=marks)
    task_id = 'T-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
    try:
        adapter.create_task(answer, acceptance or acceptance_for(folder, product),
                            task_id=task_id)
    except Exception as exc:
        if getattr(exc, 'code', '') == 'workspace_not_found':
            return dict(ok=False, stage='joycode', error='workspace_not_found',
                        message='没找到 JoyCode 当前工程，请选择',
                        candidates=getattr(exc, 'candidates', []),
                        answer_path=capture['answer_path'], timings=marks)
        return dict(ok=False, stage='joycode', error='joycode_task_failed',
                    message='无法把任务写进 JoyCode 工作区：%s。%s'
                            % (str(exc)[:150], joycode_hint),
                    answer_path=capture['answer_path'], timings=marks)
    dispatch_started = time.perf_counter()
    time.sleep(3.2)   # 让 JoyCode 的前台约束回到用户真实输入基线，避免把本链自己的按键当成用户操作
    # adapter.trigger 默认也是“真实点击 → 回读断言 → 清空 → 键入 → 断言 → 回车”；
    # 这里仍走 _say，是因为它多一步“派单后把前台还给用户原来的窗口”，
    # 且整条链的失败分级沿用 stage/dispatch 字段。两条路径都是真实点击 + 回读断言，
    # 不存在旧的盲打或快捷键探测路径。
    trigger = _say(task_id)
    if not trigger.get('ok'):
        first = trigger.get('error')
        try:
            adapter._ui_layer().ensure_panel()
            time.sleep(1.0)
            retry = _say(task_id)
        except Exception as exc:
            retry = dict(ok=False, error='panel_open_failed',
                         message='%s: %s' % (getattr(exc, 'code', type(exc).__name__), str(exc)[:160]))
        retry['first_error'] = first
        trigger = retry
    if not trigger.get('ok'):
        return dict(ok=False, stage='dispatch', error=trigger.get('error') or 'trigger_failed',
                    message=str(trigger.get('message') or '')[:300],
                    task_id=task_id, timings=marks, trigger=trigger,
                    answer_path=capture['answer_path'],
                    dispatch_ms=int((time.perf_counter() - dispatch_started) * 1000))
    marks['dispatch_ms'] = int((time.perf_counter() - dispatch_started) * 1000)
    wait_started = time.perf_counter()
    receipt = adapter.wait_for_result(task_id, timeout=timeout, cancel=cancel)
    marks['joycode_ms'] = int((time.perf_counter() - wait_started) * 1000)
    if not receipt:
        return dict(ok=False, stage='joycode', error='receipt_timeout', task_id=task_id,
                    message='%d 秒内没有 JoyCode 回执' % timeout, timings=marks,
                    answer_path=capture['answer_path'], trigger=trigger)
    status = str(receipt.get('status') or '').lower()
    if status not in ('done', 'completed', 'success', 'ready'):
        return dict(ok=False, stage='joycode', error='joycode_%s' % (status or 'failed'),
                    message=str(receipt.get('error') or receipt.get('summary') or '')[:300],
                    task_id=task_id, receipt=receipt, timings=marks, answer_path=capture['answer_path'])
    entry = folder / 'index.html'
    verify_started = time.perf_counter()
    verify = verify_site(entry) if product == 'website' else dict(ok=True, entry=str(folder))
    marks['verify_ms'] = int((time.perf_counter() - verify_started) * 1000)
    result = dict(ok=bool(verify.get('ok')), stage='done', task_id=task_id, folder=str(folder),
                  requirement=requirement, product=product, decision_basis=why,
                  prompt_chars=len(Path(capture['prompt_path']).read_text(encoding='utf-8')),
                  answer_chars=len(answer), answer_path=capture['answer_path'],
                  page_chars=capture['page_chars'], receipt=receipt, verify=verify,
                  timings=marks, total_ms=int((time.perf_counter() - started) * 1000),
                  trigger=trigger)
    (folder / 'pipeline.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                                          encoding='utf-8')
    if not result['ok']:
        result['error'] = verify.get('error') or 'verify_failed'
    return result
