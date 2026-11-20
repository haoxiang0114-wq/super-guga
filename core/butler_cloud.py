# -*- coding: utf-8 -*-
"""管家云端大脑客户端 — 桌宠 ⇆ 云端监工服务（合一架构的"主人端"通道）

桌宠合一后的分工:
  - 拆解/调度/验收裁决: 云端大脑（需要 LLM，API Key 只放服务器）
  - 台账/设备/证据墙:   云端存储（同账号跨设备: 台式机派的活, 笔记本同账号可见）
  - 真实执行:           永远在桌宠所在的本机（executor:'local' 派给本机执行方）

用法(CompWindow):
    self._cloud = ButlerCloud(self)
    self._cloud.login_ok.connect(...)        # 登录成功(用户名)
    self._cloud.state_updated.connect(...)   # 台账刷新(任务列表)
    self._cloud.announce.connect(...)        # 需要语音播报的话
    self._cloud.start()                      # 读 ai_config.json 的 cloud 段自动登录

配置(ai_config.json 的 cloud 段, 见 ai_config.example.json):
    "cloud": {
        "base_url": "https://<你的云端服务地址>",
        "username": "<购买云端账号后拿到的用户名>",
        "password": "<你自己的密码>",
        "device":   "书房台式机",
        "poll_secs": 6
    }
没有 cloud 段或缺少任一项时，云端能力不可用；本地功能不受影响。
"""
import json
import re
import threading
import urllib.error
import urllib.request

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from core import guga_paths

# ---------- 意图路由: 哪些话应该走"派单真机执行"而不是普通问答 ----------
# 与云端 server.js 的 LOCAL_APPS 词表对齐(桌宠本地一份, 离线也能判):
# 用户话里出现目标软件 = 要咕嘎指挥那个软件干活, 绝不能让咕嘎自己的 API 代写冒充.
_DEVICE_APPS_RE = re.compile(
    r'豆包|doubao|deepseek|deep seek|记事本|notepad|计算器|'
    r'浏览器|chrome|edge|wps|word|excel|ppt|剪映|飞书|钉钉|腾讯会议|workbuddy|'
    r'wrokbuddy|wrorkbuddy|work\s*buddy|画图|修图',
    re.I)
# 询问/比较类保护: 提到软件名但只是问知识, 不该派单
_ASK_ONLY_RE = re.compile(r'哪个好|哪个强|区别|是什么|是什么公司|怎么样|比一比|谁更好|推荐')


def looks_like_device_task(text):
    """对话意图路由: 文本提到目标软件 → 应走派单真机执行(宁可误路由, 不可漏路由:
    误路由会被云端拆解/验收正常处理, 漏路由则变成咕嘎自己代写——那是演剧本)."""
    if not text or len(text) < 4:
        return False
    if _ASK_ONLY_RE.search(text):
        return False
    return bool(_DEVICE_APPS_RE.search(text))

# 子任务状态 → 中文播报词（服务端口径是 error，不是 failed；failed 兼容旧数据）
ST_WORD = {
    'pending': '排队中', 'running': '进行中', 'review': '待验收',
    'done': '已完成', 'failed': '失败', 'error': '失败',
    'returned': '打回返工', 'paused': '已暂停', 'aborted': '已中止',
}

_FAIL_STATES = ('error', 'failed')


def is_failed(sub):
    """子任务是否失败：服务端用 error，历史数据可能是 failed，两者都认。"""
    return str((sub or {}).get('status') or '') in _FAIL_STATES


def is_aborted(sub):
    """是否用户中止：服务端 accepted=aborted，任务上可能带 aborted 标记或中止类备注。"""
    sub = sub or {}
    if sub.get('aborted'):
        return True
    note = str(sub.get('note') or sub.get('checkResult') or sub.get('feedback') or '')
    return note.startswith(('已中止', '用户终止', 'Stopped', '中止'))


