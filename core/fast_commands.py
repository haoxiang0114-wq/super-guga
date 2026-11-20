"""Zero-LLM command router for latency-sensitive, low-ambiguity voice actions."""
import os
import re
import urllib.parse
import time
import unicodedata
import subprocess
import webbrowser
from pathlib import Path


SITE_ALIASES={
    '百度':'https://www.baidu.com','必应':'https://www.bing.com','谷歌':'https://www.google.com',
    'google':'https://www.google.com','b站':'https://www.bilibili.com','哔哩哔哩':'https://www.bilibili.com',
    'bilibili':'https://www.bilibili.com','github':'https://github.com','知乎':'https://www.zhihu.com',
    'youtube':'https://www.youtube.com','微博':'https://weibo.com',
    # 2026-11-12：原来只有上面 9 个站。其余网站（京东、淘宝…）不在表里，
    # 就会被下面的「打开X」当成软件名交给 applications，报
    # 「未找到名为X的软件」。用户说「打开京东」失败、说「打开京东官网」
    # 反而成功（后者含"官网"被排除词挡下，交给了智能体用 browser 打开）。
    # 这里补齐只有网站形态、基本不会被当本机软件的常见站点。
    '京东':'https://www.jd.com','淘宝':'https://www.taobao.com','天猫':'https://www.tmall.com',
    '豆瓣':'https://www.douban.com','掘金':'https://juejin.cn','csdn':'https://www.csdn.net',
    '12306':'https://www.12306.cn','小红书':'https://www.xiaohongshu.com',
    'mdn':'https://developer.mozilla.org','抖音':'https://www.douyin.com','快手':'https://www.kuaishou.com',
    '贴吧':'https://tieba.baidu.com','百度贴吧':'https://tieba.baidu.com','百科':'https://baike.baidu.com',
    '腾讯视频':'https://v.qq.com','爱奇艺':'https://www.iqiyi.com','优酷':'https://www.youku.com',
    'pixiv':'https://www.pixiv.net','twitter':'https://x.com','推特':'https://x.com',
    'stackoverflow':'https://stackoverflow.com','pypi':'https://pypi.org','菜鸟教程':'https://www.runoob.com',
}
APP_ALIASES={
    '记事本':'notepad.exe','计算器':'calc.exe','画图':'mspaint.exe','资源管理器':'explorer.exe',
    '设置':'ms-settings:','终端':'wt.exe','命令提示符':'cmd.exe','任务管理器':'taskmgr.exe',
}
BROWSER_NAMES=('浏览器','默认浏览器','网页浏览器')
STOP_PHRASES=('停止对话','结束对话','停止通话','结束通话','停止语音','结束语音','别说了','不用聊了',
    '闭嘴','闭嘴吧','安静','安静点','别说话','别出声','不要说话','不用说了',
    '退下','退下了','退下吧','你退下吧','你可以退下了','可以先退下了','去休息吧','你可以去休息了',
    '不用陪我了','先聊到这里','今天就聊到这里')


def normalize(text):
    text=unicodedata.normalize('NFKC',str(text or '')).strip().strip('。！？!?，, ')
    return re.sub(r'\s+',' ',text)


def command_text(text):
    value=normalize(text)
    # ASR keeps conversational fillers. Remove only a standalone leading filler,
    # never a negation or a clause describing what somebody else said.
    value=re.sub(r'^(?:(?:嗯+|呃+|额+|哦+|啊+|好的|好吧|好|行)[，,\s]+)+','',value)
    # An explicit correction cancels the first launch and selects the second.
    correction=re.fullmatch(r'(?:先)?(?:不|别|不用|不要)(?:打开|启动)[^，,；;]{1,30}[，,；;]\s*((?:打开|启动).+)',value)
    if correction:value=correction.group(1)
    value=re.sub(r'^(?:(?:不[，,\s]*){1,4}[，,\s]*|不是[，,\s]*)?(?:我的意思是|我是说|我说的是)(?:让你|要你)?[，,\s]*','',value)
    value=re.sub(r'(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])','',value)
    value=re.sub(r'^(?:小?咕嘎)[，,\s]*','',value)
    value=re.sub(r'^(?:(?:你能不能|你能|你可以|你帮我|你给我|请你?|麻烦你?|能不能|可以请你|可以|帮我|给我|替我)[，,\s]*)+','',value)
    value=re.sub(r'^(?:(?:你)?现在)?(?:重新|再)?(?=打开|启动)','',value)
    value=re.sub(r'^((?:打开|启动|访问|前往)(?:一下)?)[，,:：\s]+',r'\1',value)
    return value


def command_key(command):
    if command.get('action') in ('browser_step','desktop_step'):
        return ('desktop_step',command.get('step_action') or command.get('browser_action'),str(command.get('target','')),str(command.get('text','')),command.get('index'))
    target=str(command.get('target','')).casefold()
    if target in BROWSER_NAMES:target='浏览器'
    return (command.get('action'),command.get('kind'),target)


def _open_app(target,computer=None):
    target=target.strip().strip('“”"\' ')
    if target in BROWSER_NAMES:
        import threading
        session=_existing_browser(computer);cancel=threading.Event()
        session.select(cancel,launch_url='about:blank')
        result=session.snapshot(cancel)
        return dict(result,kind='app',target='浏览器',verified_running=True,
            message='已打开默认浏览器，后续会继续使用这个窗口。' if result.get('launch_requested') else '已切到原来的浏览器，继续沿用当前窗口和登录状态')
    alias=APP_ALIASES.get(target)
    if alias:
        os.startfile(alias)
        return dict(kind='app',target=target,message='Windows 已接受启动请求')
    # WorkBuddy 不在 authorized_apps.json 里（那份表只登记了豆包浏览器），
    # 但它有稳定的安装路径与窗口探测，走 agent_launch 比"未找到软件"靠谱。
    if target.strip().casefold() in ('workbuddy','work buddy','wb'):
        from core.agent_launch import ensure
        state=ensure('workbuddy')
        return dict(state,kind='app',target='WorkBuddy',ok=bool(state.get('ok')),
                    message=str(state.get('message') or ''))
    from core.authorized_apps import AuthorizedApps
    result=AuthorizedApps().open(target)
    if result.get('selection_required'):
        raise RuntimeError('找到多个已运行窗口，需要交给内部智能体按当前任务选择；尚未切换窗口')
    return dict(result,kind='app',target=target)


