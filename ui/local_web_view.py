"""Off-record renderer for self-contained results. No user browser or login profile."""
import hashlib
from pathlib import Path
from PyQt5.QtCore import QUrl,QTimer
from PyQt5.QtWebEngineCore import QWebEngineUrlRequestInterceptor
from PyQt5.QtWebEngineWidgets import QWebEnginePage,QWebEngineProfile,QWebEngineView,QWebEngineSettings


class InlineOnly(QWebEngineUrlRequestInterceptor):
    def __init__(self,parent):super().__init__(parent);self.blocked=0
    def interceptRequest(self,info):
        if info.requestUrl().scheme() not in ('data','about','blob'):
            self.blocked+=1;info.block(True)


class ResultPage(QWebEnginePage):
    def acceptNavigationRequest(self,url,kind,main):
        return url.scheme() in ('data','about')
    def createWindow(self,kind):return None
    def javaScriptConsoleMessage(self,level,message,line,source):
        if int(level)>=2:self.viewer.errors.append(str(message)[:240])


PROBE="""(()=>{let c=document.querySelector('canvas'),shot='';
try{if(c){let t=document.createElement('canvas');t.width=80;t.height=50;
t.getContext('2d').drawImage(c,0,0,80,50);shot=t.toDataURL()}}catch(e){}
return {title:document.title,ready:document.readyState,canvas_count:document.querySelectorAll('canvas').length,
animations:typeof document.getAnimations==='function'?document.getAnimations().slice(0,12).map(a=>({state:a.playState,time:a.currentTime})):[],
svg_count:document.querySelectorAll('svg').length,buttons:Array.from(document.querySelectorAll('button')).map(b=>b.innerText).slice(0,12),
text:document.body.innerText.slice(0,1600),width:innerWidth,height:innerHeight,frame:shot}})()"""


class LocalWebView(QWebEngineView):
    def __init__(self,parent=None):
        super().__init__(parent);self.path='';self.loaded=False;self.errors=[];self.metrics={}
        self.profile=QWebEngineProfile(self)  # No storageName => off the record.
        self.interceptor=InlineOnly(self.profile);self.profile.setUrlRequestInterceptor(self.interceptor)
        self.profile.downloadRequested.connect(lambda item:item.cancel())
        page=ResultPage(self.profile,self);page.viewer=self;self.setPage(page)
        settings=page.settings()
        for attr in ('LocalContentCanAccessFileUrls','LocalContentCanAccessRemoteUrls','JavascriptCanOpenWindows',
                     'JavascriptCanAccessClipboard','PluginsEnabled','FullScreenSupportEnabled'):
            settings.setAttribute(getattr(QWebEngineSettings,attr),False)
        self.loadFinished.connect(self._loaded)

    def load_file(self,path):
        path=Path(path).resolve()
        if self.path==str(path) and self.loaded:return
        self.load_inline(path.read_text(encoding='utf-8-sig'),str(path))

    def load_inline(self,source,path='free-canvas'):
        if len(source.encode('utf-8'))>1500000:raise ValueError('独立网页超过黑板展示上限（1.5MB）')
        self.path=path;self.loaded=False;self.metrics={};self.errors=[]
        policy="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; media-src data: blob:; connect-src 'none'; frame-src 'none'; object-src 'none'; form-action 'none'; base-uri 'none'"
        self.setHtml('<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="'+policy+'">'+source,QUrl('about:blank'))

    def _loaded(self,ok):
        self.loaded=bool(ok)
        QTimer.singleShot(120,self.probe)

    def probe(self):
        def received(data):
            if not isinstance(data,dict):return
            frame=data.pop('frame','')
            if frame:data['canvas_frame_sha256']=hashlib.sha256(frame.encode()).hexdigest()
            self.metrics=data
        if self.loaded:self.page().runJavaScript(PROBE,received)

    def state(self):
        self.probe()
        return dict(path=self.path,loaded=self.loaded,off_the_record=self.profile.isOffTheRecord(),
            blocked_requests=self.interceptor.blocked,javascript_errors=self.errors[-6:],**self.metrics)


FREE_CANVAS='''<html lang="zh-CN"><title>咕嘎 · 自由画布</title><style>
body{margin:0;background:#fcfaf4;font:15px "Microsoft YaHei",sans-serif;color:#3b4355}
header{padding:12px;display:flex;gap:12px;align-items:center}button{padding:7px 18px;border:0;border-radius:12px;background:#e6eafa;color:#35416d}
canvas{display:block;touch-action:none;background:white}</style><header>自由画布 <input id="color" type="color" value="#5076c5">
<input id="width" type="range" min="1" max="20" value="4"><button id="clear">清空</button></header><canvas></canvas>
<script>const c=document.querySelector('canvas'),x=c.getContext('2d');let down=false;
function resize(){let saved=document.createElement('canvas');saved.width=c.width;saved.height=c.height;saved.getContext('2d').drawImage(c,0,0);
c.width=innerWidth;c.height=Math.max(100,innerHeight-60);x.drawImage(saved,0,0)}onresize=resize;resize();
c.onpointerdown=e=>{down=true;c.setPointerCapture(e.pointerId);x.beginPath();x.moveTo(e.offsetX,e.offsetY)};
c.onpointermove=e=>{if(!down)return;x.strokeStyle=document.querySelector('#color').value;x.lineWidth=+document.querySelector('#width').value;x.lineCap='round';x.lineTo(e.offsetX,e.offsetY);x.stroke()};
c.onpointerup=c.onpointercancel=()=>down=false;document.querySelector('#clear').onclick=()=>x.clearRect(0,0,c.width,c.height);</script></html>'''
