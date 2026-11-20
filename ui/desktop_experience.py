"""Pet-side controls, local desktop reading and a catalogue of installed features."""
import random
import threading
import time

from PyQt5.QtCore import Qt, QEvent, QObject, QTimer, pyqtSignal, QBuffer, QIODevice
from PyQt5.QtGui import QCursor,QPainter,QColor,QPen
from PyQt5.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSpinBox,
    QVBoxLayout, QWidget, QFormLayout, QScrollArea)


class DraftEditor(QPlainTextEdit):
    submitted=pyqtSignal()
    def __init__(self):
        super().__init__();self.composing=False
    def inputMethodEvent(self,event):
        self.composing=bool(event.preeditString());super().inputMethodEvent(event)
    def keyPressEvent(self,event):
        if event.key() in (Qt.Key_Return,Qt.Key_Enter) and not event.modifiers() & Qt.ShiftModifier and not self.composing:
            self.submitted.emit();event.accept();return
        super().keyPressEvent(event)


class QuickInput(QDialog):
    def __init__(self,experience):
        super().__init__(None,Qt.Tool|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.experience=experience;self.setWindowTitle('咕嘎 · 打字');self.resize(360,160)
        self.setStyleSheet('QDialog{background:transparent;border:0;} '
            'QLabel{color:#536575;font:12px "Microsoft YaHei UI";} QPlainTextEdit{border:0;background:#fffdf9;'
            'color:#293b49;font:14px "Microsoft YaHei UI";} QPushButton{border:0;border-radius:7px;'
            'padding:6px 12px;color:#334d5c;background:#e9f2f5;} QPushButton:hover{background:#d6e9ee;}')
        layout=QVBoxLayout(self);header=QHBoxLayout();header.addWidget(QLabel('和咕嘎说点什么'),1)
        close=QPushButton('×');close.clicked.connect(self.hide);header.addWidget(close);layout.addLayout(header)
        self.editor=DraftEditor();self.editor.setPlaceholderText('Enter 发送 · Shift+Enter 换行 · Esc 收起')
        self.editor.submitted.connect(self.submit);layout.addWidget(self.editor)
        row=QHBoxLayout();self.notice=QLabel('');row.addWidget(self.notice,1)
        voice=QPushButton('语音');voice.clicked.connect(experience.pet._open_voice_recording);row.addWidget(voice)
        send=QPushButton('发送 ↑');send.clicked.connect(self.submit);row.addWidget(send);layout.addLayout(row)
        for b in self.findChildren(QPushButton):b.setAutoDefault(False)

    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor('#cbd6e1'),1));p.setBrush(QColor('#fffdf9'))
        p.drawRoundedRect(1,1,self.width()-2,self.height()-2,14,14)

    def submit(self):
        text=self.editor.toPlainText().strip()
        if not text:return
        if len(text)>15000:
            self.notice.setText('单条消息最多 15000 字，请分段发送。');return
        panel=self.experience.pet._ensure_agent_panel()
        try:
            if panel.send(text,read_reply=True) is False:
                self.notice.setText(panel.status.text()[:70]);return
        except Exception as exc:
            self.notice.setText(str(exc)[:90]);return
        self.editor.clear();self.hide()


