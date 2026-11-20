# -*- coding: utf-8 -*-
"""首次配置向导 — 给完全不懂 JSON / 没碰过 API 的人用

设计原则（小白口径）:
  1. 桌宠启动时检测不到有效配置 → 自动弹出本向导（用户不需要知道任何文件名）
  2. 对话 API 用"选平台"代替"填地址"：下拉选 DeepSeek/智谱/通义/自定义，
     地址和模型名自动填好，用户只填一个 API Key
  3. 每一项都有"测试"按钮，现场调一次真实接口，绿勾才允许下一步——
     避免"配完跑不起来不知道哪错了"
  4. 云端大脑段可选：填了就有管家监工/跨设备，不填桌宠照样能跑
  5. 保存时 json.dump 生成合法 JSON，用户永远不手写引号和逗号

接入(comp_main.py): 启动前调 ui.first_run.ensure_config(parent=None)，
返回 True=配置就绪继续启动；False=用户关闭了向导（提示后退出）。
"""
import json
import os
import socket
import urllib.error
import urllib.request

from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QComboBox, QLineEdit, QPushButton, QCheckBox,
                             QGroupBox, QGridLayout, QMessageBox, QFrame,
                             QMenu, QScrollArea, QSystemTrayIcon, QStyle,
                             QWidget)

from ui.dialog_fit import fit_dialog

# 关窗后仍在跑的探针必须由模块持有，否则 QThread 被 GC 会 abort 整个进程。
_LIVE_PROBES = []


def _release_probe(probe):
    try:
        _LIVE_PROBES.remove(probe)
    except ValueError:
        pass
    try:
        probe.deleteLater()
    except Exception:
        pass

from core import guga_paths

CONFIG_PATH = str(guga_paths.CONFIG)   # 用户可写目录，不依赖安装目录

# 平台预设: (显示名, base_url, model, 申请链接)
PRESETS = [
    ('智谱 GLM（推荐：有免费额度，注册最快）',
     'https://open.bigmodel.cn/api/paas/v4', 'glm-4-flash',
     'https://open.bigmodel.cn/usercenter/apikeys'),
    ('DeepSeek 官方',
     'https://api.deepseek.com', 'deepseek-chat',
     'https://platform.deepseek.com/api_keys'),
    ('通义千问（阿里）',
     'https://dashscope.aliyuncs.com/compatible-mode/v1', 'qwen-plus',
     'https://bailian.console.aliyun.com/'),
    ('自定义（自己填地址和模型名）',
     '', '', ''),
]

DEFAULT_VISION_MODEL = 'glm-4.6v-flash'


def config_ok(path=None):
    """配置是否已可用: api_key/base_url/model 都非占位（统一走 guga_paths 读取）."""
    cfg = guga_paths.read_config(path=path)
    key = str(cfg.get('api_key', ''))
    url = str(cfg.get('base_url', ''))
    model = str(cfg.get('model', ''))
    return (bool(key) and 'YOUR_' not in key
            and url.startswith('http') and 'YOUR_' not in url
            and bool(model) and 'YOUR_' not in model)


def _snippet(text, size=150):
    text = ' '.join(str(text or '').split())
    return text[:size]