def load_cloud_cfg():
    """读 ai_config.json 的 cloud 段; 未配置返回 None(桌宠照常跑, 只是没有云端能力)."""
    try:
        cfg = guga_paths.read_config()
        c = cfg.get('cloud') or {}
        if c.get('base_url') and c.get('username') and c.get('password'):
            c.setdefault('poll_secs', 6)
            c.setdefault('device', '桌宠本机')
            return c
    except Exception:
        pass
    return None


def task_overall(task):
    """把一条任务的子任务状态聚合成一个总状态（与云端语义一致）."""
    subs = task.get('subtasks') or []
    sts = [s.get('status', 'pending') for s in subs]
    if not sts:
        return 'pending'
    if any(s in _FAIL_STATES or s == 'aborted' for s in sts):
        return 'error'
    if all(s == 'done' for s in sts):
        return 'done'
    if any(s in ('review', 'returned') for s in sts):
        return 'review'
    if any(s == 'running' for s in sts):
        return 'running'
    return 'pending'


# 跨设备派单被服务端拒绝时的三种业务码 → 用户能听懂的说明
_DISPATCH_ERRORS = {
    'DEVICE_OFFLINE': '「{device}」那台电脑的咕嘎本体没在线——换一台在线设备，'
                      '或者去那台电脑把咕嘎重新打开。',
    'NO_INVENTORY': '「{device}」还没上报软件清单，我没法确认它装了哪些 AI 软件——'
                    '在那台电脑启动一次咕嘎就会自动上报。',
    'TOOL_MISSING': '「{device}」上没有 {missing}——换一台设备，或先在那边装好再派单。',
    # 买软件 ≠ 买云端账号：云端服务未开通/额度用尽时必须明说，不能悄悄用运营 Key 兜底。
    'CLOUD_NOT_ENTITLED': '云端服务未开通：当前账号没有购买云端大脑服务。'
                          '派单、跨设备台账需要单独购买云端账号；本地对话请在「模型设置」填自己的 Key。',
    'CLOUD_QUOTA_EXCEEDED': '云端额度已用完：请购买/续费云端服务，或等额度重置后再派单。',
    'ADMIN_ONLY': '该操作仅管理员可用：当前账号没有权限。请用管理员账号登录，'
                  '或让管理员在云端开通后再试。',
    'BASEURL_REQUIRES_NEW_KEY': '服务商地址变了，旧 Key 不再被接受：请打开「模型设置」，'
                                '按新的服务商地址重新填写 Key 后再试。',
}

_ENTITLEMENT_CODES = ('CLOUD_NOT_ENTITLED', 'CLOUD_QUOTA_EXCEEDED',
                      'ADMIN_ONLY', 'BASEURL_REQUIRES_NEW_KEY')


def cloud_entitlement_error(code):
    """服务端业务码 → 可展示文案；不是权限/额度类返回空串。"""
    return _DISPATCH_ERRORS.get(str(code or ''), '') \
        if str(code or '') in _ENTITLEMENT_CODES else ''


def _tool_matches(item, wanted):
    fields = [item.get('name'), item.get('launch')] + list(item.get('aliases') or [])
    value = str(wanted or '').casefold()
    return any(value in str(field).casefold() for field in fields if field)


def _devices_with_tools(inventory, tools):
    """从设备清单里找装有全部所需软件的在线设备（TOOL_MISSING 的“哪台有”）。"""
    names = []
    for item in inventory or []:
        if not item.get('online') or not item.get('tools'):
            continue
        available = item['tools']
        if all(any(_tool_matches(tool, wanted) for tool in available) for wanted in tools):
            name = str(item.get('device') or item.get('id') or '').strip()
            if name:
                names.append(name)
    return names