def _browser_executable():
    if os.name!='nt':return None
    # Check fixed install locations only; never walk the user's folders.
    for root in (os.getenv('PROGRAMFILES(X86)'),os.getenv('PROGRAMFILES'),os.getenv('LOCALAPPDATA')):
        if not root:continue
        for relative in ('Microsoft/Edge/Application/msedge.exe','Google/Chrome/Application/chrome.exe'):
            path=Path(root)/relative
            if path.is_file():return path
    return None


def _existing_browser(computer=None):
    global _fast_browser_computer
    if computer is None:
        if '_fast_browser_computer' not in globals():
            from core.computer_control import ComputerControl
            _fast_browser_computer=ComputerControl()
        computer=_fast_browser_computer
    if not computer.access.enabled:raise RuntimeError('完整访问未开启')
    if getattr(computer,'existing_browser',None) is None:
        from core.existing_browser import ExistingBrowser
        computer.existing_browser=ExistingBrowser(computer)
    return computer.existing_browser


def _open_url(url,computer=None):
    import threading
    result=_existing_browser(computer).navigate(url,threading.Event())
    return dict(result,kind='url',target=url)


_TAIL_ONLY=re.compile(r'^(?:(?:一下|吧|好吗|可以吗|行吗|谢谢|呢|啊|哦|呀|然后|接着|再|请)(?:呢|吧|啊|哦|呀)?)?[。！？!?，,\s]*$')


def _needs_agent(remaining):
    """Whether a compound sentence's second clause is real work.

    The fast path may only claim a sentence it can finish. A tail that still
    names an action ("在输入框里输入X", "找到小军") belongs to the agent loop.
    """
    tail=str(remaining or '').strip().strip('。！？!?，, ')
    return bool(tail) and not _TAIL_ONLY.match(tail)


def _is_known_local_app(target):
    """目标是否确实是一个已知的本地软件。

    只读 runtime/chat/authorized_apps.json（几行 JSON），不扫开始菜单或注册表，
    所以可以在路由阶段安全调用。
    """
    value=str(target or '').strip()
    if not value:return False
    if value in APP_ALIASES:return True
    # 咕嘎本来就有一等公民通道的软件（微信、豆包…）。window_inventory 里的
    # 身份表是这些软件的唯一事实来源；不认它们的话，「打开微信」会被下面的
    # 「像网站就交给智能体」规则误伤——而微信恰恰是咕嘎最常操作的本机软件。
    try:
        from core.window_inventory import ALIASES as _APP_IDENTITIES
        if value in _APP_IDENTITIES:return True
    except Exception:
        pass
    try:
        from core.authorized_apps import SCRIPT_ALIASES
        if value in SCRIPT_ALIASES:return True
    except Exception:
        pass
    try:
        from core.authorized_apps import AuthorizedApps
        entries=AuthorizedApps().entries()
    except Exception:
        return False
    folded=value.casefold()
    return any(str(key).casefold()==folded for key in entries)


# 像网站名：2-8 个中文/字母/数字字符，允许间隔号与连字符。
SITE_LIKE=re.compile(r'^[\u4e00-\u9fffA-Za-z0-9·\-]{2,8}$')


def _unresolved_site_candidate(target):
    """看着像网站名、但不是任何已知本地软件的短词。

    2026-11-12：fast path 只该拦它确定能完成的事。原来「打开京东」会被直接判成
    open_app，applications 找不到就报「未找到名为京东的软件」收场——而智能体本来
    能用 browser/web_search 打开 jd.com。用户说「打开京东官网」反而成功，正是因为
    含"官网"被排除词挡下、交给了智能体。
    这里把这类词交还给智能体，让 fast path 不再抢它做不到的活。
    """
    value=str(target or '').strip()
    if not value or len(value)>8:return False
    if not SITE_LIKE.match(value):return False
    if value in SITE_ALIASES or value.casefold() in SITE_ALIASES:return False
    if _is_known_local_app(value):return False
    return True


# ---------------------------------------------------------------------------
# match(): 把一句话路由成一条结构化命令（只解析，不执行任何动作）
#
# 规则按 _MATCH_RULES 的顺序求值，第一条命中即返回；全部不命中时返回
# matched=False。**顺序本身就是优先级**，例如：
#   · 「复合句守卫」必须早于「打开软件」，否则「打开微信，找到小军」会被
#     fast path 抢走，只做前一半然后报告后一半没做；
#   · 「输入法提问」必须早于步骤解析，否则「输入法怎么切换」会被当成要输入的文字。
# 改顺序或改正则都会改变线上行为，改动前先跑 tools/test_fast_commands.py。
# ---------------------------------------------------------------------------


class _CommandText:
    """一句话的三种视图，只算一次，供各条规则复用。"""

    __slots__ = ('text', 'raw', 'value', 'step_source', 'prefix')

    def __init__(self, text):
        self.text = text
        self.raw = str(text)
        # 归一化后的文本：剥掉「帮我」「嗯，」这类口语前缀，后续规则都基于它匹配
        self.value = command_text(text)
        # 步骤解析器要的是原文（未归一化），保持与旧实现一致
        self.step_source = str(text or '')
        self.prefix = dict(source_text=normalize(text))


