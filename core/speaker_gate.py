"""Local owner-only speaker gate. Audio never leaves this process until verified."""
import json
import os
from pathlib import Path
import threading
import time

import numpy as np

ROOT=Path(__file__).resolve().parents[1]
MODEL_PATH=ROOT/'resources/models/speaker/3dspeaker_eres2net_base_16k.onnx'
PROFILE_PATH=ROOT/'runtime/voice/owner_profile.npy'
META_PATH=ROOT/'runtime/voice/owner_profile.json'
SAMPLE_RATE=16000
_EXTRACTOR=None


def _get_extractor(model_path=MODEL_PATH):
    global _EXTRACTOR
    if _EXTRACTOR is not None:return _EXTRACTOR
    import onnxruntime as ort
    opts=ort.SessionOptions();opts.inter_op_num_threads=1;opts.intra_op_num_threads=1
    session=ort.InferenceSession(str(model_path),sess_options=opts,providers=['CPUExecutionProvider'])
    meta=session.get_modelmeta().custom_metadata_map
    _EXTRACTOR=dict(session=session,sample_rate=int(meta.get('sample_rate','16000')),
        normalize_samples=int(meta.get('normalize_samples','1')),normalize_type=meta.get('feature_normalize_type','global-mean'))
    return _EXTRACTOR


def _embedding(samples,model_path=MODEL_PATH):
    import kaldi_native_fbank as knf
    model=_get_extractor(model_path)
    samples=np.asarray(samples,dtype=np.float32).reshape(-1)
    if not model['normalize_samples']:samples=samples*32768
    opts=knf.FbankOptions();opts.frame_opts.dither=0;opts.frame_opts.samp_freq=model['sample_rate']
    opts.frame_opts.snip_edges=True;opts.mel_opts.num_bins=80;opts.mel_opts.debug_mel=False
    fbank=knf.OnlineFbank(opts);fbank.accept_waveform(model['sample_rate'],samples);fbank.input_finished()
    frames=[fbank.get_frame(i) for i in range(fbank.num_frames_ready)]
    if not frames:raise ValueError('声纹音频太短')
    feature=np.stack(frames,axis=0).astype(np.float32)
    if model['normalize_type']=='global-mean':feature-=feature.mean(axis=0,keepdims=True)
    session=model['session']
    return np.asarray(session.run([session.get_outputs()[0].name],{session.get_inputs()[0].name:feature[None,:,:]})[0][0],dtype=np.float32)


def cosine(a,b):
    a=np.asarray(a,dtype=np.float32).reshape(-1);b=np.asarray(b,dtype=np.float32).reshape(-1)
    denom=float(np.linalg.norm(a)*np.linalg.norm(b))
    return float(np.dot(a,b)/denom) if denom else 0.0


