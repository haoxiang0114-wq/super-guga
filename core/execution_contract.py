"""Deterministic completion checks for tool side effects and verification receipts."""
import re

INPUT_ACTIONS={"click","double_click","drag","scroll","hotkey","type","fill","editor_select_tail","uia"}
VERIFY_ACTIONS={"observe","accessibility","uia_read"}
ACTION_WORDS=("打开","启动","关闭","读取","修改","写入","保存","运行","安装","下载",
              "绘制","画","制作","生成",
              "删除","移动","复制","重命名","上传","提交","点击","输入","执行","修复",
              "测试","检查窗口","截屏","看屏幕","整理文件","创建文件","发送","发一段","发一句",
              "勾选","打勾","打卡","设置每日计划","每天提醒","记住","忘记记忆","更正记忆",
              # 2026-11-12：原来只认「整理文件」「发送」，用户说「把图片整理成文档」
              # 「在微信里发给小军」时匹配不到，被判成闲聊、落到本地问答里干不了活。
              "整理成","整理到","汇总成","汇总到","做成","转成","导出","归档",
              "发给","发送给","转发给","转给","回复给","截图给",
              # 口语里动词和名词中间爱插量词：「截个屏」「截一张图」「看下屏幕」，
              # 纯子串匹配会漏，这里把常见说法直接列进来。
              "截图","截个屏","截一张","截个图","抓个图","看下屏幕","看一下屏幕",
              "预检","探测","抽帧","转写","分章","渲染","做成片","改名","填好")

# 中文口语的量词插入：把「截个屏」这类还原成「截屏」，兜住没列全的说法。
# 第三组要覆盖常见宾语（屏/图/消息/邮件/文件…），只写动词后面的名词，
# 不要写成「任意字」，否则「看一下这个想法」这种闲聊也会被当成任务。
_MEASURE_WORD=re.compile(
    r'(截|打|关|发|看|整理|导出|转|打开|关闭)(个|张|一下|一遍|下)'
    r'(屏|图|开|给|成|到|窗口|文件|消息|邮件|微信|文字|链接|视频|表格|文档)')
# Questions about how/why something works are not action requests even when they
# mention an action word ("下载速度慢是什么原因"). A real request keeps an
# explicit imperative marker, so "能不能帮我打开浏览器" still executes.
QUESTION_WORDS=("如何","怎么","为什么","解释一下","有什么区别","是什么","什么是","教我","步骤是",
                "什么","哪些","哪个","多少","多久","哪里","谁","何时","怎样","怎么样","是否",
                "是不是","有没有","吗","呢","？","?")
REQUEST_WORDS=("帮我","给我","替我","麻烦","请你","我要你","现在开始","执行一下","跑一下",
               "去把","去打开","去下载","去执行")


def _question_like(text):
    return any(word in text for word in QUESTION_WORDS)


def is_dsh_live_query(prompt):
    """Questions about this machine's DSH need tools, not a web Q&A model."""
    text=str(prompt or '').lower()
    return bool(re.search(r'\bdsh\b',text) and
                any(word in text for word in ('技能','插件','会话','连接','连上','任务状态')) and
                any(word in text for word in ('现在','目前','有没有','哪些','查看','查一下','报一下',
                    '列出','第一段','名字','名称','读取','能连上','用的是哪个')))


