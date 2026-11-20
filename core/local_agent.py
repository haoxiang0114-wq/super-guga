# -*- coding: utf-8 -*-
"""桌宠内嵌本机执行方 — 合一架构的最后一块

合一前: 云端派单 → 独立值守程序 GUGA-Agent.exe 领单 → 指挥本机 AI 软件
合一后: 云端派单 → 桌宠进程内的 LocalAgent 直接领单 → 同一套执行器干活
        （桌宠既是主人端, 又是执行端; 独立值守程序不再是必需品）

自动配对原理（用户全程无感）:
  桌宠已持有用户 token(ButlerCloud 登录来的) → 调 /api/pair/create 生成 6 位配对码
  → 立刻用该码走 /api/agent/pair 换取执行方 token → 桌宠以执行方身份心跳领单
  （等价于网页生成配对码 + 值守程序手动输入, 只是把两步缩进同一进程）

执行逻辑零复制: 直接复用 agent/guga_agent.py 的 run_job（其内部 import
guga_executor 的视觉定位/窗口管理/取证全套能力）, 本模块只做三件事:
  ① 把 BASE 指到云端大脑  ② 自动配对拿执行方 token  ③ 心跳领单 + 线程派活

安全边界不变: FAILSAFE（鼠标甩左上角急停）、剪贴板还原、新对话隔离、
执行全程截图取证回传证据墙。
"""
import os
import sys
import threading

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

try:
    from core import input_guard
except Exception:                   # 独立运行/裁剪包时保持原行为
    input_guard = None

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
AGENT_DIR = os.path.join(REPO, 'agent')


