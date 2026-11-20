"""Local-first routing for desktop work.

The fast command parser handles deterministic one-shot commands before this
module.  Here, ordinary desktop tasks stay in Guga's visual/UI-automation
agent.  DSH is reserved for explicit code/project work or an explicit user
override. Routing itself does not depend on a model connection.
"""
from __future__ import annotations

import re


def _is_code_task(text):
    value = str(text or '').lower()
    # Reading a repository page or editing a line is ordinary desktop work.
    engineering=any(word in value for word in ('重构','调试','修复','开发','实现','编译','审查','测试','写'))
    code=any(word in value for word in ('代码','源码','程序','python','react','vue','typescript','前端','后端'))
    scope=any(word in value for word in ('项目','代码库','仓库','多文件','多个文件','多个源码','工程','完整程序'))
    deep=any(word in value for word in ('重构','跨模块','集成测试','端到端','多文件','多个源码'))
    return engineering and code and (scope or deep)


def choose_task_route(prompt, image=False, backend='auto', workflow=None):
    """Return an inspectable route without executing any task."""
    backend = str(backend or 'auto').lower()
    if image:
        return dict(executor='local_agent', category='screenshot', reason='用户提供了截图')
    if backend == 'self':
        return dict(executor='local_agent', category='manual', reason='用户固定为本地智能体')
    if backend == 'dsh':
        return dict(executor='dsh', category='manual', reason='用户固定为 DSH')
    if backend == 'workbuddy':
        # 2026-11-20：以前这里没有 workbuddy 分支，用户把执行器钉成 WorkBuddy
        # 也照样落到 local_agent，看到的就是"接了 WorkBuddy 却没用上"。
        return dict(executor='workbuddy', category='manual', reason='用户固定为 WorkBuddy',
                    **({'workflow_id': workflow.get('id')} if workflow else {}))
    from core.execution_contract import is_dsh_live_query
    if is_dsh_live_query(prompt):
        return dict(executor='dsh', category='live_query', reason='读取本机 DSH 的真实能力与会话')
    if re.search(r'(?:交给|使用|用|让|通过)\s*dsh(?![a-z0-9_])',str(prompt),re.I):
        return dict(executor='dsh',category='explicit',reason='用户明确要求使用 DSH')
    if workflow:
        # 工作流可以自带 executor（比如纯界面活交给 WorkBuddy）。没写就还是咕嘎自己跑。
        preferred = str(workflow.get('executor') or '').lower()
        if preferred in ('workbuddy', 'dsh', 'local_agent'):
            return dict(executor=preferred, category='workflow',
                        reason='工作流指定执行器：' + preferred,
                        workflow_id=workflow.get('id'))
        return dict(executor='local_agent', category='workflow', reason='命中已保存工作流',
                    workflow_id=workflow.get('id'))
    if re.search(r'(?:画|绘制|生成|制作|做).*(?:思维导图|流程图|关系图|架构图)',str(prompt),re.S):
        return dict(executor='dsh',category='diagram',reason='由 DSH 绘图，咕嘎展示并讲解真实文件')
    if _is_code_task(prompt):
        return dict(executor='dsh', category='code', reason='涉及代码、项目或多文件工程')
    return dict(executor='local_agent', category='local_task',
                reason='日常电脑操作由咕嘎直接执行，失败后再考虑升级')
