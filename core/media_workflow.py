"""Local, bounded media jobs for a real meeting-to-publishing workflow.

Originals are read-only. Publishing is performed separately in the browser.
"""
import base64,json,re,subprocess,threading,time,uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
VIDEO_SUFFIXES={'.mp4','.mkv','.mov','.webm'}


def ffmpeg_path():
    import shutil
    for path in (ROOT/'runtime/music/bin/ffmpeg.exe',ROOT/'vendor/ffmpeg.exe'):
        if path.is_file():return str(path)
    value=shutil.which('ffmpeg')
    if not value:raise ValueError('缺少FFmpeg，尚未具备视频处理依赖')
    return value


def inspect_video(path):
    path=Path(path).expanduser().resolve(strict=True)
    if path.suffix.lower() not in VIDEO_SUFFIXES:raise ValueError('请选择MP4/MKV/MOV/WebM视频文件')
    run=subprocess.run([ffmpeg_path(),'-hide_banner','-i',str(path)],capture_output=True,
                       encoding='utf-8',errors='replace',timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
    match=re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)',run.stderr)
    if not match or 'Video:' not in run.stderr:raise ValueError('文件尚未完成下载或不是可解码视频')
    h,m,s=map(float,match.groups());duration=h*3600+m*60+s
    return dict(path=str(path),seconds=round(duration,3),size=path.stat().st_size,audio='Audio:' in run.stderr)


def srt_timestamp(seconds):
    total=max(0,round(float(seconds)*1000));h,rest=divmod(total,3600000);m,rest=divmod(rest,60000);s,ms=divmod(rest,1000)
    return f'{h:02}:{m:02}:{s:02},{ms:03}'


def validate_srt(path,duration):
    text=Path(path).read_text(encoding='utf-8-sig')
    pattern=r'(\d\d):(\d\d):(\d\d)[,.](\d{3})\s*-->\s*(\d\d):(\d\d):(\d\d)[,.](\d{3})'
    cues=re.findall(pattern,text)
    if not cues:raise ValueError('字幕缺少有效时间戳，不能把无时间戳文本冒充同步字幕')
    last=-1
    for cue in cues:
        a,b=[int(cue[i])*3600+int(cue[i+1])*60+int(cue[i+2])+int(cue[i+3])/1000 for i in (0,4)]
        if a<last or b<=a or b>duration+2:raise ValueError('字幕时间轴无效或超出视频时长')
        last=a
    return text