def requires_execution(prompt):
    text=str(prompt or "").lower().strip()
    if is_dsh_live_query(text):return True
    # 2026-11-20：「进入浏览器里的B站」既没有 ACTION_WORDS 里的动词，也不像问题，
    # 于是被判成闲聊——那一轮手上没有桌面工具，只能答"这轮点不了"。进出已有
    # 站点/软件确实是动作，但只认「动词+确切目标」，别把"我刚进入会议室了"也算。
    enter=re.match(r'^(?:进入|切到|切换到|回到|跳回)(?:一下)?\s*'
                   r'(?:浏览器(?:里|中|内)?(?:的)?|原来的|原本的)?\s*'
                   r'([\u4e00-\u9fffA-Za-z0-9·\-]{1,12})$',text)
    if enter:
        try:
            from core.fast_commands import (_is_known_local_app, APP_ALIASES,
                                            BROWSER_NAMES, SITE_ALIASES)
            tail=enter.group(1)
            if (tail in SITE_ALIASES or tail in APP_ALIASES or tail in BROWSER_NAMES
                    or _is_known_local_app(tail)):return True
        except Exception:
            pass
    # Complex routine declarations/edits go to the agent with plan tools, rather
    # than a Q&A worker that cannot persist them. Simple ones use the local parser.
    routine=re.match(r'^(?:咕嘎[，,\s]*)?(?:(?:请|帮我)?记住[：:，,\s]*)?(?:这是)?(?:我的(?:每日计划|每日清单|日常安排)|我(?:以后|从现在起|接下来)?(?:每天|每日|每周))',text)
    if routine and not _question_like(text) and not re.search(r'不要记|别记|不用记|不保存|别保存|不要保存|如果|假如|比如|例如|假设|[“”「」『』"]',text):return True
    if re.search(r'每日计划|每日清单|日常安排',text) and re.search(r'改成|改为|改到|改一下|取消|移除',text) and not _question_like(text):return True
    if not any(word in text for word in ACTION_WORDS) and not _MEASURE_WORD.search(text):
        return False
    value=re.sub(r'^(?:小?咕嘎[，,\s]*)?(?:(?:请你?|麻烦你?|帮我|给我|替我|我要你|你能不能|你能|你可以|你可不可以|能不能|能否|可不可以|可以)[，,\s]*)*','',text)
    skill=re.match(r'^(?:用|使用|调用|启用)\s*[\w.-]+\s*(?:技能|工作流)[，,:：\s]+(.+)',value)
    if skill:
        # Later verification questions are arguments of an explicit skill run.
        # Inspect its first action clause, not a later "滤镜是否存在".
        lead=re.split(r'[，,。;；]',skill.group(1),maxsplit=1)[0]
        if any(word in lead for word in ACTION_WORDS) and not _question_like(lead):return True
    if re.match(r'^(?:解释|讲解|告诉我|教我).*(?:如何|怎么|为什么|是什么|什么意思)',value):return False
    imperative=re.match(r'^('+'|'.join(sorted(ACTION_WORDS,key=len,reverse=True))+r')[，,:：\s]*(.*)',value)
    if imperative:
        # A question inside a message/field is an action argument, not the
        # user's intent. Only inspect the leading instruction clause.
        clause=re.split(r'[，,。;；]|(?:发送|发给)',imperative.group(2),maxsplit=1)[0]
        knowledge=tuple(w for w in QUESTION_WORDS if w not in ('吗','呢','？','?'))
        if clause and not any(w in clause for w in knowledge):return True
    if _question_like(text) and not any(word in text for word in REQUEST_WORDS):return False
    return True


