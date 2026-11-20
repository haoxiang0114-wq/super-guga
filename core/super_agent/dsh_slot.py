# -*- coding: utf-8 -*-
"""DSH 单槽闸门：同一时刻只允许一个咕嘎派出去的 DSH 会话在跑。

# 功能名：咕嘎派发 DSH 的单会话闸门
# 生效条件：任何代码路径要新建 DSH 会话或向 DSH 派单之前（start_task / dispatch / headless）
# 子功能：占槽、查槽、放槽、过期回收、排队语义
# 执行：读 runtime/dsh-slot.json；占槽前先判断原占用者是否已结束；抢不到就不新建会话
# 验证方式：连续两次 dispatch，第二次必须返回 busy 且 DSH 会话总数不增加
# 不适用条件：用户手动在 GUI 里自己开的会话（那些不归咕嘎管，也不进这个槽）

为什么要有它
------------
2026-11-14 事故：咕嘎在 9:29–9:58 半小时里拉起了 122 个 DSH 会话，全部掉进
「未分组」，互相抢同一个工作目录。根因不是任务多，是**没有任何一层拦第二个**：
`DshGateway.start_task()` 每次调用都 `create_session()`，外层循环把它放大成扇出。

三条规则
--------
1. 槽位全局唯一：`runtime/dsh-slot.json` 记录当前占用者。
2. 占槽只在「会话真的在跑」时成立：会话结束 / 进程死掉 / 超过 TTL → 自动作废。
   绝不能因为一次崩溃就把闸门永久焊死，那样咕嘎会彻底派不出任务。
3. 抢不到槽就**排队**，不新建会话、不并发。这是硬规则，不是建议。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / 'runtime'
STATE_PATH = RUNTIME / 'dsh-slot.json'
# 超过这个时长仍占着槽，视为异常残留（模型卡死、进程被杀没来得及放槽）。
DEFAULT_TTL_SECONDS = 7200.0


def _now(clock=None):
    return round(float((clock or time.time)()), 3)


def _read():
    try:
        raw = STATE_PATH.read_text(encoding='utf-8')
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(state):
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        os.replace(tmp, STATE_PATH)
        return True
    except OSError:
        return False


def _pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)  # 0 号信号只探活，不投递
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # 进程在，只是不是我们的
    except OSError:
        return False
    return True


def status(probe=None, clock=None, ttl=DEFAULT_TTL_SECONDS):
    """查当前槽位。`probe(session_id) -> bool` 由调用方注入"会话是否还在跑"。

    返回 dict(active, session_id, ticket_id, started_at, age, stale, reason)。
    stale=True 表示占用者已经不可信，调用方可以放心抢。
    """
    state = _read()
    session_id = str(state.get('session_id') or '').strip()
    if not session_id:
        return dict(active=False, session_id='', ticket_id='', started_at=0.0,
                    age=0.0, stale=False, reason='空闲')
    started_at = float(state.get('started_at') or 0.0)
    age = max(0.0, _now(clock) - started_at) if started_at else 0.0
    pid = state.get('pid')
    stale = False
    reason = '占用中'
    if pid and not _pid_alive(pid):
        stale, reason = True, '占用进程已退出（残留槽位）'
    elif ttl and age > ttl:
        stale, reason = True, '占用超过 %.0f 秒（疑似卡死）' % ttl
    elif probe is not None:
        try:
            if not probe(session_id):
                stale, reason = True, '占用会话已经不在运行'
        except Exception:  # noqa: BLE001
            # 探活失败时保守处理：当作还在跑，宁可排队也不并发。
            reason = '探活失败，按占用处理'
    return dict(active=not stale, session_id=session_id,
                ticket_id=str(state.get('ticket_id') or ''), started_at=started_at,
                age=round(age, 1), stale=stale, reason=reason)


def claim(session_id, ticket_id='', probe=None, clock=None, ttl=DEFAULT_TTL_SECONDS):
    """抢槽。抢到返回 ok=True；抢不到返回 ok=False 并给出当前占用者，绝不覆盖。

    允许抢的两种情况：槽位空闲，或原占用者已被判定 stale。
    """
    session_id = str(session_id or '').strip()
    if not session_id:
        return dict(ok=False, reason='没有会话编号，无法占槽', active_session_id='')
    current = status(probe=probe, clock=clock, ttl=ttl)
    if current['active'] and current['session_id'] != session_id:
        return dict(ok=False, reason='已有会话在跑：%s' % current['reason'],
                    active_session_id=current['session_id'],
                    active_ticket=current['ticket_id'], age=current['age'])
    state = dict(session_id=session_id, ticket_id=str(ticket_id or ''),
                 started_at=_now(clock), pid=os.getpid())
    if not _write(state):
        return dict(ok=False, reason='槽位文件写不进去（权限或磁盘）', active_session_id='')
    return dict(ok=True, session_id=session_id, reclaimed=bool(current.get('stale')))


def release(session_id=''):
    """放槽。`session_id` 给定时只放自己那个槽，避免误放别人的。"""
    session_id = str(session_id or '').strip()
    state = _read()
    holder = str(state.get('session_id') or '').strip()
    if session_id and holder and holder != session_id:
        return dict(ok=False, reason='槽位属于另一个会话，拒绝释放', holder=holder)
    _write({})
    return dict(ok=True, released=holder)


def snapshot():
    """给人看的当前槽位（不含 pid 之类实现细节）。"""
    state = _read()
    return dict(session_id=str(state.get('session_id') or ''),
                ticket_id=str(state.get('ticket_id') or ''),
                started_at=float(state.get('started_at') or 0.0))
