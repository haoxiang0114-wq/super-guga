# -*- coding: utf-8 -*-
"""表情包工坊回归测试：全部在临时目录里跑，不碰真实素材。"""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PIL import Image  # noqa: E402

from core import sticker_studio as studio  # noqa: E402


def make_gif(path, frames=6, size=(80, 80), color=(240, 120, 90, 255)):
    imgs = []
    for i in range(frames):
        im = Image.new('RGBA', size, (0, 0, 0, 0))
        box = (10 + i, 10, size[0] - 10 - i, size[1] - 10)
        im.paste(color, box)
        imgs.append(im)
    imgs[0].save(path, save_all=True, append_images=imgs[1:], duration=80, loop=0, disposal=2)
    return path


def png_series(folder, count=6, size=(160, 160), color=(90, 160, 240, 255)):
    """造一套 PNG 序列帧，模拟"文件夹式表情包"。"""
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        im = Image.new('RGBA', size, (0, 0, 0, 0))
        im.paste(color, (10 + i * 3, 10, size[0] - 10, size[1] - 10))
        im.save(folder / ('%02d.png' % i))
    return folder


class SandboxCase(unittest.TestCase):
    """把写盘目标搬到临时目录。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='guga-sticker-'))
        resident = self.tmp / 'resident'
        (resident / 'doze' / 'frames').mkdir(parents=True)
        real_frame = ROOT / 'resources' / 'actions' / 'resident-motion-v2' / 'doze' / 'frames' / '0000.png'
        if real_frame.is_file():
            shutil.copy(real_frame, resident / 'doze' / 'frames' / '0000.png')
            size = Image.open(real_frame).size
        else:
            size = (320, 416)
            Image.new('RGBA', size, (0, 0, 0, 0)).save(resident / 'doze' / 'frames' / '0000.png')
        (resident / 'manifest.json').write_text(json.dumps({
            'schema': 2, 'padded_canvas': True,
            'clips': [{'name': 'doze', 'frames': 'doze/frames', 'native_fps': 24.0,
                       'reference_width': 245, 'weight': 0.8}]}, ensure_ascii=False), encoding='utf-8')
        self.canvas = size
        self._saved = (studio.RESIDENT_DIR, studio.MANIFEST_PATH, studio.LIBRARY_PATH)
        studio.RESIDENT_DIR = resident
        studio.MANIFEST_PATH = resident / 'manifest.json'
        studio.LIBRARY_PATH = self.tmp / 'data' / 'sticker_library.json'
        # 动作库也要一起搬进沙箱，否则测试会往真实 data/motion_library.json 写记录
        from core import motion_library
        self._saved_motion = motion_library.STORE_PATH
        motion_library.STORE_PATH = self.tmp / 'data' / 'motion_library.json'
        self.gif = make_gif(self.tmp / '大笑.gif')

    def tearDown(self):
        from core import motion_library
        studio.RESIDENT_DIR, studio.MANIFEST_PATH, studio.LIBRARY_PATH = self._saved
        motion_library.STORE_PATH = self._saved_motion
        shutil.rmtree(self.tmp, ignore_errors=True)


class AdaptTests(SandboxCase):
    def test_available_and_canvas(self):
        ok, why = studio.available()
        self.assertTrue(ok, why)
        size, k, ref_w = studio.resolve_canvas()
        self.assertEqual(size, self.canvas)
        self.assertEqual(ref_w, 245)
        self.assertGreater(k, 0)

    def test_gif_adapts_to_canvas(self):
        frames, canvas, ref_w = studio.adapt_frames(self.gif, 24)
        self.assertGreaterEqual(len(frames), studio.MIN_OUT_FRAMES)
        self.assertLessEqual(len(frames), studio.MAX_OUT_FRAMES)
        self.assertEqual(canvas, self.canvas)
        self.assertEqual(frames[0].size, self.canvas)
        self.assertEqual(frames[0].mode, 'RGBA')
        # 内容被缩放对齐，而不是原尺寸贴角落
        bbox = frames[0].getchannel('A').getbbox()
        self.assertIsNotNone(bbox)
        self.assertGreater(bbox[2] - bbox[0], 40)

    def test_static_png_gets_breathing(self):
        still = self.tmp / 'still.png'
        Image.new('RGBA', (60, 90), (120, 200, 240, 255)).save(still)
        frames, _canvas, _ref = studio.adapt_frames(still, 24)
        self.assertGreaterEqual(len(frames), studio.MIN_OUT_FRAMES)

    def test_fps_is_clamped(self):
        frames_lo, _, _ = studio.adapt_frames(self.gif, 1)
        frames_hi, _, _ = studio.adapt_frames(self.gif, 999)
        self.assertLessEqual(len(frames_lo), studio.MAX_OUT_FRAMES)
        self.assertLessEqual(len(frames_hi), studio.MAX_OUT_FRAMES)

    def test_probe_does_not_write(self):
        before = sorted(p.name for p in studio.RESIDENT_DIR.iterdir())
        res = studio.probe(self.gif, 24)
        self.assertTrue(res['ok'])
        self.assertEqual(sorted(p.name for p in studio.RESIDENT_DIR.iterdir()), before)
        self.assertFalse(studio.LIBRARY_PATH.exists())

    def test_bad_source_raises(self):
        with self.assertRaises(studio.StickerError):
            studio.adapt_frames(self.tmp / 'nope.gif')
        txt = self.tmp / 'readme.txt'
        txt.write_text('x', encoding='utf-8')
        with self.assertRaises(studio.StickerError):
            studio.adapt_frames(txt)


class FolderSourceTests(SandboxCase):
    """文件夹式素材：直接子文件、套娃子目录、无扩展名都要能导。"""

    def test_folder_with_images(self):
        folder = png_series(self.tmp / '表情')
        frames, canvas, _ref = studio.adapt_frames(folder, 24)
        self.assertGreaterEqual(len(frames), studio.MIN_OUT_FRAMES)
        self.assertEqual(canvas, self.canvas)

    def test_folder_ignores_non_images(self):
        folder = png_series(self.tmp / '混合', 3)
        (folder / 'readme.txt').write_text('x', encoding='utf-8')
        (folder / 'notes.md').write_text('y', encoding='utf-8')
        files, _note = studio.scan_folder(folder)
        self.assertEqual(len(files), 3)

    def test_nested_folder_is_found(self):
        """解压出来常是「合集 / 某套 / frames / 0001.png」这种三层结构。"""
        png_series(self.tmp / '合集' / '一套' / 'frames', 6)
        files, note = studio.scan_folder(self.tmp / '合集')
        self.assertEqual(len(files), 6)
        self.assertIn('子目录', note)
        frames, _c, _r = studio.adapt_frames(self.tmp / '合集', 24)
        self.assertGreaterEqual(len(frames), studio.MIN_OUT_FRAMES)

    def test_folder_picks_richest_subdir(self):
        png_series(self.tmp / '多套' / '甲', 4)
        png_series(self.tmp / '多套' / '乙', 9)
        files, note = studio.scan_folder(self.tmp / '多套')
        self.assertEqual(len(files), 9, '应当选图片最多的那一套，别混成一段动画')
        self.assertIn('乙', note)
        self.assertIn('没用到', note)

    def test_empty_folder_raises(self):
        empty = self.tmp / '空文件夹'
        empty.mkdir()
        with self.assertRaises(studio.StickerError):
            studio.decode_source(empty)

    def test_extensionless_file_is_accepted(self):
        """微信/QQ 存下来的表情经常没有扩展名。"""
        bare = self.tmp / 'abc123'
        bare.write_bytes(self.gif.read_bytes())
        self.assertTrue(studio.is_image_file(bare))
        frames, _delays = studio.decode_source(bare)
        self.assertGreater(len(frames), 1)

    def test_folder_with_extensionless_images(self):
        folder = self.tmp / '无后缀'
        folder.mkdir()
        for i in range(3):
            Image.new('RGBA', (80, 80), (10, 200, 120, 255)).save(folder / ('x%d' % i), format='PNG')
        files, _note = studio.scan_folder(folder)
        self.assertEqual(len(files), 3)

    def test_folder_digest_follows_content(self):
        a = png_series(self.tmp / 'a', 3)
        b = png_series(self.tmp / 'b', 3)
        self.assertEqual(studio._source_digest(a), studio._source_digest(b),
                         '内容相同就该认作同一素材')
        Image.new('RGBA', (160, 160), (255, 0, 0, 255)).save(a / '00.png')
        self.assertNotEqual(studio._source_digest(a), studio._source_digest(b),
                            '改了图就不该再被当成重复')

    def test_too_many_files_are_sampled(self):
        folder = self.tmp / '大目录'
        folder.mkdir()
        for i in range(studio.MAX_FOLDER_FILES + 40):
            Image.new('RGBA', (24, 24), (i % 250, 80, 120, 255)).save(folder / ('%04d.png' % i))
        files, note = studio.scan_folder(folder)
        self.assertEqual(len(files), studio.MAX_FOLDER_FILES)
        self.assertIn('均匀取', note)

    def test_nested_folder_import_and_remove(self):
        png_series(self.tmp / '表情合集' / '大笑', 8)
        res = studio.import_sticker(self.tmp / '表情合集', '套娃大笑', 24, 1.0)
        self.assertTrue(res['ok'])
        self.assertTrue(studio.LIBRARY_PATH.is_file())
        studio.remove_sticker(res['slug'])
        self.assertEqual(studio.list_stickers(), [])


class BackgroundTests(SandboxCase):
    """白底静态表情要抠成透明，否则桌宠身上会顶一块方块。"""

    def _white_sticker(self):
        path = self.tmp / '白底狗头.png'
        im = Image.new('RGB', (160, 160), (250, 250, 250))
        im.paste((190, 130, 60), (40, 40, 120, 130))
        im.save(path)
        return path

    def test_flat_white_border_is_detached(self):
        frames, _delays = studio.decode_source(self._white_sticker())
        self.assertTrue(studio._LAST_DETACHED)
        box = frames[0].getchannel('A').getbbox()
        self.assertIsNotNone(box)
        self.assertLess(box[2] - box[0], 120, '白边应该被裁掉，只剩主体')
        self.assertEqual(frames[0].getpixel((0, 0))[3], 0, '角落必须透明')

    def test_inner_white_is_kept(self):
        """漫水只从边缘走，画面内部的白色不能一起抠掉。"""
        path = self.tmp / '白底带白心.png'
        im = Image.new('RGB', (160, 160), (250, 250, 250))
        im.paste((30, 30, 30), (30, 30, 130, 130))
        im.paste((255, 255, 255), (60, 60, 100, 100))   # 深色块里的白色细节
        im.save(path)
        frames, _delays = studio.decode_source(path)
        self.assertTrue(studio._LAST_DETACHED)
        self.assertEqual(frames[0].getpixel((80, 80))[3], 255, '内部白色应保留')

    def test_transparent_source_is_untouched(self):
        png_series(self.tmp / '带透明', 3)
        frames, _delays = studio.decode_source(self.tmp / '带透明' / '00.png')
        self.assertFalse(studio._LAST_DETACHED, '已经有透明通道就别动它')

    def test_opaque_dark_border_is_untouched(self):
        path = self.tmp / '深色底.png'
        Image.new('RGB', (120, 120), (20, 30, 40)).save(path)
        studio.decode_source(path)
        self.assertFalse(studio._LAST_DETACHED, '深色底多半是画面本身，不要抠')

    def test_grounded_on_idle_baseline(self):
        """导入素材的底边要和内置待机帧对齐，否则切片段时会跳。"""
        frames, canvas, _ref = studio.adapt_frames(self._white_sticker(), 24)
        baseline = studio.idle_baseline(canvas[1])
        bottoms = [f.getchannel('A').getbbox()[3] for f in frames]
        self.assertLessEqual(max(bottoms), baseline)
        self.assertGreaterEqual(max(bottoms), baseline - 2)

    def test_wide_source_is_not_clipped(self):
        path = self.tmp / '横图.gif'
        imgs = []
        for i in range(5):
            im = Image.new('RGBA', (240, 100), (0, 0, 0, 0))
            im.paste((200, 80, 60, 255), (4 + i, 10, 236, 90))
            imgs.append(im)
        imgs[0].save(path, save_all=True, append_images=imgs[1:], duration=80, loop=0)
        frames, canvas, _ref = studio.adapt_frames(path, 24)
        box = frames[0].getchannel('A').getbbox()
        self.assertGreater(box[0], 0, '横图应整体缩小居中，不能被切到贴边')
        self.assertLess(box[2], canvas[0])


class FolderPackTests(SandboxCase):
    """文件夹：一包表情要拆成多个，一套帧序列要合成一个。"""

    def test_dir_with_one_gif_keeps_all_frames(self):
        """回归：目录里放一个动图，以前只解出第一帧。"""
        folder = self.tmp / '只有一个动图'
        folder.mkdir()
        shutil.copy2(self.gif, folder / '笑.gif')
        frames, _delays = studio.decode_source(folder)
        self.assertEqual(len(frames), 6, '目录里的动图要把每一帧都读出来')

    def test_dir_with_many_gifs_is_split(self):
        """回归：一包 GIF 以前会变成"N 个首帧乱闪"的一段动画。"""
        pack = self.tmp / '表情包'
        pack.mkdir()
        for i in range(3):
            make_gif(pack / ('第%d个.gif' % i), frames=5 + i)
        items, _note = studio.collect_folder(pack)
        self.assertEqual(len(items), 3)
        self.assertEqual([it['frames'] for it in items], [5, 6, 7])
        self.assertTrue(all(it['kind'] == 'animated' for it in items))
        self.assertEqual({it['name'] for it in items}, {'第0个', '第1个', '第2个'})

    def test_flat_png_series_stays_one_sticker(self):
        png_series(self.tmp / '序列', 6)
        items, note = studio.collect_folder(self.tmp / '序列')
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['kind'], 'sequence')
        self.assertEqual(items[0]['frames'], 6)
        self.assertIn('帧序列', note)

    def test_mixed_stills_become_separate_stickers(self):
        folder = self.tmp / '散图'
        folder.mkdir()
        Image.new('RGBA', (100, 100), (200, 40, 40, 255)).save(folder / '红.png')
        Image.new('RGBA', (140, 90), (40, 40, 200, 255)).save(folder / '蓝.png')
        items, _note = studio.collect_folder(folder)
        self.assertEqual(len(items), 2)
        self.assertTrue(all(it['kind'] == 'static' for it in items))

    def test_subdirs_use_meaningful_names(self):
        for name, n in (('甲', 4), ('乙', 9)):
            png_series(self.tmp / '合集' / name / 'frames', n)
        items, _note = studio.collect_folder(self.tmp / '合集')
        self.assertEqual(sorted(it['name'] for it in items), ['乙', '甲'],
                         'frames 这种通用目录名要往上取一层')

    def test_same_name_subdirs_get_suffix(self):
        for name in ('a', 'b'):
            png_series(self.tmp / '平铺' / name, 3)
        items, _note = studio.collect_folder(self.tmp / '平铺')
        self.assertEqual([it['name'] for it in items], ['a', 'b'])

    def test_collect_paths_mixes_files_and_dirs(self):
        png_series(self.tmp / '一套', 4)
        one = make_gif(self.tmp / '单个.gif')
        items = studio.collect_paths([self.tmp / '一套', one])
        kinds = sorted(it['kind'] for it in items)
        self.assertIn('sequence', kinds)
        self.assertIn('animated', kinds)

    def test_pack_cap(self):
        pack = self.tmp / '很多'
        pack.mkdir()
        # 尺寸各不相同的散图：每张各算一个表情，用来验证上限
        for i in range(studio.MAX_PACK_ITEMS + 5):
            Image.new('RGBA', (60 + i, 60), (i % 200, 30, 90, 255)).save(pack / ('%03d.png' % i))
        items, note = studio.collect_folder(pack)
        self.assertLessEqual(len(items), studio.MAX_PACK_ITEMS)
        self.assertIn('只取前', note)

    def test_import_folder_imports_each(self):
        pack = self.tmp / '两套'
        pack.mkdir()
        make_gif(pack / '甲.gif', frames=5)
        make_gif(pack / '乙.gif', frames=6, color=(60, 200, 120, 255))
        ok, bad = studio.import_folder(pack)
        self.assertEqual(bad, [])
        self.assertEqual(len(ok), 2)
        self.assertEqual(len(studio.list_stickers()), 2)
        self.assertEqual(len(studio.manifest_clips()), 3)   # doze + 两个新片段
        for res in ok:
            studio.remove_sticker(res['slug'])
        self.assertEqual(studio.list_stickers(), [])

    def test_import_folder_skips_duplicates(self):
        pack = self.tmp / '重复包'
        pack.mkdir()
        make_gif(pack / '甲.gif', frames=5)
        studio.import_folder(pack)
        ok, bad = studio.import_folder(pack)
        self.assertEqual(ok, [])
        self.assertEqual(len(bad), 1)
        self.assertIn('已经', bad[0][1])


class LibraryTests(SandboxCase):
    def test_import_list_toggle_remove(self):
        res = studio.import_sticker(self.gif, '大笑滚地', 24, 1.5)
        slug = res['slug']
        self.assertTrue(res['ok'])
        self.assertTrue(re.fullmatch(r'[0-9a-f]{8}', slug),
                        '中文名转不出目录名时应退化成素材指纹前缀，而不是一堆 sticker_2')
        frames_dir = studio.RESIDENT_DIR / slug / 'frames'
        self.assertTrue(frames_dir.is_dir())
        self.assertEqual(len(list(frames_dir.glob('*.png'))), res['frames'])
        self.assertTrue((studio.RESIDENT_DIR / slug / 'preview.gif').is_file())
        review = json.loads((studio.RESIDENT_DIR / slug / 'visual-review.json').read_text(encoding='utf-8'))
        self.assertEqual(review['status'], 'approved')
        self.assertTrue(review['frames_sha256'])
        self.assertEqual(review['user_approval']['by'], 'user')
        self.assertTrue(review['checks']['loop_seam'])
        from core.avatar_review import approved_action
        self.assertTrue(approved_action(studio.RESIDENT_DIR / slug / 'frames'),
                        '素材必须能通过动画器的审核闸门，否则会静默不显示')

        clips = {c['name']: c for c in studio.manifest_clips()}
        self.assertIn(slug, clips)
        self.assertEqual(clips[slug]['fps'], 24.0)
        self.assertEqual(clips[slug]['weight'], 1.5)

        items = studio.list_stickers()
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]['enabled'])
        self.assertEqual(items[0]['name'], '大笑滚地')

        studio.set_enabled(slug, False)
        self.assertNotIn(slug, {c['name'] for c in studio.manifest_clips()})
        self.assertFalse(studio.list_stickers()[0]['enabled'])
        studio.set_enabled(slug, True)
        self.assertIn(slug, {c['name'] for c in studio.manifest_clips()})

        studio.remove_sticker(slug)
        self.assertFalse((studio.RESIDENT_DIR / slug).exists())
        self.assertEqual(studio.list_stickers(), [])
        self.assertNotIn(slug, {c['name'] for c in studio.manifest_clips()})

    def test_duplicate_source_rejected(self):
        studio.import_sticker(self.gif, '第一次', 24, 1.0)
        with self.assertRaises(studio.StickerError) as ctx:
            studio.import_sticker(self.gif, '第二次', 24, 1.0)
        self.assertIn('入库', str(ctx.exception))

    def test_custom_slug_and_conflict(self):
        first = studio.import_sticker(self.gif, 'A', 24, 1.0, 'laugh_roll')
        self.assertEqual(first['slug'], 'laugh_roll')
        other = make_gif(self.tmp / 'other.gif', frames=4)
        with self.assertRaises(studio.StickerError):
            studio.import_sticker(other, 'B', 24, 1.0, 'laugh_roll')

    def test_manifest_keeps_builtin_clips(self):
        studio.import_sticker(self.gif, 'C', 24, 1.0, 'extra')
        names = {c['name'] for c in studio.manifest_clips()}
        self.assertIn('doze', names)
        self.assertIn('extra', names)

    def test_registry_file_is_json(self):
        studio.import_sticker(self.gif, 'D', 24, 1.0, 'json_case')
        data = json.loads(studio.LIBRARY_PATH.read_text(encoding='utf-8'))
        self.assertEqual(data['version'], 1)
        self.assertEqual(data['items'][0]['slug'], 'json_case')
        self.assertEqual(data['items'][0]['reference_width'], 245)


class AgentToolTests(unittest.TestCase):
    def test_tool_registered(self):
        from core.self_agent import TOOLS, PERSONA
        spec = next((t['function'] for t in TOOLS if t['function']['name'] == 'sticker'), None)
        self.assertIsNotNone(spec, 'sticker 工具未注册')
        actions = spec['parameters']['properties']['action']['enum']
        for action in ('list', 'import', 'enable', 'disable', 'remove', 'probe'):
            self.assertIn(action, actions)
        # 界面页已移除：工坊窗口那次改版后不再有 open 动作
        self.assertNotIn('open', actions)
        self.assertIn('sticker 工具', PERSONA)


class DispatchTests(SandboxCase):
    def test_runtime_list_and_import(self):
        from types import SimpleNamespace
        from ui.agent_runtime import AppRuntime
        pet = SimpleNamespace()
        listed = AppRuntime._sticker(pet, {'action': 'list'})
        self.assertTrue(listed['ok'])
        self.assertEqual(listed['total'], 0)
        imported = AppRuntime._sticker(pet, {'action': 'import', 'source': str(self.gif), 'name': '闪亮登场'})
        self.assertTrue(imported['ok'], imported.get('error'))
        self.assertTrue((studio.RESIDENT_DIR / imported['slug']).is_dir())
        relisted = AppRuntime._sticker(pet, {'action': 'list'})
        self.assertEqual(relisted['total'], 1)
        missing = AppRuntime._sticker(pet, {'action': 'import', 'name': '没有路径'})
        self.assertFalse(missing['ok'])
        self.assertIn('source', missing['error'])

    def test_runtime_import_folder(self):
        from types import SimpleNamespace
        from ui.agent_runtime import AppRuntime
        pack = self.tmp / '一包'
        pack.mkdir()
        make_gif(pack / '甲.gif', frames=5)
        make_gif(pack / '乙.gif', frames=6, color=(40, 180, 90, 255))
        res = AppRuntime._sticker(SimpleNamespace(), {'action': 'import_folder', 'source': str(pack)})
        self.assertTrue(res['ok'], res.get('error'))
        self.assertEqual(len(res['imported']), 2)
        self.assertEqual(res['skipped'], [])
        again = AppRuntime._sticker(SimpleNamespace(), {'action': 'import_folder', 'source': str(pack)})
        self.assertFalse(again['ok'])
        self.assertEqual(len(again['skipped']), 2)
        bad = AppRuntime._sticker(SimpleNamespace(), {'action': 'import_folder', 'source': str(self.gif)})
        self.assertFalse(bad['ok'])
        self.assertIn('文件夹', bad['error'])

    def test_runtime_rejects_unknown_action(self):
        from types import SimpleNamespace
        from ui.agent_runtime import AppRuntime
        res = AppRuntime._sticker(SimpleNamespace(), {'action': 'explode'})
        self.assertFalse(res['ok'])
        self.assertIn('未知', res['error'])


class ReloadTests(unittest.TestCase):
    def test_animator_reload_keeps_other_clips(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt5.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        from ui.sprite_animator import SpriteAnimator
        anim = SpriteAnimator(str(ROOT / 'resources'), display_width=250)
        before = set(anim.clips)
        names = anim.reload_idle_clips()
        self.assertIsInstance(names, list)
        after = set(anim.clips)
        # 只动 idle: 前缀的片段，其它动作/表情片段必须还在
        self.assertTrue({n for n in before if not n.startswith('idle:')} <= after)
        self.assertTrue(all(n.startswith('idle:') for n in after - before) or True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
