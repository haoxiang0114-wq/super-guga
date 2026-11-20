"""Per-agent local memory. Credentials are redacted; no screenshot payloads."""
import re,sqlite3,time,json
from core.agent_identity import PERSONALITIES,VOICES
from contextlib import contextmanager
from pathlib import Path

from core.desktop_access import redact

ROOT=Path(__file__).resolve().parents[1]
DEFAULT_PATH=ROOT/'runtime/multiagent/memory.sqlite3'
PROFILES={
    '001':dict(display_name='咕嘎',role='管家与陪伴伙伴',specialty='日常陪伴、习惯与计划管理、生活协助、电脑事务与任务协调',expression='happy',appearance='guga'),
    '002':dict(display_name='大肥鱼',role='技术审查员',specialty='DeepSeek Harness、方案审查、长任务拆解、测试验收与漏洞检查',expression='surprised',appearance='dafish'),
    '003':dict(display_name='Dora',role='橘子创意搭档',specialty='创意陪伴、音乐、橘子、生活话题与用户体验；同咕嘎一样可执行全部任务',expression='happy',appearance='dora'),
}
SECRET_PATTERNS=(re.compile(r'\b(?:oc_sk_[A-Za-z0-9_-]{16,}|sk-[A-Za-z0-9_-]{20,})\b'),)


def safe_text(value):
    text=redact(str(value or ''))[:12000]
    for pattern in SECRET_PATTERNS:text=pattern.sub('[REDACTED]',text)
    return text.strip()


def _tokens(text):
    clean=''.join(ch.lower() if ch.isalnum() or '\u4e00'<=ch<='\u9fff' else ' ' for ch in str(text))
    chinese=''.join(re.findall(r'[\u4e00-\u9fff]',clean))
    parts=set(re.findall(r'[a-z0-9]{2,}',clean))
    if chinese:
        parts.update(chinese[i:i+2] for i in range(max(0,len(chinese)-1)))
    return parts


def profile_facts(text,previous=''):
    """Only explicit first-person facts or short answers to a profile question.

    No LLM inference from screenshots, assistant claims, fiction or secrets.
    Kinds are stable slots so a correction replaces the old profile fact.
    """
    text=str(text or '').strip()
    if (not text or safe_text(text)!=text or len(text)>2000 or
        re.search(r'[“”「」『』"<>]|https?://|\b(?:password|token|api.?key)\b|密码|密钥|令牌|身份证|手机号|邮箱|不要记|别记|不用记|不保存|别保存|不要保存',text,re.I)):
        return []
    out=[]
    for clause in re.split(r'[，,。！!；;\n]',text):
        clause=clause.strip()
        if not clause or any(x in clause for x in ('如果','假如','假设','比如','例如','他说','她说','角色扮演','写故事',
            '什么','怎么','如何','是否','是不是','能不能','还是','吗','？','?')):continue
        address=re.fullmatch(r'(?:请)?(?:以后)?(?:叫我|称呼我为)\s*([\w\u4e00-\u9fff]{1,20})',clause)
        if address and not re.search(r'\d{7,}',address[1]):
            out.append(('profile:address','用户希望被称呼为：'+address[1]))
        communication=re.fullmatch(r'(?:请)?(?:以后|平时)(?:都)?(?:用|使用)(简体中文|中文|英语|英文)(?:跟我|和我)?(?:交流|聊天|说话|回答)',clause)
        if communication:out.append(('profile:language','用户交流语言偏好：'+communication[1]))
        occupation=re.fullmatch(r'(?:我(?:现在|目前)?(?:的职业是|的工作是|从事|是一?名?|是一?个)|职业[：:])\s*(.{2,30})',clause)
        if occupation:
            value=occupation.group(1).strip()
            if (not value.startswith(('说','想','觉得','不是','不确定','希望','让')) and
                (any(x in clause for x in ('职业','工作','从事')) or re.search(r'工程师|程序员|设计师|教师|老师|医生|护士|学生|研究生|博士生|会计|律师|产品经理|自由职业者|开发者|运营|销售|创业者',value))):
                out.append(('profile:occupation','用户职业背景：'+value))
        preference=re.fullmatch(r'我(?:现在|目前|平时|比较|更|最)?(不喜欢|不爱|喜欢|爱好是|兴趣是|偏好)\s*(.{1,60})',clause)
        if preference:
            verb,value=preference.groups();value=value.strip()
            if re.search(r'记住|密码|密钥|令牌|\d{7,}',value) or value.startswith(('这个','那个','刚才','这样','这么','你这')):continue
            key=re.sub(r'^(?:喝|吃|玩|听|看|用|做)','',value).casefold()
            out.append(('profile:preference:'+key,'用户'+('不喜欢' if verb in ('不喜欢','不爱') else '喜欢')+'：'+value))
    # A proactive question often receives a terse answer without “我”.
    if not out and previous and 1<len(text)<=35 and not re.search(r'[？?！!。；;\n]|\d{7,}',text):
        if re.search(r'(?:做什么工作|职业是什么|从事什么|什么职业)',previous):
            if re.search(r'工程师|程序员|设计师|教师|老师|医生|护士|学生|研究生|会计|律师|经理|自由职业',text):
                out.append(('profile:occupation','用户职业背景：'+text))
        elif re.search(r'(?:喜欢|爱好|兴趣).{0,16}[？?]',previous):
            if text not in ('不知道','不清楚','随便','都行','没有','不想说','不告诉你'):
                out.append(('profile:preference:'+text.casefold(),'用户偏好（回答“'+previous[:100]+'”）：'+text))
    return out[:4]


