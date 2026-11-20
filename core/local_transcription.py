"""Accurate local Chinese transcription using an already cached Whisper model."""
import importlib.util
import os
import threading
from pathlib import Path

_lock=threading.Lock()
_model=None
_load_failed=False


def cached_model():
    custom=os.environ.get('GUGA_WHISPER_MODEL','').strip()
    if custom and (Path(custom)/'model.bin').is_file():return Path(custom)
    cache=Path(os.environ.get('HF_HUB_CACHE',str(Path.home()/'.cache/huggingface/hub')))
    for size in ('small','base'):
        root=cache/('models--Systran--faster-whisper-'+size)
        for path in sorted(root.glob('snapshots/*/model.bin')):
            if all((path.parent/n).is_file() for n in ('config.json','tokenizer.json')):return path.parent
    return None


def available():
    return not _load_failed and bool(importlib.util.find_spec('faster_whisper') and cached_model())


def transcribe(pcm,sample_rate=16000):
    import numpy as np
    global _model, _load_failed
    if sample_rate!=16000:raise ValueError('识别输入需要16kHz单声道PCM')
    audio=np.frombuffer(pcm,dtype=np.int16).astype(np.float32)/32768.
    if audio.size<3200 or float(np.sqrt(np.mean(audio*audio)))<.0005:return ''
    # One gain for the whole utterance; never amplify silence in every block.
    peak=float(np.max(np.abs(audio)));audio*=min(4.,.8/max(peak,.001))
    with _lock:
        if _model is None:
            if _load_failed:
                raise RuntimeError('本地 Whisper 加载失败')
            try:
                from faster_whisper import WhisperModel
                path = cached_model()
                if path is None:
                    raise RuntimeError('本地 Whisper 模型未就绪')
                from core.asr import _ascii_path
                _model = WhisperModel(_ascii_path(str(path)), device='cpu', compute_type='float32',
                    cpu_threads=min(4, os.cpu_count() or 2), num_workers=1, local_files_only=True)
            except Exception:
                _load_failed = True
                raise
        segments,info=_model.transcribe(audio,language='zh',beam_size=3,temperature=0,
            condition_on_previous_text=False,vad_filter=False)
        text=''.join(s.text for s in segments if s.no_speech_prob<.85).strip()
        import re
        return re.sub(r'\bguga\b','咕嘎',text,flags=re.I)
