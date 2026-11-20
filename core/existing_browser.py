"""Browser actions stay in one verified user window and its existing login profile."""
import time
import uuid
import json
import subprocess
import sys
from pathlib import Path


class ExistingBrowser:
    def __init__(self,computer):
        self.computer=computer;self.bound={};self.observed=None;self.operations={};self._owner_cancel=None
        self.provider=''

    def _identity(self,window):
        import psutil,win32process
        pid=win32process.GetWindowThreadProcessId(window['hwnd'])[1]
        return dict(window,pid=pid,executable=psutil.Process(pid).exe())

    def select(self,cancel,launch_url=None):
        from core.browser_launch import default_executable,launch
        import win32gui
        executable=default_executable()
        candidates=[]
        for window in self.computer.execute(dict(action='windows'),cancel).get('windows',[]):
            try:identity=self._identity(window)
            except Exception:continue
            if self.provider=='doubao':
                title=window.get('title','')
                if Path(identity['executable']).name.casefold()=='doubao.exe' and title.endswith('豆包浏览器'):
                    candidates.append(identity)
            elif executable and Path(identity['executable'])==Path(executable):
                candidates.append(identity)
        browser_windows=[w for w in candidates if '浏览器' in w.get('title','')]
        if browser_windows:candidates=browser_windows
        for item in candidates:
            if all(item.get(k)==self.bound.get(k) for k in ('hwnd','pid','executable')):
                self.bound.update(item);return self.bound
        focused=[w for w in candidates if w['hwnd']==win32gui.GetForegroundWindow()]
        chosen=focused[0] if len(focused)==1 else candidates[0] if len(candidates)==1 else None
        if chosen:
            self.bound=dict(chosen,page_id='user-'+str(chosen['hwnd'])+'-'+str(chosen['pid']))
            return self.bound
        if candidates:raise ValueError('找到了多个浏览器窗口，请先切到要继续使用的那个窗口；没有新开浏览器')
        if launch_url and not self.provider:
            launch(launch_url)
            if cancel.wait(.8):raise RuntimeError('已请求打开默认浏览器，随后任务停止；未重试')
            selected=self.select(cancel);self.bound['just_launched']=True;return selected
        raise ValueError('尚未找到已打开的'+('豆包浏览器' if self.provider=='doubao' else '默认浏览器')+'窗口')

    def snapshot(self,cancel):
        window=self.select(cancel);hwnd=window['hwnd']
        self.computer.execute(dict(action='focus',hwnd=hwnd),cancel)
        image=self.computer.execute(dict(action='observe',hwnd=hwnd),cancel)
        tree=self.computer.execute(dict(action='accessibility',hwnd=hwnd,limit=240),cancel)
        controls=[];refs={}
        for item in tree.get('elements',[]):
            if not isinstance(item,dict) or item.get('offscreen') or item.get('password'):continue
            if item.get('type') not in ('Button','Hyperlink','Edit','TabItem','CheckBox','ComboBox','MenuItem'):continue
            if not (item.get('name') or item.get('value')):continue
            ref=str(item.get('ref') or '')
            if not ref:continue
            refs[ref]=dict(item)
            controls.append(dict(ref=ref,label=item.get('name',''),type=item.get('type'),value=item.get('value','')))
        token=uuid.uuid4().hex
        self.observed=dict(id=token,page_id=window['page_id'],at=time.monotonic(),refs=refs,image=image)
        self.bound['title']=image.get('title') or window.get('title','')
        launched=bool(self.bound.pop('just_launched',False))
        return dict(ok=True,**image,page_id=window['page_id'],snapshot_id=token,
                    controls=controls,accessibility=tree,owned_browser=False,browser_mode='existing',
                    reused_window=not launched,launch_requested=launched,profile_changed=False,
                    message='已查看原来的浏览器窗口，会继续沿用这里的登录状态。')

    def navigate(self,url,cancel,new_tab=False):
        from core.browser_agent import validate_url,verified_address
        url=validate_url(url)
        try:window=self.select(cancel)
        except ValueError as exc:
            if self.provider or '尚未找到' not in str(exc):raise
            window=self.select(cancel,launch_url=url)
            self.bound['requested_url']=url
            return self._navigation_receipt(url,cancel,already_sent=True)
        self.computer.execute(dict(action='focus',hwnd=window['hwnd']),cancel)
        if new_tab:self.computer.execute(dict(action='hotkey',keys='ctrl+t'),cancel)
        image=self.computer.execute(dict(action='observe',hwnd=window['hwnd']),cancel)
        self.computer.execute(dict(action='fill',observation_id=image['observation_id'],keys='ctrl+l',text=url),cancel)
        if cancel.is_set():return dict(ok=False,action_sent=None,error='网址已输入但任务停止，未发送回车')
        self.computer.execute(dict(action='hotkey',keys='enter'),cancel)
        self.bound['requested_url']=url
        cancel.wait(.7)
        return self._navigation_receipt(url,cancel,already_sent=False)

    def _navigation_receipt(self,url,cancel,already_sent):
        from core.browser_agent import verified_address
        result=self.snapshot(cancel)
        address=verified_address(result.get('accessibility',{}),url)
        self.bound['last_title']=result.get('title','')
        result.update(url_requested=url,url_observed=address,navigation_verified=bool(address),
                      page_verified=bool(address),navigation_sent=not already_sent,
                      task_complete=False)
        result['message']='已在原来的浏览器打开这个网址。' if address else '网址已输入原来的浏览器，页面还需要核对。'
        return result

    def execute(self,args,cancel):
        if cancel is not self._owner_cancel:
            self._owner_cancel=cancel;self.operations.clear();self.observed=None
        action=args.get('action')
        provider=args.get('browser')
        if provider is not None:
            if provider not in ('doubao','default'):raise ValueError('未知浏览器')
            provider='' if provider=='default' else provider
            if self.provider!=provider:
                self.provider=provider;self.bound={};self.observed=None
        if action in ('open','navigate','new_tab'):return self.navigate(args['url'],cancel,action=='new_tab')
        if action=='session_start':return self.navigate(args['url'],cancel) if args.get('url') and not self.bound else self.snapshot(cancel)
        if action in ('snapshot','session_status','tabs','wait'):
            if action=='wait':cancel.wait(min(3,max(0,float(args.get('seconds',float(args.get('milliseconds',300))/1000)))))
            result=self.snapshot(cancel)
            if action=='tabs':result['tabs']=[dict(page_id=result['page_id'],title=result.get('title',''),active=True)]
            return result
        if action=='session_stop':
            self.bound={};self.observed=None
            return dict(ok=True,active=False,user_browser_closed=False,message='已解除控制绑定，用户浏览器保持打开')
        if action=='upload' and self.provider=='doubao':
            return self.select_doubao_file(args,cancel)
        if action in ('upload','download'):
            return dict(ok=False,action_sent=False,error='请在当前浏览器窗口中使用computer控件操作文件对话框；不会换到未登录浏览器')
        op=str(args.get('operation_id') or '')
        if op and op in self.operations:return dict(self.operations[op],duplicate_suppressed=True)
        observed=self.observed or {};window=self.select(cancel)
        if (args.get('page_id')!=window['page_id'] or args.get('snapshot_id')!=observed.get('id') or
                time.monotonic()-observed.get('at',0)>60):
            return dict(ok=False,action_sent=False,error='当前浏览器观察已变化，请先snapshot；没有新开浏览器')
        if action in ('click','press') and not op:raise ValueError('请给本次操作设置operation_id，避免重复点击或提交')
        if op:self.operations[op]=dict(ok=False,action_sent=None,error='该操作已发出或结果尚待核对，请重新观察，不要重放')
        self.computer.execute(dict(action='focus',hwnd=window['hwnd']),cancel)
        self.observed=None
        if action=='click':
            ref=args.get('ref')
            if ref not in observed.get('refs',{}):raise ValueError('控件ref不属于当前窗口，未点击')
            self.computer.execute(dict(action='uia',hwnd=window['hwnd'],uia_ref=ref,uia_operation='invoke'),cancel)
        elif action=='press':self.computer.execute(dict(action='hotkey',keys=args.get('keys','enter')),cancel)
        elif action=='scroll':
            self.computer.execute(dict(action='scroll',observation_id=observed['image']['observation_id'],
                delta=int(max(-20,min(20,-int(args.get('delta',400))/120)))),cancel)
        elif action=='fill_form':
            for field in args.get('fields',[]):
                if field.get('ref') not in observed.get('refs',{}) or field.get('kind')!='text':
                    raise ValueError('当前入口仅支持已观察到的文字输入框，其他控件请使用computer')
                self.computer.execute(dict(action='uia',hwnd=window['hwnd'],uia_ref=field['ref'],
                    uia_operation='set_value',uia_value=str(field.get('value',''))),cancel)
        else:raise ValueError('当前浏览器不支持这个动作；未启动任何独立浏览器')
        cancel.wait(.4)
        result=self.snapshot(cancel);result['action_sent']=True
        if op:self.operations[op]=result
        while len(self.operations)>60:self.operations.pop(next(iter(self.operations)))
        return result

    def select_doubao_file(self,args,cancel):
        if cancel.is_set():raise RuntimeError('任务已停止')
        window=self.bound
        if not window or args.get('page_id')!=window.get('page_id'):
            raise ValueError('请先绑定发起上传的豆包窗口，使用该页面的 page_id')
        op=str(args.get('operation_id') or '')
        if not op:raise ValueError('选择文件需要 operation_id')
        if op in self.operations:return dict(self.operations[op],duplicate_suppressed=True)
        file=Path(args.get('path') or '')
        if not file.is_absolute() or not file.is_file():raise ValueError('需要已核实的本地文件绝对路径')
        script=Path.home()/'.codex/skills/doubao-browser/scripts/doubao_browser.py'
        if not script.is_file():raise RuntimeError('本机缺少豆包文件选择器')
        def run(*parameters):
            result=subprocess.run([sys.executable,'-X','utf8',str(script),*parameters],
                capture_output=True,text=True,encoding='utf-8',timeout=18,creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:raise RuntimeError((result.stderr or result.stdout).strip()[:240])
            return json.loads(result.stdout)
        matches=[row for row in run('list') if row['hwnd']==window['hwnd']]
        if len(matches)!=1:raise RuntimeError('豆包启动了新窗口或原窗口已经关闭')
        self.operations[op]=dict(ok=False,error='文件选择状态待核对，请先查看页面和文件框')
        selected=run('select-file',matches[0]['id'],str(file))
        receipt=dict(ok=bool(selected.get('chooser_closed')),file_selected=selected.get('selected_file'),
                     page_id=window['page_id'],message='已选择文件，请继续在网页核对上传状态')
        self.operations[op]=receipt
        return receipt
