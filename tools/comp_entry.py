# -*- coding: utf-8 -*-
"""打包入口：默认走参赛版桌宠；同时承接冻结后由自身 exe 拉起的子进程模式。

不改 comp_main.py 的业务逻辑，只在 frozen 下补上两个原本由 launcher.py /
tools/frozen_entry.py 承接的命令行分支：
  --uia-worker   core.uia_service 的常驻 UIA 进程（core/uia_service.py 用
                 sys.executable --uia-worker 拉起自身）
  --guga-mcp     MCP stdio 服务（core/dsh_worker.py 用 sys.executable --guga-mcp 拉起）
PID/参数判断都只做字符串匹配，其余参数原样交给 comp_main。

windowed 构建没有 stderr，这里在最早时机装最小日志（用户目录下），
记录启动节点与未捕获异常；日志初始化失败不阻断任何分支。
"""
import sys

# 源码直跑 tools/comp_entry.py 时 sys.path[0] 是 tools/，补上仓库根方便 import core；
# 冻结包里 core 已在 _MEIPASS，不影响。
if not getattr(sys, 'frozen', False):
    import os
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _root not in sys.path:
        sys.path.insert(0, _root)

try:
    from core import app_log
    app_log.install()
    _log = app_log.log
    _log_exc = app_log.log_exc
except Exception:      # 日志不可用也必须能启动
    def _log(*args, **kwargs):
        return None

    def _log_exc(*args, **kwargs):
        return None

def _start_mcp_http():
    """主程序启动时顺手拉起 HTTP 端点；失败只记日志，不影响桌宠启动。"""
    try:
        import atexit
        import threading
        from core import mcp_http
        if not mcp_http.enabled():
            _log('mcp-http-disabled')
            return

        def _run():
            try:
                state = mcp_http.start()
                _log('mcp-http', '%s %s' % (state.get('state'), state.get('url') or ''))
            except BaseException:
                _log_exc('mcp-http-start-failed')

        threading.Thread(target=_run, name='guga-mcp-http', daemon=True).start()
        atexit.register(mcp_http.stop)
    except BaseException:
        _log_exc('mcp-http-unavailable')


_log('entry', 'frozen=%s mode=%s pid=%s' % (
    getattr(sys, 'frozen', False),
    ('uia-worker' if '--uia-worker' in sys.argv else
     'selfcheck' if '--selfcheck-json' in sys.argv else
     'paths-probe' if '--paths-probe-json' in sys.argv else
     'diagnostics-export' if '--export-diagnostics' in sys.argv else
     'simulate-adapter' if '--simulate-adapter-failure' in sys.argv else
     'download-vosk-model' if '--download-vosk-model' in sys.argv else
     'fast-command' if '--fast-command-json' in sys.argv else
     'model-probe' if '--model-probe-json' in sys.argv else
     'sendinput-selftest' if '--sendinput-selftest-json' in sys.argv else
     'guga-mcp-http' if '--guga-mcp-http' in sys.argv else
     'guga-mcp' if '--guga-mcp' in sys.argv else 'gui'),
    __import__('os').getpid()))

if '--uia-worker' in sys.argv:
    _log('uia-worker-enter')
    try:
        from core.uia_service import serve
        serve()
    except BaseException:
        _log_exc('uia-worker-crashed')
        raise
