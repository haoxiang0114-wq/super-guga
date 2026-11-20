# -*- coding: utf-8 -*-
"""启动自检闸门 — 桌宠启动的第一步（在配置向导之前）

顺序: 双击 exe → 本闸门（每项都真实调用被检能力）→ 配置向导 → 建议安装弹窗 → 桌宠

结果四态:
  通过(pass)     真调用成功，显示真实返回值
  降级(degraded) 能用但有损，写清影响 + 缺什么 + 去哪补
  失败(fail)     该能力不可用，写清缺什么 + 去哪补 + 要不要重启
  未测(untested) 本次没法实测（只查了文件/注册表也算未测），绝不冒充“通过”
"""
import json

from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtWidgets import (QApplication, QCheckBox, QDialog, QFrame,
                             QHBoxLayout, QLabel, QProgressBar, QPushButton,
                             QScrollArea, QVBoxLayout, QWidget)

from core import env_check
from ui.dialog_fit import fit_dialog

_LEVEL_STYLE = {
    'ok': ('✓ 通过', '#1d7a3e', '#eef8f1'),
    'info': ('i 提示', '#4a5568', '#f3f5f8'),
    'untested': ('— 未测', '#6b7280', '#f1f2f4'),
    'warn': ('! 降级', '#b36b00', '#fff6e8'),
    'fatal': ('× 失败', '#c0392b', '#fdecea'),
}


class _CheckThread(QThread):
    item = pyqtSignal(dict)
    finished_all = pyqtSignal(list, list)

    def __init__(self, dpi, hwnd=0):
        super().__init__()
        self._dpi = dpi
        self._hwnd = hwnd

    def run(self):
        items = []
        procs, _ok = env_check.scan_old_processes()
        for one in env_check.iter_checks(display_dpi=self._dpi,
                                         old_processes=(procs, _ok),
                                         window_hwnd=self._hwnd):
            items.append(one)
            self.item.emit(one)
        self.finished_all.emit(items, procs)


class _RetryThread(QThread):
    got = pyqtSignal(dict)

    def __init__(self, check_id, context):
        super().__init__()
        self._check_id = check_id
        self._context = dict(context or {})

    def run(self):
        self.got.emit(env_check.run_single(self._check_id, self._context))


