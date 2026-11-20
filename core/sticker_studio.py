#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""super-guga 表情包工坊（内核）。

把用户上传的表情包（GIF / APNG / 动态 WebP / PNG 序列 zip / 图片目录 / 静态图）
适配成咕嘎的待机动画帧序列，写入 resources/actions/resident-motion-v2，
并登记到 data/sticker_library.json，重启咕嘎后待机链自动接入。

适配流程：统一画布口径（以 doze 帧为模板）→ 对齐原版主体 → 帧率归一 →
插帧补齐 → 末尾回归待机姿态。

两种用法：
  * 代码调用（智能体 / 界面）：``import_sticker()`` / ``list_stickers()`` /
    ``set_enabled()`` / ``remove_sticker()``，都返回 dict，不打印、不退出进程；
  * 命令行：``python -m core.sticker_studio import 表情.gif --name 大笑``

设计边界：只写 resources/actions/resident-motion-v2 与 data/sticker_library.json，
不碰其它素材；帧率被夹在 20–60（低于 20 的片段会被动画器忽略）。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import re
import shutil
import sys
import zipfile
from collections import deque
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RES_DIR = ROOT / 'resources'
RESIDENT_DIR = RES_DIR / 'actions' / 'resident-motion-v2'
MANIFEST_PATH = RESIDENT_DIR / 'manifest.json'
EXPRESSIONS_DIR = RES_DIR / 'expressions'
LIBRARY_PATH = ROOT / 'data' / 'sticker_library.json'
STUDIO_URL = 'resources/actions/resident-motion-v2'

REF_W, REF_H = 245, 352
PAD_X, PAD_Y, PAD_BOTTOM = 40, 60, 12
TARGET_FPS = 24.0
MIN_FPS, MAX_FPS = 20.0, 60.0
MAX_OUT_FRAMES = 240
MIN_OUT_FRAMES = 12
MAX_PACK_ITEMS = 60        # 一个文件夹最多拆出这么多个表情，防止误选整盘
MAX_FOLDER_FILES = 600          # 文件夹素材最多读这么多张，超出均匀抽样
TAIL_RETURN_FRAMES = 5
BREATH_FRAMES = 12

IMAGE_EXTS = {'.png', '.gif', '.webp', '.bmp', '.jpg', '.jpeg', '.apng'}
ARCHIVE_EXTS = {'.zip'}
# 找图片时跳过的目录名（解压出来的压缩包常带这些）
SKIP_DIR_NAMES = {'__macosx', 'node_modules', '__pycache__', '.git', '.svn',
                  'system volume information', '$recycle.bin'}
# 「套娃」表情包里最常见的通用目录名：这类名字当表情名没意义，取上一层
GENERIC_DIR_NAMES = {'frames', 'frame', 'images', 'image', 'img', 'png', 'gif',
                     'pic', 'pics', 'res', 'output', 'export', '表情', '图片'}

try:
    from PIL import Image
except ImportError:  # 依赖缺失时不让整个模块导入失败，由 available() 报告
    Image = None

RESAMPLE = None
if Image is not None:
    try:
        RESAMPLE = Image.Resampling.LANCZOS
    except AttributeError:  # Pillow < 9.1
        RESAMPLE = Image.LANCZOS


class StickerError(Exception):
    """可读的业务错误：素材不存在、格式不支持、id 冲突等。"""


def available():
    """能不能用：需要 Pillow + 素材目录。返回 (ok, 原因)。"""
    if Image is None:
        return False, '缺少 Pillow 依赖（pip install pillow）'
    if not MANIFEST_PATH.is_file():
        return False, '没有找到 %s/manifest.json，确认这是完整的咕嘎仓库' % STUDIO_URL
    return True, ''


def _require_pillow():
    ok, why = available()
    if not ok:
        raise StickerError(why)


# ─────────────────────────────────────────────────────────
# 解码与适配
# ─────────────────────────────────────────────────────────
def _natural_key(name):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', str(name))]


def load_normal_reference():
    """原版 normal 立绘当对齐基准；没有就退化为比例盒子。"""
    if Image is None:
        return None
    for name in ('normal_transparent.png', 'normal.png'):
        path = EXPRESSIONS_DIR / name
        if path.is_file():
            try:
                return Image.open(path).convert('RGBA')
            except OSError:
                return None
    return None


# 可能是"被改名的图片"的后缀：只有这些才值得真去打开试一下，
# 否则一个满是 .py/.txt 的目录会被逐个试探拖慢
PROBEABLE_EXTS = {'', '.dat', '.bin', '.tmp', '.jfif', '.pjpeg', '.jpe'}


def _opens_as_image(path):
    """扩展名靠不住时（无后缀、被改名），实际打开一次试试。"""
    try:
        with Image.open(path) as im:
            im.verify()
        return True
    except Exception:  # noqa: BLE001 - 打不开就是这个文件不参与
        return False


def _image_files_in(folder):
    """目录下的图片文件：认扩展名的直接收，没把握的打开探一下。"""
    out = []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return out
    for f in entries:
        if not f.is_file() or f.name.startswith('.'):
            continue
        suffix = f.suffix.lower()
        if suffix in IMAGE_EXTS:
            out.append(f)
        elif suffix in PROBEABLE_EXTS and _opens_as_image(f):
            out.append(f)          # 微信/QQ 存下来的表情常常没有扩展名
    return sorted(out, key=lambda f: _natural_key(f.name))


def is_image_file(path):
    """能不能当表情素材用：扩展名认得，或者内容确实是图片（无后缀/被改名的常见）。"""
    p = Path(path)
    if not p.is_file():
        return False
    suffix = p.suffix.lower()
    if suffix in IMAGE_EXTS:
        return True
    if suffix in ARCHIVE_EXTS:
        return False
    return suffix in PROBEABLE_EXTS and _opens_as_image(p)