class LocalAgent(QObject):
    """桌宠内置执行方. 用法:
        agent = LocalAgent(cloud, self)   # cloud: core.butler_cloud.ButlerCloud(已登录)
        agent.paired.connect(...)         # 配对成功(设备名)
        agent.job_started.connect(...)    # 领到单(子任务名/目标软件)
        agent.job_finished.connect(...)   # 执行结束(ok, 摘要)
        agent.start()                     # 自动配对并开始值守
    """

    paired = pyqtSignal(str)
    pair_fail = pyqtSignal(str)
    job_started = pyqtSignal(dict)        # {name, app, attempt, goal, taskId, stId}
    job_finished = pyqtSignal(dict, bool, str)  # (job, ok, 摘要) —— 带原单，界面才能回写台账
    job_paused = pyqtSignal(str)          # 叫停：不按完成/失败收尾，等用户说「继续」
    job_skipped = pyqtSignal(str)         # 重复派单被本机台账拦下（已终止/已完成）
    announce = pyqtSignal(str)

    def __init__(self, cloud, parent=None):
        super().__init__(parent)
        self._cloud = cloud
        self._token = ''
        self._device = ''
        self._busy = False
        self._timer = None
        self._ga = None        # guga_agent 模块（首次 start 时注入 path 再 import）
        self._import_err = ''
        from core.task_ledger import TaskLedger
        self._ledger = TaskLedger()

    # ---------- 状态 ----------

    @property
    def active(self):
        return bool(self._token)

    @property
    def busy(self):
        return self._busy

    # ---------- 启动: 注入路径 + 自动配对 ----------

    def start(self):
        if self._token or self._busy:
            return
        threading.Thread(target=self._bootstrap, daemon=True).start()

    def _import_agent(self):
        """把 agent/ 目录加进 path, 复用现成执行器（一次性）."""
        if self._ga is not None:
            return True
        try:
            if AGENT_DIR not in sys.path:
                sys.path.insert(0, AGENT_DIR)
            # 执行器不再内置云端地址；由本机 cloud 配置显式下发（只对已购买账号生效）。
            if self._cloud.base:
                os.environ['GUGA_CLOUD'] = self._cloud.base
            import guga_agent as ga
            self._ga = ga
            self._ga.BASE = self._cloud.base    # 执行器模块级 API 地址 → 云端大脑
            return True
        except Exception as e:
            self._import_err = str(e)
            return False

    def _bootstrap(self):
        if not self._import_agent():
            self.pair_fail.emit('执行器缺失: %s' % self._import_err[:80])
            return
        try:
            # ① 用户身份生成配对码（cloud._call 自带 Bearer 用户 token）
            r = self._cloud._call('/api/pair/create', {})
            code = str(r.get('code', ''))
            if not code.isdigit():
                self.pair_fail.emit('配对码生成失败')
                return
            # ② 立刻用配对码换执行方 token（guga_agent.pair 内部会跑本机自检并上传）
            token = self._ga.pair(code)
            self._token = token
            self._device = os.environ.get('COMPUTERNAME', 'PC')
            self.paired.emit(self._device)
            self._attach_inventory_report()
            # ③ 启动心跳领单（QTimer 在主线程, 回调里起线程, 绝不卡 UI）
            QTimer.singleShot(0, self._start_beat)
        except Exception as e:
            self.pair_fail.emit(str(e)[:120])

    # ---------- 本机清单上报 ----------

    def _attach_inventory_report(self):
        """配对成功后挂上清单上报：启动即报一次，之后只在清单变化时才报。

        全程后台线程 + 静默失败——未登录不上报、断网不影响心跳和 UI。
        """
        try:
            from core import local_tools
            local_tools.set_change_reporter(self._report_inventory)
            # 启动必报一次（不看上次的缓存签名）；之后清单变化时才报。
            threading.Thread(target=self._report_inventory, args=(None, True),
                             daemon=True).start()
        except Exception:
            pass

    def _report_inventory(self, _payload=None, startup=False):
        try:
            from core import local_tools
            return local_tools.report_agent(self._token, self._cloud.base,
                                            only_if_changed=not startup)
        except Exception:
            return None

    # ---------- 心跳领单 ----------

    def _start_beat(self):
        if self._timer is not None:
            return
        self._timer = QTimer(self)
        self._timer.setInterval(4000)          # 与独立值守程序同拍
        self._timer.timeout.connect(self._beat)
        self._timer.start()
        self._beat()

    def _beat(self):
        if not self._token or self._ga is None:
            return
        if self._busy:
            threading.Thread(target=self._beat_keepalive, daemon=True).start()
            return
        threading.Thread(target=self._beat_poll, daemon=True).start()

    def _beat_keepalive(self):
        """干活期间的保活心跳: 只报平安不领新单（防误判离线, 也防一机双单）."""
        try:
            self._ga.api('/api/agent/beat',
                         {'path': AGENT_DIR, 'busy': True}, self._token, timeout=15)
        except Exception:
            pass

    def _beat_poll(self):
        try:
            b = self._ga.api('/api/agent/beat',
                             {'path': AGENT_DIR}, self._token, timeout=20)
            job = b.get('job')
        except Exception:
            return                              # 心跳暂时不通: 下一拍再试, 不打扰桌宠
        if not job or self._busy:
            return
        try:
            # 幂等闸门：同一 (taskId, stId, attempt) 已经跑过就不再跑第二遍。
            # 云端若因回执丢失/验收延迟重复派同一单，这里直接回执并让界面显示「已终止」。
            status = self._ledger.claim(job)
            if status:
                word = {'done': '这份子任务已经完成', 'failed': '这份子任务上次已失败',
                        'running': '这份子任务正在执行中', 'paused': '这份子任务已暂停待续'}[status]
                note = '%s，重复派单已忽略（未再执行）' % word
                self._ledger.mark_skipped(job, note)
                self.job_skipped.emit(note)
                threading.Thread(target=self._ack_duplicate,
                                 args=(job, status, note), daemon=True).start()
                return
            self._ledger.start(job)
        except Exception as exc:
            # 领单/记账出问题必须让人看见，不能让心跳线程静默死掉
            self.announce.emit('领单失败：%s' % str(exc)[:120])
            return
        self._busy = True
        self.job_started.emit({'name': job.get('name', ''),
                               'app': job.get('app', '豆包'),
                               'attempt': job.get('attempt', 1),
                               'taskId': job.get('taskId', ''),
                               'stId': job.get('stId', ''),
                               'goal': job.get('goal', '')})
        threading.Thread(target=self._run_job, args=(job,), daemon=True).start()

    def _ack_duplicate(self, job, status, note):
        """把「已经做过」如实回执，避免云端继续把同一单发回来。"""
        try:
            self._ga.api('/api/agent/result',
                         {'taskId': job.get('taskId'), 'stId': job.get('stId'),
                          'ok': status == 'done', 'note': note[:200],
                          'duplicate': True}, self._token)
        except Exception:
            pass
        if self._ledger.error:
            self.announce.emit(self._ledger.error)

    # ---------- 执行（复用 guga_agent.run_job 全套） ----------

    def _run_job(self, job):
        ok, note, paused = False, '', False
        listener = None
        if input_guard is not None:
            def report(info):
                # 台账同步：暂停不是完成也不是失败，先落成 paused，等用户说「继续」再自然续跑
                try:
                    self._ga.api('/api/agent/paused',
                                 {'taskId': job['taskId'], 'stId': job['stId'],
                                  'ctx': ('执行器在动作前停手：%s' % (info.get('reason') or '用户叫停'))[:400]},
                                 self._token, timeout=15)
                except Exception:
                    pass
            listener = report
            input_guard.set_listeners(pause=report, resume=None)
        try:
            # run_job 内部: 指挥本机 AI 软件 → 每轮请示大脑 → 回传 → 网页验收
            # 逐轮回调写台账（第几步做到哪了），返回值给出这一单的真实结果
            res = self._ga.run_job(job, self._token,
                                   on_step=lambda i, t: self._ledger.step(job, t, i))
            ok = bool((res or {}).get('ok'))
            note = str((res or {}).get('note') or '')
            if ok and not note:
                note = '子任务「%s」执行完毕，云端验收中' % job.get('name', '')
            if not ok and not note:
                note = '子任务「%s」未能交付（未拿到产物）' % job.get('name', '')
            self._ledger.finish(job, 'done' if ok else 'failed', note)
        except Exception as e:
            if input_guard is not None and isinstance(e, (input_guard.Paused, input_guard.Stopped)):
                paused = isinstance(e, input_guard.Paused)
                note = str(e) or ('已暂停：用户叫停，控制权已还给用户' if paused
                                  else '已中止：用户终止了本轮操作')
                self._ledger.finish(job, 'paused' if paused else 'failed', note)
            else:
                note = '执行异常: %s' % str(e)[:120]
                self._ledger.finish(job, 'failed', note)
                try:
                    self._ga.api('/api/agent/result',
                                 {'taskId': job['taskId'], 'stId': job['stId'],
                                  'ok': False, 'note': note[:200]}, self._token)
                except Exception:
                    pass
        finally:
            if input_guard is not None and listener is not None:
                input_guard.set_listeners(None, None)
            self._busy = False
            if self._ledger.error:
                self.announce.emit(self._ledger.error)   # 台账写不进去必须说出来，不许静默
            if paused:
                self.job_paused.emit(note)      # 台账停在已暂停，不记完成也不记失败
            else:
                self.job_finished.emit(dict(job), ok, note)

    # ---------- 停止 ----------

    def stop(self):
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._token = ''
        try:
            from core import local_tools
            local_tools.set_change_reporter(None)
        except Exception:
            pass
