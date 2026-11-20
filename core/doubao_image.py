#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""豆包网页生图客户端（super-guga 表情包工坊可选增强）。

定位
----
本地运动补偿补帧已经能解决大部分过渡；只有"两帧之间姿态跨度太大、形变会糊"
的段落，才值得让豆包按参考图重绘一张中间姿态。

所以这里的定位是**可选增强**：没登录、超时、限流都不应该影响主流程，
调用方拿到 None 就退回本地补帧。

用法
----
    from core import doubao_image as db
    db.available()                      # 有没有可用登录态
    db.login(log=print)                 # 弹窗扫码（首次）
    img = db.ask('生成...', refs=[frame_png])

一次性脚本可以直接 `python -m core.doubao_image login / probe`。
"""
from __future__ import annotations

import io
import json
import os
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / 'data' / 'doubao_profile'
CACHE_DIR = ROOT / 'data' / 'doubao_cache'
LOG_PATH = ROOT / 'data' / 'doubao_session.json'
CHAT_URL = 'https://www.doubao.com/chat/'
DEFAULT_TIMEOUT = 150

PROMPT_RULE = (
    '严格以参考图为唯一风格来源：保持角色外观、发型、配色、线条粗细、五官比例完全一致，'
    '只把姿态改为「%s」。输出单个角色、纯白背景、无文字、无水印、无边框、无多余元素。'
)
PAIR_RULE = (
    '参考图有两张：第一张是这一小段的起始姿态，第二张是结束姿态，'
    '请画出两者之间最自然的过渡姿态，只要一张图。'
)


def _playwright():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError('缺少 playwright，请执行: pip install playwright') from exc


# ─────────────────────────────────────────────────────────
# 登录态
# ─────────────────────────────────────────────────────────
# 字节系真正的登录凭证：设备标识（ttwid/passport_csrf_token）在没登录时也会写，
# 不能用"有没有 cookie 文件"来判断。
LOGIN_COOKIES = {'sessionid', 'sessionid_ss', 'sid_tt', 'uid_tt', 'sid_guard'}


def _cookie_names():
    """从 profile 的 cookie 库里取 doubao 域下的 cookie 名（拷贝一份再读，避免被锁）。"""
    import sqlite3
    import shutil
    import tempfile
    db = PROFILE / 'Default' / 'Network' / 'Cookies'
    if not db.exists():
        db = PROFILE / 'Default' / 'Cookies'
    if not db.exists():
        return set()
    tmp = None
    try:
        tmp = tempfile.mktemp(suffix='.db')
        shutil.copy2(db, tmp)
        conn = sqlite3.connect(tmp)
        try:
            rows = conn.execute(
                "select name from cookies where host_key like '%doubao%'").fetchall()
        finally:
            conn.close()
        return {r[0] for r in rows}
    except Exception:  # noqa: BLE001 - 库被占用/格式变化都当作"读不到"
        return set()
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def profile_ready():
    """是不是真的登录了（有 sessionid 一类凭证），而不是仅仅打开过页面。"""
    return bool(_cookie_names() & LOGIN_COOKIES)


def available():
    """能不能直接调生图：profile 有登录态 + playwright 可用。"""
    if not profile_ready():
        return False
    try:
        _playwright()
    except RuntimeError:
        return False
    return True


def status():
    if not PROFILE.exists():
        return {'ready': False, 'reason': '还没登录过豆包'}
    names = _cookie_names()
    if not names:
        return {'ready': False, 'reason': 'profile 里读不到 cookie，需要扫码'}
    if not (names & LOGIN_COOKIES):
        return {'ready': False,
                'reason': '只有设备标识没有登录凭证（sessionid 缺失），需要扫码'}
    try:
        _playwright()
    except RuntimeError:
        return {'ready': False, 'reason': '缺少 playwright（pip install playwright）'}
    return {'ready': True, 'reason': '已登录', 'profile': str(PROFILE)}


def _dismiss(page):
    for sel in ('button[aria-label*="关闭"]', '[class*=close-btn]', 'svg[class*=close]'):
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 3)):
                try:
                    loc.nth(i).click(timeout=600)
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            continue
    try:
        page.keyboard.press('Escape')
    except Exception:  # noqa: BLE001
        pass


def login(timeout=300, log=None):
    """弹出浏览器等扫码，成功返回 True。已登录则直接返回。"""
    log = log or (lambda _m: None)
    if profile_ready():
        log('豆包已登录，无需重复扫码')
        return True
    sync_playwright = _playwright()
    PROFILE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE), channel='msedge', headless=False,
            viewport={'width': 1440, 'height': 950},
            args=['--disable-blink-features=AutomationControlled'])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(CHAT_URL, wait_until='domcontentloaded', timeout=45000)
            page.wait_for_timeout(2500)
            _dismiss(page)
            log('已打开豆包，请在窗口里扫码登录（最多 %d 秒）' % timeout)
            deadline = time.time() + timeout
            ok = False
            while time.time() < deadline:
                page.wait_for_timeout(2500)
                try:
                    if page.locator('[class*=avatar], img[src*=avatar]').count() > 0:
                        ok = True
                        break
                    if (page.locator('input[type=file]').count() > 0
                            and page.locator('button:has-text("登录")').count() == 0):
                        ok = True
                        break
                except Exception:  # noqa: BLE001
                    break
            log('登录%s' % ('成功' if ok else '未完成'))
            return ok
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass


# ─────────────────────────────────────────────────────────
# 生成
# ─────────────────────────────────────────────────────────
class DoubaoError(RuntimeError):
    """生图链路失败（登录、超时、DOM 变化）。"""


def _visible_editor(page):
    """找到可见的输入框（contenteditable 或 textarea）。"""
    for sel in ('textarea[placeholder]', '[contenteditable="true"]', 'textarea'):
        try:
            loc = page.locator(sel)
            for i in range(loc.count()):
                el = loc.nth(i)
                if el.is_visible():
                    box = el.bounding_box()
                    if box and box.get('width', 0) > 120:
                        return el
        except Exception:  # noqa: BLE001
            continue
    return None


# ─────────────────────────────────────────────────────────
# 参考图准备
# ─────────────────────────────────────────────────────────
# 豆包对上传过来的图有隐性偏好：透明底会被当成黑底、太大的图会卡在上传、
# 太小的图细节不够导致"风格锁不住"。所以进上传口之前先统一口径。
PREP_MAX_EDGE = 1024
PREP_MIN_EDGE = 192
PREP_MAX_BYTES = 3 * 1024 * 1024


def prepare_ref(src, dest_dir=None, name=None):
    """把任意帧/文件整理成豆包能稳定接收的参考图：白底、边长受控的 PNG。

    返回保存后的 Path。表情包的透明底必须铺白：直接传透明 PNG，豆包会按黑底
    理解，生成图背景发黑，后面抠底就会把主体一起抠掉。
    """
    from PIL import Image
    if isinstance(src, (str, Path)):
        img = Image.open(str(src))
    elif isinstance(src, (bytes, bytearray)):
        img = Image.open(io.BytesIO(bytes(src)))
    else:
        img = src

    img = img.convert('RGBA')
    alpha_min = img.getchannel('A').getextrema()[0]
    if alpha_min < 250:
        bg = Image.new('RGBA', img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    img = img.convert('RGB')

    w, h = img.size
    long_edge, short_edge = max(w, h), min(w, h)
    scale = 1.0
    if long_edge > PREP_MAX_EDGE:
        scale = PREP_MAX_EDGE / float(long_edge)
    elif short_edge < PREP_MIN_EDGE:
        # 小表情放大一点：细节太少的图豆包容易"自由发挥"，风格就跑偏了
        scale = min(PREP_MIN_EDGE / float(max(short_edge, 1)), 4.0)
    if abs(scale - 1.0) > 0.01:
        # 像素/低色数的表情用最近邻，插值会把硬边糊成一圈毛
        colors = img.getcolors(maxcolors=512)
        pixel_art = colors is not None and len(colors) <= 96
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                         Image.NEAREST if pixel_art else Image.LANCZOS)

    dest_dir = Path(dest_dir) if dest_dir else (CACHE_DIR / 'refs')
    dest_dir.mkdir(parents=True, exist_ok=True)
    # 注意别用 time.strftime('...%f')：%f 是 datetime 的格式符，strftime 会直接报错
    base = name or ('ref-%s-%03d' % (time.strftime('%Y%m%d-%H%M%S'),
                                     int(time.time() * 1000) % 1000))
    out = dest_dir / (base + '.png')
    img.save(out, 'PNG', optimize=True)
    if out.stat().st_size > PREP_MAX_BYTES:
        out.unlink(missing_ok=True)
        out = out.with_suffix('.jpg')
        img.save(out, 'JPEG', quality=92, optimize=True)
        if out.stat().st_size > PREP_MAX_BYTES:
            img.save(out, 'JPEG', quality=80, optimize=True)
    return out


# ─────────────────────────────────────────────────────────
# 上传参考图（多策略 + 校验）
# ─────────────────────────────────────────────────────────
IMAGE_MAGIC = (b'\x89PNG', b'\xff\xd8\xff', b'GIF8', b'RIFF')
# 豆包生成/托管图片的 CDN；用来把"生成结果"和本地预览、页面图标区分开
CDN_HOSTS = ('byteimg', 'bytedns', 'tos-cn', 'doubao.com')


def _looks_image(data):
    return bool(data) and len(data) > 2000 and any(data.startswith(m) for m in IMAGE_MAGIC)


def _snapshot(page):
    """页面图片快照：现有 src 集合 + 大图数量。用来定"哪些是上传的、哪些是新生成的"。"""
    srcs, big = set(), 0
    try:
        handles = page.locator('img').all()
    except Exception:  # noqa: BLE001
        return {'srcs': srcs, 'big': big}
    for h in handles:
        try:
            src = h.get_attribute('src') or ''
            if src:
                srcs.add(src)
            box = h.bounding_box() or {}
            if (box.get('width') or 0) >= 150 and (box.get('height') or 0) >= 150:
                big += 1
        except Exception:  # noqa: BLE001
            continue
    return {'srcs': srcs, 'big': big}


def _new_images(page, before, min_edge=32, near=None, kind='thumb'):
    """相对快照新增的图片。

    kind='thumb'：刚上传的缩略图。它们一定是本地预览（`blob:` / `data:`）、
                 尺寸不大、并且出现在输入框附近——只凭"新增了 img"会被页面
                 自己加载的推荐图和图标骗过去，直接把没传上去的图当成传好了。
    kind='gen'：  生成结果。走 CDN 的 https 大图，本地预览一律不算。
    """
    out = []
    try:
        handles = page.locator('img').all()
    except Exception:  # noqa: BLE001
        return out
    for h in handles:
        try:
            src = h.get_attribute('src') or ''
            if not src or src in before:
                continue
            head = src[:40]
            if head.startswith('data:image/svg'):      # 图标占位
                continue
            box = h.bounding_box() or {}
            w = box.get('width') or 0
            hh = box.get('height') or 0
            if w < min_edge or hh < min_edge:
                continue
            if any(t in src for t in ('avatar', 'icon', 'logo')):
                continue
            local = src.startswith('blob:') or src.startswith('data:')
            if kind == 'thumb':
                if not local:
                    continue
                if max(w, hh) > 260:                   # 缩略图不会是大图
                    continue
            else:
                if local:
                    continue
                if not any(t in src for t in CDN_HOSTS):
                    continue
                if w < 150 or hh < 150:
                    continue
            if near:
                cx = box['x'] + w / 2.0
                cy = box['y'] + hh / 2.0
                if not (near['x'] - 100 <= cx <= near['x'] + near['width'] + 100):
                    continue
                if not (near['y'] - 420 <= cy <= near['y'] + near['height'] + 100):
                    continue
            out.append(src)
        except Exception:  # noqa: BLE001
            continue
    return out


def _wait_upload(page, before, want, timeout=14, near=None):
    """等到输入框附近出现 want 个新的上传缩略图；返回实际数量。"""
    deadline = time.time() + timeout
    got = 0
    while time.time() < deadline:
        try:
            got = len(_new_images(page, before, 32, near=near, kind='thumb'))
        except Exception:  # noqa: BLE001
            got = 0
        if got >= want:
            return got
        page.wait_for_timeout(600)
    return got


def _upload_via_input(page, paths):
    """策略一：直接给页面里的 file input 塞文件（最干净，不依赖按钮长得什么样）。"""
    try:
        loc = page.locator('input[type=file]')
        n = loc.count()
    except Exception:  # noqa: BLE001
        return False
    for i in range(n):
        try:
            loc.nth(i).set_input_files([str(p) for p in paths], timeout=8000)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _near_editor_buttons(page, editor):
    """输入框工具条上的按钮（上传入口就在这里，但通常只有图标没有文字）。

    豆包改版后 class 全是 Tailwind 的原子类，靠 `[class*=upload]` 认不出来，
    所以按**空间位置**筛：在输入框同宽范围内、垂直距离不远的小按钮。
    """
    out = []
    try:
        ebox = editor.bounding_box() or {}
    except Exception:  # noqa: BLE001
        ebox = {}
    try:
        buttons = page.locator('button, [role=button]').all()[:80]
    except Exception:  # noqa: BLE001
        return out
    for btn in buttons:
        try:
            if not btn.is_visible():
                continue
            box = btn.bounding_box() or {}
            if not box or box.get('width', 0) > 72 or box.get('height', 0) > 72:
                continue
            if ebox:
                dy = abs((box['y'] + box['height'] / 2) - (ebox['y'] + ebox['height'] / 2))
                if dy > ebox['height'] + 220:
                    continue
                if box['x'] > ebox['x'] + ebox['width'] + 60:
                    continue
            label = ' '.join(filter(None, [
                btn.get_attribute('aria-label') or '',
                btn.get_attribute('title') or '',
                (btn.inner_text(timeout=300) or '')[:12],
            ])).lower()
            if any(bad in label for bad in ('发送', 'send', '停止', 'stop', '语音', '麦克风')):
                continue
            out.append(btn)
        except Exception:  # noqa: BLE001
            continue
    return out


def _upload_via_chooser(page, paths):
    """策略二：点输入框附近的工具按钮，接住系统文件选择框。"""
    hints = ('上传', '图片', '附件', '添加', '文件', 'upload', 'image', 'attach', 'plus', 'file')

    def label_of(btn):
        try:
            return ' '.join(filter(None, [btn.get_attribute('aria-label') or '',
                                          btn.get_attribute('title') or '',
                                          (btn.inner_text(timeout=300) or '')[:12]])).lower()
        except Exception:  # noqa: BLE001
            return ''

    editor = _visible_editor(page)
    if editor is not None:
        cands = sorted(_near_editor_buttons(page, editor),
                       key=lambda b: 0 if any(h in label_of(b) for h in hints) else 1)
        cands = cands[:8]
    else:
        # 连输入框都定位不到时，只敢点名字明确写着"上传/图片"的按钮
        cands = []
        try:
            for btn in page.locator('button, [role=button]').all()[:60]:
                if btn.is_visible() and any(h in label_of(btn) for h in hints):
                    cands.append(btn)
        except Exception:  # noqa: BLE001
            pass
        cands = cands[:4]

    for btn in cands:
        try:
            with page.expect_file_chooser(timeout=2500) as info:
                btn.click(timeout=2000)
            info.value.set_files([str(p) for p in paths])
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _inject_files_js(events):
    """构造 DataTransfer 并派发事件（粘贴/拖放共用）。"""
    return """async (payload) => {
        const dt = new DataTransfer();
        for (const f of payload.files) {
            const bin = atob(f.b64);
            const arr = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
            dt.items.add(new File([arr], f.name, {type: 'image/png'}));
        }
        const ed = document.querySelector('[contenteditable="true"]')
                || document.querySelector('textarea') || document.body;
        const targets = [ed, ed.closest('form'), ed.parentElement].filter(Boolean);
        if (ed.focus) ed.focus();
        for (const type of payload.events) {
            if (type === 'paste') {
                ed.dispatchEvent(new ClipboardEvent('paste',
                    {bubbles: true, cancelable: true, clipboardData: dt}));
                continue;
            }
            for (const t of targets) {
                t.dispatchEvent(new DragEvent(type,
                    {bubbles: true, cancelable: true, dataTransfer: dt}));
            }
        }
        return dt.items.length;
    }"""


def _b64_files(paths):
    import base64
    out = []
    for p in paths:
        try:
            out.append({'name': Path(p).name,
                        'b64': base64.b64encode(Path(p).read_bytes()).decode()})
        except OSError:
            return []
    return out


def _upload_via_paste(page, paths):
    """策略三：把图片当剪贴板内容粘进输入框。"""
    files = _b64_files(paths)
    if not files:
        return 0
    try:
        return int(page.evaluate(_inject_files_js(['paste']),
                                 {'files': files, 'events': ['paste']}) or 0)
    except Exception:  # noqa: BLE001
        return 0


def _upload_via_drop(page, paths):
    """策略四：拖放。聊天类产品对拖入图片的支持比粘贴更常见。"""
    files = _b64_files(paths)
    if not files:
        return 0
    try:
        return int(page.evaluate(_inject_files_js(['dragenter', 'dragover', 'drop']),
                                 {'files': files,
                                  'events': ['dragenter', 'dragover', 'drop']}) or 0)
    except Exception:  # noqa: BLE001
        return 0


def _new_chat(page):
    """开一段干净的对话，避免上一轮没发出去的参考图缩略图残留在输入框里。"""
    try:
        page.keyboard.press('Control+Shift+K')      # 豆包自己的"新对话"快捷键
        page.wait_for_timeout(1500)
        return True
    except Exception:  # noqa: BLE001
        pass
    try:
        btn = page.locator('button:has-text("新对话"), [role=button]:has-text("新对话")').first
        btn.click(timeout=1500)
        page.wait_for_timeout(1500)
        return True
    except Exception:  # noqa: BLE001
        return False


def _upload(page, paths, log=None, wait=14):
    """把参考图送进豆包，返回**确认出现在页面上**的张数。

    只有缩略图出现了才算成功：set_input_files 不报错并不代表豆包接住了
    （元素被隐藏、accept 不匹配、单图 input 被后一张顶替，都是静默失败）。
    """
    write = log or (lambda _m: None)
    paths = [Path(p) for p in paths]
    want = len(paths)
    before = _snapshot(page)['srcs']
    editor = _visible_editor(page)
    near = None
    try:
        near = editor.bounding_box() if editor is not None else None
    except Exception:  # noqa: BLE001
        near = None

    # 输入框里还挂着上一轮的附件：不清理的话，新参考图会和旧的混在一起发出去，
    # 豆包会拿错的风格图当依据。
    stale = _new_images(page, set(), 32, near=near, kind='thumb')
    if stale:
        write('输入框里还有 %d 张上次的图，先开新对话' % len(stale))
        _new_chat(page)
        editor = _visible_editor(page)
        try:
            near = editor.bounding_box() if editor is not None else near
        except Exception:  # noqa: BLE001
            pass
        before = _snapshot(page)['srcs']

    strategies = (('file input', _upload_via_input),
                  ('上传按钮', _upload_via_chooser),
                  ('粘贴', _upload_via_paste),
                  ('拖放', _upload_via_drop))
    for label, fn in strategies:
        try:
            fired = fn(page, paths)
        except Exception as exc:  # noqa: BLE001
            write('上传策略「%s」异常：%s' % (label, str(exc)[:60]))
            fired = False
        if not fired and label in ('file input', '上传按钮'):
            continue
        # 走文件选择框的要等上传完（大图慢）；粘贴/拖放若生效是立刻出缩略图的
        budget = wait if label in ('file input', '上传按钮') else min(wait, 5)
        got = _wait_upload(page, before, want, timeout=budget, near=near)
        if got >= want:
            write('参考图已上传 %d 张（%s）' % (got, label))
            return got
        if got:
            write('参考图只传上 %d/%d 张（%s），继续尝试补传' % (got, want, label))
            # 缺的那几张换下一套策略再试一次
            for extra in paths:
                _upload_via_paste(page, [extra])
            got = _wait_upload(page, before, want, timeout=max(3, budget // 2), near=near)
            if got:
                return got
    got = len(_new_images(page, before, 32, near=near, kind='thumb'))
    if got:
        write('参考图已上传 %d/%d 张' % (got, want))
    else:
        write('参考图没传进去：页面里没出现缩略图')
    return got


def _collect_images(page, before):
    """页面上新出现的生成结果（CDN 大图，排除头像/图标/上传预览）。"""
    return _new_images(page, before, 150, kind='gen')


def _download(page, url):
    """把生成结果下载成 bytes（http 用 page.request，blob/data 走 canvas 导出）。"""
    try:
        if url.startswith('data:'):
            head, _, payload = url.partition(',')
            import base64
            return base64.b64decode(payload)
        if url.startswith('blob:'):
            b64 = page.evaluate(
                """async (u) => {
                    const r = await fetch(u); const b = await r.blob();
                    const buf = await b.arrayBuffer();
                    let s = ''; const v = new Uint8Array(buf);
                    for (let i = 0; i < v.length; i++) s += String.fromCharCode(v[i]);
                    return btoa(s);
                }""", url)
            import base64
            return base64.b64decode(b64)
        resp = page.request.get(url, timeout=30000)
        if resp.ok:
            return resp.body()
    except Exception:  # noqa: BLE001
        return None
    return None


def ask(prompt, refs=None, timeout=DEFAULT_TIMEOUT, log=None, headless=True):
    """向豆包要一张图，返回 PNG/JPEG 的 bytes；失败返回 None（调用方自行降级）。"""
    log = log or (lambda _m: None)
    if not available():
        log('豆包未登录，跳过生图')
        return None
    refs = [Path(r) for r in (refs or []) if r]
    for r in refs:
        if not r.exists():
            raise DoubaoError('参考图不存在: %s' % r)
    if refs:
        refs = [prepare_ref(r) for r in refs]     # 统一成白底、边长受控的 PNG

    sync_playwright = _playwright()
    PROFILE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE), channel='msedge', headless=headless,
            viewport={'width': 1440, 'height': 950},
            args=['--disable-blink-features=AutomationControlled'])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(CHAT_URL, wait_until='domcontentloaded', timeout=45000)
            page.wait_for_timeout(2500)
            _dismiss(page)

            # 顺序很关键：先传图，再把"传完之后"的页面当基线。
            # 反过来会把刚上传的参考图缩略图当成"新生成的图"直接返回。
            if refs:
                got = _upload(page, refs, log=log)
                if got <= 0:
                    raise DoubaoError('参考图没能传进豆包（页面上没出现缩略图）')
                page.wait_for_timeout(2500)     # 等附件真正上传完，否则发送时会被丢掉

            base = _snapshot(page)
            editor = _visible_editor(page)
            if editor is None:
                raise DoubaoError('找不到输入框，页面结构可能变了')

            # 生成图有时只在网络响应里出得来（DOM 是 canvas 或懒加载），两边都收
            grabbed = []

            def _on_response(resp):
                try:
                    ctype = (resp.headers or {}).get('content-type', '')
                    if 'image/' in ctype:
                        grabbed.append(resp)
                except Exception:  # noqa: BLE001 - 回调里什么都不能抛
                    pass

            page.on('response', _on_response)
            editor.click()
            page.keyboard.type(prompt, delay=12)
            page.wait_for_timeout(400)
            page.keyboard.press('Enter')

            log('已发送，等待生成...')
            deadline = time.time() + timeout
            urls = []
            while time.time() < deadline:
                page.wait_for_timeout(2500)
                urls = _collect_images(page, base['srcs'])
                if urls:
                    page.wait_for_timeout(2500)   # 等大图加载完
                    urls = _collect_images(page, base['srcs']) or urls
                    break

            data = None
            for url in reversed(urls):
                data = _download(page, url)
                if _looks_image(data):
                    break
                data = None
            if data is None and grabbed:
                for resp in reversed(grabbed[-6:]):
                    try:
                        body = resp.body()
                    except Exception:  # noqa: BLE001
                        continue
                    if _looks_image(body):
                        data = body
                        break
            if data is None:
                log('等待超时，没拿到图片')
                return None

            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime('%Y%m%d-%H%M%S')
            out = CACHE_DIR / ('gen-%s.%s' % (stamp, 'png' if data[:4] == b'\x89PNG' else 'jpg'))
            out.write_bytes(data)
            log('已拿到生成图: %s' % out.name)
            return data
        except DoubaoError:
            raise
        except Exception as exc:  # noqa: BLE001 - 网页自动化什么都能抛
            log('豆包调用异常: %s' % str(exc)[:80])
            return None
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass


def check_upload(ref, timeout=60, log=None):
    """只验证"这张表情能不能传进豆包"，不生图。给操作台的「测试传图」用。

    上传本身不需要登录（豆包允许先传图再要求登录），所以这里不卡登录态，
    只把登录情况一起报回去，免得"图能传但生成不了"被误当成素材有问题。

    返回 {'ok', 'uploaded', 'prepared', 'ready', 'reason', 'shot'}；不抛异常。
    """
    write = log or (lambda _m: None)
    out = {'ok': False, 'uploaded': 0, 'prepared': '', 'ready': profile_ready(),
           'reason': '', 'shot': ''}
    try:
        _playwright()
    except RuntimeError as exc:
        out['reason'] = str(exc)
        return out
    try:
        prepared = prepare_ref(ref)
    except Exception as exc:  # noqa: BLE001
        out['reason'] = '参考图准备失败：%s' % str(exc)[:80]
        return out
    out['prepared'] = str(prepared)

    sync_playwright = _playwright()
    PROFILE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE), channel='msedge', headless=False,
            viewport={'width': 1440, 'height': 950},
            args=['--disable-blink-features=AutomationControlled'])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(CHAT_URL, wait_until='domcontentloaded', timeout=45000)
            page.wait_for_timeout(2500)
            _dismiss(page)
            got = _upload(page, [prepared], log=write)
            page.wait_for_timeout(1500)
            shot = CACHE_DIR / ('upload-check-%s.png' % time.strftime('%Y%m%d-%H%M%S'))
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            try:
                page.screenshot(path=str(shot))
                out['shot'] = str(shot)
            except Exception:  # noqa: BLE001
                pass
            out['uploaded'] = int(got)
            out['ok'] = got > 0
            if got:
                out['reason'] = '上传成功，缩略图已出现在输入框'
                if not out['ready']:
                    out['reason'] += '（还没登录：素材能进去，生图前要先扫码）'
            else:
                out['reason'] = '上传失败：页面里没出现缩略图，见截图确认页面结构'
            return out
        except Exception as exc:  # noqa: BLE001
            out['reason'] = '打开豆包失败：%s' % str(exc)[:80]
            return out
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass


def render_motion_prompt(motion, extra='', pair=False):
    text = PROMPT_RULE % motion
    if pair:
        text = PAIR_RULE + text
    if extra:
        text += ' ' + extra.strip()
    return text


def diagnose(log=None):
    """看真实页面到底长什么样：有没有 file input、输入框、附近有哪些按钮。

    上传失败时先跑这个，比猜选择器快。
    """
    write = log or (lambda _m: None)
    report = {'ready': profile_ready(), 'inputs': [], 'editable': 0,
              'near_buttons': [], 'title': '', 'reason': ''}
    sync_playwright = _playwright()
    PROFILE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE), channel='msedge', headless=True,
            viewport={'width': 1440, 'height': 950},
            args=['--disable-blink-features=AutomationControlled'])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(CHAT_URL, wait_until='domcontentloaded', timeout=45000)
            page.wait_for_timeout(4000)
            _dismiss(page)
            report['title'] = page.title()
            report['inputs'] = page.evaluate(
                """() => Array.from(document.querySelectorAll('input[type=file]'))
                    .map(i => ({accept: i.accept, multiple: i.multiple,
                                hidden: i.offsetParent === null}))""")
            editor = _visible_editor(page)
            report['editable'] = page.evaluate(
                """() => document.querySelectorAll(
                    '[contenteditable="true"], textarea').length""")
            if editor is None:
                report['reason'] = '找不到输入框（多半是未登录或页面结构变了）'
            else:
                for btn in _near_editor_buttons(page, editor)[:12]:
                    try:
                        report['near_buttons'].append({
                            'label': (btn.get_attribute('aria-label') or '')
                                     + '|' + (btn.get_attribute('title') or '')
                                     + '|' + (btn.inner_text(timeout=300) or '')[:8],
                        })
                    except Exception:  # noqa: BLE001
                        continue
        except Exception as exc:  # noqa: BLE001
            report['reason'] = str(exc)[:120]
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
    write(json.dumps(report, ensure_ascii=False))
    return report


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description='豆包网页生图客户端')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('status', help='看登录态')
    p_login = sub.add_parser('login', help='扫码登录')
    p_login.add_argument('--timeout', type=int, default=300)
    p_ask = sub.add_parser('probe', help='试生成一张')
    p_ask.add_argument('--prompt', default='画一只企鹅，纯白背景')
    p_ask.add_argument('--ref', action='append', default=None)
    p_up = sub.add_parser('upload', help='只测「参考图能不能传进去」，不生图')
    p_up.add_argument('--ref', required=True, help='要传的图（表情帧/任意图片）')
    sub.add_parser('diagnose', help='打印页面结构：有没有上传入口、输入框、工具条按钮')
    args = ap.parse_args(argv)

    if args.cmd == 'status':
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == 'login':
        return 0 if login(args.timeout, log=print) else 1
    if args.cmd == 'diagnose':
        diagnose(log=print)
        return 0
    if args.cmd == 'upload':
        res = check_upload(args.ref, log=print)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res['ok'] else 1
    data = ask(args.prompt, args.ref, log=print)
    print('结果: %s' % ('%d 字节' % len(data) if data else '没有拿到图片'))
    return 0 if data else 1


if __name__ == '__main__':
    raise SystemExit(main())