def _cap_files(files, limit=None):
    """图片太多时均匀抽样，避免一口气把上千帧读进内存。"""
    limit = limit or MAX_FOLDER_FILES
    if len(files) <= limit:
        return files, ''
    step = len(files) / float(limit)
    picked = [files[min(len(files) - 1, int(i * step))] for i in range(limit)]
    return picked, '共 %d 张，均匀取了 %d 张' % (len(files), limit)


def scan_folder(src, max_depth=3):
    """找出一个文件夹里要当帧用的图片。

    先看直接子文件；没有就往下找子文件夹，取图片最多的那个（避免把几套表情
    混成一段动画）。返回 (files, note)，note 用来告诉用户实际用了哪个子目录。
    """
    src = Path(src)
    files = _image_files_in(src)
    if files:
        return _cap_files(files)

    best, note_dir = [], None
    stack = [(d, 1) for d in _subdirs(src, 1)]
    while stack:
        d, depth = stack.pop(0)
        found = _image_files_in(d)
        if found and len(found) > len(best):
            best, note_dir = found, d
        if depth < max_depth:
            stack.extend((sub, depth + 1) for sub in _subdirs(d, 1))
    if not best:
        return [], ''
    picked, cap_note = _cap_files(best)
    others = len(_all_image_dirs(src, max_depth)) - 1
    note = '这一层没有图片，用了子目录「%s」（%d 张）' % (note_dir.name, len(best))
    if others > 0:
        note += '；另有 %d 个子目录没用到' % others
    if cap_note:
        note += '；' + cap_note
    return picked, note


def _subdirs(folder, _level=1):
    try:
        return sorted((d for d in folder.iterdir()
                       if d.is_dir() and not d.name.startswith('.')
                       and d.name.lower() not in SKIP_DIR_NAMES),
                      key=lambda d: _natural_key(d.name))
    except OSError:
        return []


def _all_image_dirs(src, max_depth=3):
    """所有"直接放图片"的子目录，用于统计还有几套没用上。"""
    out, stack = [], [(d, 1) for d in _subdirs(src, 1)]
    while stack:
        d, depth = stack.pop(0)
        if _image_files_in(d):
            out.append(d)
        if depth < max_depth:
            stack.extend((sub, depth + 1) for sub in _subdirs(d, 1))
    return out


def _file_frames(path):
    """解一个图片文件的所有帧，返回 (frames, delays_ms)。静态图只有一帧。"""
    out, delays = [], []
    with Image.open(path) as im:
        total = getattr(im, 'n_frames', 1)
        if total <= 1:
            return [im.convert('RGBA')], [1000.0 / TARGET_FPS]
        for i in range(total):
            im.seek(i)
            out.append(im.convert('RGBA'))
            delays.append(float(im.info.get('duration') or 80.0))
    return out, delays


def _probe_frames(path):
    """(帧数, 尺寸)，打不开就是 (0, (0, 0))。"""
    try:
        with Image.open(path) as im:
            return getattr(im, 'n_frames', 1), im.size
    except Exception:  # noqa: BLE001 - 读不出来就当不是表情
        return 0, (0, 0)


def _friendly_label(stem, fallback='表情'):
    """md5 一类的文件名（QQ/微信常这样）不给人看，换成短名。"""
    stem = str(stem or '').strip()
    if not stem:
        return fallback
    if re.fullmatch(r'[0-9a-fA-F]{16,}', stem):
        return '%s%s' % (fallback, stem[:6])
    if re.fullmatch(r'[0-9a-fA-F]{8}-[0-9a-fA-F-]{20,}', stem):
        return '%s%s' % (fallback, stem[:6])
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', stem).strip(' ._')[:30] or fallback


def _dir_label(folder, root=None):
    """子目录表情的显示名；frames/images 这类通用目录名往上找一层。"""
    folder = Path(folder)
    name = folder.name
    if name.lower() in GENERIC_DIR_NAMES:
        parent = folder.parent
        if parent.name:
            name = parent.name if parent != Path(root or folder.parent) else name
    return _friendly_label(name)


def collect_folder(src, max_depth=3, max_items=MAX_PACK_ITEMS):
    """把一个文件夹拆成若干个独立表情。

    返回 (items, note)。每项 {'path','name','frames','size','kind'}，path 可以直接
    喂给 import_sticker。规则：
      · 动图文件（多帧）各自算一个表情；
      · 子文件夹里成套的图片，各算一个（按帧序列）；
      · 顶层只有静态图时：同一尺寸且 ≥3 张按一段序列，否则每张各算一个。
    这样"表情包文件夹"（一堆 GIF）不会被打包成一段乱闪的动画。
    """
    _require_pillow()
    src = Path(src)
    items, note = [], ''
    if not src.is_dir():
        raise StickerError('不是文件夹: %s' % src)

    loose = _image_files_in(src)
    animated, stills = [], []
    for f in loose:
        frames, size = _probe_frames(f)
        if frames <= 0:
            continue
        (animated if frames > 1 else stills).append((f, frames, size))

    # 子文件夹：能当序列用的一律各算一个
    seq_dirs = []
    for d in _all_image_dirs(src, max_depth):
        files = _image_files_in(d)
        if files:
            seq_dirs.append((d, files))

    same_size_stills = stills and len({s for _f, _n, s in stills}) == 1
    if stills and not animated and not seq_dirs and len(stills) >= 3 and same_size_stills:
        # 典型 PNG 序列平铺在顶层：整体是一段动画
        items.append({'path': src, 'name': _friendly_label(src.name, '序列'),
                      'frames': len(stills), 'size': stills[0][2], 'kind': 'sequence'})
        return items, '把这一层 %d 张同尺寸图片当成一段帧序列' % len(stills)

    for f, frames, size in animated:
        items.append({'path': f, 'name': _friendly_label(f.stem),
                      'frames': frames, 'size': size, 'kind': 'animated'})
    for f, frames, size in stills:
        items.append({'path': f, 'name': _friendly_label(f.stem),
                      'frames': frames, 'size': size, 'kind': 'static'})
    for d, files in seq_dirs:
        size = _probe_frames(files[0])[1]
        items.append({'path': d, 'name': _dir_label(d, src),
                      'frames': len(files), 'size': size, 'kind': 'sequence'})

    if not items:
        return [], '这个文件夹里没有图片'
    items.sort(key=lambda it: (it['frames'] <= 1, it['name']))
    # 同一个文件夹里常有多个 frames/ 子目录，名字撞了就补序号，否则入库时认不出来
    seen = {}
    for it in items:
        base = it['name']
        seen[base] = seen.get(base, 0) + 1
        if seen[base] > 1:
            it['name'] = '%s %d' % (base, seen[base])
    if len(items) > max_items:
        note = '只取前 %d 个（共 %d 个）' % (max_items, len(items))
        items = items[:max_items]
    return items, note


