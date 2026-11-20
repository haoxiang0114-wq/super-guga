"""Bounded read-only recall from the owner's existing local assistant conversations."""
from collections import deque
from contextlib import closing
from itertools import chain
from functools import lru_cache
import json
from pathlib import Path
import re
import sqlite3
import time

from core.agent_memory import _tokens, profile_facts, safe_text


_ID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', re.I)
_PRIVATE = re.compile(r'验证码|Cookie|登录凭证|密码|密钥|令牌|身份证|银行卡|authorization|api[_ -]?key', re.I)

def installed_skills(query='', home=None):
    home = Path(home or Path.home())
    query = str(query or '').strip().casefold()
    result = {}
    for provider, folder in (('Codex', '.codex'), ('DSH', '.dsh'), ('WorkBuddy', '.workbuddy')):
        skills = home / folder / 'skills'
        entries = []
        try:
            paths = sorted(skills.glob('*/SKILL.md'))
        except OSError:
            paths = []
        for path in paths[:200]:
            try:
                with path.open(encoding='utf-8-sig') as stream:
                    header = stream.read(1600).split('---', 2)
            except (OSError, UnicodeError):
                continue
            metadata = header[1] if len(header) == 3 and not header[0].strip() else ''
            desc = re.search(r'^description:\s*(.+)$', metadata, re.MULTILINE)
            description = safe_text(desc[1])[:240] if desc and not _PRIVATE.search(desc[1]) else ''
            if query and query not in (path.parent.name + ' ' + description).casefold():
                continue
            entries.append(dict(name=path.parent.name, description=description))
        result[provider] = entries[:30]
    return result


@lru_cache(maxsize=64)
def _messages(path, modified, provider, session_id):
    """Read a bounded tail, dropping tool output, injected setup blocks and secrets."""
    del modified
    rows = {'user': deque(maxlen=16), 'assistant': deque(maxlen=20)}
    with path.open('rb') as stream:
        size = stream.seek(0, 2)
        head = b''
        if size > 384 * 1024:
            stream.seek(0)
            head = stream.read(min(192 * 1024, size - 384 * 1024))
        stream.seek(max(0, size - 384 * 1024))
        if size > 384 * 1024:
            stream.readline()
        for line in chain(head.split(b'\n')[:-1], stream):
            try:
                row = json.loads(line)
            except (UnicodeError, ValueError):
                continue
            if not isinstance(row, dict):
                continue
            if provider == 'Codex':
                if row.get('type') != 'response_item':
                    continue
                row = row.get('payload')
                if not isinstance(row, dict):
                    continue
                if row.get('role') == 'assistant' and row.get('phase') not in (None, 'final_answer'):
                    continue
            elif row.get('sessionId') != session_id:
                continue
            role = row.get('role')
            if row.get('type') != 'message' or role not in ('user', 'assistant'):
                continue
            blocks = row.get('content')
            if not isinstance(blocks, list):
                continue
            kind = 'input_text' if role == 'user' else 'output_text'
            text = '\n'.join(
                str(block.get('text') or '') for block in blocks
                if isinstance(block, dict) and block.get('type') == kind
            ).strip()
            text = _eligible(text) or '\n'.join(filter(None, (
                _eligible(part.strip()) for part in text[:2600].splitlines()
                if 6 <= len(part.strip()) <= 500
            )))
            if text:
                rows[role].append(text)
    return tuple((role, text) for role, items in rows.items() for text in items)


def _eligible(text):
    text = str(text or '').strip()
    if (not text or len(text) > 2500 or safe_text(text) != text or _PRIVATE.search(text)
            or text.startswith(('# AGENTS.md', '<', '【咕嘎分工】', '【分层记忆资料】'))):
        return ''
    return text


