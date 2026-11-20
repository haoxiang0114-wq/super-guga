# -*- coding: utf-8 -*-
"""首次接入教程：列出已装的目标软件，给出可复制的 MCP 配置与“是否已接入”状态。

新增文件，不改 ui/first_run.py。接入点（由主程序在首次配置完成后调用）：

    from ui.mcp_onboarding_dialog import show_mcp_onboarding
    show_mcp_onboarding(parent=None)      # 无 Qt 应用时自行创建，便于单独运行

也可单独预览：
    python -m ui.mcp_onboarding_dialog
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from PyQt5.QtCore import QThread, QTimer, Qt, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (QApplication, QDialog, QFileDialog, QFormLayout,
                             QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                             QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout,
                             QWidget)

from core import guga_paths
from core.adapters import app_reports, mcp_channels
from core.ai_chat import endpoint_label, model_config_error, provider_label
from ui.dialog_fit import fit_dialog

DOUBAO_CONNECTOR_GUIDE = """豆包连接器配置教程（HTTP 优先，不用粘 JSON 文件）

豆包的 MCP 入口叫“自定义连接器”，不在配置文件里。按下面点：

1. 打开豆包桌面端并登录，账号需有个人订阅或团队/企业额度（否则进不了豆包工作）。
2. 打开「插件·技能·伙伴」→「插件」→ 右上角「+ 添加」→「新建自定义连接器」。
3. 传输类型只有 HTTP / STDIO（官方没有 SSE）。首选 HTTP：
   · 服务器名称：guga
   · 传输类型：HTTP
   · 服务器 URL：http://127.0.0.1:<实际端口>/mcp（卡片上会写真实端口）
   · Headers：留空，不要填任何 Key
4. 保存后进入豆包「工作」模式的任务输入框。该连接器只在「工作」模式生效，
   对话模式调不到，/ 和 @ 也搜不到；豆包弹窗原文是「自定义连接器仅支持在本地电脑中使用」。
5. HTTP 端点没启动或端口冲突时，改填同一份卡片文本里的 STDIO 备选：
   · 传输类型：STDIO
   · 命令 / 参数 / 环境变量：照抄卡片，不要自己改路径
6. 官方没有独立的“连通状态/工具清单”页面，只能看「插件 → 管理」。
   是否真的连上以咕嘎「验证连接」的真实握手为准（会列出 5 个工具）；未验证前不要写“已接入”。

