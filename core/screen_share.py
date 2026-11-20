"""Memory-only 2-5 fps screen capture plus bounded visual-context sampling."""
import base64
import io
import threading
import time
from collections import deque

import numpy as np
from PIL import Image,ImageGrab
from PyQt5.QtCore import QObject,pyqtSignal

from core.desktop_access import DesktopAccess


def _foreground_snapshot(cache=None):
    try:
        import os,psutil,win32gui,win32process
        hwnd=int(win32gui.GetForegroundWindow())
        if not hwnd:return None
        pid=int(win32process.GetWindowThreadProcessId(hwnd)[1])
        cache=cache if cache is not None else {}
        name=cache.get(pid,'') if pid in cache else ''
        if not name:
            try:name=psutil.Process(pid).name()
            except psutil.Error:name=''
            cache[pid]=name
        left,top,right,bottom=win32gui.GetWindowRect(hwnd)
        return dict(hwnd=hwnd,title=str(win32gui.GetWindowText(hwnd))[:240],pid=pid,
            process=name,rect=[int(left),int(top),int(right),int(bottom)],own_app=pid==os.getpid())
    except Exception:return None


class ScreenShareController(QObject):
    """Capture is continuous; remote vision requests only follow material changes."""
    frame_ready=pyqtSignal(object)
    context_ready=pyqtSignal(object)
    failed=pyqtSignal(str)

    def __init__(self,panel):
        super().__init__(panel if isinstance(panel,QObject) else None)
        self.panel=panel
        settings=getattr(getattr(panel,'pet',None),'_config',None)
        configured=settings._s.value('screen/share_fps',1,type=int) if settings else 1
        self.fps=max(1,min(5,int(configured)))
        self.capture_scope=str(settings._s.value('screen/capture_scope','foreground_window',type=str)) if settings else 'foreground_window'
        self.analysis_interval=max(5.0,float(settings._s.value('screen/context_seconds',8.0,type=float))) if settings else 8.0
        self.max_calls_per_minute=max(1,min(12,int(settings._s.value('screen/max_analyses_per_minute',3,type=int)))) if settings else 3
        self._lock=threading.RLock()
        self._stop=threading.Event();self._thread=None
        self._analysis_active=False;self._last_analysis_request=0.0
        self._active=False;self._seq=0;self._last_at=0.0;self._last_change=0.0
        self._frame_uri='';self._frame_jpeg=b'';self._frame_image=None;self._frame_size=None
        self._gray=None;self._context='';self._context_at=0.0;self._error=''
        self._analysis_count=0;self._analysis_errors=0;self._last_analysis=None
        self._visual_count=0;self._ocr_count=0;self._analysis_backoff_until=0.0
        self._consecutive_errors=0
        self._generation=0
        self._primary_vision_backoff_until=0.0
        self._usage=dict(prompt_tokens=0,completion_tokens=0,total_tokens=0,unreported=0,calls=0)
        self._analysis_request_times=deque(maxlen=60)
        self._reason='';self._started_at=0.0
        self._foreground=None;self._context_foreground=None;self._window_changes=0
        self._capture_errors=0;self._capture_recoveries=0
        self._analysis_pending=False
        self._context_frame_at=0.0
        self._local_perception=None
        self._native_video = False

    def start(self,fps=None,reason='manual', *, native_video=False):
        if fps is not None:
            if type(fps) is not int or not 1<=fps<=5:raise ValueError('共享帧率需要是1到5之间的整数')
            self.fps=fps
        if not DesktopAccess().enabled:raise ValueError('完整访问未开启，不能读取共享屏幕')
        with self._lock:
            already=self._active
            if already:return dict(started=False,already_active=True,**self.status())
            self._generation+=1;generation=self._generation
            self._native_video = bool(native_video)
            self._stop.clear();self._active=True;self._error='';self._reason=str(reason)[:80]
            self._started_at=time.monotonic();self._seq=0;self._frame_uri='';self._frame_jpeg=b''
            self._frame_image=None
            self._context='';self._context_at=0.0;self._last_analysis=None
            self._context_frame_at=0.0
            self._analysis_backoff_until=0.0;self._consecutive_errors=0;self._visual_count=0;self._ocr_count=0
            self._analysis_active=False
            self._primary_vision_backoff_until=0.0
            self._usage=dict(prompt_tokens=0,completion_tokens=0,total_tokens=0,unreported=0,calls=0)
            self._analysis_request_times.clear()
            self._foreground=None;self._context_foreground=None;self._window_changes=0
            self._capture_errors=0;self._capture_recoveries=0
            self._analysis_pending=True;self._gray=None;self._last_at=0.0;self._last_analysis_request=0.0
            if not self._native_video:
                from core.local_perception import LocalPerception
                self._local_perception=LocalPerception()
            self._thread=threading.Thread(target=self._capture_loop,args=(generation,),name='guga-screen-share',daemon=True)
            self._thread.start()
        return dict(started=True,already_active=False,**self.status())

    def stop(self):
        self._stop.set()
        with self._lock:
            self._active=False;self._generation+=1;self._analysis_active=False
            self._frame_image=None;self._frame_uri='';self._frame_jpeg=b''
            self._context='';self._context_at=0.0;self._analysis_pending=False
        thread=self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():thread.join(timeout=1.5)
        local=self._local_perception;self._local_perception=None
        if local:local.close()
        return self.status()

    def status(self):
        now=time.monotonic()
        with self._lock:
            return dict(active=self._active,fps=self.fps,reason=self._reason,sequence=self._seq,
                frame_age_seconds=round(now-self._last_at,2) if self._last_at else None,
                last_change_seconds=round(now-self._last_change,2) if self._last_change else None,
                frame_size=self._frame_size,context_age_seconds=round(now-self._context_at,2) if self._context_at else None,
                context=self._context,analysis_count=self._analysis_count,analysis_errors=self._analysis_errors,
                last_analysis=self._last_analysis,error=self._error,storage='memory_only',
                visual_count=self._visual_count,ocr_count=self._ocr_count,
                analysis_active=self._analysis_active,
                analysis_pending=self._analysis_pending,
                analysis_retry_after_seconds=max(0,round(self._analysis_backoff_until-time.monotonic(),1)),
                primary_vision_retry_after_seconds=max(0,round(self._primary_vision_backoff_until-time.monotonic(),1)),
                usage=dict(self._usage),primary_model=(self._last_analysis or {}).get('model'),
                visual_calls_last_minute=len(self._analysis_request_times),max_visual_calls_per_minute=self.max_calls_per_minute,
                foreground=dict(self._foreground) if self._foreground else None,
                context_foreground=dict(self._context_foreground) if self._context_foreground else None,
                window_changes=self._window_changes,capture_recoveries=self._capture_recoveries,
                capture_scope=self.capture_scope,
                model_bridge='native_video' if self._native_video else 'local_uia_then_change_sampling_vision',
                local_perception={k:v for k,v in self.local_context().items() if k not in ('context','elements')})

    def context(self,max_age=12):
        state=self.status()
        age=state.get('context_age_seconds')
        current=state.get('foreground') or {};context_window=state.get('context_foreground') or {}
        state['context_matches_foreground']=bool(not context_window or not current or context_window.get('hwnd')==current.get('hwnd'))
        # A static page remains useful without paying to re-analyze identical frames.
        with self._lock:
            unchanged=bool(self._context_frame_at and self._last_change<=self._context_frame_at and
                self._last_at and time.monotonic()-self._last_at<2)
            changed=bool(self._context_frame_at and self._last_change>self._context_frame_at)
        state['unchanged_since_analysis']=unchanged
        state['fresh']=bool(state['active'] and not changed and age is not None and (age<=max_age or unchanged) and state['context_matches_foreground'])
        if state['active'] and not state.get('context'):
            last=state.get('last_analysis') or {}
            state['context']=('共享屏幕已开启，最新画面理解暂未完成：'+str(last.get('error') or '仍在分析')+
                '；可以用最新截图或结构化控件核对。') if last else '共享屏幕已开启，首帧画面理解尚未返回；先用截图工具核对当前目标。'
        elif state['active'] and not state.get('context_matches_foreground'):
            state['context']='画面理解对应的是之前的窗口，当前前台已经改变；旧资料不能用于点击，请先读取最新画面。'
        elif state['active'] and changed:
            state['context']='分析采样后画面已经变化，新的画面理解尚未返回；请读取最新截图或结构化控件。'
        elif state['active'] and not state['fresh']:
            state['context']='上一段共享画面理解已超过'+str(max_age)+'秒；它是旧资料，当前状态需要重新观察。'
        return state

    def observation(self,max_age=12):
        """Structured, freshness-bound observation for task planners.

        One id per captured frame. The id only proves what was visible at that
        moment; actions must reference it and re-observe afterwards. `context`
        is a model-generated summary, not trusted evidence by itself.
        """
        state=self.context(max_age)
        frame_age=state.get('frame_age_seconds')
        sequence=state.get('sequence') or 0
        active=bool(state.get('active'))
        matches=state.get('context_matches_foreground')
        if not active:
            why='screen_share_inactive'
        elif frame_age is None or frame_age>max_age:
            why='frame_stale'
        elif not matches:
            why='foreground_changed'
        elif not state.get('context'):
            why='context_pending'
        elif not state.get('fresh'):
            why='context_stale'
        else:
            why=''
        usable=bool(active and state.get('fresh') and frame_age is not None
                    and frame_age<=max_age and matches)
        return dict(
            observation_id=('obs-share-%d-%d' % (sequence,int(round((self._last_at or 0)*1000))))
                if active and sequence else None,
            usable=usable,reason=why,active=active,fresh=bool(state.get('fresh')),
            sequence=sequence,captured_at=round(self._last_at,3) if self._last_at else None,
            frame_age_seconds=frame_age,context_age_seconds=state.get('context_age_seconds'),
            foreground=state.get('foreground'),context_foreground=state.get('context_foreground'),
            context_matches_foreground=matches,context=state.get('context') or '',
            context_trusted=False,frame_size=state.get('frame_size'),capture_scope=state.get('capture_scope'),
            rule=('动作前必须引用 observation_id 或重新观察；usable=false 时先观察；'
                  '动作后重新观察并核对目标变化，不能拿旧画面坐标继续操作'))

    def assist_context(self,max_age=12):
        state=self.context(max_age)
        local=self.local_context()
        if local.get('fresh'):
            state.update(context=local['context'],fresh=True,context_matches_foreground=True,
                context_foreground=state.get('foreground'),context_age_seconds=local['age_seconds'])
        return dict(active=state['active'],fresh=state['fresh'],context=state.get('context',''),
            source='windows_uia' if local.get('fresh') else 'screen_vision',visual_content=not local.get('fresh'),
            foreground=state.get('foreground'),context_foreground=state.get('context_foreground'),
            context_matches_foreground=state.get('context_matches_foreground'),
            unchanged_since_analysis=state.get('unchanged_since_analysis'),
            frame_age_seconds=state.get('frame_age_seconds'),context_age_seconds=state.get('context_age_seconds'),
            sequence=state.get('sequence'),frame_size=state.get('frame_size'),error=state.get('error'))

    def local_context(self):
        local=self._local_perception
        return local.context(self._foreground) if local and self._active else dict(fresh=False,source='windows_uia')

    def snapshot(self):
        with self._lock:
            image=self._frame_image.copy() if self._frame_image is not None else None
            if image is None:return dict(ok=False,error='共享屏幕尚未取得画面；没有发送视觉请求')
            sequence=self._seq;last_at=self._last_at
        buffer=io.BytesIO();image.save(buffer,format='JPEG',quality=80,optimize=False)
        uri='data:image/jpeg;base64,'+base64.b64encode(buffer.getvalue()).decode('ascii')
        with self._lock:return dict(ok=True,_image_url=uri,sequence=sequence,frame_age_seconds=round(time.monotonic()-last_at,2),
                context=self._context,context_age_seconds=round(time.monotonic()-self._context_at,2) if self._context_at else None,
                foreground=dict(self._foreground) if self._foreground else None)

    def native_frame(self):
        with self._lock:
            if (self._active and self._native_video and self._frame_jpeg
                    and time.monotonic() - self._last_at <= 2.0):
                return (self._generation, self._seq), self._frame_jpeg
        return None

    def _capture_loop(self,generation):
        native_video = self._native_video
        interval = 1.0 / (2 if native_video else max(1, min(5, self.fps)))
        process_cache={}
        while not self._stop.is_set() and generation==self._generation:
            started=time.monotonic()
            try:
                foreground=_foreground_snapshot(process_cache)
                capture_box=None
                if (native_video or self.capture_scope=='foreground_window') and foreground and not foreground.get('own_app'):
                    rect=foreground.get('rect') or []
                    if len(rect)==4 and rect[2]-rect[0]>=240 and rect[3]-rect[1]>=180:capture_box=tuple(rect)
                if native_video and capture_box is None:
                    # A focused window is the boundary for remote screen share.
                    # When our controls are frontmost, wait for the next window.
                    with self._lock:
                        if generation != self._generation:return
                        self._frame_jpeg = b''
                    if self._stop.wait(interval):
                        return
                    continue
                frame=ImageGrab.grab(bbox=capture_box,all_screens=True).convert('RGB')
                original=frame.size;frame.thumbnail((1280,720 if native_video else 800),Image.Resampling.BILINEAR)
                jpeg = b''
                if native_video:
                    buffer = io.BytesIO()
                    frame.save(buffer, format='JPEG', quality=70)
                    jpeg = buffer.getvalue()
                gray=np.asarray(frame.resize((64,36),Image.Resampling.BILINEAR).convert('L'),dtype=np.int16)
                with self._lock:
                    if generation!=self._generation or self._stop.is_set():return
                    old=self._gray
                    change=float(np.mean(np.abs(gray-old))) if old is not None else 255.0
                    previous_hwnd=(self._foreground or {}).get('hwnd')
                    window_changed=bool(foreground and foreground.get('hwnd')!=previous_hwnd)
                    self._gray=gray;self._seq+=1;self._last_at=time.monotonic();self._frame_image=frame
                    if native_video:self._frame_jpeg=jpeg
                    self._frame_size=dict(width=frame.width,height=frame.height,original_width=original[0],original_height=original[1])
                    self._foreground=foreground
                    if change>=1.5 or window_changed:self._last_change=self._last_at
                    if window_changed:self._window_changes+=1
                    if self._seq==1 or change>=1.5 or window_changed:self._analysis_pending=True
                    capture_errors=self._capture_errors;self._capture_errors=0
                if capture_errors:self._capture_recoveries+=1
                local=self._local_perception
                if local:local.request(foreground)
                self.frame_ready.emit(dict(sequence=self._seq,captured_at=self._last_at,change=round(change,2),
                    window_changed=window_changed,capture_box=capture_box,size=self._frame_size,foreground=foreground))
                if not native_video and self._analysis_pending and time.monotonic()-self._last_analysis_request>=self.analysis_interval:
                    buffer=io.BytesIO();frame.save(buffer,format='JPEG',quality=58,optimize=False)
                    uri='data:image/jpeg;base64,'+base64.b64encode(buffer.getvalue()).decode('ascii')
                    with self._lock:self._frame_uri=uri;self._frame_jpeg=buffer.getvalue()
                    self._start_analysis(uri,generation,foreground)
            except Exception as exc:
                with self._lock:
                    if generation!=self._generation:return
                    self._capture_errors+=1;count=self._capture_errors
                    self._error='共享屏幕采集暂时失败（第'+str(count)+'次）：'+type(exc).__name__
                if count>=3:
                    with self._lock:self._active=False
                    self.failed.emit(self._error);self._stop.set();break
                self._stop.wait(min(1.0,.25*count))
                continue
            remaining=interval-(time.monotonic()-started)
            if remaining>0:self._stop.wait(remaining)

    def _start_analysis(self,uri,generation,foreground=None):
        with self._lock:
            if generation!=self._generation or self._analysis_active or self._stop.is_set() or time.monotonic()<self._analysis_backoff_until:return
            now=time.monotonic()
            while self._analysis_request_times and now-self._analysis_request_times[0]>=60:self._analysis_request_times.popleft()
            if len(self._analysis_request_times)>=self.max_calls_per_minute:
                self._analysis_backoff_until=max(self._analysis_backoff_until,self._analysis_request_times[0]+60)
                return
            self._analysis_active=True;self._last_analysis_request=time.monotonic()
            self._analysis_pending=False
            self._analysis_request_times.append(self._last_analysis_request)
        threading.Thread(target=self._analyze,args=(uri,generation,foreground,self._last_at),name='guga-screen-context',daemon=True).start()

    def _analyze(self,uri,generation,foreground=None,sampled_at=None):
        from core.screen_visual import describe_frame
        sampled_at=time.monotonic() if sampled_at is None else sampled_at
        try:result=describe_frame(uri,allow_primary=time.monotonic()>=self._primary_vision_backoff_until)
        except Exception as exc:result=dict(ok=False,code='vision_exception',error=type(exc).__name__)
        ocr_text=''
        if not result.get('ok'):
            # OCR is expensive. It is a failure fallback, not a second vision
            # pipeline running on every sampled frame.
            fallback=self._ocr_fallback(uri)
            if fallback.get('ok'):
                ocr_text=fallback['text']
                with self._lock:
                    if generation!=self._generation:return
                    self._context=ocr_text;self._context_at=time.monotonic();self._analysis_count+=1;self._ocr_count+=1
                    self._context_foreground=dict(foreground) if foreground else None
                    self._context_frame_at=sampled_at
                self.context_ready.emit(dict(text=ocr_text,at=self._context_at,sequence=self._seq,fallback='ocr'))
        with self._lock:
            if generation!=self._generation:return
            self._analysis_active=False
            self._last_analysis=result
            self._add_usage(result.get('usage'))
            if result.get('ok'):
                self._context=result['text'];self._context_at=time.monotonic();self._analysis_count+=1
                self._context_foreground=dict(foreground) if foreground else None
                self._context_frame_at=sampled_at
                self._visual_count+=1;self._consecutive_errors=0;self._analysis_backoff_until=0
                primary_error=result.get('primary_error') or {}
                if primary_error.get('code')=='http' and primary_error.get('http_status')==429:
                    self._primary_vision_backoff_until=time.monotonic()+60
            else:
                self._analysis_errors+=1;self._consecutive_errors+=1
                # Retry a failed static first frame at most twice, respecting backoff.
                if self._consecutive_errors<3:self._analysis_pending=True
                backoff=15 if result.get('code')=='http' and result.get('http_status')==429 else min(20,4*self._consecutive_errors)
                self._analysis_backoff_until=time.monotonic()+backoff
                primary_error=result.get('primary_error') or {}
                if primary_error.get('code')=='http' and primary_error.get('http_status')==429:
                    self._primary_vision_backoff_until=time.monotonic()+60
        if result.get('ok'):self.context_ready.emit(dict(text=result['text'],at=self._context_at,sequence=self._seq))

    def _add_usage(self,usage):
        self._usage['calls']+=1
        if not isinstance(usage,dict):
            self._usage['unreported']+=1;return
        for key in ('prompt_tokens','completion_tokens','total_tokens'):
            value=usage.get(key)
            if isinstance(value,(int,float)):self._usage[key]+=max(0,int(value))

    def _ocr_fallback(self,uri):
        started=time.monotonic()
        try:
            header,data=uri.split(',',1)
            image=Image.open(io.BytesIO(base64.b64decode(data))).convert('RGB')
            output=io.BytesIO();image.save(output,format='PNG')
            from core.screen_question import recognize
            text=' '.join(recognize(output.getvalue()).split())[:1000]
            if not text:return dict(ok=False,code='ocr_empty')
            return dict(ok=True,text='本机文字识别：'+text,elapsed_ms=round((time.monotonic()-started)*1000,1))
        except Exception as exc:
            return dict(ok=False,code='ocr_failed',error='本机文字识别未完成：'+type(exc).__name__)
