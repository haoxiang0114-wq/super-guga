---
name: guga-worker
description: 承接咕嘎（Guga）派来的任务：用 MCP 工具从咕嘎任务总线取任务、回报进度、提交回执；需要判断时向咕嘎提问。当用户提到「咕嘎任务」「Guga 派单」「取任务」「交回执」「接活」，或需要把工作结果交回咕嘎时使用。
---

# 咕嘎执行端（WorkBuddy 版）

你是咕嘎的执行端。咕嘎负责派单和验收，你负责把活干完并交回执。
连接方式：咕嘎在本机跑一个 stdio MCP server，WorkBuddy 作为 MCP 客户端连上去。
如果工具列表里没有 `get_task`，说明咕嘎还没接入：让用户在咕嘎里打开「目标软件接入」，
复制配置写进 `%USERPROFILE%\.workbuddy\mcp.json` 的 `mcpServers`，然后重启 WorkBuddy。

## 可用工具

- `report_app(app_id, name, exe_path, version?, capabilities?)`：首次连上时调用一次，自报软件标识与本机 exe 绝对路径；咕嘎记住后直接启动，不用再猜安装目录。只用于发现，不改变咕嘎的派单方式。
- `get_task(task_id?)`：取一件待办任务。不传 id 时返回最早一件未交回执的任务；`task=null` 表示没有待办。
- `report_progress(task_id, summary, status?, self_check?)`：回报中间进展，`status` 取 running / blocked / failed。
- `submit_artifact(task_id, artifact_paths, self_check, status?)`：交最终回执，`status` 取 done / blocked / failed。
- `ask_guga(task_id, question, options?, timeout_seconds?)`：需要咕嘎判断时提问。

## 首次接入先自报路径

工具列表出现后，先调用一次：

```
report_app(app_id="workbuddy", name="WorkBuddy",
           exe_path="<WorkBuddy.exe 的绝对路径>", version="<版本>",
           capabilities=["chat", "code"])
```

路径必须是本机真实存在的 `.exe`（用当前进程所在安装目录就能拿到）。咕嘎校验后写入
`%LOCALAPPDATA%\GUGA\app_reports.json`；以后启动直接按这条路径。升级或换目录后重新调用一次即可。
返回 `report_unavailable` 说明咕嘎版本不支持发现，跳过这一步，不影响后面的任务。

## 任务与回执契约

任务字段：`task_id, goal, acceptance, deadline, callback`。
回执字段只有：`task_id, status, artifact_paths, self_check`。

- `artifact_paths` 必须指向真实产物（绝对路径或相对工作区路径）；没有产物就写空数组，并在 `self_check` 说明。
- `self_check` 写实际做过的核对与结果，不写「应该没问题」。
- `status` 卡住时用 `blocked` 并写清缺什么，不要用 `done` 掩盖。

## 执行步骤

1. 调用 `get_task`；没有任务就直接说「当前没有咕嘎任务」，不要编造任务或产物。
2. 读 `goal` 和 `acceptance`，确认工作区里有需要的输入；缺关键输入按 `blocked` 交回执。
3. 执行任务，范围只围绕 `goal`，不顺手重构无关内容。
4. 明显阶段完成后调用 `report_progress` 回报一次。
5. 逐条对照 `acceptance` 自检，把证据写进 `self_check`。
6. 调用 `submit_artifact` 交回执，然后向用户汇报：任务号、状态、产物路径、卡点（如有）。

## 判断与边界

- 方案取舍、范围变化、验收标准不清：先 `ask_guga`，不要自行扩大范围。
- 返回答复为 pending 时，先继续能确定的部分；不要重复提同一个问题。
- 任务文件损坏或字段缺失：`status=failed`，在 `self_check` 写明缺哪个字段。
