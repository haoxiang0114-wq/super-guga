#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把外部生成的图像"焊"回工程口径（防穿帮的关键一跳）。

豆包生成的图长什么样是不确定的：任意尺寸、纯白底、可能偏色、可能构图偏。
直接丢进动画链，切段时会出现"换了个人"的突兀感。

这里做四件事，缺一不可：
1. **抠底**：白底/纯色底漫水抠成透明（复用 sticker_studio 的实现）；
2. **同口径**：按参考帧的内容高度缩放，底边钉到参考帧底边、水平居中；
3. **调色**：Reinhard 通道均值/方差匹配，把生成图的色偏拉回原素材；
4. **羽化**：alpha 轻模糊，消除硬边锯齿。

贴合后的帧与原帧同画布、同位置、同色调，才谈得上"不穿帮"。
"""
from __future__ import annotations

from PIL import Image, ImageFilter

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

RESAMPLE = getattr(getattr(Image, 'Resampling', Image), 'LANCZOS')


class ConformError(Exception):
    """生成图与参考帧差得太远，无法安全贴合。"""


def _need_numpy():
    if np is None:
        raise ConformError('缺少 numpy')


def _alpha_bbox(img):
    return img.convert('RGBA').getchannel('A').getbbox()


def _lum(color):
    r, g, b = color[:3]
    return 0.299 * r + 0.587 * g + 0.114 * b


def _edge_color(img):
    """四边出现最多的颜色及其覆盖率。"""
    rgb = img.convert('RGB')
    w, h = rgb.size
    px = rgb.load()
    counts = {}
    total = 0
    step = max(1, min(w, h) // 220)          # 大图抽样，避免 O(周长) 太慢
    for x in range(0, w, step):
        for y in (0, h - 1):
            c = px[x, y]
            counts[c] = counts.get(c, 0) + 1
            total += 1
    for y in range(0, h, step):
        for x in (0, w - 1):
            c = px[x, y]
            counts[c] = counts.get(c, 0) + 1
            total += 1
    if not total:
        return None, 0.0
    color, n = max(counts.items(), key=lambda kv: kv[1])
    return color, n / float(total)


def _bg_tolerance(img, bg, sample_step=3):
    """背景色的实际抖动幅度，据此定阈值（纯色底小，渐变底大）。"""
    _need_numpy()
    arr = np.asarray(img.convert('RGB'), dtype=np.int16)
    edges = np.concatenate([
        arr[0, ::sample_step].reshape(-1, 3),
        arr[-1, ::sample_step].reshape(-1, 3),
        arr[::sample_step, 0].reshape(-1, 3),
        arr[::sample_step, -1].reshape(-1, 3),
    ])
    diff = np.abs(edges - np.array(bg, dtype=np.int16)).max(axis=1)
    near = diff[diff <= 60]
    if near.size == 0:
        return 24
    return int(min(64, max(18, np.percentile(near, 98) + 6)))


def _flood_mask(arr, bg, tol):
    """从四边向内标记"与背景接近且与边缘连通"的区域（内部同色区域保留）。"""
    _need_numpy()
    from PIL import ImageDraw

    diff = np.abs(arr[..., :3].astype(np.int16) - np.array(bg, dtype=np.int16)).max(axis=2)
    cand = (diff <= tol).astype(np.uint8) * 255
    # fromarray 直接构造的图像是只读缓冲，PIL 13 下 floodfill 会静默不生效
    mask = Image.fromarray(cand, 'L').copy()
    w, h = mask.size
    for corner in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        if mask.getpixel(corner) == 255:
            try:
                ImageDraw.floodfill(mask, corner, 128, thresh=0)
            except Exception:  # noqa: BLE001
                continue
    return np.asarray(mask) == 128


def _largest_component(alpha_mask):
    """只保留最大连通块，清掉零散背景残留（降采样后 BFS，够快）。"""
    _need_numpy()
    from collections import deque

    h, w = alpha_mask.shape
    k = 4 if max(h, w) > 512 else 2
    small = alpha_mask[::k, ::k]
    sh, sw = small.shape
    seen = np.zeros_like(small, dtype=bool)
    best, best_size = None, 0
    for sy in range(0, sh, 3):
        for sx in range(0, sw, 3):
            if not small[sy, sx] or seen[sy, sx]:
                continue
            q = deque([(sy, sx)])
            seen[sy, sx] = True
            cells = []
            while q:
                y, x = q.popleft()
                cells.append((y, x))
                for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                    if 0 <= ny < sh and 0 <= nx < sw and small[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        q.append((ny, nx))
            if len(cells) > best_size:
                best_size, best = len(cells), cells
    if best is None:
        return alpha_mask
    keep = np.zeros_like(small, dtype=bool)
    for y, x in best:
        keep[y, x] = True
    up = np.repeat(np.repeat(keep, k, axis=0), k, axis=1)
    out = np.zeros_like(alpha_mask, dtype=bool)
    hh, ww = min(h, up.shape[0]), min(w, up.shape[1])
    out[:hh, :ww] = up[:hh, :ww]
    # 原始掩码里属于保留块的像素才算数（降采样会糊边，这里用膨胀近似补回）
    return alpha_mask & out


def detach(img, light_thr=196, keep_main=True):
    """白底/米白底/浅色底 → 透明底。已经是透明底的原样返回。

    比 sticker_studio 里的版本更宽容：按**亮度**判断浅色底（豆包常输出米白、
    浅黄背景，用通道下限卡会把 224 的蓝通道误判成深色），阈值按边框实际抖动
    自适应，最后只保留最大连通块，避免残留一圈浅色边框。
    """
    _need_numpy()
    if img.mode != 'RGBA':
        img = img.convert('RGBA')
    if img.getchannel('A').getextrema()[0] < 250:
        return img
    bg, cover = _edge_color(img)
    if bg is None or cover < 0.72 or _lum(bg) < light_thr:
        return img
    arr = np.asarray(img, dtype=np.float32).copy()
    tol = _bg_tolerance(img, bg)
    dead = _flood_mask(arr, bg, tol)
    if not dead.any():
        return img
    alpha = arr[..., 3]
    alpha[dead] = 0
    arr[..., 3] = alpha
    if keep_main:
        keep = _largest_component(alpha > 8)
        if keep.any() and keep.sum() < (alpha > 8).sum():
            alpha2 = alpha.copy()
            alpha2[~keep] = 0
            arr[..., 3] = alpha2
    return Image.fromarray(np.clip(arr, 0, 255).astype('uint8'), 'RGBA')


def _content_stats(arr, mask):
    px = arr[..., :3][mask]
    if px.size == 0:
        return None, None
    return px.mean(axis=0), px.std(axis=0) + 1e-6


def match_style(gen, ref, strength=1.0):
    """Reinhard 色调迁移：把 gen 的 RGB 分布拉向 ref（只在内容区域统计）。

    strength=0 不改；1 完全匹配。默认 1，因为风格漂移是穿帮的头号原因。
    """
    _need_numpy()
    if strength <= 0:
        return gen
    ga = np.asarray(gen.convert('RGBA'), dtype=np.float32)
    ra = np.asarray(ref.convert('RGBA'), dtype=np.float32)
    gm = ga[..., 3] > 8
    rm = ra[..., 3] > 8
    if not gm.any() or not rm.any():
        return gen
    g_mean, g_std = _content_stats(ga, gm)
    r_mean, r_std = _content_stats(ra, rm)
    if g_mean is None or r_mean is None:
        return gen
    norm = (ga[..., :3] - g_mean) / g_std
    moved = norm * (g_std * (1 - strength) + r_std * strength) \
        + (g_mean * (1 - strength) + r_mean * strength)
    out = ga.copy()
    out[..., :3] = np.clip(moved, 0, 255)
    return Image.fromarray(out.astype('uint8'), 'RGBA')


def _feather(img, radius=0.7):
    if radius <= 0:
        return img
    r, g, b, a = img.split()
    a = a.filter(ImageFilter.GaussianBlur(radius))
    return Image.merge('RGBA', (r, g, b, a))


def conform(gen, ref, canvas=None, style=1.0, feather=0.7, max_ratio=1.9):
    """生成图 → 与 ref 同画布、同位置、同色调的一帧。

    gen:    豆包返回的图（任意尺寸）
    ref:    参考帧（已对齐工程画布的 RGBA 帧）
    canvas: 目标画布，默认取 ref.size
    返回:   RGBA Image（画布尺寸）
    """
    _need_numpy()
    ref = ref.convert('RGBA')
    canvas = canvas or ref.size
    gen = detach(gen)
    gbox = _alpha_bbox(gen)
    rbox = _alpha_bbox(ref)
    if not gbox:
        raise ConformError('生成图抠底后是空的（可能整张都是背景色）')
    if not rbox:
        raise ConformError('参考帧没有可见内容')

    gw, gh = gbox[2] - gbox[0], gbox[3] - gbox[1]
    rw, rh = rbox[2] - rbox[0], rbox[3] - rbox[1]
    if rh <= 0 or rw <= 0:
        raise ConformError('参考帧内容尺寸异常')

    # 内容几乎填满外接矩形 → 背景没抠掉，直接贴合会带一块底色方块
    ga = np.asarray(gen)[..., 3]
    inside = ga[gbox[1]:gbox[3], gbox[0]:gbox[2]]
    fill_ratio = float((inside > 8).mean()) if inside.size else 0.0
    if fill_ratio > 0.93:
        raise ConformError('生成图背景没抠干净（内容填满矩形 %.0f%%）' % (fill_ratio * 100))
    # 比例差太多说明不是同一个角色/构图（比如生成了全身像或拼图）
    ratio = (gw / float(gh)) / (rw / float(rh)) if gh else 99
    if ratio > max_ratio or ratio < 1.0 / max_ratio:
        raise ConformError('生成图长宽比与角色差太多（%.2f:1），判定为构图跑偏' % ratio)

    content = gen.crop(gbox)
    scale = rh / float(content.height)
    nw, nh = max(1, round(content.width * scale)), max(1, round(content.height * scale))
    content = content.resize((nw, nh), RESAMPLE)
    content = match_style(content, ref.crop(rbox), strength=style)
    content = _feather(content, feather)

    out = Image.new('RGBA', canvas, (0, 0, 0, 0))
    px = int(round((rbox[0] + rbox[2]) / 2.0 - nw / 2.0))
    py = int(round(rbox[3] - nh))                 # 底边与参考帧对齐
    out.alpha_composite(content, (max(0, px), max(0, py)))
    return out


def conform_many(images, ref):
    """批量贴合，跳过失败的（返回 (成功列表, 失败原因列表)）。"""
    ok, bad = [], []
    for i, im in enumerate(images):
        try:
            ok.append(conform(im, ref))
        except ConformError as exc:
            bad.append((i, str(exc)))
    return ok, bad


def similarity(a, b):
    """两帧内容的归一化差异（0 = 完全一样），用于挑最贴合的生成结果。"""
    _need_numpy()
    aa = np.asarray(a.convert('RGBA'), dtype=np.float32)
    bb = np.asarray(b.convert('RGBA'), dtype=np.float32)
    if aa.shape != bb.shape:
        return 1.0
    mask = (aa[..., 3] > 8) | (bb[..., 3] > 8)
    if not mask.any():
        return 1.0
    diff = np.abs(aa[..., :3] - bb[..., :3])[mask].mean()
    amask = np.abs(aa[..., 3] - bb[..., 3])[mask].mean()
    return float((diff + amask) / 255.0)


def best_of(images, ref):
    """从多张生成结果里挑与原画风最接近的一张。"""
    scored = []
    for im in images:
        try:
            scored.append((similarity(im, ref), im))
        except Exception:  # noqa: BLE001
            continue
    if not scored:
        return None
    scored.sort(key=lambda t: t[0])
    return scored[0][1]
