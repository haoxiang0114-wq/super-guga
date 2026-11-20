# -*- coding: utf-8 -*-
"""M1 学者甲: 本地知识库检索 KnowledgeRAG

检索 knowledge/<armor>/ 下的 .md/.txt(队友B交付的领域知识库),
按标题/段落切块, CJK bigram + ASCII 词元 TF-IDF 打分, 返回 top-k 片段.

设计:
  - 纯标准库, 不引重型依赖; 知识库规模(百篇级)完全够用
  - 惰性建索引 + mtime 失效检测(队友改了文档自动重建)
  - 这是"检索层": 返回片段给 LLM 组上下文(ai_chat 侧接入在 M2),
    真题验收(scripts/quiz_knowledge.py)只考检索是否有用
"""
import math
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = REPO / 'knowledge'

_DOC_RE = re.compile(r'\.(md|txt)$', re.I)
_HEADING_RE = re.compile(r'^#{1,6}\s+', re.M)


def _tokenize(text):
    """CJK 拆 bigram, ASCII 拆单词, 全小写."""
    tokens = []
    for word in re.findall(r'[A-Za-z0-9_]+', text):
        tokens.append(word.lower())
    cjk = re.findall(r'[\u4e00-\u9fff]', text)
    tokens += [''.join(cjk[i:i + 2]) for i in range(len(cjk) - 1)]
    return tokens


def _chunks(doc_path):
    """按 markdown 标题切块; 无标题的 txt 按空行段落块(约500字)切."""
    try:
        text = doc_path.read_text(encoding='utf-8')
    except (OSError, ValueError):
        return []
    if not text.strip():
        return []
    title = doc_path.stem
    m = re.search(r'^>\s*摘要[:：]\s*(.+)$', text, re.M)
    summary = m.group(1).strip() if m else ''
    # 问题行(队友B格式: "> 本篇回答:" 下面的列表)并入首块检索权重
    # 按 markdown 标题切块(用 finditer 捕获标题文本, 而非 ## 标记)
    matches = list(re.finditer(r'^#{1,6}\s+(.+)$', text, re.M))
    if matches:
        out = []
        pre = text[:matches[0].start()].strip()
        if pre:
            out.append((title + ' 总述', pre))
        for i, m in enumerate(matches):
            head = m.group(1).strip()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[m.end():end].strip()
            if body:
                out.append(('%s · %s' % (title, head), body))
        return [(t, b, title, summary) for (t, b) in out]
    # 纯文本: 按段落聚合 ~500 字
    paras = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
    out, buf = [], ''
    for p in paras:
        buf = (buf + '\n' + p).strip()
        if len(buf) >= 500:
            out.append((title, buf))
            buf = ''
    if buf:
        out.append((title, buf))
    return [(t, b, title, summary) for (t, b) in out]


class KnowledgeRAG:
    def __init__(self, armor_id, knowledge_root=None):
        self.armor_id = str(armor_id)
        self.root = Path(knowledge_root) if knowledge_root \
            else KNOWLEDGE_DIR / self.armor_id
        self._index = None      # list of dict(chunk_text, doc, title, summary)
        self._stats = None      # {'df': {term: df}, 'n': chunks, 'mtime': float}

    # ==================== 索引 ====================

    def _docs(self):
        if not self.root.is_dir():
            return []
        return [p for p in sorted(self.root.rglob('*')) if _DOC_RE.search(p.name)]

    def _build(self):
        docs = self._docs()
        chunks = []
        for doc in docs:
            for title, body, doc_name, summary in _chunks(doc):
                chunks.append({'title': title, 'doc': doc_name,
                               'summary': summary, 'text': body,
                               'tokens': _tokenize(body)})
        n = len(chunks)
        df = {}
        for c in chunks:
            for t in set(c['tokens']):
                df[t] = df.get(t, 0) + 1
        self._index = chunks
        self._stats = {'df': df, 'n': n,
                       'mtime': max((p.stat().st_mtime for p in docs), default=0)}
        return self._stats

    def _ensure(self):
        docs = self._docs()
        latest = max((p.stat().st_mtime for p in docs), default=0)
        if self._index is None or (self._stats and latest > self._stats['mtime']):
            self._build()
        return bool(self._index)

    # ==================== 检索 ====================

    def retrieve(self, question, top_k=3):
        """返回 [{'title','doc','summary','text','score'}], 无知识库返回 []."""
        if not self._ensure():
            return []
        q_tokens = _tokenize(str(question))
        if not q_tokens:
            return []
        df, n = self._stats['df'], max(1, self._stats['n'])
        scored = []
        for c in self._index:
            tf = {}
            for t in c['tokens']:
                tf[t] = tf.get(t, 0) + 1
            # 问题词命中加权: 每个问题词的 tf-idf 之和(未命中为 0)
            score = 0.0
            for t in set(q_tokens):
                if t in tf and t in df:
                    score += tf[t] * math.log(n / df[t])
            # "总述"块(只有摘要+问题行, 无正文干货)降权, 防止抢正文排名
            if score > 0 and c['title'].endswith('总述'):
                score *= 0.5
            if score > 0:
                scored.append((score, c))
        scored.sort(key=lambda x: -x[0])
        return [{'title': c['title'], 'doc': c['doc'],
                 'summary': c['summary'], 'text': c['text'],
                 'score': round(s, 2)}
                for s, c in scored[:top_k]]

    def build_context(self, question, top_k=3, char_budget=1200):
        """给 LLM 的上下文块: 检索片段拼成带出处的文本(超预算截断)."""
        hits = self.retrieve(question, top_k)
        if not hits:
            return ''
        parts, used = [], 0
        for h in hits:
            piece = '【%s】(来源: %s)\n%s' % (h['title'], h['doc'], h['text'])
            if used + len(piece) > char_budget:
                piece = piece[:max(0, char_budget - used)]
                if not piece:
                    break
            parts.append(piece)
            used += len(piece)
            if used >= char_budget:
                break
        header = ('以下是从本地知识库检索到的资料, 回答时依据资料并标注来源:\n\n')
        return header + '\n\n'.join(parts)

    def status(self):
        self._ensure()
        docs = self._docs()
        return {'armor': self.armor_id, 'docs': len(docs),
                'indexed': bool(self._index),
                'chunks': self._stats['n'] if self._stats else 0}
