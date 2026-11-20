"""Explicit-recipient WeChat capability; uncertain delivery is never retried."""
import base64,json,subprocess,tempfile,time,re
from secrets import token_urlsafe
from threading import Lock
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def explicit_message(prompt):
    """Extract a recipient and literal body from a direct sending instruction."""
    value = str(prompt or '').strip()
    if '\n' in value or len(value) > 1200:
        return None
    value = re.sub(r'^(?:(?:咕嘎|请你?|帮我|麻烦你?)[，,、\s]*)+', '', value)
    value = re.sub(r'^(?:交给|让|使用|用|通过)\s*dsh(?![a-z0-9_])(?:\s*执行)?[：:，,\s]*', '', value, flags=re.I)
    value, wechat_context = re.subn(r'^(?:打开微信|在微信(?:中|里)?|用微信|微信)[，,、：:\s]*', '', value)
    match = re.fullmatch(
        r'(?:给|向)?\s*(?P<recipient>[^，,、：:。！？?！]{1,80}?)\s*[，,、]?\s*'
        r'(?:发送|发一[条句]|发消息|发)\s*[：:，,、]?\s*(?P<body>.+)', value)
    if wechat_context and re.match(r'^(?:回复|发给|发送给)', value):
        match = re.fullmatch(r'(?:回复|发送给|发给)\s*(?P<recipient>[^，,、：:。！？?！]{1,80})[：:]\s*(?P<body>.+)',value)
    if not match:
        return None
    recipient, body = (match[key].strip() for key in ('recipient', 'body'))
    if any(word in recipient for word in ('不要','别','为什么','怎么','如何','比如','上次','是否','能否','先确认','或者')):
        return None
    if recipient.startswith(('“','「','"')) and recipient[-1:] in ('”','」','"'):
        recipient = recipient[1:-1].strip()
    if recipient != '文件传输助手' and not (wechat_context and re.match(r'^(?:给|向|回复|发给|发送给)', value)):
        return None
    if body.startswith(('“','「','"')) and body[-1:] in ('”','」','"'):
        body = body[1:-1]
    elif body.endswith(('吗','吗？','吗?','么？','么?')) or re.search(r'^(?:什么|一条|一段|帮我|一项)', body):
        return None
    if not recipient or not 1 <= len(body) <= 1000:
        return None
    return dict(recipient=recipient, text=body)

def is_self_message_request(prompt):
    from core.fast_commands import command_text
    text=command_text(prompt)
    if any(word in text for word in ('不要','别发','为什么','失败','错误','上次','之前','如何','怎么')):return False
    return bool(re.match(r'^(?:在微信中?|用微信|打开微信[，,]?|微信)?(?:给|向)?(?:我自己的?|我的)?文件传输助手.*(?:发一段|发送|发一句|写一段|组织语言)',text))

def compact(value):return re.sub(r'[\W_]+','',str(value))

def phrase_boxes(ocr,phrase):
    """Windows Chinese OCR often returns one character per word."""
    result=[]
    for line in ocr.get('lines',[]):
        words=line.get('words',[])
        for start in range(len(words)):
            value='';selected=[]
            for word in words[start:]:
                value+=compact(word['text']);selected.append(word)
                if value==phrase:
                    x=min(w['x'] for w in selected);y=min(w['y'] for w in selected)
                    result.append(dict(text=phrase,x=x,y=y,width=max(w['x']+w['width'] for w in selected)-x,height=max(w['y']+w['height'] for w in selected)-y));break
                if not phrase.startswith(value):break
    return result


def exact_region_text(ocr, text, left, top, bottom):
    for line in ocr.get('lines', []):
        words = [word['text'] for word in line.get('words', [])
                 if word['x'] >= left and top <= word['y'] < bottom]
        if words and compact(''.join(words)) == compact(text):
            return True
    return False


