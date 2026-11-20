# super 咕嘎

有记忆、会陪伴、能协助电脑事务的桌面小企鹅。仓库地址：<https://github.com/jscjscjscjscjsc/super-guga>。

**2026-11-20 · 协作基线**

- 咕嘎说话沿用戳击动图的原画，支持随语音动嘴、自然眨眼、轻微跟随鼠标和按对话切换表情。对话框会避开咕嘎的脸。[角色三视图](resources/character-reference/guga-turnaround-20261120.png)
- 在咕嘎的右键菜单打开「插件市场」。点「更新仓库」可以查看 `main` 上的社区插件并一键安装，也能在市场里载入本机已有的 Codex 技能和咕嘎工作流。[提交社区插件](plugins/community/README.md)
- 咕嘎可结合本机 Codex、WorkBuddy 对话和知识资料。豆包浏览器沿用当前浏览器的登录会话。每台电脑分别配置自己的模型与账号。
- 微信自动回复首次使用时默认关闭。电脑微信保持在「文件传输助手」会话，在咕嘎对话中输入「开启文件传输助手自动回复」。要暂停时输入「停止文件传输助手自动回复」。

已有源码的协作者可运行 `git pull --ff-only origin main`。首次使用请按下方的「公开版快速开始」配置，再运行 `python launcher.py`。

**2026-11-10：独立文字问答与桌面管理台。** 普通聊天直接调用驱动模型，无需打开 DSH；统一陪伴提示词优先用大白话解释，并结合用户、项目、当前对话记忆、实时每日计划和灵枢资料。管理台分别设置驱动模型、语音输出及可选长语音；朗读默认 Edge TTS，手动录音独立于长语音额度。另提供划词工具条、隐藏桌宠后的悬浮入口。实际验证结果和待验收边界见 [独立聊天与记忆](docs/independent-chat-model-memory-20261110.md)、[桌面划词与语音验收](docs/桌面划词_悬浮入口_本地语音验收.md)。

**2026-11-10：每日计划与 DSH 插件。** 聊天输入“设置每日计划：学习；运动”即可建立每天重复、逐项勾选的清单；聊天窗顶部和右键菜单可编辑与查看历史。碎碎念会依据真实勾选提醒进度并鼓励。支持独立 DSH 通过官方 MCP 插件调用咕嘎桌面能力与每日计划；使用、上游整合范围及当前模型凭据限制见 [每日计划与 DSH 插件](docs/daily-plan-dsh-plugin-20261110.md)。

**同日补充：本机 DSH UI 原生插件与进度回传。** 插件列表显示“咕嘎 · 桌面助手”，自动接收按会话区分的真实任务状态，支持气泡和现有长语音定期汇报。语音动态更新去掉同连接重复对话文本，并独立保留真实用量日志。人工验收步骤见 [本地 DSH 与长语音真机验收](docs/咕嘎与本地DSH_真机验收方案.md)。

一个 Windows 桌面企鹅宠物：从炉石酒馆战棋军师进化为 **Python/大模型助教帮手**，
能看屏幕、能出题、能连续对话问答，同时保留战棋胜率分析能力。

**下载后快速开始：** 双击 `首次配置.bat` 填写聊天 API 与实时语音 API，再双击 `启动企鹅.bat`。完整环境、两条模型链路、免费额度保护和权限说明见 [公开版快速开始](docs/public-quickstart.md)。桌面控制优先使用 UIA 结构化控件，内存读取只用于明确支持的只读集成，见 [快速桌面控制与内存边界](docs/fast-desktop-control.md)。

**本机感知与录屏修复（2026-11-05）：** 共享屏幕现在同时读取当前窗口的本地 UIA 文字和控件，供语音直接理解输入框、按钮及页面文字，不必每次等远程识图；观察和操作使用独立执行器。补齐常用口语与搜索框占位文字识别，短桌面任务关闭深度思考并限制自主试探轮数。取消形象和气泡的系统捕获排除，浏览器工具也不再自动隐藏咕嘎。已实际抓取到形象截图；验证范围见 [本机感知、网页响应与捕获修复](docs/local-eyes-20261105.md)。

