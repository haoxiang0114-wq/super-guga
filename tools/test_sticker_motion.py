# -*- coding: utf-8 -*-
"""补帧 / 贴合 / 动作库回归测试。

重点不是"跑通"，而是把"不穿帮"变成可断言的数字：
轮廓不能因为插值而膨胀（膨胀 = 两个姿态叠在一起 = 鬼影），
落地线不能漂，生成图必须被拉回原口径。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PIL import Image, ImageDraw  # noqa: E402

from core import sticker_motion as motion  # noqa: E402
from core import sticker_conform as conform  # noqa: E402
from core import motion_library as lib  # noqa: E402
from core import sticker_studio as studio  # noqa: E402


def character(size=(120, 120), color=(240, 110, 90, 255), dx=0):
    """造一个不规则形状的"角色"（实心矩形会被判成背景没抠干净）。"""
    im = Image.new('RGBA', size, (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    w, h = size
    d.ellipse([8 + dx, h // 3, w - 8 + dx, h - 6], fill=color)
    d.ellipse([w // 4 + dx, 8, w // 2 + dx, h // 3], fill=color)
    d.ellipse([w // 2 + dx - 4, 10, w * 3 // 4 + dx, h // 3], fill=color)
    return im


def moving_series(n=6, size=(120, 120)):
    frames = []
    for i in range(n):
        im = Image.new('RGBA', size, (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        w, h = size
        shift = int(round(6 * i / max(n - 1, 1)))
        d.ellipse([10 + shift, h // 2, w - 10 + shift, h - 8], fill=(230, 90, 80, 255))
        d.ellipse([w // 3 + shift, 16, w // 2 + shift, h // 2], fill=(230, 90, 80, 255))
        frames.append(im)
    return frames


def alpha_area(img):
    """可见像素数。补帧内部是 float32 数组，这里统一转成 8 位再数。"""
    import numpy as np
    if isinstance(img, Image.Image):
        arr = img.convert('RGBA')
    else:
        arr = Image.fromarray(np.clip(img, 0, 255).astype('uint8'), 'RGBA')
    return int((np.asarray(arr)[..., 3] > 24).sum())


def bbox(img):
    return img.convert('RGBA').getchannel('A').getbbox()


class MotionTests(unittest.TestCase):
    def setUp(self):
        motion._FLOW_CACHE.clear()

    def test_factor_one_is_identity(self):
        frames = moving_series(5)
        out, rep = motion.smooth_frames(frames, factor=1)
        self.assertEqual(len(out), 5)
        self.assertEqual(rep['factor'], 1)

    def test_smooth_doubles_frame_count(self):
        frames = motion.pil_list(moving_series(6))
        out, rep = motion.smooth_frames(frames, factor=2)
        self.assertEqual(len(out), 11)          # (6-1)*2 + 1
        self.assertEqual(rep['pairs'], 5)

    def test_morph_does_not_inflate_silhouette(self):
        """关键断言：运动补偿插值不能让轮廓比交叉淡化更胖（胖 = 重影）。"""
        frames = motion.pil_list(moving_series(6))
        a, b = frames[0], frames[3]
        avg = (alpha_area(a) + alpha_area(b)) / 2.0
        blended = alpha_area(Image.blend(motion._to_image(a), motion._to_image(b), 0.5))
        morphed = alpha_area(motion.morph(a, b, 0.5))
        grow_blend = (blended - avg) / avg
        grow_morph = (morphed - avg) / avg
        self.assertLessEqual(grow_morph, grow_blend + 0.01)

    def test_stabilize_pins_baseline(self):
        """落地线纠偏只做小幅修正（默认 4px），所以这里造 3px 级别的参差。"""
        frames = motion.pil_list(moving_series(6))
        shifted = []
        for i, f in enumerate(frames):
            canvas = motion.np.zeros_like(f)
            dy = (i % 3) * 2
            canvas[dy:] = f[:f.shape[0] - dy] if dy else f
            shifted.append(canvas)
        before = {motion._content_bbox(f)[3] for f in shifted}
        fixed = motion.stabilize(shifted, baseline=motion._content_bbox(shifted[0])[3])
        bottoms = {motion._content_bbox(f)[3] for f in fixed}
        self.assertLessEqual(max(bottoms) - min(bottoms), 1)
        self.assertLess(max(bottoms) - min(bottoms), max(before) - min(before))

    def test_retime_hits_target(self):
        frames = motion.pil_list(moving_series(4))
        self.assertEqual(len(motion.retime(frames, 9)), 9)
        self.assertEqual(len(motion.retime(frames, 3)), 3)

    def test_enrich_doubles_fps_keeps_duration(self):
        frames = moving_series(6)
        out, fps, rep = motion.enrich(frames, fps=12, factor=2)
        self.assertEqual(rep['src_frames'], 6)
        self.assertAlmostEqual(fps, 24.0, places=1)
        # 时长应基本不变（允许 1 帧误差）
        self.assertAlmostEqual(len(out) / fps, 0.5, delta=0.08)

    def test_enrich_respects_fps_cap(self):
        frames = moving_series(6)
        out, fps, rep = motion.enrich(frames, fps=40, factor=3)
        self.assertLessEqual(fps, motion.MAX_FPS_CAP + 0.01)
        self.assertEqual(rep['factor'], 1)      # 40*2 已超 60，只能不补

    def test_quality_reports_baseline(self):
        frames = motion.pil_list(moving_series(5))
        q = motion.quality(frames)
        self.assertEqual(q['frames'], 5)
        self.assertIn('jitter', q)
        self.assertLessEqual(q['baseline_spread'], 0)


class ConformTests(unittest.TestCase):
    def setUp(self):
        self.ref = character((320, 416), (230, 100, 90, 255))
        # 参考帧要贴在落地线上，模拟工程口径
        self.ref_box = bbox(self.ref)

    def _fake_gen(self, size=(800, 800), bg=(255, 255, 224), color=(250, 120, 90, 255), angle=0):
        """模拟豆包输出：米白底、偏大、可能旋转。"""
        big = Image.new('RGBA', size, (0, 0, 0, 0))
        art = character((260, 260), color)
        if angle:
            art = art.rotate(angle, expand=True, resample=Image.BICUBIC)
        big.alpha_composite(art, (size[0] // 2 - art.width // 2, size[1] // 2 - art.height // 2))
        flat = Image.new('RGB', size, bg)
        flat.paste(big, (0, 0), big)
        return flat

    def test_detach_handles_offwhite_background(self):
        """米白底（蓝通道 224）过去被 225 的阈值误判成深色，必须能抠。"""
        gen = self._fake_gen()
        cut = conform.detach(gen)
        self.assertEqual(cut.mode, 'RGBA')
        box = bbox(cut)
        self.assertIsNotNone(box)
        self.assertLess(box[2] - box[0], gen.width - 40)
        self.assertLess(box[3] - box[1], gen.height - 40)

    def test_conform_matches_baseline_and_center(self):
        gen = self._fake_gen()
        out = conform.conform(gen, self.ref)
        box = bbox(out)
        self.assertEqual(out.size, self.ref.size)
        self.assertEqual(box[3], self.ref_box[3])            # 底边对齐
        self.assertLessEqual(abs((box[0] + box[2]) // 2 - (self.ref_box[0] + self.ref_box[2]) // 2), 2)

    def test_conform_pulls_color_back(self):
        import numpy as np
        gen = self._fake_gen(color=(255, 150, 60, 255))     # 明显偏橙
        out = conform.conform(gen, self.ref)
        a = np.asarray(out)[..., 3] > 8
        got = np.asarray(out)[..., :3][a].mean(axis=0)
        want = np.asarray(self.ref)[..., :3][np.asarray(self.ref)[..., 3] > 8].mean(axis=0)
        # 红蓝差应被拉近（不要求完全相等）
        self.assertLess(abs((got[0] - got[2]) - (want[0] - want[2])), 60)

    def test_conform_rejects_empty(self):
        blank = Image.new('RGB', (400, 400), (255, 255, 255))
        with self.assertRaises(conform.ConformError):
            conform.conform(blank, self.ref)

    def test_conform_rejects_wide_composition(self):
        """把角色横向拉成 3 倍宽，模拟"生成了一张拼图"，应当拒绝贴合。"""
        wide = Image.new('RGBA', (900, 300), (0, 0, 0, 0))
        art = character((240, 240), (250, 120, 90, 255)).resize((720, 240), Image.LANCZOS)
        wide.alpha_composite(art, (90, 30))
        flat = Image.new('RGB', (900, 300), (255, 255, 255))
        flat.paste(wide, (0, 0), wide)
        with self.assertRaises(conform.ConformError):
            conform.conform(flat, self.ref)

    def test_similarity_prefers_closer_frame(self):
        good = conform.conform(self._fake_gen(), self.ref)
        bad = conform.conform(self._fake_gen(color=(20, 200, 60, 255)), self.ref)
        self.assertLess(conform.similarity(good, self.ref), conform.similarity(bad, self.ref))
        self.assertIsNotNone(conform.best_of([bad, good], self.ref))


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='guga-motion-'))
        self._saved = lib.STORE_PATH
        lib.STORE_PATH = self.tmp / 'motion_library.json'

    def tearDown(self):
        lib.STORE_PATH = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_record_and_get(self):
        lib.record('abc', '测试', source='x.gif', source_frames=6, out_frames=11,
                   src_fps=12, fps=24, factor=2, quality={'jitter': 1.2})
        item = lib.get('abc')
        self.assertEqual(item['slug'], 'abc')
        self.assertEqual(item['factor'], 2)
        self.assertEqual(item['quality']['jitter'], 1.2)
        self.assertTrue(lib.STORE_PATH.is_file())

    def test_record_keeps_history(self):
        lib.record('abc', '测试', out_frames=11, factor=2, fps=24)
        lib.record('abc', '测试', out_frames=21, factor=3, fps=30)
        item = lib.get('abc')
        self.assertEqual(item['out_frames'], 21)
        self.assertEqual(len(item.get('history', [])), 1)

    def test_summary_and_remove(self):
        self.assertIn('空', lib.summary())
        lib.record('a', 'A', factor=2)
        lib.record('b', 'B', factor=1, doubao_calls=1)
        self.assertIn('2 条', lib.summary())
        self.assertTrue(lib.remove('a'))
        self.assertFalse(lib.remove('a'))
        self.assertIsNone(lib.get('a'))


class DoubaoTests(unittest.TestCase):
    def test_status_without_login_is_not_ready(self):
        """不能因为浏览器写过 cookie 文件就认为已登录（设备标识不是登录态）。"""
        from core import doubao_image as db
        st = db.status()
        self.assertIn('ready', st)
        self.assertIsInstance(st['reason'], str)
        if not st['ready']:
            self.assertFalse(db.available())

    def test_prompt_mentions_style_lock(self):
        from core import doubao_image as db
        text = db.render_motion_prompt('抬手打招呼')
        self.assertIn('抬手打招呼', text)
        self.assertIn('参考图', text)

    def test_login_cookie_names_are_specific(self):
        from core import doubao_image as db
        self.assertIn('sessionid', db.LOGIN_COOKIES)
        self.assertNotIn('ttwid', db.LOGIN_COOKIES)   # 设备标识不能算登录


class StudioSmoothTests(unittest.TestCase):
    """导入链路走真实写盘，但统统落在临时目录。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='guga-sm-'))
        resident = self.tmp / 'resident'
        (resident / 'doze' / 'frames').mkdir(parents=True)
        real = ROOT / 'resources' / 'actions' / 'resident-motion-v2' / 'doze' / 'frames' / '0000.png'
        if real.is_file():
            shutil.copy(real, resident / 'doze' / 'frames' / '0000.png')
        else:
            Image.new('RGBA', (320, 416), (0, 0, 0, 0)).save(resident / 'doze' / 'frames' / '0000.png')
        (resident / 'manifest.json').write_text(json.dumps({
            'schema': 2, 'padded_canvas': True,
            'clips': [{'name': 'doze', 'frames': 'doze/frames', 'native_fps': 24.0,
                       'reference_width': 245, 'weight': 0.8}]}, ensure_ascii=False), encoding='utf-8')
        self._saved = (studio.RESIDENT_DIR, studio.MANIFEST_PATH, studio.LIBRARY_PATH)
        self._saved_lib = lib.STORE_PATH
        studio.RESIDENT_DIR = resident
        studio.MANIFEST_PATH = resident / 'manifest.json'
        studio.LIBRARY_PATH = self.tmp / 'data' / 'sticker_library.json'
        lib.STORE_PATH = self.tmp / 'data' / 'motion_library.json'

        self.gif = self.tmp / '动作.gif'
        frames = moving_series(6, (120, 120))
        frames[0].save(self.gif, save_all=True, append_images=frames[1:],
                       duration=45, loop=0, disposal=2)

    def tearDown(self):
        studio.RESIDENT_DIR, studio.MANIFEST_PATH, studio.LIBRARY_PATH = self._saved
        lib.STORE_PATH = self._saved_lib
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_import_without_smooth_keeps_factor_one(self):
        res = studio.import_sticker(self.gif, '原速表情', 12, 1.0)
        self.assertTrue(res['ok'])
        self.assertEqual(res['smooth'], 1)
        studio.remove_sticker(res['slug'])

    def test_import_with_smooth_doubles_frames(self):
        res = studio.import_sticker(self.gif, '补帧表情', 12, 1.0, smooth=2)
        self.assertTrue(res['ok'])
        self.assertEqual(res['smooth'], 2)
        self.assertGreater(res['frames'], res['source_frames'])
        self.assertGreater(res['fps'], 12)
        saved = len(list(Path(res['dir']).glob('*.png')))
        self.assertEqual(saved, res['frames'])
        entry = lib.get(res['slug'])
        self.assertIsNotNone(entry)
        self.assertEqual(entry['factor'], 2)
        self.assertEqual(entry['out_frames'], res['frames'])
        studio.remove_sticker(res['slug'])
        lib.remove(res['slug'])
        self.assertEqual(studio.list_stickers(), [])

    def test_smooth_import_keeps_clip_registered(self):
        res = studio.import_sticker(self.gif, '补帧待机', 12, 1.0, smooth=2)
        clips = [c['name'] for c in studio.manifest_clips()]
        self.assertIn(res['slug'], clips)
        studio.remove_sticker(res['slug'])
        lib.remove(res['slug'])

    def test_import_needs_no_doubao_login(self):
        """豆包没登录时，doubao=True 也只能降级，不能抛错。"""
        notes = []
        res = studio.import_sticker(self.gif, '降级表情', 12, 1.0, smooth=2,
                                    doubao=True, log=notes.append)
        self.assertTrue(res['ok'])
        self.assertEqual(res['redrawn'], 0)
        studio.remove_sticker(res['slug'])
        lib.remove(res['slug'])


