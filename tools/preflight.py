# -*- coding: utf-8 -*-
"""发布前找茬机（preflight）：把"踩过的坑"变成机器检查，而不是靠人记得。

跑法：
    python -X utf8 tools/preflight.py

输出 PASS / WARN / FAIL。**任何 FAIL 都不该发布**；WARN 表示能用但你要知道。
纪律：凡是这个项目真踩过一次的坑，这里必须有一条对应检查。加新坑就加新检查。

来源：队友仓库 `guga-desktop/agent/preflight.py` 的"教训固化成检查"做法。
"""
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CHECKS = []


def check(name, level='fail'):
    """把一个函数登记成检查项。返回值为 (ok, detail)。"""
    def wrap(fn):
        CHECKS.append((name, level, fn))
        return fn
    return wrap


def _read(relative):
    path = ROOT / relative
    return path.read_text(encoding='utf-8') if path.is_file() else ''


# --------------------------------------------------------------- 快速命令
@check('快速命令：长句不能被整句当软件名')
def _fast_command_long_sentence():
    from core import fast_commands as fc
    matched = fc.match('打开豆包后在赛事区中新建一个对话')
    if matched.get('matched'):
        return False, '长句被快速命令截胡了：%s → %s' % (
            matched.get('action'), matched.get('target'))
    return True, '长句正确地交给模型'


@check('快速命令：口语"我的b站"要认成网址')
def _fast_command_my_bilibili():
    from core import fast_commands as fc
    matched = fc.match('打开我的b站')
    if matched.get('action') != 'open_url':
        return False, '期望 open_url，实际 %s / %s' % (
            matched.get('action'), matched.get('target'))
    if 'bilibili.com' not in str(matched.get('target')):
        return False, '目标不是 bilibili：%s' % matched.get('target')
    return True, str(matched.get('target'))


# --------------------------------------------------------------- MCP/传输
def _code_only(source):
    """只保留真正的代码：剥掉注释和字符串字面量（含 docstring）。

    为什么不用 `'子串' in source`：解释历史 bug 的文档里会写同样的字，
    那样检查器就成了"文档一提到就报错"的橡皮章。
    """
    import io
    import tokenize
    pieces = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            pieces.append(token.string)
    except (tokenize.TokenError, IndentationError):
        return source  # 解析失败就退回原文，宁可误报也不静默放过
    return ' '.join(pieces)


@check('MCP：SSE 不能再用 iter_lines(decode_unicode=True)')
def _mcp_sse_encoding():
    source = _read('core/mcp_host.py')
    if not source:
        return False, 'core/mcp_host.py 不存在'
    code = _code_only(source).replace(' ', '')  # tokenize 会插空格，比较前先压实
    if 'iter_lines' in code and 'decode_unicode' in code:
        return False, '代码里又用回了 iter_lines(decode_unicode=True)（中文 JSON 会被截断）'
    if 'raw.read' not in code:
        return False, '没有按字节读（缺 raw.read）'
    # 编码名本身是字符串字面量，必须回原文查
    if "decode('utf-8'" not in source and 'decode("utf-8"' not in source:
        return False, '没有显式 UTF-8 解码'
    return True, '按字节读、只按 \\n 切行、显式 UTF-8 解码'


@check('MCP：保留 streamable-http 分支')
def _mcp_http_branch():
    source = _read('core/mcp_host.py')
    if 'StreamableHTTPMCP' not in source:
        return False, 'StreamableHTTPMCP 不见了'
    return True, '存在 `StreamableHTTPMCP`'


# --------------------------------------------------------------- DSH 双向
@check('DSH 网关：新会话必须能新建（create/start）')
def _dsh_gateway_create():
    from core.self_agent import TOOLS
    spec = next((t['function'] for t in TOOLS if t['function']['name'] == 'dsh_gateway'), None)
    if not spec:
        return False, 'dsh_gateway 工具不存在'
    actions = spec['parameters']['properties']['action'].get('enum') or []
    missing = [a for a in ('create', 'start') if a not in actions]
    if missing:
        return False, '缺少动作：%s（模型只能退回点窗口）' % ','.join(missing)
    return True, '动作含 create/start'