**长语音稳定性补充（2026-11-05）：** 修复原生语音断句后、服务端开始回复前的请求竞态；协议收发统一由一个工作线程处理，移除客户端每 110 分钟主动换连接。默认通话和空闲时限均为 0，不因时长主动结束。真实服务的原生合成语音测试在同一连接持续 10 分钟，完成 26 次转写、56 次回复、8 次隔离快捷回执，断线、重连、协议错误均为 0；另完成本机 90 秒静音设备检查。测试方法、失败对照与尚未覆盖的范围见 [持续通话修复与验收](docs/voice-continuity-20261105.md)。

**2026-11-05 更新：** 浏览器改为用户逐步指挥、当前网页一步一动作：“点击输入框”→“输入内容”→“点击搜索”。简单点击、填写、滚动和导航直接本地执行，成功即时气泡反馈并继续收音；UIA 精确查询替代每步全树扫描。豆包与 Edge 自有页面、连续合成语音已验证，计时和边界见 [浏览器响应修复与验收](docs/browser-response-20261104.md)。GitHub 的 Download ZIP / `git pull` 获取最新源码；历史 EXE 和私人测试包不代表本次版本。

**同日补充修复：** “打开，微信”“你能打开微信吗”直接执行；发送正文中的问号不再把操作错分到普通问答。微信按进程身份识别，避免误聚焦企业微信。修复未收到回复创建确认时重复请求回复的竞态；平台直接返回 `code/message` 的额度错误现在明确停止。后台屏幕与记忆变化合并更新语音会话，等待确认后再发送下一次，减少频繁重设配置。详见 [软件启动与语音会话修复](docs/command-session-repair-20261105.md)。

此前的长对话打开网站、打断竞态与自动续接修复，以及常用形态、可删记忆、联网问答、屏幕辅助和后台进度改动均包含在当前源码中，见 [长语音修复与验收](docs/long-voice-stability-20261104.md)。

长语音的时长、上下文、打断、断线、额度与任务执行边界见 [实时语音能力边界](docs/realtime-voice-boundaries.md)；Computer Use 各路线和 UIA、DOM、内存在自动化中的作用见 [Computer Use 路线与边界](docs/computer-use-landscape.md)。

实时语音默认不做声纹匹配，无需录入主人声音；DSH 长任务 sidecar 的安装、登录和事件协议见 [DSH sidecar 接入](docs/dsh-sidecar.md)。

2026-11-02：执行链已改为本地优先。常见软件、微信、截图识别和多步桌面任务由咕嘎自己的视觉/UIA 智能体执行；DSH 只用于源码仓库、多文件工程和复杂代码任务。常见流程可保存为持久工作流，重复成功后会生成待审核的经验候选。设计与边界见 [本地优先任务路由](docs/local-first-workflows-20261102.md) 和 [DSH 任务监督](docs/dsh-supervisor-20261102.md)。

形象保持原版贴图，保留原有语音和动作。带侧背壳的 3D 开发预览已暂停，不在普通启动或日常菜单中启用；相关研究归档见 [角色表现层升级](docs/desktop-avatar-research-20261026.md)。

立体动作实验与 Blender 工作副本仅供开发研究，自动演示和普通用户入口已停用，优先保证原画形象一致；[实验记录](docs/avatar-motion-v2.md)。

## 功能概览

新增：[咕嘎智能体与个人软件生态](docs/agent-ecosystem.md)。右键“对话”打开项目/会话悬浮窗；“长语音 · 边聊边执行”使用现有 DeepSeek 模型调用自身工具、打开用户授权的软件，并通过双向 stdio MCP 接入个人软件生态。

智能体采用四层执行：本地快速命令、已保存工作流、咕嘎本地视觉智能体、DSH 复杂代码执行器。微信、腾讯会议到 B 站、截图识屏和普通软件操作默认留在本地，避免把日常任务绕进长任务 sidecar。

新增：[战棋决策实验室（算法候选 + 现有模型 + 动作校验）](docs/battleground-agent.md)。
在右键菜单或托盘打开，可先载入合成局面做离线分析；当前为基础规则子集，尚未实现完整实服自动对局。

### 1. 企鹅助教（Python / 大模型）

按 **数字键 1**（请教助教）打开输入条，提问 Python / 大模型相关问题：

