"""Task-specific choices before costly creation; everyday browser actions stay direct."""
import re


def choices_for(text):
    text=re.sub(r'(?:不要|禁止|无需|不必|不能|不允许)[^。；;，,！!\n]*','',str(text))
    if re.search(r'上传|发布',text) and re.search(r'并|然后|接着|最新|多个',text):
        return [('先准备并核对（推荐）','准备好用户要求的内容，核对文件与页面，停在最终发布或提交之前。'),
                ('按明确要求执行','仅执行用户已经明确授权的步骤；关键信息不全时继续询问，不自行发布。')]
    if not re.search(r'制作|生成|绘制|画|设计|开发|重构|实现|整理|分析|写|做|搭建',text):return None
    if re.search(r'网页|网站|html|canvas|动画',text,re.I):
        return [('互动演示（推荐）','做成可以直接运行的独立 HTML，包含动画和必要的交互按钮，在咕嘎黑板里展开。'),
                ('简洁展示','先把主体和布局做清楚，保持轻量，减少装饰和复杂交互，同样在黑板里展示。')]
    if re.search(r'思维导图|流程图|关系图|架构图',text):
        return [('清晰易读（推荐）','优先梳理层级和连线，生成可读 SVG 和简短讲解，在黑板中展示。'),
                ('内容详细','保留更多分支、步骤和说明，生成 SVG 和配套讲解，在黑板中展示。')]
    if re.search(r'项目|代码|报告|文档|视频|多文件|计划方案|分析.*数据|方案',text):
        return [('先做可用版本（推荐）','按当前明确要求完成可用成果，核对关键结果后交付文件和说明。'),
                ('完整细化','细化结构、内容和必要检查，交付完整成果，说明尚未确认的部分。')]
    return None


def reply_choice(text):
    value=str(text).strip().strip('。！! ')
    if value in ('1','一','第一个','选一','选1','方案一','确认执行','按推荐来'):return 1,''
    if value in ('2','二','第二个','选二','选2','方案二'):return 2,''
    if value in ('3','三','第三个','选三','选3','补充要求'):return 3,''
    if value.startswith(('补充：','补充:')):return 3,value[3:].strip()
    if value in ('取消','算了','不执行','先不要'):return 0,''
    return None,''
