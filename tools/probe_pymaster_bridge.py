"""PyMaster 桥的验收脚本：查词 → 开章 → 翻知识点 → 打开练习 → 写码 → 运行 → 讲解材料。

默认离线跑（术语表 + 依赖检查）；加 --live 才真的开浏览器访问线上站，
首次运行 Pyodide 要下载/启动内置 Python，请留出 1—2 分钟。

    python tools/probe_pymaster_bridge.py            # 离线检查
    python tools/probe_pymaster_bridge.py --live     # 连线上站全链路
    python tools/probe_pymaster_bridge.py --live --headed   # 看着它操作
"""
import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SAMPLE_CODE = 'print("姓名：咕嘎")\nprint("年龄：1")\nprint("学校：PyMaster")'


def offline():
    checks = []
    from core.pymaster_bridge import available, glossary_terms, lookup_term, SITE
    ok, reason = available()
    checks.append(('playwright 可用', ok, reason))
    terms = glossary_terms()
    checks.append(('内置术语表', len(terms) >= 50, '%d 条' % len(terms)))
    hit = lookup_term('闭包')
    checks.append(('查词：闭包', bool(hit.get('ok')), (hit.get('matches') or [{}])[0].get('desc', '')[:40]))
    hit = lookup_term('list')
    checks.append(('查词：英文 list', bool(hit.get('ok')), (hit.get('matches') or [{}])[0].get('term', '')))
    from core.selection_actions import ACTION_LABELS, prompt
    checks.append(('划词动作含查词', ACTION_LABELS.get('lookup') == '查词', ACTION_LABELS.get('lookup', '')))
    text = prompt('闭包', 'lookup')
    checks.append(('查词提示词带术语表', 'PyMaster 术语表' in text, '命中术语表定义' if 'PyMaster 术语表' in text else '未命中'))
    checks.append(('站点地址', SITE.startswith('https://'), SITE))
    failed = [name for name, ok, _ in checks if not ok]
    for name, ok, detail in checks:
        print(('  [OK]   ' if ok else '  [失败] ') + name + ((' — ' + str(detail)) if detail else ''))
    print('\n结果：%d 通过 / %d 失败' % (len(checks) - len(failed), len(failed)))
    return 0 if not failed else 1


def live(headed):
    if headed:
        os.environ.pop('GUGA_PYMASTER_HEADLESS', None)
    else:
        os.environ['GUGA_PYMASTER_HEADLESS'] = '1'
    from core.pymaster_bridge import PyMasterBridge, lookup_term
    bridge = PyMasterBridge()
    steps = []

    def step(name, ok, detail=''):
        steps.append((name, ok, detail))
        print(('  [OK]   ' if ok else '  [失败] ') + name + ((' — ' + str(detail)[:160]) if detail else ''))

    started = time.time()
    try:
        result = bridge.open()
        step('打开站点', bool(result.get('ok')), '%s / %s' % (result.get('kind'), (result.get('title') or '')[:30]))
        result = bridge.chapter(1)
        step('进入第 1 章', result.get('kind') == 'chapter' and result.get('chapter') == 1,
             '共 %d 个知识点' % len(result.get('kps') or []))
        result = bridge.kp(1)
        kp = result.get('kp') or {}
        step('打开第 2 个知识点', kp.get('index') == 1, (kp.get('title') or '')[:40])
        result = bridge.open_exercise(kp=1)
        step('打开课后练习', bool(result.get('exercise_opened')), '入口来源：' + ('直接点击' if not result.get('fallback_used') else '页面函数兜底'))
        result = bridge.set_code(SAMPLE_CODE)
        step('写入代码', result.get('editor') == 'chapter-editor', '%d 字' % int(result.get('chars') or 0))
        result = bridge.run_code(90)
        out = (result.get('output') or '').strip()
        step('运行代码', bool(result.get('ok')) and bool(out), (out[:80] or result.get('status') or '').replace('\n', ' '))
        step('运行结果可判读', '咕嘎' in out or '姓名' in out, out[:60].replace('\n', ' '))
        bundle = bridge.explain_bundle('这段代码为什么这样输出')
        step('讲解材料', bool(bundle.get('ok')) and bool(bundle.get('kp_text')),
             '知识点 %d 字 / 术语命中 %d 条 / 有报错 %s' % (
                 len(bundle.get('kp_text') or ''), len(bundle.get('glossary') or []), bundle.get('failed')))
        hit = lookup_term('闭包')
        step('查词（术语表）', bool(hit.get('ok')), (hit.get('matches') or [{}])[0].get('term', ''))
    finally:
        bridge.close()

    failed = [name for name, ok, _ in steps if not ok]
    print('\n全链路：%d 通过 / %d 失败，用时 %.1fs' % (len(steps) - len(failed), len(failed), time.time() - started))
    if failed:
        print('未通过：' + '、'.join(failed))
    return 0 if not failed else 1


def main():
    parser = argparse.ArgumentParser(description='PyMaster 桥验收')
    parser.add_argument('--live', action='store_true', help='连线上站跑完整流程')
    parser.add_argument('--headed', action='store_true', help='显示浏览器窗口（默认可视化关闭）')
    args = parser.parse_args()
    print('== 离线检查 ==')
    code = offline()
    if not args.live:
        return code
    print('\n== 线上全链路 ==')
    return live(args.headed) or code


if __name__ == '__main__':
    raise SystemExit(main())
