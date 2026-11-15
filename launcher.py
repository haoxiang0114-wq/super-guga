# -*- coding: utf-8 -*-
"""咕嘎企鹅桌宠守护启动器 — 由 启动黑猫.bat 用 pythonw 调用

作用：
- 以 __main__ 方式运行 main.py（等价于 python main.py）
- 任何启动异常：写入 startup.log，并弹窗显示错误（不再无声消失）
- 正常运行则本进程就是桌宠进程（无多余进程）
"""
import os
import sys
import io
import json
import traceback
import ctypes
import runpy

ROOT = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(ROOT, 'startup.log')

# 官方 Qt 目录（aqt 安装的 Qt 5.15.2；存在时优先使用）
QT_DIR = os.path.join(os.environ.get('USERPROFILE', ''),
                      '.zcode', 'tools', 'qt', '5.15.2', 'msvc2019_64')


def _preload_msvc_runtime():
    """先按名字载入系统 MSVC 运行库，避免后续扩展模块拿到 Qt 自带的旧版本。

    PyQt5 wheel 的 Qt5/bin 里有 msvcp140/vcruntime140 等运行库，_prepare_qt()
    把这个目录注册进 DLL 搜索路径后，先导入 PyQt5 再导入 onnxruntime（声纹门禁、
    RIFE 补帧）会解析到这些旧版本并直接 access violation，进程无提示退出。
    进程内同名 DLL 只加载一次，因此这里先加载系统版本，后续依赖复用同一模块即可。
    """
    if os.name != 'nt':
        return
    import ctypes
    for dll in ('msvcp140.dll', 'msvcp140_1.dll', 'msvcp140_2.dll',
                'vcruntime140.dll', 'vcruntime140_1.dll'):
        try:
            ctypes.WinDLL(dll)
        except OSError:
            pass


def _prepare_qt():
    """在导入 PyQt5 之前：设置正确的 Qt DLL 和插件路径，解决平台插件初始化失败问题"""
    _preload_msvc_runtime()
    qt_bin = None
    qt_plugins = None
    
    # 1. 优先尝试官方 Qt
    official_bin = os.path.join(QT_DIR, 'bin')
    official_plugins = os.path.join(QT_DIR, 'plugins')
    if not getattr(sys,'frozen',False) and os.path.isdir(official_bin) and os.path.isdir(os.path.join(official_plugins, 'platforms')):
        qt_bin = official_bin
        qt_plugins = official_plugins
    
    # 2. 如果官方 Qt 不可用，自动检测 PyQt5 wheel 自带的 Qt
    if qt_plugins is None:
        try:
            import PyQt5
            pyqt5_dir = os.path.dirname(PyQt5.__file__)
            # 尝试常见的 PyQt5 Qt 路径布局
            for qt_sub in ['Qt5', 'Qt', 'qt5', 'qt']:
                candidate_plugins = os.path.join(pyqt5_dir, qt_sub, 'plugins')
                candidate_bin = os.path.join(pyqt5_dir, qt_sub, 'bin')
                if os.path.isdir(os.path.join(candidate_plugins, 'platforms')):
                    qt_plugins = candidate_plugins
                    if os.path.isdir(candidate_bin):
                        qt_bin = candidate_bin
                    break
        except Exception:
            pass
    
    # 3. 应用找到的路径
    if qt_bin:
        try:
            os.add_dll_directory(qt_bin)
        except Exception:
            pass
        # 把 bin 目录加入 PATH 作为备用
        os.environ['PATH'] = qt_bin + os.pathsep + os.environ.get('PATH', '')
    
    if qt_plugins:
        os.environ['QT_PLUGIN_PATH'] = qt_plugins
        platforms = os.path.join(qt_plugins, 'platforms')
        if os.path.isdir(platforms):
            os.environ['QT_QPA_PLATFORM_PLUGIN_PATH'] = platforms
        mediaservice = os.path.join(qt_plugins, 'mediaservice')
        if os.path.isdir(mediaservice):
            os.environ['QT_MEDIA_BACKEND'] = 'windows'
    
    return qt_plugins is not None



def _show_error(text):
    try:
        ctypes.windll.user32.MessageBoxW(None, text, '咕嘎企鹅桌宠 - 启动失败', 0x10)
    except Exception:
        pass


def main():
    if '--uia-worker' in sys.argv:
        from core.uia_service import serve
        serve();return
    # 把输出重定向到 startup.log（pythonw 默认无控制台）
    try:
        logf = open(LOG_PATH, 'wb')
        sys.stdout = io.TextIOWrapper(logf, encoding='utf-8')
        sys.stderr = sys.stdout
    except Exception:
        pass

    def excepthook(tp, val, tb):
        text = ''.join(traceback.format_exception(tp, val, tb))
        try:
            sys.stderr.write(text)
            sys.stderr.flush()
        except Exception:
            pass
        _show_error('桌宠启动时发生错误：\n\n' + text[-2000:])

    sys.excepthook = excepthook

    # 管理员模式：一键拔线需要管理员权限（ai_config.json 的 run_as_admin）
    # UAC 被拒绝时降级为普通模式继续运行（桌宠不消失，仅拔线不可用）
    try:
        with open(os.path.join(ROOT, 'ai_config.json'), 'r', encoding='utf-8') as _f:
            _cfg = json.load(_f)
        if _cfg.get('run_as_admin'):
            import ctypes
            if not ctypes.windll.shell32.IsUserAnAdmin():
                ret = ctypes.windll.shell32.ShellExecuteW(
                    None, 'runas', sys.executable,
                    __import__('subprocess').list2cmdline(([] if getattr(sys,'frozen',False) else [os.path.join(ROOT, 'launcher.py')])+
                        [arg for arg in sys.argv[1:] if arg in ('--voice','--realtime-voice','--bg-live','--daily-plan')]), ROOT, 1)
                # ShellExecuteW 返回 > 32 表示成功; <= 32 表示失败(用户拒绝UAC等)
                if ret and ret > 32:
                    sys.exit(0)  # 新提权进程已启动, 当前进程退出
                # UAC 被拒绝: 降级为普通模式继续运行, 桌宠不消失
                # 拔线功能将不可用, 但其他功能正常
                try:
                    with open(os.path.join(ROOT, 'elev_fail.log'), 'w', encoding='utf-8') as _f2:
                        _f2.write('elevation failed, ShellExecuteW ret=%s\n' % ret)
                except Exception:
                    pass
    except Exception:
        pass

    # 优先加载官方 Qt（修复平台插件初始化失败）
    if _prepare_qt():
        try:
            sys.stderr.write('[launcher] 使用官方 Qt 5.15.2\n')
            sys.stderr.flush()
        except Exception:
            pass

    try:
        # 等价于 python main.py
        if getattr(sys,'frozen',False):
            from main import main as start_pet
            start_pet()
        else:runpy.run_path(os.path.join(ROOT, 'main.py'), run_name='__main__')
    except SystemExit:
        pass
    except Exception:
        excepthook(*sys.exc_info())
        sys.exit(1)


if __name__ == '__main__':
    main()
