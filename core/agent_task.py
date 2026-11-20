"""Durable, bounded task checkpoints, without credentials or screenshot payloads."""
import json
from pathlib import Path
import time
import uuid
from core.desktop_access import ROOT,redact_value

class TaskJournal:
    def __init__(self,root=None):
        self.root=Path(root or ROOT/'runtime/agent-tasks');self.data={}
    def start(self,conversation,goal):
        self.data=dict(id=uuid.uuid4().hex,conversation=conversation,goal=goal,status='running',plan=[],events=[],updated=time.time())
        workspace=(self.root/self.data['id']/'workspace').resolve();workspace.mkdir(parents=True,exist_ok=True)
        self.data['workspace']=str(workspace)
        self.save();return self.data['id']
    def latest(self,conversation):
        if not self.root.exists():return None
        candidates=sorted(self.root.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:100]
        for path in candidates:
            try:
                data=json.loads(path.read_text(encoding='utf-8'))
                if data.get('conversation')==conversation:return data
            except (OSError,ValueError):continue
        return None
    def update(self,**values):
        self.data.update(values);self.save()
    def event(self,name,result):
        self.data['events']=(self.data.get('events',[])+[dict(tool=name,ok=result.get('ok'),time=time.time())])[-120:]
        self.save()
    def save(self):
        self.data['updated']=time.time();self.root.mkdir(parents=True,exist_ok=True)
        target=self.root/(self.data['id']+'.json');temporary=target.with_suffix('.tmp')
        temporary.write_text(json.dumps(redact_value(self.data),ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(target)


# 回复前读台账：读得到就报真实步骤/失败码/证据数，读不到就明说读不到。
# 这句话必须同时出现在台账缺失提示和 reply_guard 的如实说明里，用户才对得上。
LEDGER_MISSING_PHRASE='读不到任务状态'
LEDGER_MISSING=('本机任务台账缺失或不可读，%s；步骤、失败码、产物都未确认，不会编造进度。'
                % LEDGER_MISSING_PHRASE)


def _ledger_root(root=None):
    return Path(root) if root is not None else ROOT/'runtime/agent-tasks'


def _latest_ledger(root):
    """最新一份任务台账；损坏的文件跳过继续往前找，不猜内容。"""
    if not root.is_dir():return None
    try:candidates=sorted(root.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:100]
    except OSError:return None
    for path in candidates:
        try:data=json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError):continue
        if isinstance(data,dict) and data.get('id'):return data
    return None


def ledger_snapshot(root=None):
    """读最新台账。读不到时 ok=False，由调用方如实说明，绝不填默认进度。"""
    path=_ledger_root(root)
    try:data=_latest_ledger(path)
    except OSError:data=None
    if not data:
        return dict(ok=False,code='ledger_missing',root=str(path),error='任务台账不存在或不可读')
    def mapping(key):
        value=data.get(key)
        return value if isinstance(value,dict) else {}
    return dict(ok=True,root=str(path),id=data.get('id'),conversation=data.get('conversation'),
                goal=data.get('goal'),status=data.get('status'),updated=data.get('updated'),
                chain=mapping('chain'),last_tool_failure=mapping('last_tool_failure'),
                contract=mapping('contract'))


def ledger_note(snapshot):
    """给回复提示词的一行台账摘要：真实步骤，或一句明确的「读不到」。"""
    if not isinstance(snapshot,dict) or not snapshot.get('ok'):
        return LEDGER_MISSING
    parts=[]
    if snapshot.get('goal'):parts.append('目标：%s' % snapshot['goal'])
    chain=snapshot.get('chain') or {}
    step,total=chain.get('step'),chain.get('total')
    if step and total:parts.append('第 %s/%s 步' % (step,total))
    status=snapshot.get('status') or chain.get('status')
    if status:parts.append('状态：%s' % status)
    failure=snapshot.get('last_tool_failure') or {}
    if failure.get('code'):
        parts.append('最后失败：%s' % failure['code'])
        if failure.get('error'):parts.append('原因：%s' % failure['error'])
        if failure.get('next_action'):parts.append('下一步：%s' % failure['next_action'])
    contract=snapshot.get('contract') or {}
    if contract.get('evidence_count') is not None:
        parts.append('证据条数：%s' % contract['evidence_count'])
    return '；'.join(parts)+'。' if parts else '任务台账可读，但没有可确认的步骤记录。'
