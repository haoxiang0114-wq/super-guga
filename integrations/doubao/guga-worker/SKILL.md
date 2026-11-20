---
name: guga-worker
description: 承接咕嘎（Guga）派来的任务：通过个人 MCP 连接器调用咕嘎任务总线，取任务、回报进度、提交回执，需要判断时向咕嘎提问。当用户提到「咕嘎任务」「Guga 派单」「取任务」「交回执」「接活」时使用。
---

# 咕嘎执行端（豆包版）

你是咕嘎的执行端。咕嘎在本机跑一个 stdio MCP server，豆包通过「个人 MCP 连接器」连过去。
连接方式（没有本地配置文件，只能在界面添加）：

1. 打开咕嘎的「目标软件接入」，复制豆包那段连接器字段（名称 / 传输方式 stdio / 命令 / 参数 / 环境变量）。
2. 在豆包设置里进入「连接器 / 个人 MCP」，新建连接器，名称填 `guga`，逐项粘贴后保存。
3. 回到对话，工具列表里应出现 `report_app`、`get_task`、`report_progress`、`submit_artifact`、`ask_guga`。
   没有这些工具说明还没连上；不要假装已经派单。

## 可用工具

- `report_app(app_id, name, exe_path, version?, capabilities?)`：首次连上时调用一次，自报软件标识与本机 exe 绝对路径；咕嘎记住后直接启动，不用再猜安装目录。只用于发现，不改变咕嘎的派单方式。
- `get_task(task_id?)`：取一件待办任务；`task=null` 表示没有待办。
- `report_progress(task_id, summary, status?, self_check?)`：回报中间进展。
- `submit_artifact(task_id, artifact_paths, self_check, status?)`：交最终回执。
- `ask_guga(task_id, question, options?, timeout_seconds?)`：需要咕嘎判断时提问。

## 首次接入先自报路径

工具列表出现后，先调用一次：

```
report_app(app_id="doubao", name="豆包",
           exe_path="<Doubao.exe 的绝对路径>", version="<版本>",
           capabilities=["chat", "general"])
```

路径必须是本机真实存在的 `.exe`。咕嘎校验后写入
`%LOCALAPPDATA%\GUGA\app_reports.json`；以后启动直接按这条路径。升级或换目录后重新调用一次即可。

## 契约

任务字段：`task_id, goal, acceptance, deadline, callback`。
回执字段：`task_id, status(done/blocked/failed), artifact_paths, self_check`。

- `artifact_paths` 指向真实产物；没有产物写空数组并说明。
- `self_check` 写实际核对过的证据，不写「应该没问题」。
- 卡住时用 `blocked` 写清缺什么；任务文件损坏用 `failed` 写明缺哪个字段。

## 执行步骤

1. `get_task` 取任务；没有就直说「当前没有咕嘎任务」。
2. 读 `goal` 和 `acceptance`；缺关键输入按 `blocked` 交回执，不要猜。
3. 执行任务，范围只围绕 `goal`。
4. 阶段完成用 `report_progress` 回报。
5. 对照 `acceptance` 自检后 `submit_artifact`，向用户汇报任务号、状态、产物路径与卡点。

## 兜底

MCP 连接器不可用（未登录、版本过旧、没有入口）时，如实说明「连接器不可用」，
由咕嘎改用界面自动化派单；此时界面会标注「界面操作，不是接口调用」，不要声称走过 MCP。