elif '--selfcheck-json' in sys.argv:
    # 裸机验收用：不开界面，把启动自检的逐项结果写成 JSON 后退出。
    # 结果里带 check_version / check_count，打包闸门会拿它和源码断言一致。
    # 用法: GUGA-Desktop.exe --selfcheck-json D:\out.json
    _log('selfcheck-enter')
    try:
        import json
        from pathlib import Path
        from core import env_check
        items = list(env_check.iter_checks())
        summary = env_check.summarize(items)
        expected = int(env_check.check_count())
        consistent = (bool(summary.get('consistent'))
                      and summary.get('state_total') == len(items)
                      and len(items) == expected)
        package = {}
        for candidate in (Path(str(getattr(sys, '_MEIPASS', '') or '')) / 'build-info.json',
                          Path(sys.executable).resolve().parent / '_internal' / 'build-info.json'):
            try:
                package = json.loads(candidate.read_text(encoding='utf-8-sig'))
                package['path'] = str(candidate)
                break
            except Exception:
                continue
        index = sys.argv.index('--selfcheck-json')
        target = sys.argv[index + 1] if len(sys.argv) > index + 1 else 'guga-selfcheck.json'
        payload = dict(check_version=getattr(env_check, 'CHECK_VERSION', ''),
                       check_count=expected, items_count=len(items),
                       summary=summary, summary_consistent=consistent,
                       package=package,
                       fatal=env_check.has_fatal(items), items=items)
        if not consistent:
            payload['summary_error'] = (
                'summary 计数不自洽：state_total=%s, level_total=%s, items=%s, check_count=%s'
                % (summary.get('state_total'), summary.get('level_total'),
                   len(items), expected))
        with open(target, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        _log('selfcheck-done', 'fatal=%s consistent=%s -> %s'
             % (payload['fatal'], consistent, target))
    except BaseException:
        _log_exc('selfcheck-crashed')
        raise
    sys.exit(0 if consistent else 3)
elif '--fast-command-json' in sys.argv:
    # 裸机验收入口：在冻结包里真跑一次本地指令（不调模型），把匹配与执行结果写 JSON。
    # 用法: GUGA-Desktop.exe --fast-command-json "打开豆包" [D:\out.json]
    _log('fast-command-enter')
    try:
        import json
        from pathlib import Path
        from core import fast_commands
        index = sys.argv.index('--fast-command-json')
        text = sys.argv[index + 1] if len(sys.argv) > index + 1 else ''
        target = (Path(sys.argv[index + 2]) if len(sys.argv) > index + 2
                  and not sys.argv[index + 2].startswith('-')
                  else Path('guga-fast-command.json'))
        matched = fast_commands.match(text)
        answer = fast_commands.execute(matched) if matched.get('matched') else {}
        # 本地没命中才交给模型派单。命中但“没找到这个软件/没说是哪个软件”属于
        # 本地可判定的失败，不需要模型 Key；让用户补路径或说清楚目标即可。
        delegate = not matched.get('matched')
        payload = dict(text=text, matched=matched,
                       ok=bool(answer.get('ok')), result=answer,
                       key_required=bool(delegate),
                       note=('本地未命中，将交给模型派单，需要模型 Key' if delegate
                             else '本地匹配，不调用模型，无需 Key'))
        with open(str(target), 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        _log('fast-command-done', 'matched=%s ok=%s -> %s'
             % (matched.get('matched'), payload['ok'], target))
        print(json.dumps(payload, ensure_ascii=False))
    except BaseException:
        _log_exc('fast-command-crashed')
        raise
    sys.exit(0)
elif '--model-probe-json' in sys.argv:
    # 405 定位入口：用咕嘎自己的配置真发一次 POST，并对比“直连 / 跟随系统代理”，
    # 把实际 URL、方法、状态码、代理设置写进 JSON。不写 Key。
    # 用法: GUGA-Desktop.exe --model-probe-json [D:\out.json]
    _log('model-probe-enter')
    try:
        import json
        from pathlib import Path
        from core import guga_paths, model_endpoint
        guga_paths.prepare()
        cfg = guga_paths.read_config()
        index = sys.argv.index('--model-probe-json')
        target = (Path(sys.argv[index + 1]) if len(sys.argv) > index + 1
                  and not sys.argv[index + 1].startswith('-')
                  else Path(guga_paths.RUNTIME) / 'model-probe.json')
        # 与设置页「验证 Key」共用同一份探测逻辑，避免命令行与界面结论不一致。
        record = model_endpoint.probe(cfg)
        record['config_path'] = str(guga_paths.CONFIG)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        _log('model-probe-done', 'ok=%s -> %s' % (record.get('ok'), target))
        try:
            print(json.dumps(record, ensure_ascii=False, indent=2))
        except Exception:
            pass
        sys.exit(0 if record.get('ok') else 1)
    except BaseException:
        _log_exc('model-probe-crashed')
        raise
elif '--sendinput-selftest-json' in sys.argv:
    # 冻结包回归：同一进程内先跑启动自检的 sendinput 项，再真实发一次 A 键。
    # q 包现场：自检给 ctypes.windll.user32 绑了 POINTER(INPUT)，派单 _send_keys 用 _INPUT，
    # 第二次调用抛 ArgumentError: expected LP_INPUT ... pointer to _INPUT。
    # 用法: GUGA-Desktop.exe --sendinput-selftest-json [D:\out.json]
    _log('sendinput-selftest-enter')
    try:
        import json
        from pathlib import Path
        from core import computer_control, env_check
        index = sys.argv.index('--sendinput-selftest-json')
        target = (Path(sys.argv[index + 1]) if len(sys.argv) > index + 1
                  and not sys.argv[index + 1].startswith('-')
                  else Path('guga-sendinput-selftest.json'))
        record = dict(frozen=bool(getattr(sys, 'frozen', False)))
        check = env_check._sendinput_check()
        record['selfcheck'] = dict(state=check.get('state'), level=check.get('level'),
                                   value=check.get('value'),
                                   accepted=check.get('state') == 'pass')
        try:
            computer_control._send_keys([65])
            record['send_keys'] = dict(ok=True, keys=[65])
        except BaseException as exc:
            record['send_keys'] = dict(ok=False, type=type(exc).__name__, error=str(exc))
        record['ok'] = bool(record['selfcheck']['accepted'] and record['send_keys'].get('ok'))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        _log('sendinput-selftest-done', 'ok=%s -> %s' % (record['ok'], target))
        try:
            print(json.dumps(record, ensure_ascii=False, indent=2))
        except Exception:
            pass
        sys.exit(0 if record['ok'] else 1)
    except BaseException:
        _log_exc('sendinput-selftest-crashed')
        raise
elif '--guga-mcp-http' in sys.argv:
    # 豆包 HTTP 连接器用：streamable HTTP，只绑 127.0.0.1，端口 18760 起冲突 +1。
    # 工具集与 stdio 完全一致（同一个 server 对象），主程序也会自动拉起这个子进程。
    _log('guga-mcp-http-enter')
    try:
        try:
            sys.stdin.reconfigure(encoding='utf-8')
            sys.stdout.reconfigure(encoding='utf-8')
        except Exception:
            pass
        from core.adapters.guga_mcp_server import main as serve
        serve()
    except BaseException:
        _log_exc('guga-mcp-http-crashed')
        raise
elif '--guga-mcp' in sys.argv:
    _log('guga-mcp-enter')
    try:
        try:
            sys.stdin.reconfigure(encoding='utf-8')
            sys.stdout.reconfigure(encoding='utf-8')
        except Exception:
            pass
        if '--dsh-tools' in sys.argv:
            # DSH worker 的旧通道：保留 Qt 管道协议
            from launcher import _prepare_qt
            _prepare_qt()
            from guga_mcp_server import serve
        else:
            # 任务总线通道：packaged 版直接由 exe 自身起 stdio MCP server，
            # 不依赖外部 Python，也不初始化 Qt。
            from core.adapters.guga_mcp_server import main as serve
        _log('guga-mcp-serve', 'dsh=%s' % ('--dsh-tools' in sys.argv))
        serve()
    except BaseException:
        _log_exc('guga-mcp-crashed')
        raise
elif '--paths-probe-json' in sys.argv:
    # 打包版写入落点自检：不开界面，用真实模块把“豆包证据 / 日志 / 交付物”各写一次，
    # 结果写 JSON 后退出。供只读安装目录验收确认没有写进安装目录。
    # 用法: GUGA-Desktop.exe --paths-probe-json D:\out.json
    _log('paths-probe-enter')
    try:
        import json
        import os
        from pathlib import Path
        from core import guga_paths, app_log, file_delivery
        from core.adapters import doubao_adapter
        index = sys.argv.index('--paths-probe-json')
        target = (sys.argv[index + 1] if len(sys.argv) > index + 1
                  else 'guga-paths-probe.json')
        guga_paths.prepare()
        record = dict(base=str(guga_paths.BASE), source=guga_paths.BASE_SOURCE,
                      writable=bool(guga_paths.BASE_WRITABLE),
                      runtime=str(guga_paths.RUNTIME))
        # 1) 豆包适配器证据目录
        evidence = Path(doubao_adapter.DEFAULT_EVIDENCE_DIR)
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / 'probe.json').write_text('{"probe": true}', encoding='utf-8')
        record['doubao_evidence'] = str(evidence)
        # 2) 日志
        app_log.log('paths-probe', 'evidence/log/delivery 写入落点')
        record['log'] = str(app_log.log_path())
        record['log_dir_controlled_by_base'] = True
        record['guga_home'] = str(os.environ.get('GUGA_HOME') or '')
        try:
            record['log_in_base'] = Path(record['log']).resolve().is_relative_to(
                Path(guga_paths.BASE).resolve())
        except Exception:
            record['log_in_base'] = None
        # 3) 交付物：走真实的 file_delivery.deliver()，产物复制到交付目录
        source = Path(os.environ.get('TEMP') or '.') / 'guga-paths-probe.txt'
        source.write_text('guga paths probe', encoding='utf-8')
        outcome = file_delivery.deliver(text='已生成：%s' % source, user_text='交付探针')
        record['delivery'] = dict(root=outcome.get('root'), status=outcome.get('status'),
                                  files=[f.get('path') for f in outcome.get('files') or []])
        record['ok'] = bool(record['writable'] and record['delivery']['status'] == 'ok')
        with open(target, 'w', encoding='utf-8') as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)
        _log('paths-probe-done', 'ok=%s -> %s' % (record['ok'], target))
        sys.exit(0 if record['ok'] else 1)
    except BaseException:
        _log_exc('paths-probe-crashed')
        raise
