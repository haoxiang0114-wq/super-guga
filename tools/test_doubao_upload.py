#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""参考图上传链路的回归测试（不起真豆包，用本地模拟页）。

为什么要单测这一层
------------------
"传图失败"在网页自动化里几乎全是静默的：`set_input_files` 不报错、页面没反应、
后面照样发 prompt，于是豆包画的是一张凭空想的图，回来一贴合就穿帮。
所以这里用三种模拟页面（隐藏 input / 按钮弹选择框 / 只能粘贴）复现豆包可能的
入口形态，确认 core.doubao_image 的 `_upload` 能认出来、并且**校验到位**：
- 传上去要返回张数；
- 页面里没出现缩略图时必须返回 0（不许假装成功）。

用法: python -X utf8 tools/test_doubao_upload.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from core import doubao_image as db  # noqa: E402

PAGE_INPUT = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<div id="thumbs"></div>
<input type="file" accept="image/*" multiple style="display:none">
<script>
document.querySelector('input[type=file]').addEventListener('change', (e) => {
  for (const f of e.target.files) {
    const img = document.createElement('img');
    img.src = URL.createObjectURL(f);
    img.style.width = '72px'; img.style.height = '72px';
    document.getElementById('thumbs').appendChild(img);
  }
});
</script></body></html>"""

PAGE_CHOOSER = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<div id="thumbs"></div>
<button aria-label="上传图片" class="upload-btn">+</button>
<script>
document.querySelector('button').addEventListener('click', () => {
  const input = document.createElement('input');
  input.type = 'file'; input.accept = 'image/*'; input.multiple = true;
  input.style.display = 'none';
  document.body.appendChild(input);
  input.addEventListener('change', (e) => {
    for (const f of e.target.files) {
      const img = document.createElement('img');
      img.src = URL.createObjectURL(f);
      img.style.width = '72px'; img.style.height = '72px';
      document.getElementById('thumbs').appendChild(img);
    }
  });
  input.click();
});
</script></body></html>"""

PAGE_PASTE = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<div contenteditable="true" style="width:600px;height:120px;border:1px solid #ccc"></div>
<div id="thumbs"></div>
<script>
const ed = document.querySelector('[contenteditable]');
ed.addEventListener('paste', (e) => {
  const files = Array.from((e.clipboardData || new DataTransfer()).files || []);
  for (const f of files) {
    const img = document.createElement('img');
    img.src = URL.createObjectURL(f);
    img.style.width = '72px'; img.style.height = '72px';
    document.getElementById('thumbs').appendChild(img);
  }
});
</script></body></html>"""

PAGE_DROP = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<div contenteditable="true" style="width:600px;height:120px;border:1px solid #ccc"></div>
<div id="thumbs"></div>
<script>
const ed = document.querySelector('[contenteditable]');
ed.addEventListener('drop', (e) => {
  for (const f of Array.from(e.dataTransfer.files || [])) {
    const img = document.createElement('img');
    img.src = URL.createObjectURL(f);
    img.style.width = '72px'; img.style.height = '72px';
    document.getElementById('thumbs').appendChild(img);
  }
});
</script></body></html>"""

PAGE_DUD = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<input type="file" accept="image/*" multiple style="display:none">
<button aria-label="上传图片">+</button>
<div contenteditable="true" style="width:600px;height:120px"></div>
<script>/* 什么都不显示：模拟"豆包静默拒收" */</script>
</body></html>"""


def _ref_image(path, size=(240, 240), transparent=True):
    img = Image.new('RGBA', size, (0, 0, 0, 0))
    img.paste((230, 90, 60, 255), (size[0] // 4, size[1] // 4,
                                   size[0] * 3 // 4, size[1] * 3 // 4))
    if not transparent:
        img = img.convert('RGB')
    img.save(path)
    return path


class UploadStrategyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls._pw = sync_playwright().start()
        # 跟线上一致走系统 Edge：项目没装 playwright 自带的 chromium
        cls._browser = cls._pw.chromium.launch(channel='msedge', headless=True)

    @classmethod
    def tearDownClass(cls):
        try:
            cls._browser.close()
        finally:
            cls._pw.stop()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='guga-up-'))
        self.ref = _ref_image(self.tmp / 'ref.png')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _page(self, html, name='page.html'):
        f = self.tmp / name
        f.write_text(html, encoding='utf-8')
        page = self._browser.new_page(viewport={'width': 900, 'height': 600})
        page.goto(f.as_uri())
        self.addCleanup(page.close)
        return page

    def test_hidden_input_upload_counts(self):
        page = self._page(PAGE_INPUT)
        got = db._upload(page, [self.ref], wait=3)
        self.assertEqual(got, 1, '隐藏 file input 应该传成功 1 张')

    def test_button_chooser_upload_counts(self):
        page = self._page(PAGE_CHOOSER)
        got = db._upload(page, [self.ref], wait=3)
        self.assertEqual(got, 1, '按钮弹选择框也要能传上')

    def test_paste_fallback_upload_counts(self):
        page = self._page(PAGE_PASTE)
        got = db._upload(page, [self.ref], wait=3)
        self.assertEqual(got, 1, '粘贴兜底要能传上')

    def test_drop_fallback_upload_counts(self):
        page = self._page(PAGE_DROP)
        got = db._upload(page, [self.ref], wait=3)
        self.assertEqual(got, 1, '拖放兜底要能传上')

    def test_silent_failure_returns_zero(self):
        """页面收下文件却不显示缩略图时，必须报 0，而不是假装成功。"""
        page = self._page(PAGE_DUD)
        got = db._upload(page, [self.ref], wait=3)
        self.assertEqual(got, 0, '没出现缩略图就不能算上传成功')

    def test_multiple_refs(self):
        page = self._page(PAGE_INPUT)
        second = _ref_image(self.tmp / 'ref2.png', size=(260, 200))
        got = db._upload(page, [self.ref, second], wait=3)
        self.assertEqual(got, 2, '两张参考图都要传上去')


class PrepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='guga-prep-'))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_alpha_goes_white(self):
        src = _ref_image(self.tmp / 'a.png')
        out = db.prepare_ref(src, dest_dir=self.tmp, name='a-out')
        img = Image.open(out)
        self.assertEqual(img.mode, 'RGB')
        self.assertEqual(img.getpixel((1, 1)), (255, 255, 255),
                         '透明底必须铺白，否则豆包按黑底理解')

    def test_large_is_scaled_down(self):
        src = self.tmp / 'big.png'
        Image.new('RGB', (2600, 1800), (255, 255, 255)).save(src)
        out = db.prepare_ref(src, dest_dir=self.tmp, name='big-out')
        with Image.open(out) as im:
            self.assertLessEqual(max(im.size), db.PREP_MAX_EDGE)

    def test_tiny_is_scaled_up(self):
        src = self.tmp / 'tiny.png'
        Image.new('RGB', (24, 32), (255, 255, 255)).save(src)
        out = db.prepare_ref(src, dest_dir=self.tmp, name='tiny-out')
        with Image.open(out) as im:
            w, h = im.size
        self.assertGreaterEqual(min(w, h), 24)
        self.assertGreater(min(w, h), 24, '小图应该被放大，不然豆包锁不住风格')

    def test_looks_image_rejects_html(self):
        self.assertFalse(db._looks_image(b'<html>not an image</html>' * 200))

    def test_pair_prompt_mentions_two_refs(self):
        text = db.render_motion_prompt('抬手', pair=True)
        self.assertIn('第一张', text)
        self.assertIn('第二张', text)
        self.assertIn('抬手', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