重要：整份配置里没有任何模型 API Key。模型 Key 只由你在咕嘎首次配置里自己填，
不会随包分发，也不会出现在这里。"""

# 启动提醒的去重标记：同一天、同一批待接入软件只提醒一次。
_NUDGE_MARKER = guga_paths.RUNTIME / 'mcp_onboarding_nudge.json'


def write_clipboard(value) -> dict:
    """写剪贴板并读回校验；失败返回原因，供调用方降级成可选文本框。"""
    expected = str(value or '')
    result = dict(ok=False, expected=expected, actual='', reason='')
    clipboard = QApplication.clipboard()
    if clipboard is None:
        result['reason'] = '剪贴板不可用'
        return result
    try:
        clipboard.setText(expected)
        QApplication.processEvents()
        actual = clipboard.text() or ''
    except Exception as exc:  # pragma: no cover - 依赖桌面环境
        result['reason'] = str(exc)[:120]
        return result
    result['actual'] = actual
    if expected.replace('\r\n', '\n') == actual.replace('\r\n', '\n'):
        result['ok'] = True
    else:
        result['reason'] = '读回内容与写入不一致（可能被别的程序占用）'
    return result


def manual_copy_dialog(parent, text, reason=''):
    """剪贴板写入失败时的兜底：文本框全选，按 Ctrl+C 手动复制。"""
    dialog = QDialog(parent)
    dialog.setWindowTitle('剪贴板不可用 · 手动复制')
    fit_dialog(dialog, 560, 360, min_width=380, min_height=240)
    layout = QVBoxLayout(dialog)
    tip = QLabel('自动复制没有通过读回校验（%s）。下面文本框已全选，按 Ctrl+C 手动复制。'
                 % (reason or '未知原因'))
    tip.setWordWrap(True)
    layout.addWidget(tip)
    area = QPlainTextEdit(dialog)
    area.setPlainText(text or '')
    layout.addWidget(area, 1)
    row = QHBoxLayout()
    row.addStretch(1)
    close = QPushButton('关闭')
    close.clicked.connect(dialog.accept)
    row.addWidget(close)
    layout.addLayout(row)
    area.selectAll()
    area.setFocus()
    dialog.exec_()


def connector_fields_text(entry) -> str:
    """豆包连接器用的“字段：值”文本（不是 JSON，用户逐栏照填）；HTTP 优先 + STDIO 备选。"""
    return mcp_channels.ui_fields_text(entry if isinstance(entry, dict) else {})


def show_doubao_guide(parent=None, on_copy=None):
    """打开豆包连接器教程；on_copy 为空时不显示“复制全文”。"""
    dialog = QDialog(parent)
    dialog.setWindowTitle('豆包：用连接器接入，不是粘贴 JSON')
    fit_dialog(dialog, 640, 520, min_width=400, min_height=260)
    layout = QVBoxLayout(dialog)
    text = QPlainTextEdit(dialog)
    text.setReadOnly(True)
    text.setPlainText(DOUBAO_CONNECTOR_GUIDE)
    layout.addWidget(text)
    row = QHBoxLayout()
    if callable(on_copy):
        copy = QPushButton('复制全文', dialog)
        copy.clicked.connect(lambda: on_copy(text.toPlainText()))
        row.addWidget(copy)
    row.addStretch(1)
    close = QPushButton('关闭', dialog)
    close.clicked.connect(dialog.accept)
    row.addWidget(close)
    layout.addLayout(row)
    dialog.exec_()


class _VerifyWorker(QThread):
    done = pyqtSignal(dict)

    def __init__(self, app_id, entry, workspace, parent=None):
        super().__init__(parent)
        self.app_id = app_id
        self.entry = entry
        self.workspace = workspace

    def run(self):
        try:
            result = mcp_channels.verify_connection(self.app_id, entry=self.entry,
                                                    workspace=self.workspace)
        except Exception as exc:  # 保证线程一定回报结果
            result = dict(ok=False, status='exception',
                          message='%s: %s' % (type(exc).__name__, exc))
        self.done.emit(result)


def merge_custom_items(data, workspace=None):
    """接入面板数据 + 用户“新增软件”写进 app_reports 的自定义条目（纯函数，可在后台线程跑）。"""
    items = list((data or {}).get('apps') or [])
    known = {str(item.get('app') or '') for item in items}
    try:
        records = app_reports.read_all().get('apps') or {}
    except Exception:
        records = {}
    for app_id, record in records.items():
        if app_id in known:
            continue
        status = mcp_channels.channel_status(app_id, workspace=workspace)
        if status.get('state') != 'report_only':
            continue
        items.append(dict(
            app=app_id, name=status.get('name') or record.get('name') or app_id,
            installed=True, exe=status.get('exe') or record.get('exe') or '',
            action='copy_config', state='report_only', channel=status,
            download={}, probe_message=status.get('message') or '',
            capabilities=list(record.get('capabilities') or []),
        ))
        known.add(app_id)
    # 豆包没装时不显示这一块，避免噪音（用户明确要求）。
    return [item for item in items
            if not (str(item.get('app')) == 'doubao' and not item.get('installed'))]


_LIVE_SCANS = []


def _release_scan(worker):
    try:
        _LIVE_SCANS.remove(worker)
    except ValueError:
        pass
    try:
        worker.deleteLater()
    except Exception:
        pass


class _ScanWorker(QThread):
    """后台探测外部软件/解释器，别把窗口首开卡在“正在检测…”上十几秒。"""

    done = pyqtSignal(dict)

    def __init__(self, apps, workspace, force, parent=None):
        super().__init__(parent)
        self.apps = apps
        self.workspace = workspace
        self.force = force
        self.quiet = False

    def run(self):
        try:
            data = mcp_channels.onboarding(apps=self.apps, workspace=self.workspace,
                                           force_probe=self.force)
            if isinstance(data, dict) and not data.get('error'):
                data['apps'] = merge_custom_items(data, self.workspace)
        except Exception as exc:  # 保证窗口一定拿到结果
            data = dict(error='%s: %s' % (type(exc).__name__, exc))
        self.done.emit(data)


class McpOnboardingDialog(QDialog):
    def __init__(self, parent=None, workspace=None, apps=None):
        super().__init__(parent)
        self.setWindowTitle('连接器与接入状态（MCP 自报路径）')
        # 高度按屏幕可用区收口：150% 缩放下 760 高会超过 1080 屏，底栏按钮被裁掉。
        fit_dialog(self, 760, 760, min_width=520, min_height=360)
        self.workspace = workspace
        self.apps = apps
        self._workers = []
        self._state_sig = None
        self._scan = None
        self._poll_pending = False
        self._poll = QTimer(self)
        self._poll.setInterval(3000)
        self._poll.timeout.connect(self._poll_states)
        self._poll.start()
        self._build()
        # 窗口先出来，探测在后台跑；首开不再用主线程同步扫描。
        self.summary.setText('正在检测已装软件和接入状态…（这一步不会卡住窗口）')
        QTimer.singleShot(0, lambda: self.reload(force=False))

    # -- UI ---------------------------------------------------------------
    def _build(self):
        root = QVBoxLayout(self)
        title = QLabel('连接器与接入状态（MCP 只做发现，指挥仍走界面自动化）')
        title.setStyleSheet('font-size:16px;font-weight:600;')
        root.addWidget(title)

        model_row = QHBoxLayout()
        self.model_status = QLabel('')
        self.model_status.setWordWrap(True)
        model_row.addWidget(self.model_status, 1)
        self.model_button = QPushButton('去配置模型')
        self.model_button.clicked.connect(self._open_model_settings)
        model_row.addWidget(self.model_button)
        root.addLayout(model_row)

        intro = QLabel(
            '不接入：咕嘎只能按常见目录、注册表、开始菜单去猜软件装在哪；猜不到就退回纯视觉操作，'
            '慢且容易被窗口挡住。\n'
            '接入后：软件会调用 report_app 自报 exe 绝对路径，咕嘎记住，以后直接启动。'
            '派单仍然是把窗口置前、模拟键鼠，MCP 不会接管指挥。')
        intro.setWordWrap(True)
        intro.setStyleSheet('color:#1a3c5e;background:#eef4fb;border:1px solid #cfe0f4;'
                            'border-radius:6px;padding:8px;')
        root.addWidget(intro)

        self.summary = QLabel('正在检测…')
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)

        self.server_line = QLabel('')
        self.server_line.setWordWrap(True)
        self.server_line.setStyleSheet('color:#555;')
        root.addWidget(self.server_line)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setMinimumHeight(0)   # 允许被压缩，额外内容交给滚动条
        self.body = QWidget()
        self.cards = QVBoxLayout(self.body)
        self.cards.setSpacing(10)
        self.scroll.setWidget(self.body)
        root.addWidget(self.scroll, 1)

        bottom = QHBoxLayout()
        self.refresh = QPushButton('刷新状态')
        self.refresh.clicked.connect(lambda: self.reload(force=True))
        bottom.addWidget(self.refresh)
        add_app = QPushButton('新增软件')
        add_app.clicked.connect(self._add_app)
        bottom.addWidget(add_app)
        bottom.addStretch(1)
        close = QPushButton('关闭')
        close.clicked.connect(self.accept)
        bottom.addWidget(close)
        root.addLayout(bottom)
        self._refresh_model_status()

    def _refresh_model_status(self):
        cfg = guga_paths.read_config() or {}
        problem = model_config_error(cfg)
        if not problem:
            self.model_status.setText(
                '模型：已配置 · %s · %s · %s。MCP 接入是另一件事，只负责发现软件路径。'
                % (provider_label(cfg), endpoint_label(cfg), cfg.get('model') or '未填写模型名'))
            self.model_status.setStyleSheet('color:#2e7d32;font-weight:600;')
            self.model_button.setText('更换模型')
        else:
            self.model_status.setText('模型：%s' % problem)
            self.model_status.setStyleSheet('color:#c62828;font-weight:600;')
            self.model_button.setText('去配置模型')

    def _open_model_settings(self):
        try:
            from ui.model_settings import open_model_settings
            open_model_settings(self)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, '打不开模型设置', '%s: %s' % (type(exc).__name__, exc))
        self._refresh_model_status()
        self.reload(force=False)

    def _merged_items(self, data):
        return merge_custom_items(data, self.workspace)

    def _add_app(self):
        dialog = _AddAppDialog(self)
        if dialog.exec_() != QDialog.Accepted:
            return
        data = dialog.result_data or {}
        box = QDialog(self)
        box.setWindowTitle('已记录：%s' % data.get('name', ''))
        fit_dialog(box, 620, 460, min_width=420, min_height=280)
        layout = QVBoxLayout(box)
        tip = QLabel('已把自报路径写入 app_reports.json，下次启动会优先用它。\n'
                     '下面是给该软件自己 MCP 设置用的配置；不同软件入口不同，找不到就整段复制问它的帮助。')
        tip.setWordWrap(True)
        layout.addWidget(tip)
        text = QPlainTextEdit(box)
        text.setReadOnly(True)
        text.setPlainText(data.get('text') or '')
        layout.addWidget(text, 1)
        row = QHBoxLayout()
        copy = QPushButton('复制配置')
        hint = QLabel('')

        def do_copy():
            result = write_clipboard(text.toPlainText())
            if result['ok']:
                copy.setText('已复制')
                hint.setText('已复制并读回校验通过。')
                hint.setStyleSheet('color:#2e7d32;')
            else:
                hint.setText('自动复制失败：%s；请在文本框里全选按 Ctrl+C。' % result['reason'])
                hint.setStyleSheet('color:#c62828;')
                text.selectAll()
                text.setFocus()

        copy.clicked.connect(do_copy)
        row.addWidget(copy)
        row.addWidget(hint, 1)
        close = QPushButton('关闭')
        close.clicked.connect(box.accept)
        row.addWidget(close)
        layout.addLayout(row)
        box.exec_()
        self.reload(force=True)

    def _clear_cards(self):
        while self.cards.count():
            item = self.cards.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def reload(self, force=False, quiet=False):
        """后台扫描；同一时间只跑一个，结果回来再渲染。"""
        if self._scan is not None and self._scan.isRunning():
            self._poll_pending = self._poll_pending or force
            return
        if not quiet:
            self.refresh.setEnabled(False)
            self.refresh.setText('检测中…')
            self.summary.setText('正在检测已装软件和接入状态…（已连接窗口可继续看说明）')
        worker = _ScanWorker(self.apps, self.workspace, force, parent=self)
        worker.quiet = quiet
        worker.done.connect(self._apply_data)
        worker.finished.connect(lambda w=worker: self._release_scan(w))
        self._scan = worker
        worker.start()

    def _release_scan(self, worker):
        try:
            if self._scan is worker:
                self._scan = None
        except Exception:
            pass
        self.refresh.setEnabled(True)
        self.refresh.setText('刷新状态')
        try:
            worker.deleteLater()
        except Exception:
            pass
        if self._poll_pending:
            self._poll_pending = False
            self.reload(force=False, quiet=True)

    def _apply_data(self, data):
        if not isinstance(data, dict) or data.get('error'):
            self.summary.setText('检测失败：%s' % ((data or {}).get('error') or '未知原因'))
            self.summary.setStyleSheet('color:#c62828;')
            return
        self.data = data
        merged = list(data.get('apps') or [])
        sig = tuple((item.get('app'), item.get('state')) for item in merged)
        if sig == self._state_sig:
            return
        self._state_sig = sig
        summary = data.get('summary') or {}
        self.summary.setText('已装 %d/%d；已连接（真实握手）%d；仅检测到配置未验证 %d；需界面添加 %d。工作区：%s' % (
            summary.get('installed', 0), summary.get('total', 0),
            summary.get('connected', 0), summary.get('restart', 0),
            summary.get('ui_managed', 0), data.get('workspace', '')))
        server = data.get('server') or {}
        if server.get('ok'):
            args = ' '.join(str(item) for item in (server.get('args') or []))
            self.server_line.setText('MCP server（%s）：%s %s（%s）' % (
                server.get('mode'), server.get('command'), args, server.get('source', '')))
            self.server_line.setStyleSheet('color:#2e7d32;')
        else:
            self.server_line.setText('MCP server 不可用：%s' % (server.get('error') or '未找到启动入口'))
            self.server_line.setStyleSheet('color:#c62828;')
        self._clear_cards()
        for item in merged:
            self.cards.addWidget(self._card(item))
        self.cards.addStretch(1)

    def _poll_states(self):
        """每 3 秒后台轮询，状态变化才重建卡片，避免打断用户复制/填写。"""
        if self._scan is not None and self._scan.isRunning():
            return
        self.reload(force=False, quiet=True)

    STATE_BADGES = {
        'connected': ('已连接（真实握手 / 5 个工具）', '#2e7d32'),
        'restart_required': ('已检测到配置，未验证连接', '#ef6c00'),
        'config_exe_missing': ('配置失效：程序位置变了', '#c62828'),
        'config_conflict': ('配置冲突', '#ef6c00'),
        'connector_pending': ('曾连接过，当前未验证', '#ef6c00'),
        'endpoint_ready': ('通道已就绪，等待软件首次握手', '#ef6c00'),
        'ui_manage': ('未验证：需在官方界面添加', '#c62828'),
        'report_only': ('已记录路径，未验证连接', '#ef6c00'),
        'not_configured': ('已安装，未验证', '#c62828'),
        'server_unavailable': ('MCP 启动入口不可用', '#c62828'),
        'unknown_app': ('未知软件', '#9e9e9e'),
    }

    def _report_line(self, app_id):
        report = app_reports.valid_report(app_id)
        if report.get('valid'):
            record = report.get('record') or {}
            return '最近一次 report_app 自报：%s\n时间：%s' % (
                report.get('exe'), record.get('reported_at') or '未知')
        if report.get('exists'):
            return '最近一次 report_app 自报：记录已失效（%s）→ %s' % (
                report.get('message'), report.get('exe'))
        return '最近一次 report_app 自报：还没有。接上后让软件调用一次 report_app。'

    def _card(self, item):
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        box = QVBoxLayout(frame)
        channel = item.get('channel') or {}
        app_id = str(item.get('app') or '')
        is_doubao = app_id == 'doubao' or channel.get('registration') == 'ui'

        head = QHBoxLayout()
        title = '豆包：用连接器接入，不是粘贴 JSON' if app_id == 'doubao' \
            else (item.get('name') or app_id)
        name = QLabel(title)
        name.setStyleSheet('font-size:14px;font-weight:600;')
        name.setWordWrap(True)
        head.addWidget(name, 1)
        if not item.get('installed'):
            badge_text, color = ('未安装', '#9e9e9e')
        elif app_id == 'doubao':
            badge_text, color = {
                'connected': ('已连接（真实握手）', '#2e7d32'),
                'endpoint_ready': ('通道已就绪，等待豆包首次握手', '#ef6c00'),
                'connector_pending': ('曾连接过，当前未验证', '#ef6c00'),
            }.get(str(item.get('state') or ''), ('未验证：需在官方界面添加', '#c62828'))
        else:
            badge_text, color = self.STATE_BADGES.get(
                str(item.get('state') or ''), ('状态未知', '#555'))
        badge = QLabel(badge_text)
        badge.setStyleSheet('color:%s;font-weight:600;' % color)
        head.addWidget(badge)
        box.addLayout(head)

        if is_doubao:
            channel_text = '发现通道：豆包连接器（在豆包界面新建，本机没有可粘的 JSON 文件）；指挥仍是界面自动化'
        elif channel.get('direction') == 'guga_server':
            channel_text = '发现通道：MCP 自报路径（咕嘎 server → 软件 client，%s）；指挥仍是界面自动化' % (
                channel.get('transport') or 'stdio')
        else:
            channel_text = '通道：界面操作（UI 自动化）'
        if channel.get('fallback') and not is_doubao:
            channel_text += '；兜底：%s' % channel['fallback']
        line = QLabel(channel_text)
        line.setWordWrap(True)
        line.setStyleSheet('color:#444;')
        box.addWidget(line)

        install_line = QLabel('安装：%s' % ('已定位 ' + item.get('exe', '') if item.get('installed')
                                          else '未安装或未定位'))
        install_line.setWordWrap(True)
        install_line.setStyleSheet('color:%s;' % ('#2e7d32' if item.get('installed') else '#c62828'))
        box.addWidget(install_line)

        if not item.get('installed'):
            download = item.get('download') or {}
            hint = QLabel(str(download.get('hint') or ''))
            hint.setWordWrap(True)
            hint.setStyleSheet('color:#666;')
            box.addWidget(hint)
            if download.get('url'):
                btn = QPushButton('打开下载页')
                btn.clicked.connect(lambda _=False, url=download['url']:
                                    QDesktopServices.openUrl(QUrl(str(url))))
                box.addWidget(btn, 0, Qt.AlignLeft)
            return frame

        buttons = QHBoxLayout()
        result_label = QLabel('')
        result_label.setWordWrap(True)

        def add_verify():
            verify = QPushButton('验证连接')
            verify.clicked.connect(lambda _=False: self._test(
                app_id, channel.get('entry'), verify, result_label))
            buttons.addWidget(verify)

        if is_doubao:
            tutorial = QPushButton('查看豆包连接器教程')
            tutorial.clicked.connect(self._show_guide)
            buttons.addWidget(tutorial)
            fields_value = connector_fields_text(channel.get('entry') or {})
            copy = QPushButton('复制连接器字段')

            def copy_fields():
                self._copy_config(fields_value, item, copy, result_label)
                if result_label.text().startswith('已复制'):
                    result_label.setText('已复制连接器字段并读回校验通过；到豆包「插件·技能·伙伴 → '
                                         '插件 → 右上角 + 添加 → 新建自定义连接器」逐栏粘贴。')

            copy.clicked.connect(copy_fields)
            buttons.addWidget(copy)
            add_verify()

            connector_state = QLabel('连接器状态：%s' % badge_text)
            connector_state.setWordWrap(True)
            connector_state.setStyleSheet('color:%s;font-weight:600;' % color)
            box.addWidget(connector_state)

            endpoint = channel.get('http_endpoint') or {}
            default_url = 'http://127.0.0.1:18760/mcp'
            endpoint_text = ('HTTP 端点：%s（%s%s）\n豆包需在「工作」模式下才会调用该连接器；'
                             '对话模式、/ 和 @ 搜不到。'
                             % (endpoint.get('state_text') or '未启动',
                                endpoint.get('url') or default_url,
                                ('；' + endpoint.get('detail')) if endpoint.get('detail') else ''))
            endpoint_label = QLabel(endpoint_text)
            endpoint_label.setWordWrap(True)
            endpoint_label.setStyleSheet('color:%s;' % (
                '#2e7d32' if endpoint.get('state') == 'listening' else '#c62828'))
            box.addWidget(endpoint_label)

            fields = QPlainTextEdit()
            fields.setReadOnly(True)
            fields.setPlainText(fields_value)
            fields.setMaximumHeight(150)
            box.addWidget(fields)

            steps = '\n'.join('· ' + str(step) for step in channel.get('ui_instructions') or [])
            if steps:
                label = QLabel(steps)
                label.setWordWrap(True)
                label.setStyleSheet('color:#555;')
                box.addWidget(label)
        else:
            if channel.get('config_path'):
                path = QLabel('配置位置：%s%s' % (
                    channel['config_path'],
                    '（已存在）' if channel.get('config_exists') else '（尚未创建）'))
                path.setWordWrap(True)
                path.setStyleSheet('color:#444;')
                box.addWidget(path)

            guide = mcp_channels.guidance(app_id) or {}
            if guide.get('where'):
                where = QLabel('粘贴到：%s' % guide.get('where'))
                where.setWordWrap(True)
                where.setStyleSheet('color:#1a3c5e;font-weight:600;')
                box.addWidget(where)
            if guide.get('steps'):
                steps = QLabel('\n'.join('· ' + str(step) for step in guide['steps']))
                steps.setWordWrap(True)
                steps.setStyleSheet('color:#555;')
                box.addWidget(steps)

            text = QPlainTextEdit()
            text.setReadOnly(True)
            text.setPlainText(channel.get('snippet') or '')
            text.setMaximumHeight(160)
            box.addWidget(text)

            copy = QPushButton('复制配置')
            copy.clicked.connect(lambda _=False, value=text.toPlainText(), current=item,
                                 btn=copy, tip=result_label:
                                 self._copy_config(value, current, btn, tip))
            buttons.addWidget(copy)
            if channel.get('registration') == 'file':
                open_dir = QPushButton('打开配置目录')
                open_dir.clicked.connect(lambda _=False: self._open_dir(app_id))
                buttons.addWidget(open_dir)
                if not channel.get('added'):
                    write = QPushButton('写入配置')
                    write.clicked.connect(lambda _=False: self._write(app_id))
                    buttons.addWidget(write)
            add_verify()

        buttons.addStretch(1)
        box.addLayout(buttons)

        if item.get('state') == 'config_exe_missing':
            fix = QLabel('程序位置变了：旧配置里的 exe 已不存在，重启目标软件也连不上。'
                         '请点「复制配置」重新粘一遍，覆盖原条目。')
            fix.setWordWrap(True)
            fix.setStyleSheet('color:#c62828;font-weight:600;')
            box.addWidget(fix)
        elif item.get('state') == 'config_conflict':
            conflict = QLabel('同名条目的命令/参数与本机不一致：可点「写入配置」用本机路径更新，或手动复制覆盖。')
            conflict.setWordWrap(True)
            conflict.setStyleSheet('color:#ef6c00;')
            box.addWidget(conflict)
        elif item.get('state') == 'restart_required' and not is_doubao:
            restart = QLabel('只是检测到配置，还没验证连接：点「验证连接」跑一次真实握手，通过才算真的接上。')
            restart.setWordWrap(True)
            restart.setStyleSheet('color:#ef6c00;')
            box.addWidget(restart)

        if channel.get('message'):
            status_label = QLabel(str(channel['message']))
            status_label.setWordWrap(True)
            status_label.setStyleSheet('color:#666;')
            box.addWidget(status_label)

        report_hint = QLabel(
            self._report_line(app_id) + '\n'
            '连上后让软件调用 report_app 自报安装路径；咕嘎会记住，以后直接按这条路径启动。'
            '复制的配置/字段只有本地命令和参数，不含任何模型 API Key，也不含开发目录或替换后的 Python 路径。')
        report_hint.setWordWrap(True)
        report_hint.setStyleSheet('color:#1565c0;')
        box.addWidget(report_hint)

        box.addWidget(result_label)
        return frame

    def closeEvent(self, event):
        self._poll.stop()
        if self._scan is not None and self._scan.isRunning():
            # 窗口关闭后探测线程还得跑完；交给模块持有，避免 QThread 被 GC 触发 abort。
            worker = self._scan
            self._scan = None
            try:
                worker.done.disconnect()
            except Exception:
                pass
            try:
                worker.setParent(None)
            except Exception:
                pass
            _LIVE_SCANS.append(worker)
            worker.finished.connect(lambda w=worker: _release_scan(w))
        for worker in list(self._workers):
            try:
                worker.done.disconnect()
            except Exception:
                pass
            try:
                worker.setParent(None)
            except Exception:
                pass
            self._workers.remove(worker)
            _LIVE_SCANS.append(worker)
            worker.finished.connect(lambda w=worker: _release_scan(w))
        super().closeEvent(event)

    # -- actions ---------------------------------------------------------
    def last_copy_result(self):
        return getattr(self, '_last_copy', {})

    def _write_clipboard(self, value):
        """写入剪贴板后立刻读回比对；失败时把原文带回去做手动兜底。"""
        return write_clipboard(value)

    def _copy_config(self, text, item, button=None, hint=None):
        result = self._write_clipboard(text)
        self._last_copy = dict(result, app=(item or {}).get('app'), name=(item or {}).get('name'))
        if result['ok']:
            if button is not None:
                button.setText('已复制')
                button.setEnabled(False)
                QTimer.singleShot(3000, lambda: self._reset_copy_button(button))
            if hint is not None:
                where = (mcp_channels.guidance((item or {}).get('app')) or {}).get('where') or ''
                hint.setStyleSheet('color:#2e7d32;')
                hint.setText('已复制并读回校验通过。贴到：%s' % where if where
                             else '已复制并读回校验通过。')
            return result
        if hint is not None:
            hint.setStyleSheet('color:#c62828;')
            hint.setText('剪贴板写入失败：%s。下面弹出可手动全选的文本。' % (result.get('reason') or ''))
        self._show_copy_fallback(text, item, result)
        return result

    def _reset_copy_button(self, button):
        button.setText('复制配置')
        button.setEnabled(True)

    def _show_copy_fallback(self, text, item, result):
        dialog = QDialog(self)
        dialog.setWindowTitle('剪贴板不可用 · 手动复制')
        fit_dialog(dialog, 560, 360, min_width=380, min_height=240)
        layout = QVBoxLayout(dialog)
        tip = QLabel('自动复制没有通过读回校验（%s）。下面文本框已全选，按 Ctrl+C 手动复制。'
                     % (result.get('reason') or '未知原因'))
        tip.setWordWrap(True)
        layout.addWidget(tip)
        area = QPlainTextEdit()
        area.setPlainText(text or '')
        layout.addWidget(area, 1)
        row = QHBoxLayout()
        retry = QPushButton('重试自动复制')
        retry.clicked.connect(lambda _=False: (
            dialog.accept() if self._write_clipboard(text).get('ok') else None))
        row.addWidget(retry)
        row.addStretch(1)
        close = QPushButton('关闭')
        close.clicked.connect(dialog.accept)
        row.addWidget(close)
        layout.addLayout(row)
        area.selectAll()
        area.setFocus()
        dialog.exec_()

    def _show_guide(self):
        show_doubao_guide(self, on_copy=lambda text: self._copy_config(
            text, {'app': 'doubao', 'name': '豆包教程'}, None))

    def _open_dir(self, app_id):
        result = mcp_channels.open_config_dir(app_id, workspace=self.workspace)
        if not result.get('ok'):
            QMessageBox.warning(self, '打不开配置目录', result.get('message') or '')

    def _write(self, app_id):
        result = mcp_channels.apply(app_id, workspace=self.workspace)
        if result.get('ok'):
            QMessageBox.information(self, '已写入', result.get('message') or '')
            self.reload(force=False)
        else:
            QMessageBox.warning(self, '写入失败', result.get('message') or '')

    FAILURE_LABELS = {
        'no_entry': '配置里缺少命令或 --guga-mcp 参数',
        'dev_path': '配置指向开发机路径，不是本机安装目录',
        'config_invalid': '配置文件损坏或读不出来',
        'handshake_failed': '握手失败：exe 不存在 / 起不来 / 超时',
        'tool_mismatch': '工具清单不符（应有 5 个，含 report_app）',
        'exception': '握手线程异常',
    }

    def _test(self, app_id, entry, button=None, label=None):
        """真实握手；按钮置灰并显示进行中，结果原地展示，不再弹模态框。"""
        http_entry = (isinstance(entry, dict)
                      and str(entry.get('transport') or '').upper() == 'HTTP'
                      and str(entry.get('url') or ''))
        if not http_entry:
            if not isinstance(entry, dict) or not entry.get('args'):
                # 豆包 STDIO 备选/界面表单（command+params），握手要用底层 stdio 命令
                entry = mcp_channels.mcp_entry(app_id, workspace=self.workspace)
            command = str((entry or {}).get('command') or '').strip().strip('"')
            if not isinstance(entry, dict) or not command:
                self._show_test_result(label, dict(
                    ok=False, status='no_entry',
                    message='还没有可用的 MCP 条目；先重新检测，或设置 GUGA_MCP_PYTHON 指向能 import mcp 的解释器。'), button)
                return
            if command.lower().endswith('.exe'):
                try:
                    if Path(command).is_absolute() and not Path(command).is_file():
                        self._show_test_result(label, dict(
                            ok=False, status='handshake_failed',
                            message='exe 不存在：%s。程序被移动/改名后要重新复制配置。' % command), button)
                        return
                except OSError:
                    pass
        if button is not None:
            button.setEnabled(False)
            button.setText('验证中…（最长 25 秒）')
        if label is not None:
            label.setStyleSheet('color:#555;')
            label.setText(('正在连 %s 做真实握手…' % entry.get('url')) if http_entry
                          else '正在按配置拉起 MCP server，做握手 + list_tools…')
        worker = _VerifyWorker(app_id, entry, self.workspace, parent=self)
        worker.done.connect(lambda result: self._on_tested(result, button, label))
        worker.finished.connect(lambda w=worker: self._release_verify(w))
        self._workers.append(worker)
        worker.start()

    def _release_verify(self, worker):
        try:
            self._workers.remove(worker)
        except ValueError:
            pass
        try:
            worker.deleteLater()
        except Exception:
            pass

    def _show_test_result(self, label, result, button=None):
        if button is not None:
            button.setEnabled(True)
            button.setText('验证连接')
        if label is None:
            return
        if result.get('ok'):
            tools = result.get('tools') or []
            label.setStyleSheet('color:#2e7d32;font-weight:600;')
            label.setText('已连接：握手通过，工具 %d 个（%s）。' % (len(tools), ', '.join(tools)))
        else:
            status = str(result.get('status') or '')
            reason = self.FAILURE_LABELS.get(status, status or '未知原因')
            label.setStyleSheet('color:#c62828;font-weight:600;')
            label.setText('验证失败（%s）：%s' % (reason, result.get('message') or '没有返回原因'))

    def _on_tested(self, result, button=None, label=None):
        self._show_test_result(label, result or {}, button)


class _AddAppDialog(QDialog):
    """用户手填软件名 + exe 路径：写进 app_reports.json，并生成通用 MCP 配置。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        import re
        self._slug = re
        self.setWindowTitle('新增软件（记录路径并生成配置）')
        fit_dialog(self, 580, 330, min_width=440, min_height=260)
        self.result_data = {}
        self._id_manual = False
        layout = QVBoxLayout(self)
        intro = QLabel(
            '这里只做两件事：① 把该软件的 exe 绝对路径记进 app_reports.json，'
            '咕嘎以后直接按这条路径启动；② 生成一段可复制的 MCP 配置。'
            '不会启动软件，也不会改它自己的文件。')
        intro.setWordWrap(True)
        intro.setStyleSheet('color:#1a3c5e;background:#eef4fb;border:1px solid #cfe0f4;'
                            'border-radius:6px;padding:8px;')
        layout.addWidget(intro)

        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText('例如：我的笔记软件')
        self.name_edit.textChanged.connect(self._sync_id)
        form.addRow('软件名称', self.name_edit)

        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText('自动生成，可改：小写字母/数字/点/下划线/连字符')
        self.id_edit.textEdited.connect(self._mark_manual)
        form.addRow('软件标识', self.id_edit)

        exe_row = QHBoxLayout()
        self.exe_edit = QLineEdit()
        self.exe_edit.setPlaceholderText(r'例如：C:\Program Files\某软件\某软件.exe')
        exe_row.addWidget(self.exe_edit, 1)
        browse = QPushButton('浏览…')
        browse.clicked.connect(self._browse)
        exe_row.addWidget(browse)
        form.addRow('可执行文件', exe_row)
        layout.addLayout(form)

        self.error = QLabel('')
        self.error.setWordWrap(True)
        self.error.setStyleSheet('color:#c62828;')
        layout.addWidget(self.error)
        layout.addStretch(1)

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton('取消')
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        ok = QPushButton('保存并生成配置')
        ok.setDefault(True)
        ok.clicked.connect(self._save)
        row.addWidget(ok)
        layout.addLayout(row)

    def _mark_manual(self, *_):
        self._id_manual = True

    def _sync_id(self, text):
        if self._id_manual:
            return
        slug = self._slug.sub(r'[^a-z0-9._-]+', '-', str(text or '').strip().lower())
        slug = slug.strip('.-_')[:40]
        self.id_edit.setText(slug)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择软件的可执行文件', '',
                                              '可执行文件 (*.exe)')
        if path:
            self.exe_edit.setText(path)

    def _save(self):
        name = self.name_edit.text().strip()
        app_id = app_reports.normalize_app_id(self.id_edit.text())
        exe, code, detail = app_reports.validate_exe(self.exe_edit.text().strip())
        if not name:
            self.error.setText('请填写软件名称，例如“我的笔记软件”。')
            return
        if not app_id:
            self.error.setText('软件标识不合法：只允许小写字母/数字/点/下划线/连字符，'
                               '且以字母或数字开头（最长 40 字符）。')
            return
        if code:
            self.error.setText(detail or '可执行文件不可用')
            return
        written = app_reports.write_report(
            app_id, name, exe, capabilities=['ui_automation'],
            client_id='manual_onboarding')
        if not written.get('ok'):
            self.error.setText(written.get('message') or '写入 app_reports.json 失败')
            return
        generated = mcp_channels.snippet(app_id)
        if not generated.get('ok'):
            self.error.setText('路径已记录，但配置生成失败：%s' % (
                generated.get('message') or '未知原因'))
            return
        self.result_data = dict(app_id=app_id, name=name, exe=exe,
                                text=generated.get('text') or '',
                                entry=generated.get('entry') or {},
                                report=written)
        self.accept()


