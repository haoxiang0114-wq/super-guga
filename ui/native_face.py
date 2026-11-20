"""Live face overlays drawn directly over frames from the approved poke animation."""
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image
from PyQt5.QtCore import QRectF
from PyQt5.QtGui import QImage, QPixmap


_EYES = ((96, 205, 147, 249, 123, 224), (163, 200, 214, 244, 187, 220))


class NativeFace:
    def __init__(self, resources):
        root = Path(resources) / 'actions/poke-face'
        self.body = QPixmap(str(root / 'body.png'))
        self.mouths = [QPixmap(str(root / (name + '.png')))
                       for name in ('mouth-closed', 'mouth-mid', 'mouth-open')]
        self.lids = [QPixmap(str(root / (name + '.png'))) for name in ('lid-left', 'lid-right')]
        if any(item.isNull() for item in [self.body, *self.mouths, *self.lids]):
            raise FileNotFoundError('Missing poke face animation layers')
        with Image.open(root / 'body.png') as image:
            self._eyes = tuple(np.asarray(image.convert('RGBA').crop(eye[:4])).copy() for eye in _EYES)
        self._cached_gaze = lru_cache(maxsize=50)(self._gaze)

    def _gaze(self, side, horizontal, vertical):
        left, top, right, bottom, iris_x, iris_y = _EYES[side]
        pixels = self._eyes[side]
        rows, columns = np.indices(pixels.shape[:2])
        dx, dy = columns + left - iris_x, rows + top - iris_y
        weight = np.maximum(0, 1 - (dx / 15.) ** 4 - (dy / 15.) ** 4)
        weight *= np.clip((rows - 9) / 6, 0, 1)
        ix = np.clip(columns - horizontal * .8 * weight, 0, pixels.shape[1] - 1)
        iy = np.clip(rows - vertical * .75 * weight, 0, pixels.shape[0] - 1)
        x0, y0 = ix.astype(int), iy.astype(int)
        x1, y1 = np.minimum(x0 + 1, right - left - 1), np.minimum(y0 + 1, bottom - top - 1)
        rx, ry = (ix - x0)[..., None], (iy - y0)[..., None]
        rgba = ((pixels[y0, x0] * (1 - rx) + pixels[y0, x1] * rx) * (1 - ry)
                + (pixels[y1, x0] * (1 - rx) + pixels[y1, x1] * rx) * ry)
        return QPixmap.fromImage(QImage(np.uint8(rgba).tobytes(), right-left, bottom-top, QImage.Format_RGBA8888))

    def draw(self, painter, x, y, width, height, eyes, mouth, gaze):
        scale = height / 303.
        origin_x = x + (width - self.body.width() * scale) / 2
        origin_y = y + height - 408 * scale
        painter.save()
        painter.translate(origin_x, origin_y)
        painter.scale(scale, scale)
        painter.drawPixmap(0, 0, self.body)
        gx, gy = round(gaze[0] * 4), round(gaze[1] * 2)
        for side, eye in enumerate(_EYES):
            if eyes > .55 and (gx or gy):
                painter.drawPixmap(eye[0], eye[1], self._cached_gaze(side, gx, gy))
            if eyes < .72:
                painter.setOpacity(min(1., (.72 - eyes) / .47))
                painter.drawPixmap(eye[0], eye[1], self.lids[side])
                painter.setOpacity(1.)
        if mouth < .16:
            painter.drawPixmap(120, 236, self.mouths[0])
        elif mouth > .37:
            index = 1 if mouth < .70 else 2
            painter.drawPixmap(120, 236, self.mouths[index])
        painter.restore()
        return QRectF(origin_x, origin_y, self.body.width() * scale, self.body.height() * scale)
