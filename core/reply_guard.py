"""聊天回复的诚实性硬规则（第六项）。

现场问题：任务一步失败后，回复里出现“已转交/正在生成/后台每 N 秒同步”，
或者明明本轮能操作电脑却说“我没有操作电脑的权限/纯问答模式”，
再或者让用户“手动点一下”。这些说法在本轮没有执行回执时都是假的。

规则：
  · 本轮具备执行能力时，禁止“没有操作电脑的权限/纯问答模式”；
  · 没有实际执行证据时，禁止“已转交/已提交/正在生成/后台每 N 秒同步”；
  · 任务处于可执行状态时，禁止“请你手动打开/复制粘贴/点一下”；
  · 没证据只能说“未执行/未确认”。
"""

#: 本轮具备执行能力时不许说的话。
BANNED_EXEC_CAPABILITY = (
    '我没有操作电脑的权限', '没有操作电脑的权限', '我没有电脑权限',
    '纯问答模式', '只能纯问答',
)
#: 没有实际执行证据时不许说的话（编造进度）。
BANNED_PHANTOM_EXECUTION = (
    '已转交', '已提交', '正在生成', '后台每', '每隔', '会自动同步',
)
#: 任务处于可执行状态时不许把动作推回给用户。
BANNED_ASK_USER_MANUAL = (
    '请你手动打开', '请你手动复制', '请你手动点', '请手动打开', '请手动复制',
    '手动复制粘贴', '请你点一下', '你自己点一下', '你手动操作',
    # 20261110z 现场：失败卡片里写“到豆包窗口点一下输入框手动发一次”，
    # 这同样是把手动操作推回用户，必须与聊天回复共用同一套闸。
    '请手动启动', '手动启动一次', '手动发一次', '手动发送一次',
    '点一下输入框', '请你切换', '请你自己', '复制粘贴',
    # 20261110ab 现场：input_verify_failed 的 hint 是“请点一下豆包输入框后重试”，
    # 中间夹了“豆包”三字，旧的“点一下输入框”匹配不到，卡片原样漏出。
    # 这里按动作补全，避免再靠某个固定词序才命中。
    '点一下豆包', '点一下回答', '点一下正文', '手动点开', '手动滚',
    '手动复制', '手动确认', '请把豆包切',
)

#: 没有证据时唯一允许的进度说法。
HONEST_UNKNOWN = '未执行/未确认'


def banned_hits(text, executable=True, has_evidence=False):
    """返回命中的禁用话术（按三类规则分别判定）。"""
    value = str(text or '')
    hits = []
    if executable:
        hits.extend(p for p in BANNED_EXEC_CAPABILITY if p in value)
    if not has_evidence:
        hits.extend(p for p in BANNED_PHANTOM_EXECUTION if p in value)
    if executable:
        hits.extend(p for p in BANNED_ASK_USER_MANUAL if p in value)
    return hits


def sanitize(text, executable=True, has_evidence=False):
    """剔除命中禁用话术的句子，并补一句如实的“未执行/未确认”。

    返回 ``(cleaned, hits)``；没有命中时原样返回，不改写正常聊天。
    """
    source = str(text or '')
    hits = banned_hits(source, executable=executable, has_evidence=has_evidence)
    if not hits:
        return source, []
    kept = []
    for sentence in source.replace('\r\n', '\n').splitlines():
        for piece in sentence.replace('！', '！\n').replace('？', '？\n').replace('。', '。\n').splitlines():
            if not piece.strip():
                continue
            if any(hit in piece for hit in hits):
                continue
            kept.append(piece)
    cleaned = '\n'.join(kept).strip()
    if has_evidence:
        note = '（涉及权限/手动操作的句子已按诚实性规则剔除：本轮具备本机执行能力。）'
    else:
        note = ('（涉及执行进度的句子已按诚实性规则剔除：%s，没有实际执行回执，'
                '不会编造进度。）' % HONEST_UNKNOWN)
    return (cleaned + '\n' + note).strip() if cleaned else note, hits


def sanitize_hint(text, executable=True):
    """失败卡片 / hint / 下一步建议的话术闸：与聊天回复共用禁用清单。

    只删命中句，不追加聊天式说明；整段被删空时给一句如实的“未确认”，
    绝不退回“请你自己点一下”这类把操作推回用户的句子。
    """
    source = str(text or '')
    hits = banned_hits(source, executable=executable, has_evidence=False)
    if not hits:
        return source, []
    kept = []
    for line in source.replace('\r\n', '\n').splitlines():
        for piece in line.replace('；', '；\n').replace('。', '。\n').splitlines():
            piece = piece.strip()
            if not piece or any(hit in piece for hit in hits):
                continue
            kept.append(piece)
    cleaned = '\n'.join(kept).strip()
    if not cleaned:
        cleaned = ('这一步没有拿到可执行回执（%s）；我没有把操作推回给你，'
                   '也不会编造结果。' % HONEST_UNKNOWN)
    return cleaned, hits


#: 用户消息里出现这些词，视为在问任务状态/进度。
STATUS_QUERY_HINTS = (
    '进度', '任务', '做到哪', '到哪一步', '状态', '失败', '失败码', '结果',
    '回执', '卡住', '卡了', '为什么没', '完成了吗', '有没有完成', '产物', '做完',
)


def is_status_query(text):
    """这句用户消息是否在问任务状态。"""
    value = str(text or '')
    return any(hint in value for hint in STATUS_QUERY_HINTS)


def ledger_disclosure(snapshot, user_msg, executable=True):
    """台账读不到且用户在问进度时，返回必须前置的如实说明；否则返回空串。"""
    if not isinstance(snapshot, dict) or snapshot.get('ok'):
        return ''
    if not executable or not is_status_query(user_msg):
        return ''
    return ('我现在读不到任务状态（本机任务台账缺失或不可读）。'
            '步骤、失败码、产物都未确认，%s；不会编造进度。' % HONEST_UNKNOWN)
