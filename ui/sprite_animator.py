# -*- coding: utf-8 -*-
"""咕嘎企鹅精灵动画器 — 多表情图 + alpha 过渡 + 企鹅专属动作

表情: normal/happy/sleepy/surprised/sad/playful（5张素材，happy兼任normal）
动作: bounce(开心蹦) / shake(受惊抖) / point(指点江山-战棋军师)
      peek(偷偷看我) / tsundere(傲娇摇头)
"""
import math
import random
import os
import time
from collections import OrderedDict
from ui.avatar_motion import AvatarMotion
from ui.executor_appearance import draw_executor_accessory
from PyQt5.QtCore import Qt, QPointF, QRectF, QTimer
from PyQt5.QtGui import QPixmap, QPainter, QColor, QPen, QPainterPath, QRadialGradient, QLinearGradient


class LazyFrameSequence:
    """Small sequential cache so long native video clips do not inflate startup."""
    def __init__(self,paths,loader,max_cached=36):
        self.paths=list(paths);self.loader=loader;self.max_cached=max_cached
        self.cache=OrderedDict()

    def __len__(self):return len(self.paths)

    def __getitem__(self,index):
        index=int(index)
        if index<0:index+=len(self.paths)
        if not 0<=index<len(self.paths):raise IndexError(index)
        if index in self.cache:
            self.cache.move_to_end(index);return self.cache[index]
        pixmap=self.loader(self.paths[index])
        self.cache[index]=pixmap
        while len(self.cache)>self.max_cached:self.cache.popitem(last=False)
        return pixmap


