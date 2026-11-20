"""Read-only task facts from the independent native DSH host, isolated by session."""
import threading
import time
from collections import OrderedDict
from PyQt5.QtCore import QObject,QTimer


_ACTION_WORDS = (
    ('mcp__guga__applications', '帮你打开要用的软件'),
    ('mcp__guga__computer', '在电脑上帮你操作画面'),
    ('mcp__guga__pet_say', '跟你同步消息'),
    ('mcp__guga__pet_control', '摆弄一下我的小动作'),
    ('mcp__guga__app_state', '检查我这边的情况'),
    ('mcp__guga__wechat', '帮你发微信'),
    ('mcp__guga__daily_plan', '帮你整理每日计划'),
    ('mcp__guga__workspace', '帮你整理项目和会话'),
    ('mcp__guga__voice_session', '准备语音通话'),
    ('mcp__guga__screen_share', '帮你看着屏幕'),
)
_OPENERS = ('主人～', '跟你汇报下哈～', '进度来啦～', '我跟你讲哦～')
# 主人 2026-11-11 要求：聚焦 DSH 正在跑的那个任务，每隔 1 分钟用自然语言汇报一次。
# 节奏是固定的，不再由「里程碑工具」触发——否则要么太吵，要么一分钟都没一句。
_NARRATE_SECONDS = 60
# 模型组织那句话可能还没回来；最多等这么久，超时就退回确定性文案。
# 实测 opencode 这条路由组织一句中文要 5—9 秒（含推理），7 秒会把好句子丢掉，
# 所以放宽到 15 秒——每分钟才一次汇报，等一下完全值得。
_DIGEST_WAIT_SECONDS = 15
# DSH 自己的工具名 → 中文动作词。按「具体在前、笼统在后」排，避免 write 抢了 todo_write。
_DSH_TOOL_WORDS = (
    ('todo_write', '排任务清单'),
    ('read_image', '看图片'),
    ('str_replace', '改代码'),
    ('working_directory', '确认工作目录'),
    ('team_task', '排团队任务'),
    ('send_message', '跟队友通气'),
    ('wait_agent', '等队友回话'),
    ('web_search', '上网查资料'),
    ('web_fetch', '读网页'),
    ('subagent', '派子任务'),
    ('workflow', '跑工作流'),
    ('skill', '调用技能'),
    ('present', '整理交付物'),
    ('job_', '跑后台任务'),
    ('glob', '翻文件目录'),
    ('grep', '搜代码'),
    ('edit', '改代码'),
    ('write', '写文件'),
    ('read', '读文件'),
    ('pwsh', '跑命令'),
    ('bash', '跑命令'),
)
_SUMMARY_WORDS = (
    ('applications', '帮你操作软件'),
    ('computer', '帮你操作电脑画面'),
    ('screen_share', '帮你看屏幕'),
    ('pet_say', '跟你说话'),
    ('wechat', '帮你发微信'),
    ('app_state', '检查我这边状态'),
    ('desktop_command', '帮你拉起软件'),
    ('screen_context', '帮你看着屏幕'),
    ('lingshu_memory', '翻灵枢记忆'),
)
_TERMINAL_WORDS = {
    'finished': '主人～DSH 这一轮干完啦，我把结果整理给你～',
    'failed': '主人～DSH 那边任务失败了，我先看看是怎么回事，再跟你说～',
    'cancelled': '主人～DSH 这边已经停下了，我就不乱动啦～',
    'waiting': '主人～DSH 在等你补充信息呢，你有空瞄一眼哈～',
}



def _mmss(seconds):
    total = max(0, int(seconds or 0))
    if total < 60:
        return '%d 秒' % total
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return '%d 分' % minutes + (' %d 秒' % sec if sec else '')
    hours, minutes = divmod(minutes, 60)
    return '%d 小时 %d 分' % (hours, minutes)


def _mostly_latin(text):
    """DSH 的英文思考不该被原样念出来；判断这段是不是「英文为主的原文」。"""
    letters = [c for c in str(text or '') if c.isalpha()]
    if not letters:
        return False
    cjk = sum(1 for c in letters if '\u4e00' <= c <= '\u9fff')
    return cjk / len(letters) < 0.2


def _activity(row):
    """这一轮具体卡在哪个动作上，用中文短词说，供报时句式复用。"""
    probe = (str(row.get('tool') or '') + ' ' + str(row.get('summary') or '')).lower()
    for key, words in _SUMMARY_WORDS:
        if key in probe:
            return words
    tool = str(row.get('tool') or '').lower()
    if tool:
        for key, words in _DSH_TOOL_WORDS:
            if key in tool:
                return words
    for key, words in _ACTION_WORDS:
        if key in probe:
            return words
    return '帮你干活'


