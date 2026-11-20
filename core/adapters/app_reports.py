# -*- coding: utf-8 -*-
"""软件自报安装路径的注册表（MCP report_app 工具写、适配器读）。

定位：MCP 只负责“发现/自报路径”，不承担指挥。被适配软件第一次连上咕嘎时
调用 report_app 自报名称、标识、exe 绝对路径、版本和能力；咕嘎把记录写到
``%LOCALAPPDATA%\\GUGA\\app_reports.json``（沿用 guga_paths 的原子写 + 回读校验）。
适配器启动软件时按 用户手动指定 > 自报路径 > 原有探测 的顺序取路径；
自报路径失效（文件被删/移动）时自动回退，并把原因写进 warnings，不会卡死。
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from pathlib import Path

from core import guga_paths

CONTRACT = 'guga.app_report/1'
REPORT_NAME = 'app_reports.json'
APP_ID = re.compile(r'[a-z0-9][a-z0-9._-]{0,39}')
CAPABILITY_LIMIT = 24
_LOCK = threading.RLock()


def report_path() -> Path:
    """自报注册表落点；测试可用 GUGA_APP_REPORTS 指向临时文件。"""
    override = os.environ.get('GUGA_APP_REPORTS', '').strip()
    if override:
        return Path(override).expanduser()
    return Path(guga_paths.BASE) / REPORT_NAME


def normalize_app_id(value) -> str:
    text = str(value or '').strip().casefold()
    return text if APP_ID.fullmatch(text) else ''


def validate_exe(value) -> tuple:
    """返回 (规范化路径, 错误码, 说明)。只接受绝对路径、真实存在、非空的 .exe。"""
    text = str(value or '').strip().strip('"')
    if not text:
        return '', 'missing_exe', '缺少可执行文件绝对路径'
    path = Path(text).expanduser()
    if not path.is_absolute():
        return str(path), 'not_absolute', '可执行文件路径必须是绝对路径：%s' % text
    normalized = os.path.normpath(str(path))
    target = Path(normalized)
    try:
        if not target.is_file():
            return normalized, 'not_file', '文件不存在：%s' % normalized
        if target.suffix.lower() != '.exe':
            return normalized, 'not_exe', '不是 Windows 可执行文件（.exe）：%s' % normalized
        if target.stat().st_size <= 0:
            return normalized, 'empty_exe', '文件为空：%s' % normalized
    except OSError as exc:
        return normalized, 'not_file', '无法读取文件：%s（%s）' % (normalized, str(exc)[:80])
    return normalized, '', ''


def read_all() -> dict:
    path = report_path()
    try:
        data = guga_paths.read_config(default={}, path=path)
    except Exception:
        return dict(contract=CONTRACT, apps={})
    apps = data.get('apps')
    if not isinstance(apps, dict):
        apps = {}
    return dict(contract=str(data.get('contract') or CONTRACT),
                updated_at=str(data.get('updated_at') or ''), apps=apps)


def valid_report(app_id: str) -> dict:
    """读一条自报记录并做可执行校验；失效返回 valid=False + 原因，调用方据此回退。"""
    app_id = normalize_app_id(app_id) or str(app_id or '').strip().casefold()
    record = read_all().get('apps', {}).get(app_id)
    if not isinstance(record, dict):
        return dict(exists=False, valid=False, record=None, exe='', reason='missing',
                    message='没有 %s 的自报路径' % app_id)
    exe, code, detail = validate_exe(record.get('exe'))
    if code:
        return dict(exists=True, valid=False, record=record, exe=exe, reason=code,
                    message=detail or '自报路径不可用')
    return dict(exists=True, valid=True, record=record, exe=exe, reason='',
                message='自报路径可用')


def _clean_capabilities(value):
    if isinstance(value, str):
        value = [item for item in re.split(r'[,\s]+', value) if item]
    if not isinstance(value, (list, tuple)):
        return []
    cleaned = []
    for item in value:
        text = str(item or '').strip()[:80]
        if text and text not in cleaned:
            cleaned.append(text)
        if len(cleaned) >= CAPABILITY_LIMIT:
            break
    return cleaned


def write_report(app_id, name, exe_path, version='', capabilities=None,
                 client_id='', pid=0) -> dict:
    """原子写入并回读校验；校验不通过时返回结构化错误，不落脏记录。"""
    app = normalize_app_id(app_id)
    if not app:
        return dict(ok=False, code='bad_app_id',
                    message='软件标识不合法：只允许小写字母/数字/点/下划线/连字符，最长 40 字符')
    display = str(name or '').strip()[:80]
    if not display:
        return dict(ok=False, code='missing_name', message='缺少软件名称')
    exe, code, detail = validate_exe(exe_path)
    if code:
        return dict(ok=False, code=code, message=detail,
                    hint='请传本机真实存在的 .exe 绝对路径；路径暂时拿不到可以先不调用')
    record = dict(
        app_id=app,
        name=display,
        exe=exe,
        version=str(version or '').strip()[:120],
        capabilities=_clean_capabilities(capabilities),
        client_id=str(client_id or '').strip()[:40],
        pid=int(pid or 0),
        reported_at=time.strftime('%Y-%m-%dT%H:%M:%S'),
        source='mcp_report',
    )
    with _LOCK:
        data = read_all()
        apps = dict(data.get('apps') or {})
        apps[app] = record
        payload = dict(contract=CONTRACT, updated_at=time.strftime('%Y-%m-%dT%H:%M:%S'),
                       apps=apps)
        path = report_path()
        try:
            guga_paths.write_config(payload, path=path)
        except Exception as exc:
            return dict(ok=False, code='write_failed',
                        message='写入自报路径失败：%s' % str(exc)[:160])
        back = read_all()
        saved = (back.get('apps') or {}).get(app)
        if not isinstance(saved, dict) or saved.get('exe') != exe:
            return dict(ok=False, code='verify_failed',
                        message='写入后回读校验失败：%s 里没有匹配的 %s 记录' % (path, app))
    return dict(ok=True, app_id=app, record=record, path=str(path),
                message='已记住 %s 的安装路径，后续直接按这条路径启动' % display)


def delete_report(app_id) -> dict:
    """删除一条自报记录（自测/换机时用；不通过 MCP 暴露给客户端）。"""
    app = normalize_app_id(app_id) or str(app_id or '').strip().casefold()
    with _LOCK:
        data = read_all()
        apps = dict(data.get('apps') or {})
        removed = apps.pop(app, None)
        path = report_path()
        if not removed:
            return dict(ok=False, code='missing', path=str(path),
                        message='没有 %s 的自报记录' % app)
        if apps:
            payload = dict(contract=CONTRACT, updated_at=time.strftime('%Y-%m-%dT%H:%M:%S'),
                           apps=apps)
            guga_paths.write_config(payload, path=path)
        else:
            try:
                path.unlink()
            except OSError as exc:
                return dict(ok=False, code='delete_failed',
                            message='删除自报注册表失败：%s' % str(exc)[:160])
    return dict(ok=True, app_id=app, path=str(path), message='已删除 %s 的自报记录' % app)


def signature() -> str:
    """给 app_registry 缓存失效判断用：文件大小 + 修改时间。"""
    path = report_path()
    try:
        stat = path.stat()
    except OSError:
        return 'missing'
    return '%s:%s:%s' % (stat.st_size, stat.st_mtime_ns, os.path.normcase(str(path)))


if __name__ == '__main__':
    # 手工查看：python -m core.adapters.app_reports [app_id]
    print(read_all() if len(sys.argv) < 2 else valid_report(sys.argv[1]))
