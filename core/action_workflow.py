"""可执行桌面工作流：录制一次 → 落盘 → 带逐步核验地飞快回放。

设计目标（对应用户要求）：
- 一段很长的自然语言操作指令，编译成 ``runtime/workflows/<slug>.json`` 里的可执行步骤；
- 下次同一句话命中触发短语，直接按步骤回放，不再让模型重新想一遍；
- 每一步都必须有可读回的核验（selector + expect），核验不过就停，绝不盲点、绝不猜。

模块分层：
- 纯函数层：工作流/步骤/选择器的规范化与校验，选择器多路回退解析（可单测，无窗口依赖）；
- 存储层：WorkflowStore（runtime/workflows/*.json，原子写入）；
- 执行层：RealDesktop（真机适配器，内部复用 core.computer_control.ComputerControl 与
  core.uia_service 的 UIA 提供者）+ WorkflowRunner（逐步执行 + 核验 + 计时）；
- 回放层：Player（play/dry_run，统计 success_count/last_ok_at）；
- 录制层：Recorder（start/observe/capture/finish，逐动作落盘草稿）；
- 编译层：compile_from_text（**一次**模型调用把长指令分解为 steps；模型不可用就明确失败）；
- 接线层：模块级 start_recording / record_computer_action / finish_recording / find_workflow /
  play_workflow，供 core/self_agent.py 的 computer 工具在成功执行后调用（接线由 Lead 做）。

安全边界：
- 危险控件（发送/删除/支付/确认/卸载/关机/窗口关闭……）在保存、编译、回放三处都会被拒绝；
- ``type`` 只允许写进 Edit/ComboBox；坐标类动作没有稳定选择器时拒绝录制；
- 选择器命中多个控件时抛 SelectorAmbiguous 并停在本步，不动作；
- 每一步动作后重新快照读回目标/状态，核验失败即停并报告卡在第几步、为什么。
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
DEFAULT_DIR = ROOT / 'runtime/workflows'

# 步骤动作：act=UIA 模式动作，click=解析到控件后的几何点击（仍先精确解析），
# key=组合键，type=写入输入框，scroll=UIA 滚动，wait=等待状态，verify=只读核验，
# focus=聚焦窗口，open_app=确保应用窗口已打开并聚焦（不默认启动新进程）。
OPS = ('focus', 'open_app', 'act', 'click', 'key', 'type', 'scroll', 'wait', 'verify')
SELECTOR_KEYS = ('automation_id', 'name', 'name_contains', 'control_type', 'class_name', 'ordinal')
EXPECT_KINDS = ('exists', 'absent', 'name_equals', 'name_contains', 'value_equals',
                'value_contains', 'enabled', 'disabled', 'focused', 'foreground',
                'window_title_contains')
RECIPE_KINDS = ('window_title_contains', 'element_exists', 'element_name_contains',
                'element_value_contains')

# 任何一步都不允许点/调用的危险控件（按可读名称子串匹配）。
DANGEROUS_NAME_PARTS = ('发送', '提交', '发布', '删除', '卸载', '支付', '付款', '购买',
                        '确认', '确定', '格式化', '关机', '重启', '注销', '退出登录',
                        '永久删除', '清空回收站', '重置系统')
# 窗口级别的“关闭”按钮单独判定（自己开的标签页用 ctrl+w 收尾，不点系统关闭按钮）。
DANGEROUS_CLASS_PARTS = ('CaptionButton',)
DANGEROUS_KEY_COMBOS = ('alt+f4', 'ctrl+alt+delete', 'ctrl+shift+delete', 'win+r', 'win+x')


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #
class WorkflowError(RuntimeError):
    """工作流格式、录制或回放失败；消息面向用户，说明卡在哪里。"""


class SelectorError(WorkflowError):
    """选择器层面的错误。"""


class SelectorNotFound(SelectorError):
    pass


class SelectorAmbiguous(SelectorError):
    pass


class StepFailed(WorkflowError):
    def __init__(self, message, *, step_index=None):
        super().__init__(message)
        self.step_index = step_index


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _text(value, limit=1000):
    return ' '.join(str(value if value is not None else '').split())[:limit]


def _now_local():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def _now_epoch():
    return round(time.time(), 3)


def _atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def _read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _int_in(value, low, high, default=None, label='数值'):
    try:
        number = int(value)
    except (TypeError, ValueError):
        if default is not None:
            return int(default)
        raise WorkflowError('%s 需要整数' % label) from None
    if not low <= number <= high:
        raise WorkflowError('%s 需要在 %d—%d 之间' % (label, low, high))
    return number


def _slugify(name, fallback='workflow'):
    value = _text(name, 120)
    ascii_part = re.sub(r'[^a-z0-9._-]+', '-', value.casefold()).strip('-._')
    if ascii_part:
        return ascii_part[:64]
    return fallback + '-' + format(abs(hash(value)) & 0xFFFFFFFF, '08x')[:8]


def _normalize_keys(keys):
    return str(keys or '').casefold().replace(' ', '')


# --------------------------------------------------------------------------- #
# 选择器：规范化 + 多路回退解析（纯函数，独立可测）
# --------------------------------------------------------------------------- #
def normalize_selector(raw, *, required=False):
    if raw is None:
        if required:
            raise WorkflowError('该步骤必须有 selector（自动化ID/名称/控件类型至少其一）')
        return None
    if not isinstance(raw, dict) or not raw:
        raise WorkflowError('selector 需要是非空对象')
    unknown = [key for key in raw if key not in SELECTOR_KEYS]
    if unknown:
        raise WorkflowError('selector 不认识的字段：%s；只支持 %s'
                            % (', '.join(unknown), '/'.join(SELECTOR_KEYS)))
    selector = {}
    for key in SELECTOR_KEYS:
        if key not in raw or raw[key] is None:
            continue
        value = raw[key]
        if key == 'ordinal':
            selector['ordinal'] = _int_in(value, 0, 99, label='selector.ordinal')
        else:
            token = _text(value, 200)
            if token:
                selector[key] = token
    if 'name' in selector and 'name_contains' in selector:
        raise WorkflowError('selector 里 name 与 name_contains 只能用一个')
    if not any(key in selector for key in ('automation_id', 'name', 'name_contains',
                                           'control_type', 'class_name')):
        raise WorkflowError('selector 不能只有 ordinal，需要至少一个定位字段')
    return selector


def selector_tiers(selector):
    """把选择器展开成“从最具体到最宽松”的回退梯队。

    自动化 ID 优先、名称次之、控件类型+序号兜底；名称精确匹配又优先于包含匹配。
    """
    selector = selector or {}
    aid = selector.get('automation_id')
    name = selector.get('name')
    part = selector.get('name_contains')
    ctype = selector.get('control_type')
    cls = selector.get('class_name')
    tiers = []

    def add(tier):
        tier = {key: value for key, value in tier.items() if value}
        if tier and tier not in tiers:
            tiers.append(tier)

    if aid:
        add({'automation_id': aid, 'control_type': ctype, 'class_name': cls})
        add({'automation_id': aid, 'control_type': ctype})
        add({'automation_id': aid})
    if name:
        add({'name': name, 'control_type': ctype, 'class_name': cls})
        add({'name': name, 'control_type': ctype})
        add({'name': name, 'class_name': cls})
        add({'name': name})
    if part:
        add({'name_contains': part, 'control_type': ctype, 'class_name': cls})
        add({'name_contains': part, 'control_type': ctype})
        add({'name_contains': part})
    if cls and ctype:
        add({'class_name': cls, 'control_type': ctype})
    # 只有明确给了序号才允许“仅控件类型”兜底，否则同名控件谁都不选。
    if ctype and selector.get('ordinal') is not None:
        add({'control_type': ctype})
    return tiers


def _matches_tier(element, tier):
    for key, value in tier.items():
        actual = element.get(key)
        if key == 'name':
            if str(element.get('name') or '') != value:
                return False
        elif key == 'name_contains':
            if value.casefold() not in str(element.get('name') or '').casefold():
                return False
        elif key == 'automation_id':
            if str(element.get('id') or '') != value:
                return False
        elif key == 'control_type':
            if str(element.get('type') or '').casefold() != value.casefold():
                return False
        elif key == 'class_name':
            if value.casefold() not in str(element.get('class_name') or '').casefold():
                return False
        else:  # pragma: no cover - 防御
            if str(actual) != str(value):
                return False
    return True


def _element_order(element):
    rect = element.get('rect') or {}
    try:
        top, left = float(rect.get('top', 0)), float(rect.get('left', 0))
    except (TypeError, ValueError):
        return (0, 0)
    if top != top or left != left or top in (float('inf'), float('-inf')) \
            or left in (float('inf'), float('-inf')):
        return (0, 0)
    return (round(top), round(left))


def resolve_selector(elements, selector, *, skip_offscreen=True):
    """按梯队解析出唯一元素；返回 (element, tier)。

    - 多个命中且选择器没给 ordinal → SelectorAmbiguous（调用方必须停，不许猜）；
    - 给了 ordinal → 按屏幕位置排序后取第 ordinal 个；
    - 全部梯队都没有 → SelectorNotFound。
    """
    selector = normalize_selector(selector, required=True)
    pool = [element for element in (elements or [])
            if isinstance(element, dict) and (not skip_offscreen or not element.get('offscreen'))]
    ordinal = selector.get('ordinal')
    for tier in selector_tiers(selector):
        matches = [element for element in pool if _matches_tier(element, tier)]
        if not matches:
            continue
        if ordinal is not None:
            matches = sorted(matches, key=_element_order)
            if ordinal >= len(matches):
                raise SelectorNotFound('选择器序号 %d 超出命中数 %d，未执行动作'
                                       % (ordinal, len(matches)))
            return matches[ordinal], tier
        if len(matches) > 1:
            names = '、'.join(_text(match.get('name'), 30) or '(无名)' for match in matches[:5])
            raise SelectorAmbiguous('选择器命中 %d 个控件（%s），不唯一，未执行动作；'
                                    '请补充 automation_id/控件类型或 ordinal'
                                    % (len(matches), names))
        return matches[0], tier
    raise SelectorNotFound('选择器没有命中任何可见控件，未执行动作：%s'
                           % json.dumps(selector, ensure_ascii=False))


# --------------------------------------------------------------------------- #
# 工作流 / 步骤校验
# --------------------------------------------------------------------------- #
def normalize_app_hint(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict) or not raw:
        raise WorkflowError('app_hint 需要是非空对象，例如 {"query": "豆包浏览器"}')
    hint = {}
    for key in ('query', 'process', 'window_class', 'title_contains'):
        token = _text(raw.get(key), 120)
        if token:
            hint[key] = token
    unknown = [key for key in raw if key not in hint and key not in
               ('query', 'process', 'window_class', 'title_contains')]
    if unknown:
        raise WorkflowError('app_hint 不认识的字段：%s' % ', '.join(unknown))
    if not hint:
        raise WorkflowError('app_hint 至少需要 query/process/window_class/title_contains 之一')
    return hint


def _reject_dangerous(step):
    """危险控件/危险快捷键在保存、编译和回放时都拒绝。"""
    selector = step.get('selector') or {}
    name = _text(selector.get('name') or selector.get('name_contains'), 200)
    for part in DANGEROUS_NAME_PARTS:
        if part and part in name:
            raise WorkflowError('拒绝执行危险控件“%s”（命中禁止词“%s”）；工作流不允许发送/删除/'
                                '支付/确认/卸载/关机类操作' % (name, part))
    class_name = _text(selector.get('class_name'), 200)
    if 'name' in selector and name in ('关闭', 'Close') and \
            any(part.casefold() in class_name.casefold() for part in DANGEROUS_CLASS_PARTS):
        raise WorkflowError('拒绝点击窗口关闭按钮；回放只允许关闭自己打开的子窗口/标签页')
    keys = _normalize_keys(step.get('keys') or (step.get('value') if step.get('op') == 'key' else ''))
    if keys:
        for combo in DANGEROUS_KEY_COMBOS:
            if keys == combo or keys.startswith(combo + '+'):
                raise WorkflowError('拒绝执行危险快捷键 %s' % keys)


def normalize_expect(raw, step=None):
    if raw is None:
        raise WorkflowError('每个步骤都必须有 expect（动作后读回核验），不允许无核验的盲操作')
    if isinstance(raw, str):
        raw = {'kind': raw}
    if not isinstance(raw, dict):
        raise WorkflowError('expect 需要是对象，例如 {"kind": "exists"}')
    kind = _text(raw.get('kind'), 40)
    if kind not in EXPECT_KINDS:
        raise WorkflowError('未知核验方式 %r；支持 %s' % (kind, '/'.join(EXPECT_KINDS)))
    expect = {'kind': kind}
    if raw.get('selector') is not None:
        expect['selector'] = normalize_selector(raw['selector'], required=True)
    if 'value' in raw:
        expect['value'] = _text(raw['value'], 4000)
    if kind in ('name_equals', 'name_contains', 'value_equals', 'value_contains',
                'window_title_contains') and 'value' not in expect:
        raise WorkflowError('核验方式 %s 需要 value' % kind)
    if 'timeout_ms' in raw:
        expect['timeout_ms'] = _int_in(raw['timeout_ms'], 50, 30000, label='expect.timeout_ms')
    return expect


_SELECTOR_EXPECT_KINDS = ('exists', 'absent', 'name_equals', 'name_contains', 'value_equals',
                          'value_contains', 'enabled', 'disabled', 'focused')


def normalize_step(raw, index):
    if not isinstance(raw, dict):
        raise WorkflowError('第 %d 步不是对象' % index)
    allowed = ('id', 'op', 'title', 'selector', 'operation', 'keys', 'value', 'times',
               'state', 'expect', 'timeout_ms', 'window', 'launch')
    unknown = [key for key in raw if key not in allowed]
    if unknown:
        raise WorkflowError('第 %d 步有不认识的字段：%s' % (index, ', '.join(unknown)))
    op = _text(raw.get('op'), 40)
    if op not in OPS:
        raise WorkflowError('第 %d 步未知动作 %r；支持 %s' % (index, op, '/'.join(OPS)))
    step = {'id': index, 'op': op, 'title': _text(raw.get('title'), 120) or (op + ' 第%d步' % index)}
    expect = normalize_expect(raw.get('expect'), step)
    step['selector'] = normalize_selector(raw.get('selector'),
                                          required=_step_needs_selector(op, expect))
    step.update(_step_payload_fields(raw, index, op))
    if raw.get('window') is not None:
        step['window'] = normalize_app_hint(raw['window'])
    step['timeout_ms'] = _int_in(raw.get('timeout_ms'), 100, 30000, default=5000,
                                 label='第 %d 步 timeout_ms' % index)
    step['expect'] = expect
    _require_step_expect_selector(step, index)
    _reject_dangerous(step)
    return step


def _step_needs_selector(op, expect):
    """Whether the action itself (or a selector-based verification) needs a target."""
    return op in ('act', 'click', 'type', 'scroll') or (
        op in ('wait', 'verify') and expect['kind'] in _SELECTOR_EXPECT_KINDS
        and expect.get('selector') is None)


def _step_payload_fields(raw, index, op):
    """Per-op fields: UIA operation, text/times/keys/state, and open_app launch."""
    fields = {}
    operation = _text(raw.get('operation'), 40)
    if op in ('act', 'click'):
        allowed_ops = ('invoke', 'set_value', 'focus', 'click', 'toggle', 'expand',
                       'collapse', 'select', 'scroll')
        if operation and operation not in allowed_ops:
            raise WorkflowError('第 %d 步不支持 UIA 操作 %r' % (index, operation))
        if operation:
            fields['operation'] = operation
    if op == 'type':
        value = raw.get('value')
        if not isinstance(value, str) or not value:
            raise WorkflowError('第 %d 步 type 需要 1—4000 字的 value' % index)
        if len(value) > 4000:
            raise WorkflowError('第 %d 步 type 的 value 超过 4000 字' % index)
        fields['value'] = value
    elif op == 'scroll':
        fields['times'] = _int_in(raw.get('times', raw.get('value', 1)), 1, 20, default=1,
                                  label='第 %d 步 scroll.times' % index)
    elif op == 'key':
        keys = _text(raw.get('keys') or raw.get('value'), 80)
        if not keys:
            raise WorkflowError('第 %d 步 key 需要 keys' % index)
        fields['keys'] = keys
    elif op in ('act',) and isinstance(raw.get('value'), str):
        fields['value'] = _text(raw.get('value'), 4000)
    elif op == 'wait':
        state = _text(raw.get('state'), 20) or 'exists'
        if state not in ('exists', 'absent'):
            raise WorkflowError('第 %d 步 wait.state 只支持 exists/absent' % index)
        fields['state'] = state
    if op == 'open_app' and raw.get('launch'):
        fields['launch'] = True
    return fields


def _require_step_expect_selector(step, index):
    if step['expect']['kind'] in _SELECTOR_EXPECT_KINDS \
            and step['expect'].get('selector') is None and step.get('selector') is None:
        raise WorkflowError('第 %d 步的核验需要目标 selector（步骤或 expect 里给一个）' % index)


def normalize_workflow(raw, *, allow_empty=False):
    if not isinstance(raw, dict):
        raise WorkflowError('工作流文件需要是对象')
    schema = _int_in(raw.get('schema', SCHEMA_VERSION), 1, SCHEMA_VERSION,
                     label='schema') if raw.get('schema') is not None else SCHEMA_VERSION
    if schema != SCHEMA_VERSION:
        raise WorkflowError('工作流 schema 版本不支持：%s' % schema)
    name = _text(raw.get('name'), 120)
    if not name:
        raise WorkflowError('工作流需要 name')
    slug = _text(raw.get('slug'), 80) or _slugify(name)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', slug):
        raise WorkflowError('slug 只允许字母数字和 . _ -，且以字母数字开头')
    phrases = []
    for phrase in raw.get('trigger_phrases') or []:
        token = _text(phrase, 80)
        if token and token not in phrases:
            phrases.append(token)
    if not phrases:
        phrases = [name[:80]]
    steps_raw = raw.get('steps')
    if not isinstance(steps_raw, list):
        raise WorkflowError('工作流需要 steps 数组')
    if not steps_raw and not allow_empty:
        raise WorkflowError('工作流至少需要一个步骤')
    if len(steps_raw) > 40:
        raise WorkflowError('单个工作流最多 40 步')
    steps = [normalize_step(step, index) for index, step in enumerate(steps_raw, 1)]
    recipe = None
    if raw.get('verify_recipe') is not None:
        recipe = normalize_recipe(raw['verify_recipe'])
    workflow = {
        'schema': SCHEMA_VERSION,
        'name': name,
        'slug': slug,
        'version': _int_in(raw.get('version', 1), 1, 999, default=1, label='version'),
        'description': _text(raw.get('description'), 500),
        'trigger_phrases': phrases[:8],
        'app_hint': normalize_app_hint(raw.get('app_hint')),
        'steps': steps,
        'verify_recipe': recipe,
        'success_count': _int_in(raw.get('success_count', 0), 0, 10 ** 9, default=0),
        'failure_count': _int_in(raw.get('failure_count', 0), 0, 10 ** 9, default=0),
        'last_ok_at': raw.get('last_ok_at') or None,
        'last_error': _text(raw.get('last_error'), 500),
        'last_run': raw.get('last_run') if isinstance(raw.get('last_run'), dict) else None,
        'baseline': raw.get('baseline') if isinstance(raw.get('baseline'), dict) else None,
        'created_at': raw.get('created_at') or _now_local(),
        'updated_at': _now_local(),
        'source': _text(raw.get('source'), 40) or 'manual',
    }
    unknown = [key for key in raw if key not in workflow and key != 'stats']
    if unknown:
        raise WorkflowError('工作流有不认识的字段：%s' % ', '.join(unknown))
    return workflow


def normalize_recipe(raw):
    if not isinstance(raw, dict) or not raw:
        raise WorkflowError('verify_recipe 需要是非空对象')
    kind = _text(raw.get('kind'), 40)
    if kind not in RECIPE_KINDS:
        raise WorkflowError('未知 verify_recipe.kind %r；支持 %s'
                            % (kind, '/'.join(RECIPE_KINDS)))
    recipe = {'kind': kind}
    if kind == 'window_title_contains':
        recipe['value'] = _text(raw.get('value'), 200)
        if not recipe['value']:
            raise WorkflowError('window_title_contains 需要 value')
    else:
        recipe['selector'] = normalize_selector(raw.get('selector'), required=True)
        if kind in ('element_name_contains', 'element_value_contains'):
            recipe['value'] = _text(raw.get('value'), 200)
            if not recipe['value']:
                raise WorkflowError('%s 需要 value' % kind)
    rule = dict(recipe)
    if rule.get('selector'):
        _reject_dangerous({'op': 'verify', 'selector': dict(rule['selector'])})
    return recipe


def validate_workflow(raw, *, allow_empty=False):
    return normalize_workflow(raw, allow_empty=allow_empty)


# --------------------------------------------------------------------------- #
# 存储
# --------------------------------------------------------------------------- #
class WorkflowStore:
    def __init__(self, root=None):
        self.root = Path(root or DEFAULT_DIR)

    def path(self, slug):
        slug = _text(slug, 80)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', slug):
            raise WorkflowError('非法工作流 slug：%r' % slug)
        return self.root / (slug + '.json')

    def draft_path(self, slug):
        return self.root / 'drafts' / (slug + '.json')

    def save(self, workflow):
        workflow = validate_workflow(workflow)
        _atomic_json(self.path(workflow['slug']), workflow)
        return workflow

    def load(self, slug):
        path = self.path(slug)
        if not path.is_file():
            raise WorkflowError('没有找到工作流 %r（%s）' % (slug, path))
        try:
            return validate_workflow(_read_json(path))
        except (OSError, ValueError) as exc:
            raise WorkflowError('工作流文件读不出来：%s' % exc) from None

    def list(self):
        if not self.root.is_dir():
            return []
        workflows = []
        for path in sorted(self.root.glob('*.json')):
            try:
                workflows.append(validate_workflow(_read_json(path)))
            except (OSError, ValueError, WorkflowError):
                continue
        return workflows

    def find_by_phrase(self, text):
        value = _text(text, 4000)
        if not value:
            return []
        matches = []
        for workflow in self.list():
            for phrase in workflow.get('trigger_phrases') or []:
                if phrase and phrase in value:
                    matches.append((len(phrase), workflow))
                    break
        matches.sort(key=lambda pair: pair[0], reverse=True)
        return [workflow for _, workflow in matches]

    def save_draft(self, draft):
        slug = _text(draft.get('slug'), 80)
        _atomic_json(self.draft_path(slug), draft)

    def discard_draft(self, slug):
        try:
            self.draft_path(slug).unlink()
        except (OSError, FileNotFoundError):
            pass


# --------------------------------------------------------------------------- #
# 真机适配器：完全复用 computer 工具的同一套执行器
# --------------------------------------------------------------------------- #
class RealDesktop:
    """真机桌面适配器。动作与 ``mcp__guga__computer`` 共用 ComputerControl/UIA 提供者。"""

    def __init__(self, access=None):
        from core.computer_control import ComputerControl
        self.control = ComputerControl(access)
        self._uia = None

    # -- 只读 ------------------------------------------------------------- #
    def _backend(self):
        if self._uia is None:
            from core.uia_service import shared_service
            self._uia = shared_service()
        return self._uia

    def list_windows(self, query=''):
        from core.window_inventory import list_windows
        return list_windows(query)

    def foreground(self):
        import win32gui
        return int(win32gui.GetForegroundWindow() or 0)

    def window_title(self, hwnd):
        import win32gui
        return str(win32gui.GetWindowText(int(hwnd)) or '')

    def resolve_window(self, app_hint):
        hint = dict(app_hint or {})
        query = str(hint.get('query') or '')
        windows = self.list_windows(query) if query else self.list_windows('')
        if hint.get('process'):
            from pathlib import Path as _Path
            target = _Path(str(hint['process'])).stem.casefold()
            windows = [w for w in windows
                       if target in str(w.get('process_name') or '').casefold()]
        if hint.get('window_class'):
            need = str(hint['window_class']).casefold()
            windows = [w for w in windows
                       if need in str(w.get('window_class') or '').casefold()]
        if hint.get('title_contains'):
            need = str(hint['title_contains'])
            windows = [w for w in windows if need in str(w.get('title') or '')]
        if len(windows) > 1:
            foreground = self.foreground()
            focused = [w for w in windows if int(w.get('hwnd') or 0) == foreground]
            if len(focused) == 1:
                windows = focused
        if not windows:
            raise WorkflowError('没有找到目标窗口；请先打开应用（app_hint=%s）'
                                % json.dumps(hint, ensure_ascii=False))
        if len(windows) > 1:
            raise WorkflowError('目标窗口不唯一（%d 个），已停下不猜：%s'
                                % (len(windows), json.dumps(
                                    [w.get('title') for w in windows[:5]], ensure_ascii=False)))
        return dict(windows[0])

    def app_hint_from_window(self, hwnd):
        windows = self.list_windows('')
        target = next((w for w in windows if int(w.get('hwnd') or 0) == int(hwnd)), None)
        if target is None:
            raise WorkflowError('窗口不存在或已关闭，无法生成 app_hint')
        hint = dict(process=str(target.get('process_name') or ''),
                    window_class=str(target.get('window_class') or ''))
        others = [w for w in windows if int(w.get('hwnd') or 0) != int(hwnd)
                  and str(w.get('process_name')) == hint['process']
                  and str(w.get('window_class')) == hint['window_class']]
        title = str(target.get('title') or '')
        tokens = sorted({token.strip() for token in re.split(r'[-|—]', title)
                         if len(token.strip()) >= 2}, key=len, reverse=True)
        for token in tokens:
            if not any(token in str(w.get('title') or '') for w in others):
                hint['title_contains'] = token
                break
        return normalize_app_hint(hint)

    def snapshot(self, hwnd):
        return self._backend().snapshot(int(hwnd), limit=400, time_budget=2.0)

    def locate(self, hwnd, selector):
        selector = normalize_selector(selector, required=True)
        snapshot = self.snapshot(hwnd)
        try:
            element, tier = resolve_selector(snapshot.get('elements') or [], selector)
        except SelectorNotFound:
            direct = self._direct_locate(hwnd, selector) if snapshot.get('truncated') else None
            if direct is not None:
                return direct
            raise
        result = dict(element)
        result['_tier'] = list(tier)
        result['_revision'] = snapshot.get('revision')
        return result

    def _direct_locate(self, hwnd, selector):
        """整棵子树直接查找：快照被截断（控件数超限）时的兜底。"""
        name = selector.get('name') or ''
        automation_id = selector.get('automation_id') or ''
        control_type = selector.get('control_type') or ''
        if not any((name, automation_id, control_type)) or 'name_contains' in selector:
            return None
        try:
            result = self._backend().read(int(hwnd), name=name, automation_id=automation_id,
                                          control_type=control_type)
        except RuntimeError as exc:
            if '不唯一' in str(exc):
                raise SelectorAmbiguous('选择器在整棵控件树里命中多个控件，不唯一，未执行动作') from None
            return None
        element = dict(result.get('element') or {})
        element['_tier'] = ['direct']
        return element

    # -- 动作 ------------------------------------------------------------- #
    def focus(self, hwnd):
        """聚焦目标窗口；Windows 临时拒绝时用系统级切换再试。

        这里是“动作发出前的前置条件恢复”，不是重发已经发过的输入：两次尝试验证的都是
        焦点，失败时没有任何鼠标/键盘输入被送到目标窗口。
        """
        hwnd = int(hwnd)
        try:
            result = self.control.execute({'action': 'focus', 'hwnd': hwnd})
        except RuntimeError:
            result = None
        if self.foreground() == hwnd:
            return result or dict(focused=True)
        import ctypes
        import win32gui
        user = ctypes.windll.user32
        try:
            win32gui.ShowWindow(hwnd, 9)
            user.SwitchToThisWindow(hwnd, True)
            time.sleep(0.12)
        except Exception:
            pass
        if self.foreground() != hwnd:
            # 经典解锁技巧：先注入一次 ALT 抬起/按下，再请求前台，不点按任何控件。
            try:
                user.keybd_event(0x12, 0, 0, 0)
                user.keybd_event(0x12, 0, 2, 0)
                user.SetForegroundWindow(hwnd)
                time.sleep(0.1)
            except Exception:
                pass
        if self.foreground() != hwnd:
            raise WorkflowError('Windows 没有把焦点交给目标窗口（可能被系统/用户占用），'
                                '本步未发送任何输入')
        return dict(focused=True, method='SwitchToThisWindow')

    def act(self, hwnd, operation, selector, value=''):
        element = self.locate(hwnd, selector)
        return self._act_element(hwnd, operation, element, value)

    def _act_element(self, hwnd, operation, element, value=''):
        reference = element.get('ref')
        if not reference:
            raise WorkflowError('目标控件没有可操作引用（UIA ref），本步未执行')
        if operation in ('invoke', 'set_value', 'focus', 'click'):
            arguments = {'action': 'uia', 'hwnd': int(hwnd), 'uia_ref': reference,
                         'uia_operation': operation}
            if operation == 'set_value':
                arguments['uia_value'] = str(value)[:4000]
            return self.control.execute(arguments)
        # toggle/expand/collapse/select/scroll 不在 computer 工具的 uia 白名单里，
        # 直接走同一个 UIA 提供者；仍由 provider 校验前台窗口。
        return self._backend().act(int(hwnd), operation, ref=reference)

    def type_text(self, hwnd, selector, value):
        element = self.locate(hwnd, selector)
        kind = str(element.get('type') or '')
        if kind not in ('Edit', 'ComboBox'):
            raise WorkflowError('type 只允许写进输入框（Edit/ComboBox），当前目标是 %s，本步未执行'
                                % (kind or '未知'))
        return self._act_element(hwnd, 'set_value', element, value=str(value)[:4000])

    def scroll(self, hwnd, selector, times=1):
        """UIA ScrollPattern 优先；目标不暴露该模式时退回“聚焦目标 + 方向键”。

        注意：滚动的位置变化无法通用地读回，工作流里 scroll 步的 expect 通常只能核验
        “目标仍在/窗口仍在前台”，属于弱核验，文档里已注明。
        """
        element = self.locate(hwnd, selector)
        reference = element.get('ref')
        if not reference:
            raise WorkflowError('滚动目标没有可操作引用，本步未执行')
        times = max(1, min(int(times), 20))
        try:
            for _ in range(times):
                self._backend().act(int(hwnd), 'scroll', ref=reference)
            return dict(scrolled=times, method='ScrollPattern')
        except RuntimeError as exc:
            message = str(exc)
            if not any(token in message for token in
                       ('不支持', '未发送', 'UIA执行器', 'UIA引用', '已过期')):
                raise
        if not element.get('focused'):
            try:
                self._backend().act(int(hwnd), 'focus', ref=reference)
            except RuntimeError as exc:
                raise WorkflowError('滚动目标不支持 ScrollPattern，也无法聚焦，本步未执行：%s'
                                    % str(exc)[:160]) from None
        for _ in range(times):
            self.control.execute({'action': 'hotkey', 'keys': 'down'})
        return dict(scrolled=times, method='KeyboardArrow|page_focused')

    def key(self, keys):
        return self.control.execute({'action': 'hotkey', 'keys': str(keys)[:80]})

    def launch(self, query):
        from core.authorized_apps import AuthorizedApps
        return AuthorizedApps().open(str(query)[:80])

    # -- 期望等待 --------------------------------------------------------- #
    def wait_present(self, hwnd, selector, state='exists', timeout=3.0):
        """轮询目标是否存在；返回命中的元素，超时返回 None。仅用于调试/调用方自测。"""
        clock = time.monotonic
        deadline = clock() + max(0.05, float(timeout))
        while True:
            try:
                element = self.locate(hwnd, selector)
                if state == 'exists':
                    return element
            except SelectorNotFound:
                if state == 'absent':
                    return {}
            except SelectorAmbiguous:
                pass  # 存在但不唯一：对 absent 不成立，继续等到超时
            if clock() >= deadline:
                return None
            time.sleep(min(0.12, max(0.01, deadline - clock())))


# --------------------------------------------------------------------------- #
# 执行器：逐步执行 + 核验 + 计时
# --------------------------------------------------------------------------- #
class WorkflowRunner:
    def __init__(self, desktop, workflow, *, clock=time.monotonic):
        self.desktop = desktop
        self.workflow = workflow or {}
        self.clock = clock
        self._windows = {}
        self.primary_hwnd = None
        self.last_verify_error = ''

    # -- 窗口 ------------------------------------------------------------- #
    def window(self, step=None):
        hint = (step or {}).get('window') or self.workflow.get('app_hint') or {}
        if not hint:
            hint = {}
        key = json.dumps(hint, sort_keys=True, ensure_ascii=False)
        cached = self._windows.get(key)
        if cached is None:
            cached = self.desktop.resolve_window(hint)
            self._windows[key] = cached
        hwnd = int(cached.get('hwnd'))
        if self.primary_hwnd is None:
            self.primary_hwnd = hwnd
        return hwnd

    def window_title(self, step=None):
        return self.desktop.window_title(self.window(step))

    def locate(self, selector, step=None, hwnd=None):
        return self.desktop.locate(int(hwnd if hwnd is not None else self.window(step)),
                                   selector)

    # -- 核验 ------------------------------------------------------------- #
    def _poll(self, check, timeout_s):
        deadline = self.clock() + max(0.05, float(timeout_s))
        while True:
            try:
                value = check()
            except SelectorNotFound as exc:
                self.last_verify_error = str(exc)
                value = None
            except SelectorAmbiguous as exc:
                self.last_verify_error = str(exc)
                value = None
            if value:
                return value
            if self.clock() >= deadline:
                return None
            time.sleep(min(0.12, max(0.01, deadline - self.clock())))

    def check_expect(self, step):
        expect = step['expect']
        kind = expect['kind']
        timeout_s = (expect.get('timeout_ms') or step.get('timeout_ms') or 5000) / 1000.0
        self.last_verify_error = ''
        if kind == 'foreground':
            return self._expect_foreground(step, timeout_s)
        if kind == 'window_title_contains':
            return self._expect_window_title(step, expect, timeout_s)
        selector = expect.get('selector') or step.get('selector')
        if selector is None:
            self.last_verify_error = '核验没有目标 selector'
            return False
        hwnd = self.window(step)
        if kind == 'exists':
            if self._poll(lambda: dict(self.desktop.locate(hwnd, selector), _ok=True), timeout_s):
                return True
            return False
        if kind == 'absent':
            return self._expect_absent(hwnd, selector, timeout_s)
        return self._expect_compare(hwnd, selector, kind, expect, timeout_s)

    def _expect_foreground(self, step, timeout_s):
        hwnd = self.window(step)
        if self._poll(lambda: self.desktop.foreground() == hwnd, timeout_s):
            return True
        self.last_verify_error = '前台窗口不是目标窗口'
        return False

    def _expect_window_title(self, step, expect, timeout_s):
        hwnd = self.window(step)
        need = expect.get('value') or ''
        if self._poll(lambda: need in self.desktop.window_title(hwnd), timeout_s):
            return True
        self.last_verify_error = '窗口标题里没有 %r（当前 %r）' % (
            need, self.desktop.window_title(hwnd))
        return False

    def _expect_absent(self, hwnd, selector, timeout_s):
        def gone():
            try:
                self.desktop.locate(hwnd, selector)
            except SelectorNotFound:
                return True
            return False
        if self._poll(gone, timeout_s):
            return True
        self.last_verify_error = self.last_verify_error or '目标仍然存在'
        return False

    def _expect_compare(self, hwnd, selector, kind, expect, timeout_s):
        def compare():
            found = self.desktop.locate(hwnd, selector)
            if kind == 'name_equals':
                return str(found.get('name') or '') == expect.get('value')
            if kind == 'name_contains':
                return str(expect.get('value') or '').casefold() in \
                    str(found.get('name') or '').casefold()
            if kind == 'value_equals':
                return str(found.get('value') or '') == expect.get('value')
            if kind == 'value_contains':
                return str(expect.get('value') or '').casefold() in \
                    str(found.get('value') or '').casefold()
            if kind == 'enabled':
                return bool(found.get('enabled'))
            if kind == 'disabled':
                return not bool(found.get('enabled'))
            if kind == 'focused':
                return bool(found.get('focused'))
            return False
        if self._poll(compare, timeout_s):
            return True
        try:
            found = self.desktop.locate(hwnd, selector)
            actual = found.get('value') if 'value' in kind else found.get('name')
        except SelectorError as exc:
            self.last_verify_error = str(exc)
            return False
        self.last_verify_error = '读回值不符合预期：期望 %r，实际 %r' % (
            expect.get('value'), actual)
        return False

    def _ensure_foreground(self, step):
        """输入前把目标窗口拿回前台（前置条件恢复，不是重发已发送的动作）。"""
        hwnd = self.window(step)
        if self.desktop.foreground() == hwnd:
            return
        try:
            self.desktop.focus(hwnd)
        except (WorkflowError, RuntimeError) as exc:
            raise StepFailed('目标窗口不在前台且切不回去，未发送任何输入：%s'
                             % str(exc)[:160]) from None
        if self.desktop.foreground() != hwnd:
            raise StepFailed('目标窗口不在前台，未发送任何输入')

    # -- 单步 ------------------------------------------------------------- #
    def run(self, step, index):
        started = self.clock()
        method = ''
        try:
            op = step['op']
            if op in ('act', 'click', 'type', 'scroll', 'key'):
                # 真实桌面会有别的窗口随时抢前台（例如桌宠自己的悬浮入口）：
                # 发输入前先核对并恢复前台，避免把按键送错窗口。
                self._ensure_foreground(step)
            if op == 'focus':
                method = 'focus'
                self.desktop.focus(self.window(step))
            elif op == 'open_app':
                method = 'open_app'
                self._open_app(step)
            elif op in ('act', 'click'):
                hwnd = self.window(step)
                operation = step.get('operation') or ('click' if op == 'click' else 'invoke')
                # act 内部先精确解析选择器再输入：歧义/不存在会抛错，动作不会发出。
                result = self.desktop.act(hwnd, operation, step['selector'],
                                          value=step.get('value') or '')
                method = (result or {}).get('method') or operation
            elif op == 'type':
                hwnd = self.window(step)
                result = self.desktop.type_text(hwnd, step['selector'], step['value'])
                method = (result or {}).get('method') or 'set_value'
            elif op == 'scroll':
                hwnd = self.window(step)
                result = self.desktop.scroll(hwnd, step['selector'], step.get('times') or 1)
                method = (result or {}).get('method') or 'ScrollPattern'
            elif op == 'key':
                result = self.desktop.key(step['keys'])
                method = (result or {}).get('method') or 'hotkey'
            elif op == 'wait':
                method = 'wait'
                if not self._wait_state(step):
                    raise StepFailed('等待状态没有达成：%s（%s）'
                                     % (step.get('state'), self.last_verify_error))
            elif op == 'verify':
                method = 'read_only'
            else:  # pragma: no cover - normalize_step 已保证
                raise WorkflowError('未知动作 %r' % op)
            if not self.check_expect(step):
                raise StepFailed('核验未通过：%s' % (self.last_verify_error or '读回状态不符合预期'))
            return dict(ok=True, verified=True, elapsed_ms=round((self.clock() - started) * 1000, 1),
                        method=method)
        except StepFailed as exc:
            return dict(ok=False, verified=False, error=str(exc), method=method,
                        elapsed_ms=round((self.clock() - started) * 1000, 1))
        except SelectorAmbiguous as exc:
            return dict(ok=False, verified=False, error=str(exc), method=method,
                        elapsed_ms=round((self.clock() - started) * 1000, 1))
        except (WorkflowError, RuntimeError, OSError, ValueError, TypeError, KeyError) as exc:
            return dict(ok=False, verified=False, error=str(exc), method=method,
                        elapsed_ms=round((self.clock() - started) * 1000, 1))

    def _wait_state(self, step):
        state = step.get('state') or 'exists'
        selector = step.get('selector')
        hwnd = self.window(step)
        timeout_s = (step.get('timeout_ms') or 5000) / 1000.0
        if state == 'absent':
            def gone():
                try:
                    self.desktop.locate(hwnd, selector)
                except SelectorNotFound:
                    return True
                return False
            return bool(self._poll(gone, timeout_s))
        return bool(self._poll(lambda: dict(self.desktop.locate(hwnd, selector), _ok=True),
                               timeout_s))

    def _open_app(self, step):
        hint = step.get('window') or self.workflow.get('app_hint') or {}
        try:
            window = self.desktop.resolve_window(hint)
        except WorkflowError:
            if not step.get('launch'):
                raise
            query = str(hint.get('query') or hint.get('process') or '')
            self.desktop.launch(query)
            deadline = self.clock() + 8.0
            while True:
                try:
                    window = self.desktop.resolve_window(hint)
                    break
                except WorkflowError:
                    if self.clock() >= deadline:
                        raise
                    time.sleep(0.25)
        hwnd = int(window.get('hwnd'))
        self._windows[json.dumps(hint, sort_keys=True, ensure_ascii=False)] = window
        if self.primary_hwnd is None:
            self.primary_hwnd = hwnd
        return self.desktop.focus(hwnd)


# --------------------------------------------------------------------------- #
# 回放器
# --------------------------------------------------------------------------- #
class Player:
    def __init__(self, store=None, desktop=None, *, clock=time.monotonic):
        self.store = store or WorkflowStore()
        self._desktop = desktop
        self.clock = clock
        self._lock = threading.RLock()

    def desktop(self):
        if self._desktop is None:
            self._desktop = RealDesktop()
        return self._desktop

    def play(self, slug=None, *, path=None, dry_run=False, clock=None, preflight=True):
        clock = clock or self.clock
        if path:
            workflow = validate_workflow(_read_json(Path(path)))
        else:
            workflow = self.store.load(slug)
        desktop = self.desktop()
        runner = WorkflowRunner(desktop, workflow, clock=clock)
        report = {'ok': False, 'dry_run': bool(dry_run), 'slug': workflow['slug'],
                  'name': workflow['name'], 'total_steps': len(workflow['steps']),
                  'executed_steps': 0, 'verified_steps': 0, 'steps': [],
                  'stuck_at': None, 'error': '', 'started_at': _now_local(),
                  'success_rate': 0.0}
        with self._lock:
            started = clock()
            if dry_run:
                return self._dry_run(workflow, runner, report, started, clock)
            # 预检门：先只解析选择器、不发任何输入。录下来的工作流依赖录制当时的界面状态，
            # 状态不对时要在"动第一下手"之前停住，而不是先滚动了、先按了键再失败。
            if preflight:
                probe = self._dry_run(workflow, runner, dict(report, steps=[]), started, clock)
                if not probe.get('ok'):
                    bad = [step for step in probe.get('steps') or [] if not step.get('resolved')]
                    first = bad[0].get('index') if bad else None
                    report.update(
                        ok=False, stopped='preflight_failed', stuck_at=first,
                        error='回放前预检没过：当前界面状态和录制时不一样，一步都没执行。'
                              '把界面恢复成录制时的样子再试，或者重新录制一遍。',
                        preflight_failed=[{'index': step.get('index'),
                                           'title': step.get('title'),
                                           'error': step.get('error', '')} for step in bad],
                        total_ms=round((clock() - started) * 1000, 1))
                    return report
            for index, step in enumerate(workflow['steps'], 1):
                result = runner.run(step, index)
                report['steps'].append({
                    'index': index, 'op': step['op'], 'title': step['title'],
                    'elapsed_ms': result.get('elapsed_ms'), 'verified': result.get('verified'),
                    'method': result.get('method') or '', 'ok': result['ok'],
                    'error': result.get('error', '')})
                report['executed_steps'] = index
                if result['ok']:
                    report['verified_steps'] += 1
                    continue
                report.update(stuck_at=index, error=result.get('error') or '未核验通过',
                              ok=False)
                break
            else:
                recipe_ok, recipe_error = self._check_recipe(workflow, runner)
                report['verify_recipe_ok'] = recipe_ok
                if recipe_ok:
                    report['ok'] = True
                else:
                    report.update(stuck_at=len(workflow['steps']), ok=False,
                                  error='结束核验未通过：' + recipe_error)
            report['total_ms'] = round((clock() - started) * 1000, 1)
            report['first_step_ms'] = (report['steps'][0]['elapsed_ms']
                                       if report['steps'] else None)
            total = max(1, len(workflow['steps']))
            report['success_rate'] = round(report['verified_steps'] / total, 3)
            self._record_stats(workflow, report)
            return report

    def _dry_run(self, workflow, runner, report, started, clock):
        """干跑：只解析窗口/选择器，不发送任何输入。

        说明：``verify`` 步的目标常常是“动作之后才出现”的元素，所以它只作为信息展示；
        只有真正要输入/点击/滚动/聚焦的步骤必须在本步之前就能唯一定位，才算干跑通过。
        """
        planned = []
        required_ok = True
        for index, step in enumerate(workflow['steps'], 1):
            entry = {'index': index, 'op': step['op'], 'title': step['title']}
            entry['checked'] = bool(step.get('selector'))
            try:
                if step.get('selector') is not None:
                    element = runner.locate(step['selector'], step)
                    entry.update(resolved=True, control={
                        'name': element.get('name'), 'id': element.get('id'),
                        'type': element.get('type'), 'value': element.get('value')},
                        tier=element.get('_tier'))
                else:
                    entry['resolved'] = True
                    runner.window(step)  # 顺带确认窗口可解析
            except (SelectorError, WorkflowError) as exc:
                entry.update(resolved=False, error=str(exc))
                if step['op'] == 'verify':
                    entry['note'] = 'verify 目标可能在动作之后才出现，干跑不据此判失败'
                else:
                    required_ok = False
            planned.append(entry)
        report['steps'] = planned
        report['total_ms'] = round((clock() - started) * 1000, 1)
        report['ok'] = required_ok
        if not required_ok:
            report['error'] = '干跑仍有动作目标选择器解析不唯一/不存在，未发送任何输入'
        report['dry_run'] = True
        return report

    def _check_recipe(self, workflow, runner):
        recipe = workflow.get('verify_recipe')
        if not recipe:
            return True, ''
        kind = recipe['kind']
        if kind == 'window_title_contains':
            title = runner.window_title()
            if recipe['value'] in title:
                return True, ''
            return False, '窗口标题里没有 %r（当前 %r）' % (recipe['value'], title)
        try:
            element = runner.locate(recipe['selector'])
        except SelectorError as exc:
            return False, str(exc)
        if kind == 'element_exists':
            return True, ''
        if kind == 'element_name_contains':
            return (recipe['value'].casefold() in str(element.get('name') or '').casefold(),
                    '元素名称不符合：%r' % element.get('name'))
        return (recipe['value'].casefold() in str(element.get('value') or '').casefold(),
                '元素值不符合：%r' % element.get('value'))

    def _record_stats(self, workflow, report):
        workflow = dict(workflow)
        workflow['last_run'] = {
            'at': report['started_at'], 'ok': bool(report['ok']),
            'total_ms': report.get('total_ms'), 'first_step_ms': report.get('first_step_ms'),
            'executed_steps': report.get('executed_steps'),
            'verified_steps': report.get('verified_steps'),
            'success_rate': report.get('success_rate'),
            'stuck_at': report.get('stuck_at'),
            'error': _text(report.get('error'), 300),
            'steps': [{key: step.get(key) for key in ('index', 'op', 'elapsed_ms', 'ok')}
                      for step in report.get('steps') or []],
        }
        if report['ok']:
            workflow['success_count'] = int(workflow.get('success_count') or 0) + 1
            workflow['last_ok_at'] = _now_local()
            workflow['last_error'] = ''
        else:
            workflow['failure_count'] = int(workflow.get('failure_count') or 0) + 1
            workflow['last_error'] = _text(report.get('error'), 500)
        try:
            self.store.save(workflow)
        except WorkflowError:
            pass  # 统计写盘失败不能推翻已经真实发生的回放结果


# --------------------------------------------------------------------------- #
# 录制器
# --------------------------------------------------------------------------- #
_ACTION_TO_STEP = {
    'focus': ('focus', None),
    'uia': ('act', None),
    'hotkey': ('key', None),
    'press': ('key', None),
}
_COORDINATE_ACTIONS = ('click', 'double_click', 'drag', 'move', 'scroll', 'type', 'fill',
                       'editor_select_tail')


def _selector_from_action(action, result=None, override=None):
    if override is not None:
        return normalize_selector(override, required=True)
    control = {}
    if isinstance(result, dict):
        control = result.get('control') if isinstance(result.get('control'), dict) else {}
    selector = {}
    automation_id = _text(action.get('uia_id') or control.get('id'), 200)
    name = _text(action.get('uia_name') or control.get('name'), 200)
    control_type = _text(action.get('uia_type') or control.get('type'), 80)
    if automation_id:
        selector['automation_id'] = automation_id
    if name:
        selector['name'] = name
    if control_type:
        selector['control_type'] = control_type
    return normalize_selector(selector, required=True) if selector else None


class Recorder:
    """录制会话：start → observe/capture… → finish。

    - ``capture(step)``：真机执行一步并只在核验通过后追加（推荐训练/教学用）；
    - ``observe(action, result)``：把一次**已经成功执行**的 computer 工具动作追加为一步
      （供 core/self_agent.py 在 computer 工具成功后接线）；
    - ``finish()``：校验整份工作流并落盘到 ``runtime/workflows/<slug>.json``。
    """

    def __init__(self, store=None, desktop=None, *, clock=time.monotonic):
        self.store = store or WorkflowStore()
        self.desktop = desktop
        self.clock = clock
        self._draft = None
        self._lock = threading.RLock()
        self._started_at = None

    # -- 会话 ------------------------------------------------------------- #
    def start(self, name, trigger_phrases=(), *, slug=None, app_hint=None,
              window_hwnd=None, verify_recipe=None, desktop=None):
        with self._lock:
            if self._draft is not None:
                raise WorkflowError('已有进行中的录制会话（%s），先 finish() 或 abort()'
                                    % self._draft.get('slug'))
            if desktop is not None:
                self.desktop = desktop
            name = _text(name, 120)
            if not name:
                raise WorkflowError('录制需要 name')
            slug = _text(slug, 80) or _slugify(name)
            phrases = []
            for phrase in trigger_phrases or ():
                token = _text(phrase, 80)
                if token and token not in phrases:
                    phrases.append(token)
            if not phrases:
                phrases = [name[:80]]
            hint = normalize_app_hint(app_hint) if app_hint is not None else None
            if hint is None and window_hwnd is not None and self.desktop is not None:
                hint = self.desktop.app_hint_from_window(int(window_hwnd))
            self._draft = {
                'schema': SCHEMA_VERSION, 'name': name, 'slug': slug, 'version': 1,
                'description': '', 'trigger_phrases': phrases[:8], 'app_hint': hint,
                'steps': [], 'verify_recipe': verify_recipe, 'success_count': 0,
                'failure_count': 0, 'last_ok_at': None, 'last_error': '',
                'created_at': _now_local(), 'updated_at': _now_local(),
                'source': 'recorder', '_window_hwnd': int(window_hwnd) if window_hwnd else None,
            }
            self._started_at = self.clock()
            self._persist()
            return self.status()

    def status(self):
        with self._lock:
            if self._draft is None:
                return {'recording': False, 'steps': 0}
            return {'recording': True, 'name': self._draft.get('name'),
                    'slug': self._draft.get('slug'), 'steps': len(self._draft.get('steps') or []),
                    'app_hint': self._draft.get('app_hint'),
                    'trigger_phrases': list(self._draft.get('trigger_phrases') or [])}

    def _persist(self):
        draft = dict(self._draft)
        draft.pop('_window_hwnd', None)
        self.store.save_draft(draft)

    def _require_draft(self):
        if self._draft is None:
            raise WorkflowError('没有进行中的录制会话；先 start(name)')
        return self._draft

    def _ensure_app_hint(self, action=None):
        """接线录制时没显式给 app_hint：用当前动作/前台窗口补一个可回放的 app_hint。"""
        draft = self._draft
        if draft is None or draft.get('app_hint') or self.desktop is None:
            return
        hwnd = draft.get('_window_hwnd')
        if not hwnd and isinstance(action, dict):
            hwnd = action.get('hwnd') or action.get('_hwnd')
        if not hwnd:
            try:
                hwnd = self.desktop.foreground()
            except Exception:
                return
        try:
            draft['app_hint'] = self.desktop.app_hint_from_window(int(hwnd))
        except Exception:
            return

    # -- 追加 ------------------------------------------------------------- #
    def _append(self, step, *, baseline=None):
        draft = self._require_draft()
        draft['steps'].append(step)
        draft['updated_at'] = _now_local()
        if baseline:
            entry = draft.setdefault('baseline', {'steps': [], 'started_at': _now_local()})
            entry['steps'].append(baseline)
            entry['total_ms'] = round(sum(item.get('elapsed_ms') or 0
                                          for item in entry['steps']), 1)
        self._persist()
        return step

    def record_step(self, raw):
        with self._lock:
            draft = self._require_draft()
            step = normalize_step(raw, len(draft['steps']) + 1)
            return self._append(step)

    def observe(self, action, result=None, expect=None, title=None, selector=None):
        """把一次已经成功执行的 computer 工具动作追加为一步。"""
        with self._lock:
            self._ensure_app_hint(action)
            step = self.step_from_action(action, result=result, expect=expect,
                                         title=title, selector=selector)
            return self._append(step)

    def step_from_action(self, action, result=None, expect=None, title=None, selector=None,
                         index=None):
        action = dict(action or {})
        kind = _text(action.get('action'), 40)
        index = index or (len(self._require_draft()['steps']) + 1)
        if kind not in _ACTION_TO_STEP and kind not in _COORDINATE_ACTIONS:
            raise WorkflowError('“%s”不是可录制的桌面动作' % (kind or '空'))
        if kind in ('windows', 'editor_context', 'observe', 'accessibility', 'uia_read', 'uia_wait'):
            raise WorkflowError('观察类动作不进入工作流（它只读取，不改变状态）')
        if kind in _COORDINATE_ACTIONS and selector is None:
            raise WorkflowError('“%s”是坐标/画面动作，没有稳定选择器，无法在别的窗口回放；'
                                '请改用 uia 动作或显式给 selector' % kind)
        if kind == 'focus':
            raw_step = {'op': 'focus', 'title': title or '聚焦目标窗口',
                        'expect': expect or {'kind': 'foreground'}}
        elif kind == 'uia':
            operation = _text(action.get('uia_operation'), 40) or 'invoke'
            target = _selector_from_action(action, result, override=selector)
            raw_step = {'op': 'act', 'operation': operation,
                        'title': title or ('%s %s' % (operation, _text(target.get('name'), 40))),
                        'selector': target,
                        'expect': expect or {'kind': 'exists', 'selector': target}}
            if operation == 'set_value':
                raw_step['value'] = _text(action.get('uia_value'), 4000)
        elif kind in ('hotkey', 'press'):
            keys = _text(action.get('keys') or action.get('key') or action.get('text'), 80)
            raw_step = {'op': 'key', 'keys': keys, 'title': title or ('按键 ' + keys),
                        'expect': expect or {'kind': 'foreground'}}
        elif kind in ('type', 'fill'):
            target = normalize_selector(selector, required=True)
            raw_step = {'op': 'type', 'selector': target,
                        'value': action.get('text') if kind == 'fill' else action.get('text'),
                        'title': title or ('输入到 ' + _text(target.get('name'), 40)),
                        'expect': expect or {'kind': 'value_equals',
                                             'value': action.get('text') or ''}}
        elif kind in ('click', 'double_click'):
            target = normalize_selector(selector, required=True)
            raw_step = {'op': 'click', 'selector': target,
                        'title': title or ('点击 ' + _text(target.get('name'), 40)),
                        'expect': expect or {'kind': 'exists', 'selector': target}}
        else:  # scroll / drag / move / editor_select_tail
            target = normalize_selector(selector, required=True)
            raw_step = {'op': 'scroll' if kind == 'scroll' else 'act',
                        'selector': target,
                        'title': title or ('%s %s' % (kind, _text(target.get('name'), 40))),
                        'expect': expect or {'kind': 'exists', 'selector': target}}
        return normalize_step(raw_step, index)

    def capture(self, raw):
        """真机执行一步，核验通过才记入录制草稿；返回该步执行结果。"""
        with self._lock:
            draft = self._require_draft()
            if self.desktop is None:
                raise WorkflowError('录制器没有桌面执行器；capture 需要 desktop=RealDesktop()')
            step = normalize_step(raw, len(draft['steps']) + 1)
            workflow = {key: draft.get(key) for key in ('slug', 'name', 'app_hint', 'steps')}
            runner = WorkflowRunner(self.desktop, workflow, clock=self.clock)
            if draft.get('_window_hwnd') and not draft.get('app_hint'):
                hint = self.desktop.app_hint_from_window(draft['_window_hwnd'])
                draft['app_hint'] = hint
                workflow['app_hint'] = hint
            result = runner.run(step, step['id'])
            if not result.get('ok'):
                raise StepFailed('录制第 %d 步没有通过真机核验，未记入工作流：%s'
                                 % (step['id'], result.get('error')), step_index=step['id'])
            self._append(step, baseline=dict(index=step['id'], op=step['op'],
                                             title=step['title'],
                                             elapsed_ms=result.get('elapsed_ms'),
                                             method=result.get('method') or ''))
            return result

    # -- 收尾 ------------------------------------------------------------- #
    def finish(self):
        with self._lock:
            draft = self._require_draft()
            draft = {key: value for key, value in draft.items() if key != '_window_hwnd'}
            if draft.get('baseline'):
                draft['baseline']['recorded_at'] = _now_local()
                draft['baseline']['recorded_total_ms'] = draft['baseline'].get('total_ms')
            workflow = validate_workflow(draft)
            saved = self.store.save(workflow)
            self.store.discard_draft(saved['slug'])
            self._draft = None
            self._started_at = None
            return saved

    def abort(self):
        with self._lock:
            if self._draft is not None:
                self.store.discard_draft(self._draft.get('slug'))
            self._draft = None
            self._started_at = None
            return True


# --------------------------------------------------------------------------- #
# 自然语言 → 步骤：一次模型调用；模型不可用就明确失败，绝不伪造步骤
# --------------------------------------------------------------------------- #
COMPILER_INSTRUCTION = '''你是“桌面工作流编译器”。把用户的一段自然语言长操作指令，编译成一份
可以在同一个 Windows 窗口上回放的可执行步骤 JSON。你只能输出一个 JSON 对象，不要 Markdown、
不要解释。

JSON 结构：
{
  "name": "短中文名",
  "description": "一句话说明",
  "trigger_phrases": ["用户下次可能怎么说这句话（2-5条，短句）"],
  "app_hint": {"query": "软件名（用用户说的名称）"},
  "steps": [
    {"op": "focus", "title": "聚焦窗口", "expect": {"kind": "foreground"}},
    {"op": "verify", "title": "确认窗口", "selector": {"name_contains": "窗口标题片段", "control_type": "Window"}, "expect": {"kind": "exists"}},
    {"op": "act", "operation": "invoke", "selector": {"name": "按钮名称", "control_type": "Button"}, "expect": {"kind": "exists", "selector": {"name": "出现后的目标", "control_type": "Edit"}}},
    {"op": "type", "selector": {"name": "输入框名称", "control_type": "Edit"}, "value": "要输入的文字", "expect": {"kind": "value_equals", "value": "要输入的文字"}},
    {"op": "key", "keys": "enter", "expect": {"kind": "foreground"}},
    {"op": "scroll", "selector": {"name": "页面", "control_type": "Document"}, "times": 2, "expect": {"kind": "exists", "selector": {"name": "页面", "control_type": "Document"}}},
    {"op": "verify", "selector": {"name_contains": "标题片段", "control_type": "Document"}, "expect": {"kind": "name_contains", "value": "标题片段"}}
  ],
  "verify_recipe": {"kind": "window_title_contains", "value": "窗口标题里的稳定片段"}
}

硬性规则：
1. 每个步骤必须有 expect（动作后靠读回核验），不允许没有核验的盲操作。
2. selector 字段只允许：automation_id、name、name_contains、control_type、class_name、ordinal。
   优先生成 automation_id（如 view_1012 这类稳定 ID）或名称+控件类型；只有明确知道同名控件
   的顺序时才用 ordinal（0 开始）。选择器必须能唯一命中，否则回放会安全地停下。
   只写 control_type 而没有 name/automation_id/ordinal 的选择器一定无法唯一命中，不要这样写。
3. 只允许这些 op：focus/open_app/act/click/key/type/scroll/wait/verify。
   act 的 operation 只允许 invoke/set_value/focus/toggle/expand/collapse/select/scroll。
4. 严禁生成发送、提交、删除、卸载、支付、购买、确认、确定、关机、重启、注销、发布、
   格式化、退出登录类控件的动作；严禁 alt+f4 等危险快捷键。这是硬拒绝。
5. type 只能写入 Edit/ComboBox 输入框；不要生成坐标点击（没有 x/y 字段）。
6. 最多 15 步；步骤要能在一个窗口内完成；需要打开软件时第一步用 open_app（不要 launch=true）。
7. 不要臆造控件名称：如果用户描述不足以确定控件，宁可在对应步骤写最保守的 verify 步骤
   （例如只核验窗口级元素），也不要编造具体按钮名。
只输出 JSON。'''


def _strip_json_fence(text):
    value = str(text or '').strip()
    if value.startswith('```'):
        value = value.split('\n', 1)[-1]
        if value.rstrip().endswith('```'):
            value = value.rstrip()[:-3]
    start = value.find('{')
    end = value.rfind('}')
    if start < 0 or end <= start:
        return value
    return value[start:end + 1]


def _default_complete(messages, timeout=45):
    """一次模型调用：沿用 core/model_settings + core/chat_answer.request_payload 的既有通道。"""
    from core.model_settings import load_config
    from core.chat_answer import request_payload
    config = load_config()
    key = config.get('api_key')
    base = str(config.get('base_url') or '').rstrip('/')
    if not key or not base:
        raise WorkflowError('模型通道未配置（缺 api_key/base_url），不能把长指令编译成工作流')
    import requests
    from urllib.parse import urlsplit
    import uuid
    payload = request_payload(config, messages, 'quick')
    payload.update(stream=False, max_tokens=3072, temperature=0)
    payload.pop('stream_options', None)
    headers = {'Authorization': 'Bearer ' + key,
               'Content-Type': 'application/json',
               'User-Agent': 'GugaDesktopPet/1.0'}
    if urlsplit(base).hostname == 'opencode.ai':
        # 该通道要求会话头才能路由（其它本地智能体一致的做法）。
        headers['x-opencode-session'] = 'guga-action-workflow-' + uuid.uuid4().hex[:16]
    try:
        response = requests.post(base + '/chat/completions',
                                 headers=headers,
                                 json=payload, timeout=(3, timeout))
        try:
            if response.status_code != 200:
                detail = _text(response.text, 240)
                raise WorkflowError('模型通道返回 HTTP %s，未生成任何步骤：%s'
                                    % (response.status_code, detail))
            data = response.json()
        finally:
            response.close()
    except requests.RequestException as exc:
        raise WorkflowError('模型通道连接失败（%s），未生成任何步骤' % type(exc).__name__) from None
    text = str(((data.get('choices') or [{}])[0].get('message') or {}).get('content') or '')
    if not text.strip():
        raise WorkflowError('模型返回空内容，未生成任何步骤')
    return text


_MODEL_TOP_FIELDS = {'schema', 'name', 'description', 'trigger_phrases', 'app_hint',
                     'steps', 'verify_recipe', 'slug', 'version'}
_MODEL_STEP_FIELDS = {'id', 'op', 'title', 'selector', 'operation', 'keys', 'value', 'times',
                      'state', 'expect', 'timeout_ms', 'window', 'launch'}
_MODEL_SELECTOR_ALIASES = {'id': 'automation_id', 'automationid': 'automation_id',
                           'type': 'control_type', 'controltype': 'control_type',
                           'text': 'name', 'label': 'name', 'title': 'name',
                           'classname': 'class_name'}
_MODEL_APP_ALIASES = {'name': 'query', 'title': 'query', 'software': 'query', 'app': 'query',
                      'exe': 'process', 'class': 'window_class'}


def _sanitize_model_selector(raw, notes, index):
    if not isinstance(raw, dict):
        return raw
    out = {}
    for key, value in raw.items():
        mapped = _MODEL_SELECTOR_ALIASES.get(str(key).casefold(), key)
        if mapped not in SELECTOR_KEYS:
            notes.append('第%d步 selector.%s' % (index, key))
            continue
        if mapped == 'ordinal':
            out['ordinal'] = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            out[mapped] = str(value)
        elif isinstance(value, str) and value.strip():
            out[mapped] = value.strip()
    return out


def _sanitize_model_output(data):
    """把模型输出里常见的小偏差规范化（别名/多余字段），不改动任何语义动作。

    只做三件事：字段别名映射、丢弃不认识的字段（记入 notes）、可选校验器降级。
    任何“动作本身不合法/危险”的情况仍然交给 validate_workflow 明确拒绝。
    """
    notes = []
    data = dict(data)
    _drop_unknown_model_fields(data, notes)
    if isinstance(data.get('trigger_phrases'), str):
        data['trigger_phrases'] = [data['trigger_phrases']]
    if isinstance(data.get('app_hint'), dict):
        data['app_hint'] = _sanitize_model_app_hint(data['app_hint'], notes)
    data['steps'] = [_sanitize_model_step(raw, index, notes)
                     for index, raw in enumerate(data.get('steps') or [], 1)]
    if data.get('verify_recipe') is not None:
        try:
            normalize_recipe(data['verify_recipe'])
        except WorkflowError:
            notes.append('verify_recipe')
            data.pop('verify_recipe')
    return data, notes


def _drop_unknown_model_fields(data, notes):
    for key in list(data):
        if key not in _MODEL_TOP_FIELDS:
            notes.append('顶层字段 %s' % key)
            data.pop(key)


def _sanitize_model_app_hint(app_hint, notes):
    hint = {}
    for key, value in app_hint.items():
        mapped = _MODEL_APP_ALIASES.get(str(key).casefold(), key)
        if mapped in ('query', 'process', 'window_class', 'title_contains') \
                and isinstance(value, str) and value.strip():
            hint[mapped] = value.strip()
        else:
            notes.append('app_hint.%s' % key)
    return hint or None


def _sanitize_model_step(raw, index, notes):
    if not isinstance(raw, dict):
        raise WorkflowError('模型返回的第 %d 步不是对象，未生成工作流' % index)
    step = dict(raw)
    if isinstance(step.get('selector'), dict):
        step['selector'] = _sanitize_model_selector(step['selector'], notes, index)
    if isinstance(step.get('expect'), dict):
        expect = dict(step['expect'])
        if isinstance(expect.get('selector'), dict):
            expect['selector'] = _sanitize_model_selector(expect['selector'], notes, index)
        step['expect'] = expect
    _normalize_model_step_timeouts(step)
    if 'key' in step and 'keys' not in step:
        step['keys'] = step.pop('key')
    for key in list(step):
        if key not in _MODEL_STEP_FIELDS:
            notes.append('第%d步字段 %s' % (index, key))
            step.pop(key)
    return step


def _normalize_model_step_timeouts(step):
    for alias in ('seconds', 'wait_seconds', 'wait_ms'):
        if alias in step:
            try:
                millis = int(float(step.pop(alias)) * (1000 if alias != 'wait_ms' else 1))
                step.setdefault('timeout_ms', max(100, min(millis, 30000)))
            except (TypeError, ValueError):
                step.pop(alias)


def compile_from_text(text, *, name=None, trigger_phrases=None, app_hint=None, slug=None,
                      store=None, complete=None, timeout=45):
    """把一段自然语言长指令用**一次**模型调用编译成可执行工作流并落盘。

    ``complete`` 可注入（测试用）：callable(messages) -> str。模型不可用/返回不可执行时
    抛 WorkflowError，且不会写入任何文件、不会伪造步骤。
    """
    instruction = _text(text, 8000)
    if not instruction:
        raise WorkflowError('编译工作流需要一段操作指令')
    store = store or WorkflowStore()
    complete = complete or (lambda messages: _default_complete(messages, timeout=timeout))
    messages = [{'role': 'system', 'content': COMPILER_INSTRUCTION},
                {'role': 'user', 'content': instruction}]
    try:
        reply = complete(messages)
    except WorkflowError:
        raise
    except Exception as exc:
        raise WorkflowError('模型调用失败（%s），未生成任何步骤' % type(exc).__name__) from None
    if not isinstance(reply, str) or not reply.strip():
        raise WorkflowError('模型返回空内容，未生成任何步骤')
    try:
        data = json.loads(_strip_json_fence(reply))
    except ValueError:
        raise WorkflowError('模型返回的不是合法 JSON，未生成任何步骤') from None
    if not isinstance(data, dict) or not isinstance(data.get('steps'), list):
        raise WorkflowError('模型返回缺少 steps，未生成任何步骤')
    data, notes = _sanitize_model_output(data)
    if not data.get('steps'):
        raise WorkflowError('模型没有给出任何步骤，未生成工作流')
    if notes:
        suffix = '（编译时规范化：' + '、'.join(sorted(set(notes))[:8]) + '）'
        data['description'] = (_text(data.get('description'), 400) + suffix)[:500]
    if name:
        data['name'] = _text(name, 120)
    if trigger_phrases:
        data['trigger_phrases'] = [_text(phrase, 80) for phrase in trigger_phrases if _text(phrase, 80)]
    if app_hint is not None:
        data['app_hint'] = app_hint
    if not data.get('name'):
        data['name'] = _text(instruction, 60)
    data.setdefault('slug', _text(slug, 80) or _slugify(data['name']))
    if slug:
        data['slug'] = _text(slug, 80)
    data['source'] = 'model-compiled'
    workflow = validate_workflow(data)
    return store.save(workflow)


# --------------------------------------------------------------------------- #
# 接线层：给 core/self_agent.py / 语音入口用的小函数（线程安全）
# --------------------------------------------------------------------------- #
_ACTIVE = None
_ACTIVE_LOCK = threading.RLock()


def start_recording(name, trigger_phrases=(), **kwargs):
    """在咕嘎进程里开一段录制；computer 工具成功后调用 record_computer_action 追加。"""
    global _ACTIVE
    with _ACTIVE_LOCK:
        if _ACTIVE is not None:
            raise WorkflowError('已有进行中的录制会话（%s）' % _ACTIVE.status().get('slug'))
        store = kwargs.pop('store', None) or WorkflowStore()
        desktop = kwargs.pop('desktop', None)
        _ACTIVE = Recorder(store=store, desktop=desktop)
        try:
            _ACTIVE.start(name, trigger_phrases, **kwargs)
        except Exception:
            _ACTIVE = None
            raise
        return _ACTIVE.status()


def active_recording():
    with _ACTIVE_LOCK:
        return _ACTIVE.status() if _ACTIVE is not None else {'recording': False}


def record_computer_action(action, result=None, expect=None):
    """computer 工具成功执行后调用：把这次动作追加进正在录制的工作流。"""
    with _ACTIVE_LOCK:
        if _ACTIVE is None:
            return None
        try:
            return _ACTIVE.observe(action, result=result, expect=expect)
        except WorkflowError:
            return None  # 不可回放的动作（坐标类）静默跳过，不打断用户当前操作


def finish_recording():
    global _ACTIVE
    with _ACTIVE_LOCK:
        if _ACTIVE is None:
            raise WorkflowError('没有进行中的录制会话')
        try:
            return _ACTIVE.finish()
        finally:
            _ACTIVE = None


def abort_recording():
    global _ACTIVE
    with _ACTIVE_LOCK:
        if _ACTIVE is None:
            return False
        _ACTIVE.abort()
        _ACTIVE = None
        return True


def find_workflow(text, store=None):
    """用户这句话是否命中已保存工作流的触发短语；命中返回工作流 dict。"""
    matches = (store or WorkflowStore()).find_by_phrase(text)
    return matches[0] if matches else None


def play_workflow(slug, *, store=None, dry_run=False):
    """回放一个已保存工作流；返回带逐步耗时/成功率的报告。"""
    return Player(store=store).play(slug, dry_run=dry_run)