def describe_error(exc, endpoint, model=''):
    """把探测异常翻译成“哪一步错了 + 改哪个字段”，并保留 URL/模型/HTTP 状态与服务端原文。

    服务端 error 字段常是对象（{message,type,code}），旧代码直接对它切片会抛 TypeError，
    在 QThread 里没人接住就会把进程带崩，所以这里所有取值都做类型保护。
    """
    status = getattr(exc, 'code', None)
    body = ''
    response = getattr(exc, 'response', None)
    if response is not None:
        # model_endpoint.post 抛的是 requests.HTTPError，状态/正文在 response 上。
        status = getattr(response, 'status_code', None) or status
        endpoint = getattr(response, 'url', '') or endpoint
        try:
            body = response.text or ''
        except Exception:
            body = ''
    if hasattr(exc, 'read'):
        try:
            raw = exc.read()
            body = raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else str(raw)
        except Exception:
            body = ''
    message, code = '', ''
    try:
        data = json.loads(body) if body else {}
        err = data.get('error') if isinstance(data, dict) else None
        if isinstance(err, dict):
            message = str(err.get('message') or '')
            code = str(err.get('code') or err.get('type') or '')
        elif isinstance(err, str):
            message = err
        elif isinstance(data, dict):
            message = str(data.get('message') or data.get('msg') or '')
            code = str(data.get('code') or '')
    except Exception:
        message = _snippet(body)
    lower = ('%s %s %s' % (message, code, body)).lower()
    reason = getattr(exc, 'reason', '') if isinstance(exc, urllib.error.URLError) else ''

    if isinstance(exc, socket.timeout) or 'timed out' in lower or 'timeout' in lower:
        kind, fix = '超时（25 秒内没有响应）', '检查网络/代理，或换一个能访问的服务地址'
    elif status in (401, 403) and any(k in lower for k in (
            'unpurchased', 'access denied', 'quota', 'balance', 'insufficient', '额度')):
        kind, fix = '额度不足（账号未开通该模型/无可用额度）', '到平台确认该模型已开通、免费额度还在，或换一个可用模型'
    elif status in (401, 403):
        kind, fix = 'Key 无效或没有该模型权限', '检查「API Key」是否粘贴完整；确认账号有该模型权限'
    elif status == 404 and ('model' in lower or 'model_not_found' in lower):
        kind, fix = '模型不存在', '地址已连通，改「模型名」（要带平台版本号，如 qwen3.8-max-0902）'
    elif status == 404 or 'not found' in lower:
        kind, fix = '接口地址不存在', '核对「接口地址」（通常以 /v1 结尾）'
    elif status == 405:
        from core.model_endpoint import http_failure, text_of
        kind = '接口地址与协议不匹配'
        fix = text_of(http_failure(405, endpoint, model=model))
    elif status == 429 or 'rate limit' in lower:
        kind, fix = '被限流', '稍后重试，或降低请求频率'
    elif status == 400:
        kind, fix = '请求被拒（地址或模型名与平台不匹配）', '按平台文档核对「接口地址」与「模型名」'
    else:
        kind, fix = '网络不通', '检查「接口地址」是否可访问（含 http(s)、端口正确），以及本机网络/代理'

    parts = ['%s%s' % (kind, '（HTTP %s）' % status if status else '')]
    parts.append('URL %s' % endpoint)
    if model:
        parts.append('模型 %s' % model)
    if reason:
        parts.append('原因 %s' % _snippet(reason, 60))
    if message:
        parts.append('服务端：%s%s' % (_snippet(message, 110),
                                    ' [%s]' % code if code else ''))
    elif body:
        parts.append('返回：%s' % _snippet(body, 110))
    parts.append('下一步：' + fix)
    return ' | '.join(parts)


class _Probe(QThread):
    """后台探针: 调一次真实接口验证配置. done(ok, msg) 回主线程."""

    done = pyqtSignal(bool, str)

    def __init__(self, kind, base_url, api_key, model, extra=None):
        super().__init__()
        if kind == 'chat':
            from core.model_endpoint import normalize_base_url
            fixed = normalize_base_url(base_url)
            base_url = fixed.get('base') or base_url
        self.kind, self.base, self.key = kind, base_url.rstrip('/'), api_key
        self.model, self.extra = model, extra or {}

    def endpoint(self):
        return (self.base + '/chat/completions' if self.kind == 'chat'
                else self.base + '/api/login')

    def run(self):
        try:
            import requests
            from core import model_endpoint
            if self.kind == 'chat':
                url = model_endpoint.request_url(self.base)
                body = {'model': self.model, 'messages': [
                    {'role': 'user', 'content': '回复"OK"两个字母即可'}],
                    'max_tokens': 8, 'stream': False}
                # 统一 POST：重定向保持 POST，405 + 系统代理时自动绕代理重试。
                # 直接 urlopen 会在 302 后把 POST 改成 GET，被只接受 POST 的网关回 405。
                r = model_endpoint.post(
                    url, headers={'Content-Type': 'application/json',
                                  'Authorization': 'Bearer ' + self.key},
                    json=body, timeout=25)
                status, raw = r.status_code, r.text
                if status != 200:
                    exc = requests.HTTPError('模型服务 HTTP 错误', response=r)
                    r.close()
                    raise exc
                r.close()
                resp = json.loads(raw)
                text = ((resp.get('choices') or [{}])[0].get('message') or {}
                        ).get('content', '')
                self.done.emit(True, '对话 API 连通（HTTP %s）· 模型 %s 已回复：%s'
                               % (status or 200, self.model,
                                  _snippet(text.strip(), 24) or '(空)'))
            elif self.kind == 'cloud':
                # 云端登录同属 JSON POST，也走保留 POST 的统一请求，避免 302 后被改成 GET。
                r = model_endpoint.post(
                    self.endpoint(), headers={'Content-Type': 'application/json'},
                    json={'username': self.key, 'password': self.model}, timeout=20)
                status, raw = r.status_code, r.text
                if status != 200:
                    exc = requests.HTTPError('云端登录 HTTP 错误', response=r)
                    r.close()
                    raise exc
                r.close()
                resp = json.loads(raw)
                self.done.emit(True, '云端登录成功（账号 %s）' % resp.get('username', ''))
        except Exception as exc:
            # 线程里绝不能带着异常出去：Qt 会把未捕获异常升级成 abort，进程直接消失。
            try:
                msg = describe_error(exc, self.endpoint(),
                                     self.model if self.kind == 'chat' else '')
            except Exception as exc2:
                msg = '连接失败（%s）：%s；下一步：检查接口地址与 Key' % (
                    type(exc2).__name__, _snippet(exc, 80))
            try:
                self.done.emit(False, msg)
            except Exception:
                pass


