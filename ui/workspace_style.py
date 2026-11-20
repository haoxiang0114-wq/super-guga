"""Visual tokens for Guga's Windows workspace and desktop menu."""
from pathlib import Path

_CHEVRON = (Path(__file__).resolve().parents[1] / 'resources' / 'ui' / 'chevron.png').as_posix()

MENU_STYLE = '''
QMenu { background:#f7fafc; color:#23313e; border:1px solid #bccbd5;
        padding:5px; font:13px "Microsoft YaHei UI"; }
QMenu::item { padding:7px 26px 7px 13px; border-radius:6px; }
QMenu::item:selected { background:#e0eef1; color:#143943; }
QMenu::item:disabled { color:#657583; }
QMenu::separator { height:1px; background:#dee7ed; margin:4px 8px; }
'''

WORKSPACE_STYLE = MENU_STYLE + '''
QWidget { color:#263542; font-family:"Microsoft YaHei UI"; }
QDialog#AgentWindow { background:transparent; }
QFrame#ChatShell { background:rgba(248,251,253,244); border:1px solid #b8c8d4;
                  border-radius:18px; }
QFrame#WorkspaceBody { background:rgba(255,255,255,212); border:1px solid #d5e0e7;
                      border-radius:12px; }
QFrame#Sidebar { background:qlineargradient(x1:0,y1:0,x2:0,y2:1,
                    stop:0 rgba(222,236,245,219),stop:1 rgba(235,244,248,171));
                border:1px solid #d7e1e8;
                border-radius:12px; }
QFrame#Options { background:#eef4f7; border:1px solid #dce7ec; border-radius:10px; }
QLabel#Title { font-size:14px; font-weight:600; }
QLabel#SectionTitle { color:#4e6273; font-size:11px; font-weight:600; }
QLabel#HeroTitle { color:#192c39; font-size:27px; font-weight:600; }
QLabel#Muted, QLabel#Badge, QCheckBox { color:#526879; font-size:12px; }
QPushButton, QToolButton { background:transparent; border:1px solid transparent;
                           border-radius:7px; padding:5px 8px; font-size:12px; }
QPushButton:hover, QToolButton:hover { background:#e9f1f5; }
QPushButton:focus, QToolButton:focus { border-color:#6c9fab; }
QPushButton:checked, QToolButton:checked { background:#dfeef1; color:#1a5461; }
QPushButton:disabled, QToolButton:disabled { color:#818f9a; }
QPushButton#NewChat, QPushButton#SideLink { text-align:left; padding:7px 9px; }
QPushButton#NewChat { background:#d5e9ed; color:#174b59; font-weight:600; }
QPushButton#NewChat:hover { background:#c3dfe5; }
QPushButton#Primary { background:#215b68; color:white; border-radius:9px; font-weight:600; }
QPushButton#Primary:hover { background:#174958; }
QPushButton#Primary:disabled { background:#81949e; }
QPushButton#Close:hover { background:#fae7e7; color:#a22f3d; }
QPushButton#More::menu-indicator { image:none; width:0; }
QPushButton#SettingsEntry { background:#e9f1f5; color:#254a58; padding:5px 11px; }
QPushButton#SettingsEntry:hover { background:#d8e9ee; }
QToolButton#Member { padding:5px; font-size:11px; }
QToolButton#SmallTools { padding-right:14px; }
QFrame#Composer { background:#fff; border:1px solid #cddbe4; border-radius:13px; }
QFrame#Composer[editing="true"] { border-color:#78a6b0; }
QPlainTextEdit { background:transparent; border:0; font-size:14px;
                 selection-background-color:#d0e8ed; }
QTextBrowser { background:transparent; border:0; font-size:13px;
               selection-background-color:#d0e8ed; }
QTreeWidget { background:transparent; border:0; font-size:12px; outline:0;
              selection-background-color:#d8e9ef; selection-color:#164958; }
QTreeWidget::item { min-height:22px; padding:3px 3px; border-radius:6px; }
QTreeWidget::item:hover { background:#e5eff4; }
QTreeWidget::item:selected, QTreeWidget::branch:selected { background:#d8e9ef; }
QComboBox { border:1px solid #cddae3; border-radius:6px; padding:4px 7px;
            background:#fbfdfe; font-size:12px; }
QComboBox:focus { border-color:#6c9fab; }
QComboBox::drop-down { border:0; width:22px; }
QComboBox::down-arrow { image:url("@CHEVRON@"); width:9px; height:6px; }
QComboBox QAbstractItemView { background:#fff; selection-background-color:#e0eef1;
                             selection-color:#193f4a; }
QScrollBar:vertical { width:6px; background:transparent; margin:0; }
QScrollBar::handle:vertical { background:#bfced9; border-radius:3px; min-height:26px; }
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical { height:0; }
QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical { background:transparent; }
'''.replace('@CHEVRON@', _CHEVRON)