class ConnectedKnowledge:
    def __init__(self, codex_bound, codex_root=None, workbuddy_root=None):
        self.codex_bound = codex_bound
        self.codex_root = Path(codex_root or Path.home() / '.codex' / 'sessions')
        self.workbuddy_root = Path(workbuddy_root or Path.home() / '.workbuddy')
        self._codex_history = {}
        self._workbuddy_paths = {}
        self._scan_until = 0.

    def observe_codex(self, conversation_id, thread_id, messages):
        # Only called by the existing verified binding's read path.
        rows = deque(maxlen=30)
        for item in messages:
            role = item.get('role')
            if role in ('user', 'assistant') and item.get('phase') in (None, 'final_answer'):
                text = _eligible(item.get('text'))
                if text:
                    rows.append((role, text))
        if len(self._codex_history) >= 24 and conversation_id not in self._codex_history:
            del self._codex_history[next(iter(self._codex_history))]
        self._codex_history[conversation_id] = (thread_id, tuple(rows))

    def _workbuddy_sources(self):
        path = self.workbuddy_root / 'workbuddy.db'
        if not path.is_file():
            return []
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.25)) as db:
                # WorkBuddy stores local personal conversations with no remote user_id.
                ids = [row[0] for row in db.execute('''
                    SELECT id FROM sessions
                    WHERE deleted_at IS NULL AND COALESCE(user_id, '') = ''
                    AND COALESCE(is_playground, 0) = 0 AND COALESCE(is_background_automation, 0) = 0
                    ORDER BY last_activity_at DESC LIMIT 12
                ''') if _ID.fullmatch(str(row[0]))]
        except (sqlite3.Error, OSError):
            return []
        if time.monotonic() >= self._scan_until:
            found = {}
            for transcript in (self.workbuddy_root / 'projects').glob('*/*.jsonl'):
                if _ID.fullmatch(transcript.stem):
                    found.setdefault(transcript.stem, []).append(transcript)
            self._workbuddy_paths = found
            self._scan_until = time.monotonic() + 45
        return [(group[0], sid) for sid in ids
                if len(group := self._workbuddy_paths.get(sid, ())) == 1]

    def _source_rows(self, conversation_id):
        bound = self.codex_bound(conversation_id) if conversation_id else {}
        sid = str(bound.get('thread_id') or '')
        if _ID.fullmatch(sid):
            cached_id, cached = self._codex_history.get(conversation_id, ('', ()))
            if sid == cached_id and cached:
                for role, text in cached:
                    yield 'Codex', role, text
            elif bound.get('host_id') in ('', None, 'local'):
                paths = list(self.codex_root.glob(f'*/*/*/rollout-*-{sid}.jsonl'))
                if len(paths) == 1:
                    for role, text in self._read(paths[0], 'Codex', sid):
                        yield 'Codex', role, text
        for path, sid in self._workbuddy_sources():
            for role, text in self._read(path, 'WorkBuddy', sid):
                yield 'WorkBuddy', role, text

    @staticmethod
    def _read(path, provider, session_id):
        try:
            return _messages(path, path.stat().st_mtime_ns, provider, session_id)
        except OSError:
            return ()

    def recall(self, query, conversation_id):
        words = _tokens(query)
        if len(words) < 2:
            return []
        ranked = []
        seen = set()
        for provider, role, text in self._source_rows(conversation_id):
            facts = profile_facts(text) if role == 'user' else ()
            for excerpt in dict.fromkeys([value for _, value in facts] + text.splitlines()):
                excerpt = excerpt.strip()
                if not (6 <= len(excerpt) <= 500) or excerpt in seen:
                    continue
                # WorkBuddy facts can inform other chats. Leave historical model
                # responses scoped to the Codex conversation the owner linked.
                if provider == 'WorkBuddy' and role != 'user':
                    continue
                hits = len(words & _tokens(excerpt))
                if hits < max(2, min(4, len(words) // 5)):
                    continue
                seen.add(excerpt)
                ranked.append((hits + (role == 'user'), provider, role, excerpt[:280]))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [f'[{provider} · 历史{"用户" if role == "user" else "回答"}] {text}'
                for _, provider, role, text in ranked[:4]]
