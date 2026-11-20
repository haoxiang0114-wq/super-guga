# 咕嘎社区插件

把一个 `<id>.json` 文件提交到 `plugins/community/`。合并到 `main` 后，用户在咕嘎插件市场点「更新仓库」，即可发现。插件按各自的版本安装到用户数据目录，刷新和安装不需要拉取整个应用源码。

| 字段 | 含义 |
| --- | --- |
| `schema` | 当前为整数 `1`。 |
| `id` | 2–63 位小写英文字母、数字、短横线，首位是字母，与文件名一致。 |
| `name` | 市场展示名称。 |
| `description` | 简短说明使用场景。 |
| `version` | 三段数字版本，如 `1.0.0`。更新时递增版本。 |
| `kind` | `workflow`、`codex-skill` 或 `feature`。 |
| `body` | 工作流步骤或 Codex 技能正文，最多 7200 字。选择「载入对话」后可在发送前查看和补充。 |
| `entry` | 内置功能入口。当前可用 `hearthstone`、`battleground-advisor`、`desktop-ocr`、`companion-memory`、`executor-settings`。 |

已有 Codex 本机技能和咕嘎保存的工作流可在市场直接载入对话。新增内置功能入口需要更新咕嘎本体，现有入口的技能说明和工作流可以独立更新。