# --- DSH 直连 -------------------------------------------------------------
_DSH_STATUS = re.compile(r'(?:查看)?\s*DSH\s*(?:进度|任务状态|现在在干什么|正在做什么)[。？！?!]*', re.I)
_DSH_FOLLOW = re.compile(r'关注\s*DSH\s*会话\s+([a-zA-Z0-9_-]+)', re.I)


def _match_dsh_status(ctx):
    if not _DSH_STATUS.fullmatch(ctx.raw.strip()):
        return None
    return dict(matched=True, action='dsh_link', arguments=dict(action='status'),
                source_text=ctx.raw, silent=False)


def _match_dsh_follow(ctx):
    hit = _DSH_FOLLOW.fullmatch(ctx.raw.strip())
    if not hit:
        return None
    return dict(matched=True, action='dsh_link', arguments=dict(action='focus', session_id=hit[1]),
                source_text=ctx.raw, silent=False)


# --- 每日计划 -------------------------------------------------------------
def _match_daily_plan(ctx):
    from core.daily_plan import parse_command
    daily = parse_command(ctx.text)
    if not daily:
        return None
    return dict(matched=True, action='daily_plan', arguments=daily, source_text=ctx.raw, silent=False)


# --- 输入法提问：问的是输入法，不是要输入的文字 ---------------------------
def _match_input_method_question(ctx):
    from core.desktop_interaction import _input_method_question
    if not _input_method_question(ctx.value):
        return None
    return dict(matched=False, action='none', silent=False)


# --- 桌面 / 浏览器步骤 ----------------------------------------------------
_STEP_PRIORITY_ACTIONS = ('list_targets', 'click_index', 'unsupported')


def _match_desktop_or_browser_step(ctx):
    from core.browser_interaction import parse_step
    from core.desktop_interaction import parse_desktop_step
    browser_step = parse_step(ctx.value, ctx.step_source)
    desktop_step = parse_desktop_step(ctx.value, ctx.step_source)
    # 带序号的目标与新增的键盘短语，优先于旧解析器的泛化「点击X」结果
    if desktop_step and desktop_step.get('step_action') in _STEP_PRIORITY_ACTIONS:
        return desktop_step
    if browser_step:
        return dict(desktop_step, action='browser_step', kind='browser') if desktop_step else browser_step
    if desktop_step:
        return desktop_step
    return None


# --- 复合句守卫：后半句是真活，整句交给智能体 ----------------------------
_MULTI_STEP_TAIL = re.compile(
    r'(?:[，,。；;]|然后|接着|并且|并)(?:再|请|帮我|将|把)?[^，,。；;]{0,20}'
    r'(?:写入|写成|保存|发送|创建|整理成|下载|安装)')


def _match_work_tail(ctx):
    if not _MULTI_STEP_TAIL.search(ctx.value):
        return None
    return dict(matched=False, action='none', silent=False)


# --- 多智能体：召唤 / 讨论 ------------------------------------------------
_SUMMON = re.compile(r'(?:召唤|叫出|叫来)\s*(?:咕嘎)?\s*(002|003)')
_NAMED_SUMMON = re.compile(r'(?:召唤|叫出|叫来)\s*(大肥鱼|dora|朵拉)', re.I)
_SUMMON_PAIR_TEXTS = ('召唤两个咕嘎', '叫出两个咕嘎', '召唤002和003', '召唤两个同伴')
_TEAM_DISCUSS = re.compile(
    r'让\s*(001|002|003)\s*(?:和|与|、)\s*(001|002|003)\s*(?:一起)?(?:讨论|聊聊|商量)(?:一下)?\s*(.*)')


def _match_summon(ctx):
    hit = _SUMMON.fullmatch(ctx.value)
    if not hit:
        return None
    return dict(ctx.prefix, matched=True, action='multiagent', kind='summon',
                target=hit.group(1), silent=True)


def _match_named_summon(ctx):
    hit = _NAMED_SUMMON.fullmatch(ctx.value)
    if not hit:
        return None
    target = '002' if hit.group(1).lower() in ('大肥鱼') else '003'
    return dict(ctx.prefix, matched=True, action='multiagent', kind='summon',
                target=target, silent=True)


def _match_summon_pair(ctx):
    if ctx.value not in _SUMMON_PAIR_TEXTS:
        return None
    return dict(ctx.prefix, matched=True, action='multiagent', kind='summon_pair',
                target='002,003', silent=True)


def _match_team_discuss(ctx):
    hit = _TEAM_DISCUSS.fullmatch(ctx.value)
    if not hit:
        return None
    target = hit.group(1) + ',' + hit.group(2)
    topic = hit.group(3).strip(' ：:，,。')
    if topic:
        return dict(ctx.prefix, matched=True, action='multiagent', kind='discuss',
                    target=target, topic=topic, silent=True)
    return dict(ctx.prefix, matched=True, action='multiagent', kind='discuss_ask',
                target=target, silent=True)


# --- 表演：唱歌 / 音乐开关 / 手势 -----------------------------------------
_SINGING_LEAD = re.compile(r'^(?:你能不能|你能|能不能|能|我想听你|我想听|想听你|想听)')
_SINGING_TAIL = re.compile(r'(?:给我听|给我|听听|听|吧|好吗|可以吗)[？?。！!]*$')
_SINGING_REQUESTS = ('唱歌', '唱首歌', '唱一首歌', '来首歌', '来一首歌', '唱一下歌')
_MUSIC_TOGGLE_TEXTS = ('暂停唱歌', '暂停音乐', '暂停一下', '继续唱歌', '继续音乐')
_MUSIC_STOP_TEXTS = ('停止唱歌', '停歌', '别唱了', '停止音乐')
_SING_TITLE = re.compile(r'(?:唱(?:一首|首|一下)?|来一首|播放翻唱)\s*《?(.+?)》?')
_GESTURES = {
    '戴墨镜': 'sunglasses', '戴上墨镜': 'sunglasses', '摘墨镜': 'remove_sunglasses', '摘下墨镜': 'remove_sunglasses',
    '跳舞': 'dance', '摇摆': 'dance', '晃动身体': 'dance', '停止跳舞': 'stop_dance', '停止摇摆': 'stop_dance',
    '暗中观察': 'sneak_peek',
    '打盹': 'idle_doze', '困倦打盹': 'idle_doze', '比心': 'idle_heart', '双手比心': 'idle_heart',
    '比个心': 'idle_heart', '比一下心': 'idle_heart', '给你比心': 'idle_heart', '比心啦': 'idle_heart',
    '嘟嘴': 'idle_pout', '鼓脸': 'idle_pout', '生闷气': 'idle_pout', '抱臂嘟嘴': 'idle_pout',
    '大笑': 'idle_laugh', '仰头笑': 'idle_laugh', '哈哈笑': 'idle_laugh', '开怀大笑': 'idle_laugh',
}


