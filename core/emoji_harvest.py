#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""表情收件箱：把散落在桌面/下载/微信QQ目录里的表情包捞出来，去重归集，供工坊一键导入。

为什么需要它：
    微信 4.x 与 QQ NT 把收藏表情加密存在本地（实测 xwechat_files 下 6500+ 文件无一明文图），
    官方也没有"批量导出收藏表情"的接口。唯一可靠的办法是在客户端里逐张「另存为」，
    于是表情会散落在桌面、下载、图片等各处且大量重名。本模块负责把这些文件
    按内容去重、按特征筛出真表情（排除截图/照片/整屏长图），统一收进一个收件箱。

命令行：
    python -m core.emoji_harvest                  # 扫桌面/下载/图片
    python -m core.emoji_harvest --chat            # 额外扫微信 QQ 数据目录（基本扫不到，用于自检）
    python -m core.emoji_harvest -r D:/表情 -r D:/临时
    python -m core.emoji_harvest --list            # 只看收件箱现状
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INBOX = ROOT / 'data' / 'emoji_inbox'

HOME = Path(os.path.expanduser('~'))
DESKTOP = HOME / 'Desktop'
DOWNLOADS = HOME / 'Downloads'
PICTURES = HOME / 'Pictures'

# 微信 / QQ 的数据目录。实测结论（2026-11，微信 4.x / QQ NT）：
#   QQ NT  —— 聊天里收到、收藏的自定义表情以明文存于
#             nt_data\Emoji\emoji-related\emoji\<hash>\<md5>.gif|png|jpg，可以直接拷走；
#             系统黄脸动态表情在 EmojiSystermResource（1300+ 个 APNG），量太大，按需开启。
#   微信 4.x —— Emoticon 缓存全部加密，没有任何明文图，只能靠客户端「另存为」。
#   注意：不能整个 Tencent Files 乱扫，OnlineStatus / PokeFace 里的 UI 资源会被误判成表情。
CHAT_DIRS = [
    HOME / 'Documents' / 'xwechat_files',
    HOME / 'Documents' / 'WeChat Files',
    HOME / 'Documents' / 'Tencent Files',
]

WECHAT_NOTE = ('微信 4.x 的表情缓存是加密的（实测 %s 下数千文件无一张明文图），'
               '只能先在微信里逐张「另存为」，再扫描桌面/下载目录')

# 扫聊天目录时默认跳过的子目录（QQ 系统表情资源、缩略图缓存、客户端 UI 资源）
CHAT_EXCLUDE = ('EmojiSystermResource', 'ThumbTemp', 'emoji-resource', 'marketface',
                'OnlineStatus', 'PokeFace')

DEFAULT_ROOTS = [DESKTOP, DOWNLOADS, PICTURES]

CANDIDATE_EXTS = {'.gif', '.png', '.webp', '.apng', '.bmp', '.jpg', '.jpeg'}
ANIMATED_FORMATS = {'GIF', 'PNG', 'WEBP'}

MAX_BYTES = 8 * 1024 * 1024     # 单个文件上限
MAX_FILES = 8000                # 单次扫描文件数上限，防止整盘乱扫
MIN_SIDE = 16                   # 再小就不是表情了
MAX_SIDE = 1024                 # 动图上限，再大多半是录屏/壁纸
MAX_RATIO = 3.0                 # 长截图、横幅一律排除
STATIC_MAX_SIDE = 400           # 静态表情通常很小，超过多半是截图和照片
STATIC_MAX_BYTES = 400 * 1024

SKIP_DIRS = {'node_modules', '__pycache__', '.git', '.svn', '$RECYCLE.BIN',
             'System Volume Information', 'AppData', 'Windows'}

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# 截图/照片的文件名几乎都会带这些词，静态图阶段据此剔除（动图不受影响）
_NAME_DENY = re.compile(
    r'(截图|截屏|屏幕|screenshot|screen[ _-]?shot|snipaste|截取|'
    r'微信图片|wechat[ _-]?image|qq图片|qq[ _-]?image|mmexport|wx_camera|'
    r'img[ _-]?\d{4}|dsc[ _-]?\d+|photo[ _-]?\d+|pixel[ _-]?\d+|'
    r'wallpaper|壁纸|cover|封面|banner|二维码|qrcode)', re.I)


