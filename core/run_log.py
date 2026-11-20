# -*- coding: utf-8 -*-
"""运行期统一日志：adapter_chain.log 是唯一落点。

20261110z 现场：e2e 结束时 adapter_chain.log 只有 589B / 2 行，
composer / inject / done / timeout 四类关键事件全在 result.json 的 timeline 里，
链层与验收脚本都看不到，等于没有证据。这里给适配器和链层同一个写入函数。
"""
import json
import time
from pathlib import Path


def log_path():
    from core import guga_paths
    return Path(guga_paths.RUNTIME) / 'adapter_chain.log'


def emit(event, **fields):
    """追加一行 JSON 事件；写盘失败不抛（日志不能反过来弄坏主流程）。"""
    try:
        row = dict(time=time.strftime('%Y-%m-%d %H:%M:%S'), event=str(event))
        for key, value in fields.items():
            if value is not None:
                row[key] = value
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')
        return True
    except Exception:
        return False
