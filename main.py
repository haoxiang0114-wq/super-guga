#! python
# -*- coding: utf-8 -*-
"""咕嘎企鹅桌宠入口。"""
import sys
import os

# 将项目根目录加入 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from launcher import _prepare_qt
_prepare_qt()

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt, QTimer
from ui.pet_window import PetWindow

_INSTANCE_MUTEX = None


def _single_instance():
    """Prevent duplicate pets from stacking timers, captures and AI workers."""
    global _INSTANCE_MUTEX
    if os.name != 'nt':
        return True
    import ctypes
    kernel32 = ctypes.windll.kernel32
    _INSTANCE_MUTEX = kernel32.CreateMutexW(None, False, 'Local\\GugaPenguinPet.SingleInstance')
    if not _INSTANCE_MUTEX:
        return True
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        ctypes.windll.user32.MessageBoxW(None, '咕嘎已经在运行啦，看看桌面右下角的托盘。',
                                         '咕嘎企鹅桌宠', 0x40)
        return False
    return True


def main():
    if not _single_instance():
        return
    if os.name == 'nt':
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Guga.DesktopCompanion')
    # Codex owns the current task path. DSH is started only from the fish's
    # explicit conversation entry, never as a side effect of launching Guga.
    # 高 DPI 支持
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("咕嘎企鹅桌宠")
    app.setOrganizationName("BlackCatPet")
    # Without this the pet shows the generic Python icon in the taskbar and
    # Alt-Tab list; the tray already draws the penguin itself.
    icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', 'guga.ico')
    if os.path.exists(icon_path):
        from PyQt5.QtGui import QIcon
        app.setWindowIcon(QIcon(icon_path))
    # This is a tray companion. Closing a transient menu/dialog must not end
    # the desktop process; the existing explicit Quit action still does that.
    app.setQuitOnLastWindowClosed(False)

    # 创建并显示窗口
    window = PetWindow()
    # [teammate-merge] 队友能力接入：两处兜底桥都是「本地先试、失败才用」，
    # 不改变现有行为，任何异常都不影响桌宠启动。
    try:
        from core.app_registry_bridge import install as _install_app_registry
        _install_app_registry()
    except Exception:
        pass
    try:
        from core.executor_bridge import install as _install_executor_bridge
        _install_executor_bridge()
    except Exception:
        pass
    # [teammate-merge] 队友「管家与员工」系统挂载：纯新增，任何异常都不影响桌宠启动。
    # 关闭方式：设置环境变量 GUGA_BUTLER=0
    try:
        from ui.butler_attach_compat import attach_butler_safe
        attach_butler_safe(window)
    except Exception:
        pass
    if '--summon-fish' in sys.argv:
        QTimer.singleShot(400, lambda:window._multi_agent.summon('002'))
    # Only an explicit launch flag starts capture; normal startup stays quiet.
    if '--voice' in sys.argv:
        QTimer.singleShot(300, window._open_voice_recording)
    if '--realtime-voice' in sys.argv:
        QTimer.singleShot(300, window._open_agent_voice)
    if '--daily-plan' in sys.argv:
        QTimer.singleShot(300, window._show_daily_plan)
    if '--bg-live' in sys.argv:
        QTimer.singleShot(300, window._open_bg_live)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