def contact_results(ocr, recipient, height):
    """Match whole search rows, including WeChat's built-in 功能 results."""
    matches, candidates = [], []
    for line in ocr.get('lines', []):
        words = line.get('words') or []
        if not words:
            continue
        x, y = min(w['x'] for w in words), min(w['y'] for w in words)
        if not 0.13 * height <= y < 0.94 * height:
            continue
        text = ''.join(w['text'] for w in words).strip()
        if not text or text in ('联系人', '群聊', '聊天记录', '功能', '搜索'):
            continue
        candidates.append(text)
        if text == recipient or ''.join(text.split()) == ''.join(recipient.split()):
            matches.append(dict(x=x, y=y,
                width=max(w['x']+w['width'] for w in words)-x,
                height=max(w['y']+w['height'] for w in words)-y))
    return matches, list(dict.fromkeys(candidates))[:12]


def search_surface(main):
    """Locate the actual foreground search surface of this same WeChat window."""
    import win32gui
    import win32process
    current = int(win32gui.GetForegroundWindow() or 0)
    if not current:
        raise RuntimeError('微信未处于前台，请点击微信搜索框后再试')
    if current == main:
        return current
    if (win32process.GetWindowThreadProcessId(current)[1] == win32process.GetWindowThreadProcessId(main)[1]
            and win32gui.IsWindowVisible(current)):
        if win32gui.GetAncestor(current, 3) == main or win32gui.GetWindow(current, 4) == main:
            return current
        name = win32gui.GetClassName(current)
        if name.startswith('Qt') and 'Popup' in name:
            return current
    raise RuntimeError('前台已切换到其他窗口，请回到微信再继续')


def focus_wechat(computer, hwnd, cancel):
    # Windows may acknowledge SetForegroundWindow before the desktop updates.
    try:
        computer.execute(dict(action='focus',hwnd=hwnd),cancel)
    except RuntimeError:
        if cancel.is_set():
            raise
    for _ in range(6):
        if cancel.wait(.08):
            raise RuntimeError('任务已停止，未继续微信输入')
        try:
            return search_surface(hwnd)
        except RuntimeError:
            continue
    raise RuntimeError('微信窗口未稳定获得焦点，尚未继续输入')

def read_frame(observation):
    folder=ROOT/'runtime/chat/ocr';folder.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix='.jpg',dir=folder,delete=False) as handle:
        path=Path(handle.name);handle.write(base64.b64decode(observation['_image_url'].split(',',1)[1]))
    try:
        try:
            result=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(ROOT/'tools/windows_ocr.ps1'),str(path)],
                capture_output=True,timeout=12,creationflags=0x08000000)
        except subprocess.TimeoutExpired:raise RuntimeError('读取微信界面超时；未确认发送，交给智能体检查当前窗口') from None
        if result.returncode:raise RuntimeError('本机没有可用的 Windows 中文识字组件，请在微信中手动核对')
        return json.loads(result.stdout.decode('utf-8-sig',errors='replace'))
    finally:path.unlink(missing_ok=True)

LAUNCHER_LABELS=('进入微信','点击进入','开始使用','开始','进入','登录','打开微信')


def wechat_window(computer,cancel):
    """The welcome screen and the main window are DIFFERENT handles."""
    import win32process,psutil
    matches=[]
    for window in computer.execute(dict(action='windows'),cancel)['windows']:
        try:
            pid=win32process.GetWindowThreadProcessId(window['hwnd'])[1]
            if psutil.Process(pid).name().lower() in ('weixin.exe','wechat.exe') and window['title']=='微信':
                matches.append(window['hwnd'])
        except (OSError,psutil.Error):pass
    return matches[0] if len(matches)==1 else None


def pass_launcher(computer,observe,cancel,obs=None,ocr=None,holder=None):
    """WeChat cold start parks on a welcome screen with a single "enter" button.

    Finding the window is not the same as getting inside it: without this step
    every later step fails on a screen that has no search box at all, and the
    pet reports that WeChat never opened. The main window is a new handle, so
    the target has to be re-resolved after the click.
    """
    if obs is None or ocr is None:
        obs,ocr=observe()
    clicked=False
    for _ in range(3):
        if phrase_boxes(ocr,'搜索'):
            return obs,ocr,clicked
        hit=None
        for label in LAUNCHER_LABELS:
            boxes=phrase_boxes(ocr,label)
            if boxes:
                hit=max(boxes,key=lambda b:b['width']*b['height'])
                break
        if not hit:
            return obs,ocr,clicked
        computer.execute(dict(action='click',observation_id=obs['observation_id'],
            x=int(hit['x']+hit['width']/2),y=int(hit['y']+hit['height']/2)),cancel)
        clicked=True
        cancel.wait(2.0)
        if holder is not None:
            new=wechat_window(computer,cancel)
            if new and new!=holder.get('hwnd'):
                holder['hwnd']=new
                try:computer.execute(dict(action='focus',hwnd=new),cancel)
                except Exception:pass
                cancel.wait(0.8)
        try:
            obs,ocr=observe();width,height=obs['image_size']
        except Exception:
            return obs,ocr,clicked
    return obs,ocr,clicked