class DispatchTests(unittest.TestCase):
    def test_doubao_status_action(self):
        from ui.agent_runtime import AppRuntime
        rt = AppRuntime.__new__(AppRuntime)
        res = AppRuntime._sticker(rt, {'action': 'doubao_status'})
        self.assertTrue(res['ok'])
        self.assertIn('ready', res)
        self.assertIn('message', res)

    def test_unknown_action_rejected(self):
        from ui.agent_runtime import AppRuntime
        rt = AppRuntime.__new__(AppRuntime)
        res = AppRuntime._sticker(rt, {'action': 'explode'})
        self.assertFalse(res['ok'])
        self.assertIn('未知', res['error'])

    def test_doubao_check_uploads_middle_frame(self):
        """doubao_check 只传图不生图：取中间帧，把结果原样回报。"""
        from ui.agent_runtime import AppRuntime
        from core import doubao_image as db

        tmp = Path(tempfile.mkdtemp(prefix='guga-check-'))
        self.addCleanup(shutil.rmtree, tmp, True)
        gif = tmp / 'c.gif'
        frames = moving_series(5, (120, 120))
        frames[0].save(gif, save_all=True, append_images=frames[1:],
                       duration=40, loop=0, disposal=2)

        seen = {}

        def fake_check(ref, timeout=60, log=None):
            seen['size'] = getattr(ref, 'size', None)
            return {'ok': True, 'uploaded': 1, 'ready': False,
                    'reason': '上传成功', 'shot': 'x.png'}

        saved = db.check_upload
        db.check_upload = fake_check
        try:
            rt = AppRuntime.__new__(AppRuntime)
            res = AppRuntime._sticker(rt, {'action': 'doubao_check', 'source': str(gif)})
        finally:
            db.check_upload = saved
        self.assertTrue(res['ok'])
        self.assertEqual(res['uploaded'], 1)
        self.assertTrue(seen['size'], '应该把一帧真实图片交给 check_upload')

    def test_doubao_check_needs_source(self):
        from ui.agent_runtime import AppRuntime
        rt = AppRuntime.__new__(AppRuntime)
        res = AppRuntime._sticker(rt, {'action': 'doubao_check'})
        self.assertFalse(res['ok'])
        self.assertIn('source', res['error'])

    def test_import_passes_smooth(self):
        from ui.agent_runtime import AppRuntime
        rt = AppRuntime.__new__(AppRuntime)
        rt._sticker_after = lambda r: r
        calls = {}

        def fake_import(source, name, fps, weight, slug, smooth=0, doubao=False,
                        motion_hint='', log=None):
            calls.update(smooth=smooth, doubao=doubao, hint=motion_hint)
            return {'ok': True, 'slug': 'x', 'name': name, 'frames': 10, 'fps': 24,
                    'smooth': smooth, 'redrawn': 0, 'message': 'ok'}

        saved = studio.import_sticker
        studio.import_sticker = fake_import
        try:
            AppRuntime._sticker(rt, {'action': 'import', 'source': 'a.gif', 'name': 'x',
                                     'smooth': 2, 'doubao': True, 'hint': '抬手'})
        finally:
            studio.import_sticker = saved
        self.assertEqual(calls['smooth'], 2)
        self.assertTrue(calls['doubao'])
        self.assertEqual(calls['hint'], '抬手')


if __name__ == '__main__':
    unittest.main()