class SpriteAnimator:
    EXPRESSIONS = ["normal", "happy", "sleepy", "surprised", "sad", "playful"]
    DANCE_CLIP = "dance:joy_sway"
    LEGACY_DANCE_CLIP = "dance:eyes_shake"

    FILE_MAP = {
        "normal":    "normal",
        "happy":     "happy",
        "sleepy":    "sleepy",
        "surprised": "surprised",
        "sad":       "sad",
        "playful":   "playful",
    }

    ACTIONS = ("idle_doze", "idle_heart", "look_up_step", "none", "bounce", "shake", "point", "peek", "tsundere", "head_shake", "nod", "tilt", "wave", "sunglasses", "remove_sunglasses", "dance", "stop_dance", "sneak_peek")

    def __init__(self, res_dir: str, display_width: int = 260):
        self._res_dir = res_dir
        self._presence = AvatarMotion()
        from core.task_animation import TaskAnimationDirector
        self._task_motion=TaskAnimationDirector()
        self._being_dragged=False
        self._walking=False
        self.on_walk_requested=None
        self.on_work_started=None
        self.auto_walk=True
        self.natural_actions=True
        self._walk_countdown=random.uniform(35.,70.)
        self._natural_pause = 0.0
        self._last_task_variant=None
        self.work_activity='working'
        self._walk_direction=1
        self._walk_phase=0.
        self._face_details=None
        self._native_face = None
        self._fade_surface = None
        self._gaze = (0., 0.)
        self._pointer_near = False
        self._blink_at = random.uniform(2.2, 4.2)
        self._mouth_pose = 0.
        self._speaking=False
        self._hand_blend=0.
        self._hand_releasing=False
        self._tap_elapsed = 1.0
        self._tap_amount = .92
        self._dw = display_width

        self._sprites = {}
        self._src_size = None
        for expr in self.EXPRESSIONS:
            file_key = self.FILE_MAP.get(expr, expr)
            paths = [
                os.path.join(res_dir, "expressions", f"{file_key}_transparent.png"),
                os.path.join(res_dir, "expressions", f"{file_key}.png"),
            ]
            for path in paths:
                if os.path.exists(path):
                    pm = QPixmap(path)
                    if not pm.isNull():
                        self._sprites[expr] = pm
                        if self._src_size is None:
                            self._src_size = (pm.width(), pm.height())
                        break

        if not self._sprites:
            raise FileNotFoundError(f"No penguin sprite images found in {res_dir}")
        if "normal" not in self._sprites:
            self._sprites["normal"] = self._sprites[list(self._sprites.keys())[0]]
        for expr in self.EXPRESSIONS:
            if expr not in self._sprites:
                self._sprites[expr] = self._sprites["normal"]

        ref_w, ref_h = self._src_size
        self._scale = display_width / ref_w
        ref_pm = self._sprites["normal"]
        ref_w = int(ref_pm.width() * self._scale)
        ref_h = int(ref_pm.height() * self._scale)
        self.width = ref_w
        self.height = ref_h
        self._dw, self._dh = ref_w, ref_h

        self._scaled = {}
        for expr, pm in self._sprites.items():
            scaled = pm.scaled(ref_w, ref_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            canvas = QPixmap(ref_w, ref_h)
            canvas.fill(Qt.transparent)
            cp = QPainter(canvas)
            cp.setRenderHint(QPainter.SmoothPixmapTransform, True)
            x = (ref_w - scaled.width()) // 2
            y = (ref_h - scaled.height()) // 2
            cp.drawPixmap(x, y, scaled)
            cp.end()
            self._scaled[expr] = canvas

        # ---- 动画状态 ----
        self.frame = 0
        self._time = 0.0
        self.on_update = None

        self.expression = "normal"
        self._context_expression = None
        self._ambient_expression = 'normal'
        self._temporary_expression = None
        self._temporary_until = 0.0
        self._prev_expression = "normal"
        self._transition = 1.0
        self._transition_speed = 0.06
        # 常驻动画链：原生片段接续时短暂混合两段画面。
        # 这才是"常态是动态表情"——不是静止贴图 + 偶尔播一段。
        self._idle_chain = True
        self._xfade_pm = None
        self._xfade_t = 1.0
        self._xfade_seconds = 0.16
        self._xfade_y = 0

        # 企鹅动画参数
        self.breath_y = 0.0
        self.body_sway_x = 0.0        # 身体微摆
        self.wing_twitch = 0.0        # 翅膀抖动
        self.eye_open = 1.0
        self.body_squash = 1.0
        self.body_squash_target = 1.0
        self.mouth_open = 0.0

        # 动作叠加
        self.anim_rotate = 0.0
        self.anim_offset = QPointF(0, 0)
        self.anim_scale_x = 1.0
        self.anim_scale_y = 1.0
        self.action = "none"
        self._action_timer = 0.0
        self._action_phase = 0.0
        self._peek_side = 1
        self.head_yaw = self.head_nod = self.head_tilt = 0.0
        self.executor_appearance = 'guga'
        self._executor_caps = {}
        self._hat_anchors = {}
        self.sunglasses = False
        self._sunglasses_until = 0.0
        self._music_stage_active = False
        self._dancing = False
        self.dance_bpm = 110.0

        # Source artwork owns the eyes; only the wings receive idle motion.
        self._wing_twitch_countdown = random.uniform(3.0, 9.0)
        self._wing_twitch_active = False
        self._wing_twitch_duration = 0.0
        self._wing_twitch_phase = 0.0

        # 动画系统：关键帧轨道（驱动身体位移/挤压/旋转，不替换表情）
        # type=track: frames=[(dy, squash, rot)] 参数帧；type=frames: 真实图片帧（GIF表情包）
        self.clips = {}
        self._clip = None
        self._clip_frame = 0.0
        self._clip_started_at = None
        self._idle_rest = None
        self._last_native_pm = None
        self._last_native_y = self._idle_rest_y = 0
        self._resident_pad = (0,0)
        self._last_idle_motion = None
        self._idle_playlist = []
        self._track_dy = 0.0
        self._track_squash = 1.0
        self._track_rot = 0.0
        self._build_tracks()
        self._load_stickers(os.path.join(res_dir, 'stickers'))
        self._load_action_frames(self.LEGACY_DANCE_CLIP,
                                 os.path.join(res_dir, 'actions', 'eyes_shake_dance', 'frames'),
                                 fps=24.0)
        self._load_action_frames(self.DANCE_CLIP,
                                 os.path.join(res_dir, 'actions', 'joy_sway_dance', 'frames'),
                                 fps=25.0)
        self.idle_motion_clips=[]
        # User rejected alternate video identities. Normal operation uses only
        # the original sprite; reference bundles remain available for dev review.
        self._reference_clips_enabled=False
        # These two clips are supplied and explicitly requested by the user for
        # natural idle playback. Keep that scope separate from task-video policy.
        self._idle_motion_enabled=True
        self._load_idle_clips()
        self._load_personality_clips()
        from ui.pet_effects import PetEffects
        self.effects = PetEffects()

        # 常态表情来回切换
        self._idle_expr_countdown = random.uniform(4.0, 9.0)
        self._idle_expr_pick = None
        self._idle_expr_until = 0.0

        # 随机小动作（让企鹅"活"起来）
        self._idle_action_countdown = random.uniform(4.0, 8.0)

        self._timer = QTimer()
        self._timer.setInterval(33)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        self._last_tick=time.monotonic()
        # Idle breathing/sway has a 4s period, so 10fps is visually identical to
        # 20fps while halving the transparent-window repaints on an idle desktop.
        # _tick() clamps dt to 0.12s, so 100ms stays inside the integration budget.
        self._active_interval_ms=16
        # 2026-11-20：原来空闲档 100ms（10fps）。对 4 秒周期的呼吸"理论上"看不出差别，
        # 但配上现在这套高光立绘，10fps 会把缓慢位移切成肉眼可见的台阶——用户说的
        # "普通模式抖动、卡顿"就是这个。25fps 下同样的呼吸是连续的，重绘成本仍很低。
        self._idle_interval_ms=40

    @property
    def _src(self):
        return self._scaled.get(self.expression, self._scaled["normal"])

    def start(self):
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def look_at(self, x, y, nearby=False):
        self._presence.look_at(x, y)
        if nearby and not self._pointer_near and self.expression != 'sleepy':
            self._idle_rest = None
            # The scheduled idle chain can yield to attention; keep explicit actions.
            if self._clip and self._clip.startswith('idle:') and self.action == 'none':
                self.stop_clip()
        self._pointer_near = nearby

    def set_dragging(self, active, speed=0.0):
        self._being_dragged=bool(active)
        if active:self._idle_rest=None
        if active and (self._clip or '').startswith('task:'):self.stop_clip()
        self._presence.set_dragging(active, max(-85.,min(85.,speed)) if active else 0.)
        if active and (self._clip or '').startswith('idle:'):self.stop_clip()
        if active and (self._clip or '').startswith('personality:'):self.stop_clip()

    def set_walking(self, active, direction=1):
        self._walking=bool(active)
        self._walk_direction=1 if direction>=0 else -1
        if active:
            self._idle_rest=None
            if (self._clip or '').startswith(('idle:','personality:')):self.stop_clip()
            self.play_personality('walk')
        elif self._clip=='personality:walk':self.stop_clip(natural=True)

    def _face_frame(self):
        if self._face_details is None:
            from ui.pet_face import FaceDetails
            self._face_details = FaceDetails(self._res_dir, self._dw, self._dh)
        mouth = self._mouth_pose if self._speaking else 0.
        return self._face_details.frame(self.expression, self.eye_open, mouth, self._gaze, self._speaking)

    def set_work_state(self,state,task_id='main'):
        if state in ('thinking','working','waiting') and self.on_work_started:self.on_work_started()
        if state in ('thinking','working','waiting') and self._walking:self.set_walking(False)
        if state in ('thinking','working','waiting') and not self._task_motion.jobs:self._last_task_variant=None
        if state in ('thinking','working','waiting'):self._idle_rest=None
        if state in ('thinking','working','waiting','result','success','error') and (self._clip or '').startswith('idle:'):
            self.stop_clip()
        applied=self._task_motion.update(state,task_id)
        if state in ('idle','cancelled','success','error','result') and not self._task_motion.jobs:
            self._hand_releasing=True
            if state in ('idle','cancelled') and (self._clip or '').startswith('task:'):self.stop_clip()
            if self._clip=='personality:work':self.stop_clip(natural=True)
        return applied

    def set_speaking(self,active):
        self._speaking=bool(active)
        self.eye_open=1.0
        if active:self._idle_rest=None
        if active:self._hand_releasing=True
        if active and ((self._clip or '').split(':',1)[0] in ('task','idle') or self._clip=='personality:work'):self.stop_clip()

    def set_music_stage(self,active):
        """Singing latches the accessory until the real audio state ends."""
        self._music_stage_active=bool(active)
        if active:self._idle_rest=None
        if 'personality:sing' in self.clips:
            self.sunglasses=False
            if active:
                if self._clip!='personality:sing':self.play_personality('sing')
            elif self._clip=='personality:sing':self.stop_clip(natural=True)
            return
        if active:
            if (self._clip or '').startswith('idle:'):self.stop_clip()
            self.sunglasses=True
            self._sunglasses_until=0.
        else:
            self.sunglasses=False

    def play_idle_motion(self,name):
        clip='idle:'+str(name)
        if name not in {n for n,_ in self.idle_motion_clips} or clip not in self.clips:return False
        if self._idle_motion_blocked():return False
        self._dancing=False;self.action='none'
        self.play_clip(clip,loop=False)
        return self._clip==clip

    def _load_personality_clips(self):
        import json
        from pathlib import Path
        base=Path(self._res_dir)/'actions/personality-20261114'
        try:
            manifest=json.loads((base/'manifest.json').read_text(encoding='utf-8'))
            for name in ('entrance','poke','sing'):
                spec=manifest['clips'][name]
                folder=(base/spec['frames']).resolve()
                if not folder.is_relative_to(base.resolve()):continue
                fps=float(spec['fps'])
                if not 20<=fps<=60:continue
                self._load_action_frames('personality:'+name,str(folder),fps,lazy=True,
                                         reference_width=manifest['reference_width'], placement=spec.get('placement'))
        except (OSError,ValueError,TypeError,KeyError):return
        base=Path(self._res_dir)/'actions/task-walk-20261116'
        try:
            manifest=json.loads((base/'manifest.json').read_text(encoding='utf-8'))
            for name in ('work','walk'):
                spec=manifest['clips'][name];folder=(base/spec['frames']).resolve()
                fps=float(spec['fps'])
                if not folder.is_relative_to(base.resolve()) or not 20<=fps<=60:continue
                self._load_action_frames('personality:'+name,str(folder),fps,lazy=True,
                                         reference_width=manifest['reference_width'], placement=spec.get('placement'))
        except (OSError,ValueError,TypeError,KeyError):pass

    def play_personality(self,name):
        """Explicit events own the source pose until finished or dragged."""
        clip='personality:'+name
        if clip not in self.clips or self._being_dragged:return False
        if self.expression=='sleepy' and name!='entrance':return False
        if name in ('work','walk') and (self._music_stage_active or self._speaking):return False
        self.action='none';self._dancing=False
        self.play_clip(clip,loop=name=='sing')
        return self._clip==clip

    def _idle_motion_blocked(self):
        return (self._speaking or self._being_dragged or self._walking or self._music_stage_active or self.dancing or
            self.mouth_open>.05 or self._task_motion.active or self.expression=='sleepy' or
            self._context_expression not in (None,'happy') or self._temporary_expression is not None)

    def _load_task_clips(self):
        """A source manifest may only point to reviewed native-rate PNG bundles."""
        import json
        from pathlib import Path
        base=Path(self._res_dir)/'actions/task-motion'
        try:
            manifest=json.loads((base/'manifest.json').read_text(encoding='utf-8'))
            loaded={}
            for state,spec in manifest.get('states',{}).items():
                if state not in self._task_motion.GESTURES:continue
                folder=(base/spec['frames']).resolve();fps=float(spec['native_fps'])
                if not folder.is_relative_to(base.resolve()) or not 20<=fps<=60:continue
                name='task:'+state;key=(str(folder),fps)
                if key in loaded:
                    # State aliases share decoded pixels, not mutable playback flags.
                    self.clips[name]=dict(self.clips[loaded[key]])
                else:
                    self._load_action_frames(name,str(folder),fps, placement=spec.get('placement'))
                    if name in self.clips:loaded[key]=name
        except (OSError,ValueError,TypeError,KeyError):return

    def _load_idle_clips(self,base=None):
        import json
        from pathlib import Path
        if base is None:
            base=Path(self._res_dir)/'actions/idle-motion'
            candidate=Path(self._res_dir)/'actions/resident-motion-v2'
            if (candidate/'manifest.json').is_file():base=candidate
        base=Path(base)
        try:
            manifest=json.loads((base/'manifest.json').read_text(encoding='utf-8'))
            for spec in manifest.get('clips',[]):
                name=str(spec.get('name','')).strip();folder=(base/spec['frames']).resolve();fps=float(spec['native_fps'])
                if not name or not folder.is_relative_to(base.resolve()) or not 20<=fps<=60:continue
                if 'idle:' + name in self.clips:continue
                clip='idle:'+name;self._load_action_frames(clip,str(folder),fps,lazy=True,
                    reference_width=spec.get('reference_width'), placement=spec.get('placement'))
                if clip in self.clips:self.idle_motion_clips.append((name,max(.01,float(spec.get('weight',1)))))
            if manifest.get('padded_canvas'):
                # 以前是"少于 4 个片段就整组丢弃、退回 idle-motion（245×352、无扩边、按适配盒缩放）"，
                # 于是通过审核的高质量素材一个都没用上，看起来就跟静态立绘不是同一图层。
                # 现在按真正通过审核的片段来用，扩边与 reference_width 口径保持不变。
                if not self.idle_motion_clips:
                    self._load_idle_clips(Path(self._res_dir)/'actions/idle-motion')
                    return
                self._resident_pad=(round(self._dw*40/245),round(self._dw*60/245))
                self.width=self._dw+2*self._resident_pad[0]
                self.height=self._dh+self._resident_pad[1]+round(self._dw*12/245)
                self._load_idle_clips(Path(self._res_dir)/'actions/idle-motion')
        except (OSError,ValueError,TypeError,KeyError):return

    def _tick_task_motion(self):
        blocked=(self.expression=='sleepy' or self._context_expression not in (None,'happy') or
                 self.dancing or self._speaking or self.mouth_open>.05 or self._being_dragged or
                 self.action!='none' or self._clip not in (None,'idle','talk'))
        event=self._task_motion.poll(blocked)
        if event:
            if event['state'] in ('thinking','working','waiting') and 'personality:work' in self.clips:
                # A whole source performance, then a different short gesture.
                # The board appears for real work/drawing, never random success.
                choices=['work','tilt','nod','peek'] if event['state']!='waiting' else ['peek','tilt']
                if self._last_task_variant=='work':choices=[x for x in choices if x!='work']
                variant='work' if self._last_task_variant is None and 'work' in choices else random.choice(choices)
                self._last_task_variant=variant
                if variant=='work':self.play_personality('work')
                else:self.trigger_action(variant)
                return
            clip='task:'+event['state']
            if self._reference_clips_enabled and clip in self.clips:self.play_clip(clip,loop=False)
            else:self.trigger_action({'thinking':'tilt','waiting':'peek'}.get(event['state'],event['gesture']))

    def task_animation_status(self):
        return dict(version='2026-11-08.resident-motion.1',active_clip=self._clip,
                    frame=int(self._clip_frame),director=self._task_motion.snapshot(),
                    personality=dict(version='2026-11-14.personality.1',effects=self.effects.enabled,
                        loaded={name.split(':',1)[1]:dict(frames=len(clip['frames']),fps=clip['fps'],
                            loop=bool(clip['loop'])) for name,clip in self.clips.items()
                            if name.startswith('personality:')}),
                    loaded={name[5:]:dict(frames=len(clip['frames']),fps=clip['fps'],
                                        one_shot=bool(clip.get('one_shot_only')))
                            for name,clip in self.clips.items() if name.startswith('task:')},
                    idle_motion=dict(active=self._clip if (self._clip or '').startswith('idle:') else None,
                        settled_pose=self._idle_rest is not None,canvas=[self.width,self.height],
                        body_canvas=[self._dw,self._dh],padding=list(self._resident_pad),
                        loaded={name:dict(frames=len(self.clips['idle:'+name]['frames']),fps=self.clips['idle:'+name]['fps'],
                                         one_shot=bool(self.clips['idle:'+name].get('one_shot_only')))
                                for name,_ in self.idle_motion_clips if 'idle:'+name in self.clips}))

    def tap(self, amount=.92):
        # The short compression must start immediately, even while a longer clip starts.
        self._tap_elapsed = 0.
        self._tap_amount = max(.88, min(.98, amount))
        if self.play_personality('poke'):return
        if (self._clip or '').startswith('idle:'):self.stop_clip()
        self._tap_elapsed = 0.
        self._tap_amount = max(.88, min(.98, amount))

    def set_expression(self, expr: str):
        """An intentional expression persists until the next explicit choice.

        Idle expressions and mood updates must never undo a model/user choice.
        Explicit `normal` releases other held expressions; sleepy requires
        user_interaction() first.
        """
        if expr not in self.EXPRESSIONS:
            return
        self._idle_rest=None
        if expr not in ('normal','happy') and (self._clip or '').startswith('idle:'):self.stop_clip()
        # Sleep is latched: autonomous replies, gestures and stale callbacks
        # cannot wake the pet. Only an actual user input releases this state.
        if self._context_expression == 'sleepy' and expr != 'sleepy':
            return
        if expr not in ('normal','happy') and (self._clip or '').startswith('task:'):self.stop_clip()
        self._context_expression = None if expr == 'normal' else expr
        if expr == 'normal': self._ambient_expression = 'normal'
        self._temporary_expression = None
        self._temporary_until = 0.0
        self._display_expression(expr)

    def user_interaction(self):
        """Wake on user speech/message/click, never on pointer tracking."""
        if self.expression != 'sleepy':
            return False
        self._context_expression = None
        self.set_expression('happy')
        return True

    def _display_expression(self, expr):
        if expr not in self.EXPRESSIONS or expr == self.expression:
            return
        self._idle_rest=None
        self._prev_expression = self.expression
        self.expression = expr
        self._transition = 0.0

    def temporary_expression(self, expr, seconds=2.0):
        if expr not in self.EXPRESSIONS or self._context_expression not in (None, 'happy'):
            return False
        self._temporary_expression = expr
        self._temporary_until = self._time + max(.1,float(seconds))
        self._display_expression(expr)
        return True

    def _restore_expression(self):
        self._temporary_expression = None
        self._temporary_until = 0.0
        self._display_expression(self._context_expression or self._ambient_expression)

    def _set_expression_fast(self, expr: str):
        """立即切换表情（不做 blend）——用于说话时的口型闪烁"""
        if expr not in self.EXPRESSIONS or expr == self.expression:
            return
        self.set_expression(expr)
        self._transition = 1.0

    def update_mood(self, mood_emoji: str):
        mapping = {
            '😄': 'happy', '😊': 'happy', '😴': 'sleepy',
            '😢': 'sad', '😕': 'sad',
        }
        self._ambient_expression = mapping.get(mood_emoji, 'normal')
        if self._context_expression is None and self._temporary_expression is None:
            self._display_expression(self._ambient_expression)

    def trigger_bounce(self):
        self.body_squash_target = 0.86

    def set_talking(self, talking: bool):
        self.set_speaking(talking)

    # ==================== 动画关键帧轨道 ====================

    def _build_tracks(self):
        """身体动画轨道（只驱动位移/挤压/旋转，表情由 expression 决定）"""
        # (dy 位移, squash 挤压(1=正常), rot 旋转)
        self.clips['idle'] = {'type': 'track', 'loop': True, 'fps': 6.0,
                              'frames': [(-1, 1.02, 0), (-3, 1.0, 0), (-2, 0.985, 0), (-1, 1.0, 0)]}
        self.clips['talk'] = {'type': 'track', 'loop': True, 'fps': 8.0,
                              # 极轻微脉动（几乎不可见，仅示意"在说话"）
                              'frames': [(0, 1.0, 0), (-1, 1.006, 0), (0, 1.0, 0), (-1, 1.006, 0)]}
        self.clips['jump'] = {'type': 'track', 'loop': False, 'fps': 15.0,
                              'frames': [(0, 1.04, 0), (-8, 0.95, 0), (-14, 0.9, 0),
                                         (-8, 0.95, 0), (0, 1.0, 0), (2, 1.08, 0)]}
        self.clips['wave'] = {'type': 'track', 'loop': False, 'fps': 8.0,
                              'frames': [(0, 1.0, -6), (0, 1.0, 0), (0, 1.0, 6), (0, 1.0, 0)]}

    def _start_next_idle_motion(self):
        """Play every available motion before reshuffling the idle sequence."""
        if not self._idle_playlist:
            self._idle_playlist = [name for name, _ in self.idle_motion_clips]
            random.shuffle(self._idle_playlist)
            if len(self._idle_playlist) > 1 and self._idle_playlist[-1] == self._last_idle_motion:
                self._idle_playlist[0], self._idle_playlist[-1] = self._idle_playlist[-1], self._idle_playlist[0]
        if self._idle_playlist:
            self.play_clip('idle:' + self._idle_playlist.pop(), loop=False)

    def play_clip(self, name, loop=None):
        if name.startswith('task:') and not self._reference_clips_enabled:return
        if name.startswith('idle:') and not self._idle_motion_enabled:return
        if name not in self.clips:
            return
        if name.startswith(('idle:', 'personality:', 'task:')):
            if name.startswith('idle:') and self._idle_motion_blocked():return
            if name != 'personality:entrance':
                if self._last_native_pm is not None:
                    self._fade_from(self._last_native_pm, self._last_native_y)
                elif self._idle_rest is not None:
                    self._fade_from(self._idle_rest, self._idle_rest_y)
                elif self._xfade_pm is None:
                    self._fade_from(self._src, 0)
            if name.startswith('idle:'):self._last_idle_motion=name[5:]
            self._idle_rest=None
            self._last_native_pm=None
        self._track_dy=0.;self._track_squash=1.;self._track_rot=0.
        if loop is not None:
            self.clips[name]['loop'] = bool(loop) and not self.clips[name].get('one_shot_only',False)
        self._clip = name
        self._clip_frame = 0.0
        self._clip_started_at = time.monotonic()

    def stop_clip(self, natural=False):
        name = self._clip or ''
        if name.startswith(('idle:', 'personality:', 'task:')):
            self._natural_pause = 0.0
            clip = self.clips[name]
            frame = self._last_native_pm
            if frame is None:
                frames = clip['frames']
                frame = frames[min(int(self._clip_frame), len(frames) - 1)]
            frame_y = self._native_y(clip, frame)
            self._idle_rest = None
            if natural and name.startswith('idle:'):
                self._idle_rest = frame
                self._idle_rest_y = frame_y
            else:
                self._fade_from(frame, frame_y)
            self._last_native_pm = None
        self._clip = None
        self._clip_frame = 0.0
        self._clip_started_at = None
        self._track_dy = 0.0
        self._track_squash = 1.0
        self._track_rot = 0.0

    @property
    def active_clip(self):
        return self._clip

    def _load_stickers(self, stickers_dir):
        """GIF 表情包 → 剪辑（名称为 sticker:<文件名>）"""
        if not os.path.isdir(stickers_dir):
            return
        try:
            from PIL import Image as PILImage
        except ImportError:
            return
        for f in sorted(os.listdir(stickers_dir)):
            if not f.lower().endswith('.gif'):
                continue
            try:
                im = PILImage.open(os.path.join(stickers_dir, f))
                frames = []
                idx = 0
                while True:
                    im.seek(idx)
                    frames.append(self._pil_to_pixmap(im.convert('RGBA')))
                    idx += 1
                    try:
                        im.seek(idx)
                    except EOFError:
                        break
                if frames:
                    self.clips[f'sticker:{os.path.splitext(f)[0]}'] = {
                        'type': 'frames', 'frames': frames, 'fps': 12.0, 'loop': True,
                    }
            except Exception:
                continue

    def _load_action_frames(self, name, frames_dir, fps=15.0, lazy=False, reference_width=None, placement=None):
        """Load a transparent PNG animation without GIF palette loss."""
        if not os.path.isdir(frames_dir):
            return
        from core.avatar_review import approved_action
        if not approved_action(frames_dir):
            return
        try:
            from PIL import Image as PILImage
        except ImportError:
            return
        paths=[os.path.join(frames_dir,filename) for filename in sorted(os.listdir(frames_dir))
               if filename.lower().endswith('.png')]
        scale = self._dh / float(placement['body_height']) if placement else None

        def load_path(path):
            with PILImage.open(path) as source:
                frame = source.convert('RGBA')
                if placement and 'neck_y' in placement:
                    frame = self._align_native_body(frame, placement, scale)
                    return self._pil_to_pixmap(frame, scale=1.0)
                return self._pil_to_pixmap(frame, reference_width=reference_width, scale=scale)
        if lazy:
            frames=LazyFrameSequence(paths,load_path)
        else:
            frames=[]
            for path in paths:
                try:frames.append(load_path(path))
                except Exception:continue
        if frames:
            import json
            from pathlib import Path
            review=json.loads((Path(frames_dir).parent/'visual-review.json').read_text(encoding='utf-8'))
            once=review.get('playback')=='once'
            self.clips[name] = {'type': 'frames', 'frames': frames,
                                'fps': float(fps), 'loop': not once,'one_shot_only':once}

            if placement:
                self.clips[name]['ground_y'] = round(placement['ground_y'] * scale)

    def _align_native_body(self, frame, placement, scale):
        from PIL import Image
        top = placement['head_top']
        neck = placement['neck_y']
        ground = placement['ground_y']
        # Keep the hood crown and feet on their shared marks. The blackboard
        # take has a longer head, so match the collar before playing the take.
        head_scale = self._dh * placement['neck_fraction'] / (neck - top)
        lower_scale = self._dh * (1 - placement['neck_fraction']) / (ground - neck)
        width = round(frame.width * scale * placement.get('width_factor', 1.0))
        ground_y = round(ground * scale)
        head_h = round(neck * head_scale)
        lower_h = round((frame.height - neck) * lower_scale)
        offset = ground_y - round((ground - neck) * lower_scale) - head_h
        canvas = Image.new('RGBA', (width, offset + head_h + lower_h))
        head = frame.crop((0, 0, frame.width, neck)).resize((width, head_h), Image.Resampling.BICUBIC)
        body = frame.crop((0, neck, frame.width, frame.height)).resize(
            (width, lower_h), Image.Resampling.BICUBIC)
        canvas.paste(head, (0, offset))
        canvas.paste(body, (0, offset + head_h))
        return canvas

    def _pil_to_pixmap(self, pil_img, reference_width=None, scale=None):
        from PyQt5.QtGui import QImage
        data = pil_img.tobytes('raw', 'RGBA')
        qimg = QImage(data, pil_img.width, pil_img.height, QImage.Format_RGBA8888)
        pm = QPixmap.fromImage(qimg)
        if scale is None and reference_width:
            scale = self._dw / float(reference_width)
        if scale is not None:
            return pm.scaled(round(pm.width()*scale),round(pm.height()*scale),
                Qt.KeepAspectRatio,Qt.SmoothTransformation)
        scale = min(self._dw / max(1, pm.width()), self._dh / max(1, pm.height()))
        return pm.scaled(int(pm.width() * scale), int(pm.height() * scale),
                         Qt.KeepAspectRatio, Qt.SmoothTransformation)

    def play_sticker(self, name):
        self.play_clip(f'sticker:{name}')

    @property
    def sticker_names(self):
        return [n[8:] for n in self.clips if n.startswith('sticker:')]

    @property
    def dancing(self):
        return self._dancing

    @dancing.setter
    def dancing(self, active):
        self._dancing = bool(active)
        if self._dancing and self.DANCE_CLIP in self.clips:
            self.action = 'none'
            if self._clip != self.DANCE_CLIP:
                self.play_clip(self.DANCE_CLIP, loop=True)
        elif not self._dancing and self._clip == self.DANCE_CLIP:
            self.stop_clip()

    # ==================== 动作控制 ====================

    def trigger_action(self, action: str):
        if (self._clip or '').startswith('personality:'):return
        from core.embodiment import ALIASES
        action=ALIASES.get(action,action)
        if action!='none' and (self._clip or '').startswith('idle:'):self.stop_clip()
        if action.startswith('idle_') and action[5:] in {n for n,_ in self.idle_motion_clips}:
            if self.expression!='sleepy' and not self._speaking and not self._being_dragged:
                self.play_idle_motion(action[5:])
            return
        if action=='look_up_step':
            if not self._reference_clips_enabled:
                self.trigger_action('tilt');return
            if self.expression!='sleepy' and not self._speaking and not self._being_dragged and not self.dancing:
                self.action='none';self.play_clip('task:thinking',loop=False)
            return
        if action in ('sunglasses','remove_sunglasses'):
            self.sunglasses=action=='sunglasses'
            self._sunglasses_until=self._time+8 if self.sunglasses else 0
            return
        if action in ('dance','stop_dance'):
            self.dancing=action=='dance'
            if not self.dancing:self.stop_clip()
            return
        if action in self.ACTIONS and action != "none":
            self.action = action
            self._action_timer = 0.0
            self._action_phase = 0.0
            if action in ('think_chin','scratch_head'):
                self._hand_blend=0.;self._hand_releasing=False
                self.temporary_expression('surprised',3.4)
                # These source expressions have slightly different silhouettes;
                # avoid ghosting the whole body during an articulated gesture.
                self._transition=1.
            if action == "bounce":
                self.body_squash_target = 0.86
                self.play_clip('jump', loop=False)   # 跳跃真动画
            if action == "peek":
                self._peek_side = random.choice([-1, 1])

    def _tick(self):
        now=time.monotonic();dt=max(.001,min(.12,now-self._last_tick));self._last_tick=now
        # 本帧是否有片段自然放完。自然结束的那一帧必须先把结算姿态显示出来，
        # 不能在同一帧里又被常驻动画链接上下一段，否则 _idle_rest 永远看不到。
        self._clip_just_ended=False
        self._tap_elapsed += dt
        self.frame += 1
        t = self._time = self._time + dt
        if self._temporary_expression is not None and (
                t >= self._temporary_until or self.mouth_open > .05):
            self._restore_expression()

        # ---- 呼吸（圆滚滚企鹅，起伏小）----
        breath = math.sin(t * 2 * math.pi / 4.0) * 0.75
        self.breath_y = -breath * 1.5

        # ---- 身体微摆 ----
        self.body_sway_x = math.sin(t * 0.7 + 0.4) * 1.1 + math.sin(t * 2.2 + 1.3) * 0.25

        # ---- 翅膀抖动 ----
        self._wing_twitch_countdown -= dt
        if self._wing_twitch_countdown <= 0 and not self._wing_twitch_active:
            self._wing_twitch_active = True
            self._wing_twitch_phase = 0.0
            self._wing_twitch_duration = random.uniform(0.25, 0.5)
            self._wing_twitch_countdown = random.uniform(5.0, 12.0)
        if self._wing_twitch_active:
            self._wing_twitch_phase += dt
            if self._wing_twitch_phase >= self._wing_twitch_duration:
                self._wing_twitch_active = False
                self.wing_twitch = 0.0
            else:
                env = math.sin(self._wing_twitch_phase / self._wing_twitch_duration * math.pi)
                self.wing_twitch = env * abs(math.sin(self._wing_twitch_phase * 26))

        # ---- 表情过渡 ----
        if self._transition < 1.0:
            t_ease = 1 - (1 - self._transition) ** 3
            self._transition = min(1.0, self._transition + (self._transition_speed + t_ease * 0.02)*dt*30)

        # 常驻片段的交叉淡入进度（250ms）
        if self._xfade_t < 1.0:
            self._xfade_t = min(1.0, self._xfade_t + dt / max(0.05, self._xfade_seconds))
            if self._xfade_t >= 1.0:
                self._xfade_pm = None

        # Close quickly and reopen a little more slowly. Native sequences retain their own eyes.
        blink_age = t - self._blink_at
        self.eye_open = 1.
        if 0 <= blink_age < .18:
            self.eye_open = min(1., abs(blink_age - .062) / (.067 if blink_age < .062 else .118))
        elif blink_age >= .18:
            self._blink_at = t + random.uniform(2.5, 6.2)
        mouth_target = self.mouth_open if self._speaking else 0.
        self._mouth_pose += (mouth_target - self._mouth_pose) * (1 - math.exp(-dt * 28))

        # ---- 身体挤压回弹 ----
        if self.body_squash < self.body_squash_target:
            self.body_squash = min(self.body_squash_target, self.body_squash + .9*dt)
        elif self.body_squash > self.body_squash_target:
            self.body_squash = max(self.body_squash_target, self.body_squash - .75*dt)
        if self.body_squash == self.body_squash_target and self.body_squash_target < 1.0:
            self.body_squash_target = 1.0

        # ---- 动作动画 ----
        self._tick_task_motion()
        self._tick_action(dt)
        # A stale TTS lip-sync frame must not suppress idle videos forever.
        if not self._speaking and self.mouth_open>.05:
            self._stale_mouth_frames=getattr(self,'_stale_mouth_frames',0)+1
            self._stale_mouth_seconds=getattr(self,'_stale_mouth_seconds',0.)+dt
            if self._stale_mouth_frames>=60 or self._stale_mouth_seconds>=2.0:
                self.mouth_open=0.;self._stale_mouth_frames=0;self._stale_mouth_seconds=0.
                if self._clip=='talk':self.stop_clip()
        else:self._stale_mouth_frames=0;self._stale_mouth_seconds=0.
        presence = self._presence.advance(dt)
        self._gaze = (presence.head_yaw / 18., presence.head_pitch / 9.)
        if self._walking:self._walk_phase+=dt*2.15*math.tau
        self.head_yaw += presence.head_yaw / 36
        self.head_nod += presence.head_pitch / 24
        self.anim_rotate += presence.roll
        self.head_tilt += presence.head_roll / 24

        # ---- 动画剪辑帧推进 ----
        if self._clip is not None:
            c = self.clips[self._clip]
            if (c.get('one_shot_only') or self._clip.startswith('personality:')) and self._clip_started_at is not None:
                self._clip_frame = max(0.,time.monotonic()-self._clip_started_at)*c['fps']
            else:self._clip_frame += dt * c['fps']
            n = len(c['frames'])
            if self._clip_frame >= n:
                if c['loop']:
                    self._clip_frame %= n
                else:
                    self.stop_clip(natural=True)
                    self._clip_just_ended=True
            if c.get('type') == 'track':
                idx = min(int(self._clip_frame), n - 1)
                dy, sq, rt = c['frames'][idx]
                self._track_dy, self._track_squash, self._track_rot = dy, sq, rt
        else:
            self._track_dy = 0.0
            self._track_squash = 1.0
            self._track_rot = 0.0

        # 说话 → 表情动作完全不变，只保留极轻微的身体脉动（talk 轨道已调至几乎不可见）
        if (self._music_stage_active and not self._being_dragged and
                self._clip in (None,'idle','talk') and not self._clip_just_ended):
            self.play_personality('sing')
        if self._clip is None:
            if self.mouth_open > 0.05:
                self.play_clip('talk', loop=True)
            elif self.action == 'none':
                self.play_clip('idle', loop=True)

        # ---- 常态表情来回切换（空闲时偶尔换脸再换回来）----
        idle_chain = self._idle_chain and self._idle_motion_enabled and bool(self.idle_motion_clips)
        if (not (self.natural_actions and idle_chain) and not self._task_motion.active
                and self.action == "none" and self.mouth_open <= 0.05
                and self._clip in (None, 'idle') and self._context_expression in (None, 'happy')):
            self._idle_expr_countdown -= dt
            if self._idle_expr_countdown <= 0:
                self._idle_expr_countdown = random.uniform(6.0, 14.0)
                self.temporary_expression(random.choice(['playful', 'surprised', 'happy']),
                                          random.uniform(1.0, 2.2))

        available = not self._idle_motion_blocked() and self.action == 'none'
        if available and self.natural_actions and (
                self._clip in (None, 'idle') or (self._clip or '').startswith('idle:')):
            self._walk_countdown -= dt
            if self._clip in (None, 'idle'):
                self._natural_pause -= dt
                if self.auto_walk and self._walk_countdown <= 0 and self.on_walk_requested:
                    self._walk_countdown = random.uniform(40., 90.)
                    self.on_walk_requested()
        # ---- 常驻自然动画 ----
        if (not self._task_motion.active and self.expression != 'sleepy' and
                self._context_expression in (None,'happy') and self._temporary_expression is None and
                self.action == "none" and not self._idle_motion_blocked() and self._clip in (None, 'idle')
                and not self._clip_just_ended and not self._pointer_near):
            if self.natural_actions and self._natural_pause <= 0 and idle_chain:
                self._start_next_idle_motion()

        # 随机小动作：空闲时偶尔蹦一下/偷看（保持动态）
        if (self.natural_actions and not idle_chain and not self._task_motion.active and self.expression != 'sleepy' and
                self._context_expression in (None,'happy') and self._temporary_expression is None and
                self.action == "none" and not self._idle_motion_blocked() and self._clip in (None, 'idle')):
            self._idle_action_countdown -= dt
            if self._idle_action_countdown <= 0:
                self._idle_action_countdown = random.uniform(12.0, 24.0)
                r = random.random()
                if r < .55 and self.idle_motion_clips:
                    choices=[(name,weight) for name,weight in self.idle_motion_clips if name!=self._last_idle_motion]
                    if not choices:choices=self.idle_motion_clips
                    names=[name for name,_ in choices];weights=[weight for _,weight in choices]
                    self.play_clip('idle:'+random.choices(names,weights=weights,k=1)[0],loop=False)
                elif r < 0.72:
                    self.trigger_action('bounce')
                elif r < 0.82:
                    self.trigger_action('peek')
                elif r < 0.90:
                    self.trigger_action('tsundere')
                elif self.DANCE_CLIP in self.clips:
                    # Occasional one full choreography, then return to normal
                    # instead of leaving an unattended pet dancing forever.
                    self.play_clip(self.DANCE_CLIP, loop=False)

        # 原生序列帧（24fps 的 idle/task 片段、GIF 表情、舞蹈）必须按活跃帧率跑。
        # 10fps 的空闲档只适合程序化的 6fps 呼吸轨；套在 24fps 素材上会丢掉一半多的帧，
        # 看起来就是"自然状态比动图卡、还抖"。
        native_clip = bool((self.clips.get(self._clip or '') or {}).get('type') == 'frames')
        energetic=bool(self._xfade_t < 1.0 or self._walking or self._speaking or self.mouth_open>.05 or self.action!='none' or self.dancing or
            self._task_motion.active or self._temporary_expression is not None or self._being_dragged or
            self._transition<1 or self._clip not in (None,'idle') or self._tap_elapsed<1.0 or native_clip)
        interactive = self._pointer_near or max(map(abs, self._gaze)) > .025 or t >= self._blink_at - .14
        target = self._active_interval_ms if energetic or interactive else self._idle_interval_ms
        if self._timer.interval()!=target:self._timer.setInterval(target)
        if self.on_update:
            self.on_update()

    def _tick_action(self,dt=1/30):
        self._action_timer += dt
        self._action_phase += dt
        a = self.action
        self.anim_rotate = 0.0
        self.anim_offset = QPointF(0, 0)
        self.anim_scale_x = 1.0
        self.anim_scale_y = 1.0
        self._point_active = 0.0
        self.head_yaw = self.head_nod = self.head_tilt = 0.0
        if self._sunglasses_until and self._time>=self._sunglasses_until:
            self.sunglasses=False;self._sunglasses_until=0
        if self._music_stage_active and 'personality:sing' not in self.clips:
            self.sunglasses=True;self._sunglasses_until=0.

        if self.dancing and a=='none' and self._clip != self.DANCE_CLIP:
            beat=self._time*self.dance_bpm/60*math.pi*2
            # Temporary original-art sway, not advertised as full choreography.
            self.anim_rotate=2.5*math.sin(beat/2)
            self.anim_offset=QPointF(self._dw*.008*math.sin(beat/2),0)
            return

        if a in ('spin','sneak_peek'):
            duration=1.8 if a=='spin' else 2.6
            phase=min(1,self._action_timer/duration)
            if phase>=1:self.action='none';return
            envelope=math.sin(math.pi*phase)
            if a=='spin':
                angle=phase*math.pi*2
                self.anim_scale_x=math.copysign(max(.14,abs(math.cos(angle))),math.cos(angle))
                self.anim_offset=QPointF(math.sin(angle)*self._dw*.035,-envelope*self._dh*.025)
                self.head_yaw=.35*math.sin(angle)
            else:
                self.anim_offset=QPointF(self._dw*.09*envelope,self._dh*.035*envelope)
                self.head_yaw=.8*envelope;self.head_tilt=-.35*envelope
            return

        if a == "none":
            return

        if a in ('think_chin','scratch_head'):
            blocked=(self._speaking or self._being_dragged or self.expression=='sleepy' or self.dancing)
            target=0. if self._hand_releasing or blocked or self._action_timer>2.6 else 1.
            self._hand_blend+=(target-self._hand_blend)*(1-math.exp(-9*dt))
            self.head_tilt=.14*self._hand_blend
            if target==0 and self._hand_blend<.008:
                self.action='none';self._hand_blend=0.
                if self._temporary_expression:
                    self._restore_expression();self._transition=1.
            return

        if a in ('head_shake','nod','tilt','wave'):
            duration=1.35
            phase=min(1,self._action_timer/duration)
            if phase>=1:
                self.action='none';return
            envelope=math.sin(math.pi*phase)**.7
            if a=='head_shake':self.head_yaw=math.sin(phase*math.pi*5)*envelope
            elif a=='nod':self.head_nod=math.sin(phase*math.pi*4)*envelope
            elif a=='tilt':self.head_tilt=envelope
            elif a=='wave':
                self.anim_rotate=3*math.sin(phase*math.pi*6)*envelope
                self.anim_offset=QPointF(0,-2*envelope)
            return

        if a == "bounce":
            if self._action_timer > 0.45:
                self.action = "none"
                return
            pr = math.sin(self._action_phase * math.pi / 0.45)
            self.anim_offset = QPointF(0, -9 * pr)
            return

        if a == "shake":
            if self._action_timer > 0.5:
                self.action = "none"
                return
            amp = 5 * (1 - self._action_timer / 0.5)
            self.anim_offset = QPointF(amp * math.sin(self._action_phase * math.pi * 5), 0)
            self.anim_rotate = 2 * math.sin(self._action_phase * math.pi * 5)
            return

        if a == "tsundere":  # 傲娇摇头：慢摆 + 昂头
            dur = 1.4
            if self._action_timer > dur:
                self.action = "none"
                return
            amp = 4.5 * math.sin(self._action_phase / dur * math.pi)
            self.anim_rotate = -2.5 + amp * math.sin(self._action_phase * math.pi * 2.2)
            self.anim_offset = QPointF(0, -1.5)
            return

        if a == "point":  # 指点江山：前倾 + 指点手臂
            dur = 1.6
            if self._action_timer > dur:
                self.action = "none"
                return
            env = math.sin(self._action_timer / dur * math.pi)
            self.anim_rotate = 5 * env
            self.anim_offset = QPointF(0, -2 * env)
            self._point_active = env
            return

        if a == "peek":  # 偷偷看我：轻轻侧移探头偷看（不歪斜不压扁）
            dur = 1.1
            if self._action_timer > dur:
                self.action = "none"
                return
            side = self._peek_side
            if self._action_timer < 0.3:
                k = self._action_timer / 0.3
            elif self._action_timer > dur - 0.3:
                k = (dur - self._action_timer) / 0.3
            else:
                k = 1.0
            # 只做轻微横移 + 2.5 度内小倾（像探头偷看），保持正常比例
            self.anim_scale_x = 1.0
            self.anim_scale_y = 1.0
            self.anim_offset = QPointF(side * self._dw * 0.12 * k, -self._dh * 0.03 * k)
            self.anim_rotate = side * 2.5 * k
            return

        self.action = "none"

    @property
    def point_active(self):
        return getattr(self, "_point_active", 0.0)

    # ==================== 绘制 ====================

    def draw(self, p: QPainter, x: int, y: int):
        clip=self._clip or ''
        kind=clip.split(':',1)[1] if clip.startswith('personality:') else (
            'heart' if clip=='idle:heart' else ('bounce' if self.action=='bounce' else ''))
        age=self._clip_frame/self.clips[clip]['fps'] if clip in self.clips else self._action_timer
        fx=x+self._resident_pad[0]+self._dw/2
        fy=y+self._resident_pad[1]+self._dh
        self.effects.draw(p,kind,age,self._dw,self._dh,fx,fy,behind=True)
        self._draw_character(p,x,y)
        self.effects.draw(p,kind,age,self._dw,self._dh,fx,fy)

    def _native_y(self, clip, frame):
        return self._dh - clip.get('ground_y', frame.height())

    def _fade_from(self, frame, frame_y):
        self._xfade_pm = frame
        self._xfade_y = frame_y
        self._xfade_t = 0.0

    def _draw_pose(self, painter, x, y, frame, frame_y):
        old = self._xfade_pm
        blend = self._xfade_t
        frame_x = round((self._dw - frame.width()) / 2)
        if old is None or blend >= 1.0:
            painter.drawPixmap(x + frame_x, y + frame_y, frame)
            return
        old_y = self._xfade_y
        width = max(old.width(), frame.width())
        top = min(old_y, frame_y)
        bottom = max(old_y + old.height(), frame_y + frame.height())
        # Premultiplied addition preserves opacity where both frames overlap.
        from PyQt5.QtGui import QImage
        if self._fade_surface is None or (self._fade_surface.width(), self._fade_surface.height()) != (width, bottom - top):
            self._fade_surface = QImage(width, bottom - top, QImage.Format_ARGB32_Premultiplied)
        self._fade_surface.fill(Qt.transparent)
        layer_painter = QPainter(self._fade_surface)
        layer_painter.setOpacity(1 - blend)
        layer_painter.drawPixmap(round((width - old.width()) / 2), old_y - top, old)
        layer_painter.setCompositionMode(QPainter.CompositionMode_Plus)
        layer_painter.setOpacity(blend)
        layer_painter.drawPixmap(round((width - frame.width()) / 2), frame_y - top, frame)
        layer_painter.end()
        painter.drawImage(x + round((self._dw - width) / 2), y + top, self._fade_surface)

    def _draw_character(self, p: QPainter, x: int, y: int):
        x+=self._resident_pad[0];y+=self._resident_pad[1]
        p.save()
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.setRenderHint(QPainter.Antialiasing, True)

        # Apply impact before native-frame early returns, so clicks never feel delayed.
        if self._tap_elapsed < .22:
            from ui.pet_physics import squash_scale
            touch=squash_scale(self._tap_elapsed/.22,self._tap_amount)
            anchor_x=x+self._dw/2;anchor_y=y+self._dh
            p.translate(anchor_x,anchor_y);p.scale(1+(1-touch)*.5,touch);p.translate(-anchor_x,-anchor_y)

        self._draw_shadow(p, x, y)

        native = (self._clip or '').startswith(('idle:', 'personality:', 'task:'))
        settled = (self._idle_rest is not None and self.expression in ('normal', 'happy')
                   and self.action == 'none' and not self._speaking and not self._task_motion.active)
        fading = self._xfade_pm is not None and self._xfade_t < 1.0
        if native or settled or (fading and not self._clip):
            mirrored = native and self._walking and self._walk_direction < 0
            if mirrored:
                p.translate(2*x + self._dw, 0)
                p.scale(-1, 1)
            if native:
                clip = self.clips[self._clip]
                frames = clip['frames']
                pm = frames[min(int(self._clip_frame), len(frames) - 1)]
                frame_y = self._native_y(clip, pm)
                self._last_native_pm, self._last_native_y = pm, frame_y
            elif settled:
                pm, frame_y = self._idle_rest, self._idle_rest_y
            else:
                pm, frame_y = self._src, 0
            self._draw_pose(p, x, y, pm, frame_y)
            frame_x = round((self._dw - pm.width()) / 2)
            draw_executor_accessory(p, self, x + frame_x, y + frame_y, pm,
                                    native=True, mirrored=mirrored)
            if self.sunglasses:
                self._draw_sunglasses(p, x, y)
            p.restore()
            return

        cx = x + self._dw / 2
        planted=self.action=='none' and self._clip in (None,'idle','talk')
        cy = y + self._dh if planted else y + self._dh / 2
        sx = 2 - self.body_squash
        sy = self.body_squash
        breath_scale = 1.0 if planted else 1.0 + math.sin(self._time * math.tau / 4.0) * .003
        p.translate(cx + self.anim_offset.x(), cy + (0 if planted else self.breath_y) + self.anim_offset.y()
                    + (0 if planted else self._track_dy))
        p.rotate(self.anim_rotate + self._track_rot)
        sq = self._track_squash
        p.scale(breath_scale * sx * self.anim_scale_x * (2 - sq),
                breath_scale * sy * self.anim_scale_y * sq)
        p.translate(-cx, -cy)

        # GIF 表情包（真实图片帧）优先绘制；轨道动画不替换表情
        if self._clip is not None and self.clips[self._clip].get('type') == 'frames':
            frames = self.clips[self._clip]['frames']
            if frames:
                idx = min(int(self._clip_frame), len(frames) - 1)
                pm = frames[idx]
                frame_x = int(x + (self._dw - pm.width()) / 2)
                frame_y = int(y + self._dh - pm.height())
                p.drawPixmap(frame_x, frame_y, pm)
                draw_executor_accessory(p, self, frame_x, frame_y, pm, native=True)
                if self.sunglasses:
                    self._draw_sunglasses(p, x, y)
                p.restore()
                return

        if (not self._being_dragged and not self._walking and self.expression != 'sleepy'
                and self.action not in ('think_chin', 'scratch_head') and self._draw_native_face(p, x, y)):
            pass
        elif self._speaking or self._being_dragged or self._walking or self._pointer_near or max(map(abs, self._gaze)) > .025 or self.eye_open < .99:
            p.save()
            if self._walking:
                p.translate(cx,y+self._dh)
                p.rotate(2.3*math.sin(self._walk_phase)+self._walk_direction*2)
                p.translate(-cx,-y-self._dh)
                p.translate(0,-abs(math.sin(self._walk_phase))*2.5)
            use_face = self._speaking or (not self._walking and not self._being_dragged and self.expression != 'sleepy')
            self._draw_sprite(p, x, y, self._face_frame() if use_face else self._src)
            draw_executor_accessory(p, self, x, y, self._src)
            p.restore()
        elif self._transition < 1.0:
            old_pm = self._scaled.get(self._prev_expression, self._scaled["normal"])
            new_pm = self._scaled.get(self.expression, self._scaled["normal"])
            p.setOpacity(1.0 - self._transition)
            self._draw_sprite(p,x,y,old_pm)
            p.setOpacity(self._transition)
            self._draw_sprite(p,x,y,new_pm)
            p.setOpacity(1.0)
            draw_executor_accessory(p, self, x, y, new_pm)
        else:
            frame = self._face_frame() if self.expression in ('normal', 'happy') else self._src
            self._draw_sprite(p, x, y, frame)
            draw_executor_accessory(p, self, x, y, frame)

        if self.sunglasses:self._draw_sunglasses(p,x,y)

        # 指点江山手臂
        if self.action == "point" and self.point_active > 0.05:
            self._draw_pointing_arm(p, x, y)

        p.restore()

    def _draw_native_face(self, painter, x, y):
        if self._native_face is None:
            from ui.native_face import NativeFace
            try:
                self._native_face = NativeFace(self._res_dir)
            except (FileNotFoundError, OSError):
                self._native_face = False
        if not self._native_face:
            return False
        mouth = self._mouth_pose if self._speaking else (.25 if self.expression == 'surprised' else 0.)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        self._native_face.draw(painter, x, y, self._dw, self._dh, self.eye_open, mouth, self._gaze)
        draw_executor_accessory(painter, self, x, y, self._src)
        return True

    def _draw_sunglasses(self,p,x,y):
        """Vector accessory anchored to the existing character's eyes."""
        w,h=self._dw,self._dh
        p.save()
        p.translate(x+self.head_yaw*w*.038,y+self.head_nod*h*.017)
        p.setPen(QPen(QColor('#171d29'),max(2,w*.014)))
        p.setBrush(QColor('#111824'))
        for left in (.268,.518):
            p.drawRoundedRect(QRectF(w*left,h*.318,w*.215,h*.085),w*.026,w*.026)
        p.drawLine(QPointF(w*.48,h*.339),QPointF(w*.522,h*.339))
        p.drawLine(QPointF(w*.27,h*.328),QPointF(w*.225,h*.309))
        p.drawLine(QPointF(w*.73,h*.328),QPointF(w*.775,h*.309))
        p.setPen(QPen(QColor(152,216,245,200),max(1,w*.008)))
        for left in (.305,.555):
            p.drawLine(QPointF(w*left,h*.338),QPointF(w*(left+.04),h*.373))
        p.restore()

    def _draw_sprite(self,p,x,y,pm):
        if self._walking:
            # Alternate the original feet; body and face remain the original artwork.
            w,h=pm.width(),pm.height();cut=round(h*.925)
            p.drawPixmap(x,y,pm.copy(0,0,w,cut))
            for side in (0,1):
                phase=self._walk_phase+side*math.pi
                p.drawPixmap(int(x+side*w/2+math.cos(phase)*2),int(y+cut-max(0,math.sin(phase))*5),
                    pm.copy(int(side*w/2),cut,int(w/2)+1,h-cut))
            return
        if self.action in ('think_chin','scratch_head') and self._hand_blend>.008:
            # Keep the source face/body intact; replace only the right flipper.
            # This is a native articulated limb, not generated/interpolated art.
            p.save()
            w,h=pm.width(),pm.height()
            area=QPainterPath();area.addRect(QRectF(x,y,w,h))
            cut=QPainterPath();cut.moveTo(x+w*.805,y+h*.576)
            cut.cubicTo(x+w*1.18,y+h*.585,x+w*1.15,y+h*.85,x+w*.965,y+h*.85)
            cut.cubicTo(x+w*.90,y+h*.81,x+w*.845,y+h*.737,x+w*.805,y+h*.655)
            cut.closeSubpath();p.setClipPath(area.subtracted(cut),Qt.IntersectClip)
            p.drawPixmap(x,y,pm);p.restore()
            self._draw_thinking_flipper(p,x,y,w,h)
            return
        if self._speaking or max(abs(self.head_yaw),abs(self.head_nod),abs(self.head_tilt))<.001:
            p.drawPixmap(x,y,pm);return
        width,height=pm.width(),pm.height()
        # A sub-pixel gaze offset is invisible but the mesh path slices the
        # sprite into ~125 blits, so skip it until the displacement can be seen.
        dx=abs(self.head_yaw)*width*.038+abs(self.head_tilt)*height*.034
        dy=abs(self.head_nod)*height*.017
        if max(dx,dy)<.75:
            p.drawPixmap(x,y,pm);return
        # Continuous mesh warp: head moves, deformation fades through the neck,
        # and the lower body remains anchored. Uses the existing transparent art.
        for row in range(0,height,2):
            ratio=row/height
            weight=1.0 if ratio<.39 else max(0,min(1,(.66-ratio)/.27))
            weight=weight*weight*(3-2*weight)
            if weight==0:
                p.drawPixmap(QRectF(x,y+row,width,height-row),pm,QRectF(0,row,width,height-row))
                break
            dx=self.head_yaw*width*.038*weight+self.head_tilt*(height*.34-row)*.10*weight
            dy=self.head_nod*height*.017*weight
            scale=1-.055*abs(self.head_yaw)*weight
            target=QRectF(x+dx+width*(1-scale)/2,y+row+dy,width*scale,min(3,height-row))
            p.drawPixmap(target,pm,QRectF(0,row,width,min(3,height-row)))

    def _draw_thinking_flipper(self,p,x,y,w,h):
        blend=self._hand_blend
        # Smooth raising/settling; small repetitive scratch only after arrival.
        rest=(.945,.745)
        touch=(.675,.515) if self.action=='think_chin' else (.855,.287)
        motion=math.sin(self._action_timer*17)*.009*blend**4 if self.action=='scratch_head' else 0.
        tx=x+w*(rest[0]+(touch[0]-rest[0])*blend)
        ty=y+h*(rest[1]+(touch[1]-rest[1])*blend+motion)
        sx,sy=x+w*.80,y+h*.616
        limb=QPainterPath(QPointF(sx,sy))
        limb.cubicTo(x+w*(.90-.035*blend),y+h*(.65-.095*blend),
                     tx+w*.06*blend,ty+h*.05,tx,ty)
        gradient=QLinearGradient(sx-w*.05,sy,tx+w*.07,ty)
        gradient.setColorAt(0,QColor('#141b20'));gradient.setColorAt(.5,QColor('#343e45'))
        gradient.setColorAt(1,QColor('#20282e'))
        p.save();p.setPen(QPen(gradient,w*.105,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin))
        p.setBrush(Qt.NoBrush);p.drawPath(limb);p.restore()

    def _draw_shadow(self, p: QPainter, ox: int, oy: int):
        dw, dh = self._dw, self._dh
        shadow_w = dw * 0.5
        shadow_h = 11
        shadow_x = ox + (dw - shadow_w) / 2
        shadow_y = oy + dh - 8
        grad = QRadialGradient(shadow_x + shadow_w / 2, shadow_y, shadow_w / 2)
        grad.setColorAt(0, QColor(0, 0, 0, 40))
        grad.setColorAt(0.6, QColor(0, 0, 0, 16))
        grad.setColorAt(1, QColor(0, 0, 0, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(grad)
        p.drawEllipse(QRectF(shadow_x, shadow_y - shadow_h / 2, shadow_w, shadow_h))

    def _draw_mouth(self, p: QPainter, ox: int, oy: int):
        """说话小嘴（脸部位置动嘴弧线）"""
        dw, dh = self._dw, self._dh
        mx = ox + dw * 0.50
        my = oy + dh * 0.66
        open_ = 1.0 + self.mouth_open * 3
        pen = QPen(QColor(40, 30, 30), 2.0)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        path = QPainterPath()
        path.moveTo(mx - 6, my)
        path.quadTo(mx, my + open_, mx + 6, my)
        p.drawPath(path)

    def _draw_pointing_arm(self, p: QPainter, ox: int, oy: int):
        """指点江山：右侧伸出的指点手臂（黄手臂+小拳头+指向箭头）"""
        dw, dh = self._dw, self._dh
        env = self.point_active
        arm_x = ox + dw * 0.86
        arm_y = oy + dh * 0.42 + math.sin(self._action_phase * math.pi * 3) * 2.5 * env

        pen = QPen(QColor(245, 180, 60), 7)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        path = QPainterPath()
        path.moveTo(arm_x - 26, arm_y + 20)
        path.quadTo(arm_x - 8, arm_y + 6, arm_x, arm_y)
        p.drawPath(path)

        p.setPen(Qt.NoPen)
        p.setBrush(QColor(245, 190, 70))
        p.drawEllipse(QPointF(arm_x, arm_y), 10, 10)

        arrow = QPainterPath()
        arrow.moveTo(arm_x + 18, arm_y - 8)
        arrow.lineTo(arm_x + 18, arm_y + 8)
        arrow.lineTo(arm_x + 30, arm_y)
        arrow.closeSubpath()
        p.setBrush(QColor(255, 120, 60))
        p.drawPath(arrow)


# ---- [teammate-merge] legacy compat re-exports (additive) ----
from core.legacy_compat import (  # noqa: E402,F401
    adaptive_display_width,
)