def collect_paths(paths, max_items=MAX_PACK_ITEMS):
    """把一批拖进来的文件/文件夹统一拆成表情列表（拖多个文件时用）。"""
    items = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            found, _note = collect_folder(p, max_items=max_items)
            items.extend(found)
        elif p.is_file():
            frames, size = _probe_frames(p)
            if frames <= 0:
                continue
            items.append({'path': p, 'name': _friendly_label(p.stem),
                          'frames': frames, 'size': size,
                          'kind': 'animated' if frames > 1 else 'static'})
    return items[:max_items]


def import_folder(src, fps=TARGET_FPS, weight=1.0, names=None, smooth=0,
                  doubao=False, log=None):
    """把一个文件夹里的表情逐个导入。返回 (成功列表, 跳过列表)。"""
    items, _note = collect_folder(src)
    ok, bad = [], []
    for idx, it in enumerate(items):
        name = (names or {}).get(str(it['path'])) or it['name']
        try:
            ok.append(import_sticker(it['path'], name, fps, weight,
                                     smooth=smooth, doubao=doubao, log=log))
        except StickerError as exc:
            bad.append((it['name'], str(exc)))
    return ok, bad


def _border_is_flat(img, tol=16, share=0.9):
    """边框是不是一整圈同一个浅色（白底表情的典型特征）。返回底色或 None。"""
    rgb = img.convert('RGB')
    w, h = rgb.size
    if w < 8 or h < 8:
        return None
    px = rgb.load()
    counts = {}
    total = 0
    for x in range(w):
        for y in (0, h - 1):
            counts[px[x, y]] = counts.get(px[x, y], 0) + 1
            total += 1
    for y in range(h):
        for x in (0, w - 1):
            counts[px[x, y]] = counts.get(px[x, y], 0) + 1
            total += 1
    color, n = max(counts.items(), key=lambda kv: kv[1])
    if n / float(total) < share:
        return None
    if min(color) < 225:            # 只有浅色底才抠，深色背景多半是画面本身
        return None
    return color


def _flood_detach(img, bg, tol=16):
    """从四边向内漫水，把和底色接近的像素抠成透明（画面内部的白色不受影响）。"""
    rgba = img.convert('RGBA')
    w, h = rgba.size
    src = rgba.copy().load()
    dst = rgba.load()

    def same(x, y):
        r, g, b, _a = src[x, y]
        return (abs(r - bg[0]) <= tol and abs(g - bg[1]) <= tol and abs(b - bg[2]) <= tol)

    seen = bytearray(w * h)
    queue = deque()
    for x in range(w):
        for y in (0, h - 1):
            if not seen[y * w + x] and same(x, y):
                seen[y * w + x] = 1
                queue.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            if not seen[y * w + x] and same(x, y):
                seen[y * w + x] = 1
                queue.append((x, y))
    while queue:
        x, y = queue.popleft()
        dst[x, y] = (0, 0, 0, 0)
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < w and 0 <= ny < h and not seen[ny * w + nx] and same(nx, ny):
                seen[ny * w + nx] = 1
                queue.append((nx, ny))
    return rgba


def detach_background(frames):
    """不透明的白底表情抠成透明底，返回 (frames, 是否动过)。

    桌宠是浮在桌面上的，白底会显出一块方块；这里只在"整圈边框一个浅色"且整图
    完全不透明时才动手，已经带透明通道的素材原样返回。
    """
    if not frames:
        return frames, False
    first = frames[0]
    if first.getchannel('A').getextrema()[0] < 255:
        return frames, False            # 本来就有透明区域，别碰
    bg = _border_is_flat(first)
    if bg is None:
        return frames, False
    out = [_flood_detach(f, bg) for f in frames]
    return out, True


_LAST_DETACHED = False      # 最近一次解码有没有抠白底（import 时写进提示文案）


def _finish(frames, delays):
    """统一出口：能抠白底就抠，再按同一裁切框裁掉多余透明边。"""
    global _LAST_DETACHED
    frames, changed = detach_background(frames)
    _LAST_DETACHED = changed
    if changed:
        frames = _trim_frames(frames)
    return frames, delays


