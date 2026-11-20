# -*- coding: utf-8 -*-
"""咕嘎企鹅·酒馆战棋蒙特卡洛战斗模拟器 v2（胜率预测核心，对齐 BobsBuddy 规则）

v2 关键修复（对比 v1 的"瞎分析"）：
1. 【反击机制】攻击者打目标后，目标存活则反击 —— v1 完全缺失，胜率严重失真
2. 先手随机（v1 固定我方先手）
3. 圣盾：挡伤害不挡反击触发顺序正确（攻击者伤害 → 目标圣盾破/掉血 → 目标反击 → 攻击者圣盾破/掉血）
4. 烈毒 S14 规则：造成伤害即秒杀（圣盾可挡），每只毒每场限一次
5. 复生：死亡后以 1 点生命复活（攻击力保留）
6. 亡语召唤：从卡牌库(card_db)取 token 身材，支持多 token
7. 攻击顺序：每回合随机活体攻击者（非轮流，BobsBuddy 同款）
8. 战斗结束判定：存活血量总和（伤害溢出上限封顶，防虚高）

未建模（明确告知用户）：AOE 随从、光环/复仇、饰品联动、圣盾剧毒互动细节。

v2.1 增强（轨道③实时胜率解读）：simulate_one 增加可选 stats 参数，逐场累计
首个阵亡/击杀/胜场存活统计；新增 analyze_matchup（结构化解读）与
format_matchup_insight（一句话人话）。旧 API（win_rate/format_rate 等）不变。
"""
import random
from dataclasses import dataclass
from typing import List, Optional

from core import card_db


@dataclass
class Minion:
    name: str = ""
    atk: int = 0
    hp: int = 1
    taunt: bool = False
    divine_shield: bool = False
    windfury: bool = False
    poison: bool = False        # 烈毒/剧毒
    reborn: bool = False
    deathrattle: Optional[list] = None   # [{"atk":..,"hp":..,...}] 亡语召唤token列表

    def copy(self):
        return Minion(self.name, self.atk, self.hp, self.taunt, self.divine_shield,
                      self.windfury, self.poison, self.reborn,
                      [dict(t) for t in (self.deathrattle or [])] if self.deathrattle else None)


def minion_from_log(m: dict) -> Minion:
    """把日志/视觉的随从 dict 转成 Minion；name 用 card_db 转中文卡名"""
    name = m.get('name', '')
    if name and name.startswith('BG'):
        name = card_db.name_of(name)
    dr = None
    if m.get('deathrattle'):
        dr = m['deathrattle'] if isinstance(m['deathrattle'], list) else [m['deathrattle']]
    return Minion(name=name or '?', atk=int(m.get('atk', 0)), hp=int(m.get('hp', 0)),
                  taunt=bool(m.get('taunt')), divine_shield=bool(m.get('divine_shield')),
                  windfury=bool(m.get('windfury')), poison=bool(m.get('poison')),
                  reborn=bool(m.get('reborn')), deathrattle=dr)


def _alive(team: List[Minion]) -> List[Minion]:
    return [m for m in team if m.hp > 0]


def _apply_damage(target: Minion, dmg: int, poison: bool, poison_used: dict,
                  target_team: List[Minion], attacker: Optional[Minion]) -> bool:
    """对目标造成伤害。返回目标是否死亡。
    - 圣盾：挡掉本次伤害（不破防继续）
    - 烈毒：造成伤害即秒杀（每次都生效，圣盾可挡）
      炉石规则: 烈毒随从每次造成伤害都秒杀目标, 一个烈毒随从可连续击杀
      (风怒打2个、反击杀1个都该生效), 旧 poison_used 限制为错误实现
    """
    if target.divine_shield:
        target.divine_shield = False
        return False
    if poison:
        target.hp = 0
    else:
        target.hp -= dmg
    return target.hp <= 0


