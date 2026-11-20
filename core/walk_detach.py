# -*- coding: utf-8 -*-
"""抠掉豆包行走视频的背景，输出透明帧。

背景是「深红→洋红→浅粉」的空间渐变，同一个色号在画面不同位置完全不一样，
所以不能用一个颜色值加 tolerance，也不能用「红通道高就是背景」这种硬判据
（会把深橙色的脚一起吃掉）。这里用影视抠像的标准做法：

1. 只用画面最外圈像素，给每个颜色通道拟合一张二次曲面 B(x,y)；
   渐变背景是平滑的空间函数，二次曲面能拟合到残差 < 8。
2. 残差 = 像素与它所在位置的背景估计之差。背景残差小，角色（黑、白、黄、肤）
   残差大，与颜色本身无关——所以黄脚不会再被当成背景。
3. 残差做软阈值得到 alpha，再走一次「从画面中心取最大连通块」，
   右下角「豆包AI生成」水印这类孤立小块就自动消失了。
4. 半透明边缘做去红溢出，避免角色轮廓挂一圈洋红描边。
"""
import numpy as np
from PIL import Image, ImageFilter

_TERMS = 6


def _design(xs, ys, w, h):
    """归一化二次多项式基：1, u, v, u², uv, v²"""
    u = xs / float(max(1, w - 1)) - 0.5
    v = ys / float(max(1, h - 1)) - 0.5
    return np.stack([np.ones_like(u), u, v, u * u, u * v, v * v], axis=1)


def fit_background(rgb, ring=0.07, robust=2):
    """用外圈像素拟合背景曲面；返回 (拟合函数, 残差图)。"""
    h, w = rgb.shape[:2]
    ys, xs = np.mgrid[0:h, 0:w]
    mx = max(2, int(round(w * ring)))
    my = max(2, int(round(h * ring)))
    band = (xs < mx) | (xs >= w - mx) | (ys < my) | (ys >= h - my)
    # 采样稀疏化：外圈全用太浪费，每 3 像素取一个足够
    pick = band & (((xs % 3) == 0) & ((ys % 3) == 0))
    if pick.sum() < 2000:
        pick = band
    px, py = xs[pick].astype(np.float64), ys[pick].astype(np.float64)
    samples = rgb[pick].astype(np.float64)
    A = _design(px, py, w, h)

    coef = None
    keep = np.ones(len(px), dtype=bool)
    for _ in range(max(1, robust)):
        if keep.sum() < _TERMS * 20:
            break
        coef = np.linalg.lstsq(A[keep], samples[keep], rcond=None)[0]
        pred = A @ coef
        err = np.abs(pred - samples).max(axis=1)
        # 只用贴合背景的样本再拟一遍，把误入外圈的角/影子剔掉
        keep = err < max(6.0, np.percentile(err, 92))

    allA = _design(xs.ravel().astype(np.float64), ys.ravel().astype(np.float64), w, h)
    bg = (allA @ coef).reshape(h, w, 3)
    resid = np.abs(rgb.astype(np.float64) - bg).max(axis=2)
    return coef, resid, bg


def _component_from_center(mask):
    """保留包含画面中心的内容连通块（水印等孤立小块被丢掉）。

    用 PIL 的 C 实现做灌水，比 Python 逐像素 DFS 快两个数量级。
    """
    from PIL import ImageDraw
    h, w = mask.shape
    img = Image.fromarray((mask * 255).astype(np.uint8), 'L').copy()
    # 中心不一定落在角色上（角色偏一侧），从中心附近的连通点里挑一个
    cy0, cx0 = h // 2, w // 2
    seed = None
    for rad in range(0, max(h, w) // 2, 8):
        y0, y1 = max(0, cy0 - rad), min(h, cy0 + rad + 1)
        x0, x1 = max(0, cx0 - rad), min(w, cx0 + rad + 1)
        sub = img.crop((x0, y0, x1, y1))
        arr = np.asarray(sub)
        yy, xx = np.nonzero(arr >= 128)
        if len(yy):
            seed = (int(x0 + xx[0]), int(y0 + yy[0]))
            break
    if seed is None:
        return np.zeros_like(mask)
    # 从种子把整块前景灌成 128（thresh=1 只连 255 的像素），孤立小块保持 255
    ImageDraw.floodfill(img, seed, 128, thresh=1)
    return np.asarray(img) == 128


def _fill_holes(mask):
    """把主体轮廓内部的"洞"填实。

    嘴内、腮红阴影这类深红区域与背景同色，残差抠像会把它们判成透明，
    露出来就是一块棋盘格。轮廓内部一律当实体，透明度只在轮廓边缘做过渡。
    """
    from PIL import ImageDraw
    h, w = mask.shape
    inv = Image.fromarray(((~mask) * 255).astype(np.uint8), 'L').copy()
    seed = None
    for xy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        if inv.getpixel(xy) >= 128:
            seed = xy
            break
    if seed is None:
        return mask.copy()
    ImageDraw.floodfill(inv, seed, 0, thresh=1)
    enclosed = np.asarray(inv) >= 128      # 外部灌不到 = 被轮廓包住的洞
    return mask | enclosed


def detach(rgb, lo=14.0, hi=42.0, feather=1.1):
    """rgb: HxWx3 uint8 → (去溢出的 rgb, alpha 0..1)"""
    _, resid, _bg = fit_background(rgb)
    soft = np.clip((resid - lo) / max(1e-6, hi - lo), 0.0, 1.0)
    body = _component_from_center(soft > 0.5)
    solid = _fill_holes(body)
    # 轮廓内部实心（保护嘴/腮红），边缘保留残差算出的软过渡
    erode = Image.fromarray((solid * 255).astype(np.uint8), 'L').filter(ImageFilter.MinFilter(3))
    core = np.asarray(erode) >= 128
    alpha = np.maximum(np.where(core, 1.0, 0.0), soft * solid.astype(np.float32))
    if feather > 0:
        img = Image.fromarray((alpha * 255).astype(np.uint8), 'L')
        alpha = np.asarray(img.filter(ImageFilter.GaussianBlur(feather))).astype(np.float32) / 255.0
    alpha = np.where(core, 1.0, alpha).clip(0.0, 1.0)

    out = rgb.astype(np.float32).copy()
    r = out[..., 0]
    g = out[..., 1]
    # 脚底那圈红光：视频里是"角色踩在红底上"的反射，色号跟背景几乎一样，
    # 残差抠像会把它留下成一片红边。轮廓内部（core）不动，避免连带抠掉
    # 嘴里的暗红和项圈上的红块。
    seep = (alpha > 0.02) & (r - g > 55) & (g < 75) & ~core
    if seep.any():
        alpha = np.where(seep, alpha * 0.12, alpha)
    rim = (alpha > 0.03) & (alpha < 0.96) & ~core
    if rim.any():
        gb = (g + out[..., 2]) / 2.0
        spill = rim & (r > gb + 10)
        out[..., 0] = np.where(spill, gb + 10, r)
    return out.clip(0, 255).astype(np.uint8), alpha.clip(0.0, 1.0)


def bbox_of(alpha, thresh=0.35):
    ys, xs = np.nonzero(alpha > thresh)
    if len(ys) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
