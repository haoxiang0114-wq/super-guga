#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运动补偿补帧引擎（super-guga 表情包工坊）。

解决的问题
----------
旧实现用 ``Image.blend(a, b, t)`` 补中间帧，两个姿态直接叠在一起，会出现
"两个头/四只手"的鬼影；手绘表情包帧率又常常只有 6~12fps，看着一顿一顿。

这里改成**位移场形变插值**：
1. 块匹配估计 A→B、B→A 的位移场（降采样 + 网格模板匹配，纯 numpy）；
2. 插值帧把 A 按 t 前向形变、B 按 1-t 反向形变，两个姿态先"对上"再融合，
   鬼影基本消失；
3. Alpha 用预乘合成，边缘不会越插越虚；
4. 底边/中轴钉回内置待机帧的落地线，避免补帧后跳动。

对外接口
--------
``estimate_flow(a, b)``      位移场估计
``morph(a, b, t)``           单张过渡帧
``smooth_frames(frames, ...)`` 整段补帧（保持时长、提高帧率）
``stabilize(frames, ...)``   落地线与中轴校正
``quality(frames, ...)``     抖动/鬼影指标
``enrich(...)``              完整流程（可选调用豆包重绘跨度大的段落）
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image

try:
    import numpy as np
except ImportError:  # pragma: no cover - 环境缺依赖时给出可读错误
    np = None

RESAMPLE = getattr(getattr(Image, 'Resampling', Image), 'LANCZOS')
BILINEAR = getattr(getattr(Image, 'Resampling', Image), 'BILINEAR')

# 位移场估计参数：网格密度、模板大小、搜索半径（降采样后的像素）
GRID = 9
PATCH = 13
SEARCH = 5
DOWNSCALE = 3
MAX_FPS_CAP = 60.0
# 配准后残差超过这个值，说明两帧之间不是"同一主体的运动"，形变插值会糊
HARD_PAIR = 0.16
SOFT_PAIR = 0.07


class MotionError(Exception):
    """补帧参数或素材不合法。"""


def _need_numpy():
    if np is None:
        raise MotionError('缺少 numpy，请先执行: pip install numpy')


# ─────────────────────────────────────────────────────────
# 基础图像运算
# ─────────────────────────────────────────────────────────
def _rgba(img):
    """PIL.Image → (H, W, 4) float32 数组。"""
    return np.asarray(img.convert('RGBA'), dtype=np.float32)


def _to_image(arr):
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), 'RGBA')


def _luma(arr):
    """带 alpha 加权的灰度，透明区域不参与匹配。"""
    rgb = arr[..., :3]
    alpha = arr[..., 3] / 255.0
    return (0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]) * alpha


def _down(arr, k):
    if k <= 1:
        return arr
    h, w = arr.shape[0] // k, arr.shape[1] // k
    if h < PATCH or w < PATCH:
        return arr
    block = arr[:h * k, :w * k]
    if block.ndim == 2:
        return block.reshape(h, k, w, k).mean(axis=(1, 3))
    c = block.shape[2]
    return block.reshape(h, k, w, k, c).mean(axis=(1, 3))


def _bilinear(arr, ys, xs):
    """对 (H,W,C) 数组做双线性采样，坐标可为浮点，越界按边缘钳制。"""
    h, w = arr.shape[:2]
    ys = np.clip(ys, 0, h - 1)
    xs = np.clip(xs, 0, w - 1)
    y0 = np.floor(ys).astype(np.int32)
    x0 = np.floor(xs).astype(np.int32)
    y1 = np.minimum(y0 + 1, h - 1)
    x1 = np.minimum(x0 + 1, w - 1)
    wy = (ys - y0)[..., None]
    wx = (xs - x0)[..., None]
    top = arr[y0, x0] * (1 - wx) + arr[y0, x1] * wx
    bot = arr[y1, x0] * (1 - wx) + arr[y1, x1] * wx
    return top * (1 - wy) + bot * wy


