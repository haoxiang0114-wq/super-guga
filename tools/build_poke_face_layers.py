"""Author the talking rig out of the approved poke animation's native PNG layers."""
from pathlib import Path
from shutil import copyfile

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
POKE = ROOT / 'resources/actions/personality-20261114/poke/frames'
OUT = ROOT / 'resources/actions/poke-face'


def take(number):
    with Image.open(POKE / f'{number:04}.png') as image:
        return image.convert('RGBA')


def patch(number, crop, output_size, shape, name):
    image = take(number).crop(crop).resize(output_size, Image.Resampling.LANCZOS)
    mask = Image.new('L', output_size)
    ImageDraw.Draw(mask).rounded_rectangle(shape, radius=8, fill=255)
    image.putalpha(mask.filter(ImageFilter.GaussianBlur(2.5)))
    image.save(OUT / (name + '.png'), optimize=True)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    copyfile(POKE / '0000.png', OUT / 'body.png')
    # Part dimensions & origins are calibrated against frame 0 in ui/native_face.py.
    patch(144, (119, 233, 178, 263), (61, 32), (5, 4, 54, 26), 'mouth-closed')
    patch(153, (118, 232, 181, 274), (61, 32), (7, 3, 53, 27), 'mouth-mid')
    patch(156, (118, 230, 184, 280), (61, 32), (5, 3, 55, 28), 'mouth-open')
    patch(134, (96, 213, 145, 252), (51, 44), (2, 3, 48, 40), 'lid-left')
    patch(134, (166, 216, 215, 254), (51, 44), (2, 3, 48, 40), 'lid-right')


if __name__ == '__main__':
    main()
