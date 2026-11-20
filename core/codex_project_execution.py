"""Execute an approved plan in a workspace-bound conversation linked to its source."""
from pathlib import Path
import os
import shutil
import subprocess
import time

from core.codex_gateway import CodexError
from core.codex_readonly_plan import _PlannerConnection


def implement_project(plan_thread_id, prompt, workspace, on_event=None, timeout=300):
    workspace = Path(workspace).resolve(strict=True)
    if not workspace.is_dir() or not plan_thread_id or not prompt.strip():
        raise ValueError('开发需要已审批的方案编号和确切目录')
    executable = shutil.which('codex') or shutil.which('codex.exe')
    if not executable:
        raise CodexError('没有找到 Codex 命令')
    process = subprocess.Popen([executable, 'app-server', '--listen', 'stdio://', '--disable', 'apps',
                               '--disable', 'skill_mcp_dependency_install', '-c', 'mcp_servers={}',
                               '-c', 'web_search=disabled'],
        cwd=str(workspace), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        encoding='utf-8', errors='replace', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        connection = _PlannerConnection(process, time.monotonic() + timeout, on_event)
        connection.call('initialize', dict(clientInfo=dict(name='guga-project', title='Guga Approved Project',
                                                          version='2.2.0')))
        connection.send(dict(method='initialized'))
        result = connection.call('thread/start', dict(cwd=str(workspace),
            sandbox='workspace-write', approvalPolicy='never',
            developerInstructions='只在当前项目目录内实施已获批准的方案。无网络、无需安装依赖。'
                                  '不要修改其他项目，不提交或发布。实际验证后给出入口与结果。'))
        sandbox = result.get('sandbox') or {}
        if isinstance(sandbox, dict):
            sandbox = sandbox.get('type')
        thread_id = str((result.get('thread') or {}).get('id') or '')
        if (not thread_id or thread_id == plan_thread_id
                or os.path.normcase(str(Path(result.get('cwd') or '.').resolve())) != os.path.normcase(str(workspace))
                or sandbox not in ('workspaceWrite', 'workspace-write') or result.get('approvalPolicy') != 'never'):
            raise CodexError('原会话或开发目录权限没有核对通过，未提交开发')
        if on_event:
            on_event('guga/implementationThread', dict(threadId=thread_id, planThreadId=plan_thread_id))
        deliver = f'已通过人工审批。对应方案会话：{plan_thread_id}。\n\n{prompt}'
        turn = connection.call('turn/start', dict(threadId=thread_id, cwd=str(workspace),
            approvalPolicy='never', effort='low',
            sandboxPolicy=dict(type='workspaceWrite', writableRoots=[str(workspace)], networkAccess=False,
                               excludeSlashTmp=True, excludeTmpdirEnvVar=True),
            input=[dict(type='text', text=deliver, text_elements=[])]))
        turn_id = str((turn.get('turn') or {}).get('id') or '')
        if not turn_id:
            raise CodexError('开发任务没有获得回合编号，请核对原会话')
        connection.wait_for_turn(thread_id, turn_id)
        snapshot = connection.call('thread/read', dict(threadId=thread_id, includeTurns=True))
        thread = snapshot.get('thread') or {}
        turns = [item for item in thread.get('turns', []) if item.get('id') == turn_id]
        return dict(thread=thread, turns=turns)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        process.stdin.close()
        process.stdout.close()
