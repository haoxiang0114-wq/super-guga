# -*- coding: utf-8 -*-
"""power.log 增量解析 → 酒馆战棋局面（v4：跨调用续读，长对局也完整）

- 首次：从尾部最多回看 8MB 找最后一个 CREATE_GAME 作为起点
- 之后：记录文件偏移，每次只读新增字节并增量更新实体状态（进程内常驻）
- 己方判定：第一个获得 HERO_ENTITY 的玩家实体 = 本地玩家 → 英雄 player 字段 = 控制器
"""
import os
import re
import glob
import hashlib

_TAG_RE = re.compile(r' tag=(\w+) value=(\S+)')
_FULL_RE = re.compile(r'FULL_ENTITY - Creating ID=(\d+) CardID=(\w+)')
_FULL_UP_RE = re.compile(r'FULL_ENTITY - Updating (?:\[entityName=[^\]]*? id=(\d+) [^\]]*?\]|Entity=(\d+)) CardID=(\w+)')
_SHOW_RE = re.compile(r'SHOW_ENTITY - Updating Entity=(\d+) CardID=(\w+)')
_TAGCHANGE_RE = re.compile(r'TAG_CHANGE Entity=(\d+)')
_BRACKET_RE = re.compile(r'Entity=\[entityName=[^\]]*? id=(\d+) zone=(\w+) zonePos=(\d+) cardId=(\w+) player=(\d+)\]')
_PLAYER_RE = re.compile(r'TAG_CHANGE Entity=([^ \]]+#\d+)')
_PLAYER_ENT_RE = re.compile(r'Player EntityID=(\d+) PlayerID=(\d+) GameAccountId=\[hi=(\d+) lo=(\d+)\]')
_LOCAL_PLAYER_RE = re.compile(r'DebugPrintGame\(\) - PlayerID=(\d+), PlayerName=')
_GAME_RE = re.compile(r'CREATE_GAME')
_GAME_ENTITY_RE = re.compile(r'TAG_CHANGE Entity=GameEntity')
_GAME_ENTITY_CREATE_RE = re.compile(r'GameEntity EntityID=(\d+)')
_INDENT_TAG_RE = re.compile(r' - +tag=\w+ value=\S+')


def _select_log_path(candidates, session_dirs, current_session=False):
    if current_session and session_dirs:
        # A fresh game process can create its session directory before it writes
        # Power.log. Never fall back to yesterday's complete game in that gap.
        latest=max(session_dirs,key=lambda p:(os.path.basename(p),os.path.getmtime(p)))
        current=os.path.join(latest,'Power.log')
        return current if os.path.isfile(current) else None
    existing=[p for p in candidates if os.path.isfile(p)]
    return max(existing,key=os.path.getmtime) if existing else None


