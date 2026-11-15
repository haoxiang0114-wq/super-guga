# -*- coding: utf-8 -*-
"""咕嘎企鹅语音识别模块 — 麦克风录音 + 千问在线 / Vosk 离线识别

两种后端：
- qwen ：千问 qwen3-asr-flash（准，需要 API Key 和网络）
- vosk ：本地 vosk-model-small-cn（离线，无网络依赖）
后端选择：asr_backend = auto / qwen / vosk
    auto（默认）配置了千问密钥就走千问，千问失败时回落到本地 Vosk；
    本地模型不存在则保持千问并在失败时明确报错。

- 依赖：sounddevice（录音）、vosk（离线识别，可选）
- 模型：vosk-model-small-cn（在 .zcode/tools/vosk-model-cn）
- 自动增益：录音时按峰值归一化（笔记本麦克风通常声音偏小）
- 用法：
    recorder = ASRRecorder()
    recorder.start()      # 开始录音（后台线程）
    recorder.stop()       # 停止并转写
    text = recorder.result
"""
import os
import queue
import threading

# 单次录音上限（30 秒，16k/16bit 单声道 ≈ 960KB），防止长按把内存吃满
_MAX_PCM_BYTES = 16000 * 2 * 30
# 少于 0.35 秒基本是误触
_MIN_PCM_BYTES = 16000 * 2 * 35 // 100

_VOSK_MODEL = None
_VOSK_LOCK = threading.Lock()

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


def resolve_backend(preferred=None):
    """返回实际使用的后端名：qwen 或 vosk。"""
    if preferred in ('qwen', 'vosk'):
        return preferred
    from core.qwen_speech import available, read_raw, speech_settings
    raw = read_raw()
    wanted = str(raw.get('asr_backend') or (raw.get('qwen_speech') or {}).get('asr_backend') or 'auto').lower()
    if wanted in ('qwen', 'vosk'):
        return wanted
    return 'qwen' if available(speech_settings(raw)) else 'vosk'


def _local_model():
    """懒加载本地 Vosk 模型（只加载一次，供千问失败时兜底）。"""
    global _VOSK_MODEL
    if _VOSK_MODEL is not None:
        return _VOSK_MODEL
    with _VOSK_LOCK:
        if _VOSK_MODEL is not None:
            return _VOSK_MODEL
        import vosk
        if not os.path.isdir(MODEL_DIR):
            return None
        try:
            _VOSK_MODEL = vosk.Model(MODEL_DIR)
        except Exception:
            return None
    return _VOSK_MODEL


def local_available() -> bool:
    """本地 Vosk 是否可用（小模型平铺在根目录，大模型在 am/ 下）。"""
    try:
        import vosk  # noqa: F401
    except ImportError:
        return False
    if not os.path.isdir(MODEL_DIR):
        return False
    return (os.path.isfile(os.path.join(MODEL_DIR, 'final.mdl'))
            or os.path.isdir(os.path.join(MODEL_DIR, 'am')))


def transcribe_pcm_local(pcm, rate=16000) -> str:
    """用本地 Vosk 识别一段裸 PCM（兜底路径）。"""
    if not pcm:
        return ''
    model = _local_model()
    if model is None:
        return ''
    try:
        import vosk
        import json as _json
        recognizer = vosk.KaldiRecognizer(model, rate)
        text = ''
        step = rate * 2
        for start in range(0, len(pcm), step):
            if recognizer.AcceptWaveform(bytes(pcm[start:start + step])):
                text += _json.loads(recognizer.Result()).get('text', '')
        text += _json.loads(recognizer.FinalResult()).get('text', '')
        return text.strip()
    except Exception:
        return ''