class AgentMemoryStore:
    def __init__(self,path=None):
        self.path=Path(path or DEFAULT_PATH);self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS agent_profiles (
                    agent_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, role TEXT NOT NULL,
                    specialty TEXT NOT NULL, expression TEXT NOT NULL, appearance TEXT NOT NULL DEFAULT 'guga', updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS agent_dialogue (
                    id INTEGER PRIMARY KEY, agent_id TEXT NOT NULL, role TEXT NOT NULL,
                    content TEXT NOT NULL, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS agent_dialogue_agent_created
                    ON agent_dialogue(agent_id,created DESC);
                CREATE TABLE IF NOT EXISTS agent_facts (
                    id INTEGER PRIMARY KEY, agent_id TEXT NOT NULL, kind TEXT NOT NULL,
                    content TEXT NOT NULL, importance INTEGER NOT NULL DEFAULT 2,
                    created REAL NOT NULL, last_used REAL, uses INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS agent_facts_agent_importance
                    ON agent_facts(agent_id,importance DESC,created DESC);
                CREATE TABLE IF NOT EXISTS shared_dialogue (
                    id INTEGER PRIMARY KEY, speaker TEXT NOT NULL, role TEXT NOT NULL,
                    content TEXT NOT NULL, created REAL NOT NULL);
            ''')
            columns={row[1] for row in db.execute('PRAGMA table_info(agent_profiles)')}
            for name,default in (('user_instructions',''),('user_address','饲养员')):
                if name not in columns:
                    db.execute("ALTER TABLE agent_profiles ADD COLUMN "+name+" TEXT NOT NULL DEFAULT '"+default+"'")
            if 'appearance' not in columns:db.execute("ALTER TABLE agent_profiles ADD COLUMN appearance TEXT NOT NULL DEFAULT 'guga'")
            # Additive migration: old dialogue is retained, never assigned to a guessed chat.
            for table, additions in (
                ('agent_dialogue', [('conversation_id', "TEXT NOT NULL DEFAULT ''"), ('project_id', "TEXT NOT NULL DEFAULT ''")]),
                ('agent_facts', [('scope', "TEXT NOT NULL DEFAULT 'agent'"), ('scope_id', "TEXT NOT NULL DEFAULT ''"),
                                 ('source_conversation', "TEXT NOT NULL DEFAULT ''")])):
                existing_columns={row[1] for row in db.execute('PRAGMA table_info('+table+')')}
                for name,definition in additions:
                    if name not in existing_columns:db.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+definition)
            db.execute("UPDATE agent_facts SET scope='user' WHERE scope='agent' AND (kind LIKE 'profile:%' OR kind='user_preference')")
            db.execute('CREATE INDEX IF NOT EXISTS dialogue_conversation ON agent_dialogue(agent_id,conversation_id,id)')
            for column,definition in (('personality',"TEXT NOT NULL DEFAULT ''"),('voice_settings',"TEXT NOT NULL DEFAULT '{}'")):
                if column not in columns:db.execute('ALTER TABLE agent_profiles ADD COLUMN '+column+' '+definition)
            for agent_id,profile in PROFILES.items():
                existing=db.execute('SELECT * FROM agent_profiles WHERE agent_id=?',(agent_id,)).fetchone()
                if existing is None:
                    db.execute('INSERT INTO agent_profiles (agent_id,display_name,role,specialty,expression,appearance,updated,personality,voice_settings) VALUES (?,?,?,?,?,?,?,?,?)',
                        (agent_id,profile['display_name'],profile['role'],profile['specialty'],profile['expression'],profile['appearance'],time.time(),PERSONALITIES[agent_id],json.dumps(VOICES[agent_id])))
                else:
                    # Migrate old placeholder identities while preserving a user-edited specialty.
                    old=dict(existing);specialty=old.get('specialty') or profile['specialty']
                    if str(old.get('display_name','')).startswith('咕嘎 00'):specialty=profile['specialty']
                    if agent_id=='001' and specialty=='综合分析、通用任务、电脑操作与项目监督':specialty=profile['specialty']
                    personality=old.get('personality') or PERSONALITIES[agent_id]
                    if agent_id=='001' and personality=='你是咕嘎，保留原来的娇憨、调皮、体贴的小企鹅人格。':
                        personality=PERSONALITIES[agent_id]
                    voices=old.get('voice_settings')
                    if not voices or voices=='{}':voices=json.dumps(VOICES[agent_id])
                    db.execute('UPDATE agent_profiles SET display_name=?,role=?,specialty=?,expression=?,appearance=?,updated=?,personality=?,voice_settings=? WHERE agent_id=?',
                        (profile['display_name'],profile['role'],specialty,profile['expression'],profile['appearance'],time.time(),personality,voices,agent_id))

    @contextmanager
    def connect(self):
        db=sqlite3.connect(str(self.path),timeout=5);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()

    def profile(self,agent_id):
        agent_id=str(agent_id)
        with self.connect() as db:row=db.execute('SELECT * FROM agent_profiles WHERE agent_id=?',(agent_id,)).fetchone()
        return dict(row) if row else None

    def profiles(self):
        with self.connect() as db:return [dict(r) for r in db.execute('SELECT * FROM agent_profiles ORDER BY agent_id')]

    def update_profile(self,agent_id,specialty=None,expression=None,role=None,personality=None,voice_settings=None,user_instructions=None,user_address=None):
        profile=self.profile(agent_id)
        if not profile:raise ValueError('未知咕嘎编号')
        specialty=safe_text(specialty) if specialty is not None else profile['specialty']
        role=safe_text(role) if role is not None else profile['role']
        expression=expression if expression in ('normal','happy','sleepy','surprised','sad','playful') else profile['expression']
        if not specialty:raise ValueError('擅长领域不能为空')
        personality=safe_text(personality) if personality is not None else profile['personality']
        instructions=profile.get('user_instructions','') if user_instructions is None else str(user_instructions).strip()
        address=profile.get('user_address','饲养员') if user_address is None else str(user_address).strip()
        if len(instructions)>8000 or safe_text(instructions)!=instructions:
            raise ValueError('习惯与提示词最多 8000 字，不能包含密钥')
        if not address or len(address)>20 or safe_text(address)!=address:
            raise ValueError('称呼需要 1—20 字，不能包含密钥')
        if voice_settings is not None:
            if not isinstance(voice_settings,dict):raise ValueError('音色设置必须为对象')
            allowed={'qwen31','qwen30','edge_voice','edge_rate','edge_pitch'}
            if set(voice_settings)-allowed:raise ValueError('未知音色字段')
            if any(not isinstance(v,str) or not re.fullmatch(r'[A-Za-z0-9_+%.:-]{1,100}',v) for v in voice_settings.values()):raise ValueError('音色参数无效')
            voices=json.dumps(voice_settings)
        else:voices=profile['voice_settings']
        with self.connect() as db:db.execute('UPDATE agent_profiles SET specialty=?,expression=?,role=?,updated=?,personality=?,voice_settings=?,user_instructions=?,user_address=? WHERE agent_id=?',
            (specialty,expression,role,time.time(),personality,voices,instructions,address,str(agent_id)))
        return self.profile(agent_id)

    def add_dialogue(self,agent_id,role,content,conversation_id=None,project_id=None):
        text=safe_text(content)
        if not text:return
        with self.connect() as db:
            previous=db.execute('SELECT role,content FROM agent_dialogue WHERE agent_id=? AND conversation_id=? ORDER BY id DESC LIMIT 1',
                                (str(agent_id),str(conversation_id or ''))).fetchone()
            db.execute('INSERT INTO agent_dialogue(agent_id,role,content,created,conversation_id,project_id) VALUES (?,?,?,?,?,?)',
                (str(agent_id),str(role),text,time.time(),str(conversation_id or ''),str(project_id or '')))
            if role=='user':
                context=previous['content'] if previous and previous['role']=='assistant' else ''
                for kind,fact in profile_facts(content,context):
                    db.execute("DELETE FROM agent_facts WHERE agent_id=? AND kind=? AND scope='user'",(str(agent_id),kind))
                    db.execute("INSERT INTO agent_facts(agent_id,kind,content,importance,created,scope,source_conversation) VALUES (?,?,?,?,?,'user',?)",
                        (str(agent_id),kind,fact,4,time.time(),str(conversation_id or '')))

    def remember(self,agent_id,content,kind='note',importance=2,scope='agent',scope_id='',source_conversation=''):
        text=safe_text(content)
        if not text:raise ValueError('记忆内容不能为空')
        importance=max(1,min(5,int(importance)))
        if scope not in ('agent','user','project','conversation'):raise ValueError('未知记忆层级')
        if scope in ('project','conversation') and not scope_id:raise ValueError('项目或对话记忆需要明确来源')
        with self.connect() as db:
            if db.execute('SELECT 1 FROM agent_facts WHERE agent_id=? AND kind=? AND content=? AND scope=? AND scope_id=?',
                          (str(agent_id),str(kind),text,scope,str(scope_id))).fetchone():return
            db.execute('INSERT INTO agent_facts(agent_id,kind,content,importance,created,scope,scope_id,source_conversation) VALUES (?,?,?,?,?,?,?,?)',
                (str(agent_id),str(kind),text,importance,time.time(),scope,str(scope_id),str(source_conversation or '')))

    def shared_turn(self,speaker,role,content):
        text=safe_text(content)
        if not text:return
        with self.connect() as db:db.execute('INSERT INTO shared_dialogue(speaker,role,content,created) VALUES (?,?,?,?)',
            (str(speaker),str(role),text,time.time()))

    def forget(self,agent_id,ident):
        with self.connect() as db:
            count=db.execute('DELETE FROM agent_facts WHERE agent_id=? AND id=?',(str(agent_id),int(ident))).rowcount
        return dict(deleted=bool(count))

    @staticmethod
    def _scope_filter(conversation_id=None,project_id=None):
        sql="(scope IN ('agent','user')"
        values=[]
        for scope,ident in (('conversation',conversation_id),('project',project_id)):
            if ident:
                sql+=' OR (scope=? AND scope_id=?)';values.extend((scope,str(ident)))
        return sql+')',values

    def facts(self,agent_id,limit=30,conversation_id=None,project_id=None):
        condition,values=self._scope_filter(conversation_id,project_id)
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT id,kind,content,importance,scope,scope_id,source_conversation,created FROM agent_facts WHERE agent_id=? AND '+condition+' ORDER BY id DESC LIMIT ?',
                                              [str(agent_id),*values,max(1,min(100,int(limit)))])]

    def recent_dialogue(self,agent_id,limit=12,conversation_id=None):
        """这个成员最近说过的话。

        给了 conversation_id 就只看那一段对话；没给就按成员整体取最近几条。
        原来没给会话号时直接返回空列表，结果两处都读不到东西：
        议会讨论落库时不带会话号，写完再也读不回来；主动搭话在没有当前对话时
        也拿不到「最近说过什么」，只能退回空记忆。
        recall(include_dialogue=True) 仍然要求明确的 conversation_id，
        不会把旧对话当成长期记忆带进新话题。
        """
        rows_limit=max(1,min(100,int(limit)))
        with self.connect() as db:
            if conversation_id:
                rows=list(db.execute('SELECT role,content,created FROM agent_dialogue WHERE agent_id=? AND conversation_id=? ORDER BY id DESC LIMIT ?',
                                     (str(agent_id),str(conversation_id),rows_limit)))
            else:
                rows=list(db.execute('SELECT role,content,created FROM agent_dialogue WHERE agent_id=? ORDER BY id DESC LIMIT ?',
                                     (str(agent_id),rows_limit)))
        return [dict(r) for r in reversed(rows)]

    def recall(self,agent_id,query='',limit=6,include_dialogue=False,conversation_id=None,project_id=None):
        """Only stable facts by default. Raw chat history is NOT a memory:
        pulling old error reports into a new call is what made the pet bring
        up previous failures as if they were things the user had said."""
        q=_tokens(query)
        condition,values=self._scope_filter(conversation_id,project_id)
        with self.connect() as db:
            facts=[dict(r) for r in db.execute('SELECT * FROM agent_facts WHERE agent_id=? AND '+condition+' ORDER BY importance DESC,created DESC LIMIT 120',[str(agent_id),*values])]
        dialogue=self.recent_dialogue(agent_id,40,conversation_id) if include_dialogue and conversation_id else []
        scored=[]
        now=time.time()
        for i,item in enumerate(facts):
            text=item['content'];overlap=len(q & _tokens(text)) if q else 0
            # Unrelated task notes must not displace stable owner facts in a new topic.
            if q and not overlap and not (item['scope']=='user' or item['kind'].startswith('profile:')):continue
            score=int(item['importance'])*4+overlap*8+(1/(1+i))
            scored.append((score,item['kind'],text,'fact',item['scope'],item['created'],item['id'],item['source_conversation']))
        for item in dialogue:
            text=item['content'];overlap=len(q & _tokens(text)) if q else 0
            age=(now-float(item['created'] or now))/86400
            score=overlap*7+max(0,3-age)*.5+(1 if item['role']=='user' else 0)
            scored.append((score,'dialogue',text,item['role'],'conversation',item['created'],None,str(conversation_id)))
        scored.sort(reverse=True,key=lambda x:x[0]);out=[];seen=set()
        for score,kind,text,role,scope,created,ident,source in scored:
            key=text.casefold()
            if key in seen:continue
            seen.add(key);out.append(dict(id=ident,kind=kind,role=role,scope=scope,content=text[:1600],created=created,
                                         source_conversation=source,score=round(score,2)))
            if len(out)>=limit:break
        return out

    def proactive_topics(self,agent_id,limit=4,conversation_id=None,project_id=None):
        rows=[r for r in self.recent_dialogue(agent_id,16,conversation_id) if r['role']=='user']
        if not rows and conversation_id:
            # 当前对话里还没有用户发言（刚开新对话，或主动搭话时用户没说话）。
            # 可以借用「不属于任何具体会话」的发言（conversation_id 为空：议会讨论、
            # 旧版本写入、没归档的随口一说）；但属于别的会话的正文一律不搬过来——
            # 换个对话还被追问同一件事，就是记忆串台。
            with self.connect() as db:
                rows=[dict(r) for r in reversed(list(db.execute(
                    "SELECT role,content,created FROM agent_dialogue "
                    "WHERE agent_id=? AND conversation_id='' AND role='user' "
                    "ORDER BY id DESC LIMIT ?",
                    (str(agent_id),16))))]
        facts=self.facts(agent_id,12,conversation_id,project_id)
        topics=[safe_text(r['content'])[:500] for r in rows if len(safe_text(r['content']))>=4]
        topics[:0]=[safe_text(r['content'])[:500] for r in facts if len(safe_text(r['content']))>=4]
        out=[]
        for text in topics:
            if text.casefold() not in {x.casefold() for x in out}:out.append(text)
            if len(out)>=limit:break
        return out

    def status(self):
        with self.connect() as db:
            counts=[]
            for profile in db.execute('SELECT agent_id,display_name,role,specialty FROM agent_profiles ORDER BY agent_id'):
                item=dict(profile);item['dialogue_count']=db.execute('SELECT COUNT(*) FROM agent_dialogue WHERE agent_id=?',(item['agent_id'],)).fetchone()[0]
                item['fact_count']=db.execute('SELECT COUNT(*) FROM agent_facts WHERE agent_id=?',(item['agent_id'],)).fetchone()[0]
                counts.append(item)
        return dict(path=str(self.path),agents=counts)