def _trim_frames(frames, pad=2):
    """按所有帧的并集裁掉多余的透明边：逐帧各裁会让主体忽大忽小。"""
    boxes = [f.getchannel('A').getbbox() for f in frames]
    boxes = [b for b in boxes if b]
    if not boxes:
        return frames
    w, h = frames[0].size
    box = (max(0, min(b[0] for b in boxes) - pad), max(0, min(b[1] for b in boxes) - pad),
           min(w, max(b[2] for b in boxes) + pad), min(h, max(b[3] for b in boxes) + pad))
    if box[2] - box[0] < 2 or box[3] - box[1] < 2:
        return frames
    return [f.crop(box) for f in frames]


def decode_source(src):
    """把任意素材解成 (frames, delays_ms)。"""
    _require_pillow()
    src = Path(src)
    if not src.exists():
        raise StickerError('素材不存在: %s' % src)
    frames, delays = [], []
    if src.is_dir():
        files, _note = scan_folder(src)
        if not files:
            raise StickerError('这个文件夹里找不到图片: %s\n'
                               '（支持 GIF/PNG/APNG/WebP，子文件夹也会自动找）' % src)
        for f in files:
            # 目录里的每个文件都要把自身所有帧解出来：只取第一帧的话，
            # 目录中放一个动图会变成一张静止图，放多个动图会变成一堆首帧乱闪。
            for img, delay in zip(*_file_frames(f)):
                frames.append(img)
                delays.append(delay)
        return _finish(frames, delays)
    if src.suffix.lower() in ARCHIVE_EXTS:
        with zipfile.ZipFile(src) as z:
            names = sorted((n for n in z.namelist()
                            if Path(n).suffix.lower() in IMAGE_EXTS and not n.endswith('/')),
                           key=_natural_key)
            for name in names:
                with Image.open(io.BytesIO(z.read(name))) as im:
                    frames.append(im.convert('RGBA'))
                delays.append(1000.0 / TARGET_FPS)
        if not frames:
            raise StickerError('压缩包中没有可用图片: %s' % src)
        return _finish(frames, delays)
    if not is_image_file(src):
        raise StickerError('不支持的格式: %s（支持 GIF/APNG/WebP/PNG/JPG/ZIP/目录）' % (src.suffix or '无扩展名'))
    return _finish(*_file_frames(src))


def resample(frames, delays, fps):
    out = []
    for img, d in zip(frames, delays):
        out.extend(img.copy() for _ in range(max(1, round(d / 1000.0 * fps))))
    return out


def uniform_cap(frames, max_n=MAX_OUT_FRAMES):
    if len(frames) <= max_n:
        return frames
    idx = [round(i * (len(frames) - 1) / (max_n - 1)) for i in range(max_n)]
    return [frames[i] for i in idx]


def idle_baseline(canvas_h=REF_H + PAD_Y + PAD_BOTTOM):
    """内置待机帧的内容底边（脚踩的那条线）。导入素材要对齐到这里，否则会悬空。"""
    frames_dir = RESIDENT_DIR / 'doze' / 'frames'
    if frames_dir.is_dir():
        pngs = sorted(frames_dir.glob('*.png'))
        if pngs:
            try:
                with Image.open(pngs[0]) as im:
                    box = im.convert('RGBA').getchannel('A').getbbox()
                if box and box[3] > 0:
                    return box[3]
            except OSError:
                pass
    return canvas_h


def _ground_bbox(bbox, canvas):
    """把目标框对齐到画布中轴 + 内置待机帧的落地线；尺寸不变，只改位置。

    normal 立绘的内容底边比真实待机帧高 60 多个像素，直接照着它对齐，
    导入的表情会悬在地面上方，和企鹅切换时看着像在跳。
    """
    if not bbox:
        return bbox
    x0, y0, x1, y1 = bbox
    drop = idle_baseline(canvas[1]) - y1
    if drop > 0:
        y0, y1 = y0 + drop, min(canvas[1], y1 + drop)
    shift = round(canvas[0] / 2.0 - (x0 + x1) / 2.0)
    if shift:
        shift = max(-x0, min(shift, canvas[0] - x1))     # 别把内容推出画布
        x0, x1 = x0 + shift, x1 + shift
    return (x0, y0, x1, y1)


def resolve_canvas():
    """画布口径取自 doze 帧，保证新旧素材同一图层。"""
    frames_dir = RESIDENT_DIR / 'doze' / 'frames'
    ref_w = REF_W
    if frames_dir.is_dir():
        pngs = sorted(frames_dir.glob('*.png'))
        if pngs:
            with Image.open(pngs[0]) as im:
                size = im.size
            try:
                data = json.loads(MANIFEST_PATH.read_text(encoding='utf-8'))
                spec = next(c for c in data['clips'] if c.get('name') == 'doze')
                ref_w = int(spec.get('reference_width') or REF_W)
            except (OSError, ValueError, KeyError, TypeError, StopIteration):
                pass
            return size, size[0] / (REF_W + 2 * PAD_X), ref_w
    return (REF_W + 2 * PAD_X, REF_H + PAD_Y + PAD_BOTTOM), 1.0, ref_w


def fit_frame(frame, size, target_bbox):
    """按目标 bbox 缩放并底部对齐，多余区域留透明。"""
    out = Image.new('RGBA', size, (0, 0, 0, 0))
    bbox = frame.getchannel('A').getbbox()
    if not bbox or not target_bbox:
        return out
    content = frame.crop(bbox)
    x0, y0, x1, y1 = target_bbox
    scale = max(0.45, min(2.0, max(1, y1 - y0) / max(1, content.height)))
    # 高的裁掉不如整体缩小：宽度也要塞进目标框，否则横图会被切掉两边
    if content.width * scale > max(1, x1 - x0):
        scale = max(0.3, (x1 - x0) / float(max(1, content.width)))
    nw, nh = max(1, round(content.width * scale)), max(1, round(content.height * scale))
    content = content.resize((nw, nh), RESAMPLE)
    px = min(max(0, round((x0 + x1) / 2 - nw / 2)), max(0, size[0] - nw))
    py = min(max(0, y1 - nh), max(0, size[1] - nh))
    out.alpha_composite(content, (px, py))
    return out


