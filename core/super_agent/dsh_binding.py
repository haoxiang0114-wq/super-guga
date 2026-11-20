# -*- coding: utf-8 -*-
"""咕嘎 × DSH 闭环 · 认准「同一个 DSH」（契约第 2 节）。

为什么需要它
------------
本机同时可能有两类 DSH 进程：

1. **桌面 web 实例**：`dsh web`，默认监听 127.0.0.1:3080，有 GUI、有会话列表，
   主人正看着它 —— 咕嘎派单应该进这里；
2. **headless sidecar**：`core/dsh_worker.py` 用 `dsh --profile guga` 起的一次性
   任务进程，跑完就退，没有 GUI、没有会话列表。

两者混在一起会让咕嘎把任务投进一个没人看的进程。本模块负责：
探测当前真实监听 3080 的那个实例（pid 取自端口归属，不靠猜）、算一个稳定指纹、
把「认准了谁」持久化到 `runtime/dsh-binding/binding.json`，并在每次使用前实测
`verify()` 是否还是同一个实例。

安全约束（重要）
----------------
launch token / operator cookie 是本机 owner 级凭据：
**只驻留内存，绝不写磁盘、绝不写日志**。`binding.json` 只存指纹与非敏感字段
（port / pid / profile / session_id / bound_at / fingerprint / dsh_home）。
本模块所有对外返回的错误串都经过 `_redact()`，不会带出 token 或 cookie 原文。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:  # 同包内相对导入；被当脚本直接跑时退回绝对导入
    from .dsh_gateway import DshGateway, discover_launch_token
except ImportError:  # pragma: no cover - 脚本模式
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from core.super_agent.dsh_gateway import DshGateway, discover_launch_token

try:
    import psutil
except ImportError:  # pragma: no cover - 无 psutil 时走 netstat 兜底
    psutil = None

DEFAULT_PORT = 3080
# 桌面实例还挂着一个 MCP 桥（插件 dsh-as-mcp-server / id dsh-mcp-bridge）。
# 它是**同一个进程**的另一个口：两个端口的监听 pid 相同 = 同一个 DSH 的最硬证据。
MCP_BRIDGE_PORT = 51630
DSH_HOME = Path.home() / '.dsh'
PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = PROJECT_ROOT / 'runtime' / 'dsh-binding'
BINDING_FILE = RUNTIME_DIR / 'binding.json'
BINDING_VERSION = 1

# 桌面实例 / sidecar 的判定口径（来自实际命令行，不猜）。
DESKTOP_KIND = 'desktop-web'
SIDECAR_KIND = 'headless-sidecar'
UNKNOWN_KIND = 'unknown'


# --------------------------------------------------------------------------- 工具
def _now() -> str:
    """带本地时区的 ISO 时间戳（人读得懂，也便于比对先后）。"""
    return datetime.now().astimezone().isoformat(timespec='seconds')


def _redact(text: str) -> str:
    """错误串里永远不带 cookie / token 原文。"""
    cleaned = re.sub(r'(dsh-auth[^=]*=)[^;,\s]+', r'\1<redacted>', str(text))
    cleaned = re.sub(r'(token=)[^\s&,;]+', r'\1<redacted>', cleaned, flags=re.I)
    return cleaned


def _atomic_write_json(path: Path, payload: dict) -> None:
    """先写临时文件再替换，避免半截 JSON 被读走。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def _load_yaml(text: str):
    """容忍 `!!js` 自定义标签的 YAML 载入（DSH 组合配置里大量使用）。"""
    try:
        import yaml
    except Exception:  # pragma: no cover - 没有 PyYAML 就不解析 provider 路由
        return None

    class _JsValue(str):
        """`!!js <表达式>`：只是占位，绝不当真值使用。"""

    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_constructor('tag:yaml.org,2002:js', lambda loader, node: _JsValue(node.value))
    try:
        return yaml.load(text, Loader=_Loader)
    except Exception:  # noqa: BLE001 - 配置不合法时按「取不到」处理
        return None


def _walk_dicts(node):
    """深度遍历 YAML 结构里的所有 dict。"""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_dicts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_dicts(value)