# --------------------------------------------------------------- 工具面
@check('工具面：每个工具都要有前置条件，不能只写能干什么')
def _tool_preconditions():
    from core.self_agent import TOOLS
    required = {
        'applications': ('不是软件', 'browser'),
        'browser': ('session_start', '登录'),
        'computer': ('坐标', 'uia'),
        'code_task': ('终态', '检查命令'),
    }
    problems = []
    for name, needles in required.items():
        spec = next((t['function'] for t in TOOLS if t['function']['name'] == name), None)
        if not spec:
            problems.append('%s 不存在' % name)
            continue
        description = str(spec.get('description', ''))
        missing = [n for n in needles if n not in description]
        if missing:
            problems.append('%s 的描述缺 %s' % (name, '/'.join(missing)))
    if problems:
        return False, '；'.join(problems)
    return True, '四个关键工具都有前置条件'


@check('工具面：workflow 要能静态校验和真跑')
def _workflow_tool():
    from core.self_agent import TOOLS
    spec = next((t['function'] for t in TOOLS if t['function']['name'] == 'workflow'), None)
    if not spec:
        return False, 'workflow 工具不存在'
    actions = spec['parameters']['properties']['action'].get('enum') or []
    missing = [a for a in ('list', 'plan', 'run') if a not in actions]
    if missing:
        return False, '缺少动作：%s' % ','.join(missing)
    return True, '动作含 list/plan/run'


# --------------------------------------------------------------- harness
@check('harness：计划/卡点模块在，且有逃生开关')
def _agent_harness():
    module = importlib.import_module('core.agent_harness')
    if not hasattr(module, 'enabled'):
        return False, '没有 enabled() 逃生开关'
    if not hasattr(module, 'parse_plan'):
        return False, '没有 parse_plan'
    source = _read('core/self_agent.py')
    if 'agent_harness' not in source:
        return False, 'agent_harness 没接进 SelfAgent'
    return True, '有开关，且已接进主循环'


@check('harness：每步验收要有硬判据（命中验收条件才推进）')
def _harness_acknowledge():
    module_source = _read('core/agent_harness.py')
    if 'evidence_matches' not in module_source:
        return False, '没有 evidence_matches：验收条件无法被判定'
    if 'auto_advanced' not in module_source:
        return False, '没有 auto_advanced：推进没有留痕'
    if 'harness.auto_advanced' not in _read('core/self_agent.py'):
        return False, '自动推进没接进主循环（模型仍可自己说做完了）'
    return True, '验收条件命中即自动推进，且会告诉模型"已判定完成，别重复做"'


# --------------------------------------------------------------- 编码通道
@check('编码通道：拉到终态才算完，且有退出码兜底')
def _coding_agent():
    source = _read('core/coding_agent.py')
    if not source:
        return False, 'core/coding_agent.py 不存在'
    need = ['check_cmd', 'timeout_but_verified', 'DONE.json']
    missing = [n for n in need if n not in source]
    if missing:
        return False, '缺：%s' % ','.join(missing)
    return True, '有 check_cmd 兜底与完成信号'


@check('编码通道：code_task 已暴露给模型')
def _code_task_tool():
    from core.self_agent import TOOLS
    spec = next((t['function'] for t in TOOLS if t['function']['name'] == 'code_task'), None)
    if not spec:
        return False, 'code_task 不在工具面'
    props = spec['parameters']['properties']
    if 'check_cmd' not in props:
        return False, 'code_task 没有 check_cmd 参数'
    return True, 'code_task 带 check_cmd'


# --------------------------------------------------------------- 工作流
@check('编码通道：上一轮留下的完成文件不能骗过这一轮')
def _code_stale_signal():
    module = _read('core/coding_agent.py')
    if '_stale_signals' not in module or 'stale_signals' not in module:
        return False, '没有记录旧完成文件：同一个项目再改会瞬间"完成"'
    return True, '本轮开始时就存在的 DONE/FAILED 视为旧文件，忽略'


@check('编码通道：check_cmd 不能只支持 python -m')
def _code_check_forms():
    module = _read('core/coding_agent.py')
    if "'-m']" not in module:
        return False, '缺少 python 模块形式（-m）'
    if 'shell=' not in module:
        return False, '没有 shell 兜底：npm test / 脚本路径类命令会失败'
    return True, '模块 / 脚本 / 外部命令三种形式都支持'


