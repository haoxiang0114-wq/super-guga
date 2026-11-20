# -*- coding: utf-8 -*-
"""建议安装的外部工具：只认 runtime/local_tools.json 的真实探测结果。

- 已安装（exists=True）或当前正在运行的工具不再推荐；
- 没有清单时用 local_tools.scan(launch_probe=False) 做一次轻量探测
  （只查 exe/开始菜单快捷方式，不启动软件、不抢前台）；
- “以后不再提示”状态单独存 runtime/install_suggestions.json，
  不写进 local_tools 缓存，也不改任何业务配置。
"""
import json
import time
from pathlib import Path

from core import guga_paths, local_tools

ROOT = Path(__file__).resolve().parents[1]
PREF = guga_paths.RUNTIME / 'install_suggestions.json'

# 只推荐比赛里适配度最高的三项；JoyCode 必须写。
TOOLS = (
    dict(
        id='joycode', name='JoyCode', badge='必装',
        gains='代码类任务的施工方：收件箱派单 → 自动唤起并提交 → 回执回传，'
              '网站、脚本、小工具都能真正落地。',
        missing='代码类任务只能自己写或复制到别的 IDE，跨工具链会断在最后一步。',
        install='默认需装在 D:\\JoyCode；装在别处时设置环境变量 '
                'GUGA_JOYCODE_LAUNCHER 指向 JoyCode.exe。装好后登录并保持可启动。',
    ),
    dict(
        id='workbuddy', name='WorkBuddy', badge='推荐',
        gains='把一句话需求扩写成高质量提示词，并把回答全文抓回来交给下游工具，'
              '是跨工具链的起点。',
        missing='跨工具链少了提示词环节，直接派给代码工具时成品质量会下降；'
                '桌面、聊天、本地文档/视频不受影响。',
        install='安装 WorkBuddy 桌面端并登录；默认路径 '
                '%LOCALAPPDATA%\\Programs\\WorkBuddy\\WorkBuddy.exe。',
    ),
    dict(
        id='doubao', name='豆包', badge='推荐',
        gains='界面指挥适配最广：写作、生图、资料整理等通用任务，'
              '咕嘎可以直接在窗口里指挥。',
        missing='少一块通用 AI 助手能力；桌宠、聊天、本地 PPT/视频兜底仍然可用。',
        install='官网 www.doubao.com 下载桌面端并登录，保持窗口可被找到。',
    ),
)

_SPECS = {spec['id']: spec for spec in local_tools.APPS}


_MEMO = {'tools': None}


def _read_inventory():
    """每次启动都重新探测（缓存可能过期：用户刚装/刚卸载都会判错）。

    只查 exe 与开始菜单快捷方式，不启动软件、不抢前台；单进程内只做一次。
    """
    if _MEMO['tools'] is not None:
        return _MEMO['tools']
    try:
        data = local_tools.scan(launch_probe=False)
    except Exception:
        data = None
        if local_tools.CACHE.exists():
            try:
                data = json.loads(local_tools.CACHE.read_text(encoding='utf-8'))
            except Exception:
                data = None
    _MEMO['tools'] = (data or {}).get('tools') or []
    return _MEMO['tools']


def installed_ids():
    """真实已安装的工具 id 集合（按 exe/快捷方式探测，叠加“当前正在运行”）。"""
    installed = set()
    for tool in _read_inventory():
        if tool.get('exists'):
            installed.add(tool.get('id'))
    for tool in TOOLS:
        spec = _SPECS.get(tool['id'])
        if not spec:
            continue
        try:
            if local_tools._windows_of(spec):
                installed.add(tool['id'])
        except Exception:
            pass
    return installed


def missing_tools():
    installed = installed_ids()
    return [dict(tool) for tool in TOOLS if tool['id'] not in installed]


def installed_names():
    installed = installed_ids()
    return [tool['name'] for tool in TOOLS if tool['id'] in installed]


def _prefs():
    try:
        return json.loads(PREF.read_text(encoding='utf-8'))
    except Exception:
        return {}


def is_dismissed():
    return bool(_prefs().get('dismissed'))


def should_show():
    """未选择“不再提示”时，最多每 24 小时弹一次，避免每次启动都堵一下。"""
    data = _prefs()
    if data.get('dismissed'):
        return False
    last = data.get('last_shown')
    if not last:
        return True
    try:
        shown = time.mktime(time.strptime(str(last), '%Y-%m-%d %H:%M:%S'))
    except (ValueError, OverflowError):
        return True
    return (time.time() - shown) > 24 * 3600


def mark_shown():
    """记录本次弹出时间（保留已有 dismissed 状态）。"""
    try:
        PREF.parent.mkdir(parents=True, exist_ok=True)
        data = _prefs()
        data.update(last_shown=time.strftime('%Y-%m-%d %H:%M:%S'),
                    missing=[tool['id'] for tool in missing_tools()])
        temporary = PREF.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(PREF)
        return True
    except Exception:
        return False


def dismiss():
    try:
        PREF.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(dismissed=True,
                       dismissed_at=time.strftime('%Y-%m-%d %H:%M:%S'),
                       missing=[tool['id'] for tool in missing_tools()])
        temporary = PREF.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(PREF)
        return True
    except Exception:
        return False
