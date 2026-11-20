# -*- coding: utf-8 -*-
"""在桌面生成「语音智能体」开关快捷方式。

指向 venv 的 pythonw + tools/pet_toggle.pyw，因此双击不弹黑框：
单击启动桌宠，再单击关闭。项目换了位置就重跑一次本脚本。

用法：
    .venv\\Scripts\\python.exe tools\\make_desktop_shortcut.py
    .venv\\Scripts\\python.exe tools\\make_desktop_shortcut.py --name 咕嘎 --dir D:\\某处
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / '.venv' / 'Scripts' / 'pythonw.exe'
SCRIPT = ROOT / 'tools' / 'pet_toggle.pyw'
ICON = ROOT / 'resources' / 'guga.ico'
DESC = '语音智能体（咕嘎企鹅桌宠）：单击启动，再单击关闭'


def desktop_dir():
    try:
        import ctypes
        from ctypes import wintypes
        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        # CSIDL_DESKTOPDIRECTORY = 0x0010
        ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf)
        if buf.value:
            return Path(buf.value)
    except Exception:
        pass
    return Path(os.path.expanduser('~')) / 'Desktop'


def make(link_path, name):
    if not TARGET.exists():
        raise SystemExit('找不到 pythonw：%s' % TARGET)
    if not SCRIPT.exists():
        raise SystemExit('找不到开关脚本：%s' % SCRIPT)
    import pythoncom
    from win32com.shell import shell

    link = pythoncom.CoCreateInstance(
        shell.CLSID_ShellLink, None,
        pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
    link.SetPath(str(TARGET))
    link.SetArguments('"%s"' % SCRIPT)
    link.SetWorkingDirectory(str(ROOT))
    if ICON.exists():
        link.SetIconLocation(str(ICON), 0)
    link.SetDescription(DESC)
    link.QueryInterface(pythoncom.IID_IPersistFile).Save(str(link_path), 0)
    return link_path


def verify(link_path):
    """pywin32 的 Get* 在中文路径下会乱码，直接查 .lnk 二进制里的字符串。

    目标路径（LinkInfo）按本地代码页存，参数/工作目录/图标按 UTF-16 存，
    所以两边都要试。
    """
    raw = Path(link_path).read_bytes()
    report = {}
    for label, value in (('target', str(TARGET)), ('script', str(SCRIPT)),
                         ('workdir', str(ROOT)), ('icon', str(ICON))):
        report[label] = any(value.encode(enc, 'ignore') in raw
                            for enc in ('utf-16-le', 'mbcs'))
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description='创建语音智能体桌面快捷方式')
    ap.add_argument('--name', default='语音智能体', help='快捷方式名字（不含 .lnk）')
    ap.add_argument('--dir', default='', help='放到哪个目录，默认桌面')
    args = ap.parse_args(argv)

    out_dir = Path(args.dir) if args.dir else desktop_dir()
    any_lnk = out_dir / (args.name + '.lnk')
    make(any_lnk, args.name)
    report = verify(any_lnk)
    print('已创建：%s' % any_lnk)
    for key, ok in report.items():
        print('  %-8s %s' % (key, '✓' if ok else '✗ 未写入'))
    return 0 if all(report.values()) else 1


if __name__ == '__main__':
    sys.exit(main())
