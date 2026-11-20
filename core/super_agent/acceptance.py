# -*- coding: utf-8 -*-
"""零模型验收台账：用环境事实判定"这一轮到底成没成"，不用模型自己的说法。

设计原则（这是它存在的全部理由）：
- **模型自述不算证据**。DSH 说"我完成了"只记录为 `claim`，不参与判定。
- 判定只吃三种硬信号：①声明式检查命令的退出码 ②会话终态 ③工作区实际改动。
- 没有声明任何检查命令时，结论恒为 `unverified` —— 绝不因为"看起来像做完了"而放行。
- 全程不调用任何模型。

典型用法：
    ledger = AcceptanceLedger()
    rec = ledger.open(goal='把首页按钮改成蓝色', session_id=sid, cwd=repo,
                      checks=[['python','-m','pytest','-q']])
    ledger.observe(rec['id'])          # git status/diff + 会话视图快照
    ledger.run_checks(rec['id'])       # 逐条跑，记退出码
    verdict = ledger.judge(rec['id'])  # pass / fail / unverified + 理由
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIR = ROOT / 'runtime' / 'acceptance'
MAX_OUTPUT = 4000
DEFAULT_TIMEOUT = 300

PASS = 'pass'
FAIL = 'fail'
UNVERIFIED = 'unverified'


def _now():
    return round(time.time(), 3)


def _atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.pending')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def _tail(text, limit=MAX_OUTPUT):
    text = str(text or '')
    return text if len(text) <= limit else '…' + text[-limit:]


class AcceptanceLedger:
    def __init__(self, root=None):
        self.root = Path(root or DEFAULT_DIR)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index = self.root / '_index.jsonl'

    # ---------------------------------------------------------------- 存储
    def path(self, record_id):
        return self.root / ('%s.json' % record_id)

    def load(self, record_id):
        path = self.path(record_id)
        if not path.exists():
            raise KeyError('没有这条验收记录：' + str(record_id))
        return json.loads(path.read_text(encoding='utf-8-sig'))

    def save(self, record):
        record['updated_at'] = _now()
        _atomic_write(self.path(record['id']), record)
        return record

    def _append_index(self, record):
        with self.index.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(dict(id=record['id'], at=record.get('opened_at'),
                                         goal=record.get('goal', '')[:200],
                                         verdict=record.get('verdict', '')), ensure_ascii=False) + '\n')

    # ---------------------------------------------------------------- 开/关
    def open(self, goal, session_id='', cwd='', checks=(), expect_changes=True):
        record = dict(
            id='acc-%s' % uuid.uuid4().hex[:12],
            goal=str(goal or '')[:2000],
            session_id=str(session_id or ''),
            cwd=str(cwd or ''),
            checks=[self._normalize_check(check) for check in (checks or [])],
            expect_changes=bool(expect_changes),
            opened_at=_now(),
            state='open',
            claim='',
            session=None,
            workspace=None,
            check_results=[],
            verdict='',
            reasons=[],
        )
        self.save(record)
        self._append_index(record)
        return record

    @staticmethod
    def _normalize_check(check):
        if isinstance(check, str):
            return dict(command=check, shell=True, timeout=DEFAULT_TIMEOUT, expect_exit=0)
        if isinstance(check, (list, tuple)):
            return dict(command=[str(part) for part in check], shell=False,
                        timeout=DEFAULT_TIMEOUT, expect_exit=0)
        if isinstance(check, dict):
            command = check.get('command')
            if isinstance(command, (list, tuple)):
                command = [str(part) for part in command]
            elif command is not None:
                command = str(command)
            return dict(command=command, shell=not isinstance(command, list),
                        timeout=float(check.get('timeout') or DEFAULT_TIMEOUT),
                        expect_exit=int(check.get('expect_exit') or 0),
                        label=str(check.get('label') or '')[:200])
        raise ValueError('检查项必须是命令字符串、argv 列表或配置字典')

    def claim(self, record_id, text):
        """记录 DSH 的自述结论；**不参与判定**，只留档对照。"""
        record = self.load(record_id)
        record['claim'] = str(text or '')[:4000]
        return self.save(record)

    # ---------------------------------------------------------------- 观察
    def observe(self, record_id, session_view=None):
        """抓环境事实：会话视图 + 工作区实际改动。"""
        record = self.load(record_id)
        record['session'] = self._session_facts(session_view)
        record['workspace'] = self._workspace_facts(record.get('cwd') or '')
        return self.save(record)

    @staticmethod
    def _session_facts(view):
        if not isinstance(view, dict):
            return dict(available=False)
        outline = view.get('turn_outline') or []
        outline = outline if isinstance(outline, list) else []
        last = outline[-1] if outline and isinstance(outline[-1], dict) else {}
        return dict(available=True, running=bool(view.get('running')),
                    turns=len(outline),
                    title=str(view.get('title') or '')[:200],
                    last_prompt=str(last.get('prompt') or '')[:400],
                    last_response=str(last.get('response') or '')[:1200])

    @staticmethod
    def _git(cwd, *args):
        return subprocess.run(['git'] + list(args), cwd=cwd, capture_output=True,
                              text=True, encoding='utf-8', errors='replace', timeout=30)

    def _workspace_facts(self, cwd):
        if not cwd or not Path(cwd).is_dir():
            return dict(available=False, reason='没有可用的工作目录')
        try:
            inside = self._git(cwd, 'rev-parse', '--is-inside-work-tree')
        except (OSError, subprocess.SubprocessError) as exc:
            return dict(available=False, reason='git 不可用：' + type(exc).__name__)
        if inside.returncode != 0:
            return dict(available=False, reason='不是 git 工作区')
        status = self._git(cwd, 'status', '--porcelain')
        stat = self._git(cwd, 'diff', '--stat')
        staged = self._git(cwd, 'diff', '--cached', '--stat')
        lines = [line for line in (status.stdout or '').splitlines() if line.strip()]
        return dict(available=True,
                    changed_files=len(lines),
                    changes=[line[:300] for line in lines[:200]],
                    diff_stat=_tail((stat.stdout or '') + (staged.stdout or ''), 2000),
                    clean=not lines)

    # ---------------------------------------------------------------- 检查
    def run_checks(self, record_id):
        record = self.load(record_id)
        cwd = record.get('cwd') or str(ROOT)
        results = []
        for check in record.get('checks') or []:
            results.append(self._run_one(check, cwd))
        record['check_results'] = results
        return self.save(record)

    def _run_one(self, check, cwd):
        command = check.get('command')
        started = time.perf_counter()
        if not command:
            return dict(ok=False, exit=None, label=check.get('label') or '',
                        error='检查项没有命令', elapsed_ms=0)
        try:
            completed = subprocess.run(
                command, shell=bool(check.get('shell')), cwd=cwd,
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=float(check.get('timeout') or DEFAULT_TIMEOUT))
            output = (completed.stdout or '') + (completed.stderr or '')
            code = completed.returncode
        except subprocess.TimeoutExpired:
            return dict(ok=False, exit=None, label=check.get('label') or '',
                        error='超时', elapsed_ms=round((time.perf_counter() - started) * 1000, 1))
        except (OSError, ValueError) as exc:
            return dict(ok=False, exit=None, label=check.get('label') or '',
                        error=type(exc).__name__, elapsed_ms=round((time.perf_counter() - started) * 1000, 1))
        expected = int(check.get('expect_exit') or 0)
        return dict(ok=code == expected, exit=code, expected_exit=expected,
                    label=check.get('label') or '',
                    command=command if isinstance(command, str) else ' '.join(command),
                    output=_tail(output),
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 1))

    # ---------------------------------------------------------------- 判定
    def judge(self, record_id):
        """只按硬信号判定；模型自述一律不参与。"""
        record = self.load(record_id)
        reasons = []
        verdict = PASS

        if not record.get('checks'):
            verdict = UNVERIFIED
            reasons.append('没有声明任何检查命令：不做"看起来完成了"的放行')
        else:
            if len(record.get('check_results') or []) != len(record.get('checks')):
                verdict = UNVERIFIED
                reasons.append('检查还没有全部执行（先 run_checks）')
            for result in record.get('check_results') or []:
                if result.get('ok'):
                    continue
                verdict = FAIL
                label = result.get('label') or str(result.get('command') or '')
                reasons.append('检查未通过：%s（退出码 %s，期望 %s）' % (
                    label[:80], result.get('exit'), result.get('expected_exit')))

        session = record.get('session') or {}
        if verdict == PASS:
            if not session.get('available'):
                verdict = UNVERIFIED
                reasons.append('没有拿到会话终态（先 observe 并传入 session_view）')
            elif session.get('running'):
                verdict = UNVERIFIED
                reasons.append('会话还在跑，谈不上验收')

        workspace = record.get('workspace') or {}
        if verdict == PASS and record.get('expect_changes'):
            if not workspace.get('available'):
                verdict = UNVERIFIED
                reasons.append('没有拿到工作区改动（无法确认真的改了东西）')
            elif not workspace.get('changed_files'):
                verdict = FAIL
                reasons.append('工作区没有任何改动，但这一轮被要求有改动')

        if verdict == PASS:
            reasons.append('检查命令全部通过' + (
                '，会话已结束' if session.get('available') else '') +
                ('，工作区有 %d 个文件改动' % workspace.get('changed_files')
                 if workspace.get('changed_files') else ''))
        claim = (record.get('claim') or '').strip()
        if claim:
            reasons.append('（模型自述仅留档，不参与判定：%s）' % claim[:120])

        record['verdict'] = verdict
        record['reasons'] = reasons
        record['judged_at'] = _now()
        if verdict != UNVERIFIED:
            record['state'] = 'judged'
        return self.save(record)

    def close(self, record_id):
        record = self.load(record_id)
        if not record.get('verdict'):
            record = self.judge(record_id)
        record['state'] = 'closed'
        record['closed_at'] = _now()
        return self.save(record)

    # ---------------------------------------------------------------- 导出
    def export(self, record_id, path=None):
        record = self.load(record_id)
        if path:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.suffix.lower() == '.md':
                target.write_text(self.to_markdown(record), encoding='utf-8')
            else:
                target.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
            return dict(ok=True, path=str(target))
        return dict(ok=True, record=record, markdown=self.to_markdown(record))

    @staticmethod
    def to_markdown(record):
        lines = ['# 验收台账 %s' % record.get('id', ''),
                 '', '- 目标：%s' % record.get('goal', ''),
                 '- 会话：%s' % (record.get('session_id') or '（未关联）'),
                 '- 结论：**%s**' % (record.get('verdict') or '未判定'),
                 '- 工作目录：%s' % (record.get('cwd') or '（未指定）'),
                 '']
        session = record.get('session') or {}
        if session.get('available'):
            lines += ['## 会话终态', '',
                      '- 标题：%s' % (session.get('title') or ''),
                      '- 轮数：%s，仍在跑：%s' % (session.get('turns'), session.get('running')),
                      '- 最后一轮答复：%s' % (session.get('last_response') or ''), '']
        workspace = record.get('workspace') or {}
        if workspace.get('available'):
            lines += ['## 工作区改动', '',
                      '- 改动文件数：%s' % workspace.get('changed_files'), '',
                      '```', (workspace.get('diff_stat') or '').strip(), '```', '']
            for change in workspace.get('changes') or []:
                lines.append('- `%s`' % change)
            lines.append('')
        results = record.get('check_results') or []
        if results:
            lines += ['## 检查命令', '', '| 结果 | 退出码 | 耗时 | 命令 |', '|---|---|---|---|']
            for result in results:
                lines.append('| %s | %s | %sms | `%s` |' % (
                    'PASS' if result.get('ok') else 'FAIL', result.get('exit'),
                    result.get('elapsed_ms'), str(result.get('command') or result.get('label'))[:90]))
            lines.append('')
        if record.get('reasons'):
            lines += ['## 判定理由', '']
            lines += ['- %s' % reason for reason in record['reasons']]
            lines.append('')
        return '\n'.join(lines)

    def list(self, limit=30):
        if not self.index.exists():
            return []
        rows = []
        for line in self.index.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows[-limit:]
