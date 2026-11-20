#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探测豆包网页（生图）页面结构，给 core/doubao_image.py 定选择器。

用法:
  python tools/probe_doubao.py            # 无头，只看结构（首次多半是登录页）
  python tools/probe_doubao.py --headful  # 有头，弹出来人扫码登录，登录态存 data/doubao_profile
  python tools/probe_doubao.py --shot out.png
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROFILE = ROOT / 'data' / 'doubao_profile'
TARGET = 'https://www.doubao.com/chat/'


def probe(headful=False, shot=None, wait_ms=9000):
    from playwright.sync_api import sync_playwright

    PROFILE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE), channel='msedge', headless=not headful,
            viewport={'width': 1440, 'height': 950},
            args=['--disable-blink-features=AutomationControlled'],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(TARGET, wait_until='domcontentloaded', timeout=45000)
        page.wait_for_timeout(wait_ms)

        info = {'url': page.url, 'title': page.title()}
        probes = {
            'textarea': 'textarea',
            'contenteditable': '[contenteditable="true"]',
            'file_input': 'input[type=file]',
            'login_hint': 'text=/登录|扫码|二维码/',
            'upload_btn': 'text=/上传|图片|附件/',
            'send_btn': '[data-testid*=send], button:has-text("发送")',
            'imgs': 'img',
        }
        for key, sel in probes.items():
            try:
                info[key] = page.locator(sel).count()
            except Exception as exc:  # noqa: BLE001
                info[key] = 'err:%s' % str(exc)[:60]

        # 输入框/上传口的可见文本，便于判断是否进到了对话页
        texts = []
        try:
            for el in page.locator('textarea, [contenteditable="true"]').all()[:3]:
                ph = el.get_attribute('placeholder') or el.get_attribute('data-placeholder') or ''
                texts.append(ph[:60])
        except Exception:  # noqa: BLE001
            pass
        info['placeholders'] = texts
        info['body_head'] = (page.locator('body').inner_text() or '')[:280].replace('\n', ' | ')

        if shot:
            page.screenshot(path=shot, full_page=False)
            info['shot'] = str(shot)

        print(json.dumps(info, ensure_ascii=False, indent=2))
        ctx.close()
        return info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--headful', action='store_true')
    ap.add_argument('--shot', default=None)
    ap.add_argument('--wait', type=int, default=9000)
    args = ap.parse_args()
    shot = args.shot or str(ROOT / 'data' / 'doubao_probe.png')
    probe(args.headful, shot, args.wait)


if __name__ == '__main__':
    main()