class ASRRecorder:
    """按住说话式录音器（start 开始，stop 结束并转写）"""

    def __init__(self, backend=None):
        self._q = queue.Queue()
        self._stream = None
        self._recording = False
        self._thread = None
        self._recognizer = None
        self._model = None
        self._sd = None
        self._vosk = None
        self._pcm = bytearray()
        self.backend = resolve_backend(backend)
        self.result = ''
        self.error = None
        self.device = None
        self._frame_count = 0
        self._peak = 0
        self.level = 0.
        self._start_time = 0.0

    @property
    def hint(self) -> str:
        """没有就绪时给用户看的话（区分在线/离线通道）。"""
        if self.backend == 'qwen':
            return '千问语音没就绪：检查 API Key 和网络'
        return '离线语音识别没就绪：检查 Vosk 模型和麦克风依赖'

    @property
    def available(self) -> bool:
        """依赖与模型是否就绪"""
        try:
            import sounddevice  # noqa
        except ImportError:
            return False
        if self.backend == 'qwen':
            from core.qwen_speech import available, speech_settings
            return available(speech_settings())
        return self._check_imports() and self._ensure_model()

    def _check_imports(self):
        try:
            import sounddevice  # noqa
            import vosk  # noqa
            self._sd = sounddevice
            self._vosk = vosk
            return True
        except ImportError:
            return False

    def _ensure_model(self) -> bool:
        if self._model is not None:
            return True
        import vosk
        # 检查模型目录（解压后是 vosk-model-small-cn-0.22 子目录）
        if not os.path.isdir(MODEL_DIR):
            return False
        try:
            self._model = vosk.Model(MODEL_DIR)
            return True
        except Exception:
            return False

    def start(self):
        if self._recording:
            return
        try:
            import sounddevice as _sd
            self._sd = _sd
        except ImportError:
            self.error = '缺少 sounddevice 依赖，请先安装'
            return
        if self.backend == 'qwen':
            from core.qwen_speech import available, speech_settings
            if not available(speech_settings()):
                self.error = self.hint
                return
        else:
            if not self._check_imports():
                self.error = '缺少 vosk 依赖，请先安装'
                return
            if not self._ensure_model():
                self.error = f'未找到中文语音模型（{MODEL_DIR}），请先下载'
                return
        self.result = ''
        self.error = None
        self._pcm = bytearray()
        self._recording = True
        self._recognizer = self._vosk.KaldiRecognizer(self._model, 16000) \
            if self.backend == 'vosk' else None
        self.device = _pick_input_device()
        self._frame_count = 0
        self._peak = 0
        self.level = 0.
        import time as _time
        self._start_time = _time.time()
        self._thread = threading.Thread(target=self._record_loop, daemon=True)
        self._thread.start()

    def _collect(self, data):
        """把一块音频交给当前后端：Vosk 流式识别，或累积给千问整段识别。"""
        if self._recognizer is not None:
            try:
                if self._recognizer.AcceptWaveform(data):
                    import json as _json
                    try:
                        txt = _json.loads(self._recognizer.Result()).get('text', '')
                        if txt:
                            self.result += txt
                    except Exception:
                        pass
            except Exception:
                pass
            return
        if len(self._pcm) < _MAX_PCM_BYTES:
            self._pcm.extend(data)

    def _drain(self):
        """取干净队列里的尾部音频（停止瞬间可能还有几帧）。"""
        while True:
            try:
                data = self._q.get_nowait()
            except queue.Empty:
                return
            self._collect(data)

    def _record_loop(self):
        import sounddevice as sd
        try:
            with sd.RawInputStream(samplerate=16000, blocksize=4000, dtype='int16',
                                   channels=1, device=self.device, callback=self._callback):
                while self._recording:
                    # 持续消费队列，交给当前后端
                    try:
                        self._collect(self._q.get(timeout=0.3))
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
            if peak > 0:
                gain = min(_MAX_GAIN, _GAIN_PEAK / peak)
                arr = arr * gain
                np.clip(arr, -32767, 32767, out=arr)
                raw = arr.astype(np.int16).tobytes()
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
        # 取残留
        self._drain()
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
        return self._finish_qwen()

    def _finish_qwen(self) -> str:
        """千问通道：整段 PCM → WAV → qwen3-asr-flash；失败回落本地 Vosk。"""
        if len(self._pcm) < _MIN_PCM_BYTES:
            self.error = '录音太短，按住说话再说一次'
            return ''
        from core.qwen_speech import pcm_to_wav
        pcm = bytes(self._pcm)
        try:
            from core.qwen_speech import transcribe
            text = transcribe(pcm_to_wav(pcm), 'audio/wav').strip()
            if text:
                self.result = text
                return text
        except Exception as exc:
            self.error = str(exc) or '千问语音识别失败'
        # 在线识别不可用时，能本地兜底就兜底，不让主人白说一遍
        if local_available():
            local = transcribe_pcm_local(pcm)
            if local:
                self.result = local
                self.error = None
                return local
        return ''

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
