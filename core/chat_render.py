# -*- coding: utf-8 -*-
"""Markdown → Qt 富文本（只负责“展示”，不负责朗读）。

为什么单独一层：
- 桌面端聊天窗是 QTextBrowser，Qt 5.15 只支持 HTML4/CSS2.1 子集（没有圆角、
  flex、伪元素），所以这里用内联样式 + 单元格表格拼出“卡片式”排版；
- 语音朗读必须去掉 Markdown 符号，展示必须保留，两者不能共用一个清洗函数；
- 复制按钮用 ``guga-copy:<消息key>:<序号>`` 锚点交给 UI 处理，渲染层不碰剪贴板。

对外只有 :func:`render_markdown`，纯函数、无 Qt 依赖，便于单元测试。
若 markdown-it-py / pygments 任一缺失，自动退化成“转义 + 换行”的纯文本，
聊天不会因此打不开。
"""
from __future__ import annotations

import html
import re

try:  # 这两个包在桌宠环境里已存在；缺失时降级而不是崩溃
    from markdown_it import MarkdownIt
except Exception:  # pragma: no cover - 环境兜底
    MarkdownIt = None

try:
    from pygments import highlight as _pyg_highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import get_lexer_by_name
except Exception:  # pragma: no cover - 环境兜底
    _pyg_highlight = None
    get_lexer_by_name = None

UI_FONT = 'Microsoft YaHei UI'
CODE_FONT = 'Consolas'
INK = '#303540'
MUTED = '#8f959e'
FAINT = '#b3b8c2'
ACCENT = '#476bec'
CODE_BG = '#f6f8fa'
CODE_BORDER = '#e3e6ea'
QUOTE_BG = '#f3f6ff'
TABLE_HEAD_BG = '#f7f8fa'

_MD = (MarkdownIt('commonmark', {'html': False}).enable('strikethrough')
       if MarkdownIt else None)
_FORMATTER = (HtmlFormatter(noclasses=True, nowrap=True, style='friendly', stripnl=False)
              if _pyg_highlight else None)


def _esc(text):
    return html.escape(str(text or ''), quote=False)


# --------------------------------------------------------------------------- #
# inline
# --------------------------------------------------------------------------- #
def _inline(text):
    if _MD is None:
        return _esc(text)
    out = _MD.renderInline(str(text or '')).strip()
    out = out.replace('<strong>', '<b>').replace('</strong>', '</b>')
    out = out.replace('<em>', '<i>').replace('</em>', '</i>')
    out = re.sub(r'<code>(.*?)</code>', lambda m: (
        '<span style="font-family:%s;font-size:12px;background-color:#f2f3f5;'
        'color:#c7254e">%s</span>' % (CODE_FONT, m.group(1))), out, flags=re.S)
    out = re.sub(r'<a href="([^"]*)"',
                 lambda m: '<a href="%s" style="color:%s">' % (m.group(1), ACCENT), out)
    out = out.replace('<br>\n', '<br>')
    return out


# --------------------------------------------------------------------------- #
# blocks
# --------------------------------------------------------------------------- #
def _code_card(lang, source, copy_token=None):
    """代码块卡片：语言标签 + 复制锚点 + 语法高亮。"""
    lang = (lang or 'text').strip().split()[0][:20] or 'text'
    token = ''
    if callable(copy_token):
        token = copy_token(source) or ''
    copy_link = ('&nbsp;&nbsp;<a href="%s" style="font-family:%s;font-size:10px;'
                 'color:%s">复制</a>' % (token, UI_FONT, ACCENT)) if token else ''
    colored = _esc(source)
    if _pyg_highlight is not None:
        try:
            lexer = get_lexer_by_name(lang, stripnl=False, ensurenl=False)
            colored = _pyg_highlight(source, lexer, _FORMATTER).strip('\n')
        except Exception:
            colored = _esc(source)
    return (
        '<table width="100%%" cellspacing="0" cellpadding="0" style="margin:8px 0">'
        '<tr><td bgcolor="%s" style="padding:2px 9px;border:1px solid %s;border-bottom:0">'
        '<span style="font-family:%s;font-size:10px;color:%s">%s</span>%s</td></tr>'
        '<tr><td bgcolor="%s" style="padding:7px 9px;border:1px solid %s">'
        '<pre style="margin:0;white-space:pre-wrap;font-family:%s;font-size:12px;'
        'line-height:1.55">%s</pre></td></tr></table>'
        % (CODE_BG, CODE_BORDER, CODE_FONT, MUTED, _esc(lang), copy_link,
           CODE_BG, CODE_BORDER, CODE_FONT, colored))


