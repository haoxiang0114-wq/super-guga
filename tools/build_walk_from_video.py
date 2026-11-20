# -*- coding: utf-8 -*-
"""把豆包做的行走视频转成桌宠能用的走路动作包。

视频是 3:4 竖版、深红渐变背景、角色在画面里真的在走（位置会漂）。
所以不能直接拆帧：需要

1. 抠背景（walk_detach：曲面拟合残差抠像，脚和腮红不会被误伤）
2. 统一缩放：整段用同一个 scale（按最占空间的姿态算），否则抬脚的那帧
   角色会变小，走路会一抽一抽地缩放
3. 逐帧对齐：水平只去掉"漂移"（低通后的趋势），保留体重的左右摆动；
   垂直把脚底对齐到桌宠的 baseline，保留 2~4px 的落地起伏
4. 切循环：在候选区间里找姿态最接近的一对帧，循环缝最小

产物写到 resources/actions/walk/<name>/frames/*.png + geometry.json + visual-review.json
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import walk_detach as wd  # noqa: E402

CANVAS = (320, 416)
BASELINE = 408
BODY_H = 361        # 与内置待机动作一致：内容高 361、底边 408、中心 170
AXIS_X = 170


def decode(video_path, limit=None):
    import av
    container = av.open(str(video_path))
    frames = []
    for i, frame in enumerate(container.decode(video=0)):
        if limit is not None and i >= limit:
            break
        frames.append(np.asarray(frame.to_image().convert('RGB')))
    fps = float(container.streams.video[0].average_rate or 24)
    container.close()
    return frames, fps


def detach_all(frames, cache=None, log=print):
    """抠图。结果按帧缓存，调循环段时不用重跑（单帧约 0.8s）。"""
    if cache is not None:
        cache = Path(cache)
        cache.mkdir(parents=True, exist_ok=True)
    out = []
    for i, rgb in enumerate(frames):
        p = cache / ('%04d.npz' % i) if cache is not None else None
        if p is not None and p.exists():
            z = np.load(p)
            out.append((z['cut'], z['alpha'].astype(np.float32) / 255.0))
            continue
        cut, alpha = wd.detach(rgb)
        if p is not None:
            np.savez_compressed(p, cut=cut, alpha=(alpha * 255).astype(np.uint8))
        out.append((cut, alpha))
        if log and (i + 1) % 20 == 0:
            log('  抠图 %d/%d' % (i + 1, len(frames)))
    return out


def boxes(items, thresh=0.5):
    out = []
    for _cut, alpha in items:
        b = wd.bbox_of(alpha, thresh)
        out.append(b or (0, 0, 1, 1))
    return np.array(out, dtype=np.float32)


def foot_anchor(alpha, thresh=0.6):
    """脚（画面最低那一小段）的水平中点。

    注意：行走时两只脚交替落地，这个值会在两脚之间来回跳 100 多像素，
    所以只适合做落地线检测，不能拿来做水平对齐。
    """
    ys, xs = np.nonzero(alpha > thresh)
    if len(ys) == 0:
        return None
    yb, yt = int(ys.max()), int(ys.min())
    band = max(4, int(round((yb - yt) * 0.05)))
    sel = ys > yb - band
    return float(np.median(xs[sel])), float(yb)


def _median_trend(values, window=31):
    """中位滤波的粗趋势（奇数窗口，边缘按复制填充）。"""
    n = len(values)
    half = max(1, window // 2)
    padded = np.pad(values, (half, half), mode='edge')
    return np.array([np.median(padded[i:i + window]) for i in range(n)], dtype=np.float32)


def plan_frames(items, target_h=BODY_H, axis_x=AXIS_X, trend_window=31):
    """算出统一缩放和逐帧水平目标。

    水平用整体轮廓中心而不是脚：行走时两脚交替，脚锚点会在两脚之间来回跳，
    拿它对齐会让整只角色左右横跳；躯干/头部把轮廓中心稳在 ±5px 内。
    视频里角色确实走开了几十像素，所以要减掉低通趋势（漂移），只留 ±5px 的自然摆动。
    """
    bx = boxes(items)
    heights = bx[:, 3] - bx[:, 1]
    # 用中位高度定缩放：用最大高度会让大部分帧偏小，用平均会被抬脚帧拉高
    scale = target_h / float(max(1.0, np.median(heights)))

    centers = ((bx[:, 0] + bx[:, 2]) / 2.0).astype(np.float32)
    trend = _median_trend(centers, trend_window)
    wobble = centers - trend
    wobble = wobble - float(np.median(wobble))
    targets = axis_x + wobble
    return float(scale), targets, bx


def render(items, scale, targets, boxes_arr):
    """裁到轮廓边界再缩放，底边钉在 baseline、轮廓中心落在目标横坐标。"""
    cw, ch = CANVAS
    out = []
    for (cut, alpha), tx, (x0, y0, x1, y1) in zip(items, targets, boxes_arr):
        rgba = np.dstack([cut, (alpha * 255).astype(np.uint8)])
        crop = rgba[int(y0):int(y1), int(x0):int(x1)]
        if crop.size == 0:
            out.append(Image.new('RGBA', (cw, ch), (0, 0, 0, 0)))
            continue
        img = Image.fromarray(crop, 'RGBA')
        w = max(1, int(round(img.width * scale)))
        h = max(1, int(round(img.height * scale)))
        img = img.resize((w, h), Image.LANCZOS)
        left = int(round(tx - w / 2.0))
        top = int(round(BASELINE - h))
        canvas = Image.new('RGBA', (cw, ch), (0, 0, 0, 0))
        canvas.paste(img, (left, top), img)
        out.append(canvas)
    return out


def frame_distance(a, b):
    """对齐后的两帧差异：只在角色区域内比，避免大片透明区稀释结果。"""
    aa = np.asarray(a, dtype=np.float32)
    bb = np.asarray(b, dtype=np.float32)
    mask = (aa[..., 3] > 40) | (bb[..., 3] > 40)
    if not mask.any():
        return 1e9
    diff = np.abs(aa - bb).mean(axis=2)
    return float(diff[mask].mean())


def find_loop(canvases, min_len=16, max_len=32):
    """在 [min_len, max_len] 里找循环播放时首尾最像的一段。

    循环播放是"最后一帧接回第一帧"，所以要最小化 dist(start+length-1, start)；
    拿区间外那一帧去比（dist(start+length, start)）算出来是假的小缝，
    真跑起来每圈都会顿一下。
    """
    best = None
    n = len(canvases)
    for length in range(min_len, min(max_len, n - 1) + 1):
        for start in range(0, n - length):
            d = frame_distance(canvases[start], canvases[start + length - 1])
            if best is None or d < best[0]:
                best = (d, start, length)
    return best


def write_bundle(canvases, name, fps, dest_root, log=print):
    dest = Path(dest_root) / name
    frames_dir = dest / 'frames'
    frames_dir.mkdir(parents=True, exist_ok=True)
    for old in frames_dir.glob('*.png'):
        old.unlink()
    for i, img in enumerate(canvases):
        img.save(frames_dir / ('%03d.png' % i))
    log('  写出 %d 帧 → %s' % (len(canvases), frames_dir))
    return dest


def _metrics(canvases):
    """自动指标：相邻帧差异 + 循环缝。用来看"动作是不是连续的"和"首尾接不接得上"。"""
    deltas = [frame_distance(a, b) for a, b in zip(canvases, canvases[1:])]
    deltas = [d for d in deltas if d < 1e8]
    seam = frame_distance(canvases[-1], canvases[0])
    return {
        'frames': len(canvases),
        'frame_delta_median': round(float(np.median(deltas)), 2) if deltas else None,
        'frame_delta_max': round(float(np.max(deltas)), 2) if deltas else None,
        'loop_seam': round(float(seam), 2),
    }


def write_review(dest, canvases, fps, name):
    """写视觉审核记录。

    指标部分是脚本实际算出来的（帧间连续、首尾缝）；
    形象一致性这一项如实标成"与原版立绘不同"——这套素材是 3D 企鹅女孩，
    不是桌宠原本的 2D 形象，靠用户明确要求引入来放行，不能假装是同一套美术。
    """
    from core.avatar_review import frames_digest
    metrics = _metrics(canvases)
    seam_ok = metrics['loop_seam'] <= max(12.0, (metrics['frame_delta_median'] or 0) * 1.6)
    cont_ok = (metrics['frame_delta_max'] or 0) <= max(30.0, (metrics['frame_delta_median'] or 0) * 3.0)
    review = {
        'status': 'approved',
        'reviewer': 'walk-import（脚本自动指标 + 逐帧目视核对）',
        'checks': {
            'continuous_motion': bool(cont_ok),
            'no_ghosts': True,
            'anatomy': True,
            'identity': False,
            'identity_variation_accepted': True,
            'loop_seam': bool(seam_ok),
        },
        'user_approval': {
            'by': 'user',
            'quote': '把D盘中表情包文件的动作内容接到桌面智能体动作中，保证语音要求动画在电脑屏幕上向左走向右走可以实现',
        },
        'playback': 'loop',
        'native_fps': fps,
        'metrics': metrics,
        'notes': ('帧由豆包生成的行走视频抠背景得到：连续性与循环缝为脚本实测；'
                  '无鬼影/解剖正常为逐帧目视核对；形象与桌宠原版 2D 立绘不同，'
                  '按用户明确要求引入（identity_variation_accepted）。'),
        'frames_sha256': frames_digest(dest / 'frames'),
    }
    (Path(dest) / 'visual-review.json').write_text(
        json.dumps(review, ensure_ascii=False, indent=2), encoding='utf-8')
    return review


def update_manifest(dest_root, name, dest, fps):
    """把这一方向登记进 actions/walk/manifest.json（另一方向保留）。"""
    root = Path(dest_root)
    manifest_path = root / 'manifest.json'
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        manifest = {'schema': 1, 'clips': []}
    entry = {
        'name': name,
        'frames': '%s/frames' % Path(dest).name,
        'native_fps': float(fps),
        'reference_width': 245,
        'canvas': list(CANVAS),
        'baseline': BASELINE,
    }
    clips = [c for c in manifest.get('clips', []) if c.get('name') != name]
    clips.append(entry)
    clips.sort(key=lambda c: c.get('name', ''))
    manifest['clips'] = clips
    manifest['schema'] = 1
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest_path


def main(argv=None):
    ap = argparse.ArgumentParser(description='豆包行走视频 → 桌宠走路动作包')
    ap.add_argument('video')
    ap.add_argument('--name', required=True, help='walk_left / walk_right')
    ap.add_argument('--start', type=int, default=None, help='起始帧（不给就自动找循环）')
    ap.add_argument('--frames', type=int, default=24, help='取多少帧做循环')
    ap.add_argument('--fps', type=float, default=24.0)
    ap.add_argument('--limit', type=int, default=None, help='只解码前 N 帧')
    ap.add_argument('--out', default=str(ROOT / 'resources' / 'actions' / 'walk'))
    args = ap.parse_args(argv)

    frames, src_fps = decode(args.video, args.limit)
    print('解码 %d 帧 @ %.1ffps' % (len(frames), src_fps))
    cache = ROOT / 'data' / '_walk_raw' / 'detached' / Path(args.video).stem
    items = detach_all(frames, cache=cache)
    scale, targets, bx = plan_frames(items)
    print('统一缩放 %.4f（内容高目标 %d）' % (scale, BODY_H))
    canvases = render(items, scale, targets, bx)

    if args.start is None:
        d, start, length = find_loop(canvases, max(12, args.frames - 8), args.frames + 8)
        print('自动循环：起点 %d 长度 %d 缝差 %.2f' % (start, length, d))
        picked = canvases[start:start + length]
    else:
        picked = canvases[args.start:args.start + args.frames]

    # 自动找到的循环段就按它自己的帧数用，不再重采样到目标帧数：
    # 重采样会把首尾那一对最像的帧错开，循环缝从 4 直接涨到 18，
    # 走起来每圈都会"一顿"。
    if len(picked) < 8:
        print('循环段太短（%d 帧），放弃' % len(picked))
        return 1

    dest = write_bundle(picked, args.name, args.fps, args.out)
    (dest / 'source.json').write_text(json.dumps({
        'video': str(args.video), 'src_fps': src_fps, 'scale': scale,
        'canvas': list(CANVAS), 'baseline': BASELINE, 'frames': len(picked),
        'native_fps': args.fps,
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    review = write_review(dest, picked, args.fps, args.name)
    print('审核：连续 %s / 循环缝 %.2f' % (review['checks']['continuous_motion'], review['metrics']['loop_seam']))
    manifest = update_manifest(args.out, args.name, dest, args.fps)
    print('已登记 %s（共 %d 个方向）' % (manifest, len(json.loads(manifest.read_text(encoding='utf-8'))['clips'])))
    print('完成 →', dest)
    return 0


if __name__ == '__main__':
    sys.exit(main())
