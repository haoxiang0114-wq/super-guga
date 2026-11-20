#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""动作库：记录每次补帧产物的来历，支持复查、重做与回滚。

素材库（data/sticker_library.json）只关心"有哪些表情、怎么播"；
动作库关心的是**这段动画是怎么来的**：源帧数、补帧倍数、实际帧率、
有没有调用豆包、质量指标、关键帧存在哪。

两者按 slug 关联。动作库丢了不影响播放（有个兜底重建），
但有了它才能回答"这条动作是哪次生成的、能不能重调参数再来一遍"。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORE_PATH = ROOT / 'data' / 'motion_library.json'
KEYFRAME_ROOT = ROOT / 'resources' / 'actions' / 'resident-motion-v2'
MAX_ENTRIES = 200


def _empty():
    return {'version': 1, 'actions': []}


def load():
    if STORE_PATH.is_file():
        try:
            data = json.loads(STORE_PATH.read_text(encoding='utf-8'))
            if isinstance(data, dict) and isinstance(data.get('actions'), list):
                return data
        except (OSError, ValueError):
            pass
    return _empty()


def save(store):
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    actions = store.get('actions', [])[-MAX_ENTRIES:]
    store['actions'] = actions
    STORE_PATH.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding='utf-8')


def record(slug, name, *, source='', source_frames=0, out_frames=0,
           src_fps=None, fps=None, factor=1, engine='motion-compensated',
           doubao_calls=0, quality=None, keyframes='', notes=''):
    """写入/覆盖一条动作记录（同 slug 视为重新生成，保留上一次的历史）。"""
    store = load()
    entry = {
        'slug': slug,
        'name': name,
        'source': str(source),
        'source_frames': int(source_frames or 0),
        'out_frames': int(out_frames or 0),
        'src_fps': src_fps,
        'fps': fps,
        'factor': int(factor or 1),
        'engine': engine,
        'doubao_calls': int(doubao_calls or 0),
        'quality': quality or {},
        'keyframes': str(keyframes or ''),
        'created': datetime.now().isoformat(timespec='seconds'),
        'notes': notes,
    }
    history = []
    for old in store['actions']:
        if old.get('slug') == slug and isinstance(old.get('history'), list):
            history = old['history']
    prev = next((a for a in store['actions'] if a.get('slug') == slug), None)
    if prev and prev.get('created'):
        history = history[-4:] + [{
            'created': prev.get('created'),
            'out_frames': prev.get('out_frames'),
            'fps': prev.get('fps'),
            'factor': prev.get('factor'),
        }]
    if history:
        entry['history'] = history
    store['actions'] = [a for a in store['actions'] if a.get('slug') != slug] + [entry]
    save(store)
    return entry


def get(slug):
    return next((a for a in load()['actions'] if a.get('slug') == slug), None)


def list_actions():
    return load()['actions']


def remove(slug):
    store = load()
    before = len(store['actions'])
    store['actions'] = [a for a in store['actions'] if a.get('slug') != slug]
    if len(store['actions']) != before:
        save(store)
        return True
    return False


def prune(live_slugs):
    """清掉素材库里已经不存在的记录（移除表情、测试残留都会留下孤儿）。"""
    live = set(live_slugs or ())
    store = load()
    kept = [a for a in store['actions'] if a.get('slug') in live]
    dropped = len(store['actions']) - len(kept)
    if dropped:
        store['actions'] = kept
        save(store)
    return dropped


def summary():
    """一句话概览，给智能体和界面用。"""
    actions = list_actions()
    if not actions:
        return '动作库还是空的（导入表情时开补帧就会记一条）'
    smooth = sum(1 for a in actions if (a.get('factor') or 1) > 1)
    doubao = sum(1 for a in actions if (a.get('doubao_calls') or 0) > 0)
    return '动作库 %d 条，其中补过帧的 %d 条，用过豆包重绘的 %d 条' % (
        len(actions), smooth, doubao)
