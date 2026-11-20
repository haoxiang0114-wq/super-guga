"""Model, voice and desktop settings for the companion."""
import threading
from PyQt5.QtCore import Qt,pyqtSignal
from PyQt5.QtWidgets import (QDialog,QVBoxLayout,QFormLayout,QTabWidget,QWidget,
    QLabel,QLineEdit,QComboBox,QPushButton,QHBoxLayout,QListWidget,QCheckBox)
from core.model_settings import read_raw,resolve_config,save_settings,test_driver,vision_config
from ui.companion_widgets import SETTINGS_STYLE


class DesktopConsole(QDialog):
    checked=pyqtSignal(bool,str)
    pymaster_done=pyqtSignal(bool,str)
    def __init__(self,pet):
        super().__init__(None,Qt.Window|Qt.WindowStaysOnTopHint)
        self.pet=pet;self.setWindowTitle('咕嘎 · 设置');self.resize(810,590)
        self.setMinimumSize(660,490)
        self.raw=read_raw()
        try:self.cfg=resolve_config(self.raw)
        except Exception:self.cfg=dict(self.raw)
        self.setStyleSheet(SETTINGS_STYLE + '''
            QDialog {background:#f0f5f8;color:#243844;font:13px "Microsoft YaHei UI";}
            QLabel#PageTitle {color:#203743;}
            QLabel#Muted {color:#546b79;}
            QListWidget#SettingsNav {background:#e7eff4;border:1px solid #d8e4eb;outline:0;}
            QListWidget#SettingsNav::item {padding:8px 9px;}
            QListWidget::item:selected {background:#d4e7ec;color:#1d5361;}
            QPushButton#Primary {background:#215b68;}
            QPushButton#Primary:hover {background:#174958;}
        ''')
        root=QVBoxLayout(self);root.setContentsMargins(18,15,18,13);root.setSpacing(11)
        root.addWidget(QLabel('设置',objectName='PageTitle'))
        body=QHBoxLayout();body.setSpacing(12);root.addLayout(body,1)
        nav=QListWidget(objectName='SettingsNav');nav.setFixedWidth(150)
        nav.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);body.addWidget(nav)
        tabs=QTabWidget();tabs.tabBar().hide();self.tabs=tabs;body.addWidget(tabs,1)
        driver=QWidget();form=QFormLayout(driver)
        self.source=QComboBox();self.source.addItem('复用本机 DSH 模型配置（无需打开 DSH）','dsh');self.source.addItem('自定义大模型服务','custom')
        self.source.setCurrentIndex(0 if self.raw.get('driver_source')=='dsh' else 1)
        form.addRow('聊天模型来源',self.source)
        self.base=QLineEdit(str(self.cfg.get('base_url','')));form.addRow('服务地址',self.base)
        self.model=QLineEdit(str(self.cfg.get('model','')));form.addRow('模型名称',self.model)
        self.key=QLineEdit();self.key.setEchoMode(QLineEdit.Password);form.addRow('API 密钥',self.key)
        note=QLabel('用于对话和任务调度。视觉模型、语音输出和长语音可分别设置。',objectName='Muted');note.setWordWrap(True);form.addRow(note)
        self.test=QPushButton('测试聊天模型');self.test.clicked.connect(self.check_driver);form.addRow(self.test)
        tabs.addTab(driver,'模型接入')
        self._build_vision_page(tabs)
        speech=QWidget();speech_form=QFormLayout(speech)
        self.tts=QComboBox();self.tts.addItem('Edge TTS（默认，无需密钥）','edge');self.tts.addItem('豆包语音（需已有密钥）','doubao')
        self.tts.setCurrentIndex(max(0,self.tts.findData(self.raw.get('tts_provider','edge'))))
        speech_form.addRow('语音输出',self.tts)
        self.voice=QLineEdit(self.raw.get('edge_voice','zh-CN-XiaoyiNeural'));speech_form.addRow('Edge 音色',self.voice)
        self.rate=QLineEdit(self.raw.get('edge_rate','+15%'));speech_form.addRow('语速',self.rate)
        self.pitch=QLineEdit(self.raw.get('edge_pitch','+14Hz'));speech_form.addRow('音调',self.pitch)
        speech_form.addRow(QLabel('不设置时直接使用 Edge TTS；手动录音使用本地识别。'))
        preview=QPushButton('保存并试听');preview.clicked.connect(self.preview);speech_form.addRow(preview)
        tabs.addTab(speech,'语音输出')
        realtime=QWidget();rt=QVBoxLayout(realtime)
        label=QLabel('长语音模型是可选通道。\n开启后保留连接；手动录音优先，录音结束后恢复。\n额度耗尽或连接失败时，手动录音继续走驱动模型 + Edge TTS。')
        label.setWordWrap(True);rt.addWidget(label)
        settings=QPushButton('设置长语音模型');settings.clicked.connect(lambda:pet._ensure_agent_panel().open_voice_settings());rt.addWidget(settings);rt.addStretch()
        tabs.addTab(realtime,'长语音（可选）')
        from ui.desktop_experience import add_experience_tabs
        add_experience_tabs(self,tabs,pet)
        # 学习助手（PyMaster）：教学站遥控的设置页，必须在下面生成左侧导航前加进去
        study=QWidget();study_form=QFormLayout(study)
        try:
            from core.pymaster_bridge import settings as pm_settings
            pm=pm_settings()
        except Exception:pm=dict(url='',speak_lookup=True,headless=False)
        self._pm_old=dict(url=pm.get('url',''),speak_lookup=bool(pm.get('speak_lookup',True)),headless=bool(pm.get('headless')))
        self.pm_url=QLineEdit(pm.get('url',''));self.pm_url.setPlaceholderText('教学站地址（换发布链接只改这里）')
        study_form.addRow('教学站',self.pm_url)
        self.pm_speak=QCheckBox('查词结果直接朗读');self.pm_speak.setChecked(self._pm_old['speak_lookup'])
        study_form.addRow('朗读',self.pm_speak)
        self.pm_headless=QCheckBox('后台打开浏览器（不弹窗口，看不到页面）');self.pm_headless.setChecked(self._pm_old['headless'])
        study_form.addRow('浏览器',self.pm_headless)
        pm_note=QLabel('咕嘎用自己的浏览器打开这个站点：说「打开第 3 章」「运行一下」，它就到页面上点；\n答疑前会先读页面上的代码、报错和知识点，不凭空猜。')
        pm_note.setWordWrap(True);study_form.addRow(pm_note)
        pm_row=QHBoxLayout();self.pm_open=QPushButton('打开学习助手');self.pm_open.clicked.connect(self.open_pymaster)
        self.pm_stop=QPushButton('关闭浏览器');self.pm_stop.clicked.connect(self.stop_pymaster)
        pm_row.addWidget(self.pm_open);pm_row.addWidget(self.pm_stop);pm_row.addStretch();study_form.addRow(pm_row)
        self.pm_status=QLabel('未启动');self.pm_status.setWordWrap(True);study_form.addRow(self.pm_status)
        tabs.addTab(study,'学习助手（PyMaster）')
        for index in range(tabs.count()):
            nav.addItem(tabs.tabText(index))
            page=tabs.widget(index)
            if page.layout():
                page.layout().setContentsMargins(16,16,16,12)
                page.layout().setSpacing(9)
        nav.currentRowChanged.connect(tabs.setCurrentIndex)
        tabs.currentChanged.connect(nav.setCurrentRow)
        nav.setCurrentRow(0)
        self.status=QLabel('',objectName='Muted');self.status.setWordWrap(True);root.addWidget(self.status)
        row=QHBoxLayout();row.addStretch()
        close=QPushButton('关闭');close.clicked.connect(self.close);row.addWidget(close)
        save=QPushButton('保存设置',objectName='Primary');save.clicked.connect(self.save);row.addWidget(save);root.addLayout(row)
        for b in self.findChildren(QPushButton):b.setAutoDefault(False)
        self.checked.connect(self.check_done);self.source.currentIndexChanged.connect(self.source_changed);self.source_changed()
        self.pymaster_done.connect(self.pymaster_report)
        self._show_vision_target()

    def select_tab(self,which):
        """按名字（或序号）切到某一页；桌宠菜单用它直接打开学习助手。"""
        try:
            if isinstance(which,str):
                for i in range(self.tabs.count()):
                    if which in self.tabs.tabText(i):which=i;break
            self.tabs.setCurrentIndex(int(which))
        except Exception:
            pass

    def _build_vision_page(self, tabs):
        page=QWidget();form=QFormLayout(page)
        desc=QLabel('右键「看屏幕」只截取一次鼠标所在屏幕，并将图片交给下面的视觉模型。',objectName='Muted')
        desc.setWordWrap(True);form.addRow(desc)
        self.vision_target=QLabel();self.vision_target.setWordWrap(True);form.addRow('当前模型',self.vision_target)
        self.vision_base=QLineEdit(str(self.raw.get('vision_base_url') or ''))
        self.vision_base.setPlaceholderText('留空使用默认服务地址');form.addRow('服务地址',self.vision_base)
        self.vision_model=QLineEdit(str(self.raw.get('vision_model') or ''))
        self.vision_model.setPlaceholderText('支持图片输入的模型名称');form.addRow('模型名称',self.vision_model)
        self.vision_key=QLineEdit(str(self.raw.get('vision_api_key') or ''))
        self.vision_key.setEchoMode(QLineEdit.Password)
        self.vision_key.setPlaceholderText('留空沿用相同服务的密钥');form.addRow('API 密钥',self.vision_key)
        self.vision_test=QPushButton('用测试图片验证')
        self.vision_test.clicked.connect(self.check_vision);form.addRow(self.vision_test)
        tabs.addTab(page,'看屏幕')

    def _show_vision_target(self):
        from urllib.parse import urlsplit
        target=vision_config(self.cfg)
        model=target.get('model') or '待配置'
        try:host=urlsplit(target.get('base_url') or '').hostname
        except ValueError:host=None
        self.vision_target.setText(model + ('  ·  '+host if host else ''))

    def source_changed(self):
        linked=self.source.currentData()=='dsh'
        self.base.setReadOnly(linked);self.model.setReadOnly(linked);self.key.setEnabled(not linked)
        self.key.setPlaceholderText('复用 DSH 已保存密钥，不显示' if linked else '留空保留当前自定义密钥')
        if linked:
            try:
                from core.model_settings import dsh_driver
                cfg=dsh_driver();self.base.setText(cfg['base_url']);self.model.setText(cfg['model'])
            except Exception as exc:self.status.setText(str(exc))

    def values(self):
        source=self.source.currentData()
        values=dict(driver_source=source,base_url=self.base.text().strip(),model=self.model.text().strip(),
            tts_provider=self.tts.currentData(),edge_voice=self.voice.text().strip() or 'zh-CN-XiaoyiNeural',
            edge_rate=self.rate.text().strip() or '+15%',edge_pitch=self.pitch.text().strip() or '+14Hz',
            vision_base_url=self.vision_base.text().strip(),vision_model=self.vision_model.text().strip(),
            vision_api_key=self.vision_key.text().strip())
        if values['vision_base_url']:
            from urllib.parse import urlsplit
            url=urlsplit(values['vision_base_url'])
            if url.scheme not in ('https','http') or not url.hostname or url.username or url.password:
                raise ValueError('视觉模型地址需为完整的 HTTP 或 HTTPS 地址')
        if source=='dsh':values.update(api_key='',api_key_ref='')
        else:
            values['api_key_ref']=''
            values['api_key']=self.key.text().strip() or (self.raw.get('api_key','') if self.raw.get('driver_source')!='dsh' else '')
        import re
        if not re.fullmatch(r'[+-]\d+%',values['edge_rate']):raise ValueError('语速请使用 +15% 或 -10% 这样的格式')
        if not re.fullmatch(r'[+-]\d+Hz',values['edge_pitch']):raise ValueError('音调请使用 +14Hz 或 -5Hz 这样的格式')
        if values['tts_provider']=='doubao' and not self.raw.get('doubao_tts_api_key'):raise ValueError('尚未配置豆包语音密钥；Edge TTS 无需此密钥')
        # 千问语音的密钥/音色由长语音设置页维护，这里原样透传，避免保存时被抹掉
        values['qwen_speech']=dict(self.raw.get('qwen_speech') or {})
        from core.pymaster_bridge import SITE as _PM_SITE
        pm_url=self.pm_url.text().strip() or _PM_SITE
        if not pm_url.startswith(('http://','https://')):raise ValueError('教学站地址请用 http:// 或 https:// 开头')
        values['pymaster']=dict(url=pm_url,speak_lookup=self.pm_speak.isChecked(),headless=self.pm_headless.isChecked())
        return values

    def save(self):
        try:
            new=self.values()
            self.cfg=save_settings(new);self.raw=read_raw();self.key.clear()
            self.pet._tts.reload_settings()
            self.pet._ai._cfg=dict(self.cfg)
            panel=getattr(self.pet,'_agent_panel',None)
            if panel:panel.agent.config=dict(self.cfg)
            pm=new.get('pymaster') or {}
            changed=any(pm.get(k)!=self._pm_old.get(k) for k in ('url','headless'))
            if changed:
                bridge=getattr(self.pet,'_pymaster_bridge',None)
                if bridge is not None:
                    try:bridge.run('stop')
                    except Exception:pass
                self.pm_status.setText('已保存，教学站浏览器已按新设置关闭，下次打开生效')
            self._pm_old=dict(url=pm.get('url',''),speak_lookup=bool(pm.get('speak_lookup')),headless=bool(pm.get('headless')))
            self._show_vision_target()
            self.status.setText('已保存 · 驱动模型：'+self.cfg.get('model','')+' · 朗读：'+self.tts.currentText())
            return True
        except Exception as exc:self.status.setText(str(exc));return False

    def preview(self):
        if self.save():self.pet._tts.speak('咕嘎，语音输出已经准备好了。')

    def check_driver(self):
        try:cfg=dict(self.raw,**self.values())
        except Exception as exc:self.status.setText(str(exc));return
        self._checking('正在测试聊天模型…')
        def work():
            try:self.checked.emit(True,test_driver(cfg))
            except Exception as exc:self.checked.emit(False,str(exc) if not hasattr(exc,'request') else '驱动模型连接未完成，请检查地址和网络')
        threading.Thread(target=work,daemon=True,name='guga-driver-check').start()

    def check_vision(self):
        try:target=vision_config(resolve_config(dict(self.raw,**self.values())))
        except Exception as exc:self.status.setText(str(exc));return
        if not target.get('api_key') or not target.get('model'):
            self.status.setText('请先填好支持图片输入的模型和密钥。');return
        self._checking('正在发送测试图片…')
        def work():
            try:
                from core.model_capability import vision_test
                result=vision_test(target,budget=18)
                ok=bool(result.get('recognized'))
                message=('视觉连接成功 · '+target['model']+' 已读出测试图片'
                         if ok else result.get('error') or '模型已接收图片，但未读出图中的内容')
                self.checked.emit(ok,message)
            except Exception as exc:self.checked.emit(False,'视觉测试未完成 · '+type(exc).__name__)
        threading.Thread(target=work,daemon=True,name='guga-vision-check').start()

    def _checking(self, message):
        self.test.setEnabled(False);self.vision_test.setEnabled(False);self.status.setText(message)

    def check_done(self,ok,message):
        self.test.setEnabled(True);self.vision_test.setEnabled(True);self.status.setText(message)

    # ── 学习助手（PyMaster）────────────────────────────
    def _pymaster_bridge(self):
        bridge=getattr(self.pet,'_pymaster_bridge',None)
        if bridge is None:
            from core.pymaster_bridge import PyMasterBridge
            bridge=PyMasterBridge();self.pet._pymaster_bridge=bridge
        return bridge

    def open_pymaster(self):
        if not self.save():return
        self.pm_open.setEnabled(False);self.pm_status.setText('正在打开教学站…')
        def work():
            try:
                res=self._pymaster_bridge().open()
                if not res.get('ok'):
                    self.pymaster_done.emit(False,str(res.get('error') or '没能打开教学站'));return
                head=str(res.get('title') or res.get('kind') or '已打开')[:40]
                kps=len(res.get('kps') or [])
                self.pymaster_done.emit(True,'已打开：'+head+('（%d 个知识点）'%kps if kps else ''))
            except Exception as exc:self.pymaster_done.emit(False,'打开失败：'+str(exc)[:160])
        threading.Thread(target=work,daemon=True,name='guga-pymaster-open').start()

    def stop_pymaster(self):
        bridge=getattr(self.pet,'_pymaster_bridge',None)
        if bridge is None:
            self.pm_status.setText('教学站浏览器没在运行');return
        try:bridge.run('stop')
        except Exception:pass
        self.pm_open.setEnabled(True);self.pm_status.setText('已关闭教学站浏览器')

    def pymaster_report(self,ok,text):
        self.pm_open.setEnabled(True);self.pm_status.setText(text)