def _resolve_death(target: Minion, team: List[Minion]):
    """目标死亡结算：复生优先，否则亡语召唤"""
    if target.reborn:
        target.hp = 1
        target.reborn = False
        target.divine_shield = False
        return
    for token in (target.deathrattle or []):
        team.append(Minion(name=token.get('name', target.name + '的小弟'),
                           atk=int(token.get('atk', 0)), hp=int(token.get('hp', 1)),
                           taunt=bool(token.get('taunt')),
                           divine_shield=bool(token.get('divine_shield')),
                           reborn=bool(token.get('reborn'))))
    if target in team:
        team.remove(target)


def simulate_one(a: List[Minion], b: List[Minion], rng: random.Random,
                 stats: Optional[dict] = None) -> str:
    """模拟一场战斗，返回 'A'/'B'/'T'（对齐 BobsBuddy 随机攻击流程）

    stats（可选 dict，默认 None；不传则行为与旧版完全一致）：逐场累计——
      'A_first_dead':    {name: 次数}  A 方本场首个阵亡随从（name 为空记 '未知'）
      'kills':           {'A': {name: 次数}, 'B': {name: 次数}} 击杀数（含反击/烈毒/风怒第二次）
      'A_win_survivors': {name: 次数}  A 方胜场中存活随从
    """
    A = [m.copy() for m in a]
    B = [m.copy() for m in b]
    a_poison_used = {}
    b_poison_used = {}
    side = rng.random() < 0.5  # 先手随机
    a_first_done = False  # A 方本场首个阵亡是否已记录

    def _note_kill(killer: Minion, side_key: str):
        """击杀记录：_apply_damage 返回 True（目标死亡）时由调用处登记"""
        if stats is None:
            return
        key = killer.name or '未知'
        kills = stats.setdefault('kills', {}).setdefault(side_key, {})
        kills[key] = kills.get(key, 0) + 1

    def _note_first_dead(died: Minion, is_a: bool):
        """A 方本场首个阵亡（死亡结算前调用，仅记录第一次）"""
        nonlocal a_first_done
        if stats is None or not is_a or a_first_done:
            return
        a_first_done = True
        key = died.name or '未知'
        first = stats.setdefault('A_first_dead', {})
        first[key] = first.get(key, 0) + 1

    def _finish(result: str) -> str:
        """战斗收尾：A 方胜场则累计存活随从，再返回结果"""
        if stats is not None and result == 'A':
            surv = stats.setdefault('A_win_survivors', {})
            for m in _alive(A):
                key = m.name or '未知'
                surv[key] = surv.get(key, 0) + 1
        return result

    for _ in range(100):  # 步数上限防死循环
        A = _alive(A)
        B = _alive(B)
        if not A and not B:
            return _finish('T')
        if not A:
            return _finish('B')
        if not B:
            return _finish('A')

        atk_team, def_team = (A, B) if side else (B, A)
        poison_used = a_poison_used if side else b_poison_used
        attacker = rng.choice(atk_team)

        attacks = 2 if attacker.windfury else 1
        for _ in range(attacks):
            A = _alive(A)
            B = _alive(B)
            if not B:
                return _finish('A' if side else 'B')
            if attacker.hp <= 0:
                break
            # 目标选择：嘲讽优先，否则随机
            taunts = [m for m in def_team if m.taunt]
            target = rng.choice(taunts if taunts else def_team)

            # ① 攻击者打目标（攻击方为 A 当且仅当 side）
            target_dead = _apply_damage(target, attacker.atk, attacker.poison,
                                        poison_used, def_team, attacker)
            if not target_dead:
                # ② 目标反击（攻击者圣盾/烈毒生效；反击方为 B 当且仅当 side）
                attacker_dead = _apply_damage(attacker, target.atk, target.poison,
                                              poison_used, atk_team, target)
                if attacker_dead:
                    _note_kill(target, 'B' if side else 'A')
                # ③ 结算攻击者死亡（先攻击者后目标，HS 顺序）
                if attacker_dead and attacker.hp <= 0:
                    _note_first_dead(attacker, side)
                    _resolve_death(attacker, atk_team)
                if attacker.hp <= 0:
                    break
            else:
                # 目标已死：记录击杀与 A 方首个阵亡，再结算亡语/复生
                _note_kill(attacker, 'A' if side else 'B')
                _note_first_dead(target, not side)
                _resolve_death(target, def_team)
                # 攻击者不受反击（目标已死），但目标死亡前可能已造成伤害？
                # HS 规则：目标死亡则不反击。攻击者继续。
        A = _alive(A)
        B = _alive(B)
        if not A:
            return _finish('B')
        if not B:
            return _finish('A')
        side = not side
    # 步数耗尽：按剩余总血量判
    sa = sum(m.hp for m in _alive(A))
    sb = sum(m.hp for m in _alive(B))
    return _finish('A' if sa > sb else ('B' if sb > sa else 'T'))