def _goal_hint(row):
    """从 DSH 目标里取一段真正说人话的任务提示（跳过运行时噪声）。"""
    goal = str(row.get('goal') or '').replace('\n', ' ').strip()
    if not goal or len(goal) > 90:
        return ''
    if goal.lower().startswith('current runtime context'):
        return ''
    return goal


def _plain_progress(row, elapsed=0):
    """把 DSH 的结构化事实翻成咕嘎的大白话，去掉会话编号和日志腔。"""
    summary = str(row.get('summary') or '').strip()
    tool = str(row.get('tool') or '').strip()
    step = row.get('step')
    state = str(row.get('state') or '')
    if state in _TERMINAL_WORDS:
        return _TERMINAL_WORDS[state]
    hint = _goal_hint(row)
    tail = '；你说的「' + hint + '」我记着呢' if hint else ''
    spent = ('，已经跑了 ' + _mmss(elapsed)) if elapsed else ''
    marker = 'DSH 的公开答复：'
    if summary.startswith(marker):
        body = summary[len(marker):].replace('\n', ' ').strip()
        # DSH 的公开答复常常是英文过程话（"I'll start by..."），原样念给主人听很傻。
        if body and not _mostly_latin(body):
            return body[:160]
    last_answer = str(row.get('last_answer') or '').strip()
    if _mostly_latin(last_answer):
        last_answer = ''
    probe = (tool + ' ' + summary).lower()
    for key, words in _SUMMARY_WORDS:
        if key in probe:
            if last_answer:
                return last_answer[:150] + spent
            return _OPENERS[(step or 0) % len(_OPENERS)] + '我正在' + words + '呢' + spent + '，有结果马上告诉你～' + tail
    if last_answer:
        return last_answer[:150] + spent
    return '主人～我还在帮你干活呢' + spent + '，弄好了第一时间跟你说～' + tail


