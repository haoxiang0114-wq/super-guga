"""Non-activating, clickable speech above the pet; does not own audio lifetime."""
from PyQt5.QtCore import Qt, QEvent, QTimer, pyqtSignal, QPointF
from PyQt5.QtGui import QFont, QPainter, QColor, QPolygonF, QPen, QKeySequence
from PyQt5.QtWidgets import QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QApplication,QLineEdit, QShortcut, QComboBox


class PetSpeechBubble(QFrame):
    clicked = pyqtSignal()
    end_call = pyqtSignal()
    share_screen = pyqtSignal(bool)
    talk_started = pyqtSignal()
    talk_stopped = pyqtSignal(bool)
    alignment_selected = pyqtSignal(str,int,str)

    def __init__(self, pet):
        super().__init__(pet, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        from ui.desktop_fonts import ensure_readable_fonts
        ensure_readable_fonts()
        self.pet = pet
        self.call_active = False
        self.speaking = False
        self._recording = False
        self._voice_state = 'idle'
        self._wave_index = 0
        self.conversation_id = None
        self.full_text = ''
        self._alignment_id=''
        self._hovered=False
        self._reveal_index=0
        self._preview=''
        self.reveal_timer=QTimer(self);self.reveal_timer.setInterval(24)
        self.reveal_timer.timeout.connect(self._reveal)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.PointingHandCursor)
        self.setFont(QFont('Microsoft YaHei UI', 10))
        self.setFixedWidth(360)
        self.setObjectName('SpeechBubble')
        self.setStyleSheet('''
            QFrame#BubbleCard { background:#fffdf9; border:1px solid #cbd6e1; border-radius:17px; }
            QLabel { color:#30334b; background:transparent; border:none; }
            QLabel#BubbleTitle { color:#70628d; font-size:12px; font-weight:600; }
            QLabel#BubbleHint { color:#9793a5; font-size:10px; }
            QPushButton { color:#927b84; background:transparent; border:none; padding:2px 5px; font-size:11px; }
            QPushButton:hover { color:#b6415c; background:#fbeef0; border-radius:6px; }
            QPlainTextEdit#BubbleEditor { color:#263749; background:#f2f5fa; border:1px solid #dce4f0;
                border-radius:9px; padding:7px; font:13px "Microsoft YaHei UI"; }
            QPlainTextEdit#BubbleEditor:focus { border-color:#7f9be8; }
            QPlainTextEdit#BubbleEditor[pulse="true"] { background:#e9efff; border-color:#6689e6; }
            QPushButton#BubbleSend, QPushButton#BubbleMic { background:#e9effa; color:#344e83; border-radius:8px;
                min-height:25px; min-width:44px; padding:0 7px; }
            QPushButton#BubbleSend { background:#4266cb; color:white; }
            QPushButton#BubbleSend:pressed, QPushButton#BubbleMic:pressed { background:#2d4fba; color:white; }
            QPushButton#BubbleSend:disabled { background:#e1e7f3; color:#6f7e98; }
            QPushButton#BubbleShare:checked { color:#185c45; background:#e3f4e9; border-radius:6px; }
            QFrame#TalkDock { background:#f3f6ff; border:1px solid #dce5ff; border-radius:19px; }
            QFrame#TalkDock[recording="true"] { background:#5b86ff; border:1px solid #4b79f5; }
            QLabel#TalkWave { color:#7692d8; font-size:15px; letter-spacing:2px; }
            QFrame#TalkDock[recording="true"] QLabel#TalkWave { color:#ffffff; }
            QLabel#TalkHint { color:#7180a0; font-size:11px; }
            QFrame#TalkDock[recording="true"] QLabel#TalkHint { color:#ffffff; }
            QPushButton#TalkCancel { min-width:30px; max-width:30px; min-height:30px; max-height:30px;
                background:#e3e9f8; color:#526689; border-radius:15px; font-size:16px; padding:0; }
            QPushButton#TalkSend { min-width:34px; max-width:34px; min-height:34px; max-height:34px;
                background:#dce7ff; color:#315fd0; border-radius:17px; font-size:18px; padding:0; }
            QFrame#TalkDock[recording="true"] QPushButton#TalkCancel,
            QFrame#TalkDock[recording="true"] QPushButton#TalkSend { background:#ffffff; color:#416def; }
        ''')
        outer = QVBoxLayout(self); outer.setContentsMargins(3,3,3,14)
        card = QFrame(self); card.setObjectName('BubbleCard')
        layout = QVBoxLayout(card); layout.setContentsMargins(16,12,16,12); layout.setSpacing(7)
        header = QHBoxLayout()
        self.title = QLabel('咕嘎'); self.title.setObjectName('BubbleTitle')
        self.title.setAttribute(Qt.WA_TransparentForMouseEvents)
        header.addWidget(self.title,1)
        for title,tip,callback in (
            ('展开','查看完整对话',lambda:pet._open_agent_chat()),
            ('×','收起气泡',self._dismiss)):
            button=QPushButton(title);button.setToolTip(tip);button.setFocusPolicy(Qt.NoFocus)
            button.clicked.connect(callback);header.addWidget(button)
        self.share_button = QPushButton('共享屏幕')
        self.share_button.setObjectName('BubbleShare')
        self.share_button.setCheckable(True)
        self.share_button.setFocusPolicy(Qt.NoFocus)
        self.share_button.toggled.connect(self.share_screen)
        self.share_button.hide()
        header.addWidget(self.share_button)
        self.stop_button = QPushButton('结束通话')
        self.stop_button.setFocusPolicy(Qt.NoFocus)
        self.stop_button.clicked.connect(self.end_call)
        self.stop_button.hide()
        header.addWidget(self.stop_button)
        layout.addLayout(header)
        self.text = QLabel(); self.text.setWordWrap(True); self.text.setTextFormat(Qt.PlainText)
        self.text.setMaximumHeight(180)
        self.text.setStyleSheet('font:13px "Microsoft YaHei UI";color:#303b49;')
        self.text.setAttribute(Qt.WA_TransparentForMouseEvents)
        layout.addWidget(self.text)
        self.alignment_box=QFrame();alignment_layout=QVBoxLayout(self.alignment_box)
        alignment_layout.setContentsMargins(0,2,0,2)
        self.alignment_buttons=[]
        for index in range(1,4):
            button=QPushButton(' ');button.setMinimumHeight(44)
            button.setStyleSheet('text-align:left;padding:9px 12px;min-height:22px;font:13px "Microsoft YaHei UI";background:#edf2ff;border-radius:9px;color:#34436d;')
            button.clicked.connect(lambda checked=False,i=index:self._choose_alignment(i))
            alignment_layout.addWidget(button);self.alignment_buttons.append(button)
        self.alignment_input=QLineEdit();self.alignment_input.setMaxLength(3000)
        self.alignment_input.setPlaceholderText('补充风格、内容或限制，回车后开始')
        self.alignment_input.returnPressed.connect(lambda:self._choose_alignment(3,self.alignment_input.text()))
        alignment_layout.addWidget(self.alignment_input);self.alignment_input.hide()
        self.alignment_box.hide();layout.addWidget(self.alignment_box)
        from ui.desktop_experience import DraftEditor
        self.composer = QFrame()
        input_row = QHBoxLayout(self.composer)
        input_row.setContentsMargins(0, 0, 0, 0)
        input_row.setSpacing(6)
        self.editor = DraftEditor()
        self.editor.setObjectName('BubbleEditor')
        self.editor.setPlaceholderText('和咕嘎说点什么…')
        self.editor.setToolTip('Enter 发送 · Shift+Enter 换行')
        self.editor.setFixedHeight(57)
        self.editor.installEventFilter(self)
        input_row.addWidget(self.editor, 1)
        self.mic_button = QPushButton('语音')
        self.mic_button.setObjectName('BubbleMic')
        self.mic_button.setToolTip('录音、识别并发送')
        self.mic_button.setFocusPolicy(Qt.NoFocus)
        self.mic_button.clicked.connect(self.toggle_talk)
        input_row.addWidget(self.mic_button)
        self.send_button = QPushButton('发送 ↑')
        self.send_button.setObjectName('BubbleSend')
        self.send_button.setFocusPolicy(Qt.NoFocus)
        self.send_button.setEnabled(False)
        self.send_button.clicked.connect(self.submit)
        input_row.addWidget(self.send_button)
        self.editor.submitted.connect(self.submit)
        self.editor.textChanged.connect(self._draft_changed)
        self.notice = QLabel()
        self.notice.setObjectName('BubbleHint')
        self.notice.setWordWrap(True)
        self.notice.hide()
        self.options = QFrame()
        option_row = QHBoxLayout(self.options)
        option_row.setContentsMargins(0, 0, 0, 0)
        option_row.setSpacing(5)
        self.route_choice = QComboBox()
        self.effort_choice = QComboBox()
        self.executor_choice = QComboBox()
        for combo, name, choices in (
            (self.route_choice, '任务分流', (('自动分流', 'auto'), ('只问答', 'chat'), ('执行任务', 'task'))),
            (self.effort_choice, '思考档位', (('快速回答', 'quick'), ('轻度思考', 'low'), ('深度思考', 'high'), ('极限思考', 'max'))),
            (self.executor_choice, '任务执行方式', (('咕嘎执行', 'self'), ('Codex 执行', 'codex'), ('DSH 执行', 'dsh'))),
        ):
            combo.setAccessibleName(name)
            combo.setToolTip(name)
            for label, value in choices:
                combo.addItem(label, value)
            option_row.addWidget(combo, 1)
        self._bound_panel = None
        layout.addWidget(self.options)
        layout.addWidget(self.composer)
        layout.addWidget(self.notice)
        self._key_feedback = QTimer(self)
        self._key_feedback.setSingleShot(True)
        self._key_feedback.timeout.connect(lambda: self._paint_key_feedback(False))
        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self._dismiss)
        from ui.voice_record_bar import VoiceRecordBar
        self.talk_dock=VoiceRecordBar(self)
        self.cancel_button=self.talk_dock.cancel_button
        self.wave=self.talk_dock.wave
        self.talk_hint=self.talk_dock.hint
        self.talk_button=self.talk_dock.send_button
        self.wave_timer=self.talk_dock.timer
        self.talk_dock.started.connect(self.talk_started)
        self.talk_dock.submitted.connect(lambda:self.talk_stopped.emit(False))
        self.talk_dock.cancelled.connect(self._cancel_recording)
        self.talk_dock.state_changed.connect(self._dock_state_changed)
        layout.addWidget(self.talk_dock)
        self.talk_dock.hide()
        outer.addWidget(card)
        self.timer = QTimer(self); self.timer.setSingleShot(True); self.timer.timeout.connect(self._expire)
        pet.installEventFilter(self)

    def _draft_changed(self):
        self.send_button.setEnabled(bool(self.editor.toPlainText().strip()))
        if self.editor.hasFocus():
            self.timer.stop()
            self._paint_key_feedback(True)
            self._key_feedback.start(100)

    def _paint_key_feedback(self, on):
        if self.editor.property('pulse') != on:
            self.editor.setProperty('pulse', on)
            self.editor.style().unpolish(self.editor)
            self.editor.style().polish(self.editor)

    def _bind_options(self):
        panel = self.pet._ensure_agent_panel()
        if self._bound_panel is panel:
            return
        self._bound_panel = panel
        for bubble_combo, panel_combo in (
            (self.route_choice, panel.route_combo),
            (self.effort_choice, panel.effort_combo),
            (self.executor_choice, panel.executor_combo),
        ):
            def synchronize(source, target):
                index = target.findData(source.currentData())
                if index >= 0 and target.currentIndex() != index:
                    target.setCurrentIndex(index)
            synchronize(panel_combo, bubble_combo)
            bubble_combo.currentIndexChanged.connect(
                lambda _, source=bubble_combo, target=panel_combo: synchronize(source, target))
            panel_combo.currentIndexChanged.connect(
                lambda _, source=panel_combo, target=bubble_combo: synchronize(source, target))

    def open_composer(self, text='', *, focus=True):
        self._bind_options()
        if text:
            self.editor.setPlainText(text)
        self._set_notice('')
        self._sync_composer()
        self.text.setVisible(bool(self.full_text))
        self.adjustSize()
        self.follow_pet()
        self.show()
        self.raise_()
        if focus and self.composer.isVisible():
            self.activateWindow()
            self.editor.setFocus()

    def submit(self):
        if self._voice_state != 'idle' or self.call_active or self._alignment_id:
            return
        text = self.editor.toPlainText().strip()
        if not text:
            return
        if len(text) > 15000:
            self._set_notice('单条消息最多 15000 字，请分段发送。')
            return
        panel = self.pet._ensure_agent_panel()
        if panel.send(text, read_reply=True) is False:
            self._set_notice(panel.status.text()[:90] or '请稍后重试')
            return
        self.editor.clear()
        self._set_notice('')
        self._arm_expiry()

    def _set_notice(self, message):
        self.notice.setText(message)
        self.notice.setVisible(bool(message))
        self.adjustSize()
        self.follow_pet()

    def _sync_composer(self):
        available = self._voice_state == 'idle' and not self.call_active and not self._alignment_id
        self.composer.setVisible(available)
        self.options.setVisible(available)
        self.talk_dock.setVisible(self._voice_state != 'idle')

    def _dismiss(self):
        if self._voice_state != 'idle':
            self.cancel_talk()
        self.hide()

    def set_screen_sharing(self, active):
        previous = self.share_button.blockSignals(True)
        self.share_button.setChecked(active)
        self.share_button.setText('停止共享' if active else '共享屏幕')
        self.share_button.blockSignals(previous)

    def show_alignment(self,ident,goal,options):
        self._alignment_id=ident
        for i,item in enumerate(options[:2]):
            self.alignment_buttons[i].setText(str(i+1)+' · '+item[0])
            self.alignment_buttons[i].setToolTip(item[1])
        self.alignment_buttons[2].setText('3 · 我来补充要求…')
        self.alignment_input.clear();self.alignment_input.hide();self.alignment_box.show()
        brief=str(goal).split('。',1)[0][:110]
        self.show_text('先对齐一下：\n'+brief+'\n选一个方向，或补充你的要求。',ident)
        self.alignment_box.layout().activate();self.layout().activate();self.adjustSize();self.follow_pet()
        self.timer.stop()

    def _choose_alignment(self,index,supplement=''):
        if not self._alignment_id:return
        if index==3 and not supplement.strip():
            self.alignment_input.show();self.adjustSize();self.follow_pet();self.show()
            self.activateWindow();self.alignment_input.setFocus();return
        self.alignment_selected.emit(self._alignment_id,index,supplement.strip())

    def clear_alignment(self,ident=None):
        if ident and ident!=self._alignment_id:return
        self._alignment_id='';self.alignment_box.hide();self.alignment_input.hide()
        self._sync_composer()
        self.adjustSize()

    def set_call_active(self, active, conversation_id=None):
        self.call_active = bool(active)
        if conversation_id:
            self.conversation_id = conversation_id
        self.stop_button.setVisible(self.call_active)
        self.share_button.setVisible(self.call_active)
        self.share_button.setEnabled(self.call_active)
        self._sync_composer()
        self.title.setText('咕嘎 · 正在听' if active else '咕嘎')
        if active:
            self.show_text('我在听，可以直接和我说话。', self.conversation_id)
        elif self.isVisible():
            self.show_text('通话已结束。', self.conversation_id)
            self.timer.start(4000)

    def set_call_reconnecting(self, conversation_id=None, *, initial=False):
        """Transient WS gap: never claim the call ended; the worker reconnects."""
        if conversation_id:
            self.conversation_id = conversation_id
        self.call_active = True
        self.stop_button.setVisible(True)
        self.share_button.show()
        self.share_button.setEnabled(False)
        self._sync_composer()
        self.title.setText('咕嘎 · 正在连接' if initial else '咕嘎 · 正在重连')
        self.show_text('正在连接语音服务…' if initial else '语音连接中断，正在自动重连…',
                       self.conversation_id)
        self.timer.stop()

    def show_text(self, text, conversation_id=None):
        self.full_text = str(text).strip()
        if not self.full_text:
            return
        if conversation_id:
            self.conversation_id = conversation_id
        # Readable on the desktop without opening the conversation window.
        screen = QApplication.screenAt(self.pet.frameGeometry().center()) or QApplication.primaryScreen()
        available = screen.availableGeometry().width() if screen else 1200
        self.setFixedWidth(max(220,min(390,available-24)))
        preview = self.full_text if len(self.full_text)<=340 else self.full_text[:338].rstrip()+'…'
        previous=self._preview
        self._preview=preview
        self._reveal_index=min(self._reveal_index,len(previous)) if preview.startswith(previous) else 0
        # Reserve the final layout before revealing, preventing a jumping bubble.
        self.text.show()
        self.text.setText(preview)
        self.text.setMinimumHeight(0)
        self.text.setMinimumHeight(min(180,self.text.heightForWidth(self.width()-40)))
        self.text.setText(preview[:self._reveal_index] or ' ')
        self.reveal_timer.start()
        self._sync_composer()
        self.adjustSize()
        self.follow_pet()
        if self.pet.isVisible():
            try:
                import ctypes
                ctypes.windll.user32.SetWindowDisplayAffinity(int(self.winId()),0)
            except Exception:pass
            self.show()
        self.timer.stop()

    def _reveal(self):
        self._reveal_index=min(len(self._preview),self._reveal_index+2)
        self.text.setText(self._preview[:self._reveal_index])
        if self._reveal_index>=len(self._preview):
            self.reveal_timer.stop();self._arm_expiry()

    def _arm_expiry(self):
        if not (self._hovered or self.speaking or self._alignment_id or self._voice_state!='idle'
                or self.call_active or self.editor.hasFocus() or self.editor.toPlainText().strip()):
            self.timer.start(max(8000,min(26000,len(self._preview)*65)))

    def enterEvent(self,event):
        self._hovered=True;self.timer.stop();super().enterEvent(event)

    def leaveEvent(self,event):
        self._hovered=False;self._arm_expiry();super().leaveEvent(event)

    def hideEvent(self,event):
        self.reveal_timer.stop();self.timer.stop();self._hovered=False
        super().hideEvent(event)

    def showEvent(self,event):
        if self._reveal_index<len(self._preview):self.reveal_timer.start()
        else:self._arm_expiry()
        super().showEvent(event)

    def paintEvent(self,event):
        super().paintEvent(event)
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        center=max(24,min(self.width()-24,self.pet.frameGeometry().center().x()-self.x()))
        p.setPen(QPen(QColor('#cbd6e1'),1));p.setBrush(QColor('#fffdf9'))
        p.drawPolygon(QPolygonF([QPointF(center-8,self.height()-14),QPointF(center,self.height()-4),QPointF(center+8,self.height()-14)]))

    def set_speaking(self, speaking):
        self.speaking = bool(speaking)
        self.title.setText('咕嘎 · 正在回答' if speaking else ('咕嘎 · 正在听' if self.call_active else '咕嘎'))
        if speaking:
            self.timer.stop()
        elif self.isVisible():
            self._arm_expiry()

    def _dock_state_changed(self,state):
        self._voice_state=state;self._recording=state=='recording'
        if state!='idle':self.timer.stop()
        elif self.isVisible():self._arm_expiry()
        self._sync_composer()
        self.adjustSize();self.follow_pet()

    def set_voice_state(self,state):
        self.talk_dock.set_state(state)

    def set_voice_level(self,value):
        self.talk_dock.set_level(value)

    def toggle_talk(self):
        self.talk_dock.primary()

    def _begin_talk(self):
        self.talk_dock.begin()

    def _finish_talk(self):
        if self._voice_state in ('recording','ready'):self.talk_dock.primary()

    def _cancel_recording(self):
        self.talk_stopped.emit(True)

    def cancel_talk(self):
        self.talk_dock.cancel()

    def _expire(self):
        if (self.speaking or self._hovered or self.reveal_timer.isActive()
                or self.editor.hasFocus() or self.editor.toPlainText().strip()):return
        if self._alignment_id:return
        if self._recording or self._voice_state in ('preparing','transcribing','ready'):return
        if self.call_active:
            self.text.setText('正在听你说话…')
            self.adjustSize(); self.follow_pet()
        else:
            self.hide()

    def follow_pet(self):
        center = self.pet.frameGeometry().center()
        screen = QApplication.screenAt(center) or QApplication.primaryScreen()
        if not screen:
            return
        area = screen.availableGeometry()
        from ui.desktop_position import pet_adjacent_position
        self.move(pet_adjacent_position(self.pet.frameGeometry(), self.size(), area))

    def eventFilter(self, obj, event):
        if obj is self.editor:
            if event.type() == QEvent.FocusIn:self.timer.stop()
            elif event.type() == QEvent.FocusOut:QTimer.singleShot(0, self._arm_expiry)
        if obj is self.pet:
            if event.type() in (QEvent.Move,QEvent.Resize):
                self.follow_pet()
            elif event.type()==QEvent.Hide:
                self.hide()
            elif event.type()==QEvent.Show and self.call_active:
                self.follow_pet(); self.show()
        return super().eventFilter(obj,event)

    def mouseReleaseEvent(self, event):
        if event.button()==Qt.LeftButton:
            self._reveal_index=len(self._preview);self.reveal_timer.stop()
            self.text.setText(self._preview);self._arm_expiry();event.accept()
        else:
            super().mouseReleaseEvent(event)
