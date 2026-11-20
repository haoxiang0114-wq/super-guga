"""Bounded launch-to-recruit automation using only approved visible entries."""
import time


def _ready(reader):
    reader.poll()
    from core.bg_live import observed_phase
    phase=observed_phase(reader.parser)
    if phase not in ('recruit','combat'):
        return None
    try:
        state=reader.state()
    except RuntimeError:
        return None
    return dict(state=state,phase=phase)


def prepare_battleground(backend,config,emit,stop,deadline,launch_wait=75):
    """Open game, enter BG, wait through queue/hero screens, stop at live recruit."""
    from core.log_reader import find_log_path, read_current_board
    from core.bg_live import LiveReader, lobby_step
    launched=False;last_click=None;last_click_at=0.;last_error='游戏尚未进入酒馆战棋'
    reader=None;reader_path=None
    while not stop.is_set() and time.monotonic()<deadline:
        path=find_log_path(current_session=True)
        board=read_current_board() or {}
        if path and board.get('active'):
            if reader_path!=path:
                reader=LiveReader(path,latest_game=True);reader_path=path
            found=_ready(reader)
            if found:
                emit('已确认进入酒馆战棋实局，开始逐步决策。')
                return dict(**found,log_path=path,reader=reader)
        try:
            backend.find()
        except RuntimeError:
            if not launched:
                emit('正在打开炉石传说；启动器、登录和更新由游戏自己完成，我只点击明确的免费入口。')
                backend.open();launched=True
            time.sleep(2)
            continue
        try:
            record=lobby_step(backend,config,emit,stop,deadline)
            if record:
                key=(record.get('action',{}).get('kind'),record.get('action',{}).get('label'))
                now=time.monotonic()
                # The screen may need time to transition after a click; never
                # repeat the exact visible action within this cooldown.
                if key==last_click and now-last_click_at<4:time.sleep(1);continue
                last_click,last_click_at=key,now
                emit('自动入口已提交：'+str(key[0])+'；等待画面或日志确认。')
                time.sleep(2)
        except RuntimeError as exc:
            last_error=str(exc)[:200]
            waiting=any(word in last_error for word in ('排队','战斗','等待','没有可确认','未知'))
            # Vision/model latency is normal during loading. Continue only
            # through the same hidden free-action allowlist.
            time.sleep(1.5 if not waiting else 2)
        except Exception as exc:
            last_error=type(exc).__name__
            time.sleep(2)
    raise RuntimeError('未进入可决策的招募阶段：'+last_error+'；未执行任何未确认操作')