def dispatch_error_text(exc, device='', inventory=None):
    """把服务端的业务错误码翻译成人话，而不是把 HTTP 400 原样抛给用户。"""
    detail = {}
    if isinstance(exc, urllib.error.HTTPError):
        try:
            detail = json.loads(exc.read().decode('utf-8') or '{}')
        except (ValueError, OSError):
            detail = {}
    code = str(detail.get('code') or '')
    name = str(detail.get('deviceName') or detail.get('deviceId') or device or '目标设备')
    missing = detail.get('missing') or []
    if isinstance(missing, str):
        missing = [missing]
    if code in _DISPATCH_ERRORS:
        text = _DISPATCH_ERRORS[code].format(
            device=name, missing='、'.join(str(x) for x in missing) or '所需软件')
        if code == 'TOOL_MISSING':
            alternatives = _devices_with_tools(inventory, [str(x) for x in missing])
            if alternatives:
                text += ' 现在有这些软件、可以换到：%s。' % '、'.join(alternatives[:3])
            else:
                text += ' 账号下暂时没有别的在线设备同时装有这些软件。'
        return text
    message = detail.get('error') or detail.get('message')
    if message:
        return str(message)
    return '派单失败（HTTP %s）' % (getattr(exc, 'code', '?'))


def task_progress(task):
    """完成度 0~1（done 子任务占比）."""
    subs = task.get('subtasks') or []
    if not subs:
        return 0.0
    return sum(1 for s in subs if s.get('status') == 'done') / float(len(subs))


class _OnceCall(threading.Thread):
    """一次性后台调用（登录/派单）: 不卡 UI 线程, 结果走回调."""

    def __init__(self, fn, on_ok, on_fail):
        super().__init__(daemon=True)
        self._fn, self._ok, self._fail = fn, on_ok, on_fail

    def run(self):
        try:
            result = self._fn()
            self._ok(result)
        except Exception as e:
            self._fail(str(e) or e.__class__.__name__)