def _send_message(computer,recipient,text,cancel,prepare_only=False,chat=None,verify_current=False):
    recipient=str(recipient).strip()
    if not recipient or len(recipient)>80 or any(c in recipient for c in "\n\r"):raise ValueError("需要用户指定的精确聊天对象名称")
    text=str(text).strip()
    if not prepare_only and (not 1<=len(text)<=1000 or '\n' in text or '\r' in text):raise ValueError('演示消息需要1—1000字单段文本')
    hwnd=wechat_window(computer,cancel)
    if not hwnd and not chat and not verify_current:
        from core.super_agent.desktop_command import open_app
        open_app('微信')
        for _ in range(12):
            if cancel.wait(.25):
                raise RuntimeError('任务已停止')
            hwnd=wechat_window(computer,cancel)
            if hwnd:
                break
    if not hwnd:raise RuntimeError('没有找到唯一的微信主窗口，请打开微信主窗口')
    if chat and chat['hwnd'] != hwnd:
        raise RuntimeError('微信窗口已变化，请重新定位联系人')
    from core.wechat_input_scope import verified_wechat_input
    with verified_wechat_input(hwnd):
        return _within_wechat(computer, recipient, text, cancel, prepare_only, chat, hwnd, verify_current)


def _within_wechat(computer,recipient,text,cancel,prepare_only,chat,hwnd,verify_current=False):
    holder={'hwnd':hwnd}
    focus_wechat(computer,hwnd,cancel)
    def observe():
        obs=computer.execute(dict(action='observe',hwnd=holder['hwnd']),cancel)
        return obs,read_frame(obs)
    obs,ocr=observe();width,height=obs['image_size']
    if not chat and not verify_current:
        if any(phrase_boxes(ocr,label) for label in ('扫码登录','进入微信','开始使用')):
            raise RuntimeError('微信仍在登录界面，请你登录后再定位联系人')
        if not exact_region_text(ocr,recipient,width*.20,0,height*.19):
            rejected = locate_search_result(computer, recipient, cancel, hwnd)
            if rejected is not None:
                return rejected
            cancel.wait(.3)
            obs,ocr=observe();width,height=obs['image_size']
    # Observation/OCR can outlive the original foreground lease. Recheck before input.
    focus_wechat(computer,hwnd,cancel)
    obs,ocr=observe()
    # Require the actual conversation header, not a matching sidebar contact.
    header=exact_region_text(ocr,recipient,width*.20,0,height*.19)
    if not header:raise RuntimeError('顶部会话名未验证为指定对象，未输入或发送')
    if prepare_only:return dict(ok=True,ready_for_message=True,recipient=recipient,hwnd=holder['hwnd'],message='已定位并核对会话标题')
    editor = (dict(hwnd=hwnd, uia_id='chat_input_field', uia_type='Edit')
              if recipient == '文件传输助手' else dict(hwnd=hwnd,uia_name=recipient,uia_type='Edit'))
    button = dict(hwnd=hwnd,uia_name='发送',uia_type='Button')
    edit = computer.execute(dict(action='uia_read',**editor),cancel).get('element',{})
    if not edit.get('enabled') or 'value' not in edit or not edit.get('rect'):
        raise RuntimeError('微信输入框无法回读；未输入')
    if edit['value'] and edit['value']!=text:
        raise RuntimeError('微信输入框已有草稿；草稿已保留')
    # The Send button is disabled and often invisible to OCR until text is entered.
    control=computer.execute(dict(action='uia_read',**button),cancel).get('element',{})
    if not control or control.get('offscreen'):
        raise RuntimeError('未定位对应会话里的发送按钮；未输入')
    _, top, _, bottom = obs['desktop_rect']
    cutoff = (edit['rect']['top']-top) * height/(bottom-top)
    def transcript_controls():
        entries=computer.execute(dict(action='accessibility',hwnd=hwnd,limit=450),cancel).get('elements',[])
        return sum(item.get('type')=='ListItem' and str(item.get('name','')).strip()==text
                   and (item.get('rect') or {}).get('top',0)>top+height*.12
                   and (item.get('rect') or {}).get('bottom',0)<=edit['rect']['top']
                   and (item.get('rect') or {}).get('right',0)>edit['rect']['left']
                   for item in entries)
    def transcript_hits(ocr_result):
        value=compact(text)
        return sum(compact(line.get('text','')) == value
                   for line in ocr_result.get('lines',[]) if value and line.get('words')
                   and all(w['x'] > width*.48 and height*.17 < w['y'] < cutoff for w in line['words']))
    before = transcript_hits(ocr)
    before_controls = transcript_controls()
    if edit['value']!=text:
        computer.execute(dict(action='uia',uia_operation='focus',**editor),cancel)
        current=computer.execute(dict(action='uia_read',**editor),cancel).get('element',{})
        if not current.get('focused') or current.get('value')!='':
            raise RuntimeError('微信输入框焦点或草稿已变化；未输入')
        obs=computer.execute(dict(action='observe',hwnd=hwnd),cancel)
        computer.execute(dict(action='type',observation_id=obs['observation_id'],text=text),cancel)
        cancel.wait(.25)
    current=computer.execute(dict(action='uia_read',**editor),cancel).get('element',{})
    if current.get('value')!=text:
        return dict(ok=False,sent=False,error='输入的文字未通过控件回读，未点击发送')
    control=computer.execute(dict(action='uia_read',**button),cancel).get('element',{})
    if not control.get('enabled') or control.get('offscreen') or search_surface(hwnd)!=hwnd:
        return dict(ok=False,sent=False,error='微信发送按钮或窗口状态发生变化，未点击发送')
    try:
        computer.execute(dict(action='uia',uia_operation='click',**button),cancel)
    except (RuntimeError, ValueError, OSError) as exc:
        return dict(ok=False, recipient=recipient, delivery_unknown=True,
                    error='发送操作结果待核对，请检查对应聊天记录：' + str(exc))
    found=False
    try:
        for delay in (1.0,2.0,3.0):
            cancel.wait(delay);obs,ocr=observe()
            if (exact_region_text(ocr,recipient,width*.20,0,height*.19)
                    and (transcript_controls()>before_controls or transcript_hits(ocr)>before)):
                found=True
                break
    except (RuntimeError, ValueError, OSError):
        pass
    return dict(ok=bool(found),recipient=recipient,send_clicked=True,verified_message_visible=bool(found),
        delivery_unknown=not found,
        delivery_verified=False,message='消息已出现在指定聊天区' if found else '已点击发送，但未回读到消息；请检查，不会自动重发')