class DesktopExperience(QObject):
    ocr_ready=pyqtSignal(object)
    def __init__(self,pet):
        super().__init__(pet);self.pet=pet;self.settings=pet._config._s
        self.quick_input=None;self.ocr_busy=False
        self.latest_text='';self.latest_at=0.;self.ocr_views=[]
        self.roam_timer=QTimer(self);self.roam_timer.setSingleShot(True);self.roam_timer.timeout.connect(self._roam)
        self.observe_timer=QTimer(self);self.observe_timer.timeout.connect(lambda:self.read_desktop(False))
        self._ctrl_at=0.
        self.ocr_ready.connect(self._ocr_done);pet.installEventFilter(self);self.apply_settings()

    def apply_settings(self):
        self.roam_timer.stop();self.observe_timer.stop()
        if not hasattr(self.pet,'_walk_on_desktop') and self.settings.value('experience/walking',False,type=bool):self.roam_timer.start(25000)
        if self.settings.value('experience/desktop_observe',False,type=bool):
            self.observe_timer.start(max(15,self.settings.value('experience/observe_seconds',30,type=int))*1000)

    def stop(self):
        for timer in (self.roam_timer,self.observe_timer):timer.stop()
        if self.quick_input:self.quick_input.hide()

    def eventFilter(self,obj,event):
        if obj is self.pet and self.quick_input:
            if event.type() in (QEvent.Move,QEvent.Resize) and self.quick_input.isVisible():
                self._place(self.quick_input)
            elif event.type()==QEvent.Hide:self.quick_input.hide()
        return super().eventFilter(obj,event)

    def _place(self,widget):
        area=self.pet.screen().availableGeometry();pet=self.pet.frameGeometry()
        x=pet.center().x()-widget.width()//2
        y=pet.top()-widget.height()-8
        if y<area.top():y=pet.bottom()+8
        widget.move(max(area.left(),min(x,area.right()-widget.width()+1)),
                    max(area.top(),min(y,area.bottom()-widget.height()+1)))

    def show_input(self,text=''):
        bubble = getattr(self.pet, '_agent_bubble', None)
        if bubble is not None:
            if getattr(self.pet, '_desktop_hidden', False):
                self.pet._open_agent_chat()
                if isinstance(text, str) and text:
                    self.pet._ensure_agent_panel().editor.setPlainText(text)
            else:
                bubble.open_composer(text if isinstance(text, str) else '')
            return
        if not self.quick_input:self.quick_input=QuickInput(self)
        if isinstance(text,str) and text:self.quick_input.editor.setPlainText(text)
        self.quick_input.notice.setText('');self._place(self.quick_input)
        self.quick_input.show();self.quick_input.raise_();self.quick_input.activateWindow();self.quick_input.editor.setFocus()

    def key(self,vk):
        if not self.settings.value('experience/typing_shortcut',True,type=bool):return
        if vk in (0x11,0xA2):
            now=time.monotonic()
            # Low-level auto-repeat is ignored by KeyboardHook's edge filtering.
            if .08<now-self._ctrl_at<.45:self._ctrl_at=0.;self.show_input()
            else:self._ctrl_at=now
        else:self._ctrl_at=0.

    def walk_once(self):
        panel=getattr(self.pet,'_agent_panel',None)
        if (getattr(self.pet,'_is_drag',False) or self.pet._tts.is_speaking or
                (panel and (panel.busy or panel.voice_active)) or self.pet._anim2d.dancing):return False
        if hasattr(self.pet,'_walk_on_desktop'):return self.pet._walk_on_desktop()
        direction=-1 if self.pet.x()>self.pet.screen().availableGeometry().center().x() else 1
        return self.pet._pet_physics.walk(direction*random.randint(85,155),seconds=3.,lead=.12,tail=.15)

    def _roam(self):
        if self.settings.value('experience/walking',False,type=bool):
            self.walk_once();self.roam_timer.start(random.randint(24000,45000))

    def open_settings(self,name):
        self.pet._open_desktop_console()
        console=self.pet._desktop_console
        for i in range(console.tabs.count()):
            if console.tabs.tabText(i)==name:console.tabs.setCurrentIndex(i);break

    def read_desktop(self,show_result=True):
        if self.ocr_busy:return False
        screen=QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        self.ocr_busy=True
        surfaces=[self.quick_input,getattr(self.pet,'_agent_bubble',None),getattr(self.pet,'_desktop_console',None)]
        restore=[surface for surface in surfaces if surface is not None and surface.isVisible()]
        for surface in restore:surface.hide()
        # Let our own overlays disappear before taking one screenshot.
        QTimer.singleShot(90,lambda:self._capture_desktop(screen,show_result,restore))
        return True

    def _capture_desktop(self,screen,show_result,restore):
        try:pixmap=screen.grabWindow(0) if screen else None
        except Exception:
            self._ocr_done(dict(ok=False,message='未能取得当前屏幕画面。'));return
        finally:
            for surface in restore:surface.show()
        if show_result:self.open_settings('桌面识别')
        if pixmap is None or pixmap.isNull():
            self._ocr_done(dict(ok=False,message='未能取得当前屏幕画面。'));return False
        self._notify_ocr('正在本机识别桌面文字…')
        buffer=QBuffer();buffer.open(QIODevice.WriteOnly);pixmap.save(buffer,'PNG')
        png=bytes(buffer.data());buffer.close()
        def work():
            try:
                from core.desktop_ocr_native import recognize
                result=recognize(png,timeout=15)
                message=str((result.get('data') or {}).get('text') or '')
                if not result.get('ok'):message=str(result.get('message') or result.get('kind') or '未识别到文字')
                self.ocr_ready.emit(dict(ok=bool(result.get('ok')),message=message))
            except Exception as exc:self.ocr_ready.emit(dict(ok=False,message=str(exc)[:240]))
        threading.Thread(target=work,daemon=True,name='guga-desktop-ocr').start()

    def _notify_ocr(self,text):
        for view in list(self.ocr_views):
            try:view.setPlainText(text)
            except RuntimeError:self.ocr_views.remove(view)

    def _ocr_done(self,result):
        self.ocr_busy=False
        if result.get('ok'):self.latest_text=result['message'];self.latest_at=time.monotonic()
        else:self.latest_text='';self.latest_at=0.
        self._notify_ocr(result.get('message',''))

    def ask_desktop(self):
        if not self.latest_text or time.monotonic()-self.latest_at>120:
            self._notify_ocr('识别结果已过期，请重新读取桌面。');return
        self.show_input('请解释当前桌面中的这些内容：\n<desktop_text>\n'+self.latest_text[:7000]+'\n</desktop_text>')


