# -*- coding: utf-8 -*-
"""发布前找茬机（preflight）：把"细节问题"变成机器检查，而不是靠人眼看。

跑法：
    python preflight.py

每项输出 PASS / WARN / FAIL。FAIL 表示不能发布，WARN 表示能用但要知道。
这份检查器本身就是上一轮教训的固化——凡是曾经错过的细节，都必须在下面有对应一条。
"""
import os
import re
import io
import sys
import json
import time
import hashlib
import zipfile
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CLOUD = os.path.join(ROOT, "cloud")                   # 开源仓库结构：cloud/ 是云端大脑
if not os.path.isdir(CLOUD):
    CLOUD = os.path.join(ROOT, "guga-server")          # 旧开发结构兼容
PUB = os.path.join(CLOUD, "public")
ZIP = os.path.join(PUB, "guga-desktop.zip")
VER = os.path.join(PUB, "guga-version.txt")
PY = sys.executable

results = []


def rec(level, name, detail):
    results.append((level, name, detail))


def read(p):
    return io.open(p, encoding="utf-8").read()


def main():
    sys.path.insert(0, HERE)
    import guga_agent
    ver = guga_agent.AGENT_VERSION

    # 1. 版本三处同源：代码常量 / 版本清单 / 包内使用说明
    meta = {}
    if os.path.exists(VER):
        meta = json.loads(read(VER))
        if meta.get("version") == ver:
            rec("PASS", "版本·清单与代码一致", f"v{ver}")
        else:
            rec("FAIL", "版本·清单与代码不一致", f"代码 v{ver} vs 清单 v{meta.get('version')}")
    else:
        rec("FAIL", "版本·清单缺失", VER)

    if os.path.exists(ZIP):
        with zipfile.ZipFile(ZIP) as z:
            names = z.namelist()
            # 2. 包内说明文件的版本行
            if "guga-desktop/使用说明.txt" in names:
                t = z.read("guga-desktop/使用说明.txt").decode("utf-8")
                vs = set(re.findall(r"v(\d+(?:\.\d+)+)", t))
                bad = {v for v in vs if v != ver}
                if bad:
                    rec("FAIL", "版本·包内说明写的是旧号", f"说明里出现 {sorted(vs)}，代码是 v{ver}")
                else:
                    rec("PASS", "版本·包内说明一致", f"v{ver}")
            else:
                rec("FAIL", "版本·包内缺使用说明", "guga-desktop/使用说明.txt 不在包里")

            # 3. 包内容白名单 + 无隐私泄漏
            want = {"GUGA-Agent.exe", "1-环境自检.bat", "2-启动咕嘎本体.bat", "使用说明.txt",
                    "guga_agent.py", "guga_executor.py", "guga_selfcheck.py",
                    "workbuddy_executor.py", "requirements.txt"}
            got = {n.split("/", 1)[1] for n in names if n.startswith("guga-desktop/") and "/" in n}
            if got == want:
                rec("PASS", "包内容·9 项齐全", "无多余、无缺失")
            else:
                rec("FAIL", "包内容·与预期不符", f"多 {sorted(got-want)} 缺 {sorted(want-got)}")
            leak = [n for n in names if re.search(r"(^|/)(data|evidence|\.env|sessions|users)", n)]
            rec("FAIL" if leak else "PASS", "包内容·无敏感目录", leak or "干净")

            # 4. 包与清单的 md5/大小对得上
            data = open(ZIP, "rb").read()
            md5 = hashlib.md5(data).hexdigest()
            if meta.get("md5") == md5 and meta.get("size") == len(data):
                rec("PASS", "包·md5 与清单一致", md5[:12])
            else:
                rec("FAIL", "包·md5 与清单对不上", f"清单 {str(meta.get('md5'))[:12]} vs 实际 {md5[:12]}")

            # 5. exe 比版本清单新？说明 exe 重打过但没重新打包
            exe = os.path.join(HERE, "dist", "GUGA-Agent.exe")
            if os.path.exists(exe):
                zt = z.getinfo("guga-desktop/GUGA-Agent.exe").date_time
                if time.mktime(zt + (0, 0, -1)) < os.path.getmtime(exe) - 60:
                    rec("WARN", "包·exe 可能过期", "dist 里的 exe 比包内新，重跑 package_release.py")
                else:
                    rec("PASS", "包·exe 与 dist 同步", "无重打未打包情况")
    else:
        rec("WARN", "包·未生成 guga-desktop.zip",
            "源码仓库不分发 exe 安装包；要做发布包请先跑 package_release.py")

    # 6. public 下不许有备份/证据/数据目录（都会被静态服务直接对外）
    junk = [d for d in os.listdir(PUB)
            if re.search(r"(bak|backup|evidence|^data$|\.orig$)", d, re.I)]
    rec("FAIL" if junk else "PASS", "公网·无备份/数据目录外泄", junk or "干净")

    # 7. 页面与脚本里不许有写死的版本号（除注释）
    stale = []
    for f in ("index.html", "app.js"):
        p = os.path.join(PUB, f)
        if not os.path.exists(p):
            continue
        for i, line in enumerate(read(p).splitlines(), 1):
            if line.lstrip().startswith(("//", "/*", "*")):
                continue
            for m in re.finditer(r"v(\d+\.\d+)", line):
                if m.group(1) != ver:
                    stale.append(f"{f}:{i} → v{m.group(1)}")
    rec("FAIL" if stale else "PASS", "页面·无写死旧版本号", stale or "全部动态渲染")

    # 8. 缓存戳：index.html 引用的 app.js 版本号得是最新的（改过前端就要换）
    try:
        html = read(os.path.join(PUB, "index.html"))
        stamp = re.search(r"app\.js\?v=([a-z0-9]+)", html).group(1)
        js_mtime = time.strftime("%Y%m%d", time.localtime(os.path.getmtime(os.path.join(PUB, "app.js"))))
        if stamp < js_mtime:
            rec("WARN", "前端·缓存戳早于 app.js 修改日", f"{stamp} < {js_mtime}，改了前端记得 bump")
        else:
            rec("PASS", "前端·缓存戳有效", stamp)
    except Exception as e:
        rec("WARN", "前端·缓存戳无法判定", str(e))

    # 9. 本体脚本：关键链路函数齐全 + 软件档案数量
    ex = read(os.path.join(HERE, "guga_executor.py"))
    must = ["find_window", "launch", "focus", "new_chat", "click_input", "send_text", "wait_answer",
            "copy_answer", "read_reply", "verify", "evidence", "ask_round", "run_chain", "see",
            "locate", "refresh_box", "grab", "win_rect"]
    miss = [f for f in must if not re.search(r"^def %s\b" % f, ex, re.M)]
    rec("FAIL" if miss else "PASS", "本体·关键链路函数齐全", miss or f"{len(must)}/{len(must)}")
    apps = len(re.findall(r'^\s{4}"([^"]+)":\s*\{', ex, re.M))
    rec("PASS" if apps >= 19 else "WARN", "本体·软件档案数", f"{apps} 款")

    # 9b. 跨机器口径：DPI 声明必须早于第三方导入，否则等于没声明
    en = re.search(r"^DPI_MODE = _enable_dpi_awareness\(\)", ex, re.M)
    imp = re.search(r"^import pyautogui", ex, re.M)
    if en and imp and en.start() < imp.start():
        rec("PASS", "本体·DPI 声明早于 pyautogui 导入", "顺序正确")
    else:
        rec("FAIL", "本体·DPI 声明早于 pyautogui 导入",
            "必须在 import pyautogui/PIL 之前调用，否则屏幕 DC 已缓存，后面声明无效")

    # 9c. 视觉图宽不能退回写死 800（跨分辨率失效的根）
    if "_fit_vis_width" in ex and re.search(r"VIS_MAX_W\s*=\s*(\d+)", ex):
        mx = int(re.search(r"VIS_MAX_W\s*=\s*(\d+)", ex).group(1))
        hard = re.search(r"def _shot_window\(w, max_w=(\d+)\)", ex)   # 旧签名=写死宽度
        if hard:
            rec("FAIL", "本体·视觉图宽自适应", f"仍是写死 max_w={hard.group(1)}，大分辨率会糊")
        elif mx >= 1200:
            rec("PASS", "本体·视觉图宽自适应", f"上限 {mx}px（小窗仍 800）")
        else:
            rec("WARN", "本体·视觉图宽上限偏小", f"{mx}px，4K 屏会认不出")
    else:
        rec("FAIL", "本体·视觉图宽自适应", "缺 _fit_vis_width / VIS_MAX_W")

    # 9d. 两级定位在（全窗找不到时放大区域再看）
    rec("PASS" if "def locate_deep" in ex and ex.count("locate_deep(") >= 3 else "WARN",
        "本体·两级定位", "全窗→区域放大" if "def locate_deep" in ex else "缺 locate_deep")

    # 9e. 隐私阻断不能被"好心"恢复：微信 / 企业微信 / QQ 已拍板不接管（2026-11-05）
    blocked = set(re.findall(r'"([^"]+)":\s*\{[^}]*"blocked"', ex))
    need = {"微信", "企业微信", "QQ"}
    if need <= blocked and "def _privacy_guard" in ex:
        rec("PASS", "本体·隐私阻断（IM 三款不接管）", "三处 blocked + 闸门在位")
    else:
        rec("FAIL", "本体·隐私阻断（IM 三款不接管）",
            f"缺标记：{sorted(need - blocked)}；须保留 blocked 且 _privacy_guard 在 find_window/launch 前生效")
    try:
        sjs = read(os.path.join(CLOUD, "server.js"))
        if "function isPrivacyGoal" in sjs and sjs.count("isPrivacyGoal(") >= 2:
            rec("PASS", "服务端·隐私阻断", "派单入口先拦，不静默滑走")
        else:
            rec("FAIL", "服务端·隐私阻断", "dispatchGoal 未调用 isPrivacyGoal（会落到云端代写冒充完成）")
    except Exception as e:
        rec("WARN", "服务端·隐私阻断无法判定", str(e))

    # 10. 语法
    r = subprocess.run(["node", "--check", os.path.join(CLOUD, "server.js")],
                       capture_output=True)
    rec("PASS" if r.returncode == 0 else "FAIL", "语法·server.js", "ok" if r.returncode == 0 else r.stderr.decode()[:120])
    r = subprocess.run([PY, "-m", "py_compile", os.path.join(HERE, "guga_executor.py")], capture_output=True)
    rec("PASS" if r.returncode == 0 else "FAIL", "语法·guga_executor.py", "ok" if r.returncode == 0 else r.stderr.decode()[:120])

    # 11. 单测：几何边界 + 跨分辨率换算
    #     缺依赖时记 WARN 而不是 FAIL——这不是代码错，是这台机器还没 pip install
    def _unit(script, name):
        r = subprocess.run([PY, os.path.join(HERE, script)], capture_output=True)
        out = (r.stdout + r.stderr).decode("utf-8", "ignore")
        if "ModuleNotFoundError" in out:
            m = re.search(r"No module named '([^']+)'", out)
            rec("WARN", name, f"未安装运行依赖 {m.group(1) if m else ''}，先 pip install -r requirements.txt")
            return
        ok = r.returncode == 0 and "全部通过" in out
        rec("PASS" if ok else "FAIL", name, "全部通过" if ok else f"有失败，见 {script}")

    _unit("test_clip_zone.py", "单测·几何边界 14 项")
    _unit("test_vis_scale.py", "单测·跨分辨率换算")

    # 12. 语音包齐全（规格问题归队友，这里只查在不在）
    vd = os.path.join(PUB, "voice")
    need = {"hello", "idle", "dispatch", "urge", "return", "pass", "party"}
    have = {f[:-4] for f in os.listdir(vd) if f.endswith(".mp3")} if os.path.isdir(vd) else set()
    rec("FAIL" if need - have else "PASS", "语音·7 条齐全", f"缺 {sorted(need-have)}" if need - have else "7/7")

    # 13. 文档是否跟上了当前版本（只要求"每份文档至少出现一次当前版本号或当前 md5"，
    #     不追究历史段落里的旧值——文档本来就是按时间累加的）
    docs = os.path.join(CLOUD, "docs")
    cur = meta.get("md5", "")
    stale_docs = []
    for f in os.listdir(docs) if os.path.isdir(docs) else []:
        if not f.endswith(".md"):
            continue
        txt = read(os.path.join(docs, f))
        if not re.search(r"v\d+\.\d+|md5", txt):
            continue
        if (cur and cur in txt) or re.search(r"v%s\b" % re.escape(ver), txt):
            continue
        stale_docs.append(f)
    rec("WARN" if stale_docs else "PASS", "文档·已记录当前版本", stale_docs or "全部同步")

    # 输出
    w = max(len(n) for _, n, _ in results) + 2
    print("=" * (w + 46))
    print("  咕嘎发布前找茬机")
    print("=" * (w + 46))
    for lv, n, d in results:
        mark = {"PASS": "✅", "WARN": "🟡", "FAIL": "❌"}[lv]
        print(f" {mark} {n:<{w}} {d}")
    f = sum(1 for l, _, _ in results if l == "FAIL")
    wn = sum(1 for l, _, _ in results if l == "WARN")
    print("-" * (w + 46))
    print(f"  共 {len(results)} 项：PASS {len(results)-f-wn} · WARN {wn} · FAIL {f}")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