def _pillow():
    from core import sticker_studio as ss
    if ss.Image is None:
        raise RuntimeError('缺少 Pillow 依赖（pip install pillow）')
    return ss.Image


# ─────────────────────────────────────────────────────────
# 单文件鉴定
# ─────────────────────────────────────────────────────────
def probe_image(path):
    """读一张图的元信息；打不开就返回 None（缓存残留、损坏文件都走这里）。"""
    Image = _pillow()
    try:
        with Image.open(path) as im:
            frames = int(getattr(im, 'n_frames', 1) or 1)
            fmt = (im.format or '').upper()
            w, h = im.size
            alpha = (im.mode in ('RGBA', 'LA', 'PA')
                     or 'transparency' in (im.info or {}))
    except Exception:  # noqa: BLE001 - 任何解码失败都只是"跳过这个文件"
        return None
    return {'fmt': fmt, 'frames': frames, 'size': [w, h], 'alpha': alpha}


def classify(meta, size_bytes, name=''):
    """判定是不是表情包。返回 (ok, 原因)；原因只在拒绝时有用。"""
    if not meta:
        return False, 'decode'
    w, h = meta['size']
    if min(w, h) < MIN_SIDE:
        return False, 'too_small'
    if max(w, h) > MAX_SIDE:
        return False, 'too_large'
    if w / max(h, 1) > MAX_RATIO or h / max(w, 1) > MAX_RATIO:
        return False, 'bad_ratio'
    if size_bytes > MAX_BYTES:
        return False, 'too_heavy'
    # 动图：表情包的主体，只要尺寸和比例过关就收
    if meta['frames'] > 1 and meta['fmt'] in ANIMATED_FORMATS:
        return True, 'animated'
    if _NAME_DENY.search(name or ''):
        return False, 'screenshot'
    if meta['fmt'] in ('JPEG', 'JPG', 'MPO'):
        return False, 'photo'          # 表情极少是 jpg，收了满屏都是照片
    # 静态图：必须够小，且要么带透明通道、要么文件够轻，才当表情
    if max(w, h) > STATIC_MAX_SIDE:
        return False, 'too_big_static'
    if meta['alpha']:
        return True, 'alpha-static'
    if size_bytes <= STATIC_MAX_BYTES:
        return True, 'small-static'    # 白底静态表情（无透明通道）
    return False, 'too_big_static'


def _digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _safe_stem(path, limit=30):
    stem = _BAD_CHARS.sub('_', path.stem).strip(' ._') or 'emoji'
    return friendly_name(stem)[:limit]


def friendly_name(stem):
    """QQ 的表情文件名是 md5，直接当名字没法认；换成「表情 a45379」这种。"""
    stem = str(stem or '')
    if re.fullmatch(r'[0-9a-fA-F]{12,}', stem):
        return '表情' + stem[:6]
    return stem or '表情'


def _dest_name(path, sha):
    ext = path.suffix.lower()
    if ext == '.jpeg':
        ext = '.jpg'
    return '%s_%s%s' % (sha[:8], _safe_stem(path), ext)


# ─────────────────────────────────────────────────────────
# 扫描与归集
# ─────────────────────────────────────────────────────────
def _walk(root, exclude=()):
    """产出候选文件（按扩展名过滤，跳过系统目录、隐藏目录与 exclude 命中的路径）。"""
    root = Path(root)
    if root.is_file():
        yield root
        return
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith('.')]
        if exclude and any(part in Path(dirpath).parts for part in exclude):
            continue
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() in CANDIDATE_EXTS:
                yield p


