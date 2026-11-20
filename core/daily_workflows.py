"""User-authorized recurring workflows. Instructions are explicit, inspectable data."""
import re

VIDEO='''【用户的日常工作流：腾讯会议录制到B站投稿】
这是咕嘎已经学会的固定流程，由咕嘎本地的 UIA/截图智能体执行；只有遇到实际无法完成的代码改动时才升级给 DSH。
1. applications查找/打开腾讯会议；读取真实历史会议或云录制列表，按会议时间选择最新一场会议的真实录制。跳过“WorkBuddy”等非会议录屏。
2. 本地录制：从会议详情获取准确路径。云录制：在浏览器打开该录制页面，用账号已有的下载功能导出视频和有时间戳的中文字幕。不可猜下载地址、绕过下载权限或读取浏览器凭据。
3. 若最新会议有多段录制、来源不确定、无法登录或无下载权限，停下来向用户提出一个具体问题后等待；DSH 使用task_report(needs_input)，本地智能体使用task_plan记录后直接询问。不能用下载目录里随便一段视频代替最新录制。
4. media_workflow(inspect)核实真实视频时长和流；extract_frames抽帧；优先使用会议导出的SRT字幕，无字幕才transcribe本地转写，原文件保留。
5. 根据字幕和画面拟定真实准确的标题、简介和标签。render生成新MP4：底部时间进度条+中文字幕；不得用朗读TTS代替字幕识别，不擅自剪掉视频内容。
6. 后台处理用media_workflow(wait)有限等待，不反复截图或紧密轮询；每完成重要阶段，DSH 用task_report(progress,milestone=true)、本地智能体用task_plan说明具体进展。用status核对文件生成且时长一致，预览字幕及底部进度条。
7. media_workflow(submission)保存投稿清单和成片路径，打开 https://member.bilibili.com/platform/upload/video/frame ，确认账号、选择成片并填写标题“AI 全栈开发系列”；简介按视频实际内容生成一段简短准确说明，不套用固定废话。
8. 上传完成后回读标题、封面和简介。本次用户明确要求发布时提交一次并查稿件状态；只要求上传或草稿时到对应步骤止。上传中或草稿不等于已发布。
9. 界面步骤允许升级接力：某一项（找控件、点投稿、上传卡住）连续两次做不成，就把「已完成到哪一步 + 还差哪一步 + 证据」整理成一段话，交给 WorkBuddy 桌面端（workbuddy 工具）接力，并在回执里区分"咕嘎做完的"和"WorkBuddy 做完的"。不要同一动作反复试超过两次。
当前阶段没做完不能冒称完成；若媒体任务仍在运行，不得结束DSH轮次后留下无人监管的后台转码。
'''

REVIEW='''【用户的日常工作流：验收其他智能体的项目】
咕嘎承担独立验收，不把另一个模型的“已完成”当作证据。
1. 识别用户指定项目目录、当前软件和验收标准；不足以定位时只问关键缺失信息，不扫描全部磁盘。
2. 用现有工具打开真实软件，读取控件和屏幕；做真实操作或运行与用户场景相关的测试，核对保存文件、页面、错误状态和交互结果。
3. 保存验收清单，区分已证实、未通过、无法验证；截图只能证明当时画面，不能证明整个项目所有功能正常。
4. 根据实际缺陷形成下一步修改计划，包括复现、预期结果和回归标准。按用户当前指令在WorkBuddy或Codex的正确项目会话中输入计划并提交执行，先核对软件/会话/编辑区；不得串到无关聊天，不自动接受扩大权限或破坏性操作。
5. 对方开始执行不代表修复成功；下轮重新验收。每个验收阶段task_report(progress,milestone=true)，关键产品选择或验收标准矛盾时task_report(needs_input)与用户商量。
6. 单次只做一轮验收和派单，不启动无限的模型互相聊天/自我修改循环。后续迭代保留检查点。
'''

COURSE='''【腾讯会议云录制 → 小鹅通「西工业python」】
本次由咕嘎使用自己的 computer、desktop_command 和 browser 工具执行。网页复用豆包浏览器的现有登录会话。
1. 打开腾讯会议电脑端的「回顾」，按实际时间选择最新正常会议云录制。记录标题、时间和时长。
2. 进入详情的「⋯ → 回顾文件」，如有多流与单流，下载「单流录制（跟随主持人的布局）」整场 MP4。核对文件已落盘、音视频轨道和时长；不能用「说话人」分轨代替。
3. 打开豆包浏览器现有小鹅通素材中心，进入「视频 → 鞠晟驰 → 西工业python」。按名称、时长和素材记录去重。打开网站自己的文件选择器，选择已核实的完整路径，核对待上传文件和「上传到」分组后完成上传。等素材列表显示文件可选。
4. 进入同名专栏「目录管理 → 添加课程 → 批量创建 → 视频 → 选择文件」。按目录实际条目及素材引用逐一对照，只加入缺少的素材。保留原专栏上架方式，单独售卖保持关闭，核对批量数量后保存一次。
5. 刷新目录核对实际新增数、失败数和总数，报告本地路径、素材、课节、转码状态。转码准备中即尚未验证播放。
6. 每阶段用 task_plan 记录实查状态。提交状态不清时先读回列表，无法核实就停止重试。若确定本地工具无法完成，保留已完成步骤和回执，使用一次 codex_task(start) 接力，提示词带上本工作流、已核验内容、具体卡点和重复提交检查。只允许继续这次用户要求的同步范围。遇到登录验证、素材来源不清或待提交结果不明，明确汇报实际卡点。
'''


BUILTINS=(
    dict(id='meeting-to-xiaoe',title='腾讯会议录制同步到西工业python',trigger='同步最新腾讯会议录制到小鹅通',
         instructions=COURSE,state='active',executor='local_agent',budget_seconds=1200),
    dict(id='meeting-to-bilibili',title='腾讯会议录制到B站投稿',trigger='最新会议录制上传B站',
         instructions=VIDEO,state='active',defaults=dict(title='AI 全栈开发系列',
         progress='bottom_time_bar',subtitles='zh',publish_requires_explicit_request=True)),
    dict(id='agent-project-review',title='独立项目验收与反馈',trigger='验收其他智能体项目',
         instructions=REVIEW,state='active'),
)


def workflow_for(text):
    value=str(text).strip()
    if re.search(r'如何|怎么|为什么|是什么|能否实现|可不可以实现|不要|取消',value):return None
    if (re.search(r'同步|上传|补齐|补课|运行', value) and
            re.search(r'小鹅通|西工业\s*python', value, re.I)):
        return dict(BUILTINS[0], builtin=True)
    if ('投稿视频' in value or '帮我投稿' in value or
        '上传视频' in value and not re.search(r'小鹅通|抖音|视频号', value) or
        '最新' in value and '录制' in value and any(x in value for x in ('b站','B站','投稿','上传'))):
        return dict(id='meeting-to-bilibili',title='腾讯会议录制到B站投稿',instructions=VIDEO,
                    defaults=dict(title='AI 全栈开发系列',progress='bottom_time_bar',subtitles='zh',publish_requires_explicit_request=True))
    if any(x in value for x in ('验收项目','验收这个项目','验收一下项目','帮我验收','检查workbuddy','检查codex')):
        return dict(id='agent-project-review',title='独立项目验收与反馈',instructions=REVIEW)
    return None
