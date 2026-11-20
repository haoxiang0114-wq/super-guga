# -*- coding: utf-8 -*-
"""
咕嘎管家 · 本机环境自检 v0.2
扫描本机可被咕嘎指挥的 AI 软件 + 判定本体版本是否最新
—— 只读探测：不写入任何文件（除自身报告）、不启动任何程序。
（v0.2 起会读一次线上版本号做比对；断网时自动跳过，绝不影响自检本身）
用法：
    python guga_selfcheck.py            # 控制台看报告
结果另存 guga_selfcheck_report.json（写入用户可写目录，安装目录只读时不失败）
"""
import os
import sys
import json
import time

_REPORT_DIR = os.path.join(os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA')
                           or os.path.expanduser('~'), 'GUGA', 'runtime')
try:
    os.makedirs(_REPORT_DIR, exist_ok=True)
except Exception:  # 用户目录也建不出来时退回临时目录，自检照常出结果
    import tempfile
    _REPORT_DIR = tempfile.gettempdir()
REPORT = os.path.join(_REPORT_DIR, 'guga_selfcheck_report.json')
# 云端大脑地址只由用户配置下发（桌面端写入 GUGA_CLOUD），不内置任何端点。
BASE = os.environ.get("GUGA_CLOUD", "").rstrip("/")


def local_version():
    """本机这份代码的版本（与心跳上报的同一个常量，不另立山头）。"""
    try:
        from guga_agent import AGENT_VERSION
        return AGENT_VERSION
    except Exception:
        return ""


def latest_version(timeout=4):
    """线上最新包版本：public/guga-version.txt 随打包生成，是"该给用户哪一版"的唯一口径。
    取不到返回空串——版本提示是附加项，断网不能把自检拖垮。"""
    try:
        import urllib.request
        with urllib.request.urlopen(BASE + "/guga-version.txt", timeout=timeout) as r:
            o = json.loads(r.read().decode("utf-8"))
        return str(o.get("version") or ""), str(o.get("builtAt") or "")
    except Exception:
        return "", ""

def ex(p):
    return os.path.exists(os.path.expandvars(p))

