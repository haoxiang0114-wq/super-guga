"""Register the existing Windows Chinese font for Qt installations with sparse fonts."""
from pathlib import Path


def ensure_readable_fonts():
    global _loaded
    if globals().get('_loaded'):return
    from PyQt5.QtGui import QFontDatabase
    import os
    font=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts/msyh.ttc'
    if font.is_file():QFontDatabase.addApplicationFont(str(font))
    _loaded=True