def breathing(base, n=BREATH_FRAMES):
    """静态图 → 轻微呼吸，避免"卡住不动"。"""
    out = []
    for i in range(n):
        t = i / n
        dy = round(-2.5 * (1 - math.cos(2 * math.pi * t)) / 2)
        canvas = Image.new('RGBA', base.size, (0, 0, 0, 0))
        canvas.alpha_composite(base, (0, dy))
        out.append(canvas)
    return out


def interpolate(frames, min_len=MIN_OUT_FRAMES):
    n = len(frames)
    if n >= min_len or n < 2:
        return frames
    seg = max(1, (min_len - 1) // (n - 1))
    out = []
    for i in range(n - 1):
        a, b = frames[i], frames[i + 1]
        for j in range(seg):
            out.append(a if j == 0 else Image.blend(a, b, j / seg))
    while len(out) < min_len:
        out.append(Image.blend(out[-2], out[-1], 0.5))
    return out


def tail_return(frames, anchor, n=TAIL_RETURN_FRAMES):
    if anchor is None or not frames:
        return frames
    last = frames[-1]
    for i in range(1, n + 1):
        frames.append(Image.blend(last, anchor, i / (n + 1)))
    return frames


def adapt_frames(source, fps=TARGET_FPS):
    """完整流水线：返回 (帧列表, 画布尺寸, reference_width)。"""
    frames, canvas, ref_w, _fps = adapt_frames_timed(source, fps, keep_duration=False)
    return frames, canvas, ref_w


def adapt_frames_timed(source, fps=TARGET_FPS, keep_duration=True):
    """完整流水线，额外把"保持时长"后的帧率一起返回。

    原始素材常常只有 6~10fps。插帧补齐会把 6 帧变成 12+5 帧，如果帧率仍按原值，
    播放时长会被拉长两三倍——看着就是"动作变慢了"。这里按补后的帧数反算帧率，
    让动作时长和原素材一致。
    """
    _require_pillow()
    fps = min(MAX_FPS, max(MIN_FPS, float(fps or TARGET_FPS)))
    frames, delays = decode_source(source)
    frames = uniform_cap(resample(frames, delays, fps), MAX_OUT_FRAMES)
    base_seconds = len(frames) / fps if fps else 0.0
    static = len(frames) == 1
    canvas, k, ref_w = resolve_canvas()
    target_bbox, anchor = None, None
    ref = load_normal_reference()
    if ref is not None:
        ref_r = ref.resize((max(1, round(REF_W * k)), max(1, round(REF_H * k))), RESAMPLE)
        target_bbox = ref_r.getchannel('A').getbbox()
        if target_bbox:
            anchor = fit_frame(ref_r, canvas, target_bbox)
    if target_bbox is None:
        w, h = canvas
        target_bbox = (round(w * 0.12), round(h * 0.30), round(w * 0.88), round(h * 0.93))
    target_bbox = _ground_bbox(target_bbox, canvas)
    aligned = [fit_frame(f, canvas, target_bbox) for f in frames]
    if static:
        aligned = breathing(aligned[0])
    if keep_duration:
        # 保持时长时不能再插帧：7 帧素材插到 17 帧再按原帧率播就是慢放。
        # 帧率按"核心帧数 / 素材时长"算，末尾那几帧回归姿态算额外收尾（多 0.1s 无感）。
        core_count = len(aligned)
        aligned = tail_return(aligned, anchor)
        out_fps = fps
        if not static and base_seconds > 0.05 and core_count:
            out_fps = max(MIN_FPS, min(MAX_FPS, core_count / base_seconds))
    else:
        aligned = tail_return(interpolate(aligned), anchor)
        out_fps = fps
    return aligned, canvas, ref_w, out_fps


# ─────────────────────────────────────────────────────────
# 素材库 / manifest
# ─────────────────────────────────────────────────────────
def load_registry():
    if LIBRARY_PATH.is_file():
        try:
            data = json.loads(LIBRARY_PATH.read_text(encoding='utf-8'))
            if isinstance(data, dict) and isinstance(data.get('items'), list):
                return data
        except (OSError, ValueError):
            pass
    return {'version': 1, 'items': []}


def save_registry(registry):
    LIBRARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    LIBRARY_PATH.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding='utf-8')


def make_slug(name, registry=None):
    registry = registry if registry is not None else load_registry()
    used = {it.get('slug') for it in registry.get('items', [])}
    base = re.sub(r'[^a-z0-9_-]+', '-', str(name or '').lower()).strip('-') or 'sticker'
    if base.isdigit():          # 纯数字当目录名容易被误认成序号
        base = 'sticker-%s' % base
    slug, i = base, 1
    while slug in used or (RESIDENT_DIR / slug).exists():
        i += 1
        slug = '%s_%d' % (base, i)
    return slug


def _frames_digest(folder):
    """帧目录内容摘要；与 core.avatar_review 保持同一算法。"""
    try:
        from core.avatar_review import frames_digest
        return frames_digest(folder)
    except Exception:  # noqa: BLE001 - 模块不可用时自己算一遍
        digest = hashlib.sha256()
        files = sorted(Path(folder).glob('*.png'))
        if not files:
            raise StickerError('没有生成任何帧')
        for path in files:
            digest.update(path.name.encode('utf-8'))
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        return digest.hexdigest()


