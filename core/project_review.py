"""Review a completed planning turn before issuing an implementation request."""
from dataclasses import dataclass
import re

from core.codex_gateway import task_facts


SECTIONS = (
    ('玩法', ('玩法', '机制')),
    ('关卡', ('关卡', '流程')),
    ('美术', ('美术', '视觉')),
    ('技术', ('技术', '选型')),
    ('排期', ('排期', '里程碑', '工期')),
)
SIDE_EFFECTS = frozenset(('fileChange', 'commandExecution', 'mcpToolCall',
                         'dynamicToolCall', 'collabAgentToolCall'))


@dataclass(frozen=True)
class ProjectPlan:
    conversation_id: str
    thread_id: str
    turn_id: str
    goal: str
    text: str
    issues: tuple


def is_design_only(text):
    return bool(not re.match(r'^\s*(?:咕嘎[，,、 ]*)?(?:不要|别|停止|取消)', text)
                and re.search(r'codex', text, re.I)
                and re.search(r'方案|设计思路|项目规划', text)
                and re.search(r'先只|先出|先给|只出|只要|不要动手|不要写代码|不写代码', text))


def plan_request(goal):
    return ('先只输出中文设计方案。保持计划模式，不写入文件、不调用工具、不运行命令。'
            '请按玩法、关卡、美术、技术和排期五部分给出方案，列明资源来源、依赖和风险，'
            '等我在咕嘎黑板批准后再实施。\n\n需求：' + goal.strip())


def inspect_plan(conversation_id, thread_id, goal, response):
    facts = task_facts(response)
    if facts['thread_id'] != thread_id or facts['state'] != 'completed':
        raise ValueError('请等待已绑定的 Codex 方案回合完成')
    turns = response.get('turns') or []
    items = turns[0].get('items', [])
    plan_items = [str(item.get('text') or '') for item in items if item.get('type') == 'plan']
    text = ('\n'.join(plan_items) or facts['text']).strip()
    if not text or not facts['turn_id']:
        raise ValueError('这个回合尚无可审核的方案正文')
    issues = []
    prompts = [str(piece.get('text') or '') for item in items if item.get('type') == 'userMessage'
               for piece in item.get('content', []) if isinstance(piece, dict)]
    if not any(plan_request(goal) in prompt for prompt in prompts):
        issues.append('当前回合没有这份需求的方案指令，请复制黑板上的需求后重新提交。')
    if any(item.get('type') in SIDE_EFFECTS for item in items):
        issues.append('方案回合包含命令、改文件或工具调用记录，需要重新提供纯方案。')
    if re.search(r'\x60{3}|<script\b', text, re.I):
        issues.append('方案包含代码块或脚本，请改用文字说明。')
    for heading, alternatives in SECTIONS:
        if not any(term in text for term in alternatives):
            issues.append(f'缺少「{heading}」部分。')
    return ProjectPlan(conversation_id, thread_id, str(facts['turn_id']), goal, text, tuple(issues))
