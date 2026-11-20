"""Project-manager conversations: real bindings, exact remote history, independent review."""
import json
import re
import threading
import time
from PyQt5.QtCore import QObject,QTimer,pyqtSignal

SETUP='这是咕嘎为用户明确创建的执行器对话。只回复“连接就绪，等待任务”。不要修改文件、运行命令或创建其他对话。后续用户任务仍在这个对话中处理。'


def default_backend(agent_id='001'):return 'guga'


def complex_task(text):
    from core.task_alignment import choices_for
    return bool(choices_for(text) or len(text)>200 or re.search(r'复杂|开发|重构|修复|项目|验收|多步',text))


def verdict(text):
    value=str(text).strip()
    if value.startswith('```'):value=re.sub(r'^```(?:json)?\s*|\s*```$','',value)
    try:result=json.loads(value)
    except ValueError:return dict(verdict='unverified',checks=[],issues=['验收没有返回约定的结构化结果'])
    if (not isinstance(result,dict) or result.get('verdict') not in ('passed','failed','needs_input')
            or not isinstance(result.get('checks'),list) or not result['checks']):
        return dict(verdict='unverified',checks=[],issues=['缺少明确结论或实际检查记录'])
    return result


class ManagedChats(QObject):
    changed=pyqtSignal(dict)
    finished_work=pyqtSignal(object)

    def __init__(self,panel):
        super().__init__(panel);self.panel=panel;self.store=panel.store;self.codex=panel.runtime.codex
        self._inflight=set();self._closed=False;self._dsh=None;self.cache={};self._lock=threading.RLock()
        with self.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS managed_chats (conversation_id TEXT PRIMARY KEY,data TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS managed_reviews (parent TEXT NOT NULL,turn TEXT NOT NULL,review TEXT NOT NULL,PRIMARY KEY(parent,turn))')
            db.execute('CREATE TABLE IF NOT EXISTS project_plan_decisions (conversation_id TEXT NOT NULL,'
                       'turn_id TEXT NOT NULL, decision TEXT NOT NULL, plan TEXT NOT NULL, created REAL NOT NULL,'
                       'PRIMARY KEY(conversation_id,turn_id))')
            # Preserve the external IDs and history, move existing DSH chats to the fish.
            db.execute("UPDATE conversations SET agent_id='002' WHERE dsh_session_id IS NOT NULL AND "
                       "dsh_session_id<>'' AND agent_id='001' AND id NOT IN (SELECT conversation_id FROM managed_chats)")
        self.finished_work.connect(self._finish)
        self.timer=QTimer(self);self.timer.setInterval(12000);self.timer.timeout.connect(self.poll);self.timer.start()

    def get(self,cid):
        with self.store.connect() as db:row=db.execute('SELECT data FROM managed_chats WHERE conversation_id=?',(cid,)).fetchone()
        if row:
            value=json.loads(row[0])
            if value['backend']=='guga':
                conversation=self.store.conversation(cid);sid=conversation.get('dsh_session_id') or ''
                bound=self.codex.bound(cid)
                if sid:value=self.put(value,backend='dsh',external_id=sid,state='ready')
                elif bound:
                    value=self.put(value,backend='codex',external_id=bound.get('thread_id',''),
                        host_id=bound.get('host_id') or 'local',state=bound.get('state','new'))
            if value['backend']=='codex' and not value['external_id']:
                bound=self.codex.bound(cid)
                if bound.get('thread_id'):
                    value=self.put(value,external_id=bound['thread_id'],
                        state='ready' if bound.get('state')=='completed' and not value.get('goal') else value['state'])
            return value
        c=self.store.conversation(cid);sid=c.get('dsh_session_id') or '';bound=self.codex.bound(cid)
        return dict(conversation_id=cid,backend='dsh' if sid else 'codex' if bound else default_backend(c['agent_id']),
            agent_id=c['agent_id'],external_id=sid or bound.get('thread_id',''),host_id=bound.get('host_id','local'),
            state='ready' if sid or bound.get('thread_id') else bound.get('state','new'),
            goal='',auto_review=False,review_for='')

    def put(self,row,**values):
        row=dict(row,**values);row['updated']=time.time()
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO managed_chats VALUES (?,?)',(row['conversation_id'],json.dumps(row,ensure_ascii=False)))
        return row

    def rows(self):
        with self.store.connect() as db:return [json.loads(r[0]) for r in db.execute('SELECT data FROM managed_chats')]

    def _work(self,cid,kind,fn):
        key=(cid,kind)
        with self._lock:
            if key in self._inflight:return False
            self._inflight.add(key)
        def run():
            try:event=dict(cid=cid,kind=kind,result=fn())
            except Exception as exc:event=dict(cid=cid,kind=kind,error=str(exc)[:400])
            if not self._closed:self.finished_work.emit(event)
        threading.Thread(target=run,name='guga-managed-'+kind,daemon=True).start();return True

    def create(self,backend='guga',name='新对话',project_id=None,review_for='',agent_id=None):
        if backend not in ('guga','codex','dsh'):raise ValueError('请选择咕嘎、Codex或DSH')
        owner=str(agent_id) if agent_id is not None else '002' if backend=='dsh' else '001'
        cid=self.store.create_conversation(name,project_id,owner)
        row=self.get(cid);self.put(row,backend=backend,review_for=review_for,auto_review=False)
        return cid

    def bind_existing(self, selection, remote_state, owner):
        """Bind the exact conversation verified by the local-agent picker."""
        backend = selection.backend
        if backend not in ('codex', 'dsh') or not selection.external_id:
            raise ValueError('需要选中一个可绑定的原对话')
        with self._lock:
            if backend == 'codex':
                existing = self.codex.conversation_for_thread(selection.external_id, selection.host_id)
            else:
                row = self.store.conversation_by_dsh(selection.external_id)
                existing = row['id'] if row else ''
            if existing:
                return existing
            label = ' '.join(selection.title.split())[:80] or '外部对话'
            cid = self.create(backend, name=label, agent_id=owner)
            if backend == 'codex':
                cid = self.codex.bind_thread(cid, owner, selection.external_id, selection.host_id, remote_state)
            else:
                self.store.bind_dsh(cid, selection.external_id)
            # DSH's independent session reservation checks if it is busy before sending.
            state = remote_state if backend == 'codex' and remote_state in ('running', 'needs_input') else 'ready'
            self.put(self.get(cid), external_id=selection.external_id, host_id=selection.host_id, state=state)
            return cid

    def target(self,cid,text,backend=None):
        if backend not in (None,'self','guga','codex','dsh'):raise ValueError('未知执行器')
        with self._lock:return self._target_locked(cid,text,backend)

    def save_project_decision(self, plan, decision):
        if decision not in ('revise', 'stopped', 'approved'):
            raise ValueError('未知方案决策')
        if decision == 'approved' and plan.issues:
            raise ValueError('方案预检未通过')
        with self.store.connect() as db:
            saved = db.execute('INSERT OR IGNORE INTO project_plan_decisions VALUES (?,?,?,?,?)',
                               (plan.conversation_id, plan.turn_id, decision, plan.text, time.time()))
            if not saved.rowcount:
                raise ValueError('该方案回合已经处理，请读取新的方案回合')

    def approve_project(self, plan, dispatch=None):
        with self._lock:
            row = self.get(plan.conversation_id)
            if row['backend'] != 'codex' or row['external_id'] != plan.thread_id:
                raise ValueError('方案对应的 Codex 绑定已变化')
            if row['state'] not in ('ready', 'completed'):
                raise ValueError('请等待原 Codex 对话结束当前任务')
            prompt = ('用户已在咕嘎项目黑板批准同一对话预审的方案。现在按方案实施。'
                      '分阶段汇报原型、资源、故障和验证结果。完成后给出项目目录和可预览文件。\n\n'
                      '需求：' + plan.goal + '\n\n已获批准的方案：\n' + plan.text)
            if len(prompt) > 15000:
                raise ValueError('方案过长，请让 Codex 返回较短的方案后重新审核')
            self.save_project_decision(plan, 'approved')
            if dispatch is not None:
                self.put(row, goal=plan.goal, state='dispatching', started_at=time.time(),
                         needs_review=False, project_plan_turn=plan.turn_id)
                try:
                    return dispatch(plan, prompt)
                except Exception as exc:
                    self.put(self.get(plan.conversation_id), state='delivery_unknown', error=str(exc)[:300])
                    raise
            return self._send_locked(plan.conversation_id, prompt)

    def _target_locked(self,cid,text,backend):
        row=self.get(cid);active=row
        original=cid
        if row.get('dispatch_parent'):
            cid=row['dispatch_parent'];row=self.get(cid)
        elif row.get('active_worker'):
            candidate=self.get(row['active_worker'])
            if candidate.get('dispatch_parent')==cid:active=candidate
        from core.companion_policy import read_policy,selected_backend
        settings=getattr(getattr(getattr(self.panel,'pet',None),'_config',None),'_s',None)
        policy=read_policy(settings) if settings is not None else {}
        backend=backend or selected_backend(text,policy,active['backend'])
        if backend in ('self','guga'):return original
        if backend==row['backend']:
            if row.get('active_worker'):self.put(row,active_worker='')
            return cid
        if row['backend']=='guga':
            self.put(row,backend=backend)
            return cid
        existing=next((r for r in self.rows() if r.get('dispatch_parent')==cid and r['backend']==backend),None)
        if existing:worker=existing['conversation_id']
        else:
            worker=self.create(backend,name=backend.upper()+' · '+self.store.conversation(cid)['name'][:50],
                project_id=self.store.conversation(cid).get('project_id'))
            self.put(self.get(worker),dispatch_parent=cid)
        self.put(row,active_worker=worker)
        return worker

    def _gateway(self):
        if self._dsh is None:
            from core.super_agent.dsh_gateway import DshGateway
            self._dsh=DshGateway(timeout=6)
        return self._dsh

    def _ensure_dsh(self):
        from core.dsh_bootstrap import ensure_dsh_web
        state = ensure_dsh_web(timeout=12, open_window=False)
        if not state['started'] and state['reason'] != 'already-running':
            raise RuntimeError('DSH 启动失败：' + state['reason'])

    def provision(self,cid):
        with self._lock:return self._provision_locked(cid)

    def _provision_locked(self,cid):
        row=self.get(cid)
        if row['backend']=='guga':return
        if row['external_id'] or row['state']=='connecting':return
        if row['state']=='delivery_unknown':raise RuntimeError('上次连接结果未知，请先核对执行器，不能自动重建')
        self.put(row,state='connecting')
        if row['backend']=='codex':
            try:self.codex.start(SETUP,cid,row['agent_id'])
            except Exception as exc:
                self.put(self.get(cid),state='disconnected',error=str(exc)[:400]);raise
            return
        def create():
            self._ensure_dsh();return self._gateway().create_session(conversation_id=cid)
        self._work(cid,'connect',create)

    def send(self,cid,text):
        with self._lock:return self._send_locked(cid,text)

    def _send_locked(self,cid,text):
        text=str(text or '').strip()
        if not text or len(text)>15000:raise ValueError('任务需要 1—15000 个字符，请分段发送')
        row=self.get(cid)
        if row['backend']=='guga':raise RuntimeError('咕嘎自己处理的任务请走本地任务入口，不创建外部执行器对话')
        if row['state'] in ('delivery_unknown','disconnected'):raise RuntimeError('执行器连接状态不明确，请先打开原对话核对；没有另建或重发')
        if not row['external_id'] or row['state']=='connecting':
            if row['backend']=='codex' and row['state']!='connecting':
                self.codex.start(text,cid,row['agent_id'])
                self.put(row,goal=text,state='dispatching',started_at=time.time(),needs_review=complex_task(text))
                return True
            if row.get('pending'):raise RuntimeError('已有一条消息等待连接，尚未重复投递')
            self.put(row,pending=text)
            self.provision(cid);return True
        if row['state']=='dispatching' or row['backend']=='dsh' and row['state']=='running':
            raise RuntimeError('执行器仍在处理上一条任务，请等结果返回或在原对话补充')
        steering=row['backend']=='codex' and row['state'] in ('running','needs_input','stop_requested')
        from core.companion_policy import read_policy,role_instructions
        settings=getattr(getattr(getattr(self.panel,'pet',None),'_config',None),'_s',None)
        policy=read_policy(settings) if settings is not None else {}
        review=bool(policy.get('enabled') and policy.get('auto_review') and row['backend']=='dsh' and not row.get('review_for'))
        updates=dict(state='running' if steering else 'dispatching',goal=row.get('goal') if steering else text,
            started_at=row.get('started_at',time.time()) if steering else time.time(),auto_review=review,
            needs_review=bool(row.get('needs_review') or complex_task(text)) if steering else bool(complex_task(text) or (policy.get('enabled') and review)),
            amendments=(row.get('amendments',[])+[text])[-20:] if steering else [],review_state='',error='')
        from core.artifact_review import delivery_instructions
        prompt=text+role_instructions(text,policy)+delivery_instructions(text)
        if row['backend']=='codex':
            self.codex.send(prompt,cid,row['agent_id']);self.put(row,**updates);return True
        self.put(row,**updates)
        def dispatch():
            from core.super_agent import dsh_slot
            from core.super_agent.dsh_transcript import read_transcript
            gateway=self._gateway();sid=row['external_id']
            claim=dsh_slot.claim(sid,ticket_id=cid,probe=gateway.session_running)
            if not claim.get('ok'):raise RuntimeError('DSH正在处理另一项任务，本次未重复派单')
            baseline=max((int(r.get('seq') or 0) for r in read_transcript(sid)),default=0)
            from core.wechat_demo import explicit_message
            message = explicit_message(text)
            dispatch_prompt = prompt
            if message:
                args = dict(action='send', **message, authorization=self.panel.agent.wechat.authorize(message, cid))
                dispatch_prompt = ('用户的微信任务：'+text+'\n'
                    '只调用一次 mcp__guga__wechat，参数如下。收到回执后简短回复并结束。'
                    '无需使用其他工具、生成文件、查改源码或重启应用。若回执失败，只报告卡点。\n'
                    + json.dumps(args, ensure_ascii=False))
            gateway.prompt(sid,dispatch_prompt,mode='queue')
            return dict(baseline=baseline)
        self._work(cid,'send',dispatch);return True

    def codex_event(self,event):
        cid=event.get('conversation_id')
        if not cid:return False
        row=self.get(cid)
        if row['backend']!='codex':return False
        state=event.get('state') or '';thread=event.get('thread_id') or row['external_id']
        if thread:row=self.put(row,external_id=thread,validation_receipts=event.get('validation_receipts') or [])
        setup=not row.get('goal')
        if state=='completed':
            self.put(row,state='ready' if setup else 'completed')
            if setup:
                self.changed.emit(dict(cid=cid,type='connected',backend='codex'))
                self._flush(cid)
            else:self.complete(cid,event.get('text') or event.get('summary') or '',event.get('turn_id') or thread)
        elif state=='stopped':
            self.put(row,state='stopped',pending='')
            self.changed.emit(dict(cid=cid,type='stopped',backend='codex',text=event.get('summary','')))
        elif event.get('type')=='codex_failed':
            self.put(row,state='delivery_unknown' if state=='delivery_unknown' else 'disconnected',error=event.get('error','连接失败'))
            self.changed.emit(dict(cid=cid,type='error',text=event.get('error') or 'Codex连接失败'))
        elif not setup:
            self.put(row,state='needs_input' if state=='needs_input' else 'running')
            self.changed.emit(dict(cid=cid,type='progress',backend='codex',text=event.get('summary',''),state=state))
        self.refresh(cid);return True

    def _flush(self,cid):
        row=self.get(cid);pending=row.get('pending','')
        if not pending:return
        if cid in getattr(self.codex,'_inflight',set()):
            deadline=row.get('flush_deadline') or time.time()+60
            if time.time()>=deadline:
                self.put(row,state='disconnected',error='连接回合尚未释放，任务保留，未重复发送')
                self.changed.emit(dict(cid=cid,type='error',text='连接尚未就绪，任务已保留，未重复发送'));return
            self.put(row,flush_deadline=deadline)
            QTimer.singleShot(150,lambda:self._flush(cid) if not self._closed else None);return
        row.pop('pending',None);row.pop('flush_deadline',None);self.put(row)
        try:self.send(cid,pending)
        except Exception as exc:
            self.put(self.get(cid),pending=pending,state='disconnected',error=str(exc)[:400])
            self.changed.emit(dict(cid=cid,type='error',text=str(exc)[:400]))

    def stop(self,cid):
        row=self.get(cid)
        if row['backend']!='codex':raise RuntimeError('请在大肥鱼原 DSH 对话处理停止要求')
        if not row.get('external_id'):
            self.put(row,pending='')
            self.changed.emit(dict(cid=cid,type='stopped',backend='codex',text='已取消等待连接的任务'))
            return dict(ok=True)
        result=self.codex.execute(dict(action='stop'),cid,row['agent_id'])
        self.put(row,state='stop_requested',pending='')
        return result

    def refresh(self,cid):
        row=self.get(cid)
        if not row['external_id']:return
        def read():
            if row['backend']=='codex':
                result=dict(messages=self.codex.gateway.messages(row['external_id'],row.get('host_id') or 'local'))
                error=str(row.get('error') or '')
                if row['state']=='disconnected' and not row.get('goal') and 'read_thread' in error and 'turnLimit' in error:
                    from core.codex_gateway import task_facts
                    result['connection_state']=task_facts(self.codex.gateway.read(row['external_id'],row.get('host_id') or 'local'))['state']
                return result
            from core.super_agent.dsh_transcript import read_transcript
            messages=read_transcript(row['external_id'])
            view=self._gateway().session_view(row['external_id']) if row['state']=='running' else {}
            return dict(messages=messages,view=view)
        self._work(cid,'refresh',read)

    def _finish(self,event):
        cid=event['cid'];kind=event['kind'];self._inflight.discard((cid,kind));row=self.get(cid)
        if event.get('error'):
            if kind!='refresh':self.put(row,state='delivery_unknown' if kind in ('send','connect') else 'disconnected',error=event['error'])
            self.changed.emit(dict(cid=cid,type='error',text=event['error']));return
        result=event['result']
        if kind=='team_report':
            self.changed.emit(dict(cid=cid,type='fish_report',text=result));return
        if kind=='connect':
            sid=result['session_id'];self.store.bind_dsh(cid,sid);self.put(row,external_id=sid,state='ready')
            self.changed.emit(dict(cid=cid,type='connected',backend='dsh'));self._flush(cid)
        elif kind=='send':
            self.put(row,state='running',baseline=result['baseline'])
            self.changed.emit(dict(cid=cid,type='dispatched',backend='dsh'));self.refresh(cid)
        elif kind=='refresh':
            error=str(row.get('error') or '')
            if (row['backend']=='codex' and row['state']=='disconnected' and
                    row.get('external_id') and not row.get('goal') and
                    'read_thread' in error and 'turnLimit' in error and result['messages'] and
                    result.get('connection_state')=='completed'):
                bound=self.codex.bound(cid)
                if not bound.get('write_uncertain') and bound.get('thread_id')==row['external_id']:
                    # The setup thread exists and can now be read. Old queued
                    # text remains recoverable but is never replayed on restart.
                    values=dict(state='ready',error='',connection_recovered=True)
                    if row.get('pending'):
                        values.update(undelivered_pending=row['pending'],pending='')
                    row=self.put(row,**values)
            if row['backend'] == 'codex':
                connected = getattr(self.panel.runtime, 'connected_knowledge', None)
                if connected is not None:
                    connected.observe_codex(cid, row['external_id'], result['messages'])
            self.cache[cid]=result['messages'];self.changed.emit(dict(cid=cid,type='history'))
            if row['backend']=='dsh' and row['state']=='running':
                replies=[r for r in result['messages'] if r['role']=='assistant' and int(r.get('seq') or 0)>row.get('baseline',0)]
                view=result.get('view',{})
                if replies and view.get('running') is False:
                    self._gateway().finish_task(row['external_id'])
                    from core.wechat_demo import explicit_message
                    message = explicit_message(row.get('goal'))
                    reply = replies[-1]['text']
                    if message:
                        receipt = self.panel.agent.wechat.receipt(cid)
                        if not receipt.get('verified_message_visible'):
                            error = receipt.get('error') or receipt.get('message') or '没有收到可核对的微信发送回执'
                            state='delivery_unknown' if receipt.get('send_clicked') or receipt.get('delivery_unknown') else 'failed'
                            self.put(row,state=state,error=error,result=reply,wechat_receipt=receipt)
                            self.changed.emit(dict(cid=cid,type='task_failed',text=error,backend='dsh',state=state))
                            return
                        row=self.put(row,wechat_receipt=receipt)
                        reply='微信「'+message['recipient']+'」的聊天记录已出现「'+message['text']+'」。'
                    self.put(row,state='completed')
                    self.complete(cid,reply,str(replies[-1].get('seq')))

    def complete(self,cid,text,turn):
        row=self.get(cid)
        if row.get('delivered_turn')==turn:return
        row=self.put(row,delivered_turn=turn,result=text)
        if row.get('review_for'):
            result=verdict(text);parent=row['review_for'];original=self.get(parent)
            if row.get('review_parent_started') is not None and row['review_parent_started']!=original.get('started_at'):
                self.put(row,review_state='superseded',review_result=result)
                self.changed.emit(dict(cid=cid,type='history'));return
            if result['verdict']=='passed' and not any(x.get('exit_code')==0 for x in row.get('validation_receipts',[]) if isinstance(x,dict)):
                result=dict(verdict='unverified',checks=result.get('checks',[]),issues=['验收没有返回实际运行检查的成功回执，不能判为通过'])
            self.put(original,review_state=result['verdict'],review_result=result)
            self.changed.emit(dict(cid=parent,type='review_completed',text=text,verdict=result['verdict'],review_cid=cid))
        else:
            from core.companion_policy import read_policy
            settings=getattr(getattr(getattr(self.panel,'pet',None),'_config',None),'_s',None)
            policy=read_policy(settings) if settings is not None else {}
            if (row['backend']=='dsh' and row.get('auto_review') and row.get('needs_review')
                    and policy.get('enabled') and policy.get('auto_review')):
                self.start_review(cid,turn,text)
            else:self.changed.emit(dict(cid=cid,type='completed',text=text,backend=row['backend']))

    def start_review(self,cid,turn,text):
        row=self.get(cid)
        with self.store.connect() as db:
            existing=db.execute('SELECT review FROM managed_reviews WHERE parent=? AND turn=?',(cid,turn)).fetchone()
            if existing:return existing[0]
            import uuid
            review_cid=uuid.uuid4().hex
            db.execute('INSERT INTO conversations(id,agent_id,name) VALUES (?,?,?)',(review_cid,'001','验收 · '+self.store.conversation(cid)['name'][:55]))
            db.execute('INSERT INTO managed_reviews VALUES (?,?,?)',(cid,turn,review_cid))
        review=self.get(review_cid);self.put(review,backend='codex',review_for=cid,auto_review=False,
            review_parent_started=row.get('started_at'))
        self.put(row,review_state='starting',review_cid=review_cid)
        prompt=('这是独立测试验收对话，你未参与实现。仅阅读成果和运行必要的有限检查，不修改源码、不提交、不发布。'
            '执行器的自述不是验收证据；对无法检查的项目如实说明。验证原需求、实际文件、关键行为和遗漏。'
            '至少实际运行一次文件检查或行为测试，没有执行回执不能通过。最终只输出JSON：{"verdict":"passed或failed或needs_input","checks":[实际执行的检查和结果],"issues":[问题]}。'
            '\n原始需求：\n'+row.get('goal','')[:6500]+'\n执行器返回（待核实）：\n'+str(text)[:6500])
        self.put(self.get(review_cid),goal=prompt,needs_review=False,state='dispatching',started_at=time.time())
        self.codex.start(prompt,review_cid,'001')
        self.changed.emit(dict(cid=cid,type='review_started',review_cid=review_cid,backend=row['backend']))
        return review_cid

    def poll(self):
        if self._closed:return
        selected=self.panel.active_id
        for row in self.rows():
            if row['backend']=='dsh' and row['state']=='running' or row['conversation_id']==selected:
                self.refresh(row['conversation_id'])

    def report_fish(self,cid):
        root=self.get(cid)
        rows=[r for r in self.rows() if r['backend']=='dsh' and (r['conversation_id']==cid or r.get('dispatch_parent')==cid)]
        if not rows:raise RuntimeError('这条对话尚未交给肥鱼DSH任务，不能虚构执行进度')
        row=max(rows,key=lambda r:r.get('updated',0));target=row['conversation_id']
        def report():
            from core.companion_report import compose
            view=self._gateway().session_view(row['external_id']) if row['external_id'] else {}
            facts=dict(goal=row.get('goal'),executor='DSH',state=row['state'],running=view.get('running'),
                independent_review=row.get('review_state','not_finished'),last_result=row.get('result','')[:3000])
            return '报告咕嘎指挥员，'+compose(facts)
        return self._work(target,'team_report',report)

    def close(self):self._closed=True;self.timer.stop()
