# -*- coding: utf-8 -*-
"""M2: 管家中枢 — 把"当前员工"挂到对话引擎与生活服务上

职责(全部松耦合, 不侵入既有模块):
  1. 知识检索链: 监听 ModeManager.mode_changed, 按当前员工的
     ArmorPack.knowledge_dirs 建 KnowledgeRAG; 对话时 provider 链:
       当前员工知识库(非空命中) -> 回退 bg_knowledge(战棋, 兼容旧功能)
  2. 引擎挂载: attach(chat_engine) 注册 set_rag_provider, 员工切换时
     可选清空对话历史(换人=换话题, 默认开)
  3. 员工人设: persona_for_chat() 返回(人设前缀, 称谓), 供调用方拼
     system prompt —— 不强制接管 system_override, 尊重 tutor 等既有流

用法:
    hub = ButlerHub(mode_manager)
    hub.attach(ai_chat_engine)
    # 之后 engine.ask() 自动按当前员工检索知识库
"""
import logging
from pathlib import Path

from core.knowledge_rag import KnowledgeRAG
from core.bg_knowledge import build_context as _bg_build_context

logger = logging.getLogger(__name__)

_MAX_CONTEXT_CHARS = 1600      # 多目录合并后的总字符预算


class ButlerHub:
    """员工(模式) -> 知识检索 / 对话人设 的粘合中枢."""

    def __init__(self, mode_manager, reset_history_on_switch=True, repo_root=None):
        self._mm = mode_manager
        self._engine = None
        self._reset_history = reset_history_on_switch
        self._repo_root = Path(repo_root) if repo_root else Path.cwd()
        self._rags = {}            # dir_str -> KnowledgeRAG (惰性建)
        mode_manager.mode_changed.connect(self._on_mode_changed)

    # ==================== 挂载 ====================

    def attach(self, chat_engine):
        """把检索链+人设注入注册进 AIChatEngine; 员工切换清空历史."""
        self._engine = chat_engine
        chat_engine.set_rag_provider(self.provider, label=self._label())
        chat_engine.set_persona_provider(self.persona_system_prefix)
        if self._reset_history:
            self._on_mode_changed(self._mm.current_id)   # 顺带清一次
        return self

    def _label(self):
        pack = self._mm.current
        if pack and pack.knowledge_dirs:
            return '%s知识库' % (pack.display_name or pack.id)
        return '知识库参考'

    def _on_mode_changed(self, _mode_id):
        self._rags.clear()
        if self._engine is not None:
            self._engine._rag_label = self._label()
            if self._reset_history:
                self._engine.reset_history()
        logger.info('butler hub: switched to %s', _mode_id)

    # ==================== 检索链 ====================

    def _rag_for(self, dir_str):
        rag = self._rags.get(dir_str)
        if rag is None:
            p = Path(dir_str)
            if not p.is_absolute():
                p = self._repo_root / p
            rag = KnowledgeRAG(armor_id='custom', knowledge_root=str(p))
            self._rags[dir_str] = rag
        return rag

    def provider(self, query):
        """AIChatEngine 检索入口: 当前员工知识库优先, 空则回退战棋库."""
        pack = self._mm.current
        parts = []
        if pack and pack.knowledge_dirs:
            budget = _MAX_CONTEXT_CHARS
            for d in pack.knowledge_dirs:
                if budget <= 0:
                    break
                try:
                    ctx = self._rag_for(str(d)).build_context(query, top_k=3,
                                                              char_budget=budget)
                except Exception:
                    logger.exception('rag failed for %s', d)
                    continue
                if ctx:
                    parts.append(ctx)
                    budget -= len(ctx)
        if parts:
            return '\n\n'.join(parts)
        return _bg_build_context(query)      # 兼容旧战棋功能(管家/无命中时)

    # ==================== 人设 ====================

    def persona_for_chat(self):
        """当前员工的(人设前缀, 称谓); 调用方可拼进 system prompt.

        返回 (persona_str, address); 无员工上下文(管家)时人设为空串.
        """
        pack = self._mm.current
        if pack is None:
            return '', '少爷'
        persona = pack.persona_prompt or ''
        if not pack.is_hub:
            persona = ('(你现在是被管家召唤上岗的部门员工, 只谈你职责范围内的事, '
                       '与职责无关的话题建议少爷召唤管家)\n' + persona)
        address = getattr(pack, 'address', None) or '少爷'
        return persona, address

    def persona_system_prefix(self):
        """AIChatEngine 人设注入口: 当前在岗者身份前缀(空串=默认人设).

        回复格式(表情|动作|台词)由底层 prompt 保证, 这里只叠加身份.
        """
        persona, address = self.persona_for_chat()
        if not persona:
            return ''
        return ('【当前身份】%s\n【称呼用户】%s\n'
                '【注意】回复仍按底层要求的格式输出, 身份体现在语气与内容上.'
                % (persona, address))
