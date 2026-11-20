"""PPT 生成：模型给结构化大纲，这里只负责版式渲染与文件校验。

设计边界：
- 大纲完全由模型提供（标题/要点/备注/主题色），本模块不改写内容，也不套死版式，
  换主题只要换 outline，不需要改代码。
- 内置版式全部用 python-pptx 自绘（矩形、线条、文本框），不引用任何第三方模板或素材，
  没有模板版权问题；字体用系统字体（微软雅黑），不嵌入文件。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

from core import guga_paths

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = guga_paths.DELIVERY
PAGE_W, PAGE_H = Inches(13.333), Inches(7.5)
FONT = 'Microsoft YaHei'
INK = RGBColor(0x16, 0x23, 0x3A)
MUTED = RGBColor(0x6B, 0x76, 0x84)
PAPER = RGBColor(0xFA, 0xFB, 0xFD)
DEFAULTS = dict(accent='#2B6DE5', dark='#16233A', name='咕嘎')


class PPTError(ValueError):
    """大纲或输出不可用时抛出，调用方按 message 改大纲重试。"""


def _hex(value, fallback):
    text = str(value or '').strip()
    match = re.fullmatch(r'#?([0-9a-fA-F]{6})', text)
    return text if match else fallback


def _rgb(value):
    text = str(value or '').strip().lstrip('#')
    if not re.fullmatch(r'[0-9a-fA-F]{6}', text):
        raise PPTError('颜色必须是 #RRGGBB：' + str(value))
    return RGBColor.from_string(text.upper())


def _slant(run, size, bold=False, color=None, font=FONT):
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.name = font
    run.font.color.rgb = color or INK
    rpr = run._r.get_or_add_rPr()
    ea = rpr.find(qn('a:ea'))
    if ea is None:
        ea = rpr.makeelement(qn('a:ea'), {})
        rpr.append(ea)
    ea.set('typeface', font)


def _text(slide, value, left, top, width, height, size, bold=False, color=None,
          align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, spacing=1.15):
    box = slide.shapes.add_textbox(left, top, width, height)
    frame = box.text_frame
    frame.word_wrap = True
    frame.vertical_anchor = anchor
    frame.margin_left = frame.margin_right = 0
    frame.margin_top = frame.margin_bottom = 0
    lines = str(value or '').split('\n')
    for index, line in enumerate(lines):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        paragraph.alignment = align
        paragraph.line_spacing = spacing
        _slant(paragraph.add_run(), size, bold, color)
        paragraph.runs[0].text = line
    return box


def _shape(slide, left, top, width, height, fill, kind=MSO_SHAPE.RECTANGLE):
    shape = slide.shapes.add_shape(kind, left, top, width, height)
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill
    shape.line.fill.background()
    shape.shadow.inherit = False
    return shape


def _validate(outline):
    if not isinstance(outline, dict):
        raise PPTError('outline 必须是对象：{title, subtitle, slides:[{title, bullets, notes}]}')
    slides = outline.get('slides')
    if not isinstance(slides, list) or not slides:
        raise PPTError('outline.slides 不能为空，至少 1 页；每页给 title 和 bullets')
    if len(slides) > 30:
        raise PPTError('单次最多 30 页，请精简到演示需要的页数')
    clean = []
    for index, slide in enumerate(slides, 1):
        if not isinstance(slide, dict):
            raise PPTError('第 %d 页必须是对象' % index)
        title = str(slide.get('title') or '').strip()
        if not title:
            raise PPTError('第 %d 页缺少 title' % index)
        if len(title) > 80:
            raise PPTError('第 %d 页标题超过 80 字' % index)
        bullets = [str(x).strip() for x in (slide.get('bullets') or []) if str(x).strip()]
        if len(bullets) > 8:
            raise PPTError('第 %d 页要点超过 8 条' % index)
        clean.append(dict(kind=str(slide.get('kind') or '').strip().lower(),
                          title=title, bullets=bullets,
                          notes=str(slide.get('notes') or '')[:2000]))
    return clean


def _cover(prs, deck, accent, slides_total):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _shape(slide, 0, 0, PAGE_W, PAGE_H, PAPER)
    _shape(slide, 0, 0, Inches(0.22), PAGE_H, accent)
    _text(slide, deck['title'], Inches(0.95), Inches(2.25), Inches(11.2), Inches(1.6), 42, True)
    subtitle = deck.get('subtitle')
    if subtitle:
        _text(slide, subtitle, Inches(0.95), Inches(3.9), Inches(10.6), Inches(0.9), 19, False, MUTED)
    _shape(slide, Inches(0.95), Inches(3.62), Inches(1.5), Inches(0.06), accent)
    _text(slide, deck.get('author') or time.strftime('%Y-%m-%d'), Inches(0.95),
          Inches(6.5), Inches(6), Inches(0.5), 12, False, MUTED)


def _section(prs, page, accent, dark, number):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _shape(slide, 0, 0, PAGE_W, PAGE_H, dark)
    _shape(slide, 0, Inches(4.9), PAGE_W, Inches(0.06), accent)
    _text(slide, '%02d' % number, Inches(1.0), Inches(2.15), Inches(2.2), Inches(1.1), 40, True, accent)
    _text(slide, page['title'], Inches(1.0), Inches(3.25), Inches(10.8), Inches(1.2), 34, True,
          RGBColor(0xFF, 0xFF, 0xFF))


def _content(prs, page, deck, accent, number, total):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _shape(slide, 0, 0, PAGE_W, PAGE_H, PAPER)
    _text(slide, page['title'], Inches(0.95), Inches(0.62), Inches(11.4), Inches(0.9), 30, True)
    _shape(slide, Inches(0.95), Inches(1.5), Inches(1.2), Inches(0.06), accent)
    if page['bullets']:
        top = Inches(1.95)
        width = Inches(11.3)
        height = Inches(4.3)
        box = slide.shapes.add_textbox(Inches(0.95), top, width, height)
        frame = box.text_frame
        frame.word_wrap = True
        frame.margin_left = frame.margin_right = 0
        for index, bullet in enumerate(page['bullets']):
            paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
            paragraph.line_spacing = 1.3
            paragraph.space_after = Pt(12)
            _slant(paragraph.add_run(), 20, True, accent)
            paragraph.runs[0].text = '▍ '
            _slant(paragraph.add_run(), 19, False, INK)
            paragraph.runs[1].text = bullet
    _text(slide, deck['title'], Inches(0.95), Inches(6.78), Inches(7), Inches(0.4), 11, False, MUTED)
    _text(slide, '%d / %d' % (number, total), Inches(10.4), Inches(6.78), Inches(2), Inches(0.4),
          11, False, MUTED, PP_ALIGN.RIGHT)
    return slide


def _closing(prs, page, accent, dark):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _shape(slide, 0, 0, PAGE_W, PAGE_H, dark)
    _shape(slide, 0, 0, Inches(0.22), PAGE_H, accent)
    _text(slide, page['title'], Inches(1.0), Inches(3.0), Inches(11), Inches(1.4), 40, True,
          RGBColor(0xFF, 0xFF, 0xFF))
    if page['bullets']:
        _text(slide, '\n'.join(page['bullets']), Inches(1.0), Inches(4.45), Inches(10.5),
              Inches(1.2), 17, False, RGBColor(0xC9, 0xD3, 0xE0))


def _safe_name(title):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', '-', str(title or 'deck')).strip(' -')
    return (name[:40] or 'deck') + '-' + time.strftime('%Y%m%d-%H%M%S') + '.pptx'


def build(outline, output=None, accent=None, template=None):
    """按大纲渲染 pptx，返回 {path, slides, size, verified}。"""
    slides = _validate(outline)
    theme = outline.get('theme') if isinstance(outline.get('theme'), dict) else {}
    accent_hex = _hex(accent or theme.get('accent'), DEFAULTS['accent'])
    dark_hex = _hex(theme.get('dark'), DEFAULTS['dark'])
    deck = dict(title=str(outline.get('title') or slides[0]['title']),
                subtitle=str(outline.get('subtitle') or ''),
                author=str(outline.get('author') or ''),
                year=str(outline.get('date') or ''))
    if template:
        target = Path(template).expanduser()
        if not target.is_file():
            raise PPTError('模板文件不存在：' + str(target))
        prs = Presentation(str(target))
    else:
        prs = Presentation()
        prs.slide_width, prs.slide_height = PAGE_W, PAGE_H
    accent_rgb, dark_rgb = _rgb(accent_hex), _rgb(dark_hex)
    _cover(prs, deck, accent_rgb, len(slides) + 1)
    total = len(slides) + 1
    for number, page in enumerate(slides, 2):
        if page['kind'] == 'section':
            _section(prs, page, accent_rgb, dark_rgb, number - 1)
        elif page['kind'] == 'closing':
            _closing(prs, page, accent_rgb, dark_rgb)
        else:
            _content(prs, page, deck, accent_rgb, number, total)
        if page['notes']:
            prs.slides[-1].notes_slide.notes_text_frame.text = page['notes']
    folder = OUTPUT_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = Path(output).expanduser() if output else folder / _safe_name(deck['title'])
    if path.suffix.lower() != '.pptx':
        path = path.with_suffix('.pptx')
    path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(path))
    return inspect(path)


def inspect(path):
    """重新打开产物做校验：页数、每页是否有文字、文件大小。"""
    path = Path(path).expanduser().resolve(strict=True)
    if path.suffix.lower() != '.pptx':
        raise PPTError('产物不是 .pptx：' + path.name)
    prs = Presentation(str(path))
    titles, texts = [], 0
    for slide in prs.slides:
        words = [shape.text_frame.text.strip() for shape in slide.shapes
                 if shape.has_text_frame and shape.text_frame.text.strip()]
        texts += len(words)
        titles.append(words[0] if words else '')
    if not titles or texts == 0:
        raise PPTError('生成的文件没有可读文字，请检查大纲')
    return dict(path=str(path), slides=len(titles), texts=texts,
                size=path.stat().st_size, titles=titles[:30], verified=True)


def outline_of(text_or_path):
    """把 JSON 字符串或文件路径归一成大纲对象。"""
    if isinstance(text_or_path, dict):
        return text_or_path
    raw = str(text_or_path or '').strip()
    if not raw:
        raise PPTError('缺少 outline')
    candidate = Path(raw).expanduser()
    if len(raw) < 400 and not raw.startswith('{') and candidate.is_file():
        raw = candidate.read_text(encoding='utf-8')
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise PPTError('outline 不是合法 JSON：%s' % exc) from None
