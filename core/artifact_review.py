"""Inspect explicit final artifacts; never scan unrelated deliveries or run executables."""
import json
import re
import subprocess
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

PREVIEWABLE={'.svg','.png','.jpg','.jpeg','.webp','.gif','.pdf','.md','.txt','.csv','.html','.htm','.pptx','.docx','.xlsx','.mp4','.mov','.webm','.zip'}
DIAGRAM=re.compile(r'思维导图|流程图|关系图|架构图|mind\s*map|flow\s*chart',re.I)


def delivery_instructions(goal):
    if not re.search(r'生成|制作|绘制|画|写|导出|保存|文件|文档|报告|视频|思维导图|流程图',str(goal),re.I):return ''
    text=('\n【成果交付约定】将本次最终文件保存到本机，最终回复逐行列出真实绝对路径和简短内容说明。'
          '仅列本次生成的最终文件；列清未完成项和真实检查结果，不把中间日志当成果。'
          '咕嘎会自己读取文件、展示、打开并向用户讲解，你不要代替咕嘎播报或重复打开。')
    if re.search(r'网页|网站|html|canvas|动画',str(goal),re.I):
        text+=('网页或Canvas成果保存为可直接运行的独立HTML，CSS、JavaScript、SVG等全部内嵌，大小小于1.5MB。'
               '不使用外部图片、CDN、网络接口或本地相邻资源；黑板会隔离这些访问。'
               '动画需要清晰主体和必要的暂停/继续等按钮，适应黑板内可变宽高。'
               '不要启动浏览器、Chrome、ego或Playwright预览，由咕嘎在成果黑板中实际打开。')
    if DIAGRAM.search(str(goal)):
        text+=('思维导图、简单流程图必须实际绘制为独立SVG（含可读中文节点和连线），同时保存一份Markdown讲解。'
               'SVG不含脚本、外部图片、外部字体或网络链接。不只给Mermaid代码或口头说明。')
    return text


def inspect_file(path):
    path=Path(path).resolve();stat=path.stat()
    result=dict(path=str(path),name=path.name,size=stat.st_size,mtime_ns=stat.st_mtime_ns,
                suffix=path.suffix.lower(),readable=False,checks=[],excerpt='',preview_kind='text')
    if not stat.st_size:raise ValueError('文件为空')
    suffix=result['suffix']
    if suffix not in PREVIEWABLE:raise ValueError('此类型不自动打开')
    if suffix in ('.svg','.md','.txt','.csv','.html','.htm'):
        if stat.st_size>4*1024*1024:raise ValueError('文本或SVG过大，未自动预览')
        text=path.read_text(encoding='utf-8-sig')
        if suffix=='.svg':
            external_urls=any(not re.fullmatch(r'''\s*['"]?#[\w.-]+['"]?\s*''',target)
                              for target in re.findall(r'url\s*\((.*?)\)',text,re.I|re.S))
            if external_urls or re.search(r'<!DOCTYPE|<!ENTITY|<\s*(?:script|foreignObject)\b|\bon\w+\s*=|\bhref\s*=|@import',text,re.I):
                raise ValueError('SVG含脚本或外部资源引用，未自动预览和打开')
            root=ET.fromstring(text)
            if root.tag.split('}')[-1]!='svg':raise ValueError('SVG根节点无效')
            labels=[''.join(e.itertext()).strip() for e in root.iter() if e.tag.split('}')[-1]=='text']
            result.update(preview_kind='image',excerpt='；'.join(labels)[:4000],
                          checks=['SVG结构可解析','图中文字：'+str(len(labels))+'组'])
        else:
            if suffix in ('.html','.htm'):
                if stat.st_size>1500000:raise ValueError('网页超过1.5MB，未自动预览')
                result['preview_kind']='html'
                text=re.sub(r'<(script|style)\b.*?</\1>','',text,flags=re.I|re.S)
                text=re.sub('<[^>]+>',' ',text)
            result.update(excerpt=text[:12000],checks=['已读取文件正文'])
    elif suffix in ('.png','.jpg','.jpeg','.webp','.gif'):
        from PIL import Image
        with Image.open(path) as im:
            width,height=im.size
            if width*height>50_000_000:raise ValueError('图片过大，未自动预览')
            im.verify()
        result.update(preview_kind='image',checks=[f'图片可解码，{width} × {height}'])
    elif suffix in ('.docx','.pptx','.xlsx','.zip'):
        with zipfile.ZipFile(path) as z:
            entries=z.infolist()
            if len(entries)>10000 or sum(x.file_size for x in entries)>100*1024*1024:raise ValueError('压缩内容过大，未自动检查')
            if suffix=='.docx':names=['word/document.xml']
            elif suffix=='.pptx':names=[x.filename for x in entries if re.fullmatch(r'ppt/slides/slide\d+\.xml',x.filename)]
            elif suffix=='.xlsx':names=[x.filename for x in entries if x.filename=='xl/sharedStrings.xml']
            else:names=[]
            if suffix=='.docx' and names[0] not in z.namelist():raise ValueError('Word正文缺失')
            if suffix=='.pptx' and not names:raise ValueError('演示文稿没有页面')
            if suffix=='.xlsx' and 'xl/workbook.xml' not in z.namelist():raise ValueError('工作簿结构缺失')
            parts=[]
            for name in names[:20]:
                node=ET.fromstring(z.read(name))
                parts.extend(e.text for e in node.iter() if e.tag.split('}')[-1] in ('t','v') and e.text)
            result.update(excerpt='\n'.join(parts)[:12000],checks=['文件结构可解析，内部文件 '+str(len(entries))+' 项'])
    elif suffix=='.pdf':
        import fitz
        with fitz.open(path) as doc:
            if not doc.page_count:raise ValueError('PDF没有页面')
            result.update(excerpt='\n'.join(doc[i].get_text() for i in range(min(3,len(doc))))[:12000],
                          checks=['PDF可读取，共 '+str(len(doc))+' 页'],preview_kind='pdf')
    else:
        proc=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration:stream=codec_type,width,height',
                             '-of','json',str(path)],capture_output=True,text=True,timeout=8,
                            creationflags=0x08000000)
        if proc.returncode:raise ValueError('视频探测未通过')
        data=json.loads(proc.stdout)
        if not any(x.get('codec_type')=='video' for x in data.get('streams',[])):raise ValueError('未找到视频轨')
        result['checks']=['视频轨可读取，时长 '+str(data.get('format',{}).get('duration','未知'))+' 秒']
    result['readable']=True
    return result


def collect(text,workspace=None,since=0):
    from core.file_delivery import extract_paths
    files=[];seen=set()
    for raw in extract_paths(text):
        path=Path(raw).expanduser()
        if not path.is_absolute():
            if workspace is None:continue
            path=Path(workspace)/path
        path=path.resolve();key=str(path).casefold()
        if key in seen or path.suffix.lower() not in PREVIEWABLE:continue
        seen.add(key)
        if len(files)>=8:break
        # Only this attempt's files may be read, auto-opened or presented as new results.
        try:
            if not path.is_file() or (since and path.stat().st_mtime<since-2):continue
            item=inspect_file(path)
        except Exception as exc:
            item=dict(path=str(path),name=path.name,readable=False,error=str(exc)[:180])
        files.append(item)
    return files


def still_current(item):
    if not item.get('readable'):return False
    try:
        p=Path(item['path']);s=p.stat()
        return p.suffix.lower() in PREVIEWABLE and s.st_size==item['size'] and s.st_mtime_ns==item['mtime_ns']
    except OSError:return False