# ─────────────────────────────────────────────────────────
# 位移场
# ─────────────────────────────────────────────────────────
def _grid_points(lum):
    h, w = lum.shape
    ys = np.linspace(0, h - 1, GRID)
    xs = np.linspace(0, w - 1, GRID)
    return [(min(max(int(round(y)), 0), h - 1), min(max(int(round(x)), 0), w - 1))
            for y in ys for x in xs]


def _match(lum_a, lum_b, shifts):
    """在 b 里为 a 的每个网格点找最相似的偏移，返回 (gy, gx, ok)。"""
    h, w = lum_b.shape
    half = PATCH // 2
    out = []
    for cy, cx in _grid_points(lum_a):
        y0, x0 = cy - half, cx - half
        if y0 < 0 or x0 < 0 or y0 + PATCH > h or x0 + PATCH > w:
            out.append((0.0, 0.0, None))
            continue
        tpl = lum_a[y0:y0 + PATCH, x0:x0 + PATCH]
        base = float(np.sum(tpl * tpl))
        best, best_err = (0.0, 0.0), None
        for dy, dx in shifts:
            yy, xx = y0 + dy, x0 + dx
            if yy < 0 or xx < 0 or yy + PATCH > h or xx + PATCH > w:
                continue
            win = lum_b[yy:yy + PATCH, xx:xx + PATCH]
            err = base - 2.0 * float(np.sum(tpl * win)) + float(np.sum(win * win))
            if best_err is None or err < best_err:
                best_err, best = err, (float(dy), float(dx))
        if best_err is None:
            out.append((0.0, 0.0, None))
            continue
        denom = base + 1e-6
        out.append((best[0], best[1], max(0.0, best_err / denom)))
    return out


def _dense_field(points, size):
    """网格位移 → 稠密场（用 PIL 双线性放大）。"""
    gy = np.array([p[0] for p in points], dtype=np.float32).reshape(GRID, GRID)
    gx = np.array([p[1] for p in points], dtype=np.float32).reshape(GRID, GRID)
    w, h = size
    fy = np.asarray(Image.fromarray(gy, 'F').resize((w, h), BILINEAR), dtype=np.float32)
    fx = np.asarray(Image.fromarray(gx, 'F').resize((w, h), BILINEAR), dtype=np.float32)
    return fy, fx


def estimate_flow(a, b, down=DOWNSCALE, search=SEARCH):
    """估计 b 相对 a 的位移场，返回 (fy, fx, residual)。

    residual 是配准后残差（0 表示完全吻合，越大越不像同一主体的运动）。
    """
    _need_numpy()
    la, lb = _luma(a), _luma(b)
    if la.shape != lb.shape:
        raise MotionError('两帧尺寸不一致: %s vs %s' % (la.shape, lb.shape))
    small_a, small_b = _down(la, down), _down(lb, down)
    shifts = [(dy, dx)
              for dy in range(-search, search + 1)
              for dx in range(-search, search + 1)]
    points = _match(small_a, small_b, shifts)
    valid = [p[2] for p in points if p[2] is not None]
    residual = float(np.mean(valid)) if valid else 1.0
    scaled = [(p[0] * down, p[1] * down, p[2]) for p in points]
    h, w = la.shape
    fy, fx = _dense_field(scaled, (w, h))
    return fy, fx, residual


def warp(arr, fy, fx, t):
    """把 arr 按 t 比例的位移场形变（逆向采样）。"""
    h, w = arr.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    return _bilinear(arr, yy - t * fy, xx - t * fx)


def _blend_rgba(wa, wb, t):
    """预乘 alpha 的正确合成。

    alpha 必须线性插值：取 max 会把两个姿态的轮廓合并，面积膨胀、看着像重影。
    RGB 用预乘值加权，避免半透明边缘叠加后发灰。
    """
    aa, ab = wa[..., 3:4] / 255.0, wb[..., 3:4] / 255.0
    out_a = aa * (1 - t) + ab * t
    num = wa[..., :3] * aa * (1 - t) + wb[..., :3] * ab * t
    rgb = num / np.maximum(out_a, 1e-4)
    alpha = np.clip(out_a, 0, 1) * 255.0
    return np.concatenate([np.clip(rgb, 0, 255), alpha], axis=2)