def add_experience_tabs(console,tabs,pet):
    """The pages save independently of model keys and perform no work on construction."""
    experience=pet._experience;s=pet._config._s
    page=QWidget();form=QFormLayout(page)
    shortcut=QCheckBox('双击左 Ctrl 打开打字框');shortcut.setChecked(s.value('experience/typing_shortcut',True,type=bool))
    draft=QCheckBox('语音识别后先放入打字框');draft.setChecked(s.value('experience/voice_draft',False,type=bool))
    for c in (shortcut,draft):form.addRow(c)
    behavior=QPushButton('打开动作与行为设置…')
    behavior.setEnabled(hasattr(pet,'_show_behavior_dialog'))
    behavior.clicked.connect(lambda:pet._show_behavior_dialog());form.addRow(behavior)
    save=QPushButton('保存桌宠习惯');form.addRow(save)
    def save_habits():
        for key,c in (('typing_shortcut',shortcut),('voice_draft',draft)):s.setValue('experience/'+key,c.isChecked())
        s.sync();experience.apply_settings();console.status.setText('桌宠习惯已保存，立即生效。')
    save.clicked.connect(save_habits);tabs.addTab(page,'桌宠习惯')

    page=QWidget();box=QVBoxLayout(page)
    note=QLabel('在本机读取鼠标所在屏幕的文字；点击“带入打字框”后可以向咕嘎提问。');note.setWordWrap(True);box.addWidget(note)
    row=QHBoxLayout();read=QPushButton('读取当前桌面');read.clicked.connect(lambda:experience.read_desktop());row.addWidget(read)
    ask=QPushButton('带入打字框');ask.clicked.connect(experience.ask_desktop);row.addWidget(ask);box.addLayout(row)
    result=QPlainTextEdit();result.setReadOnly(True);result.setPlaceholderText('识别结果显示在这里，关闭软件后清空。');box.addWidget(result,1);experience.ocr_views.append(result)
    observe=QCheckBox('定时更新本地桌面文字');observe.setChecked(s.value('experience/desktop_observe',False,type=bool));box.addWidget(observe)
    seconds=QSpinBox();seconds.setRange(15,120);seconds.setSuffix(' 秒');seconds.setValue(s.value('experience/observe_seconds',30,type=int));box.addWidget(seconds)
    apply=QPushButton('保存桌面识别设置');box.addWidget(apply)
    def save_observation():
        s.setValue('experience/desktop_observe',observe.isChecked());s.setValue('experience/observe_seconds',seconds.value());s.sync();experience.apply_settings()
        console.status.setText('桌面识别设置已保存。定时识别仅在本机处理。')
    apply.clicked.connect(save_observation);tabs.addTab(page,'桌面识别')

    from core.companion_policy import read_policy
    policy=read_policy(s);page=QWidget();form=QFormLayout(page)
    enabled=QCheckBox('按分工主动调用智能体处理我的任务');enabled.setChecked(policy['enabled']);form.addRow(enabled)
    executor=QComboBox()
    for label,value in (('咕嘎 · 自己处理','self'),('Codex · OpenAI 帽子','codex'),('DSH · 鲸鱼帽子','dsh')):executor.addItem(label,value)
    executor.setCurrentIndex(max(0,executor.findData(policy['executor'])));form.addRow('执行方式',executor)
    reviewer=QComboBox();reviewer.addItem('Codex · 审计、架构与测试','codex');form.addRow('审计智能体',reviewer)
    auto=QCheckBox('DSH 结果使用独立 Codex 对话验收（会额外创建验收对话）');auto.setChecked(policy['auto_review']);form.addRow(auto)
    note=QLabel('默认由咕嘎聊天并处理任务。选择 Codex 或 DSH 后，只在执行任务时连接；追加要求继续使用原对话。\n右键“管家与员工”也能切换工作形态。');note.setWordWrap(True);form.addRow(note)
    button=QPushButton('保存智能体分工');form.addRow(button)
    def save_policy():
        for key,value in dict(enabled=enabled.isChecked(),executor=executor.currentData(),reviewer=reviewer.currentData(),auto_review=auto.isChecked()).items():s.setValue('team/'+key,value)
        pet._set_executor_mode(executor.currentData())
        s.sync();console.status.setText('智能体分工已保存，用于后续任务。')
    button.clicked.connect(save_policy);tabs.addTab(page,'智能体分工')

    from ui.plugin_market import PluginMarketPage
    console.plugin_market = PluginMarketPage(pet, experience)
    tabs.addTab(console.plugin_market, '插件市场')
