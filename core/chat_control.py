"""Programmatic Guga chat entry: use the actual UI router, no simulated input."""
import hashlib
import json
import time
import uuid
from core.dsh_progress import reporting_config


class ChatControl:
    def __init__(self, panel):
        self.panel = panel
        with panel.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS chat_submissions ('
                'request_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, '
                'digest TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL)')

    def execute(self, args):
        panel, store = self.panel, self.panel.store
        action = str(args.get('action') or 'state')
        cid = str(args.get('conversation_id') or panel.active_id or '')
        if action == 'new':
            if panel.busy or panel.voice_active:
                raise ValueError('当前对话正在执行或通话，未切换会话')
            if panel.editor.toPlainText().strip():
                raise ValueError('输入框有未发送草稿，未切换会话')
            if getattr(panel,'_manager_enabled',False):
                from core.managed_chat import default_backend
                backend=args.get('backend') or default_backend(panel.active_agent_id)
                owner=panel.active_agent_id if backend=='guga' else '002' if backend=='dsh' else '001'
                if owner!=panel.active_agent_id:panel.activate_agent(owner)
                cid=panel.managed.create(backend,args.get('name') or '程序验收',agent_id=owner)
            else:cid = store.create_conversation(args.get('name') or '程序验收', agent_id=panel.active_agent_id)
            panel.open_conversation(cid)
            return dict(ok=True, conversation_id=cid)
        conversation = store.conversation(cid)
        if str(conversation.get('agent_id') or '001') != str(panel.active_agent_id):
            raise ValueError('对话不属于当前角色，未发送')
        if action=='alignment':
            pending=getattr(panel,'_pending_alignment',None)
            if not pending or pending['ident']!=cid:return dict(ok=False,error='此对话没有待选择的任务')
            choice=int(args.get('choice',-1))
            accepted=panel._choose_alignment(cid,choice,str(args.get('supplement') or ''))
            return dict(ok=bool(accepted),conversation_id=cid,awaiting_alignment=bool(panel._pending_alignment))
        if action=='board_state':
            board=panel.result_board
            if cid!=panel.active_id or board is None:return dict(ok=False,error='此对话未展示黑板')
            return dict(ok=True,**board.diagnostics(capture=bool(args.get('capture'))))
        if action=='present_result':
            if panel.busy or panel.voice_active or panel.editor.toPlainText().strip():
                raise ValueError('当前有任务、通话或未发送草稿，尚未切换成果')
            text=str(args.get('text') or '').strip()
            if not text or len(text)>8000:raise ValueError('成果说明需要1—8000字')
            if cid!=panel.active_id:panel.open_conversation(cid)
            from pathlib import Path
            panel._begin_result_delivery(cid,str(args.get('goal') or '这次的成果'),text,
                str(Path(__file__).resolve().parents[1]),float(args.get('since') or 0),
                'present-'+cid+'-'+hashlib.sha256(text.encode()).hexdigest()[:12])
            return dict(ok=True,conversation_id=cid,state='reviewing_artifacts',task_dispatched=False)
        if action == 'reporting':
            from pathlib import Path
            config=reporting_config(store,cid)
            interval=int(args.get('interval_seconds',config['interval_seconds']))
            if not 60<=interval<=3600:raise ValueError('汇报间隔需要60—3600秒')
            config.update(interval_seconds=interval,enabled=bool(args.get('enabled',True)))
            if 'phase' in args:
                if args['phase'] not in ('task','processing','submission'):raise ValueError('未知汇报阶段')
                config['phase']=args['phase']
            if args.get('work_directory'):
                work=Path(args['work_directory']).resolve()
                if not work.is_dir():raise ValueError('工作目录不存在')
                config['work_directory']=str(work)
            if 'duration_seconds' in args:
                duration=float(args['duration_seconds'])
                if not 0<duration<=1209600:raise ValueError('视频时长无效')
                config['duration_seconds']=duration
            store.preference('dsh_reporting:'+cid,json.dumps(config,ensure_ascii=False))
            monitor=getattr(panel,'_dsh_monitor',None)
            if monitor and monitor.status().get('conversation_id')==cid and monitor.reporter:
                monitor.reporter.config=config
            return dict(ok=True,conversation_id=cid,reporting=config)
        if action == 'stop':
            if cid != panel.active_id:
                raise ValueError('停止只允许作用于当前明确指定的对话')
            if panel.voice_active:
                raise ValueError('语音通话中，未自动结束通话')
            requested = []
            if getattr(panel,'_manager_enabled',False):
                managed=panel.managed.get(cid)
                if managed['backend']=='codex' and managed['state'] in ('connecting','dispatching','running','needs_input','stop_requested'):
                    panel.managed.stop(cid);requested.append('codex')
            if getattr(panel,'_pending_alignment',None) and panel._pending_alignment['ident']==cid:
                panel._choose_alignment(cid,0);requested.append('alignment')
            sid = conversation.get('dsh_session_id')
            if sid:
                from core.super_agent.dsh_gateway import DshGateway
                if DshGateway(timeout=5).session_view(sid).get('running'):
                    DshGateway(timeout=5).cancel(sid)
                    requested.append('dsh')
            if getattr(panel.answer, 'active', False):
                panel.answer.cancel()
                requested.append('answer')
            if panel.busy:
                panel._cancel_execution()
                requested.append('local')
            return dict(ok=True, conversation_id=cid, stop_requested=requested,
                        state='stop_requested' if requested else 'idle')
        if action == 'state':
            capture_path=''
            if args.get('capture') and cid==panel.active_id and getattr(panel,'_pending_alignment',None):
                from pathlib import Path
                output=Path(__file__).resolve().parents[1]/'runtime/browser-bubble-upgrade-20261114/live-alignment.png'
                output.parent.mkdir(parents=True,exist_ok=True)
                if panel.pet._agent_bubble.grab().save(str(output)):capture_path=str(output)
            rows = [m for m in panel._messages_for(cid) if m.get('role') in ('user', 'assistant')]
            monitor = getattr(panel, '_dsh_monitor', None)
            state = monitor.status() if monitor is not None else {}
            if state.get('conversation_id') != cid:
                state = {}
            return dict(ok=True, conversation_id=cid, active=cid == panel.active_id,
                managed=(dict(panel.managed.get(cid),remote_message_count=len(panel.managed.cache.get(cid,[])))
                    if getattr(panel,'_manager_enabled',False) else None),
                capture_path=capture_path,
                fast_action_running=bool(getattr(panel,'_fast_inflight',False) or getattr(panel,'_fast_launch_count',0)),
                last_fast={k:v for k,v in (getattr(panel,'_last_fast_result',None) or {}).items()
                    if k in ('ok','error','page_id','hwnd','title','browser_mode','profile_changed','reused_window','navigation_verified','url_requested','launch_requested')},
                alignment=({k:v for k,v in panel._pending_alignment.items() if k in ('text','options','ident')}
                    if getattr(panel,'_pending_alignment',None) and panel._pending_alignment['ident']==cid else None),
                dsh_session_id=conversation.get('dsh_session_id') or '',
                dsh=state, local_busy=panel.busy,
                reporting=reporting_config(store,cid),
                answering=bool(getattr(panel.answer, 'active', False)),
                messages=[dict(role=m.get('role'), text=m.get('content', '')[:6000])
                          for m in rows[-max(1, min(20, int(args.get('limit') or 8))):]])
        if action != 'send':
            raise ValueError('chat_control 只支持 new/send/state/stop/reporting')
        text = str(args.get('text') or '').strip()
        if not text or len(text) > 8000:
            raise ValueError('程序提交的消息需要 1—8000 字')
        if panel.busy or panel.voice_active:
            raise ValueError('当前有本地任务或语音通话，未提交新消息')
        if panel.editor.toPlainText().strip():
            raise ValueError('输入框有未发送草稿，未覆盖或发送')
        request_id = str(args.get('request_id') or uuid.uuid4().hex)
        digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
        with store.connect() as db:
            old = db.execute('SELECT * FROM chat_submissions WHERE request_id=?', (request_id,)).fetchone()
            if old:
                if old['conversation_id'] != cid or old['digest'] != digest:
                    raise ValueError('同一个 request_id 对应了不同消息，拒绝重用')
                return dict(ok=True, duplicate=True, accepted=old['state'] in ('submitted','awaiting_alignment','waiting_connection'),
                            state=old['state'], request_id=request_id, conversation_id=cid)
            db.execute('INSERT INTO chat_submissions VALUES (?,?,?,?,?)',
                       (request_id, cid, digest, 'dispatching', time.time()))
        try:
            if cid != panel.active_id:
                panel.open_conversation(cid)
            accepted = panel.send(text, record_user=True) is not False
            state = 'submitted' if accepted else 'rejected'
            if accepted and getattr(panel,'_pending_alignment',None):state='awaiting_alignment'
            elif accepted and getattr(panel,'_manager_enabled',False) and panel.managed.get(cid).get('state')=='connecting':state='waiting_connection'
        except Exception:
            with store.connect() as db:
                db.execute('UPDATE chat_submissions SET state=? WHERE request_id=?', ('delivery_unknown', request_id))
            raise
        with store.connect() as db:
            db.execute('UPDATE chat_submissions SET state=? WHERE request_id=?', (state, request_id))
        return dict(ok=accepted, accepted=accepted, state=state, request_id=request_id,
                    conversation_id=cid, dsh_session_id=store.conversation(cid).get('dsh_session_id') or '')