def _match_singing_request(ctx):
    request = _SINGING_LEAD.sub('', ctx.value)
    request = _SINGING_TAIL.sub('', request)
    if request not in _SINGING_REQUESTS:
        return None
    return dict(ctx.prefix, matched=True, action='performance', kind='sing',
                target='晴天', silent=True)


def _match_music_toggle(ctx):
    if ctx.value not in _MUSIC_TOGGLE_TEXTS:
        return None
    kind = 'music_pause' if '暂停' in ctx.value else 'music_resume'
    return dict(ctx.prefix, matched=True, action='performance', kind=kind, target='', silent=True)


def _match_gesture(ctx):
    gesture = _GESTURES.get(ctx.value)
    if not gesture:
        return None
    return dict(ctx.prefix, matched=True, action='performance', kind='gesture',
                target=gesture, silent=True)


def _match_music_stop(ctx):
    if ctx.value not in _MUSIC_STOP_TEXTS:
        return None
    return dict(ctx.prefix, matched=True, action='performance', kind='music_stop',
                target='', silent=True)


def _match_sing_title(ctx):
    hit = _SING_TITLE.fullmatch(ctx.value)
    if not hit or len(hit.group(1)) >= 80:
        return None
    return dict(ctx.prefix, matched=True, action='performance', kind='sing',
                target=hit.group(1).strip('《》'), silent=True)


# --- 结束通话 -------------------------------------------------------------
_STAND_DOWN = re.compile(r'(?:先)?(?:退下|去休息|休息去吧|走吧|走了)(?:了|吧|啦|啊)?')


def _match_stop_call(ctx):
    if ctx.value not in STOP_PHRASES and not _STAND_DOWN.fullmatch(ctx.value):
        return None
    return dict(ctx.prefix, matched=True, action='stop_call', silent=True, priority='critical',
                message='结束实时语音通话')


# --- 屏幕共享 -------------------------------------------------------------
_SHARE_START = re.compile(
    r'(?:开始|打开)?(?:共享|分享)(?:一下)?(?:我的|整个|当前)?屏幕'
    r'(?:[，,]?每秒([2-5两三四五])帧)?(?:吧|一下)?')
_SHARE_STOP = re.compile(r'(?:停止|关闭|结束)(?:一下)?(?:共享|分享)(?:屏幕)?(?:吧|一下)?')
_SHARE_FPS_CN = {'两': 2, '二': 2, '三': 3, '四': 4, '五': 5}


def _share_fps(raw):
    if raw in _SHARE_FPS_CN:
        return _SHARE_FPS_CN[raw]
    return int(raw) if raw else 3


def _match_screen_share_start(ctx):
    hit = _SHARE_START.fullmatch(ctx.value)
    if not hit:
        return None
    return dict(ctx.prefix, matched=True, action='screen_share_start', kind='screen_share',
                fps=_share_fps(hit.group(1)), silent=False, priority='fast')


def _match_screen_share_stop(ctx):
    if not _SHARE_STOP.fullmatch(ctx.value):
        return None
    return dict(ctx.prefix, matched=True, action='screen_share_stop', kind='screen_share',
                silent=False, priority='fast')


# --- 关闭页面 / 浏览器 ----------------------------------------------------
_CLOSE_PAGE = re.compile(
    r'(?:(?:关闭|关掉|关上)(?:一下)?(?:这个|当前|现在|正在看|活动|前台)?(?:的)?(?:浏览器)?(?:页面|网页|标签页|标签)'
    r'|(?:把)?(?:这个|当前|现在|活动|前台)?(?:的)?(?:浏览器)?(?:页面|网页|标签页|标签)(?:给)?(?:关掉|关闭|关上))'
    r'(?:吧|一下|好吗|可以吗)?')
_CLOSE_BROWSER = re.compile(
    r'(?:把)?(默认浏览器|网页浏览器|浏览器|豆包浏览器|edge|chrome|firefox)(?:窗口)?(?:关掉|关闭|退出)(?:吧|一下)?'
    r'|(?:关闭|关掉|退出)(?:一下)?\s*(默认浏览器|网页浏览器|浏览器|豆包浏览器|edge|chrome|firefox)(?:窗口)?(?:吧|一下)?',
    re.I)


def _match_close_page(ctx):
    if not _CLOSE_PAGE.fullmatch(ctx.value):
        return None
    return dict(ctx.prefix, matched=True, action='close_page', kind='close_page',
                target='current_tab', silent=True, priority='fast')


def _match_close_browser(ctx):
    hit = _CLOSE_BROWSER.fullmatch(ctx.value)
    if not hit:
        return None
    target = (hit.group(1) or hit.group(2)).lower()
    return dict(ctx.prefix, matched=True, action='close_browser', kind='close_browser',
                target=target, silent=True, priority='fast')


