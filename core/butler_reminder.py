# -*- coding: utf-8 -*-
"""M1 管家职能②: 事务提醒 ReminderService

少爷说"明早九点提醒我开会" -> 管家记下 -> 到点主动播报.

设计:
  - JSON 持久化(data/butler_reminders.json), 跨重启恢复
  - 不依赖 Qt: tick() 由外部定时器驱动(桌宠已有 UI 定时器可挂),
    到期回调 on_due(reminder) 由调用方注入(语音播报/气泡)
  - 时间解析: 支持绝对 "YYYY-MM-DD HH:MM" 与相对 "+30m/+2h/明天09:00"
"""
import json
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from core import guga_paths

# 提醒数据属于运行期数据：必须放用户可写目录。包里如果放在 data/（解压到
# Program Files 时只读），启动第一次 _save 就会 PermissionError 把进程带崩。
DEFAULT_PATH = guga_paths.RUNTIME / 'butler_reminders.json'


def parse_when(text, now=None):
    """把口语时间解析成时间戳. 返回 (ts, 说明) 或抛 ValueError.

    支持: +30m / +2h / +1d, HH:MM(今天, 已过则明天), 明天HH:MM,
          YYYY-MM-DD HH:MM, YYYY-MM-DD
    """
    now = now if now is not None else time.time()
    text = str(text).strip()
    m = re.fullmatch(r'\+(\d+)\s*([mhd])', text, re.I)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {'m': 60, 'h': 3600, 'd': 86400}[unit] * n
        return now + delta, '%d%s后' % (n, {'m': '分钟', 'h': '小时', 'd': '天'}[unit])
    m = re.fullmatch(r'(今天|明天)?\s*(\d{1,2})[:：点](\d{1,2})?分?', text)
    if m:
        base = datetime.fromtimestamp(now)
        if m.group(1) == '明天':
            base += timedelta(days=1)
        hh = int(m.group(2))
        mm = int(m.group(3) or 0)
        cand = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if m.group(1) != '明天' and cand.timestamp() <= now:
            cand += timedelta(days=1)  # 已过 -> 明天
        return cand.timestamp(), cand.strftime('%m-%d %H:%M')
    for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            cand = datetime.strptime(text, fmt)
            if len(fmt) == 10:
                cand = cand.replace(hour=9)  # 只给日期默认 09:00
            return cand.timestamp(), cand.strftime('%m-%d %H:%M')
        except ValueError:
            continue
    raise ValueError('听不懂时间: %r (试试 +30m / 09:00 / 明天9点 / 2026-11-05 09:00)' % text)


class ReminderService:
    def __init__(self, path=None, on_due=None):
        self.path = Path(path or DEFAULT_PATH)
        self.save_error = ''
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self.on_due = on_due          # 回调: fn({'id','text','when_text',...})
        self._load()

    # ==================== 持久化 ====================

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            data = {'reminders': []}
        self._reminders = [r for r in data.get('reminders', [])
                           if not r.get('done')]
        self._save()

    def _save(self):
        """落盘失败只降级不抛：提醒还能在本次会话里用，界面由调用方提示。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({'reminders': self._reminders},
                           ensure_ascii=False, indent=2),
                encoding='utf-8')
            self.save_error = ''
            return True
        except OSError as exc:
            self.save_error = str(exc)[:160]
            return False

    # ==================== 增删查 ====================

    def add(self, when_text, text, now=None):
        """添加提醒. when_text 支持 +30m / 09:00 / 明天9点 / 绝对时间."""
        ts, when_display = parse_when(when_text, now)
        reminder = {
            'id': uuid.uuid4().hex[:8],
            'text': str(text).strip(),
            'due': ts,
            'when_text': when_display,
            'created': now if now is not None else time.time(),
            'done': False,
        }
        self._reminders.append(reminder)
        self._save()
        return reminder

    def remove(self, rid):
        before = len(self._reminders)
        self._reminders = [r for r in self._reminders if r['id'] != rid]
        self._save()
        return len(self._reminders) < before

    def list_pending(self):
        return sorted(self._reminders, key=lambda r: r['due'])

    # ==================== 到期检查 ====================

    def tick(self, now=None):
        """由外部定时器周期调用; 返回本批到期的提醒列表.

        到期的提醒标记 done 并持久化(防重复播报), 同时逐个回调 on_due.
        """
        now = now if now is not None else time.time()
        due, keep = [], []
        for r in self._reminders:
            if not r['done'] and r['due'] <= now:
                due.append(r)
                r['done'] = True
            else:
                keep.append(r)
        if due:
            self._reminders = keep
            self._save()
            if self.on_due:
                for r in due:
                    try:
                        self.on_due(r)
                    except Exception:
                        pass  # 播报失败不影响主循环
        return due

    def status(self):
        return {'pending': len(self._reminders),
                'next': (self.list_pending()[0]['when_text']
                         + ' ' + self.list_pending()[0]['text']
                         if self._reminders else None)}