def threshold_from_pairs(pairs,default=.30):
    """3D-Speaker cosine scores are commonly around .2-.5, not .7+."""
    values=sorted(float(x) for x in pairs if np.isfinite(x))
    if not values:return default
    middle=values[len(values)//2]
    return max(.24,min(.46,middle-.05))


class LocalSpeakerGate:
    def __init__(self, profile_path=PROFILE_PATH, model_path=MODEL_PATH, threshold=None):
        self.profile_path=Path(profile_path);self.model_path=Path(model_path)
        if not self.profile_path.is_file():raise ValueError('尚未录入主人声纹；请先运行“录入主人声音.bat”')
        if not self.model_path.is_file():raise ValueError('缺少声纹模型；请运行 tools/download_speaker_model.py')
        self.owner=np.asarray(np.load(self.profile_path),dtype=np.float32).reshape(-1)
        metadata={}
        try:metadata=json.loads(self.profile_path.with_suffix('.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):pass
        self.threshold=float(threshold if threshold is not None else metadata.get('threshold',0.30))
        if str(self.model_path)!=str(MODEL_PATH):_get_extractor(self.model_path)
        else:_get_extractor()
        self.lock=threading.Lock()
        self.recent=[];self.pending=[];self.last_eval=0.;self.last_speech=0.
        self.voiced_samples=0
        self.verified=False;self.state='listening';self.last_score=0.;self._rejections=0

    def embedding(self,samples):
        return _embedding(samples,self.model_path)

    def reset(self):
        self.recent.clear();self.pending.clear();self.voiced_samples=0
        self.verified=False;self.last_eval=0.;self.last_speech=0.;self.state='listening';self._rejections=0

    def process_pcm(self,raw,now=None,blocked=False):
        """Verify a buffered phrase, then release its original PCM exactly once.

        Short pauses belong to the same phrase. Rejected/unverified PCM never
        leaves this gate; identity expires after 700 ms of actual silence.
        Capture timestamps, not inference completion times, delimit phrases.
        """
        now=time.monotonic() if now is None else now
        data=np.frombuffer(raw,dtype=np.int16).astype(np.float32)/32768.0
        rms=float(np.sqrt(np.mean(data*data))) if len(data) else 0.
        speech=rms>=.004
        silent=b'\x00'*len(raw)
        with self.lock:
            if blocked:
                self.reset()
                return silent
            if not speech and not self.pending and not self.verified:
                return silent
            if not speech and now-self.last_speech>=.70:
                discarded=sum(len(x) for x in self.pending)
                self.reset()
                return b'\x00'*(discarded+len(raw))
            self.recent.append(data)
            keep=int(SAMPLE_RATE*2.0)
            while sum(len(x) for x in self.recent)>keep:self.recent.pop(0)
            if self.verified:
                if speech:self.last_speech=now
                return bytes(raw)
            # Buffer both speech and its brief internal pauses. Never append the
            # current block again after successful verification.
            self.pending.append(bytes(raw))
            if speech:
                self.last_speech=now
                self.voiced_samples+=len(data)
                window=np.concatenate(self.recent)
                if self._can_eval(now,window):
                    score=cosine(self.owner,self.embedding(window))
                    self.last_score=score
                    if score>=self.threshold:
                        self.verified=True;self.state='owner'
                        release=b''.join(self.pending);self.pending.clear()
                        return release
                    self._rejections+=1
                    self.state='rejected'
            # Bound rejected buffers; allow a longer sample to verify instead of
            # deleting the beginning after two short, inconclusive comparisons.
            if sum(len(x) for x in self.pending)>=3*SAMPLE_RATE*2:
                discarded=sum(len(x) for x in self.pending)
                self.reset();self.state='rejected'
                return b'\x00'*discarded
            return b''

    def _can_eval(self,now,window):
        if now-self.last_eval<.32:return False
        if self.voiced_samples<int(SAMPLE_RATE*.64):return False
        if len(window)<int(SAMPLE_RATE*.64):return False
        self.last_eval=now;return True


def enroll(samples):
    if not MODEL_PATH.is_file():raise ValueError('缺少声纹模型；请先运行 tools/download_speaker_model.py')
    _get_extractor()
    vectors=[_embedding(x) for x in samples]
    pairs=[cosine(vectors[i],vectors[j]) for i in range(len(vectors)) for j in range(i+1,len(vectors))]
    if pairs and min(pairs)<.20:raise ValueError('几段声音差异过大，请在同一种环境和距离重新录入')
    owner=np.mean(np.stack(vectors),axis=0);owner/=max(1e-8,float(np.linalg.norm(owner)))
    PROFILE_PATH.parent.mkdir(parents=True,exist_ok=True)
    np.save(PROFILE_PATH,owner)
    threshold=threshold_from_pairs(pairs)
    META_PATH.write_text(json.dumps(dict(created=time.time(),threshold=threshold,pair_scores=pairs,
        model=MODEL_PATH.name,sample_rate=SAMPLE_RATE),ensure_ascii=False,indent=2),encoding='utf-8')
    return dict(profile=str(PROFILE_PATH),threshold=threshold,pair_scores=pairs)


class SpeakerGate:
    """Isolate ONNX Runtime from Qt/pythonnet while preserving a synchronous audio stream."""
    def __init__(self,profile_path=PROFILE_PATH,model_path=MODEL_PATH,threshold=None,emit=None):
        import queue,subprocess,sys
        self.emit=emit or (lambda data:None);self.state='listening';self.last_score=0.;self._closed=False
        self.error='';self.stats={};self._max_queue=0
        worker=ROOT/'tools/speaker_worker.py'
        self.proc=subprocess.Popen([sys.executable,'-X','utf8',str(worker)],stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf-8',bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        init=dict(cmd='init',profile=str(profile_path),model=str(model_path),threshold=threshold)
        reply=self._exchange(init)
        if not reply.get('ok'):self.close();raise ValueError(reply.get('error','声纹进程初始化失败'))
        self._queue=queue.Queue(maxsize=24);self._thread=threading.Thread(target=self._run,daemon=True);self._thread.start()

    def _exchange(self,value):
        import json
        self.proc.stdin.write(json.dumps(value,ensure_ascii=False)+'\n');self.proc.stdin.flush()
        line=self.proc.stdout.readline()
        if not line:raise RuntimeError('声纹进程意外退出')
        return json.loads(line)

    def submit(self,raw,blocked=False):
        if self._closed:return False
        if self.error:return False
        import queue
        try:
            self._queue.put_nowait((bytes(raw),time.monotonic(),bool(blocked)))
            self._max_queue=max(self._max_queue,self._queue.qsize())
            return True
        except queue.Full:
            self.error='声纹处理队列已满，已停止收音，避免丢字；请检查 CPU 占用后重新开启通话'
            self.state='error'
            return False

    def _run(self):
        import base64,time
        while not self._closed:
            try:raw,captured,blocked=self._queue.get(timeout=.2)
            except Exception:continue
            try:
                if self.error:break
                if time.monotonic()-captured>1.2:raise RuntimeError('声纹处理延迟超过 1.2 秒，请检查 CPU 占用后重开通话')
                reply=self._exchange(dict(cmd='audio',pcm=base64.b64encode(raw).decode('ascii'),now=captured,blocked=blocked))
                if not reply.get('ok'):raise RuntimeError(reply.get('error','声纹处理失败'))
                self.state=reply.get('state','listening');self.last_score=float(reply.get('score',0))
                self.stats=dict(reply.get('stats',{}),queue_depth=self._queue.qsize(),max_queue_depth=self._max_queue)
                self.emit(base64.b64decode(reply.get('out','')))
            except Exception as exc:
                self.error=str(exc);self.state='error';self.emit(b'');break

    def close(self):
        if self._closed:return
        self._closed=True
        try:self.proc.stdin.write('{"cmd":"close"}\n');self.proc.stdin.flush()
        except Exception:pass
        try:self.proc.terminate();self.proc.wait(timeout=2)
        except Exception:
            try:self.proc.kill()
            except Exception:pass
        worker=getattr(self,'_thread',None)
        if worker is not None and worker is not threading.current_thread():worker.join(timeout=1)
        for pipe in (self.proc.stdin,self.proc.stdout):
            try:pipe.close()
            except Exception:pass