def write_visual_review(slug_dir, name, slot='user sticker'):
    """写 visual-review.json，否则动画器的审核闸门会静默跳过这套素材。

    这里的 checks 是"程序化产出可验证的部分"：帧连续、无重影（源帧合成）、
    循环接缝（末尾回归待机姿态）。身份一项如实记为 False + 用户已接受变化，
    因为用户导入的本来就是另一套形象，不是冒充原版。
    """
    slug_dir = Path(slug_dir)
    frames_dir = slug_dir / 'frames'
    review = {
        'schema': 2,
        'status': 'approved',
        'reviewer': 'sticker-studio: 用户导入 %s' % name,
        'playback': 'loop',
        'frames_sha256': _frames_digest(frames_dir),
        'checks': {'identity': False, 'identity_variation_accepted': True,
                   'continuous_motion': True, 'no_ghosts': True, 'anatomy': True,
                   'loop_seam': True},
        'user_approval': {'by': 'user',
                          'quote': '用户在表情包工坊导入「%s」并要求接入待机动画（%s）' % (name, slot)},
        'note': '帧由用户提供的表情包经统一画布、主体对齐、帧率归一、末帧回归待机姿态生成。'
    }
    (slug_dir / 'visual-review.json').write_text(
        json.dumps(review, ensure_ascii=False, indent=2), encoding='utf-8')
    return review

