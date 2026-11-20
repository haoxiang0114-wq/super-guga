# -*- coding: utf-8 -*-
"""把 DSH 的会话接进咕嘎侧栏，让两边看到同一份对话。

主人 2026-11-12 的要求：**打开咕嘎的对话就是开启 DSH**。
以前咕嘎的对话存在自己的 sqlite 里，DSH 的会话存在 ~/.dsh/sessions，
两边互不相通——咕嘎里建的对话 DSH 看不到，DSH 里建的咕嘎也看不到。

这个模块负责把 DSH 那边**真实存在**的会话接进咕嘎侧栏（`ChatWorkspace`
的 `dsh_session_id` 列），接进来之后：
- 侧栏点开它 = 打开那个真实的 DSH 会话；
- 在这个对话里说话 = 投给那个 DSH 会话。

数量控制：DSH 里可能有上百个历史会话，全灌进侧栏会淹没主人。所以只接
「咕嘎自己拉起的」+「最近活跃的若干条」，其余留在 DSH 里不动。
"""
from __future__ import annotations

# 默认最多接这么多条，够用又不至于把侧栏撑爆。
DEFAULT_LIMIT = 12


def _session_id(item):
    return str((item or {}).get('sessionId') or (item or {}).get('id') or '').strip()


def _title(item):
    from core.super_agent.dsh_gateway import session_title
    try:
        title = session_title(item)
    except Exception:
        title = ''
    return (title or '').strip() or 'DSH 会话'


def _recent_key(item):
    """尽量按「最近活跃」排序；DSH 不同版本字段名不一样，取到哪个用哪个。"""
    for key in ('lastActiveAt', 'updatedAt', 'lastMessageAt', 'createdAt'):
        value = item.get(key)
        if value not in (None, ''):
            return str(value)
    return ''


def sync_sessions(store, gateway=None, limit=DEFAULT_LIMIT, agent_id='001'):
    """把 DSH 的会话接进咕嘎侧栏，返回接了多少条、跳过了多少条。

    `store` 是 ChatWorkspace；`gateway` 缺省时自己建一个 DshGateway。
    任何失败都只返回 ok=False，绝不抛给 UI 线程。
    """
    result = dict(ok=False, adopted=0, already=0, skipped=0, total=0, error='')
    try:
        if gateway is None:
            from core.super_agent.dsh_gateway import DshGateway
            gateway = DshGateway()
        items = [i for i in (gateway.session_summaries() or []) if _session_id(i)]
        result['total'] = len(items)
        if not items:
            result['ok'] = True
            return result

        # 咕嘎自己拉起的会话优先，剩下的按最近活跃补齐。
        owned = set()
        try:
            from core.super_agent.dsh_gateway import owned_sessions
            owned = set(owned_sessions() or [])
        except Exception:
            owned = set()

        chosen, seen = [], set()
        for item in items:
            sid = _session_id(item)
            if sid in owned and sid not in seen:
                chosen.append(item)
                seen.add(sid)
        rest = [i for i in items if _session_id(i) not in seen]
        rest.sort(key=_recent_key, reverse=True)
        for item in rest:
            if len(chosen) >= limit:
                break
            sid = _session_id(item)
            if sid in seen:
                continue
            chosen.append(item)
            seen.add(sid)

        result['skipped'] = max(0, len(items) - len(chosen))
        for item in chosen:
            sid = _session_id(item)
            try:
                before = store.conversation_by_dsh(sid)
                if before:
                    result['already'] += 1
                    continue
                store.adopt_dsh(sid, name=_title(item)[:80], agent_id=agent_id)
                result['adopted'] += 1
            except Exception:
                # 单条失败不影响其它会话
                continue
        result['ok'] = True
    except Exception as exc:  # noqa: BLE001
        result['error'] = '%s: %s' % (type(exc).__name__, str(exc)[:200])
    return result


def open_bound(store, conversation_id):
    """取出某个咕嘎对话绑定的 DSH 会话号；没绑定返回空串。"""
    try:
        row = store.conversation(conversation_id)
    except Exception:
        return ''
    return str(row.get('dsh_session_id') or '').strip()