# 常见 AI 软件安装位置（路径探测，不查注册表、不动系统）
TARGETS = [
    ("豆包",        "桌面 AI 助手（已被咕嘎真机验证可指挥）",
     [r"%LOCALAPPDATA%\Doubao\Application\app\Doubao.exe",
      r"%LOCALAPPDATA%\ByteDance\Doubao\app\Doubao.exe"]),
    ("WorkBuddy",   "AI 工作台（已被咕嘎真机验证可指挥）",
     [r"%LOCALAPPDATA%\Programs\WorkBuddy\WorkBuddy.exe"]),
    ("Cursor",      "AI 代码编辑器",
     [r"%LOCALAPPDATA%\Programs\cursor\Cursor.exe"]),
    ("VS Code",     "编辑器（可装 AI 插件）",
     [r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"]),
    ("Chrome",      "浏览器（网页版 AI 的宿主）",
     [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
      r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"]),
    ("Edge",        "浏览器（自带 Copilot）",
     [r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"]),
]

# 隐私策略不纳入的软件（2026-11-05 拍板）。
# 仍然检测，但只用来在报告里说明"是主动不做，不是漏了"——绝不进可指挥清单。
# 理由：读屏式自动化必须截整窗，个人通信软件里必然是私人聊天、群文件、转账、家庭照片，
# 这些与任务无关却会被一并读走。能读不等于该读。
PRIVACY_EXCLUDED = {
    "微信": ("个人通信：读屏会读到与任务无关的私人聊天、群文件和转账记录",
             [r"%ProgramFiles%\Tencent\Weixin\Weixin.exe",
              r"%ProgramFiles%\Tencent\WeChat\WeChat.exe",
              r"%ProgramFiles(x86)%\Tencent\WeChat\WeChat.exe"]),
    "企业微信": ("办公通信：读屏会读到与任务无关的同事私聊和内部文件",
                 [r"%ProgramFiles%\WXWork\WXWork.exe",
                  r"%ProgramFiles(x86)%\WXWork\WXWork.exe",
                  r"%ProgramFiles%\Tencent\WXWork\WXWork.exe"]),
    "QQ": ("个人通信：读屏会读到与任务无关的私人聊天、群文件和转账",
           [r"%ProgramFiles%\Tencent\QQNT\QQ.exe",
            r"%ProgramFiles%\Tencent\QQ\Bin\QQ.exe",
            r"%ProgramFiles(x86)%\Tencent\QQ\Bin\QQScLauncher.exe"]),
}

def _lnk_named(kw):
    """开始菜单里有没有含该关键词的快捷方式（只读，不启动、不解析目标）。
    用来判断"这台机器上有没有这个软件的入口"——比写死安装路径稳，
    因为同一个软件可能装在 D 盘、便携目录或用户目录。"""
    for root in (os.path.join(os.environ.get("ProgramData", ""), r"Microsoft\Windows\Start Menu\Programs"),
                 os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs")):
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if f.lower().endswith(".lnk") and kw in f:
                    return os.path.join(dirpath, f)
    return None


def check_python():
    v = sys.version_info
    return {"ok": v >= (3, 8), "detail": f"Python {v.major}.{v.minor}.{v.micro}"}


def screen_visual():
    """显示与视觉密度体检（只读）：这台机器上"模型能不能看清界面"。
    与执行器 _fit_vis_width 同口径（图宽夹在 800~1600）——满屏窗口送进模型后
    14px 正文还剩几像素。≥7 可靠 / 5~7 勉强（执行器会自动区域放大）/ <5 基本认不出。
    换演示机前先看这一行，比到现场点歪了再猜强。"""
    try:
        import ctypes
        try:                                        # 先声明 DPI，否则拿到的是缩放后的逻辑尺寸
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
        sw = int(ctypes.windll.user32.GetSystemMetrics(0))
        sh = int(ctypes.windll.user32.GetSystemMetrics(1))
        if sw <= 0 or sh <= 0:
            return {"ok": True, "detail": "未取到屏幕尺寸（跳过）"}
        img_w = max(800, min(1600, sw))
        dens = 14.0 * img_w / sw
        if dens >= 7:
            return {"ok": True, "detail": f"{sw}x{sh} · 送模型 {img_w}px · 满屏正文 {dens:.1f}px（可靠）"}
        if dens >= 5:
            return {"ok": True, "detail": f"{sw}x{sh} · 送模型 {img_w}px · 满屏正文 {dens:.1f}px"
                                          f"（勉强，建议别把软件窗口最大化）"}
        return {"ok": False, "detail": f"{sw}x{sh} · 送模型 {img_w}px · 满屏正文 {dens:.1f}px"
                                       f"（偏小，易认错：请把软件窗口拖窄到 1500px 内或调高显示缩放）"}
    except Exception as e:
        return {"ok": True, "detail": f"跳过（{e}）"}


# 开始菜单兜底扫描用的关键词：软件千千万，靠写死安装路径永远只能认出几个，
# 而几乎每款软件都会在开始菜单留快捷方式——按名字筛一遍，识别面立刻打开。
AI_KEYWORDS = (
    "豆包", "doubao", "元宝", "yuanbao", "kimi", "deepseek", "清言", "chatglm", "智谱",
    "通义", "tongyi", "文心", "yiyan", "cursor", "code", "copilot", "claude", "chatgpt",
    "openai", "perplexity", "夸克", "quark", "秘塔", "metaso", "跃问", "阶跃", "minimax",
    "海螺", "天工", "百川", "workbuddy", "wps", "office", "notion", "feishu", "飞书",
    "钉钉", "dingtalk", "剪映", "jianying",
)   # 微信/wechat/weixin 刻意不在列：个人通信按隐私策略不纳入，见 PRIVACY_EXCLUDED


# 快捷方式黑名单：卸载器/帮助/文档这类同名.lnk 绝不能当成"可指挥的软件"派单
LNK_BLOCK = ("卸载", "uninst", "remove", "帮助", "help", "readme", "说明", "文档",
             "documentation", "更新", "update", "检查", "check for", "官方网站", "官网")


def scan_startmenu():
    """扫开始菜单快捷方式，返回 {显示名: lnk 路径}。
    只读：不启动、不改名、不写盘。同名的只留第一个（快捷方式可能重复放）。"""
    roots = [
        os.path.join(os.environ.get("ProgramData", ""), r"Microsoft\Windows\Start Menu\Programs"),
        os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs"),
    ]
    hits = {}
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if not f.lower().endswith(".lnk"):
                    continue
                low = f.lower()
                if not any(k in low for k in AI_KEYWORDS):
                    continue
                if any(b in low for b in LNK_BLOCK):
                    continue
                name = f[:-4].strip()
                if name and name not in hits:
                    hits[name] = os.path.join(dirpath, f)
    return hits


def discovered_apps():
    """未被白名单覆盖、但本机确实装了的软件（开始菜单里认出来的）。"""
    hits = scan_startmenu()
    known = set()
    for name, _role, paths in TARGETS:
        if any(ex(p) for p in paths):
            known.add(name)
    return {n: p for n, p in hits.items() if n not in known}

def run_check():
    """供 guga_agent 配对时程序化调用：返回 {'ai': [名称...], 'python': detail, 'version': x}"""
    rows = []
    for name, role, paths in TARGETS:
        hit = next((os.path.expandvars(p) for p in paths if ex(p)), None)
        rows.append((name, bool(hit)))
    py = check_python()
    ai = [n for n, ok in rows if ok]
    try:                                   # 开始菜单补充识别：让"能指挥的软件"不止那几个白名单
        for n in discovered_apps():
            if n not in ai:
                ai.append(n)
    except Exception:
        pass
    return {
        "ai": ai,
        "python": py["detail"],
        "display": screen_visual()["detail"],
        "version": local_version(),
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def main():
    rows = []
    found = 0
    for name, role, paths in TARGETS:
        hit = next((os.path.expandvars(p) for p in paths if ex(p)), None)
        if hit:
            found += 1
        rows.append({"name": name, "role": role, "found": bool(hit), "path": hit})
    py = check_python()
    sv = screen_visual()
    lv = local_version()
    rv, rbuilt = latest_version()
    extra = discovered_apps()
    prv = []
    for name, (why, paths) in PRIVACY_EXCLUDED.items():
        hit = next((os.path.expandvars(p) for p in paths if ex(p)), None) or _lnk_named(name)
        prv.append({"name": name, "why": why, "installed": bool(hit), "path": hit or ""})
    report = {
        "tool": "guga-selfcheck",
        "version": lv,
        "latest": rv,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "python": py,
        "display": sv,
        "ai_found": found,
        "ai_extra": list(extra.keys()),
        "privacy_excluded": prv,
        "items": rows,
    }
    print("=" * 56)
    print("  咕嘎管家 · 本机环境自检（只读探测，不改任何设置）")
    print("=" * 56)
    print(f"  Python 环境 : {'OK ' if py['ok'] else '缺'}  {py['detail']}")
    print(f"  显示与识别  : {'OK ' if sv['ok'] else '⚠ '} {sv['detail']}")
    print(f"  本体版本    : 本机 v{lv or '未知'} / 线上最新 v{rv or '未取到'}"
          + (f"（{rbuilt} 打包）" if rbuilt else ""))
    if lv and rv and lv != rv:
        print("  ⚠ 本机不是最新包：关掉本体黑窗口 → 网页重下 guga-desktop.zip 解压 → 重新双击启动。")
        print("    （本体进程只认启动那一刻的代码，换包后必须重开，否则跑的还是旧逻辑）")
    elif lv and rv:
        print("  ✅ 本机已是最新包")
    else:
        print("  · 未联网或未取到线上版本，跳过版本比对")
    for r in rows:
        mark = "✅" if r["found"] else "——"
        print(f"  {mark} {r['name']:<10} {r['role']}")
        if r["found"]:
            print(f"        └ {r['path']}")
    for pv in prv:
        # 不管这台机器装没装都显示：这是产品策略，不是本机状态。主动写明"不做"，比让人猜强。
        tail = "（本机已检测到安装）" if pv["installed"] else "（本机未见安装）"
        print(f"  ⛔ {pv['name']:<10} 按隐私策略不接管{tail} —— {pv['why']}")
    if extra:
        print("-" * 56)
        print(f"  另外从开始菜单认出 {len(extra)} 个可用视觉指挥的软件（未逐一真机验证）：")
        for n, p in list(extra.items())[:12]:
            print(f"  ＋ {n:<12} {p}")
        if len(extra) > 12:
            print(f"  ＋ …另有 {len(extra) - 12} 个")
    print("-" * 56)
    print(f"  共识别 {found + len(extra)} 个可指挥的 AI 软件")
    print("  报告已存：", os.path.abspath(REPORT))
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
