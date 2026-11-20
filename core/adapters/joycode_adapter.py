"""JoyCode 适配器：咕嘎派单 → 唤起/激活 JoyCode IDE → 自动触发 /guga-run → 轮询回执 → 回送状态机。

边界（重要）：
- JoyCode 只开放「被扩展」，没有可 headless 运行 Agent 的 REST/CLI，所以这里只能
  写任务、唤起 IDE、通过界面把 /guga-run 送进对话，等待 Agent 执行后写回回执。
- 本文件不导入形象渲染、动画、窗口动效，也不改调度核心；只做文件协议和进程/界面编排。
- 界面自动化全部收敛在 `core/adapters/joycode_ui.py`，复用既有 UIA/OCR/输入原语；
  失败会返回分级错误并保留人工确认路径（`auto_trigger=False` 或直接触发失败时）。

任务协议：{task_id, goal, acceptance, deadline, callback}
回执协议：{task_id, status, artifact_paths, self_check}

典型用法：
    adapter = JoyCodeAdapter(journal=agent.journal)
    task = adapter.dispatch('整理验收清单', '清单包含 3 条可核对项')       # 只派单，人工敲 /guga-run
    result = adapter.run_task('整理验收清单', '清单包含 3 条可核对项')     # 全自动：派单+触发+等回执

    receipt = adapter.wait_for_result(task['task_id'], timeout=1800)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

from core.failure_report import failure_payload

TASK_ID = re.compile(r'[A-Za-z0-9._-]{1,80}')
RECEIPT_STATUS = ('done', 'blocked', 'failed')
# 回执状态 → 咕嘎 TaskJournal 的状态
STATE_MAP = {'done': 'completed', 'blocked': 'paused', 'failed': 'paused'}


def _idem_key(goal, workspace):
    """幂等键：同一 goal + 同一目标工程 = 同一件事，未终态前不允许再写任务文件。"""
    raw = '%s|%s' % (' '.join(str(goal or '').split()),
                     str(workspace or '').rstrip('\\/').casefold())
    return 'goal-' + hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]


def _bus_log(event, **fields):
    """派单总线日志（%GUGA_HOME%/runtime/joycode_bus.log），逐行可核对。"""
    try:
        from core import guga_paths
        path = Path(guga_paths.RUNTIME) / 'joycode_bus.log'
        row = dict(time=time.strftime('%Y-%m-%d %H:%M:%S'), event=event)
        row.update(fields)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + '\n')
    except Exception:
        pass


class DuplicateTaskError(RuntimeError):
    """同一 goal + 同一工程已有未终态任务：不写新文件，也不重复触发 /guga-run。"""
    code = 'duplicate_task'

    def __init__(self, existing, idem_key='', workspace=''):
        self.existing = str(existing or '')
        self.idem_key = str(idem_key or '')
        self.workspace = str(workspace or '')
        self.message = ('同一目标已经在执行（%s），这次不重复派单；'
                        '等它出回执（done/failed/blocked）后才允许重派。' % self.existing)
        super().__init__(self.message)


class JoyCodeWorkspaceError(RuntimeError):
    """找不到 JoyCode 当前工程。

    任务必须写到 JoyCode 真正打开的工作区（它只读自己工作区里的 ``.guga``），
    找不到时宁可停下来让用户选择，也不能静默写到默认目录后回“任务不存在”。
    """
    code = 'workspace_not_found'

    def __init__(self, message=None, candidates=None):
        super().__init__(message or '没找到 JoyCode 当前工程，请选择')
        self.candidates = list(candidates or [])


def _file_uri_to_path(uri):
    """把 JoyCode 配置里的 file:///d%3A/xxx 还原成本机路径。"""
    text = str(uri or '').strip()
    if not text:
        return ''
    if text.startswith('file:'):
        raw = unquote(urlparse(text).path or '')
        # /d:/rengongzhineng -> d:/rengongzhineng
        if raw.startswith('/') and len(raw) > 2 and raw[2] == ':':
            raw = raw[1:]
        return raw.replace('/', '\\')
    return text


def _joycode_user_dir():
    roaming = os.environ.get('APPDATA')
    base = Path(roaming) if roaming else Path(os.path.expanduser('~')) / 'AppData' / 'Roaming'
    return base / 'JoyCode' / 'User'


def _read_json_file(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except Exception:
        return None


def _storage_candidates(user_dir):
    """JoyCode 自己记录的当前/最近窗口工程（storage.json）。"""
    rows = []
    storage = _read_json_file(user_dir / 'globalStorage' / 'storage.json') or {}
    last = (storage.get('windowsState') or {}).get('lastActiveWindow') or {}
    if last.get('folder'):
        rows.append(('joycode_last_active_window', _file_uri_to_path(last['folder'])))
    for item in ((storage.get('backupWorkspaces') or {}).get('folders') or []):
        if item.get('folderUri'):
            rows.append(('joycode_backup_workspaces', _file_uri_to_path(item['folderUri'])))
    return rows


def _recent_candidates(user_dir):
    """globalStorage/state.vscdb 里的最近打开记录（只读打开，不碰用户配置）。"""
    db = user_dir / 'globalStorage' / 'state.vscdb'
    if not db.is_file():
        return []
    rows = []
    try:
        import sqlite3
        con = sqlite3.connect('file:%s?mode=ro' % db.as_posix(), uri=True)
        try:
            row = con.execute("select value from ItemTable where key='history.recentlyOpenedPathsList'").fetchone()
        finally:
            con.close()
        data = json.loads(row[0]) if row and row[0] else {}
        for item in (data.get('entries') or []):
            uri = item.get('folderUri') or item.get('workspace')
            if not uri:
                continue
            path = _file_uri_to_path(uri)
            if path.casefold().endswith('.code-workspace'):
                rows.append(('joycode_recent_workspace_file', str(Path(path).parent)))
            else:
                rows.append(('joycode_recent', path))
    except Exception:
        return []
    return rows


def _last_workspace_file():
    """咕嘎自己记下的“上次派单工程”，只用于选择框默认值，不自动落单。"""
    try:
        from core import guga_paths
        return Path(guga_paths.RUNTIME) / 'joycode_workspace.json'
    except Exception:
        return None


def _remember_workspace(path, source=''):
    target = _last_workspace_file()
    if target is None:
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({'path': str(path), 'source': str(source or '')},
                                     ensure_ascii=False), encoding='utf-8')
    except Exception:
        pass