# --------------------------------------------------------------------- 进程/端口
def _listen_pids(ports) -> dict:
    """一次扫出多个端口的 LISTEN pid：{port: pid}。pid 来自端口归属，不靠命令行猜。"""
    wanted = {int(port) for port in ports if port}
    found = {}
    if not wanted:
        return found
    if psutil is not None:
        try:
            for conn in psutil.net_connections(kind='tcp'):
                if conn.status != psutil.CONN_LISTEN:
                    continue
                laddr = conn.laddr
                port = int(getattr(laddr, 'port', 0) or 0) if laddr else 0
                if port in wanted and conn.pid:
                    found[port] = int(conn.pid)
            if found:
                return found
        except Exception:  # noqa: BLE001 - 权限不足时退回 netstat
            pass
    try:
        out = subprocess.run(['netstat', '-ano', '-p', 'TCP'], capture_output=True,
                             timeout=15, text=True, encoding='utf-8', errors='replace')
        for line in (out.stdout or '').splitlines():
            parts = line.split()
            if len(parts) < 5 or 'LISTENING' not in parts[3].upper():
                continue
            for port in wanted:
                if parts[1].endswith(':%d' % port):
                    found[port] = int(parts[4])
    except Exception:  # noqa: BLE001 - 读不到端口归属时返回已找到的部分
        return found
    return found


def _listen_pid(port: int) -> tuple:
    """返回 (pid, error)。pid 必须来自「谁在 LISTEN 这个端口」。"""
    port = int(port)
    pid = _listen_pids([port]).get(port, 0)
    if pid:
        return pid, ''
    return 0, '端口 %d 上没有监听进程（DSH 可能没在跑）' % port


def cross_check(primary_port: int = DEFAULT_PORT, bridge_port: int = MCP_BRIDGE_PORT) -> dict:
    """交叉校验：3080（Typert RPC）与 51630（MCP 桥）是不是同一个进程。

    两个口同 pid ⇒ 确实是同一个桌面实例；拿不到就降级为「无法交叉校验」，不报错。
    """
    result = dict(available=False, port_3080_pid=0, port_51630_pid=0,
                  same_process=None, bridge_port=int(bridge_port), reason='')
    try:
        pids = _listen_pids([primary_port, bridge_port])
        primary_pid = int(pids.get(int(primary_port)) or 0)
        bridge_pid = int(pids.get(int(bridge_port)) or 0)
        result['port_3080_pid'] = primary_pid
        result['port_51630_pid'] = bridge_pid
        if not primary_pid or not bridge_pid:
            missing = []
            if not primary_pid:
                missing.append(str(primary_port))
            if not bridge_pid:
                missing.append(str(bridge_port))
            result['reason'] = '端口 %s 没有监听进程，无法交叉校验' % '/'.join(missing)
            return result
        result['available'] = True
        result['same_process'] = primary_pid == bridge_pid
        return result
    except Exception as exc:  # noqa: BLE001 - 交叉校验失败不应让 verify 崩
        result['reason'] = _redact(str(exc))[:160]
        return result


def _profile_from_cmdline(cmdline: list) -> str:
    """从命令行推 profile：`--profile X` 优先，其次 `dsh web` / `dsh headless`。"""
    low = [str(item).lower() for item in cmdline]
    if '--profile' in low:
        index = low.index('--profile')
        if index + 1 < len(cmdline):
            return str(cmdline[index + 1])
    for token in low:
        base = token.replace('\\', '/').rsplit('/', 1)[-1]
        if base in ('web', 'headless', 'tui'):
            return base
    return ''


def _kind_of(profile: str, cmdline: list) -> str:
    """桌面 web 实例 vs headless sidecar。"""
    profile = str(profile or '').lower()
    low = ' '.join(str(item).lower() for item in cmdline)
    if profile == 'web' or re.search(r'\bweb\b', low):
        return DESKTOP_KIND
    if profile in ('guga', 'headless') or 'headless' in low:
        return SIDECAR_KIND
    return UNKNOWN_KIND


