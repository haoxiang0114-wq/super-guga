"""本机 AI 软件清单：真实发现 + 真实启动核验 + 可查询 + 上报格式。

只认“真的能启动”：先在开始菜单/注册表找到真实 exe，再实际拉起一个窗口，
确认窗口出现才记为 launch_ok=True。仅凭配置或安装目录猜测一律记为未验证。

清单缓存 runtime/local_tools.json，供路由决策与本机查询使用。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path

from core import guga_paths

ROOT = Path(__file__).resolve().parents[1]
CACHE = guga_paths.RUNTIME / 'local_tools.json'
REPORT_STATE = guga_paths.RUNTIME / 'local_tools_report.json'
CONTRACT = 'guga.tool_inventory/1'
_REPORTER = None          # 已配对的设备清单上报器（由 LocalAgent 注入，未登录时为 None）

# 能力词表：路由只按能力匹配，不按软件名写死。
CAPABILITIES = ('prompt_engineering', 'code', 'website', 'document', 'slides',
                'image', 'video', 'chat', 'research')

# automation:
#   bridge      有本机专用适配器，可直接派单/抓取，最可靠
#   ide_dispatch 有 IDE 派单协议（任务文件 + 对话注入）
#   ui_only     无专用适配器，只能窗口感知 + 键鼠/UIA 指挥
#   launch_only 只能启动与观察
APPS = (
    dict(id='workbuddy', name='WorkBuddy', aliases=('workbuddy', 'work buddy', 'wb'),
         exe_globs=(r'%LOCALAPPDATA%\Programs\WorkBuddy\WorkBuddy.exe',),
         process='workbuddy.exe', window_keywords=('workbuddy', 'work buddy'),
         capabilities=('prompt_engineering', 'document', 'slides', 'website'),
         automation='bridge', adapter='core.workbuddy_bridge',
         notes='能把一句话扩写成高质量提示词；文档与演示文稿能直接产出完整文件（从零生成 .pptx、'
               '读写已有、改已有），视频/3D 也可真出片，费用边界见 knowledge/buttons/workbuddy-buttons.md'),
    dict(id='joycode', name='JoyCode', aliases=('joycode', 'joy code'),
         exe_globs=(r'%USERPROFILE%\..\JoyCode\JoyCode.exe',),
         process='joycode.exe', window_keywords=('joycode',),
         capabilities=('code', 'website'),
         automation='ide_dispatch', adapter='core.adapters.joycode_adapter',
         notes='IDE，通过 .guga/inbox 派单 + 对话注入执行；无 API，靠回执驱动'),
    dict(id='trae', name='Trae CN', aliases=('trae', 'trae cn', 'trae-cn'),
         exe_globs=(r'%LOCALAPPDATA%\Programs\Trae CN\Trae CN.exe',),
         process='trae cn.exe', window_keywords=('trae',),
         capabilities=('code', 'website'),
         automation='ui_only', adapter='',
         notes='IDE，当前只有窗口/键鼠指挥，没有派单适配器'),
    dict(id='doubao', name='豆包', aliases=('豆包', 'doubao'),
         exe_globs=(r'%LOCALAPPDATA%\Doubao\Application\Doubao.exe',),
         process='doubao.exe', window_keywords=('豆包', 'doubao'),
         capabilities=('chat', 'prompt_engineering', 'image', 'document', 'research'),
         automation='ui_only', adapter='',
         notes='通用对话，可生成文本/提示词、生图与写作；只能界面指挥'),
)

# 明确声明本机没有的软件，路由时要能如实说“本机没有”。
ABSENT_NAMES = ('wps', '剪映', 'jianying', 'capcut')


def _expand(pattern):
    return Path(os.path.expandvars(pattern)).expanduser()


def _resolve_shortcut(lnk):
    script = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
              "Write-Output $s.TargetPath" % str(lnk).replace("'", "''"))
    try:
        out = subprocess.run(['powershell', '-NoProfile', '-Command', script],
                             capture_output=True, text=True, timeout=20)
        target = (out.stdout or '').strip().splitlines()
        return target[-1].strip() if target else ''
    except (OSError, subprocess.SubprocessError):
        return ''


def _start_menu_candidates(spec):
    """只在名称匹配的快捷方式上解析真实目标，避免遍历几百个 .lnk。"""
    aliases = tuple(a.casefold() for a in spec['aliases'])
    roots = (Path(os.environ.get('APPDATA', '')) / 'Microsoft/Windows/Start Menu/Programs',
             Path(os.environ.get('PROGRAMDATA', 'C:/ProgramData')) / 'Microsoft/Windows/Start Menu/Programs',
             Path.home() / 'Desktop')
    found = []
    for index, root in enumerate(roots):
        if not root.is_dir():
            continue
        for lnk in (root.glob('*.lnk') if index == 2 else root.rglob('*.lnk')):
            stem = lnk.stem.casefold()
            if any(alias in stem for alias in aliases):
                target = _resolve_shortcut(lnk)
                if target and Path(target).is_file():
                    found.append(Path(target))
    return found


def _find_exe(spec):
    for pattern in spec['exe_globs']:
        candidate = _expand(pattern)
        if candidate.is_file():
            return candidate
    found = _start_menu_candidates(spec)
    return found[0] if found else None


def _windows_of(spec, by_pid=None):
    try:
        import win32gui
        import win32process
    except ImportError:
        return []
    keywords = tuple(k.casefold() for k in spec['window_keywords'])
    results = []

    def visit(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title or not any(k in title.casefold() for k in keywords):
            return
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if by_pid and pid != by_pid:
            return
        results.append(dict(hwnd=hwnd, title=title, pid=pid))

    win32gui.EnumWindows(visit, None)
    return results


def _close_window(hwnd, pid=None):
    try:
        import win32con
        import win32gui
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    except (ImportError, OSError):
        pass


def probe(spec, seconds=14):
    """真实启动核验：窗口出现才算可启动；只关闭自己新拉起的实例。"""
    exe = _find_exe(spec)
    result = dict(id=spec['id'], name=spec['name'], exe=str(exe) if exe else '',
                  exists=bool(exe), launch_ok=False, reused_running=False,
                  window_title='', verified_at=time.strftime('%Y-%m-%dT%H:%M:%S'))
    if not exe:
        result.update(reason='exe_not_found')
        return result
    running = _windows_of(spec)
    if running:
        result.update(launch_ok=True, reused_running=True, window_title=running[0]['title'],
                      reason='already_running')
        return result
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 7           # SW_SHOWMINNOACTIVE：不抢用户前台
    try:
        proc = subprocess.Popen([str(exe)], cwd=str(exe.parent), startupinfo=startup,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        result.update(reason='launch_failed', error=str(exc)[:200])
        return result
    deadline = time.monotonic() + max(3, seconds)
    window = None
    while time.monotonic() < deadline:
        window = _windows_of(spec, by_pid=proc.pid) or _windows_of(spec)
        if window:
            break
        if proc.poll() is not None and proc.returncode != 0:
            result.update(reason='exited_early', exit_code=proc.returncode)
            return result
        time.sleep(0.5)
    if not window:
        result.update(reason='window_timeout', pid=proc.pid)
        try:
            proc.terminate()
        except OSError:
            pass
        return result
    result.update(launch_ok=True, window_title=window[0]['title'], pid=proc.pid,
                  reason='window_verified')
    # 只关掉我们刚拉起的这个窗口；原本就在跑的软件不动。
    _close_window(window[0]['hwnd'])
    for _ in range(10):
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    if proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
    return result


def _spec_signature():
    """能力表指纹：改了 APPS 里的能力/自动化方式，旧缓存必须重扫，否则路由读不到新能力。"""
    raw = json.dumps([[spec['id'], list(spec['capabilities']), spec['automation']]
                      for spec in APPS], ensure_ascii=False)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def scan(launch_probe=True, only=None):
    """采集本机清单。launch_probe=True 才会真的拉起窗口验证可启动性。"""
    specs = [spec for spec in APPS if not only or spec['id'] in only]
    tools = []
    for spec in specs:
        entry = dict(id=spec['id'], name=spec['name'], aliases=list(spec['aliases']),
                     process=spec['process'], capabilities=list(spec['capabilities']),
                     automation=spec['automation'], adapter=spec['adapter'],
                     notes=spec['notes'])
        if launch_probe:
            entry.update(probe(spec))
        else:
            exe = _find_exe(spec)
            entry.update(exe=str(exe) if exe else '', exists=bool(exe),
                         launch_ok=None, reason='not_probed')
        tools.append(entry)
    payload = dict(contract=CONTRACT, device=_device(), scanned_at=time.strftime('%Y-%m-%dT%H:%M:%S'),
                   machine=os.environ.get('COMPUTERNAME', ''), tools=tools,
                   spec_signature=_spec_signature(), absent_declared=list(ABSENT_NAMES))
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    temporary = CACHE.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(CACHE)
    _notify_change(payload)
    return payload


def _notify_change(payload):
    """清单落盘后在后台通知已配对的上报器；未配对时什么也不做，绝不阻塞。"""
    reporter = _REPORTER
    if reporter is None:
        return
    threading.Thread(target=_safe_call, args=(reporter, payload), daemon=True).start()


def _safe_call(fn, *args):
    try:
        fn(*args)
    except Exception:
        pass


def set_change_reporter(reporter):
    """LocalAgent 配对成功后注入；传 None 表示下线/未登录。"""
    global _REPORTER
    _REPORTER = reporter


def _device():
    try:
        from core.butler_cloud import load_cloud_cfg
        cfg = load_cloud_cfg()
        if cfg:
            return cfg.get('device', '本机')
    except Exception:
        pass
    return os.environ.get('COMPUTERNAME', '本机')


def inventory(query='', capability=''):
    """可查询清单：只返回真实可用项；未核验的如实标注。"""
    data = json.loads(CACHE.read_text(encoding='utf-8')) if CACHE.exists() else None
    if data and data.get('spec_signature') != _spec_signature():
        data = None
    if not data:
        data = scan(launch_probe=False)
    tools = data.get('tools', [])
    if query:
        key = str(query).casefold()
        tools = [t for t in tools
                 if key in str(t.get('name', '')).casefold()
                 or key in str(t.get('id', '')).casefold()
                 or any(key in a.casefold() for a in t.get('aliases', []))]
    if capability:
        tools = [t for t in tools if capability in (t.get('capabilities') or [])]
    return [dict(t, commandable=t.get('automation') in ('bridge', 'ide_dispatch')) for t in tools]


def commandable_tools():
    """真正能自动指挥的软件（有专用适配器），不含只能界面点的。"""
    return [t for t in inventory() if t.get('commandable') and t.get('launch_ok') is not False]


def _exe_version(exe):
    """读 exe 文件版本（拿不到就留空，不编造）。"""
    try:
        import win32api
        info = win32api.GetFileVersionInfo(str(exe), '\\')
        ms, ls = int(info['FileVersionMS']), int(info['FileVersionLS'])
        return '%d.%d.%d.%d' % (ms >> 16, ms & 0xffff, ls >> 16, ls & 0xffff)
    except Exception:
        return ''


def inventory_items():
    """按服务端 /api/agent/inventory 的字段组装清单。

    只上报真实可启动（launch_ok 为 True 或未否决）且 exe 真实存在的软件；
    启动核验失败的条目不上报，避免另一台电脑按清单派单时扑空。
    """
    data = json.loads(CACHE.read_text(encoding='utf-8')) if CACHE.exists() else scan(launch_probe=False)
    if data.get('spec_signature') != _spec_signature():
        data = scan(launch_probe=False)
    stamp = data.get('scanned_at') or time.strftime('%Y-%m-%dT%H:%M:%S')
    items = []
    for tool in data.get('tools', []):
        if not tool.get('exe') or tool.get('launch_ok') is False:
            continue
        exe = str(tool['exe'])
        items.append(dict(
            name=str(tool.get('name', '')),
            launch=exe,
            version=_exe_version(exe),
            path=str(Path(exe).parent),
            summary=str(tool.get('notes', ''))[:120],
            aliases=[str(a)[:40] for a in (tool.get('aliases') or [])][:8],
            updatedAt=stamp,
        ))
    return items


def report_payload():
    """上报 body：{ items: [...] }，与 POST /api/agent/inventory 契约一致。"""
    return dict(items=inventory_items())


def _signature(items):
    return hashlib.sha1(json.dumps(items, sort_keys=True, ensure_ascii=False)
                        .encode('utf-8')).hexdigest()


def _report_state():
    try:
        return json.loads(REPORT_STATE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def report_agent(token, base_url=None, only_if_changed=True, timeout=20):
    """把本机清单上报到已配对设备（POST /api/agent/inventory，头 x-agent-token）。

    未配对（没有 token）直接跳过，不报错、不阻塞调用方。
    """
    import urllib.error
    import urllib.request
    if not token:
        return dict(ok=True, skipped=True, reason='not_paired',
                    message='本机执行方还没配对，跳过清单上报')
    items = inventory_items()
    signature = _signature(items)
    state = _report_state()
    if only_if_changed and state.get('ok') and state.get('signature') == signature:
        return dict(ok=True, skipped=True, reason='unchanged',
                    deviceId=state.get('deviceId'), count=len(items))
    base = str(base_url or os.environ.get('GUGA_CLOUD_BASE', '')).rstrip('/')
    if not base:
        return dict(ok=False, error='no_base_url', message='没有云端地址，清单只保存在本地')
    body = json.dumps(dict(items=items), ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(base + '/api/agent/inventory', data=body, method='POST')
    request.add_header('Content-Type', 'application/json')
    request.add_header('x-agent-token', str(token))
    try:
        # 云端 JSON POST 保留 POST 重定向，避免 http→https/补斜杠跳转后变 GET 回 405。
        from core.model_endpoint import urlopen_post
        with urlopen_post(request, body, timeout=timeout) as response:
            result = json.loads(response.read().decode('utf-8') or '{}')
        state = dict(ok=True, signature=signature, deviceId=result.get('deviceId', ''),
                     count=int(result.get('count') or len(items)),
                     reported_at=time.strftime('%Y-%m-%dT%H:%M:%S'))
        try:
            REPORT_STATE.parent.mkdir(parents=True, exist_ok=True)
            REPORT_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError:
            pass
        return dict(ok=True, deviceId=state['deviceId'], count=state['count'],
                    updatedAt=result.get('updatedAt'))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode('utf-8') or '{}')
        except (ValueError, OSError):
            detail = {}
        return dict(ok=False, error='agent_rejected', status=exc.code,
                    message=str(detail.get('error') or '配对 token 失效或未配对'))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return dict(ok=False, error='cloud_unreachable', message=str(exc)[:200])


def report(token=None, timeout=20):
    """兼容入口：有显式 token 就直接上报；否则复用已配对执行方注入的上报器。"""
    if token:
        return report_agent(token, timeout=timeout)
    reporter = _REPORTER
    if reporter is not None:
        result = reporter()
        if isinstance(result, dict):
            return result
        return dict(ok=False, error='report_failed',
                    message='清单上报未完成（网络或配对异常）')
    return report_agent('', timeout=timeout)
