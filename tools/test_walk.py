# -*- coding: utf-8 -*-
"""走路动作回归测试。

这组测试守的不是"能跑"，而是三件会让走路看起来穿帮的事：

1. 抠图不能把黄色脚当成深红背景一起抠掉（颜色阈值法就会犯这个错）
2. 对齐后每帧内容高度必须一致、底边必须压在同一个 baseline 上
   （逐帧独立缩放会让角色一抽一抽地变大变小）
3. 语音说"向左走"必须解析成 left，"左边的文件"不能被当成指令
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PIL import Image, ImageDraw  # noqa: E402

from core import walk_detach as wd  # noqa: E402
from core.fast_commands import match, parse_walk  # noqa: E402


def gradient_scene(w=240, h=320, feet=True, watermark=True):
    """造一张和豆包视频同构的图：深红渐变背景 + 角色 + 黄脚 + 水印。"""
    yy, xx = np.mgrid[0:h, 0:w]
    # 左上洋红 → 右下深红，与视频里的渐变方向一致
    r = 170 - 70 * (xx / w) - 20 * (yy / h)
    g = 14 + 6 * (yy / h)
    b = 88 - 70 * (xx / w)
    rgb = np.dstack([r, g, b]).clip(0, 255).astype(np.uint8)
    im = Image.fromarray(rgb, 'RGB')
    d = ImageDraw.Draw(im)
    # 身体（深黑灰，低饱和）
    d.ellipse([w * 0.22, h * 0.18, w * 0.78, h * 0.86], fill=(46, 46, 52))
    # 白肚皮
    d.ellipse([w * 0.36, h * 0.46, w * 0.64, h * 0.84], fill=(238, 238, 240))
    # 腮红（浅粉，不能被当背景）
    d.ellipse([w * 0.30, h * 0.44, w * 0.38, h * 0.50], fill=(238, 168, 168))
    if feet:
        # 黄色脚：颜色阈值法会把这个判成"红系背景"，是必须守住的用例
        d.ellipse([w * 0.34, h * 0.82, w * 0.48, h * 0.91], fill=(232, 186, 52))
        d.ellipse([w * 0.52, h * 0.82, w * 0.66, h * 0.91], fill=(232, 186, 52))
    if watermark:
        d.text((w * 0.62, h * 0.95), 'doubao', fill=(220, 220, 220))
    return np.asarray(im)


class DetachTests(unittest.TestCase):
    def test_yellow_feet_survive(self):
        rgb = gradient_scene()
        _cut, alpha = wd.detach(rgb)
        h, w = alpha.shape
        foot = alpha[int(h * 0.85):int(h * 0.90), int(w * 0.38):int(w * 0.46)]
        self.assertGreater(foot.mean(), 0.8, '黄色脚被当成背景抠掉了')

    def test_background_and_watermark_removed(self):
        rgb = gradient_scene()
        _cut, alpha = wd.detach(rgb)
        h, w = alpha.shape
        corner = alpha[:int(h * 0.06), :int(w * 0.06)]
        self.assertLess(corner.mean(), 0.02, '背景没有被抠干净')
        mark = alpha[int(h * 0.93):, int(w * 0.62):]
        self.assertLess(mark.mean(), 0.05, '水印残留了')

    def test_cheek_not_punched_out(self):
        rgb = gradient_scene()
        _cut, alpha = wd.detach(rgb)
        h, w = alpha.shape
        cheek = alpha[int(h * 0.45):int(h * 0.49), int(w * 0.31):int(w * 0.37)]
        self.assertGreater(cheek.mean(), 0.9, '腮红被挖穿了')

    def test_bbox_tracks_content(self):
        rgb = gradient_scene()
        _cut, alpha = wd.detach(rgb)
        box = wd.bbox_of(alpha)
        h, w = alpha.shape
        self.assertIsNotNone(box)
        x0, y0, x1, y1 = box
        self.assertGreater(x1 - x0, w * 0.4)
        self.assertGreater(y1, h * 0.85, '脚没有被算进轮廓')


class AlignTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / 'tools'))
        from build_walk_from_video import CANVAS, BASELINE, plan_frames, render
        self.CANVAS, self.BASELINE = CANVAS, BASELINE
        self.plan_frames, self.render = plan_frames, render

    def _items(self, shifts):
        """同一尺寸的角色，只有水平/垂直位置不同（模拟视频里走动时的位移）。"""
        items = []
        for dx, dy in shifts:
            rgb = gradient_scene()
            cut, alpha = wd.detach(rgb)
            im = Image.fromarray(np.dstack([cut, (alpha * 255).astype(np.uint8)]), 'RGBA')
            moved = Image.new('RGBA', im.size, (0, 0, 0, 0))
            moved.paste(im, (dx, dy), im)
            arr = np.asarray(moved)
            items.append((arr[..., :3].copy(), arr[..., 3].astype(np.float32) / 255.0))
        return items

    def test_frames_share_one_scale(self):
        items = self._items([(0, 0), (-20, 4), (16, -3), (-6, 6)])
        scale, targets, bx = self.plan_frames(items)
        out = self.render(items, scale, targets, bx)
        heights = []
        for frame in out:
            a = np.asarray(frame)[..., 3]
            ys = np.nonzero((a > 60).any(axis=1))[0]
            heights.append(int(ys.max() - ys.min()) if len(ys) else 0)
        self.assertLessEqual(max(heights) - min(heights), 4,
                             '逐帧缩放不一致，角色会忽大忽小：%s' % heights)

    def test_baseline_is_shared(self):
        items = self._items([(0, 0), (-14, 5), (10, -4)])
        scale, targets, bx = self.plan_frames(items)
        out = self.render(items, scale, targets, bx)
        bottoms = []
        for frame in out:
            a = np.asarray(frame)[..., 3]
            ys = np.nonzero((a > 60).any(axis=1))[0]
            bottoms.append(int(ys.max()) if len(ys) else -1)
        self.assertLessEqual(max(bottoms) - min(bottoms), 3,
                             '落地线在漂：%s' % bottoms)

    def test_drift_is_removed_but_wobble_kept(self):
        """角色从左走到右（漂 80px），对齐后中心必须仍然收在画布中轴附近。"""
        items = self._items([(0, 0), (-25, 0), (-50, 0), (-75, 0), (-95, 0)])
        scale, targets, bx = self.plan_frames(items)
        out = self.render(items, scale, targets, bx)
        centers = []
        for frame in out:
            a = np.asarray(frame)[..., 3]
            xs = np.nonzero((a > 60).any(axis=0))[0]
            if len(xs):
                centers.append((int(xs.min()) + int(xs.max())) / 2)
        self.assertLessEqual(max(centers) - min(centers), 12,
                             '整体漂移没被消掉，会在屏幕上横跳：%s' % centers)

    def test_canvas_matches_idle_bundle(self):
        items = self._items([(0, 0)])
        scale, targets, bx = self.plan_frames(items)
        frame = self.render(items, scale, targets, bx)[0]
        self.assertEqual(frame.size, (320, 416))


class CommandTests(unittest.TestCase):
    def test_direction_phrases(self):
        # 走真实链路：先过 command_text 归一化（去掉"你能/帮我"和语气词），再匹配
        for text, want in (('向左走', 'left'), ('往右走', 'right'), ('走到左边', 'left'),
                           ('去右边', 'right'), ('朝左边走', 'left'), ('向右走几步', 'right'),
                           ('你能往左走吗', 'left'), ('给我走到右边', 'right')):
            self.assertEqual(match(text).get('target'), want, text)

    def test_anywhere_and_stop(self):
        for text in ('走一走', '随便走走', '散散步', '走两步'):
            self.assertEqual(parse_walk(text), 'auto', text)
        for text in ('别走了', '停下', '站住', '不走了', '停止走动'):
            self.assertEqual(parse_walk(text), 'stop', text)

    def test_not_a_command(self):
        for text in ('左边', '右边的文件', '打开左边的窗口'):
            self.assertIsNone(parse_walk(text), text)

    def test_match_routes_to_performance(self):
        got = match('向左走')
        self.assertTrue(got.get('matched'))
        self.assertEqual(got.get('action'), 'performance')
        self.assertEqual(got.get('kind'), 'walk')
        self.assertEqual(got.get('target'), 'left')


class ClipTests(unittest.TestCase):
    """只有素材已经构建出来时才检查加载；没构建就跳过，不让测试变成环境依赖。"""

    def setUp(self):
        self.base = ROOT / 'resources' / 'actions' / 'walk'
        if not (self.base / 'manifest.json').is_file():
            self.skipTest('走路素材还没构建')

    def test_animator_loads_both_directions(self):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PyQt5.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        from ui.sprite_animator import SpriteAnimator
        anim = SpriteAnimator(str(ROOT / 'resources'), display_width=250)
        clips = anim.walk_clips
        self.assertIn('left', clips)
        self.assertIn('right', clips)
        self.assertGreater(clips['left'], 8)
        self.assertTrue(anim.play_walk('left'))
        self.assertTrue(anim.walking)
        anim.stop_walk()
        self.assertFalse(anim.walking)
        del app

    def test_frames_are_content_bound(self):
        from core.avatar_review import approved_action
        for name in ('walk_left', 'walk_right'):
            folder = self.base / name / 'frames'
            if folder.is_dir():
                self.assertTrue(approved_action(folder),
                                '%s 的审核记录和帧内容对不上，动画器会静默不加载' % name)


if __name__ == '__main__':
    unittest.main()
