# -*- coding: utf-8 -*-
"""本机任务台账 —— 让「同一任务只执行一次」有可查凭据。

节点: start / step / done / failed / paused（首尾各一条，中途每轮一条）。
落盘: %LOCALAPPDATA%\\GUGA\\runtime\\task-ledger.json（用户可写，不依赖安装目录）。
写入失败不静默: self.error 保留原因，界面据此显示「台账写入失败」。
"""
import json
import time
from pathlib import Path

from core import guga_paths

PATH = guga_paths.RUNTIME / 'task-ledger.json'
MAX_STEPS = 60


def _now():
    return time.strftime('%Y-%m-%d %H:%M:%S')


class TaskLedger:
    def __init__(self, path=None):
        self.path = Path(path or PATH)
        self.error = ''
        self.records = {}
        self._load()

    # ---------- 落盘 ----------

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8-sig'))
            self.records = data.get('records') or {}
        except (OSError, ValueError, AttributeError):
            self.records = {}

    def save(self):
        """原子写；失败只记录原因，调用方负责把它显示给用户。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({'updated': _now(), 'records': self.records},
                                 ensure_ascii=False, indent=2)
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(payload, encoding='utf-8')
            tmp.replace(self.path)
            self.error = ''
            return True
        except OSError as exc:
            self.error = '台账写入失败：%s' % str(exc)[:160]
            return False

    # ---------- 记账 ----------

    @staticmethod
    def key(job):
        return '%s|%s|%s' % (job.get('taskId') or '', job.get('stId') or '',
                             job.get('attempt') or 1)

    def get(self, job):
        return self.records.get(self.key(job))

    def claim(self, job):
        """None=可以开始；返回终态字符串表示这是重复单，不许再执行。

        running 记录超过 15 分钟视为上一进程中途没了（崩溃/断电）：改成 failed 并允许重来，
        否则一次崩溃会让这单永久卡死。
        """
        rec = self.get(job)
        if not rec:
            return None
        status = rec.get('status')
        if status == 'running':
            try:
                started = float(rec.get('started_ts') or 0)
            except (TypeError, ValueError):
                started = 0.0
            if not started or time.time() - started > 900:
                rec['status'] = 'failed'
                rec['note'] = '上次执行中断（进程重启），允许重新执行'
                rec['finished'] = _now()
                self.save()
                return None
            return 'running'
        if status in ('done', 'failed', 'paused'):
            return status
        return None

    def start(self, job):
        key = self.key(job)
        self.records[key] = {
            'task_id': str(job.get('taskId') or ''),
            'st_id': str(job.get('stId') or ''),
            'attempt': int(job.get('attempt') or 1),
            'name': str(job.get('name') or ''),
            'app': str(job.get('app') or ''),
            'goal': str(job.get('goal') or '')[:200],
            'status': 'running',
            'started': _now(),
            'started_ts': time.time(),
            'steps': [],
        }
        self.save()

    def step(self, job, text, index=None):
        rec = self.get(job)
        if not rec:
            return
        steps = rec.setdefault('steps', [])
        steps.append({'at': _now(), 'text': str(text or '')[:300],
                      'round': int(index or len(steps) + 1)})
        if len(steps) > MAX_STEPS:
            del steps[:-MAX_STEPS]
        self.save()

    def finish(self, job, status, note='', product=''):
        rec = self.get(job)
        if not rec:
            rec = {'task_id': str(job.get('taskId') or ''),
                   'st_id': str(job.get('stId') or ''),
                   'attempt': int(job.get('attempt') or 1),
                   'name': str(job.get('name') or ''),
                   'app': str(job.get('app') or ''),
                   'goal': str(job.get('goal') or '')[:200],
                   'steps': [],
                   'started': _now()}
            self.records[self.key(job)] = rec
        rec['status'] = status
        rec['finished'] = _now()
        rec['note'] = str(note or '')[:300]
        if product:
            rec['product'] = str(product)[:400]
        self.save()

    def mark_skipped(self, job, note=''):
        """重复派单被拦：单独记一条，界面上能看到「已终止/已完成」。"""
        rec = self.get(job)
        if rec:
            rec['skipped_at'] = _now()
            rec['skip_note'] = str(note or '')[:200]
            self.save()

    def snapshot(self):
        return sorted(self.records.values(),
                      key=lambda r: r.get('finished') or r.get('started') or '',
                      reverse=True)