- 咕嘎会**截屏看你的屏幕**（IDE / 文档 / 错误信息），结合你的问题作答
- 回答显示在置顶大对话面板（可拖动、可滚动、保留 30 条历史）
- AI 回答后**自动重新打开输入条**，可连续追问
- 8 秒屏幕分析超时保护：超时则直接用文字提问

按 **数字键 2**（助教出题）：

- 咕嘎出一道 Python/大模型中级工程师思考题
- 题目展示在对话面板，自动打开答题输入条
- 你写出答案后，咕嘎用助教人设点评

### 2. 酒馆战棋军师（保留）

右键菜单 → **分析对局（看屏幕）**：

- 优先读取 `power.log` 日志解析棋盘（零 API 调用，不触发限流）
- 日志不可用时回退 GLM-4.6V 视觉模型截屏分析
- 蒙特卡洛模拟胜率（n=600，避免主线程卡顿）
- 对手阵容流派分析 + 己方阵容方向展望

> 注意：自动分析已关闭，只在手动点击时触发。

**一键拔线**（数字键 4 / F1）：

- 战斗阶段跳战斗动画，需管理员权限
- 30 秒重连保护期，每局 9 次上限，15 秒冷却

### 3. 基础交互

- **数字键 3**：语音对话模式（按住说话）
- **数字键 5**：摇摆动画
- 透明悬浮置顶，可拖动
- 状态系统：饥饿/心情/精力，自动衰减
- 右键托盘菜单：显示/隐藏、退出
- Bongo 面板：按键时面板反馈

## 运行环境

- Windows 10/11
- Python 3.10+（推荐 3.12）
- PyQt5 + Qt 5.15（需 plugins/platforms 配置，见 `launcher.py`）

## 安装

```powershell
pip install -r requirements.txt
```

复制 `ai_config.example.json` 为 `ai_config.json`，填入你自己的 API Key：

```powershell
copy ai_config.example.json ai_config.json
# 然后用记事本编辑 ai_config.json 填入:
# - api_key: DeepSeek API Key
# - zhipu_api_key: 智谱 GLM-4.6V API Key（视觉分析用）
```

## 启动

双击 `启动企鹅.bat`，或：

```powershell
python launcher.py
```

> `run_as_admin` 默认为 `false`。仅在需要"一键拔线"功能时改为 `true`（会触发 UAC 提权）。

## 项目结构

```
guga-penguin-pet/
├── main.py                  # 应用入口
├── launcher.py              # 启动器（配置 Qt 路径、可选 UAC 提权）
├── ai_config.example.json   # 配置模板（脱敏）
├── ai_config.json           # 实际配置（gitignore，含 API Key）
├── requirements.txt
├── 启动企鹅.bat
│
├── core/                    # 核心逻辑
│   ├── ai_chat.py           # AI 对话引擎（DeepSeek + RAG 上下文）
│   ├── tutor.py             # 助教人设/出题/屏幕分析 prompt
│   ├── vision.py            # GLM-4.6V 视觉分析（截屏 + API）
│   ├── log_reader.py        # 炉石日志解析（带 _prune_ents 内存清理）
│   ├── battle_sim.py        # 蒙特卡洛胜率模拟
│   ├── card_db.py           # 随从卡牌库
│   ├── comp_analysis.py     # 对手阵容分析
│   ├── lineup_planner.py    # 阵容规划
│   ├── match_history.py     # 战绩记忆（最近 5 场）
│   ├── keyboard_hook.py     # 全局键盘钩子
│   ├── status_system.py     # 状态系统（饥饿/心情/精力）
│   ├── interaction.py       # 交互动作定义
│   ├── netcut.py            # 一键拔线
│   ├── bg_knowledge.py      # 酒馆战棋知识库
│   └── config_manager.py    # 配置管理
│
├── ui/                      # 界面
│   ├── pet_window.py        # 主窗口（透明悬浮、对话面板、事件过滤）
│   ├── sprite_animator.py   # 2D 精灵动画
│   ├── gl_penguin.py        # 3D 企鹅渲染（实验性，默认关闭）
│   ├── tts_engine.py        # TTS 语音（edge-tts 软萌音 + 豆包兜底）
│   ├── speech_system.py    # 台词库
│   ├── bongo_panel.py       # Bongo 按键面板
│   └── status_bar.py        # 状态栏
│
├── resources/               # 素材
│   ├── expressions/         # 表情 PNG
│   └── stickers/            # 动画贴图
│
├── knowledge/battlegrounds/  # 战棋知识库（JSON）
├── data/cards_zhcn.json      # 卡牌数据
└── tools/                    # 开发辅助脚本
```