class ButlerCloud(QObject):
    """桌宠的云端大脑通道. 一个实例管: 登录、派单、台账轮询、播报去重."""

    login_ok = pyqtSignal(str)          # 用户名
    login_fail = pyqtSignal(str)        # 错误摘要
    dispatch_ok = pyqtSignal(dict)      # 新任务对象
    dispatch_fail = pyqtSignal(str)
    state_updated = pyqtSignal(list)    # 最新任务列表(最多30条)
    poll_error = pyqtSignal(str)        # 轮询异常(断线等, 面板提示用, 不打断桌宠)
    _login_result = pyqtSignal(dict)    # 登录结果回主线程：QTimer 必须在主线程创建
    announce = pyqtSignal(str)          # 需要 TTS 播报的状态变化

    def __init__(self, parent=None):
        super().__init__(parent)
        cfg = load_cloud_cfg()
        self._cfg = cfg
        self.base = str((cfg or {}).get('base_url') or '').rstrip('/')
        self.token = ''
        self.user = ''
        self._poll = None       # QTimer
        self._last_sig = {}     # task_id -> 子任务状态摘要(播报去重)
        self._net_fail = 0      # 连续失败计数(只报一次, 恢复时报一次)
        self._inventory = []    # 最近一次设备清单(TOOL_MISSING 时回答“哪台有”)
        # 登录结果在后台线程拿到，但 _start_poll 里要建 QTimer：
        # QTimer 只能在主线程创建，否则定时器不触发（2026-11-07 实测：
        # /api/state 只在登录那一刻请求两次，之后台账再也不刷新，
        # 失败/中止播报自然永远不触发）。AutoConnection 会排队回主线程。
        self._login_result.connect(self._on_login)

    # ---------- 状态 ----------

    @property
    def enabled(self):
        return self._cfg is not None

    @property
    def logged_in(self):
        return bool(self.token)

    # ---------- 生命周期 ----------

    def start(self):
        """配置存在才启用: 自动登录 + 启动台账轮询. 未配置时静默跳过."""
        if not self.enabled:
            return False
        self.login(self._cfg['username'], self._cfg['password'],
                   self._cfg.get('device', '桌宠本机'))
        return True

    def stop(self):
        if self._poll is not None:
            self._poll.stop()
            self._poll = None

    def reload(self):
        """云端账号保存后重新读取 cloud 段并重新登录；清空配置则停用。

        菜单入口是常驻的，用户可能在启动后才第一次填账号；不重载的话
        `enabled` 会一直停留在启动时的 False，派单/台账入口就永远不出现。
        """
        self.stop()
        cfg = load_cloud_cfg()
        self._cfg = cfg
        self.base = str((cfg or {}).get('base_url') or '').rstrip('/')
        self.token = ''
        self.user = ''
        self._last_sig = {}
        self._net_fail = 0
        if not cfg:
            return False
        self.login(cfg['username'], cfg['password'], cfg.get('device', '桌宠本机'))
        return True

    # ---------- 底层 HTTP ----------

    def _call(self, path, data=None, timeout=25):
        req = urllib.request.Request(self.base + path,
                                     method='POST' if data is not None else 'GET')
        req.add_header('Content-Type', 'application/json')
        if self.token:
            req.add_header('Authorization', 'Bearer ' + self.token)
        body = json.dumps(data).encode('utf-8') if data is not None else None
        try:
            if data is None:
                with urllib.request.urlopen(req, body, timeout=timeout) as r:
                    return json.loads(r.read().decode('utf-8'))
            # 云端 JSON POST 统一保留 POST 重定向，避免 http→https/补斜杠跳转后变 GET 回 405。
            from core import model_endpoint
            with model_endpoint.urlopen_post(req, body, timeout=timeout) as r:
                return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            # 服务端业务码（含云端未开通/额度用尽）翻译成人话，不把运营凭据兜底给本地。
            raise RuntimeError(dispatch_error_text(exc)) from exc

    # ---------- 登录 ----------

    def login(self, username, password, device='桌宠本机'):
        def job():
            return self._call('/api/login',
                              {'username': username, 'password': password,
                               'device': device[:40]})
        _OnceCall(job, self._login_result.emit, self.login_fail.emit).start()

    def _on_login(self, result):
        self.token = result.get('token', '')
        self.user = result.get('username', '')
        self.login_ok.emit(self.user)
        self._start_poll()

    def _start_poll(self):
        if self._poll is not None:
            return
        secs = int(self._cfg.get('poll_secs', 6))
        self._poll = QTimer(self)
        self._poll.setInterval(max(4, secs) * 1000)
        self._poll.timeout.connect(self.refresh_state)
        self._poll.start()
        self.refresh_state()

    # ---------- 派单 ----------

    def dispatch(self, goal, space='', prefer_local=True, device='', required_tools=None):
        """说目标 → 云端拆解派单.

        prefer_local: 涉及本机软件时优先派给本机执行方。
        device: 明确指定另一台设备时填设备名/ID；此时服务端会校验在线、清单与所需软件。
        required_tools: 目标设备必须装有的软件名（最多 10 个）；指定 device 时自动推导。
        """
        if not self.logged_in:
            self.dispatch_fail.emit('还没连上云端大脑，请先登录')
            return
        payload = {'goal': goal[:500], 'space': space}
        if prefer_local:
            payload['executor'] = 'local'
        if device:
            payload['device'] = str(device)[:60]
            tools = [str(t).strip()[:40] for t in (required_tools or []) if str(t).strip()][:10]
            if not tools:
                try:
                    from core.tool_router import required_tools as derive_tools
                    tools = derive_tools(goal)
                except Exception:
                    tools = []
            if tools:
                payload['requiredTools'] = tools[:10]
        _OnceCall(lambda: self._dispatch_call(payload, device),
                  self._on_dispatch, self.dispatch_fail.emit).start()

    def _dispatch_call(self, payload, device=''):
        try:
            return self._call('/api/tasks', payload)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(dispatch_error_text(exc, device, self._inventory))

    # ---------- 跨设备 ----------

    def devices_inventory(self):
        """拉本账号的设备清单（设备名/在线/已装工具/是否上报清单）.

        调用方应放在后台线程里执行，结果回主线程再弹选择框。
        """
        if not self.logged_in:
            raise RuntimeError('还没连上云端大脑，请先登录')
        result = self._call('/api/devices/inventory')
        devices = result.get('devices') if isinstance(result, dict) else []
        devices = devices if isinstance(devices, list) else []
        self._inventory = devices
        return {'devices': devices,
                'total': len(devices),
                'online': sum(1 for d in devices if d.get('online')),
                'withInventory': sum(1 for d in devices if d.get('hasInventory'))}

    def _on_dispatch(self, result):
        task = result.get('task') or {}
        self._remember_sig(task)
        self.dispatch_ok.emit(task)

    # ---------- 台账轮询 ----------

    def refresh_state(self):
        if not self.logged_in:
            return
        _OnceCall(lambda: self._call('/api/state'),
                  self._on_state, self._on_poll_fail).start()

    def _on_poll_fail(self, err):
        self._net_fail += 1
        if self._net_fail == 1:
            self.poll_error.emit(err)

    def _on_state(self, result):
        if self._net_fail:
            self._net_fail = 0
            self.announce.emit('少爷，云端大脑连上了。')
        tasks = result.get('tasks') or []
        self.state_updated.emit(tasks)
        self._diff_announce(tasks)

    # ---------- 状态变化 → 语音播报（去重: 只播新增变化） ----------

    @staticmethod
    def _sig(task):
        return tuple((s.get('id'), s.get('status')) for s in (task.get('subtasks') or []))

    def _remember_sig(self, task):
        self._last_sig[task.get('id')] = self._sig(task)

    def _diff_announce(self, tasks):
        """台账刷新后对比签名, 有变化才开口; 一轮最多播两条, 避免刷屏."""
        lines = []
        for task in tasks:
            tid = task.get('id')
            sig = self._sig(task)
            old = self._last_sig.get(tid)
            if old is None:
                self._remember_sig(task)      # 首次见到的历史任务不开口
                continue
            if sig == old:
                continue
            self._remember_sig(task)
            line = self._describe_change(task, old)
            if line:
                lines.append(line)
            if len(lines) >= 2:
                break
        for line in lines:
            self.announce.emit(line)

    def _describe_change(self, task, old_sig):
        subs = task.get('subtasks') or []
        old_map = {sid: st for sid, st in old_sig}
        # 找"刚变 done"的子任务优先播, 其次是打回/异常
        for s in subs:
            sid, st = s.get('id'), s.get('status')
            if st == 'done' and old_map.get(sid) not in (None, 'done'):
                return ('少爷，「%s」验收通过了。' % s.get('name', '子任务')
                        if task_overall(task) != 'done'
                        else '少爷，「%s」全部完成——%d 个子任务都验收通过。'
                             % (task.get('goal', '任务'), len(subs)))
        for s in subs:
            sid, st = s.get('id'), s.get('status')
            if (st in _FAIL_STATES or st == 'aborted') and old_map.get(sid) != st:
                name = s.get('name', '子任务')
                why = str(s.get('note') or s.get('checkResult') or '').strip()
                for prefix in ('已中止：', '已中止:', '执行失败：', '执行失败:', '失败：', '失败:'):
                    if why.startswith(prefix):
                        why = why[len(prefix):]
                if st == 'aborted' or is_aborted(s):
                    return '少爷，「%s」已中止：%s。' % (name, (why or '用户终止了本轮操作')[:40])
                return '少爷，「%s」执行失败：%s。' % (name, (why or '三次交付仍不达标')[:40])
        for s in subs:
            sid, st = s.get('id'), s.get('status')
            if st in ('review', 'returned') and old_map.get(sid) != st:
                return '少爷，「%s」交付了，我正在按验收标准核对。' % s.get('name', '子任务')
        # 兜底: 总状态变了才播
        before = [old_map.get(s.get('id')) for s in subs]
        after = [s.get('status') for s in subs]
        if sorted(filter(None, before)) != sorted(filter(None, after)):
            return '少爷，「%s」有新进展，%s。' % (
                task.get('goal', '任务')[:24],
                '、'.join(sorted({ST_WORD.get(st, st) for st in after})))
        return ''
