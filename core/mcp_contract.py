"""Shared self-MCP schema and pipe identity, without importing UI or credentials."""
import getpass
import hashlib
import os,re
from pathlib import Path


def pipe_name():
    override=os.environ.get('GUGA_MCP_PIPE','')
    if override and re.fullmatch(r'guga-self-[a-zA-Z0-9-]{1,60}',override):return override
    source = str(Path(__file__).resolve().parents[1]).lower() + getpass.getuser()
    return 'guga-self-' + hashlib.sha256(source.encode('utf-8')).hexdigest()[:20]


def extension_tools():
    """个人超级智能体新增能力：屏幕感知 / 桌面命令 / 灵枢记忆。"""
    return [
        dict(name='screen_context',
             description='实时屏幕感知（内存 only）：查看当前前台窗口与画面变化摘要，或启停 2fps 观察。summary 给出一句大白话。',
             inputSchema=dict(type='object',properties=dict(
                 action=dict(type='string',enum=['status','context','summary','start','stop'])),
                 required=['action'],additionalProperties=False)),
        dict(name='desktop_command',
             description='桌面软件命令总线：list 列出可启动软件，open 拉起或复用软件窗口，focus 切到前台，aliases 查看内置别名。只接受软件名或已授权路径。',
             inputSchema=dict(type='object',properties=dict(
                 action=dict(type='string',enum=['list','open','focus','aliases']),
                 target=dict(type='string'),query=dict(type='string')),
                 required=['action'],additionalProperties=False)),
        dict(name='lingshu_memory',
             description='灵枢（md_cg）统一记忆桥：status 看缓存，recall 后台召回相关记忆，remember 把一条工作结果写成 CCG 合规记忆。非阻塞。',
             inputSchema=dict(type='object',properties=dict(
                 action=dict(type='string',enum=['status','recall','remember']),
                 query=dict(type='string'),title=dict(type='string'),content=dict(type='string')),
                 required=['action'],additionalProperties=False)),
        dict(name='dsh_gateway',
             description='直连本机 DSH web 网关（127.0.0.1:3080，0 重启）：create 新建一个空会话并返回 session_id；start 新建会话并立刻投第一条指令；prompt 向指定会话投递一条用户消息（写操作，必须给出 session_id，不允许猜目标）；status/session 用于查看；cancel 仅中断明确 session_id 的本轮任务并保留会话。要新建 DSH 对话请用 create/start，不要用桌面控制在 DSH 窗口里点按钮。',
             inputSchema=dict(type='object',properties=dict(
                 action=dict(type='string',enum=['status','session','prompt','create','start','cancel']),
                 session_id=dict(type='string'),text=dict(type='string'),
                 mode=dict(type='string',enum=['queue','steer']),query=dict(type='string')),
                 required=['action'],additionalProperties=False)),
        dict(name='workflow',
             description='咕嘎的多步操作工作流库：list 同时列出 UIA 录制型（workflows）与工具调用型（tool_workflows）；play 回放 UIA 型（先自动预检，界面状态不一致就一步都不执行）；plan 对工具调用型做静态校验（工具名/必填参数/人工确认闸），不执行动作；compile 把自然语言长指令编译成 UIA 步骤并保存。',
             inputSchema=dict(type='object',properties=dict(
                 action=dict(type='string',enum=['list','status','play','plan','run','compile']),
                 slug=dict(type='string'),text=dict(type='string'),
                 title=dict(type='string'),triggers=dict(type='string'),
                 allow_confirm=dict(type='boolean'),allow_dangerous=dict(type='boolean'),
                 start_at=dict(type='integer'),dry_run=dict(type='boolean')),
                 required=['action'],additionalProperties=False)),
    ]


def self_tools():
    from core.self_agent import TOOLS
    result=[dict(name=t['function']['name'], description=t['function']['description'],
                 inputSchema=t['function']['parameters'])
            for t in TOOLS if t['function']['name'] in ('app_state', 'pet_control', 'workspace', 'applications', 'computer',
                'browser', 'wechat', 'pet_say', 'daily_plan', 'dsh_task', 'codex_task', 'agent_skills', 'workflow_catalog')]
    result.extend(extension_tools())
    result.append(dict(name='chat_control',description='程序控制咕嘎真实聊天入口：new 新建独立对话，send 经原路由提交消息（request_id 防重复），state 读取所属对话及真实 DSH 回执，stop 中断当前明确对话的任务。用于本机验收；不要让执行中的智能体向自己递归派单。',
        inputSchema=dict(type='object',properties=dict(action=dict(type='string',enum=['new','send','state','stop','reporting','present_result','alignment','board_state']),
            choice=dict(type='integer',minimum=0,maximum=3),supplement=dict(type='string'),capture=dict(type='boolean'),
            backend=dict(type='string',enum=['guga','codex','dsh']),
            goal=dict(type='string'),since=dict(type='number'),
            enabled=dict(type='boolean'),interval_seconds=dict(type='integer'),phase=dict(type='string',enum=['task','processing','submission']),
            work_directory=dict(type='string'),duration_seconds=dict(type='number'),
            name=dict(type='string'),conversation_id=dict(type='string'),text=dict(type='string'),
            request_id=dict(type='string'),limit=dict(type='integer')),required=['action'],additionalProperties=False)))
    result.append(dict(name='voice_session',description='控制咕嘎自己的实时语音会话；start受当前免费额度保护约束，status查看真实状态，stop结束通话；不自动重试。',
        inputSchema=dict(type='object',properties=dict(action=dict(type='string',enum=['start','status','stop'])),
                         required=['action'],additionalProperties=False)))
    result.append(dict(name='screen_share',description='控制当前长通话的屏幕共享。火山 Seed Realtime 按 2 帧/秒发送前台窗口 JPEG，其他平台沿用本地采集及视觉摘要。画面只在内存中保留。',
        inputSchema=dict(type='object',properties=dict(action=dict(type='string',enum=['start','status','stop','context','assist','snapshot']),
            fps=dict(type='integer'),max_age=dict(type='integer')),required=['action'],additionalProperties=False)))
    return result


