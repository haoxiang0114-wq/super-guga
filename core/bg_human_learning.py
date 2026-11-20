"""Human-decision case memory and version-gated ranking weights."""
import json,threading
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
CASES=ROOT/'data/bg_agent/human_learning_254477.json'
_LOCK=threading.Lock();_CACHE=None


def load(path=None):
    global _CACHE
    target=Path(path or CASES)
    with _LOCK:
        if _CACHE is None or _CACHE['path']!=target:
            if not target.is_file():return None
            if target.stat().st_size>20_000_000:return None
            _CACHE=dict(path=target,data=json.loads(target.read_text(encoding='utf-8')))
        return _CACHE['data']


def advisory(state,actions):
    data=load()
    build=state.get('client_build')
    if not data or not build or int(build)!=int(data.get('client_build',0)):
        return dict(available=False,reason='no_matching_live_build')
    turn=int(state.get('round') or 0);tier=int(state.get('tier') or 0)
    cases=[c for c in data.get('cases',[]) if abs(int(c.get('turn',0))-turn)<=2
           and (tier<=0 or abs(int(c.get('tier',0))-tier)<=1)]
    counts={}
    for case in cases:
        kind=str(case.get('selected_kind') or '')
        if kind:counts[kind]=counts.get(kind,0)+1
    total=sum(counts.values())
    if not total:return dict(available=False,reason='no_similar_cases')
    kinds=sorted(counts.items(),key=lambda item:-item[1])[:5]
    return dict(available=True,build=build,samples=total,
        preferred=[dict(kind=k,count=n,share=round(n/total,3)) for k,n in kinds],
        model_deployable=bool(data.get('metrics',{}).get('deployable')),
        limitation='真实实局中的人类选择统计，不等于最优策略或胜率')
