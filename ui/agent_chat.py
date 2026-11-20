"""Guga's floating workspace: projects, durable chats, voice and observable self-control."""
import html
import json
import os
import re
import time
from pathlib import Path

FAST_DIAGNOSTICS_PATH=Path(__file__).resolve().parents[1]/'runtime/voice/fast_command_diagnostics.json'
SCREEN_SHARE_DIAGNOSTICS_PATH=Path(__file__).resolve().parents[1]/'runtime/screen-share-lifecycle.json'


def execution_backend_command(text):
    value=re.sub(r'\s+','',str(text)).lower().strip('。！!')
    return {'切换到旧执行器':'self','切换到本地智能体':'self','切换到本地执行器':'self',
            '咕嘎自己处理':'self','切换到咕嘎':'self','开启咕嘎':'self',
            '开启codex':'codex','启用codex':'codex','开启dsh':'dsh','启用dsh':'dsh','切换到dsh':'dsh',
            '开启workbuddy':'workbuddy','切换到workbuddy':'workbuddy',
            '切换到dsh执行器':'dsh','切换到自动执行器':'auto',
            '切换到codex':'codex','切换到codex执行器':'codex',
            '切换执行器codex':'codex','切换到codex桌面端':'codex','切换到codex桌面端执行器':'codex',
            '切换到chatgpt桌面端':'codex','切换到chatgpt桌面端执行器':'codex'}.get(value)


def codex_command(text):
    """Only explicit desktop delegation/control; merely mentioning Codex stays chat."""
    value=re.sub(r'^(?:(?:请你?|帮我|麻烦你?)[，,\s]*)+','',str(text).strip()).strip('。！!？? ')
    name=r'(?:codex(?:\s*桌面端)?|chatgpt\s*桌面端)'
    if re.fullmatch(r'(?:打开|切到|切换到|查看)\s*'+name+r'\s*(?:的)?(?:任务)?(?:对话|会话|窗口)?',value,re.I):
        return dict(action='open')
    if re.fullmatch(r'(?:停止|取消|中止)\s*'+name+r'\s*(?:的)?(?:当前)?(?:任务|执行)?',value,re.I):
        return dict(action='stop')
    if (re.fullmatch(r'(?:查看|查询|查一下|看看|汇报)?\s*'+name+r'\s*(?:的|现在|当前)?(?:任务)?(?:的)?\s*(?:进度|进展|状态|做到哪了|完成了吗|做完了吗)(?:怎么样(?:了)?|如何|呢)?',value,re.I)
            or re.fullmatch(r'(?:查看|查询|查一下|看看|汇报)\s*'+name+r'\s*(?:的)?(?:当前)?任务',value,re.I)):
        return dict(action='status')
    match=re.fullmatch(r'(?:交给|交由|让|用|使用)\s*'+name+r'\s*[：:，,]?\s*(.*)',value,re.I|re.S)
    if match:
        task=match.group(1).strip()
        if re.fullmatch(r'(?:停止|取消|中止)(?:当前)?(?:任务|执行)?',task):return dict(action='stop')
        if re.fullmatch(r'(?:查询|查看|汇报|报告)(?:当前)?(?:任务)?(?:进度|状态|进展)',task):return dict(action='status')
        return dict(action='start',text=task)
    match=re.fullmatch(r'(?:把|将)(.+?)(?:交给|交由)\s*'+name,value,re.I|re.S)
    if match:return dict(action='start',text=match.group(1).strip())
    return None


CODEX_STATES={'dispatching':'正在派发','queued':'已受理，等待执行','starting':'正在启动',
              'running':'正在执行','needs_input':'等待确认或补充',
              'stop_requested':'已请求停止，等待确认','stopping':'正在收尾，等待停止确认',
              'completed':'已返回结果，待核验','failed':'任务失败','stopped':'已确认停止',
              'cancelled':'已确认停止','canceled':'已确认停止','interrupted':'已确认停止',
              'unknown':'任务状态未知','monitor_disconnected':'监测连接已断开，任务状态未知',
              'delivery_unknown':'派发结果待核对，请勿重复发送'}


def codex_status_text(result):
    task=result.get('task') or {}
    if not task:return '当前咕嘎对话尚未绑定 Codex 任务。'
    state=str(task.get('state') or 'unknown')
    detail=task.get('error') if state in ('failed','monitor_disconnected','delivery_unknown') else None
    detail=detail or task.get('summary') or task.get('text') or '尚未收到新进度；受理不代表完成。'
    return 'Codex 状态：'+CODEX_STATES.get(state,'任务状态待确认')+'。\n'+str(detail)


def profile_only(memory,agent_id,limit=6):
    """Stable facts about the owner, never chat logs, error reports or narration."""
    rows=memory.facts(agent_id,limit=60)
    out=[]
    for row in rows:
        kind=str(row.get('kind') or '')
        if kind.startswith('profile:') or kind=='user_preference':
            out.append(str(row.get('content') or '')[:400])
        if len(out)>=limit:break
    return out


def web_fact_from_tool_event(event):
    """Pick the browser facts a DSH ego_* tool event carries into the voice layer."""
    if not isinstance(event,dict):return None
    ego=event.get('ego') if isinstance(event.get('ego'),dict) else None
    if not ego:return None
    url=str(ego.get('url') or '')[:500]
    title=str(ego.get('title') or '')[:200]
    if not (url or title):return None
    return dict(tool=str(ego.get('tool') or event.get('tool') or '')[:60],
                url=url,title=title,ok=ego.get('ok'),at=time.time())

def short_bubble_text(text,limit=260):
    """Bubbles stay short and plain; rich text belongs to the chat window."""
    from core.companion_dialogue import speech_answer
    value=speech_answer(str(text or '')).strip()
    if len(value)>limit:return value[:limit].rstrip()+'…'
    return value


def search_mode_of(panel):
    """Read the shell's 联网 switch without assuming every test double has one."""
    combo=getattr(panel,'search_combo',None)
    try:return (combo.currentData() or 'auto') if combo is not None else 'auto'
    except Exception:return 'auto'


from PyQt5.QtCore import Qt, QEvent, QTimer, pyqtSignal
from PyQt5.QtWidgets import (QDialog, QTreeWidgetItem, QInputDialog, QMenu,
    QMessageBox, QApplication)

from core.chat_workspace import ChatWorkspace
from core.chat_render import render_markdown
from core.self_agent import SelfAgent, ToolBridge
from core.realtime_agent import AgentRealtimeVoice
from core.task_control import TaskControlGate
from ui.agent_runtime import AppRuntime


