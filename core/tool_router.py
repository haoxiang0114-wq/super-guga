"""本地优先的工具路由：默认只指派本机已核验的软件；明确的跨设备语义才走服务器。

判断依据是可解释的信号（能力匹配、启动核验结果、适配器等级、跨设备词），
不是写死某一句关键词。没有合适的本机工具时如实返回“本机没有”，
不允许静默降级成咕嘎自己生成——那等于绕过整个指派链路。
"""
from __future__ import annotations


from core import local_tools

# 能力词表：词是能力的证据，能力才是路由对象。
CAPABILITY_SIGNALS = {
    'website': ('网站', '网页', '单页', '官网', '落地页', 'h5', '前端页面', '产品页'),
    'code': ('代码', '程序', '脚本', '项目', '函数', '接口', '重构', '调试', '报错', '源码'),
    'slides': ('ppt', '演示文稿', '幻灯片', '汇报材料', '路演'),
    'document': ('文档', 'word', '报告', '合同', '简历', '方案', '说明书', '文案', '写稿'),
    'image': ('生图', '画一张', '海报', 'logo', '封面', '插画', '配图', '图片'),
    'video': ('视频', '剪辑', '字幕', '短视频', '成片', '配音'),
    'research': ('搜索', '调研', '竞品', '对比', '查一下', '检索'),
    'prompt_engineering': ('提示词', 'prompt', '扩写提示', '写一段提示'),
}
CAPABILITY_LABELS = {'website': '网站/页面', 'code': '代码', 'slides': '演示文稿',
                     'document': '文档/文案', 'image': '图片', 'video': '视频/剪辑',
                     'research': '检索调研', 'chat': '对话问答', 'prompt_engineering': '提示词'}
# 出现这些词说明要“先出提示词再生成”的复合链路，而不是单点问答。
PROMPT_CHAIN_SIGNALS = ('提示词', 'prompt', '再生成', '先写提示词', '交给', '高质量')

CROSS_DEVICE_SIGNALS = ('另一台', '另一台电脑', '其他电脑', '别的电脑', '跨设备', '远程电脑',
                        '第二台', '那台电脑', '另一台机器', '远程机器', '同事的电脑',
                        '公司的电脑', '家里那台', '办公室那台', '1号电脑', '2号电脑',
                        '一号电脑', '二号电脑', '另一台设备', '另一台机子')
LOCAL_SIGNALS = ('本机', '这台电脑', '这台机器', '当前电脑', '就在这', '在我电脑上')

# 适配器等级：数字越小越可靠。只有这里能自动指挥的才排前面。
AUTOMATION_RANK = {'bridge': 0, 'ide_dispatch': 1, 'ui_only': 2, 'launch_only': 3}


def detect_capabilities(text):
    value = str(text or '').casefold()
    return [name for name, words in CAPABILITY_SIGNALS.items()
            if any(word in value for word in words)]


def detect_cross_device(text):
    value = str(text or '')
    hits = [word for word in CROSS_DEVICE_SIGNALS if word in value]
    if any(word in value for word in LOCAL_SIGNALS):
        # “本机没有的话再用另一台”仍然是本机优先，不算明确的跨设备指派。
        hits = [hit for hit in hits if '再用' not in value and '如果没有' not in value]
    return hits


def _labels(capabilities):
    return '、'.join(CAPABILITY_LABELS.get(item, item) for item in capabilities) or '该需求'


def _score(tool, capabilities):
    matched = [cap for cap in capabilities if cap in (tool.get('capabilities') or [])]
    rank = AUTOMATION_RANK.get(str(tool.get('automation')), 9)
    return len(matched) * 10 - rank, matched