elif '--download-vosk-model' in sys.argv:
    # 不依赖模型 Key 的模型获取入口：联网下载约 40MB 到 %GUGA_HOME%/models/vosk-model-cn。
    # 用法: GUGA-Desktop.exe --download-vosk-model [--model-dir D:\path] [--download-out out.json]
    _log('vosk-download-enter')
    try:
        import json
        from pathlib import Path
        from core import asr, guga_paths
        guga_paths.prepare()
        dest = ''
        if '--model-dir' in sys.argv:
            i = sys.argv.index('--model-dir')
            dest = sys.argv[i + 1] if len(sys.argv) > i + 1 else ''
        out = guga_paths.RUNTIME / 'vosk-model-download.json'
        if '--download-out' in sys.argv:
            i = sys.argv.index('--download-out')
            out = Path(sys.argv[i + 1]) if len(sys.argv) > i + 1 else out
        result = asr.download_model(dest=dest,
                                    progress=lambda msg: _log('vosk-download', msg))
        payload = dict(ok=bool(result.get('ok')), result=result,
                       model_dir=str(asr.MODEL_DIR),
                       package_has_model=False,
                       note='模型不随安装包；下载后自检“离线语音模型”才会从 untested 变为 pass')
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        _log('vosk-download-done', 'ok=%s -> %s' % (payload['ok'], out))
        sys.exit(0 if payload['ok'] else 1)
    except BaseException:
        _log_exc('vosk-download-crashed')
        raise