def harvest(roots=None, dest=None, max_files=MAX_FILES, exclude=()):
    """扫描 roots，把认出来的表情收进 dest（默认 data/emoji_inbox）。"""
    _pillow()
    roots = [Path(r) for r in (roots or DEFAULT_ROOTS)]
    dest = Path(dest or INBOX)
    dest.mkdir(parents=True, exist_ok=True)

    # 收件箱里已有的副本按 sha 前缀索引，重复的不再拷第二份
    known = {}
    for existing in dest.iterdir():
        if existing.is_file() and '_' in existing.name:
            known[existing.name.split('_', 1)[0]] = existing

    report = {'scanned': 0, 'added': [], 'duplicates': 0, 'rejected': {},
              'truncated': False, 'missing_roots': [], 'roots': [str(r) for r in roots],
              'dest': str(dest)}

    for root in roots:
        if not root.exists():
            report['missing_roots'].append(str(root))
    live_roots = [r for r in roots if r.exists()]

    for root in live_roots:
        if report['truncated']:
            break
        for path in _walk(root, exclude):
            if report['scanned'] >= max_files:
                report['truncated'] = True
                break
            try:
                size = path.stat().st_size
            except OSError:
                continue
            report['scanned'] += 1
            if size == 0 or size > MAX_BYTES:
                report['rejected']['too_heavy'] = report['rejected'].get('too_heavy', 0) + 1
                continue
            meta = probe_image(path)
            ok, reason = classify(meta, size, path.stem)
            if not ok:
                report['rejected'][reason] = report['rejected'].get(reason, 0) + 1
                continue
            try:
                sha = _digest(path)
            except OSError:
                report['rejected']['io'] = report['rejected'].get('io', 0) + 1
                continue
            if sha[:8] in known:
                report['duplicates'] += 1
                continue
            target = dest / _dest_name(path, sha)
            try:
                shutil.copy2(path, target)
            except OSError:
                report['rejected']['io'] = report['rejected'].get('io', 0) + 1
                continue
            known[sha[:8]] = target
            report['added'].append({
                'path': str(target), 'source': str(path),
                'name': _safe_stem(path), 'sha': sha[:16],
                'frames': meta['frames'], 'size': meta['size'],
                'kind': reason,
            })

    report['added'].sort(key=lambda it: (it['frames'] <= 1, it['name']))
    return report


def inbox_items():
    """收件箱现有内容，按动图优先排序。"""
    if not INBOX.is_dir():
        return []
    Image = _pillow()
    out = []
    for p in sorted(INBOX.iterdir()):
        if not p.is_file():
            continue
        try:
            with Image.open(p) as im:
                frames = int(getattr(im, 'n_frames', 1) or 1)
                size = list(im.size)
        except Exception:  # noqa: BLE001
            continue
        name = friendly_name(p.stem.split('_', 1)[1]) if '_' in p.stem else friendly_name(p.stem)
        out.append({'path': str(p), 'name': name, 'frames': frames,
                    'size': size, 'bytes': p.stat().st_size})
    out.sort(key=lambda it: (it['frames'] <= 1, it['name']))
    return out


def clear_inbox():
    """清空收件箱（只删自己归集进来的副本，不动原始文件）。

    注意：这里必须是整目录删除。Windows 上逐个 unlink 每个文件都会被
    杀毒软件实时扫描拦一下（实测 0.15s/个），几百个文件要卡几分钟。
    """
    if not INBOX.is_dir():
        return 0
    removed = sum(1 for _ in INBOX.iterdir())
    shutil.rmtree(INBOX, ignore_errors=True)
    INBOX.mkdir(parents=True, exist_ok=True)
    return removed


def qq_emoji_dirs():
    """QQ NT 的自定义表情目录（聊天里收到 / 收藏的表情包，明文）。"""
    base = HOME / 'Documents' / 'Tencent Files'
    if not base.is_dir():
        return []
    return [p for p in base.glob('*/nt_qq/nt_data/Emoji/emoji-related/emoji') if p.is_dir()]


