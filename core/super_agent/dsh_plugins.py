# -*- coding: utf-8 -*-
"""读取 DSH 的插件清单，供咕嘎侧栏显示。

DSH 的插件挂在 profile 的 `cordis.patch.yml` 里，形如：

    - id: ui-theme
      name: "@deepseek-ai/dsh-client-ui-theme"
    - insert:
        - id: whale-pet
          name: './whale-pet/index.js'

市场状态在 `.dsh-market/state.json`（disabled / favorites / groups）。

这里只做**读**：列出装了哪些、哪些被停用。装/卸插件是写操作，要经过 DSH
自己的接口，不在这个模块里硬改配置——改坏了 DSH 起不来，代价太大。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

DSH_HOME = Path(os.path.expanduser('~')) / '.dsh'
DEFAULT_PROFILE = 'web'

# 简单 YAML 行解析：只认 `- id:` / `name:` / `enabled:`，够用且不引依赖。
# 缩进要卡死：插件在顶层（0 缩进）或 insert 列表里（4 空格）；
# `models:` 下面的 `- id: deepseek-v4.1-flash` 是模型不是插件，
# 不卡缩进会把模型全当成插件列出来（实测踩过）。
_ID_RE = re.compile(r'^ {0,4}-\s*id:\s*(.+?)\s*$')
_NAME_RE = re.compile(r'^\s*name:\s*(.+?)\s*$')
_ENABLED_RE = re.compile(r'^\s*enabled:\s*(.+?)\s*$')


def _clean(value):
    value = str(value or '').strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
        value = value[1:-1]
    return value.strip()


def profile_dir(profile=DEFAULT_PROFILE):
    return DSH_HOME / 'profiles' / str(profile or DEFAULT_PROFILE)


def market_state(profile=DEFAULT_PROFILE):
    """读市场状态（停用列表、收藏、分区）。读不到就返回空结构。"""
    path = profile_dir(profile) / '.dsh-market' / 'state.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return dict(disabled=[], favorites=[], groups={}, region='')
    return dict(disabled=list(data.get('disabled') or []),
                favorites=list(data.get('favorites') or []),
                groups=data.get('groups') or {},
                region=str(data.get('region') or ''))


def list_plugins(profile=DEFAULT_PROFILE):
    """列出已装插件：[{id, name, enabled, kind}]。

    kind: 'builtin' 是 DSH 自带（名字以 @deepseek-ai/ 开头），
          'local' 是本地目录插件（名字以 ./ 开头），
          'other' 其余。
    """
    patch = profile_dir(profile) / 'cordis.patch.yml'
    try:
        lines = patch.read_text(encoding='utf-8', errors='replace').splitlines()
    except Exception:
        return []
    disabled = {str(x) for x in market_state(profile).get('disabled') or []}

    plugins = []
    current = None
    for line in lines:
        if line.lstrip().startswith('#'):
            continue
        hit = _ID_RE.match(line)
        if hit:
            if current:
                plugins.append(current)
            current = dict(id=_clean(hit.group(1)), name='', enabled=True)
            continue
        if current is None:
            continue
        hit = _NAME_RE.match(line)
        if hit and not current['name']:
            current['name'] = _clean(hit.group(1))
            continue
        hit = _ENABLED_RE.match(line)
        if hit:
            current['enabled'] = _clean(hit.group(1)).lower() not in ('false', '0', 'no', 'off')
    if current:
        plugins.append(current)

    for item in plugins:
        name = item.get('name') or item.get('id') or ''
        item['name'] = name
        item['kind'] = ('builtin' if name.startswith('@deepseek-ai/')
                        else 'local' if name.startswith('./') else 'other')
        if item['id'] in disabled:
            item['enabled'] = False
    return plugins


def summary(profile=DEFAULT_PROFILE):
    """一句话概括：装了几个、停用几个。"""
    try:
        items = list_plugins(profile)
    except Exception:
        return dict(total=0, enabled=0, disabled=0, plugins=[])
    on = [i for i in items if i.get('enabled')]
    return dict(total=len(items), enabled=len(on), disabled=len(items) - len(on), plugins=items)
