"""Browser capability for the reasoning agent, with actual post-action observation."""
from urllib.parse import urlsplit


def verified_address(accessibility, requested):
    """Match a browser address control, never a link in the webpage body."""
    expected = urlsplit(requested)
    for item in accessibility.get('elements', []):
        kind = str(item.get('type') or item.get('control_type') or '').lower()
        name = str(item.get('name') or '').lower()
        ident = str(item.get('id') or item.get('automation_id') or '').lower()
        if not ('edit' in kind and any(x in name + ' ' + ident for x in ('地址', 'address', '网址', 'url', 'omnibox', '搜索或输入'))):
            continue
        value = str(item.get('value') or '').strip()
        actual = urlsplit(value if '://' in value else 'https://' + value)
        if (actual.hostname == expected.hostname and
                actual.path.rstrip('/') == expected.path.rstrip('/') and
                actual.query == expected.query and actual.fragment == expected.fragment):
            return value
    return ''


def execute(computer,args,cancel):
    if cancel.is_set():return dict(ok=False,error='任务已停止，未发送浏览器操作',action_sent=False)
    from core.browser_session import ACTIONS,BrowserSession
    # Only a trusted caller/fixture can opt into a separate profile. A model's
    # session_start or snapshot can never silently spawn Chrome for Testing.
    if not getattr(computer,'allow_isolated_browser',False) and args.get('action')!='close_tab':
        if not computer.access.enabled:return dict(ok=False,action_sent=False,error='完整访问未开启')
        from core.existing_browser import ExistingBrowser
        session=getattr(computer,'existing_browser',None)
        if session is None:session=computer.existing_browser=ExistingBrowser(computer)
        return session.execute(args,cancel)
    if args.get('action') in ACTIONS:
        if not computer.access.enabled:return dict(ok=False,error='完整访问未开启',action_sent=False)
        session=getattr(computer,'browser_session',None)
        if session is None:session=computer.browser_session=BrowserSession()
        return session.execute(args,cancel)
    if args.get('action')=='close_tab':
        from core.browser_actions import bounded_close
        return bounded_close()
    if args.get('action')=='open':return open_page(computer,args['url'],cancel)
    raise ValueError('未知浏览器操作')

def validate_url(url):
    url=str(url).strip()
    parsed=urlsplit(url)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('需要有效的 http/https 网址；网站名称先由模型解析或搜索')
    if len(url)>3000:raise ValueError('网址过长')
    return url

def open_page(computer,url,cancel):
    from core.browser_launch import launch,default_executable
    import psutil,win32process
    url=validate_url(url);executable=default_executable()
    launch(url)
    if cancel.wait(.6):return dict(ok=False,launch_requested=True,error='导航已提交，随后任务被停止，未复查')
    candidates=[]
    for window in computer.execute(dict(action='windows'),cancel)['windows']:
        try:
            pid=win32process.GetWindowThreadProcessId(window['hwnd'])[1]
            path=psutil.Process(pid).exe()
            if executable and path.casefold()==str(executable).casefold():candidates.append(window)
        except (OSError,psutil.Error):pass
    if not candidates:return dict(ok=False,launch_requested=True,error='浏览器已接受网址，但尚未定位窗口；使用 computer.windows 继续核对，不要重复启动')
    preferred=[w for w in candidates if '浏览器' in w['title']] or candidates
    window=preferred[0]
    observation = {}; accessibility = {}; warning = ''
    try:
        computer.execute(dict(action='focus',hwnd=window['hwnd']),cancel)
        observation=computer.execute(dict(action='observe',hwnd=window['hwnd']),cancel)
        accessibility=computer.execute(dict(action='accessibility',hwnd=window['hwnd']),cancel)
    except (RuntimeError,ValueError,OSError) as exc:
        warning = str(exc)[:200]
    address = verified_address(accessibility, url)
    # One bounded read after loading; do not open a second profile to verify this one.
    if not address and not warning and not cancel.wait(.8):
        try:
            accessibility = computer.execute(dict(action='accessibility',hwnd=window['hwnd']),cancel)
            address = verified_address(accessibility, url)
        except (RuntimeError,ValueError,OSError) as exc:warning = str(exc)[:200]
    verified = bool(address)
    return dict(ok=True,result=dict(observation,url_requested=url,launch_requested=True,
        hwnd=window['hwnd'],title=observation.get('title') or window['title'],
        navigation_verified=verified,page_verified=verified,url_observed=address,
        accessibility=accessibility,task_complete=False,verification_warning=warning,
        message=('目标网址已在默认浏览器的地址栏核对，页面导航已完成；登录和投稿提交未核验。'
                 if verified else '默认浏览器已接受网址并找到真实窗口，页面内容尚未确认。')+
                '不要重复打开，也不要换专用浏览器；后续操作继续使用这个窗口。'))
