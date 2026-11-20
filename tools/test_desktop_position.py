"""Standalone offscreen checks. Use temporary settings and never start the pet runtime."""
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from launcher import _prepare_qt


os.environ['QT_QPA_PLATFORM'] = 'offscreen'
_prepare_qt()

from PyQt5.QtCore import QObject, QPoint, QRect, QSettings, Qt, pyqtSignal
from PyQt5.QtWidgets import QApplication, QWidget

from core.config_manager import ConfigManager
from ui.desktop_position import visible_position
with patch('logging.basicConfig'):
    from ui.pet_window import PetWindow


PRIMARY = QRect(0, 0, 1920, 1040)
LEFT = QRect(-1600, 0, 1600, 900)


class ScreenEvents(QObject):
    screenAdded = pyqtSignal(object)
    screenRemoved = pyqtSignal(object)
    primaryScreenChanged = pyqtSignal(object)
    availableGeometryChanged = pyqtSignal(QRect)


class FakeScreenTools(QObject):
    selection_enabled_changed = pyqtSignal(bool)
    selection_enabled = False
    set_selection_enabled = Mock()


class PositionPet(QWidget):
    _restore_position = PetWindow._restore_position
    _watch_screen_layout = PetWindow._watch_screen_layout
    _watch_work_area = PetWindow._watch_work_area
    _queue_screen_recovery = PetWindow._queue_screen_recovery
    _recover_desktop_surfaces = PetWindow._recover_desktop_surfaces
    _reset_pos = PetWindow._reset_pos
    set_pet_hidden = PetWindow.set_pet_hidden

    def __init__(self, config):
        super().__init__(None, Qt.FramelessWindowHint)
        self.setFixedSize(250, 300)
        self._config = config
        self._is_drag = False
        self._pet_physics = SimpleNamespace(stop=Mock())
        self._agent_bubble = QWidget(self)
        self._agent_bubble.follow_pet = Mock()
        self._screen_tools = FakeScreenTools(self)

    def _unused(self):
        raise AssertionError('Position recovery must not invoke another feature')

    _open_desktop_console = _show_companion_settings = _open_voice_recording = _unused
    _open_agent_voice = _configure_realtime_voice = _show_daily_plan = _unused
    _open_agent_chat = _toggle_silent = _unused

    def _hide_inline(self):
        pass

    def _force_top(self):
        pass


class VisiblePositionTests(unittest.TestCase):
    def test_position_on_left_screen_is_preserved(self):
        rect = QRect(-1450, 50, 250, 300)
        self.assertEqual(visible_position(rect, [PRIMARY, LEFT]), rect.topLeft())

    def test_cross_screen_release_uses_screen_with_more_of_the_pet(self):
        rect = QRect(-180, 50, 250, 300)
        self.assertEqual(visible_position(rect, [PRIMARY, LEFT]), QPoint(-250, 50))

    def test_gap_falls_back_to_primary_work_area_including_its_offset(self):
        primary = QRect(300, 50, 1600, 850)
        rect = QRect(0, -520, 250, 300)
        self.assertEqual(visible_position(rect, [primary, LEFT]), QPoint(975, 325))

    def test_changed_taskbar_keeps_window_above_reserved_area(self):
        rect = QRect(1200, 800, 250, 240)
        self.assertEqual(visible_position(rect, [QRect(0, 0, 1920, 970)]), QPoint(1200, 730))

    def test_oversized_window_stays_anchored_inside_small_work_area(self):
        area = QRect(100, 80, 150, 200)
        self.assertEqual(visible_position(QRect(140, 120, 250, 300), [area]), area.topLeft())

    def test_missing_screens_leave_position_available_for_later_recovery(self):
        rect = QRect(-1200, 100, 250, 300)
        self.assertEqual(visible_position(rect, [QRect()]), rect.topLeft())


class DesktopPositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        resources = ExitStack()
        self.addCleanup(resources.close)
        resources.enter_context(patch('socket.socket.connect', side_effect=AssertionError('network blocked')))
        resources.enter_context(patch('subprocess.Popen', side_effect=AssertionError('process launch blocked')))
        folder = resources.enter_context(tempfile.TemporaryDirectory())
        self.config = ConfigManager.__new__(ConfigManager)
        self.config._s = QSettings(str(Path(folder) / 'position.ini'), QSettings.IniFormat)
        self.pet = PositionPet(self.config)
        self.addCleanup(self.pet.deleteLater)
        self.areas = resources.enter_context(
            patch('ui.desktop_position.available_work_areas', return_value=[PRIMARY, LEFT])
        )

    def test_startup_restores_saved_position_on_secondary_screen(self):
        self.config.save_pos(-1200, 150)
        self.pet._restore_position()
        self.assertEqual(self.pet.pos(), QPoint(-1200, 150))

    def test_startup_centers_position_from_disconnected_screen(self):
        self.config.save_pos(-1200, 150)
        self.areas.return_value = [PRIMARY]
        self.pet._restore_position()
        self.assertEqual(self.pet.pos(), QPoint(835, 370))

    def test_incomplete_and_invalid_settings_fall_back_as_a_pair(self):
        fallback = (104, 50)
        self.config._s.setValue('pos/x', -1400)
        self.assertEqual(self.config.load_pos(fallback), fallback)
        self.config._s.setValue('pos/y', 'invalid')
        self.assertEqual(self.config.load_pos(fallback), fallback)

    def test_screen_removal_recovers_hidden_pet_after_layout_event(self):
        self.pet.move(-1200, 200)
        self.pet.set_pet_hidden(True)
        screen = ScreenEvents()
        screens = ScreenEvents()
        screens.screens = lambda: [screen]
        with patch.object(QApplication, 'instance', return_value=screens):
            self.pet._watch_screen_layout()
        self.app.processEvents()
        self.assertEqual(self.pet.pos(), QPoint(-1200, 200))
        self.areas.return_value = [PRIMARY]
        screens.screenRemoved.emit(screen)
        self.assertTrue(self.pet._screen_layout_timer.isActive())
        self.app.processEvents()
        self.assertTrue(PRIMARY.contains(self.pet.geometry()))
        self.assertFalse(self.pet.isVisible())
        self.assertEqual(self.config.load_pos(), (self.pet.x(), self.pet.y()))

    def test_added_screen_is_watched_for_later_work_area_changes(self):
        self.pet.move(1250, 740)
        screens = ScreenEvents()
        screens.screens = lambda: []
        with patch.object(QApplication, 'instance', return_value=screens):
            self.pet._watch_screen_layout()
        screen = ScreenEvents()
        screens.screenAdded.emit(screen)
        self.app.processEvents()
        self.assertEqual(self.pet.y(), 740)
        self.areas.return_value = [QRect(0, 0, 1920, 1000)]
        screen.availableGeometryChanged.emit(self.areas.return_value[0])
        self.app.processEvents()
        self.assertEqual(self.pet.pos(), QPoint(1250, 700))

    def test_tray_recovery_shows_hidden_pet_and_saves_position(self):
        self.pet.set_pet_hidden(True)
        screen = SimpleNamespace(availableGeometry=lambda: PRIMARY)
        with patch.object(QApplication, 'primaryScreen', return_value=screen):
            self.pet._reset_pos()
        self.assertEqual(self.pet.pos(), QPoint(835, 370))
        self.assertTrue(self.pet.isVisible())
        self.assertFalse(self.config._s.value('desktop/pet_hidden', type=bool))
        self.assertEqual(self.config.load_pos(), (835, 370))


if __name__ == '__main__':
    unittest.main()