def _proc_info(pid: int) -> dict:
    """进程的真实命令行 / profile / 类型；读不到就返回空，不猜。"""
    info = dict(pid=int(pid), cmdline='', profile='', kind=UNKNOWN_KIND, name='')
    if not pid:
        return info
    try:
        if psutil is not None:
            proc = psutil.Process(int(pid))
            cmdline = proc.cmdline() or []
            info['name'] = proc.name() or ''
        else:  # pragma: no cover - 无 psutil 时用 CIM 查询
            cmdline = _cim_cmdline(pid)
            info['name'] = ''
        info['cmdline'] = _redact(' '.join(cmdline))[:400]
        info['profile'] = _profile_from_cmdline(cmdline)
        info['kind'] = _kind_of(info['profile'], cmdline)
    except Exception:  # noqa: BLE001 - 进程已退出 / 权限不足
        pass
    return info


def _cim_cmdline(pid: int) -> list:  # pragma: no cover - 仅无 psutil 时使用
    try:
        out = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             "(Get-CimInstance Win32_Process -Filter 'ProcessId=%d').CommandLine" % int(pid)],
            capture_output=True, timeout=15, text=True, encoding='utf-8', errors='replace')
        return [out.stdout.strip()] if out.stdout.strip() else []
    except Exception:  # noqa: BLE001
        return []


def _dsh_processes(exclude_pid: int = 0) -> list:
    """本机所有 DSH 启动进程（桌面实例 + sidecar），用于「能区分」这件事有据可查。"""
    found = []
    if psutil is None:
        return found
    try:
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                pid = int(proc.info.get('pid') or 0)
                if not pid or pid == int(exclude_pid or 0):
                    continue
                cmdline = proc.info.get('cmdline') or []
                joined = ' '.join(str(item) for item in cmdline).replace('\\', '/')
                if 'dsh' not in joined.lower():
                    continue
                if not (('lib/bin.js' in joined) or joined.endswith('/dsh.cmd')
                        or 'dsh.ps1' in joined):
                    continue
                profile = _profile_from_cmdline(cmdline)
                kind = _kind_of(profile, cmdline)
                found.append(dict(pid=pid, profile=profile, kind=kind,
                                  cmdline=_redact(joined)[:240]))
            except Exception:  # noqa: BLE001 - 进程在遍历途中退出
                continue
    except Exception:  # noqa: BLE001
        return found
    return found


# ------------------------------------------------------------------------- 指纹
def _config_files(profile: str) -> list:
    """参与指纹的组合配置文件（profile 层 + HOME 覆盖层）。"""
    profile = str(profile or '')
    candidates = [
        DSH_HOME / 'profiles' / profile / 'cordis.yml',
        DSH_HOME / 'profiles' / profile / 'cordis.patch.yml',
        DSH_HOME / 'profiles' / profile / 'package.json',
        DSH_HOME / 'cordis.patch.yml',
    ]
    return [path for path in candidates if profile and path.is_file()]


def _collect_route_names(node, routes: set) -> None:
    """从任意一层配置里收集 provider 路由名（`llm-pi-ai` 的 providers 段）。"""
    for item in _walk_dicts(node):
        providers = item.get('providers')
        if not isinstance(providers, dict):
            continue
        for route, config in providers.items():
            if not isinstance(config, dict):
                continue
            if {'baseURL', 'baseUrl', 'apiKeyEnv', 'models'} & set(config):
                routes.add(str(route))


def _provider_routes(profile: str) -> list:
    routes = set()
    for path in _config_files(profile):
        try:
            text = path.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
        data = _load_yaml(text)
        if data is not None:
            _collect_route_names(data, routes)
    return sorted(routes)


def _detect_profile(port: int) -> str:
    pid, _error = _listen_pid(port)
    return _proc_info(pid)['profile'] if pid else ''


def fingerprint(port: int = DEFAULT_PORT, profile: str = '') -> str:
    """实例指纹 = sha1(port|profile|config 文件内容|provider 路由)。

    只看配置，不碰 token/cookie；配置变了指纹就变，verify() 据此发现漂移。
    """
    try:
        port = int(port or DEFAULT_PORT)
        profile = str(profile or '') or _detect_profile(port)
        parts = ['port=%d' % port, 'profile=%s' % profile]
        for path in _config_files(profile):
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                digest = 'unreadable'
            label = path.relative_to(DSH_HOME).as_posix() if path.is_relative_to(DSH_HOME) else path.name
            parts.append('%s=%s' % (label, digest))
        parts.append('routes=' + ','.join(_provider_routes(profile)))
        return hashlib.sha1('|'.join(parts).encode('utf-8')).hexdigest()
    except Exception:  # noqa: BLE001 - 指纹算不出来时给空串，由调用方判失败
        return ''