def qq_face_dirs():
    """QQ 自带的动态黄脸表情（APNG 序列）所在目录。"""
    base = HOME / 'Documents' / 'Tencent Files'
    if not base.is_dir():
        return []
    return [p for p in base.glob('*/nt_qq/nt_data/Emoji/**/EmojiSystermResource') if p.is_dir()]


def harvest_chats(dest=None, with_faces=False):
    """收 QQ 客户端里的自定义表情。

    微信那边不用扫：4.x 的 Emoticon 缓存全加密，扫不出图（WECHAT_NOTE 里写了替代办法）。
    """
    roots = qq_emoji_dirs()
    if with_faces:
        roots = roots + qq_face_dirs()
    report = harvest(roots, dest)
    report['note'] = WECHAT_NOTE % (HOME / 'Documents' / 'xwechat_files')
    if not roots:
        report['note'] = '没有找到 QQ 的表情目录（没装 QQ 或还没收过表情）。' + report['note']
    return report


def summary(report):
    """把 report 变成一句人话。"""
    parts = ['扫描 %d 个文件' % report['scanned']]
    parts.append('收进 %d 个' % len(report['added']))
    if report['duplicates']:
        parts.append('去重 %d 个' % report['duplicates'])
    rej = report['rejected']
    if rej:
        label = {'decode': '打不开', 'photo': '照片', 'too_large': '尺寸过大',
                 'too_small': '尺寸过小', 'bad_ratio': '长图/横幅',
                 'too_heavy': '体积过大', 'too_big_static': '普通大图', 'io': '读写失败'}
        parts.append('跳过 ' + '、'.join('%s %d' % (label.get(k, k), v)
                                        for k, v in sorted(rej.items(), key=lambda kv: -kv[1])[:4]))
    if report['truncated']:
        parts.append('（达扫描上限，未扫完）')
    return '，'.join(parts)


def main(argv=None):
    ap = argparse.ArgumentParser(description='把桌面/下载/微信QQ目录里的表情包收进收件箱')
    ap.add_argument('-r', '--root', action='append', default=None, help='扫描目录，可重复')
    ap.add_argument('--chat', action='store_true', help='额外汇总微信/QQ 客户端目录（QQ 自定义表情可直取）')
    ap.add_argument('--qq-faces', action='store_true', help='额外收 QQ 自带的动态黄脸表情（数量很大）')
    ap.add_argument('--dest', default=None, help='收件箱目录（默认 data/emoji_inbox）')
    ap.add_argument('--list', action='store_true', help='只列收件箱，不扫描')
    ap.add_argument('--clear', action='store_true', help='清空收件箱')
    ap.add_argument('--json', action='store_true', help='输出 JSON')
    args = ap.parse_args(argv)

    if args.clear:
        n = clear_inbox()
        print('已清空收件箱：%d 个文件' % n)
        return 0
    if args.list:
        items = inbox_items()
        if args.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
            return 0
        print('收件箱 %s：%d 个表情' % (INBOX, len(items)))
        for it in items:
            print('  %-28s %s帧 %dx%d' % (it['name'], it['frames'], it['size'][0], it['size'][1]))
        return 0

    if args.chat or args.qq_faces:
        report = harvest_chats(args.dest, with_faces=args.qq_faces)
    else:
        report = harvest(args.root or list(DEFAULT_ROOTS), args.dest)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(summary(report))
        for it in report['added'][:20]:
            print('  + %-26s %s帧 %dx%d' % (it['name'], it['frames'], it['size'][0], it['size'][1]))
        if len(report['added']) > 20:
            print('  ... 另有 %d 个' % (len(report['added']) - 20))
        if report['missing_roots']:
            print('跳过不存在的目录：%s' % '、'.join(report['missing_roots']))
        if report.get('note'):
            print('提示：%s' % report['note'])
        if report['added']:
            print('收件箱：%s（让咕嘎导入时用这个目录）' % report['dest'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