def route(requirement, inventory=None):
    """返回可检查的路由决策，不执行任何操作。"""
    text = str(requirement or '').strip()
    capabilities = detect_capabilities(text)
    cross_hits = detect_cross_device(text)
    tools = inventory if inventory is not None else local_tools.inventory()
    usable = [t for t in tools if t.get('launch_ok') is not False and t.get('exists')]
    blocked = [t for t in tools if t.get('launch_ok') is False]
    evidence = dict(capabilities=capabilities, cross_device_hits=cross_hits,
                    inventory_size=len(tools), usable=len(usable),
                    unverified_rejected=[t.get('name') for t in blocked])

    if cross_hits:
        return dict(scope='cross_device', capabilities=capabilities, chain=[], primary=None,
                    server_lookup=True, signals=cross_hits, evidence=evidence,
                    block_self_fallback=True,
                    reason='用户明确要求跨设备（%s）；走服务器查同账号的另一台机器上的工具'
                           % '、'.join(cross_hits),
                    message='已判定为跨设备任务：本机优先不适用，需要通过服务器查询另一台电脑上'
                            '具备「%s」能力的软件。' % _labels(capabilities))

    if not capabilities:
        return dict(scope='local', capabilities=[], chain=[], primary=None, signals=[], evidence=evidence,
                    block_self_fallback=True,
                    reason='没有识别到需要外部软件能力的需求；按普通本机任务处理，不指派软件',
                    message='这不像需要外部 AI 软件的任务。')

    scored = []
    for tool in usable:
        score, matched = _score(tool, capabilities)
        if matched:
            scored.append((score, bool(tool.get('commandable')), tool, matched))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if not scored:
        return dict(scope='unavailable', capabilities=capabilities, chain=[], primary=None,
                    signals=[], evidence=evidence, block_self_fallback=True,
                    reason='本机清单里没有具备「%s」能力的已安装软件' % _labels(capabilities),
                    message=('本机没有能完成「%s」的软件（已检查并核验：%s）。要不要改用另一台电脑？'
                             '那需要同一账号并连上服务器。'
                             % (_labels(capabilities),
                                '、'.join(t.get('name') for t in tools) or '无')))

    ranked = [dict(id=item[2]['id'], name=item[2]['name'],
                   capabilities=item[3], automation=item[2].get('automation'),
                   commandable=item[1], score=item[0]) for item in scored]
    chain = []
    text_cf = text.casefold()
    aliases = {spec['id']: tuple(spec.get('aliases') or ()) for spec in local_tools.APPS}

    def explicitly_named(tool):
        names = (tool['name'],) + aliases.get(tool['id'], ())
        return any(str(name).casefold() in text_cf for name in names)

    producers = [t for t in ranked if any(c in t['capabilities'] for c in ('slides', 'website', 'document'))]
    prompters = [t for t in ranked if 'prompt_engineering' in t['capabilities']]
    named_prompt = next((t for t in prompters if explicitly_named(t)), None)
    named_producer = next((t for t in producers if explicitly_named(t)), None)
    wants_prompt_chain = ('prompt_engineering' in capabilities
                          or any(word in text for word in PROMPT_CHAIN_SIGNALS))
    workbuddy = next((t for t in ranked if t['id'] == 'workbuddy'), None)
    code_tool = next((t for t in ranked if t['id'] in ('joycode', 'trae')), None)
    if ('website' in capabilities and workbuddy and code_tool
            and not (named_producer and named_producer['id'] != 'workbuddy')):
        # 网站/页面类：有 WorkBuddy 就先出提示词，再交给代码生成工具。
        chain = [workbuddy['id'], code_tool['id']]
    elif named_producer and named_prompt and named_prompt['id'] != named_producer['id']:
        chain = [named_prompt['id'], named_producer['id']]
    elif wants_prompt_chain and prompters and producers:
        target = named_producer or workbuddy or producers[0]
        helper = (named_prompt if (named_prompt and named_prompt['id'] != target['id'])
                  else next((t for t in prompters if t['id'] != target['id']), None))
        chain = ([helper['id'], target['id']] if helper else [target['id']])
    elif ranked[0]['id'] == 'workbuddy' and code_tool and 'website' in capabilities:
        chain = [ranked[0]['id'], code_tool['id']]
    else:
        chain = [ranked[0]['id']]
    # 同一个软件既会扩写提示词又能直接产出时，不要排成“自己 → 自己”两跳。
    deduped = []
    for key in chain:
        if key not in deduped:
            deduped.append(key)
    chain = deduped

    primary = chain[0]
    names = ' → '.join(next(t['name'] for t in ranked if t['id'] == key) for key in chain)
    reason = ('本机已核验可用的%s能覆盖该需求（能力：%s）；按本地优先指派 %s'
              % ('软件', '、'.join(capabilities), names))
    if chain and len(chain) > 1:
        second = next(t for t in ranked if t['id'] == chain[1])
        if 'slides' in second['capabilities']:
            reason += '；先用提示词工具把需求扩写，再交给能直接产出 .pptx 的软件'
        elif 'website' in second['capabilities'] or 'code' in second['capabilities']:
            reason += '；先用提示词工具把需求扩写，再交给代码生成工具，产出质量更高'
        else:
            reason += '；先由提示词工具扩写，再交给具备产物能力的软件'
    elif len(chain) == 1:
        only = next(t for t in ranked if t['id'] == chain[0])
        if 'prompt_engineering' in only['capabilities'] and 'slides' in only['capabilities']:
            reason += '；由它先把需求扩写成提示词，再直接产出 .pptx（不需要用户手动打开 PowerPoint）'
    return dict(scope='local', capabilities=capabilities, chain=chain, primary=primary,
                ranked=ranked, signals=[], evidence=evidence, block_self_fallback=True,
                reason=reason, message='指派本机：' + names)