class _NudgeDialog(QDialog):
    """启动自检后的主动提醒：已安装但没接入的软件，先接入再派单。

    豆包单独成块（用连接器，不是粘 JSON）；没装豆包就完全不出现这一块。
    """

    DOUBAO_STATE = {
        'connector_pending': '曾连接过，当前未验证',
        'endpoint_ready': '通道已就绪，等待豆包首次握手',
        'ui_manage': '未验证：需在官方界面添加',
        'connected': '已连接（真实握手）',
    }

    def __init__(self, pending, parent=None):
        super().__init__(parent)
        self.setWindowTitle('咕嘎 · 软件接入提醒')
        fit_dialog(self, 560, 420, min_width=440, min_height=300)
        # 内容放进滚动区、按钮留在固定底栏，150% 缩放下“现在接入”仍点得到。
        outer = QVBoxLayout(self)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setMinimumHeight(0)
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 4, 0)
        scroll.setWidget(holder)
        outer.addWidget(scroll, 1)
        doubao = next((item for item in pending
                       if str(item.get('app') or '') == 'doubao'), None)
        others = [item for item in pending if str(item.get('app') or '') != 'doubao']

        title = QLabel('发现 %d 款软件已安装，但还没接入咕嘎' % len(pending))
        title.setStyleSheet('font-size:15px;font-weight:600;')
        title.setWordWrap(True)
        layout.addWidget(title)

        if others:
            names = QLabel('、'.join('%s（%s）' % (
                item.get('name') or item.get('app'), '未验证')
                for item in others))
            names.setWordWrap(True)
            names.setStyleSheet('color:#333;')
            layout.addWidget(names)

        if doubao is not None:
            card = QFrame()
            card.setFrameShape(QFrame.StyledPanel)
            card.setStyleSheet('QFrame{border:1px solid #cfe0f4;border-radius:8px;background:#f7fbff;}')
            card_box = QVBoxLayout(card)
            dou_title = QLabel('豆包：用连接器接入，不是粘贴 JSON')
            dou_title.setStyleSheet('font-size:14px;font-weight:600;color:#1a3c5e;')
            dou_title.setWordWrap(True)
            card_box.addWidget(dou_title)
            state_text = self.DOUBAO_STATE.get(str(doubao.get('state') or ''), '连接器未创建')
            dou_state = QLabel('连接器状态：%s' % state_text)
            dou_state.setWordWrap(True)
            dou_state.setStyleSheet('color:%s;font-weight:600;' % (
                '#2e7d32' if '已连接' in state_text else '#ef6c00'))
            card_box.addWidget(dou_state)
            dou_why = QLabel(
                '豆包没有本地 JSON 配置文件，官方入口是「插件·技能·伙伴 → 插件 → 右上角 + 添加 → '
                '新建自定义连接器」。首选 HTTP：服务器名称 guga、传输类型 HTTP、'
                '服务器 URL http://127.0.0.1:%s/mcp、Headers 留空；端点不可用时改填同一份文本里的 '
                'STDIO 备选。该连接器只在「工作」模式生效，对话模式和 /、@ 都搜不到；'
                '官方没有独立的工具清单页面，是否连上以咕嘎「验证连接」的真实握手为准。'
                % ((mcp_channels.http_endpoint().get('port') or 18760)))
            dou_why.setWordWrap(True)
            dou_why.setStyleSheet('color:#555;')
            card_box.addWidget(dou_why)

            dou_row = QHBoxLayout()
            guide = QPushButton('查看豆包连接器教程')
            guide.clicked.connect(lambda _=False: show_doubao_guide(self))
            dou_row.addWidget(guide)
            copy = QPushButton('复制连接器字段')
            dou_row.addWidget(copy)
            dou_row.addStretch(1)
            card_box.addLayout(dou_row)
            dou_feedback = QLabel('')
            dou_feedback.setWordWrap(True)
            card_box.addWidget(dou_feedback)

            def copy_fields():
                try:
                    entry = mcp_channels.form_entry('doubao')
                except Exception as exc:  # noqa: BLE001
                    entry = dict(error='%s: %s' % (type(exc).__name__, exc))
                if entry.get('error'):
                    dou_feedback.setStyleSheet('color:#c62828;')
                    dou_feedback.setText('暂时生成不了连接器字段：%s' % entry['error'])
                    return
                text = connector_fields_text(entry)
                result = write_clipboard(text)
                if result.get('ok'):
                    copy.setText('已复制')
                    dou_feedback.setStyleSheet('color:#2e7d32;')
                    dou_feedback.setText('已写入剪贴板并读回校验通过；到豆包「插件·技能·伙伴 → 插件 → '
                                         '右上角 + 添加 → 新建自定义连接器」逐栏粘贴（HTTP 优先）。')
                else:
                    dou_feedback.setStyleSheet('color:#c62828;')
                    dou_feedback.setText('自动复制失败：%s；下面弹出可手动全选的文本。'
                                         % result.get('reason'))
                    manual_copy_dialog(self, text, result.get('reason'))

            copy.clicked.connect(copy_fields)
            layout.addWidget(card)

        why = QLabel(
            '不接入：咕嘎只能按常见目录、注册表、开始菜单去猜安装路径；猜不到就只能纯视觉派单，'
            '慢、容易被窗口挡住。\n'
            '接入后：软件连上来调用 report_app 自报 exe 路径，咕嘎记住，下次直接启动；'
            '派单仍然走界面自动化，MCP 不接管指挥。\n'
            '接入不等于模型可用：模型 Key 只由你自己在「模型设置」里填，配置里不会出现任何模型 Key。')
        why.setWordWrap(True)
        why.setStyleSheet('color:#1a3c5e;background:#eef4fb;border:1px solid #cfe0f4;'
                          'border-radius:6px;padding:8px;')
        layout.addWidget(why)
        layout.addStretch(1)

        row = QHBoxLayout()
        later = QPushButton('稍后')
        later.clicked.connect(self.reject)
        row.addWidget(later)
        row.addStretch(1)
        now = QPushButton('现在接入（推荐）')
        now.setDefault(True)
        now.clicked.connect(self.accept)
        row.addWidget(now)
        outer.addLayout(row)      # 底栏固定在滚动区外，任何缩放下都能点到


