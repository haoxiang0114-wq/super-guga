# -*- coding: utf-8 -*-
"""管家引擎适配桥 — 纯新增模块，不修改本地 AIChatEngine 源码。

背景
----
队友仓库的 ``ButlerHub.attach(engine)`` 依赖旧版 ``AIChatEngine`` 的三个接口：
``set_rag_provider`` / ``set_persona_provider`` / ``reset_history``，并在
``ask()`` 内用 provider 结果替代写死的 ``build_context``。本地新版
``core/ai_chat.py`` 已重写，没有前两个方法，直接 attach 会被静默吞掉。

本模块用「实例级注入 + 模块内函数代理」补齐接口，原则：

1. **零源码修改**：不改 ``core/ai_chat.py`` 的任何一行；只在实例上挂方法，
   并把 ``core.ai_chat.build_context`` 这个**模块内名字**替换为代理函数。
2. **默认行为不变**：未挂管家时代理直接调用原 ``build_context``；员工人设
   注入默认关闭（``_PERSONA_ACTIVE=False``），只有召唤非管家员工时才启用。
3. **可一键关闭**：环境变量 ``GUGA_BUTLER=0`` 时 attach 不生效。
"""
import logging
import os

logger = logging.getLogger(__name__)

#: 当前生效的知识检索 provider（同一进程只服务一只桌宠）
_PROVIDER = None
_PERSONA = None
#: 人设注入开关：默认关闭，仅在召唤「部门员工」时由挂载层打开
_PERSONA_ACTIVE = False
_PATCHED = False


def butler_enabled() -> bool:
    """总开关：默认开启；``GUGA_BUTLER=0`` 可完全禁用队友管家功能。"""
    return os.environ.get("GUGA_BUTLER", "1").strip().lower() not in ("0", "false", "off", "no")


def _patch_build_context() -> None:
    """把 ``core.ai_chat`` 里的 ``build_context`` 名字换成代理函数。

    只替换该模块命名空间内的名字；``core.bg_knowledge.build_context`` 本身不变，
    因此战棋知识库与其它调用方行为完全不受影响。
    """
    global _PATCHED
    if _PATCHED:
        return
    import core.ai_chat as ai_chat

    original = getattr(ai_chat, "build_context", None)
    if original is None or getattr(original, "_butler_proxy", False):
        _PATCHED = True
        return

    def _delegating(query):
        if _PROVIDER is not None:
            try:
                context = _PROVIDER(query)
                if context:
                    return context
            except Exception:
                logger.exception("butler rag provider failed; falling back to battlegrounds")
        return original(query)

    _delegating._butler_proxy = True
    ai_chat.build_context = _delegating
    _PATCHED = True


def _install_persona_wrap(engine) -> None:
    """包装实例的 ``_start``：仅在人设开关打开且调用方未指定 system_override 时
    才把员工人设拼到系统提示前，其余情况完全走原逻辑。"""
    original = engine._start
    if getattr(original, "_butler_wrapped", False):
        return
    import core.ai_chat as ai_chat

    def _start(user_msg, rag_context, system_override):
        if _PERSONA_ACTIVE and _PERSONA is not None and not system_override:
            try:
                prefix = _PERSONA()
                if prefix:
                    system_override = prefix + "\n\n" + ai_chat.SYSTEM_PROMPT
            except Exception:
                logger.exception("butler persona injection failed; using default persona")
        return original(user_msg, rag_context, system_override)

    _start._butler_wrapped = True
    engine._start = _start


def ensure_engine_hooks(engine):
    """为旧版引擎接口补齐三个方法（若已存在则完全不动）。"""
    if engine is None:
        return engine
    _patch_build_context()

    if not hasattr(engine, "set_rag_provider"):
        def set_rag_provider(provider, label=None):
            global _PROVIDER
            _PROVIDER = provider
            try:
                engine._rag_provider = provider
                engine._rag_label = label or "知识库参考"
            except Exception:
                pass

        engine.set_rag_provider = set_rag_provider

    if not hasattr(engine, "set_persona_provider"):
        def set_persona_provider(provider):
            global _PERSONA
            _PERSONA = provider
            try:
                engine._persona_fn = provider
            except Exception:
                pass

        engine.set_persona_provider = set_persona_provider

    if not hasattr(engine, "_rag_provider"):
        engine._rag_provider = None
    if not hasattr(engine, "_persona_fn"):
        engine._persona_fn = None

    _install_persona_wrap(engine)
    return engine


def set_persona_active(active: bool) -> None:
    """由挂载层在员工切换时调用：管家态保持本地原有咕嘎人设。"""
    global _PERSONA_ACTIVE
    _PERSONA_ACTIVE = bool(active)


def persona_active() -> bool:
    return _PERSONA_ACTIVE


def reset_provider() -> None:
    """卸载管家时恢复原始知识检索链。"""
    global _PROVIDER, _PERSONA, _PERSONA_ACTIVE
    _PROVIDER = None
    _PERSONA = None
    _PERSONA_ACTIVE = False
