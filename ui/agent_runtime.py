"""Application-owned tool registry; the same handlers serve typed and spoken commands."""
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QApplication
from core.embodiment import IDLE_GESTURES


class AppRuntime:
    def __init__(self, pet, panel, store):
        self.pet, self.panel, self.store = pet, panel, store
        from core.authorized_apps import AuthorizedApps
        self.apps = AuthorizedApps()
        from core.bg_director import BattlegroundDirector
        from core.bg_shadow import ShadowManager
        self.shadow=ShadowManager()
        self.battleground = BattlegroundDirector(panel)
        self.battleground.event.connect(panel._battleground_event)
        from core.dsh_worker import DshManager
        self.dsh = DshManager(panel)
        self.dsh.tools_enabled=True
        self.dsh.archive.recover_once()
        self.dsh.event.connect(panel._dsh_event)
        from core.codex_manager import CodexManager
        self.codex = CodexManager(panel)
        self.codex.update.connect(panel._codex_event)
        from core.screen_share import ScreenShareController
        self.screen_share=ScreenShareController(panel)
        self.screen_share.failed.connect(panel._call_screen_failed)
        from core.music_performance import MusicPerformance
        self.music=MusicPerformance(panel)
        self.music.performance_event.connect(panel._music_event)
        from core.media_workflow import MediaWorkflow
        self.media=MediaWorkflow(self.store.path.parent/'video-workflows')
        from core.workflow_catalog import WorkflowCatalog
        self.workflows=WorkflowCatalog(self.store.path.parent/'workflows.sqlite3')
        from core.daily_plan import DailyPlan
        self.daily_plan=DailyPlan(self.store.path.parent/'daily-plan.sqlite3')
        from core.companion_memory import CompanionMemory
        from core.connected_knowledge import ConnectedKnowledge
        self.connected_knowledge = ConnectedKnowledge(self.codex.bound)
        self.companion_memory = CompanionMemory(panel.memory, self.store, self.daily_plan,
                                                self.connected_knowledge)
        from core.dsh_link import DshLink
        self.dsh_link=DshLink(panel)
        import threading
        self.external_desktop_lock=threading.Lock()
        self.external_browser_cancel=threading.Event()
        from core.wechat_file_assistant import FileAssistant
        self.file_assistant = FileAssistant(panel, self.external_desktop_lock, self.companion_memory)
        QTimer.singleShot(900, self.file_assistant.restore)
        self._restore_upgrade_playback()

    def _restore_upgrade_playback(self):
        """Consume one short-lived local restart receipt; never start a voice call."""
        import json,time
        from pathlib import Path
        path=Path(__file__).resolve().parents[1]/'runtime/personal-harness/restart.json'
        if not path.is_file():return
        try:
            state=json.loads(path.read_text(encoding='utf-8'))
            path.unlink()
            music=state.get('music',{})
            if not 0<=time.time()-float(state.get('created',0))<120:return
            if music.get('playing') and isinstance(music.get('title'),str) and len(music['title'])<=80:
                def restore():
                    self.music.sing(music['title'])
                    QTimer.singleShot(1200,lambda:self.music.player.setPosition(max(0,int(music.get('position_ms',0)))))
                from PyQt5.QtCore import QTimer
                QTimer.singleShot(700,restore)
        except (OSError,ValueError,TypeError):pass

    def _pymaster(self,args):
        """PyMaster 教学站遥控：固定动作，页面文字只作资料。"""
        from core.pymaster_bridge import PyMasterBridge
        # 菜单和语音共用同一个桥，避免各自拉起一个浏览器
        bridge=getattr(self.pet,'_pymaster_bridge',None)
        if bridge is None:
            bridge=PyMasterBridge()
            self.pet._pymaster_bridge=bridge
        action=str(args.get('action','status')).strip().lower()
        try:
            if action=='status':
                return bridge.run('status')
            if action=='glossary':
                term=str(args.get('term') or args.get('query') or '').strip()
                if not term:raise ValueError('查词需要 term')
                return bridge.run('glossary',term=term)
            if action=='close_modal':
                return dict(bridge.run('close_modal'),action='close_modal')
            if action in ('open','state','chapter','kp','next','prev','open_exercise','set_code','run','explain'):
                if action=='open':
                    result=bridge.open(page=args.get('page'),chapter=args.get('chapter'))
                elif action=='chapter':
                    number=int(args.get('chapter') or 0)
                    if not number:raise ValueError('跳章需要 chapter（1 起）')
                    result=bridge.chapter(number)
                elif action=='kp':
                    result=bridge.kp(int(args.get('index') or 0))
                elif action in ('next','prev'):
                    result=bridge.run(action)
                elif action=='open_exercise':
                    result=bridge.open_exercise(kp=args.get('kp'),ex=int(args.get('ex') or 0))
                elif action=='set_code':
                    code=str(args.get('code') or '')
                    if not code.strip():raise ValueError('set_code 需要 code')
                    result=bridge.set_code(code)
                elif action=='run':
                    result=bridge.run_code(int(args.get('seconds') or 75))
                elif action=='explain':
                    result=bridge.explain_bundle(str(args.get('question') or ''))
                else:
                    result=bridge.state()
                return self._pymaster_view(action,result)
            raise ValueError('未知 PyMaster 操作：'+action)
        except Exception as exc:
            return dict(ok=False,action=action,error=type(exc).__name__+': '+str(exc)[:300])

    def _sticker_log(self,msg):
        """补帧/生图的进度，收集起来随结果一起给智能体，不打断对话。"""
        try:
            notes=getattr(self,'_sticker_notes',None)
            if notes is None:
                notes=[]; self._sticker_notes=notes
            notes.append(str(msg))
            del notes[:-12]
        except Exception:
            pass

    def _sticker(self,args):
        """表情包工坊：动作固定，结果直接给智能体读。"""
        from core import sticker_studio as studio
        action=str(args.get('action','list')).strip().lower()
        known=('list','import','import_folder','enable','disable','remove','probe',
               'doubao_status','doubao_check')
        if action not in known:
            return dict(ok=False,error='未知表情动作：'+action+'（可用：'+'/'.join(known)+'）')
        ok,why=studio.available()
        if not ok:return dict(ok=False,error=why)
        settle=lambda r:(self._sticker_after(r) if callable(getattr(self,'_sticker_after',None)) else r)
        try:
            if action=='list':
                items=studio.list_stickers()
                return dict(ok=True,total=len(items),
                    items=[dict(slug=it.get('slug'),name=it.get('name'),frames=it.get('frames'),
                                fps=it.get('fps'),weight=it.get('weight'),
                                enabled=bool(it.get('enabled')),created=it.get('created')) for it in items],
                    idle_clips=studio.manifest_clips(),
                    message=('表情库有 %d 个自定义表情'%len(items)) if items
                            else '表情库还是空的：可以说"把这个文件做成表情"')
            if action=='doubao_status':
                try:
                    from core import doubao_image as db
                    st=db.status()
                except Exception as exc:  # noqa: BLE001
                    return dict(ok=False,error='豆包客户端不可用：%s'%str(exc)[:70])
                try:
                    from core import motion_library as ml
                    lib=ml.summary()
                except Exception:  # noqa: BLE001
                    lib=''
                return dict(ok=True,ready=bool(st.get('ready')),reason=st.get('reason'),
                    motion_library=lib,
                    message=('豆包已登录，AI 重绘可用' if st.get('ready')
                             else '豆包还没登录：%s。在那之前只能用本地补帧'%st.get('reason')))
            source=str(args.get('source') or '').strip()
            if action in ('import','import_folder','probe','doubao_check') and not source:
                return dict(ok=False,error='需要 source：素材文件或目录的真实路径（用户没给就不要编）')
            if action=='doubao_check':
                # 只验"这张表情能不能传进豆包"，不生图也不写库
                try:
                    frames,_delays=studio.decode_source(source)
                except Exception as exc:  # noqa: BLE001
                    return dict(ok=False,error='解不出表情帧：%s'%str(exc)[:70])
                if not frames:
                    return dict(ok=False,error='这个素材里没有可用的帧')
                try:
                    from core import doubao_image as db
                    res=db.check_upload(frames[len(frames)//2],
                                        log=getattr(self,'_sticker_log',None))
                except Exception as exc:  # noqa: BLE001
                    return dict(ok=False,error='传图自检失败：%s'%str(exc)[:80])
                return dict(ok=bool(res.get('ok')),uploaded=int(res.get('uploaded') or 0),
                            ready=bool(res.get('ready')),shot=res.get('shot') or '',
                            message=res.get('reason') or '')
            if action=='probe':
                return studio.probe(source,args.get('fps') or studio.TARGET_FPS)
            if action=='import_folder':
                from pathlib import Path as _Path
                if not _Path(source).expanduser().is_dir():
                    return dict(ok=False,error='source 要是一个文件夹：%s'%source)
                ok_list,bad_list=studio.import_folder(source,
                    args.get('fps') or studio.TARGET_FPS,args.get('weight') or 1.0,
                    smooth=int(args.get('smooth') or 0),
                    doubao=bool(args.get('doubao')),
                    log=getattr(self,'_sticker_log',None))
                result=dict(ok=bool(ok_list),imported=[r['slug'] for r in ok_list],
                    imported_names=[r['name'] for r in ok_list],
                    skipped=[dict(name=n,reason=r) for n,r in bad_list],
                    message='导入 %d 个表情%s'%(len(ok_list),
                        ('，%d 个跳过（多半是已经导入过）'%len(bad_list)) if bad_list else ''))
                if ok_list and any((r.get('smooth') or 1)>1 for r in ok_list):
                    framed=[(r['name'],r['frames'],r['fps']) for r in ok_list]
                    result['smooth']=[dict(name=n,frames=f,fps=fp) for n,f,fp in framed]
                if not ok_list and not bad_list:
                    result.update(ok=False,error='这个文件夹里没有认出表情')
                return settle(result) if ok_list else result
            if action=='import':
                result=studio.import_sticker(source,args.get('name') or '',
                                             args.get('fps') or studio.TARGET_FPS,
                                             args.get('weight') or 1.0,args.get('slug') or '',
                                             smooth=int(args.get('smooth') or 0),
                                             doubao=bool(args.get('doubao')),
                                             motion_hint=str(args.get('hint') or ''),
                                             log=getattr(self,'_sticker_log',None))
                return settle(result)
            slug=str(args.get('slug') or '').strip()
            if not slug:return dict(ok=False,error='需要 slug：先用 list 看库里的 id')
            if action=='enable':return settle(studio.set_enabled(slug,True))
            if action=='disable':return settle(studio.set_enabled(slug,False))
            if action=='remove':return settle(studio.remove_sticker(slug))
            return dict(ok=False,error='未知表情动作：'+action)
        except studio.StickerError as exc:
            return dict(ok=False,action=action,error=str(exc))
        except Exception as exc:
            return dict(ok=False,action=action,error=type(exc).__name__+': '+str(exc)[:200])

    def _sticker_after(self,result):
        """清单改了就热重载待机链，省掉"导入后要重启"这一步。"""
        if not isinstance(result,dict) or not result.get('ok'):return result
        anim=getattr(getattr(self,'pet',None),'_anim',None)
        reload=getattr(anim,'reload_idle_clips',None)
        if reload is None:return result
        try:
            live=reload()
        except Exception:  # noqa: BLE001 - 热重载失败不该抹掉导入结果
            return result
        if live:
            result['live_idle_clips']=list(live)
            slug=result.get('slug')
            if slug and slug in live:
                result['message']=result.get('message','')+'（已即时接入待机链，不用重启）'
            elif slug:
                result['message']=result.get('message','')+'（已从待机链移除，立即生效）'
            elif result.get('imported'):
                result['message']=result.get('message','')+'（已即时接入待机链，不用重启）'
        return result

    @staticmethod
    def _pymaster_view(action,result):
        """压掉 DOM 噪音，只把智能体做判断需要的事实交回去。"""
        if not isinstance(result,dict):return dict(ok=False,error='PyMaster 桥返回了异常结果')
        view=dict(result)
        view['action']=action
        view.pop('state',None)
        kps=view.get('kps')
        if isinstance(kps,list) and len(kps)>20:
            view['kps']=kps[:20];view['kps_total']=len(kps)
        modal=view.get('modal')
        if isinstance(modal,dict) and not modal.get('open'):
            view['modal']=dict(open=False)
        playground=view.get('playground')
        if isinstance(playground,dict):
            playground=dict(playground)
            playground['code']=(playground.get('code') or '')[:1200]
            playground['output']=(playground.get('output') or '')[:1500]
            view['playground']=playground
        for key in ('kp_text','code','output','exercise_prompt'):
            if isinstance(view.get(key),str) and len(view[key])>2600:
                view[key]=view[key][:2600]+'…'
        return view

    def execute_internal(self, name, args):
        """Only the application's own ToolBridge uses this entry, never IPC args."""
        if (name=='codex_task' and args.get('action')=='start' and
                getattr(self.panel,'_active_workflow',None) and self.panel.busy):
            return self._handoff_workflow(args)
        if name in ('computer', 'wechat', 'browser'):
            if self.dsh.status().get('running') or getattr(self.panel, '_fast_inflight', False):
                raise RuntimeError('另一项任务正在操作桌面，本次没有发送输入')
            executor = getattr(self.panel.agent, 'computer' if name=='browser' else name, None)
            if executor is None:raise RuntimeError('桌面智能体尚未初始化')
            if not self.external_desktop_lock.acquire(blocking=False):
                raise RuntimeError('已有桌面操作正在执行，请等待本次回执')
            try:
                if name=='browser':
                    from core.browser_agent import execute
                    return execute(executor,args,self.panel.agent.cancel_event)
                return executor.execute(args, self.panel.agent.cancel_event)
            finally:self.external_desktop_lock.release()
        return self.execute(name, args)

    def _handoff_workflow(self,args):
        panel=self.panel
        source=panel.active_id
        run=(source,panel._task_started)
        prior=getattr(self,'_workflow_handoff',None)
        if prior and prior[0]==run:
            return dict(ok=True,accepted=True,conversation_id=prior[1],duplicate_suppressed=True)
        checkpoint=str(args.get('text') or '').strip()
        if not checkpoint:raise ValueError('接力需要已执行步骤、可核对的回执和当前卡点')
        workflow=panel._active_workflow
        prompt=('继续咕嘎的这次工作流。先核对交接中的结果，再从未完成处接手。'
                '已提交或结果不清的操作先查证；沿用本次任务授权的对象和范围。'
                '\n\n用户原任务\n'+panel._active_task_text[:3200]+
                '\n\n工作流步骤\n'+str(workflow.get('instructions') or '')[:5500]+
                '\n\n咕嘎的实查记录和卡点\n'+checkpoint[:5400])
        target=panel.managed.create('codex','接力 · '+str(workflow.get('title') or '工作流')[:48],
                                   project_id=self.store.conversation(source).get('project_id'))
        panel.managed.put(panel.managed.get(target),dispatch_parent=source)
        self._workflow_handoff=(run,target)
        try:
            panel.managed.send(target,prompt)
        except Exception:
            self._workflow_handoff=None
            raise
        return dict(ok=True,accepted=True,conversation_id=target,message='已请求新建 Codex 接力对话，我会继续跟进实际结果')

    def execute(self, name, args):
        if (name == 'codex_task' and
                getattr(self.panel,'_manager_enabled',False) and self.panel.active_agent_id=='001'):
            managed=self.panel.managed;cid=self.panel.active_id
            action=str(args.get('action') or ('run' if name=='code_task' else 'status'))
            if action in ('start','run','send'):
                text=str(args.get('text') or args.get('task') or '').strip()
                if not text or len(text)>15000:raise ValueError('任务需要 1—15000 个字符')
                target=managed.target(cid,text,backend='codex');managed.send(target,text)
                self.panel._managed_motion(target,'thinking',text)
                return dict(ok=True,accepted=True,backend='codex',conversation_id=target,
                    thread_id=managed.get(target).get('external_id',''),message='已交给当前 Codex 对话，等待真实回执')
            if action=='stop':return managed.stop(cid)
            if action=='status':return dict(ok=True,task=managed.get(cid),backend='codex')
            if action=='open' and not managed.get(cid).get('external_id'):
                managed.provision(cid);return dict(ok=True,accepted=True)
            return self.codex.execute(args,conversation_id=cid,agent_id='001')
        if name=='chat_control':
            if not hasattr(self,'chat_control'):
                from core.chat_control import ChatControl
                self.chat_control=ChatControl(self.panel)
            return self.chat_control.execute(args)
        if name=='codex_task':
            return self.codex.execute(args,conversation_id=self.panel.active_id,
                                      agent_id=self.panel.active_agent_id)
        if name=='personal_memory':
            return self.companion_memory.execute(self.panel.active_agent_id,self.panel.active_id,args)
        if name=='dsh_link':return self.dsh_link.execute(args)
        if name=='agent_skills':
            from core.connected_knowledge import installed_skills
            return dict(ok=True, skills=installed_skills(args.get('query')))
        if name=='workflow_catalog':
            action = args['action']
            if action=='list':
                return dict(ok=True,workflows=[{key: row.get(key) for key in ('id','title','trigger','state')}
                                               for row in self.workflows.list()])
            if action=='get':
                ident=args.get('id')
                for row in self.workflows.list():
                    if row['id']==ident:
                        return dict(ok=True,workflow=row)
                raise ValueError('找不到该工作流')
            if action=='define':
                row=self.workflows.define(args.get('trigger',''),args.get('instructions',''),args.get('title',''),state='candidate')
                return dict(ok=True,workflow=row)
            raise ValueError('未知工作流操作')
        if name=='daily_plan':
            args=dict(args)
            message_only=args.pop('message_only',None)
            reminder_answer=args.pop('reminder_answer',False)
            if args.pop('open_window',False) or args.get('action')=='open':
                self.pet._show_daily_plan()
                args['action']='list'
            result=self.daily_plan.execute(args)
            if message_only:result['message']=message_only
            if reminder_answer:self.daily_plan.clear_reminder(self.panel.active_id)
            dialog=getattr(self.pet,'_daily_plan_dialog',None)
            if dialog is not None and dialog.isVisible():dialog.refresh()
            return result
        if name in ('computer','wechat','browser'):
            import threading
            if self.panel.busy or getattr(self.panel,'_fast_inflight',False) or self.dsh.status().get('running'):
                raise RuntimeError('咕嘎已有任务占用桌面，请等待结束或先停止；本次未发送输入')
            executor=getattr(self.panel.agent,'computer' if name=='browser' else name,None)
            if executor is None:raise RuntimeError('桌面智能体尚未初始化')
            if not self.external_desktop_lock.acquire(blocking=False):
                raise RuntimeError('已有外部桌面操作正在执行，本次未发送输入')
            try:
                if name=='browser':
                    from core.browser_agent import execute
                    return execute(executor,args,self.external_browser_cancel)
                return executor.execute(args,threading.Event())
            finally:self.external_desktop_lock.release()
        if name=='pet_say':
            text=str(args.get('text','')).strip()
            if not text:raise ValueError('text不能为空')
            self.panel._show_active_bubble(text[:1200])
            return dict(ok=True,shown=True,chars=len(text[:1200]))
        if name=='screen_context':
            from core.super_agent import screen_context as screen_ctx
            action=str(args.get('action','status')).lower()
            try:
                if action in ('start','watch'):return dict(ok=True,**screen_ctx.start_watching('mcp'))
                if action in ('stop','unwatch'):return dict(ok=True,**screen_ctx.stop_watching())
                if action=='summary':return dict(ok=True,summary=screen_ctx.summary_line(),stats=screen_ctx.get_watcher().status())
                if action in ('context','status'):
                    return dict(ok=True,active=screen_ctx.get_watcher().active,context=screen_ctx.context(),
                        stats=screen_ctx.get_watcher().status(),summary=screen_ctx.summary_line())
            except Exception as exc:
                return dict(ok=False,error=type(exc).__name__+': '+str(exc))
            raise ValueError('未知屏幕感知操作')
        if name=='desktop_command':
            from core.super_agent import desktop_command
            return desktop_command.handle(args.get('action','list'),args.get('target') or args.get('name',''),args.get('query',''))
        if name=='pymaster':
            return self._pymaster(args)
        if name=='sticker':
            return self._sticker(args)
        if name=='lingshu_memory':
            from core.super_agent import lingshu_memory
            return lingshu_memory.handle(args.get('action','status'),args.get('query',''),args.get('title',''),args.get('content',''))
        if name=='dsh_gateway':
            from core.super_agent import dsh_gateway as gateway
            action=str(args.get('action','status')).strip().lower()
            try:
                client=gateway.DshGateway()
                if action in ('status','list'):
                    items=client.session_summaries()
                    items=sorted(items,key=lambda item:float(item.get('updatedAt') or 0),reverse=True)
                    slim=[]
                    for item in items[:40]:
                        slim.append(dict(session_id=str(item.get('sessionId') or ''),
                            running=bool(item.get('running')),cwd=str(item.get('cwd') or '')[:140],
                            title=gateway.session_title(item)[:80]))
                    return dict(ok=True,total=len(items),sessions=slim,
                        focused=getattr(self.dsh_link,'focused_session',''),
                        current=client.current_session_id())
                if action=='session':
                    session_id=str(args.get('session_id') or '').strip()
                    if not session_id:
                        session_id=gateway.session_id_from_focused(
                            getattr(self.dsh_link,'focused_session',''),client.session_summaries())
                    if not session_id:
                        session_id=client.current_session_id()
                    if not session_id:raise ValueError('没有可读会话：请提供 session_id')
                    return dict(ok=True,**client.session_view(session_id))
                if action=='cancel':
                    return dict(ok=True,**client.cancel(args.get('session_id')))
                if action=='prompt':
                    session_id=str(args.get('session_id') or '').strip()
                    text=str(args.get('text') or '').strip()
                    if not session_id or not text:raise ValueError('prompt 需要 session_id 与 text（不允许猜目标会话）')
                    return dict(ok=True,**client.prompt(session_id,text,mode=args.get('mode') or 'queue'))
                if action in ('create','new','new_session'):
                    # 新建一个空会话。这是"在 DSH 里开个新对话"的正路，
                    # 不需要去 DSH 窗口里用电脑控制点按钮（那条路会因为窗口没焦点失败）。
                    created=client.create_session()
                    session_id=str(created.get('session_id') or '')
                    if not session_id:
                        raise RuntimeError('网关没有返回新的 session_id')
                    # created 自带 ok 键：直接展开再补 ok=True 会 TypeError（重复关键字）。
                    return dict(created,session_id=session_id,created=True)
                if action=='start':
                    text=str(args.get('text') or '').strip()
                    if not text:raise ValueError('start 需要 text：第一条要投递给新会话的指令')
                    created=client.start_task(text,mode=args.get('mode') or 'queue')
                    # start_task 的返回值自带 ok（投递失败时是 False），不能再补一个 ok。
                    return dict(created)
            except Exception as exc:
                return dict(ok=False,error=type(exc).__name__+': '+str(exc)[:300])
            raise ValueError('未知 DSH 网关操作')
        if name=='workflow':
            from core import action_workflow as aw

            def _tool_workflow_names():
                from core.super_agent import tool_workflow as tw
                if not tw.DEFAULT_DIR.is_dir():return []
                return sorted(path.stem for path in tw.DEFAULT_DIR.glob('*.json'))

            def _tool_workflow_runs():
                from core.super_agent import tool_workflow as tw
                return tw.last_runs()
            action=str(args.get('action','list')).strip().lower()
            try:
                store=aw.WorkflowStore()
                if action=='plan':
                    # 工具调用型工作流（browser/local_computer/media_workflow 组合）：只做静态校验
                    from core.super_agent import tool_workflow as tw
                    slug=str(args.get('slug') or '').strip()
                    if not slug:raise ValueError('plan 需要 slug（action=list 会列出可用的）')
                    return dict(ok=True,**tw.dry_run(tw.load(slug)))
                if action=='run':
                    # 按步执行工具调用型工作流：确认闸默认停下，任何一步失败即停
                    from core.super_agent import tool_workflow as tw
                    slug=str(args.get('slug') or '').strip()
                    if not slug:raise ValueError('run 需要 slug（action=list 会列出可用的）')
                    return tw.run(tw.load(slug), lambda tool,arguments: self.execute(tool,arguments),
                        allow_confirm=bool(args.get('allow_confirm')),
                        allow_dangerous=bool(args.get('allow_dangerous')),
                        start_at=int(args.get('start_at') or 1),
                        ledger=tw.DEFAULT_DIR/'receipts.jsonl')
                if action in ('list','status'):
                    rows=[]
                    for path in sorted(store.root.glob('*.json')):
                        try: wf=store.load(path.stem)
                        except Exception: continue
                        if action=='status' and str(args.get('slug') or '').strip() and wf.get('slug')!=str(args.get('slug')).strip():
                            continue
                        rows.append(dict(slug=wf.get('slug'), name=wf.get('name'),
                            steps=len(wf.get('steps') or []),
                            triggers=list(wf.get('trigger_phrases') or [])[:4],
                            success=int(wf.get('success_count') or 0),
                            failure=int(wf.get('failure_count') or 0),
                            last_ok=bool(wf.get('last_ok_at'))))
                    return dict(ok=True,total=len(rows),workflows=rows,root=str(store.root),
                        tool_workflows=_tool_workflow_names(),
                        tool_workflow_runs=_tool_workflow_runs())
                if action=='play':
                    slug=str(args.get('slug') or '').strip()
                    if not slug:raise ValueError('play 需要 slug（先用 action=list 查有哪些）')
                    report=aw.play_workflow(slug,dry_run=bool(args.get('dry_run')))
                    return dict(ok=bool(report.get('ok')),slug=slug,report=report)
                if action=='compile':
                    text=str(args.get('text') or '').strip()
                    if not text:raise ValueError('compile 需要 text：把要做的那套操作完整说出来')
                    triggers=[t.strip() for t in str(args.get('triggers') or '').replace('，',',').split(',') if t.strip()]
                    wf=aw.compile_from_text(text,name=args.get('title') or None,
                        trigger_phrases=triggers or None,slug=args.get('slug') or None)
                    return dict(ok=True,slug=wf.get('slug'),name=wf.get('name'),
                        steps=len(wf.get('steps') or []),workflow=wf)
                raise ValueError('未知工作流操作')
            except Exception as exc:
                return dict(ok=False,error=type(exc).__name__+': '+str(exc)[:300])
        if name=='screen_share':
            action=args.get('action')
            if action=='start':
                if not self.panel.voice_active:
                    raise RuntimeError('共享屏幕只在长语音对话中自动启用；当前没有长语音通话')
                if self.panel._native_video_call:
                    if not self.panel.voice or not self.panel.voice.connected:
                        raise RuntimeError('通话接通后可以共享屏幕')
                    result = self.screen_share.start(2, 'call', native_video=True)
                else:
                    result = self.screen_share.start(args.get('fps'), 'call')
                self.panel._screen_share_manual_stop = False
                self.panel._set_call_share_control(True)
                return result
            if action=='stop':
                self.panel.toggle_call_screen_share(False)
                return self.screen_share.status()
            if action=='status':return self.screen_share.status()
            if action=='context':return self.screen_share.context(max(1,min(60,int(args.get('max_age',12)))))
            if action=='assist':return self.screen_share.assist_context(max(1,min(60,int(args.get('max_age',12)))))
            if action=='observation':return self.screen_share.observation(max(1,min(60,int(args.get('max_age',12)))))
            if action=='snapshot':return self.screen_share.snapshot()
            raise ValueError('未知共享屏幕操作')
        if name=='voice_session':
            action=args.get('action')
            voice=self.panel.voice
            if action=='status':
                from core.realtime_voice import load_settings
                cfg=load_settings()
                return dict(active=bool(self.panel.voice_active and voice and voice.active),
                    connected=bool(voice and voice.connected),
                    reconnecting=bool(voice and voice.active and not voice.connected),
                    starting=bool(self.panel.voice_active and voice and not voice.active),
                    last_error=(voice.last_error if voice else ''),provider=cfg.get('provider'),
                    model=cfg.get('model'),free_only=bool(cfg.get('free_only',True)),
                    quota_confirmation_matches=(cfg.get('free_quota_confirmed_provider')==cfg.get('provider') and
                        cfg.get('free_quota_confirmed_model')==cfg.get('model')),
                    usage=dict(rounds=voice._usage_rounds,reported_total_tokens=voice._usage_total,
                        unreported_rounds=voice._usage_missing) if voice else None)
            if action=='start':
                if self.panel.voice_active or self.panel.busy or self.panel.short_voice.active:
                    raise RuntimeError('当前已有通话、任务或录音；未启动新语音会话')
                if self.pet._realtime_active() or getattr(self.pet,'_recording',False):
                    raise RuntimeError('另一种语音模式仍在使用麦克风；未启动新会话')
                from core.realtime_voice import load_settings,connection_url
                cfg=load_settings();connection_url(cfg)
                if cfg.get('free_only',True) and (cfg.get('free_quota_confirmed_provider')!=cfg.get('provider') or
                        cfg.get('free_quota_confirmed_model')!=cfg.get('model')):
                    raise RuntimeError('当前免费额度保护未匹配所选模型；已阻止语音联网')
                from PyQt5.QtCore import QTimer
                QTimer.singleShot(0,lambda:self.pet._open_agent_voice())
                return dict(accepted=True,started=False,message='已请求咕嘎打开实时通话；请查询status确认麦克风与连接状态')
            if action=='stop':
                if not self.panel.voice_active:
                    return dict(stopped=False,active=False,message='当前没有活动的实时通话')
                from PyQt5.QtCore import QTimer
                QTimer.singleShot(0,lambda:self.pet._end_agent_voice())
                return dict(stopped=True,active=True,message='已请求结束实时通话；请查询status确认会话已断开')
            raise ValueError('未知实时语音操作')
        if name=='battleground' and args.get('action') in ('shadow','shadow_stop'):
            if args.get('action')=='shadow_stop':return self.shadow.stop()
            return self.shadow.start(args.get('seconds',900),bool(args.get('train',True)))
        if name=='music_performance':
            action=args.get('action')
            if action=='status':return self.music.status()
            if action=='stop':return self.music.stop()
            if action=='pause':return self.music.pause()
            if action=='resume':return self.music.resume()
            if action=='sing':return self.music.sing(str(args.get('title','')).strip())
            raise ValueError('未知音乐操作')
        if name=='media_workflow':
            return self.media.execute(dict(args),self.panel.agent.cancel_event)
        if name=='code_task':
            # 把"改一个项目"交给本机 DSH 跑；这个 handler 会阻塞很久，所以后台线程起飞、status 查结果
            import threading, time as _time
            action=str(args.get('action','run')).strip().lower()
            if action=='status':
                job=dict(getattr(self,'_code_job',None) or {})
                return dict(ok=True, job=job)
            if action=='stop':
                cancel=getattr(self,'_code_cancel',None)
                if not cancel or not (getattr(self,'_code_job',None) or {}).get('running'):
                    return dict(ok=True, stopped=None, message='没有正在跑的编码任务')
                cancel.set()
                return dict(ok=True, stopping=True,
                            message='已请求中止；DSH 会话里已做的改动不会自动回滚，需要你自己看')
            if action=='run':
                from core.coding_agent import CodingAgent, write_receipt
                job=getattr(self,'_code_job',None) or {}
                if job.get('running'):raise RuntimeError('已经有一个编码任务在跑；先 action=status 看进度')
                task=str(args.get('task') or '').strip()
                cwd=str(args.get('cwd') or '').strip() or str(self.panel.store.root if hasattr(self.panel.store,'root') else '')
                if not task:raise ValueError('code_task 需要 task：说清要改什么')
                if not cwd:raise ValueError('code_task 需要 cwd：项目目录绝对路径')
                check_cmd=str(args.get('check_cmd') or '').strip() or None
                fresh=str(args.get('fresh') or '').strip().lower() in ('1','true','yes')
                sessions=getattr(self,'_code_sessions',None)
                if sessions is None:
                    sessions={}; self._code_sessions=sessions
                # 同一个项目再来一次就复用上次的会话：上下文不丢，也不在 DSH 里堆一串不结束的会话
                reuse=None if fresh else sessions.get(cwd)
                cancel=threading.Event()
                self._code_cancel=cancel
                self._code_job=dict(running=True, ok=None, session_id=reuse or '', answer='', cwd=cwd,
                                    started=_time.time(), task=task[:300], check_cmd=check_cmd or '',
                                    reused=bool(reuse))
                holder=self._code_job
                def work():
                    try:
                        report=CodingAgent().run(task, cwd, check_cmd=check_cmd,
                                                 cancel=cancel, session_id=reuse)
                        report.setdefault('task', task[:200])
                        if report.get('session_id'):
                            sessions[cwd]=report['session_id']
                        holder.update(report, running=False)
                        try:
                            write_receipt(report)
                        except OSError:
                            pass   # 台账写不进去不能影响任务结果
                    except Exception as exc:
                        holder.update(running=False, ok=False,
                                      error=type(exc).__name__+': '+str(exc)[:300])
                threading.Thread(target=work, name='guga-code-task', daemon=True).start()
                return dict(ok=True, accepted=True, cwd=cwd, reused=bool(reuse),
                            message='已交给本机 DSH 跑，用 action=status 查；终态才有结论，受理不等于做完')
            raise ValueError('未知编码操作')
        if name == 'dsh_task':
            action=args.get('action')
            if action=='status':return self.dsh.status()
            if action=='stop':return self.dsh.stop()
            if action=='start':
                check_cmd=str(args.get('check_cmd') or '').strip()
                accepted=self.dsh.start(args.get('task',''),self.panel.active_id,args.get('cwd'),
                    args.get('seconds',900),args.get('session_id'),profile=dict(self.panel.active_profile),
                    memory_context=self.companion_memory.context(self.panel.active_agent_id,args.get('task',''),self.panel.active_id),
                    workflow=self.workflows.match(args.get('task','')),
                    checks=[check_cmd] if check_cmd else [])
                self.pet._multi_agent.work_state(self.panel.active_agent_id,'thinking','dsh:'+accepted['job_id'])
                return accepted
            raise ValueError('未知 DSH 操作')
        if name == 'battleground':
            action = args.get('action')
            if action == 'status':
                return dict(self.battleground.status(),shadow=self.shadow.status())
            if action == 'stop':
                return self.battleground.stop()
            if action == 'open':
                from core.bg_live_windows import HearthstoneWindow
                return dict(message=HearthstoneWindow().open())
            self.battleground.conversation_id = self.panel.active_id
            return self.battleground.start(action,
                args.get('max_actions',120 if action=='auto' else 12),
                args.get('seconds',1200 if action=='auto' else 240))
        if name == 'applications':
            if args.get('action')=='command':
                from core.fast_commands import match
                text=str(args.get('name',''))
                if not match(text).get('matched'):
                    # 反射动作与已录好的长任务工作流同样走本地零模型通道。
                    from core.reflex import match as reflex_match
                    if not reflex_match(text).get('matched'):
                        raise ValueError('仅支持已识别的本地快速命令或已录好的工作流；本操作不会转交大模型')
                import uuid
                if self.panel.busy or getattr(self.panel,'_fast_inflight',False):
                    raise RuntimeError('咕嘎正在执行其他任务，本次未提交快速命令')
                receipt=uuid.uuid4().hex
                self.panel.send(text,execution_context={'_receipt_id':receipt,'_external_fast':True})
                result=dict(self.panel._last_fast_result or {})
                if result.get('receipt_id')!=receipt:
                    result=dict(ok=True,execution_pending=True,receipt_id=receipt,message='命令已提交，等待实际结果')
                return dict(result,route='local_fast_command')
            if args.get('action') == 'list':
                entries=self.apps.available(args.get('query',''))
                from core.window_inventory import list_windows
                running=list_windows(args.get('query','')) if self.apps.access.enabled else []
                return {'authorized_apps': list(entries)[:200], 'total':len(entries), 'running_windows':running,
                    'access':self.apps.access.status(),
                    'note':'running_windows是已打开软件，authorized_apps是可启动软件；先复用已打开窗口。'}
            if args.get('action') == 'open':
                result=self.apps.open(args['name'])
                if result.get('verified_running') or result.get('selection_required'):return result
                import time
                from core.window_inventory import list_windows
                from pathlib import Path
                query=str(args['name'])
                if Path(query).is_absolute():query=Path(query).stem
                deadline=time.monotonic()+8
                while time.monotonic()<deadline:
                    windows=list_windows(query)
                    if windows:return dict(result,ok=True,verified_running=True,windows=windows[:5],message='已核对目标软件窗口出现')
                    time.sleep(.3)
                return dict(result,ok=False,verified_running=False,error='已请求启动，但8秒内未核对到目标软件窗口；不要盲目重复启动')
            raise ValueError('未知软件操作')
        if name == 'confirm_external':
            if self.apps.access.enabled:
                return {'approved':True,'source':'user_enabled_full_access'}
            from PyQt5.QtWidgets import QMessageBox
            from PyQt5.QtCore import QTimer
            import json
            box = QMessageBox(self.panel)
            box.setWindowTitle('咕嘎 · 确认软件操作')
            box.setText('此工具尚未被标记为可直接执行的常规操作，是否允许本次调用？')
            box.setInformativeText(args['server'] + ' / ' + args['tool'] + '\n' + json.dumps(args.get('arguments', {}), ensure_ascii=False)[:1500])
            box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
            box.setDefaultButton(QMessageBox.No)
            QTimer.singleShot(30000, box.reject)
            cancel_timer = QTimer(box)
            cancel_timer.setInterval(100)
            cancel_timer.timeout.connect(lambda: box.reject() if self.panel.agent.cancel_event.is_set() else None)
            cancel_timer.start()
            approved = box.exec_() == QMessageBox.Yes
            cancel_timer.stop()
            return {'approved': approved}
        if name == 'app_state':
            import os
            from core.authorized_apps import ROOT
            from core.realtime_agent import ROUTER_VERSION
            from core.self_agent import HARNESS_VERSION
            daily_dialog=getattr(self.pet,'_daily_plan_dialog',None)
            from core.model_settings import public_settings
            return dict(runtime=dict(pid=os.getpid(),project_root=str(ROOT),voice_router_version=ROUTER_VERSION,
                                     daily_plugin_version='2026-11-10.daily-dsh.1'),
                        desktop_entry_version='2026-11-10.independent-chat-memory.4',
                        companion_repair_version='2026-11-14.companion-closure.1',
                        browser_bubble_version='2026-11-14.existing-browser-web-bubble.1',
                        public_reporting=dict(getattr(getattr(self.panel,'public_reporter',None),'last_receipt',{})),
                        result_board=dict(visible=bool(getattr(self.panel,'result_board',None) and self.panel.result_board.isVisible()),
                            open_receipt=dict(getattr(getattr(self.panel,'result_board',None),'open_receipt',{}))),
                        models=public_settings(),
                        model_answer=dict(getattr(self.panel.answer,'last_receipt',{})),
                        desktop=dict(chat_visible=self.panel.isVisible(),chat_hwnd=int(self.panel.winId()),
                                     pet_hidden=bool(getattr(self.pet,'_desktop_hidden',False)),
                                     platform=QApplication.platformName()),
                        daily_plan=dict(self.daily_plan.snapshot(),dialog_visible=bool(daily_dialog and daily_dialog.isVisible()),
                                        dialog_hwnd=int(daily_dialog.winId()) if daily_dialog else None),
                        agent_harness_version=HARNESS_VERSION,
                        companion_memory=dict(position='管家与陪伴伙伴',scopes=['user','project','conversation'],
                            dialogue_source='current_conversation_only',daily_plan_source=str(self.daily_plan.path)),
                        pet=dict(x=self.pet.x(), y=self.pet.y(), visible=self.pet.isVisible(),
                                opacity=self.pet.windowOpacity(), status=self.pet._status.snapshot(),
                                volume=self.pet._tts._player.volume()),
                        current_conversation_id=self.panel.active_id,
                        codex=dict(version='2026-11-13.codex-desktop.1',
                                   task=self.codex.bound(self.panel.active_id)),
                        voice_active=self.panel.voice_active,
                        local_voice=dict(active=bool(self.panel.short_voice.active),
                                         mode='click_record_send',phase=self.panel.short_voice.phase,
                            recognition=('whisper-small' if __import__('core.local_transcription',fromlist=['available']).available() else 'vosk'),speech='edge-tts',realtime_quota_used=False),
                        voice_path=dict(owner_only=bool(self.panel.voice._cfg.get('owner_only',False)) if self.panel.voice else False,
                            speaker_gate_active=bool(self.panel.voice and self.panel.voice._speaker_gate),
                            interaction=self.panel.voice.interaction_status() if self.panel.voice else None),
                        workspace=self.store.snapshot(),
                        authorized_apps=list(self.apps.entries()),
                        battleground=self.battleground.status(),
                        dsh=self.dsh.status(),dsh_link=self.dsh_link.status(),
                        screen_share=dict(self.screen_share.status(),
                            call_expected=bool(self.panel.voice_active and (self.screen_share.status().get('active')
                                if self.panel._native_video_call else not self.panel._screen_share_manual_stop)),
                            watchdog_active=self.panel._screen_share_watchdog_timer.isActive(),
                            start_error=getattr(self.panel,'_screen_share_start_error',''),
                            voice_bridge=self.panel.voice.screen_bridge_status() if self.panel.voice else dict(attached=False),
                            recovery_failures=getattr(self.panel,'_screen_share_recovery_failures',0)),
                        performance=dict(avatar_idle_fps=round(1000/self.pet._anim2d._idle_interval_ms,1),
                            avatar_active_fps=round(1000/self.pet._anim2d._active_interval_ms,1),
                            avatar_interval_ms=self.pet._anim2d._timer.interval(),gaze_interval_ms=self.pet._gaze_timer.interval(),
                            background_interval_ms=self.pet._bg_timer.interval(),battleground_interval_ms=self.pet._watch_timer.interval(),
                            hidden_animation_paused=not self.pet.isVisible()),
                        media_workflow=self.media.status(),
                        personal_harness=dict(backend='auto:local-first',tickets=self.dsh.archive.recent(self.panel.active_agent_id,limit=5)),
                        assistant_contract=dict(version=HARNESS_VERSION,routing='local_first_dsh_for_complex_engineering',
                            browser_control=dict(mode='current_page_single_step',task_model=False,
                                success_feedback='immediate_bubble',manual_quiet_seconds=60),
                            desktop_control=dict(version=ROUTER_VERSION,mode='foreground_single_step',
                                task_model=False,numbered_targets=True,visual_fallback='on_native_target_missing',
                                success_feedback='immediate_bubble_no_audio',automatic_retry=False,
                                workflow_recording=True),
                            conversation=dict(mode='awake' if self.panel.voice_active else 'companion',
                                companion_interval_seconds=self.panel.proactive.interval_seconds,
                                news_refresh_seconds=self.panel.proactive.news_refresh_seconds,
                                task_progress_seconds=30,foreground_qa_independent=True,
                                hold_to_talk_in_speech_bubble=False,click_to_talk_in_speech_bubble=True,speech_bubble_count=1,
                                qa_search='public_snippets_with_sources',profile_memory='explicit_user_facts'),
                            voice_control='local_mcp_start_status_stop_free_guard_v1',
                            fast_action_running=bool(getattr(self.panel,'_fast_inflight',False) or getattr(self.panel,'_fast_launch_count',0)),
                            failure_explanations='host_receipts_even_when_model_unavailable',
                            close_page='one_selected_tab_then_verify',
                            screen_share=dict(capture_fps=self.screen_share.fps,range=[2,5],max_visual_calls_per_minute=self.screen_share.max_calls_per_minute,
                                storage='memory_only',visible_preview=False,start_policy='long_voice_call',
                                visual_bridge=('native_video' if self.panel._native_video_call else 'change_sampling_primary_chat_fallback'),
                                audio_model_direct_video=self.panel._native_video_call),
                            isolation=dict(desktop='host',tab_worker='separate_process_7s',
                                           security_sandbox=False)),
                        music=self.music.status(),
                        stage=dict(personality=self.pet.personality_status(),
                                   executor_appearance=getattr(self.pet._anim2d,'executor_appearance','guga'),
                                   executor_mode=self.pet._config._s.value('team/executor','self'),
                                   speech_face='original_open_eyes',
                                   sunglasses=self.pet._anim.sunglasses,dancing=self.pet._anim.dancing,
                                   pet_scale=getattr(self.pet,'_pet_scale',1.),
                                   floating_ui_version='2026-10-29.7',
                                   task_motion=self.pet._anim2d._task_motion.snapshot(),
                                   task_animation=self.pet._anim2d.task_animation_status(),
                                   gesture_version='2026-11-02.1',
                                   selection_trigger='uia_text_geometry_before_capture',
                                   selection_actions=['解释','截图提问'],
                                   menu_version='2026-11-02.3',
                                   selection_reading='verified_text_first_cached_image_for_question',
                                   selection_status=getattr(getattr(self.pet,'_screen_tools',None),'last_selection_status','unavailable'),
                                   selection_probe=dict(getattr(getattr(self.pet,'_screen_tools',None),'selection_diagnostics',{})),
                                   chat_route=self.panel.route_combo.currentData(),
                                   answer_effort=self.panel.effort_combo.currentData(),
                                   short_voice_active=self.panel.short_voice.active,
                                   proactive=self.panel.proactive.status(),
                                   selection_toolbar_enabled=bool(getattr(getattr(self.pet,'_screen_tools',None),'selection_enabled',False)),
                                   selection_hook_active=bool(getattr(getattr(self.pet,'_screen_tools',None),'selection_hook_ok',False)),
                                   screen_hotkey_conflicts=list(getattr(getattr(self.pet,'_screen_tools',None),'hotkey_errors',[])),
                                   expression=self.pet._anim.expression,
                                   appearance='3d' if self.pet._anim is self.pet._anim3d else 'illustration',
                                   motion_version='2',demo_active=bool(getattr(self.pet,'_avatar_demo_running',False)),
                                   last_demo=getattr(self.pet,'_last_avatar_demo',None)),
                        multi_agent=getattr(getattr(self.pet,'_multi_agent',None),'status',lambda:{})(),
                        active_agent=dict(id=self.panel.active_agent_id,profile=self.panel.active_profile),
                        local_access=self.apps.access.status(),
                        task=self.panel.agent.task_status(),
                        wechat_file_assistant=self.file_assistant.status(),
                        capabilities=['chat_control','dsh_gateway','pet_control','pet_say','daily_plan','personal_memory', 'workspace', 'applications', 'self_ask', 'ecosystem','battleground','dsh_task','codex_task','local_computer','computer','screen_share','task_plan','media_workflow','workflow_catalog','wechat_send_self','wechat','browser','web_search','music_performance'],
                        scope='本机能力由完整访问开关控制；继承咕嘎进程实际Windows权限')
        if name == 'browser':
            # 2026-11-12 修复：capabilities 里一直声明着 browser，但 execute 里没有
            # 对应分支，调用会掉到末尾 raise ValueError('该工具未注册')。
            # 而 SelfAgent 路径是直接调 browser_agent.execute 的（core/self_agent.py
            # 里 name == 'browser' 的分支），所以切到 smolagents 内核（ConsoleWorker）
            # 后 browser 就成了"注册了名字、没有实现"的幽灵工具。
            # 用户说"在浏览器里进入b站"时，模型发来的调用就是被这里挡掉的。
            import threading
            from core.browser_agent import execute as browser_execute
            if self.panel.busy or getattr(self.panel,'_fast_inflight',False) or self.dsh.status().get('running'):
                raise RuntimeError('咕嘎已有任务占用桌面，本次没有发送外部浏览器操作')
            executor = getattr(self.panel.agent, 'computer', None)
            if executor is None:
                raise RuntimeError('桌面智能体尚未初始化')
            if not self.external_desktop_lock.acquire(blocking=False):
                raise RuntimeError('已有外部桌面操作正在执行，本次未发送浏览器操作')
            try:
                return browser_execute(executor, args, threading.Event())
            finally:
                self.external_desktop_lock.release()
        if name == 'pet_control':
            return self._pet(args)
        if name == 'workspace':
            return self._workspace(args)
        raise ValueError('该工具未注册')

    def _pet(self, args):
        pet = self.pet
        action = args.get('action')
        if action == 'move':
            x, y = int(args['x']), int(args['y'])
            screens = QApplication.screens()
            screen = next((s for s in screens if s.availableGeometry().contains(x, y)), QApplication.primaryScreen())
            g = screen.availableGeometry()
            x = max(g.left(), min(x, g.right() - pet.width() + 1))
            y = max(g.top(), min(y, g.bottom() - pet.height() + 1))
            pet.move(x, y)
            pet._save_state()
            return dict(x=pet.x(), y=pet.y())
        if action == 'show':
            pet.set_pet_hidden(False)
            pet._force_top()
        elif action == 'hide':
            pet.set_pet_hidden(True)
        elif action == 'reset_position':
            pet._reset_pos()
        elif action == 'animate':
            name = args.get('name', 'bounce')
            if name not in ('entrance','poke','work','walk','look_up_step','bounce', 'shake', 'point', 'peek', 'tsundere','head_shake','nod','tilt','wave','sunglasses','remove_sunglasses','dance','stop_dance','sneak_peek') + IDLE_GESTURES:
                raise ValueError('未知动画')
            if str(getattr(self.panel,'active_agent_id','001'))!='001' and hasattr(pet,'_multi_agent'):
                pet._multi_agent.gesture(self.panel.active_agent_id,name)
            else:
                applied=pet._perform_gesture(name)
                if applied is False:return dict(ok=False,action=action,error='当前状态或屏幕空间不适合播放')
        elif action == 'set_expression':
            name = args.get('name')
            allowed=('normal','happy','sleepy','surprised','sad','playful')
            if str(getattr(self.panel,'active_agent_id','001'))!='001':allowed+=('angry','eating')
            if name not in allowed:raise ValueError('未知表情')
            if str(getattr(self.panel,'active_agent_id','001'))!='001' and hasattr(pet,'_multi_agent'):
                pet._multi_agent.set_expression(self.panel.active_agent_id,name)
                companion=pet._multi_agent.companions.get(self.panel.active_agent_id)
                expression=companion.anim.expression if companion else name
                return dict(action=action,expression=expression,held=expression!='normal',applied=expression==name)
            pet._anim.set_expression(name)
            return dict(action=action,expression=pet._anim.expression,
                        held=pet._anim.expression!='normal',
                        applied=pet._anim.expression==name)
        elif action == 'walk':
            target = str(args.get('name') or args.get('direction') or 'auto').strip().lower()
            if target not in ('left', 'right', 'auto', 'stop'):
                raise ValueError('走路方向只能是 left/right/auto/stop')
            if str(getattr(self.panel, 'active_agent_id', '001')) != '001' and hasattr(pet, '_multi_agent'):
                return dict(ok=False, error='同伴窗口不支持走动，先切回 001 再让它走')
            result = pet.walk_command(target)
            if not result.get('ok'):
                raise ValueError(result.get('error') or '没能走起来')
            return dict(action=action, direction=result.get('direction'), distance=result.get('distance'),
                        animated=result.get('animated'), x=pet.x(), y=pet.y())
        elif action == 'interact':
            name = args.get('name', '')
            if name not in ('feed', 'play', 'pet', 'sleep', 'dance'):
                raise ValueError('未知互动')
            pet._act(name)
        elif action == 'set_opacity':
            value = float(args['value'])
            if not .3 <= value <= 1:
                raise ValueError('透明度需要在 0.3—1 之间')
            pet.setWindowOpacity(value)
            pet._config.opacity = value
        elif action == 'set_volume':
            value = int(args['value'])
            if not 0 <= value <= 100:
                raise ValueError('音量需要在 0—100 之间')
            pet._tts._player.setVolume(value)
            pet._config._s.setValue('agent/volume', value)
        elif action == 'set_topmost':
            enabled = bool(args['value'])
            pet.setWindowFlag(Qt.WindowStaysOnTopHint, enabled)
            pet.show()
            pet._config._s.setValue('agent/topmost', enabled)
        elif action == 'set_bongo':
            if args['value']:
                pet._show_bongo()
            else:
                pet._bongo.hide()
                pet._bongo_on = False
                pet.setFixedSize(pet._iw, pet._ih)
        elif action == 'open_chat':
            pet._open_agent_chat()
        elif action == 'open_desktop_console':
            pet._open_desktop_console()
        elif action == 'open_behavior_settings':
            pet._show_behavior_dialog()
        elif action == 'open_voice_settings':
            # Deferred to avoid blocking the queued tool callback in a modal dialog.
            from PyQt5.QtCore import QTimer
            QTimer.singleShot(0, pet._configure_realtime_voice)
            return {'opened': '语音设置', 'state': '已请求打开，等待用户设置'}
        elif action == 'open_battleground_lab':
            pet._open_bg_agent()
        elif action == 'open_battleground_live':
            pet._open_bg_live()
        else:
            raise ValueError('未注册的自身操作')
        return dict(action=action, visible=pet.isVisible(), x=pet.x(), y=pet.y())

    def _workspace(self, args):
        action = args.get('action')
        ident = args.get('id') or self.panel.active_id
        result = {}
        if action == 'create_project':
            result['project_id'] = self.store.create_project(args['name'])
        elif action == 'rename_project':
            if not args.get('id'):
                raise ValueError('更名项目需要明确的项目 ID')
            self.store.rename('project', ident, args['name'])
        elif action == 'new_conversation':
            if getattr(self.panel,'_manager_enabled',False):
                from core.managed_chat import default_backend
                result['conversation_id']=self.panel.managed.create(default_backend(self.panel.active_agent_id),
                    args.get('name','新对话'),args.get('project_id') or None,agent_id=self.panel.active_agent_id)
            else:
                result['conversation_id'] = self.store.create_conversation(args.get('name', '新对话'), args.get('project_id') or None,agent_id=self.panel.active_agent_id)
            # Keep this running turn bound to its original conversation.
        elif action == 'rename_conversation':
            self.store.rename('conversation', ident, args['name'])
        elif action == 'move_conversation':
            self.store.move(ident, args.get('project_id') or None)
        elif action == 'archive_conversation':
            self.store.archive(ident)
        elif action == 'restore_conversation':
            self.store.archive(ident, False)
        elif action == 'list_archived':
            return self.store.snapshot(archived=True)
        else:
            raise ValueError('未注册的会话操作')
        self.panel.refresh_tree()
        return dict(result, workspace=self.store.snapshot())
