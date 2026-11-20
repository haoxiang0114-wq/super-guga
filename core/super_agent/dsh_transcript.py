# -*- coding: utf-8 -*-
"""读取 DSH 会话的真实对话记录。

DSH 的会话日志存在 `~/.dsh/sessions/<工作目录编码>/<会话号>/session.v4.jsonl.zstd`，
是**多帧追加**的 zstd 压缩 JSONL。用 `decompress()` 只能拿到第一帧（往往只有
会话元数据），必须用流式解压才能读全——这里踩过一次，记下来。

事件类型（实测）：
- `user/message`      data.content[].text  主人说的话
- `assistant/message` data.message.content[].text   DSH 的回复
- `system/message`    系统提示词，不给人看，跳过
- `session/title`     data.title 会话标题

咕嘎侧栏要显示 DSH 的对话，就得从这里读——DSH 的网关 RPC 没有读消息的接口
（session/messages 等全部 404）。
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

SESSIONS_ROOT = Path(os.path.expanduser('~')) / '.dsh' / 'sessions'
LOG_NAME = 'session.v4.jsonl.zstd'


def session_dir(session_id):
    """按会话号找目录。工作目录编码规则不自己猜，直接遍历匹配。"""
    sid = str(session_id or '').strip()
    if not sid:
        return None
    try:
        roots = [d for d in SESSIONS_ROOT.iterdir() if d.is_dir()]
    except Exception:
        return None
    for root in roots:
        candidate = root / sid
        if candidate.is_dir() and (candidate / LOG_NAME).exists():
            return candidate
    return None


def _text_of(content):
    """把 content 数组拼成纯文本，忽略图片等非文字块。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get('type') == 'text':
            value = block.get('text')
            if value:
                parts.append(str(value))
    return '\n'.join(parts).strip()


def read_transcript(session_id, limit=200):
    """读会话记录，返回 [{role, text, seq}]，按时间从早到晚。

    role 只有 user / assistant 两种；系统提示词和审批注入的假用户消息都过滤掉。
    读不到就返回空列表，绝不抛异常给 UI。
    """
    folder = session_dir(session_id)
    if folder is None:
        return []
    path = folder / LOG_NAME
    try:
        import zstandard as zstd
        data = path.read_bytes()
        out = bytearray()
        reader = zstd.ZstdDecompressor().stream_reader(io.BytesIO(data))
        try:
            while True:
                chunk = reader.read(65536)
                if not chunk:
                    break
                out.extend(chunk)
        finally:
            reader.close()
        text = bytes(out).decode('utf-8', 'replace')
    except Exception:
        return []

    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        kind = str(event.get('type') or '')
        data = event.get('data') if isinstance(event.get('data'), dict) else {}
        seq = event.get('seq')
        if kind == 'user/message':
            source = data.get('source') if isinstance(data.get('source'), dict) else {}
            # 审批策略变化、心跳这类注入会伪装成 user/message，不是主人说的话。
            # 实测 source.kind 是 'user-approval'，必须挡掉，否则聊天框里会多出
            # 一行英文的「The approval policy changed...」。
            source_kind = str(source.get('kind') or '')
            if source_kind and source_kind != 'user':
                continue
            body = _text_of(data.get('content'))
            if body:
                rows.append(dict(role='user', text=body, seq=seq, time=event.get('time')))
        elif kind == 'assistant/message':
            message = data.get('message') if isinstance(data.get('message'), dict) else {}
            body = _text_of(message.get('content'))
            if body:
                rows.append(dict(role='assistant', text=body, seq=seq, time=event.get('time')))
    return rows[-int(limit):] if limit else rows


def merge_messages(local, remote):
    """Keep local diagnostics at their real time, before later remote success."""
    from datetime import datetime, timezone
    merged, seen = [], set()
    for row in remote:
        key = (row.get('role'), row.get('text'))
        if key in seen:continue
        seen.add(key)
        merged.append(dict(id='dsh-%s' % row.get('seq',len(merged)),
            role=row.get('role'),content=row.get('text'),time=row.get('time')))
    for row in local:
        key=(row.get('role'),row.get('content'))
        if key in seen:continue
        seen.add(key);merged.append(row)
    def stamp(row):
        if isinstance(row.get('time'),(int,float)):return row['time']/1000
        try:return datetime.fromisoformat(row['created']).replace(tzinfo=timezone.utc).timestamp()
        except (KeyError,TypeError,ValueError):return float('-inf')
    return sorted(merged,key=stamp)


def read_title(session_id):
    """取 DSH 给这个会话起的标题（读不到返回空串）。"""
    folder = session_dir(session_id)
    if folder is None:
        return ''
    path = folder / LOG_NAME
    title = ''
    try:
        import zstandard as zstd
        out = bytearray()
        reader = zstd.ZstdDecompressor().stream_reader(io.BytesIO(path.read_bytes()))
        try:
            while True:
                chunk = reader.read(65536)
                if not chunk:
                    break
                out.extend(chunk)
        finally:
            reader.close()
    except Exception:
        return ''
    for line in bytes(out).decode('utf-8', 'replace').splitlines():
        try:
            event = json.loads(line)
        except Exception:
            continue
        if event.get('type') == 'session/title':
            data = event.get('data') or {}
            value = str(data.get('title') or '').strip()
            if value:
                title = value
    return title[:100]
