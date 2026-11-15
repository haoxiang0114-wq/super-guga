"""Local recording draft. Only the explicit Send action submits a transcript."""
import re
import threading
import time
from PyQt5.QtCore import QObject,pyqtSignal


class ShortVoice(QObject):
    state=pyqtSignal(str)
    transcript=pyqtSignal(str)
    failed=pyqtSignal(str)
    level=pyqtSignal(float)

    def __init__(self,parent=None,factory=None,max_seconds=30):
        super().__init__(parent)
        self.active=False;self.recorder=None;self.generation=0;self.factory=factory
        self.max_seconds=max_seconds;self.phase='idle';self.draft='';self._worker=None
        self.stop_event=threading.Event();self._submit=False
        self.before_start=None

    def _state(self,value):
        self.phase=value;self.state.emit(value)

    def start(self):
        if self.active:return True
        if self._worker and self._worker.is_alive():
            self.failed.emit('上一段录音正在释放麦克风，请稍后再点');return False
        self.active=True;self.draft='';self._submit=False;self.generation+=1
        generation=self.generation;stop=self.stop_event=threading.Event()
        before_start=self.before_start
        self._state('preparing')
        def run():
            recording=False
            try:
                if self.recorder is None:
                    from core.asr import ASRRecorder
                    self.recorder=(self.factory or ASRRecorder)()
                if not self.recorder.available:raise RuntimeError(getattr(self.recorder,'hint','离线语音识别未就绪，请检查 Vosk 模型和麦克风依赖'))
                if stop.is_set() or generation!=self.generation:return
                if before_start:before_start()
                if stop.is_set() or generation!=self.generation:return
                self.recorder.start();recording=True
                if self.recorder.error:raise RuntimeError(self.recorder.error)
                if generation!=self.generation:return
                self._state('recording');deadline=time.monotonic()+self.max_seconds
                while not stop.wait(.08) and time.monotonic()<deadline:
                    if generation==self.generation:self.level.emit(getattr(self.recorder,'level',0.))
                if generation==self.generation:self._state('transcribing')
                text=self.recorder.stop();recording=False
                if generation!=self.generation:return
                if self.recorder.error:raise RuntimeError(self.recorder.error)
                text=re.sub(r'(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])','',str(text).strip())
                if not text:raise RuntimeError('没有识别到语音，请靠近麦克风再试一次')
                if self._submit:
                    self.active=False;self.transcript.emit(text)
                else:
                    self.draft=text;self._state('ready')
            except Exception as exc:
                if generation==self.generation:self.failed.emit(str(exc))
            finally:
                if recording:
                    try:self.recorder.stop()
                    except Exception:pass
                if generation==self.generation and self.phase!='ready':
                    self.active=False;self._state('idle')
        self._worker=threading.Thread(target=run,daemon=True,name='guga-recording')
        self._worker.start();return True

    def stop(self,cancel=False):
        if cancel:
            self.generation+=1;self.active=False;self.draft='';self._submit=False
            self.stop_event.set();self._state('idle');return
        if not self.active:return
        self._submit=True
        if self.phase=='ready':
            text=self.draft;self.draft='';self.active=False
            self.transcript.emit(text);self._state('idle')
        else:self.stop_event.set()