class StartupGate(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('咕嘎 · 启动自检')
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self._procs = []
        self._items = []
        self._rows = {}
        self._retry_threads = []
        self._done = False
        self._fatal = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 12)
        root.setSpacing(8)

        title = QLabel('启动自检：每一项都真实调用一次，没测到就标“未测”')
        title.setStyleSheet('font-size:15px; font-weight:bold; color:#1a1c20;')
        root.addWidget(title)
        self._summary = QLabel('正在真实调用各项能力…')
        self._summary.setWordWrap(True)
        self._summary.setStyleSheet('color:#4a5568; font-size:12px;')
        root.addWidget(self._summary)

        # 配置状态条：在 __init__ 里同步算好，先于任何线程显示。
        # 这样即使自检线程一条结果都没回来，窗口里也不是“什么都没有的白框”。
        self._config_banner = QLabel('')
        self._config_banner.setWordWrap(True)
        self._config_banner.setVisible(False)
        root.addWidget(self._config_banner)
        self._refresh_config_banner()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        holder = QWidget()
        self._rows_box = QVBoxLayout(holder)
        self._rows_box.setContentsMargins(0, 0, 4, 0)
        self._rows_box.setSpacing(6)
        self._rows_box.addStretch(1)
        scroll.setWidget(holder)
        root.addWidget(scroll, 1)

        self._kill_box = QCheckBox('结束上面发现的旧进程（推荐——否则会和新桌宠抢活）')
        self._kill_box.setChecked(True)
        self._kill_box.setVisible(False)
        root.addWidget(self._kill_box)

        self._bar = QProgressBar()
        self._bar.setRange(0, max(1, env_check.check_count()))
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        root.addWidget(self._bar)

        btns = QHBoxLayout()
        self._btn_quit = QPushButton('退出')
        self._btn_quit.setStyleSheet(
            'QPushButton{background:transparent;color:#4a5568;padding:8px 16px;'
            'border:1px solid #d5dae2;border-radius:6px;}'
            'QPushButton:hover{background:#f3f5f8;}')
        self._btn_quit.clicked.connect(self.reject)
        self._btn_go = QPushButton('继续 → 进咕嘎')
        self._btn_go.setEnabled(False)
        self._btn_go.setStyleSheet(
            'QPushButton{background:#2f7cf6;color:white;padding:8px 20px;'
            'border-radius:6px;font-weight:bold;}'
            'QPushButton:hover{background:#2468d9;}'
            'QPushButton:disabled{background:#b9c2d0;}')
        self._btn_go.clicked.connect(self._go)
        btns.addWidget(self._btn_quit)
        self._btn_connector = QPushButton('连接器 / 接入状态')
        self._btn_connector.setToolTip('查看 WorkBuddy / 豆包 / JoyCode 的接入情况；只有官方界面显示 guga 才算接入成功')
        self._btn_connector.setStyleSheet(
            'QPushButton{background:transparent;color:#2f5f9e;padding:8px 12px;'
            'border:1px solid #c7d7ee;border-radius:6px;}'
            'QPushButton:hover{background:#eef4fb;}')
        self._btn_connector.clicked.connect(self._open_connectors)
        btns.addWidget(self._btn_connector)
        btns.addStretch(1)
        btns.addWidget(self._btn_go)
        root.addLayout(btns)

        try:
            self._dpi = env_check.effective_dpi() or int(
                QApplication.primaryScreen().logicalDotsPerInch())
        except Exception:
            self._dpi = 0
        self._thread = None
        fit_dialog(self, 700, 660, min_width=520, min_height=420)

    def run_checks(self):
        try:
            hwnd = int(self.winId())
        except Exception:
            hwnd = 0
        self._thread = _CheckThread(self._dpi, hwnd)
        self._thread.item.connect(self._on_item)
        self._thread.finished_all.connect(self._on_done)
        self._thread.start()
        # 看门狗：45 秒一条结果都没回来（被安全软件拦截/权限不足/线程没起来），
        # 也不能只剩一个空白窗口——给出可照做的提示。
        QTimer.singleShot(45000, self._watchdog)

    def _watchdog(self):
        if self._done or self._items:
            return
        self._summary.setText(
            '自检在 45 秒内没有返回任何结果：可能被安全软件拦截、PowerShell 被策略禁止，'
            '或当前用户权限不足。请点左下「退出」后重新双击 exe；'
            '仍空白请右键托盘「导出诊断」，把 logs\\startup.log 一起发出来。')
        self._summary.setStyleSheet('color:#c0392b;font-size:12px;font-weight:bold;')
        self._bar.setVisible(False)
        self._btn_go.setText('自检无结果，请退出重开')
        self._btn_go.setEnabled(False)
        if not self._config_banner.isVisible():
            self._set_config_banner(
                '! 自检无结果', '#c0392b', '#fdecea', '#f3b7b0',
                '本次启动没有拿到任何自检结果（窗口里不会再有别的提示）。'
                '怎么处理：点「退出」后重新双击 exe；若反复出现，右键托盘「导出诊断」，'
                '附上 logs\\startup.log 联系维护者。')

    def _set_config_banner(self, title, color, background, border, body):
        self._config_banner.setText(
            '<b style="color:%s">%s</b><br><span style="color:#3a3f47">%s</span>'
            % (color, title, body))
        self._config_banner.setStyleSheet(
            'QLabel{background:%s;border:1px solid %s;border-radius:6px;'
            'padding:8px 10px;font-size:12px;}' % (background, border))
        self._config_banner.setVisible(True)

    def _refresh_config_banner(self):
        """配置状态条：在 __init__ 里同步算好，先于自检线程显示。

        检查项：配置文件是否存在、目录是否可写、api_key 是否为空、Key 是否含空白。
        全部缺失/异常时给出“缺什么 + 去哪补”，避免只剩一个空白窗口。
        """
        try:
            from core import guga_paths
            path = guga_paths.CONFIG
            base = guga_paths.BASE
        except Exception as exc:
            self._set_config_banner(
                '配置模块加载失败', '#c0392b', '#fdecea', '#f3b7b0',
                '%s: %s。怎么处理：重新解压完整压缩包后再启动，不要把 exe 单独拷出来运行。'
                % (type(exc).__name__, exc))
            return
        header = '配置文件：%s' % path
        try:
            exists = path.is_file()
        except Exception:
            exists = False
        if not exists:
            self._set_config_banner(
                '配置缺失：还没有 ai_config.json', '#b36b00', '#fff6e8', '#f0d9a8',
                '缺哪项：主模型配置（api_key / base_url / model），应位于 %s。<br>'
                '去哪补：关掉本窗口后按提示进入「图形化配置向导」填写；'
                '或手工把 ai_config.json 放到 %s（该目录需可写）。<br>'
                '不补的后果：桌宠能启动，但派单/对话无法调用模型。'
                % (header, base))
            return
        try:
            raw = path.read_text(encoding='utf-8-sig')
            data = json.loads(raw or '{}')
            if not isinstance(data, dict):
                raise ValueError('顶层不是 JSON 对象')
        except Exception as exc:
            self._set_config_banner(
                '配置内容损坏：ai_config.json 读不出来', '#c0392b', '#fdecea', '#f3b7b0',
                '缺哪项：可解析的配置文件（当前报错 %s: %s）。<br>'
                '去哪补：打开 %s 修正 JSON 格式；不确定就删掉它后重开，'
                '按提示进入「图形化配置向导」重填。<br>'
                '不补的后果：所有模型调用都会失败。'
                % (type(exc).__name__, exc, header))
            return
        try:
            cleaned, error = guga_paths.clean_api_key(data.get('api_key'))
        except Exception as exc:
            self._set_config_banner(
                '配置检查失败', '#c0392b', '#fdecea', '#f3b7b0',
                '读 %s 时出错：%s: %s。怎么处理：在「图形化配置向导」里重填后保存。'
                % (header, type(exc).__name__, exc))
            return
        if error:
            self._set_config_banner(
                '配置里的 API Key 不可用', '#c0392b', '#fdecea', '#f3b7b0',
                '缺哪项：合法的主模型 API Key（%s）。<br>'
                '去哪补：关掉本窗口后进入「图形化配置向导」重新粘贴完整 Key；'
                '或编辑 %s 修正 api_key 后重开。' % (error, header))
            return
        if not cleaned:
            self._set_config_banner(
                '配置缺 API Key（api_key 为空）', '#b36b00', '#fff6e8', '#f0d9a8',
                '缺哪项：主模型 API Key（%s）。<br>'
                '去哪补：关掉本窗口后进入「图形化配置向导」填写 api_key / base_url / model；'
                '或编辑 %s 补齐后重开。<br>'
                '不补的后果：桌宠能启动，但派单/对话无法调用模型。' % (header, header))
            return
        try:
            writable = bool(guga_paths.BASE_WRITABLE)
        except Exception:
            writable = False
        if not writable:
            self._set_config_banner(
                '配置目录不可写', '#b36b00', '#fff6e8', '#f0d9a8',
                '缺哪项：可写的用户目录（当前落点 %s）。<br>'
                '去哪补：把 %s 换到可写位置（设置 GUGA_HOME 指向你的文档目录），'
                '或让管理员给该目录写权限后重开。' % (base, base))
            return
        self._config_banner.setVisible(False)

    def _add_row(self, name):
        frame = QFrame()
        frame.setStyleSheet('QFrame{background:#fafbfc;border:1px solid #e6e9ef;'
                            'border-radius:6px;}')
        row = QHBoxLayout(frame)
        row.setContentsMargins(10, 7, 10, 7)
        row.setSpacing(10)
        badge = QLabel('…')
        badge.setFixedWidth(58)
        badge.setAlignment(Qt.AlignCenter)
        badge.setStyleSheet('color:#6b7280;font-weight:bold;font-size:12px;')
        column = QVBoxLayout()
        column.setSpacing(2)
        title = QLabel(name)
        title.setStyleSheet('font-weight:bold;color:#1a1c20;font-size:12px;')
        detail = QLabel('等待真实调用…')
        detail.setWordWrap(True)
        detail.setStyleSheet('color:#4a5568;font-size:12px;')
        hint = QLabel('')
        hint.setWordWrap(True)
        hint.setVisible(False)
        hint.setStyleSheet('color:#8a6d3b;font-size:11px;')
        column.addWidget(title)
        column.addWidget(detail)
        column.addWidget(hint)
        retry = QPushButton('重试')
        retry.setVisible(False)
        retry.setFixedWidth(52)
        retry.setStyleSheet('QPushButton{background:transparent;color:#2f5f9e;'
                            'border:1px solid #c7d7ee;border-radius:4px;padding:2px 4px;'
                            'font-size:11px;}QPushButton:hover{background:#eef4fb;}')
        row.addWidget(badge, 0, Qt.AlignTop)
        row.addLayout(column, 1)
        row.addWidget(retry, 0, Qt.AlignTop)
        self._rows_box.insertWidget(self._rows_box.count() - 1, frame)
        self._rows[name] = (frame, badge, detail, hint, retry)

    def _paint(self, one):
        name = one.get('name', '检查项')
        if name not in self._rows:
            self._add_row(name)
        frame, badge, detail, hint, retry = self._rows[name]
        text, color, background = _LEVEL_STYLE.get(one.get('level', 'info'),
                                                   _LEVEL_STYLE['info'])
        badge.setText(text)
        badge.setStyleSheet('color:%s;font-weight:bold;font-size:12px;'
                            'background:%s;border-radius:4px;padding:2px 4px;'
                            % (color, background))
        detail.setText(one.get('detail', ''))
        hint_text = one.get('hint', '')
        hint.setText(('怎么办：' + hint_text) if hint_text else '')
        hint.setVisible(bool(hint_text))
        if one.get('level') == 'fatal':
            frame.setStyleSheet('QFrame{background:#fdecea;border:1px solid #f3b7b0;'
                                'border-radius:6px;}')
        elif one.get('level') == 'untested':
            frame.setStyleSheet('QFrame{background:#f7f8fa;border:1px dashed #d8dde5;'
                                'border-radius:6px;}')
        else:
            frame.setStyleSheet('QFrame{background:#fafbfc;border:1px solid #e6e9ef;'
                                'border-radius:6px;}')
        check_id = one.get('check_id') or ''
        retry.setVisible(bool(check_id) and one.get('state') in ('degraded', 'fail')
                         and one.get('level') != 'fatal')
        try:
            retry.clicked.disconnect()
        except Exception:
            pass
        retry.clicked.connect(lambda _=False, cid=check_id, n=name: self._retry(cid, n))
        retry.setEnabled(True)
        retry.setText('重试')

    def _on_item(self, one):
        self._items.append(one)
        self._paint(one)
        self._bar.setValue(min(self._bar.maximum(), len(self._items)))
        counts = env_check.summarize(self._items)
        self._summary.setText(
            '已查 %d 项：通过 %d · 降级 %d · 未测 %d · 失败 %d'
            % (len(self._items), counts['pass'], counts['degraded'],
               counts['untested'], counts['fail']))
        self._summary.setStyleSheet('color:#4a5568; font-size:12px;')

    def _retry(self, check_id, name):
        if not check_id:
            return
        frame, badge, _detail, _hint, retry = self._rows.get(name, (None, None, None, None, None))
        if retry is None:
            return
        retry.setEnabled(False)
        retry.setText('…')
        badge.setText('…')
        badge.setStyleSheet('color:#6b7280;font-weight:bold;font-size:12px;')
        thread = _RetryThread(check_id, dict(dpi=self._dpi, hwnd=int(self.winId() or 0)))
        thread.got.connect(self._on_retry_result)
        thread.finished.connect(lambda t=thread: self._retry_threads.remove(t)
                                if t in self._retry_threads else None)
        self._retry_threads.append(thread)
        thread.start()

    def _on_retry_result(self, one):
        check_id = one.get('check_id')
        replaced = False
        for index, old in enumerate(self._items):
            if old.get('check_id') == check_id:
                self._items[index] = one
                replaced = True
                break
        if not replaced:
            self._items.append(one)
        self._paint(one)
        counts = env_check.summarize(self._items)
        self._summary.setText(
            '已查 %d 项：通过 %d · 降级 %d · 未测 %d · 失败 %d'
            % (len(self._items), counts['pass'], counts['degraded'],
               counts['untested'], counts['fail']))
        self._fatal = int((counts.get('levels') or {}).get('fatal') or 0)
        if not self._done:
            return
        if self._fatal:
            self._btn_go.setText('缺少必需环境，无法继续')
            self._btn_go.setEnabled(False)
        elif self._fatal == 0 and self._btn_go.text() == '缺少必需环境，无法继续':
            self._btn_go.setText('带着降级项继续 →')
            self._btn_go.setEnabled(True)

    def _on_done(self, items, procs):
        self._done = True
        self._procs = procs
        if procs:
            self._kill_box.setVisible(True)
        counts = env_check.summarize(items)
        fatal_count = int((counts.get('levels') or {}).get('fatal') or 0)
        self._fatal = fatal_count
        if fatal_count:
            self._summary.setText(
                '发现 %d 项失败且无法启动：请按下面的“怎么办”补齐后重开（失败项可点“重试”）。'
                % fatal_count)
            self._summary.setStyleSheet('color:#c0392b;font-size:12px;font-weight:bold;')
            self._btn_go.setText('缺少必需环境，无法继续')
            self._btn_go.setEnabled(False)
            self._btn_quit.setText('退出')
        elif counts['degraded']:
            self._summary.setText(
                '自检完成：%d 项通过，%d 项降级（程序能启动，对应能力有损），%d 项未测。'
                % (counts['pass'], counts['degraded'], counts['untested']))
            self._summary.setStyleSheet('color:#4a5568;font-size:12px;')
            self._btn_go.setText('带着降级项继续 →')
            self._btn_go.setEnabled(True)
        elif counts['untested']:
            self._summary.setText(
                '自检完成：%d 项通过，%d 项未测（本次没法实测，未冒充通过）。'
                % (counts['pass'], counts['untested']))
            self._summary.setStyleSheet('color:#4a5568;font-size:12px;')
            self._btn_go.setText('继续 →')
            self._btn_go.setEnabled(True)
        else:
            self._summary.setText('自检完成：%d 项全部真实调用并通过。' % counts['pass'])
            self._btn_go.setText('全部通过，继续 →')
            self._btn_go.setEnabled(True)

    def _open_connectors(self):
        try:
            from ui.mcp_onboarding_dialog import show_mcp_onboarding
            show_mcp_onboarding(parent=self)
        except Exception as exc:
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.warning(self, '打不开接入面板', '%s: %s' % (type(exc).__name__, exc))

    def _go(self):
        if self._fatal:
            self.reject()
            return
        if self._procs and self._kill_box.isChecked():
            for pid, _n, _d, proc in self._procs:
                try:
                    proc.terminate()
                except Exception:
                    pass
        self.accept()


def run_startup_gate():
    """comp_main 启动入口调用: 弹自检窗并等用户确认.

    返回结构化结果，调用方必须检查：``code`` 为 ``accepted`` 才继续启动；
    ``canceled``（用户点退出 / 关窗）和 ``fatal``（有致命项）都必须真的退出。
    """
    app = QApplication.instance()
    if app is None:
        return dict(code='accepted', fatal=0)
    gate = StartupGate()
    gate.run_checks()
    if gate.exec_() != QDialog.Accepted:
        return dict(code='canceled', fatal=int(gate._fatal or 0))
    if int(gate._fatal or 0):
        return dict(code='fatal', fatal=int(gate._fatal))
    nudged = False
    try:
        from ui.mcp_onboarding_dialog import maybe_show_mcp_nudge
        nudged = maybe_show_mcp_nudge()
    except Exception:
        nudged = False
    if nudged:
        return dict(code='accepted', fatal=0)
    try:
        from ui.install_suggestions import maybe_show
        maybe_show()
    except Exception:
        pass
    return dict(code='accepted', fatal=0)
