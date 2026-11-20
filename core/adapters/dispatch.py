# -*- coding: utf-8 -*-
"""按声明把「点名软件」的任务路由到已注册的适配器。

只做查表与实例化，不实现任何界面操作：
  · 声明里 `adapter` 为空 → 没有适配器，调用方必须给“缺通道”的可行动提示，
    绝不能回退成咕嘎自己的模型代写（这是立身之本）；
  · `adapter` 支持两种写法：`包.模块:类名` 或 `包.模块`（模块里取 Adapter/ask）；
  · 实例按 app_id 复用（适配器内部持有 MCP 连接与窗口句柄）。
"""
import importlib

from . import app_registry

_INSTANCES = {}
# app_id -> 最近一次适配器加载失败原因；供路由层把“没有通道”说清楚，而不是静默换路。
_LOAD_ERRORS = {}


def _norm(value):
    return str(value or '').strip().casefold()


def load_error(app_id):
    """最近一次适配器加载失败的原因；没有失败返回空串。"""
    return _LOAD_ERRORS.get(_norm(app_id), '')


def app_id_for(name):
    """按 id / 别名找到声明里的 app_id；没有返回 ''。"""
    key = _norm(name)
    if not key:
        return ''
    for app_id, decl in (app_registry.declarations() or {}).items():
        if key == _norm(app_id) or key == _norm(decl.get('name')):
            return app_id
        for alias in decl.get('aliases') or ():
            if key == _norm(alias):
                return app_id
    return ''


def _load_instance(spec):
    module_name, _, attr = str(spec or '').partition(':')
    module_name = module_name.strip()
    if not module_name:
        return None
    module = importlib.import_module(module_name)
    if attr:
        cls = getattr(module, attr.strip(), None)
        if cls is None:
            raise ImportError('%s 里没有 %s' % (module_name, attr))
        return cls()
    cls = getattr(module, 'Adapter', None)
    if cls is not None:
        return cls()
    ask = getattr(module, 'ask', None)
    if callable(ask):
        class _CallableAdapter(object):
            def ask(self, prompt, **_kw):
                return ask(prompt)
        return _CallableAdapter()
    raise ImportError('%s 没有 Adapter 类或 ask() 入口' % module_name)


def adapter_for(name):
    """返回 (app_id, adapter 实例或 None)。

    None 表示“这个软件没有可用适配器”：调用方应当明确拒绝并把原因说出来，
    不要换模型自己写。
    """
    app_id = app_id_for(name)
    if not app_id:
        return '', None
    decl = app_registry.declaration(app_id) or {}
    if app_id in _INSTANCES:
        return app_id, _INSTANCES[app_id]
    spec = str(decl.get('adapter') or '').strip()
    if not spec:
        _LOAD_ERRORS[_norm(app_id)] = '声明里没有 adapter 入口'
        return app_id, None
    try:
        instance = _load_instance(spec)
    except Exception as exc:
        # 不吞异常：路由层要据此明确拒绝（no_adapter），绝不能悄悄降级成云端拆解。
        _LOAD_ERRORS[_norm(app_id)] = '%s: %s' % (type(exc).__name__, str(exc)[:160])
        return app_id, None
    _LOAD_ERRORS.pop(_norm(app_id), None)
    _INSTANCES[app_id] = instance
    return app_id, instance