def _last_workspace():
    target = _last_workspace_file()
    if target is None:
        return ''
    data = _read_json_file(target) or {}
    return str(data.get('path') or '')


def _workspace_storage_candidates(user_dir):
    """每个工程一份 workspaceStorage/<hash>/workspace.json，按最近修改排序。"""
    root = user_dir / 'workspaceStorage'
    if not root.is_dir():
        return []
    try:
        folders = sorted((p for p in root.iterdir() if p.is_dir()),
                         key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return []
    rows = []
    for folder in folders:
        data = _read_json_file(folder / 'workspace.json') or {}
        uri = data.get('folder') or data.get('workspace')
        if uri:
            rows.append(('joycode_workspace_storage', _file_uri_to_path(uri)))
    return rows


def _joycode_window_title():
    """当前 JoyCode 主窗口标题，例如 ``index.html - rengongzhineng - JoyCode``。"""
    try:
        import psutil
        import win32gui
        import win32process
    except Exception:
        return ''
    found = []

    def visit(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if (psutil.Process(pid).name() or '').casefold() != 'joycode.exe':
                return True
            title = str(win32gui.GetWindowText(hwnd) or '')
            if title:
                found.append(title)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(visit, None)
    except Exception:
        return ''
    return max(found, key=len) if found else ''


def _title_matches(title, path):
    name = Path(path).name.casefold()
    if not name or not title:
        return False
    if name in title.casefold():
        return True
    short = name[:12]
    return len(short) >= 4 and short in title.casefold()


def resolve_workspace(explicit=None):
    """定位 JoyCode **当前打开**的工程目录。

    返回 ``{path, source, candidates, error, window_title}``；``path`` 为空表示
    识别失败，调用方必须请用户选择，不能退回到某个默认目录静默写入。

    识别优先级：
    1. 显式传入 / ``GUGA_WORKSPACE``（仍是覆盖项，但演示不依赖它）；
    2. JoyCode 的 ``storage.json`` → ``windowsState.lastActiveWindow.folder``；
    3. ``state.vscdb`` 的 ``history.recentlyOpenedPathsList``；
    4. ``workspaceStorage/*/workspace.json``（最近修改优先）；
    5. 源码运行时才用模块路径向上找 ``.joycode`` / ``.guga``（冻结后该线索失效）；
    6. 用 JoyCode 窗口标题在候选中做二次确认。
    """
    candidates = []

    def add(source, value):
        if not value:
            return
        try:
            path = Path(str(value)).expanduser().resolve()
        except Exception:
            return
        key = str(path).rstrip('\\/').casefold()
        if not key or any(str(item['path']).rstrip('\\/').casefold() == key for item in candidates):
            return
        candidates.append(dict(path=str(path), source=source, exists=path.is_dir()))

    if explicit:
        add('explicit', explicit)
        if candidates and candidates[0]['exists']:
            return dict(path=candidates[0]['path'], source='explicit',
                        candidates=candidates, error='', window_title='')

    env = os.environ.get('GUGA_WORKSPACE', '').strip()
    if env:
        add('env:GUGA_WORKSPACE', env)
        if candidates and candidates[0]['exists']:
            return dict(path=candidates[0]['path'], source='env:GUGA_WORKSPACE',
                        candidates=candidates, error='', window_title='')

    user_dir = _joycode_user_dir()
    for source, value in (_storage_candidates(user_dir)
                          + _recent_candidates(user_dir)
                          + _workspace_storage_candidates(user_dir)):
        add(source, value)

    # 源码运行保留原线索；冻结后 __file__ 在 _internal，不能当成工程路径。
    if not getattr(sys, 'frozen', False):
        for parent in Path(__file__).resolve().parents[:6]:
            if (parent / '.joycode').is_dir() or (parent / '.guga').is_dir():
                add('module_path', parent)
                break

    candidates = [item for item in candidates if item['exists']]
    title = _joycode_window_title()
    if not candidates:
        # 识别失败：带上咕嘎上次派过单的工程，仅作为选择框默认值，不自动写入。
        return dict(path=None, source='none', candidates=[], window_title=title,
                    default_workspace=_last_workspace(),
                    error='没找到 JoyCode 当前工程，请选择')
    matched = [item for item in candidates if _title_matches(title, item['path'])]
    if len(matched) == 1:
        hit = matched[0]
        if hit is not candidates[0]:
            hit = dict(hit, source='joycode_window_title')
            candidates = [hit] + [item for item in candidates if item is not matched[0]]
        return dict(path=hit['path'], source=hit['source'], candidates=candidates,
                    error='', window_title=title, title_confirmed=True)
    return dict(path=candidates[0]['path'], source=candidates[0]['source'],
                candidates=candidates, error='', window_title=title,
                title_confirmed=False)


def _workspace_root(explicit=None):
    """兼容旧调用：只返回路径；识别失败返回 None（不落默认目录）。"""
    path = resolve_workspace(explicit).get('path')
    return Path(path) if path else None


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


class JoyCodeAdapter:
    def __init__(self, workspace=None, launcher=None, journal=None, on_result=None,
                 launch=True, poll_interval=1.0, ui_automation=False, auto_trigger=None,
                 ui=None, computer=None):
        self._ui = ui
        self._computer = computer
        self.workspace_info = resolve_workspace(workspace)
        self.workspace = None
        self.inbox = None
        self.outbox = None
        self.workspace_source = str(self.workspace_info.get('source') or 'none')
        self.workspace_candidates = list(self.workspace_info.get('candidates') or [])
        self.workspace_pinned = bool(workspace) or bool(os.environ.get('GUGA_WORKSPACE', '').strip())
        if self.workspace_info.get('path'):
            self._set_workspace(self.workspace_info['path'])
        self.launcher = launcher or os.environ.get('GUGA_JOYCODE_LAUNCHER') or self._default_launcher()
        self.journal = journal
        self.on_result = on_result
        self.launch = launch
        self.poll_interval = max(.2, float(poll_interval))
        # 自动触发开关：默认关闭，保留原手工路径；run_task() 默认开启。
        self.auto_trigger = bool(ui_automation if auto_trigger is None else auto_trigger)
        self._handled = set()
        self._lock = threading.Lock()
        # 本进程派出过的任务：只有这些任务在回执到达后才会被移出 inbox，
        # 避免把历史上保留的成功单（060558/063703/065513）顺手扫走。
        self._created = {}
        self._stale_swept = False

    # ---- 工作区 ----
    def _set_workspace(self, path):
        self.workspace = Path(path).expanduser().resolve()
        self.inbox = self.workspace / '.guga' / 'inbox'
        self.outbox = self.workspace / '.guga' / 'outbox'
        self.inbox_archive = self.workspace / '.guga' / 'inbox-archive'
        self.outbox_archive = self.workspace / '.guga' / 'outbox-archive'
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.outbox.mkdir(parents=True, exist_ok=True)
        _remember_workspace(self.workspace, self.workspace_source)
        if self._ui is not None:
            self._ui.workspace = str(self.workspace)

    def refresh_workspace(self):
        """每次派单前重新确认工程：JoyCode 换工程后不能继续写旧目录。"""
        if self.workspace_pinned:
            return self.workspace_info
        info = resolve_workspace(None)
        if info.get('path'):
            self.workspace_info = info
            self.workspace_source = str(info.get('source') or 'none')
            self.workspace_candidates = list(info.get('candidates') or [])
            if str(info['path']) != str(self.workspace):
                self._set_workspace(info['path'])
        return self.workspace_info

    def use_workspace(self, path):
        """用户在弹框里选中的工程：钉住，不再自动改判。"""
        path = Path(path).expanduser().resolve()
        self.workspace_pinned = True
        self.workspace_source = 'user_selected'
        self.workspace_info = dict(path=str(path), source='user_selected', error='',
                                   candidates=[dict(path=str(path), source='user_selected',
                                                    exists=path.is_dir())])
        self.workspace_candidates = list(self.workspace_info['candidates'])
        self._set_workspace(path)
        return self.workspace

    def _workspace_failure(self):
        return failure_fields(
            'workspace_not_found', 'refresh_workspace 没有找到 JoyCode 当前打开的工程目录',
            app='JoyCode',
            candidates=self.workspace_candidates or self.workspace_info.get('candidates') or [],
            default=(self.workspace_info.get('default_workspace') or _last_workspace()),
            workspace=dict(source=self.workspace_source, path=''),
            manual=('请在咕嘎弹出的选择框里选中 JoyCode 当前打开的工程目录；'
                    '任务会写到该目录的 .guga/inbox，JoyCode 只读自己工作区的回执。'),
            message='没找到 JoyCode 当前工程，请选择')

    # ---- 界面 ----
    def _ui_layer(self):
        if self._ui is None:
            from core.adapters.joycode_ui import JoyCodeUI
            self._ui = JoyCodeUI(computer=self._computer, launcher=self.launcher,
                                 workspace=self.workspace)
        return self._ui

    def trigger(self, task_id=None, text=None, submit=True):
        """把 /guga-run（可带 task_id）送进 JoyCode 对话并回车。

        默认路径是按窗口比例真实点击输入框，回读断言后再键入/回车。
        失败抛 JoyCodeUIError，分级码：
        - ``click_miss``：点不到输入框（附候选点与焦点证据，需人工点一下）
        - ``input_text_mismatch``：点到了但回读与命令不一致（不会带残留回车）
        - ``input_not_found``：面板未确认可见（已不再自动按 Ctrl+Alt+B，避免把面板按关）
        - ``occluded``：面板区域被其他窗口遮挡，未点击也未按键
        - ``window_not_ready`` / ``foreground_busy`` / ``modal_blocked``：环境或弹窗问题
        回执超时由 run_task 判定为 ``no_receipt``。
        """
        command = text or ('/guga-run ' + str(task_id)) if (text or task_id) else None
        if not command:
            raise ValueError('trigger 需要 task_id 或 text')
        return self._ui_layer().trigger(command, submit=submit)

    def open_ide(self, project=None):
        """确保 JoyCode 存在：没有才冷启动；已在运行只复用，不抢占前台（最小化才恢复）。"""
        if project:
            self.use_workspace(project)
        if self.workspace is None:
            raise JoyCodeWorkspaceError(candidates=self.workspace_candidates)
        target = Path(self.workspace)
        if not target.exists():
            raise FileNotFoundError('工程路径不存在：' + str(target))
        try:
            return self._ui_layer().ensure_window(activate=True)
        except Exception as exc:
            from core.adapters.joycode_ui import JoyCodeUIError
            if isinstance(exc, JoyCodeUIError):
                raise
            raise RuntimeError('唤起 JoyCode 失败：%s' % exc) from None

    # ---- 派单 ----
    def _receipt_status(self, task_id):
        if self.outbox is None:
            return ''
        path = self.outbox / (task_id + '.json')
        if not path.is_file():
            return ''
        data = _read_json_file(path)
        if isinstance(data, dict) and data.get('status') in RECEIPT_STATUS:
            return str(data['status'])
        return ''

    def _idem_state(self, idem, exclude=''):
        """扫 inbox：返回（未终态的同目标任务, 最近一条已终态任务）。

        已消费到 inbox-archive 的旧任务不再算 pending，但如果它的回执是终态，
        就作为“重派依据”返回，日志才能写清为什么允许重派。
        """
        pending, terminal = '', None
        if self.inbox is None or not self.inbox.is_dir():
            return pending, terminal

        def match(path):
            data = _read_json_file(path)
            if not isinstance(data, dict):
                return None
            item_idem = str(data.get('idem_key') or '') or _idem_key(
                data.get('goal') or path.stem, data.get('workspace') or self.workspace)
            return data if item_idem == idem else None

        for path in sorted(self.inbox.glob('*.json'), key=lambda p: (p.stat().st_mtime, p.name)):
            if path.stem == exclude or not path.is_file() or match(path) is None:
                continue
            status = self._receipt_status(path.stem)
            if status:
                terminal = dict(task_id=path.stem, status=status)
            elif not pending:
                pending = path.stem
        if terminal is None and self.inbox_archive is not None and self.inbox_archive.is_dir():
            archived = sorted(self.inbox_archive.glob('*.json'),
                              key=lambda p: p.stat().st_mtime, reverse=True)
            for path in archived:
                if match(path) is None:
                    continue
                status = self._receipt_status(path.stem)
                if status:
                    terminal = dict(task_id=path.stem, status=status)
                    break
        return pending, terminal

    def pending_duplicate(self, goal, log_block=False):
        """同一 goal + 同一工程是否已有未终态任务；用于界面侧挡住重复派单。"""
        if self.inbox is None:
            return ''
        idem = _idem_key(goal, self.workspace)
        pending, _ = self._idem_state(idem)
        if not pending:
            with self._lock:
                for record in self._created.values():
                    if record.get('idem_key') == idem:
                        pending = str(record.get('task_id') or '')
                        break
        if pending and log_block:
            _bus_log('duplicate_blocked', stage='ui_busy', task_id=pending,
                     idem_key=idem, goal=str(goal), workspace=str(self.workspace),
                     reason='同一目标已有未终态任务，界面侧没有放行新的派单')
        return pending or ''

    def _consume(self, task_id, receipt):
        """回执到达终态：把本进程派出的任务移出 inbox，避免被再次触发。"""
        with self._lock:
            record = self._created.pop(str(task_id), None)
        if not record or self.inbox is None:
            return ''
        source = self.inbox / (str(task_id) + '.json')
        if not source.is_file():
            return ''
        target_dir = self.inbox_archive or (self.workspace / '.guga' / 'inbox-archive')
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / source.name
            if target.exists():
                target = target_dir / ('%s-%s.json' % (task_id, time.strftime('%H%M%S')))
            shutil.move(str(source), str(target))
        except Exception as exc:
            _bus_log('consume_failed', task_id=str(task_id), error=str(exc)[:200])
            return ''
        _bus_log('consume', task_id=str(task_id), status=str(receipt.get('status') or ''),
                 workspace=str(self.workspace), moved_to=str(target),
                 reason='回执已到终态，任务移出 inbox，不再被 /guga-run 重复触发')
        return str(target)

    def _rollback_task(self, task_id, reason):
        """触发没送出去（未提交任何内容）时撤回任务文件，避免它变成待办垃圾。"""
        if self.inbox is None:
            return False
        path = self.inbox / (str(task_id) + '.json')
        with self._lock:
            self._created.pop(str(task_id), None)
        if not path.is_file():
            return False
        try:
            path.unlink()
        except Exception:
            return False
        _bus_log('create_rollback', task_id=str(task_id), reason=str(reason))
        return True

    def sweep_stale_receipts(self, dry_run=True):
        """done 回执的产物已不存在 → 悬空记录；默认只列候选，dry_run=False 才归档。"""
        rows = []
        if self.outbox is None or not self.outbox.is_dir():
            return rows
        for path in sorted(self.outbox.glob('*.json')):
            if path.name.endswith('.progress.json'):
                continue
            data = _read_json_file(path)
            if not isinstance(data, dict) or data.get('status') != 'done':
                continue
            artifacts = [str(item) for item in (data.get('artifact_paths') or [])
                         if str(item).strip()]
            if not artifacts:
                continue
            if any(Path(item).expanduser().exists() for item in artifacts):
                continue
            rows.append(dict(task_id=path.stem, path=str(path), artifact_paths=artifacts))
        if dry_run:
            return rows
        target_dir = self.outbox_archive or (self.workspace / '.guga' / 'outbox-archive')
        for item in rows:
            source = Path(item['path'])
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / source.name
                if target.exists():
                    target = target_dir / ('%s-%s.json' % (source.stem, time.strftime('%H%M%S')))
                shutil.move(str(source), str(target))
                _bus_log('stale_receipt', task_id=item['task_id'], moved_to=str(target),
                         artifact_paths=item['artifact_paths'],
                         reason='回执 status=done 但产物已不存在，移出 outbox 避免误判为已完成')
            except Exception as exc:
                _bus_log('stale_receipt_failed', task_id=item['task_id'], error=str(exc)[:200])
        return rows

    def create_task(self, goal, acceptance, deadline='', callback='guga:desk', task_id=None):
        if self.inbox is None:
            # 没有工程目录就不写任务：写错位置只会换来 JoyCode 的“任务不存在”。
            raise JoyCodeWorkspaceError(candidates=self.workspace_candidates)
        key = task_id or ('T-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
        if not TASK_ID.fullmatch(key):
            raise ValueError('task_id 只允许字母、数字、点、下划线和连字符，长度 1-80')
        if not str(goal or '').strip():
            raise ValueError('goal 不能为空')
        if not str(acceptance or '').strip():
            raise ValueError('acceptance 不能为空')
        idem = _idem_key(goal, self.workspace)
        pending, terminal = self._idem_state(idem, exclude=key)
        if not pending:
            with self._lock:
                for record in self._created.values():
                    if record.get('idem_key') == idem:
                        pending = str(record.get('task_id') or '')
                        break
        if pending:
            _bus_log('duplicate_blocked', stage='create', task_id=pending, blocked_task=key,
                     idem_key=idem, goal=str(goal), workspace=str(self.workspace),
                     reason='同一目标未终态，本次没有写新任务文件，也没有触发 /guga-run')
            raise DuplicateTaskError(pending, idem_key=idem, workspace=str(self.workspace))
        task = dict(task_id=key, goal=str(goal), acceptance=str(acceptance),
                    deadline=str(deadline or ''), callback=str(callback or 'guga:desk'),
                    workspace=str(self.workspace), idem_key=idem)
        _write_json(self.inbox / (key + '.json'), task)
        with self._lock:
            self._created[key] = dict(task_id=key, goal=str(goal), idem_key=idem,
                                      workspace=str(self.workspace), created=time.time())
        _bus_log('create', task_id=key, idem_key=idem, workspace=str(self.workspace),
                 goal=str(goal))
        if terminal:
            reason = '上一条 %s 已终态 status=%s，允许重派' % (terminal['task_id'],
                                                              terminal['status'])
            _bus_log('repost', task_id=key, previous=terminal['task_id'],
                     previous_status=terminal['status'], idem_key=idem,
                     workspace=str(self.workspace), reason=reason)
        return task

    def dispatch(self, goal, acceptance, deadline='', callback='guga:desk', task_id=None,
                 wait=False, timeout=1800, on_result=None, auto_trigger=None):
        """写任务文件；默认确保 JoyCode 存在（已运行只复用，不抢前台）。auto_trigger=True 时自动送 /guga-run。"""
        self.refresh_workspace()
        if self.workspace is None:
            raise JoyCodeWorkspaceError(candidates=self.workspace_candidates)
        task = self.create_task(goal, acceptance, deadline, callback, task_id)
        if self.launch:
            self.open_ide()
        enabled = self.auto_trigger if auto_trigger is None else bool(auto_trigger)
        if enabled:
            task['trigger'] = self.trigger(task['task_id'])
        if wait:
            receipt = self.wait_for_result(task['task_id'], timeout=timeout)
            if receipt and (on_result or self.on_result):
                (on_result or self.on_result)(receipt)
            return task, receipt
        return task

    def run_task(self, goal, acceptance, deadline='', callback='guga:desk', task_id=None,
                 timeout=1800, auto_trigger=True, cancel=None):
        """咕嘎可调用的端到端入口：派单 → 自动触发 → 等回执 → 状态机回传。

        不抛界面异常，统一返回结构化结果：
        ok / error / detail / manual（人工确认说明）/ receipt。
        失败分三级，提示互不复用：
        - ``click_miss``：点不到输入框 → 请人工点一下输入框再重试
        - ``input_text_mismatch``：点到了但回读断言不一致 → 已中止，未提交任何内容
        - ``no_receipt``：命令已发出但没有回执 → 不要重复派单，先确认任务是否仍在执行
        """
        from core.adapters.joycode_ui import JoyCodeUIError
        self.refresh_workspace()
        if self.workspace is None:
            return self._workspace_failure()
        if not self._stale_swept:
            self._stale_swept = True
            try:
                self.sweep_stale_receipts(dry_run=False)
            except Exception:
                pass
        try:
            task = self.create_task(goal, acceptance, deadline, callback, task_id)
        except DuplicateTaskError as exc:
            return failure_fields(
                'duplicate_task', exc.message, app='JoyCode',
                existing=exc.existing, idem_key=exc.idem_key,
                workspace=dict(path=str(self.workspace), source=self.workspace_source),
                manual=('同一目标已有未终态任务 %s，本次没有写新任务文件、也没有触发 '
                        '/guga-run。等它出回执（done/failed/blocked）后才允许重派。'
                        % exc.existing))
        attempt = None
        if auto_trigger:
            try:
                attempt = self.trigger(task['task_id'])
            except JoyCodeUIError as exc:
                self._rollback_task(task['task_id'], '触发失败 %s，未提交任何内容' % exc.code)
                return failure_fields(
                    exc.code, str(exc), app='JoyCode',
                    next_action=self._manual_for(exc.code, task['task_id']),
                    task=task, detail=exc.detail,
                    manual=self._manual_for(exc.code, task['task_id']))
            except Exception as exc:
                self._rollback_task(task['task_id'], '触发异常，未提交任何内容')
                return failure_fields(
                    'trigger_failed', '%s: %s' % (type(exc).__name__, str(exc)[:200]),
                    app='JoyCode',
                    next_action=self._ui_layer().manual_hint(task['task_id']),
                    task=task, manual=self._ui_layer().manual_hint(task['task_id']))
        else:
            self.open_ide()
        # 等回执的同时隔一段时间看一眼界面（生成中/报错），回执仍然是唯一判定依据。
        receipt = None
        last_status = None
        deadline_at = time.monotonic() + max(0.0, float(timeout))
        while True:
            remaining = deadline_at - time.monotonic()
            if remaining <= 0:
                break
            receipt = self.wait_for_result(task['task_id'], timeout=min(10.0, remaining), cancel=cancel)
            if receipt:
                break
            try:
                last_status = self._ui_layer().status()
            except Exception:
                last_status = last_status or None
        if receipt is None:
            return failure_fields(
                'no_receipt',
                ('命令已发出，但 %.0f 秒内没有出现 .guga/outbox/%s.json；'
                 '任务可能仍在执行，也可能跑完后没有调用 submit_artifact'
                 % (timeout, task['task_id'])),
                app='JoyCode',
                task=task, trigger=attempt, ui_status=last_status,
                manual=('不要重复派单。先看 JoyCode 面板里这一单是否还在生成；'
                        '跑完仍未写回执时，在面板里追问“请调用 submit_artifact 写回执”，'
                        '回执应落在 .guga/outbox/%s.json。' % task['task_id']))
        return dict(ok=True, task=task, trigger=attempt, receipt=receipt,
                    ui_status=last_status, state=STATE_MAP.get(receipt.get('status'), 'paused'))

    def _manual_for(self, code, task_id):
        """按失败级别给不同的人工提示；各级不共用一套话术。"""
        if code == 'occluded':
            return ('JoyCode 面板被其他窗口挡住，本轮已中止，没有点击也没有按键。'
                    '请关掉或移开遮挡窗口后再触发 /guga-run %s。' % task_id)
        if code == 'input_not_found':
            return ('看不到 JoyCode 对话面板，已中止且没有自动按 Ctrl+Alt+B（该键会把面板按关）。'
                    '请人工确认右侧面板已打开后再触发 /guga-run %s。' % task_id)
        if code == 'busy_generating':
            return ('JoyCode 还在生成上一轮，本轮没有点击也没有输入。'
                    '等这一轮结束（界面不再显示“停止生成”）后再触发 /guga-run %s。' % task_id)
        if code == 'click_miss':
            return ('没点到 JoyCode 输入框，本轮没有输入任何内容。请人工点一下右侧面板底部的'
                    '输入框，再触发 /guga-run %s。' % task_id)
        if code == 'input_text_mismatch':
            return ('已点中输入框，但回读文本与命令不一致，本轮已中止且没有回车'
                    '（不会带着残留命令发出去）。请检查输入法/焦点后重试 /guga-run %s。' % task_id)
        if code in ('input_verify_failed', 'clipboard_conflict'):
            return ('输入框回读连续 3 次与命令不一致（或剪贴板被其他操作改写），本轮已中止且没有回车，'
                    '用户原剪贴板已恢复。请点一下右侧面板底部输入框后重试 /guga-run %s。' % task_id)
        if code == 'modal_blocked':
            return ('JoyCode 弹出了无法自动关闭的弹窗，已中止。请人工关闭弹窗后再触发 '
                    '/guga-run %s。' % task_id)
        return self._ui_layer().manual_hint(task_id)

    # ---- 收单 ----
    def poll(self, mark_handled=True):
        """扫描 outbox，返回尚未处理过的回执（按文件名排序）。"""
        results = []
        if self.outbox is None or not self.outbox.is_dir():
            return results
        for path in sorted(self.outbox.glob('*.json')):
            if path.name.endswith('.progress.json'):
                continue
            try:
                receipt = json.loads(path.read_text(encoding='utf-8-sig'))
            except (OSError, ValueError):
                continue
            if not isinstance(receipt, dict) or not receipt.get('task_id'):
                continue
            receipt['task_id'] = str(receipt['task_id'])
            if receipt.get('status') not in RECEIPT_STATUS:
                continue
            if receipt['task_id'] in self._handled:
                continue
            if mark_handled:
                with self._lock:
                    self._handled.add(receipt['task_id'])
            self._handoff(receipt)
            results.append(receipt)
        return results

    def wait_for_result(self, task_id, timeout=1800, cancel=None):
        """轮询直到该任务出现回执；超时返回 None，不抛出。"""
        deadline = time.monotonic() + max(0, float(timeout))
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                return None
            with self._lock:
                path = self.outbox / (task_id + '.json')
            if path.is_file():
                try:
                    receipt = json.loads(path.read_text(encoding='utf-8-sig'))
                except (OSError, ValueError):
                    time.sleep(self.poll_interval)
                    continue
                if isinstance(receipt, dict) and receipt.get('status') in RECEIPT_STATUS:
                    with self._lock:
                        first = receipt['task_id'] not in self._handled
                        self._handled.add(receipt['task_id'])
                    if first:
                        self._handoff(receipt)
                    return receipt
            time.sleep(self.poll_interval)
        return None

    def pending_tasks(self):
        if self.inbox is None or not self.inbox.is_dir():
            return []
        paths = sorted((p for p in self.inbox.glob('*.json') if p.is_file()),
                       key=lambda p: (p.stat().st_mtime, p.name))
        return [p.stem for p in paths if not (self.outbox / (p.stem + '.json')).exists()]

    # ---- 回送状态机 ----
    def _handoff(self, receipt):
        state = STATE_MAP.get(receipt.get('status'), 'paused')
        summary = str(receipt.get('self_check') or '')[:2000]
        artifacts = [str(item) for item in (receipt.get('artifact_paths') or [])]
        # 回执已终态：先把本进程派出的任务移出 inbox，再回送状态机。
        self._consume(receipt.get('task_id'), receipt)
        journal = self.journal
        if journal is not None and getattr(journal, 'data', {}).get('id'):
            journal.update(status=state, summary='JoyCode 回执：' + summary,
                           plan=['任务 {0} 已完成'.format(receipt.get('task_id'))
                                 if state == 'completed' else
                                 '任务 {0} 未完成，等待处理'.format(receipt.get('task_id'))],
                           artifacts=artifacts, joycode_receipt=receipt)
            journal.event('joycode', {'ok': state == 'completed'})
        if self.on_result:
            self.on_result(receipt, state)

    # ---- 小工具 ----
    @staticmethod
    def _registry_launcher():
        """注册表：App Paths 的 JoyCode.exe + 卸载项 InstallLocation/DisplayIcon。"""
        try:
            import winreg
        except Exception:
            return ''

        def value(key, name):
            try:
                return str(winreg.QueryValueEx(key, name)[0] or '').strip()
            except Exception:
                return ''

        def usable(path):
            text = str(path or '').strip().strip('"').split(',')[0].strip('"')
            return text if text and text.lower().endswith(('.exe', '.cmd', '.bat')) and Path(text).is_file() else ''

        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\JoyCode.exe',
                                        0, winreg.KEY_READ | view) as key:
                        found = usable(value(key, ''))
                        if found:
                            return found
                except Exception:
                    pass
                try:
                    root = winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
                                          0, winreg.KEY_READ | view)
                except Exception:
                    continue
                with root:
                    for index in range(1024):
                        try:
                            name = winreg.EnumKey(root, index)
                        except OSError:
                            break
                        try:
                            with winreg.OpenKey(root, name, 0, winreg.KEY_READ | view) as sub:
                                if 'joycode' not in value(sub, 'DisplayName').lower():
                                    continue
                                install = value(sub, 'InstallLocation').strip().strip('"')
                                if install:
                                    for rel in ('JoyCode.exe', 'bin/JoyCode.exe', 'bin/joycode.cmd'):
                                        candidate = Path(install) / rel
                                        if candidate.is_file():
                                            return str(candidate)
                                found = usable(value(sub, 'DisplayIcon'))
                                if found:
                                    return found
                        except Exception:
                            continue
        return ''

    @staticmethod
    def _start_menu_launcher():
        """开始菜单里叫 JoyCode 的快捷方式，解析出真实 TargetPath。"""
        bases = [Path(os.environ.get('APPDATA', '')) / r'Microsoft\Windows\Start Menu\Programs',
                 Path(os.environ.get('ProgramData', '')) / r'Microsoft\Windows\Start Menu\Programs']
        links = []
        for base in bases:
            if base.is_dir():
                links.extend(path for path in base.rglob('*.lnk') if 'joycode' in path.name.lower())
        for link in links[:4]:
            quoted = str(link).replace("'", "''")
            script = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
                      "if($s.TargetPath){$s.TargetPath}" % quoted)
            try:
                done = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                                      capture_output=True, text=True, timeout=8)
                target = (done.stdout or '').strip()
                if target and Path(target).is_file():
                    return target
            except Exception:
                continue
        return ''

    def _default_launcher(self):
        # joycode.cmd 在本机会以 ELECTRON_RUN_AS_NODE 运行 cli.js 后直接退出，
        # 实测无法拉起窗口；优先直接启动 JoyCode.exe。
        # 评委可能装在别的盘、也没设环境变量：按 环境变量 → 注册表 → 开始菜单
        # → 常见安装目录 → PATH 的顺序找，不能写死 D:\JoyCode。
        home = os.environ.get('JOYCODE_HOME', '').strip()
        if home:
            for rel in ('JoyCode.exe', r'bin\JoyCode.exe', r'bin\joycode.cmd', 'joycode.cmd'):
                candidate = Path(home) / rel
                if candidate.is_file():
                    return str(candidate)
        # 用户指定 > MCP 自报 > 注册表/开始菜单/常见目录；
        # 自报路径失效时 launch_exe 返回 ok=False，继续走下面的兜底探测。
        try:
            from core.adapters import app_registry
            found = app_registry.launch_exe('joycode')
            if found.get('ok'):
                return str(found['exe'])
        except Exception:
            pass
        for finder in (self._registry_launcher, self._start_menu_launcher):
            found = finder()
            if found:
                return found
        roots = [os.environ.get('ProgramFiles', r'C:\Program Files'),
                 os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
                 os.environ.get('LOCALAPPDATA', ''), os.environ.get('ProgramData', '')]
        roots += [letter + ':\\' for letter in 'CDEFGHIJKLMNOPQRSTUVWXYZ'
                  if Path(letter + ':\\').is_dir()]
        for root in roots:
            if not root:
                continue
            base = Path(root)
            for rel in ('JoyCode/JoyCode.exe', 'Programs/JoyCode/JoyCode.exe',
                        'Program Files/JoyCode/JoyCode.exe',
                        'Program Files (x86)/JoyCode/JoyCode.exe',
                        'JoyCode/bin/joycode.cmd'):
                candidate = base / rel
                if candidate.is_file():
                    return str(candidate)
        return shutil.which('joycode') or shutil.which('joycode.cmd') or ''

    def progress(self, task_id):
        if self.outbox is None:
            return None
        path = self.outbox / (task_id + '.progress.json')
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            return None


