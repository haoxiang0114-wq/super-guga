"""Small local painter effects; shares the pet's timer and transparent canvas."""
import math

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QPainterPath, QPen, QRadialGradient


class PetEffects:
    def __init__(self):
        self.enabled = True

    def draw(self, painter, kind, age, width, height, foot_x, foot_y, behind=False):
        if not self.enabled or not kind:
            return
        painter.save()
        painter.setRenderHint(painter.Antialiasing, True)
        if kind == 'entrance':
            envelope = min(1., age/.65)*min(1., max(0., (6.-age)/1.3))
            if behind:
                radius = width*.55
                glow = QRadialGradient(QPointF(foot_x, foot_y-height*.12), radius)
                glow.setColorAt(0., QColor(121, 220, 255, round(70*envelope)))
                glow.setColorAt(1., QColor(130, 180, 255, 0))
                painter.setPen(Qt.NoPen);painter.setBrush(glow)
                painter.drawEllipse(QPointF(foot_x, foot_y-height*.12), radius, radius*.7)
                painter.setBrush(Qt.NoBrush)
                for i in range(3):
                    ripple = (age*.45+i/3) % 1.
                    radius = width*(.24+.3*ripple)
                    painter.setPen(QPen(QColor(141, 224, 255, round(150*envelope*(1-ripple))), 1.4))
                    painter.drawEllipse(QRectF(foot_x-radius, foot_y-7-ripple*3, radius*2, 12+ripple*5))
            else:
                for i in range(15):
                    phase = (age*.23+i*.173) % 1.
                    x = foot_x+math.sin(i*7.13+age*.24)*width*(.26+.17*(i % 3)/2)
                    y = foot_y-height*(.1+.85*phase)
                    opacity = math.sin(phase*math.pi)*envelope
                    self._star(painter, x, y, 1.7+(i % 3)*.8, QColor(185, 235, 255, round(190*opacity)))
        elif not behind and kind == 'sing':
            for i in range(4):
                phase = (age*.23+i*.25) % 1.
                side = -1 if i % 2 else 1
                x = foot_x+side*width*(.44+.06*math.sin(phase*math.pi))
                y = foot_y-height*(.35+.4*phase)
                color=QColor(125,165,230,round(210*math.sin(phase*math.pi)))
                self._note(painter,x,y,color,double=not i%2)
        elif not behind and kind in ('poke', 'bounce', 'heart'):
            life = 3.0 if kind in ('poke', 'heart') else 1.4
            phase = age/life
            if 0 <= phase <= 1:
                alpha = round(185*math.sin(math.pi*phase))
                for i in range(6):
                    angle = i*math.pi/3+.15
                    x = foot_x+math.cos(angle)*width*(.24+.2*phase)
                    y = foot_y-height*.57+math.sin(angle)*height*(.18+.1*phase)-12*phase
                    self._star(painter, x, y, width*.013, QColor(255, 196, 204, alpha))
        painter.restore()

    @staticmethod
    def _note(painter,x,y,color,double=False):
        # Paths work on both the desktop and headless Qt; no symbol-font lookup.
        painter.save();painter.translate(x,y)
        painter.setPen(QPen(color,1.8,Qt.SolidLine,Qt.RoundCap));painter.setBrush(color)
        painter.drawEllipse(QRectF(-4,0,7,4.5))
        painter.drawLine(QPointF(2.5,2),QPointF(2.5,-13))
        if double:
            painter.drawEllipse(QRectF(8,-2,7,4.5))
            painter.drawLine(QPointF(14.5,0),QPointF(14.5,-15))
            painter.setPen(QPen(color,3.,Qt.SolidLine,Qt.RoundCap))
            painter.drawLine(QPointF(2.5,-13),QPointF(14.5,-15))
        else:
            flag=QPainterPath(QPointF(2.5,-13))
            flag.cubicTo(12,-10,12,-6,5,-3)
            painter.setBrush(Qt.NoBrush);painter.drawPath(flag)
        painter.restore()

    @staticmethod
    def _star(painter, x, y, radius, color):
        path = QPainterPath()
        path.moveTo(x, y-radius)
        for dx, dy in ((.28, -.28), (1, 0), (.28, .28), (0, 1),
                       (-.28, .28), (-1, 0), (-.28, -.28)):
            path.lineTo(x+dx*radius, y+dy*radius)
        path.closeSubpath()
        painter.setPen(Qt.NoPen);painter.setBrush(color);painter.drawPath(path)