def find_log_path(current_session=False):
    candidates = []
    session_dirs = []
    roots = ['G:\\游戏\\Battle.net\\Hearthstone', 'D:\\Hearthstone',
             os.path.expandvars(r'%ProgramFiles(x86)%\Hearthstone')]
    default = os.path.join(os.environ.get('LOCALAPPDATA', ''),
                           'Blizzard', 'Hearthstone', 'Logs', 'Power.log')
    candidates.append(default)
    try:
        import winreg
        for hive, key in (
            (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Blizzard Entertainment\Hearthstone'),
            (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Blizzard Entertainment\Hearthstone'),
        ):
            try:
                k = winreg.OpenKey(hive, key)
                inst, _ = winreg.QueryValueEx(k, 'InstallPath')
                winreg.CloseKey(k)
                if inst:
                    roots.append(inst)
                    candidates.append(os.path.join(inst, 'Logs', 'Power.log'))
            except OSError:
                continue
    except Exception:
        pass
    for root in dict.fromkeys(roots):
        logs_dir = os.path.join(root, 'Logs')
        if os.path.isdir(logs_dir):
            for d in glob.glob(os.path.join(logs_dir, 'Hearthstone_*')):
                if os.path.isdir(d):
                    session_dirs.append(d)
                candidates.append(os.path.join(d, 'Power.log'))
            candidates.append(os.path.join(logs_dir, 'Power.log'))
    return _select_log_path(candidates,session_dirs,current_session)


LOG_PATH = find_log_path()

# ---- 增量解析状态（进程内常驻）----
_ents = {}
_players = {}
_hero_player = {}
_hero_order = []
_player_order = []
_player_id_to_ent = {}   # PlayerID(controller) -> 玩家实体 EntityID
_local_controller = None
_last_offset = 0
_initialized = False
_game_serial = 0
_game_token = ''


def _reset():
    global _ents, _players, _hero_player, _hero_order, _player_order
    global _player_id_to_ent, _local_controller, _last_offset, _initialized, _game_serial, _game_token
    _ents = {}
    _players = {}
    _hero_player = {}
    _hero_order = []
    _player_order = []
    _player_id_to_ent = {}
    _local_controller = None
    _last_offset = 0
    _initialized = False
    _game_serial = 0
    _game_token = ''


# 实体数量上限: 超过则清理非 PLAY 区实体, 防止开久了 _ents 无限增长
_ENT_LIMIT = 3000


def _prune_ents():
    """清理非 PLAY/SETASIDE 区旧随从, 防止 _ents 字典无限增长。"""
    if len(_ents) <= _ENT_LIMIT:
        return
    purged = {}
    for eid, e in _ents.items():
        zone = e.get("ZONE")
        cid = e.get("CARD_ID", "")
        ctype = e.get("CARDTYPE")
        if not cid or ctype in ("GAME", "HERO") or cid.endswith("_PH"):
            purged[eid] = e
            continue
        if zone in (None, "PLAY", "SETASIDE"):
            purged[eid] = e
            continue
    if len(purged) > _ENT_LIMIT:
        emergency = {}
        for eid, e in purged.items():
            zone = e.get("ZONE")
            cid = e.get("CARD_ID", "")
            ctype = e.get("CARDTYPE")
            if (not cid or ctype in ("GAME", "HERO") or cid.endswith("_PH")
                    or zone in (None, "PLAY")):
                emergency[eid] = e
        purged = emergency
    _ents.clear()
    _ents.update(purged)


def _parse_chunk(lines):
    """解析一批日志行，更新全局状态"""
    global _ents, _players, _hero_player, _hero_order, _player_order
    global _player_id_to_ent, _local_controller, _game_serial, _game_token
    pending = None  # 挂起实体：FULL_ENTITY/SHOW_ENTITY/Player 的缩进 tag 块归属
    for ln in lines:
        tags = _TAG_RE.findall(ln)
        eid = None
        cid = None
        player = None
        zone = None
        is_create = False

        m = _LOCAL_PLAYER_RE.search(ln)
        if m:
            _local_controller = int(m.group(1))
        m = _PLAYER_ENT_RE.search(ln)
        if m:
            eid = int(m.group(1))
            pid = int(m.group(2))
            _player_id_to_ent[pid] = eid
            player = pid
            is_create = True
            if int(m.group(3)) != 0:  # GameAccountId hi 非零 = 真人（本地玩家）
                _local_controller = pid
        if _GAME_RE.search(ln):
            eid = 0
            is_create = True
            _game_serial += 1
            _game_token = hashlib.sha256(
                f'{os.path.abspath(LOG_PATH or "")}|{ln}'.encode()).hexdigest()[:24]
        elif eid is None:
            m = _GAME_ENTITY_CREATE_RE.search(ln)
            if m:
                eid = int(m.group(1))  # 重载 dump: GameEntity EntityID=13
                is_create = True
            else:
                m = _BRACKET_RE.search(ln)
                if m:
                    eid = int(m.group(1))
                    zone = m.group(2)
                    cid = m.group(4)
                    player = int(m.group(5))
                    if 'HERO' in cid and not cid.endswith('_PH') and eid not in _hero_order:
                        _hero_order.append(eid)
                    if player is not None:
                        _hero_player[eid] = player
                    # 只有实体创建/身份更新行（FULL_ENTITY / SHOW_ENTITY）的
                    # bracket zone 才是权威状态；TAG_CHANGE 行 bracket 是变化前快照，
                    # 一律忽略，避免把 REMOVEDFROMGAME 又改回 PLAY
                    if 'FULL_ENTITY' in ln or 'SHOW_ENTITY' in ln:
                        is_create = True
                else:
                    m = _FULL_UP_RE.search(ln)
                    if m:
                        eid = int(m.group(1) or m.group(2))
                        cid = m.group(3)
                        is_create = True
                    else:
                        m = _PLAYER_RE.search(ln)
                        if m:
                            pname = m.group(1)
                            if pname not in _players:
                                _players[pname] = {}
                                _player_order.append(pname)
                            for tag, value in tags:
                                _players[pname][tag] = _val(value)
                            continue
                        if _GAME_ENTITY_RE.search(ln):
                            eid = 0  # TAG_CHANGE Entity=GameEntity tag=TURN ...
                        else:
                            m = _TAGCHANGE_RE.search(ln)
                            if m:
                                eid = int(m.group(1))
                            else:
                                m = _FULL_RE.search(ln)
                                if m:
                                    eid = int(m.group(1))
                                    cid = m.group(2)
                                    is_create = True
                                else:
                                    m = _SHOW_RE.search(ln)
                                    if m:
                                        eid = int(m.group(1))
                                        cid = m.group(2)
                                        is_create = True

        if eid is not None:
            pending = eid
        elif _INDENT_TAG_RE.search(ln) and pending is not None and not _BRACKET_RE.search(ln):
            # FULL_ENTITY/SHOW_ENTITY/Player/GameEntity 后跟的缩进 tag 块，归属挂起实体
            eid = pending
        else:
            pending = None
            continue

        ent = _ents.setdefault(eid, {})
        if cid:
            ent['CARD_ID'] = cid
        if is_create and zone:
            ent['ZONE'] = zone
        if player is not None:
            ent['CONTROLLER'] = player
        for tag, value in tags:
            ent[tag] = _val(value)

    _prune_ents()


def _init():
    """首次：定位最近 CREATE_GAME，从那里开始解析"""
    global _last_offset, _initialized
    if not LOG_PATH or not os.path.exists(LOG_PATH):
        return
    size = os.path.getsize(LOG_PATH)
    look = min(size, 8 * 1024 * 1024)
    with open(LOG_PATH, 'rb') as f:
        f.seek(size - look)
        data = f.read(look)
    # 字节级找最后一个 CREATE_GAME 行首（避免 UTF-8 中文导致字符/字节偏移错位）
    off = -1
    for m in re.finditer(rb'(?m)^.*CREATE_GAME.*$', data):
        off = m.start()
    if off >= 0:
        _last_offset = size - look + off
        _initialized = True
        return
    # 窗口内没找到（文件大/窗口太小）：全文件解析，保证 Player/英雄信息完整
    with open(LOG_PATH, 'rb') as f:
        full = f.read()
    _parse_chunk(full.decode('utf-8', 'replace').splitlines())
    _last_offset = size
    _initialized = True


def read_current_board():
    global _last_offset, _initialized, LOG_PATH
    # ---- 关键：每次读取重新查找最新日志会话 ----
    # 游戏每次启动会新建 Hearthstone_时间戳 目录；桌宠启动后游戏新开一局时，
    # 必须切换到新会话的 Power.log，否则一直读旧日志误判"没在玩"。
    _refresh_log_path()
    if not LOG_PATH or not os.path.exists(LOG_PATH):
        return None
    # ---- 对局新鲜度：日志太久没写入 = 没在玩，绝不拿旧日志瞎分析 ----
    import time as _time
    mtime = os.path.getmtime(LOG_PATH)
    stale_min = ( _time.time() - mtime) / 60.0
    if stale_min > 15:
        return {'active': False, 'reason': 'log_stale', 'stale_min': round(stale_min, 1),
                'game_serial': _game_serial, 'game_key': _current_game_key()}
    if not _initialized:
        _init()
    size = os.path.getsize(LOG_PATH)
    if size < _last_offset or size - _last_offset > 16 * 1024 * 1024:
        # 日志轮转/新会话：重置重来
        _reset()
        _init()
        size = os.path.getsize(LOG_PATH)
    if size == _last_offset:
        b = _build_board()
    else:
        with open(LOG_PATH, 'rb') as f:
            f.seek(_last_offset)
            data = f.read(size - _last_offset)
        _last_offset = size
        text = data.decode('utf-8', 'replace')
        lines = text.splitlines()
        if lines and not (lines[0].startswith('D ') or 'CREATE_GAME' in lines[0]):
            lines = lines[1:]  # 丢弃半行
        # 同一会话内新开一局（新 CREATE_GAME）：重置后重新完整解析
        if any('CREATE_GAME' in ln for ln in lines):
            _reset()
            # 从本行所在位置重新解析：先定位到最新 CREATE_GAME，再解析其后的全部内容
            size2 = os.path.getsize(LOG_PATH)
            with open(LOG_PATH, 'rb') as f:
                f.seek(_last_offset)
                data2 = f.read(size2 - _last_offset)
            text2 = data2.decode('utf-8', 'replace')
            lines2 = text2.splitlines()
            if lines2 and not (lines2[0].startswith('D ') or 'CREATE_GAME' in lines2[0]):
                lines2 = lines2[1:]
            _parse_chunk(lines2)
            b = _build_board()
            if b:
                b['active'] = b.get('reason') != 'game_over'
            return b
        _parse_chunk(lines)
        b = _build_board()
    if b:
        b['active'] = b.get('reason') != 'game_over'
    return b


def _refresh_log_path():
    """重新查找最新的 Power.log；发现新会话目录就切换并重置解析状态"""
    global LOG_PATH, _last_offset, _initialized
    try:
        new_path = find_log_path()
        if not new_path:
            return
        if new_path == LOG_PATH:
            return
        if LOG_PATH and os.path.exists(LOG_PATH):
            # 旧日志仍在更新（同一会话）→ 不切
            try:
                if os.path.getmtime(LOG_PATH) >= os.path.getmtime(new_path) - 5:
                    return
            except OSError:
                pass
        # 新会话：切换路径并重置
        LOG_PATH = new_path
        _reset()
    except Exception:
        pass


def _count_eliminated() -> int:
    """统计本局已出局玩家数（PLAYSTATE=LOST/CONCEDED）。
    玩家实体无 CARD_ID（区别于随从/英雄）、CARDTYPE 非 GAME（区别于 GameEntity）。"""
    out = 0
    for e in _ents.values():
        if e.get('PLAYSTATE') in ('LOST', 'CONCEDED') \
                and not e.get('CARD_ID') and e.get('CARDTYPE') != 'GAME':
            out += 1
    return out


def _find_real_hero(controller):
    """同 controller 下 PLAY 区的真实英雄（排除 *_PH 占位）。"""
    for eid, e in _ents.items():
        if (e.get('CARDTYPE') == 'HERO' and e.get('ZONE') == 'PLAY'
                and e.get('CONTROLLER') == controller
                and not e.get('CARD_ID', '').endswith('_PH')):
            return eid
    return None


def _resolve_my_hero(my_controller):
    """定位本地英雄，返回 (my_hero_id, hero_entity)；找不到时 my_hero_id 为 None。

    通过本地玩家实体（Player EntityID=xx）的 HERO_ENTITY tag 定位英雄；
    酒馆战棋里该 tag 常指向占位英雄（*_PH，SETASIDE 区），真正的英雄是同
    CONTROLLER 下 PLAY 区的 HERO 实体 —— 换用真实英雄。仍找不到就退回
    _hero_order 的第一个。注意：走 _hero_order 兜底时不重新读取 hero 实体。
    """
    my_hero_id = None
    p_ent_id = _player_id_to_ent.get(my_controller) if my_controller is not None else None
    if p_ent_id is not None:
        my_hero_id = _ents.get(p_ent_id, {}).get('HERO_ENTITY')
    if my_hero_id is not None:
        my_hero_id = int(my_hero_id)
    hero = _ents.get(my_hero_id, {}) if my_hero_id is not None else {}
    if hero.get('ZONE') != 'PLAY' or hero.get('CARD_ID', '').endswith('_PH'):
        real_hero = _find_real_hero(my_controller)
        if real_hero is not None:
            my_hero_id = real_hero
            hero = _ents[real_hero]
    # 兜底：第一个真实英雄
    if my_hero_id is None and _hero_order:
        my_hero_id = _hero_order[0]
    return my_hero_id, hero


def _hero_controller(my_hero_id):
    """英雄的控制器：优先 _hero_player 映射，其次实体上的 CONTROLLER tag。"""
    return _hero_player.get(my_hero_id, _ents.get(my_hero_id, {}).get('CONTROLLER'))


def _resolve_player_entity(my_controller):
    """承载 RESOURCES 的玩家实体：玩家实体优先，其次第一个 name#id 实体。"""
    p_ent = {}
    for pname in _player_order:  # name#id 实体上更新了 RESOURCES 等
        p_ent = _players[pname]
        break
    p_eid = _player_id_to_ent.get(my_controller)
    if p_eid is not None:
        pe = _ents.get(p_eid, {})
        if pe.get('RESOURCES') is not None:
            p_ent = pe
    return p_ent


def _minion_entry(ent, name, db):
    """随从的展示字段（己方/对手共用；zone_pos 由己方收集器追加）。"""
    return {
        'name': name,
        'atk': ent.get('ATK', 0),
        'hp': ent.get('HEALTH', 0),
        'taunt': ent.get('TAUNT', 0) == 1,
        'divine_shield': ent.get('DIVINE_SHIELD', 0) == 1,
        'windfury': ent.get('WINDFURY', 0) == 1 or ent.get('MEGA_WINDFURY', 0) == 1,
        'poison': ent.get('POISONOUS', 0) == 1 or ent.get('VENOMOUS', 0) == 1,
        'reborn': ent.get('REBORN', 0) == 1,
        'golden': ent.get('GOLDEN', 0) == 1,
        'deathrattle': [db['deathrattle']] if db and db.get('deathrattle') else None,
    }


def _collect_my_minions(controller):
    """己方场面：PLAY 区（或 ZONE 未知）的随从，按 ZONE_POSITION 排序。"""
    from core import card_db
    out = []
    for eid, ent in _ents.items():
        if ent.get('CONTROLLER') != controller:
            continue
        cid = ent.get('CARD_ID', '')
        if 'HERO' in cid:
            continue
        if ent.get('ZONE') not in ('PLAY', None):
            continue
        if ent.get('CARDTYPE') not in (None, 'MINION'):
            continue
        if not ent.get('ATK') and not ent.get('HEALTH'):
            continue
        db = card_db.lookup(cid) if cid.startswith('BG') else None
        entry = _minion_entry(ent, cid or f'#{eid}', db)
        entry['zone_pos'] = ent.get('ZONE_POSITION', 99)
        out.append(entry)
    out.sort(key=lambda x: x['zone_pos'])
    return out


def _collect_opponent_minions(my_controller):
    """对手：仅当前战斗中在 PLAY 区的随从（按控制器分组，取数量最多的那家）。"""
    from core import card_db as _card_db
    opp_groups = {}
    for ent in _ents.values():
        c = ent.get('CONTROLLER')
        if c is None or c == my_controller:
            continue
        cid = ent.get('CARD_ID', '')
        if 'HERO' in cid or not cid.startswith('BG'):
            continue
        if ent.get('ZONE') != 'PLAY':
            continue
        if not ent.get('ATK') and not ent.get('HEALTH'):
            continue
        _db = _card_db.lookup(cid)
        opp_groups.setdefault(c, []).append(_minion_entry(ent, cid, _db))
    if opp_groups:
        return max(opp_groups.values(), key=len)
    return []


def _game_entity():
    """GameEntity：优先实体 0；重载 dump 中它是数字实体 id（如 EntityID=13）。"""
    game = _ents.get(0, {})
    if not game.get('TURN'):
        for ge in _ents.values():
            if ge.get('CARDTYPE') == 'GAME' and ge.get('TURN'):
                return ge
    return game


def _local_playstate(my_controller):
    """本地玩家实体的 PLAYSTATE（拿不到就是 None）。"""
    if my_controller is None:
        return None
    p_eid = _player_id_to_ent.get(my_controller)
    if p_eid is None:
        return None
    return _ents.get(p_eid, {}).get('PLAYSTATE')


def _game_over_board(playstate, place, turn):
    """对局已结束的返回结构（两条结算路径共用）。"""
    return {'active': False, 'reason': 'game_over', 'playstate': playstate,
            'place': place, 'turn': turn,
            'game_serial': _game_serial, 'game_key': _current_game_key()}


def _final_game_over_board(hero, my_controller, game):
    """STEP=FINAL_GAMEOVER/FINAL_WRAPUP：以 FINAL 状态为准，不数 LOST 实体。

    FINAL_GAMEOVER is authoritative even if a player entity still carries
    PLAYING during the result screen. Do not expose a finished match as live.
    The final placement is written to the local hero as
    PLAYER_LEADERBOARD_PLACE. Counting LOST entities is not a placement: at
    the result screen tags may already be cleared, and that previously
    misreported a 4th-place finish as 1st.
    """
    local_ps = _local_playstate(my_controller)
    place_raw = hero.get('PLAYER_LEADERBOARD_PLACE')
    if place_raw is None and my_controller is not None:
        # At the result screen the original hero can be a placeholder or
        # an old copy. The newest local hero entity carries the final
        # placement tag.
        candidates = [(int(eid), ent.get('PLAYER_LEADERBOARD_PLACE'))
                      for eid, ent in _ents.items()
                      if ent.get('CARDTYPE') == 'HERO'
                      and ent.get('CONTROLLER') == my_controller
                      and ent.get('PLAYER_LEADERBOARD_PLACE') is not None]
        if candidates:
            place_raw = max(candidates, key=lambda pair: pair[0])[1]
    try:
        final_place = int(place_raw) if place_raw is not None else 0
    except (TypeError, ValueError):
        final_place = 0
    if local_ps not in ('WON', 'LOST', 'CONCEDED'):
        local_ps = ('WON' if final_place == 1 else
                    'LOST' if final_place > 1 else 'FINAL_GAMEOVER')
    return _game_over_board(local_ps, final_place or None, game.get('TURN', 0))


def _playstate_game_over_board(ps, game):
    """玩家 PLAYSTATE 已结束：WON=吃鸡；其余用本局已出局人数近似名次。"""
    # WON / LOST / CONCEDED / COMPLETE → 对局已结束
    # 名次近似：吃鸡=第1；出局=本局已出局玩家数（含自己，LOST 结算时含己方）
    place = 1 if ps == 'WON' else max(1, _count_eliminated())
    return _game_over_board(ps, place, game.get('TURN', 0))


def _hero_vitals(hero):
    """(血量, 护甲)：部分英雄血量存在 ARMOR，血量为空时用护甲顶上。"""
    hp = hero.get('HEALTH', 0)
    armor = hero.get('ARMOR', 0)
    if not hp and armor:
        hp = armor
    return hp, armor


def _build_board():
    """从当前状态组装局面"""
    my_controller = _local_controller
    my_hero_id, hero = _resolve_my_hero(my_controller)
    if my_hero_id is None:
        return None
    if my_controller is None:
        my_controller = _hero_controller(my_hero_id)
    if my_controller is None:
        return None
    p_ent = _resolve_player_entity(my_controller)
    mine = _collect_my_minions(my_controller)
    theirs = _collect_opponent_minions(my_controller)
    game = _game_entity()
    if game.get('STEP') in ('FINAL_GAMEOVER', 'FINAL_WRAPUP'):
        return _final_game_over_board(hero, my_controller, game)
    # ---- 对局是否真的在进行：玩家 PLAYSTATE 结束状态检测 ----
    if my_controller is not None:
        ps = _local_playstate(my_controller)
        if ps and ps not in ('PLAYING', 'MULLIGAN'):
            return _playstate_game_over_board(ps, game)
    hp, armor = _hero_vitals(hero)
    return {
        'game_serial': _game_serial,
        'game_key': _current_game_key(),
        'tier': hero.get('PLAYER_TECH_LEVEL', hero.get('TECH_LEVEL', 0)),
        'gold': p_ent.get('RESOURCES', hero.get('RESOURCES', 0)),
        'hp': hp,
        'armor': armor,
        'turn': game.get('TURN', 0),
        'mine': mine,
        'theirs': theirs,
        'fresh': bool(mine),
    }


def _current_game_key():
    if not LOG_PATH:
        return ''
    if _game_token:
        return _game_token
    return hashlib.sha256(f'{os.path.abspath(LOG_PATH)}:{_game_serial}'.encode()).hexdigest()[:24]


def format_board(board) -> str:
    from core import card_db
    lines = []
    hp_txt = f"{board['hp']}"
    if board.get('armor'):
        hp_txt += f"(+{board['armor']}甲)"
    lines.append(f"酒馆等级 {board['tier']} ｜ 铸币 {board['gold']} ｜ 血量 {hp_txt} ｜ 回合 {board['turn']}")
    if board['mine']:
        lines.append("己方随从：")
        for m in board['mine']:
            kw = []
            if m['taunt']: kw.append('嘲讽')
            if m['divine_shield']: kw.append('圣盾')
            if m['windfury']: kw.append('风怒')
            if m['poison']: kw.append('烈毒')
            if m['reborn']: kw.append('复生')
            if m['golden']: kw.append('金色')
            name = m['name']
            if name.startswith('BG'):
                nm = card_db.name_of(name)
                race = card_db.race_of(name)
                name = nm + (f'（{race}）' if race else '')
            lines.append(f"  {name} {m['atk']}-{m['hp']}{('(' + '+'.join(kw) + ')') if kw else ''}")
    else:
        lines.append("己方随从：暂无")
    if board['theirs']:
        lines.append("对手随从（战斗中可见）：")
        for m in board['theirs']:
            name = m['name']
            if name.startswith('BG'):
                nm = card_db.name_of(name)
                race = card_db.race_of(name)
                name = nm + (f'（{race}）' if race else '')
            lines.append(f"  {name} {m['atk']}-{m['hp']}")
    else:
        lines.append("对手随从：不可见（招募阶段）")
    return "\n".join(lines)


def _val(v):
    try:
        return int(v)
    except ValueError:
        return v


if __name__ == '__main__':
    import sys
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
    print('日志:', LOG_PATH)
    b = read_current_board()
    if b is None:
        print('未检测到酒馆战棋对局（或日志不存在）')
    else:
        print('fresh =', b['fresh'], '| 己方随从 =', len(b['mine']), '| 对手 =', len(b['theirs']))
        print(format_board(b))
