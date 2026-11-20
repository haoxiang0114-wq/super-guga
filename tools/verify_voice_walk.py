# -*- coding: utf-8 -*-
"""语音操控走路的端到端验证（无网络、无麦克风）。

模拟语音层的真实分发：ASR 文本 → fast_commands.match → voice._dispatch_fast
→ fast_command 信号 → panel._fast_command → pet.walk_command。
这里从 panel._fast_command 开始接管（上游由 match 保证），看桌宠是否真的动。
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ['QUGA_SUPER_AGENT'] = 'off'
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from launcher import _prepare_qt  # noqa: E402

_prepare_qt()

from PyQt5.QtCore import QSettings, QTimer  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from core import fast_commands  # noqa: E402

OUT = ROOT / 'data' / '_walk_raw'


def main():
    app = QApplication.instance() or QApplication([])
    report = dict(steps=[], phrases=[])

    folder = tempfile.mkdtemp(prefix='guga-voice-walk-')
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, folder)

    from core.chat_workspace import ChatWorkspace
    from ui.pet_window import PetWindow
    from ui.agent_chat import AgentChatWindow

    with patch('core.keyboard_hook.KeyboardHook.start'), \
         patch('core.reflex.start_foreground_tracker'), \
         patch('ui.tts_engine.TTSEngine.speak'):
        pet = PetWindow()
        panel = AgentChatWindow(pet, ChatWorkspace(Path(folder) / 'chat.db'))
        pet._agent_panel = panel
        pet.show()
        app.processEvents()
        panel.voice_active = True          # 真实语音通话中该值就是 True

        screen = pet.screen().availableGeometry()
        pet.move(screen.left() + 130, screen.bottom() - pet.height() - 60)

        def note(tag):
            rec = dict(tag=tag, x=pet.x(), clip=getattr(pet._anim2d, '_clip', None),
                       walking=bool(pet._anim2d.walking),
                       last=panel._last_fast_result if isinstance(getattr(panel, '_last_fast_result', None), dict) else None)
            report['steps'].append(rec)
            return rec

        def say(text):
            """完整复刻语音层：解析 → 组 dispatch → 走 panel 的接收端。"""
            fast = fast_commands.match(text) or {}
            rec = dict(text=text, matched=bool(fast.get('matched')),
                       action=fast.get('action'), kind=fast.get('kind'), target=fast.get('target'))
            report['phrases'].append(rec)
            if fast.get('matched'):
                dispatch = dict(fast, fast_id='voice-sim', recognized_at=0.0, asr_after_speech_ms=None)
                panel._fast_command(dispatch)
            return rec

        chain = [
            (0, lambda: note('start')),
            (100, lambda: say('向左走')),
            (800, lambda: note('left_moving')),
            (3200, lambda: note('left_done')),
            (3300, lambda: say('向右走')),
            (4000, lambda: note('right_moving')),
            (6800, lambda: note('right_done')),
            (6900, lambda: say('往左走一点')),
            (7350, lambda: say('别走了')),
            (7600, lambda: note('stopped')),
            (8600, lambda: note('stopped_hold')),
            (8700, lambda: say('向右看看')),   # 不该触发走路
            (9000, lambda: note('non_walk_phrase')),
        ]

        def finish():
            report['ok'] = True
            report['clips'] = pet._anim2d.walk_clips
            (OUT / 'voice_walk_report.json').write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(report, ensure_ascii=False, indent=2))
            try:
                pet._anim2d.stop_walk()
                panel.shutdown()
                pet._screen_tools.stop()
                pet._self_mcp.close()
                pet._tray.hide()
            except Exception as exc:  # noqa: BLE001
                print('cleanup:', exc)
            app.quit()

        for ms, fn in chain:
            QTimer.singleShot(ms, fn)
        QTimer.singleShot(9500, finish)

        app.exec_()


if __name__ == '__main__':
    main()