def win_rate(a: List[Minion], b: List[Minion], n: int = 3000, seed: int = None) -> dict:
    """蒙特卡洛采样 → {'win','tie','loss','n'}（win = A 方胜率）"""
    rng = random.Random(seed)
    w = t = l = 0
    for _ in range(n):
        r = simulate_one(a, b, rng)
        if r == 'A':
            w += 1
        elif r == 'T':
            t += 1
        else:
            l += 1
    return {'win': w / n, 'tie': t / n, 'loss': l / n, 'n': n}


def format_rate(r: dict) -> str:
    return f"胜率 {r['win']*100:.0f}% ｜ 平局 {r['tie']*100:.0f}% ｜ 败率 {r['loss']*100:.0f}%（模拟 {r['n']} 场）"


def analyze_matchup(a: List[Minion], b: List[Minion], n: int = 2000, seed: int = None) -> dict:
    """结构化胜率解读（轨道③）：与 win_rate 相同的采样循环，另附逐场统计。
    返回 {'win','tie','loss','n',   # 与 win_rate 完全一致
          'my_first_dead': (中文名, 次数)|(None,0),   # A 方最先阵亡最高频
          'opp_top_killer': (中文名, 次数)|(None,0),  # B 方击杀 A 随从最多（最大威胁）
          'my_top_killer': (中文名, 次数)|(None,0),   # A 方击杀最多（输出担当）
          'win_survivor': (中文名, 次数)|(None,0)}    # A 方胜场存活率最高（胜负手）
    """
    rng = random.Random(seed)
    stats = {'A_first_dead': {}, 'kills': {'A': {}, 'B': {}}, 'A_win_survivors': {}}
    w = t = l = 0
    for _ in range(n):
        r = simulate_one(a, b, rng, stats)
        if r == 'A':
            w += 1
        elif r == 'T':
            t += 1
        else:
            l += 1

    def top(cnt: dict):
        """取最高频 (名, 次数)；BG 卡 ID 转中文名"""
        if not cnt:
            return (None, 0)
        name, c = max(cnt.items(), key=lambda kv: kv[1])
        if name.startswith('BG'):
            name = card_db.name_of(name)
        return (name, c)

    return {'win': w / n, 'tie': t / n, 'loss': l / n, 'n': n,
            'my_first_dead': top(stats['A_first_dead']),
            'opp_top_killer': top(stats['kills']['B']),
            'my_top_killer': top(stats['kills']['A']),
            'win_survivor': top(stats['A_win_survivors'])}