# ------------------------------------------------------------------- 对外契约 API
def discover(port: int = DEFAULT_PORT) -> dict:
    """探测本机实际桌面 DSH 实例，不写盘。

    {ok, port, pid, profile, dsh_home, launch_token_present, cookie_ok, sessions,
     focused_session, config_fingerprint, error}（另附 kind / cmdline / sidecars）
    """
    result = dict(ok=False, port=int(port or DEFAULT_PORT), pid=0, profile='',
                  dsh_home=str(DSH_HOME), launch_token_present=False, cookie_ok=False,
                  sessions=0, focused_session='', config_fingerprint='', error='',
                  kind=UNKNOWN_KIND, cmdline='', sidecars=[])
    try:
        pid, perr = _listen_pid(result['port'])
        if not pid:
            result['error'] = perr or ('端口 %d 上没有监听进程' % result['port'])
            return result
        result['pid'] = pid

        info = _proc_info(pid)
        result['profile'] = info['profile']
        result['kind'] = info['kind']
        result['cmdline'] = info['cmdline']
        result['sidecars'] = _dsh_processes(exclude_pid=pid)

        result['config_fingerprint'] = fingerprint(result['port'], result['profile'])
        # 3080（RPC）与 51630（MCP 桥）同 pid ⇒ 同一个桌面实例的硬证据。
        result['cross_check'] = cross_check(result['port'], MCP_BRIDGE_PORT)

        # token 只在内存里用：这里只暴露「有没有」，绝不暴露内容。
        result['launch_token_present'] = bool(discover_launch_token())

        gateway = DshGateway(port=result['port'])
        health = gateway.health()
        result['cookie_ok'] = bool(health.get('ok'))
        if not health.get('ok'):
            result['error'] = _redact(health.get('error') or 'DSH 网关不可用')[:240]
            return result

        result['sessions'] = int(health.get('sessions') or 0)
        try:
            result['focused_session'] = gateway.current_session_id()
        except Exception:  # noqa: BLE001 - 会话列表为空时不影响实例可用性
            result['focused_session'] = ''
        result['ok'] = True
        return result
    except Exception as exc:  # noqa: BLE001 - 任何异常都转成 ok=False
        result['error'] = _redact(str(exc))[:240]
        return result


def bind(session_id: str = '') -> dict:
    """认准并持久化绑定：{ok, bound_at, fingerprint, session_id, port, pid}。"""
    try:
        found = discover()
        if not found.get('ok'):
            return dict(ok=False, error=found.get('error') or '没有可绑定的 DSH 桌面实例')

        sid = str(session_id or '').strip() or str(found.get('focused_session') or '')
        bound_at = _now()
        payload = dict(
            version=BINDING_VERSION,
            bound_at=bound_at,
            fingerprint=found.get('config_fingerprint') or '',
            session_id=sid,
            port=int(found.get('port') or DEFAULT_PORT),
            pid=int(found.get('pid') or 0),
            profile=str(found.get('profile') or ''),
            dsh_home=str(found.get('dsh_home') or ''),
            kind=str(found.get('kind') or ''),
        )
        # 只写指纹与非敏感字段；token/cookie 绝不进入这个文件。
        _atomic_write_json(BINDING_FILE, payload)
        return dict(ok=True, bound_at=bound_at, fingerprint=payload['fingerprint'],
                    session_id=sid, port=payload['port'], pid=payload['pid'],
                    profile=payload['profile'], kind=payload['kind'])
    except Exception as exc:  # noqa: BLE001
        return dict(ok=False, error=_redact(str(exc))[:240])


def current() -> dict:
    """读持久化绑定（重启后仍可用）；没有则返回 {ok: False}。"""
    try:
        if not BINDING_FILE.is_file():
            return dict(ok=False, error='尚未绑定任何 DSH 实例（%s 不存在）' % BINDING_FILE.name)
        payload = json.loads(BINDING_FILE.read_text(encoding='utf-8'))
        if not isinstance(payload, dict):
            return dict(ok=False, error='绑定文件内容不是对象，已拒绝使用')
        out = dict(ok=True)
        out.update(payload)
        try:
            bound = datetime.fromisoformat(str(payload.get('bound_at') or ''))
            out['age_seconds'] = max(0.0, (datetime.now().astimezone() - bound).total_seconds())
        except Exception:  # noqa: BLE001
            out['age_seconds'] = None
        return out
    except Exception as exc:  # noqa: BLE001
        return dict(ok=False, error='读取绑定失败：' + _redact(str(exc))[:200])


