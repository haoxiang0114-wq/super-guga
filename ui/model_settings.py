# -*- coding: utf-8 -*-
"""模型设置面板：改 base_url / 模型 / Key / 超时，测试连接与视觉，保存即生效。

独立文件，挂载点只做最小接线：右键菜单、完整对话窗口的「···」菜单或标题栏按钮。
Key 只写入本机 ai_config.json（已被 .gitignore 排除），界面不回显、不写日志。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from PyQt5.QtCore import QObject, QThread, pyqtSignal, pyqtSlot
from PyQt5.QtWidgets import (QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel,
                             QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
                             QVBoxLayout)

from core import model_capability as mc
from core import guga_paths

# 配置必须落在用户可写目录：解压到 Program Files 时安装目录不可写。

PRESETS = (
    ('千问 MaaS 网关（qwen3.8-flash）', 'https://maas.qianwenaiapi.com/compatible-mode/v1', 'qwen3.8-flash'),
    ('智谱 GLM（glm-4-flash）', 'https://open.bigmodel.cn/api/paas/v4', 'glm-4-flash'),
    ('DeepSeek（deepseek-chat）', 'https://api.deepseek.com/v1', 'deepseek-chat'),
    ('自定义', '', ''),
)

_open_dialog = None


def _mask(key):
    key = str(key or '')
    return (key[:6] + '…' + key[-6:]) if len(key) > 14 else ('未填写' if not key else '已填写')


class _Worker(QThread):
    finished_ok = pyqtSignal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.finished_ok.emit(self.fn())
        except Exception as exc:
            self.finished_ok.emit(dict(error='%s: %s' % (type(exc).__name__, str(exc)[:120])))


_diag_workers = set()


class _DiagnosticWorker(QThread):
    done = pyqtSignal(object)

    def __init__(self):
        super().__init__(None)

    def run(self):
        try:
            from core.diagnostic_export import export_diagnostics
            result = export_diagnostics()
        except Exception as exc:
            result = dict(ok=False, error='%s: %s' % (type(exc).__name__, str(exc)[:200]))
        finally:
            _diag_workers.discard(self)
        self.done.emit(result)


class _DiagnosticReceiver(QObject):
    """把后台线程结果拉回主线程再弹框；lambda 直连在部分 PyQt 版本会留在工作线程。"""

    def __init__(self, parent, worker):
        super().__init__(parent)
        self._window = parent
        self._worker = worker

    @pyqtSlot(object)
    def on_done(self, result):
        try:
            _show_diagnostic_result(self._window, result)
        finally:
            _diag_workers.discard(self._worker)
            self.deleteLater()


def run_diagnostic_export(parent=None):
    """右键菜单与设置页共用：后台打包，完成后弹路径/大小并可打开所在文件夹。"""
    if any(worker.isRunning() for worker in list(_diag_workers)):
        QMessageBox.information(parent, '导出诊断', '诊断包正在打包，请稍候…')
        return None
    worker = _DiagnosticWorker()
    receiver = _DiagnosticReceiver(parent, worker)
    worker._receiver = receiver          # 防止接收器被提前回收
    _diag_workers.add(worker)
    worker.done.connect(receiver.on_done)
    worker.start()
    return worker


def _show_diagnostic_result(parent, result):
    try:
        box = QMessageBox(parent)
    except Exception:
        box = QMessageBox()
    if not isinstance(result, dict) or not result.get('ok'):
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle('导出诊断失败')
        box.setText('诊断包没有导出成功')
        box.setInformativeText(str((result or {}).get('error') or '未知错误'))
        box.exec_()
        return
    path = str(result.get('zip_path') or '')
    size = int(result.get('size') or 0)
    size_text = ('%.2f MB' % (size / (1024 * 1024))) if size >= 1024 * 1024 \
        else ('%.1f KB' % (size / 1024.0))
    box.setIcon(QMessageBox.Information)
    box.setWindowTitle('诊断包已导出')
    box.setText('诊断包已生成，可以直接发给我们')
    box.setInformativeText('已保存到：\n%s\n\n大小：%s（%d 字节）\n文件数：%s'
                           % (path, size_text, size, result.get('entries') or 0))
    open_btn = box.addButton('打开所在文件夹', QMessageBox.ActionRole)
    box.addButton('关闭', QMessageBox.RejectRole)
    box.exec_()
    if box.clickedButton() is open_btn and path:
        try:
            os.startfile(str(Path(path).parent))
        except Exception as exc:
            QMessageBox.warning(parent, '打开文件夹失败', str(exc)[:200])


class ModelSettingsDialog(QDialog):
    def __init__(self, parent=None, on_saved=None):
        super().__init__(parent)
        self.setWindowTitle('咕嘎 · 模型设置')
        self.setMinimumWidth(620)
        self._on_saved = on_saved
        self._worker = None
        self._tier = None
        self._cfg = self._read_config()
        self._build()
        self._load(self._cfg)
        self._refresh_tier()

    # ------------------------------------------------------------ 配置文件
    @staticmethod
    def _read_config():
        return guga_paths.read_config()

    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)

        self.tier_label = QLabel('当前档位：读取中…')
        self.tier_label.setWordWrap(True)
        self.tier_label.setStyleSheet('QLabel{background:#f3f5f9;border-radius:8px;padding:10px;color:#2b3a55}')
        root.addWidget(self.tier_label)

        # F-05：当前到底在用哪个服务商/端点必须看得见；没配就明说，不能默默发往默认地址。
        self.effective_label = QLabel('')
        self.effective_label.setWordWrap(True)
        self.effective_label.setStyleSheet('QLabel{color:#6b7280;font-size:12px}')
        root.addWidget(self.effective_label)

        form = QFormLayout()
        form.setSpacing(8)
        self.preset = QComboBox()
        self.preset.addItems([item[0] for item in PRESETS])
        self.preset.currentIndexChanged.connect(self._apply_preset)
        form.addRow('常用预设', self.preset)

        self.base_url = QLineEdit()
        self.base_url.setPlaceholderText('https://example.com/v1')
        form.addRow('Base URL', self.base_url)

        self.model = QLineEdit()
        self.model.setPlaceholderText('模型 id，例如 qwen3.8-flash')
        form.addRow('模型名', self.model)

        key_row = QHBoxLayout()
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.setPlaceholderText('sk-...')
        self.show_key = QPushButton('显示')
        self.show_key.setCheckable(True)
        self.show_key.setFixedWidth(56)
        self.show_key.toggled.connect(
            lambda on: (self.api_key.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password),
                        self.show_key.setText('隐藏' if on else '显示')))
        key_row.addWidget(self.api_key, 1)
        key_row.addWidget(self.show_key)
        form.addRow('API Key', key_row)

        self.timeout = QSpinBox()
        self.timeout.setRange(5, 300)
        self.timeout.setSuffix(' 秒')
        form.addRow('请求超时', self.timeout)

        # 视觉模型可以单独指定：主模型不是多模态时，截图理解走这里；
        # 留空则跟随上面的主模型（主模型本身支持视觉时无需重复填写）。
        vision_title = QLabel('视觉模型（可选，留空则跟随主模型）')
        vision_title.setStyleSheet('QLabel{color:#2b3a55;font-weight:600;margin-top:6px}')
        form.addRow(vision_title)
        self.vision_base_url = QLineEdit()
        self.vision_base_url.setPlaceholderText('留空 = 跟随主模型；例如 https://maas.example.com/compatible-mode/v1')
        form.addRow('视觉 Base URL', self.vision_base_url)
        self.vision_model = QLineEdit()
        self.vision_model.setPlaceholderText('留空 = 跟随主模型；例如 qwen3-vl-plus')
        form.addRow('视觉模型名', self.vision_model)
        self.vision_api_key = QLineEdit()
        self.vision_api_key.setEchoMode(QLineEdit.Password)
        self.vision_api_key.setPlaceholderText('留空 = 沿用主模型 API Key')
        form.addRow('视觉 API Key', self.vision_api_key)
        root.addLayout(form)

        actions = QHBoxLayout()
        self.test_button = QPushButton('测试连接（真实请求）')
        self.test_button.clicked.connect(self._test)
        self.verify_button = QPushButton('验证 Key（直连/系统代理）')
        self.verify_button.setToolTip('各发一次真实请求，只看模式、状态码、耗时与打码后的响应片段')
        self.verify_button.clicked.connect(self._verify_key)
        self.probe_button = QPushButton('重新探测档位')
        self.probe_button.clicked.connect(self._probe_tier)
        self.save_button = QPushButton('保存并生效')
        self.save_button.setDefault(True)
        self.save_button.setStyleSheet('QPushButton{background:#476bec;color:white;border-radius:8px;padding:8px 18px}')
        self.save_button.clicked.connect(self._save)
        actions.addWidget(self.test_button)
        actions.addWidget(self.verify_button)
        actions.addWidget(self.probe_button)
        actions.addStretch(1)
        actions.addWidget(self.save_button)
        root.addLayout(actions)

        self.result = QPlainTextEdit()
        self.result.setReadOnly(True)
        self.result.setMinimumHeight(150)
        self.result.setPlaceholderText('测试结果会显示：HTTP 状态码、耗时、回复是否正常、图片是否被接受并识别。')
        root.addWidget(self.result)

        note = QLabel('Key 只保存在本机 ai_config.json（已被 .gitignore 排除，不会随包发布）。'
                      '保存后立刻生效，不需要重启。')
        note.setWordWrap(True)
        note.setStyleSheet('QLabel{color:#6b7280;font-size:12px}')
        root.addWidget(note)

        diag_row = QHBoxLayout()
        self.diagnostic_button = QPushButton('一键导出诊断')
        self.diagnostic_button.setToolTip(
            '导出自检结果、最近 30 分钟日志、最近一次任务全时段日志与 result.json、任务台账与证据清单')
        self.diagnostic_button.clicked.connect(lambda: run_diagnostic_export(self))
        diag_row.addWidget(self.diagnostic_button)
        diag_row.addStretch(1)
        root.addLayout(diag_row)
        diag_note = QLabel('会打包自检结果、最近 30 分钟日志，并强制带上最近一次任务时段'
                           '（含失败时段）的日志与 result.json，不含 API Key；'
                           '导出完成后把桌面上的 zip 直接发给我们即可。')
        diag_note.setWordWrap(True)
        diag_note.setStyleSheet('QLabel{color:#6b7280;font-size:12px}')
        root.addWidget(diag_note)

    # ------------------------------------------------------------ 读取/写回
    def _load(self, cfg):
        self._show_effective(cfg)
        self.base_url.setText(str(cfg.get('base_url') or ''))
        self.model.setText(str(cfg.get('model') or ''))
        self.api_key.setText(str(cfg.get('api_key') or ''))
        self.vision_base_url.setText(str(cfg.get('vision_base_url') or ''))
        self.vision_model.setText(str(cfg.get('vision_model') or ''))
        self.vision_api_key.setText(str(cfg.get('vision_api_key') or ''))
        self.timeout.setValue(int(cfg.get('request_timeout') or 60))
        self.preset.setCurrentIndex(self._preset_index())

    def _show_effective(self, cfg):
        from core.ai_chat import endpoint_label, model_config_error, provider_label
        problem = model_config_error(cfg)
        if problem:
            self.effective_label.setText('当前生效：未配置模型 · %s' % problem)
            self.effective_label.setStyleSheet('QLabel{color:#c62828;font-size:12px;font-weight:600}')
        else:
            self.effective_label.setText(
                '当前生效：%s · 端点 %s · 模型 %s · Key 来源 %s（%s）' % (
                    provider_label(cfg), endpoint_label(cfg), cfg.get('model'),
                    guga_paths.key_source(cfg), guga_paths.mask_key(cfg.get('api_key'))))
            self.effective_label.setStyleSheet('QLabel{color:#2e7d32;font-size:12px}')
        try:
            from core.screen_visual import vision_ready
            vr = vision_ready(cfg)
            route_text = {'dedicated': '单独配置的视觉模型', 'zhipu': '智谱视觉（旧配置）',
                          'chat': '跟随主模型', 'none': '未配置'}.get(vr.get('route'), vr.get('route'))
            self.effective_label.setText(self.effective_label.text() +
                                         '\n视觉：%s · %s' % (route_text, vr.get('model') or '—'))
        except Exception:
            pass

    def _preset_index(self):
        for index, (_name, url, model) in enumerate(PRESETS):
            if url and url == self.base_url.text().strip() and model == self.model.text().strip():
                return index
        return len(PRESETS) - 1

    def _apply_preset(self, index):
        if index >= len(PRESETS) - 1:
            return
        _name, url, model = PRESETS[index]
        self.base_url.setText(url)
        self.model.setText(model)

    def _form_cfg(self):
        return dict(self._cfg, base_url=self.base_url.text().strip(),
                    model=self.model.text().strip(), api_key=self.api_key.text().strip(),
                    vision_base_url=self.vision_base_url.text().strip(),
                    vision_model=self.vision_model.text().strip(),
                    vision_api_key=self.vision_api_key.text().strip(),
                    request_timeout=int(self.timeout.value()))

    @staticmethod
    def _vision_cfg(cfg):
        """测试视觉时优先用单独配置的视觉目标，没填才跟随主模型。"""
        out = dict(cfg)
        base = str(cfg.get('vision_base_url') or '').strip()
        model = str(cfg.get('vision_model') or '').strip()
        key = str(cfg.get('vision_api_key') or '').strip()
        if base:
            out['base_url'] = base
        if model:
            out['model'] = model
        if key:
            out['api_key'] = key
        return out

    # ------------------------------------------------------------ 档位
    def _refresh_tier(self, result=None, note=''):
        cfg = self._form_cfg()
        try:
            result = result or mc.assess(cfg, use_cache=True, probe_if_missing=False)
        except Exception as exc:
            self.tier_label.setText('当前档位：读取失败（%s）' % type(exc).__name__)
            return
        self._tier = result
        grades = result.get('evidence') or {}
        vision = grades.get('vision', ('unknown', ''))
        lines = ['当前档位：%s · %s' % (result.get('tier_name'), result.get('model') or '(未配置)'),
                 '视觉：%s（%s）' % (vision[0], vision[1][:70])]
        if result.get('missing'):
            short = '、'.join('%s[%s]' % (mc.SHORT_NAMES.get(x['capability'], x['capability']), x['grade'])
                             for x in result['missing'][:5])
            lines.append('缺项：' + short)
            lines.append('代价：' + result['missing'][0]['sacrifice'])
        prov = result.get('provenance') or {}
        lines.append('结论来源：%s · %s · %s（%s）' % (
            result.get('probe_source'), prov.get('host') or '本机', prov.get('at') or '时间未知',
            prov.get('probe') or '方式未记录'))
        if note:
            lines.append(note)
        self.tier_label.setText('\n'.join(lines))

    def _run(self, label, fn, done, button):
        if self._worker is not None and self._worker.isRunning():
            return
        button.setEnabled(False)
        self.result.setPlainText(label + '…')
        self._worker = _Worker(fn, self)
        self._worker.finished_ok.connect(lambda data: (button.setEnabled(True), done(data)))
        self._worker.start()

    # ------------------------------------------------------------ 测试连接
    def _test(self):
        cfg = self._form_cfg()
        if not cfg.get('api_key') or not str(cfg.get('base_url')).startswith('http'):
            self.result.setPlainText('请先填写 Base URL 和 API Key；没有 Base URL 时咕嘎不会向任何默认服务商发请求。')
            return
        if not str(cfg.get('model') or '').strip():
            self.result.setPlainText('请先填写模型名（模型 id），否则请求无法指定模型。')
            return
        from core import model_endpoint
        fixed = model_endpoint.normalize_base_url(cfg.get('base_url'))
        corrected = ''
        if fixed.get('ok') and fixed.get('changed'):
            cfg['base_url'] = fixed['base']
            corrected = '已自动校正地址：%s → %s\n\n' % (fixed.get('original'), fixed.get('base'))

        def work():
            from core import model_endpoint
            base = str(cfg['base_url']).rstrip('/')
            started = time.monotonic()
            try:
                response = model_endpoint.post(base + '/chat/completions', timeout=(4, int(cfg['request_timeout'])),
                                         headers={'Authorization': 'Bearer ' + cfg['api_key'],
                                                  'Content-Type': 'application/json'},
                                         json={'model': cfg['model'], 'max_tokens': 32, 'temperature': 0,
                                               'messages': [{'role': 'user', 'content': '回复“可用”两个字'}]})
                elapsed = round(time.monotonic() - started, 2)
                body = {}
                try:
                    body = response.json()
                except Exception:
                    pass
                text = ''
                if response.status_code == 200:
                    try:
                        text = (body['choices'][0]['message'].get('content') or '').strip()
                    except Exception:
                        text = ''
                error = ''
                if response.status_code != 200:
                    error = str((body.get('error') or {}).get('message') or '')[:100]
                chat = dict(status=response.status_code, elapsed=elapsed, reply=text[:60],
                            ok=bool(text), error=error)
            except Exception as exc:
                chat = dict(status=None, elapsed=round(time.monotonic() - started, 2), reply='',
                            ok=False, error='请求未完成：%s' % type(exc).__name__)
            vision = mc.vision_test(self._vision_cfg(cfg), budget=int(cfg['request_timeout']))
            return dict(chat=chat, vision=vision, model=cfg['model'], base_url=base)

        def done(data):
            if data.get('error'):
                self.result.setPlainText('测试失败：' + data['error'])
                return
            chat, vision = data['chat'], data['vision']
            lines = [corrected.rstrip(), '模型：%s' % data['model'], '端点：%s' % data['base_url'], '']
            if not corrected:
                lines = lines[1:]
            if chat['status'] is None:
                lines.append('文本请求：未完成（%s）' % chat['error'])
            else:
                lines.append('文本请求：HTTP %s，%s 秒，%s' % (
                    chat['status'], chat['elapsed'], ('正常，回复：' + chat['reply']) if chat['ok']
                    else ('异常' + ('：' + chat['error'] if chat['error'] else ''))))
            lines.append('视觉请求：HTTP %s，%s 秒' % (vision.get('status') or '未完成', vision.get('elapsed')))
            if vision.get('status') == 200:
                lines.append('  图片是否被接受：是；能否读出验证码：%s；回复：%s'
                             % ('是（识别到 7421）' if vision.get('recognized') else '否',
                                (vision.get('reply') or '空回复')[:40]))
            else:
                lines.append('  图片是否被接受：%s；原因：%s'
                             % ('是' if vision.get('accepted') else '未确认（不代表没有视觉）',
                                vision.get('error') or '未返回'))
            lines.append('  探测：%s @ %s（%s）' % (vision.get('host'), vision.get('at'), vision.get('method')))
            if chat['status'] in (400, 401, 403, 404, 405):
                lines.extend(['', '诊断：' + model_endpoint.text_of(
                    model_endpoint.http_failure(chat['status'], data['base_url'],
                                                model=data['model'], detail=chat['error']))])
            self.result.setPlainText('\n'.join(lines))

        self._run('正在测试连接', work, done, self.test_button)

    # ------------------------------------------------------------ 验证 Key
    def _verify_key(self):
        """只显示模式/状态码/耗时/打码片段；401/403 直接给三类下一步。"""
        from core import guga_paths
        from core import model_endpoint
        cfg = self._form_cfg()
        key, key_error = guga_paths.clean_api_key(cfg.get('api_key'))
        if key_error:
            self.result.setPlainText('API Key 不合法：%s' % key_error)
            return
        if not key:
            self.result.setPlainText('请先填写 API Key。')
            return
        cfg['api_key'] = key
        fixed = model_endpoint.normalize_base_url(cfg.get('base_url'))
        if fixed.get('ok') and fixed.get('changed'):
            cfg['base_url'] = fixed['base']
        if not str(cfg.get('model') or '').strip():
            self.result.setPlainText('请先填写模型名（模型 id）。')
            return

        def work():
            return model_endpoint.probe(cfg, timeout=int(cfg['request_timeout']))

        def done(data):
            if data.get('error'):
                self.result.setPlainText('验证未发出：' + str(data['error']) +
                                         '\n下一步：' + str(data.get('next_action') or ''))
                return
            fp = data.get('key_fingerprint') or {}
            lines = ['本次凭据：长度 %s · 前四 %s … 后四 %s · 来源 %s'
                     % (fp.get('length', 0), fp.get('head') or '—',
                        fp.get('tail') or '—', fp.get('source') or 'main'),
                     '端点：%s' % data.get('base_url_normalized'),
                     '模型：%s' % data.get('model'), '']
            for item in data.get('attempts') or []:
                mode = '直连' if item.get('mode') == 'direct' else '系统代理'
                if item.get('status') is None:
                    lines.append('%s：未完成（%s 秒）· %s' % (
                        mode, item.get('elapsed_s'), item.get('error') or '无响应'))
                else:
                    snippet = str(item.get('body') or '').replace('\n', ' ')[:120]
                    lines.append('%s：HTTP %s，%s 秒 · %s' % (
                        mode, item.get('status'), item.get('elapsed_s'),
                        snippet or '（空响应）'))
            diagnosis = data.get('diagnosis') or {}
            if diagnosis:
                lines.extend(['', '诊断：%s。%s' % (
                    diagnosis.get('title') or '模型请求失败',
                    diagnosis.get('next_step') or '')])
            lines.append('')
            lines.append('提示：把“长度/前四/后四/来源”和昨晚能通的配置对比；'
                         '三项都一样仍 401，就不是换 Key，而是权限或额度变化。')
            self.result.setPlainText('\n'.join(lines))

        self._run('正在验证 Key（直连 + 系统代理各一次真实请求）', work, done, self.verify_button)

    # ------------------------------------------------------------ 档位探测
    def _probe_tier(self):
        cfg = self._form_cfg()
        if not cfg.get('api_key'):
            self.result.setPlainText('请先填写 API Key。')
            return

        def work():
            return mc.probe(cfg, budget=90.0)

        def done(data):
            if data.get('error'):
                self.result.setPlainText('探测失败：' + data['error'])
                return
            try:
                mc._cache_write(cfg, data, probe='settings:7项真实请求')
            except Exception:
                pass
            self._refresh_tier(note='已重新实测七项能力，保存后新结论生效。')
            self.result.setPlainText('七项探测完成：' + json.dumps(
                {k: data[k] for k in ('quota', 'speed', 'tool_call', 'forced_tool', 'vision',
                                      'structured', 'recovery') if k in data},
                ensure_ascii=False, indent=1))

        self._run('正在真实探测七项能力（可能需要 1~2 分钟）', work, done, self.probe_button)

    # ------------------------------------------------------------ 保存
    def _save(self):
        cfg = self._form_cfg()
        if not str(cfg.get('base_url')).startswith('http') or not cfg.get('model'):
            QMessageBox.warning(self, '模型设置', 'Base URL 必须以 http 开头，且模型名不能为空。')
            return
        cleaned, key_error = guga_paths.clean_api_key(cfg.get('api_key'))
        if key_error:
            QMessageBox.warning(self, 'API Key 不合法', key_error)
            return
        cfg['api_key'] = cleaned
        try:
            merged = self._read_config()
            merged.update(base_url=cfg['base_url'], model=cfg['model'], api_key=cfg['api_key'],
                          request_timeout=int(cfg['request_timeout']))
            # 视觉专用配置：留空表示跟随主模型，必须删掉旧值而不是写空串。
            for key in ('vision_base_url', 'vision_model', 'vision_api_key'):
                value = str(cfg.get(key) or '').strip()
                if value:
                    merged[key] = value
                else:
                    merged.pop(key, None)
            if str(merged.get('api_key') or '').strip():
                merged['key_source'] = 'local_input'   # 用户在本机自己填的
            else:
                merged.pop('key_source', None)
            # 换了模型/端点后视觉结论必须重新确认，不沿用旧模型的缓存。
            if merged.get('supports_vision') is not None:
                merged['supports_vision'] = None
            guga_paths.write_config(merged)
        except guga_paths.ApiKeyError as exc:
            QMessageBox.warning(self, 'API Key 不合法', str(exc))
            return
        except Exception as exc:
            # 写不进磁盘也不能把人挡在门外：本次进程内生效，明确告知重启后可能丢失。
            merged = locals().get('merged') or cfg
            guga_paths.remember_config(merged)
            QMessageBox.warning(self, '模型设置',
                                '配置未能写入磁盘（%s）。\n本次已临时生效，重启后可能需要重新填写。\n'
                                '目标路径：%s' % (exc, guga_paths.CONFIG))
        mc.invalidate_cache()
        self._cfg = merged
        self._show_effective(merged)
        if self._on_saved:
            try:
                self._on_saved(merged)
            except Exception:
                pass
        self._refresh_tier(note='已保存并生效；自检缓存已清空，下次自检会按新模型重新确认。')
        self.result.setPlainText('已保存：%s / %s（Key %s）\n已立即生效，无需重启。'
                                 % (merged.get('model'), merged.get('base_url'), _mask(merged.get('api_key'))))

    def closeEvent(self, event):
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(500)
        super().closeEvent(event)


def open_model_settings(parent=None, on_saved=None):
    """右键菜单与完整对话窗共用的入口：同一个面板复用，避免重复弹窗。"""
    global _open_dialog
    if _open_dialog is not None:
        try:
            _open_dialog._on_saved = on_saved or _open_dialog._on_saved
            _open_dialog.show()
            _open_dialog.raise_()
            _open_dialog.activateWindow()
            return _open_dialog
        except Exception:
            _open_dialog = None
    _open_dialog = ModelSettingsDialog(parent, on_saved=on_saved)
    _open_dialog.show()
    return _open_dialog
