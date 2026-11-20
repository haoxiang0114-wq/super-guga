# -*- coding: utf-8 -*-
"""桌宠走路实机验证：真实 PetWindow + 真实动画器，跑一遍语音口径的左右走。

产出：
- data/_walk_raw/live_report.json  每一步的坐标/片段/返回值
- data/_walk_raw/live_left.png     走的过程中抓的窗口画面
- data/_walk_raw/live_right.png
- data/_walk_raw/live_idle.png     停下后的待机画面
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtWidgets import QApplication  # noqa: E402
from PyQt5.QtCore import QTimer  # noqa: E402

from core import fast_commands  # noqa: E402

OUT = ROOT / 'data' / '_walk_raw'
OUT.mkdir(parents=True, exist_ok=True)

PHRASES = [
    '向左走', '往左走一点', '往左边走过去', '走两步', '随便走走',
    '向右走', '往右走', '停下', '别走了', '站住',
    '向右看看', '左边的文件在哪',
]


def parse_table():
    rows = []
    for text in PHRASES:
        hit = fast_commands.match(text) or {}
        walk = fast_commands.parse_walk(text)
        rows.append(dict(text=text, action=hit.get('action'),
                         kind=hit.get('kind'), target=hit.get('target'), walk=walk))
    return rows


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    from ui.pet_window import PetWindow

    report = dict(phrases=parse_table(), steps=[])
    pet = PetWindow()
    pet.show()
    screen = pet.screen().availableGeometry()

    def snap(name):
        p = OUT / ('live_%s.png' % name)
        pet.grab().save(str(p))
        return str(p.relative_to(ROOT))

    def note(tag, extra=None):
        rec = dict(tag=tag, x=pet.x(), y=pet.y(),
                   clip=getattr(pet._anim2d, '_clip', None),
                   walking=bool(pet._anim2d.walking),
                   mode=getattr(pet._pet_physics, 'mode', None))
        if extra:
            rec.update(extra)
        report['steps'].append(rec)
        return rec

    def place():
        # 放到屏幕中间偏左，两边都留出空间
        pet.move(screen.left() + 120, screen.bottom() - pet.height() - 60)

    def ask(text):
        """复刻真实链路：语音文本 → fast_commands 解析 → walk_command。"""
        hit = fast_commands.match(text) or {}
        rec = dict(text=text, hit=dict(action=hit.get('action'), kind=hit.get('kind'),
                                      target=hit.get('target')))
        if hit.get('action') == 'performance' and hit.get('kind') == 'walk':
            rec['ret'] = pet.walk_command(hit.get('target'))
        else:
            rec['ret'] = dict(ok=False, skipped='未命中走路快指令')
        return rec

    chain = []

    def at(ms, fn):
        chain.append((ms, fn))

    state = {}

    at(0, lambda: (place(), note('start', dict(screen=[screen.width(), screen.height()],
                                              size=[pet.width(), pet.height()]))))
    at(120, lambda: state.update(left=ask('向左走')) or note('left_cmd', dict(ret=state['left'])))
    at(700, lambda: note('left_moving', dict(img=snap('left'))))
    at(3200, lambda: note('left_settled'))
    at(3300, lambda: state.update(right=ask('向右走')) or note('right_cmd', dict(ret=state['right'])))
    at(3900, lambda: note('right_moving', dict(img=snap('right'))))
    at(6600, lambda: note('right_settled'))
    # 中途喊停：走 400ms 后立刻 stop
    at(6700, lambda: state.update(again=ask('往左走一点')) or note('walk_again', dict(ret=state['again'])))
    at(7150, lambda: state.update(stop=ask('停下')) or note('stop_cmd', dict(ret=state['stop'], img=snap('idle'))))
    at(7400, lambda: note('stop_hold_1'))
    at(8000, lambda: note('stop_hold_2'))
    # 撞边：一路向左给足 4 秒，看它是否贴边就停
    at(8100, lambda: (pet.move(screen.left() + 260, pet.y()),
                      state.update(edge=pet.walk('left', seconds=4.0)),
                      note('edge_cmd', dict(ret=state['edge']))))
    at(12600, lambda: note('edge_settled'))

    def finish():
        report['clips'] = pet._anim2d.walk_clips
        report['ok'] = True
        (OUT / 'live_report.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2))
        pet._anim2d.stop_walk()
        pet.hide()
        app.quit()

    total = 13000
    at(total, finish)

    for ms, fn in chain:
        QTimer.singleShot(ms, fn)

    app.exec_()


if __name__ == '__main__':
    main()
