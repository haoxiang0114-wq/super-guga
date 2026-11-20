# -*- coding: utf-8 -*-
"""托管循环：失败才继续，通过就停；每轮都有硬上限，绝不无限套娃。

安全边界（这是它存在的意义，不是为了"多跑几轮"）：
- `max_rounds` 硬上限；
- `max_seconds` 总预算；
- **同一失败原因连续出现 `max_same_reason` 次就停**（防止同一个错误反复空转）；
- 每一轮都进验收台账；只有 `pass` 才结束；
- 任何一轮判定为 `unverified` 也停下交给人，不硬猜。

它不调用模型做判定——判定完全交给 `AcceptanceLedger`。
"""
from __future__ import annotations

import time

from core.super_agent.acceptance import AcceptanceLedger, PASS, UNVERIFIED

DEFAULT_MAX_ROUNDS = 3
DEFAULT_MAX_SECONDS = 1800
DEFAULT_SAME_REASON = 2
# 真实修复轮可能很长（模型要读代码、跑命令、验证），默认等到 10 分钟。
DEFAULT_WAIT_SECONDS = 600.0

FOLLOW_UP_TEMPLATE = (
    '上一轮的验收没有通过，请继续在这一轮里修好它。\n'
    '判定结论：{verdict}\n'
    '具体原因：\n{reasons}\n'
    '要求：只修这些问题；不要重做已经通过的部分；改完自己跑一遍检查再结束。'
)


class AutoLoop:
    def __init__(self, ledger=None, gateway=None, max_rounds=DEFAULT_MAX_ROUNDS,
                 max_seconds=DEFAULT_MAX_SECONDS, max_same_reason=DEFAULT_SAME_REASON,
                 poll_seconds=1.5, wait_seconds=DEFAULT_WAIT_SECONDS, follow_up=None):
        self.ledger = ledger or AcceptanceLedger()
        self.gateway = gateway
        self.max_rounds = max(1, int(max_rounds))
        self.max_seconds = float(max_seconds)
        self.max_same_reason = max(1, int(max_same_reason))
        self.poll_seconds = float(poll_seconds)
        self.wait_seconds = float(wait_seconds)
        self.follow_up = follow_up or FOLLOW_UP_TEMPLATE

    def _follow_up(self, verdict):
        """追问词：可以是模板字符串，也可以是 callable(verdict) -> str。"""
        if callable(self.follow_up):
            return str(self.follow_up(verdict))
        return str(self.follow_up).format(
            verdict=verdict.get('verdict'),
            reasons='\n'.join('- ' + str(reason) for reason in (verdict.get('reasons') or [])[:6]))

    # ------------------------------------------------------------------ 等待
    def _turns(self, view):
        outline = (view or {}).get('turn_outline') or []
        return len(outline) if isinstance(outline, list) else 0

    def _wait(self, session_id, baseline_turns):
        """等到出现新的一轮且会话不再 running；超时如实返回 None。"""
        deadline = time.monotonic() + self.wait_seconds
        while time.monotonic() < deadline:
            time.sleep(self.poll_seconds)
            try:
                view = self.gateway.session_view(session_id)
            except Exception:  # noqa: BLE001
                continue
            if self._turns(view) > baseline_turns and not (view or {}).get('running'):
                return view
        return None

    def _wait_idle(self, session_id):
        """投递前先确认会话空闲：否则新指令会排在上一轮后面，看起来像"没反应"。"""
        deadline = time.monotonic() + self.wait_seconds
        while time.monotonic() < deadline:
            try:
                view = self.gateway.session_view(session_id)
            except Exception:  # noqa: BLE001
                view = None
            if isinstance(view, dict) and not view.get('running'):
                return view
            time.sleep(self.poll_seconds)
        return None

    # ------------------------------------------------------------------ 主循环
    def run(self, goal, prompt, cwd='', checks=(), conversation_id='', session_id=''):
        started = time.monotonic()
        rounds = []
        reasons_seen = {}
        current_prompt = str(prompt or '')
        current_session = str(session_id or '')

        for index in range(1, self.max_rounds + 1):
            if time.monotonic() - started > self.max_seconds:
                return self._report(goal, rounds, 'budget_exhausted',
                                    '总预算 %.0f 秒用完，停在跑第 %d 轮之前' % (self.max_seconds, index))
            record = self.ledger.open(goal=goal, session_id=current_session,
                                      cwd=cwd, checks=checks)
            baseline_turns = 0
            if self.gateway is not None:
                if not current_session:
                    created = self.gateway.start_task(current_prompt, conversation_id=conversation_id)
                    current_session = created.get('session_id') or ''
                    record = self.ledger.load(record['id'])
                    record['session_id'] = current_session
                    record['started_ok'] = bool(created.get('ok'))
                    self.ledger.save(record)
                else:
                    idle = self._wait_idle(current_session)
                    if idle is None:
                        self.ledger.observe(record['id'], None)
                        self.ledger.run_checks(record['id'])
                        verdict = self.ledger.judge(record['id'])
                        rounds.append(dict(index=index, verdict=verdict.get('verdict'),
                                           reasons=verdict.get('reasons')))
                        return self._report(goal, rounds, 'session_busy',
                                            '会话 %s 一直没空闲，本轮不投递，避免指令排队后误判' % current_session)
                    baseline_turns = self._turns(idle)
                    self.gateway.prompt(current_session, current_prompt, mode='queue')
                view = self._wait(current_session, baseline_turns) if created_ok(record) else None
                if view is None:
                    self.ledger.observe(record['id'], None)
                    self.ledger.run_checks(record['id'])
                    verdict = self.ledger.judge(record['id'])
                    rounds.append(dict(index=index, verdict=verdict.get('verdict'),
                                       reasons=verdict.get('reasons')))
                    return self._report(goal, rounds, 'turn_not_finished',
                                        '第 %d 轮没等到新的一轮结束；不假装完成' % index)
            else:
                view = None

            self.ledger.observe(record['id'], view)
            self.ledger.run_checks(record['id'])
            verdict = self.ledger.judge(record['id'])
            rounds.append(dict(index=index, verdict=verdict.get('verdict'),
                               record=record['id'], reasons=verdict.get('reasons'),
                               changed=(verdict.get('workspace') or {}).get('changed_files')))

            if verdict.get('verdict') == PASS:
                self.ledger.close(record['id'])
                return self._report(goal, rounds, 'passed', '第 %d 轮验收通过' % index)
            if verdict.get('verdict') == UNVERIFIED:
                return self._report(goal, rounds, 'unverified',
                                    '第 %d 轮无法判定，交给人工：%s' % (
                                        index, '；'.join(verdict.get('reasons') or [])[:300]))

            # 失败：同一原因连续出现就停，别空转。
            key = '|'.join((verdict.get('reasons') or [''])[:2])[:300]
            reasons_seen[key] = reasons_seen.get(key, 0) + 1
            if reasons_seen[key] >= self.max_same_reason:
                return self._report(goal, rounds, 'no_progress',
                                    '同一个失败原因连续出现 %d 次，停手交给人' % reasons_seen[key])

            current_prompt = self._follow_up(verdict)

        return self._report(goal, rounds, 'rounds_exhausted',
                            '已跑满 %d 轮仍未通过' % self.max_rounds)

    @staticmethod
    def _report(goal, rounds, stopped, detail):
        return dict(goal=goal, rounds=rounds, stopped=stopped, detail=detail,
                    passed=any(round_row.get('verdict') == PASS for round_row in rounds))


def created_ok(record):
    return record.get('started_ok', True) is not False
