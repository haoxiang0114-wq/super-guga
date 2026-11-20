"""One bounded call to the upstream free Seed-VC demo; no paid fallback."""
import json,sys,wave,subprocess,hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def convert(source,reference,seconds,title='跳楼机'):
    from gradio_client import Client,handle_file
    source=Path(source).resolve();reference=Path(reference).resolve()
    if not source.is_file() or not reference.is_file():raise ValueError('缺少原曲或咕嘎音色参考文件')
    folder=ROOT/'runtime/music';folder.mkdir(exist_ok=True,parents=True)
    separate_python=folder/'.venv/Scripts/python.exe'
    if not separate_python.is_file():raise RuntimeError('缺少隔离的人声分离环境，拒绝将带伴奏混音直接送入音色转换')
    separation=subprocess.run([str(separate_python),str(ROOT/'tools/separate_music.py'),str(source)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=180,creationflags=0x08000000)
    stems=None
    for line in reversed(separation.stdout.splitlines()):
        try:stems=json.loads(line);break
        except ValueError:pass
    if not stems or not stems.get('ok'):raise RuntimeError('人声与伴奏分离未完成；尚未制作翻唱')
    print(json.dumps(dict(phase='connecting_free_singing_service')),flush=True)
    client=Client('Plachta/Seed-VC',verbose=False,httpx_kwargs={'timeout':20})
    print(json.dumps(dict(phase='submitting_full_song')),flush=True)
    job=client.submit(handle_file(stems['vocals']),handle_file(str(reference)),30,1.0,0.7,True,False,0,api_name='/predict_1')
    try:result=job.result(timeout=240)
    except BaseException:
        job.cancel();raise
    candidates=[]
    def collect(value):
        if isinstance(value,str) and Path(value).is_file():candidates.append(Path(value))
        elif isinstance(value,dict):
            for k in ('path','name','value'):collect(value.get(k))
        elif isinstance(value,(list,tuple)):
            for v in value:collect(v)
    collect(result)
    if not candidates:raise RuntimeError('歌声模型没有返回可用音频文件')
    valid=[]
    for path in candidates:
        try:
            audio=wave.open(str(path),'rb');duration=audio.getnframes()/audio.getframerate();audio.close()
            if .97*seconds<=duration<=1.04*seconds:valid.append((path,duration))
        except Exception:continue
    if not valid:raise RuntimeError('模型返回的不是完整歌曲，拒绝登记为完整翻唱')
    path,duration=valid[-1];target=folder/('guga-cover-'+hashlib.sha256(title.encode()).hexdigest()[:12]+'.wav')
    subprocess.run([str(folder/'bin/ffmpeg.exe'),'-v','error','-y','-i',str(path),'-i',stems['instrumental'],'-filter_complex','[0:a]highpass=f=75,lowpass=f=14000,volume=0.95[v];[1:a]volume=0.8[b];[v][b]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95:level=false[out]','-map','[out]','-ar','44100','-c:a','pcm_s16le',str(target)],check=True,timeout=40,creationflags=0x08000000)
    manifest_path=folder/'library.json';manifest=json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {}
    manifest[title]=dict(path=str(target),seconds=duration,verified_full_length=True,provider='Plachta/Seed-VC',voice='guga-current-realtime',quality_reviewed=False,pipeline_version=2,separated_vocals=True)
    manifest_path.write_text(json.dumps(manifest,ensure_ascii=True,indent=2),encoding='utf-8')
    return dict(ok=True,title=title,seconds=duration,path=str(target),quality_reviewed=False,pipeline_version=2,separated_vocals=True)

if __name__=='__main__':
    try:result=convert(sys.argv[1],sys.argv[2],float(sys.argv[3]),sys.argv[4] if len(sys.argv)>4 else '跳楼机')
    except Exception as exc:result=dict(ok=False,error=type(exc).__name__,message=str(exc)[:600])
    print(json.dumps(result,ensure_ascii=True))
    sys.exit(0 if result['ok'] else 1)