# --- 窗口级操作（最大化/最小化/还原/关闭）--------------------------------
def _match_window_control(ctx):
    window_kind = _window_control_intent(ctx.value)
    if not window_kind:
        return None
    return dict(ctx.prefix, matched=True, action='window_control', kind=window_kind['kind'],
                target=('浏览器' if window_kind['browser'] else window_kind['kind']),
                silent=True, priority='fast')


# --- 打开软件：复合句 / 倒装句 / 普通句 ----------------------------------
_COMPOUND_OPEN = re.compile(r'^(?:帮我|请)?(?:把|将)?(?:打开|启动)(?:一下)?\s*([^，,。；;？?]{1,20})[，,]\s*(.+)$')
_COMPOUND_TARGET_REJECT = re.compile(r'然后|接着|搜索|网页|网站|http|是什么|为什么|怎么')
_INVERTED_OPEN = re.compile(r'(?:把|将)?\s*(.+?)\s*(?:给)?(?:打开|启动|开一下)(?:一下|吧)?')
_INVERTED_TARGET_REJECT = re.compile(
    r'[，,。;；：:]|[?？]|然后|接着|找到|搜索|点击|写入|保存|关闭')


def _match_compound_open(ctx):
    """「打开微信，找到小军」是一个任务两步骤，fast path 只能claim 它能做完的。"""
    hit = _COMPOUND_OPEN.match(ctx.value)
    if not hit:
        return None
    target = hit.group(1).strip().strip('“”"\' ')
    if target in BROWSER_NAMES:
        target = '浏览器'
    if not (target and len(target) <= 20 and target not in SITE_ALIASES
            and not _COMPOUND_TARGET_REJECT.search(target)):
        return None
    remaining = str(hit.group(2)).strip()[:200]
    if _needs_agent(remaining):
        return dict(ctx.prefix, matched=False, action='none', silent=False)
    return dict(ctx.prefix, matched=True, action='open_app', silent=True, priority='fast',
                kind='app', target=target)


def _match_inverted_open(ctx):
    """倒装：「把微信打开」「将记事本启动」。"""
    hit = _INVERTED_OPEN.fullmatch(ctx.value)
    if not hit:
        return None
    target = re.sub(r'^(?:这个|那个|我的)', '', hit.group(1).strip()).strip('“”"\' ')
    if target in BROWSER_NAMES:
        target = '浏览器'
    if not (target and len(target) <= 20 and target not in SITE_ALIASES
            and not _INVERTED_TARGET_REJECT.search(target)):
        return None
    return dict(ctx.prefix, matched=True, action='open_app', silent=True, priority='fast',
                kind='app', target=target)


# --- 打开网址 -------------------------------------------------------------
_OPEN_SITE = re.compile(
    r'(?:(?:在|用)(?:默认)?浏览器)?(?:打开|启动|访问|前往)(?:一下)?(?:一个)?(?:网页|网站|页面)?\s*(.+)')
_BARE_DOMAIN = re.compile(r'[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?::\d+)?(?:/[^\s\u3400-\u9fff]*)?')
_MAYBE_DOMAIN = re.compile(r'(?:https?://)?[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?::\d+)?(?:/[^\s\u3400-\u9fff]*)?')
_FULL_URL = re.compile(r'https?://[A-Za-z0-9.-]+(?::\d+)?(?:/[^\s\u3400-\u9fff]*)?')
_TARGET_TAIL_NOISE = re.compile(r'(?:一下|好吗|可以吗|吗|吧)$')
_SITE_SUFFIX_NOISE = re.compile(r'(?:这个|那个|的)?(?:网站|官网|网页|主页)$')


def _resolve_open_site(target):
    """把「京东」「b站」「example.com」折算成 (要打开的 URL, 归一化后的目标名)。

    第二个返回值不是附属品：旧实现会在同一个分支里就地把 target 换成归一化后的
    短名，后面的「目标是不是浏览器/已知软件」判断用的是归一化后的值。丢掉它就会
    把「前往记事本一下」判成不匹配。
    认不出 URL 时返回 (None, 归一化目标)。
    """
    candidate = _TARGET_TAIL_NOISE.sub('', target).strip()
    site_candidate = _SITE_SUFFIX_NOISE.sub('', candidate).strip()
    if site_candidate.casefold() in SITE_ALIASES:
        candidate = site_candidate.casefold()
    if candidate in SITE_ALIASES or candidate in APP_ALIASES or candidate in BROWSER_NAMES:
        target = candidate
    elif _MAYBE_DOMAIN.fullmatch(candidate):
        target = candidate
    target = target.casefold() if target.casefold() in SITE_ALIASES else target
    if target in SITE_ALIASES:
        return SITE_ALIASES[target], target
    if _BARE_DOMAIN.fullmatch(target):
        return 'https://' + target, target
    if _FULL_URL.fullmatch(target):
        return target, target
    return None, target


# --- 进入 / 切到 已有浏览器里的站点 ---------------------------------------
# 2026-11-20：「进入浏览器里的B站」「今日B站」以前一条规则都不命中，被当成闲聊，
# 那一轮模型手上没有桌面工具，只能回「这轮没有能操作桌面的工具」——用户看到的
# 就是"和桌面断开了"。这两句本来就是确定能做完的打开动作，fast path 直接接。
_ENTER_SITE = re.compile(
    r'^(?:进入|切到|切换到|回到|跳回)(?:一下)?(?:浏览器(?:里|中|内)?(?:的)?|原来的|原本的)?\s*(.{1,20})$')
_TODAY_SITE = re.compile(r'^(?:看|打开)?(?:今日|今天|首页|推荐|热榜|热搜)(?:的)?\s*(.{1,12})$')


def _match_enter_site(ctx):
    hit = _ENTER_SITE.fullmatch(ctx.value) or _TODAY_SITE.fullmatch(ctx.value)
    if not hit:
        return None
    target = hit.group(1).strip().strip('“”"\' ')
    url, _target = _resolve_open_site(target)
    if not url:
        return None
    return dict(ctx.prefix, matched=True, action='open_url', silent=True, priority='fast',
                kind='url', target=url)