## 稳定性设计

- **TTS 跨线程安全**：QMediaPlayer 仅主线程操作，后台线程只合成 mp3，通过 pyqtSignal 回主线程播放
- **临时文件清理**：TTS mp3 用完即删，启动时清理残留，列表限长 4 个
- **内存防泄漏**：log_reader `_prune_ents()` 自动清理非 PLAY 区实体，总量限 3000
- **主线程防阻塞**：蒙特卡洛模拟用 n=600（非 2000），背景采样失败退避
- **跨线程 UI**：后台视觉分析通过类级 pyqtSignal 投递到主线程
- **超时保护**：屏幕分析 8 秒超时，edge-tts 合成 20 秒超时

## 技术栈

- Python 3.12
- PyQt5（QMediaPlayer/QTimer/pyqtSignal/eventFilter）
- 兼容 Chat Completions 的对话 API（实际平台与模型由本地 `ai_config.json` 的 `base_url` / `model` 决定）
- 智谱 GLM-4.6V（视觉）
- edge-tts >= 7.3.0（软萌语音；修复旧版请求被微软服务以 403 拒绝的问题）
- 豆包语音 V3（TTS 兜底）

## License

代码采用 MIT License。角色素材、第三方卡牌数据和可选模型另有来源与许可边界，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 持续语音对话（2026-10-13 升级）

右键企鹅或托盘 → **实时语音配置** → **持续语音对话**。
默认接入 `qwen-audio-3.1-realtime-plus`（千问 AI 平台，无需业务空间 ID），支持持续收音、流式播放、静音、耳机语音打断和断线续接。
仅使用赠送额度：先在千问 AI 平台权益页面为同名模型确认“免费额度用尽即停”保护；未确认时程序阻止联网，额度耗尽后停止。
详细设置、当前限制与回滚方法见 [持续语音使用说明](docs/realtime-voice.md)。

## 普通 Edge 语音排查

普通聊天的朗读仍使用 `zh-CN-XiaoyiNeural`，语速 `+15%`、音调 `+14Hz`；助教模式语速为 `+5%`。千问实时音频仅在主动开启持续语音通话时使用。

2026-10-16 已复现旧版 `edge-tts 7.2.8` 合成时返回 HTTP 403；升级到 `7.3.0` 后合成与 Qt 播放通过。上游修复记录见 [edge-tts #490](https://github.com/rany2/edge-tts/issues/490)。升级依赖后需退出并重新启动桌宠，运行中的 Python 进程会保留已导入的旧版本。

使用桌宠的 Python 运行 `python tools/diag_tts.py` 可验证 Edge 合成及 Qt 播放。该诊断会朗读一句通用测试语，最多运行 30 秒，不使用豆包兜底或麦克风；成功返回 `ok: true`。播放状态通过后，最终听感仍取决于 Windows 输出设备及音量。

## OpenCode Go 普通聊天

本地配置可将 `base_url` 设为 `https://opencode.ai/inference/go/openai/v1`，`model` 设为 `deepseek-v4.1-flash`，API Key 仅保存在 Git 忽略的 `ai_config.json` 中。此调用消耗 Go 套餐额度；普通 Edge 朗读、视觉分析和千问持续通话仍使用各自配置。

聊天引擎为 Go 请求附带自身客户端标识和会话 ID，多轮对话复用 ID，清空历史时更新；不需要本地 CLI 转发或代理。配置更新后需重启桌宠。`python tools/test_ai_go.py` 提供不联网的会话路由检查。
# 战棋后台与语音复盘

长语音和文字智能体现在可调用独立战棋后台：打开游戏、看游戏画面、只读日志、读取内存棋盘、执行一步、有限接管、查询和停止。操作完成按日志回执主动复盘；直接内存执行及当前版本完整规则仍未完成。使用方法、模拟训练结果与边界见 [战棋后台升级说明](docs/battleground-director.md)。
