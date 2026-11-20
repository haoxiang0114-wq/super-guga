# -*- coding: utf-8 -*-
"""咕嘎企鹅语音识别模块 — 麦克风录音 + Vosk 离线中文识别

- 依赖：sounddevice（录音）、vosk（识别）
- 模型：vosk-model-small-cn（首次自动下载到 .zcode/tools/vosk-model-cn）
- 自动增益：录音时按峰值归一化（笔记本麦克风通常声音偏小）
- 用法：
    recorder = ASRRecorder()
    recorder.start()      # 开始录音（后台线程）
    recorder.stop()       # 停止并转写
    text = recorder.result
"""
import json
import logging
import os
import queue
import threading

# 模型下载地址（若缺失）
VOSK_MODEL_URL = 'https://alphacephei.com/vosk/models/vosk-model-small-cn-0.3.zip'

# 注意：vosk 的 C++ 引擎不支持非 ASCII 路径！
# 原始位置在用户目录下（可能含中文），实际加载必须用 ASCII 路径：
# 优先 %TEMP%\vosk-model-cn（复制品），否则用 8.3 短名转换。
_MODEL_SRC = os.path.join(os.environ.get('USERPROFILE', ''), '.zcode', 'tools', 'vosk-model-cn')

# 自动增益目标峰值（int16），太小听不清，太大削波
_GAIN_PEAK = 7000
_MAX_GAIN = 6.0


def _pick_input_device() -> int:
    """自动选择麦克风输入设备（优先名称含 麦克风/Microphone/Mic 的）"""
    try:
        import sounddevice as sd
        devs = sd.query_devices()
        default_dev = sd.default.device
        default_in = None
        if default_dev is not None:
            try:
                default_in = int(default_dev[0])  # InputOutputPair / list
            except Exception:
                default_in = int(default_dev)
        candidates = []
        for i, d in enumerate(devs):
            if d['max_input_channels'] > 0:
                name = str(d['name']).lower()
                if any(k in name for k in ('麦克风', 'microphone', 'mic', 'audio in', 'capture')):
                    candidates.append(i)
        # 优先默认输入设备（若在候选里），否则第一个候选
        if default_in in candidates:
            return int(default_in)
        if candidates:
            return candidates[0]
        return int(default_in) if default_in is not None else 0
    except Exception:
        return 0


def _ascii_path(path: str) -> str:
    """把含中文的路径转成 8.3 短名（ASCII），失败返回原路径"""
    if path.isascii():
        return path
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.windll.kernel32.GetShortPathNameW(path, buf, 1024)
        if n and buf.value:
            return buf.value
    except Exception:
        pass
    return path


def _ensure_model_dir() -> str:
    """返回可用的 ASCII 模型目录（必要时从源复制到 %TEMP%）"""
    # 1) 临时目录里的复制品（ASCII）
    tmp = os.path.join(os.environ.get('TEMP', ''), 'vosk-model-cn')
    if tmp and os.path.isdir(tmp):
        return tmp
    # 2) 源目录的 8.3 短名
    src = _ascii_path(_MODEL_SRC)
    if os.path.isdir(src):
        return src
    # 3) 复制到临时目录
    if os.path.isdir(_MODEL_SRC):
        try:
            import shutil
            shutil.copytree(_MODEL_SRC, tmp)
            return tmp
        except Exception:
            pass
    return _MODEL_SRC


MODEL_DIR = _ensure_model_dir()