class MediaWorkflow:
    def __init__(self,root=None):
        self.root=Path(root or ROOT/'runtime/video-workflows');self.root.mkdir(parents=True,exist_ok=True)
        self.thread=None;self.proc=None;self.cancel=threading.Event();self.state={};self.lock=threading.RLock()

    def _save(self,**values):
        with self.lock:
            self.state.update(values,updated=time.time())
            target=self.root/(self.state['id']+'.json')
            temp=target.with_suffix('.tmp');temp.write_text(json.dumps(self.state,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(target)

    def status(self,ident=None):
        with self.lock:
            if ident and ident!=self.state.get('id'):
                if not re.fullmatch('[a-f0-9]{32}',ident):raise ValueError('媒体任务编号无效')
                data=json.loads((self.root/(ident+'.json')).read_text(encoding='utf-8'))
                if data.get('state')=='running':data.update(state='interrupted',message='旧进程任务已中断，需明确重新开始')
                return data
            return dict(self.state)

    def stop(self):
        self.cancel.set()
        if self.proc and self.proc.poll() is None:
            try:self.proc.terminate()
            except OSError:pass
        return dict(requested=True)

    def execute(self,args,cancel=None):
        action=args.get('action')
        if action=='inspect':return dict(ok=True,**inspect_video(args['path']))
        if action=='status':return dict(ok=True,job=self.status(args.get('job_id')))
        if action=='stop':return dict(ok=True,**self.stop())
        if action=='wait':
            duration=max(1,min(20,int(args.get('seconds',15))))
            end=time.monotonic()+duration
            while self.thread and self.thread.is_alive() and time.monotonic()<end:
                if cancel and cancel.wait(.2):self.stop();break
                if not cancel:time.sleep(.2)
            data=self.status(args.get('job_id'))
            return dict(ok=data.get('state')!='failed',job=data)
        if action=='submission':
            data=self.status(args.get('job_id'))
            if data.get('state')!='ready' or data.get('action')!='render':raise ValueError('尚无已核对的成片')
            title=str(args.get('title','')).strip();description=str(args.get('description','')).strip()
            if not 1<=len(title)<=80:raise ValueError('请提供根据视频内容拟定的1—80字标题')
            manifest=dict(video=data['output'],subtitles=data.get('subtitles'),title=title,description=description,
                          state='prepared_not_uploaded',publish_requires_confirmation=True,
                          upload_url='https://member.bilibili.com/platform/upload/video/frame')
            target=Path(data['folder'])/'submission.json';target.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
            return dict(ok=True,manifest=str(target),**manifest)
        if action=='extract_frames':return self.frames(args['path'])
        if action not in ('render','transcribe'):raise ValueError('未知媒体工作流操作')
        if self.thread and self.thread.is_alive():raise ValueError('已有媒体任务在运行，请先等待或停止')
        source=inspect_video(args['path'])
        if not source['audio'] and action=='transcribe':raise ValueError('视频没有音轨，不能生成语音字幕')
        srt=None
        if action=='render':
            srt=Path(args.get('subtitles','')).resolve(strict=True)
            if srt.suffix.lower()!='.srt':raise ValueError('当前烧录需要UTF-8 SRT字幕')
            validate_srt(srt,source['seconds'])
        ident=uuid.uuid4().hex;folder=self.root/ident;folder.mkdir()
        self.cancel=threading.Event()
        self.state=dict(id=ident,state='running',action=action,source=source,folder=str(folder),progress=0,created=time.time())
        self._save(message='正在处理视频，原文件保持不变')
        self.thread=threading.Thread(target=self._work,args=(action,source,srt,folder,cancel),daemon=True);self.thread.start()
        return dict(ok=True,accepted=True,job_id=ident,job=self.status())

    def _run(self,command,folder,parent_cancel,duration=0):
        log=folder/'ffmpeg.log'
        with log.open('w',encoding='utf-8') as errors:
            self.proc=subprocess.Popen(command,cwd=str(folder),stdout=subprocess.PIPE,stderr=errors,
                                       encoding='utf-8',errors='replace',creationflags=subprocess.CREATE_NO_WINDOW)
            end=time.monotonic()+1800
            # Reader decouples a silent encoder from cancellation/deadline checks.
            import queue
            lines=queue.Queue()
            def reader():
                for line in self.proc.stdout:lines.put(line)
            thread=threading.Thread(target=reader,daemon=True);thread.start()
            while self.proc.poll() is None:
                if self.cancel.is_set() or parent_cancel and parent_cancel.is_set() or time.monotonic()>end:
                    self.proc.terminate();self.proc.wait(5);raise RuntimeError('媒体处理已停止或达到本阶段预算')
                try:line=lines.get(timeout=.2).strip()
                except queue.Empty:continue
                if duration and line.startswith('out_time_us='):
                    try:self._save(progress=min(99,round(float(line.split('=',1)[1])/1e6/duration*100)))
                    except ValueError:pass
            if self.proc.returncode!=0:raise RuntimeError('FFmpeg处理失败，详见 '+str(log))
            thread.join(1)
            self.proc.stdout.close()

    def _work(self,action,source,srt,folder,parent_cancel):
        try:
            if action=='render':
                (folder/'captions.srt').write_text(validate_srt(srt,source['seconds']),encoding='utf-8')
                output=folder/'ready-to-upload.mp4';duration=source['seconds']
                # geq gives the bottom bar its true time-dependent width; drawbox
                # width expressions are evaluated at initialization in FFmpeg.
                filters=(f"[0:v]subtitles=captions.srt:force_style='FontName=Microsoft YaHei,FontSize=22,Outline=2,MarginV=28',split=2[video][base];"
                         f"[base]crop=iw:8:0:ih-8,format=rgb24,geq=r='if(lte(X/W,T/{duration}),65,32)':g='if(lte(X/W,T/{duration}),160,38)':"
                         f"b='if(lte(X/W,T/{duration}),255,52)'[bar];"
                         "[video][bar]overlay=0:H-h:shortest=1[out]")
                self._run([ffmpeg_path(),'-hide_banner','-nostdin','-y','-i',source['path'],'-filter_complex_threads','2','-filter_complex',filters,
                           '-map','[out]','-map','0:a?','-c:v','libx264','-preset','veryfast','-crf','22','-threads','2',
                           '-c:a','aac','-b:a','160k','-movflags','+faststart','-progress','pipe:1',str(output)],folder,parent_cancel,duration)
                verified=inspect_video(output)
                if abs(verified['seconds']-duration)>1:raise RuntimeError('成片时长与原视频不一致，不能交付')
                self._save(state='ready',progress=100,output=str(output),subtitles=str(folder/'captions.srt'),verified=verified,
                           message='成片已生成并核对时长；字幕和进度条仍需预览验收')
            else:
                audio=folder/'audio.wav'
                self._run([ffmpeg_path(),'-v','error','-nostdin','-y','-i',source['path'],'-vn','-ac','1','-ar','16000',str(audio)],folder,parent_cancel)
                self._transcribe(audio,folder,parent_cancel)
        except Exception as exc:
            self._save(state='cancelled' if self.cancel.is_set() or parent_cancel and parent_cancel.is_set() else 'failed',error=str(exc))
        finally:
            if self.proc and self.proc.poll() is None:self.proc.terminate()
            self.proc=None

    def _transcribe(self,audio,folder,parent_cancel):
        import vosk,wave
        from core.asr import MODEL_DIR
        if not Path(MODEL_DIR).is_dir():raise ValueError('缺少本地识别模型，请导出会议SRT字幕；不会生成虚假字幕')
        vosk.SetLogLevel(-1);recognizer=vosk.KaldiRecognizer(vosk.Model(MODEL_DIR),16000);recognizer.SetWords(True)
        segments=[];end=time.monotonic()+600
        def collect(result):
            words=result.get('result',[])
            for start in range(0,len(words),10):
                chunk=words[start:start+10]
                if chunk:segments.append((chunk[0]['start'],chunk[-1]['end'],''.join(x['word'] for x in chunk)))
        with wave.open(str(audio),'rb') as stream:
            total=stream.getnframes();read=0
            while True:
                if self.cancel.is_set() or parent_cancel and parent_cancel.is_set() or time.monotonic()>end:
                    raise RuntimeError('字幕转写已停止或达到本阶段预算')
                data=stream.readframes(8000)
                if not data:break
                read+=len(data)//2
                if recognizer.AcceptWaveform(data):collect(json.loads(recognizer.Result()))
                if read%160000<8000:self._save(progress=min(99,round(read/total*100)))
            collect(json.loads(recognizer.FinalResult()))
        if not segments:raise ValueError('未识别到可靠语音，尚未生成字幕')
        target=folder/'auto-zh.srt'
        target.write_text('\n\n'.join(f'{i}\n{srt_timestamp(a)} --> {srt_timestamp(b)}\n{text}' for i,(a,b,text) in enumerate(segments,1)),encoding='utf-8')
        self._save(state='ready',progress=100,output=str(target),transcript_sample='\n'.join(x[2] for x in segments)[:6000],
                   message='本地自动字幕已生成，准确率未人工核对；优先采用会议导出字幕')

    def frames(self,path):
        source=inspect_video(path);folder=self.root/('frames-'+uuid.uuid4().hex);folder.mkdir()
        from PIL import Image
        canvas=Image.new('RGB',(640,360*3),'#eeeeee');paths=[]
        for i,fraction in enumerate((.05,.5,.9)):
            output=folder/(str(i)+'.png')
            subprocess.run([ffmpeg_path(),'-v','error','-ss',str(source['seconds']*fraction),'-i',source['path'],
                            '-frames:v','1','-vf','scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2',str(output)],
                           check=True,timeout=25,creationflags=subprocess.CREATE_NO_WINDOW)
            with Image.open(output) as im:canvas.paste(im,(0,i*360))
            paths.append(str(output))
        import io
        data=io.BytesIO();canvas.save(data,format='JPEG',quality=80)
        return dict(ok=True,frames=paths,seconds=source['seconds'],_image_url='data:image/jpeg;base64,'+base64.b64encode(data.getvalue()).decode())