class FirstRunWizard(QDialog):
    """三段式配置窗口: 对话API(必填) / 云端大脑(可选) / 保存."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle('咕嘎 · 首次配置向导')
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self._probes = []
        self._chat_ok = False
        self._cloud_ok = False
        self._allow_start = False
        self._tray = None
        self._closing_choice = ''

        # 内容放滚动区、保存按钮放固定底栏：150%/175% 缩放下窗口不再超屏，
        # 内容超出可用高度时滚动，底栏按钮始终可点。
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setMinimumHeight(0)   # 允许压缩；超出部分交给滚动条
        self._content = QWidget()
        root = QVBoxLayout(self._content)
        root.setContentsMargins(18, 14, 18, 12)
        self._scroll.setWidget(self._content)
        outer.addWidget(self._scroll, 1)

        title = QLabel('三步让咕嘎跑起来：① 选平台填 Key  ②（可选）填云端账号  ③ 保存')
        title.setStyleSheet('font-size:14px; font-weight:bold; color:#1a1c20;')
        root.addWidget(title)

        # ---------- ① 对话 API ----------
        g1 = QGroupBox('① 对话 API（必填 —— 咕嘎的大脑）')
        f1 = QGridLayout(g1)
        self._preset = QComboBox()
        for name, *_ in PRESETS:
            self._preset.addItem(name)
        self._preset.currentIndexChanged.connect(self._on_preset)
        f1.addWidget(QLabel('平台'), 0, 0)
        f1.addWidget(self._preset, 0, 1, 1, 2)
        self._url = QLineEdit()
        self._url.setPlaceholderText('https://…（选平台自动填，自定义才手填）')
        f1.addWidget(QLabel('接口地址'), 1, 0)
        f1.addWidget(self._url, 1, 1, 1, 2)
        self._model = QLineEdit()
        f1.addWidget(QLabel('模型名'), 2, 0)
        f1.addWidget(self._model, 2, 1, 1, 2)
        self._key = QLineEdit()
        self._key.setEchoMode(QLineEdit.Password)
        self._key.setPlaceholderText('粘贴你的 API Key（形如 sk-…）')
        f1.addWidget(QLabel('API Key'), 3, 0)
        f1.addWidget(self._key, 3, 1)
        self._key_vis = QCheckBox('显示')
        self._key_vis.toggled.connect(
            lambda on: self._key.setEchoMode(
                QLineEdit.Normal if on else QLineEdit.Password))
        f1.addWidget(self._key_vis, 3, 2)
        self._link = QLabel()
        self._link.setOpenExternalLinks(True)
        f1.addWidget(QLabel('申请 Key'), 4, 0)
        f1.addWidget(self._link, 4, 1, 1, 2)
        self._btn_chat = QPushButton('测试连接')
        self._btn_chat.clicked.connect(self._test_chat)
        f1.addWidget(self._btn_chat, 5, 1)
        self._lbl_chat = QLabel('未测试')
        self._lbl_chat.setWordWrap(True)
        self._lbl_chat.setStyleSheet('color:#8a8f98;')
        f1.addWidget(self._lbl_chat, 5, 2)
        vision_hint = QLabel('主模型不支持看图？保存后到「模型设置 → 视觉模型」单独指定一个视觉模型即可，'
                             '对话/拆解仍用这里的主模型。')
        vision_hint.setWordWrap(True)
        vision_hint.setStyleSheet('color:#8a8f98;font-size:12px')
        f1.addWidget(vision_hint, 6, 0, 1, 3)
        root.addWidget(g1)

        # ---------- ② 云端大脑 ----------
        g2 = QGroupBox('② 云端大脑（可选 —— 不填则没有管家派单/跨设备，其余功能不受影响）')
        f2 = QGridLayout(g2)
        self._cbase = QLineEdit()
        self._cbase.setPlaceholderText('购买云端账号后拿到的服务地址（不填则没有云端能力）')
        f2.addWidget(QLabel('服务地址'), 0, 0)
        f2.addWidget(self._cbase, 0, 1, 1, 2)
        self._cuser = QLineEdit()
        self._cuser.setPlaceholderText('购买云端账号后拿到的用户名')
        f2.addWidget(QLabel('账号'), 1, 0)
        f2.addWidget(self._cuser, 1, 1)
        self._cpass = QLineEdit()
        self._cpass.setEchoMode(QLineEdit.Password)
        f2.addWidget(QLabel('密码'), 2, 0)
        f2.addWidget(self._cpass, 2, 1)
        self._btn_cloud = QPushButton('测试登录')
        self._btn_cloud.clicked.connect(self._test_cloud)
        f2.addWidget(self._btn_cloud, 3, 1)
        self._lbl_cloud = QLabel('未填 = 跳过云端')
        self._lbl_cloud.setWordWrap(True)
        self._lbl_cloud.setStyleSheet('color:#8a8f98;')
        f2.addWidget(self._lbl_cloud, 3, 2)
        root.addWidget(g2)

        # ---------- ③ 保存 ----------
        tip = QLabel('提示：API Key 只保存在本机，不会上传，也不会进代码仓库。')
        tip.setStyleSheet('color:#8a8f98; font-size:11px;')
        root.addWidget(tip)
        self._where = QLabel()
        self._where.setWordWrap(True)
        root.addWidget(self._where)
        self._refresh_where()
        footer = QWidget(self)
        row = QHBoxLayout(footer)
        row.setContentsMargins(18, 8, 18, 12)
        self._btn_save = QPushButton('保存并启动咕嘎')
        self._btn_save.setStyleSheet(
            'QPushButton{background:#2f7cf6;color:white;padding:8px 20px;'
            'border-radius:6px;font-weight:bold;}'
            'QPushButton:hover{background:#2468d9;}')
        self._btn_save.clicked.connect(self._save)
        row.addStretch(1)
        row.addWidget(self._btn_save)
        row.addStretch(1)
        outer.addWidget(footer)   # 底栏在滚动区外，任何缩放下都能点到

        self._on_preset(0)
        fit_dialog(self, 560, 640, min_width=420, min_height=360)

    # ---------- 交互 ----------

    def _refresh_where(self):
        """明确告诉用户配置存到哪；用户目录不可写时说清替代位置，避免“保存失败”干卡住。"""
        try:
            if guga_paths.writable():
                if guga_paths.BASE_SOURCE == '安装目录':
                    self._where.setText(
                        '配置保存位置：%s（安装目录兜底，覆盖升级/换机器可能丢失）'
                        % guga_paths.CONFIG)
                else:
                    self._where.setText(
                        '配置保存位置：%s（用户可写目录，覆盖升级/换机器都不影响）'
                        % guga_paths.CONFIG)
                self._where.setStyleSheet('color:#4a5568; font-size:11px;')
            else:
                self._where.setText(
                    '注意：用户可写目录当前不可用，配置会尝试保存到 %s（%s）；'
                    '若该位置也只读，本次配置只在当前会话生效。'
                    % (guga_paths.CONFIG, guga_paths.BASE_SOURCE))
                self._where.setStyleSheet('color:#b36b00; font-size:11px;')
        except Exception:
            self._where.setText('配置保存位置：%s' % CONFIG_PATH)

    def _on_preset(self, idx):
        name, url, model, link = PRESETS[idx]
        if url:
            self._url.setText(url)
            self._model.setText(model)
            self._url.setEnabled(False)
            self._model.setEnabled(False)
        else:
            self._url.clear()
            self._model.clear()
            self._url.setEnabled(True)
            self._model.setEnabled(True)
        self._link.setText('<a href="%s" style="color:#2f7cf6;">去申请 Key（注册后在控制台生成）</a>'
                           % link if link else '')

    def _test_chat(self):
        base, key, model = self._url.text().strip(), self._key.text().strip(), \
            self._model.text().strip()
        if not (base.startswith('http') and key and model):
            self._lbl_chat.setText('请先把平台、Key、模型名填全')
            self._lbl_chat.setStyleSheet('color:#c25b00;')
            return
        self._lbl_chat.setText('测试中…（最长 25 秒）')
        self._lbl_chat.setStyleSheet('color:#8a8f98;')
        self._btn_chat.setEnabled(False)
        p = _Probe('chat', base, key, model)
        p.done.connect(self._chat_result)
        self._probes.append(p)
        p.start()

    def _chat_result(self, ok, msg):
        try:
            self._btn_chat.setEnabled(True)
            self._chat_ok = ok
            self._lbl_chat.setText(('✅ ' if ok else '❌ ') + str(msg))
            self._lbl_chat.setStyleSheet('color:%s;' % ('#1d7a3e' if ok else '#b3261e'))
        except Exception:
            pass                     # 槽里再出错也不能让窗口/进程退出

    def _test_cloud(self):
        base, user, pwd = self._cbase.text().strip(), self._cuser.text().strip(), \
            self._cpass.text()
        if not (base.startswith('http') and user and pwd):
            self._lbl_cloud.setText('地址/账号/密码没填全（不想填就留空整个②段）')
            self._lbl_cloud.setStyleSheet('color:#c25b00;')
            return
        self._lbl_cloud.setText('测试中…')
        self._btn_cloud.setEnabled(False)
        p = _Probe('cloud', base, user, pwd)
        p.done.connect(self._cloud_result)
        self._probes.append(p)
        p.start()

    def _cloud_result(self, ok, msg):
        try:
            self._btn_cloud.setEnabled(True)
            self._cloud_ok = ok
            self._lbl_cloud.setText(('✅ ' if ok else '❌ ') + str(msg))
            self._lbl_cloud.setStyleSheet('color:%s;' % ('#1d7a3e' if ok else '#b3261e'))
        except Exception:
            pass

    def _save(self):
        # F-05：填了 Key 却没选服务商/模型名时明确拦下；绝不带着空地址保存后静默发往默认端点。
        key, key_error = guga_paths.clean_api_key(self._key.text())
        if key_error:
            QMessageBox.warning(self, 'API Key 不合法', key_error)
            return
        url = self._url.text().strip()
        model = self._model.text().strip()
        if key and not url.startswith('http'):
            QMessageBox.warning(
                self, '未选择服务商',
                '已填 API Key，但没有填接口地址（Base URL）。\n'
                '咕嘎不会把你的 Key 发到任何默认服务商，请先选择平台或填写接口地址。')
            return
        if key and not model:
            QMessageBox.warning(self, '未选择模型名',
                                '已填 API Key，但模型名（模型 id）为空；请补上，例如 deepseek-chat。')
            return
        if not self._chat_ok:
            box = QMessageBox(self)
            box.setWindowTitle('先测一下')
            box.setText('对话 API 还没测试通过，直接保存大概率跑不起来。')
            keep = box.addButton('仍要保存（之后可重新打开向导修改）', QMessageBox.YesRole)
            box.addButton('返回填写', QMessageBox.NoRole)
            box.exec_()
            if box.clickedButton() is not keep:
                return
        cfg = guga_paths.read_config()          # 统一读取，保留用户已有其它配置段
        cfg['api_key'] = key
        cfg['base_url'] = self._url.text().strip()
        cfg['model'] = self._model.text().strip()
        if cfg['api_key']:
            cfg['key_source'] = 'local_input'    # 设置页如实显示“本机填写”
        else:
            cfg.pop('key_source', None)
        if self._cuser.text().strip() and self._cpass.text():
            cfg['cloud'] = {
                'base_url': self._cbase.text().strip(),
                'username': self._cuser.text().strip(),
                'password': self._cpass.text(),
                'device': os.environ.get('COMPUTERNAME', '桌宠本机'),
                'poll_secs': 6,
            }
        else:
            cfg.pop('cloud', None)
        try:
            guga_paths.write_config(cfg)
        except guga_paths.ApiKeyError as e:
            QMessageBox.warning(self, 'API Key 不合法', str(e))
            return
        except Exception as e:
            # 所有可写位置都失败：不弹“保存失败”卡死，配置只在本次进程生效，桌宠照常启动。
            guga_paths.remember_config(cfg)
            box = QMessageBox(self)
            box.setWindowTitle('配置未能写入磁盘')
            box.setIcon(QMessageBox.Warning)
            box.setText('配置暂时写不进本机用户目录，本次启动已临时生效，桌宠可以正常使用；'
                        '重启后可能需要重新填写。')
            box.setInformativeText(
                '尝试路径：%s\n原因：%s\n下一步：检查该目录的写入权限，'
                '或用环境变量 GUGA_HOME 指到一个可写目录后重开咕嘎。'
                % (guga_paths.CONFIG, e))
            box.setStandardButtons(QMessageBox.Ok)
            box.exec_()
        self._allow_start = True
        self._detach_probes()      # 保存立刻生效，探针在后台自己收尾，UI 不再冻结
        self.accept()

    def _detach_probes(self):
        """把还在跑的探针交给模块级列表持有并等它自然结束，避免 Qt 线程被窗口带走。"""
        for probe in list(self._probes):
            _LIVE_PROBES.append(probe)
            try:
                probe.done.disconnect()
            except Exception:
                pass
            try:
                probe.finished.connect(lambda p=probe: _release_probe(p))
            except Exception:
                pass
        self._probes = []

    def _ensure_tray(self):
        if self._tray is not None:
            return True
        if not QSystemTrayIcon.isSystemTrayAvailable():
            QMessageBox.information(
                self, '本机没有系统托盘',
                '这台机器没有可用的系统托盘，不能最小化到托盘。\n'
                '请选择「稍后配置（先进桌宠）」或「退出咕嘎」。')
            return False
        icon = self.windowIcon()
        if icon.isNull():
            icon = self.style().standardIcon(QStyle.SP_ComputerIcon)
        tray = QSystemTrayIcon(icon, self)
        menu = QMenu()
        open_action = menu.addAction('继续配置')
        open_action.triggered.connect(self._restore_from_tray)
        menu.addSeparator()
        quit_action = menu.addAction('退出咕嘎')
        quit_action.triggered.connect(self.reject)
        tray.setContextMenu(menu)
        tray.setToolTip('咕嘎还没配置模型：双击图标继续配置，或在「稍后配置」后从桌宠右上角「模型」补填。')
        tray.activated.connect(
            lambda reason: self._restore_from_tray()
            if reason == QSystemTrayIcon.DoubleClick else None)
        tray.show()
        self._tray = tray
        return True

    def _restore_from_tray(self):
        if self._tray is not None:
            self._tray.hide()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        if self._allow_start:
            super().closeEvent(event)
            return
        box = QMessageBox(self)
        box.setWindowTitle('还没保存模型配置')
        box.setIcon(QMessageBox.Question)
        box.setText('现在关闭，咕嘎的对话、完整对话和截图分析都没有模型可用。')
        box.setInformativeText(
            '稍后配置：先进桌宠，只保留本地功能；随时点右上角「模型」补填。\n'
            '最小化到托盘：把向导收进托盘，双击图标再打开。\n'
            '退出咕嘎：结束程序；下次双击 GUGA-Desktop.exe 会重新进入本向导。')
        later = box.addButton('稍后配置（先进桌宠）', QMessageBox.AcceptRole)
        tray_btn = box.addButton('最小化到托盘', QMessageBox.ActionRole)
        box.addButton('退出咕嘎', QMessageBox.RejectRole)
        box.setDefaultButton(later)
        box.exec_()
        clicked = box.clickedButton()
        if clicked is later:
            self._closing_choice = 'later'
            self._allow_start = True
            event.ignore()
            QTimer.singleShot(0, self.accept)
            return
        if clicked is tray_btn:
            self._closing_choice = 'tray'
            if self._ensure_tray():
                self.hide()
                event.ignore()
            return
        self._closing_choice = 'quit'
        self._detach_probes()
        event.accept()


class CloudAccountDialog(QDialog):
    """桌宠右键常驻入口：只配云端账号（派单/跨设备），不依赖是否已启用。

    与首启向导共用同一套字段与「测试登录」探针；不填也不影响本地任务。
    """

    def __init__(self, parent=None, on_saved=None):
        super().__init__(parent)
        self.setWindowTitle('咕嘎 · 云端账号（可选）')
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self._on_saved = on_saved
        self._probes = []
        self._build()
        self._load()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 12)
        title = QLabel('☁ 云端大脑（可选 · 不影响本地任务，只影响派单/跨设备）')
        title.setStyleSheet('font-size:14px; font-weight:bold; color:#1a1c20;')
        root.addWidget(title)
        tip = QLabel('不填也可以正常聊天、打开软件、截图分析；只有「派个任务 / 跨设备 / 云端台账」需要这里。')
        tip.setWordWrap(True)
        tip.setStyleSheet('color:#8a8f98;font-size:12px')
        root.addWidget(tip)

        group = QGroupBox('云端账号（购买云端服务后由服务商提供）')
        form = QGridLayout(group)
        self._base = QLineEdit()
        self._base.setPlaceholderText('例如 https://cloud.example.com（不填则没有云端能力）')
        form.addWidget(QLabel('服务地址'), 0, 0)
        form.addWidget(self._base, 0, 1, 1, 2)
        self._user = QLineEdit()
        self._user.setPlaceholderText('账号/用户名')
        form.addWidget(QLabel('账号'), 1, 0)
        form.addWidget(self._user, 1, 1)
        self._pass = QLineEdit()
        self._pass.setEchoMode(QLineEdit.Password)
        form.addWidget(QLabel('密码'), 2, 0)
        form.addWidget(self._pass, 2, 1)
        self._btn_test = QPushButton('测试登录')
        self._btn_test.clicked.connect(self._test_cloud)
        form.addWidget(self._btn_test, 3, 1)
        self._lbl = QLabel('未测试')
        self._lbl.setWordWrap(True)
        self._lbl.setStyleSheet('color:#8a8f98;')
        form.addWidget(self._lbl, 3, 2)
        root.addWidget(group)

        self._where = QLabel('')
        self._where.setWordWrap(True)
        self._where.setStyleSheet('color:#4a5568;font-size:11px')
        root.addWidget(self._where)
        root.addStretch(1)

        row = QHBoxLayout()
        clear_btn = QPushButton('清除云端账号')
        clear_btn.clicked.connect(self._clear)
        cancel_btn = QPushButton('取消')
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton('保存并重新登录')
        save_btn.setStyleSheet(
            'QPushButton{background:#2f7cf6;color:white;padding:7px 16px;'
            'border-radius:6px;font-weight:bold;}')
        save_btn.clicked.connect(self._save)
        row.addWidget(clear_btn)
        row.addStretch(1)
        row.addWidget(cancel_btn)
        row.addWidget(save_btn)
        root.addLayout(row)
        fit_dialog(self, 540, 400, min_width=420, min_height=300)

    def _load(self):
        cloud = (guga_paths.read_config() or {}).get('cloud') or {}
        self._base.setText(str(cloud.get('base_url') or ''))
        self._user.setText(str(cloud.get('username') or ''))
        self._pass.setText(str(cloud.get('password') or ''))
        if cloud:
            self._lbl.setText('已保存：%s（账号 %s）' % (
                cloud.get('base_url') or '', cloud.get('username') or ''))
        self._where.setText('保存位置：%s' % guga_paths.CONFIG)

    def _test_cloud(self):
        base, user, pwd = self._base.text().strip(), self._user.text().strip(), \
            self._pass.text()
        if not (base.startswith('http') and user and pwd):
            self._lbl.setText('地址/账号/密码没填全；不想启用就留空并点「清除云端账号」')
            self._lbl.setStyleSheet('color:#c25b00;')
            return
        self._lbl.setText('测试中…')
        self._lbl.setStyleSheet('color:#8a8f98;')
        self._btn_test.setEnabled(False)
        probe = _Probe('cloud', base, user, pwd)
        probe.done.connect(self._cloud_result)
        self._probes.append(probe)
        probe.start()

    def _cloud_result(self, ok, msg):
        try:
            self._btn_test.setEnabled(True)
            self._lbl.setText(('✅ ' if ok else '❌ ') + str(msg))
            self._lbl.setStyleSheet('color:%s;' % ('#1d7a3e' if ok else '#b3261e'))
        except Exception:
            pass

    def _save(self):
        base, user, pwd = self._base.text().strip(), self._user.text().strip(), \
            self._pass.text()
        if not (base or user or pwd):
            self._clear()
            return
        if not (base.startswith('http') and user and pwd):
            QMessageBox.warning(self, '云端账号未填全',
                                '服务地址、账号、密码三项要一起填；'
                                '不想启用就点「清除云端账号」。')
            return
        cfg = guga_paths.read_config()
        cfg['cloud'] = dict(base_url=base.rstrip('/'), username=user, password=pwd,
                            device=os.environ.get('COMPUTERNAME', '桌宠本机'),
                            poll_secs=6)
        if not self._write(cfg):
            return
        if self._on_saved:
            try:
                self._on_saved(cfg.get('cloud'))
            except Exception:
                pass
        self.accept()

    def _clear(self):
        cfg = guga_paths.read_config()
        cfg.pop('cloud', None)
        if not self._write(cfg):
            return
        if self._on_saved:
            try:
                self._on_saved(None)
            except Exception:
                pass
        self.accept()

    def _write(self, cfg):
        try:
            guga_paths.write_config(cfg)
            return True
        except Exception as exc:
            QMessageBox.warning(self, '云端账号未能写入磁盘',
                                '%s\n目标路径：%s' % (exc, guga_paths.CONFIG))
            return False

    def closeEvent(self, event):
        # 还没结束的登录探针交给模块级列表持有，避免 QThread 被回收导致进程 abort。
        for probe in list(self._probes):
            _LIVE_PROBES.append(probe)
            try:
                probe.done.disconnect()
            except Exception:
                pass
            try:
                probe.finished.connect(lambda p=probe: _release_probe(p))
            except Exception:
                pass
        self._probes = []
        super().closeEvent(event)


def maybe_adopt_legacy(parent=None):
    """安装目录存在旧配置时明确询问；默认“重新填写”，不写入任何 Key。

    这是 P0：旧配置里的 Key 属于“别处的凭据”，不能因为文件存在就默认归用户所有。
    只有用户点“沿用这份配置”才复制，并标记 key_source=legacy_inherited。
    """
    candidate = guga_paths.legacy_candidate()
    if not candidate:
        return ''
    if guga_paths.legacy_prompt_dismissed(candidate['path']):
        return ''
    from core.ai_chat import endpoint_label, provider_label
    cfg = candidate.get('config') or {}
    box = QMessageBox(parent)
    box.setWindowTitle('发现一份旧配置：是否沿用？')
    box.setIcon(QMessageBox.Question)
    box.setText('安装目录里有一份之前的配置，里面带着一份 API Key。'
                '默认不会沿用——买软件只需要你自己在本机填 Key。')
    box.setInformativeText(
        '来源：%s\n服务商：%s\n端点：%s\n模型：%s\nKey：%s（打码，只露后四位）\n\n'
        '选“重新填写”：不写入任何 Key，打开空白向导让你自己填。\n'
        '选“沿用这份配置”：把这份 Key 复制到本机用户目录，并标记为“从旧配置继承”。'
        % (candidate.get('path'), provider_label(cfg),
           endpoint_label(cfg) if candidate.get('base_url') else '未填写',
           cfg.get('model') or '未填写', guga_paths.mask_key(candidate.get('api_key'))))
    adopt_btn = box.addButton('沿用这份配置', QMessageBox.ActionRole)
    fresh_btn = box.addButton('重新填写（推荐）', QMessageBox.AcceptRole)
    box.setDefaultButton(fresh_btn)
    box.exec_()
    if box.clickedButton() is adopt_btn:
        result = guga_paths.adopt_legacy(candidate['path'])
        if not result.get('ok'):
            QMessageBox.warning(parent, '旧配置沿用失败', result.get('message') or '未知原因')
            return ''
        return 'adopted'
    guga_paths.dismiss_legacy(candidate['path'])
    return 'declined'


def ensure_config():
    """桌宠启动入口调用: 配置就绪返回 True; 否则弹向导, 用户保存后 True / 关闭后 False."""
    guga_paths.prepare()        # 建用户目录 + 迁移安装目录里的旧配置（失败只提示不阻塞）
    if config_ok():
        return True
    if QThread.currentThread() is None:      # 理论不可达, 防御
        return False
    wizard = FirstRunWizard()
    accepted = wizard.exec_() == QDialog.Accepted
    # 用户点了“仍要保存”就必须继续启动：弹窗已承诺“之后可重新打开向导修改”。
    # 旧实现无条件复查 config_ok()，空 Key 时返回 False → comp_main 直接 sys.exit(0)，
    # 在用户看来就是“按提示保存后咕嘎突然消失”（NOAI-07 / P2-NOAI-NOKEY-EXIT）。
    return bool(accepted) or config_ok()


def notify_incomplete(parent=None, on_configure=None):
    """配置不完整时进入桌宠后的可操作提示（不阻塞事件循环之外的其他窗口）。

    用户点「仍要保存」时我们承诺过“之后可以再改”，这里把入口直接指给「模型设置」，
    避免出现“程序消失了但没人告诉用户下一步”。
    """
    try:
        box = QMessageBox(parent)
        box.setWindowTitle('配置未完成')
        box.setIcon(QMessageBox.Information)
        box.setText('咕嘎已启动，但还没有配置模型，对话 / 完整对话 / 截图分析不可用，'
                    '当前只能用本地功能。')
        box.setInformativeText(
            '没有模型 Key 时，打开/关闭软件、打开网页、窗口切换、音量等本地指令仍可直接用；'
            '只有对话、任务拆解和截图理解需要模型 Key。\n'
            '接入 MCP 只让咕嘎知道软件装在哪，不等于模型可用；模型 Key 必须你自己在本机填。\n'
            '点「现在去配置模型」补填平台、端点、模型名和 Key；也可以稍后点桌宠右上角的红色'
            '「未配置模型」或右键「⚙️ 模型设置」。')
        now = box.addButton('现在去配置模型', QMessageBox.AcceptRole)
        box.addButton('先这样用', QMessageBox.RejectRole)
        box.exec_()
        if box.clickedButton() is now and callable(on_configure):
            on_configure()
    except Exception:
        pass
