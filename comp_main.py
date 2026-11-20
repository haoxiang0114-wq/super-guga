#! python
# -*- coding: utf-8 -*-
"""参赛版入口 — 管家 + 六员工, 干净产品形态

启动: python comp_main.py
原版开发窗体仍用 main.py 启动, 两者互不影响.
"""
import sys
import os
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from launcher import _prepare_qt
_prepare_qt()

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIcon

try:
    from core.app_log import log
except Exception:
    def log(*args, **kwargs):
        return None


def _report_startup_failure(exc):
    """启动期任何异常都必须留在界面里：给出可照做的下一步，而不是只写“出错”。"""
    try:
        from PyQt5.QtWidgets import QApplication, QMessageBox
        from core import guga_paths, app_log
        import traceback
        detail = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        try:
            app = QApplication.instance() or QApplication(sys.argv)
        except Exception:
            return
        writable = guga_paths.writable()
        if writable:
            fix = ('数据目录可写，问题出在程序自身：先关掉此窗口重新启动一次；'
                   '若仍出现，把下面这段详细错误连同操作步骤发给开发者。')
        else:
            fix = ('这台电脑的用户数据目录不可写（%s）。请确认账户能写自己的用户目录；'
                   '或设置环境变量 GUGA_HOME 指向一个可写目录后重新启动，'
                   '例如 GUGA_HOME=%%LOCALAPPDATA%%\\GUGA。')
            fix = fix % guga_paths.BASE
        box = QMessageBox(QMessageBox.Critical, '咕嘎启动失败',
                          '启动时遇到错误：%s' % (str(exc)[:200] or type(exc).__name__))
        box.setInformativeText('数据目录：%s\n%s\n错误详情已记录到：%s'
                               % (guga_paths.BASE, fix, app_log.log_path()))
        box.setDetailedText(detail)
        box.exec_()
    except Exception:
        pass


def main():
    try:
        return _run()
    except SystemExit:
        raise
    except BaseException as exc:
        log('startup-failed', '%s: %s' % (type(exc).__name__, exc))
        _report_startup_failure(exc)
        sys.exit(1)


def _run():
    # 运行期数据落点：一律用户可写目录（%LOCALAPPDATA%\GUGA，按 APPDATA/用户主目录/
    # 安装目录逐级兜底）。评委把包解压到 Program Files/只读盘时，配置、缓存、交付物
    # 都不能写失败；安装目录里的旧配置会先复制到用户目录（只复制不删除）。
    from core import guga_paths
    guga_paths.prepare()
    log('paths', '%s；旧配置迁移：%s' % (
        guga_paths.describe(), guga_paths.migration_status().get('status')))

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("咕嘎管家(参赛版)")
    app.setWindowIcon(QIcon(str(Path(__file__).resolve().parent / 'resources/guga.ico')))
    app.setQuitOnLastWindowClosed(False)
    log('qt-ready')

    # 第一步: 启动自检闸门 —— 先查旧进程(会抢单顶号的旧值守程序)/缺失依赖/
    # 屏幕环境, 全部过目后才进配置向导和应用. 问题在第一秒暴露, 不等派单才炸.
    from ui.startup_gate import run_startup_gate
    gate = run_startup_gate() or {}
    log('gate-done', str(gate.get('code') or ''))
    if gate.get('code') != 'accepted':
        # 用户点“退出/关窗”或出现致命项：真的结束进程，不再往下进向导和桌宠。
        log('gate-exit', 'code=%s fatal=%s' % (gate.get('code'), gate.get('fatal')))
        return 0

    # 第二步: 首次配置向导 —— 检测不到有效 ai_config.json 时自动弹出,
    # 图形化选平台/填 Key/测试连接, 小白全程不碰 JSON.
    from ui.first_run import ensure_config, config_ok, notify_incomplete, maybe_adopt_legacy
    # P0：安装目录旧配置默认不沿用，必须先让用户明确选择。
    legacy_choice = maybe_adopt_legacy()
    log('legacy-config', legacy_choice or 'none')
    if not ensure_config():
        # 向导里已经明确询问过“退出 / 稍后配置 / 最小化到托盘”，这里是用户主动选择退出。
        log('config-cancelled', '用户选择退出首次配置')
        return 0
    log('config-ready')

    from ui.comp_window import CompWindow
    window = CompWindow()
    window.show()
    window.raise_()
    log('window-shown')

    # 用户选择“先保存、不填 Key”时也要正常进桌宠；800ms 后给一次可操作提示，
    # 直接指向「模型设置」，不让用户以为程序坏了或找不到补填入口。
    if not config_ok():
        from PyQt5.QtCore import QTimer
        log('config-incomplete', '以受限模式启动，稍后提示补填')
        QTimer.singleShot(
            800, lambda: notify_incomplete(window, window.open_model_settings))

    code = app.exec_()
    log('eventloop-exit', 'code=%s' % code)
    sys.exit(code)


if __name__ == "__main__":
    main()
