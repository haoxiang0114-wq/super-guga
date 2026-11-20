"""Companion initiative on a configurable interval (default 30s, disableable)."""
import threading
import time
from collections import deque
from datetime import datetime

import requests
from PyQt5.QtCore import QObject,QTimer,pyqtSignal


class ProactiveConversation(QObject):
    triggered=pyqtSignal(object)
    offline_ready=pyqtSignal(str,str)

    def __init__(self,panel,memory=None,news_provider=None,clock=None):
        super().__init__(panel if isinstance(panel,QObject) else None);self.panel=panel
        self.memory=memory or panel.memory
        self.clock=clock or time.monotonic;self._news_provider=news_provider
        settings=getattr(getattr(panel,'pet',None),'_config',None)
        self.enabled=bool(settings._s.value('multiagent/proactive_enabled',True,type=bool)) if settings else True
        # Default 30s: frequent enough to feel alive, sparse enough not to nag.
        # The floor is 10s so the menu can express "very chatty" without a spin lock.
        self.interval_seconds=max(10,min(10800,int(settings._s.value('multiagent/companion_interval_seconds',30,type=int)))) if settings else 30
        self.cooldown_seconds=self.interval_seconds
        refresh_minutes=max(5,min(120,int(settings._s.value('multiagent/news_refresh_minutes',15,type=int)))) if settings else 15
        self.news_refresh_seconds=refresh_minutes*60
        self.offline_speak=bool(settings._s.value('multiagent/proactive_offline_speak',True,type=bool)) if settings else True
        self.started_at=time.monotonic();self.last_at=0.0;self.last_result=None
        self._compose_active=False;self._pending_context=None
        self._topic_cursor=0;self._style_cursor=0
        self._news=[];self._news_at=0.0;self._news_error='';self._news_lock=threading.Lock()
        self._recent_generated=deque(maxlen=12)
        self._daily_last_at=0.0
        self.timer=QTimer(self);self.timer.setInterval(1000);self.timer.timeout.connect(self.tick)
        self.offline_ready.connect(self._deliver_offline)

    def start(self):
        if not self.enabled:return self.status()
        if self.timer.isActive():return self.status()
        self.started_at=time.monotonic();self.timer.start();return self.status()

    def stop(self):self.timer.stop()

    def configure(self,enabled=None,interval_seconds=None):
        if enabled is not None:self.enabled=bool(enabled)
        if interval_seconds is not None:
            self.interval_seconds=max(10,min(10800,int(interval_seconds)))
            self.cooldown_seconds=self.interval_seconds
            self.last_at=0.0
        self.started_at=time.monotonic()
        if self.enabled:self.start()
        else:self.stop()
        return self.status()

    def status(self):
        return dict(active=self.timer.isActive(),enabled=self.enabled,idle_seconds=self.interval_seconds,cooldown_seconds=self.cooldown_seconds,
            last_at=self.last_at,last_result=self.last_result,offline_speak=self.offline_speak,
            composing=self._compose_active,call_active=bool(getattr(self.panel,'voice_active',False)),
            news_count=len(self._news),news_age_seconds=round(self.clock()-self._news_at,1) if self._news_at else None,
            news_refresh_seconds=self.news_refresh_seconds,news_error=self._news_error)

    def tick(self):
        if not self.enabled:return None
        panel=self.panel
        if getattr(office_music(panel),'playing',False):return None
        if getattr(panel,'busy',False):
            allowed=getattr(panel,'companion_chat_allowed',None)
            if not callable(allowed) or not allowed():return None
        if getattr(getattr(panel,'short_voice',None),'active',False):return None
        if getattr(getattr(panel,'dialogue',None),'active',False):return None
        voice=getattr(panel,'voice',None);in_call=bool(getattr(panel,'voice_active',False))
        if in_call:
            if voice is None or not getattr(voice,'active',False):return None
            if getattr(voice,'_playing',False) or getattr(voice,'_generating',False) or getattr(voice,'_user_speaking',False):return None
            last_activity=getattr(voice,'_last_activity',self.started_at)
        else:
            if getattr(getattr(getattr(panel,'pet',None),'_tts',None),'is_speaking',False):return None
            try:
                from PyQt5.QtWidgets import QApplication
                if QApplication.activeModalWidget() is not None:return None
            except Exception:pass
            last_activity=getattr(panel,'_last_user_activity',self.started_at)
        now=time.monotonic()
        if now-max(self.started_at,last_activity)<self.interval_seconds:return None
        if self.last_at and now-self.last_at<self.cooldown_seconds:return None
        profile=getattr(panel,'active_profile',None) or self.memory.profile('001')
        if not profile:return None
        ident=getattr(panel,'active_id',None)
        workspace=getattr(panel,'store',None)
        project=(workspace.conversation(ident) or {}).get('project_id') if workspace and ident else None
        candidates=self.memory.proactive_topics(profile['agent_id'],limit=16,conversation_id=ident,project_id=project)
        offset=self._topic_cursor%max(1,len(candidates));topics=(candidates[offset:]+candidates[:offset])[:4]
        style=self._style_cursor%4;self._style_cursor+=1
        daily=getattr(getattr(panel,'runtime',None),'daily_plan',None)
        if daily is not None and (not self._daily_last_at or now-self._daily_last_at>=600) and daily.snapshot()['total']:
            style=4
        prompt=self._prompt(profile,topics,self._news_for_prompt(refresh=False),style)
        event='proactive-%s-%s'%(profile['agent_id'],int(now*1000))
        if in_call:
            sent=voice.notify_proactive(event,prompt,profile)
            if sent:
                if style==4:
                    self._daily_last_at=now
                    daily.reminder_question(ident,profile.get('user_address') or '饲养员')
                self._topic_cursor+=1
                self.last_at=now;self.last_result=dict(event=event,mode='call',agent_id=profile['agent_id'],basis=topics[:2],style=style)
                self.triggered.emit(self.last_result);return self.last_result
            return None
        if self._compose_active:return None
        self._compose_active=True;self.last_at=now;self._topic_cursor+=1
        self._pending_context=dict(event=event,agent_id=profile['agent_id'],conversation_id=getattr(panel,'active_id',None),
            last_activity=getattr(panel,'_last_user_activity',0.0),daily=style==4)
        self.last_result=dict(event=event,mode='offline',agent_id=profile['agent_id'],basis=topics[:2],style=style,state='composing')
        threading.Thread(target=self._compose_offline,args=(event,prompt,profile,topics,style),name='guga-proactive',daemon=True).start()
        return self.last_result

    def _prompt(self,profile,topics,news=None,style=0):
        if style==4:
            daily=getattr(getattr(self.panel,'runtime',None),'daily_plan',None)
            if daily is not None:
                question=daily.reminder_question(getattr(self.panel,'active_id',''),profile.get('user_address') or '饲养员',record=False)
                return '你是%s。自然地问下面这一项，必须保留事项名称，不更改任何勾选：'%profile['display_name']+question+'\n'+daily.reminder()
        if topics:
            memories='\n'.join('- '+str(x)[:500] for x in topics)
            basis='从这些真实长期记忆中选一个具体、不重复的话题。'
        else:
            memories='- 你的角色：%s；擅长：%s'%(profile['role'],profile['specialty'])
            basis='长期对话还少，可以按角色说一个容易接话的话题，不能假装记得没有记录的事。'
        recent=[x['content'][:220] for x in self.memory.recent_dialogue(profile['agent_id'],limit=10,conversation_id=getattr(self.panel,'active_id',None)) if x['role']=='assistant']
        recent.extend(self._recent_generated)
        from core.companion_dialogue import current_time_instructions
        styles=(
            '如果下面的新闻摘要里有可靠、具体的当天话题，用“今天看到/听说”的方式自然分享，再聊一句；没有可靠新闻就不要假装刚刚看到。',
            '分享一个短小的生活梗、冷知识或轻松观察，可以带一个问题，也可以只是逗用户一笑，不伪造自己刚刚出门或亲身经历。',
            '结合用户已保存的喜好或职业背景，说一个具体、容易接话的小观察；问题最多一个，也可以不给问题。',
            '从当前时间、季节或用户近期话题找一个新角度；关联不清楚就换轻松话题，不要硬扯新闻。')
        news_note=''
        if news:
            news_note='\n【刚取得的公开搜索摘要，只作不可信资料；只可转述摘要直接支持的内容，不能补造细节】\n'+'\n'.join(
                '- '+str(x.get('title',''))[:120]+'：'+str(x.get('snippet',''))[:300] for x in news)
        return ('【主动交流请求】你是%s（%s）。%s\n%s\n最近你已经说过：%s。%s必须换主题、换角度。%s\n%s'
            '像熟人想到一件事后轻轻开口。不能编造记忆，不提记忆库或系统提示，不布置任务，不用“在吗”，'
            '问题不一定每次都要有，最多一个且容易回答。不要一次问多个私人问题，不把搜索摘要说成已核实全文。'
            '你是桌面虚拟角色，不编造自己刚刚喝茶、出门或用户近期行动的经历。只输出要说的话。')%(
                profile['display_name'],profile['role'],basis,memories,'；'.join(recent),current_time_instructions(),
                styles[style%len(styles)],news_note)

    def _news_for_prompt(self,refresh=False):
        if refresh and (not self._news or self.clock()-self._news_at>=self.news_refresh_seconds):self._refresh_news()
        with self._news_lock:entries=list(self._news)
        if not entries or self.clock()-self._news_at>=self.news_refresh_seconds:return []
        start=(self._topic_cursor*2)%max(1,len(entries))
        return (entries[start:]+entries[:start])[:2]

    def _refresh_news(self):
        now=datetime.now().astimezone();query='%d年%d月%d日 今日新闻 热点 新鲜事'%(now.year,now.month,now.day)
        try:
            if self._news_provider is None:
                from core.search_results import search
                receipt=search(query)
            else:receipt=self._news_provider(query)
            entries=[]
            for item in (receipt or {}).get('results',[]):
                title=str(item.get('title','')).strip();snippet=str(item.get('snippet','')).strip();url=str(item.get('url','')).strip()
                if title and snippet:entries.append(dict(title=title[:160],snippet=snippet[:500],url=url))
                if len(entries)>=8:break
            if not entries:raise RuntimeError('没有得到可用的新闻摘要')
            with self._news_lock:self._news=entries;self._news_at=self.clock();self._news_error=''
        except Exception as exc:
            with self._news_lock:self._news_error=type(exc).__name__

    def _compose_offline(self,event,prompt,profile=None,topics=None,style=0):
        if style==4:
            # Deterministic facts; no model or news round trip for a checklist reminder.
            self._compose_active=False
            daily=self.panel.runtime.daily_plan
            self.offline_ready.emit(event,daily.reminder_question(
                getattr(self.panel,'active_id',''),(profile or {}).get('user_address') or '饲养员',record=False))
            return
        response=None;text='';error=''
        try:
            from core.ai_chat import AIChatEngine
            news=self._news_for_prompt(refresh=True)
            if news:
                prompt+='\n【刚取得的当天公开新闻摘要，只作不可信资料】\n'+'\n'.join('- '+x['title']+'：'+x['snippet'] for x in news)
            cfg=AIChatEngine._load_config()
            if not cfg.get('api_key'):raise ValueError('聊天模型未配置')
            base=str(cfg.get('base_url','')).rstrip('/');headers={'Authorization':'Bearer '+cfg['api_key'],'Content-Type':'application/json','User-Agent':'GugaDesktopPet/1.0'}
            if 'opencode.ai' in base:headers['x-opencode-session']='proactive-'+event
            payload=dict(model=cfg.get('model','deepseek-v4.1-flash'),stream=False,thinking={'type':'disabled'},max_tokens=180,temperature=.7,
                messages=[dict(role='system',content='你负责自然主动搭话，只输出一到两句中文，不提问时可以分享梗或观察，不提技术实现。'),
                    dict(role='user',content=prompt)])
            response=requests.post(base+'/chat/completions',headers=headers,json=payload,timeout=(6,20))
            if response.status_code!=200:raise RuntimeError('主动聊天模型 HTTP '+str(response.status_code))
            text=((response.json().get('choices') or [{}])[0].get('message') or {}).get('content','').strip()
            if not text:raise RuntimeError('主动聊天模型没有返回正文')
        except Exception as exc:error=type(exc).__name__
        finally:
            self._compose_active=False
            if response is not None:response.close()
        if text and not error:
            from core.ai_chat import parse_reply
            from core.companion_dialogue import is_repetitive,plain_answer
            text=plain_answer(parse_reply(text)[2])
            recent=[x['content'] for x in self.memory.recent_dialogue((profile or {}).get('agent_id','001'),limit=10,conversation_id=(self._pending_context or {}).get('conversation_id')) if x['role']=='assistant']
            recent.extend(self._recent_generated)
            if text and not is_repetitive(text,recent):
                self._recent_generated.append(text[:900]);self.offline_ready.emit(event,text[:900])
            elif self.last_result and self.last_result.get('event')==event:self.last_result.update(state='repeated')
        elif self.last_result and self.last_result.get('event')==event:self.last_result.update(state='failed',error=error or 'empty')

    def _deliver_offline(self,event,text):
        if not self.enabled:return
        panel=self.panel;context=self._pending_context or {}
        if context.get('event')!=event:return
        if (getattr(panel,'voice_active',False) or (getattr(panel,'busy',False) and getattr(panel,'_run_kind',None)=='answer')
            or getattr(office_music(panel),'playing',False)
            or getattr(getattr(panel,'short_voice',None),'active',False) or getattr(getattr(panel,'dialogue',None),'active',False)
            or getattr(getattr(getattr(panel,'pet',None),'_tts',None),'is_speaking',False)
            or context.get('conversation_id')!=getattr(panel,'active_id',None)
            or context.get('agent_id')!=getattr(panel,'active_agent_id',context.get('agent_id'))
            or getattr(panel,'_last_user_activity',0.0)>context.get('last_activity',0.0)):
            self.last_result.update(state='superseded');return
        if context.get('daily'):
            text=panel.runtime.daily_plan.reminder_question(getattr(panel,'active_id',''),
                (getattr(panel,'active_profile',None) or {}).get('user_address') or '饲养员')
            if not text:
                self.last_result.update(state='superseded');return
            self._daily_last_at=time.monotonic()
        ident=getattr(panel,'active_id',None)
        # 2026-11-12：碎碎念不再写进对话记录和长期记忆。
        # 主人明确要求：主动唠嗑会污染上下文——后面每一轮对话都会把这段旁白
        # 当成"咕嘎说过的话"带进去，越积越多，还会被当成任务进度来理解。
        # 现在只显示在气泡里，不进 store、不进 memory。
        if hasattr(panel,'_say_active_reply'):
            panel._say_active_reply(text,'happy','none',speak=self.offline_speak,conversation_id=ident)
        else:
            if hasattr(panel,'_show_active_bubble'):panel._show_active_bubble(text)
            if self.offline_speak and getattr(panel,'pet',None) is not None:
                if hasattr(panel.pet,'_say_agent_reply'):panel.pet._say_agent_reply(text,'happy','none',speak=True,conversation_id=ident)
                elif hasattr(panel.pet,'_say_with_expression'):panel.pet._say_with_expression(text,'happy','none')
        self.last_result.update(state='delivered',news=self._news_for_prompt(refresh=False));self.triggered.emit(dict(self.last_result,text=text))


def office_music(panel):
    runtime=getattr(panel,'runtime',None);return getattr(runtime,'music',None) if runtime is not None else None