def locate_search_result(computer, recipient, cancel, main):
    from core.wechat_input_scope import verified_wechat_input
    current = focus_wechat(computer,main,cancel)
    with verified_wechat_input(current):
        tree = computer.execute(dict(action='accessibility', hwnd=current, limit=120), cancel)
        edit = [item for item in tree.get('elements', [])
                if item.get('type') == 'Edit' and item.get('name') == '搜索' and item.get('ref')]
        if len(edit) == 1:
            computer.execute(dict(action='uia', hwnd=current, uia_ref=edit[0]['ref'], uia_operation='focus'), cancel)
        else:
            computer.execute(dict(action='hotkey', keys='ctrl+f'), cancel)
    cancel.wait(.3)
    current = search_surface(main)
    query = '文件' if recipient == '文件传输助手' else recipient
    with verified_wechat_input(current):
        obs = computer.execute(dict(action='observe', hwnd=current), cancel)
        computer.execute(dict(action='type', observation_id=obs['observation_id'],
                              text=query, replace=True, clear=True), cancel)
    cancel.wait(.8)
    current = search_surface(main)
    with verified_wechat_input(current):
        obs = computer.execute(dict(action='observe', hwnd=current), cancel)
        matches, candidates = contact_results(read_frame(obs), recipient, obs['image_size'][1])
        if len(matches) != 1:
            return dict(ok=False, candidates=candidates, error='需要唯一匹配的微信联系人，请核对搜索结果')
        contact = matches[0]
        computer.execute(dict(action='click', observation_id=obs['observation_id'],
            x=int(contact['x']+contact['width']/2), y=int(contact['y']+contact['height']/2)), cancel)
    return None