class AgentChatWindow(QDialog):
    fast_result=pyqtSignal(object,object)
    screen_answer_ready=pyqtSignal(object)
    artifact_ready=pyqtSignal(object)

    def __init__(self, pet, store=None, memory=None, agent_id=None):
        # The chat is an independent desktop surface, including when the pet is hidden.
        super().__init__(None, Qt.Window | Qt.WindowStaysOnTopHint | Qt.WindowCloseButtonHint | Qt.WindowMinMaxButtonsHint)
        self.pet = pet
        self.store = store or ChatWorkspace()
        from core.agent_memory import AgentMemoryStore
        self.memory=memory or AgentMemoryStore(self.store.path.parent/'agent_memory.sqlite3')
        # Every app launch begins with Guga 001. Companion identities are entered
        # explicitly by summon/right-click during this session.
        requested_agent=str(agent_id or '001')
        self.active_profile=self.memory.profile(requested_agent) or self.memory.profile('001')
        if not self.active_profile:raise ValueError('咕嘎身份档案不可用')
        self.active_agent_id=self.active_profile['agent_id']
        self.active_id = None
        self.busy = False
        self.voice_active = False
        self.voice = None
        self._native_video_call = False
        self._voice_task = None
        self._voice_readback = False
        self._task_control = TaskControlGate()
        self._last_fast_result=None
        self._last_user_activity=time.monotonic()
        self._pending_tts_display=None
        self._bubble_voice_long_call=False
        self._manual_voice_turn=False
        self._manual_resume_after=0.
        self._manual_voice_timer=QTimer(self);self._manual_voice_timer.setInterval(120)
        self._manual_voice_timer.timeout.connect(self._sync_manual_voice)
        self._voice_departing=False
        self._voice_farewell_started=0.0
        self._fast_inflight=False
        # 被"桌面正忙"挡下的启动请求：占用结束后自动补开一次（见 _replay_pending_launch）
        self._pending_launch=None
        self._fast_launch_at=0.0
        self.fast_result.connect(self._finish_fast_command)
        self.show_archived = False
        self.expanded_logs = set()
        self.runtime = AppRuntime(pet, self, self.store)
        self.bridge = ToolBridge(self.runtime.execute_internal, self)
        from core import agent_console
        try:
            from core.model_settings import load_config
            console_backend = agent_console.enabled(load_config())
        except Exception:
            console_backend = False
        if console_backend:
            from core.agent_console import ConsoleWorker
            self.agent = ConsoleWorker(self.bridge, self)
        else:
            self.agent = SelfAgent(self.bridge, self)
        self.agent.completed.connect(self._completed)
        self.agent.activity.connect(self._activity)
        self.agent.progress.connect(self._task_progress)
        from core.chat_answer import AnswerWorker
        from core.short_voice import ShortVoice
        self.answer=AnswerWorker(self);self.answer.completed.connect(self._completed);self.answer.partial.connect(self._answer_partial)
        self.dialogue=AnswerWorker(self);self.dialogue.completed.connect(self._dialogue_completed)
        self._dialogue_context=None;self._question_sequence=0
        self.short_voice=ShortVoice(self);self.short_voice.state.connect(self._short_voice_state)
        self.short_voice.failed.connect(self.status_error);self.short_voice.transcript.connect(self._short_transcript)
        from core.music_stop import MusicStopListener
        self.music_listener=MusicStopListener(self);self.music_listener.stop_requested.connect(self.runtime.music.stop)
        if hasattr(self.pet._tts,'playback_started'):
            self.pet._tts.playback_started.connect(self._local_tts_playback_started)
            self.pet._tts.speech_failed.connect(self._local_tts_failed)
            self.pet._tts.speak_finished.connect(self._sync_manual_voice)
        from core.proactive_conversation import ProactiveConversation
        self.proactive=ProactiveConversation(self,self.memory)
        self._run_kind=None;self._answer_text=''
        self._last_task_notice=0
        self._task_summary='正在规划第一步，尚未修改目标文件'
        self._task_started=0
        self._task_feedback_sequence=0
        self._dsh_last_notice=0
        self._task_reporter=None
        from core.companion_report import CompanionReporter
        self.public_reporter=CompanionReporter(self)
        self.public_reporter.ready.connect(self._public_report_ready)
        self.artifact_ready.connect(self._deliver_artifacts)
        self._delivery_seen=set();self.result_board=None
        self._dsh_reports={}
        self._codex_tasks={}
        self._task_speech_owners={}
        self._codex_closing=False
        self._web_facts={}
        self._pending_execution=None
        self._pending_alignment=None;self._alignment_enabled=True
        self._manager_enabled=True;self._executor_view=False
        self.store.preference('execution_backend',self.pet._config._s.value('team/executor','auto'))
        from core.managed_chat import ManagedChats
        self.managed=ManagedChats(self);self.managed.changed.connect(self._managed_update)
        self.pet._agent_bubble.alignment_selected.connect(self._choose_alignment)
        self._active_workflow=None
        self._active_route=None
        self._active_task_text=''
        from core.task_speech import TaskSpeechQueue
        self._task_speech=TaskSpeechQueue()
        self._task_heartbeat_timer=QTimer(self)
        self._task_heartbeat_timer.setInterval(1000)
        self._task_heartbeat_timer.timeout.connect(self._task_heartbeat)
        self.logs_visible = False
        self._copy_store = {}
        self._render_cache = {}
        self._pending_image = None
        self._screen_answers_inflight={}
        self.screen_answer_ready.connect(self._deliver_screen_answer)
        self._screen_share_call_owned = False
        self._screen_share_manual_stop = False
        self._screen_share_pending = False
        self._screen_share_retry_at = 0.0
        self._screen_share_watchdog_timer = QTimer(self)
        self._screen_share_watchdog_timer.setInterval(5000)
        self._screen_share_watchdog_timer.timeout.connect(self._screen_share_watchdog)
        self._window_drag_pos = None
        from ui.chat_shell import build
        build(self)
        self.short_voice.level.connect(self.voice_record_bar.set_level)
        self.short_voice.level.connect(self.pet._agent_bubble.set_voice_level)
        self.refresh_tree()
        saved = self.store.preference('agent/%s/active_conversation'%self.active_agent_id) or self.store.preference('active_conversation')
        ids = [c['id'] for c in self.store.snapshot(agent_id=self.active_agent_id)['conversations']]
        self.open_conversation(saved if saved in ids else (ids[0] if ids else self.store.create_conversation(agent_id=self.active_agent_id)))
        self.proactive.start()
        try:
            from core.reflex import start_foreground_tracker
            start_foreground_tracker()
        except Exception:
            pass

    def _pin(self, enabled):
        self.setWindowFlag(Qt.WindowStaysOnTopHint, enabled)
        self.show()

    def hide_to_pet(self):
        self.hide()
        self._show_active_bubble('我在听，可以继续说。' if self.voice_active else '点击说话即可录音，录好后点 ↑ 发送。')

    def _show_active_bubble(self,text):
        manager=getattr(self.pet,'_multi_agent',None)
        if manager is not None:manager.say(self.active_agent_id,text)
        else:self.pet._show_agent_bubble(text,self.active_id)

    def companion_chat_allowed(self):
        """Let background work coexist with initiative without overlapping a foreground answer."""
        if time.monotonic()<getattr(self,'_direct_control_until',0):return False
        if self.busy and self._run_kind=='answer':return False
        if self.short_voice.active or self.dialogue.active:return False
        if self._task_speech.pending or self._pending_tts_display is not None:return False
        if self.voice_active and self.voice:
            return not any(getattr(self.voice,name,False) for name in ('_playing','_generating','_user_speaking'))
        return not getattr(self.pet._tts,'is_speaking',False)

    def _say_active_reply(self,text,expression=None,action='none',speak=False,conversation_id=None,spoken_text=None):
        """Display exactly one desktop bubble and optionally synthesize its speech."""
        ident=conversation_id or self.active_id;display=str(text or '').strip()
        if not display:return
        from core.embodiment import reply_performance
        expression, action = reply_performance(spoken_text or display, expression, action)
        if speak and (not self.voice_active or self._manual_voice_turn or not getattr(self.voice,'connected',False)):
            self._pending_tts_display=dict(text=display,ident=ident,agent_id=self.active_agent_id,expression=expression,action=action)
            if hasattr(self.pet,'_apply_agent_style'):self.pet._apply_agent_style(expression,action)
            from core.agent_identity import voice_preferences
            profile=voice_preferences(self.active_profile) if self.active_agent_id!='001' else None
            self.pet._tts.speak(str(spoken_text or display),voice_profile=profile);return
        manager=getattr(self.pet,'_multi_agent',None)
        if manager is not None and self.active_agent_id!='001':manager.say(self.active_agent_id,display,expression)
        elif hasattr(self.pet,'_say_agent_reply'):self.pet._say_agent_reply(display,expression,action,False,ident)
        else:self.pet._show_agent_bubble(display,ident)

    def _local_tts_playback_started(self,_text):
        pending=self._pending_tts_display;self._pending_tts_display=None
        if not pending:return
        if pending['ident']!=self.active_id or pending.get('agent_id',self.active_agent_id)!=self.active_agent_id:return
        manager=getattr(self.pet,'_multi_agent',None)
        if manager is not None and self.active_agent_id!='001':manager.say(self.active_agent_id,pending['text'],pending.get('expression'))
        elif hasattr(self.pet,'_show_agent_bubble'):self.pet._show_agent_bubble(pending['text'],pending['ident'])

    def _local_tts_failed(self,message):
        pending=self._pending_tts_display;self._pending_tts_display=None
        if pending and pending['ident']==self.active_id and pending.get('agent_id',self.active_agent_id)==self.active_agent_id:
            self._show_active_bubble(pending['text'])
            self.status.setText(str(message)+' · 已显示文字')

    def _active_gesture(self,name):
        manager=getattr(self.pet,'_multi_agent',None)
        if manager is not None and self.active_agent_id!='001':manager.gesture(self.active_agent_id,name)
        else:self.pet._perform_gesture(name)

    def _active_expression(self,name):
        manager=getattr(self.pet,'_multi_agent',None)
        if manager is not None and self.active_agent_id!='001':manager.set_expression(self.active_agent_id,name)
        else:self.pet._anim.set_expression(name)

    def _sync_dsh_sessions(self):
        if getattr(self,'_manager_enabled',False):return  # No automatic import of unrelated DSH chats.
        """把 DSH 里真实存在的会话接进侧栏（带节流，避免每次刷新都走网络）。

        主人 2026-11-12 的要求：打开咕嘎的对话就是开启 DSH。所以侧栏里必须
        能看到 DSH 那边的会话；不接进来就还是两套东西。同步失败不影响本地
        对话列表，只是少显示几条。
        """
        import time
        now = time.monotonic()
        if now - getattr(self, '_dsh_sync_at', 0.0) < 30:
            return
        self._dsh_sync_at = now
        try:
            from core.super_agent.dsh_sync import sync_sessions
            sync_sessions(self.store, agent_id=self.active_agent_id)
        except Exception:
            pass

    def refresh_tree(self):
        self.tree.clear()
        self._sync_dsh_sessions()
        snapshot = self.store.snapshot(self.show_archived,agent_id=self.active_agent_id)
        groups = {}
        root = QTreeWidgetItem(self.tree, ['最近对话' if not self.show_archived else '已归档'])
        groups[None] = root
        for p in snapshot['projects']:
            item = QTreeWidgetItem(self.tree, [p['name']])
            item.setData(0, Qt.UserRole, ('project', p['id']))
            groups[p['id']] = item
        for c in snapshot['conversations']:
            row=self.managed.get(c['id'])
            backend='咕嘎' if row['backend']=='guga' else row['backend'].upper();bound=row.get('external_id') or ''
            state=CODEX_STATES.get(row['state'],{'new':'待连接','connecting':'正在连接','ready':'已连接',
                'disconnected':'连接中断'}.get(row['state'],row['state']))
            title = c['name'] if backend == '咕嘎' else backend + ' · ' + c['name']
            item = QTreeWidgetItem(groups.get(c['project_id'], root),[title])
            item.setToolTip(0,c['name']+'\n'+backend+' · '+state+('\n对话：'+bound if bound else ''))
            item.setData(0, Qt.UserRole, ('conversation', c['id']))
            if c['id'] == self.active_id:
                self.tree.setCurrentItem(item)
        # 2026-11-12：插件市场。DSH 装了什么插件这里也列出来，两边共用一份清单，
        # 不用在咕嘎里再维护一套「装了什么」。只读展示，装卸仍走 DSH。
        plugins_root = None
        try:
            from core.super_agent.dsh_plugins import summary as _plugin_summary
            info = _plugin_summary()
            if info.get('total'):
                plugins_root = QTreeWidgetItem(self.tree, ['插件 · %d' % info['total']])
                plugins_root.setToolTip(0, 'DSH 已装插件：启用 %d，停用 %d'
                                        % (info.get('enabled', 0), info.get('disabled', 0)))
                for plugin in info.get('plugins') or []:
                    flag = '✓' if plugin.get('enabled') else '✗'
                    node = QTreeWidgetItem(plugins_root, ['%s  %s' % (flag, plugin.get('id') or '')])
                    node.setToolTip(0, plugin.get('name') or plugin.get('id') or '')
                    node.setData(0, Qt.UserRole, ('plugin', plugin.get('id') or ''))
        except Exception:
            plugins_root = None
        self.tree.expandAll()
        if plugins_root is not None:
            plugins_root.setExpanded(False)   # 插件清单默认收起，不挤占对话列表
        if self.active_id:
            self.title.setText(self.active_profile['display_name']+' · '+self.store.conversation(self.active_id)['name'])
            self.title.setToolTip(self.title.text())

    def _selected(self, item, column=0):
        data = item.data(0, Qt.UserRole)
        if data and data[0] == 'plugin':
            # 插件清单只读展示；装卸走 DSH，避免咕嘎直接改 DSH 的配置把 DSH 弄坏。
            self.status.setText('插件「%s」由 DSH 管理，装卸请到 DSH 的插件市场' % (data[1] or ''))
            return
        if data and data[0] == 'conversation':
            if self.busy or self.voice_active:
                self.status.setText('请先结束本轮或语音通话，再切换会话')
                return
            self.open_conversation(data[1])

    def open_conversation(self, ident):
        pending=getattr(self,'_pending_alignment',None)
        if pending and pending['ident']!=ident:
            self._pending_alignment=None;self.pet._agent_bubble.clear_alignment()
        if hasattr(self,'attachment_bar'):self._clear_attachment()
        self.active_id = ident
        if getattr(self,'_manager_enabled',False):self.managed.refresh(ident)
        self.store.preference('active_conversation', ident)
        self.store.preference('agent/%s/active_conversation'%self.active_agent_id,ident)
        self.refresh_tree()
        self.refresh_companion_header()
        self.render()
        self.stop_button.setEnabled(bool(self.busy or self._codex_running() or self.runtime.music.requested))

    def _rich_message(self,key,content):
        """One message -> Qt rich HTML, cached by (key, content)."""
        content=str(content or '')
        hit=self._render_cache.get(key)
        if hit and hit[0]==content:return hit[1],hit[2]
        codes=[]
        def register(source):
            codes.append(source)
            return 'guga-copy:%s:%d'%(key,len(codes)-1)
        rendered=render_markdown(content,register)
        # 2026-11-12：原来超过 120 条就整表 clear()，下一帧会把所有历史消息重新
        # 渲染一遍，表现为流式回答时每隔十几秒卡一次。改成只丢草稿条目（它每帧
        # 都变、缓存永远 miss），其余按插入顺序淘汰最旧的，历史消息继续复用。
        if len(self._render_cache)>120:
            self._render_cache.pop('draft',None)
            while len(self._render_cache)>120:
                self._render_cache.pop(next(iter(self._render_cache)),None)
        self._render_cache[key]=(content,rendered,codes)
        return rendered,codes

    def _dsh_transcript(self, session_id):
        """读 DSH 会话记录（按文件修改时间缓存，render 很频繁不能每次都解压）。"""
        try:
            from core.super_agent.dsh_transcript import read_transcript, session_dir
            folder = session_dir(session_id)
            if folder is None:
                return []
            log = folder / 'session.v4.jsonl.zstd'
            stamp = log.stat().st_mtime
        except Exception:
            return []
        cache = getattr(self, '_transcript_cache', None)
        if cache is None:
            cache = {}
            self._transcript_cache = cache
        hit = cache.get(session_id)
        if hit and hit[0] == stamp:
            return hit[1]
        try:
            rows = read_transcript(session_id, limit=200)
        except Exception:
            rows = []
        cache[session_id] = (stamp, rows)
        return rows

    def _messages_for(self, ident):
        """取这个对话该显示的消息。

        绑定了 DSH 会话的对话以 **DSH 的真实记录**为准——这样你在 DSH 网页端
        看到的就是同一份对话；本地刚发、DSH 还没写进日志的消息补在后面。
        没绑定 DSH 的对话照旧走本地存储。
        """
        local = self.store.messages(ident)
        if getattr(self,'_manager_enabled',False):
            remote=self.managed.cache.get(ident,[])
            if self._executor_view:
                return [dict(id='remote-'+str(x.get('id') or x.get('seq') or i),role=x['role'],content=x['text']) for i,x in enumerate(remote)]
            # Guga's conversation remains the main view. Raw executor replies
            # are available through the explicit history toggle above.
            visible=[]
            for message in local:
                if (visible and message.get('role')=='assistant' and
                        message.get('content','').startswith('这次连接或派单没有确认：') and
                        visible[-1].get('content')==message.get('content')):
                    continue
                visible.append(message)
            return visible
        try:
            session_id = str(self.store.conversation(ident).get('dsh_session_id') or '').strip()
        except Exception:
            return local
        if not session_id:
            return local
        remote = self._dsh_transcript(session_id)
        if not remote:
            return local
        from core.super_agent.dsh_transcript import merge_messages
        return merge_messages(local,remote)

    def _send_to_bound_dsh(self, ident, session_id, text):
        """把这句话投进已绑定的 DSH 会话，然后盯着它出回复。

        投递是同步 HTTP（几百毫秒，可接受）；等回复用主线程定时器轮询，
        不另开线程去碰界面——跨线程改控件是崩溃的常见来源。
        """
        import time
        from core.super_agent.dsh_gateway import DshGateway
        baseline=max((int(row.get('seq') or 0) for row in self._dsh_transcript(session_id)),default=0)
        try:
            from core.artifact_review import delivery_instructions
            DshGateway().prompt(session_id, text+delivery_instructions(text), mode='queue')
        except Exception as exc:
            self.status.setText('投递失败：' + str(exc)[:120])
            self.render()
            return False
        self._bound_watch = dict(session_id=session_id, ident=ident,
                                 before=len(self._dsh_transcript(session_id)),
                                 deadline=time.monotonic() + 900)
        if not hasattr(self,'_dsh_monitor'):
            from core.bound_dsh import BoundDshMonitor
            self._dsh_monitor=BoundDshMonitor(self)
            self._dsh_monitor.update.connect(self._bound_dsh_update)
            self._dsh_monitor.notice.connect(self._bound_dsh_notice)
        from core.dsh_progress import reporting_config
        self._dsh_monitor.start(ident,session_id,baseline,reporting_config(self.store,ident),goal=text)
        self.status.setText('已交给 DSH，等它回复…')
        self.render()
        return True

    def _tick_bound(self):
        if hasattr(self,'_dsh_monitor'):self._dsh_monitor.poll()

    def _bound_dsh_update(self,state):
        if state.get('generation')!=self._dsh_monitor.status().get('generation'):return
        if state.get('state')=='completed':
            self._begin_result_delivery(state['conversation_id'],state.get('goal',''),state.get('summary',''),
                str(Path(__file__).resolve().parents[1]),state.get('started_at',time.time()),'bound-'+state.get('generation',''))
            if state.get('conversation_id')==self.active_id:
                self._show_active_bubble('这一轮已返回结果，我来核对最终内容和文件。')
        if state.get('state') in ('completed','disconnected'):self._bound_watch=None
        if state.get('conversation_id')!=self.active_id:return
        labels={'queued':'DSH 已受理，等待本轮回复','running':'DSH 正在执行，尚未完成',
                'completed':'DSH 本轮已结束，结果待核验','disconnected':'DSH 监测已断开，任务状态未知'}
        self.status.setText(labels.get(state.get('state'),'DSH 状态待确认'))
        self.render()

    def _bound_dsh_notice(self,notice):
        state=self._dsh_monitor.status()
        self._request_public_report('bound-'+str(state.get('generation'))+'-'+str(notice['number']),
            dict(goal=state.get('goal','当前任务'),state=state.get('state'),
                 executor_claim=state.get('summary',''),observed_progress=notice['text']),
            notice['text'],notice['conversation_id'],generation=state.get('generation'),source='bound')

    def _request_public_report(self,key,facts,fallback,ident=None,**extra):
        context=dict(conversation_id=ident or self.active_id,agent_id=self.active_agent_id,**extra)
        return self.public_reporter.request(key,facts,fallback,context)

    def _public_report_ready(self,event):
        context=event['context'];ident=context['conversation_id']
        if ident!=self.active_id or context['agent_id']!=self.active_agent_id:return
        if context.get('source')=='local_progress' and (not self.busy or context.get('started')!=self._task_started):return
        if context.get('source')=='dsh_progress' and context.get('job') not in self._dsh_reports:return
        if context.get('source')=='bound':
            current=self._dsh_monitor.status()
            if context.get('generation')!=current.get('generation') or current.get('state') not in ('queued','running','disconnected'):return
        if context.get('source')=='delivery':
            if self.result_board and getattr(self.result_board,'delivery_key',None)==context.get('delivery_key'):
                self.result_board.set_explanation(event['text']+'\n\n'+context.get('details',''))
        self._queue_task_speech(event['key'],event['text'],context.get('terminal',False),ident,context['agent_id'])

    def _begin_result_delivery(self,ident,goal,text,workspace=None,since=0,key=''):
        from core.artifact_review import DIAGRAM
        from core.file_delivery import wants_file,extract_paths
        original_goal=str(goal).split('\n【已对齐要求】',1)[0]
        if (not extract_paths(text) and re.search(r'打开|进入|点击',original_goal) and
            not re.search(r'生成|制作|绘制|导出|下载|保存为|写入|做.*报告',original_goal)):
            return
        key=key or ident+'-'+str(since)
        if key in self._delivery_seen:return
        self._delivery_seen.add(key)
        if not (wants_file(goal) or DIAGRAM.search(goal) or extract_paths(text)):return
        if re.search(r'上一步|上次|已有|原有|已生成|不要重新生成',str(goal)):since=0
        import threading
        def work():
            from core.artifact_review import collect
            files=collect(text,workspace,since)
            self.artifact_ready.emit(dict(ident=ident,goal=goal,text=text,files=files,key=key))
        threading.Thread(target=work,name='guga-artifact-review',daemon=True).start()

    def _deliver_artifacts(self,event):
        ident=event['ident'];files=event['files']
        details='\n'.join(('已检查：' if x.get('readable') else '未通过检查：')+x['path']+
            ('\n  '+'；'.join(x.get('checks',[])) if x.get('readable') else '\n  '+x.get('error','文件未确认')) for x in files)
        if not files:details='尚未拿到本次可读取的最终文件，不能把文字答复当成文件已经交付。'
        self.store.append(ident,'assistant',details)
        if ident!=self.active_id:return
        from ui.result_board import ResultBoard
        if self.result_board is None:self.result_board=ResultBoard(self.pet)
        # Prefer the actual figure over its companion notes.
        files=sorted(files,key=lambda x:0 if x.get('preview_kind')=='html' else 1 if x.get('preview_kind') in ('image','pdf') else 2)
        self.result_board.delivery_key=event['key']
        self.result_board.present(event['goal'],files,details+'\n\n本鹅正在根据文件内容整理说明。')
        first=next((i for i,x in enumerate(files) if x.get('readable')),None)
        opened=False
        if first is not None:
            self.result_board.list.setCurrentRow(first)
            opened=self.result_board.open_selected()
        facts=dict(goal=event['goal'],executor_claim=event['text'][:1200],
                   file_checks=[{k:v for k,v in x.items() if k in ('name','checks','error','readable','excerpt')} for x in files],
                   open_requested=opened,application_render_verified=False,independent_task_acceptance=False)
        facts['embedded_html']=bool(self.result_board.open_receipt.get('embedded'))
        facts['display_location']='咕嘎成果黑板' if facts['embedded_html'] else '外部文件应用'
        embedded=bool(self.result_board.open_receipt.get('embedded'))
        fallback=('最终文件的路径已列在黑板上，我已读取其中内容。'+('网页正在黑板内展开，可以直接操作。' if opened and embedded else '也已请求电脑打开第一份文件。' if opened else '文件未能自动打开，可以在黑板上点开。')) if first is not None else details
        self._request_public_report('delivery-'+event['key'],facts,fallback,ident,source='delivery',terminal=True,
                                    delivery_key=event['key'],details=details)
        self._show_active_bubble(fallback);self.render()

    def render(self):
        self.refresh_companion_header()
        from core.chat_presentation import is_progress_ack
        messages = self._messages_for(self.active_id)
        hidden=[m for m in messages if m['role'] in ('tool','system') or
                (m['role']=='assistant' and is_progress_ack(m['content']))]
        self.log_button.setText('执行记录' + (' · '+str(len(hidden)) if hidden else ''))
        self.log_button.setVisible(bool(hidden))
        visible=[m for m in messages if m not in hidden or self.logs_visible]
        if self._answer_text:visible.append(dict(id='draft',role='assistant',content=self._answer_text))
        if not visible:
            from ui.companion_widgets import ARTWORK
            from PyQt5.QtGui import QPixmap
            if self.hero_art.property('agent') != self.active_agent_id:
                dpr = self.hero_art.devicePixelRatioF()
                portrait = QPixmap(str(ARTWORK.get(self.active_agent_id, ARTWORK['001'])))
                portrait = portrait.scaledToHeight(round(112 * dpr), Qt.SmoothTransformation)
                portrait.setDevicePixelRatio(dpr)
                self.hero_art.setPixmap(portrait)
                self.hero_art.setProperty('agent', self.active_agent_id)
            name = self.active_profile['display_name']
            address = self.active_profile.get('user_address') or '饲养员'
            self.hero_title.setText(f'{address}，{name}在这里')
            self.view.clear()
            self.conversation_pages.setCurrentWidget(self.welcome)
            return
        self.conversation_pages.setCurrentWidget(self.view)
        parts=[]
        self._copy_store={}
        for m in visible:
            role=m['role'];is_user=role=='user';detail=m in hidden
            color='#e6f1f4' if is_user else ('#f1f5f7' if detail else 'transparent')
            who='你' if is_user else ('执行记录' if detail else self.active_profile['display_name'])
            pad='12' if not detail else '8'
            footer=''
            if detail:
                key=str(m['id'])
                summary=html.escape(m['content'].split('\n',1)[0])
                if key in self.expanded_logs:
                    body=('<p style="line-height:1.65;font-size:13px">'+summary+
                          f'　<a href="guga-log:{key}">收起</a><br>'+
                          html.escape(m['content']).replace('\n','<br>')+'</p>')
                else:
                    body=('<p style="line-height:1.65;font-size:13px">'+summary+
                          f'　<a href="guga-log:{key}">展开</a></p>')
            elif is_user:
                body=('<p style="line-height:1.65;font-size:13px">'+
                      html.escape(m['content']).replace('\n','<br>')+'</p>')
            else:
                key=str(m['id'])
                body,codes=self._rich_message(key,m['content'])
                self._copy_store[key]=dict(codes=codes,text=m['content'])
                footer=('　<span style="font-size:10px;color:#b3b8c2">'
                        '<a href="guga-copy-msg:%s" style="color:#8f959e">复制</a></span>'%html.escape(key))
            content=f'<td bgcolor="{color}" style="padding:{pad}px;color:#303540"><span style="font-size:10px;color:#9aa1ad">{html.escape(str(who))}</span><br>{body}{footer}</td>'
            blank='<td width="30"></td>'
            parts.append('<table width="100%" cellspacing="0" cellpadding="0" style="margin-top:12px"><tr>'+ (blank+content if is_user else content+blank)+'</tr></table>')
        # 2026-11-12：流式回答时「整页不停抖动」的根因就在这里。
        # setHtml 会把滚动条重置到顶部，紧接着 scroll_to_bottom 又把它拉回底部，
        # 两股力每帧打一次架；而上游每 90ms 就送一个分片，于是每秒十几次弹跳。
        # 上一版只处理了「贴着底部」的情况，**用户往上翻时反而会直接跳到最上方**
        # （stick=False 就什么都不做，而 setHtml 已经把位置清到顶了）。
        # 这一版：贴着底部就跟随到底；用户翻上去就把他翻到的位置原样还回去。
        bar = self.view.verticalScrollBar()
        stick = self._near_bottom(bar)
        keep = bar.value()
        self.view.setUpdatesEnabled(False)
        try:
            self.view.setHtml(''.join(parts))
            bar.setValue(bar.maximum() if stick else min(keep, bar.maximum()))
        finally:
            self.view.setUpdatesEnabled(True)
        if stick:
            self.scroll_to_bottom(True)
        else:
            # setHtml 之后布局还没算完，立刻写回位置会被覆盖；等一拍再还回去。
            QTimer.singleShot(0, lambda: bar.setValue(min(keep, bar.maximum())))

    def _near_bottom(self, bar, slack=24):
        """用户当前是不是贴着底部。往上翻看历史时不要把他拽回底部。"""
        try:
            return bar.value() >= bar.maximum() - slack
        except Exception:
            return True

    def scroll_to_bottom(self, stick=True):
        """滚到底。setHtml 之后布局还没算完，立刻读 maximum() 得到的是旧值，
        所以要等事件循环把布局跑完再滚一次。

        2026-11-12：原来连开 0/40/160ms 三个定时器，跨帧重叠、各读各的 maximum，
        滚动位置在几个高度之间来回弹——这是抖动最主要的观感来源。
        现在只留一次 singleShot(0)；用户主动往上翻时（stick=False）完全不动。
        """
        if not stick:
            return
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())
        QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))

    def _toggle_logs(self,on):
        self.logs_visible=bool(on);self.render()

    def _toggle_sidebar(self):
        show=self.sidebar.isHidden();self.sidebar.setVisible(show)
        self.header_new.setVisible(not show)
        self.history_button.setChecked(show)

    def attach_screenshot(self,image_url):
        from core.chat_presentation import user_content
        user_content('截图',image_url)
        self._pending_image=image_url;self.attachment_bar.show()
        self.status.setText('截图已附加，输入问题后发送 · 仅本轮使用')

    def _clear_attachment(self):
        self._pending_image=None;self.attachment_bar.hide()

    def _screen_tool(self,mode):
        self.pet._screen_tools.begin(mode)

    def ask_screen(self, image_url):
        """Read the explicit snapshot without consuming the open draft or routing a task."""
        if self.busy or self.answer.active or self._fast_inflight:
            self.status.setText('请等当前操作结束，再次选择「看屏幕」')
            return False
        prompt = ('请根据这张刚截取的桌面图片简洁回答：当前是什么界面，'
                  '正在发生什么，有哪些值得留意的画面细节？看不清的内容请直说。')
        try:
            self.answer.start(self.active_id, [], prompt, image_url=image_url,
                              profile=self.active_profile, screen_snapshot=True)
        except Exception as exc:
            self.status_error('看屏幕未完成：' + str(exc))
            return False
        self._begin_turn('answer')
        self.store.append(self.active_id, 'user', '[桌面截图] 看屏幕')
        if self.store.conversation(self.active_id)['name'] == '新对话':
            self.store.rename('conversation', self.active_id, '看屏幕')
        self.status.setText('正在分析当前桌面…')
        self.refresh_tree()
        self.render()
        return True

    def _shortcut_help(self):
        failed=getattr(self.pet._screen_tools,'hotkey_errors',[])
        QMessageBox.information(self,'快捷操作','右键咕嘎或按 Ctrl+Alt+A，截取当前屏幕交给视觉模型分析。\nCtrl+Alt+E：解释当前选词。\n输入框下方的「工具」可以框选截图、框选识字或打开划词工具条。\nEnter 发送，Shift+Enter 换行。'+('\n快捷键已被其他软件占用：'+', '.join(failed) if failed else ''))

    def _log_link(self, url):
        scheme=url.scheme()
        if scheme=='guga-copy':
            key,_,index=url.path().rpartition(':')
            entry=self._copy_store.get(key) or {}
            codes=entry.get('codes') or []
            try:code=codes[int(index)]
            except Exception:code=''
            if code:
                QApplication.clipboard().setText(code)
                self.status.setText('已复制 %d 行代码到剪贴板'%len(code.splitlines()))
            else:
                self.status.setText('这段代码已经不在，请重新生成回答')
            return
        if scheme=='guga-copy-msg':
            text=str((self._copy_store.get(url.path()) or {}).get('text') or '')
            if text:
                QApplication.clipboard().setText(text)
                self.status.setText('已复制这条回答')
            return
        if scheme=='guga-log':
            key = url.path()
            if key in self.expanded_logs:
                self.expanded_logs.remove(key)
            else:
                self.expanded_logs.add(key)
            self.render()

    def open_permissions(self):
        from ui.app_permissions import AppPermissionsDialog
        AppPermissionsDialog(self.runtime.apps, self).exec_()

    def _project_selection(self):
        item = self.tree.currentItem()
        data = item.data(0, Qt.UserRole) if item else None
        if data:
            return data[1] if data[0] == 'project' else self.store.conversation(data[1])['project_id']
        return None

    def new_conversation(self):
        if self.busy or self.voice_active:
            return
        self.show_archived = False
        if getattr(self,'_manager_enabled',False):
            cid=self.managed.create('guga',project_id=self._project_selection(),agent_id=self.active_agent_id)
            self.open_conversation(cid);return
        self.open_conversation(self.store.create_conversation(project_id=self._project_selection(),agent_id=self.active_agent_id))

    def toggle_executor_view(self):
        self._executor_view=not self._executor_view
        self.managed.refresh(self.active_id);self.render()

    def open_executor(self):
        row=self.managed.get(self.active_id)
        if row['backend']=='guga':
            self.status.setText('当前是咕嘎自己的对话。选择 Codex 或 DSH 后交代任务，执行时自动连接并沿用当前对话。')
            return
        if not row['external_id']:
            self.managed.provision(self.active_id);self.status.setText('正在创建真实执行器对话…');return
        if row['backend']=='codex':self.runtime.codex.execute(dict(action='open'),self.active_id,self.active_agent_id)
        else:
            from PyQt5.QtGui import QDesktopServices
            from PyQt5.QtCore import QUrl
            QDesktopServices.openUrl(QUrl('http://127.0.0.1:3080'))

    def recover_pending_draft(self):
        row=self.managed.get(self.active_id)
        text=str(row.get('undelivered_pending') or '')
        if not text:
            self.status.setText('这个对话没有保留的未发送要求。');return
        if self.editor.toPlainText().strip():
            self.status.setText('输入框已有草稿，未覆盖。');return
        self.editor.setPlainText(text)
        self.status.setText('上次未发出的要求已恢复到输入框；检查后点发送，不会自动投递。')

    def _managed_update(self,event):
        worker_cid=event['cid'];kind=event['type'];row=self.managed.get(worker_cid)
        cid=row.get('dispatch_parent') or worker_cid
        if kind=='fish_report':
            team=getattr(self.pet,'_multi_agent',None)
            if team:team.say('002',event['text'])
            self.store.append(cid,'assistant','肥鱼：'+event['text'])
            if cid==self.active_id:self.render()
            return
        if kind=='history':
            if cid==self.active_id:self.render()
            return
        label=row['backend'].upper();text=str(event.get('text') or '')
        if kind in ('progress','dispatched','review_started'):
            self._managed_motion(worker_cid,'waiting' if event.get('state')=='needs_input' else 'working',row.get('goal',''))
        elif kind in ('completed','review_completed','error','task_failed','stopped'):
            self._managed_motion(worker_cid,'error' if kind in ('error','task_failed') else 'idle')
        if kind=='connected':line=label+' 的真实对话已连接，可以直接交代任务。'
        elif kind=='error':line='这次连接或派单没有确认：'+text
        elif kind=='task_failed':
            line=('操作结果待核对：' if event.get('state')=='delivery_unknown' else '任务没有完成：')+text
        elif kind=='dispatched':line='肥鱼已把任务交给 DSH，正在等实际结果。'
        elif kind=='progress':
            if not text:return
            line=label+' · '+short_bubble_text(text)
        elif kind=='review_started':line='执行器已返回。我已经另开 Codex 对话做独立验收，验收后再给你结论。'
        elif kind=='review_completed':
            status=event['verdict'];line={'passed':'独立验收已通过。','failed':'独立验收发现问题，结果暂不算完成。',
                'needs_input':'验收还需要补充信息。','unverified':'这次没有拿到有效的独立验收结论。'}.get(status,'验收尚未确认。')
        elif kind=='completed':line=label+' 已返回结果。'+(' '+short_bubble_text(text) if text else '')
        elif kind=='stopped':line='已停止当前任务。'+text
        else:return
        self.store.append(cid,'assistant',line)
        if cid==self.active_id:
            self.status.setText(line);self._show_active_bubble(line);self.render()
            self.stop_button.setEnabled(bool(self.busy or self._codex_running() or self.runtime.music.requested))
        if kind in ('progress','completed','review_completed'):
            facts=dict(goal=row.get('goal'),executor=label,state=row['state'],executor_report=text[:4500],
                independent_review=event.get('verdict','pending' if row.get('needs_review') else 'not_requested'))
            self._request_public_report('pm-'+cid+'-'+kind+'-'+str(hash(text)),facts,line,cid,source='manager',terminal=kind!='progress')
        if kind in ('completed','review_completed'):
            from core.file_delivery import extract_paths, wants_file
            result=row.get('result') or text
            workspace = self.runtime.codex.bound(worker_cid).get('cwd') if row.get('project_plan_turn') else ''
            if row.get('project_plan_turn') and kind == 'completed' and worker_cid == self.active_id:
                self.pet._perform_gesture('wave')
            if wants_file(row.get('goal','')) or extract_paths(result):
                self._begin_result_delivery(cid,row.get('goal',''),result,workspace or str(Path(__file__).resolve().parents[1]),
                    row.get('started_at',0),'pm-result-'+cid+'-'+str(row.get('delivered_turn','')))
        if row['backend']=='dsh' and kind=='dispatched':
            team=getattr(self.pet,'_multi_agent',None)
            if team:
                self.pet._show_agent_bubble('肥鱼，这项任务交给你。执行到哪一步、有缺什么，及时告诉我。',cid)
                QTimer.singleShot(600,lambda:team.say('002','报告咕嘎指挥员，DSH 已收到任务。我会按真实进度汇报，需要补充时再找你。'))

    def _managed_motion(self,cid,state,text=''):
        anim=getattr(self.pet,'_anim2d',None)
        if anim is None:return
        anim.work_activity='drawing' if re.search(r'画图|绘图|画一|画个|绘制|插画|设计图',text) else 'working'
        anim.set_work_state(state,'managed:'+cid)

    def _managed_codex_command(self,command,text,record_user,read_reply=False,image=None):
        if record_user:self.store.append(self.active_id,'user',text)
        accepted=True
        try:
            if image:raise ValueError('截图已保留，当前 Codex 通道请使用文字任务。')
            cid=self.active_id;action=command['action']
            row=self.managed.get(cid)
            if row['backend']!='codex':
                if action not in ('start','send','open'):
                    raise ValueError('当前对话尚未绑定 Codex 任务。')
                cid=self.managed.target(cid,'让 Codex '+command.get('text',''),backend='codex');row=self.managed.get(cid)
            if action in ('start','send'):
                task=command.get('text','').strip()
                if not task:raise ValueError('请补充要交给 Codex 的任务。')
                self.managed.send(cid,task);self._managed_motion(cid,'thinking',task)
                reply='已交给 Codex 规划执行，进度和追加要求都在原对话里。'
            elif action=='open':
                if row['external_id']:self.runtime.codex.execute(dict(action='open'),cid,row['agent_id'])
                else:self.managed.provision(cid)
                reply='已请求打开 Codex 并连接当前对话。'
            elif action=='stop':
                self.managed.stop(cid);reply='已请求停止当前 Codex 任务，等待真实回执。'
            else:
                self.managed.refresh(cid)
                reply='Codex · '+CODEX_STATES.get(row['state'],row['state'])+'。'+str(row.get('result') or row.get('error') or '')[:500]
        except Exception as exc:reply=str(exc)[:400];accepted=False
        self._last_personal_answer=reply
        self.store.append(self.active_id,'tool',reply);self.status.setText(reply);self._show_active_bubble(reply)
        if read_reply:self._say_active_reply(reply,speak=True)
        self.editor.clear();self.render()
        self.stop_button.setEnabled(bool(self.busy or self._codex_running() or self.runtime.music.requested))
        return accepted

    def activate_agent(self,agent_id):
        """Switch this shared workspace to one member's isolated conversations."""
        profile=self.memory.profile(agent_id)
        if not profile:raise ValueError('未知咕嘎编号')
        if str(agent_id)==self.active_agent_id:
            self.active_profile=profile
            self.refresh_companion_header()
            return True
        if self.busy or self.voice_active:
            self.status.setText('请先结束当前任务或长语音，再切换另一位咕嘎')
            return False
        self.active_agent_id=profile['agent_id'];self.active_profile=profile
        self.store.preference('agent/active_id',self.active_agent_id)
        saved=self.store.preference('agent/%s/active_conversation'%self.active_agent_id)
        ids=[c['id'] for c in self.store.snapshot(agent_id=self.active_agent_id)['conversations']]
        ident=saved if saved in ids else (ids[0] if ids else self.store.create_conversation(agent_id=self.active_agent_id,name=profile['role']+'对话'))
        self.open_conversation(ident);self.status.setText('已切换到 '+profile['display_name']+' · '+profile['role'])
        return True

    def refresh_companion_header(self):
        for ident,button in getattr(self,'member_buttons',{}).items():
            button.setChecked(ident==self.active_agent_id)
        if hasattr(self,'model_label'):
            if getattr(self,'_manager_enabled',False) and self.active_id:
                row=self.managed.get(self.active_id)
                mode=self.pet._config._s.value('team/executor','self')
                label={'self':'咕嘎处理','codex':'Codex','dsh':'DSH'}.get(mode,'咕嘎处理')
                binding=('  ·  已绑定 '+row['backend'].upper()) if row['external_id'] else ''
                self.model_label.setText('任务执行 · '+label+binding)
                self.executor_open.setVisible(row['backend'] in ('codex','dsh'))
                self.executor_history.setVisible(bool(row['external_id']))
            else:self.model_label.setText(self.active_profile['role'])
            self.model_label.setToolTip(self.model_label.text())

    def open_companion_settings(self,agent_id=None,tab=0):
        from ui.companion_settings import CompanionSettings
        dialog=CompanionSettings(self,agent_id or self.active_agent_id,tab)
        self._companion_settings=dialog;dialog.show();dialog.raise_()
        return dialog

    def optimize_composer(self):
        from ui.screen_tools import ExplainDialog
        text=self.editor.toPlainText().strip()
        if not text:self.status.setText('先写下想优化的提示词，再点击「优化」');return
        dialog=ExplainDialog(self);self._prompt_optimizer=dialog
        dialog.apply_requested.connect(self.editor.setPlainText)
        dialog.apply_button.show();dialog.load(text,mode='optimize')

    def new_project(self):
        name, ok = QInputDialog.getText(self, '新建项目', '项目名称')
        if ok and name.strip():
            self._guard(lambda: self.store.create_project(name))

    def _guard(self, callback):
        try:
            callback()
            self.refresh_tree()
        except Exception as exc:
            self.status.setText(str(exc))

    def _rename_item(self, item, column=0):
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        old_name = (self.store.conversation(data[1])['name'] if data[0] == 'conversation' else
                    next(p['name'] for p in self.store.snapshot()['projects'] if p['id'] == data[1]))
        name, ok = QInputDialog.getText(self, '更名', '新名称', text=old_name)
        if ok and name.strip():
            self._guard(lambda: self.store.rename(data[0], data[1], name))

    def rename_active(self):
        name, ok = QInputDialog.getText(self, '更名对话', '对话名称', text=self.store.conversation(self.active_id)['name'])
        if ok and name.strip():
            self._guard(lambda: self.store.rename('conversation', self.active_id, name))

    def _context_menu(self, position):
        item = self.tree.itemAt(position)
        if not item or not item.data(0, Qt.UserRole):
            return
        kind, ident = item.data(0, Qt.UserRole)
        menu = QMenu(self)
        menu.addAction('更名').triggered.connect(lambda: self._rename_item(item))
        if kind == 'conversation':
            move = menu.addMenu('移动到项目')
            move.addAction('独立对话').triggered.connect(lambda: self._guard(lambda: self.store.move(ident, None)))
            for p in self.store.snapshot()['projects']:
                move.addAction(p['name']).triggered.connect(lambda checked=False, pid=p['id']: self._guard(lambda: self.store.move(ident, pid)))
            menu.addAction('恢复' if self.show_archived else '归档').triggered.connect(
                lambda: self._guard(lambda: self.store.archive(ident, not self.show_archived)))
        menu.exec_(self.tree.viewport().mapToGlobal(position))

    def _toggle_archive(self):
        self.show_archived = not self.show_archived
        self.archive_button.setText('返回全部对话' if self.show_archived else '已归档对话')
        self.refresh_tree()

    def eventFilter(self, obj, event):
        if obj in (getattr(self,'_drag_header',None),getattr(self,'title',None)):
            if event.type()==QEvent.MouseButtonPress and event.button()==Qt.LeftButton:
                self._window_drag_pos=event.globalPos()-self.frameGeometry().topLeft();return True
            if event.type()==QEvent.MouseMove and self._window_drag_pos is not None and event.buttons() & Qt.LeftButton:
                self.move(event.globalPos()-self._window_drag_pos);return True
            if event.type()==QEvent.MouseButtonRelease:self._window_drag_pos=None
        if obj is getattr(self,'editor',None):
            if event.type() in (QEvent.FocusIn, QEvent.FocusOut):
                self.composer.setProperty('editing', event.type() == QEvent.FocusIn)
                self.composer.style().unpolish(self.composer)
                self.composer.style().polish(self.composer)
                self.composer.update()
            if (event.type() == QEvent.KeyPress and event.key() in (Qt.Key_Enter, Qt.Key_Return)
                    and not event.modifiers() & Qt.ShiftModifier):
                self.send()
                return True
        return super().eventFilter(obj, event)

    def _remember_dialogue(self,agent_id,role,text,conversation_id=None):
        self.runtime.companion_memory.record(agent_id,role,text,conversation_id or self.active_id)

    def _record_failure_lesson(self,ident,detail,result):
        """把一次失败归纳成一条简短经验候选，进经验库（不进长期对话记忆）。

        主人 2026-11-12 的要求：失败任务不要反复提及，但要把「问题 + 卡点」
        用简洁语言自动归纳进进化史。这里只写 lessons 表的 candidate，
        要不要启用由主人验收决定。
        """
        try:
            agent_id=self.store.conversation(ident)['agent_id']
            note=str(result.get('failure_note') or '').strip()
            task=str(getattr(self,'_active_task_text','') or '').strip()[:120]
            lines=['任务：'+(task or '（未记录任务）'),
                   '卡点：'+((note or str(detail or ''))[:400])]
            if result.get('aborted'):
                lines.append('结论：判定在原地打转，已主动停下；下次先换路径或先跟主人确认目标。')
            if result.get('needs_input'):
                lines.append('结论：这一步缺主人的决定，已经停下来问了。')
            self.runtime.dsh.archive.note(agent_id,'\n'.join(lines))
        except Exception:
            pass

    def _memory_context(self,text,conversation_id=None):
        ident=conversation_id or self.active_id
        owner=self.store.conversation(ident)['agent_id']
        return self.runtime.companion_memory.context(owner,text,ident)

    def _voice_memory(self):
        # Keep previous voice sessions out; owner facts and the live routine may survive.
        facts=profile_only(self.memory,self.active_agent_id)
        if self.runtime.daily_plan.snapshot()['templates']:
            facts.append(self.runtime.companion_memory.context(self.active_agent_id,'每日计划',self.active_id))
        return facts

    def _codex_bound(self):
        manager=getattr(self.runtime,'codex',None)
        bound=manager.bound(self.active_id) if manager is not None else {}
        if bound and str(bound.get('agent_id') or self.active_agent_id)!=self.active_agent_id:return {}
        return bound or {}

    def _codex_running(self):
        if getattr(self,'_manager_enabled',False):
            row=self.managed.get(self.active_id)
            return row['backend']=='codex' and row['state'] in ('connecting','dispatching','running','needs_input','stop_requested')
        task=getattr(self,'_codex_tasks',{}).get((self.active_id,self.active_agent_id)) or self._codex_bound()
        return bool(task and not task.get('terminal') and task.get('state') not in
                    ('idle','completed','failed','cancelled','canceled','stopped','interrupted','closed'))

    def _handle_codex_command(self,text,record_user,force_agent=False,read_reply=False,image=None):
        command=codex_command(text)
        if command is not None and getattr(self,'_manager_enabled',False):
            return self._managed_codex_command(command,text,record_user,read_reply,image)
        from core.execution_contract import is_dsh_live_query
        if command is None and is_dsh_live_query(text):return None
        bound=self._codex_bound()
        backend=self.store.preference('execution_backend') or 'auto'
        if command is None and not image and (bound or backend=='codex') and backend not in ('self','dsh'):
            from core.chat_answer import uses_task
            from core.companion_dialogue import is_task_followup,is_task_status
            if bound and is_task_status(text) and not uses_task(text,'auto'):
                command=dict(action='status')
            elif bound and re.fullmatch(r'(?:停止|取消|中止)(?:当前)?(?:任务|执行)?[。！!\s]*',text):
                command=dict(action='stop')
            elif force_agent or uses_task(text,'auto') or (bound and is_task_followup(text)):
                command=dict(action='send' if bound else 'start',text=text)
        if command is None:return None
        action=command['action'];reply=''
        if record_user:self.store.append(self.active_id,'user',text)
        if image:
            reply='当前 Codex 桌面端入口只支持文字任务；截图已保留，请另发文字指令。'
        elif action in ('start','send') and not command.get('text'):
            reply='请补充要交给 Codex 桌面端的具体任务。'
        elif len(command.get('text',''))>16000:
            reply='单条 Codex 任务最多 16000 字，请分段发送。'
        else:
            try:
                result=self.runtime.execute('codex_task',command)
                if result.get('ok') is False or result.get('accepted') is False:
                    reply='Codex 请求未受理：'+str(result.get('error') or result.get('message') or '请检查桌面端连接')
                elif action in ('status','list'):
                    if action=='status':reply=codex_status_text(result)
                    else:reply='\n'.join(codex_status_text(dict(task=row)) for row in result.get('tasks',[])) or '暂无 Codex 任务。'
                else:
                    reply={'start':'Codex 任务已受理，等待后台执行与状态确认；受理不代表完成。',
                           'send':'追加要求已受理，等待 Codex 执行；尚未完成。',
                           'open':'已请求打开绑定的 Codex 对话，等待桌面端确认。',
                           'stop':'已请求 Codex 停止任务，等待真实状态确认。'}[action]
                    if action in ('start','send','stop'):
                        tasks=getattr(self,'_codex_tasks',{})
                        previous=tasks.get((self.active_id,self.active_agent_id),{})
                        tasks[(self.active_id,self.active_agent_id)]=dict(previous,
                            thread_id=result.get('thread_id') or bound.get('thread_id') or '',
                            state='stop_requested' if action=='stop' else 'queued',summary=reply,terminal=False)
                        self._codex_tasks=tasks
            except Exception as exc:
                reply='Codex 请求未受理：'+str(exc)[:240]
        self.store.append(self.active_id,'tool','Codex · '+reply)
        self._last_personal_answer=reply
        self.status.setText(short_bubble_text(reply,120))
        self._show_active_bubble(short_bubble_text(reply))
        if read_reply:self._say_active_reply(short_bubble_text(reply),speak=True)
        self.stop_button.setEnabled(bool(self.busy or self._codex_running() or self.runtime.music.requested))
        self.editor.clear();self.render()
        return True

    def _stop_codex(self):
        if getattr(self,'_manager_enabled',False):
            row=self.managed.get(self.active_id)
            if row['backend']=='codex' and row['state'] in ('connecting','dispatching','running','needs_input','stop_requested'):
                try:return self.managed.stop(self.active_id)
                except Exception as exc:return dict(ok=False,error=str(exc)[:400])
            return None
        bound=self._codex_bound()
        if not bound:return None
        try:
            result=self.runtime.codex.execute(dict(action='stop',thread_id=bound.get('thread_id','')),
                conversation_id=self.active_id,agent_id=self.active_agent_id)
        except Exception as exc:result=dict(ok=False,error=str(exc)[:240])
        return result

    def _offer_alignment(self,text,record_user=True,**kwargs):
        from core.chat_answer import uses_task
        if not kwargs.get('force_agent') and not uses_task(text,self.route_combo.currentData(),False):return False
        from core.task_alignment import choices_for
        options=choices_for(text) if self._alignment_enabled else None
        if not options or self.route_combo.currentData()=='chat':return False
        if self._pending_alignment:
            self.status.setText('请先选择当前任务方向，或发送“取消”后提出新任务');return True
        self._pending_alignment=dict(ident=self.active_id,text=text,options=options,kwargs=kwargs)
        if record_user:self.store.append(self.active_id,'user',text)
        message='先选一个方向：1 '+options[0][0]+'；2 '+options[1][0]+'；3 补充要求。'
        self.store.append(self.active_id,'assistant',message)
        self.pet._agent_bubble.show_alignment(self.active_id,text,options)
        self.status.setText('等待选择方向 · 尚未派单');self.editor.clear();self.render();return True

    def _choose_alignment(self,ident,choice,supplement=''):
        pending=self._pending_alignment
        if not pending or pending['ident']!=ident or ident!=self.active_id or self.busy:return False
        if choice not in (0,1,2,3):return False
        if choice==3 and not str(supplement).strip():
            self.pet._agent_bubble._choose_alignment(3);return True
        if len(str(supplement))>3000:return False
        self._pending_alignment=None;self.pet._agent_bubble.clear_alignment(ident)
        if choice==0:
            self.status.setText('已取消，未派单');self._show_active_bubble('好，这次先不执行。');return True
        addition=pending['options'][choice-1][1] if choice in (1,2) else str(supplement).strip()
        self.store.append(ident,'user','选择：'+(pending['options'][choice-1][0] if choice in (1,2) else '补充要求')+'\n'+addition)
        kwargs=dict(pending['kwargs']);context=dict(kwargs.pop('execution_context',None) or {},_alignment_approved=True)
        return self.send(pending['text']+'\n【已对齐要求】'+addition,record_user=False,execution_context=context,**kwargs)

    def _open_staged_task(self, text):
        from core.project_review import is_design_only
        from core.fast_commands import command_text
        normalized = command_text(text)
        if re.search(r'不要|别去|别发|取消|上次|为什么', normalized):
            # "不要动手写代码" is part of the explicit design-only instruction.
            if not is_design_only(normalized):
                return ''
        if is_design_only(normalized):
            self.pet._show_project_review(text)
            return '已打开项目黑板，等方案预审通过后再下发开发。'
        return ''

    def send(self, text=None, record_user=True, force_agent=False, execution_context=None, use_attachment=None,read_reply=False,_skip_local=False):
        text = text if isinstance(text, str) else self.editor.toPlainText().strip()
        image=self._pending_image if (record_user if use_attachment is None else use_attachment) else None
        if image:
            if self.busy:
                self.status.setText('当前任务执行中，请等待结束后发送截图；附件已保留')
                return False
            text=text.strip() or '请解释这张截图中的内容。'
            execution_context=dict(execution_context or {},_user_image_url=image)
        if not text.strip():
            return
        command = re.sub(r'[。！!\s]+$', '', text.strip())
        if (record_user or read_reply) and not image and command in ('更新仓库', '更新插件仓库'):
            self.pet._open_plugin_market(refresh=True)
            self.editor.clear()
            return True
        if (record_user or read_reply) and not image and command in (
                '开启文件传输助手自动回复', '停止文件传输助手自动回复'):
            inbox = self.runtime.file_assistant
            line = inbox.enable() if command.startswith('开启') else inbox.stop()
            self.status.setText(line)
            self.pet._show_agent_bubble(line, self.active_id)
            self.editor.clear()
            return True
        staged = self._open_staged_task(text) if not image else ''
        if staged:
            if record_user:
                self.store.append(self.active_id, 'user', text)
            self.status.setText(staged)
            self.editor.clear()
            self._show_active_bubble(staged)
            self.render()
            return True
        if getattr(self,'_pending_alignment',None) and not (execution_context or {}).get('_alignment_approved'):
            from core.task_alignment import reply_choice
            choice,supplement=reply_choice(text)
            if choice is not None:
                accepted=self._choose_alignment(self.active_id,choice,supplement)
                if accepted:self.editor.clear()
                return accepted
        if record_user:self._last_user_activity=time.monotonic()
        self._last_personal_answer=None
        if not image:
            daily_command = self.runtime.daily_plan.conversation_command(text,self.active_id)
            if daily_command:
                if record_user:self.store.append(self.active_id,'user',text)
                self._fast_command(dict(matched=True,action='daily_plan',arguments=daily_command,
                                        source_text=text,_read_reply=bool(read_reply)))
                self.editor.clear();self.render();return True
            if record_user:self.runtime.daily_plan.clear_reminder(self.active_id)
        # Explicit user memory/review controls are local, with no model/API round trip.
        if not image and (not self.busy or execution_backend_command(text)) and self._personal_command(text,record_user):
            if read_reply:self._say_active_reply(self._last_personal_answer,speak=True)
            self.editor.clear();self.render();return True
        if getattr(self,'_manager_enabled',False) and re.search(r'肥鱼.*(?:进展|进度|汇报|怎样|怎么样)',text):
            try:
                self.managed.report_fish(self.active_id)
                if record_user:self.store.append(self.active_id,'user',text)
                self.pet._show_agent_bubble('肥鱼，你目前这个任务进行得怎样？把真实进展和需要补充的地方告诉我。',self.active_id)
                self.editor.clear();return True
            except Exception as exc:self.status.setText(str(exc));return False
        if not getattr(self,'_manager_enabled',False) or codex_command(text) is not None:
            codex_result=self._handle_codex_command(text,record_user,force_agent,read_reply,image)
            if codex_result is not None:return codex_result
        memory_context=''
        if record_user:
            self._remember_dialogue(self.active_agent_id,'user',text)
        memory_context=self._memory_context(text)
        if record_user:
            self.pet._on_user_interaction()
            team=getattr(self.pet,'_multi_agent',None)
            if team:team.user_interaction(self.active_agent_id)
        if not _skip_local and not force_agent and not image and not self.busy and not self._fast_inflight:
            # Referent desktop actions ("点这块/往下滚/按回车") need no model at all.
            # A non-unique target falls through to the normal model path instead.
            try:
                from core.reflex import match as match_reflex
                reflex_command=match_reflex(text)
            except Exception:
                reflex_command=dict(matched=False)
            if reflex_command.get('matched') and not reflex_command.get('need_model'):
                self._reflex_command(reflex_command,record_user=record_user,read_reply=read_reply)
                self.editor.clear();self.render()
                return True
        from core.fast_commands import match
        fast=dict(matched=False) if _skip_local else match(text)
        if fast.get('matched') and not force_agent and not image and (self.route_combo.currentData()!='chat' or fast.get('action') in ('daily_plan','dsh_link') or (execution_context or {}).get('_external_fast')):
            fast=dict(fast,_read_reply=bool(read_reply))
            if execution_context and execution_context.get('_receipt_id'):
                fast=dict(fast,_receipt_id=execution_context['_receipt_id'])
            if record_user:self.store.append(self.active_id,'user',text)
            self._fast_command(fast)
            self.editor.clear();self.render()
            return True
        if self._fast_inflight or getattr(self,'_fast_launch_count',0):
            self.status.setText('页面关闭操作正在核验，请稍候；本次没有启动另一项桌面任务')
            return False
        if self.busy:
            if self._run_kind=='answer':
                self.status.setText('正在回答，可先点停止再提新问题');return False
            from core.chat_answer import uses_task
            from core.companion_dialogue import is_task_followup,is_task_status
            if not force_agent and not image and not uses_task(text,'auto') and not is_task_followup(text):
                if is_task_status(text):
                    answer=self._running_task_answer(text)
                    if answer:
                        if record_user:self.store.append(self.active_id,'user',text)
                        self.store.append(self.active_id,'assistant',answer);self._show_active_bubble(answer)
                        self.editor.clear();self.render();return True
                if self._start_dialogue(text,read_aloud=read_reply):
                    if record_user:self.store.append(self.active_id,'user',text)
                    self.editor.clear();self.render();return True
                return False
            control=self._task_control.observe(text,busy=True)
            if control['action'] in ('ask_stop','pending'):
                if record_user:self.store.append(self.active_id,'user',text)
                question='当前任务仍在执行。确定要停止吗？请说“停止”确认，或说“继续”任务。'
                self.status.setText(question)
                self.store.append(self.active_id,'system',question)
                self._show_active_bubble(question)
                self.editor.clear();self.render();return True
            if control['action']=='cancel':
                self._cancel_execution();self.status.setText('已请求停止任务');self.editor.clear();return True
            if control['action']=='continue':
                self.status.setText('好，继续当前任务');self._show_active_bubble('好，继续当前任务。')
                self.editor.clear();return True
            self._steer_execution(text)
            if record_user:self.store.append(self.active_id,'user',text)
            self.editor.clear();self.status.setText('追加要求已排队；当前操作结束后处理');self.render()
            return True
        if len(text) > 16000:
            self.status.setText('单条消息最多 16000 字，请分段发送')
            return
        from core.companion_policy import read_policy, workbuddy_prompt
        from core.daily_workflows import workflow_for
        from core.task_router import choose_task_route
        from core.wechat_demo import explicit_message
        policy = read_policy(self.pet._config._s)
        workflow = self.runtime.workflows.match(text) or workflow_for(text)
        direct_request=record_user or read_reply or (execution_context or {}).get('_direct_user_request')
        message = explicit_message(text) if direct_request and not image and not _skip_local else None
        if message is None and not image and re.match(
                r'^(?:(?:咕嘎|请你?|帮我)[，,\s]*)*(?:在微信|用微信|打开微信|(?:给|向)?文件传输助手).*(?:发送|发给|发消息|回复)', text):
            line = '请把联系人名称和要发送的原文一起告诉我，例如“在微信里给文件传输助手发送：你好”。'
            if record_user:self.store.append(self.active_id,'user',text)
            self.store.append(self.active_id,'assistant',line)
            self.status.setText(line);self._show_active_bubble(line);self.editor.clear();self.render()
            return True
        workbuddy = workbuddy_prompt(text, policy) if direct_request and not image and not _skip_local and message is None else ''
        if (not workbuddy and not message and not image and not workflow and policy['executor']=='workbuddy'
                and not re.search(r'(?:不要|不用|别用)\s*work\s*buddy', text, re.I)):
            from core.chat_answer import uses_task
            if force_agent or uses_task(text,self.route_combo.currentData()):
                workbuddy = text
        if not workbuddy and not getattr(self,'_manager_enabled',False) and not image and not (execution_context or {}).get('_alignment_approved'):
            if self._offer_alignment(text,record_user,force_agent=force_agent,execution_context=execution_context,
                                     use_attachment=use_attachment,read_reply=read_reply,_skip_local=_skip_local):return True
        if not workbuddy and getattr(self,'_manager_enabled',False):
            from core.chat_answer import uses_task
            from core.companion_dialogue import is_task_followup,is_task_status
            from core.companion_policy import selected_backend
            row=self.managed.get(self.active_id)
            followup=row['backend'] if row.get('goal') and is_task_followup(text) and not workflow and not message else 'self'
            backend=selected_backend(text,policy,followup,workflow=workflow)
            if message and backend!='dsh':
                backend='self'
            if is_task_status(text) and row.get('goal'):
                self.managed.refresh(self.active_id)
                line='正在核对 '+row['backend'].upper()+' 的真实进度；当前状态：'+row['state']
                self.store.append(self.active_id,'user',text);self.store.append(self.active_id,'assistant',line)
                self._show_active_bubble(line);self.editor.clear();self.render();return True
            if backend in ('codex','dsh') and (message or force_agent or uses_task(text,self.route_combo.currentData(),bool(image)) or (row.get('goal') and is_task_followup(text))):
                if image:
                    self.status.setText('截图已保留；目前执行器通道需要文字要求，请补充要处理的内容');return False
                try:
                    target=self.managed.target(self.active_id,text,backend=backend)
                    accepted=self.managed.send(target,text)
                    row=self.managed.get(target)
                    self.refresh_companion_header()
                    self._managed_motion(target,'thinking',text)
                    if record_user:self.store.append(self.active_id,'user',text)
                    self.status.setText('已交给 '+row['backend'].upper()+'，等待真实回执')
                    self._show_active_bubble('我来跟进，这项任务交给 '+row['backend'].upper()+' 执行。')
                    self.stop_button.setEnabled(True)
                    self.editor.clear();self.render();return bool(accepted)
                except Exception as exc:
                    self.status.setText(str(exc));self._show_active_bubble(str(exc));return False
        # 2026-11-12：这个对话绑定了真实的 DSH 会话，就把话直接投进那个会话。
        # 这才是「打开咕嘎的对话就是开启 DSH」——不是另起一个任务，而是接着
        # 那个会话继续说；DSH 网页端看到的也是同一条对话。
        #
        # 但**闲聊不进 DSH**（第 3 步的闸门）：说一句「在吗」也要开一次会话，
        # 既慢又烧钱。所以只有真的要动手的话才投给 DSH，其余落到下面的本地路径。
        bound = ''
        try:
            bound = str(self.store.conversation(self.active_id).get('dsh_session_id') or '').strip()
        except Exception:
            bound = ''
        direct_dsh=self.store.preference('execution_backend') not in ('self','codex')
        if direct_dsh and bound and not image and not workbuddy and not workflow and not message:
            from core.chat_answer import uses_task as _uses_task
            _is_work = bool(force_agent or _uses_task(text, self.route_combo.currentData(), False))
            if _is_work:
                if record_user:
                    self.store.append(self.active_id, 'user', text)
                self.editor.clear()
                return self._send_to_bound_dsh(self.active_id, bound, text)
        # 未绑定的新对话绝不在这里"当场开一个 DSH 会话"：那会绕过下面的
        # choose_task_route，把本该本地执行的任务（搜索/整理/日常操作）也
        # 推给 DSH，还会在测试里真的去连网关。执行者只由 route 决定。
        history = self.store.messages(self.active_id, 40)
        from core.chat_answer import uses_task,LEVELS
        from core.task_feedback import EvidenceProgress
        task=bool(workbuddy or message) or force_agent or uses_task(text,self.route_combo.currentData(),bool(image)) or bool(workflow and self.route_combo.currentData()!='chat')
        backend=self.store.preference('execution_backend') or 'auto'
        route=choose_task_route(text,bool(image),backend,workflow) if task and not workbuddy else None
        if workbuddy:
            route = dict(executor='workbuddy', category='desktop_partner', reason='使用 WorkBuddy 桌面对话')
        elif message:
            route = dict(executor='local_agent', category='wechat', reason='咕嘎直接执行已明确的微信发送要求')
        dsh=bool(route and route.get('executor')=='dsh')
        self._active_workflow=workflow if task else None
        self._active_route=route
        self._active_task_text=text if task else ''
        try:
            observation=self.runtime.screen_share.observation() if self.runtime.screen_share.status().get('active') else None
            shared_context=self.runtime.screen_share.assist_context() if observation else None
            shared_frame=self.runtime.screen_share.snapshot() if shared_context and self.voice_active and (not task or shared_context.get('source')!='windows_uia') else {}
            shared_image=shared_frame.get('_image_url') if shared_frame.get('ok') and shared_frame.get('frame_age_seconds',99)<=2 else None
            if not task:
                self.answer.start(self.active_id,history,text,image_url=image or shared_image,effort=self.effort_combo.currentData(),read_aloud=read_reply,
                    profile=self.active_profile,memory_context=memory_context,screen_context=shared_context,
                    search_mode=search_mode_of(self))
            else:
                handoff=dict(execution_context or {},_agent_id=self.active_agent_id,
                    _agent_profile=self.active_profile,_memory_context=memory_context,
                    _route=route,_workflow=workflow,
                    _screen_context=shared_context,_screen_observation=observation)
                if shared_image and not handoff.get('_user_image_url'):handoff['_user_image_url']=shared_image
                if dsh:
                    self.agent.cancel_event.clear()
                    context=memory_context or self._memory_context(text)
                    accepted=self.runtime.dsh.start(text,self.active_id,profile=dict(self.active_profile),memory_context=context,workflow=workflow,
                        screen_context=json.dumps(self.runtime.screen_share.observation(),ensure_ascii=False) if self.runtime.screen_share.status().get('active') else '')
                    from core.task_brief import DshTaskBrief
                    job_id=accepted.get('job_id','')
                    self._dsh_reports[job_id]=dict(agent_id=self.active_agent_id,
                        conversation_id=self.active_id,reporter=EvidenceProgress(interval=30),
                        brief=DshTaskBrief(job_id,text),started=time.monotonic(),wait_talks=[])
                else:
                    if workbuddy:
                        handoff.update(_workbuddy_prompt=workbuddy, _desktop_lock=self.runtime.external_desktop_lock)
                    elif message:
                        handoff.update(_wechat_message=message, _desktop_lock=self.runtime.external_desktop_lock)
                    self.agent.start(self.active_id,history,text,handoff=handoff)
        except Exception as exc:
            self.status.setText(str(exc))
            self._active_workflow=None;self._active_route=None;self._active_task_text=''
            return False
        self._begin_turn('dsh' if dsh else 'task' if task else 'answer')
        if image:self._clear_attachment()
        if record_user:
            self.store.append(self.active_id, 'user', ('[本轮截图]\n' if image else '')+text)
        if self.store.conversation(self.active_id)['name'] == '新对话':
            self.store.rename('conversation', self.active_id, text.replace('\n', ' ')[:24])
        self.editor.clear()
        self.status.setText('DSH 正在执行 · 可查看执行记录' if dsh else '正在执行任务…' if task else ('正在回答…' if self.effort_combo.currentData()=='quick' else '思考中 · '+LEVELS[self.effort_combo.currentData()][0]))
        self._task_summary='正在规划第一步，尚未修改目标文件'
        if task:
            self._task_reporter=None if dsh else EvidenceProgress()
            self._task_heartbeat_timer.start()
        else:self._question_waiting(read_reply)
        self.refresh_tree()
        self.render()
        return True

    def _begin_turn(self, kind):
        self.busy = True
        self._run_kind = kind
        self._answer_text = ''
        self._task_started = self._last_task_notice = time.monotonic()
        self.pet._multi_agent.work_state(self.active_agent_id, 'thinking', 'turn:' + self.active_id)
        self.pet._tts.stop()
        self.send_button.setEnabled(True)
        self.new_button.setEnabled(False)
        self.stop_button.setEnabled(True)

    def _question_waiting(self,read_aloud=False):
        from core.companion_dialogue import question_opener
        opener=question_opener(self._question_sequence,self.active_profile);self._question_sequence+=1
        self._say_active_reply(opener,speak=bool(read_aloud or self.read_aloud.isChecked()))

    def _start_dialogue(self,text,call_id=None,read_aloud=False):
        """Foreground Q&A never owns or clears the background task's busy flag."""
        if self.dialogue.active:
            if call_id and self.voice:self.voice.resolve_task(call_id,dict(ok=True,answer='前一个问题还在回答，本鹅马上接着聊。'))
            else:self.status.setText('前一个问题还在回答，后台任务继续执行')
            return False
        context=dict(conversation_id=self.active_id,agent_id=self.active_agent_id,profile=dict(self.active_profile),
            voice=self.voice if call_id else None,call_id=call_id,read_aloud=read_aloud or self.read_aloud.isChecked())
        self._dialogue_context=context
        try:
            recalled=self._memory_context(text)
            self.dialogue.start(self.active_id,self.store.messages(self.active_id,40),text,profile=self.active_profile,memory_context=recalled,
                search_mode=search_mode_of(self))
            if not call_id:self._question_waiting(read_aloud)
            return True
        except Exception as exc:
            self._dialogue_context=None
            if call_id and self.voice:self.voice.resolve_task(call_id,dict(ok=False,error=str(exc)))
            else:self.status_error(str(exc))
            return False

    def _dialogue_completed(self,result):
        context=self._dialogue_context
        if not context or result.get('generation')!=self.dialogue.generation:return
        self._dialogue_context=None
        from core.companion_dialogue import speech_answer
        text=result.get('text') if result.get('ok') else '这个问题暂时没能答完：'+result.get('error','连接未完成')
        ident=context['conversation_id'];agent_id=context['agent_id']
        self.store.append(ident,'assistant',text);self._remember_dialogue(agent_id,'assistant',text,ident)
        voice=context.get('voice')
        if voice and self.voice_active and voice is self.voice:
            self._voice_readback=True
            voice.resolve_task(context['call_id'],dict(ok=bool(result.get('ok')),answer=speech_answer(text),error=result.get('error','')))
        if ident==self.active_id and agent_id==self.active_agent_id:
            from core.embodiment import reply_performance
            expression, action = reply_performance(text)
            if expression is not None:self._active_expression(expression)
            if action != 'none':self._active_gesture(action)
            self._show_active_bubble(short_bubble_text(text));self.render()
            if not self.voice_active and context['read_aloud']:
                from core.agent_identity import voice_preferences
                self.pet._tts.speak(speech_answer(text),voice_profile=voice_preferences(context['profile']))

    def _cancel_execution(self):
        self._stop_codex()
        self._task_speech.clear()
        if self.voice and hasattr(self.voice,'clear_task_notices'):self.voice.clear_task_notices()
        self.agent.cancel()
        if self._run_kind=='dsh' or self.runtime.dsh.state.get('running'):self.runtime.dsh.stop()

    def _steer_execution(self,text):
        if self._run_kind=='dsh':
            self.runtime.dsh.archive.add_input(self.runtime.dsh.state['ticket_id'],text)
        else:self.agent.steer(text)

    def _personal_command(self,text,record_user):
        from core.agent_memory import safe_text
        from core.fast_commands import command_text
        from core.daily_plan import parse_command
        # A routine has one owning store, even when the user begins with “记住”.
        if parse_command(text):return False
        owner=self.active_agent_id;archive=self.runtime.dsh.archive
        answer=None
        if text.strip() in ('打开成果黑板','查看上次成果','展开成果黑板'):
            if self.result_board is not None:
                self.result_board.show();self.result_board.raise_();answer='成果黑板已经展开，文件路径和说明都在里面。'
            else:answer='这次启动后还没有可展示的成果。任务完成并拿到最终文件后，我会自动展开黑板。'
        elif text.startswith(('记住：','记住:','请记住：','请记住:')):
            content=text.replace('：',':').split(':',1)[1].strip()
            if not content or safe_text(content)!=content:
                answer='没有保存：记忆不能为空，也不能包含凭据或超长文本。'
            else:
                scope='user'
                if content.startswith(('这个项目：','这个项目:')):scope='project';content=content[5:]
                elif content.startswith(('本对话：','本对话:')):scope='conversation';content=content[4:]
                try:self.runtime.companion_memory.execute(owner,self.active_id,dict(action='remember',content=content,scope=scope))
                except ValueError as exc:answer=str(exc)
                else:answer='已记住，保存到'+{'user':'用户长期信息，可跨对话使用','project':'当前项目，供项目内对话使用','conversation':'当前对话'}[scope]+'。'
        elif command_text(text).strip('。？！?!') in ('查看任务诊断','刚才为什么失败','为什么失败','刚才发生了什么','上个任务为什么失败'):
            last_fast=getattr(self,'_last_fast_failure',None)
            if last_fast and last_fast.get('conversation_id')!=self.active_id:last_fast=None
            task=self.agent.journal.latest(self.active_id)
            if last_fast and (not task or last_fast['at']>task.get('updated',0)):
                answer=last_fast['error']
            elif task and task.get('status') not in ('running','completed'):
                from core.agent_failure import explain_failure
                answer=task.get('failure_explanation') or explain_failure(task.get('summary'),
                    operations=task.get('operations',len(task.get('events',[]))),events=task.get('events',[]),
                    last_tool_failure=task.get('last_tool_failure'))
                answer+=' 任务编号：'+task['id'][:8]+'。'
            else:answer='当前对话没有可用的失败记录，不能猜测失败原因。'
        elif text.strip() in ('查看我的记忆','查看长期记忆'):
            facts=self.runtime.companion_memory.execute(owner,self.active_id,dict(action='list'))['facts']
            labels={'user':'长期信息','agent':'成员','project':'项目','conversation':'本对话'}
            answer='\n'.join(str(x['id'])+' ['+labels[x['scope']]+']：'+x['content'] for x in facts) or '还没有保存的长期记忆。'
            from core.daily_plan import format_plan
            if self.runtime.daily_plan.snapshot()['templates']:answer+='\n日常安排：\n'+format_plan(self.runtime.daily_plan.snapshot())
        elif text.startswith('忘记记忆 '):
            try:
                self.runtime.companion_memory.execute(owner,self.active_id,dict(action='forget',id=text.split()[-1]));answer='已删除。'
            except ValueError as exc:answer=str(exc)
        elif text.strip() in ('查看长任务','查看任务进度'):
            rows=archive.recent(owner,limit=5)
            answer='\n'.join(x['id'][:8]+' · '+x['state']+' · '+x['summary'][:160] for x in rows) or '暂无长任务。'
        elif text.strip() in ('确认上个任务完成','上个任务验收失败'):
            rows=archive.recent(owner,self.active_id,1)
            if not rows:answer='当前对话没有可验收的长任务。'
            else:
                archive.review(owner,rows[0]['id'],text=='确认上个任务完成')
                answer='已记录你的验收结果；有真实工具回执的成功任务，其经验候选才会启用。'
        elif text.strip()=='查看学习经验':
            answer='\n'.join(x['id']+' · '+x['state']+' · '+x['recipe'] for x in archive.lessons(owner)) or '暂无经验候选。'
        elif text.startswith('停用经验 '):
            try:archive.disable_lesson(owner,text.split()[-1]);answer='经验已停用。'
            except ValueError as exc:answer=str(exc)
        elif text.startswith(('定义工作流：','定义工作流:')):
            content=text.replace('：',':',1).split(':',1)[1]
            parts=content.replace('｜','|').split('|',1)
            try:
                flow=self.runtime.workflows.define(parts[0],parts[1] if len(parts)>1 else '')
                answer='已保存工作流“'+flow['title']+'”，以后说“'+flow['trigger']+'”即可调用。'
            except (ValueError,IndexError) as exc:answer='没有保存工作流：'+str(exc)
        elif text.strip() in ('查看工作流','查看已学工作流'):
            rows=self.runtime.workflows.list()
            answer='\n'.join(x['id']+' · '+x['title']+' · 触发：'+x['trigger'] for x in rows) or '暂无工作流。'
        elif text.startswith('停用工作流 '):
            try:self.runtime.workflows.disable(text.split()[-1]);answer='工作流已停用。'
            except ValueError as exc:answer=str(exc)
        elif text.startswith('启用工作流 '):
            try:
                flow=self.runtime.workflows.activate(text.split()[-1])
                answer='工作流“'+flow['title']+'”已启用。'
            except ValueError as exc:answer=str(exc)
        elif execution_backend_command(text):
            backend=execution_backend_command(text)
            self.store.preference('execution_backend',backend)
            self.pet._set_executor_mode(backend)
            label={'self':'本地视觉智能体','dsh':'DSH 执行器','codex':'Codex 桌面端执行器',
                   'workbuddy':'WorkBuddy 桌面端执行器','auto':'自动分工（日常咕嘎，复杂工程 Codex）'}[backend]
            answer='后续执行任务使用 '+label+'；简单命令快通道保持。'
        if answer is None:return False
        self._last_personal_answer=answer
        if record_user:self.store.append(self.active_id,'user',text)
        self.store.append(self.active_id,'assistant',answer);self.status.setText(answer[:100])
        self._show_active_bubble(answer)
        return True

    def _answer_partial(self,ident,generation,text):
        if self._run_kind=='answer' and self.busy and ident==self.active_id and generation==self.answer.generation:
            if text.strip() and not self._answer_text:
                self.pet._multi_agent.work_state(self.active_agent_id,'idle','turn:'+ident)
            self._answer_text=text
            # 每个流式分片都 setHtml 重画整段对话，又慢又把滚动条顶回去；
            # 合并成 ~60ms 一次，既顺滑也不丢内容（结束时会再完整渲染一次）。
            self._render_soon()

    def _render_soon(self):
        timer=getattr(self,'_render_timer',None)
        if timer is None:
            # 2026-11-12：原来 60ms，比上游 chat_answer 的 90ms 分片间隔还短，
            # 定时器每次都已经空闲，合并等于没生效——每个分片都实打实全量重画一次。
            # 拉到 150ms（> 上游间隔）才是真合并；2026-11-12 晚再放宽到 250ms：
            # 全量 setHtml 每次都要重排整篇文档，频率降下来抖动感明显减轻。
            timer=QTimer(self);timer.setSingleShot(True);timer.setInterval(250)
            timer.timeout.connect(self.render);self._render_timer=timer
        if not timer.isActive():
            timer.start()

    def status_error(self,text):
        if self._bubble_voice_long_call and self.voice:
            self.voice.mute(False);self._bubble_voice_long_call=False
        self.status.setText(text);self._show_active_bubble(text)

    def _short_voice_state(self,state):
        self.voice_record_bar.set_state(state);self.voice_record_bar.setVisible(state!='idle')
        self.short_voice_button.setVisible(state=='idle')
        self.editor.setVisible(state=='idle');self.send_button.setVisible(state=='idle')
        bubble=getattr(getattr(self.pet,'_agent_bubble',None),'set_voice_state',None)
        if callable(bubble):bubble(state)
        if state=='idle':
            self._sync_manual_voice()
            return
        self.status.setText({'preparing':'正在准备离线语音识别…','recording':'正在录音 · 在录音条点 ↑ 发送，×取消','ready':'录音已结束，点击 ↑ 才会发送','transcribing':'正在识别…','idle':'就绪'}[state])

    def start_bubble_short_voice(self):
        if self.short_voice.active:return True
        if self.busy and self._run_kind=='answer':
            self._short_voice_state('idle');self.status_error('当前问题还在回答，稍后可以点击说话');return False
        self._bubble_voice_long_call=bool(self.voice_active and self.voice)
        self._manual_voice_turn=True;self._manual_resume_after=time.monotonic()+.4
        self._manual_voice_timer.start()
        if self._bubble_voice_long_call:
            self.voice.interrupt();self.voice.mute(True)
            self.voice.reserve_manual_microphone()
            self.short_voice.before_start=self.voice.wait_manual_microphone
        else:self.short_voice.before_start=None
        self.pet._tts.stop()
        accepted=self.short_voice.start()
        if not accepted:self._short_voice_state('idle')
        return accepted

    def finish_bubble_short_voice(self,cancel=False):
        if self.short_voice.active:self.short_voice.stop(cancel=bool(cancel))
        if cancel:
            self._sync_manual_voice();self.pet._agent_bubble.set_voice_state('idle')

    def toggle_short_voice(self):
        if not self.short_voice.active:self.start_bubble_short_voice()

    def _short_transcript(self,text):
        from core.desktop_transcript import normalize_transcript
        text=normalize_transcript(text)
        self._manual_voice_turn=True;self._manual_resume_after=time.monotonic()+.4
        self._manual_voice_timer.start()
        # 长语音通话中按住说话：走本地 driver + Edge 回复，通话保持不挂断。
        # 文字不能投回通话（submit_user_text）——那会打断这通电话的上下文，
        # 用户听到的就不是一次独立问答了。
        if self._bubble_voice_long_call and self.voice:
            self._bubble_voice_long_call=False;self.voice.mute(False)
        self.editor.setPlainText(text)
        settings=getattr(getattr(self.pet,'_config',None),'_s',None)
        if settings is not None and settings.value('experience/voice_draft',False,type=bool):
            self.pet._open_quick_input(text);return
        self.send(text,read_reply=True)

    def _sync_manual_voice(self):
        if not self._manual_voice_turn:return
        foreground=(self.busy and self._run_kind=='answer') or self.answer.active or self.dialogue.active or self._fast_inflight
        if self.short_voice.active or foreground or self.pet._tts.is_speaking or time.monotonic()<self._manual_resume_after:return
        self._manual_voice_turn=False;self._bubble_voice_long_call=False;self._manual_voice_timer.stop()
        if self.voice_active and self.voice:
            self.voice.resume_call_microphone();self.voice.mute(self.mute_button.isChecked())

    def _activity(self, ident, event):
        if ident==self.active_id and self._task_reporter:
            self._task_reporter.tool_result(event['name'],event.get('arguments',{}),event['result'])
        if ident==self.active_id and self._run_kind=='task':
            self.pet._multi_agent.work_state(self.active_agent_id,'thinking','turn:'+ident)
        result = event['result']
        label = {'app_state': '查看自身状态', 'pet_control': '操纵自身', 'workspace': '管理项目与会话',
                 'self_ask': '自己询问自己', 'ecosystem': '调用生态软件', 'applications': '授权软件操作'}.get(event['name'], event['name'])
        label += ' · ' + ('完成' if result.get('ok') else '未完成')
        text = label + '\n' + json.dumps(event['arguments'], ensure_ascii=False)
        text += '\n' + ('完成：' if result.get('ok') else '失败：') + json.dumps(result, ensure_ascii=False)[:6000]
        self.store.append(ident, 'tool', text)
        if ident == self.active_id:
            self.status.setText(label + (' · 已完成' if result.get('ok') else ' · 未完成'))
            self.render()

    def _task_progress(self,ident,event):
        if ident!=self.active_id:return
        state='working' if event.get('phase')=='tool_start' else 'thinking'
        if self._run_kind=='task':self.pet._multi_agent.work_state(self.active_agent_id,state,'turn:'+ident)
        if self._task_reporter and event.get('phase')=='tool_start' and event.get('name'):
            self._task_reporter.tool_start(event['name'],event.get('arguments',{}))
        summary=event.get('summary','正在执行任务')
        self._task_summary=summary
        if self.voice_active and self.voice:
            self.voice.set_task_context(dict(running=True,kind=self._run_kind,goal=self._active_task_text,summary=summary))
        self.status.setText(summary[:120])
        # Detailed task/model events stay inside the expandable execution log.

    def _task_heartbeat(self):
        self._flush_task_speech()
        reports=[]
        if self.busy and self._task_reporter:
            reports.append((self.active_agent_id,self.active_id,'task',self._task_reporter,None))
        reports.extend((item['agent_id'],item['conversation_id'],job,item['reporter'],item.get('brief'))
            for job,item in self._dsh_reports.items())
        if not reports:
            if not self._task_speech.pending:self._task_heartbeat_timer.stop()
            return
        for agent_id,ident,key,reporter,brief in reports:
            if self.voice_active and self.voice and self.voice.is_speaking:continue
            if brief is not None:
                self._dsh_wait_comfort(ident,key)
            summary=brief.watchdog() if brief is not None else reporter.notice()
            if not summary:continue
            beat=brief.beat if brief is not None else reporter.beat
            facts=reporter.snapshot(brief.goal if brief is not None else self._active_task_text)
            if brief is not None:facts.update(executor_claim=brief.summary,blockers=brief.blockers)
            self._request_public_report('heartbeat-'+key+'-'+str(self._task_started)+'-'+str(beat),facts,
                '这一步还没有确认完成，我先核对已有结果。',ident,
                source='dsh_progress' if brief is not None else 'local_progress',job=key,started=self._task_started)

    def _dsh_wait_comfort(self,ident,job):
        """First ≤5s sign of life while a DSH task is still starting.

        Host-side only: reuses the voice notice API that already carries the
        30s progress reports, so the qwen realtime layer is untouched. Two
        lines at most, ≥8s apart, never over the user, the model or music.
        """
        item=self._dsh_reports.get(job)
        if not item or not ident==self.active_id:return
        if not (self.voice_active and self.voice):return
        now=time.monotonic();item.setdefault('started',now)
        talks=item.setdefault('wait_talks',[])
        if len(talks)>=2 or now-item['started']<5:return
        if talks and now-talks[-1]<8:return
        if self.voice.is_speaking or getattr(self.voice,'_user_speaking',False):return
        try:
            if self.runtime.music.status().get('playing'):return
        except Exception:return
        line=('本鹅先把这件事拆成几步，第一步回执回来就告诉你。'
              if not talks else '还在后台推进，我先盯着；有结果马上说。')
        if self.voice.notify_battleground('wait-'+job+'-'+str(len(talks)+1),line,during_task=True):
            talks.append(now)

    def _queue_task_speech(self,key,text,important=False,conversation_id=None,agent_id=None):
        if self._task_speech.offer(key,text,important):
            owners=getattr(self,'_task_speech_owners',{})
            if conversation_id is not None:owners[key]=(conversation_id,agent_id)
            pending={item['key'] for item in self._task_speech.pending}
            self._task_speech_owners={k:v for k,v in owners.items() if k in pending}
            self._task_heartbeat_timer.start()
            self._flush_task_speech()

    def _flush_task_speech(self):
        use_realtime=bool(self.voice_active and self.voice and not self._manual_voice_turn and getattr(self.voice,'connected',False))
        speaking=bool(self.voice.is_speaking) if use_realtime else bool(self.pet._tts.is_speaking)
        # A real-time queue already waits for a break in the user's speech.
        item=self._task_speech.next(busy=speaking)
        if not item:return
        owner=getattr(self,'_task_speech_owners',{}).pop(item['key'],None)
        if owner is not None and owner!=(self.active_id,self.active_agent_id):return
        if use_realtime:
            self.voice.notify_battleground('task-'+item['key'],item['text'],during_task=True,persistent=item['important'])
        else:
            self._say_active_reply(item['text'],speak=True)

    def _battleground_event(self, event):
        ident = event.get('conversation_id')
        if not ident:
            return
        speech = event.get('speech')
        if speech:
            self.store.append(ident, 'system', speech)
            if self.active_id == ident:
                self.status.setText(speech[:100])
                self.render()
                if self.voice_active and self.voice:
                    self.voice.notify_battleground(event['event_id'], speech)
        elif event['type'] in ('action_confirmed','unconfirmed','input_sent','memory_snapshot'):
            self.store.append(ident, 'tool', '战棋后台 · '+json.dumps(event,ensure_ascii=False)[:6000])
            if self.active_id == ident:
                self.render()

    def _codex_event(self,event):
        """Manager signals arrive on the UI thread; never poll the desktop here."""
        if getattr(self,'_codex_closing',False):return
        if getattr(self,'_manager_enabled',False) and self.managed.codex_event(event):return
        kind=event.get('type')
        if kind not in ('codex_started','codex_progress','codex_completed','codex_failed'):return
        ident=event.get('conversation_id');owner=str(event.get('agent_id') or '')
        if not ident or not owner:return
        try:conversation=self.store.conversation(ident)
        except (KeyError,ValueError):return
        if not conversation or str(conversation.get('agent_id') or '001')!=owner:return
        tasks=getattr(self,'_codex_tasks',{});key=(ident,owner);previous=tasks.get(key,{})
        thread=str(event.get('thread_id') or '')
        state=str(event.get('state') or '')
        summary=str(event.get('summary') or '').strip()
        text=str(event.get('text') or '').strip();error=str(event.get('error') or '').strip()
        turn_id=str(event.get('turn_id') or '')
        signature=(kind,thread,turn_id,state,summary,text,error)
        if previous.get('signature')==signature:return
        if (previous.get('terminal') and previous.get('thread_id')==thread and kind=='codex_progress'
                and state not in ('starting','stop_requested') and turn_id==previous.get('turn_id','')):return
        terminal=state in ('completed','failed','stopped','cancelled','canceled','interrupted','aborted')
        if not state:terminal=kind in ('codex_completed','codex_failed')
        stopped=terminal and state in ('stopped','cancelled','canceled','interrupted','aborted')
        if stopped:message='Codex 已确认停止。\n'+(summary or error or text)
        elif kind=='codex_completed':message='Codex 已返回结果（待核验）：\n'+(text or summary or '未返回结果正文。')
        elif kind=='codex_failed':
            label='Codex 未完成' if terminal else 'Codex '+CODEX_STATES.get(state,'请求或监测异常，任务状态待确认')
            message=label+'：\n'+(error or summary or text or '未收到有效结果。')
        elif kind=='codex_started':message='Codex 已开始执行，尚未完成。\n'+summary
        else:message='Codex 进度：'+(summary or text or CODEX_STATES.get(state,'等待新进度。'))
        now=time.monotonic()
        announce=terminal or now-previous.get('last_notice',-1000)>=30
        tasks[key]=dict(thread_id=thread,turn_id=turn_id,state=state,summary=summary,signature=signature,
                        terminal=terminal,last_notice=now if announce else previous.get('last_notice',-1000))
        self._codex_tasks=tasks
        self.store.append(ident,'assistant' if terminal else 'tool',message)
        if ident!=self.active_id or owner!=self.active_agent_id:return
        manager=getattr(self.pet,'_multi_agent',None)
        if manager is not None:
            manager.work_state(owner,'cancelled' if stopped else 'result' if kind=='codex_completed'
                               else 'error' if kind=='codex_failed' else 'working','codex:'+(thread or ident))
        if not self.busy:self.status.setText(short_bubble_text(message,120))
        self.stop_button.setEnabled(bool(self.busy or self._codex_running() or self.runtime.music.requested))
        self.render()
        if not announce:return
        notice=short_bubble_text(message)
        self._show_active_bubble(notice)
        if self.voice_active and self.voice:
            self.voice.set_task_context(dict(running=not terminal,kind='codex',summary=notice))
        if self.voice_active or self.read_aloud.isChecked():
            self._queue_task_speech('codex-'+ident+'-'+str(now),notice,terminal,
                                    conversation_id=ident,agent_id=owner)

    def _dsh_event(self,event):
        ident=event.get('conversation_id')
        if not ident:return
        if event.get('type')=='dsh_event':
            item=event.get('event') or {};phase=item.get('phase')
            fact=web_fact_from_tool_event(item)
            if fact:
                self._web_facts=fact
                if self.voice_active and self.voice:
                    self.voice.set_web_context(dict(fact))
            if self.voice_active and self.voice:
                self.voice.set_task_context(dict(running=True,kind='dsh',goal=self._active_task_text,
                    summary=item.get('summary') or item.get('message') or phase or '后台工程任务处理中'))
            from core.task_feedback import EvidenceProgress
            from core.task_brief import DshTaskBrief
            job=event.get('job_id','')
            if job not in self._dsh_reports:
                self._dsh_reports[job]=dict(agent_id=event.get('agent_id') or self.active_agent_id,
                    conversation_id=ident,reporter=EvidenceProgress(interval=30),
                    brief=DshTaskBrief(job,self.runtime.dsh.state.get('goal','')),
                    started=time.monotonic(),wait_talks=[])
            reporter=self._dsh_reports[job]['reporter']
            brief=self._dsh_reports[job].get('brief')
            if brief is not None:brief.observe(item)
            if item.get('type')=='status' and phase in ('turn_start','step_start','step_end'):
                self.pet._multi_agent.work_state(self._dsh_reports[job]['agent_id'],
                    'working' if phase=='step_start' else 'thinking','dsh:'+job)
            if item.get('type')=='status' and phase in ('turn_start','step_start'):
                reporter.pending='执行器已进入新步骤，等待具体操作回执'
            receipt=item.get('receipt')
            if isinstance(receipt,dict):reporter.tool_result(receipt['name'],receipt.get('arguments',{}),receipt['result'])
            if item.get('type') in ('tool_receipt','report'):
                self.store.append(ident,'tool','DSH · '+json.dumps(item,ensure_ascii=False)[:2000])
            if item.get('type')=='report' and ident==self.active_id and item.get('state')!='ready':
                summary=item.get('summary','')
                important=item.get('state') in ('needs_input','blocked','failed','ready') or item.get('milestone',False)
                self.status.setText(summary[:120])
                if item.get('state')=='needs_input':
                    # Preserve the exact question/options; do not delay a decision behind narration.
                    self._queue_task_speech(job+'-needs-input-'+summary[:80],summary,True)
                else:
                    self._request_public_report(job+'-'+item.get('state','')+'-'+summary[:80],
                        dict(goal=self.runtime.dsh.state.get('goal',''),executor_claim=summary,
                             state=item.get('state'),blockers=item.get('blockers',[]),independently_verified=False),
                        '这一步需要核对，我先确认具体卡点。',ident,source='dsh_progress',job=job,terminal=important)
            elif item.get('type')=='status' and phase=='turn_start' and ident==self.active_id:
                self._queue_task_speech(job+'-start','DSH 已开始处理，我会在重要步骤完成或遇到困难时告诉你。')
            self._task_heartbeat_timer.start()
            if self.active_id==ident and item.get('type')!='report':self.status.setText('DSH 后台任务执行中…')
            return
        if event.get('type')=='dsh_completed':
            job_id=event.get('job_id','')
            report=self._dsh_reports.pop(job_id,None)
            if report and report.get('brief') is not None:
                report['brief'].complete(bool(event.get('ok')),self.runtime.dsh.status().get('ticket_state',''),
                    event.get('text') or event.get('error') or '')
            agent_id=event.get('agent_id') or (report or {}).get('agent_id') or self.active_agent_id
            ok=bool(event.get('ok'));text=event.get('text') or event.get('error') or 'DSH 任务未返回结果'
            self.pet._multi_agent.work_state(agent_id,'idle' if event.get('needs_input') else 'result' if ok else 'error',
                                             'dsh:'+event.get('job_id',''))
            if ok:text='执行器返回（尚未独立核验）：\n'+text
            if self._run_kind=='dsh' and ident==self.active_id:
                self._task_speech.clear()
                self._completed(dict(kind='dsh',conversation_id=ident,ok=ok,text=text,error=event.get('error',''),
                                     expression='normal',expression_explicit=False,action='none',needs_input=event.get('needs_input',False)))
                from core.file_delivery import extract_paths
                if not extract_paths(event.get('text','')):
                    self._request_public_report(event['job_id']+'-done',
                        dict(goal=self.runtime.dsh.state.get('goal',''),executor_claim=event.get('text',''),
                             error=event.get('error',''),execution_finished=True,independently_verified=False),
                        '这一轮已经返回结果，我来核对具体内容。' if ok else '这次还有一步没有完成，原因已记在对话里。',
                        ident,source='dsh_final',terminal=True)
                ticket=self.runtime.dsh.state['ticket_id']
                pending=self.runtime.dsh.archive.pending_inputs(ticket) if not self.runtime.dsh.stop_event.is_set() else []
                if pending:
                    def followup():
                        if self.busy:return
                        if self.send('继续处理用户保存的追加要求：\n'+'\n'.join(x['content'] for x in pending),record_user=False,force_agent=True):
                            self.runtime.dsh.archive.acknowledge_inputs(ticket,[x['id'] for x in pending])
                    QTimer.singleShot(150,followup)
                return
            self.store.append(ident,'assistant' if ok else 'system',text)
            self._remember_dialogue(agent_id,'assistant',text,ident)
            self.pet._multi_agent.work_state(agent_id,'result' if ok else 'error','dsh:'+event.get('job_id',''))
            if self.active_id==ident:
                self.status.setText('后台执行器已返回 · 待核验' if ok else 'DSH 任务未完成')
                self._show_active_bubble(text);self.render()
            if self.voice_active and self.voice and ident==self.active_id and agent_id==self.active_agent_id:
                self.voice.set_task_context(dict(running=False,kind='dsh',summary=text))
                self.voice.notify_battleground(event.get('job_id','')+'done',text[:800])

    def _music_event(self,event):
        playing=event.get('type')=='playing'
        self.music_listener.enable(bool(playing and self.voice_active))
        self.stop_button.setEnabled(bool(self.busy or self.runtime.music.requested or self.runtime.music.preparing or self._codex_running()))
        # Music owns both layers; switching appearance must not lose the stage.
        team=getattr(self.pet,'_multi_agent',None)
        companion=team.companions.get(self.active_agent_id) if team else None
        animators=[companion.anim] if companion and playing else [self.pet._anim2d,self.pet._anim3d]
        if not playing and team:animators.extend(c.anim for c in team.companions.values())
        for animator in animators:
            if animator is None: continue
            if event.get('type')=='preparing':continue
            if hasattr(animator,'set_music_stage'):animator.set_music_stage(playing)
            else:
                animator.dancing=playing
                if playing:
                    animator.sunglasses=True;animator._sunglasses_until=0
                else:animator.sunglasses=False
        if playing: self.pet._tts.stop()
        if self.voice:
            self.voice._external_playing=playing
            if playing:self.voice.interrupt()
            else:self.voice._echo_until=time.monotonic()+.5
        text=('正在'+('循环' if self.runtime.music.repeat else '')+'播放咕嘎翻唱《'+event.get('title','')+'》 · 说“停止唱歌”或右键停止') if playing else (
            '正在制作咕嘎音色翻唱，完成后再播放' if event.get('type')=='preparing' else (
            '歌曲播放失败：'+event.get('error','') if event.get('type')=='error' else '已停止唱歌，可以继续聊天'))
        if event.get('type')=='interaction':
            text='唱到这里歇一会儿～想聊什么？说“继续唱歌”我再接着唱。'
            if self.voice_active and self.voice:
                self.voice.notify_battleground('music-interaction-'+str(time.monotonic()),text)
        self.status.setText(text);self._show_active_bubble(text);self.pet.update()

    def _completed(self, result):
        if result.get('kind')=='answer' and result.get('generation',self.answer.generation)!=self.answer.generation:return
        ident = result['conversation_id']
        was_task=self._run_kind in ('task','dsh')
        self.busy = False
        self._answer_text='';self._run_kind=None
        workflow=self._active_workflow;self._active_workflow=None
        route=self._active_route;self._active_route=None
        task_text=self._active_task_text;self._active_task_text=''
        if workflow and result.get('kind')!='answer' and not result.get('needs_input'):
            try:self.runtime.workflows.record_run(workflow,bool(result.get('ok')),
                result.get('text') or result.get('error') or '')
            except Exception:pass
        if (task_text and result.get('ok') and result.get('kind')!='answer' and
                (route or {}).get('executor')=='local_agent'):
            try:
                events=self.agent.journal.data.get('events',[])
                learned=self.runtime.workflows.observe_local_success(task_text,
                    result.get('text',''),[x.get('tool') for x in events if x.get('ok')])
                if learned:self.store.append(ident,'system','已形成工作流候选：“'+learned['title']+'”。说“启用工作流 '+learned['id']+'”才会用于下次。')
            except Exception:pass
        self._task_control.reset()
        self._task_reporter=None
        # 桌面空出来了：把之前被挡下的「打开X」补上
        QTimer.singleShot(0,self._replay_pending_launch)
        if not self._dsh_reports:self._task_heartbeat_timer.stop()
        self.send_button.setEnabled(True)
        self.new_button.setEnabled(not self.voice_active)
        self.stop_button.setEnabled(bool(self.runtime.music.requested or self._codex_running()))
        if result.get('kind')!='answer' and self.agent.cancel_event.is_set() and not result.get('timed_out'):
            result = dict(result, ok=False, error='本轮已停止，已完成的操作保留在记录中')
        if was_task:
            state='idle' if result.get('needs_input') else 'cancelled' if self.agent.cancel_event.is_set() else 'success' if result.get('ok') else 'error'
            self.pet._multi_agent.work_state(self.active_agent_id,state,'turn:'+ident)
        else:self.pet._multi_agent.work_state(self.active_agent_id,'idle','turn:'+ident)
        manual_reply=bool(result.get('read_aloud'))
        voice_task = None if manual_reply else self._voice_task
        if was_task and self.voice_active and self.voice:
            self.voice.set_task_context(dict(running=False,kind='local',summary=result.get('text') or result.get('error') or '任务结束'))
        if not manual_reply:self._voice_task = None
        if voice_task and self.voice_active and voice_task[0] is self.voice:
            self._voice_readback = True
            from core.companion_dialogue import speech_answer
            self.voice.resolve_task(voice_task[1], dict(ok=result['ok'],
                answer=speech_answer(result.get('text','')) if result['ok'] else '', error=result.get('error', '')))
        elif self.voice_active and self.voice and not manual_reply:
            self._voice_readback=True
            from core.companion_dialogue import speech_answer
            self.voice.notify_battleground('task-finished-'+result.get('task_id',ident),
                ('本轮回答：' if result.get('kind')=='answer' else '任务执行结果：')+
                (speech_answer(result.get('text','')) if result['ok'] else result.get('error','未完成')))
        if result['ok']:
            self.store.append(ident, 'assistant', result['text'])
            self._remember_dialogue(self.store.conversation(ident)['agent_id'],'assistant',result['text'],ident)
            voice_reply=(self.read_aloud.isChecked() or manual_reply) and (not self.voice_active or manual_reply) and result.get('kind')!='dsh'
            bubble=short_bubble_text(result['text'])
            if not self.voice_active and not voice_reply:
                self._show_active_bubble(bubble)
            self.status.setText('●  就绪')
            expression = result['expression'] if result.get('expression_explicit',result['expression']!='normal') else None
            from core.embodiment import reply_performance
            expression, action = reply_performance(result['text'], expression, result['action'])
            if voice_reply:
                from core.companion_dialogue import speech_answer
                spoken=speech_answer(result['text']) if result.get('kind')=='answer' else result['text']
                self._say_active_reply(bubble,expression,action,True,ident,spoken_text=spoken)
            else:
                if expression is not None:self._active_expression(expression)
                if action != 'none':
                    if self.voice_active:self.pet._queue_voice_gesture(action)
                    else:self._active_gesture(action)
        else:
            detail=result.get('error', '本轮未完成')
            prefix='结果尚待核对：' if result.get('outcome')=='unconfirmed' else '等待你的决定：' if result.get('needs_input') else '这次没有完成：'
            self.store.append(ident, 'assistant', prefix+detail)
            # 2026-11-12：失败**不再写进长期记忆**。主人明确要求：长记忆只收
            # 经验性和长工作流，单次失败的流水账不该反复出现在后面的对话里
            # （会被旧错误拖住、答非所问）。改成归纳一条简短经验候选进"进化史"。
            self._record_failure_lesson(ident, detail, result)
            self.status.setText(result.get('error', '本轮未完成'))
            self._show_active_bubble(result.get('error','本轮未完成'))
            if (not self.voice_active or manual_reply) and (self.read_aloud.isChecked() or manual_reply):
                self._queue_task_speech('failure-'+result.get('task_id',ident),result.get('error','本轮未完成'),important=True)
        from core.file_delivery import extract_paths, wants_file
        if was_task and result.get('ok') and (wants_file(task_text) or extract_paths(result.get('text',''))):
            since=time.time()-max(0,time.monotonic()-self._task_started) if self._task_started else time.time()
            self._begin_result_delivery(ident,task_text,result.get('text',''),
                self.runtime.dsh.state.get('cwd') if result.get('kind')=='dsh' else str(Path(__file__).resolve().parents[1]),
                since,result.get('task_id') or ident+'-'+str(self._task_started))
        if self.active_id == ident:
            self.render()

    def stop_turn(self):
        if self.busy and self._run_kind == 'answer':
            self.answer.cancel()
            return
        codex_stop=self._stop_codex()
        self._task_speech.clear()
        self.runtime.media.stop()
        if codex_stop is None:self.pet._multi_agent.work_state(self.active_agent_id,'cancelled','turn:'+self.active_id)
        self.answer.cancel();self.short_voice.stop(cancel=True);self.runtime.music.stop()
        self.runtime.battleground.stop()
        self.runtime.dsh.stop()
        self.agent.cancel()
        self.pet._tts.stop()
        if self.voice_active:
            self.voice_button.setChecked(False)
        if codex_stop is not None:
            rejected=codex_stop.get('ok') is False or codex_stop.get('accepted') is False
            self.status.setText('Codex 停止请求未受理：'+str(codex_stop.get('error') or '请查看任务状态') if rejected
                                else '已请求 Codex 停止任务，等待真实状态确认')
        else:self.status.setText('已请求停止 · 不再执行后续操作')
        self.stop_button.setEnabled(bool(self.busy or self._codex_running()))

    def toggle_call_screen_share(self, enabled):
        if not enabled:
            self._screen_share_manual_stop = True
            self._stop_call_screen_share()
            self.status.setText('已停止共享屏幕')
            return
        if not self.voice_active or not self.voice or not self.voice.connected:
            self._set_call_share_control(False)
            self.status.setText('通话接通后可以共享屏幕')
            return
        if self._native_video_call:
            try:
                self.runtime.screen_share.start(2, reason='seed_call', native_video=True)
                self._set_call_share_control(True)
                self.status.setText('正在共享前台窗口画面 · 切换到要分享的窗口')
                self._show_active_bubble(self.status.text())
            except Exception as exc:
                self._set_call_share_control(False)
                self.status_error('共享屏幕未开始：' + str(exc))
        else:
            self._start_call_screen_share()
            self._set_call_share_control(bool(self.runtime.screen_share.status().get('active')))

    def _set_call_share_control(self, enabled):
        button = self.voice_share_button
        previous = button.blockSignals(True)
        button.setChecked(enabled)
        button.setText('停止共享' if enabled else '共享屏幕')
        button.blockSignals(previous)
        bubble = self.pet._agent_bubble
        if hasattr(bubble, 'set_screen_sharing'):
            bubble.set_screen_sharing(enabled)

    def _call_screen_failed(self, message):
        if self.voice_active and self._native_video_call:
            self._stop_call_screen_share()
            self.status.setText(message)
            self._show_active_bubble(message)

    def _start_call_screen_share(self):
        if os.environ.get('QT_QPA_PLATFORM')=='offscreen':
            self._screen_share_call_owned=False
            return
        self._screen_share_manual_stop=False;self._screen_share_retry_at=0.0
        self._screen_share_call_owned=True
        self._screen_share_start_error=''
        self._screen_share_recovery_failures=0
        self._screen_share_watchdog_timer.start()
        try:
            state=self.runtime.screen_share.status()
            if not state.get('active'):self.runtime.screen_share.start(reason='call')
            self._set_call_share_control(True)
        except Exception as exc:
            self._record_screen_share_failure(exc)

    def _record_screen_share_failure(self,exc):
        from core.desktop_access import redact
        self._screen_share_start_error=type(exc).__name__+': '+redact(str(exc))[:240]
        self._screen_share_recovery_failures=getattr(self,'_screen_share_recovery_failures',0)+1
        self.status.setText('长通话已启用，但屏幕辅助启动失败：'+self._screen_share_start_error)
        # Retain only lifecycle diagnostics, never frames or window titles.
        try:
            path=SCREEN_SHARE_DIAGNOSTICS_PATH
            path.write_text(json.dumps(dict(at=time.time(),error=self._screen_share_start_error,
                failures=self._screen_share_recovery_failures),ensure_ascii=False),encoding='utf-8')
        except OSError:pass

    def _stop_call_screen_share(self):
        self._screen_share_watchdog_timer.stop()
        self._screen_share_pending=False
        try:
            if self.runtime.screen_share.status().get('active'):self.runtime.screen_share.stop()
        except Exception:pass
        self._screen_share_call_owned=False
        self._set_call_share_control(False)

    def _screen_share_watchdog(self):
        if not self.voice_active or not self._screen_share_call_owned or self._screen_share_manual_stop:return
        state=self.runtime.screen_share.status()
        if state.get('active') and state.get('sequence',0)>0 and (state.get('frame_age_seconds') or 0)<5:return
        if getattr(self,'_screen_share_recovery_failures',0)>=3:
            self._screen_share_watchdog_timer.stop()
            return
        now=time.monotonic()
        if now<self._screen_share_retry_at:return
        self._screen_share_retry_at=now+30
        try:
            if state.get('active'):self.runtime.screen_share.stop()
            self.runtime.screen_share.start(reason='call_recovery')
            self._screen_share_start_error=''
        except Exception as exc:
            self._record_screen_share_failure(exc)

    def toggle_voice(self, enabled):
        self.voice_controls.setVisible(bool(enabled))
        if enabled:
            if self.short_voice.active:
                self.status_error('请先发送或取消录音，再开启实时通话')
                self.voice_button.blockSignals(True);self.voice_button.setChecked(False);self.voice_button.blockSignals(False)
                return
            if self.pet._realtime_active() or getattr(self.pet, '_recording', False):
                self.status.setText('请先结束原语音通话，再开启智能体语音')
                self.voice_button.setChecked(False)
                return
            if self.voice and self.voice.active:
                self.status.setText('上一段语音正在释放麦克风，请稍后重试')
                self.voice_button.setChecked(False)
                return
            self.pet._voice_mode = False
            from core.realtime_voice import load_settings, connection_url, SEED_PROVIDER
            from ui.voice_dialog import VoiceSettingsDialog
            cfg = load_settings()
            try:
                connection_url(cfg)
            except ValueError:
                if VoiceSettingsDialog(self).exec_() != QDialog.Accepted:
                    self.voice_button.setChecked(False)
                    return
                cfg = load_settings()
            self.voice_active = True
            self._voice_departing=False;self._voice_farewell_started=0.0
            from core.desktop_interaction import DesktopInteraction
            if getattr(self,'_browser_interaction',None) is None:self._browser_interaction=DesktopInteraction()
            self._browser_interaction.cancel()
            self._browser_interaction.prewarm()
            self._voice_readback = False
            if self.voice:
                self.voice.deleteLater()
            self._native_video_call = cfg.get('provider') == SEED_PROVIDER
            if self._native_video_call:
                from core.seed_realtime_voice import SeedRealtimeVoice
                self.voice = SeedRealtimeVoice(self)
                self.voice.screen_frame_provider = self.runtime.screen_share.native_frame
            else:
                self.voice = AgentRealtimeVoice(self)
                self.voice.screen_context_provider = self.runtime.screen_share.assist_context
            self.voice.screen_question_requested.connect(self._voice_screen_question)
            # Fresh call: no leftover task, browser fact or failure report from a
            # previous session. Only a task that is genuinely running right now
            # may be described, otherwise the pet starts by re-litigating old errors.
            self.voice.set_task_context(dict(running=bool(self.busy),
                kind=(self._run_kind if self.busy else ''),
                goal=(self._active_task_text if self.busy else ''),
                summary=(self._task_summary if self.busy else '')))
            external=getattr(self.runtime,'dsh_link',None)
            if not self.busy and external is not None:
                context=external.active_context()
                if context:self.voice.set_task_context(context)
            self._last_fast_failure=None
            self.voice.set_web_context(None)
            self.voice._music_stop_listener=self.music_listener
            self.voice.agent_profile=dict(self.active_profile)
            self.voice.long_term_memory=self._voice_memory()
            # A long call is a fresh context. Loading the last 40 chat messages
            # here made every new call drag in the previous call's failures.
            self.voice.context_history=[]
            self.voice.task_requested.connect(self._voice_request)
            self.voice.tasks_cancelled.connect(self._voice_cancelled)
            self.voice.transcript.connect(self._voice_transcript)
            self.voice.preview.connect(self._voice_preview)
            self.voice.gesture.connect(lambda name:self._active_gesture(name) if self.active_agent_id!='001' else self.pet._queue_voice_gesture(name))
            self.voice.expression_requested.connect(lambda name:self._active_expression(name))
            self.voice.fast_command.connect(self._fast_command)
            self.voice.status.connect(self.status.setText)
            self.voice.failed.connect(self._voice_error)
            self.voice.finished.connect(self._voice_finished)
            self.voice.connected_changed.connect(self._voice_connected_changed)
            self.voice.text_only_response.connect(self._voice_text_only)
            self.voice.speaking.connect(self.pet._on_realtime_speaking)
            if self.pet._voice_call_panel:
                self.pet._voice_call_panel.close()
                self.pet._voice_call_panel.deleteLater()
                self.pet._voice_call_panel = None
            self.pet._realtime_voice = self.voice
            self.pet._tts.stop()
            self.new_button.setEnabled(False)
            self.voice_settings_button.setEnabled(False)
            self.mute_button.setEnabled(True)
            self.interrupt_button.setEnabled(True)
            self.read_aloud.setEnabled(False)
            self.voice_button.setText('结束长对话')
            self.pet._agent_bubble.set_call_reconnecting(self.active_id, initial=True)
            # Capture is an auxiliary context source. Queue it for the connected
            # callback so a screen grab can never sit on the handshake path and
            # a capture failure can never abort a call that is still connecting.
            self._screen_share_pending=not self._native_video_call
            self.voice.start(cfg)
            if self.voice_active:
                self.proactive.start()
            self.voice._external_playing=self.runtime.music.playing
            self.music_listener.enable(bool(self.runtime.music.playing))
        else:
            self.music_listener.enable(False)
            self.voice_active = False
            self._stop_call_screen_share()
            self.pet._pending_voice_gesture=None
            self.agent.cancel()
            self.runtime.battleground.stop()
            self.runtime.dsh.stop()
            if self.voice:
                self.voice.stop()
            self._voice_cancelled()
            self.new_button.setEnabled(not self.busy)
            self.voice_settings_button.setEnabled(True)
            self.mute_button.setEnabled(False)
            self.mute_button.setChecked(False)
            self.interrupt_button.setEnabled(False)
            self.read_aloud.setEnabled(True)
            self.voice_button.setText('实时语音')
            self.pet._agent_bubble.set_call_active(False,self.active_id)

    def _voice_error(self, message):
        self._release_failed_voice()
        self.pet._agent_bubble.set_call_active(False, self.active_id)
        message=str(message)+'；长语音已降级，仍可点击说话，使用驱动模型 + Edge TTS。'
        self.status.setText(message)
        self._show_active_bubble(message)

    def _release_failed_voice(self):
        """A transport failure must never cancel independently running jobs."""
        self.voice_active=False
        self._stop_call_screen_share()
        self.voice_button.blockSignals(True);self.voice_button.setChecked(False);self.voice_button.blockSignals(False)
        self.voice_button.setText('实时语音');self.voice_controls.hide()
        if self.voice:self.voice.stop()
        self.new_button.setEnabled(not self.busy);self.voice_settings_button.setEnabled(True)
        self.mute_button.setEnabled(False);self.interrupt_button.setEnabled(False);self.read_aloud.setEnabled(True)

    def _voice_connected_changed(self, connected):
        if not self.voice_active:return
        if connected:
            self.status.setText('持续通话中 · 可以直接说话')
            self.pet._agent_bubble.set_call_active(True,self.active_id)
            if getattr(self,'_screen_share_pending',False):
                self._screen_share_pending=False
                self._start_call_screen_share()
        else:
            # Not the end of the call: the worker reconnects. Show a reconnecting
            # state, never "通话已结束", otherwise a 2-second network hiccup looks
            # like the user's long call was terminated.
            self.status.setText('语音连接中断，正在重连…（此间本地播报可用）')
            self.pet._agent_bubble.set_call_reconnecting(self.active_id)

    def _voice_text_only(self, text):
        """Server returned text without audio for this response: speak once locally."""
        if not self.voice_active or not text:return
        from core.companion_dialogue import speech_answer
        from core.embodiment import spoken_reply
        spoken=speech_answer(spoken_reply(str(text))[2])
        if not spoken:return
        try:
            if self.pet._tts.is_speaking:return
            self.pet._tts.speak(spoken)
        except Exception:
            return
        self.status.setText('本轮语音模型未返回音频，已用本地语音播报')

    def open_voice_settings(self):
        from ui.voice_dialog import VoiceSettingsDialog
        VoiceSettingsDialog(self).exec_()

    def _voice_screen_question(self,call_id,question):
        """Answer a screen question. Shared frames when running, otherwise one
        on-demand look: controls first, then OCR, then vision. Never demands
        that the user turn sharing on first."""
        import threading
        voice=self.voice
        if not self.voice_active or not voice or voice._stop.is_set():return
        if call_id in self._screen_answers_inflight:return
        if len(self._screen_answers_inflight)>=2:
            voice.resolve_task(call_id,dict(ok=False,answer='正在读取上一张共享画面，请稍候。'));return
        shared=self.runtime.screen_share
        if not shared.status().get('active'):
            self._screen_answers_inflight[call_id]=voice
            def look_work():
                try:
                    from core.quick_look import look
                    value=look(question)
                    result=dict(ok=bool(value.get('ok')),
                        observation=value.get('text') or value.get('error') or '',
                        screen_read=True,on_demand=True,source=value.get('level'),
                        level=value.get('level'),cost=value.get('cost'),
                        elapsed_ms=value.get('elapsed_ms'),window=value.get('window'),
                        notes=value.get('notes'),
                        interpretation='这是刚刚按需看一眼得到的画面事实，不是实时共享。请结合用户原问题直接回答；内容为空或读不到就直说这次没读到，不要凭印象描述。')
                    if not value.get('ok'):result['answer']='这次没读到画面：'+str(value.get('error','未知原因'))
                except Exception as exc:
                    result=dict(ok=False,answer='按需读取画面失败：'+type(exc).__name__)
                self.screen_answer_ready.emit(dict(call_id=call_id,voice=voice,result=result))
            threading.Thread(target=look_work,name='guga-quick-look',daemon=True).start()
            return
        context=shared.assist_context()
        graphical=any(word in question for word in ('颜色','图形','图片','图像','视频内容','长什么样','表情','形状','红色','蓝色','黄色'))
        local=shared.local_context()
        if local.get('fresh') and not graphical:
            voice.resolve_task(call_id,dict(ok=True,observation=local['context'],screen_read=True,
                source='windows_uia',local_only=True,visual_content=False,
                interpretation='这些是刚从当前窗口实读的文字、控件和输入值；据此回答页面/按钮/文字问题。图片、视频和颜色尚未观察，不能猜。'))
            return
        # General scene questions can use already available perception immediately.
        general=any(w in question for w in ('看得见','看得到','能看到','能看见','屏幕上是什么','屏幕是什么','屏幕里面','画面是什么'))
        detail=any(w in question for w in ('编号','数字','多少','文字','第几个','读出来'))
        if context.get('fresh') and general and not detail and (context.get('source')!='windows_uia' or not graphical):
            voice.resolve_task(call_id,dict(ok=True,observation=context['context'],screen_read=True,cached=True,
                interpretation='这是当前画面事实，由你根据用户原话直接语音回答。屏幕属于用户，不要反问用户能否看见你的屏幕。'))
            return
        self._screen_answers_inflight[call_id]=voice
        def work():
            try:
                frame=shared.snapshot()
                if not frame.get('ok') or frame.get('frame_age_seconds',99)>2:
                    result=dict(ok=False,answer='屏幕共享已开启，最新画面尚未就绪；本次没有猜测画面。')
                else:
                    from core.screen_visual import observe_frame
                    value=observe_frame(frame['_image_url'],question,
                        allow_primary=time.monotonic()>=shared._primary_vision_backoff_until)
                    with shared._lock:shared._add_usage(value.get('usage'))
                    result=dict(ok=bool(value.get('ok')),observation=value.get('text') or '',
                        screen_read=True,sequence=frame.get('sequence'),fresh_frame=True,
                        interpretation='这是当前画面的观察事实，不是代写的回答。请结合用户原问题，由你直接用自然语音回答。')
                    if not value.get('ok'):result['answer']='共享画面读取未完成：'+str(value.get('error','未知错误'))
            except Exception as exc:result=dict(ok=False,answer='屏幕共享读取遇到异常：'+type(exc).__name__)
            self.screen_answer_ready.emit(dict(call_id=call_id,voice=voice,result=result))
        threading.Thread(target=work,name='guga-voice-screen-answer',daemon=True).start()

    def _deliver_screen_answer(self,event):
        call_id=event['call_id'];voice=event['voice']
        self._screen_answers_inflight.pop(call_id,None)
        if voice is not self.voice or not self.voice_active or voice._stop.is_set() or call_id not in voice._tasks:return
        result=event['result']
        if result.get('on_demand'):
            label={'uia':'本机控件','ocr':'本机识字','cloud_ocr':'云端识字','vision':'画面识别'}.get(result.get('level'),'画面')
            self.status.setText(('按需看了一眼（'+label+'）') if result.get('ok') else '这次没读到画面')
        else:
            self.status.setText('已读取当前共享画面' if result.get('ok') else '当前画面读取未完成')
        # Only Qwen's actual speech transcript becomes the visible reply.
        if not voice.resolve_task(call_id,result):self.status.setText('画面已读取，但语音结果未能入队；请检查语音状态')

    def _voice_request(self, call_id, request):
        if not self.voice_active or self.sender() is not self.voice:
            return
        staged = self._open_staged_task(request) if not self._pending_image else ''
        if staged:
            self.status.setText(staged)
            self._show_active_bubble(staged)
            self.voice.resolve_task(call_id, dict(ok=True, answer=staged, execution_pending=False))
            return
        if getattr(self,'_manager_enabled',False):
            from core.chat_answer import uses_task
            from core.fast_commands import match
            if (not self._pending_alignment and not uses_task(request,'auto',bool(self._pending_image))
                    and not match(request).get('matched') and codex_command(request) is None):
                self._start_dialogue(request,call_id=call_id);return
            accepted=self.send(request,record_user=False,use_attachment=True,execution_context={'_direct_user_request':True})
            self.voice.resolve_task(call_id,dict(ok=accepted is not False,answer=self.status.text(),execution_pending=self._codex_running()))
            return
        if not self._pending_image and execution_backend_command(request):
            self._personal_command(request,False)
            self.voice.resolve_task(call_id,dict(ok=True,answer=self._last_personal_answer))
            return
        if self._handle_codex_command(request,False,image=self._pending_image) is not None:
            self.voice.resolve_task(call_id,dict(ok=True,answer=self._last_personal_answer))
            return
        if self._pending_alignment:
            from core.task_alignment import reply_choice
            choice,supplement=reply_choice(request)
            if choice is not None:
                accepted=self._choose_alignment(self.active_id,choice,supplement)
                self.voice.resolve_task(call_id,dict(ok=bool(accepted),answer='已按你的选择处理。' if accepted else '请在原任务对话中选择。'))
                return
        confirmed=False
        try:
            from core.reflex import match as match_reflex
            reflex_command=match_reflex(request)
        except Exception:
            reflex_command=dict(matched=False)
        if reflex_command.get('matched') and not reflex_command.get('need_model') and not self._pending_image:
            self._pending_execution=None
            self.voice._dispatch_fast(reflex_command,call_id)
            return
        from core.fast_commands import match
        fast=match(request)
        if fast.get('matched') and not self._pending_image:
            self._pending_execution=None
            self.voice._dispatch_fast(fast,call_id)
            return
        from core.chat_answer import uses_task
        from core.companion_dialogue import is_task_followup,is_task_status
        if (not self._pending_execution and not self._pending_image and
            not uses_task(request,'auto') and not is_task_followup(request) and not is_task_status(request)):
            if not self.busy and self._personal_command(request,False):
                self.voice.resolve_task(call_id,dict(ok=True,answer=self._last_personal_answer));return
            self._start_dialogue(request,call_id=call_id)
            return
        if not self.busy and self._pending_execution:
            pending=self._pending_execution
            if time.monotonic()-pending['created']>180:
                self._pending_execution=None
            else:
                value=str(request or '').strip().strip('。！! ')
                if value in ('确认执行','确认','对','是的','好的','开始','执行','可以'):
                    self._pending_execution=None
                    request=pending['text']
                    confirmed=True
                    self.store.append(self.active_id,'system','已确认任务理解，按当前路由开始执行。')
                elif value in ('取消','不对','先不要','算了','不执行'):
                    self._pending_execution=None
                    answer='已取消，不会启动这个复杂任务。'
                    self.store.append(self.active_id,'system',answer)
                    self._show_active_bubble(answer)
                    self.voice.resolve_task(call_id,dict(ok=True,answer=answer))
                    return
        if self.busy:
            control=self._task_control.observe(request,busy=True)
            if control['action'] in ('ask_stop','pending'):
                answer='当前任务仍在执行。确定要停止吗？请说“停止”确认，或说“继续”任务。'
                self.status.setText(answer)
                self.store.append(self.active_id,'system',answer)
                self._show_active_bubble(answer)
                self.voice.resolve_task(call_id,dict(ok=True,answer=answer))
            elif control['action']=='cancel':
                self._cancel_execution()
                answer='已请求停止当前任务，等待执行器确认；已经完成的操作保留在记录中。'
                self.store.append(self.active_id,'system',answer)
                self.voice.resolve_task(call_id,dict(ok=True,answer=answer))
            elif control['action']=='continue':
                answer='好，继续当前任务。'
                self.status.setText(answer)
                self.store.append(self.active_id,'system',answer)
                self.voice.resolve_task(call_id,dict(ok=True,answer=answer))
            elif (answer:=self._running_task_answer(request)):
                self.status.setText(answer[:120])
                self.store.append(self.active_id,'system',answer)
                self._show_active_bubble(answer)
                self.voice.resolve_task(call_id,dict(ok=True,answer=answer))
            else:
                self._steer_execution(request)
                self.voice.resolve_task(call_id,dict(ok=True,
                    answer='收到，这句会作为当前任务的补充要求保留；我没有把它当成新任务，也不会中断正在执行的步骤。'))
            return
        if not self._pending_image and not confirmed and self._offer_alignment(request,False,force_agent=True,use_attachment=True):
            self.voice.resolve_task(call_id,dict(ok=True,answer='气泡里有两个方向，第三项可以补充要求。选好后我再开始。',execution_pending=False))
            return
        self._voice_task = (self.voice, call_id)
        if not self._pending_image or uses_task(request,'auto',True):
            self.voice.set_task_context(dict(running=True,kind='local',goal=request,summary='已接收，正在启动后台执行'))
        self._task_control.reset()
        messages = self.store.messages(self.active_id, 1)
        record_user = not (messages and messages[-1]['role'] == 'user')
        if not self.send(request, record_user=record_user,use_attachment=True,force_agent=not bool(self._pending_image)):
            self._voice_task = None
            self.voice.resolve_task(call_id, dict(ok=False, error=self.status.text()))
        else:
            if not self.busy and getattr(self,'_last_personal_answer',None):
                self._voice_task=None;self._voice_readback=True
                self.voice.resolve_task(call_id,dict(ok=True,answer=self._last_personal_answer))
                return
            if self._run_kind=='answer':return
            from core.task_feedback import acknowledgement
            ack=acknowledgement(request)
            # Close the speech FC immediately; execution remains in the worker.
            # Otherwise pending _tasks blocks every queued progress narration.
            self._voice_task=None
            self._voice_readback=True
            self.voice.resolve_task(call_id,dict(ok=True,answer=ack,execution_pending=True))
            self._show_active_bubble(ack)
            self.store.append(self.active_id,'system','任务已接收：'+ack)

    def _running_task_answer(self,question):
        """DSH events are the source of truth for immediate spoken status."""
        if any(term in str(question) for term in ('屏幕','看到','画面','这里','当前页')):
            shared=self.runtime.screen_share.context(max_age=12)
            if shared.get('active'):
                if shared.get('context'):return '共享屏幕当前资料：'+str(shared['context'])[:240]
                return '屏幕共享已经开启，但最新画面理解还没有返回；我不能凭空描述屏幕。'
        for item in self._dsh_reports.values():
            if item.get('conversation_id')!=self.active_id:continue
            brief=item.get('brief')
            if brief is not None:
                answer=brief.answer(question)
                if answer:return answer
        if self._run_kind=='dsh':
            value=str(question or '')
            if any(term in value for term in ('进度','进展','做到哪','怎么样了','在做什么','为什么','卡住','失败','下一步','汇报')):
                state=self.runtime.dsh.status()
                summary=state.get('summary') or 'DSH 尚未返回可核验的新进展'
                return ('当前任务仍在执行。最新记录是：'+summary+
                    '。目前没有新的明确错误原因，我不会把等待说成完成。')
        elif self.busy and any(term in str(question) for term in ('进度','进展','做到哪','怎么样了','在做什么','为什么','卡住','失败','下一步','汇报')):
            summary=str(self.agent.journal.data.get('summary') or '尚未收到新的工具回执')
            return '当前任务仍在执行。最新记录是：'+summary+'。尚未确认整个目标完成，也不会重放已发送的操作。'
        return None

    def _voice_cancelled(self):
        if self._voice_task:
            self._voice_task = None
            # Speech interruption stops old playback, not a still-authorized task.
            # Explicit stop/end-call/shutdown still cancels the worker.
            if not self.voice_active:self.agent.cancel()

    def _voice_preview(self, text):
        if self.voice_active and self.sender() is self.voice:
            from core.embodiment import spoken_reply
            self._show_active_bubble(spoken_reply(text)[2])

    def _reflex_command(self, command, *, record_user=False, read_reply=False):
        """Drive one referent desktop action on a worker; the receipt lands on fast_result."""
        if self.busy or self._fast_inflight:
            return self._finish_fast_command(dict(command,_conversation_id=None),
                dict(ok=False,action_sent=False,verified=False,code='desktop_busy',
                     error='当前已有任务占用桌面，这次没有操作；请等它结束后再说一遍'))
        from core.reflex import execute as execute_reflex
        self._pending_execution=None
        self._fast_inflight=True
        command=dict(command,_conversation_id=self.active_id,_read_reply=bool(read_reply),_reflex=True)
        if record_user:
            self.store.append(self.active_id,'user',str(command.get('source_text') or ''))
        self._last_fast_result=dict(ok=True,execution_pending=True,message='正在照做…')
        self.status.setText('正在照做…')
        def work():
            try:result=execute_reflex(command,desktop=getattr(self,'_browser_interaction',None))
            except Exception:
                result=dict(ok=False,action_sent=False,verified=False,need_model=True,
                            message='界面这边我没有看清或没有做成，这次没有继续操作')
            self.fast_result.emit(command,result)
        import threading
        threading.Thread(target=work,name='guga-reflex',daemon=True).start()
        return self._last_fast_result

    def _reflex_fallback(self, command, result):
        """No unique target: give the same sentence back to the model exactly once."""
        message=str(result.get('message') or '我没看清你指的是哪一块')
        self._fast_inflight=False
        self.status.setText(message)
        if command.get('fast_id') and self.voice_active and self.voice:
            # Long voice: the live model owns the turn and receives this honest receipt.
            return self._finish_fast_command(dict(command,_reflex=False),
                dict(result,ok=False,need_model=True,error=message))
        text=str(command.get('source_text') or '')
        self._last_fast_result=dict(result,ok=False,message=message)
        if not text:
            self._show_active_bubble(message);return self._last_fast_result
        self._show_active_bubble(message)
        read_reply=bool(command.get('_read_reply'))
        QTimer.singleShot(0,lambda:self.send(text,record_user=False,_skip_local=True,read_reply=read_reply))
        return self._last_fast_result

    def _fast_command(self, command):
        if command.get('fast_id') and not self.voice_active:return
        if command.get('reflex'):
            # Long-call transcripts and the speech model's function call share this receipt.
            return self._reflex_command(command)
        if command.get('action') in ('daily_plan','dsh_link'):
            command=dict(command,_conversation_id=self.active_id)
            from core.daily_plan import format_plan
            try:
                if command['action']=='daily_plan':
                    state=self.runtime.execute('daily_plan',command['arguments'])
                    result=dict(ok=True,message=format_plan(state),daily_plan=state)
                else:
                    from core.dsh_link import format_status
                    state=self.runtime.execute('dsh_link',command['arguments'])
                    result=dict(ok=True,message=format_status(state),dsh_link=state)
                self._show_active_bubble(result['message'])
            except (ValueError,RuntimeError) as exc:
                result=dict(ok=False,error=str(exc))
            return self._finish_fast_command(command,result)
        if command.get('action') in ('screen_share_start','screen_share_stop'):
            try:
                if command['action']=='screen_share_start':
                    if not self.voice_active:raise RuntimeError('请先开启长语音通话；共享屏幕只在长对话中作为辅助开启')
                    self._screen_share_manual_stop=False
                    native = self._native_video_call
                    if native:
                        if not self.voice or not self.voice.connected:
                            raise RuntimeError('通话接通后可以共享屏幕')
                        result = self.runtime.screen_share.start(2, 'voice', native_video=True)
                    else:
                        result = self.runtime.screen_share.start(command.get('fps'), 'voice')
                    self._set_call_share_control(True)
                    result['message']='屏幕共享已作为长对话辅助开启，本地按'+str(result['fps'])+'帧/秒采集；画面只保留在内存'
                else:
                    self._screen_share_manual_stop=True
                    self.runtime.screen_share.stop()
                    self._set_call_share_control(False)
                    result=dict(ok=True,message='已停止共享屏幕')
            except Exception as exc:result=dict(ok=False,error='共享屏幕未开始：'+str(exc))
            return self._finish_fast_command(command,result)
        if command.get('action') in ('close_page','close_browser'):
            self._pending_execution=None
            if self.busy or self._fast_inflight:
                return self._finish_fast_command(command,dict(ok=False,code='desktop_busy',
                    error='当前已有任务占用桌面，本次没有发送关闭操作。请先停止当前任务或等待结束'))
            import threading
            from core.fast_commands import execute
            self._fast_inflight=True
            self._last_fast_result=dict(ok=True,execution_pending=True,message='正在识别目标并核验关闭结果')
            self.status.setText(self._last_fast_result['message'])
            command=dict(command,_conversation_id=self.active_id)
            def work():self.fast_result.emit(command,execute(command))
            threading.Thread(target=work,daemon=True).start()
            return self._last_fast_result
        if command.get('action')=='stop_call':
            interaction=getattr(self,'_browser_interaction',None)
            if interaction and hasattr(interaction,'cancel'):interaction.cancel()
            if getattr(self,'_voice_departing',False):
                # A second "退下" must end the call, not wait on a farewell
                # that may never finish speaking.
                self._voice_farewell_done=True
                self.voice_button.setChecked(False)
                return dict(ok=True,message='已结束通话')
            self._voice_departing=True;self._voice_farewell_started=time.monotonic()
            self._voice_farewell_done=False
            line='真的要让本鹅走呀？好吧，那本鹅先退下；想我的时候再叫我。'
            self.status.setText(line)
            self._show_active_bubble(line)
            try:
                if self.voice:
                    self.voice.interrupt();self.voice.mute(True)
                self.mute_button.setChecked(True)
                self.pet._tts.speak(line)
            except Exception:
                pass
            self._last_fast_result=dict(ok=True,execution_pending=True,message=line)
            QTimer.singleShot(600,self._finish_voice_farewell)
            return self._last_fast_result
        if command.get('action') in ('browser_step','desktop_step'):
            self._direct_control_until=time.monotonic()+60
            if (command.get('step_action') or command.get('browser_action'))=='cancel_step':
                interaction=getattr(self,'_browser_interaction',None)
                result=interaction.cancel() if interaction and hasattr(interaction,'cancel') else dict(ok=True,
                    action_sent=False,message='当前没有待执行的单步操作')
                return self._finish_fast_command(command,result)
            if self._fast_inflight or (self.busy and self._run_kind in ('task','dsh')):
                return self._finish_fast_command(command,dict(ok=False,code='desktop_busy',action_sent=False,
                    error='已有界面任务正在执行，本次未发送操作'))
            import threading
            from core.desktop_interaction import DesktopInteraction
            interaction=getattr(self,'_browser_interaction',None)
            if interaction is None:self._browser_interaction=interaction=DesktopInteraction()
            if getattr(self,'_step_conversation',None)!=self.active_id:
                if hasattr(interaction,'cancel'):interaction.cancel()
                self._step_conversation=self.active_id
            import win32gui
            target_hwnd=command.get('_target_hwnd',int(win32gui.GetForegroundWindow() or 0))
            self._fast_inflight=True
            command=dict(command,_conversation_id=self.active_id)
            source_voice=self.voice if command.get('fast_id') else None
            conversation_id=self.active_id
            command['_can_execute']=lambda:(self.active_id==conversation_id and
                (source_voice is None or (self.voice_active and self.voice is source_voice)))
            command['_step_generation']=getattr(interaction,'_generation',0)
            self._last_fast_result=dict(ok=True,execution_pending=True,message='正在执行这一步')
            def step():
                try:result=interaction.execute(command,hwnd=target_hwnd)
                except Exception:
                    result=dict(ok=False,action_sent=None,code='desktop_step_failed',
                        error='这一步的执行回执中断，请先核对画面；没有自动重试')
                self.fast_result.emit(command,result)
            threading.Thread(target=step,name='guga-desktop-step',daemon=True).start()
            return self._last_fast_result
        if command.get('action') in ('open_app','open_url','web_search'):
            # Shell/default-browser handlers can block. Launching must never
            # freeze the Qt thread that receives voice events and user input.
            import threading
            from core.fast_commands import execute
            blocker=self._launch_blocker()
            if blocker:
                # 不空口拒绝：把这句话记下来，占用结束自动补开，用户不用再说一遍。
                if not command.get('_replay'):
                    self._pending_launch=dict(command=dict(command),at=time.time(),
                                              conversation_id=self.active_id)
                hint='「%s」' % str(command.get('source_text') or command.get('target') or '')[:20] if (
                    command.get('source_text') or command.get('target')) else '这一步'
                return self._finish_fast_command(command,dict(ok=False,code='desktop_busy',
                    queued_retry=not command.get('_replay'),
                    error=blocker+(hint+'已经记下，做完手上这步我立刻打开' if not command.get('_replay')
                                   else hint+'这次没排上，等桌面空出来再喊我')))
            self._fast_launch_count=getattr(self,'_fast_launch_count',0)+1
            self._fast_launch_at=time.time()
            command=dict(command,_async_launch=True,_conversation_id=self.active_id)
            self._last_fast_result=dict(ok=True,execution_pending=True,message='正在提交启动请求')
            def work():
                try:result=execute(command,self.agent.computer)
                except Exception as exc:result=dict(ok=False,error=str(exc),action_sent=None)
                self.fast_result.emit(command,result)
            threading.Thread(target=work,name='guga-fast-launch',daemon=True).start()
            return self._last_fast_result
        from core.fast_commands import execute
        if command.get('action')=='multiagent':
            manager=getattr(self.pet,'_multi_agent',None)
            if manager is None:result=dict(ok=False,error='多智能体管理器不可用')
            elif command.get('kind')=='summon':result=dict(ok=True,message='已召唤成员 '+command.get('target',''))
            elif command.get('kind')=='summon_pair':result=dict(ok=True,message='已召唤大肥鱼和 Dora')
            elif command.get('kind')=='discuss':result=dict(ok=manager.start_discussion(tuple(command.get('target','').split(',')),command.get('topic','')),message='团队讨论已开始')
            else:result=dict(ok=manager.start_discussion(tuple(command.get('target','').split(',')),None),message='等待讨论话题')
            if result.get('ok'):
                if command.get('kind')=='summon':manager.summon(command['target'])
                elif command.get('kind')=='summon_pair':manager.summon_pair()
        elif command.get('action')=='performance':
            if command.get('kind')=='gesture':
                self._active_gesture(command['target']);result=dict(ok=True)
            else:
                result=self.runtime.execute('music_performance',dict(action={'music_stop':'stop','music_pause':'pause','music_resume':'resume'}.get(command.get('kind'),'sing'),title=command.get('target','')))
        else:result=execute(command)
        return self._finish_fast_command(command,result)

    def _launch_blocker(self):
        """「打开/启动」要不要等一等。空字符串=可以立刻开。

        只挡真的在操作桌面的情况：一次启动还没回执、任务/DSH 正在点桌面。
        普通问答（_run_kind=='answer'）不占桌面，不该拦「打开b站」——之前它
        就是这么被拦掉的，然后既不重试也不转交，用户看到的就是"打不开了"。
        计数泄漏（线程没回执）用 20 秒租约兜底，别让一次丢回执锁死之后所有启动。
        """
        if getattr(self,'_fast_inflight',False):
            return '上一步桌面操作还在核验。'
        count=int(getattr(self,'_fast_launch_count',0) or 0)
        if count:
            age=time.time()-float(getattr(self,'_fast_launch_at',0) or 0)
            if age<20:
                return '上一次启动还没回执。'
            self._fast_launch_count=0
        if self.busy and self._run_kind in ('task','dsh'):
            return '正在执行桌面任务。'
        return ''

    def _replay_pending_launch(self):
        """上一轮被挡下的「打开X」，桌面空出来后自动补开一次。"""
        pending=getattr(self,'_pending_launch',None)
        if not pending:return
        self._pending_launch=None
        if time.time()-float(pending.get('at',0) or 0)>120:return
        command=pending.get('command') or {}
        if not command or self.busy or getattr(self,'_fast_inflight',False):return
        try:self._fast_command(dict(command,_replay=True))
        except Exception:pass

    def _finish_fast_command(self,command,result):
        if command.get('_reflex') and result.get('need_model'):
            return self._reflex_fallback(command,result)
        if command.get('_receipt_id'):result=dict(result,receipt_id=command['_receipt_id'])
        if command.get('_async_launch'):
            self._fast_launch_count=max(0,getattr(self,'_fast_launch_count',0)-1)
        elif command.get('_conversation_id'):self._fast_inflight=False
        current=not command.get('_conversation_id') or command['_conversation_id']==self.active_id
        if result.get('ok') and command.get('report_query') and not self.busy and current:
            query=command['report_query']
            result.update(execution_pending=True,message='已请求默认浏览器搜索，正在核对网页摘要后汇报')
            QTimer.singleShot(0,lambda:self.send('请用web_search搜索：'+query+'。根据工具返回的摘要简短汇报，并附来源链接。浏览器已提交打开请求，无需再次打开。',record_user=False))
        if (not result.get('ok') and command.get('action') in ('open_app','open_url','web_search')
                and result.get('code')!='app_not_found' and not result.get('queued_retry')
                and command.get('source_text')
                and not result.get('launch_requested') and current):
            # Retrying the same sentence through the remote brain used to be the
            # default. When that gateway is down it hangs, writes the failure into
            # chat history, and the next call drags the old error back in.
            # Report the real reason once and stop here.
            result=dict(result,delegated=False,retry_skipped=True,
                error=str(result.get('error') or '这一步没做成')+'（没有再转交给联网大脑重试）')
        if not result.get('ok'):
            error='未完成：'+str(result.get('error','执行器未提供原因'))
            result=dict(result,error=error)
            self._last_fast_failure=dict(error=error,at=time.time(),conversation_id=command.get('_conversation_id') or self.active_id)
        self._last_fast_result=result
        # Fixed action/diagnostic codes, executor errors and timings; no transcript or audio.
        try:
            path=FAST_DIAGNOSTICS_PATH
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(json.dumps(dict(updated=time.time(),ok=bool(result.get('ok')),
                action=command.get('action'),code=result.get('code'),action_sent=result.get('action_sent'),
                verified=result.get('verified'),evidence=result.get('evidence'),error=result.get('error'),
                asr_after_speech_ms=command.get('asr_after_speech_ms'),dispatch_ms=result.get('dispatch_ms'),
                transcript_to_result_ms=result.get('transcript_to_result_ms')),indent=2),encoding='utf-8')
        except OSError:pass
        if command.get('fast_id') and self.voice_active and self.voice:
            self.voice.complete_fast_command(command['fast_id'],result)
        if result.get('ok'):
            self.status.setText(result.get('message','快速命令已完成'))
            if command.get('_read_reply'):
                self._say_active_reply(result.get('message','快速命令已完成'),speak=True)
            if command.get('action') in ('browser_step','desktop_step','open_app','open_url','web_search') or command.get('_reflex'):
                self._show_active_bubble(result.get('message','已执行这一步'))
            if command.get('_conversation_id'):
                self.store.append(command['_conversation_id'],'assistant',result.get('message','快速命令已完成'))
                self.render()
            return result
        detail=str(result.get('error') or result.get('message') or '未知原因')
        message=('这次没有做成：' if command.get('_reflex') else '快速命令执行失败：')+detail
        if command.get('_read_reply'):self._say_active_reply(message,speak=True)
        self.status.setText(message)
        self._show_active_bubble(message)
        self.store.append(command.get('_conversation_id') or self.active_id,'assistant',message)
        self.render()
        return result

    def _voice_transcript(self, who, text):
        if not self.voice_active or self.sender() is not self.voice:
            return
        self.pet._on_user_transcript(who, text)
        if who=='饲养员':
            self._last_user_activity=time.monotonic()
            from core.fast_commands import match
            if match(text).get('action') not in ('browser_step','desktop_step'):self._direct_control_until=0
            team=getattr(self.pet,'_multi_agent',None)
            if team:team.user_interaction(self.active_agent_id)
        if who=='饲养员' and self.busy and self._task_control.pending:
            control=self._task_control.observe(text,busy=True)
            if control['action'] in ('cancel','continue','ask_stop','pending'):
                self._finish_voice_task_confirmation(text,control['action'])
                return
        if who!='饲养员':
            from core.embodiment import spoken_reply
            _,_,text=spoken_reply(text)
        if who == '饲养员':
            self._voice_readback = False
            self.store.append(self.active_id, 'user', text)
            self._remember_dialogue(self.active_agent_id,'user',text)
            self.voice.long_term_memory=self._voice_memory()
            if self.store.conversation(self.active_id)['name'] == '新对话':
                self.store.rename('conversation', self.active_id, text[:24])
                self.refresh_tree()
        elif not self._voice_readback:
            from core.chat_presentation import is_progress_ack
            self.store.append(self.active_id, 'tool' if is_progress_ack(text) else 'assistant', text)
            if not is_progress_ack(text):self._remember_dialogue(self.active_agent_id,'assistant',text)
        if who != '饲养员':
            self._show_active_bubble(text)
        self.render()

    def _finish_voice_task_confirmation(self,text,action):
        """Consume a short yes/no locally; Qwen must not reinterpret it."""
        self._voice_readback=False
        self.store.append(self.active_id,'user',text)
        if action=='cancel':
            self._cancel_execution()
            answer='已确认停止当前任务；已经完成的操作保留在记录中。'
        elif action=='continue':
            answer='好，继续当前任务。'
        else:
            answer='当前任务仍在执行。确定要停止吗？请说“停止”确认，或说“继续”任务。'
        self.status.setText(answer)
        self.store.append(self.active_id,'system',answer)
        self._show_active_bubble(answer)
        if self.voice:
            self.voice.interrupt()
        self.render()

    def _finish_voice_farewell(self):
        if not self.voice_active or not getattr(self,'_voice_departing',False):return
        if getattr(self,'_voice_farewell_done',False):return
        self._voice_farewell_done=True
        # Leaving must not wait for the farewell TTS. A stuck or looping speaker
        # used to keep the call alive for up to twelve seconds.
        self.voice_button.setChecked(False)

    def _voice_finished(self):
        if self.sender() is self.voice:
            self._voice_departing=False;self._voice_farewell_started=0.0
            self._release_failed_voice()
            if not self.voice.last_error:
                self.status.setText('通话已结束')

    def shutdown(self):
        if hasattr(self.runtime, 'file_assistant'):
            self.runtime.file_assistant.stop(persist=False)
        if hasattr(self,'managed'):self.managed.close()
        self._codex_closing=True
        monitor=getattr(self,'_dsh_monitor',None)
        if monitor is not None:monitor.close()
        codex=getattr(self.runtime,'codex',None)
        if codex is not None:codex.close()  # Only detach monitoring; do not cancel desktop tasks.
        try:
            from core.reflex import stop_foreground_tracker
            stop_foreground_tracker()
        except Exception:
            pass
        try:
            # 浏览器反射复用了同一条 CDP 会话，退出时要关掉，别把连接留给下一次启动。
            from core.super_agent import browser_reflex
            browser_reflex.close_session()
        except Exception:
            pass
        self._manual_voice_timer.stop()
        self._task_speech.clear()
        self._screen_share_watchdog_timer.stop()
        try:self.runtime.screen_share.stop()
        except Exception:pass
        self.runtime.media.stop()
        self.proactive.stop()
        self.music_listener.enable(False)
        self._dialogue_context=None;self.dialogue.cancel()
        self.answer.cancel();self.short_voice.stop(cancel=True)
        self.runtime.music.stop()
        self._task_heartbeat_timer.stop()
        self.runtime.battleground.stop()
        self.runtime.dsh.stop()
        self.agent.cancel()
        self.voice_button.setChecked(False)
        session=getattr(self.agent.computer,'browser_session',None)
        if session:session.close()

    def closeEvent(self, event):
        self._clear_attachment()
        self.shutdown()
        self.pet._tts.stop()
        event.accept()

    def reject(self):
        self.close()
