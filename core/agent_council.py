"""Bounded, tool-free discussion between persisted Guga identities."""
import threading
from urllib.parse import urlsplit
import requests
from PyQt5.QtCore import QObject,pyqtSignal

from core.ai_chat import AIChatEngine


class DiscussionWorker(QObject):
    turn=pyqtSignal(object)
    completed=pyqtSignal(object)

    def __init__(self,parent=None,memory=None,config=None):
        super().__init__(parent)
        from core.agent_memory import AgentMemoryStore
        self.memory=memory or AgentMemoryStore();self.config=config
        self.active=False;self.cancel_event=threading.Event();self.generation=0

    def start(self,topic,agent_ids=('001','002'),rounds=1):
        ids=[]
        for agent_id in agent_ids:
            if str(agent_id) not in ids:ids.append(str(agent_id))
        if len(ids)<2:raise ValueError('讨论至少需要两位咕嘎')
        if len(ids)>3:raise ValueError('一次最多三位咕嘎讨论')
        topic=str(topic or '').strip()
        if not 2<=len(topic)<=1000:raise ValueError('讨论话题需要 2—1000 字')
        if self.active:raise RuntimeError('上一场讨论尚未结束')
        self.active=True;self.generation+=1;generation=self.generation;self.cancel_event=threading.Event()
        cfg=dict(self.config if self.config is not None else AIChatEngine._load_config())
        transcript=[];results=[]
        def work():
            try:
                if not cfg.get('api_key'):raise RuntimeError('请先配置讨论所用模型 API')
                base=cfg.get('base_url','https://api.deepseek.com').rstrip('/')
                endpoint=urlsplit(base)
                headers={'Authorization':'Bearer '+cfg['api_key'],'Content-Type':'application/json','User-Agent':'GugaDesktopPet/1.0'}
                if endpoint.hostname=='opencode.ai' and endpoint.path.startswith(('/inference/go/','/zen/go/')):
                    headers['x-opencode-session']='guga-council-'+str(generation)
                for round_index in range(max(1,min(3,int(rounds)) )):
                    for agent_id in ids:
                        if self.cancel_event.is_set():break
                        profile=self.memory.profile(agent_id)
                        if not profile:raise RuntimeError('成员档案不存在：'+agent_id)
                        recalled=self.memory.recall(agent_id,topic,limit=4)
                        memory_text='\n'.join('- '+x['content'][:600] for x in recalled) or '（尚无更多长期记忆）'
                        system=('你是%s（编号%s），咕嘎小团队的%s。你的擅长：%s。你正在和%s讨论用户给出的问题。'
                            '保持自己的立场和专长，可以同意、补充或具体反驳；用自然中文说180字以内，不要自称AI，不要输出思维链、工具日志或舞台标签。'
                            '只有以下长期记忆属于你；团队对话是公开资料，不能假装知道别人私有记忆。\n长期记忆：\n%s' %
                            (profile['display_name'],agent_id,profile['role'],profile['specialty'],
                             '另外两位咕嘎' if len(ids)>2 else '另一位咕嘎',memory_text))
                        from core.agent_identity import identity_instructions
                        system+=identity_instructions(profile)
                        dialogue='\n'.join('%s：%s'%(x['speaker'],x['text']) for x in transcript[-8:]) or '（由你先开口）'
                        prompt='讨论主题：'+topic+'\n已有对话：\n'+dialogue+'\n请给出你的下一段意见，直接说内容。'
                        payload=dict(model=cfg.get('model','deepseek-v4.1-flash'),
                            messages=[dict(role='system',content=system),dict(role='user',content=prompt)],
                            max_tokens=700,stream=False,thinking={'type':'disabled'})
                        if endpoint.hostname=='opencode.ai' and endpoint.path.startswith(('/inference/go/','/zen/go/')):
                            payload['reasoning_effort']='none'
                        response=requests.post(base+'/chat/completions',headers=headers,json=payload,timeout=(6,45))
                        if response.status_code!=200:raise RuntimeError('讨论服务返回 HTTP '+str(response.status_code))
                        data=response.json();text=str(((data.get('choices') or [{}])[0].get('message') or {}).get('content') or '').strip()
                        if not text:raise RuntimeError('讨论模型没有返回正文')
                        text=text[:1800]
                        self.memory.add_dialogue(agent_id,'assistant',text)
                        self.memory.shared_turn(agent_id,'assistant',text)
                        item=dict(agent_id=agent_id,profile=profile,text=text,round=round_index+1,generation=generation)
                        results.append(item);transcript.append(dict(speaker=profile['display_name'],text=text))
                        if generation==self.generation:self.turn.emit(item)
                    if self.cancel_event.is_set():break
                status=dict(ok=True,cancelled=False,generation=generation,results=results,topic=topic)
            except Exception as exc:
                status=dict(ok=False,cancelled=self.cancel_event.is_set(),generation=generation,results=results,
                    topic=topic,error=str(exc) if not isinstance(exc,requests.RequestException) else '讨论服务连接失败')
            finally:
                self.active=False
                if generation==self.generation:self.completed.emit(status)
        threading.Thread(target=work,daemon=True).start()
        return dict(accepted=True,generation=generation,agents=ids,topic=topic)

    def cancel(self):
        if not self.active:return
        self.cancel_event.set();self.generation+=1;self.active=False


def council_payload(config,profile,memories,topic,dialogue):
    """Small pure helper used by tests; no network or credentials."""
    system=('你是%s，角色%s，擅长%s。请就主题与同伴具体讨论，180字以内，不输出思维链。\n长期记忆：\n%s'%
        (profile['display_name'],profile['role'],profile['specialty'],memories))
    return dict(model=config.get('model','deepseek-v4.1-flash'),messages=[
        dict(role='system',content=system),dict(role='user',content='讨论主题：'+topic+'\n已有对话：\n'+dialogue)],
        max_tokens=700,stream=False,thinking={'type':'disabled'})
