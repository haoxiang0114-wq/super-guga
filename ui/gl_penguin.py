# -*- coding: utf-8 -*-
"""3D 企鹅渲染器 — 离屏 OpenGL 程序化建模（无需任何模型文件）

架构：QOffscreenSurface + QOpenGLContext + QOpenGLFramebufferObject
  → 每帧把 3D 企鹅画进 FBO → toImage() 给 QPainter.drawImage
  （PandaQt "render-to-texture + QImage" 方案，与 2D 气泡/拖拽无缝共存）

程序化建模（球体/椭球组合）：
  身体(椭球) + 肚皮(白) + 头(球) + 眼睛(黑球, 眨眼缩放) + 喙(锥, 说话开合)
  + 翅膀(扁椭球, 扇动/指点) + 脚(橙球) + 腮红(粉片)

动画参数（QTimer 驱动）：
  breathing(呼吸缩放) / blink(眨眼) / mouth(说话) / bob(跳) / shake(抖)
  / tilt(歪头) / wing(扇翅) / point(指点) / peek(探头)

接口对齐 SpriteAnimator：start/stop/set_expression/update_mood/trigger_action/
  mouth_open/on_update/width/height —— pet_window 无需大改。
"""
import math
import os
import sys

from PyQt5.QtCore import QTimer
from PyQt5.QtGui import QSurfaceFormat, QOpenGLContext, QOffscreenSurface, QOpenGLFramebufferObject, QImage
from PyQt5.QtWidgets import QApplication

try:
    from OpenGL import GL
    from OpenGL.GLU import gluSphere, gluNewQuadric, gluCylinder
    _GL_OK = True
except Exception as e:  # noqa
    _GL_OK = False
    _GL_ERR = str(e)