def send_to_self(computer,text,cancel):
    return dict(ok=False, recipient='文件传输助手',
                error='请在咕嘎对话里直接交代收件人与要发送的原文')


class WeChatAgent:
    def __init__(self,computer):
        self.computer=computer;self.prepared=None;self.locate_failures=0
        self._authorizations = {}
        self._receipts = {}
        self._authorization_lock = Lock()

    def authorize(self, message, conversation_id):
        token = token_urlsafe(24)
        now = time.monotonic()
        with self._authorization_lock:
            self._authorizations = {key: entry for key, entry in self._authorizations.items() if entry[0] > now}
            self._authorizations[token] = (now + 600, message.copy(), conversation_id)
            self._receipts[conversation_id] = dict(ok=False, error='DSH 尚未返回实际微信发送回执')
            while len(self._receipts) > 64:
                del self._receipts[next(iter(self._receipts))]
        return token

    def receipt(self, conversation_id):
        with self._authorization_lock:
            return self._receipts.get(conversation_id, {}).copy()

    def send_authorized(self, message, cancel):
        return _send_message(self.computer, message['recipient'], message['text'], cancel)

    def execute(self,args,cancel):
        import uuid
        action=args.get('action');recipient=str(args.get('recipient','')).strip()
        if action=='open':
            if self.locate_failures>=1:
                # Repeating a 3-second OCR attempt that already failed just burns
                # the step budget. Point the model at the manual path instead.
                raise RuntimeError('微信自动定位没有通过，检查联系人名称或会话状态后再试')
            try:
                result=_send_message(self.computer,recipient,'',cancel,prepare_only=True)
            except RuntimeError:
                self.locate_failures+=1
                raise
            if result.get('ok'):
                token=uuid.uuid4().hex;self.prepared=dict(token=token,recipient=recipient,hwnd=result['hwnd'],created=time.monotonic())
                result['chat_token']=token
            return result
        if action!='send':raise ValueError('微信操作仅支持open/send')
        message = dict(recipient=recipient, text=str(args.get('text') or '').strip())
        token = str(args.get('authorization') or '')
        with self._authorization_lock:
            entry = self._authorizations.get(token)
            if not entry or entry[0] <= time.monotonic() or entry[1] != message:
                return dict(ok=False, error='请直接告诉咕嘎完整微信联系人和需要发送的原文')
            del self._authorizations[token]
        try:
            result = self.send_authorized(message, cancel)
        except (RuntimeError, ValueError, OSError) as exc:
            result = dict(ok=False, sent=False, error=str(exc))
        with self._authorization_lock:
            self._receipts[entry[2]] = result.copy()
        return result

    def prepare_confirmation(self, recipient, text, cancel, current=False):
        self.prepared = None
        recipient, text = str(recipient).strip(), str(text).strip()
        if not 1 <= len(text) <= 1000 or '\n' in text or '\r' in text:
            raise ValueError('请填写 1—1000 字的单段消息')
        options = dict(prepare_only=True)
        if current:
            options['verify_current'] = True
        result = _send_message(self.computer,recipient,'',cancel,**options)
        if result.get('ok'):
            import uuid
            token = uuid.uuid4().hex
            self.prepared = dict(token=token, recipient=recipient, text=text, hwnd=result['hwnd'],
                                 created=time.monotonic())
            result['confirmation_id'] = token
        return result

    def confirm(self, confirmation_id, cancel):
        ready, self.prepared = self.prepared, None
        if (not ready or ready.get('token') != confirmation_id or 'text' not in ready
                or not 0 <= time.monotonic() - ready['created'] <= 120 or cancel.is_set()):
            raise RuntimeError('发送确认已过期或已使用，请重新定位')
        return _send_message(self.computer, ready['recipient'], ready['text'], cancel, chat=ready)
