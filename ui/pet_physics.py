"""Local, bounded desktop motion, adapted from PC2005-cloud/dsh-pet physics.

See resources/multiagent/dafish/dsh-pet/LICENSE-MIT.txt and THIRD_PARTY.md.
All coordinates are Qt logical desktop coordinates, never SendInput pixels.
"""
import math
import time
from collections import deque

from PyQt5.QtCore import QObject, QTimer


def squash_scale(progress, amount=.90):
    """Upstream two-stage compression/easeOutBack, with art-safe amplitude."""
    u = max(0., min(1., progress))
    if u < .45:
        return 1 - (1 - amount) * (u / .45) ** 2
    p = (u - .45) / .55
    f = 1 + 2.70158 * (p - 1) ** 3 + 1.70158 * (p - 1) ** 2
    return min(1.04, amount + (1 - amount) * max(0., f))


def release_velocity(samples, now):
    """150 ms throw trail; holding still before release means put down gently."""
    recent = [s for s in samples if now - s[0] <= .15]
    if len(recent) < 2 or now - recent[-1][0] > .15:
        return None
    span = recent[-1][0] - recent[0][0]
    if span < .02:
        return None
    vx = (recent[-1][1] - recent[0][1]) / span
    vy = (recent[-1][2] - recent[0][2]) / span
    speed = math.hypot(vx, vy)
    if speed < 1e-6:
        return None
    segments = [math.hypot(b[1]-a[1], b[2]-a[2])/(b[0]-a[0])
                for a, b in zip(recent, recent[1:]) if b[0]-a[0] >= .008]
    magnitude = 3600 * (1-math.exp(-(.5*speed+.5*max(segments, default=speed))/3600))
    if magnitude < 500:
        return None
    return vx/speed*magnitude, vy/speed*magnitude


class DesktopPetPhysics(QObject):
    """Only moves its owning pet. No system input, polling or background API."""
    def __init__(self, window, animator, settled=None):
        super().__init__(window)
        self.window, self.animator, self.settled = window, animator, settled
        self.samples = deque(maxlen=40)
        self.timer = QTimer(self)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self._tick)
        self.mode = None
        self.vx = self.vy = 0.

    def stop(self):
        self.timer.stop()
        self.mode = None
        if hasattr(self.animator,'set_walking'):self.animator.set_walking(False)

    def press(self, point):
        self.stop()
        self.samples.clear()
        self.sample(point)

    def sample(self, point):
        now = time.monotonic()
        self.samples.append((now, point.x(), point.y()))
        while self.samples and now-self.samples[0][0] > .2:
            self.samples.popleft()

    def release(self, dragged):
        # User preference: ordinary drag/drop. Never throw, fall or shake on release.
        self.stop()
        self.samples.clear()
        if dragged and self.settled:
            self.settled()
        return False

    def walk(self, dx, seconds=10., lead=2., tail=2.):
        if (self.mode or not self.window.isVisible() or self.animator._idle_motion_blocked()
                or self.animator.active_clip not in (None,'idle') or 'personality:walk' not in self.animator.clips):
            return False
        self.start_x, self.start_y = self.window.x(), self.window.y()
        area = self.window.screen().availableGeometry()
        target = max(area.left(), min(self.start_x+dx, area.right()-self.window.width()+1))
        self.distance = target-self.start_x
        if abs(self.distance)<2:return False
        clip=self.animator.clips['personality:walk']
        self.seconds=len(clip['frames'])/clip['fps']
        self.lead,self.tail=.25,.25
        self.mode = 'walk'
        self.started = self.last = time.monotonic()
        if hasattr(self.animator,'set_walking'):self.animator.set_walking(True,self.distance)
        self.timer.start()
        return True

    def _tick(self):
        now = time.monotonic()
        dt = min(.05, max(0., now-self.last))
        self.last = now
        if self.mode == 'walk':
            if (not self.window.isVisible() or self.animator._being_dragged or
                    self.animator._task_motion.active or self.animator._speaking or self.animator._music_stage_active):
                self._finish();return
            u = max(0., min(1., (now-self.started-self.lead)/max(.1, self.seconds-self.lead-self.tail)))
            # Smooth acceleration/deceleration instead of a constant-speed slide.
            u=u*u*(3-2*u)
            self.window.move(round(self.start_x+self.distance*u), self.start_y)
            if now-self.started >= self.seconds:
                self._finish()
            return
        if self.mode != 'throw':
            self.stop()
            return
        area = self.window.screen().availableGeometry()
        left, top = area.left(), area.top()
        right, bottom = max(left, area.right()-self.window.width()+1), max(top, area.bottom()-self.window.height()+1)
        # Small substeps prevent a fast throw tunnelling through an edge.
        count = max(1, min(64, math.ceil(max(abs(self.vx), abs(self.vy))*dt/8)))
        h = dt/count
        impact = 0.
        for _ in range(count):
            self.vy += 1400*h
            self.x += self.vx*h
            self.y += self.vy*h
            if self.x < left or self.x > right:
                self.x = max(left, min(right, self.x))
                self.vx *= -.78
            if self.y < top:
                self.y = float(top)
                self.vy = abs(self.vy)*.78
            if self.y >= bottom:
                self.y = float(bottom)
                impact = max(impact, abs(self.vy))
                self.vy = -abs(self.vy)*.78 if abs(self.vy) > 40 else 0.
                self.vx *= math.exp(-2.5*h)
                if abs(self.vx) < 15:
                    self.vx = 0.
        self.window.move(round(self.x), round(self.y))
        if impact > 300 and hasattr(self.animator, 'tap'):
            self.animator.tap(.88)
        if (self.vx == 0 and self.vy == 0) or now-self.started > 8:
            self._finish()

    def _finish(self):
        self.stop()
        if self.settled:
            self.settled()