class GLPenguinRenderer:
    """3D 企鹅渲染器（离屏 GL → QImage），接口兼容 SpriteAnimator"""

    width = 250
    height = 250

    def __init__(self, display_width: int = 250):
        self.width = display_width
        self.height = display_width
        self.on_update = None
        self._timer = QTimer()
        self._timer.setInterval(33)  # ~30fps
        self._timer.timeout.connect(self._tick)

        # ---- 动画状态 ----
        self._expr = 'happy'
        self._t = 0.0
        self._mouth = 0.0
        self._bob = 0.0
        self._shake = 0.0
        self._tilt = 0.0
        self._wing = 0.0
        self._point = 0.0
        self._peek = 0.0
        self._action = ''
        self._action_t = 0.0

        # ---- GL 对象（惰性初始化） ----
        self._ctx = None
        self._surface = None
        self._fbo = None
        self._quad = None
        self._ready = False
        self._cached_img = None   # 预渲染缓存：paintEvent 只 drawImage，绝不碰 GL
        self._dirty = True
        self.error = _GL_ERR if not _GL_OK else None

    # ==================== 生命周期 ====================

    @property
    def available(self) -> bool:
        if not _GL_OK:
            return False
        if not self._init_gl():
            return False
        # 严格校验：必须能真实渲染出一帧才算可用，否则回退 2D
        if self._cached_img is None and self._dirty:
            self._render_cached()
            if self._cached_img is None:
                return False
        return True

    def _init_gl(self) -> bool:
        if self._ready:
            return True
        if not _GL_OK:
            return False
        try:
            fmt = QSurfaceFormat()
            fmt.setAlphaBufferSize(8)
            fmt.setDepthBufferSize(24)
            fmt.setSamples(4)  # 抗锯齿
            self._ctx = QOpenGLContext()
            self._ctx.setFormat(fmt)
            if not self._ctx.create():
                self.error = 'OpenGL 上下文创建失败'
                return False
            self._surface = QOffscreenSurface()
            self._surface.setFormat(fmt)
            self._surface.create()
            if not self._surface.isValid():
                self.error = '离屏表面无效'
                return False
            self._ctx.makeCurrent(self._surface)
            self._quad = gluNewQuadric()
            self._ready = True
            return True
        except Exception as e:
            self.error = f'OpenGL 初始化失败: {e}'
            return False

    def start(self):
        if not self.available:
            return
        self._timer.start()

    def stop(self):
        self._timer.stop()

    # ==================== 状态接口（对齐 SpriteAnimator） ====================

    def set_expression(self, expr: str):
        self._expr = expr if expr else 'happy'

    def update_mood(self, mood_emoji: str):
        # 心情 → 表情（宽松映射，避免常年哭泣）
        m = str(mood_emoji or '')
        if '😢' in m or '😭' in m:
            self.set_expression('sad')
        elif '😄' in m or '😆' in m or '😊' in m:
            self.set_expression('happy')
        elif '😡' in m or '😠' in m:
            self.set_expression('playful')
        elif '😱' in m or '😨' in m:
            self.set_expression('surprised')
        else:
            self.set_expression('normal')

    def trigger_action(self, action: str):
        """动作 → 动画参数（对齐 SpriteAnimator 的 trigger_action）"""
        self._action = action
        self._action_t = 0.0
        if action == 'bounce':
            self._bob = 1.0
        elif action == 'shake':
            self._shake = 1.0
        elif action == 'tsundere':
            self._tilt = 1.0
        elif action == 'point':
            self._point = 1.0
        elif action == 'peek':
            self._peek = 1.0

    def set_talking(self, talking: bool):
        if not talking:
            self._mouth = 0.0

    def trigger_bounce(self):
        self.trigger_action('bounce')

    def play_clip(self, name, loop=None):
        pass

    def stop_clip(self):
        pass

    def active_clip(self):
        return None

    def play_sticker(self, name):
        if '跳' in str(name) or '蹦' in str(name):
            self.trigger_action('bounce')
        elif '摇' in str(name) or '傲' in str(name):
            self.trigger_action('tsundere')
        elif '指' in str(name):
            self.trigger_action('point')
        elif '偷' in str(name):
            self.trigger_action('peek')

    @property
    def mouth_open(self) -> float:
        return self._mouth

    @mouth_open.setter
    def mouth_open(self, v: float):
        self._mouth = float(v)

    @property
    def sticker_names(self):
        return []

    # ==================== 每帧更新 ====================

    def _tick(self):
        self._t += 1.0 / 30.0
        # 动作衰减
        for key in ('_bob', '_shake', '_tilt', '_point', '_peek'):
            v = getattr(self, key)
            if v > 0:
                setattr(self, key, max(0.0, v - 0.05))
        if self._action_t < 1.5:
            self._action_t += 1.0 / 30.0
        # 预渲染到缓存：paintEvent 只 drawImage，避免 GL/QPainter 上下文冲突
        self._render_cached()
        if self.on_update:
            self.on_update()

    def _render_cached(self):
        """QTimer 线程渲染一帧存入缓存（不在此处触发 GL/Qt 绘制冲突）"""
        try:
            img = self.render()
            if img is not None:
                self._cached_img = img
                self._dirty = False
        except Exception as e:
            self.error = f'渲染失败: {e}'
            self._cached_img = None

    def paint_image(self):
        """供 paintEvent 取图：只返回缓存 QImage，不触发任何 GL 调用"""
        return self._cached_img

    def render(self) -> 'QImage':
        """渲染一帧 3D 企鹅 → QImage（透明背景），失败返回 None"""
        if not _GL_OK or not self._init_gl():  # 注意：不能调 self.available（会递归）
            return None
        try:
            self._ctx.makeCurrent(self._surface)
            if self._fbo is None:
                self._fbo = QOpenGLFramebufferObject(self.width, self.height)
            self._fbo.bind()

            GL.glViewport(0, 0, self.width, self.height)
            GL.glClearColor(0, 0, 0, 0)
            GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
            GL.glEnable(GL.GL_DEPTH_TEST)
            GL.glEnable(GL.GL_BLEND)
            GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA)
            GL.glEnable(GL.GL_LIGHTING)
            GL.glEnable(GL.GL_LIGHT0)
            GL.glColorMaterial(GL.GL_FRONT_AND_BACK, GL.GL_AMBIENT_AND_DIFFUSE)
            GL.glEnable(GL.GL_COLOR_MATERIAL)
            GL.glShadeModel(GL.GL_SMOOTH)

            # 灯光
            GL.glLightfv(GL.GL_LIGHT0, GL.GL_POSITION, (0.3, 0.8, 1.5, 0.0))
            GL.glLightfv(GL.GL_LIGHT0, GL.GL_DIFFUSE, (0.9, 0.9, 0.9, 1.0))
            GL.glLightfv(GL.GL_LIGHT0, GL.GL_AMBIENT, (0.45, 0.45, 0.48, 1.0))

            # 相机
            GL.glMatrixMode(GL.GL_PROJECTION)
            GL.glLoadIdentity()
            import OpenGL.GLU as glu
            glu.gluPerspective(32, 1.0, 0.1, 100)
            GL.glMatrixMode(GL.GL_MODELVIEW)
            GL.glLoadIdentity()
            GL.glTranslatef(0, 0, -7.5)

            self._draw_penguin()

            self._fbo.release()
            self._ctx.doneCurrent()
            return self._fbo.toImage()
        except Exception as e:
            self.error = f'渲染失败: {e}'
            return None

    # ---- 绘制企鹅 ----

    def _draw_penguin(self):
        t = self._t

        # 呼吸
        breath = 1.0 + 0.02 * math.sin(t * 2.6)
        # 眨眼（约 3.7 秒一次）
        blink = 1.0
        if (t % 3.7) < 0.12:
            blink = 0.08
        # 说话嘴动
        mouth = self._mouth if self._mouth > 0 else 0.0
        if self._mouth > 0:
            mouth = self._mouth * (0.5 + 0.5 * math.sin(t * 18))
        # 表情参数
        eye_sz, eye_gap, mouth_curve, brow_angle = self._expr_params()
        # 动作
        bob = self._bob * math.sin(self._action_t * 14) * 0.35
        shake_x = self._shake * math.sin(self._action_t * 40) * 0.12
        tilt_z = self._tilt * math.sin(self._action_t * 9) * 12.0
        peek_hide = self._peek * 0.25  # 身体下沉藏一半

        GL.glPushMatrix()
        GL.glTranslatef(shake_x, bob, 0)
        GL.glRotatef(tilt_z, 0, 0, 1)

        # 整体呼吸
        GL.glPushMatrix()
        GL.glScalef(breath, breath, breath)

        # ---- 身体（黑色椭球）----
        GL.glColor3f(0.16, 0.17, 0.20)
        GL.glPushMatrix()
        GL.glScalef(0.62, 0.78, 0.55)
        GL.glTranslatef(0, -0.25, 0)
        gluSphere(self._quad, 1.0, 28, 28)
        GL.glPopMatrix()

        # ---- 肚皮（白色，稍小贴前）----
        GL.glColor3f(0.96, 0.96, 0.97)
        GL.glPushMatrix()
        GL.glScalef(0.46, 0.62, 0.45)
        GL.glTranslatef(0, -0.28, 0.42)
        gluSphere(self._quad, 1.0, 24, 24)
        GL.glPopMatrix()

        # ---- 脚（橙色）----
        GL.glColor3f(0.98, 0.62, 0.20)
        for sx in (-0.24, 0.24):
            GL.glPushMatrix()
            GL.glScalef(0.16, 0.10, 0.30)
            GL.glTranslatef(sx / 0.16 * 0.1, -1.05, 0.25)
            gluSphere(self._quad, 1.0, 14, 14)
            GL.glPopMatrix()

        # ---- 翅膀（可扇动/指点）----
        wing_flap = math.sin(t * 3.2) * 0.25
        for side, dirx in ((-1, 1), (1, -1)):
            GL.glPushMatrix()
            GL.glTranslatef(dirx * 0.62, 0.05, 0)
            # 指点：右翅前伸；扇动：绕 z 摆动
            if self._point > 0 and side == 1:
                GL.glRotatef(-75 * self._point, 0, 0, 1)
                GL.glRotatef(-40 * self._point, 1, 0, 0)
                GL.glTranslatef(0.55 * self._point, 0, 0.25 * self._point)
            else:
                GL.glRotatef(wing_flap * dirx, 0, 0, 1)
            GL.glColor3f(0.16, 0.17, 0.20)
            GL.glPushMatrix()
            GL.glScalef(0.42, 0.14, 0.16)
            gluSphere(self._quad, 1.0, 18, 18)
            GL.glPopMatrix()
            GL.glPopMatrix()

        # ---- 头（黑色球）----
        GL.glColor3f(0.16, 0.17, 0.20)
        GL.glPushMatrix()
        head_dy = 0.78 - peek_hide
        GL.glTranslatef(0, head_dy, 0)
        # 歪头
        if self._tilt > 0:
            GL.glRotatef(tilt_z * 0.4, 0, 0, 1)
        GL.glScalef(0.52, 0.50, 0.48)
        gluSphere(self._quad, 1.0, 26, 26)

        # ---- 面部白色椭圆（额头）----
        GL.glColor3f(0.96, 0.96, 0.97)
        GL.glPushMatrix()
        GL.glScalef(0.86, 0.62, 0.55)
        GL.glTranslatef(0, 0.30, 0.86)
        gluSphere(self._quad, 1.0, 20, 20)
        GL.glPopMatrix()

        # ---- 眼睛（黑球，眨眼/表情缩放）----
        GL.glColor3f(0.08, 0.08, 0.10)
        for ex in (-0.16, 0.16):
            GL.glPushMatrix()
            GL.glScalef(0.085 * eye_sz, 0.11 * eye_sz * blink, 0.06)
            GL.glTranslatef(ex / 0.085, 0.12, 1.32)
            gluSphere(self._quad, 1.0, 14, 14)
            GL.glPopMatrix()
        # 高光
        GL.glColor3f(1, 1, 1)
        for ex in (-0.16, 0.16):
            GL.glPushMatrix()
            GL.glScalef(0.03, 0.035 * blink, 0.03)
            GL.glTranslatef(ex / 0.03 + 0.6, 0.28, 1.9)
            gluSphere(self._quad, 1.0, 10, 10)
            GL.glPopMatrix()

        # ---- 腮红 ----
        GL.glColor3f(1.0, 0.55, 0.55)
        for ex in (-0.30, 0.30):
            GL.glPushMatrix()
            GL.glScalef(0.10, 0.07, 0.03)
            GL.glTranslatef(ex / 0.10, -0.02, 1.7)
            gluSphere(self._quad, 1.0, 12, 12)
            GL.glPopMatrix()

        # ---- 喙（橙色锥，说话开合）----
        GL.glColor3f(0.98, 0.62, 0.20)
        GL.glPushMatrix()
        GL.glTranslatef(0, -0.06 - mouth * 0.03, 1.28)
        GL.glRotatef(90, 1, 0, 0)
        GL.glRotatef(180, 0, 0, 1)
        gluCylinder(self._quad, 0.07, 0.10, 0.22, 14, 1)
        GL.glPopMatrix()

        GL.glPopMatrix()  # 呼吸
        GL.glPopMatrix()  # 整体

    def _expr_params(self):
        """表情 → (眼睛大小, 眼距, 嘴型曲线, 眉毛角度)"""
        e = self._expr
        if e == 'sad':
            return 0.55, 1.0, -0.3, 0.35
        if e == 'surprised':
            return 1.35, 1.15, 0.5, -0.2
        if e == 'playful':
            return 0.7, 1.0, 0.25, 0.1
        if e == 'normal':
            return 1.0, 1.0, 0.0, 0.0
        return 0.9, 1.0, 0.3, -0.1  # happy


if __name__ == '__main__':
    # 无界面自测：渲染一帧保存 PNG
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    app = QApplication(sys.argv)
    r = GLPenguinRenderer(250)
    if not r.available:
        print('GL 不可用:', r.error)
        sys.exit(1)
    r.start()
    for _ in range(3):
        r._tick()
    img = r.render()
    if img is None:
        print('渲染失败:', r.error)
        sys.exit(1)
    img.save(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'gl_test.png'))
    print('已保存 gl_test.png', img.size())