class ASRRecorder:
    """按住说话式录音器（start 开始，stop 结束并转写）"""

    def __init__(self):
        self._q = queue.Queue()
        self._stream = None
        self._recording = False
        self._thread = None
        self._recognizer = None
        self._model = None
        self._sd = None
        self._vosk = None
        self.result = ''
        self.error = None
        self.device = None
        self._frame_count = 0
        self._peak = 0
        self.level = 0.
        self._start_time = 0.0
        self.backend='vosk';self._pcm=[]

    @property
    def available(self) -> bool:
        """依赖与模型是否就绪"""
        if not self._check_imports():
            return False
        from core.local_transcription import available
        if available():self.backend='whisper-small';return True
        return self._ensure_model()

    def _check_imports(self):
        try:
            import sounddevice  # noqa
            self._sd = sounddevice
            try:
                import vosk
                self._vosk = vosk
            except ImportError:self._vosk=None
            return True
        except ImportError:
            return False

    def _ensure_model(self) -> bool:
        if self._model is not None:
            return True
        if self._vosk is None:
            return False
        # 检查模型目录（解压后是 vosk-model-small-cn-0.22 子目录）
        if not os.path.isdir(MODEL_DIR):
            return False
        try:
            self._model = self._vosk.Model(MODEL_DIR)
            return True
        except Exception:
            return False

    def start(self):
        if self._recording:
            return
        if not self._check_imports():
            self.error = '缺少 sounddevice/vosk 依赖，请先安装'
            return
        from core.local_transcription import available
        self.backend='whisper-small' if available() else 'vosk'
        if self.backend=='vosk' and not self._ensure_model():
            self.error = f'未找到中文语音模型（{MODEL_DIR}），请先下载'
            return
        self.result = ''
        self.error = None
        self._recording = True
        self._pcm=[];self._q=queue.Queue()
        self._recognizer = self._vosk.KaldiRecognizer(self._model, 16000) if self.backend=='vosk' else None
        self.device = _pick_input_device()
        self._frame_count = 0
        self._peak = 0
        self.level = 0.
        import time as _time
        self._start_time = _time.time()
        self._thread = threading.Thread(target=self._record_loop, daemon=True)
        self._thread.start()

    def _record_loop(self):
        import sounddevice as sd
        try:
            with sd.RawInputStream(samplerate=16000, blocksize=4000, dtype='int16',
                                   channels=1, device=self.device, callback=self._callback):
                while self._recording or not self._q.empty():
                    # 持续消费队列，交给 recognizer
                    try:
                        data = self._q.get(timeout=0.3)
                        self._pcm.append(data)
                        if self._recognizer is not None and self._recognizer.AcceptWaveform(data):
                            res = self._recognizer.Result()
                            import json as _json
                            try:
                                txt = _json.loads(res).get('text', '')
                                if txt:
                                    self.result += txt
                            except Exception:
                                pass
                    except queue.Empty:
                        continue
        except Exception as e:
            self.error = f'录音设备错误: {e}'

    def _callback(self, indata, frames, time_info, status):
        import numpy as np
        raw = bytes(indata)
        self._frame_count += frames
        # 自动增益：按块峰值放大到目标，防止笔记本麦克风声音过小
        try:
            arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
            peak = float(np.abs(arr).max()) if arr.size else 0.0
            self.level = min(1.,float(np.sqrt(np.mean(arr*arr)))/3500.) if arr.size else 0.
            if peak > self._peak:
                self._peak = peak
        except Exception:
            pass
        self._q.put(raw)

    def stop(self):
        """停止录音并完成转写（同步，返回文本）"""
        if not self._recording:
            return self.result
        self._recording = False
        if self._thread:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                self.error='麦克风尚未释放，本轮没有提交识别结果';return ''
        pcm, self._pcm = self._pcm, []
        if self.error:
            return ''
        if self.backend == 'whisper-small':
            try:
                from core.local_transcription import transcribe
                self.result = transcribe(b''.join(pcm))
            except Exception as exc:
                logging.getLogger(__name__).info('Whisper unavailable, using Vosk: %s', type(exc).__name__)
                try:
                    self.result = self._transcribe_backup(pcm)
                    self.backend = 'vosk'
                except Exception:
                    self.error = '离线语音识别暂不可用，请检查本地中文识别模型'
            return self.result.strip()
        # 取残留
        if self._recognizer is not None:
            try:
                res = self._recognizer.FinalResult()
                import json as _json
                try:
                    txt = _json.loads(res).get('text', '')
                    if txt:
                        self.result += txt
                except Exception:
                    pass
            except Exception:
                pass
        return self.result.strip()

    def _transcribe_backup(self, pcm):
        if not self._ensure_model():
            raise RuntimeError('Vosk 中文模型未就绪')
        recognizer = self._vosk.KaldiRecognizer(self._model, 16000)
        phrases = []
        for block in pcm:
            if recognizer.AcceptWaveform(block):
                phrases.append(json.loads(recognizer.Result()).get('text', ''))
        phrases.append(json.loads(recognizer.FinalResult()).get('text', ''))
        return ' '.join(filter(None, phrases))

    def stats(self) -> dict:
        """录音统计（供 UI 提示）：时长秒、峰值、是否捕捉到声音"""
        import time as _time
        dur = _time.time() - self._start_time if self._start_time else 0.0
        return {
            'seconds': round(dur, 1),
            'peak': self._peak,
            'has_audio': self._peak > 500,   # 峰值低于此 = 基本没收到声音
        }


# 便捷函数
def transcribe_wav_path(wav_path: str) -> str:
    """离线转写 wav 文件（16k 单声道）"""
    import vosk
    if not os.path.isdir(MODEL_DIR):
        return ''
    model = vosk.Model(MODEL_DIR)
    rec = vosk.KaldiRecognizer(model, 16000)
    import wave
    with wave.open(wav_path, 'rb') as wf:
        while True:
            data = wf.readframes(4000)
            if len(data) == 0:
                break
            if rec.AcceptWaveform(data):
                pass
    import json as _json
    try:
        return _json.loads(rec.FinalResult()).get('text', '')
    except Exception:
        return ''
