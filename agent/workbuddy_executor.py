# -*- coding: utf-8 -*-
"""
咕嘎管家 · WorkBuddy 执行器 v0.1（新软件学习：KB2 无现成剧本，边探边写）
workbuddy-ask 剧本（本次实测后将回写 KB2）：
  唤出窗口 → 新建任务（隔离当前会话）→ 点击输入框 → 粘贴问题 → 发送 → 等回答 → 截图取证
用法：
  python workbuddy_executor.py explore          # 阶段A：点新建任务并截图，确认界面形态
  python workbuddy_executor.py send "问题"      # 阶段B：输入并发送，等回答，取证
安全：FAILSAFE 开启；每次点击带画面校验；全程截图。
"""
import os
import sys
import time
import json

import pyautogui
import pyperclip
import pygetwindow as gw
from PIL import ImageChops

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.4

def _evidence_dir():
    """取证目录必须落在用户可写位置：程序可能装在 Program Files 或只读解压目录。"""
    try:
        _repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _repo not in sys.path:
            sys.path.insert(0, _repo)
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
        pass


def log(msg):
    print(f"[WB执行器 {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def find_window():
    wins = [w for w in gw.getAllWindows() if w.title == "WorkBuddy" and w.width > 400]
    return wins[0] if wins else None


def focus(w):
    try:
        if w.isMinimized:
            w.restore()
        w.activate()
    except Exception as e:
        log(f"activate 失败（{e}）")
    time.sleep(1)


def shot(region):
    return pyautogui.screenshot(region=region).convert("L")


def diff_ok(a, b):
    return ImageChops.difference(a, b).getbbox() is not None


def evidence(w, tag):
    fp = os.path.join(EVIDENCE_DIR, f"{tag}.png")
    pyautogui.screenshot(region=(w.left, w.top, w.width, w.height)).save(fp)
    log(f"证据截图已存: {fp}")
    return fp


def click_checked(w, fx, fy, box_w=200, box_h=60):
    """按窗口百分比坐标点击，点击前后局部画面必须变化才算成功。"""
    cx, cy = w.left + int(w.width * fx), w.top + int(w.height * fy)
    zone = (cx - box_w // 2, cy - box_h // 2, box_w, box_h)
    before = shot(zone)
    pyautogui.click(cx, cy)
    time.sleep(1.2)
    after = shot(zone)
    ok = diff_ok(before, after)
    log(f"点击 ({fx:.0%},{fy:.0%})→({cx},{cy}) 画面变化: {ok}")
    return ok


def wait_answer(w, max_wait=120):
    """等回答开始（画面动）→ 连续 8 秒不动 = 流式结束。采样整窗中下部。"""
    region = (w.left + int(w.width * 0.15), w.top + int(w.height * 0.3),
              int(w.width * 0.6), int(w.height * 0.6))
    started = False
    prev = shot(region)
    stable = 0
    for i in range(max_wait // 2):
        time.sleep(2)
        cur = shot(region)
        changed = diff_ok(prev, cur)
        prev = cur
        if not started:
            if changed:
                started = True
                log(f"检测到回答开始输出（{(i + 1) * 2} 秒）")
            continue
        if changed:
            stable = 0
        else:
            stable += 1
            if stable >= 4:
                log(f"回答画面连续 8 秒无变化，判定输出结束（共 {(i + 1) * 2} 秒）")
                return True
    return False


def phase_explore(w):
    """阶段A：点击「新建任务」并截图，确认新会话界面形态（输入框在哪）。"""
    # 新建任务按钮：侧栏顶部（实测坐标 x≈2.8%, y≈10.2%）
    if not click_checked(w, 0.05, 0.102, box_w=240, box_h=70):
        log("⚠ 点击新建任务后画面无变化（可能已在新建页）")
    time.sleep(1.5)
    return evidence(w, f"wb_explore_{time.strftime('%H%M%S')}")


def phase_send(w, question):
    """阶段B：点输入框 → 粘贴 → 校验 → 发送 → 等回答。
    新任务页输入框在屏幕中央（explore 实测：x≈55%, y≈43%）。"""
    if not click_checked(w, 0.50, 0.425, box_w=300, box_h=70):
        log("聚焦光环不明显（新任务页输入框无强焦点样式），继续以粘贴校验为准")
    zone = (w.left + int(w.width * 0.55) - 150, w.top + int(w.height * 0.43) - 30, 300, 60)
    before = shot(zone)
    pyperclip.copy(question)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.8)
    typed = diff_ok(before, shot(zone))
    log(f"输入框文字校验: {'有字 ✓' if typed else '无变化 ⚠'}")
    if not typed:
        raise RuntimeError("粘贴后输入框无变化，疑似焦点不在输入框，停手")

    pyautogui.press("enter")
    log(f"已发送：「{question}」")
    time.sleep(2)
    ok = wait_answer(w)
    fp = evidence(w, f"wb-ask_{time.strftime('%H%M%S')}")
    return ok, fp


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "explore"
    w = find_window()
    if not w:
        print("未找到 WorkBuddy 窗口"); sys.exit(1)
    focus(w)
    if mode == "explore":
        fp = phase_explore(w)
        print(json.dumps({"phase": "explore", "evidence": fp}, ensure_ascii=False))
    elif mode == "send":
        question = sys.argv[2] if len(sys.argv) > 2 else "用一句话介绍一下你自己"
        ok, fp = phase_send(w, question)
        print(json.dumps({"phase": "send", "question": question, "success": ok, "evidence": fp},
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