class ExecutionLedger:
    """Track only externally consequential work. Read-only failures stay recoverable."""
    def __init__(self,goal=''):
        self.expects_send=any(word in str(goal) for word in ('发送','发一段','发一句','发给','发条'))
        self.pending=[]
        self.failures={}
        self.completed=[]
        self.evidence_count=0

    @staticmethod
    def _ok(value):
        return bool(isinstance(value,dict) and value.get("ok") is True)

    def record(self,name,args,value):
        args=args if isinstance(args,dict) else {}
        value=value if isinstance(value,dict) else {}
        action=str(args.get("action",""))
        result=value.get("result") if isinstance(value.get("result"),dict) else {}
        ok=self._ok(value)
        if name not in ("task_plan","self_ask") and ok:
            self.evidence_count+=1
        if name=='applications' and action=='open':
            if ok and result.get('selection_required'):
                self.pending.append(dict(category='application_window',hwnd=None,
                    description='发现多个软件窗口，尚未选择并观察目标窗口'))
            return
        if name=='browser' and action not in ('open','close_tab'):
            page=value.get('page_id')
            if action in ('click','press','upload','scroll') and value.get('action_sent') is not False:
                self.pending.append(dict(category='browser_input',page_id=args.get('page_id'),
                    description='浏览器输入已尝试；需要重新snapshot读取页面并核对目标结果'))
            if action in ('snapshot','wait') and ok and value.get('snapshot_id'):
                self.pending=[p for p in self.pending if not (p.get('category')=='browser_input' and p.get('page_id')==page)]
                self.failures.pop('browser_session',None)
            elif not ok:self.failures['browser_session']='专用浏览器步骤未获得成功回执；先观察核对已有结果'
            elif action in ('fill_form','download') and value.get('verified'):
                self.completed.append(dict(category='browser_verified',action=action))
                self.failures.pop('browser_session',None)
            return
        if name in ("wechat_send_self","wechat","web_search","browser"):
            if name=='wechat' and action=='open' and self.expects_send and ok:
                self.pending=[p for p in self.pending if p.get('category')!='wechat_message']
                self.pending.append(dict(category='wechat_message',description='已定位微信会话，但尚未取得发送并回读消息的回执'))
            if name=='wechat' and action=='send' and ok and value.get('verified_message_visible'):
                self.pending=[p for p in self.pending if p.get('category')!='wechat_message']
            if ok:self.failures.pop(name,None)
            else:self.failures[name]=name+" 未获得成功验证回执，不能声称已完成"
            return
        if name=="computer":
            if action=='focus' and ok:
                for item in self.pending:
                    if item.get('category')=='application_window':item['hwnd']=args.get('hwnd')
            if action in INPUT_ACTIONS and ok:
                verification=result.get('verification') or {}
                if (action=='uia' and result.get('verified') is True and verification.get('matched') is True
                        and verification.get('kind') in ('value_equals','keyboard_focus')):
                    self.completed.append(dict(category='computer_verified',action=action,hwnd=args.get('hwnd')))
                    self.failures.pop('computer',None)
                    return
                item=dict(category="computer_input",action=action,hwnd=args.get("hwnd"),
                          description=f"computer.{action} 已发送，尚未复查")
                self.pending.append(item)
                self.failures.pop("computer",None)
            elif action in VERIFY_ACTIONS and ok:
                observed=args.get('hwnd') or result.get('hwnd')
                self.pending=[x for x in self.pending if not (x.get('category')=='computer_input' and
                    (not x.get('hwnd') or (observed and x.get('hwnd')==observed)))]
                if observed:
                    self.pending=[x for x in self.pending if not (x.get('category')=='application_window' and x.get('hwnd')==observed)]
                self.failures.pop("computer",None)
                self.completed.append(dict(category="computer_verified",action=action))
            elif action in INPUT_ACTIONS and not ok:
                self.failures["computer"]=f"computer.{action} 未成功"
            return
        if name=="local_computer":
            if action in ("write","run"):
                category="local_write" if action=="write" else "local_run"
                if ok and (action=="write" and result.get("written") or
                           action=="run" and result.get("exit_code")==0 and not result.get("stopped")):
                    self.failures.pop(category,None)
                    self.completed.append(dict(category=category,action=action,path=args.get("path")))
                else:
                    self.failures[category]=f"local_computer.{action} 没有成功回执"
            return
        if name=="ecosystem":
            if action=="call":
                self.failures.pop("ecosystem",None) if ok else self.failures.update(ecosystem="外部软件工具调用失败")
            return
        if name=="battleground":
            if action=="netcut" and not ok:
                self.failures["battleground_netcut"]="拔线请求失败或未获确认"
            elif action=="netcut" and ok:
                self.failures.pop("battleground_netcut",None)
                self.completed.append(dict(category="battleground_netcut",action=action))

    def blockers(self):
        reasons=[item["description"] for item in self.pending]
        reasons.extend(self.failures.values())
        return reasons

    def summary(self):
        return dict(pending=list(self.pending),failures=dict(self.failures),evidence_count=self.evidence_count,
                    completed=list(self.completed[-30:]))