_FLOW_CACHE = {}


def morph(a, b, t, flow=None):
    """生成 a→b 之间 t 处的过渡帧（0<t<1）。"""
    _need_numpy()
    if t <= 0:
        return a
    if t >= 1:
        return b
    if flow is None:
        fy, fx, _ = estimate_flow(a, b)
        ry, rx, _ = estimate_flow(b, a)
        flow = (fy, fx, ry, rx)
    fy, fx, ry, rx = flow
    wa = warp(a, fy, fx, t)
    wb = warp(b, ry, rx, 1.0 - t)
    return _blend_rgba(wa, wb, t)


def _sig(arr):
    """粗指纹：防止 id() 复用导致位移场缓存串味。"""
    h, w = arr.shape[:2]
    step = max(16, h // 12)
    return float(arr[::step, ::step].sum())


def _flow_for(a, b):
    key = (id(a), id(b), _sig(a), _sig(b))
    hit = _FLOW_CACHE.get(key)
    if hit is not None:
        return hit
    fy, fx, res = estimate_flow(a, b)
    ry, rx, res2 = estimate_flow(b, a)
    flow = (fy, fx, ry, rx)
    _FLOW_CACHE[key] = (flow, max(res, res2))
    if len(_FLOW_CACHE) > 64:
        _FLOW_CACHE.clear()
    return flow, max(res, res2)


# ─────────────────────────────────────────────────────────
# 稳定性：落地线、中轴
# ─────────────────────────────────────────────────────────
def _content_bbox(arr):
    alpha = arr[..., 3]
    mask = alpha > 8
    if not mask.any():
        return None
    ys, xs = np.where(mask)
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def stabilize(frames, baseline=None, keep_axis=True, max_shift=4):
    """把每帧内容底边钉到 baseline，中轴轻微纠偏（只做小幅移动，避免整体跳）。"""
    _need_numpy()
    if not frames:
        return frames
    boxes = [_content_bbox(f) for f in frames]
    valid = [b for b in boxes if b]
    if not valid:
        return frames
    target_y = baseline if baseline else int(round(np.median([b[3] for b in valid])))
    center = frames[0].shape[1] / 2.0
    out = []
    for arr, box in zip(frames, boxes):
        if not box:
            out.append(arr)
            continue
        dy = target_y - box[3]
        dx = 0
        if keep_axis:
            mid = (box[0] + box[2]) / 2.0
            dx = int(round(center - mid))
            dx = max(-max_shift, min(max_shift, dx))
        dy = max(-max_shift, min(max_shift, dy))
        if dy == 0 and dx == 0:
            out.append(arr)
            continue
        canvas = np.zeros_like(arr)
        h, w = arr.shape[:2]
        sy0, sy1 = max(0, -dy), min(h, h - dy)
        sx0, sx1 = max(0, -dx), min(w, w - dx)
        if sy1 <= sy0 or sx1 <= sx0:
            out.append(arr)
            continue
        canvas[sy0 + dy:sy1 + dy, sx0 + dx:sx1 + dx] = arr[sy0:sy1, sx0:sx1]
        out.append(canvas)
    return out


def _clean_alpha(arr, floor=6):
    """清掉极低 alpha 的雾点，压缩素材体积并避免动画边缘发灰。"""
    out = arr.copy()
    a = out[..., 3]
    a[a < floor] = 0
    out[..., 3] = a
    return out


# ─────────────────────────────────────────────────────────
# 补帧主流程
# ─────────────────────────────────────────────────────────
def smooth_frames(frames, factor=2, jitter=0.0, baseline=None, log=None):
    """在每两帧之间插 factor-1 帧，返回 (新帧列表, 报告)。

    jitter: 给插值位置加一点相位偏移（0~0.25），避免所有过渡都落在正中间而显得机械。
    """
    _need_numpy()
    frames = pil_list(frames)
    if factor <= 1 or len(frames) < 2:
        return list(frames), {'in': len(frames), 'out': len(frames), 'pairs': 0,
                              'hard': 0, 'soft': 0, 'factor': 1}
    out = []
    hard = soft = pairs = 0
    spans = []          # 每对源帧对应输出里的 [起, 止) 区间，供重绘精确替换
    n = len(frames)
    for i in range(n - 1):
        a, b = frames[i], frames[i + 1]
        start = len(out)
        out.append(a)
        flow, residual = _flow_for(a, b)
        pairs += 1
        if residual > HARD_PAIR:
            hard += 1
        elif residual > SOFT_PAIR:
            soft += 1
        for k in range(1, factor):
            t = k / float(factor)
            if jitter:
                t = min(0.92, max(0.08, t + jitter * math.sin((i + k) * 2.399)))
            out.append(morph(a, b, t, flow))
        spans.append((i, start, len(out)))
        if log and pairs % 8 == 0:
            log('补帧 %d/%d' % (i + 1, n - 1))
    out.append(frames[-1])
    out = [_clean_alpha(f) for f in out]
    if baseline:
        out = stabilize(out, baseline=baseline)
    return out, {'in': n, 'out': len(out), 'pairs': pairs,
                 'hard': hard, 'soft': soft, 'factor': factor,
                 'spans': spans}


def retime(frames, target):
    """把帧数凑到 target：不够就按最长间隔插帧，多了就均匀抽帧。"""
    _need_numpy()
    frames = pil_list(frames)
    cur = len(frames)
    target = max(2, int(target))
    if cur >= target:
        if cur == target:
            return list(frames)
        idx = [round(i * (cur - 1) / (target - 1)) for i in range(target)]
        return [frames[i] for i in idx]
    need = target - cur
    gaps = cur - 1
    base = need // gaps if gaps else 0
    extra = need - base * gaps
    out = []
    for i in range(gaps):
        a, b = frames[i], frames[i + 1]
        out.append(a)
        flow, _ = _flow_for(a, b)
        cnt = base + (1 if i < extra else 0)
        for k in range(1, cnt + 1):
            out.append(morph(a, b, k / float(cnt + 1), flow))
    out.append(frames[-1])
    return out


def quality(frames):
    """可读的质量指标：抖动、鬼影、落地线稳定性。"""
    _need_numpy()
    if len(frames) < 2:
        return {'frames': len(frames), 'jitter': 0.0, 'ghost': 0.0, 'baseline_spread': 0}
    diffs, centers, bottoms = [], [], []
    for i in range(len(frames) - 1):
        a, b = frames[i], frames[i + 1]
        diffs.append(float(np.mean(np.abs(a[..., 3] - b[..., 3]))) / 255.0)
        box = _content_bbox(b)
        if box:
            centers.append((box[0] + box[2]) / 2.0)
            bottoms.append(box[3])
    # 一阶差分是"运动"，二阶差分才是"抖动"：匀速位移不该算进抖动里
    if len(centers) > 2:
        accel = np.diff(np.asarray(centers, dtype=np.float32), n=2)
        jitter = float(np.std(accel))
    else:
        jitter = 0.0
    spread = int(max(bottoms) - min(bottoms)) if bottoms else 0
    return {'frames': len(frames),
            'mean_alpha_delta': round(float(np.mean(diffs)), 4) if diffs else 0.0,
            'jitter': round(jitter, 2),
            'baseline_spread': spread}


# ─────────────────────────────────────────────────────────
# 对外主入口
# ─────────────────────────────────────────────────────────
def enrich(frames, fps=24.0, factor=None, target_seconds=None, baseline=None,
           jitter=0.0, generator=None, prompt=None, log=None):
    """补帧 + 稳像，返回 (新帧, 新fps, 报告)。

    factor:       每两帧之间插几段（2 = 帧数翻倍）
    target_seconds: 指定目标时长（秒），用它反推 factor，优先于 factor
    generator:    可选的豆包生图回调 (prev, nxt, t, prompt) -> PIL.Image，用于重绘跨度大的段落
    """
    _need_numpy()
    frames = [_rgba(f) if not isinstance(f, np.ndarray) else f for f in frames]
    src_n = len(frames)
    src_fps = float(fps or 24.0)

    if target_seconds and src_n > 1:
        want = max(src_n, int(round(target_seconds * src_fps)))
        factor = max(1, int(round((want - 1) / float(src_n - 1))) + 1)
    if factor is None:
        factor = 2
    factor = max(1, min(6, int(factor)))
    # 补帧靠提高帧率换取流畅度，总帧率不能突破 60，否则播放端会截断
    max_k = max(1, int(MAX_FPS_CAP // max(src_fps, 1.0)))
    factor = max(1, min(factor, max_k))

    out, report = smooth_frames(frames, factor=factor, jitter=jitter,
                               baseline=None, log=log)
    out_fps = min(MAX_FPS_CAP, src_fps * factor)

    # 跨度太大、本地形变救不了的那些对，交给生图模型重绘（有就用，没有就保持）
    redrawn = 0
    if generator is not None:
        out, redrawn = _redraw_hard(out, frames, report.get('spans') or [],
                                    generator, prompt, log)
    if baseline:
        out = stabilize(out, baseline=baseline)

    report.update({'src_frames': src_n, 'src_fps': src_fps,
                   'fps': out_fps, 'redrawn': redrawn,
                   'seconds': round(len(out) / max(out_fps, 1e-6), 2),
                   'quality': quality(out)})
    return out, out_fps, report


def _redraw_hard(out, src_frames, spans, generator, prompt, log):
    """对跨度极大的相邻源帧，让豆包按参考图重绘中间姿态，替换掉本地形变帧。"""
    if not src_frames or len(src_frames) < 2 or not spans:
        return out, 0
    scored = []
    for i, start, end in spans:
        if i + 1 >= len(src_frames) or end - start < 2:
            continue
        _, residual = _flow_for(src_frames[i], src_frames[i + 1])
        if residual > HARD_PAIR:
            scored.append((residual, i, start, end))
    if not scored:
        return out, 0

    from core import sticker_conform as conform_mod

    redrawn = 0
    for residual, i, start, end in sorted(scored, reverse=True)[:3]:
        prev_img = _to_image(src_frames[i])
        next_img = _to_image(src_frames[i + 1])
        try:
            gen = generator(prev_img, next_img, 0.5, prompt)
        except Exception as exc:  # noqa: BLE001 - 生图失败不能毁掉整段补帧
            if log:
                log('豆包重绘失败（第 %d 对）：%s' % (i, str(exc)[:60]))
            continue
        if gen is None:
            continue
        try:
            fixed = conform_mod.conform(gen, prev_img)
        except Exception as exc:  # noqa: BLE001 - 贴合不上就保留本地形变
            if log:
                log('生成图贴合失败（第 %d 对）：%s' % (i, str(exc)[:60]))
            continue
        mid = start + (end - start) // 2
        out[mid] = _clean_alpha(_rgba(fixed))
        redrawn += 1
        if log:
            log('豆包重绘第 %d 对（残差 %.3f）' % (i, residual))
    return out, redrawn


def frames_to_pil(frames):
    return [_to_image(f) if isinstance(f, np.ndarray) else f for f in frames]


def pil_list(frames):
    """PIL 列表 → ndarray 列表。"""
    _need_numpy()
    return [f if isinstance(f, np.ndarray) else _rgba(f) for f in frames]
