"""One compact home for animation previews and persistent behavior settings."""
from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QCheckBox, QPushButton, QComboBox, QGroupBox, QDialogButtonBox)


class BehaviorDialog(QDialog):
    def __init__(self, pet):
        super().__init__(pet)
        self.pet=pet
        self.setWindowTitle('咕嘎 · 动作与行为')
        self.setMinimumWidth(460)
        layout=QVBoxLayout(self)
        intro=QLabel('空闲时交替打盹、比心、看看周围和散步。做任务和被戳时会切换动作。')
        intro.setWordWrap(True);layout.addWidget(intro)
        options=QGroupBox('自动行为');box=QVBoxLayout(options)
        for label,key in [('自然动作交替','natural_actions'),('允许在屏幕上散步','auto_walk'),
                          ('启动时播放出场','entrance'),('播放出场音效','entrance_sound'),('动作周围特效','effects')]:
            check=QCheckBox(label)
            check.setChecked(pet._config._s.value('animation/'+key,True,type=bool))
            check.toggled.connect(lambda value,k=key:pet._set_animation_option(k,value))
            box.addWidget(check)
        layout.addWidget(options)
        sound=QPushButton('调整出场音量…');sound.clicked.connect(pet._set_entrance_volume);box.addWidget(sound)
        previews=QGroupBox('动作预览');preview=QVBoxLayout(previews)
        self.choice=QComboBox()
        for label,name in [('处理任务 · 画板','work'),('屏幕散步','walk'),('戳一戳','poke'),('出场','entrance'),
                           ('困倦打盹','idle:doze'),('双手比心','idle:heart'),('看看周围','peek'),
                           ('开心','expression:happy'),('惊讶','expression:surprised'),('日常','expression:normal')]:
            self.choice.addItem(label,name)
        preview.addWidget(self.choice)
        row=QHBoxLayout();play=QPushButton('播放一次');play.clicked.connect(self.play)
        stop=QPushButton('停止预览与走路');stop.clicked.connect(self.stop)
        row.addWidget(play);row.addWidget(stop);preview.addLayout(row)
        self.status=QLabel('散步会移动咕嘎窗口；点击、拖动或开始任务会立即停步。')
        self.status.setWordWrap(True);preview.addWidget(self.status);layout.addWidget(previews)
        close=QDialogButtonBox(QDialogButtonBox.Close);close.rejected.connect(self.close);layout.addWidget(close)

    def play(self):
        pet=self.pet;anim=pet._anim2d;name=self.choice.currentData()
        if anim._task_motion.active or anim._music_stage_active or anim._speaking:
            self.status.setText('咕嘎正在工作或说话，结束后再预览。');return
        pet._on_user_interaction();pet._pet_physics.stop()
        anim.stop_clip(natural=True)
        if name=='walk':ok=pet._walk_on_desktop()
        elif name=='entrance':ok=pet._play_entrance()
        elif name.startswith('idle:'):ok=anim.play_idle_motion(name[5:])
        elif name.startswith('expression:'):anim.set_expression(name.split(':')[1]);ok=True
        elif name in ('work','poke'):ok=anim.play_personality(name)
        else:anim.trigger_action(name);ok=True
        self.status.setText('正在预览：'+self.choice.currentText() if ok is not False else '当前空间或状态不适合播放。')

    def stop(self):
        self.pet._pet_physics.stop()
        if not self.pet._anim2d._task_motion.active and not self.pet._anim2d._music_stage_active:
            self.pet._anim2d.stop_clip(natural=True)
        self.status.setText('预览已停止。')