def maybe_show_mcp_nudge(parent=None, workspace=None, force=False):
    """启动自检后调用：有“已安装未接入”就主动提醒；同一天同一批只提醒一次。

    返回 True 表示已经弹过（调用方可以据此跳过“建议安装”弹窗，避免连续两个框）。
    """
    if QApplication.instance() is None:
        return False
    try:
        data = mcp_channels.nudge(workspace=workspace)
    except Exception:
        return False
    pending = data.get('pending') or []
    if not pending:
        return False
    signature = '|'.join(sorted('%s:%s' % (item.get('app'), item.get('action'))
                                for item in pending))
    today = time.strftime('%Y-%m-%d')
    try:
        marker = json.loads(_NUDGE_MARKER.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        marker = {}
    if not force and marker.get('date') == today and marker.get('signature') == signature:
        return False
    if _NudgeDialog(pending, parent=parent).exec_() == QDialog.Accepted:
        show_mcp_onboarding(parent=parent, workspace=workspace)
    try:
        _NUDGE_MARKER.parent.mkdir(parents=True, exist_ok=True)
        _NUDGE_MARKER.write_text(json.dumps(
            dict(date=today, signature=signature,
                 asked_at=time.strftime('%Y-%m-%dT%H:%M:%S')),
            ensure_ascii=False, indent=2), encoding='utf-8')
    except OSError:
        pass
    return True


def show_mcp_onboarding(parent=None, workspace=None, apps=None):
    """主程序接入点：首次启动时调用；返回对话框执行结果。"""
    if QApplication.instance() is None:
        _app = QApplication(sys.argv)
    dialog = McpOnboardingDialog(parent=parent, workspace=workspace, apps=apps)
    return dialog.exec_() == QDialog.Accepted


if __name__ == '__main__':
    app = QApplication(sys.argv)
    dialog = McpOnboardingDialog()
    dialog.show()
    raise SystemExit(app.exec_())