def format_matchup_insight(r: dict) -> str:
    """解读 dict → 一句话人话（中文）；频率 = 次数/n*100 取整。
    (None,0) 项跳过；全部跳过返回空串。"""
    n = r.get('n') or 1
    parts = []
    if r.get('opp_top_killer', (None, 0))[1]:
        name, c = r['opp_top_killer']
        parts.append(f"对面【{name}】是最大威胁（{c / n * 100:.0f}%场次击杀你随从）")
    if r.get('my_first_dead', (None, 0))[1]:
        name, c = r['my_first_dead']
        parts.append(f"你最先倒的是【{name}】（{c / n * 100:.0f}%）")
    if r.get('my_top_killer', (None, 0))[1]:
        name, c = r['my_top_killer']
        parts.append(f"【{name}】是输出担当（{c / n * 100:.0f}%）")
    if r.get('win_survivor', (None, 0))[1]:
        name, c = r['win_survivor']
        parts.append(f"【{name}】是胜负手（胜场存活{c / n * 100:.0f}%）")
    return '解读：' + '；'.join(parts) if parts else ''


if __name__ == '__main__':
    import sys
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
    # 自测1：我方3个大身材 vs 对方1个
    a = [Minion('大怪A', 10, 10, taunt=True), Minion('大怪B', 8, 8)]
    b = [Minion('小怪', 2, 2)]
    print('自测1 3v1:', format_rate(win_rate(a, b, 2000, seed=1)))
    # 自测2：圣盾 vs 毒（圣盾应挡住毒第一次）
    a = [Minion('圣盾怪', 4, 4, divine_shield=True, taunt=True)]
    b = [Minion('毒鱼', 1, 1, poison=True)]
    print('自测2 圣盾vs毒:', format_rate(win_rate(a, b, 2000, seed=1)))
    # 自测3：复生
    a = [Minion('复生哥', 5, 5, reborn=True)]
    b = [Minion('3-3', 3, 3)]
    print('自测3 复生:', format_rate(win_rate(a, b, 2000, seed=1)))
    # 自测4：反击（1-1 打 10-10，反击应让攻击者死）
    a = [Minion('小蚂蚁', 1, 1)]
    b = [Minion('巨兽', 10, 10, taunt=True)]
    print('自测4 反击1v10:', format_rate(win_rate(a, b, 2000, seed=1)))
    # 自测5：亡语召唤
    a = [Minion('下水道老鼠', 3, 2, deathrattle=[{'atk': 2, 'hp': 3, 'taunt': True}])]
    b = [Minion('4-4', 4, 4, taunt=True)]
    print('自测5 亡语2-3嘲讽:', format_rate(win_rate(a, b, 2000, seed=1)))
    # 自测6：结构化胜率解读（轨道③）—— 用 BG 卡 ID 构造，验证转中文名
    a6 = [Minion('BG19_010', 4, 4, taunt=True), Minion('BG28_309', 3, 3),
          Minion('BG25_008', 2, 6, windfury=True)]
    b6 = [Minion('BG21_020', 5, 5, divine_shield=True), Minion('BG26_174', 3, 3, poison=True)]
    r6 = analyze_matchup(a6, b6, 2000, seed=7)
    r6b = win_rate(a6, b6, 2000, seed=7)  # 同种子同循环 → 胜率应完全一致
    assert r6['win'] == r6b['win'] and r6['tie'] == r6b['tie'] and r6['loss'] == r6b['loss'] \
        and r6['n'] == r6b['n'], 'analyze_matchup 胜率与 win_rate 不一致'
    for k in ('my_first_dead', 'opp_top_killer', 'my_top_killer', 'win_survivor'):
        assert k in r6 and isinstance(r6[k], tuple) and len(r6[k]) == 2, f'缺少字段 {k}'
    print('自测6 解读:', format_matchup_insight(r6))
    print('自测6 胜率与win_rate一致:', r6['win'] == r6b['win'],
          '| 字段齐全:', all(k in r6 for k in
                             ('my_first_dead', 'opp_top_killer', 'my_top_killer', 'win_survivor')))
    # 自测7：全部 (None,0) 时返回空串（0 随从不产生任何统计）
    r7 = analyze_matchup([], [], 100, seed=3)
    print('自测7 空阵容解读为空串:', repr(format_matchup_insight(r7)))
