"""Isolated pet UI and role-policy checks. No PetWindow/AppRuntime construction."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import sys
import tempfile
import time
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from launcher import _prepare_qt
_prepare_qt()
from types import SimpleNamespace
from unittest.mock import patch

from PyQt5.QtCore import QSettings, Qt
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import QApplication, QWidget

from core.companion_policy import read_policy, selected_backend, task_role
from ui.pet_face import FaceDetails
from ui.speech_bubble import PetSpeechBubble
from ui.desktop_experience import DesktopExperience
from ui.sprite_animator import SpriteAnimator
from ui.pet_physics import DesktopPetPhysics

ROOT=Path(__file__).resolve().parents[1]
app=QApplication.instance() or QApplication([])


class FakePet(QWidget):
    def __init__(self,path):
        super().__init__(None,Qt.FramelessWindowHint);self.resize(270,330);self.move(180,160)
        self._config=SimpleNamespace(_s=QSettings(str(path),QSettings.IniFormat))
        self._tts=SimpleNamespace(is_speaking=False)
        self._anim2d=SimpleNamespace(dancing=False)
        self.accepted=True;self.calls=[];self._agent_panel=None;self._is_drag=False
        self.panel=SimpleNamespace(send=self.send,status=SimpleNamespace(text=lambda:'当前任务忙'))
        self._experience=DesktopExperience(self)
    def send(self,text,**kwargs):self.calls.append(text);return self.accepted
    def _ensure_agent_panel(self):return self.panel
    def _open_quick_input(self,text=''):self._experience.show_input(text)
    def _open_voice_recording(self):pass
    def _open_agent_chat(self):pass
    def _open_bg_live(self):pass
    def _open_bg_agent(self):pass
    def _finish_bubble_talk(self,cancel=False):pass
    def _open_desktop_console(self):pass


class DesktopExperienceTests(unittest.TestCase):
    def setUp(self):
        # All networking and process launch is forbidden even if a local mock is missed.
        self.guards=[patch('socket.socket.connect',side_effect=AssertionError('network forbidden in UI test')),
                     patch('subprocess.Popen',side_effect=AssertionError('process forbidden in UI test'))]
        for guard in self.guards:guard.start()
        self.temp=tempfile.TemporaryDirectory();self.pet=FakePet(Path(self.temp.name)/'settings.ini')
        self.pet.show();app.processEvents()
    def tearDown(self):
        self.pet._experience.stop();self.pet.close();self.pet.deleteLater();app.processEvents()
        self.temp.cleanup()
        for guard in reversed(self.guards):guard.stop()

    def test_policy_defaults_and_explicit_delegation(self):
        policy=read_policy(self.pet._config._s)
        self.assertFalse(policy['enabled'])
        policy.update(enabled=True)
        for text,backend in [('修复这个项目并运行测试','codex'),('帮我设计系统架构方案','codex'),
                             ('审计当前代码','codex'),('运行项目测试','codex'),('交给DSH审计代码','dsh')]:
            self.assertEqual(selected_backend(text,policy,'codex'),backend,text)
        self.assertEqual(selected_backend('修复代码',dict(enabled=False),'codex'),'codex')

    def test_speech_transcript_uses_simplified_chinese(self):
        from core.desktop_transcript import normalize_transcript
        self.assertEqual(normalize_transcript('請打開打字框，整理學習計畫。'),'请打开打字框，整理学习计划。')
        self.assertEqual(normalize_transcript('Codex API 123'),'Codex API 123')

    def test_bubble_reveal_hover_and_audio_lifetime(self):
        b=PetSpeechBubble(self.pet);self.addCleanup(b.close)
        b.show_text('咕嘎，今天也一起加油。')
        self.assertLess(b.width(),430);self.assertTrue(b.reveal_timer.isActive())
        while b.reveal_timer.isActive():b._reveal()
        self.assertEqual(b.text.text(),b.full_text)
        b.set_speaking(True);b._expire();self.assertTrue(b.isVisible())
        b.speaking=False;b._hovered=True;b._expire();self.assertTrue(b.isVisible())
        b._hovered=False;b._expire();self.assertFalse(b.isVisible())

    def test_bubble_long_reply_stays_on_screen(self):
        b=PetSpeechBubble(self.pet);self.addCleanup(b.close)
        b.show_text('很长的回复。'*700)
        self.assertLessEqual(len(b._preview),340)
        self.assertTrue(self.pet.screen().availableGeometry().contains(b.frameGeometry()))
        b.set_voice_state('transcribing');b._expire();self.assertTrue(b.isVisible())

    def test_quick_input_keeps_failed_draft(self):
        e=self.pet._experience;e.show_input('帮我整理这些内容')
        self.pet.accepted=False;e.quick_input.submit()
        self.assertEqual(e.quick_input.editor.toPlainText(),'帮我整理这些内容')
        self.assertTrue(e.quick_input.isVisible())
        self.pet.accepted=True;e.quick_input.submit()
        self.assertEqual(e.quick_input.editor.toPlainText(),'');self.assertFalse(e.quick_input.isVisible())

    def test_quick_input_keeps_oversized_draft_without_dispatch(self):
        e=self.pet._experience;e.show_input('字'*15001);e.quick_input.submit()
        self.assertEqual(self.pet.calls,[]);self.assertEqual(len(e.quick_input.editor.toPlainText()),15001)

    def test_ocr_capture_hides_and_restores_own_overlays(self):
        e=self.pet._experience;e.show_input('识别时保留这份草稿');app.processEvents();seen=[]
        pixmap=QPixmap(500,150);pixmap.fill(Qt.white)
        def capture(hwnd):seen.append(e.quick_input.isVisible());return pixmap
        screen=SimpleNamespace(grabWindow=capture)
        with patch('ui.desktop_experience.QApplication.screenAt',return_value=screen),patch('core.desktop_ocr_native.recognize',return_value=dict(ok=True,data={'text':'当前任务：整理资料。'})):
            self.assertTrue(e.read_desktop(False));deadline=time.monotonic()+2
            while e.ocr_busy and time.monotonic()<deadline:app.processEvents();time.sleep(.01)
        self.assertEqual(seen,[False]);self.assertTrue(e.quick_input.isVisible())
        self.assertEqual(e.quick_input.editor.toPlainText(),'识别时保留这份草稿')
        self.assertEqual(e.latest_text,'当前任务：整理资料。');self.assertFalse(e.ocr_busy)

    def test_ocr_failure_clears_previous_context(self):
        e=self.pet._experience;e._ocr_done(dict(ok=True,message='旧屏幕'))
        self.assertEqual(e.latest_text,'旧屏幕')
        e._ocr_done(dict(ok=False,message='识别失败'));self.assertEqual(e.latest_text,'')
        e.latest_text='过期内容';e.latest_at=time.monotonic()-121;e.ask_desktop()
        self.assertIsNone(e.quick_input)

    def test_settings_page_construction_is_passive(self):
        from ui.desktop_console import DesktopConsole
        with patch('ui.desktop_console.read_raw',return_value={'driver_source':'custom'}),patch('ui.desktop_console.resolve_config',return_value={}):
            console=DesktopConsole(self.pet)
        self.addCleanup(console.close)
        names=[console.tabs.tabText(i) for i in range(console.tabs.count())]
        self.assertTrue({'插件市场','桌面识别','桌宠习惯','智能体分工'}.issubset(names))
        self.assertEqual(self.pet.calls,[]);self.assertFalse(self.pet._experience.observe_timer.isActive())

    def test_face_uses_bounded_cache_and_keeps_silhouette(self):
        face = FaceDetails(ROOT / 'resources', 250, 340)
        open_face = face.frame('surprised', 1., .1, speaking=True)
        closed_face = face.frame('surprised', 0., .8, speaking=True)
        self.assertNotEqual(open_face.toImage(), closed_face.toImage())
        self.assertEqual(open_face.copy(0, 0, 250, 100).toImage(), closed_face.copy(0, 0, 250, 100).toImage())
        self.assertIs(face.frame('surprised', 1., .1, speaking=True), open_face)
        left = face.frame('surprised', gaze=(-1, 0))
        right = face.frame('surprised', gaze=(1, 0))
        self.assertNotEqual(left.toImage(), right.toImage())
        self.assertLessEqual(face._render.cache_info().currsize, 32)

    def test_walk_is_bounded_and_interruptible(self):
        # Motion contract only: artwork approval belongs to the shared animation owner.
        anim=SimpleNamespace(_walking=False,_being_dragged=False,_speaking=False,_music_stage_active=False,
            _task_motion=SimpleNamespace(active=False),_idle_motion_blocked=lambda:False,
            active_clip=None,clips={'personality:walk':dict(frames=[None]*30,fps=30)})
        anim.set_walking=lambda value,*args:setattr(anim,'_walking',bool(value))
        physics=DesktopPetPhysics(self.pet,anim);self.addCleanup(physics.stop)
        self.assertTrue(physics.walk(99999,seconds=1.,lead=0.,tail=0.))
        self.assertTrue(anim._walking)
        physics.started=time.monotonic()-2;physics.last=time.monotonic()-.01;physics._tick()
        self.assertFalse(anim._walking);self.assertTrue(self.pet.screen().availableGeometry().contains(self.pet.frameGeometry()))
        physics.walk(-100);physics.press(self.pet.pos());self.assertFalse(anim._walking)

    def test_drag_interrupts_clips_and_click_reacts_immediately(self):
        anim=SpriteAnimator(str(ROOT/'resources'),250);anim.stop();self.addCleanup(anim.stop)
        anim.tap();self.assertEqual(anim._tap_elapsed,0)
        anim.set_dragging(True,80);self.assertTrue(anim._presence.dragging)
        self.assertFalse((anim.active_clip or '').startswith('personality:'))
        anim.set_dragging(False);self.assertFalse(anim._presence.dragging)

    def test_managed_routing_reuses_workers_without_real_gateway(self):
        from core.managed_chat import ManagedChats
        from core.chat_workspace import ChatWorkspace
        store=ChatWorkspace(Path(self.temp.name)/'chat.sqlite3')
        cid=store.create_conversation('分工检查')
        fake_codex=SimpleNamespace(bound=lambda cid:{},start=lambda *a,**k:None,send=lambda *a,**k:None)
        panel=QWidget();panel.store=store;panel.pet=self.pet;panel.runtime=SimpleNamespace(codex=fake_codex)
        manager=ManagedChats(panel);manager.timer.stop();self.addCleanup(manager.timer.stop)
        manager.provision=lambda cid:None
        self.pet._config._s.setValue('team/enabled',True)
        child=manager.target(cid,'修复项目代码')
        self.assertEqual(manager.get(child)['backend'],'codex')
        self.assertEqual(manager.target(cid,'开发另一个模块'),child)
        self.assertEqual(manager.target(cid,'审计代码'),cid)

    def test_review_handoff_is_durable_and_never_marks_dispatch_as_passed(self):
        from core.managed_chat import ManagedChats
        from core.chat_workspace import ChatWorkspace
        store=ChatWorkspace(Path(self.temp.name)/'review.sqlite3');cid=store.create_conversation('执行结果')
        calls=[];fake_codex=SimpleNamespace(bound=lambda cid:{},start=lambda *args:calls.append(args))
        panel=QWidget();panel.store=store;panel.pet=self.pet;panel.runtime=SimpleNamespace(codex=fake_codex)
        manager=ManagedChats(panel);manager.timer.stop();self.addCleanup(manager.timer.stop)
        manager.put(manager.get(cid),goal='修复模块'*4000,backend='dsh')
        review=manager.start_review(cid,'turn-1','执行器自述'*5000)
        self.assertEqual(manager.start_review(cid,'turn-1','重复回执'),review)
        self.assertEqual(len(calls),1);self.assertLessEqual(len(calls[0][0]),16000)
        self.assertEqual(manager.get(cid)['review_state'],'starting')


if __name__=='__main__':unittest.main()