# 原生 DSH 执行器允许调用的咕嘎工具（A1）。这是模型可见工具面的收敛，不是完整权限隔离：
# app_state 仍需按任务裁剪，workspace/applications 的动作级权限和 IPC 身份授权在 A2 处理。
NATIVE_EXECUTOR_TOOL_NAMES = (
    'app_state',
    'workspace',
    'applications',
    'computer',
    'wechat',
    'desktop_command',
)


def native_executor_tools():
    """原生 DSH 模型可见的执行器工具；schema 直接引用既有定义，不在此重写。

    允许清单之外的工具（聊天控制、再次派单、直接播报等）一律不返回。缺少既有定义时
    直接报错，避免工具面悄悄缩小。控制器用的 self_tools() 不受影响。
    """
    from core.self_agent import TOOLS
    by_name={t['function']['name']:dict(name=t['function']['name'],description=t['function']['description'],
                                         inputSchema=t['function']['parameters']) for t in TOOLS}
    by_name.update({t['name']:t for t in extension_tools()})
    missing=[name for name in NATIVE_EXECUTOR_TOOL_NAMES if name not in by_name]
    if missing:
        raise RuntimeError('原生执行器工具定义缺失: '+', '.join(missing))
    return [by_name[name] for name in NATIVE_EXECUTOR_TOOL_NAMES]


def host_event_tool():
    """宿主内部生命周期回执定义；插件 apply() 必须排除，不注册给模型。"""
    return dict(name='dsh_host_event',description='宿主内部生命周期回执，不向模型注册。',inputSchema=dict(
        type='object',properties={key:dict(type='integer' if key in ('turn','step') else 'string') for key in
        ('session_id','parent_session_id','event_id','kind','state','goal','summary','tool','reason','turn','step')},required=['session_id','kind'],additionalProperties=False))


def plugin_tools():
    """原生 DSH 插件工具面：执行器允许清单 + 仅宿主使用的 dsh_host_event。"""
    return native_executor_tools()+[host_event_tool()]


def dsh_tools():
    from core.self_agent import TOOLS
    allowed={'app_state','pet_control','pet_say','daily_plan','workspace','applications','computer','local_computer','screen_share',
             'browser','wechat','web_search','task_plan','music_performance','dsh_task','codex_task'}
    result=[dict(name=t['function']['name'],description=t['function']['description'],inputSchema=t['function']['parameters'])
            for t in TOOLS if t['function']['name'] in allowed]
    result.extend(extension_tools())
    result.extend([
        dict(name='voice_session',description='控制咕嘎自己的实时语音会话，不用电脑UI。start只在当前配置的免费额度保护通过时连接当前模型；status查看真实状态，stop结束通话。最多一段会话，不自动重试。',
             inputSchema=dict(type='object',properties=dict(action=dict(type='string',enum=['start','status','stop'])),
                              required=['action'],additionalProperties=False)),
        dict(name='media_workflow',description='本地视频处理：检查、抽帧、中文字幕转写、烧录底部进度条和字幕、生成投稿清单。原文件不覆盖，开始处理不等于完成，wait有限等待；没有发布功能。',
             inputSchema=dict(type='object',properties=dict(action=dict(type='string',enum=['inspect','extract_frames','transcribe','render','status','wait','stop','submission']),
                 path=dict(type='string'),subtitles=dict(type='string'),job_id=dict(type='string'),seconds=dict(type='integer'),
                 title=dict(type='string'),description=dict(type='string')),required=['action'],additionalProperties=False)),
        dict(name='personal_memory',description='与咕嘎共享的分层记忆：默认当前对话，项目事实只在本项目共享，稳定用户信息可跨对话。更正用update；不存凭据或推测；每日计划用daily_plan。经验候选不自动启用。',
             inputSchema=dict(type='object',properties=dict(action=dict(type='string',enum=['recall','list','remember','update','forget','propose_lesson','lessons']),
                 scope=dict(type='string',enum=['user','project','conversation']),
                 query=dict(type='string'),content=dict(type='string'),id=dict(type='integer')),required=['action'],additionalProperties=False)),
        dict(name='task_report',description='回报真实进展或交付；ready只是待验收，不代表独立验证成功。',
             inputSchema=dict(type='object',properties=dict(state=dict(type='string',enum=['progress','ready','blocked','failed','needs_input']),
                 summary=dict(type='string'),milestone=dict(type='boolean')),required=['state','summary'],additionalProperties=False))])
    return result
