# -*- coding: utf-8 -*-
"""点名外部工具的硬约束。

用户明确说“用豆包 / 用 WorkBuddy / 用 JoyCode”时，本鹅自己的模型不得直接产出成品：
  · 先看本机是否真的装了该工具、有没有可用通道；
  · 没装 → 界面内给出“缺什么、去哪装、装好后怎么说”，绝不代写冒充；
  · 装了 → 强制进入任务模式，由本地执行方真实操作并取得回执；
  · 交付前校验证据：没有任何真实工具调用记录 = 跳步，不可交付。

本模块只做判断与文案，不执行任何动作，方便被路由/执行/交付三处共用。
"""
import re

# 指向外部工具的动词前缀
_VERBS = ('用', '使用', '通过', '叫', '让', '请', '交给', '派给', '打开', '帮我用', '给我用', '帮忙用')
# 纯咨询，不算点名执行
_ASK = ('是什么', '什么是', '怎么样', '有哪些', '怎么用', '如何用', '怎么安装', '如何安装',
        '介紹', '介绍', '区别', '多少钱', '在哪下载', '官网', '支持哪些')
_ACTION = ('生成', '写', '做', '出', '整理', '翻译', '总结', '画', '制作', '改', '查', '搜',
           '回答', '问', '提交', '输入', '发')

EXEC_TOOLS = ('applications', 'computer', 'browser', 'wechat', 'ecosystem',
              'pipeline', 'local_tools', 'web_search', 'mcp')

_SPEC_CACHE = {'map': None}


def _spec_map():
    """alias(小写) -> APPS 条目；延迟导入，避免路由热路径反复加载。"""
    if _SPEC_CACHE['map'] is None:
        try:
            from core import local_tools
            mapping = {}
            for spec in local_tools.APPS:
                for alias in (spec.get('aliases') or ()):
                    alias = str(alias).strip().lower()
                    if alias:
                        mapping[alias] = spec
            _SPEC_CACHE['map'] = mapping
        except Exception:
            _SPEC_CACHE['map'] = {}
    return _SPEC_CACHE['map']


def _alias_re(alias):
    if re.search(r'[a-z]', alias):
        return re.compile(r'(?<![a-z0-9])' + re.escape(alias) + r'(?![a-z0-9])', re.I)
    return re.compile(re.escape(alias))


def detect(text):
    """返回被点名的外部工具（dict: id/name/automation/adapter/note），没有则 None。"""
    value = str(text or '').strip()
    if not value:
        return None
    low = value.lower()
    if any(word in value for word in _ASK) and not any(word in value for word in _ACTION):
        return None
    if value.startswith(('怎么', '如何', '为什么', '什么叫', '什么是', '介绍')):
        return None
    for alias, spec in _spec_map().items():
        pattern = _alias_re(alias)
        for match in pattern.finditer(low):
            head = low[:match.start()].rstrip()
            if any(head.endswith(verb) for verb in _VERBS):
                return _shape(spec)
            # “豆包生成 200 字作文”：别名打头 + 动作词紧随其后
            if not head and low[match.end():].strip().startswith(_ACTION):
                return _shape(spec)
    return None


def _shape(spec):
    return dict(id=spec.get('id', ''), name=spec.get('name', ''),
                automation=spec.get('automation', ''), adapter=spec.get('adapter', ''),
                note=spec.get('notes', ''))


def availability(mandate):
    """真实探测本机是否装了该工具（查 exe/开始菜单，不启动软件）。"""
    if not mandate:
        return dict(installed=False, running=False)
    try:
        from core import tool_advice
        installed = mandate['id'] in tool_advice.installed_ids()
    except Exception:
        installed = False
    running = False
    try:
        from core import local_tools
        spec = next((s for s in local_tools.APPS if s.get('id') == mandate['id']), None)
        running = bool(spec and local_tools._windows_of(spec))
    except Exception:
        running = False
    return dict(installed=bool(installed or running), running=running)


_INSTALL_GUIDE = {
    'doubao': '到 www.doubao.com 下载豆包桌面端，安装并登录后窗口保持可打开',
    'workbuddy': '安装 WorkBuddy 桌面端并登录（默认 %LOCALAPPDATA%\\Programs\\WorkBuddy\\WorkBuddy.exe）',
    'joycode': '按比赛说明把 JoyCode 装到 D:\\JoyCode 并登录；装在别处时设置环境变量 '
                'GUGA_JOYCODE_LAUNCHER 指向 JoyCode.exe',
}


def install_message(mandate):
    name = mandate.get('name') or '该工具'
    guide = _INSTALL_GUIDE.get(mandate.get('id', ''))
    if not guide:
        try:
            from core import tool_advice
            row = next((t for t in tool_advice.TOOLS if t['id'] == mandate.get('id')), None)
            guide = (row or {}).get('install', '')
        except Exception:
            guide = ''
    # 承诺句必须排在最前面：气泡宽度只有约 14 字/行，长文案后半段会被截掉，
    # 而“不代写冒充”是产品的核心态度，不能放在末尾（P2-NOAI-TRUNC）。
    return ('本机没装%s，这次我不代写：本鹅的模型不会冒充它的产出。'
            '下一步：%s。装好后再对我说“用%s …”，我在它的窗口里真机执行。'
            % (name, guide or '请先安装该软件并登录', name))


def channel_message(mandate):
    name = mandate.get('name') or '该工具'
    automation = mandate.get('automation') or ''
    if automation in ('ui_only', 'launch_only'):
        return ('%s已安装，但它没有可编程接口，只能界面指挥：我会在它的窗口里真正输入、提交并读回结果。'
                '窗口若没打开，我先启动它。' % name)
    return '%s已安装，我会通过本机通道真实调用它并核对回执。' % name


def failure_message(mandate, reason='', next_step=''):
    name = mandate.get('name') or '该工具'
    # 气泡最多 7 行：原因压到一句话，保证“下一步”不被截掉（完整细节仍在结果 JSON/日志里）。
    detail = ('原因：' + str(reason).split('。')[0][:48]) if reason else ''
    step = next_step or ('打开并登录%s后重试，或告诉我换工具。' % name)
    return ('没能调起%s：没有真实工具调用回执，不拿模型代写冒充。%s '
            '下一步：%s'
            % (name, detail, step))


def evidence_ok(events):
    """执行账本里是否至少有一次真实工具成功回执。"""
    for item in events or ():
        if not isinstance(item, dict):
            continue
        if item.get('ok') is True and str(item.get('tool') or '') in EXEC_TOOLS:
            return True
    return False


def evidence_line(events):
    seen = []
    for item in events or ():
        if not isinstance(item, dict) or item.get('ok') is not True:
            continue
        tool = str(item.get('tool') or '')
        if tool in EXEC_TOOLS and tool not in seen:
            seen.append(tool)
    return '、'.join(seen)
