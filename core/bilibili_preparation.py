"""Four explicit browser steps. The last step opens the file picker."""
from core.browser_agent import verified_address
from core.existing_browser import ExistingBrowser


HOME = 'https://www.bilibili.com/'
UPLOAD = 'https://member.bilibili.com/platform/upload/video/frame'
STEPS = ('打开默认浏览器', '访问哔哩哔哩', '进入视频投稿页', '打开选择视频面板')
UPLOAD_CONTROLS = frozenset(('上传视频', '选择视频', '选择文件', '点击上传', '上传视频文件'))


def verified_bilibili_page(accessibility, url):
    if verified_address(accessibility, url):
        return True
    return url == HOME and bool(verified_address(accessibility, 'https://bilibili.com/'))


class BilibiliPreparation:
    def __init__(self, computer):
        self.browser = ExistingBrowser(computer)
        self.step = 0
        self._navigation_step = None
        self._picker_attempted = False

    def advance(self, cancel):
        step = self.step
        if cancel.is_set() or step >= len(STEPS):
            return dict(ok=False, message='当前没有待执行的步骤。')
        if step == 0:
            self.browser.select(cancel, launch_url='about:blank')
            result = dict(ok=True, message='已找到默认浏览器窗口。')
        elif step in (1, 2):
            url = HOME if step == 1 else UPLOAD
            confirmed = self._navigate(step, url, cancel)
            result = dict(ok=confirmed,
                          message='已核对页面地址。' if confirmed
                          else '地址未核对成功。若出现登录或验证，请先在浏览器处理。')
        else:
            result = self._file_picker(cancel)
        if result['ok']:
            self.step += 1
        return result

    def _navigate(self, step, url, cancel):
        if self._navigation_step != step:
            self._navigation_step = step
            page = self.browser.navigate(url, cancel, new_tab=step == 1)
            if page.get('navigation_verified') or verified_bilibili_page(page.get('accessibility', {}), url):
                return True
            if cancel.wait(1.2):
                return False
        current = self.browser.snapshot(cancel)
        return verified_bilibili_page(current.get('accessibility', {}), url)

    def _file_picker(self, cancel):
        page = self.browser.snapshot(cancel)
        if not verified_address(page.get('accessibility', {}), UPLOAD):
            return dict(ok=False, message='当前页面不是已核实的视频投稿页，请检查登录状态。')
        labels = [str(control.get('label') or '').strip() for control in page['controls']]
        if any(label in ('请先登录', '立即登录', '扫码登录', '安全验证') for label in labels):
            return dict(ok=False, message='检测到登录或安全验证，请在浏览器处理后再继续。')
        matches = [control for control in page['controls'] if control.get('type') in ('Button', 'Hyperlink')
                   and str(control.get('label') or '').strip() in UPLOAD_CONTROLS]
        if len(matches) != 1:
            return dict(ok=False, message='找不到唯一的文件选择按钮，请在浏览器核对投稿页面。')
        import win32gui

        hwnd = page['hwnd']
        bound_pid = self.browser.bound['pid']

        def file_picker_open():
            import win32process
            foreground = win32gui.GetForegroundWindow()
            return (foreground and win32gui.GetClassName(foreground) == '#32770'
                    and win32process.GetWindowThreadProcessId(foreground)[1] == bound_pid)

        if file_picker_open():
            return dict(ok=True, message='已检测到视频文件选择窗口，可以由你选择文件。')
        if self._picker_attempted:
            return dict(ok=False, message='已经试过打开文件选择窗口，请先人工核对。')
        if cancel.is_set():
            return dict(ok=False, message='已取消本次操作。')
        # The snapshot discovered precisely one file-selection control. Do not
        # focus or inspect the browser again until the OS picker is dismissed.
        self._picker_attempted = True
        self.browser.computer.execute(dict(action='uia', hwnd=hwnd, uia_ref=matches[0]['ref'],
                                           uia_operation='invoke'), cancel)
        for _ in range(6):
            if file_picker_open():
                return dict(ok=True, message='已检测到视频文件选择窗口，可以由你选择文件。')
            if cancel.wait(.2):
                break
        return dict(ok=False, message='按钮已尝试打开，系统未回报文件选择窗口，请先人工核对。')
