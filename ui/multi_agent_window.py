"""Lightweight companion Guga windows and the council controller."""
import time
from pathlib import Path
from PyQt5.QtCore import QObject,QPoint,Qt,pyqtSignal
from PyQt5.QtGui import QColor,QFont,QPainter,QCursor
from ui.attention_preview import AttentionPreview
from PyQt5.QtWidgets import QInputDialog,QMenu,QWidget,QToolTip

from core.agent_memory import AgentMemoryStore
from core.agent_council import DiscussionWorker
from ui.mascot_animator import MascotAnimator
from ui.speech_bubble import PetSpeechBubble
from ui.pet_physics import DesktopPetPhysics


class CompanionWindow(QWidget):
    hidden=pyqtSignal(str)
    def __init__(self,owner,profile,memory):
        super().__init__(None,Qt.FramelessWindowHint|Qt.Tool|Qt.WindowStaysOnTopHint|Qt.WindowDoesNotAcceptFocus)
        self.owner=owner;self.profile=dict(profile);self.memory=memory;self.agent_id=self.profile['agent_id']
        self.setWindowTitle(self.profile['display_name']+' · '+self.profile['role'])
        self.setAttribute(Qt.WA_TranslucentBackground);self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True);self.setToolTip(self.profile['display_name']+' · '+self.profile['role'])
        self.anim=MascotAnimator(self.profile.get('appearance','guga'),display_width=225)
        self._renderer_error=''
        if self.profile.get('appearance')=='dafish' and self.owner._config._s.value('multiagent/002/renderer','dsh-pet')=='dsh-pet':
            try:
                from ui.video_mascot import VideoMascotAnimator
                self.anim=VideoMascotAnimator(display_width=225)
            except Exception as exc:
                self._renderer_error=type(exc).__name__+': '+str(exc)
        self._attention=AttentionPreview(seed=int(self.agent_id))
        self._attention_last=time.monotonic()
        self.anim.on_update=self._on_animation;self.anim.start();self.anim.set_expression(self.profile['expression'])
        self._pet_scale=max(.4,min(1.,float(self.owner._config._s.value('multiagent/%s/scale'%self.agent_id,1.))))
        self.resize(round(self.anim.width*self._pet_scale),round(self.anim.height*self._pet_scale)+22)
        self.bubble=PetSpeechBubble(self);self.bubble.title.setText(self.profile['display_name']+' · '+self.profile['role'])
        self.bubble.stop_button.hide();self.bubble.clicked.connect(lambda:owner._multi_agent.open_chat(self.agent_id));self.bubble.hide()
        self._drag=None;self._moved=False;self._drag_last=None
        self._physics=DesktopPetPhysics(self,self.anim,lambda:self.owner._config._s.setValue('multiagent/%s/pos'%self.agent_id,self.pos()))
        if hasattr(self.anim,'on_move'):self.anim.on_move=self._physics.walk
        saved=self.owner._config._s.value('multiagent/%s/pos'%self.agent_id)
        if saved and isinstance(saved,QPoint):self.move(saved)
        else:
            base=self.owner.pos();offset={ '002':QPoint(-self.width()-12,35), '003':QPoint(self.owner.width()+12,35) }.get(self.agent_id,QPoint(-self.width()-12,35))
            self.move(base+offset)
        screen=self.owner.screen()
        if screen:
            bounds=screen.availableGeometry()
            self.move(max(bounds.left(),min(self.x(),bounds.right()-self.width()+1)),
                      max(bounds.top(),min(self.y(),bounds.bottom()-self.height()+1)))
        self.show();self.raise_()

    def _on_animation(self):
        now=time.monotonic();dt=min(.1,now-self._attention_last);self._attention_last=now
        pointer=self.mapFromGlobal(QCursor.pos())
        x=(pointer.x()-self.width()/2)/max(1,self.width()/2)
        y=(pointer.y()-self.height()*.4)/max(1,self.height()/2)
        busy=self.anim.expression=='sleepy' or self.anim.action!='none' or getattr(self,'_drag',None) is not None
        self.anim.look_at(*self._attention.update(dt,x,y,busy=busy))
        self.update()

    def set_scale(self,value):
        value=max(.4,min(1.,float(value)))
        bottom=self.geometry().bottom();center=self.geometry().center().x();self._pet_scale=value
        self.resize(round(self.anim.width*value),round(self.anim.height*value)+22)
        self.move(center-self.width()//2,bottom-self.height()+1)
        screen=self.screen()
        if screen:
            area=screen.availableGeometry()
            self.move(max(area.left(),min(self.x(),area.right()-self.width()+1)),
                      max(area.top(),min(self.y(),area.bottom()-self.height()+1)))
        self.owner._config._s.setValue('multiagent/%s/scale'%self.agent_id,value)
        self.bubble.follow_pet();self.update()

    def show_size_dialog(self):
        from PyQt5.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QLabel,QSlider,QPushButton
        dialog=QDialog(self);dialog.setWindowTitle(self.profile['display_name']+'大小');dialog.resize(320,130)
        layout=QVBoxLayout(dialog);label=QLabel();layout.addWidget(label)
        slider=QSlider(Qt.Horizontal);slider.setRange(40,100);slider.setValue(round(self._pet_scale*100));layout.addWidget(slider)
        def changed(value):
            label.setText(str(value)+'% · 原尺寸为最大值（鼠标滚轮也能缩放）');self.set_scale(value/100)
        slider.valueChanged.connect(changed);label.setText(str(slider.value())+'% · 原尺寸为最大值（鼠标滚轮也能缩放）')
        row=QHBoxLayout();reset=QPushButton('恢复原尺寸');reset.clicked.connect(lambda:slider.setValue(100));row.addWidget(reset)
        done=QPushButton('完成');done.clicked.connect(dialog.accept);row.addWidget(done);layout.addLayout(row);dialog.exec_()

    def wheelEvent(self,event):
        """滚轮缩放：和主桌宠一致，上滚变大、下滚变小。"""
        delta=event.angleDelta().y()
        if not delta:
            event.ignore();return
        notches=int(delta/120) or (1 if delta>0 else -1)
        notches=max(-4,min(4,notches))
        target=round(max(.4,min(1.,self._pet_scale+notches*.05)),2)
        if abs(target-self._pet_scale)<1e-6:
            QToolTip.showText(event.globalPos(),'已到'+('最大' if self._pet_scale>=1 else '最小')+' · %d%%'%round(self._pet_scale*100),self,self.rect(),1200)
            event.accept();return
        self.set_scale(target)
        QToolTip.showText(event.globalPos(),'大小 %d%%'%round(self._pet_scale*100),self,self.rect(),1200)
        event.accept()

    def _with_identity(self,callback):
        panel=self.owner._ensure_agent_panel()
        if not panel.activate_agent(self.agent_id):
            self.say(panel.status.text());return False
        callback();return True

    def choose_expression(self,name):
        self._physics.stop()
        self.anim.user_interaction()
        self.anim.set_expression(name)

    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing,True)
        p.save();p.scale(self._pet_scale,self._pet_scale);self.anim.draw(p,0,0);p.restore()
        badge=self.profile['agent_id']+' · '+self.profile['role']
        fm=p.fontMetrics();width=fm.horizontalAdvance(badge)+16
        rect=(self.width()-width)//2,self.height()-23,width,19
        p.setPen(Qt.NoPen);p.setBrush(QColor(245,248,255,230));p.drawRoundedRect(*rect,9,9)
        p.setPen(QColor('#52627a'));p.setFont(QFont('Microsoft YaHei UI',9,QFont.DemiBold));p.drawText(rect[0]+8,rect[1]+14,badge)
        if self.agent_id=='003':
            p.setFont(QFont('Microsoft YaHei UI',16));p.setPen(QColor('#f7aa47'));p.drawText(self.width()-27,self.height()-2,'🍊')
        accent={'sleepy':'Z z','surprised':'!','sad':'…','playful':'♪','happy':'✦','normal':'','angry':'!','eating':'🍊'}.get(self.anim.expression,'')
        if accent:
            p.setFont(QFont('Microsoft YaHei UI',13,QFont.DemiBold));p.setPen(QColor('#8a6a77'))
            p.drawText(self.width()-36,34,accent)

    def say(self,text,expression=None):
        if expression:self.anim.set_expression(expression)
        self.bubble.title.setText(self.profile['display_name']+' · '+self.profile['role'])
        self.bubble.show_text(str(text)[:1600],self.agent_id)

    def mousePressEvent(self,event):
        if event.button()==Qt.LeftButton:
            self._woke=self.anim.user_interaction()
            self._drag=event.globalPos()-self.frameGeometry().topLeft();self._moved=False
            self._physics.press(event.globalPos())
            self._press_point=event.globalPos()
            self._drag_last=(event.globalPos().x(),time.monotonic());event.accept()

    def mouseMoveEvent(self,event):
        if self._drag is not None and event.buttons()&Qt.LeftButton:
            if not self._moved and (event.globalPos()-self._press_point).manhattanLength()<4:return
            now=time.monotonic()
            if self._drag_last:
                speed=(event.globalPos().x()-self._drag_last[0])/max(.01,now-self._drag_last[1])
                self.anim.set_dragging(True,speed*.06)
            self._drag_last=(event.globalPos().x(),now)
            self._physics.sample(event.globalPos())
            self.move(event.globalPos()-self._drag);self._moved=True;event.accept()

    def mouseReleaseEvent(self,event):
        if event.button()==Qt.LeftButton:
            if self._moved:self.owner._config._s.setValue('multiagent/%s/pos'%self.agent_id,self.pos())
            self._drag=None;self._drag_last=None;self.anim.set_dragging(False)
            self._physics.release(self._moved)
            if not self._moved and not getattr(self,'_woke',False) and hasattr(self.anim,'click'):self.anim.click()
            event.accept()

    def mouseDoubleClickEvent(self,event):
        if event.button()==Qt.LeftButton:
            self.anim.user_interaction();self.owner._multi_agent.open_chat(self.agent_id)

    def _ensure_menu(self):
        if getattr(self,'_menu',None) is not None:return self._menu
        menu=QMenu(None);self._menu=menu
        template=self.owner._ensure_pet_menu()
        menu.setWindowFlags(Qt.Popup|Qt.FramelessWindowHint|Qt.NoDropShadowWindowHint|Qt.WindowStaysOnTopHint)
        menu.setAttribute(Qt.WA_TranslucentBackground,False);menu.setWindowOpacity(1.)
        menu.setPalette(template.palette());menu.setStyleSheet(template.styleSheet());menu.setAutoFillBackground(True)
        battle=menu.addMenu('战旗')
        battle.addAction('⚡ 一键拔线（每局最多 9 次，F1）').triggered.connect(self.owner._netcut)
        menu.addAction('💬 对话 · '+self.profile['display_name']+'智能体').triggered.connect(lambda:self.owner._multi_agent.open_chat(self.agent_id))
        menu.addAction('🎙 长语音 · 边聊边执行').triggered.connect(lambda:self._with_identity(self.owner._open_agent_voice))
        team=menu.addMenu('👥 多智能体')
        team.addAction('召唤 001 · 咕嘎').triggered.connect(lambda:self.owner._multi_agent.open_chat('001'))
        team.addAction('召唤 002 · 大肥鱼').triggered.connect(lambda:self.owner._multi_agent.summon('002'))
        team.addAction('召唤 003 · Dora').triggered.connect(lambda:self.owner._multi_agent.summon('003'))
        team.addAction('同时召唤大肥鱼和 Dora').triggered.connect(self.owner._multi_agent.summon_pair)
        team.addAction('让团队讨论…').triggered.connect(lambda:self.owner._multi_agent.start_discussion((self.agent_id,'001')))
        menu.addAction('看屏幕').triggered.connect(lambda:self._with_identity(lambda:self.owner._screen_tools.begin('ask')))
        menu.addAction('解释').triggered.connect(lambda:self._with_identity(lambda:self.owner._screen_tools.begin('explain')))
        menu.addAction(self.profile['display_name']+'大小…').triggered.connect(self.show_size_dialog)
        stage=menu.addMenu('动作与表情')
        if hasattr(self.anim,'pools'):
            original=stage.addMenu('大肥鱼原版动画')
            for category in self.anim.pools['categories']:
                group=original.addMenu(category['id'])
                for clip in category['actions']:
                    group.addAction(clip).triggered.connect(lambda checked=False,n=clip:self.anim.play_clip(n))
            roaming=stage.addAction('桌面漫游（可关闭）');roaming.setCheckable(True)
            roaming.toggled.connect(lambda on:setattr(self.anim,'_roaming',on))
        expressions=stage.addMenu('表情')
        labels={'normal':'日常','happy':'开心','sad':'委屈','angry':'生气','sleepy':'困倦'}
        for name in self.anim.available_expressions:
            expressions.addAction(labels.get(name,name)).triggered.connect(lambda checked=False,n=name:self.choose_expression(n))
        for label,name in (('暗中观察','sneak_peek'),('打招呼','wave'),('开心蹦','bounce'),('点头','nod')):
            stage.addAction(label).triggered.connect(lambda checked=False,n=name:self.anim.trigger_action(n))
        self._menu_sunglasses=stage.addAction('戴上墨镜')
        self._menu_sunglasses.triggered.connect(lambda:self.anim.trigger_action('remove_sunglasses' if self.anim.sunglasses else 'sunglasses'))
        self._menu_dance=stage.addAction('开心摆鳍舞')
        self._menu_dance.triggered.connect(lambda:self.anim.trigger_action('stop_dance' if self.anim.dancing else 'dance'))
        if self.agent_id=='003':stage.addAction('🍊 吃橘子').triggered.connect(lambda:self.choose_expression('eating'))
        settings=menu.addMenu('🎙 声音与人格')
        settings.addAction('独立声音设置…').triggered.connect(lambda:self.owner._multi_agent.configure_voice(self.agent_id))
        settings.addAction('修改人格…').triggered.connect(lambda:self.owner._multi_agent.edit_personality(self.agent_id))
        settings.addAction('修改擅长领域…').triggered.connect(lambda:self.owner._multi_agent.edit_specialty(self.agent_id))
        settings.addAction('记住一条备注…').triggered.connect(lambda:self.owner._multi_agent.remember_note(self.agent_id))
        settings.addAction('查看长期记忆摘要').triggered.connect(lambda:self.owner._multi_agent.show_memory(self.agent_id))
        battle.addAction('📋 日志分析').triggered.connect(lambda:self._with_identity(self.owner._review_bg_logs))
        battle.addAction('🎮 战局分析 / 实战操作').triggered.connect(lambda:self._with_identity(self.owner._open_bg_live))
        menu.addSeparator();self.owner._multi_agent.add_desktop_actions(menu)
        menu.ensurePolished();menu.adjustSize();menu.winId();return menu

    def contextMenuEvent(self,event):
        self._physics.stop()
        self.anim.user_interaction();menu=self._ensure_menu()
        self._menu_sunglasses.setText('摘下墨镜' if self.anim.sunglasses else '戴上墨镜')
        self._menu_dance.setText('停止摇摆' if self.anim.dancing else '摇摆跳舞')
        menu.popup(event.globalPos());event.accept()

    def _dispose_menu(self):
        menu=getattr(self,'_menu',None)
        if menu is not None:menu.close();menu.deleteLater();self._menu=None

    def close_companion(self):
        self._physics.stop()
        self._dispose_menu()
        self.hidden.emit(self.agent_id);self.bubble.hide();self.anim.stop();self.hide();self.deleteLater()

    def closeEvent(self,event):
        self._physics.stop()
        self._dispose_menu()
        self.hidden.emit(self.agent_id);self.bubble.hide();self.anim.stop();event.accept()


