# -*- coding: utf-8 -*-
"""管家挂载适配层 — 纯新增模块，不改队友源码，也不改本地 PetWindow。

职责：
  1. attach 前先给 ``pet._ai`` 补齐旧版引擎接口（``core.butler_engine_bridge``）；
  2. 调用队友原生的 ``ui.butler_attach.attach_butler``（PetWindow 零改动）；
  3. 员工切换时决定是否启用员工人设：管家态保持本地原有咕嘎人设，
     召唤部门员工时才切换——「扩展」而非「覆盖」。

用法（main.py）::

    from ui.butler_attach_compat import attach_butler_safe
    attach_butler_safe(window)

关闭：环境变量 ``GUGA_BUTLER=0``。
"""
import logging

logger = logging.getLogger(__name__)

__all__ = ["attach_butler_safe"]


def _open_task_panel(pet, runtime):
    """打开任务台账面板；云端未配置时给提示而不是报错。"""
    try:
        from core.butler_cloud import ButlerCloud
        from ui.task_panel import TaskPanel
    except Exception:
        logger.exception("task panel modules unavailable")
        return

    try:
        cloud = getattr(runtime, "_cloud", None)
        if cloud is None:
            cloud = ButlerCloud(pet)
            runtime._cloud = cloud
        panel = getattr(runtime, "_task_panel", None)
        if panel is None:
            panel = TaskPanel()
            runtime._task_panel = panel
            cloud.state_updated.connect(panel.set_tasks)
        panel.show()
        panel.raise_()

        if not cloud.enabled:
            pet._say_with_expression(
                "少爷，云端还没配置——在 ai_config.json 里填好 cloud 段，我就把台账接上。",
                "surprised")
            return
        if not cloud.logged_in:
            cloud.start()
    except Exception:
        logger.exception("open task panel failed")


def _inject_task_panel_menu(pet, runtime):
    """在桌宠右键菜单追加「管家任务台账」入口（纯追加，不改原有菜单项）。"""
    try:
        menu = pet._ensure_management_menu()
        action = menu.addAction("📋 管家任务台账")
        action.triggered.connect(lambda checked=False: _open_task_panel(pet, runtime))
        logger.info("butler task panel menu injected")
    except Exception:
        logger.exception("task panel menu injection failed")



def _wire_visible_feedback(pet, runtime):
    """给召唤/召回补上「看得见」的反馈。

    队友原版靠 summon_clip 变身动画做反馈，但他 7 张员工卡的 summon_clip 全是
    null、armor 素材只有 2 段占位，所以点菜单时后台确实切了模式，用户却毫无感知。
    这里用本地已有的能力补反馈：气泡台词 + 表情 + 拒绝提示。
    """
    mm = getattr(runtime, "mm", None)
    if mm is None:
        return

    def _pack_of(mode_id):
        try:
            return mm.get_pack(mode_id)
        except Exception:
            return None

    def on_changed(mode_id):
        pack = _pack_of(mode_id)
        name = getattr(pack, "display_name", None) or mode_id
        address = getattr(pack, "address", "少爷")
        if mode_id == "butler":
            text = f"{address}，我回来了。有事随时吩咐。"
            expr = "normal"
        else:
            text = f"{address}，{name}已上岗，请吩咐。"
            expr = "happy"
        try:
            pet._say_with_expression(text, expr)
        except Exception:
            logger.exception("butler bubble feedback failed")
        try:
            pet._anim.set_expression(expr)
        except Exception:
            pass
        logger.info("butler mode -> %s (%s)", mode_id, name)

    def on_rejected(mode_id, reason):
        reason_cn = {
            "switch_in_progress": "上一次切换还没完成",
            "unknown_mode": "没有这张员工卡",
            "already_active": "已经在这个模式了",
            "permission_denied": "权限不足",
        }.get(reason, reason)
        try:
            pet._say_with_expression(f"换不了：{reason_cn}。", "surprised")
        except Exception:
            pass

    try:
        mm.mode_changed.connect(on_changed)
        mm.mode_rejected.connect(on_rejected)
        logger.info("butler visible feedback wired")
    except Exception:
        logger.exception("butler feedback wiring failed")


def attach_butler_safe(pet):
    """安全挂载管家系统；任何失败都只记日志，绝不影响桌宠启动。"""
    from core import butler_engine_bridge as bridge

    if not bridge.butler_enabled():
        logger.info("butler disabled by GUGA_BUTLER=0")
        return None

    try:
        engine = getattr(pet, "_ai", None)
        if engine is not None:
            bridge.ensure_engine_hooks(engine)
    except Exception:
        logger.exception("butler engine hooks failed (continuing)")

    try:
        from ui.butler_attach import attach_butler
        runtime = attach_butler(pet)
    except Exception:
        logger.exception("butler attach failed (continuing without butler)")
        return None

    if runtime is None:
        return None

    try:
        mm = getattr(runtime, "mm", None)
        if mm is not None:
            bridge.set_persona_active(mm.current_id != "butler")
            mm.mode_changed.connect(
                lambda mode_id: bridge.set_persona_active(mode_id != "butler")
            )
    except Exception:
        logger.exception("butler persona wiring failed (persona stays off)")

    _wire_visible_feedback(pet, runtime)

    logger.info("butler attached via compat layer (mode=%s)",
                getattr(getattr(runtime, "mm", None), "current_id", "?"))
    try:
        from PyQt5.QtCore import QTimer
        # 队友在 1.7s 注入「👔 管家与员工」子菜单；台账入口排在其后，保证顺序稳定。
        QTimer.singleShot(1900, lambda: _inject_task_panel_menu(pet, runtime))
    except Exception:
        logger.exception("task panel menu scheduling failed")
    return runtime
