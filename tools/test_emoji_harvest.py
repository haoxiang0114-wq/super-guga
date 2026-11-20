# -*- coding: utf-8 -*-
"""表情收件箱回归测试：样本全在临时目录里造，不碰真实桌面和收件箱。"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PIL import Image  # noqa: E402

from core import emoji_harvest as harvest  # noqa: E402


def animated_gif(path, frames=6, size=(80, 80), ext=None):
    imgs = []
    for i in range(frames):
        im = Image.new('RGBA', size, (0, 0, 0, 0))
        im.paste((240, 120, 90, 255), (10 + i, 10, size[0] - 10 - i, size[1] - 10))
        imgs.append(im)
    imgs[0].save(path, save_all=True, append_images=imgs[1:], duration=80, loop=0, disposal=2)
    return path


class HarvestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='guga-emoji-'))
        self.src = self.tmp / 'src'
        self.dest = self.tmp / 'inbox'
        self.src.mkdir()
        self._saved_inbox = harvest.INBOX
        harvest.INBOX = self.dest

    def tearDown(self):
        harvest.INBOX = self._saved_inbox
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── 单文件鉴定 ────────────────────────────────────
    def test_animated_is_collected(self):
        p = animated_gif(self.src / '大笑.gif')
        meta = harvest.probe_image(p)
        ok, kind = harvest.classify(meta, p.stat().st_size, p.stem)
        self.assertTrue(ok)
        self.assertEqual(kind, 'animated')

    def test_photo_is_rejected(self):
        p = self.src / '照片.jpg'
        Image.new('RGB', (2000, 1500), (30, 60, 90)).save(p)
        ok, why = harvest.classify(harvest.probe_image(p), p.stat().st_size, p.stem)
        self.assertFalse(ok)
        self.assertIn(why, ('too_large', 'photo'))

    def test_screenshot_name_is_rejected(self):
        p = self.src / '屏幕截图 2026-01-01.png'
        Image.new('RGB', (300, 300), (250, 250, 250)).save(p)
        ok, why = harvest.classify(harvest.probe_image(p), p.stat().st_size, p.stem)
        self.assertFalse(ok)
        self.assertEqual(why, 'screenshot')

    def test_long_image_is_rejected(self):
        p = self.src / '长图.png'
        Image.new('RGB', (300, 2200), (240, 240, 240)).save(p)
        ok, why = harvest.classify(harvest.probe_image(p), p.stat().st_size, p.stem)
        self.assertFalse(ok)
        self.assertIn(why, ('bad_ratio', 'too_large'))

    def test_disguised_extension_still_recognised(self):
        """QQ 的表情目录里 .gif 里装的可能是 PNG —— 判定不能看扩展名。"""
        p = self.src / '伪装.gif'
        Image.new('RGBA', (120, 120), (0, 0, 0, 0)).save(p, format='PNG')
        meta = harvest.probe_image(p)
        self.assertEqual(meta['fmt'], 'PNG')
        ok, kind = harvest.classify(meta, p.stat().st_size, p.stem)
        self.assertTrue(ok)
        self.assertEqual(kind, 'alpha-static')

    def test_broken_file_is_skipped(self):
        p = self.src / '坏文件.gif'
        p.write_bytes(b'\x00' * 512)
        self.assertIsNone(harvest.probe_image(p))

    # ── 归集 ──────────────────────────────────────────
    def test_harvest_collects_and_dedupes(self):
        animated_gif(self.src / '大笑.gif')
        shutil.copy(self.src / '大笑.gif', self.src / '大笑 副本.gif')
        Image.new('RGBA', (120, 120), (60, 120, 220, 255)).save(self.src / '点赞.png')
        Image.new('RGB', (2000, 1500), (30, 60, 90)).save(self.src / '照片.jpg')

        rep = harvest.harvest([self.src], self.dest)
        self.assertEqual(len(rep['added']), 2)
        self.assertEqual(rep['duplicates'], 1)
        self.assertEqual(rep['scanned'], 4)
        self.assertEqual(len(list(self.dest.iterdir())), 2)
        self.assertTrue(all(Path(it['path']).is_file() for it in rep['added']))

    def test_second_run_adds_nothing(self):
        animated_gif(self.src / '大笑.gif')
        harvest.harvest([self.src], self.dest)
        rep = harvest.harvest([self.src], self.dest)
        self.assertEqual(rep['added'], [])
        self.assertEqual(rep['duplicates'], 1)

    def test_missing_root_is_reported(self):
        rep = harvest.harvest([self.tmp / '不存在的目录'], self.dest)
        self.assertEqual(rep['scanned'], 0)
        self.assertEqual(len(rep['missing_roots']), 1)

    def test_max_files_stops_scan(self):
        for i in range(6):
            animated_gif(self.src / ('表情%d.gif' % i), frames=3)
        rep = harvest.harvest([self.src], self.dest, max_files=3)
        self.assertTrue(rep['truncated'])
        self.assertEqual(rep['scanned'], 3)

    # ── 收件箱 ────────────────────────────────────────
    def test_inbox_items_use_friendly_names(self):
        self.dest.mkdir(parents=True, exist_ok=True)
        animated_gif(self.dest / ('%s_%s.gif' % ('a4537901', 'a4537901c6222593')), frames=3)
        items = harvest.inbox_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['name'], '表情a45379')

    def test_clear_inbox_removes_copies(self):
        animated_gif(self.src / '大笑.gif')
        harvest.harvest([self.src], self.dest)
        self.assertEqual(harvest.clear_inbox(), 1)
        self.assertEqual(list(self.dest.iterdir()), [])
        self.assertTrue(self.dest.is_dir())
        self.assertTrue((self.src / '大笑.gif').is_file(), '原始文件不该被删')

    def test_summary_is_readable(self):
        animated_gif(self.src / '大笑.gif')
        rep = harvest.harvest([self.src], self.dest)
        text = harvest.summary(rep)
        self.assertIn('扫描 1 个文件', text)
        self.assertIn('收进 1 个', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
