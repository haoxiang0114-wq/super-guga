# -*- coding: utf-8 -*-
"""M1 管家职能①: 习惯记忆 HabitProfile

管家"越用越懂少爷"的数据层: 记录行为事件(开软件/问询/作息),
按 (小时, 星期) 聚合出重复模式, 供管家主动建议:
  "少爷每天这个点都会开腾讯会议, 要提前准备吗?"

设计:
  - SQLite 存储, 与 AgentMemoryStore(对话记忆)分开, 互不干扰
  - 只做加法: 不修改 core/ 既有模块
  - 相同 (hour, weekday, kind, detail_norm) 重复出现 >= min_count 视为习惯
"""
import os
import shutil
import sqlite3
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

LEGACY_PATH = Path(__file__).resolve().parent.parent / 'data' / 'butler_habit.db'
try:  # 习惯记忆属运行期数据：安装目录只读时写到用户目录
    from core import guga_paths
    DEFAULT_PATH = guga_paths.RUNTIME / 'data' / 'butler_habit.db'
except Exception:  # pragma: no cover
    DEFAULT_PATH = (Path(os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA')
                         or os.path.expanduser('~')) / 'GUGA' / 'runtime' / 'data'
                    / 'butler_habit.db')

WEEKDAYS = ('周一', '周二', '周三', '周四', '周五', '周六', '周日')


def _norm_detail(detail):
    """归一化细节词: 路径取文件名去扩展, 普通词去参数, 让同类事件聚到一起."""
    d = str(detail).strip()
    if '\\' in d or '/' in d:
        d = d.replace('\\', '/').split('/')[-1]          # 取文件名
        d = d.rsplit('.', 1)[0] if '.' in d else d       # 去扩展名
    else:
        for sep in ('?', '&', ' - ', '——'):
            d = d.split(sep)[0]
    return d.lower().strip()[:64]


class HabitProfile:
    def __init__(self, path=None, min_count=2):
        self.path = Path(path or DEFAULT_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if path is None and not self.path.exists() and LEGACY_PATH.is_file():
            try:  # 旧安装目录里的习惯数据只复制一次，不删除原文件
                shutil.copy2(str(LEGACY_PATH), str(self.path))
            except Exception:
                pass
        self.min_count = int(min_count)
        with self._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS habit_events (
                    id INTEGER PRIMARY KEY,
                    kind TEXT NOT NULL,        -- app_launch / query / schedule / custom
                    detail TEXT NOT NULL,      -- 归一化主干, 如 'tencent meeting'
                    hour INTEGER NOT NULL,     -- 0-23
                    weekday INTEGER NOT NULL,  -- 0=周一
                    ts REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS habit_events_key
                    ON habit_events(kind, detail, hour, weekday);
            ''')

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    # ==================== 记录 ====================

    def record(self, kind, detail, ts=None):
        """记录一次行为事件. kind∈app_launch/query/schedule/custom."""
        ts = float(ts if ts is not None else time.time())
        lt = time.localtime(ts)
        with self._connect() as db:
            db.execute(
                'INSERT INTO habit_events (kind, detail, hour, weekday, ts) '
                'VALUES (?,?,?,?,?)',
                (str(kind), _norm_detail(detail), lt.tm_hour,
                 (lt.tm_wday) % 7, ts))

    # ==================== 模式/建议 ====================

    def _pattern_rows(self, kind=None):
        """按 (kind, detail, hour) 聚合 — "经常在几点做X".

        不按星期硬分: "每天21点开会"跨6个星期几, 按星期分会把次数打散.
        星期信息在 suggest_now 里作为加权信号而非分组键.
        """
        q = ('SELECT kind, detail, hour, COUNT(*) AS c, '
             'COUNT(DISTINCT weekday) AS wd_span ')
        cond, params = [], []
        if kind:
            cond.append('kind=?')
            params.append(str(kind))
        if cond:
            q += 'FROM habit_events WHERE ' + ' AND '.join(cond) + ' '
        else:
            q += 'FROM habit_events '
        q += 'GROUP BY kind, detail, hour HAVING c >= ?'
        params.append(self.min_count)
        with self._connect() as db:
            return db.execute(q, params).fetchall()

    def habits(self, kind=None, limit=10):
        """稳定习惯: [(kind, detail, hour, count, wd_span), ...] 按次数降序."""
        rows = self._pattern_rows(kind)
        rows.sort(key=lambda r: (-r[3], r[2]))
        return [(r[0], r[1], r[2], r[3], r[4]) for r in rows[:limit]]

    def suggest_now(self, now=None, limit=3):
        """管家的主动建议: 当前小时±1 内命中且当天星期有先例的习惯优先.

        返回 [{'text','detail','count'}...]
        """
        ts = now if now is not None else time.time()
        lt = time.localtime(ts)
        hour, weekday = lt.tm_hour, (lt.tm_wday) % 7
        out = []
        with self._connect() as db:
            for r in self._pattern_rows():
                kind, detail, h, c, wd_span = r
                if abs(h - hour) > 1:
                    continue
                # 当天星期是否有先例(加分项, 不一票否决)
                row = db.execute(
                    'SELECT COUNT(*) FROM habit_events '
                    'WHERE kind=? AND detail=? AND hour=? AND weekday=?',
                    (kind, detail, h, weekday)).fetchone()
                same_wd = row[0]
                bonus = '每逢周%d' % (weekday + 1) if same_wd >= self.min_count and wd_span < 7 else ''
                out.append({'kind': kind, 'detail': detail, 'count': c,
                            'text': '少爷, 您常在%02d:00%s%s%s, 需要提前准备吗'
                                    % (h, self._verb(kind), detail,
                                       ('(%s)' % bonus) if bonus else '')})
        out.sort(key=lambda x: -x['count'])
        return out[:limit]

    @staticmethod
    def _verb(kind):
        return {'app_launch': '打开', 'query': '查询', 'schedule': '安排',
                'custom': '做'}.get(kind, '做')

    def top_details(self, kind=None, limit=5):
        """总体高频行为(不分时段): 画像用."""
        agg = Counter()
        with self._connect() as db:
            q = 'SELECT kind, detail, COUNT(*) c FROM habit_events '
            cond, params = [], []
            if kind:
                cond.append('kind=?'); params.append(str(kind))
            if cond:
                q += 'WHERE ' + ' AND '.join(cond) + ' '
            q += 'GROUP BY kind, detail ORDER BY c DESC LIMIT ?'
            params.append(int(limit))
            for r in db.execute(q, params):
                agg['%s:%s' % (r[0], r[1])] = r[2]
        return dict(agg)

    def summary(self):
        """给管家 prompt 用的一段画像文本."""
        habits = self.habits(limit=5)
        if not habits:
            return '尚未积累到少爷的习惯数据。'
        lines = ['少爷的行为习惯(按重复次数):']
        for kind, detail, h, c, wd_span in habits:
            freq = '几乎每天' if wd_span >= 5 else ('一周多天' if wd_span >= 2 else '偶尔')
            lines.append('- %02d点%s%s (近%d次, %s)'
                         % (h, self._verb(kind), detail, c, freq))
        return '\n'.join(lines)

    def export(self):
        """调试导出."""
        return {'habits': self.habits(limit=20),
                'top': self.top_details()}
