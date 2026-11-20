"""Black embroidered caps, anchored to the head in native sprite frames."""
from pathlib import Path

import numpy as np
from PyQt5.QtCore import QRectF
from PyQt5.QtGui import QImage, QPixmap


def draw_executor_accessory(painter, anim, x, y, frame, *, native=False, mirrored=False):
    mode = anim.executor_appearance
    if mode not in ('codex', 'dsh', 'workbuddy'):
        return
    if mode not in anim._executor_caps:
        path = Path(anim._res_dir) / 'accessories' / (mode + '-black-cap.png')
        anim._executor_caps[mode] = QPixmap(str(path))
    cap = anim._executor_caps[mode]
    if cap.isNull() or frame.isNull():
        return

    center, crown = _head_anchor(anim, frame)
    if not native:
        center += anim.head_yaw * anim._dw * .038
        crown += anim.head_nod * anim._dh * .017
    w = anim._dw * .51
    h = w * .78
    painter.save()
    painter.translate(x + center, y + crown + anim._dh * .135)
    if mirrored:
        painter.scale(-1, 1)
    if not native:
        painter.rotate(anim.head_tilt * 8)
    painter.drawPixmap(QRectF(-w / 2, -h, w, h), cap, QRectF(cap.rect()))
    painter.restore()


def _head_anchor(anim, frame):
    key = frame.cacheKey()
    cache = anim._hat_anchors
    if key in cache:
        return cache[key]
    alpha = frame.toImage().convertToFormat(QImage.Format_Alpha8)
    mask = np.frombuffer(alpha.constBits().asstring(alpha.sizeInBytes()), dtype=np.uint8)
    mask = mask.reshape(alpha.height(), alpha.bytesPerLine())[:, :alpha.width()]
    height, width = mask.shape
    left, right = int(width * .27), int(width * .73)
    rows = np.flatnonzero((mask[:height // 2, left:right] >= 180).sum(axis=1) >= anim._dw * .17)
    if rows.size:
        top = int(rows[0])
        band = mask[top + round(anim._dh * .06), :] >= 180
        span = np.flatnonzero(band[left:right]) + left
        center = float(span.mean()) if span.size else width / 2
    else:
        top, center = height * .06, width / 2
    if len(cache) >= 256:
        cache.pop(next(iter(cache)))
    cache[key] = (center, top)
    return cache[key]
