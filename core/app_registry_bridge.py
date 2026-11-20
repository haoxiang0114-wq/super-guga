# -*- coding: utf-8 -*-
"""软件路径发现兜底桥 — 纯新增模块，不改本地既有实现。

做什么
----
本地 ``core/authorized_apps.AuthorizedApps.open()`` 只认「已授权清单 + 开始菜单 +
App Paths」。找不到就直接抛 AppResolutionError，模型会卡在"这个软件没有"。

本桥在**本地找不到时**回退到队友的 ``core/adapters/app_registry``（8 路探测：
用户指定路径 / MCP 自报 / 注册表卸载项 / 常见安装路径 / 绿色版目录 / 开始菜单 /
桌面快捷方式 / 进程反查 exe），命中就照常打开。

铁律
----
1. **本地优先**：本地 open() 成功就直接返回，绝不影响现有行为。
2. **只在 AppResolutionError 时兜底**：本地因其它原因报错（路径失效等）照原样抛出。
3. 兜底也失败时，抛回**本地原来那条**错误（带可用软件列表的提示）。
"""
import logging

logger = logging.getLogger(__name__)

_INSTALLED = False


def _try_registry(name):
    """用队友的注册表探测打开软件；成功返回本地格式的 dict，失败返回 None。"""
    try:
        from core.adapters import app_registry
        app_id = app_registry.resolve_app(name)
        if not app_id:
            return None
        result = app_registry.open_app(app_id, verify=False)
    except Exception:
        logger.exception("app_registry fallback failed for %r", name)
        return None

    if not isinstance(result, dict) or not result.get("ok"):
        message = (result or {}).get("message") if isinstance(result, dict) else None
        logger.info("app_registry fallback could not open %r: %s", name, message)
        return None

    logger.info("opened %r via teammate app_registry (app_id=%s)", name, app_id)
    return dict(
        name=name,
        path=result.get("exe") or result.get("path"),
        launch_requested=bool(result.get("started")),
        verified_running=bool(result.get("verified")),
        hwnd=result.get("hwnd") or 0,
        message=result.get("message") or "已通过软件注册表定位并启动",
        source="app_registry",
        app_id=app_id,
    )


def install():
    """给 AuthorizedApps.open 装上兜底。幂等；返回是否本次安装。"""
    global _INSTALLED
    if _INSTALLED:
        return False
    try:
        from core.authorized_apps import AppResolutionError, AuthorizedApps
    except Exception:
        logger.exception("authorized_apps unavailable; registry bridge skipped")
        return False

    original = AuthorizedApps.open
    if getattr(original, "_registry_bridge", False):
        _INSTALLED = True
        return False

    def open_with_fallback(self, name):
        try:
            return original(self, name)
        except AppResolutionError as original_error:
            fallback = _try_registry(name)
            if fallback is not None:
                return fallback
            raise original_error

    open_with_fallback._registry_bridge = True
    AuthorizedApps.open = open_with_fallback
    _INSTALLED = True
    logger.info("app_registry fallback installed")
    return True