# 桌面端统一契约：失败码 → 一句可行动提示。与 WorkBuddy 适配器保持同一形状，
# 这样 ui/comp_window 的交付闸与失败气泡不用为 JoyCode 单开分支。
_ASK_FAIL_HINTS = {
    'input_not_found': '看不到 JoyCode 对话面板或输入框：请打开右侧面板、点一下输入框后再派。',
    'click_miss': '没点到 JoyCode 输入框：请手动点一下右侧面板底部输入框后再派。',
    'input_text_mismatch': ('文字没有进对话输入框（可能进了代码编辑器）：点对话面板底部输入框，'
                            '清空残留后用 Ctrl+V 粘贴，再确认文字出现后按回车。'),
    'input_verify_failed': ('输入框回读与命令不一致，已清空重试 3 次仍未通过校验，本次没有回车：'
                            '点一下对话面板底部输入框后重派。'),
    'clipboard_conflict': ('粘贴时剪贴板被其他操作改写，重试后仍未通过校验，本次没有提交'
                           '（用户原剪贴板已恢复）：停手后再派。'),
    'input_not_cleared': '输入框里有残留文字：先在输入框按 Ctrl+A、Backspace 清空，再派单。',
    'skill_chip_present': ('JoyCode 输入框挂着技能标签且没移除成功，本次一个字都没发送：'
                           '点掉标签后的 × 切回普通对话，再重新派单。'),
    'wrong_skill': ('JoyCode 把这次命令当成技能/生图执行了（界面出现“正在生成图片/幻灯片”），'
                    '已立即中止、不重试：点掉技能标签切回普通对话后重新派单。'),
    'not_submitted': '文字没能提交：确认输入框里真的有 /guga-run，再按一次回车。',
    'stuck_ppt_page': '界面停在旧页面：点窗口左上角第二个图标（回主页），再重新派单。',
    'no_receipt': ('命令已发出但没等到回执：先看 JoyCode 面板这一单是否还在生成；跑完仍没回执，'
                   '就在面板输入框追问“请调用 submit_artifact 写回执”，文件应落在 .guga/outbox/。'),
    'occluded': 'JoyCode 面板被其他窗口挡住：移开遮挡窗口后再派。',
    'busy_generating': 'JoyCode 还在生成上一轮：等它结束（界面不再显示“停止生成”）后再派。',
    'window_not_ready': 'JoyCode 窗口还没就绪：等它加载完再派。',
    'foreground_busy': '你正在操作电脑，我没抢前台：停手后再派一次。',
    'modal_blocked': 'JoyCode 弹出了无法自动关闭的弹窗：关掉弹窗后再派。',
    'trigger_failed': '把 /guga-run 送进 JoyCode 失败：先在 JoyCode 面板底部输入框手动发一次，确认面板可用。',
    'empty_receipt': ('回执里既没有自检结论也没有产物路径：回 JoyCode 面板让这一单调用 '
                      'submit_artifact 补写 .guga/outbox/<task_id>.json，再让咕嘎重读回执。'),
    'empty_prompt': '提示词为空，没有可派的任务。',
    'workspace_not_found': '没找到 JoyCode 当前工程，请选择：选中它现在打开的工程目录，我再派单。',
    'duplicate_task': '同一目标已有未完成的任务：等它出回执（done/failed/blocked）后再派，不重复写任务文件。',
}