# --- 打开 DSH -------------------------------------------------------------
# 「打开dsh」以前一条规则都不命中，只能交给智能体；2026-11-17 那次它烧了 10 步，
# 翻 applications、netstat、dsh_gateway 才勉强收场。DSH 是本机 web 服务，
# 服务在跑就直接把 3080 打开；没跑就别开死页面，交回去让模型解释。
_OPEN_DSH = re.compile(r'^(?:打开|启动|拉起|切到|进入)\s*dsh(?:\s*(?:网页|web|界面|窗口|服务|首页))?$', re.I)


def _match_open_dsh(ctx):
    if not _OPEN_DSH.fullmatch(ctx.value.strip()):
        return None
    from core.agent_launch import DSH_URL, dsh_running
    if not dsh_running():
        return None
    return dict(ctx.prefix, matched=True, action='open_url', silent=True, priority='fast',
                kind='url', target=DSH_URL)


def _match_open_url(ctx):
    hit = _OPEN_SITE.fullmatch(ctx.value)
    if not hit:
        return None
    target = hit.group(1).strip().strip('“”"\' ')
    url, target = _resolve_open_site(target)
    if url:
        return dict(ctx.prefix, matched=True, action='open_url', silent=True, priority='fast',
                    kind='url', target=url)
    if target in BROWSER_NAMES or target in APP_ALIASES:
        return dict(ctx.prefix, matched=True, action='open_app', silent=True, priority='fast',
                    kind='app', target=target)
    return None


# --- 搜索 -----------------------------------------------------------------
_SEARCH_CMD = re.compile(r'(?:帮我|请)?(?:搜索|搜一下|查一下|查查)\s*(.+)')
_BROWSER_SEARCH = re.compile(
    r'(?:打开|用|在)(?:默认|豆包)?浏览器[，,\s]*'
    r'(?:帮我|然后|并|再|输入|搜索|查询|搜一下|查一下|进行搜索|进行查询|一下|[，,\s])+(.+)')
_SEARCH_VERBS = ('搜索', '查询', '搜一下', '查一下')
_REPORT_TAIL = re.compile(
    r'(?:[，,]?并?汇报(?:一下)?结果|[，,]?告诉我结果|[，,]?进行搜索查询|[，,]?进行搜索)$')
_REPORT_WORDS = ('汇报', '告诉我结果')
_BAIDU_SEARCH = 'https://www.baidu.com/s?wd='


def _baidu_url(query):
    return _BAIDU_SEARCH + urllib.parse.quote(query)


def _match_browser_search(ctx):
    """「打开浏览器帮我搜索X」——浏览器前缀 + 搜索动词，一条命令走完。"""
    hit = _BROWSER_SEARCH.fullmatch(ctx.value)
    if not hit or not any(word in ctx.value for word in _SEARCH_VERBS):
        return None
    query = _REPORT_TAIL.sub('', hit.group(1)).strip('，, 。')
    if not query:
        return None
    report = query if any(word in ctx.value for word in _REPORT_WORDS) else ''
    return dict(ctx.prefix, matched=True, action='web_search', silent=True, priority='fast',
                kind='url', target=_baidu_url(query), report_query=report)


def _match_search(ctx):
    hit = _SEARCH_CMD.fullmatch(ctx.value)
    if not hit:
        return None
    query = hit.group(1).strip()
    if not query or len(query) > 300:
        return None
    return dict(ctx.prefix, matched=True, action='web_search', silent=True, priority='fast',
                kind='url', target=_baidu_url(query))


# --- 打开本机软件 ---------------------------------------------------------
_OPEN_APP = re.compile(r'(?:打开|启动|拉起)(?:一下)?\s*(.+)')
_OPEN_APP_TARGET_REJECT = re.compile(
    r'然后|接着|并且|再帮|并|不要|如果|输入|打字|填写|写入|写一|写个|撰写|保存|发送|搜索|查找|找到|编辑|点击'
    r'|登录|录入|修复|安装|下载|网站|网页|网址|官网|是什么|什么意思|为什么|怎么|如何|会怎样|会怎么样'
    r'|在|后|以后|之后|里面|里|中|新建|创建|建一个|对话|会话|消息|发给|说|问|告诉|帮我|请|一下'
    r'|[，,。;；：:]|[?？]')
_MY_PREFIX = re.compile(r'^(?:我的|我那个|那个|咱的)')


def _match_open_app(ctx):
    hit = _OPEN_APP.fullmatch(ctx.value)
    if not hit:
        return None
    target = _TARGET_TAIL_NOISE.sub('', hit.group(1).strip()).strip()
    # 「打开我的b站」——口语里的「我的」不是软件名的一部分，先剥掉再按站点认
    bare = _MY_PREFIX.sub('', target).strip()
    if bare and bare.casefold() in SITE_ALIASES:
        return dict(ctx.prefix, matched=True, action='open_url', silent=True, priority='fast',
                    kind='url', target=SITE_ALIASES[bare.casefold()])
    if target in BROWSER_NAMES:
        return dict(ctx.prefix, matched=True, action='open_app', silent=True, priority='fast',
                    kind='app', target='浏览器')
    if not (target and len(target) <= 20 and not _OPEN_APP_TARGET_REJECT.search(target)):
        return None
    # 解析已安装软件要留给启动 worker，绝不能在 Qt/语音线程上扫开始菜单
    if _unresolved_site_candidate(target):
        # 不是已知本地软件、又长得像网站名：别抢着当软件启动，交给智能体
        return dict(matched=False, action='none', silent=False)
    return dict(ctx.prefix, matched=True, action='open_app', silent=True, priority='fast',
                kind='app', target=target)