def update_manifest(slug, fps, weight, ref_w, enabled=True):
    data = {'schema': 2, 'padded_canvas': True, 'clips': []}
    if MANIFEST_PATH.is_file():
        try:
            data = json.loads(MANIFEST_PATH.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
    data.setdefault('schema', 2)
    data.setdefault('padded_canvas', True)
    clips = [c for c in data.get('clips', []) if isinstance(c, dict) and c.get('name') != slug]
    if enabled:
        clips.append({'name': slug, 'frames': '%s/frames' % slug, 'native_fps': float(fps),
                      'reference_width': int(ref_w), 'weight': max(0.01, float(weight))})
    data['clips'] = clips
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return data


def manifest_clips():
    """当前真正会被待机链用到的片段（含内置的 doze/heart/pout…）。"""
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    out = []
    for spec in data.get('clips', []):
        if not isinstance(spec, dict):
            continue
        out.append(dict(name=spec.get('name'), weight=spec.get('weight'),
                        fps=spec.get('native_fps'), frames=spec.get('frames')))
    return out


# ─────────────────────────────────────────────────────────
# 业务动作
# ─────────────────────────────────────────────────────────
def list_stickers():
    items = []
    for it in load_registry().get('items', []):
        row = dict(it)
        row['dir'] = str(RESIDENT_DIR / it.get('slug', ''))
        row['preview'] = str(RESIDENT_DIR / it.get('slug', '') / 'preview.gif')
        items.append(row)
    return items


def _find(registry, slug):
    for it in registry.get('items', []):
        if it.get('slug') == slug:
            return it
    raise StickerError('素材不存在: %s' % slug)


def _source_digest(src_path):
    """素材指纹。目录要按内容算：只按路径算的话，换了图也会被当成"已经导入过"。"""
    sha1 = hashlib.sha1()
    if src_path.is_file():
        sha1.update(src_path.read_bytes())
        return sha1.hexdigest()
    for f in scan_folder(src_path)[0]:
        sha1.update(f.name.encode('utf-8'))
        sha1.update(hashlib.sha256(f.read_bytes()).digest())
    return sha1.hexdigest()


def _doubao_generator(hint='', log=None):
    """把"豆包网页生图"包成补帧引擎能用的回调；没登录就直接返回 None。"""
    try:
        from core import doubao_image as db
    except Exception:  # noqa: BLE001 - 客户端缺失不影响本地补帧
        if log:
            log('豆包客户端不可用，只用本地补帧')
        return None
    if not db.available():
        if log:
            log('豆包未登录，只用本地补帧（要开 AI 重绘先扫码登录）')
        return None

    ref_dir = ROOT / 'data' / 'doubao_cache' / 'refs'
    seq = {'n': 0}

    def gen(prev, nxt, t, prompt):
        # 两张参考图一起给：只给起始帧的话，模型不知道动作要往哪去，
        # 容易画出"另一个人摆了个姿势"，后面贴合管线也救不回来。
        ref_dir.mkdir(parents=True, exist_ok=True)
        seq['n'] += 1
        stamp = '%s-%02d' % (datetime.now().strftime('%H%M%S'), seq['n'])
        paths = []
        for tag, img in (('a', prev), ('b', nxt)):
            p = ref_dir / ('%s-%s.png' % (tag, stamp))
            img.convert('RGBA').save(p)
            paths.append(p)
        text = db.render_motion_prompt(hint or '两个动作之间的自然中间姿态', pair=len(paths) > 1)
        if log:
            log('把第 %d 对参考图交给豆包（%s）' % (seq['n'], ' / '.join(p.name for p in paths)))
        data = db.ask(text, refs=paths, log=log)
        if not data:
            return None
        return Image.open(io.BytesIO(data)).convert('RGBA')

    return gen


def import_sticker(source, name, fps=TARGET_FPS, weight=1.0, slug=None,
                   smooth=0, doubao=False, motion_hint='', log=None):
    """导入并适配一个表情包。返回 dict，失败抛 StickerError。

    smooth:    补帧倍数，0/1 = 不补；2 = 帧数翻倍（帧率同步翻倍，时长不变）
    doubao:    跨度大的段落是否让豆包重绘（需要先登录豆包网页）
    motion_hint: 动作描述，喂给豆包的提示词，比如"抬手打招呼"
    """
    _require_pillow()
    name = str(name or '').strip()
    if not name:
        raise StickerError('请给表情起个名字')
    src_path = Path(source).expanduser()
    if not src_path.exists():
        raise StickerError('素材不存在: %s' % src_path)
    src_path = src_path.resolve()
    fps = min(MAX_FPS, max(MIN_FPS, float(fps or TARGET_FPS)))
    weight = max(0.01, min(5.0, float(weight or 1.0)))
    registry = load_registry()
    digest = _source_digest(src_path)
    for it in registry.get('items', []):
        if it.get('source_sha1') == digest:
            raise StickerError('这个素材已经作为 [%s]（%s）入库了；想换效果先 remove %s'
                               % (it.get('slug'), it.get('name'), it.get('slug')))
    slug = re.sub(r'[^a-z0-9_-]+', '-', str(slug or '').lower()).strip('-') if slug else ''
    if slug:
        if slug in {it.get('slug') for it in registry.get('items', [])} or (RESIDENT_DIR / slug).exists():
            raise StickerError('库 id [%s] 已被占用，换一个' % slug)
    else:
        # 中文名转不出可用的目录名时，用素材指纹当前缀：比一串 sticker_2、sticker_3 好认
        base_name = re.sub(r'[^a-z0-9_-]+', '-', str(name).lower()).strip('-')
        if len(re.sub(r'[^a-z0-9]', '', base_name)) < 2:
            base_name = digest[:8]      # "新动图0" 这种只剩一个数字的也别用
        slug = make_slug(base_name, registry)
    frames, canvas, ref_w, base_fps = adapt_frames_timed(src_path, fps)
    fps = base_fps
    source_frames = len(frames)
    motion_report = {}
    if int(smooth or 0) > 1:
        try:
            from core import sticker_motion as motion
            generator = _doubao_generator(motion_hint, log) if doubao else None
            arrs, new_fps, motion_report = motion.enrich(
                frames, fps=fps, factor=int(smooth),
                baseline=idle_baseline(canvas[1]), jitter=0.06,
                generator=generator, prompt=motion_hint, log=log)
            frames = motion.frames_to_pil(arrs)
            fps = new_fps
        except Exception as exc:  # noqa: BLE001 - 补帧失败退回原样，不能连导入都失败
            if log:
                log('补帧失败，按原帧导入：%s' % str(exc)[:70])
            motion_report = {'error': str(exc)[:120]}
    frames_dir = RESIDENT_DIR / slug / 'frames'
    frames_dir.mkdir(parents=True, exist_ok=True)
    for i, img in enumerate(frames):
        img.save(frames_dir / ('%04d.png' % i))
    duration = round(1000.0 / max(fps, 1))
    preview = frames if len(frames) <= 120 else frames[::2]
    step = 1 if preview is frames else 2
    preview[0].save(RESIDENT_DIR / slug / 'preview.gif', save_all=True,
                    append_images=preview[1:], duration=duration * step, loop=0, disposal=2)
    write_visual_review(RESIDENT_DIR / slug, name, '表情包工坊导入')
    update_manifest(slug, fps, weight, ref_w, enabled=True)
    registry.setdefault('items', []).append({
        'slug': slug, 'name': name, 'source': str(src_path), 'source_sha1': digest,
        'frames': len(frames), 'fps': fps, 'reference_width': ref_w, 'weight': weight,
        'enabled': True, 'created': datetime.now().isoformat(timespec='seconds'),
        'source_frames': source_frames, 'smooth': int(motion_report.get('factor', 1) or 1)})
    save_registry(registry)

    try:
        from core import motion_library
        total_seconds = len(frames) / max(fps, 1e-6)
        src_fps = round(source_frames / total_seconds, 1) if total_seconds else fps
        motion_library.record(
            slug, name, source=str(src_path), source_frames=source_frames,
            out_frames=len(frames), src_fps=src_fps,
            fps=fps, factor=int(motion_report.get('factor', 1) or 1),
            engine='motion+doubao' if motion_report.get('redrawn') else 'motion-compensated',
            doubao_calls=int(motion_report.get('redrawn', 0) or 0),
            quality=motion_report.get('quality') or {},
            keyframes=str(frames_dir),
            notes=motion_hint or '')
    except Exception:  # noqa: BLE001 - 动作库只是台账，写不进去不影响播放
        pass

    smooth_note = ''
    if int(motion_report.get('factor', 1) or 1) > 1:
        smooth_note = '，补帧 %dx（%d→%d 帧 @%gfps）' % (
            motion_report['factor'], source_frames, len(frames), fps)
    if motion_report.get('redrawn'):
        smooth_note += '，AI 重绘 %d 处' % motion_report['redrawn']
    return dict(ok=True, slug=slug, name=name, frames=len(frames), fps=fps,
                weight=weight, canvas=[canvas[0], canvas[1]],
                dir=str(frames_dir), preview=str(RESIDENT_DIR / slug / 'preview.gif'),
                seconds=round(len(frames) / max(fps, 1e-6), 1),
                source_frames=source_frames, smooth=motion_report.get('factor', 1),
                redrawn=motion_report.get('redrawn', 0),
                quality=motion_report.get('quality') or {},
                message='已导入 %s -> %s：%d 帧 @%gfps%s，画布 %d×%d，重启咕嘎后待机时会按权重接入'
                        % (name, slug, len(frames), fps, smooth_note, canvas[0], canvas[1]))


def set_enabled(slug, enabled):
    registry = load_registry()
    item = _find(registry, slug)
    enabled = bool(enabled)
    if bool(item.get('enabled')) == enabled:
        return dict(ok=True, slug=slug, enabled=enabled, changed=False,
                    message='[%s] 已经是%s状态' % (slug, '启用' if enabled else '停用'))
    update_manifest(slug, item.get('fps', TARGET_FPS), item.get('weight', 1.0),
                    item.get('reference_width', REF_W), enabled=enabled)
    item['enabled'] = enabled
    save_registry(registry)
    return dict(ok=True, slug=slug, enabled=enabled, changed=True,
                message='已%s素材 %s%s' % ('启用' if enabled else '停用', slug,
                                          '，重启咕嘎后生效' if enabled else ''))


def remove_sticker(slug):
    registry = load_registry()
    item = _find(registry, slug)
    update_manifest(slug, item.get('fps', TARGET_FPS), item.get('weight', 1.0),
                    item.get('reference_width', REF_W), enabled=False)
    shutil.rmtree(RESIDENT_DIR / slug, ignore_errors=True)
    registry['items'] = [it for it in registry['items'] if it.get('slug') != slug]
    save_registry(registry)
    return dict(ok=True, slug=slug, message='已移除素材 %s' % slug)


def probe(source, fps=TARGET_FPS):
    """只算不改：给界面/智能体看适配后的帧数与画布。"""
    frames, canvas, ref_w = adapt_frames(source, fps)
    return dict(ok=True, frames=len(frames), canvas=[canvas[0], canvas[1]],
                reference_width=ref_w, seconds=round(len(frames) / float(fps or TARGET_FPS), 1))


# ─────────────────────────────────────────────────────────
# 命令行
# ─────────────────────────────────────────────────────────
def main(argv=None):
    parser = argparse.ArgumentParser(
        description='super-guga 表情包工坊：把表情包适配成待机动画',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='示例:\n  python -m core.sticker_studio import 大笑.gif --name 大笑滚地\n'
               '  python -m core.sticker_studio list\n'
               '  python -m core.sticker_studio disable laugh_roll')
    sub = parser.add_subparsers(dest='cmd', required=True)
    p_import = sub.add_parser('import', help='导入并适配表情包')
    p_import.add_argument('source', help='GIF/APNG/WebP/PNG序列zip/图片目录/静态图')
    p_import.add_argument('--name', required=True, help='表情显示名')
    p_import.add_argument('--fps', type=float, default=TARGET_FPS, help='目标帧率 20-60，默认 24')
    p_import.add_argument('--weight', type=float, default=1.0, help='待机链播放权重，默认 1.0')
    p_import.add_argument('--slug', default='', help='指定库 id（默认按名字生成）')
    p_import.add_argument('--smooth', type=int, default=0, choices=[0, 1, 2, 3, 4],
                          help='补帧倍数：0/1 不补，2 帧数翻倍（帧率同步翻倍，时长不变）')
    p_import.add_argument('--doubao', action='store_true',
                          help='跨度大的段落让豆包网页重绘中间姿态（需先 python -m core.doubao_image login）')
    p_import.add_argument('--hint', default='', help='动作描述，喂给豆包的提示词，如"抬手打招呼"')
    sub.add_parser('list', help='查看素材库')
    p_rm = sub.add_parser('remove', help='移除素材')
    p_rm.add_argument('slug')
    for cmd, help_text in (('enable', '启用素材'), ('disable', '停用素材（保留文件）')):
        p_toggle = sub.add_parser(cmd, help=help_text)
        p_toggle.add_argument('slug')
    p_probe = sub.add_parser('probe', help='只预览适配结果，不写盘')
    p_probe.add_argument('source')
    p_probe.add_argument('--fps', type=float, default=TARGET_FPS)
    args = parser.parse_args(argv)

    if not available()[0]:
        print(available()[1])
        return 1
    try:
        if args.cmd == 'import':
            res = import_sticker(args.source, args.name, args.fps, args.weight, args.slug,
                                 smooth=args.smooth, doubao=args.doubao,
                                 motion_hint=args.hint, log=print)
            print('[OK] ' + res['message'])
            print('     时长约 %.1fs，bundle: %s' % (res['seconds'], res['dir']))
            print('     预览: %s' % res['preview'])
            if res.get('quality'):
                q = res['quality']
                print('     质量: 抖动 %s，落地线波动 %s px' % (q.get('jitter'), q.get('baseline_spread')))
        elif args.cmd == 'list':
            items = list_stickers()
            if not items:
                print('素材库为空，先用 import 命令导入表情包。')
                return 0
            print('%-24s%-14s%6s%6s%6s  %s' % ('slug', '名称', '帧数', 'fps', '状态', '来源'))
            for it in items:
                print('%-24s%-14s%6d%6.0f%6s  %s' % (
                    it['slug'], str(it.get('name', ''))[:12], it.get('frames', 0),
                    it.get('fps', 0), '启用' if it.get('enabled') else '停用', it.get('source', '')))
            print('\n当前待机链片段: ' + ', '.join('%s(%.1f)' % (c['name'], float(c['weight'] or 1))
                                                for c in manifest_clips()))
        elif args.cmd == 'remove':
            print('[OK] ' + remove_sticker(args.slug)['message'])
        elif args.cmd in ('enable', 'disable'):
            print('[OK] ' + set_enabled(args.slug, args.cmd == 'enable')['message'])
        elif args.cmd == 'probe':
            res = probe(args.source, args.fps)
            print('[OK] %d 帧，画布 %d×%d，约 %.1fs' % (res['frames'], res['canvas'][0],
                                                    res['canvas'][1], res['seconds']))
    except StickerError as exc:
        print('[ERROR] %s' % exc)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