def failure_fields(raw_code, raw, next_action='', app='JoyCode', **extra):
    """JoyCode 失败统一四段：真实码 / 阶段 / 原始异常 / 下一步。"""
    item = failure_payload(raw_code, raw, next_action, app)
    item.update(extra)
    return item


def _receipt_text(receipt):
    """把回执转成交付正文：自检结论 + 产物路径。没有正文就返回空串，不编内容。"""
    parts = []
    summary = str(receipt.get('self_check') or '').strip()
    if summary:
        parts.append(summary)
    paths = [str(item) for item in (receipt.get('artifact_paths') or []) if str(item).strip()]
    if paths:
        parts.append('产物：\n' + '\n'.join('- ' + item for item in paths))
    if not parts and receipt.get('status') == 'done':
        parts.append('JoyCode 回执状态 done（未写 self_check）。')
    return '\n\n'.join(parts)


class Adapter:
    """``core.adapters.dispatch`` 的统一入口：点名 JoyCode → 本机适配器 → 回执。

    包装 :class:`JoyCodeAdapter` 的 dispatch/回执逻辑，不另写一套界面操作；
    返回桌面端约定契约：
      成功 {ok, text, source{label,provenance}, evidence{dir,receipt_id,local_model_used}}
      失败 {ok:False, code, hint}
    正文只来自 .guga/outbox 的真实回执，没有模型代写退路。
    """

    name = 'JoyCode'
    tool = 'joycode'

    def __init__(self, **kwargs):
        self._options = kwargs
        self._inner = None

    def _adapter(self):
        if self._inner is None:
            self._inner = JoyCodeAdapter(**self._options)
        return self._inner

    def use_workspace(self, path):
        """界面选择工程后钉住路径，后续派单不再自动改判。"""
        return self._adapter().use_workspace(path)

    def pending_duplicate(self, goal, log_block=False):
        """同一目标是否已有未终态任务；界面在“上一个还在跑”时据此给明确说明。"""
        inner = self._adapter()
        inner.refresh_workspace()
        return inner.pending_duplicate(goal, log_block=log_block)

    def _workspace_failure(self, inner):
        return failure_fields(
            'workspace_not_found', 'refresh_workspace 没有找到 JoyCode 当前打开的工程目录',
            app=self.name,
            hint='没找到 JoyCode 当前工程，请选择：选中它现在打开的工程目录，我再派单。',
            candidates=inner.workspace_candidates or inner.workspace_info.get('candidates') or [],
            default=(inner.workspace_info.get('default_workspace') or _last_workspace()),
            workspace=dict(source=inner.workspace_source, path=''),
            evidence=dict(dir='', origin='joycode_workspace_outbox', receipt_id='',
                          local_model_used=False, workspace_source=inner.workspace_source),
            message='没找到 JoyCode 当前工程，请选择')

    def ask(self, prompt, timeout_seconds=None, retry=1, prefer_mcp=False, **_kw):
        prompt = str(prompt or '').strip()
        if not prompt:
            return failure_fields(
                'empty_prompt', '适配器收到空 prompt（没有可提交的任务文本）',
                next_action=_ASK_FAIL_HINTS['empty_prompt'], app=self.name,
                hint=_ASK_FAIL_HINTS['empty_prompt'])
        timeout = max(180, int(float(timeout_seconds or 0) or 1800))
        inner = self._adapter()
        inner.refresh_workspace()
        if not inner.launcher:
            return failure_fields(
                'launcher_not_found',
                '本机没有找到 JoyCode 启动器（注册表、开始菜单、PATH、GUAGA_JOYCODE_LAUNCHER 都没命中）',
                next_action=('设置环境变量 GUAGA_JOYCODE_LAUNCHER 指向 JoyCode.exe 完整路径'
                             '（PowerShell：setx GUAGA_JOYCODE_LAUNCHER "E:\\JoyCode\\JoyCode.exe"），'
                             '重开咕嘎后再派。'),
                app=self.name,
                hint=('我把 JoyCode 装在别的盘了：请先设环境变量 GUAGA_JOYCODE_LAUNCHER 指向 '
                      'JoyCode.exe 的完整路径（PowerShell：setx GUAGA_JOYCODE_LAUNCHER '
                      '"E:\\JoyCode\\JoyCode.exe"），重开咕嘎后再派。'),
                workspace='', workspace_source='launcher_not_found',
                evidence=dict(dir=str(inner.outbox), origin='joycode_workspace_outbox',
                              receipt_id='', local_model_used=False),
                message='没找到 JoyCode 启动器：我把 JoyCode 装在别的盘了？')
        if inner.workspace is None:
            return self._workspace_failure(inner)
        result = inner.run_task(prompt, '按需求完成，并在回执 self_check 里写清结果与产物路径',
                                timeout=timeout, auto_trigger=True)
        if not result.get('ok'):
            code = str(result.get('raw_code') or result.get('error')
                       or result.get('code') or 'trigger_failed')
            if code in ('input_not_found', 'click_miss'):
                raw_code = 'input_not_found'
            else:
                raw_code = code
            if code == 'foreground_busy' and retry:
                time.sleep(3.0)
                result = inner.run_task(prompt, '按需求完成，并在回执 self_check 里写清结果与产物路径',
                                        timeout=timeout, auto_trigger=True)
                raw_code = str(result.get('raw_code') or result.get('error')
                               or result.get('code') or 'trigger_failed')
                if raw_code in ('input_not_found', 'click_miss'):
                    raw_code = 'input_not_found'
            hint = _ASK_FAIL_HINTS.get(raw_code) or str(result.get('manual') or '')
            if not hint:
                hint = 'JoyCode 没完成这次任务，没有真实回执。'
            message = str(result.get('message') or '').strip()
            task = result.get('task') or {}
            raw = message or str(result.get('raw') or '') or ('JoyCode 返回 %s' % raw_code)
            return failure_fields(
                raw_code, raw, next_action=hint, app=self.name,
                hint=hint,
                workspace=str(inner.workspace),
                workspace_source=inner.workspace_source,
                evidence=dict(dir=str(inner.outbox),
                              origin='joycode_workspace_outbox',
                              receipt_id=str(task.get('task_id') or ''),
                              local_model_used=False,
                              raw_error=str(result.get('error') or ''),
                              raw_code=raw_code,
                              workspace=str(inner.workspace),
                              workspace_source=inner.workspace_source,
                              ui_status=result.get('ui_status')),
                message=message or hint)
        receipt = result.get('receipt') or {}
        task = result.get('task') or {}
        task_id = str(receipt.get('task_id') or task.get('task_id') or '')
        text = _receipt_text(receipt)
        if not text:
            return failure_fields(
                'empty_receipt',
                '回执 %s 里没有 self_check 也没有 artifact_paths，不能算交付'
                % (task_id or '<未知任务>'),
                next_action=_ASK_FAIL_HINTS['empty_receipt'], app=self.name,
                hint=_ASK_FAIL_HINTS['empty_receipt'],
                evidence=dict(dir=str(inner.outbox), receipt_id=task_id,
                              origin='joycode_workspace_outbox', local_model_used=False),
                message='回执没有 self_check 或 artifact_paths')
        artifacts = [str(item) for item in (receipt.get('artifact_paths') or [])]
        return dict(
            ok=True,
            text=text,
            notice=('未走接口：JoyCode 没有可 headless 调用的 Agent API，'
                    '本次是写入任务文件并在它的窗口注入 /guga-run，正文来自工作区 outbox 回执。'),
            source=dict(tool='joycode', name='JoyCode', channel='file_protocol+ui_injection',
                        label='JoyCode（任务文件 + 界面注入，非接口调用）',
                        provenance='joycode_receipt:%s' % task_id,
                        call_id=task_id),
            evidence=dict(dir=str(inner.outbox), receipt_id=task_id,
                          origin='joycode_workspace_outbox', local_model_used=False,
                          executor='joycode_agent', status=receipt.get('status'),
                          workspace=str(inner.workspace),
                          workspace_source=inner.workspace_source,
                          artifact_paths=artifacts),
            workspace=str(inner.workspace),
            workspace_source=inner.workspace_source,
            receipt=receipt,
        )
