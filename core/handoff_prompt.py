"""复合任务里「上一步正文 → 本步要求」的唯一组装点。

背景（20261110 s4 现场，evidence/adapter_chain 已留证）：
  第 1 步豆包交回 2230 字 PPT 提示词，第 2 步派给 WorkBuddy 时输入框里确实有 2392 字
  （粘贴与回读都正确），但旧组装方式把正文放在独立的“参考资料”块，末尾另起一段
  “本步要求：生成PPT（规则）”。WorkBuddy 的 PPT 流程只按最后一段取“要求”，
  于是 2230 字正文被整段丢掉，成品变成与上游无关的《本周工作复盘》。

约束（所有适配器共用，不得在适配器里另写一套）：
  1. 固定模板只能做附加约束，绝不能替换或覆盖上游正文；
  2. 上游正文缺失 / 过短时直接抛 HandoffError，由派单层中止并报独立失败码，
     不允许静默退化成「模板-only」发送；
  3. 组装完成后必须回读断言：最终 prompt 里要能找到正文的首 40 字与末 40 字。
"""

import re

#: 上游正文的最小长度：少于这个字数就视为「没交东西」，不允许继续派单。
MIN_HANDOFF_CHARS = 20

#: 回读断言取正文头部/尾部的长度。
VERIFY_FRAGMENT_CHARS = 40

_DEFAULT_ASK = '完成本步要求'

#: 投递形态约束：只做附加，不改写用户原句。20261110w 现场：豆包把 18 页正文
#: 放进“在线文档卡片”，对话里只剩摘要，取正文四档全落空。
PLAIN_TEXT_RULE = ('（投递形态：请直接在对话里用纯文本输出完整全文，'
                   '不要生成在线文档、卡片、图片或附件；'
                   '全文要能在对话气泡里直接选中复制。）')

_INVISIBLE = re.compile(r'[\ufeff\u200b\u200c\u200d\u00ad]')


def normalize(text):
    """比对用的归一化：去 BOM/零宽字符、统一换行、去行尾空白与首尾空白。"""
    value = _INVISIBLE.sub('', str(text or ''))
    value = value.replace('\r\n', '\n').replace('\r', '\n')
    return '\n'.join(line.rstrip() for line in value.split('\n')).strip()


class HandoffError(RuntimeError):
    """上游交接内容不可用，或组装结果回读断言失败。"""

    def __init__(self, code, missing='', detail=''):
        self.code = str(code)
        self.missing = str(missing or '上一步交付的正文')
        self.detail = str(detail or '')
        super().__init__('%s：%s' % (self.code, self.detail or self.missing))


def fragments(body, size=VERIFY_FRAGMENT_CHARS):
    """取正文的头部与尾部片段（正文不足 size 时用全文）。"""
    value = str(body or '').strip()
    if not value:
        raise HandoffError('handoff_missing', '上一步交付的正文', '上一步正文为空（0 字）')
    return value[:size], value[-size:]


def assert_embedded(prompt, body):
    """回读断言：组装后的 prompt 必须原样包含正文首尾片段，否则拒绝发送。"""
    text = str(prompt or '')
    head, tail = fragments(body)
    missing = []
    if head not in text:
        missing.append('开头 %d 字' % len(head))
    if tail not in text:
        missing.append('结尾 %d 字' % len(tail))
    if missing:
        raise HandoffError(
            'handoff_not_embedded',
            '上游正文的%s' % '与'.join(missing),
            '组装后的提示词里找不到上游正文的%s，已拒绝发送（不会退化成模板-only）'
            % '与'.join(missing))
    return True


def _body_of(previous):
    if not isinstance(previous, dict):
        raise HandoffError('handoff_missing', '上一步交付的正文',
                           '上一步没有交接结构（%s）' % type(previous).__name__)
    name = str(previous.get('name') or '上一步')
    raw = str(previous.get('text') or '')
    body = raw.strip()
    if not body:
        raise HandoffError('handoff_missing', '%s 交付的正文' % name,
                           '上一步正文为空（0 字）')
    if len(body) < MIN_HANDOFF_CHARS:
        raise HandoffError(
            'handoff_too_short',
            '%s 交付的正文（现有 %d 字，至少 %d 字）' % (name, len(body), MIN_HANDOFF_CHARS),
            '上一步正文只有 %d 字，少于最小长度 %d 字' % (len(body), MIN_HANDOFF_CHARS))
    return body, name


def compose(instruction, previous=None, wants_file=False, plain_text=False):
    """组装本步发送给目标软件的提示词。

    ``previous is None``：第一步，只有本步要求 + 固定约束；
    否则：本步要求 + 上一步完整正文 + 固定约束，正文处于同一条要求内部，
    末尾不再出现独立的“本步要求”段，避免下游只取最后一段而丢掉正文。

    ``plain_text=True`` 只追加投递形态约束（要在对话里以纯文本输出全文，
    不要在线文档/卡片/图片/附件），不修改 ``instruction`` 原文。
    """
    ask = str(instruction or '').strip() or _DEFAULT_ASK
    if previous is None:
        file_rule = ('\n（必须产出可下载的 .pptx 文件，并在回答里写清文件名；'
                     '只给文字大纲不算完成。）' if wants_file else '')
        plain_rule = PLAIN_TEXT_RULE if plain_text else ''
        return ask + file_rule + plain_rule + '\n（只要正文，不要反问，也不要执行与本步无关的操作。）'

    body, name = _body_of(previous)
    plain_rule = PLAIN_TEXT_RULE if plain_text else ''
    if wants_file:
        prompt = ('请严格按下面这份 PPT 提示词产出 PPT，以下全部内容都是本步的要求'
                  '（主题、页数、逐页要点、风格都按它来，不得自行换主题、不得省略页面）：\n'
                  '%s\n'
                  '输出：必须产出可下载的 .pptx 文件，并在回答里写清文件名；'
                  '只给文字大纲不算完成；不要反问，不要执行与本步无关的操作。%s'
                  % (body, plain_rule))
    else:
        prompt = ('本步要求：%s\n'
                  '以下是上一步（%s）交付的正文，本次必须基于这份正文完成，'
                  '不得自行换主题、不得省略内容：\n<<<\n%s\n>>>\n'
                  '（只要本步的最终正文，不要重复上面全文，不要反问，'
                  '也不要执行与本步无关的操作。）%s' % (ask, name, body, plain_rule))
    assert_embedded(prompt, body)
    return prompt
