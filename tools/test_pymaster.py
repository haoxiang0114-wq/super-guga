"""PyMaster 集成回归：查词动作、划词窗口、工具注册、回执裁剪。

不联网、不开浏览器，纯离屏 Qt + 纯函数校验；真正的页面链路在
tools/probe_pymaster_bridge.py --live 里跑。

    QT_QPA_PLATFORM=offscreen python tools/test_pymaster.py
"""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class GlossaryTests(unittest.TestCase):
    def test_bundled_glossary_is_present(self):
        from core.pymaster_bridge import glossary_terms
        terms = glossary_terms()
        self.assertGreaterEqual(len(terms), 50)
        self.assertTrue(all({'term', 'desc'} <= set(item) for item in terms[:5]))

    def test_lookup_exact_chinese_and_english(self):
        from core.pymaster_bridge import lookup_term
        hit = lookup_term('闭包')
        self.assertTrue(hit['ok'])
        self.assertEqual(hit['matches'][0]['term'], '闭包')
        self.assertIn('Closure', hit['matches'][0]['en'])
        self.assertTrue(lookup_term('list')['ok'])

    def test_lookup_rejects_empty_and_unknown(self):
        from core.pymaster_bridge import lookup_term
        self.assertFalse(lookup_term('')['ok'])
        self.assertFalse(lookup_term('这个词术语表里真的没有')['ok'])

    def test_lookup_parses_punctuation_around_word(self):
        from core.pymaster_bridge import lookup_term
        self.assertTrue(lookup_term('「函数」。')['ok'])


class SelectionActionTests(unittest.TestCase):
    def test_lookup_label_and_prompt(self):
        from core.selection_actions import ACTION_LABELS, prompt
        self.assertEqual(ACTION_LABELS['lookup'], '查词')
        text = prompt('闭包', 'lookup')
        self.assertIn('PyMaster 术语表', text)
        self.assertIn('180 字', text)
        self.assertIn('原文开始', text)

    def test_lookup_without_glossary_hit_still_asks(self):
        from core.selection_actions import prompt
        text = prompt('zzz_unknown_token', 'lookup')
        self.assertNotIn('PyMaster 术语表', text)
        self.assertIn('查词', text)

    def test_glossary_hint_contains_definition(self):
        from core.selection_actions import glossary_hint
        hint = glossary_hint('切片')
        self.assertIn('切片', hint)
        self.assertGreater(len(hint), 10)


class AgentToolTests(unittest.TestCase):
    def test_tool_registered_with_actions(self):
        from core.self_agent import TOOLS
        names = {item['function']['name'] for item in TOOLS}
        self.assertIn('pymaster', names)
        spec = next(item['function'] for item in TOOLS if item['function']['name'] == 'pymaster')
        actions = spec['parameters']['properties']['action']['enum']
        for action in ('open', 'chapter', 'kp', 'next', 'open_exercise', 'set_code', 'run', 'explain', 'glossary'):
            self.assertIn(action, actions)

    def test_persona_mentions_pymaster(self):
        from core.self_agent import PERSONA
        self.assertIn('pymaster', PERSONA)

    def test_view_trims_dom_noise(self):
        from ui.agent_runtime import AppRuntime
        view = AppRuntime._pymaster_view('state', dict(
            ok=True, kps=[dict(index=i, title='k%d' % i) for i in range(30)],
            kp_text='x' * 5000, modal=dict(open=False, prompt='旧题'),
            playground=dict(code='y' * 3000, output='z' * 3000), state=dict(big='drop')))
        self.assertEqual(len(view['kps']), 20)
        self.assertEqual(view['kps_total'], 30)
        self.assertLessEqual(len(view['kp_text']), 2700)
        self.assertEqual(view['modal'], dict(open=False))
        self.assertLessEqual(len(view['playground']['code']), 1200)
        self.assertNotIn('state', view)


class ScreenToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_selection_bar_has_lookup_button(self):
        from ui.screen_tools import SelectionBar
        from PyQt5.QtWidgets import QPushButton
        bar = SelectionBar()
        try:
            labels = [b.text() for b in bar.findChildren(QPushButton)]
        finally:
            bar.deleteLater()
        self.assertIn('查词', labels)

    def test_lookup_dialog_mode_and_speech_guard(self):
        from ui.screen_tools import ExplainDialog
        from PyQt5.QtWidgets import QWidget

        class Pet(QWidget):
            pass

        pet = Pet()          # 没有 _tts：查词朗读必须安静跳过，不能抛异常
        dialog = ExplainDialog(pet)
        try:
            dialog.load('闭包', auto=False, mode='lookup')
            self.assertEqual(dialog.mode, 'lookup')
            self.assertIn('查词', dialog.windowTitle())
            dialog.messages.append(dict(role='user', content='查词：闭包'))
            dialog.received(dialog.generation, '闭包：内部函数引用外部函数变量。', True)
            self.assertEqual(dialog.last_answer, '闭包：内部函数引用外部函数变量。')
            dialog.speak_lookup('**闭包**：内部函数引用外部函数的变量 `closure`')
        finally:
            dialog.close()

    def test_speak_lookup_respects_console_switch(self):
        from unittest import mock
        from ui.screen_tools import ExplainDialog
        from PyQt5.QtWidgets import QWidget

        spoken = []

        class Pet(QWidget):
            class _Tts:
                def speak(self, text):
                    spoken.append(text)
            _tts = _Tts()

        pet = Pet()
        dialog = ExplainDialog(pet)
        try:
            with mock.patch('core.pymaster_bridge.settings',
                            lambda: dict(url='', speak_lookup=False, headless=False)):
                dialog.speak_lookup('闭包：内部函数引用外部函数变量')
            self.assertEqual(spoken, [])
            with mock.patch('core.pymaster_bridge.settings',
                            lambda: dict(url='', speak_lookup=True, headless=False)):
                dialog.speak_lookup('闭包：内部函数引用外部函数变量')
            self.assertEqual(len(spoken), 1)
            self.assertIn('闭包', spoken[0])
        finally:
            dialog.close()


class ConsoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def make(self):
        from types import SimpleNamespace
        from ui.desktop_console import DesktopConsole
        pet = SimpleNamespace(_tts=SimpleNamespace(reload_settings=lambda: None, speak=lambda t: None),
                              _ai=SimpleNamespace(_cfg={}), _agent_panel=None)
        return DesktopConsole(pet)

    def test_settings_have_expected_keys(self):
        from core.pymaster_bridge import settings
        cfg = settings()
        self.assertTrue(cfg['url'].startswith('https://'))
        self.assertIsInstance(cfg['speak_lookup'], bool)
        self.assertIsInstance(cfg['headless'], bool)

    def test_console_has_pymaster_tab_and_values(self):
        from PyQt5.QtWidgets import QTabWidget
        console = self.make()
        try:
            tabs = console.findChild(QTabWidget)
            titles = [tabs.tabText(i) for i in range(tabs.count())]
            self.assertIn('学习助手（PyMaster）', titles)
            block = console.values()['pymaster']
            self.assertTrue(block['url'].startswith('http'))
            self.assertIsInstance(block['speak_lookup'], bool)
            self.assertIsInstance(block['headless'], bool)
        finally:
            console.close()

    def test_console_rejects_bad_url(self):
        console = self.make()
        try:
            console.pm_url.setText('ftp://workbuddy.link')
            with self.assertRaises(ValueError):
                console.values()
        finally:
            console.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
