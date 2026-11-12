"""Visible driver/speech controls; Edge TTS requires no separate credential."""
import threading
from PyQt5.QtCore import Qt,pyqtSignal
from PyQt5.QtWidgets import (QDialog,QVBoxLayout,QFormLayout,QTabWidget,QWidget,
    QLabel,QLineEdit,QComboBox,QPushButton,QHBoxLayout)
from core.model_settings import read_raw,resolve_config,save_settings,test_driver


class DesktopConsole(QDialog):
    checked=pyqtSignal(bool,str)
    def __init__(self,pet):
        super().__init__(None,Qt.Window|Qt.WindowStaysOnTopHint)
        self.pet=pet;self.setWindowTitle('咕嘎 · 桌面管理台');self.resize(580,500)
        self.raw=read_raw()
        try:self.cfg=resolve_config(self.raw)
        except Exception:self.cfg=dict(self.raw)
        self.setStyleSheet('QDialog{background:#fff;} QLabel,QLineEdit,QComboBox,QPushButton{font:12px "Microsoft YaHei UI";} QLineEdit,QComboBox{padding:7px;border:1px solid #dce3ef;border-radius:6px;} QPushButton{padding:8px 14px;}')
        root=QVBoxLayout(self);tabs=QTabWidget();root.addWidget(tabs)
        driver=QWidget();form=QFormLayout(driver)
        self.source=QComboBox();self.source.addItem('复用本机 DSH 模型配置（无需打开 DSH）','dsh');self.source.addItem('自定义大模型服务','custom')
        self.source.setCurrentIndex(0 if self.raw.get('driver_source')=='dsh' else 1)
        form.addRow('大模型（驱动层）',self.source)
        self.base=QLineEdit(str(self.cfg.get('base_url','')));form.addRow('服务地址',self.base)
        self.model=QLineEdit(str(self.cfg.get('model','')));form.addRow('模型名称',self.model)
        self.key=QLineEdit();self.key.setEchoMode(QLineEdit.Password);form.addRow('API 密钥',self.key)
        note=QLabel('负责理解、问答和调度任务；与朗读、实时语音分开设置。\n保存后用于后续请求，当前任务不会被取消。');note.setWordWrap(True);form.addRow(note)
        self.test=QPushButton('测试驱动模型连接');self.test.clicked.connect(self.check_driver);form.addRow(self.test)
        tabs.addTab(driver,'大模型（驱动层）')
        speech=QWidget();speech_form=QFormLayout(speech)
        self.tts=QComboBox();self.tts.addItem('Edge TTS（默认，无需密钥）','edge');self.tts.addItem('豆包语音（需已有密钥）','doubao');self.tts.addItem('千问语音 qwen3-tts-flash（需 API Key）','qianwen')
        self.tts.setCurrentIndex(max(0,self.tts.findData(self.raw.get('tts_provider','edge'))))
        speech_form.addRow('语音输出',self.tts)
        self.voice=QLineEdit(self.raw.get('edge_voice','zh-CN-XiaoyiNeural'));speech_form.addRow('Edge 音色',self.voice)
        self.rate=QLineEdit(self.raw.get('edge_rate','+15%'));speech_form.addRow('语速',self.rate)
        self.pitch=QLineEdit(self.raw.get('edge_pitch','+14Hz'));speech_form.addRow('音调',self.pitch)
        qwen=dict(self.raw.get('qwen_speech') or {})
        self.qwen_key=QLineEdit();self.qwen_key.setEchoMode(QLineEdit.Password)
        self.qwen_key.setPlaceholderText('已保存，留空不修改' if qwen.get('api_key') else '可沿用实时语音里的千问密钥')
        speech_form.addRow('千问 API Key',self.qwen_key)
        self.qwen_voice=QLineEdit(qwen.get('tts_voice') or 'Cherry');speech_form.addRow('千问音色',self.qwen_voice)
        speech_form.addRow(QLabel('Edge 无需密钥；千问音色见官方音色表（Cherry / Serena / Ethan 等）。\n手动录音默认走千问识别，没配密钥时自动用本地 Vosk。'))
        preview=QPushButton('保存并试听');preview.clicked.connect(self.preview);speech_form.addRow(preview)
        tabs.addTab(speech,'语音输出')
        realtime=QWidget();rt=QVBoxLayout(realtime)
        label=QLabel('长语音模型是可选通道。\n开启后保留连接；手动录音优先，录音结束后恢复。\n额度耗尽或连接失败时，手动录音继续走驱动模型 + Edge TTS。')
        label.setWordWrap(True);rt.addWidget(label)
        settings=QPushButton('设置长语音模型');settings.clicked.connect(lambda:pet._ensure_agent_panel().open_voice_settings());rt.addWidget(settings);rt.addStretch()
        tabs.addTab(realtime,'长语音（可选）')
        self.status=QLabel('设置与连接测试分开；密钥不会显示在诊断信息中。');self.status.setWordWrap(True);root.addWidget(self.status)
        row=QHBoxLayout();row.addStretch();save=QPushButton('保存设置');save.clicked.connect(self.save);row.addWidget(save)
        close=QPushButton('关闭');close.clicked.connect(self.close);row.addWidget(close);root.addLayout(row)
        for b in self.findChildren(QPushButton):b.setAutoDefault(False)
        self.checked.connect(self.check_done);self.source.currentIndexChanged.connect(self.source_changed);self.source_changed()

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
            edge_rate=self.rate.text().strip() or '+15%',edge_pitch=self.pitch.text().strip() or '+14Hz')
        qwen=dict(self.raw.get('qwen_speech') or {})
        if self.qwen_key.text().strip():qwen['api_key']=self.qwen_key.text().strip()
        qwen['tts_voice']=self.qwen_voice.text().strip() or 'Cherry'
        qwen.setdefault('enabled',True)
        values['qwen_speech']=qwen
        if source=='dsh':values.update(api_key='',api_key_ref='')
        else:
            values['api_key_ref']=''
            values['api_key']=self.key.text().strip() or (self.raw.get('api_key','') if self.raw.get('driver_source')!='dsh' else '')
        import re
        if not re.fullmatch(r'[+-]\d+%',values['edge_rate']):raise ValueError('语速请使用 +15% 或 -10% 这样的格式')
        if not re.fullmatch(r'[+-]\d+Hz',values['edge_pitch']):raise ValueError('音调请使用 +14Hz 或 -5Hz 这样的格式')
        if values['tts_provider']=='doubao' and not self.raw.get('doubao_tts_api_key'):raise ValueError('尚未配置豆包语音密钥；Edge TTS 无需此密钥')
        if values['tts_provider']=='qianwen':
            from core.qwen_speech import speech_settings
            if not speech_settings(dict(self.raw,qwen_speech=qwen)).get('api_key'):
                raise ValueError('千问语音需要 API Key：填在上面的输入框，或先在长语音里配好千问密钥')
        return values

    def save(self):
        try:
            self.cfg=save_settings(self.values());self.raw=read_raw();self.key.clear()
            self.pet._tts.reload_settings()
            self.pet._ai._cfg=dict(self.cfg)
            panel=getattr(self.pet,'_agent_panel',None)
            if panel:panel.agent.config=dict(self.cfg)
            self.status.setText('已保存 · 驱动模型：'+self.cfg.get('model','')+' · 朗读：'+self.tts.currentText())
            return True
        except Exception as exc:self.status.setText(str(exc));return False

    def preview(self):
        if self.save():self.pet._tts.speak('咕嘎，语音输出已经准备好了。')

    def check_driver(self):
        try:cfg=dict(self.raw,**self.values())
        except Exception as exc:self.status.setText(str(exc));return
        self.test.setEnabled(False);self.status.setText('正在测试驱动模型…')
        def work():
            try:self.checked.emit(True,test_driver(cfg))
            except Exception as exc:self.checked.emit(False,str(exc) if not hasattr(exc,'request') else '驱动模型连接未完成，请检查地址和网络')
        threading.Thread(target=work,daemon=True,name='guga-driver-check').start()

    def check_done(self,ok,message):
        self.test.setEnabled(True);self.status.setText(message)