@check('编码通道：卡住要能重试，不能一次卡死就当失败')
def _code_retry():
    module = _read('core/coding_agent.py')
    if "'stalled'" not in module:
        return False, '没有停滞判定'
    if 'attempts' not in module:
        return False, '没有重试计数'
    if "'session-%d'" in module or 'retries' not in module:
        return False, '没有重试逻辑'
    return True, 'stalled → 换新会话重试，结果里带 attempts'


@check('编码通道：同一个项目再改要用回原会话')
def _code_session_reuse():
    module = _read('core/coding_agent.py')
    if 'session_id=None' not in module or 'reused' not in module:
        return False, 'CodingAgent 不支持复用会话'
    source = _read('ui/agent_runtime.py')
    if '_code_sessions' not in source:
        return False, '运行时没记住每个项目的会话'
    return True, '按项目复用会话（不影响全新项目）'


@check('编码通道：完成文件也要过 check_cmd（不信它的自述）')
def _code_done_cross_check():
    module = _read('core/coding_agent.py')
    if 'check_failed_after_done' not in module:
        return False, 'DONE.json 通过时没有复核 check_cmd：等于又回到"信它"'
    return True, '完成文件 + 咕嘎自己的退出码，两个都过才算完成'


@check('编码通道：每轮都要留台账（人能核对，不用信嘴）')
def _code_receipts():
    module = _read('core/coding_agent.py')
    if 'write_receipt' not in module:
        return False, '没有 write_receipt'
    if 'code-receipts.jsonl' not in module:
        return False, '台账文件名不见了'
    if 'write_receipt(' not in _read('ui/agent_runtime.py'):
        return False, '运行时没真的写台账'
    return True, '每轮追加 runtime/code-receipts.jsonl'


@check('编码通道：长任务能中止，且不假装成功')
def _code_cancel():
    module = _read('core/coding_agent.py')
    if 'cancelled' not in module:
        return False, 'CodingAgent 不会返回 cancelled'
    source = _read('ui/agent_runtime.py')
    if "action=='stop'" not in source:
        return False, 'code_task 没有 stop 动作'
    return True, 'cancel → stopped=cancelled，stop 动作已接'


@check('工作流：回放前必须预检（状态变了就不动）')
def _workflow_preflight():
    source = _read('core/action_workflow.py')
    if 'preflight' not in source or 'preflight_failed' not in source:
        return False, '没找到预检门'
    return True, '有 preflight 与 preflight_failed'


@check('工具型工作流：确认闸 + 安全护栏')
def _tool_workflow_guard():
    source = _read('core/super_agent/tool_workflow.py')
    if not source:
        return False, 'tool_workflow.py 不存在'
    for needle in ('confirm_required', 'guarded', 'DANGEROUS_PATTERNS', 'start_at'):
        if needle not in source:
            return False, '缺 %s' % needle
    return True, '四道闸都在'


# --------------------------------------------------------------- 环境
@check('环境：DSH 桥端口在听（51630）', level='warn')
def _bridge_port():
    import socket
    try:
        with socket.create_connection(('127.0.0.1', 51630), timeout=1.5):
            return True, '51630 LISTENING'
    except OSError:
        return False, '51630 没在听（DSH 没装/没起桥；不影响本机其它功能）'


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    selftest = '--self-test' in argv
    if selftest:
        # 证明这台检查器真的会 FAIL：塞一条必然失败的假检查
        @check('自测：这条必须 FAIL（证明检查器没变成橡皮章）')
        def _must_fail():
            return False, '这是故意失败的'
    print('=' * 68)
    print('咕嘎 preflight —— 每条对应一个真踩过的坑')
    print('=' * 68)
    fails = warns = 0
    selftest_tagged = False
    for name, level, fn in CHECKS:
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, '%s: %s' % (type(exc).__name__, str(exc)[:160])
        if ok:
            tag = 'PASS'
        elif level == 'warn':
            tag = 'WARN'
            warns += 1
        else:
            tag = 'FAIL'
            fails += 1
            if name.startswith('自测：'):
                selftest_tagged = True
        print('[%s] %s' % (tag, name))
        print('       %s' % detail)
    print('-' * 68)
    print('结果：%d 项检查，FAIL=%d，WARN=%d' % (len(CHECKS), fails, warns))
    if selftest and not selftest_tagged:
        print('自测失败：故意失败的那条没有被判成 FAIL，说明判定逻辑坏了')
        return 2
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
