"""DSH uses Guga's existing executors; input never runs on Qt's UI thread."""
import json
from collections import deque
from core.desktop_access import DesktopAccess
from core.execution_contract import ExecutionLedger


class DshTools:
    def __init__(self, panel, manager):
        self.panel=panel; self.manager=manager
        self.ledger=ExecutionLedger(manager.state.get('goal',''))
        self.sent={}; self.inputs=deque(maxlen=4)
        self.started_media=False

    def execute(self,name,args):
        mgr=self.manager; cancel=mgr.stop_event
        if cancel.is_set():
            raise RuntimeError('任务已取消，不再执行后续工具')
        agent=self.panel.agent; ticket=mgr.state.get('ticket_id')
        if name=='personal_memory':
            owner=mgr.state['agent_id']; action=args.get('action')
            if action=='propose_lesson':return dict(ok=True,**mgr.archive.propose(owner,ticket,args.get('content','')))
            if action=='lessons':return dict(ok=True,lessons=mgr.archive.lessons(owner))
            return self.panel.runtime.companion_memory.execute(owner,mgr.state.get('conversation_id'),args)
        if name=='task_report':
            state=args.get('state','progress')
            summary=str(args.get('summary',''))[:3000]
            if state not in ('progress','ready','blocked','failed','needs_input'):
                raise ValueError('回执状态无效')
            blockers=self.ledger.blockers()
            mgr.state['report_state']=state
            from core.agent_memory import safe_text
            mgr.state['report_summary']=safe_text(summary)
            mgr.archive.update(ticket,summary=summary,evidence=self.ledger.summary()['completed'])
            mgr._publish(dict(type='dsh_event',job_id=mgr.state['job_id'],conversation_id=mgr.state.get('conversation_id'),
                              event=dict(type='report',state=state,summary=summary,blockers=blockers,milestone=bool(args.get('milestone')))))
            return dict(ok=True,recorded=True,blockers=blockers,
                        completion='交付仍需核验，不能把自述当作独立成功证据')
        if name=='task_plan':
            mgr.archive.update(ticket,summary=args.get('summary',''))
            return dict(ok=True,saved=True)
        if name=='media_workflow':
            value=self.panel.runtime.media.execute(args,cancel)
            if value.get('accepted'):
                self.started_media=True
                self.ledger.pending.append(dict(category='media',description='视频处理仍在后台进行，尚无可交付成片或字幕'))
            job=value.get('job',{})
            if job.get('state')=='ready':self.ledger.pending=[x for x in self.ledger.pending if x.get('category')!='media']
            if job.get('state') in ('failed','cancelled','interrupted'):
                self.ledger.failures['media']=job.get('error',job.get('message','媒体处理没有成功'))
        elif name=='web_search':
            from core.search_results import search
            value=search(args.get('query',''))
        elif name=='local_computer':
            args=dict(args)
            if args.get('action')=='run':
                args.setdefault('cwd',mgr.state['cwd'])
                args['seconds']=min(120,max(1,int(args.get('seconds',30))))
            output=DesktopAccess().execute(args,cancel)
            value=dict(ok=output.get('exit_code',0)==0 and not output.get('stopped'),result=output)
        elif name=='computer':
            signature=json.dumps({k:v for k,v in args.items() if k!='observation_id'},sort_keys=True,ensure_ascii=False)
            if args.get('action') in ('click','type','fill','hotkey','drag','uia'):
                self.inputs.append(signature)
                if len(self.inputs)==4 and len(set(self.inputs))==1:
                    raise RuntimeError('相同输入重复四次，停止此路径并检查实际效果')
            value=dict(ok=True,result=agent.computer.execute(args,cancel))
        elif name=='browser':
            from core.browser_agent import execute as browser_execute
            previous=getattr(self,'closed_tab_receipt',None)
            if args.get('action')=='close_tab' and previous is not None:
                value=dict(previous,duplicate_suppressed=True)
            else:
                value=agent._desktop_capability(lambda:browser_execute(agent.computer,args,cancel),cancel)
                if args.get('action')=='close_tab' and value.get('action_sent') is not False:
                    self.closed_tab_receipt=dict(value)
        elif name=='wechat':
            signature=json.dumps([args.get('recipient'),args.get('text')],ensure_ascii=False)
            if args.get('action')=='send' and signature in self.sent:
                value=dict(self.sent[signature],duplicate_suppressed=True)
            else:
                value=agent._desktop_capability(lambda:agent.wechat.execute(args,cancel),cancel)
                if value.get('send_clicked'):self.sent[signature]=dict(value)
        else:
            value=agent.bridge.call(name,args,cancel)
            if isinstance(value.get('result'),dict) and value['result'].get('ok') is False:
                value=dict(value,ok=False,error=value['result'].get('error','工具报告失败'))
        self.ledger.record(name,args,value)
        mgr.archive.receipt(ticket,name,args,value)
        # Only numeric/status receipts reach the voice reporter, never raw output.
        mgr._publish(dict(type='dsh_event',job_id=mgr.state['job_id'],conversation_id=mgr.state.get('conversation_id'),
                          event=dict(type='tool_receipt',receipt=dict(name=name,arguments={'action':args.get('action','')},
                                                                     result={'ok':value.get('ok') is True}))))
        return value