# 顺序 = 优先级，勿随意调整（见本节开头说明）。
_MATCH_RULES = (
    _match_dsh_status,
    _match_dsh_follow,
    _match_daily_plan,
    _match_input_method_question,
    _match_desktop_or_browser_step,
    _match_work_tail,
    _match_summon,
    _match_named_summon,
    _match_summon_pair,
    _match_team_discuss,
    _match_singing_request,
    _match_music_toggle,
    _match_browser_search,
    _match_gesture,
    _match_music_stop,
    _match_sing_title,
    _match_stop_call,
    _match_screen_share_start,
    _match_screen_share_stop,
    _match_close_page,
    _match_close_browser,
    _match_window_control,
    _match_compound_open,
    _match_inverted_open,
    _match_enter_site,
    _match_open_dsh,
    _match_open_url,
    _match_search,
    _match_open_app,
)


def match(text):
    """Parse a high-confidence local action without performing it."""
    ctx = _CommandText(text)
    for rule in _MATCH_RULES:
        result = rule(ctx)
        if result is not None:
            return result
    return dict(matched=False, action='none', silent=False)


WINDOW_CONTROLS=(
    ('maximize',('最大化','窗口最大化','最大化窗口','这个窗口最大化','当前窗口最大化','放到最大','铺满屏幕',
                 '放大窗口','浏览器最大化','最大化浏览器')),
    ('minimize',('最小化','窗口最小化','最小化窗口','这个窗口最小化','当前窗口最小化','收到任务栏','缩到任务栏',
                 '最小化到任务栏','浏览器最小化','最小化浏览器')),
    ('restore',('还原窗口','窗口还原','恢复窗口','还原大小','恢复窗口大小','恢复正常大小','取消最大化',
                '恢复原来大小','恢复原大小','浏览器还原')),
    ('close',('关闭窗口','关掉窗口','关这个窗口','这个窗口关掉','关掉当前窗口','关闭当前窗口','关掉这个',
              '关闭这个','关掉这个窗口')),
)
BROWSER_PROCESSES=('msedge.exe','chrome.exe','firefox.exe','doubao.exe','brave.exe')


def _window_control_intent(value):
    """Recognise window-level verbs that need no accessibility tree at all."""
    text=re.sub(r'^(?:请|帮我|麻烦|把|将|给我|你)+','',value)
    text=re.sub(r'(?:一下|吧|好吗|可以吗|谢谢)$','',text).strip()
    text=text.replace('的','')
    if len(text)>10:return None
    for kind,phrases in WINDOW_CONTROLS:
        if text in phrases:return dict(kind=kind,browser=('浏览器' in text))
    return None


_WINDOW_MEMORY=dict(hwnd=0,at=0.,title='',kind='')


def _window_target(win32gui,win32process):
    """Foreground window when it is a real app; otherwise the window we just touched."""
    now=time.monotonic()
    hwnd=int(win32gui.GetForegroundWindow() or 0)
    if hwnd:
        try:desktop_class=win32gui.GetClassName(hwnd)
        except Exception:desktop_class=''
        if (hwnd==int(win32gui.GetDesktopWindow()) or desktop_class in
                ('Progman','WorkerW','Shell_TrayWnd','#32769','SysListView32')):
            return 0,now,'desktop'
    if hwnd and win32process.GetWindowThreadProcessId(hwnd)[1]==os.getpid():
        last=_WINDOW_MEMORY
        if last['hwnd'] and now-last['at']<10 and win32gui.IsWindow(last['hwnd']):
            return int(last['hwnd']),now,'memory'
        return 0,now,'own'
    return hwnd,now,'foreground'


def _window_state(hwnd,win32con):
    import win32gui
    show=int(win32gui.GetWindowPlacement(hwnd)[1])
    return show==win32con.SW_SHOWMAXIMIZED,show==win32con.SW_SHOWMINIMIZED


