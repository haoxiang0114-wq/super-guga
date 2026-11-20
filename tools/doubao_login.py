#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""豆包网页登录辅助：弹出浏览器，等人扫码，登录态写入 data/doubao_profile。

用法: python tools/doubao_login.py [--timeout 300]
成功判据: 页面出现文件上传入口（input[type=file]），且不再显示"登录"按钮。
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROFILE = ROOT / 'data' / 'doubao_profile'
TARGET = 'https://www.doubao.com/chat/'


def dismiss_popups(page):
    """关掉"下载电脑版/领取权益"这类遮挡弹窗。"""
    for sel in ('[class*=close]', 'button:has-text("关闭")', 'svg[class*=close]'):
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 4)):
                try:
                    loc.nth(i).click(timeout=800)
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            continue
    try:
        page.keyboard.press('Escape')
    except Exception:  # noqa: BLE001
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--timeout', type=int, default=300)
    args = ap.parse_args()
    from playwright.sync_api import sync_playwright

    PROFILE.mkdir(parents=True, exist_ok=True)
    shot = ROOT / 'data' / 'doubao_login.png'
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE), channel='msedge', headless=False,
            viewport={'width': 1440, 'height': 950},
            args=['--disable-blink-features=AutomationControlled', '--start-maximized'],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(TARGET, wait_until='domcontentloaded', timeout=45000)
        page.wait_for_timeout(3000)
        dismiss_popups(page)
        page.screenshot(path=str(shot))
        print('[等待] 浏览器已打开，请在弹出的窗口里扫码登录豆包（最多 %d 秒）' % args.timeout, flush=True)

        ok = False
        closed = False
        deadline = time.time() + args.timeout
        beat = 0
        while time.time() < deadline:
            try:
                page.wait_for_timeout(3000)
                avatars = page.locator('[class*=avatar], img[src*=avatar]').count()
                files = page.locator('input[type=file]').count()
                login_btn = page.locator('button:has-text("登录")').count()
                if avatars > 0 or (files > 0 and login_btn == 0):
                    ok = True
                    break
                beat += 1
                if beat % 10 == 0:
                    print('[等待] 还没检测到登录，请在弹出的窗口里完成扫码（剩余 %d 秒）'
                          % max(0, int(deadline - time.time())), flush=True)
            except Exception:  # noqa: BLE001 - 窗口被关掉就体面退出
                closed = True
                break
        try:
            page.screenshot(path=str(shot))
        except Exception:  # noqa: BLE001
            pass
        if ok:
            print('[结果] 登录成功，登录态已写入 profile', flush=True)
        elif closed:
            print('[结果] 浏览器窗口被关闭，本次未完成登录', flush=True)
        else:
            print('[结果] 超时，未检测到登录', flush=True)
        print('[截图] %s' % shot, flush=True)
        print('[profile] %s' % PROFILE, flush=True)
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass
        return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