def verify() -> dict:
    """实测是否仍是同一个实例：比对 port/pid/fingerprint + 一次 RPC + 端口交叉校验。

    {ok, same_instance, drift:[...], health, cross_check}

    交叉校验：桌面实例的 Typert RPC（3080）与 MCP 桥（51630）应当是**同一个进程**；
    同 pid ⇒ 同一实例；不同 pid ⇒ drift 里点名「可能连到了另一套后台」；
    桥端口没开 ⇒ cross_check.available=False，仍可只凭 3080 判定 same_instance。
    """
    result = dict(ok=False, same_instance=False, drift=[], health={}, error='')
    bound = current()
    if not bound.get('ok'):
        result['error'] = bound.get('error') or '没有可校验的绑定'
        result['drift'] = ['no_binding']
        return result
    try:
        port = int(bound.get('port') or DEFAULT_PORT)
        found = discover(port)

        if not found.get('ok'):
            result['drift'].append('discover_failed: ' + (found.get('error') or '未知错误'))
        if int(found.get('pid') or 0) != int(bound.get('pid') or 0):
            result['drift'].append('pid %s -> %s' % (bound.get('pid'), found.get('pid')))
        if int(found.get('port') or 0) != port:
            result['drift'].append('port %s -> %s' % (port, found.get('port')))
        if str(found.get('profile') or '') != str(bound.get('profile') or ''):
            result['drift'].append('profile %r -> %r' % (bound.get('profile'), found.get('profile')))
        if str(found.get('config_fingerprint') or '') != str(bound.get('fingerprint') or ''):
            result['drift'].append('config_fingerprint changed')

        # 一次真实 RPC：证明「现在这个实例」仍然愿意服务我们。
        gateway = DshGateway(port=port)
        health = gateway.health()
        result['health'] = health
        if not health.get('ok'):
            result['drift'].append('rpc_failed: ' + _redact(health.get('error') or '')[:160])

        # 交叉校验：3080 与 MCP 桥端口是不是同一个进程。
        # 同 pid ⇒ 同一实例；不同 pid ⇒ 很可能连到了另一套后台；桥没开则降级为「无法校验」。
        cross = cross_check(port, MCP_BRIDGE_PORT)
        result['cross_check'] = cross
        if cross.get('available') and not cross.get('same_process'):
            result['drift'].append(
                '%s 与 %s 不是同一个进程（pid %s vs %s），可能连到了另一套后台'
                % (port, MCP_BRIDGE_PORT, cross.get('port_3080_pid'), cross.get('port_51630_pid')))

        session_id = str(bound.get('session_id') or '')
        if session_id and health.get('ok'):
            try:
                present = gateway.find_session(session_id) is not None
            except Exception:  # noqa: BLE001
                present = None
            result['session_present'] = present
            if present is False:
                result['drift'].append('bound_session_missing')

        result['same_instance'] = not result['drift']
        result['ok'] = True
        result['discovered'] = {key: found.get(key) for key in
                                ('port', 'pid', 'profile', 'kind', 'sessions',
                                 'focused_session', 'config_fingerprint')}
        return result
    except Exception as exc:  # noqa: BLE001
        result['error'] = _redact(str(exc))[:240]
        result['drift'].append('verify_exception')
        return result


# --------------------------------------------------------------------------- 自检
def _self_check() -> int:
    print('== discover(3080) ==')
    found = discover(3080)
    print(json.dumps(found, ensure_ascii=False, indent=2))

    print('\n== bind() ==')
    bound = bind()
    print(json.dumps(bound, ensure_ascii=False, indent=2))

    print('\n== current() ==')
    print(json.dumps(current(), ensure_ascii=False, indent=2))

    print('\n== verify() ==')
    checked = verify()
    print(json.dumps(checked, ensure_ascii=False, indent=2))

    return 0 if found.get('ok') and checked.get('same_instance') else 1


if __name__ == '__main__':
    raise SystemExit(_self_check())
