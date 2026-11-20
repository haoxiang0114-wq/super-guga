# -*- coding: utf-8 -*-
"""
咕嘎管家 · 本机执行方值守程序 v0.6
作用：把您本机和网页上的咕嘎配对成同一账号——网页派"本机执行"的单，
     这里自动领单，真实拉起本机豆包干活，产物回传网页走同一套监工验收。
用法：
    python guga_agent.py <6位配对码>       # 配对码在网页 🩺自检 弹窗里点"生成配对码"
    python guga_agent.py                   # 不带参数会提示输入
安全：
    - 只在收到网页任务时操作本机 AI 软件（豆包），新对话隔离、不碰别的窗口
    - FAILSAFE 开启：鼠标甩到屏幕左上角立即紧急停手
    - Ctrl+C 退出值守；赛后删除整个目录即可，无任何残留
"""
import os
import re
import sys
import json
import time
import threading
import urllib.request
import urllib.error

# 暂停/中止类型：与本机桌宠共用 core.input_guard；独立运行时退回永不命中的占位类
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
try:
    from core.input_guard import Paused as PausedError, Stopped as StoppedError
except Exception:
    class _NeverControl(Exception):
        pass
    PausedError = StoppedError = _NeverControl

# 云端大脑地址只由用户配置下发（桌面端写入 GUGA_CLOUD），不内置任何端点。
BASE = os.environ.get("GUGA_CLOUD", "").rstrip("/")
# 冻结成 exe 时 __file__ 指向临时解包目录 _MEIxxxxx，必须用 sys.executable 才是本体真实位置
if getattr(sys, 'frozen', False):
    HERE = os.path.dirname(os.path.abspath(sys.executable))
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def api(path, data=None, token=None, timeout=20):
    req = urllib.request.Request(BASE + path, method="POST" if data is not None else "GET")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Agent-Token", token)
    body = json.dumps(data).encode("utf-8") if data is not None else None
    if data is not None:
        # 云端 JSON POST 也要保留 POST 重定向：http→https / 补斜杠跳转后
        # urllib 默认会改成 GET，服务端只收 POST 时就会回 405。
        from core.model_endpoint import urlopen_post
        with urlopen_post(req, body, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def log(msg):
    print(f"[咕嘎本体 {time.strftime('%H:%M:%S')}] {msg}", flush=True)


AUX_DEGRADED_HINTS = {
    'CLOUD_NOT_ENTITLED': '云端辅助未开通，已用本地模式继续',
    'CLOUD_QUOTA_EXCEEDED': '云端额度用完，已用本地模式继续',
}


def degraded_hint(code):
    """code 只用于提示文案；不写进任务状态，也不作为失败原因。"""
    return AUX_DEGRADED_HINTS.get(str(code or ''),
                                  '云端辅助不可用，已用本地模式继续')


def consult_degraded(exc):
    """精确规则：HTTP 403 且 body.degraded === true 且 helper === "consult"。

    命中时等价于服务端异常兜底 {"type":"deliverable"}：把当前回复当正文继续交付。
    返回 dict(hit, hint, code, c)；未命中 hit=False。
    """
    try:
        body = json.loads(exc.read().decode("utf-8") or "{}")
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    code = str(body.get("code") or "")
    hit = (getattr(exc, "code", 0) == 403 and bool(body.get("degraded"))
           and str(body.get("helper") or "") == "consult")
    return dict(hit=hit, code=code, hint=degraded_hint(code),
                c={"type": "deliverable"} if hit else {})


def result_payload(job, content, aux_note=""):
    """任务结果：降级只留提示，不把 code 写进状态/失败原因，ok 仍为 True。"""
    payload = {"taskId": job["taskId"], "stId": job["stId"],
               "ok": True, "content": content}
    if aux_note:
        payload["channel_notice"] = aux_note
        payload["note"] = aux_note
    return payload


AGENT_VERSION = "0.15"   # 随心跳上报：下次"跑的是哪一版"不用再靠文件时间猜


def local_caps():
    """本机能力清单：随配对/心跳上报，网页自检据此显示"这台电脑能不能真机干活"。"""
    caps = {"pyver": sys.version.split()[0], "platform": sys.platform, "agent": AGENT_VERSION}
    try:
        import pyautogui, pyperclip, pygetwindow, PIL  # noqa: F401
        caps["deps_ok"] = True
    except Exception:
        caps["deps_ok"] = False   # 只影响真机指挥；配对与心跳纯标准库即可
    return caps


def pair(code):
    import guga_selfcheck as sc
    chk = sc.run_check()
    log(f"本机自检：识别到 {len(chk['ai'])} 个可指挥的 AI 软件：{'、'.join(chk['ai']) or '无'}")
    device = f"{os.environ.get('COMPUTERNAME', 'PC')} · Windows"
    r = api("/api/agent/pair", {"code": code, "device": device, "selfcheck": chk,
                                "path": HERE, "caps": local_caps()})
    log(f"配对成功！已与网页账号「{r['user']}」绑定")
    return r["token"]


def run_job(job, token, on_step=None):
    """领到单：真实指挥本机豆包 → 每轮读完豆包回复先请示大脑 →
    大脑说是交付就交，说是在反问就按大脑拟好的话追问——绝不盲交、绝不死循环。
    视觉层：定位按钮、读回复都先"看"再动（see/locate），失败自动回退旧启发式。
    on_step(round, text)：逐轮台账回调（可选，写失败绝不影响执行）。"""

    def _step(round_no, text):
        if not on_step:
            return
        try:
            on_step(round_no, text)
        except Exception:
            pass

    import guga_executor as dex
    dex.TOKEN = token   # 视觉请示要带执行方身份
    dex.CTX = {"taskId": job["taskId"], "stId": job["stId"]}   # 取证截图随任务上传网页证据墙
    dex.reset_aux_notices()   # 每单从干净状态开始记“云端辅助不可用”
    log(f"执行器 v{dex.EXEC_VERSION}（本体 v{AGENT_VERSION}）就位")
    log(dex.dpi_report())   # 一行体检：显示缩放没对齐这里立刻露馅，不用等点击歪了才发现
    log(dex.vis_report())   # 跨分辨率体检：这台机器"模型能不能看清界面"，换机前先看这行
    import pyperclip
    # 服务端从目标里识别的目标软件（如 WorkBuddy/豆包）——派哪个软件就指挥哪个软件
    app = str(job.get("app") or "").strip() or "豆包"
    log(f"接到任务「{job['name']}」（第 {job['attempt']} 次执行），开始指挥本机{app}……")
    # ---- 路由查表：声明里带适配器的软件，直接交给适配器真机执行 ----
    # 适配器返回失败码时如实报失败并给可行动提示，绝不降级成模型自己写（立身之本）。
    try:
        from core.adapters.dispatch import adapter_for
        app_id, adapter = adapter_for(app)
    except Exception as e:
        app_id, adapter = "", None
        log(f"适配器查表异常（{e}），按执行器流程继续")
    if adapter is not None:
        return _run_with_adapter(adapter, app_id, job, app, token, _step)
    q1 = (f"【总目标】{job['goal']}\n"
          f"请完成其中的子任务「{job['name']}」。\n"
          f"验收要求（必须满足）：{job['acceptance']}\n")
    if job.get("feedback"):
        q1 += f"监工意见（上一版被打回的原因，本次必须修正）：{job['feedback']}\n"
    resume = str(job.get("resume") or "").strip()
    if resume:
        q1 = (f"【接续说明】本任务是暂停后恢复的。暂停前的进度存档：{resume}\n"
              f"请从中断处继续，不要重开话题、不要重做已完成的部分。\n") + q1
    q1 += "信息不足之处请自行合理拟定（自拟主题/自选风格），直接给出完整交付内容，不要反问。"
    tag = time.strftime("local_%H%M%S")
    old_clip = ""
    try:
        old_clip = pyperclip.paste() or ""
    except Exception:
        pass
    deliver, err = "", ""
    # 返回值统一成 {ok, note}：调用方（桌宠内嵌执行方）据此判断这一单到底成没成，
    # 不能再用「函数没抛异常就算成功」这种糊涂账（暂停/失败都会被误记成已完成）。
    result = {"ok": False, "note": ""}
    try:
        # 开工前先把鼠标挪到屏幕中心（两个坑都写在 _move_cursor_to_center 里）
        _move_cursor_to_center()
        w = dex.find_window(app) or dex.launch(app)
        if not w:
            raise RuntimeError(
                f"本机没有打开「{app}」窗口，也未能自动启动（未找到安装位置）——"
                f"请先在本机手动打开{app}再重新派单，或换一个已安装的软件")
        w = dex.focus(w)   # focus 内部会按标题保鲜矩形，返回刷过的窗口对象
        # ---- 开屏感知（KB6）：先"看一眼"界面状态，再决定动作——不是流水线 ----
        # 弹窗挡路→先清；已在新空会话→免隔离直接干；其余→照旧开新对话隔离少爷的会话。
        # 感知失败(unknown/异常)一律回退原流程，知识库故障绝不挡执行。
        sense = {}
        try:
            sense = dex.sense_screen(w, app)
            log(f"开屏感知：{sense.get('state')}（{str(sense.get('note',''))[:48]}）")
            if sense.get('state') == 'dialog_popup':
                if dex.close_popup(w, app, hint=sense.get('question') or ''):
                    sense = dex.sense_screen(w, app)
                    log(f"弹窗已清，复感知：{sense.get('state')}")
        except Exception as e:
            log(f"开屏感知异常（{e}），按原流程走")
        if sense.get('state') == 'empty_chat':
            log("已在新空会话——免开新对话，直接开工")
        else:
            dex.new_chat(w, app)
        # ---- 复合接力（KB7）：云端标了 attachPrev → 把上一步产物落成本地文件喂进去 ----
        q1 = _relay_attach_prompt(job, q1, w, app, dex)
        nxt, rounds = q1, 0
        last_reply = ""
        aux_note = ""   # 云端辅助被拒（403/degraded）时只留痕，不整单失败
        # 追问轮次上限：旧版硬编码 4，长任务常在“豆包反问/分步交付”时被误判失败。
        # 可用环境变量 GUGA_AGENT_ROUNDS 调整（2~12，默认 6）。
        try:
            max_rounds = int(os.environ.get('GUGA_AGENT_ROUNDS') or 6)
        except Exception:
            max_rounds = 6
        max_rounds = max(2, min(12, max_rounds))
        while rounds < max_rounds:
            # 暂停检查点：少爷在网页按了暂停，本轮边界立刻收手上报进度（进度存档可无缝续跑）
            _raise_if_paused(job, token, app, rounds, last_reply, _step)
            rounds += 1
            ok, reply = dex.ask_round(w, nxt, old_clip, tag, rounds, app)
            last_reply = reply or last_reply
            _step(rounds, '第 %d 轮 · %s' % (rounds, (reply or '（无回复）')[:140]))
            if not ok:
                err = f"第 {rounds} 轮未能取到{app}回复（粘贴/超时/复制失败）"
                break
            decision = _consult_round(job, token, reply, app)
            aux_note = decision["aux_note"] or aux_note
            if decision["action"] == "followup":
                nxt = decision["text"]
                continue
            deliver = reply
            break
        else:
            err = f"追问 {max_rounds} 轮仍未拿到完整交付"
        result = _report_outcome(job, token, deliver, err, aux_note, rounds, _step)
    except PausedError as e:
        # 用户叫停不是失败：进度存档后按暂停上报，并向上抛出，让桌宠知道是暂停不是完成
        ctx = f"执行器在安全点停手：{str(e)[:100]}"
        try:
            api("/api/agent/paused", {"taskId": job["taskId"], "stId": job["stId"],
                                      "ctx": ctx}, token, timeout=15)
        except Exception:
            pass
        log(f"已暂停：{ctx}，收手待命")
        raise
    except StoppedError as e:
        try:
            api("/api/agent/result", {"taskId": job["taskId"], "stId": job["stId"],
                                      "ok": False, "aborted": True,
                                      "note": ('已中止：' + str(e)[:160])[:200]}, token)
        except Exception:
            pass
        log(f"已中止：{str(e)[:120]}")
        raise
    except Exception as e:
        try:
            api("/api/agent/result", {"taskId": job["taskId"], "stId": job["stId"],
                                      "ok": False, "note": str(e)[:200]}, token)
        except Exception:
            pass
        result = {"ok": False, "note": '执行异常：%s' % str(e)[:160]}
    finally:
        dex.CTX = None
        try:
            pyperclip.copy(old_clip)   # 恢复剪贴板
        except Exception:
            pass
    return result


def _move_cursor_to_center():
    """开工前把鼠标挪到屏幕中心，避免停在左上角 FAILSAFE 急停区导致首轮操作被急停。

    两个坑，缺一个这段就等于没写：
      1) pyautogui 只在 local_caps() 里导入过，函数作用域内拿不到，必须显式导入；
      2) moveTo 自己会先跑 failSafeCheck（查的是「当前」位置），鼠标已经在角落时
         它直接抛 FailSafeException 拒绝移动——而本函数的目的恰恰是从角落挪出去。
         所以只在这一步临时关掉 FAILSAFE，挪完立刻还原，急停保护全程不受影响。
    归中失败只留痕，绝不挡开工。
    """
    try:
        import pyautogui
        failsafe = pyautogui.FAILSAFE
        pyautogui.FAILSAFE = False
        try:
            width, height = pyautogui.size()
            pyautogui.moveTo(width // 2, height // 2, duration=0.6)
        finally:
            pyautogui.FAILSAFE = failsafe
    except Exception as e:
        # 以前这里是 pass，失败了也看不出来；归中失败会直接导致首轮误触急停，必须留痕。
        log(f"开工前鼠标归中跳过（{e}）——鼠标若停在左上角，首轮操作可能触发急停")


def _relay_attach_prompt(job, prompt, window, app, dex):
    """复合接力（KB7）：云端标了 attachPrev → 把上一步产物落成本地文件喂进去。

    喂不进去不算失败：降级成把产物全文注入提问，活照样干，但如实留痕。
    返回（可能补上了接力说明的）提问文本。
    """
    atext = str(job.get('attachText') or '')
    if not atext:
        return prompt
    import tempfile
    try:
        fpath = os.path.join(tempfile.gettempdir(), f"guga_relay_{job['stId']}.txt")
        with open(fpath, 'w', encoding='utf-8') as f:
            f.write(atext)
        ok_a, msg_a = dex.attach_file(window, app, fpath)
        log(f"接力喂文件：{'✅ ' if ok_a else '⚠️ '}{msg_a}")
        if ok_a:
            return prompt
        # 降级：喂不进去就把产物全文注入提问，活照样干，如实留痕
        return (f"【接力说明】文件喂入未成功，以下为上一步产物全文，请基于它继续：\n"
                f"{atext[:4000]}\n\n") + prompt
    except Exception as e:
        log(f"接力喂文件异常（{e}），降级为文本注入")
        return f"【接力说明】以下为上一步产物全文，请基于它继续：\n{atext[:4000]}\n\n" + prompt


def _raise_if_paused(job, token, app, rounds, last_reply, step):
    """轮边界暂停检查点：少爷在网页按了暂停就存档进度并抛 PausedError，收手待命。

    控制通道本身抖动不算暂停——查不到就照常继续，绝不因为 ctl 超时把单卡死。
    """
    try:
        ctl = api("/api/agent/ctl", {"taskId": job["taskId"]}, token, timeout=15)
    except Exception:
        ctl = {}
    if ctl.get("action") != "pause":
        return
    ctx = (f"已与{app}对话 {rounds} 轮"
           + (f"，最后一轮回复开头：「{last_reply[:80]}」" if last_reply else "")
           + f"；下一步：继续指挥{app}直至拿到满足验收标准的完整交付")
    api("/api/agent/paused", {"taskId": job["taskId"], "stId": job["stId"],
                              "ctx": ctx}, token, timeout=15)
    log(f"少爷已暂停：进度已存档（{ctx}），收手待命")
    step(rounds, '已暂停：%s' % ctx)
    raise PausedError(ctx)


def _consult_round(job, token, reply, app):
    """问大脑：这一轮该继续追问，还是可以直接交付？

    返回 {"action": "followup"|"deliver", "text": 追问话术, "aux_note": 降级提示}。
    请示通道任何波动都降级成「按当前回复交付」，由网页验收兜底——
    绝不因为云端辅助不可用，把一单真实产物判成失败。
    """
    aux_note = ""
    try:
        c = api("/api/agent/consult", {
            "taskId": job["taskId"], "stId": job["stId"],
            "goal": job["goal"], "name": job["name"],
            "acceptance": job["acceptance"], "reply": reply, "app": app,
        }, token, timeout=90)
    except urllib.error.HTTPError as e:
        d = consult_degraded(e)
        if not d["hit"]:
            log(f"大脑请示通道波动（{e}），按当前回复直接交付，由网页验收兜底")
            return {"action": "deliver", "text": "", "aux_note": ""}
        aux_note = d["hint"]
        log('云端辅助降级：helper=consult code=%s at=%s'
            % (d["code"] or '-', time.strftime('%Y-%m-%d %H:%M:%S')))
        log(f"{aux_note}；等价于服务端兜底 deliverable，按当前回复继续交付")
        c = d["c"]
    except Exception as e:
        log(f"大脑请示通道波动（{e}），按当前回复直接交付，由网页验收兜底")
        return {"action": "deliver", "text": "", "aux_note": ""}
    if c.get("type") == "followup" and c.get("text"):
        log(f"大脑判定豆包在反问/未给正文，按大脑代拟的话继续追问：「{c['text'][:40]}…」")
        return {"action": "followup", "text": c["text"], "aux_note": aux_note}
    log("大脑判定这是完整交付，回传网页验收")
    return {"action": "deliver", "text": "", "aux_note": aux_note}


def _report_outcome(job, token, deliver, err, aux_note, rounds, step):
    """把这一单的结局如实回报网页，返回 run_job 要往外传的 {ok, note}。

    有产物 → 回传产物并取验收结果；没产物 → 报失败原因。
    绝不把「没拿到产物」记成完成，也绝不把「被打回」说成通过。
    """
    if deliver:
        payload = result_payload(job, deliver, aux_note)
        r = api("/api/agent/result", payload, token)
        note = ('产物已回传（%d 字）· 网页验收：%s'
                % (len(deliver), '通过' if r.get("pass") else '打回'))
        if aux_note:
            note += ' · ' + aux_note
        step(rounds, note)
        log(f"产物已回传（{len(deliver)} 字）。网页验收结果："
            + ("✅ 通过" if r.get("pass") else "❌ 被监工打回，稍后会领到返工单")
            + ((" · " + aux_note) if aux_note else ""))
        return {"ok": True, "note": note}
    api("/api/agent/result", {"taskId": job["taskId"], "stId": job["stId"],
                              "ok": False, "note": err[:200]}, token)
    log(f"未能拿到完整交付（{err}），已如实回报网页")
    return {"ok": False, "note": err[:200]}


def _adapter_prompt(job, app):
    """把派单任务变成一句能直接丢给目标软件的话。

    例：「使用豆包生成一篇 200 字作文」→「生成一篇 200 字作文。
    验收要求：正文不少于 200 字。」——去掉指派词，剩下的才是要让豆包做的事。"""
    body = str(job.get('goal') or job.get('name') or '').strip()
    body = re.sub(r'^(帮我|给我|帮忙|请|使用|通过|用|叫|让|交给|派给)\s*', '', body)
    body = re.sub(r'^%s\s*' % re.escape(app), '', body)
    acc = str(job.get('acceptance') or '').strip()
    if acc:
        body = '%s\n验收要求：%s' % (body, acc)
    text = body.strip()
    if not text:
        return str(job.get('name') or '')
    # 所有派单入口共用同一个组装函数：固定约束只能追加，不能覆盖任务正文。
    from core.handoff_prompt import compose
    return compose(text, None)


def _run_with_adapter(adapter, app_id, job, app, token, step):
    """适配器直连路径：真实调用目标软件，产物带来源标签回传。

    失败（window_not_found / login_required / blocked_by_window / timeout …）时
    如实回报网页并给可行动提示；这条路径没有任何“模型自己写”的退路。
    """
    import guga_executor as dex
    prompt = _adapter_prompt(job, app)
    log(f"路由命中「{app_id}」适配器 → 真实调用 {app}（不走模型代写）")
    step(0, '路由命中「%s」适配器，真实调用 %s' % (app_id, app))
    started = time.time()
    try:
        res = adapter.ask(prompt, timeout_seconds=180, retry=1, prefer_mcp=True)
    except TypeError:
        res = adapter.ask(prompt)
    except Exception as e:
        res = {"ok": False, "code": "exception", "hint": "适配器异常：%s" % str(e)[:120]}
    seconds = int(time.time() - started)

    if res.get("ok") and str(res.get("text") or "").strip():
        text = str(res["text"]).strip()
        source = res.get("source") or {}
        label = source.get("label") or ("%s（来源标签缺失）" % app)
        notice = str(res.get("notice") or "").strip()
        aux = dex.aux_notice_text()
        if aux:
            notice = (notice + ' · ' + aux).strip(' ·')
        note = '产物已回传（%d 字，%d 秒）· 来源：%s' % (len(text), seconds, label)
        payload = {"taskId": job["taskId"], "stId": job["stId"], "ok": True,
                   "content": text, "source": source, "note": note}
        if notice:
            payload["channel_notice"] = notice[:200]
        try:
            r = api("/api/agent/result", payload, token)
            verdict = '通过' if r.get("pass") else '打回'
        except Exception as e:
            verdict = '回传失败（%s）' % str(e)[:40]
        step(1, '%s · 网页验收：%s' % (note, verdict))
        log('%s；网页验收：%s%s' % (note, verdict, (' · ' + notice) if notice else ''))
        return {"ok": True, "note": '%s · 网页验收：%s' % (note, verdict),
                "source": source, "text": text}

    code = str(res.get("code") or "failed")
    hint = str(res.get("hint") or res.get("message") or "适配器没有返回正文")
    from core import tool_mandate
    note = tool_mandate.failure_message({"name": app}, '%s：%s' % (code, hint))[:200]
    try:
        api("/api/agent/result", {"taskId": job["taskId"], "stId": job["stId"],
                                  "ok": False, "reason": code, "note": note}, token)
    except Exception:
        pass
    step(1, '适配器失败（%s）：%s' % (code, hint[:120]))
    log(note)
    return {"ok": False, "note": note, "reason": code}


def _keepalive(token, stop):
    """干活期间的保活心跳：run_job 一跑十几分钟，主循环不拍心跳，
    服务端会把这台电脑误判成离线（另一端看到"没反应"）。busy=True 只报平安不领新单。"""
    while not stop.is_set():
        try:
            api("/api/agent/beat", {"path": HERE, "caps": local_caps(), "busy": True}, token, timeout=15)
        except Exception:
            pass
        stop.wait(4)


def main():
    # 环境守卫：本脚本是 Windows 程序。若被双击后由 WSL/Linux Python 接管（.py 关联被改），
    # 在这里拦下并给出中文指路，避免用户卡在 WSL 安装或莫名的报错里。
    if sys.platform.startswith('linux') or sys.platform == 'darwin':
        print("=" * 56)
        print("  [!] 检测到这是 Linux/macOS 环境 —— 咕嘎本体是 Windows 程序")
        print("  您可能是直接双击了 .py 文件（被系统用 WSL 打开了）。")
        print("  正确做法：关闭本窗口，回到 guga-desktop 文件夹，")
        print("  双击「2-启动咕嘎本体.bat」即可，无需敲任何命令。")
        print("=" * 56)
        try:
            input("按回车键退出……")
        except Exception:
            pass
        sys.exit(1)
    code = sys.argv[1] if len(sys.argv) > 1 else input("请输入网页上的 6 位配对码：").strip()
    if not code.isdigit():
        print("配对码应是 6 位数字（网页 🩺自检 → 生成配对码）")
        sys.exit(1)
    try:
        token = pair(code)
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("error", "配对失败")
        except Exception:
            msg = f"配对失败 HTTP {e.code}"
        print(msg)
        sys.exit(1)
    print("咕嘎本体开始值守：网页派「本机执行」的单会自动拉起本机 AI 干活")
    print("  · 任务进行中按 Ctrl+C = 打断当前任务（如实上报，值守继续）")
    print("  · 3 秒内连按两次 Ctrl+C 或空闲时按 Ctrl+C = 退出值守；鼠标甩到屏幕左上角 = 紧急停手")
    last_kb = 0.0
    while True:
        job = None
        try:
            b = api("/api/agent/beat", {"path": HERE, "caps": local_caps()}, token)
            job = b.get("job")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                # 配对被顶替/失效不是"暂时不通"——明确告知并退出，不做无声僵尸
                print("本机执行方的配对已失效（同设备重新配对过，或绑定被清除）。")
                print("本进程退出。如需值守：重新双击咕嘎本体，输入网页上的新配对码即可。")
                sys.exit(0)
            log(f"心跳暂时不通（{e}），10 秒后重试")
            time.sleep(10)
            continue
        except Exception as e:
            log(f"心跳暂时不通（{e}），10 秒后重试")
            time.sleep(10)
            continue
        if job:
            keeper = threading.Event()
            threading.Thread(target=_keepalive, args=(token, keeper), daemon=True).start()
            try:
                run_job(job, token)
            except KeyboardInterrupt:
                # 少爷随时有权打断乱抢鼠标的本体：第一下只终结这一单，值守进程绝不能死
                #（否则远程显示"正在派单"而本机早已无人接单，两边都懵）
                if time.time() - last_kb < 5:
                    raise
                last_kb = time.time()
                log("任务已被少爷打断，如实上报后继续值守（3 秒内再按 Ctrl+C 才会退出）")
                try:
                    api("/api/agent/result", {"taskId": job["taskId"], "stId": job["stId"],
                                              "ok": False, "note": "少爷手动中断了本次执行"}, token)
                except Exception:
                    pass
                time.sleep(3)
            except Exception as e:
                # 任何任务级异常都只终结这一单：领单→干活→回报，心跳循环必须活着
                log(f"任务异常退出（{e!r}），已上报，值守继续")
                try:
                    api("/api/agent/result", {"taskId": job["taskId"], "stId": job["stId"],
                                              "ok": False, "note": f"执行异常：{e}"[:200]}, token)
                except Exception:
                    pass
            finally:
                keeper.set()   # 干完活停掉保活线程，主循环心跳接管
        time.sleep(4)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n值守已停止，少爷随时可以再启动。")
