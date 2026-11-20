"""Separated fast strategy and recruit-only operation engines.

Strategy reads log/memory state and never takes a screenshot. Operation is the
only component allowed to capture the client, and only while the observed phase
is recruit. Memory state is read-only (HearthMirror); no memory write backend is
claimed here.
"""
import time


def _gold(state):
    res=state.get('resources') or {}
    return max(0,float(res.get('RESOURCES') or 0)-float(res.get('RESOURCES_USED') or 0)+float(res.get('TEMP_RESOURCES') or 0))


def _entity(state,action):
    eid=str(action.get('target') or action.get('entity_id'))
    return (state.get('entities') or {}).get(eid,{})


def _strength(card):
    return float(card.get('ATK') or 0)+float(card.get('HEALTH') or 0)


def fast_choice(state,actions,metadata=None):
    """Return a bounded local plan for obvious states; None means defer."""
    metadata=metadata or {}
    if not actions:return None
    gold=_gold(state)
    # 已知缺口（重构时未改行为，仅留痕）：这里原本会算一份 board/hand 快照，但算完从未被使用。
    # 下面 SELL 分支的注释写着「只有满场且需要腾位置时才考虑卖出」，代码却从不检查满场。
    # 更关键的是：SELL 的 score = -100 + (-strength/20)，恒 ≤ -100，而函数末尾
    # `if best[0]<=0 ... return None` 会把非正分全部丢弃——也就是说卖出策略整条不可达。
    # 要修就两件事：用 board/hand 真正判断「满场且需要腾位」，并给 SELL 一个能过门槛的正分。
    turn=float(state.get('round') or 0);tier=float(state.get('tier') or 1);scored=[]
    for action in actions:
        kind=action.get('kind');card=_entity(state,action);score=-999.;reason=''
        if kind=='PLAY':score=120+_strength(card)/10;reason='手牌可安全打出，优先把战力落到场面'
        elif kind=='BUY':score=80+_strength(card)/12-float(card.get('COST') or 3)*6;reason='商店随从性价比更高，先补即时战力'
        elif kind=='UPGRADE':
            cost=state.get('upgrade_cost')
            if cost is not None:
                expected=min(6,1+turn//2);score=(85 if tier<expected else -20)-float(cost)*3;reason='回合节奏支持升本，长期商店等级收益更高'
        elif kind=='REFRESH':score=(20 if gold>=4 else -30);reason='当前没有明确更好的动作，保留经济后刷新找牌'
        elif kind=='SELL':score=(-100+(_strength(card) and -_strength(card)/20));reason='只有满场且需要腾位置时才考虑卖出'
        elif kind=='FREEZE':score=5;reason='仅当商店价值明显时保留'
        else:score=-999
        scored.append((score,reason,action))
    scored.sort(key=lambda x:-x[0]);best,second=scored[0],scored[1] if len(scored)>1 else (-999,'',None)
    if best[0]<=0 or best[0]-second[0]<18:return None
    return dict(action=best[2],reason=best[1],confidence=round(min(.99,.65+(best[0]-second[0])/200),3),local=True)


class StrategyEngine:
    def __init__(self,config,metadata_loader=None):
        self.config=config
        self.metadata_loader=metadata_loader

    def plan(self,state):
        from core.bg_live import candidates,model_pick
        metadata=self.metadata_loader(state) if self.metadata_loader else {}
        actions=candidates(state)
        if not actions:raise RuntimeError('当前没有本版执行器支持的合法动作')
        mode=self.config.get('strategy_mode','fast')
        if mode=='deepbattler':
            from core.bg_deepbattler import model_pick as deepbattler_pick
            picked=deepbattler_pick(state,actions,metadata,self.config)
        elif mode!='model':
            picked=fast_choice(state,actions,metadata)
        else:picked=None
        if picked is None:picked=model_pick(state,actions,metadata,self.config)
        return dict(picked,state=state,metadata=metadata,actions=actions,
                    state_id=state.get('fingerprint'),phase_source='Power.log',
                    memory_state='read_only_optional',screen_used=False)

    def memory_snapshot(self):
        """Optional read-only cross-check; never used as a write channel."""
        from core.bg_memory import read_board
        return read_board()


class OperationEngine:
    def __init__(self,backend,config):
        self.backend=backend
        self.config=config
        self._fixed={}

    def execute(self,plan,reader,stop,started,seconds):
        from core.bg_live import acknowledged,compact_state,ground_action,observed_phase
        if plan.get('wait'):
            return dict(executed=False,verified=False,reason=plan.get('reason','策略等待'))
        state=plan['state'];action=plan['action']
        if observed_phase(reader.parser)!='recruit':
            raise RuntimeError('操作引擎只在招募阶段接管屏幕；当前阶段不发送输入')
        geometry=self.backend.geometry(self.backend.find())
        cache_key=None;grounding=None
        if action.get('kind') in ('REFRESH','FREEZE','UPGRADE'):
            cache_key=(action.get('kind'),tuple(geometry));grounding=self._fixed.get(cache_key)
        if grounding is None:
            image,geometry=self.backend.capture()
            entity=compact_state(state,plan.get('metadata') or {}).get('entities',{}).get(action.get('entity_id'))
            grounding=ground_action(image,state,dict(action=action,entity=entity,reason=plan.get('reason','')),self.config)
        reader.poll();current=reader.state()
        if stop.is_set() or self.backend.cancelled() or time.monotonic()-started>seconds:
            return dict(executed=False,verified=False,reason='任务在输入前停止')
        if current['fingerprint']!=state['fingerprint']:
            raise RuntimeError('策略阶段与操作阶段之间局面已变化；未发送旧动作')
        input_receipt=self.backend.execute(grounding,action,geometry)
        selected=[];verified=False;deadline=min(time.monotonic()+5,started+seconds+5)
        while time.monotonic()<deadline:
            time.sleep(.15);selected.extend(reader.poll())
            try:after=reader.state()
            except RuntimeError:break
            if acknowledged(state,after,action,selected):
                verified=True;break
        if verified and cache_key is not None:self._fixed[cache_key]=grounding
        return dict(executed=True,verified=verified,grounding=grounding,selected=selected,
                    vision_confidence=grounding['confidence'],input_receipt=input_receipt)


def backend_capabilities():
    return dict(strategy='Power.log + optional HearthMirror read-only board',
                operation='recruit_only_window_input',memory_read='HearthMirror read-only',
                memory_write='not available; requires verified offsets and is not claimed')