# 本机没装、但另一台设备常见且正是该能力载体的软件：跨设备派单时用作 requiredTools 提示。
REMOTE_HINTS = {'video': ('剪映',), 'slides': ('WPS',), 'document': ('WPS',), 'image': ('豆包',)}


def required_tools(requirement, limit=10):
    """跨设备派单时要求目标设备必须具备的软件名（按能力推，最多 2 个更稳）。

    名字用软件显示名（与服务端清单的 name 对齐），便于服务端做 TOOL_MISSING 校验。
    """
    capabilities = detect_capabilities(requirement)
    if not capabilities:
        return []
    scored = []
    for spec in local_tools.APPS:
        matched = [cap for cap in capabilities if cap in spec['capabilities']]
        if matched:
            scored.append((len(matched), -AUTOMATION_RANK.get(spec['automation'], 9),
                           spec['name']))
    scored.sort(reverse=True)
    names = [name for _, _, name in scored[:2]]
    if not names:
        names = [REMOTE_HINTS[cap][0] for cap in capabilities if cap in REMOTE_HINTS][:2]
    return names[:limit]


def explain(decision):
    """把决策转成一句用户能听懂的话（给模型或直接播报）。"""
    decision = dict(decision or {})
    scope = decision.get('scope')
    if scope == 'local':
        return '走本机：' + str(decision.get('message') or '').replace('指派本机：', '')
    if scope == 'cross_device':
        return '走跨设备：' + str(decision.get('message') or '')
    if scope == 'unavailable':
        return '本机没有可用工具：' + str(decision.get('message') or '')
    return str(decision.get('message') or '')


# 把产物动作推回给用户的“逃逸话术”。只有本机确实没有可用工具时才可以这么说。
ESCAPE_PHRASES = ('必须由你', '需要你亲自', '你得自己', '你自己打开', '你自己点', '需要你手动',
                  '你手动操作', '由你打开', '请你打开', '需要你点击', '你来操作', '只能你操作')
# 这些是真正需要人来做的一步（授权/扣费/登录），允许在回答里提出，不算逃逸。
HUMAN_STEP_WORDS = ('授权', '确认', '权限', '积分', '扣费', '支付', '登录', '验证码', '扫码', '弹窗')


def escape_verdict(text, decision):
    """回答把产物动作推回给人时给出拦截理由；确实无工具或只是请人确认则放行。"""
    body = str(text or '')
    hits = [phrase for phrase in ESCAPE_PHRASES if phrase in body]
    if not hits:
        return None
    if any(word in body for word in HUMAN_STEP_WORDS):
        # 例如“WorkBuddy 弹窗要你确认积分再继续”：这是人必须做的一步，不是逃逸。
        return None
    decision = dict(decision or {})
    if decision.get('scope') != 'local':
        return None
    chain = list(decision.get('chain') or [])
    if not chain:
        return None
    names = ' → '.join(str(t.get('name')) for t in (decision.get('ranked') or [])
                       if t.get('id') in chain)
    return dict(phrases=hits, tools=names or '、'.join(chain),
                reason='本机已有可自动指挥的工具（%s），却把产物动作推回给用户' % (names or chain))
