"""Short, factual feedback independent of whether the reasoning model finishes."""
import re
import time
from collections import deque
from pathlib import Path

def acknowledgement(request):
    match=re.search(r'([\w\u4e00-\u9fff .-]+\.py)',request)
    if '冒泡' in request and match:
        name=match.group(1).strip().split('下的')[-1].strip('“”「」 ')
        # Spoken ASR sometimes spells the extension out instead of writing .py.
        name='文件读写.py' if '文件读写' in request else name[-28:]
        return f'你的意思是让我在{name}里写冒泡排序。我先核对当前编辑器，然后开始。'
    summary=re.sub(r'^(用户(?:希望|要求)(?:我)?|请帮我|请)','',request).strip()
    summary=summary.split('。')[0].rstrip('，, ')
    if len(summary)>44:summary=summary[:44]+'…'
    return '收到，你想让我'+summary+'。我现在开始处理。'

def tool_feedback(name,args,result=None):
    if not isinstance(args,dict):args={}
    action=args.get('action','')
    subject=Path(args.get('path','')).name
    labels={('computer','windows'):'正在确认已打开的软件窗口',('computer','editor_context'):'正在定位编辑器的实际工作目录',
        ('browser','session_start'):'正在打开咕嘎专用浏览器',('browser','snapshot'):'正在读取网页表单和控件',
        ('browser','fill_form'):'正在填写表单并逐项回读',('browser','click'):'正在操作网页控件，随后核对结果',
        ('browser','upload'):'正在向指定网页选择文件',('browser','download'):'正在下载并核对本地文件',
        ('computer','focus'):'正在切换到目标窗口',('computer','observe'):'正在查看目标窗口和当前内容',
        ('computer','type'):'正在向编辑器输入代码',('computer','hotkey'):'正在操作编辑器快捷键',
        ('computer','click'):'正在定位编辑区域',('computer','accessibility'):'正在读取窗口控件',
        ('computer','uia'):'正在通过结构化控件执行操作',
        ('computer','editor_select_tail'):'正在选中待修改的函数，保留此前代码',
        ('screen_share','assist'):'正在读取当前共享画面和前台窗口上下文',
        ('screen_share','context'):'正在读取当前共享画面上下文',
        ('screen_share','snapshot'):'正在读取共享屏幕的最新画面',
        ('local_computer','read'):'正在读取'+(subject or '目标文件'),('local_computer','write'):'正在备份并写入'+(subject or '目标文件'),
        ('local_computer','run'):'正在执行命令并等待验证结果',('applications','open'):'正在打开目标软件',
        ('pymaster','status'):'正在确认 PyMaster 浏览器状态',('pymaster','open'):'正在打开 PyMaster 学习站',
        ('pymaster','state'):'正在读取 PyMaster 当前页面',('pymaster','chapter'):'正在翻到目标章节',
        ('pymaster','kp'):'正在展开这个知识点',('pymaster','next'):'正在翻到下一个知识点',
        ('pymaster','prev'):'正在回到上一个知识点',('pymaster','open_exercise'):'正在打开课后练习',
        ('pymaster','set_code'):'正在把代码写进编辑器',('pymaster','run'):'正在浏览器里运行这段代码',
        ('pymaster','explain'):'正在整理代码、报错和知识点',('pymaster','glossary'):'正在查站内术语表',
        ('pymaster','close_modal'):'正在关掉练习弹窗',
        ('sticker','list'):'正在读取表情库',('sticker','import'):'正在适配表情并写入待机动画',
        ('sticker','import_folder'):'正在逐个适配文件夹里的表情',
        ('sticker','probe'):'正在试算适配后的帧数',
        ('sticker','enable'):'正在启用这个表情',('sticker','disable'):'正在停用这个表情',
        ('sticker','remove'):'正在移除这个表情',
        ('sticker','doubao_status'):'正在检查豆包生图登录态',
        ('sticker','doubao_check'):'正在把这张表情传到豆包试一下',
        ('pet_control','walk'):'正在往那边走'}
    summary=labels.get((name,action),'正在执行下一步操作')
    if result is not None:
        payload=result.get('result') if isinstance(result.get('result'),dict) else result
        if name=='applications' and payload.get('selection_required'):
            return '找到了多个已运行窗口，正在选择目标，尚未完成切换。'
        if result.get('ok') and name=='computer' and action in ('click','double_click','drag','scroll','hotkey','type','editor_select_tail','uia'):
            summary=summary.replace('正在','已发送，等待复查：',1)
        elif result.get('ok'):summary=summary.replace('正在','已完成：',1)
        else:summary='这一步没有完成，我会根据错误检查后调整。'
    return summary


class EvidenceProgress:
    """User-facing digest of tool receipts, never of a model's private reasoning."""
    def __init__(self, interval=30, now=None):
        self.interval=interval
        self.last_notice=time.monotonic() if now is None else now
        self.receipts=deque(maxlen=8)
        self.count=0;self.reported=-1;self.pending='等待执行器返回结果';self.stale_reported=False
        self.beat=0
        self.facts=deque(maxlen=6)

    def tool_start(self,name,args):
        self.pending=tool_feedback(name,args)

    def tool_result(self,name,args,result):
        if not isinstance(result,dict):return
        from core.task_receipts import public_fact
        self.facts.append(public_fact(name,args,result))
        self.count+=1
        self.stale_reported=False
        # Keep only fixed tool labels; never narrate raw payloads, credentials,
        # reasoning tokens, or the executing model's unverified success claims.
        label=tool_feedback(name,args)
        from core.task_receipts import succeeded
        outcome='工具回执成功' if succeeded(result) else '未完成'
        self.receipts.append(label.replace('正在','',1)+'（'+outcome+'）')
        self.pending='等待下一步执行或核验'

    def snapshot(self,goal=''):
        return dict(goal=goal,recent_operations=list(self.facts),current=self.pending,
                    task_finished=False,new_receipts=self.count)

    def notice(self, now=None):
        now=time.monotonic() if now is None else now
        if now-self.last_notice<self.interval:return None
        self.last_notice=now
        self.beat+=1
        if self.reported==self.count:
            self.stale_reported=True
            return '这'+str(self.interval)+'秒没有新的操作回执，'+self.pending+'。还不能确定原因；下一步先等回执核对结果，任务仍在继续。'
        self.reported=self.count
        if not self.receipts:
            return '本鹅还在盯着：'+self.pending+'。目前没有可核验的结果或明确错误，下一步拿到回执后再判断，任务还没完成。'
        failed=any('未完成' in x for x in list(self.receipts)[-2:])
        analysis='有一步未通过，下一步先核对失败位置和已有结果，再决定如何调整。' if failed else '暂未收到明确错误，下一步继续核对后续回执。'
        return '本鹅报个进展：'+'；'.join(list(self.receipts)[-2:])+'。'+analysis+'任务还在继续。'