elif '--export-diagnostics' in sys.argv:
    # 复验入口：不经过 GUI 直接导出诊断 zip，并回写结果 JSON（含版本/构建 SHA/包 SHA）。
    _log('diagnostics-export-enter')
    try:
        import json
        from pathlib import Path
        from core import guga_paths
        from core.diagnostic_export import export_diagnostics
        guga_paths.prepare()
        index = sys.argv.index('--export-diagnostics')
        dest = ''
        if len(sys.argv) > index + 1 and not sys.argv[index + 1].startswith('-'):
            dest = sys.argv[index + 1]
        result = export_diagnostics(dest or None)
        receipt = guga_paths.RUNTIME / 'diagnostic-export.json'
        result['receipt'] = str(receipt)
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        _log('diagnostics-export-done', 'ok=%s -> %s' % (result.get('ok'), result.get('zip_path')))
        sys.exit(0 if result.get('ok') else 1)
    except BaseException:
        _log_exc('diagnostics-export-crashed')
        raise
elif '--simulate-adapter-failure' in sys.argv:
    # 不依赖真实模型/Key 的可复现入口：桩适配器跑真实 adapter_chain。
    # 用法:
    #   GUGA-Desktop.exe --simulate-adapter-failure ocr_unavailable [--simulate-out D:\out.json]
    #   GUGA-Desktop.exe --simulate-adapter-failure ocr_degraded    # 降级续跑（两步）
    _log('simulate-adapter-enter')
    try:
        import json
        import re
        from pathlib import Path
        from core import adapter_chain, guga_paths
        from core.adapters import dispatch
        from core.failure_report import format_failure, normalize_code, stage_for

        index = sys.argv.index('--simulate-adapter-failure')
        requested = sys.argv[index + 1] if len(sys.argv) > index + 1 else 'ocr_unavailable'
        target = guga_paths.RUNTIME / ('simulate-adapter-%s.json'
                                       % re.sub(r'[^A-Za-z0-9_.-]+', '_', requested))
        if '--simulate-out' in sys.argv:
            i = sys.argv.index('--simulate-out')
            target = Path(sys.argv[i + 1]) if len(sys.argv) > i + 1 else target
        guga_paths.prepare()
        degraded_mode = requested.casefold() in ('ocr_degraded', 'ocr-degraded', 'degraded')
        raw_text = ('RuntimeError: 桩适配器模拟失败（不会读取任何真实模型 Key / 不访问网络）'
                    if not degraded_mode else
                    '模拟：Windows OCR 不可用，适配器已改用 UIA + 剪贴板读出正文')
        stub_results = {}
        if degraded_mode:
            stub_results['stub_a'] = dict(
                ok=True, text='第 1 步模拟产出：一段可被下一步消费的提示词正文。',
                source=dict(label='桩适配器', provenance='simulate-degraded-step1',
                            call_id='simulate-1', channel='uia'),
                evidence=dict(dir='', ocr_unavailable=True, capture_mode='uia+clipboard'),
                ocr_unavailable=True)
            stub_results['stub_b'] = dict(
                ok=True, text='第 2 步模拟产出：上一步正文已作为输入，本步正常完成。',
                source=dict(label='桩适配器', provenance='simulate-degraded-step2',
                            call_id='simulate-2', channel='uia'))
            steps = [dict(index=1, app_id='stub_a', name='桩适配器A', instruction='产出提示词',
                          raw_segment='用桩适配器A产出提示词'),
                     dict(index=2, app_id='stub_b', name='桩适配器B', instruction='按上一步正文继续整理成文字稿',
                          raw_segment='再用桩适配器B整理成文字稿')]
        else:
            payload = dict(ok=False, code=normalize_code(requested),
                           hint=raw_text, message=raw_text,
                           missing='模拟缺失项（用于复验四段文案）',
                           where='不需要真实安装，换成 --simulate-adapter-failure ocr_degraded 可看降级续跑',
                           next_action='这是模拟失败：核对四段是否齐全；真实派单请按【下一步】处理。')
            stub_results['stub_a'] = payload
            steps = [dict(index=1, app_id='stub_a', name='桩适配器A', instruction='模拟失败',
                          raw_segment='用桩适配器A模拟失败')]

        calls = []

        class _StubAdapter:
            def __init__(self, key):
                self.key = key

            def ask(self, prompt, timeout_seconds=None, retry=1, prefer_mcp=True):
                calls.append(dict(app_id=self.key, prompt=str(prompt)[:200]))
                return dict(stub_results[self.key])

        original_adapter_for = dispatch.adapter_for
        original_focus = adapter_chain._focus_app
        dispatch.adapter_for = lambda key: (str(key), _StubAdapter(str(key)))
        adapter_chain._focus_app = lambda name: True
        try:
            chain_result = adapter_chain.run(dict(raw='模拟入口', steps=steps))
        finally:
            dispatch.adapter_for = original_adapter_for
            adapter_chain._focus_app = original_focus

        four_segment = ''
        assertions = {}
        if degraded_mode:
            rows = chain_result.get('steps') or []
            assertions = dict(
                chain_ok=bool(chain_result.get('ok')),
                step_count=len(rows) == 2,
                first_degraded=bool(rows and rows[0].get('status') == 'degraded'
                                    and rows[0].get('code') == 'ocr_unavailable'),
                second_done=bool(len(rows) > 1 and rows[1].get('status') == 'done'),
                continued_after_ocr_failure=len(calls) == 2,
                degraded_flag=bool(chain_result.get('degraded')),
                ocr_unavailable_flag=bool(chain_result.get('ocr_unavailable')),
            )
        else:
            code = str(chain_result.get('failure_code') or chain_result.get('code') or '')
            stage = str(chain_result.get('stage') or stage_for(code, app='桩适配器'))
            four_segment = format_failure(code, stage, raw_text,
                                          chain_result.get('next_action') or '', app='桩适配器')
            sections = ('【失败码】', '【阶段】', '【原始异常】', '【下一步】')
            assertions = dict(
                chain_failed=not bool(chain_result.get('ok')),
                failure_code_matches=code == normalize_code(requested),
                four_sections=all(item in four_segment for item in sections),
                raw_not_placeholder='（错误）' not in four_segment and '执行器报告失败' not in four_segment,
            )
        all_ok = all(bool(value) for value in assertions.values())
        payload = dict(
            schema='guga.simulate-adapter/1', simulated=True, requires_model_key=False,
            scenario='degraded' if degraded_mode else 'failure',
            requested_code=requested, calls=calls, chain=chain_result,
            four_segment=four_segment, assertions=assertions, all_ok=all_ok,
            usage='GUGA-Desktop.exe --simulate-adapter-failure <code> [--simulate-out out.json]',
            note='桩适配器不联网、不读 Key；跑的是真实 adapter_chain 降级/失败分支')
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        _log('simulate-adapter-done', 'scenario=%s all_ok=%s -> %s'
             % (payload['scenario'], all_ok, target))
        sys.exit(0 if all_ok else 1)
    except BaseException:
        _log_exc('simulate-adapter-crashed')
        raise
