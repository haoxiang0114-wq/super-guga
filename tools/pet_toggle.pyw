# -*- coding: utf-8 -*-
"""语音智能体（咕嘎企鹅桌宠）桌面开关。

桌面快捷方式指向本文件，用 venv 的 pythonw 运行，不弹黑框：

- 没在运行  → 启动桌宠
- 已在运行  → 让它自己正常退出（先走本机管道，_quit() 会把位置和状态存盘；
              管道不通再退化成给窗口发 WM_CLOSE，实在不行才结束进程）

退出优先走"礼貌"路径，是为了不丢桌宠位置、饥饿度/心情/精力这些存盘数据。
"""
import ctypes
import getpass
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYW = ROOT / '.venv' / 'Scripts' / 'pythonw.exe'
PY = ROOT / '.venv' / 'Scripts' / 'python.exe'
LAUNCHER = ROOT / 'launcher.py'
APP_MUTEX = 'Local\\GugaPenguinPet.SingleInstance'
MARK = ROOT / 'runtime' / 'pet_toggle.json'
TITLE = '咕嘎企鹅桌宠'


# ── 基础工具 ────────────────────────────────────────────

def _msgbox(text, flags=0x30):
    try:
        ctypes.windll.user32.MessageBoxW(None, text, TITLE, flags)
    except Exception:
        pass


def _pipe_name():
    """和 core.mcp_contract.pipe_name() 保持同一算法，避免 import 依赖。"""
    override = os.environ.get('GUGA_MCP_PIPE', '')
    if override.startswith('guga-self-'):
        return override
    source = str(ROOT).lower() + getpass.getuser()
    return 'guga-self-' + hashlib.sha256(source.encode('utf-8')).hexdigest()[:20]


def _mutex_alive():
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.OpenMutexW.restype = ctypes.c_void_p
    handle = k32.OpenMutexW(0x00100000, False, APP_MUTEX)   # SYNCHRONIZE
    if handle:
        k32.CloseHandle(ctypes.c_void_p(handle))
        return True
    return False


def _pids():
    """找出真正跑着 launcher.py 的 python 进程。"""
    try:
        import psutil
    except Exception:
        return []
    out = []
    for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'cwd']):
        try:
            name = (proc.info.get('name') or '').lower()
            if not name.startswith('python'):
                continue
            cmd = ' '.join(proc.info.get('cmdline') or [])
            if 'launcher.py' not in cmd:
                continue
            cwd = proc.info.get('cwd') or ''
            if str(ROOT).lower() not in cmd.lower() and Path(cwd or '\\').resolve() != ROOT:
                continue
            out.append(proc.info['pid'])
        except Exception:      # 进程刚好退出 / 没权限读 cwd
            continue
    return out


def is_running():
    return _mutex_alive() or bool(_pids())


# ── 退出：管道 → WM_CLOSE → 结束进程 ─────────────────────

def _pipe_quit():
    """把 {"name":"pet_control","action":"quit"} 写进本机管道；成功返回 True。"""
    name = _pipe_name()
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                ctypes.c_void_p]
    k32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32,
                              ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    handle = k32.CreateFileW('\\\\.\\pipe\\' + name, 0xC0000000, 0, None, 3, 0, None)
    if not handle or handle == ctypes.c_void_p(-1).value:
        return False
    try:
        payload = b'{"name":"pet_control","arguments":{"action":"quit"}}\n'
        written = ctypes.c_uint32(0)
        ok = k32.WriteFile(ctypes.c_void_p(handle), payload, len(payload),
                           ctypes.byref(written), None)
        return bool(ok) and written.value == len(payload)
    finally:
        k32.CloseHandle(ctypes.c_void_p(handle))


def _post_wm_close(pid):
    """给该进程的顶层窗口发 WM_CLOSE（Qt 会走 closeEvent → _quit）。"""
    user32 = ctypes.windll.user32
    target = ctypes.c_ulong()
    hit = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _enum(hwnd, _lparam):
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(owner))
        if owner.value == pid:
            hit.append(hwnd)
        return True

    user32.EnumWindows(_enum, None)
    for hwnd in hit:
        user32.PostMessageW(ctypes.c_void_p(hwnd), 0x0010, 0, 0)   # WM_CLOSE
    return len(hit)


def _wait_gone(seconds, step=0.12):
    end = time.time() + seconds
    while time.time() < end:
        if not is_running():
            return True
        time.sleep(step)
    return not is_running()


def stop():
    """返回 (是否关掉, 走的哪条路径)。"""
    if not is_running():
        return True, '本来就没运行'

    used = ''
    if _pipe_quit():
        used = '管道'
        if _wait_gone(8):
            return True, used
    for pid in _pids():
        if _post_wm_close(pid):
            used = used or '窗口消息'
    if _wait_gone(5):
        return True, used or '窗口消息'

    left = _pids()
    for pid in left:
        try:
            import psutil
            psutil.Process(pid).terminate()
        except Exception:
            pass
    if _wait_gone(4):
        return True, '强制结束进程（下次启动位置可能回退）'
    return False, ('关不掉：仍有进程在跑（%s）。\n\n如果 ai_config.json 里开了 run_as_admin，'
                   '桌宠是管理员权限，需要以管理员身份再点一次这个快捷方式。' % (left or '未知'))


# ── 启动 ───────────────────────────────────────────────

def _recently_started(window=20):
    """刚点过就再点一下时，别急着判"没运行"又开一只。"""
    try:
        import json
        stamp = json.loads(MARK.read_text(encoding='utf-8')).get('started_at', 0)
        return (time.time() - float(stamp)) < window
    except Exception:
        return False


def _mark_started():
    try:
        import json
        MARK.parent.mkdir(parents=True, exist_ok=True)
        MARK.write_text(json.dumps({'started_at': time.time()}, ensure_ascii=False),
                        encoding='utf-8')
    except Exception:
        pass


def start():
    exe = PYW if PYW.exists() else PY
    if not exe.exists() or not LAUNCHER.exists():
        _msgbox('找不到启动文件：\n%s\n\n项目是不是被移动过了？' % LAUNCHER, 0x10)
        return False
    _mark_started()
    flags = 0x00000008 | 0x00000200          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen([str(exe), str(LAUNCHER)], cwd=str(ROOT),
                         creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        _msgbox('启动失败：\n%s' % exc, 0x10)
        return False

    # 等桌宠真正起来（单实例锁出现即算成功）
    end = time.time() + 25
    while time.time() < end:
        if _mutex_alive() or _pids():
            return True
        time.sleep(0.2)
    tail = ''
    log = ROOT / 'startup.log'
    try:
        lines = log.read_text(encoding='utf-8', errors='replace').strip().splitlines()
        tail = '\n'.join(lines[-12:])
    except Exception:
        pass
    _msgbox('桌宠没能起来。startup.log 末尾：\n\n%s' % (tail or '（空）'), 0x10)
    return False


# ── 主流程 ─────────────────────────────────────────────

def main():
    if _recently_started(2.5):
        # 双击的第二次点击（桌宠正在启动）直接忽略，否则会"刚开就关"
        return 0
    mutex = _mutex_alive()
    pids = _pids()
    if not mutex and not pids:
        return 0 if start() else 1
    if not mutex and _recently_started(15):
        # 进程刚起来、单实例锁还没建好：这是上一次点击的启动过程，忽略
        return 0
    ok, how = stop()
    if not ok:
        _msgbox(how, 0x10)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
