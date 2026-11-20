"""Localized face animation using the original illustration as its texture."""
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from PyQt5.QtGui import QImage, QPixmap


# Coordinates belong to the unscaled source PNGs. Each eye lists crop and iris center.
LANDMARKS = {
    'playful': (((50, 116, 99, 151), (82, 133)), ((123, 116, 171, 151), (147, 133)), (115, 162, 11)),
    'sad': (((53, 112, 101, 149), (83, 134)), ((129, 112, 178, 149), (151, 134)), (117, 167, 12)),
    'surprised': (((49, 112, 94, 149), (75, 133)), ((120, 112, 165, 149), (137, 133)), (107, 165, 8)),
    'sleepy': (None, None, (107, 160, 8)),
}


def _skin_patch(image, box):
    """Interpolate warm skin tones from clean pixels beside a feature."""
    pixels = np.asarray(image.crop(box), dtype=float)
    weights = np.linspace(0, 1, pixels.shape[1])[None, :, None]
    skin = pixels[:, :1] * (1 - weights) + pixels[:, -1:] * weights
    return Image.fromarray(np.uint8(skin), 'RGBA')


def _feather(size):
    mask = Image.new('L', size)
    ImageDraw.Draw(mask).ellipse((0, 0, size[0] - 1, size[1] - 1), fill=255)
    return mask.filter(ImageFilter.GaussianBlur(1.2))


class FaceDetails:
    def __init__(self, resource_dir, width, height):
        self.root = Path(resource_dir) / 'expressions'
        self.size = width, height
        self._textures = {}
        # Cache completed renders without retaining old animators after a window closes.
        self._render = lru_cache(maxsize=32)(self._render_frame)

    def frame(self, expression, eyes=1., mouth=0., gaze=(0., 0.), speaking=False):
        return self._render(expression, round(eyes * 8), round(mouth * 10),
                            round(gaze[0] * 4), round(gaze[1] * 3), speaking)

    def _texture(self, name):
        if name not in self._textures:
            with Image.open(self.root / (name + '_transparent.png')) as source:
                self._textures[name] = source.convert('RGBA')
        return self._textures[name]

    def _eye(self, frame, original, item, blink, gx, gy):
        box, (ix, iy) = item
        left, top, right, bottom = box
        crop = original.crop(box)
        width, height = crop.size
        if blink < 8:
            # The eyelid is a continuation of the cheek. Using the lateral
            # border samples here would pick up the dark hair or outer lash.
            cheek = original.crop((left, bottom, right, bottom + 4))
            clean_start = 8 if ix - left > width * .5 else 2
            clean_end = width - 2 if clean_start == 8 else width - 8
            colors = np.asarray(cheek, dtype=float)[:, clean_start:clean_end, :3]
            base_color = np.percentile(colors.reshape(-1, 3), 68, axis=0)
            skin = np.empty((height, width, 4), dtype=np.uint8)
            shade = np.linspace(.91, 1.01, height)[:, None, None]
            skin[:, :, :3] = np.uint8(np.clip(base_color * shade, 0, 255))
            skin[:, :, 3] = 255
            pad = 5
            area = Image.new('L', (width + pad * 2, height + pad * 2))
            ImageDraw.Draw(area).polygon([
                (pad - 1, pad + 6), (pad + 8, pad + 1), (pad + width - 9, pad),
                (pad + width + 1, pad + 7), (pad + width - 1, pad + 21),
                (pad + width - 9, pad + height), (pad + 8, pad + height),
                (pad + 1, pad + height - 8),
            ], fill=255)
            mask = area.filter(ImageFilter.GaussianBlur(2.35)).crop((pad, pad, pad + width, pad + height))
            frame.paste(Image.fromarray(skin), (left, top), mask)
            y = round(height * .66)
            if blink > 1:
                # Retract the eyelid from the lash line, with the cheek anchored.
                open_height = max(2, round((height - 5) * blink / 8))
                visible = crop.resize((width, open_height), Image.Resampling.BICUBIC)
                frame.paste(visible, (left, top + y - round(open_height * .65)),
                            _feather(visible.size))
            else:
                lid = Image.new('RGBA', crop.size)
                ImageDraw.Draw(lid).arc((4, y - 6, width - 5, y + 4), 9, 171,
                                       (78, 47, 38, 255), width=2)
                frame.alpha_composite(lid, (left, top))
            return
        if not (gx or gy):
            return
        # Move the iris within the aperture. The lash and corners stay anchored.
        px = np.asarray(crop)
        ys, xs = np.indices((height, width))
        u, v = xs + left - ix, ys + top - iy
        influence = np.maximum(0, 1 - (u / 18.) ** 4 - (v / 16.) ** 4)
        influence *= np.clip((ys - 8) / 5., 0, 1)
        nx = np.clip(np.rint(xs - gx * .86 * influence).astype(int), 0, width - 1)
        ny = np.clip(np.rint(ys - gy * .60 * influence).astype(int), 0, height - 1)
        frame.paste(Image.fromarray(px[ny, nx]), (left, top))

    def _mouth(self, frame, original, mark, opening, expression):
        x, y, span = mark
        box = x - 17, y - 9, x + 18, y + 10
        frame.paste(_skin_patch(original, box), box[:2], _feather((35, 19)))
        # Antialias the lip at source scale, even for a one-pixel closure.
        layer = Image.new('RGBA', (112, 64))
        draw = ImageDraw.Draw(layer)
        width = (span * 2 + (4 if expression == 'happy' else 0)) * (2.2 + opening * .11)
        center, high = 56, 26
        if opening < 2:
            draw.arc((center - width / 2, high - 9, center + width / 2, high + 6),
                     12, 168, (135, 69, 55, 255), width=3)
        else:
            height = 4 + opening * 3.3
            jaw = (center - width / 2, high - height * .36, center + width / 2, high + height * .64)
            draw.ellipse(jaw, (105, 44, 39, 255), outline=(150, 75, 63, 255), width=2)
            if opening >= 4:
                draw.ellipse((center - width * .25, jaw[3] - height * .39,
                              center + width * .25, jaw[3] - 2), (192, 96, 86, 255))
        frame.alpha_composite(layer.resize((28, 16), Image.Resampling.LANCZOS), (x - 14, y - 7))

    def _render_frame(self, expression, blink, mouth, gx, gy, speaking):
        # The original neutral/happy art has both eyes shut in a laugh. Use its
        # open-eyed sibling for active attention and preserve the emotion in the mouth.
        name = 'surprised' if expression in ('normal', 'happy') else expression
        if name not in LANDMARKS:
            name = 'playful'
        original = self._texture(name)
        result = original.copy()
        left, right, mouth_mark = LANDMARKS[name]
        for eye in (left, right):
            if eye:
                self._eye(result, original, eye, blink, gx, gy)
        if speaking or expression in ('normal', 'happy'):
            self._mouth(result, original, mouth_mark, mouth, expression)
        scale = min(self.size[0] / result.width, self.size[1] / result.height)
        result = result.resize((round(result.width * scale), round(result.height * scale)),
                               Image.Resampling.LANCZOS)
        canvas = Image.new('RGBA', self.size)
        canvas.paste(result, ((canvas.width - result.width) // 2, (canvas.height - result.height) // 2))
        data = canvas.tobytes()
        return QPixmap.fromImage(QImage(data, *canvas.size, QImage.Format_RGBA8888).copy())