elif '--mcp-snippet-json' in sys.argv:
    # 验收用：从当前 exe 生成三家的可粘贴配置，不开界面。
    # 用法: GUGA-Desktop.exe --mcp-snippet-json D:\out.json [workbuddy doubao joycode]
    _log('mcp-snippet-json-enter')
    try:
        import json
        from core.adapters import mcp_channels
        index = sys.argv.index('--mcp-snippet-json')
        target = sys.argv[index + 1] if len(sys.argv) > index + 1 else 'guga-snippets.json'
        apps = [str(item).casefold() for item in sys.argv[index + 2:]
                if not str(item).startswith('-')] or list(mcp_channels.DEFAULT_ORDER)
        command = mcp_channels.server_command()
        payload = dict(mode=command.get('mode'), command=command.get('command') or '',
                       args=list(command.get('args') or []), apps={})
        for app_id in apps:
            data = mcp_channels.snippet(app_id)
            payload['apps'][app_id] = dict(ok=bool(data.get('ok')),
                                           kind=data.get('kind') or '',
                                           text=data.get('text') or '',
                                           message=data.get('message') or '')
        with open(target, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        _log('mcp-snippet-json-done', '-> %s' % target)
    except BaseException:
        _log_exc('mcp-snippet-json-crashed')
        raise
    sys.exit(0)
elif '--mcp-onboarding-json' in sys.argv:
    # 裸机/验收用：不开界面，把三家的接入状态写成 JSON 后退出。
    # 用法: GUGA-Desktop.exe --mcp-onboarding-json D:\out.json
    _log('mcp-onboarding-json-enter')
    try:
        import json
        from core.adapters import mcp_channels
        index = sys.argv.index('--mcp-onboarding-json')
        target = sys.argv[index + 1] if len(sys.argv) > index + 1 else 'guga-onboarding.json'
        payload = mcp_channels.onboarding(force_probe=True)
        with open(target, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        _log('mcp-onboarding-json-done', '-> %s' % target)
    except BaseException:
        _log_exc('mcp-onboarding-json-crashed')
        raise
    sys.exit(0)
elif '--mcp-onboarding' in sys.argv:
    # 首次接入教程窗口：可单独打开，用来复制配置、查看“已接入/待重启/未接入”。
    _log('mcp-onboarding-enter')
    try:
        from launcher import _prepare_qt
        _prepare_qt()
        from PyQt5.QtWidgets import QApplication
        from ui.mcp_onboarding_dialog import McpOnboardingDialog
        app = QApplication(sys.argv)
        dialog = McpOnboardingDialog()
        dialog.show()
        app.exec_()
        _log('mcp-onboarding-closed')
    except BaseException:
        _log_exc('mcp-onboarding-crashed')
        raise
    sys.exit(0)
else:
    _log('gui-enter')
    _start_mcp_http()
    try:
        from comp_main import main
        _log('comp-main-imported')
        main()
        _log('gui-main-returned')
    except SystemExit as exc:
        _log('gui-exit', 'code=%s' % exc.code)
        raise
    except BaseException:
        _log_exc('gui-crashed')
        raise
