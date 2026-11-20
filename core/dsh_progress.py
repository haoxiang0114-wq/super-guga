"""Minute reports from actual DSH state and optional local video progress logs."""
import json
import re
import time
from pathlib import Path


def reporting_config(store, conversation_id):
    try:
        config=json.loads(store.preference('dsh_reporting:'+conversation_id) or '{}')
    except (TypeError,ValueError):
        config={}
    return dict(dict(enabled=True,interval_seconds=60,phase='task'),**config)


def tail(path, limit=14000):
    try:
        with Path(path).open('rb') as stream:
            stream.seek(0,2);stream.seek(max(0,stream.tell()-limit))
            return stream.read(limit).decode('utf-8','replace')
    except OSError:return ''


def progress_sentence(state, config):
    if state.get('state')=='disconnected':
        return '我暂时没有连上 DSH，任务结果还不能确认，先把这个情况告诉你。'
    if state.get('state')=='queued':
        return '任务已经交给 DSH，目前还在等待这一轮的执行回执，我会继续跟进。'
    phase=config.get('phase','task')
    work=Path(config['work_directory']) if config.get('work_directory') else None
    if phase=='processing' and work:
        def current_log(name):
            path=work/name
            try:
                if path.stat().st_mtime<float(state.get('started_at') or 0):return ''
            except OSError:return ''
            return tail(path)
        rendered=current_log('render.log')
        matches=re.findall(r'time=(\d+):(\d+):(\d+(?:\.\d+)?)',rendered)
        duration=float(config.get('duration_seconds') or 0)
        if matches and duration>0:
            h,m,s=map(float,matches[-1]);position=h*3600+m*60+s
            if '主片 ->' in rendered:
                return '主片渲染已经结束，DSH 还在处理开场、拼接或终检，我继续盯着这一轮结果。'
            percent=min(99,int(position/duration*100))
            return f'视频主片已经渲染到约 {percent}%，DSH 仍在后台执行，我还在继续跟进。'
        asr=current_log('asr.log')
        pieces=re.search(r'切了\s*(\d+)\s*片',asr)
        done=set(re.findall(r'分片\s*(\d+)\s*完成\s*rc=0',asr))
        if '完成：' in asr:
            return '转写已经结束，DSH 还在整理字幕、章节或后续产物，整轮任务仍在进行。'
        if pieces:
            return f'转写已完成 {len(done)}/{pieces[1]} 个分片，其余仍在后台处理，我会继续跟进。'
    summary=str(state.get('summary') or '')
    summary=re.sub(r'```.*?```','',summary,flags=re.S)
    lines=[re.sub(r'[*#`>]+','',line).strip() for line in summary.splitlines() if line.strip()]
    latest=(lines[0][:150] if lines else '').rstrip('。')
    if phase=='submission':
        return ('我还在准备 B站投稿草稿。'+('最近收到的进展是：'+latest+'。' if latest else 'DSH 仍在执行这一步。')+
                '稿件尚未提交。')
    return ('我还在盯着 DSH 执行这项任务。'+('最近收到的进展是：'+latest+'。' if latest else '暂时还没有收到新的执行答复。'))


class MinuteReporter:
    def __init__(self,config=None,now=None):
        self.config=config or dict(enabled=True,interval_seconds=60)
        self.last=time.monotonic() if now is None else now
        self.count=0

    def take(self,state,now=None):
        now=time.monotonic() if now is None else now
        if not self.config.get('enabled',True):return None
        if state.get('state') not in ('queued','running','disconnected'):return None
        if now-self.last<max(60,int(self.config.get('interval_seconds') or 60)):return None
        text=progress_sentence(state,self.config)
        self.last=now;self.count+=1
        return dict(text=text,at=time.time(),number=self.count,
                    conversation_id=state['conversation_id'],session_id=state['session_id'])


def record_notice(store,notice):
    """Write a visible local chat message, never send a new instruction to DSH."""
    with store.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS dsh_progress_notices ('
            'id INTEGER PRIMARY KEY, conversation_id TEXT, session_id TEXT, at REAL, text TEXT)')
        db.execute('INSERT INTO dsh_progress_notices(conversation_id,session_id,at,text) VALUES (?,?,?,?)',
            (notice['conversation_id'],notice['session_id'],notice['at'],notice['text']))
    store.append(notice['conversation_id'],'assistant',notice['text'])