def _table_block(header, rows):
    def cell(value, head=False):
        return ('<td style="padding:5px 8px;border:1px solid %s">'
                '<span style="font-size:12px%s">%s</span></td>'
                % (CODE_BORDER, ';font-weight:600' if head else '', _inline(value)))
    out = ['<table width="100%%" cellspacing="0" cellpadding="0" '
           'style="margin:8px 0;border-collapse:collapse">',
           '<tr bgcolor="%s">%s</tr>' % (TABLE_HEAD_BG, ''.join(cell(c, True) for c in header))]
    for row in rows:
        out.append('<tr>%s</tr>' % ''.join(cell(c) for c in row))
    out.append('</table>')
    return ''.join(out)


def _list_block(lines, i, n, indent):
    """递归列表：支持有序/无序、按缩进嵌套、行内格式。"""
    ordered = bool(re.match(r'^\d+[.)]\s', lines[i].strip()))
    items = []
    while i < n:
        raw = lines[i]
        match = re.match(r'^(\s*)(?:(\d+)[.)]|[-*+])\s+(.*)$', raw)
        if not match or len(match.group(1)) != indent:
            break
        content = match.group(3)
        i += 1
        sub = []
        while i < n:
            nxt = lines[i]
            if not nxt.strip():
                break
            nindent = len(nxt) - len(nxt.lstrip())
            if nindent <= indent:
                break
            if re.match(r'^(\s*)(?:(\d+)[.)]|[-*+])\s+', nxt):
                nested, i = _list_block(lines, i, n, nindent)
                sub.append(nested)
            else:
                content += '<br>' + _inline(nxt.strip())
                i += 1
        items.append('<li style="margin:2px 0">%s%s</li>' % (_inline(content), ''.join(sub)))
    tag = 'ol' if ordered else 'ul'
    return '<%s style="margin:4px 0 4px 16px;line-height:1.7">%s</%s>' % (
        tag, ''.join(items), tag), i


def _is_table_separator(line):
    return bool(re.match(r'^\|?[\s:|-]+\|?$', line.strip())) and '-' in line


def render_markdown(text, copy_token=None):
    """把一条回答的 Markdown 渲染成 QTextBrowser 可用的 HTML。

    ``copy_token(source) -> str`` 由调用方提供，返回可放进 href 的复制凭据；
    不提供时不显示复制按钮（测试/导出场景）。
    """
    text = str(text or '')
    if _MD is None:
        return _esc(text).replace('\n', '<br>')

    lines = text.split('\n')
    out = []
    para = []
    i, n = 0, len(lines)

    def flush_para():
        if not para:
            return
        body = ' '.join(x.strip() for x in para)
        out.append('<p style="margin:3px 0;line-height:1.75">%s</p>' % _inline(body))
        para.clear()

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith('```'):
            flush_para()
            lang = stripped[3:].strip()
            i += 1
            body = []
            while i < n and not lines[i].strip().startswith('```'):
                body.append(lines[i])
                i += 1
            i += 1
            out.append(_code_card(lang, '\n'.join(body), copy_token))
            continue

        if stripped.startswith('#'):
            flush_para()
            level = min(6, len(stripped) - len(stripped.lstrip('#')))
            size = {1: 18, 2: 16, 3: 14.5}.get(level, 13.5)
            out.append('<p style="margin:12px 0 4px;font-size:%spx;font-weight:600">%s</p>'
                       % (size, _inline(stripped[level:].strip())))
            i += 1
            continue

        if stripped.startswith('>'):
            flush_para()
            quote = []
            while i < n and lines[i].strip().startswith('>'):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            out.append('<table width="100%%" cellspacing="0" cellpadding="0" style="margin:7px 0">'
                       '<tr><td bgcolor="%s" style="padding:6px 10px;border-left:3px solid %s">'
                       '<span style="color:#4a5270;font-size:12.5px">%s</span></td></tr></table>'
                       % (QUOTE_BG, ACCENT, '<br>'.join(_inline(q) for q in quote)))
            continue

        if (stripped.startswith('|') and i + 1 < n
                and _is_table_separator(lines[i + 1])):
            flush_para()
            header = [c.strip() for c in stripped.strip('|').split('|')]
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith('|'):
                rows.append([c.strip() for c in lines[i].strip().strip('|').split('|')])
                i += 1
            out.append(_table_block(header, rows))
            continue

        if re.match(r'^(\s*)(?:(\d+)[.)]|[-*+])\s+', line):
            indent = len(line) - len(line.lstrip())
            flush_para()
            block, i = _list_block(lines, i, n, indent)
            out.append(block)
            continue

        if stripped in ('---', '***', '___'):
            flush_para()
            out.append('<hr>')
            i += 1
            continue

        if not stripped:
            flush_para()
            i += 1
            continue

        para.append(line)
        i += 1

    flush_para()
    return ''.join(out)