def _window_control(kind,browser_only=False):
    """Window state is a system property, never a button to hunt for in UIA."""
    import win32con
    import win32gui
    import win32process
    from core.desktop_access import DesktopAccess
    if not DesktopAccess().enabled:
        return dict(ok=False,code='access_disabled',action_sent=False,error='完整访问未开启；尚未操作窗口')
    hwnd,now,source=_window_target(win32gui,win32process)
    if source=='desktop':
        return dict(ok=False,code='system_window',action_sent=False,error='当前对着的是桌面或任务栏，没有可操作的窗口')
    if source=='own':
        return dict(ok=False,code='own_window',action_sent=False,error='当前前台是咕嘎自己，先切到要操作的窗口再说一次')
    if not hwnd or not win32gui.IsWindow(hwnd):
        return dict(ok=False,code='target_missing',action_sent=False,error='没有取到可操作的窗口；尚未操作')
    if kind=='restore':
        last=_WINDOW_MEMORY
        if now-last['at']<10 and last['hwnd'] and int(last['hwnd'])!=hwnd and win32gui.IsWindow(last['hwnd']):
            # A window minimised a moment ago is what "还原" means, even after focus moved on.
            if _window_state(int(last['hwnd']),win32con)[1]:hwnd=int(last['hwnd'])
    pid=win32process.GetWindowThreadProcessId(hwnd)[1]
    if browser_only:
        try:
            import psutil
            process=psutil.Process(pid).name().lower()
        except Exception:process=''
        if process not in BROWSER_PROCESSES:
            return dict(ok=False,code='not_browser',action_sent=False,error='当前前台不是浏览器，先切到浏览器窗口再说一次')
    title=win32gui.GetWindowText(hwnd)[:40] or '当前窗口'
    _WINDOW_MEMORY.update(hwnd=hwnd,at=now,title=title,kind=kind)
    zoomed,iconic=_window_state(hwnd,win32con)
    if kind=='maximize' and zoomed:
        return dict(ok=True,verified=True,action_sent=False,code='already_maximized',evidence='is_zoomed',message='「'+title+'」已经是最大化了')
    if kind=='minimize' and iconic:
        return dict(ok=True,verified=True,action_sent=False,code='already_minimized',evidence='is_iconic',message='「'+title+'」已经最小化了')
    if kind=='restore' and not zoomed and not iconic:
        return dict(ok=True,verified=True,action_sent=False,code='already_normal',evidence='is_normal',message='「'+title+'」本来就是正常大小')
    try:
        if kind=='maximize':win32gui.ShowWindow(hwnd,win32con.SW_MAXIMIZE)
        elif kind=='minimize':win32gui.ShowWindow(hwnd,win32con.SW_MINIMIZE)
        elif kind=='restore':win32gui.ShowWindow(hwnd,win32con.SW_RESTORE)
        elif kind=='close':win32gui.PostMessage(hwnd,win32con.WM_CLOSE,0,0)
        else:raise ValueError('未知窗口操作')
    except ValueError:raise
    except Exception as exc:
        return dict(ok=False,code='window_action_failed',action_sent=False,error='窗口操作没有发出去：'+type(exc).__name__)
    deadline=time.monotonic()+1.5;verified=False
    while time.monotonic()<deadline:
        zoomed,iconic=_window_state(hwnd,win32con)
        if kind=='maximize' and zoomed:verified=True;break
        if kind=='minimize' and iconic:verified=True;break
        if kind=='restore' and not zoomed and not iconic:verified=True;break
        if kind=='close' and not win32gui.IsWindow(hwnd):verified=True;break
        time.sleep(.03)
    if kind=='close' and not verified:verified=not win32gui.IsWindow(hwnd)
    evidence=dict(maximize='is_zoomed',minimize='is_iconic',restore='is_normal',close='window_gone')[kind]
    if verified:
        return dict(ok=True,verified=True,action_sent=True,code='window_'+kind,evidence=evidence,
            message='「'+title+'」'+(dict(maximize='已最大化',minimize='已最小化',restore='已还原',close='已关闭')[kind]))
    if kind=='close':
        return dict(ok=False,verified=False,action_sent=True,code='close_unverified',evidence=evidence,
            error='已发出关闭请求，但窗口还在；可能有未保存内容或确认提示，请自己看一眼，不会重复关')
    return dict(ok=False,verified=False,action_sent=True,code='window_'+kind+'_unverified',evidence=evidence,
        error='已发出'+dict(maximize='最大化',minimize='最小化',restore='还原')[kind]+'请求，但状态没变；可能被应用自己锁定了窗口大小')


def _close_browser(target):
    import win32gui, win32con
    from core.desktop_access import DesktopAccess
    if not DesktopAccess().enabled:return dict(ok=False,code='access_disabled',action_sent=False,error='完整访问未开启；尚未发送关闭操作')
    from core.window_inventory import list_windows
    from core.browser_launch import default_executable
    names={'edge':'msedge.exe','chrome':'chrome.exe','firefox':'firefox.exe','豆包浏览器':'doubao.exe'}
    windows=list_windows('浏览器',200)
    wanted=names.get(target)
    if wanted:
        windows=[w for w in windows if w.get('process_name','').lower()==wanted]
    if not windows:return dict(ok=False,code='target_missing',error='没有检测到正在运行的目标浏览器；尚未发送关闭操作')
    selected=next((w for w in windows if w.get('foreground')),None)
    if not selected and not wanted:
        default=default_executable()
        exe=Path(str(default)).name.lower() if default else ''
        selected=next((w for w in windows if w.get('process_name','').lower()==exe),None)
    if not selected:
        selected=windows[0] if wanted or len(windows)==1 else None
    if not selected:return dict(ok=False,error='检测到多个浏览器，请指定要关闭哪一个')
    hwnd=selected['hwnd']
    win32gui.PostMessage(hwnd,win32con.WM_CLOSE,0,0)
    for _ in range(15):
        time.sleep(.1)
        if not win32gui.IsWindow(hwnd):return dict(ok=True,verified=True,action_sent=True,evidence='target_window_gone',message='浏览器窗口已关闭')
    return dict(ok=False,code='close_unverified',action_sent=True,error='已请求关闭浏览器，但窗口仍存在，请检查是否有未保存内容或确认提示；本次不会自动重复关闭')


def execute(result,computer=None):
    started=time.perf_counter()
    try:
        answer=_execute(result,computer)
    except Exception as exc:
        answer=dict(ok=False,error=str(exc),code=getattr(exc,'code',None))
    answer['dispatch_ms']=round((time.perf_counter()-started)*1000,2)
    if isinstance(result,dict) and isinstance(result.get('recognized_at'),(int,float)):
        answer['transcript_to_result_ms']=round((time.monotonic()-result['recognized_at'])*1000,2)
    return answer


def _execute(result,computer=None):
    values=dict(result or {})
    if not values.get('matched'):return dict(ok=False,error='没有匹配的快速命令')
    if values.get('action')=='stop_call':return dict(ok=True,message='结束实时语音通话')
    if values.get('action')=='close_page':
        from core.browser_actions import bounded_close
        return bounded_close()
    if values.get('action') in ('screen_share_start','screen_share_stop'):
        return dict(ok=False,code='host_ui_required',error='共享屏幕需要由咕嘎界面控制')
    if values.get('action')=='close_browser':return _close_browser(values['target'])
    if values.get('action')=='window_control':
        return _window_control(values['kind'],browser_only=str(values.get('target',''))=='浏览器')
    if values.get('kind')=='url':
        return _open_url(values['target'],computer)
    if values.get('kind')=='app':
        answer=_open_app(values['target'],computer);answer.setdefault('ok',True);return answer
    return dict(ok=False,error='未知快速动作')


def route(text):
    """Compatibility helper for scripts and tests."""
    parsed=match(text)
    return dict(parsed,execution=execute(parsed)) if parsed.get('matched') else parsed