class DshLink(QObject):
    def __init__(self,panel,clock=None,interval=30):
        super().__init__(panel if isinstance(panel,QObject) else None)
        self.panel=panel;self.clock=clock or time.monotonic
        self.sessions=OrderedDict();self.focused_session=None
        self.interval=max(15,int(interval));self.last_notice=0;self.event_count=0
        # 主人要求每分钟组织一次自然语言进度；留成实例属性方便按需调整/测试。
        self.narrate_seconds=_NARRATE_SECONDS
        self.host_seen=None;self.last_spoken_text=''
        self.timer=QTimer(self);self.timer.setInterval(1000);self.timer.timeout.connect(self.tick);self.timer.start()

    def receive(self,args):
        from core.desktop_access import redact
        session=str(args.get('session_id','')).strip()
        if not session or len(session)>160:raise ValueError('DSH 会话编号无效')
        if args.get('kind')=='host/heartbeat':
            self.host_seen=self.clock();return dict(ok=True,connected=True)
        state=str(args.get('state',''))
        if state not in ('','running','finished','failed','cancelled','waiting'):raise ValueError('DSH 任务状态无效')
        now=self.clock();old=self.sessions.get(session,{})
        self.host_seen=now
        turn=args.get('turn')
        if turn is not None and old.get('turn') is not None and int(turn)<int(old['turn']):
            return dict(ok=True,stale_event_ignored=True)
        value=dict(old,session_id=session,updated_at=now,kind=str(args.get('kind',''))[:40])
        if value['kind']!='heartbeat':value['last_event_at']=now
        # 2026-11-12：主人要的是「它跑到哪一步了」的具体汇报，不是一句「正在处理」。
        # 只留最新状态的话模型根本说不出步骤，所以这里攒一小段动作轨迹。
        try:
            steps=list(old.get('recent_steps') or [])
            note=str(args.get('summary') or '').strip()
            if note and args.get('kind')!='heartbeat':
                clean=redact(note)[:160]
                if not steps or steps[-1]!=clean:
                    steps.append(clean)
                    del steps[:-6]
            value['recent_steps']=steps
        except Exception:
            value['recent_steps']=list(old.get('recent_steps') or [])
        for key,limit in (('goal',500),('summary',500),('tool',100),('reason',100),('event_id',100),('parent_session_id',160)):
            if key in args:value[key]=redact(str(args[key]))[:limit]
        answer=str(value.get('summary') or '')
        marker='DSH 的公开答复：'
        if answer.startswith(marker):
            body=answer[len(marker):].replace('\n',' ').strip()
            if body:value['last_answer']=body[:300]
        for key in ('turn','step'):
            if key in args and args[key] is not None:value[key]=max(0,int(args[key]))
        if state:value['state']=state
        if old.get('event_id')==value.get('event_id') and old:return dict(ok=True,duplicate=True)
        value.setdefault('state','running')
        value.setdefault('first_seen',now)
        self.sessions[session]=value;self.sessions.move_to_end(session)
        while len(self.sessions)>16:self.sessions.popitem(last=False)
        self._focus(session,value)
        self.event_count+=1
        if self.focused_session==session:
            voice=getattr(self.panel,'voice',None)
            if voice is not None and not getattr(self.panel,'busy',False):
                voice.set_task_context(dict(running=value['state']=='running',kind='external_dsh',goal=value.get('goal',''),summary=value.get('summary','')))
        if value.get('state') in ('finished','failed','cancelled','waiting') and old.get('state')!=value.get('state'):
            value['terminal_pending']=True
            value['terminal_since']=now
        # 当次任务真的跑完了：先验收实际情况，再组织「下一步」，不靠模型自述。
        if value.get('state')=='finished':
            self._start_review(value)
        try:
            if self.focused_session==session:
                from core.super_agent.screen_context import start_watching,stop_watching
                if value.get('state')=='running':start_watching('dsh-task')
                else:stop_watching()
        except Exception:
            pass
        return dict(ok=True,received=True,session_id=session,state=value['state'])

    def status(self):
        now=self.clock()
        rows=[dict(v,connected=now-v['updated_at']<45,age_seconds=round(now-v['updated_at'],1)) for v in self.sessions.values()]
        return dict(connected=self.host_seen is not None and now-self.host_seen<45,focused_session=self.focused_session,
                    focus_basis='most_recent_user_message',sessions=rows,events_received=self.event_count,report_interval_seconds=self.interval)

    def execute(self,args):
        if args.get('action','status')=='focus':
            session=str(args.get('session_id',''))
            if session not in self.sessions:raise ValueError('没有收到这个 DSH 会话的状态，请用真实会话编号')
            self.focused_session=session
        elif args.get('action','status')!='status':raise ValueError('外部 DSH 目前支持查询与关注会话；暂停、停止请在 DSH 内执行')
        return dict(ok=True,**self.status())

    def active_context(self):
        row=self.sessions.get(self.focused_session)
        if not row or row.get('state')!='running' or self.clock()-row['updated_at']>=45:return None
        return dict(running=True,kind='external_dsh',goal=row.get('goal',''),summary=row.get('summary',''))

    def _owned(self):
        try:
            from core.super_agent.dsh_gateway import owned_sessions
            return owned_sessions()
        except Exception:
            return set()

    def _focus(self, session, value):
        """只盯咕嘎自己拉起的那个对话；没有归属信息时优先盯主会话，不盯别人的子会话。"""
        owned = self._owned()
        if owned and session in owned:
            self.focused_session = session
            return
        # 归属会话如果已经不在我们跟踪的列表里（被关掉/早就结束），不能因此永久闭嘴；
        # 只有在「确实还有一个归属会话在跟踪中」时，才不抢别人的会话。
        if owned and (owned & set(self.sessions.keys())):
            return
        child = bool(value.get('parent_session_id'))
        if self.focused_session is None:
            if not child:
                self.focused_session = session
                self._focus_is_child = False
            else:
                # 只看到子会话时先盯着，等主会话出现再换过去。
                self.focused_session = session
                self._focus_is_child = True
            return
        # 当前盯的是子会话，而主会话出现了：换成主会话。
        if getattr(self, '_focus_is_child', False) and not child:
            self.focused_session = session
            self._focus_is_child = False

    def _report_text(self, row, now):
        """返回这一拍要不要说话、说什么；返回空串就保持安静。

        节奏（主人 2026-11-11 指定）：
        1) 任务结束 → 立刻报终态；随后再报一条「验收结论 + 下一步」。
        2) 运行中 → **每 60 秒**组织一次自然语言进度，不多不少。
        3) 连接断了 → 只说一次「暂时听不到」，绝不谎称结束。
        """
        if row.get('terminal_pending'):
            # 正常跑完：先等「验收 + 下一步」，把结论和下一步合成一条说，不连发两条。
            if str(row.get('state') or '') == 'finished':
                result = row.get('review_result')
                if result:
                    row['terminal_pending'] = False
                    row['review_spoken'] = True
                    return str(result.get('next_step') or '') or _TERMINAL_WORDS['finished']
                if now - row.get('terminal_since', now) < 10:
                    return ''
            row['terminal_pending'] = False
            return _TERMINAL_WORDS.get(str(row.get('state') or ''), '')
        # 验收结果比终态播报晚到：补一条，且只补一次。
        result = row.get('review_result')
        if result and not row.get('review_spoken'):
            row['review_spoken'] = True
            return str(result.get('next_step') or '')
        if row.get('pending_report'):
            # 到点了，模型那句还没组织好：再等一小会儿，超时就退回确定性文案。
            elapsed = now - row.get('first_seen', now)
            line = ''
            try:
                from core.super_agent import dsh_digest
                line = dsh_digest.cached(dsh_digest.fingerprint(row, row.get('narrate_seq', 0)))
            except Exception:
                line = ''
            if not line and now - row.get('pending_since', now) < _DIGEST_WAIT_SECONDS:
                return ''
            row['pending_report'] = False
            return line or _plain_progress(row, elapsed)
        # 固定 60 秒节拍：到点就发起一次「组织」，这一拍先不打扰。
        if now - row.get('last_narrate_at', row.get('first_seen', now)) < self.narrate_seconds:
            return ''
        seq = int(row.get('narrate_seq') or 0) + 1
        row['narrate_seq'] = seq
        row['last_narrate_at'] = now
        elapsed = int(now - row.get('first_seen', now))
        line = ''
        started = ''
        try:
            from core.super_agent import dsh_digest
            started = dsh_digest.request_async(row, elapsed, variant=seq)
            if not started:
                line = dsh_digest.cached(dsh_digest.fingerprint(row, seq))
        except Exception:
            started = ''
        if line:
            return line
        if not started:
            # 模型通道关闭（测试/现场关闭超级智能体）：不白等 7 秒，直接用确定性文案。
            return _plain_progress(row, elapsed)
        row['pending_report'] = True
        row['pending_since'] = now
        return ''

    def _start_review(self, row):
        """任务结束后在后台跑一次「验收 + 下一步」，结果回填给主线程播报。"""
        if row.get('review_started'):
            return
        row['review_started'] = True
        snapshot = dict(row)

        def work():
            try:
                from core.super_agent import finish_review
                result = finish_review.review(snapshot)
            except Exception as exc:  # noqa: BLE001
                result = dict(ok=False, verdict='unverified', reasons=[type(exc).__name__],
                              next_step='')
            if result.get('next_step'):
                row['review_result'] = result

        threading.Thread(target=work, name='guga-finish-review', daemon=True).start()

    def tick(self):
        row=self.sessions.get(self.focused_session)
        if not row:return
        now=self.clock()
        try:
            from core.super_agent.screen_context import get_watcher
            watcher=get_watcher()
            if watcher.active and now-row.get('updated_at',now)>180:watcher.stop()
        except Exception:
            pass
        panel=self.panel
        allowed=getattr(panel,'companion_chat_allowed',None)
        if callable(allowed) and not allowed():return
        if not callable(allowed) and getattr(panel,'busy',False):return
        if getattr(panel,'dialogue',None) is not None and panel.dialogue.active:return
        if now-row['updated_at']>=45:
            if row.get('stale_notified'):return
            text='主人，我这边暂时听不到 DSH 的动静了，还不能判断任务是不是结束了，我先不乱动，等它回来～'
            row['stale_notified']=True
            voice=getattr(panel,'voice',None)
            if voice and not getattr(panel,'busy',False):
                voice.set_task_context(dict(running=False,kind='external_dsh_unknown',goal=row.get('goal',''),summary='进度连接暂时中断，无法确定任务是否仍在执行；没有发出停止指令'))
        else:
            row.pop('stale_notified',None)
            text=self._report_text(row,now)
            if not text:return
            # The model digest is the complete public report. Do not append
            # unrelated screen observations or memories to task progress.
            if text==self.last_spoken_text:return
        voice=getattr(panel,'voice',None)
        if getattr(panel,'voice_active',False) and voice:
            if not voice.notify_battleground('dsh-link-'+row.get('event_id',''),text,during_task=True):return
        else:
            if hasattr(panel,'_show_active_bubble'):panel._show_active_bubble(text)
            panel._say_active_reply(text,'happy','none',speak=True,conversation_id=panel.active_id)
        row['terminal_pending']=False;row['last_spoken_goal']=row.get('goal');self.last_notice=now
        self.last_spoken_text=text
        try:
            from core.super_agent.narration import log_report
            log_report(text,str(row.get('state') or ''))
        except Exception:
            pass
        try:
            from core.super_agent.screen_context import stop_watching
            if row.get('state') in ('finished','failed','cancelled','waiting'):
                stop_watching()
                if row.get('state')=='finished':
                    from core.super_agent.lingshu_memory import remember_async
                    remember_async('DSH 任务进度回顾',text)
        except Exception:
            pass


def format_status(state):
    rows=state.get('sessions',[])
    if not rows:return '还没有收到本地 DSH 的任务回执。重启本机 DSH，确认“咕嘎 · 桌面助手”插件启用，再在 DSH 中发起任务。'
    labels=dict(running='进行中',finished='本轮结束',failed='失败',cancelled='已取消',waiting='等待')
    return '\n'.join(('★ ' if x['session_id']==state.get('focused_session') else '')+x['session_id']+' · '+labels.get(x.get('state'),x.get('state','未知'))+
        ('（进度连接已中断）' if not x.get('connected') and x.get('state')=='running' else '')+'\n目标：'+x.get('goal','未取得')+'\n'+x.get('summary','暂无进度') for x in rows)