class MultiAgentManager(QObject):
    def __init__(self,owner,memory=None):
        super().__init__(owner);self.owner=owner;self.memory=memory or AgentMemoryStore()
        self.companions={};self.last_discussion=None
        self.council=DiscussionWorker(self,self.memory);self.council.turn.connect(self._discussion_turn)
        self.council.completed.connect(self._discussion_done)

    def summon(self,agent_id):
        agent_id=str(agent_id)
        if agent_id=='001':
            self.open_chat('001');return self.owner._agent_panel
        if agent_id not in ('002','003'):raise ValueError('只能召唤 002 或 003')
        if agent_id in self.companions and self.companions[agent_id].isVisible():
            self.companions[agent_id].raise_();return self.companions[agent_id]
        profile=self.memory.profile(agent_id)
        if not profile:raise ValueError('身份档案不存在')
        window=CompanionWindow(self.owner,profile,self.memory)
        window.hidden.connect(self._hidden);self.companions[agent_id]=window
        window.say(profile['display_name']+' 来了。点我或右键，可以让我和项目里的其他咕嘎一起想。')
        return window

    def summon_pair(self):
        self.summon('002');self.summon('003')

    def add_desktop_actions(self, menu):
        for agent_id, name in (('002', '大肥鱼'), ('003', 'Dora')):
            action = menu.addAction(name)

            def refresh(ident=agent_id, label=name, item=action):
                window = self.companions.get(ident)
                item.setText(('退出 ' if window is not None and window.isVisible() else '唤醒 ') + label)

            def toggle(checked=False, ident=agent_id):
                window = self.companions.get(ident)
                if window is not None and window.isVisible():
                    window.close_companion()
                else:
                    self.summon(ident)

            menu.aboutToShow.connect(refresh)
            action.triggered.connect(toggle)
            refresh()

    def _hidden(self,agent_id):self.companions.pop(str(agent_id),None)

    def user_interaction(self,agent_id):
        companion=self.companions.get(str(agent_id))
        return companion.anim.user_interaction() if companion else False

    def work_state(self,agent_id,state,task_id='main'):
        if str(agent_id)=='001':
            return self.owner._anim2d.set_work_state(state,task_id)
        companion=self.companions.get(str(agent_id))
        if companion and hasattr(companion.anim,'set_work_state'):
            if companion.profile.get('appearance')=='dafish':return companion.anim.set_work_state(state)
            return companion.anim.set_work_state(state,task_id)
        return False

    def speaking(self,agent_id,on):
        if str(agent_id)=='001':
            return self.owner._anim2d.set_speaking(on)
        companion=self.companions.get(str(agent_id))
        if companion and hasattr(companion.anim,'set_speaking'):
            companion.anim.set_speaking(on)

    def open_chat(self,agent_id):
        self.user_interaction(agent_id)
        panel=self.owner._ensure_agent_panel()
        if not panel.activate_agent(agent_id):
            self.owner._show_agent_bubble(panel.status.text(),panel.active_id);return False
        self.owner._open_agent_chat();return True

    def say(self,agent_id,text,expression=None):
        if str(agent_id)=='001':
            panel=getattr(self.owner,'_agent_panel',None);self.owner._show_agent_bubble(text,getattr(panel,'active_id',None))
        else:
            companion=self.companions.get(str(agent_id)) or self.summon(agent_id)
            companion.say(text,expression)

    def gesture(self,agent_id,name):
        if str(agent_id)=='001':self.owner._perform_gesture(name);return
        companion=self.companions.get(str(agent_id)) or self.summon(agent_id)
        companion.anim.trigger_action(name)

    def set_expression(self,agent_id,name):
        if str(agent_id)=='001':self.owner._anim.set_expression(name)
        else:
            companion=self.companions.get(str(agent_id)) or self.summon(agent_id)
            companion.anim.set_expression(name)

    def start_discussion(self,agent_ids=None,topic=None):
        if self.council.active:
            self.owner._show_agent_bubble('上一场咕嘎讨论还没结束。',self.owner._agent_panel.active_id if getattr(self.owner,'_agent_panel',None) else None);return False
        if topic is None:
            topic,ok=QInputDialog.getMultiLineText(self.owner,'咕嘎团队讨论','他们要根据各自记忆和专长讨论什么？')
            if not ok or not topic.strip():return False
        ids=list(agent_ids or ('001','002'))
        for agent_id in ids:
            if agent_id!='001':self.summon(agent_id)
        try:accepted=self.council.start(topic,ids,rounds=1)
        except Exception as exc:
            self.owner._show_agent_bubble('讨论没有启动：'+str(exc));return False
        self.last_discussion=accepted
        self.owner._show_agent_bubble('咕嘎团队开始讨论了：'+str(topic)[:80],self.owner._agent_panel.active_id if getattr(self.owner,'_agent_panel',None) else None)
        return True

    def _discussion_turn(self,item):
        if item['agent_id']=='001':self.owner._show_agent_bubble(item['text'],self.owner._agent_panel.active_id if getattr(self.owner,'_agent_panel',None) else None)
        else:
            companion=self.companions.get(item['agent_id']) or self.summon(item['agent_id'])
            companion.say(item['text'],'surprised' if item['round']%2 else 'happy')

    def _discussion_done(self,result):
        self.last_discussion=result
        if not result.get('ok'):
            self.owner._show_agent_bubble('咕嘎讨论没有完成：'+str(result.get('error','未知原因')))
        else:
            self.owner._show_agent_bubble('这场讨论结束，结论没有写入正式项目决策；需要执行时再明确交代下一步。')

    def _refresh_profile(self,agent_id,profile):
        if agent_id in self.companions:self.companions[agent_id].profile=profile
        panel=getattr(self.owner,'_agent_panel',None)
        if panel and panel.active_agent_id==agent_id:
            panel.active_profile=profile;panel.refresh_tree()

    def edit_personality(self,agent_id):
        profile=self.memory.profile(agent_id)
        if not profile:return False
        text,ok=QInputDialog.getMultiLineText(self.owner,'独立人格',profile['display_name']+'是什么性格？',text=profile.get('personality',''))
        if not ok or not text.strip():return False
        profile=self.memory.update_profile(agent_id,personality=text)
        self._refresh_profile(agent_id,profile)
        self.say(agent_id,'人格已单独保存；新消息使用新设定，正在进行的通话需重新开启。');return True

    def configure_voice(self,agent_id):
        from PyQt5.QtWidgets import QDialog,QVBoxLayout,QFormLayout,QComboBox,QLabel,QDialogButtonBox
        from core.agent_identity import voice_preferences,VOICES
        profile=self.memory.profile(agent_id)
        if not profile:return False
        voices=dict(VOICES.get(agent_id,{}));voices.update(voice_preferences(profile))
        dialog=QDialog(self.owner);dialog.setWindowTitle(profile['display_name']+' · 独立声音');dialog.resize(430,210)
        layout=QVBoxLayout(dialog)
        note=QLabel('只改变当前成员的音色，不更改 API 或其他成员。\n实时音色下次开启通话生效；普通语音下一次朗读生效。')
        note.setWordWrap(True);layout.addWidget(note);form=QFormLayout();layout.addLayout(form)
        realtime=QComboBox();realtime.setEditable(True)
        realtime.addItems(['longanqian_v3.1','longanhuan_v3.1','longanlingxin_v3.1','longanfengyue_v3.1','xunanchuan_v3.1'])
        realtime.setCurrentText(voices.get('qwen31','longanqian_v3.1'));form.addRow('千问 3.1 实时音色',realtime)
        edge=QComboBox();edge.setEditable(True);edge.addItems(['zh-CN-XiaoyiNeural','zh-CN-XiaoxiaoNeural','zh-CN-YunxiNeural'])
        edge.setCurrentText(voices.get('edge_voice','zh-CN-XiaoyiNeural'));form.addRow('普通朗读音色',edge)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject);layout.addWidget(buttons)
        if dialog.exec_()!=QDialog.Accepted:return False
        voices.update(qwen31=realtime.currentText().strip(),edge_voice=edge.currentText().strip())
        try:profile=self.memory.update_profile(agent_id,voice_settings=voices)
        except ValueError as exc:self.say(agent_id,str(exc));return False
        self._refresh_profile(agent_id,profile);self.say(agent_id,'我的声音已单独保存，下次通话就使用新音色。');return True

    def edit_specialty(self,agent_id):
        profile=self.memory.profile(agent_id)
        if not profile:return False
        text,ok=QInputDialog.getText(self.owner,'修改擅长领域',profile['display_name']+'擅长什么？',text=profile['specialty'])
        if not ok or not text.strip():return False
        profile=self.memory.update_profile(agent_id,specialty=text)
        if agent_id in self.companions:self.companions[agent_id].profile=profile
        panel=getattr(self.owner,'_agent_panel',None)
        if panel and panel.active_agent_id==agent_id:
            panel.active_profile=profile;panel.refresh_tree()
        self.say(agent_id,profile['display_name']+' 现在负责：'+profile['specialty'])
        return True

    def remember_note(self,agent_id):
        profile=self.memory.profile(agent_id)
        if not profile:return False
        text,ok=QInputDialog.getMultiLineText(self.owner,'写入长期记忆',profile['display_name']+'要长期记住什么？')
        if not ok or not text.strip():return False
        self.memory.remember(agent_id,text,kind='manual',importance=4)
        self.say(agent_id,profile['display_name']+' 记住了这条本地备注。')
        return True

    def show_memory(self,agent_id):
        profile=self.memory.profile(agent_id);items=self.memory.recall(agent_id,'',limit=4)
        if not profile:return
        if items:
            text=profile['display_name']+' 最近记住：'+ '；'.join(x['content'][:90] for x in items[:3])
        else:text=profile['display_name']+' 还没有形成长期记忆。'
        self.say(agent_id,text)

    def status(self):
        return dict(companions=[dict(agent_id=x.agent_id,name=x.profile['display_name'],role=x.profile['role'],
            visible=x.isVisible(),scale=x._pet_scale,expression=x.anim.expression,asset=Path(x.anim.path).name,
            renderer=type(x.anim).__name__,renderer_error=x._renderer_error,
            decode_error=getattr(x.anim,'decode_error',''),clip=getattr(x.anim,'current_clip',''),
            avatar_version='2026-10-31.1') for x in self.companions.values()],
            discussion=dict(active=self.council.active,last=self.last_discussion),memory=self.memory.status())

    def close(self):
        self.council.cancel()
        for companion in list(self.companions.values()):
            try:companion.close_companion()
            except Exception:pass
        self.companions.clear()
