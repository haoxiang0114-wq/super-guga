"""Singing playback is separate from conversational TTS; completion needs a real audio file."""
import json
import subprocess,sys,threading
from pathlib import Path
from PyQt5.QtCore import QObject,pyqtSignal,QUrl,QTimer
from PyQt5.QtMultimedia import QMediaPlayer,QMediaContent,QMediaPlaylist
ROOT=Path(__file__).resolve().parents[1]
MUSIC=ROOT/'runtime/music'

def song_entry(title):
    path=MUSIC/'library.json'
    library=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    return library.get(str(title).strip().strip('《》'))

class MusicPerformance(QObject):
    performance_event=pyqtSignal(object)
    prepared=pyqtSignal(object)
    def __init__(self,parent=None):
        super().__init__(parent)
        self.player=QMediaPlayer(self);self.player.setVolume(70)
        self.playlist=QMediaPlaylist(self);self.player.setPlaylist(self.playlist);self.repeat=False
        self.player.stateChanged.connect(self._state)
        self.player.error.connect(self._error)
        self.title='';self.playing=False;self.requested=False;self.preparing=False;self.proc=None;self.generation=0
        self.prepared.connect(self._prepared)
        self.interaction=QTimer(self);self.interaction.setSingleShot(True);self.interaction.timeout.connect(self.pause)
        self._last_stage=(False,'')
        self.stage_sync=QTimer(self);self.stage_sync.setInterval(250);self.stage_sync.timeout.connect(self._sync_stage)
    def status(self):
        return dict(playing=self.playing,paused=self.player.state()==QMediaPlayer.PausedState,preparing=self.preparing,title=self.title,repeat=self.repeat,position_ms=self.player.position(),duration_ms=self.player.duration())
    def sing(self,title):
        title=title or '晴天'
        entry=song_entry(title)
        if not entry or not entry.get('verified_full_length') or entry.get('pipeline_version',0)<2:
            if (MUSIC/'source.json').exists() and (MUSIC/'guga-reference.wav').exists():
                return self.prepare(title)
            return dict(ok=False,error='这首歌尚未完成咕嘎音色翻唱制作；不会用普通语音念歌词冒充唱歌。',title=title)
        path=Path(entry.get('path',''))
        if not path.is_absolute():path=MUSIC/path
        if not path.is_file() or path.stat().st_size<1024:
            return dict(ok=False,error='翻唱音频文件不存在或不完整')
        self.stop();self.title=title;self.requested=True
        self.repeat=title=='晴天';self.playlist.clear()
        self.playlist.addMedia(QMediaContent(QUrl.fromLocalFile(str(path.resolve()))));self.playlist.setCurrentIndex(0)
        self.playlist.setPlaybackMode(QMediaPlaylist.CurrentItemInLoop if self.repeat else QMediaPlaylist.CurrentItemOnce)
        self.player.play()
        self.stage_sync.start()
        return dict(ok=True,accepted=True,playing=False,title=title,repeat=self.repeat,message='已请求循环演唱，直到停止唱歌' if self.repeat else '已请求播放咕嘎翻唱；实际播放以音频播放器事件为准')
    def stop(self):
        self.interaction.stop();self.stage_sync.stop()
        self.requested=False;self.repeat=False;self.generation+=1;self.player.stop();self.playlist.setPlaybackMode(QMediaPlaylist.CurrentItemOnce)
        self.preparing=False
        proc=self.proc
        if proc and proc.poll() is None:
            try:subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],capture_output=True,timeout=5,creationflags=0x08000000)
            except Exception:proc.kill()
        return dict(ok=True,message='已停止唱歌')
    def pause(self):
        if not self.playing:return dict(ok=False,error='当前没有正在播放的歌曲')
        self.interaction.stop();self.player.pause()
        self.performance_event.emit(dict(type='interaction',title=self.title))
        return dict(ok=True,message='已暂停歌曲，可以聊天；说继续唱歌从原处继续')
    def resume(self):
        if self.player.state()!=QMediaPlayer.PausedState:return dict(ok=False,error='当前没有可继续的暂停歌曲')
        self.player.play();return dict(ok=True,accepted=True,message='已请求从暂停位置继续')
    def prepare(self,title):
        if self.preparing:return dict(ok=False,error='已有歌曲正在制作，请稍后或先停止唱歌')
        info=json.loads((MUSIC/'source.json').read_text(encoding='utf-8'))
        info=(info.get('songs',{}).get(title) or info)
        source=Path(info.get('path',''))
        if info.get('title')!=title or not source.is_file():return dict(ok=False,error='缺少这首歌的完整音频源；未启动制作')
        self.preparing=True;self.title=title;self.generation+=1;generation=self.generation
        self.performance_event.emit(dict(type='preparing',title=title))
        def work():
            result=dict(ok=False,error='歌曲转换没有返回结果')
            try:
                python=str(Path(sys.executable).with_name('python.exe'))
                proc=subprocess.Popen([python,str(ROOT/'tools/convert_song.py'),str(source),str(MUSIC/'guga-reference.wav'),str(info['expected_seconds']),title],cwd=str(ROOT),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',errors='replace',creationflags=0x08000000)
                self.proc=proc
                if generation!=self.generation:proc.kill()
                out,err=proc.communicate(timeout=480)
                for line in reversed(out.splitlines()):
                    try:result=json.loads(line);break
                    except ValueError:continue
                if proc.returncode:result=dict(ok=False,error='免费歌声服务未完成转换：'+str(result.get('error','服务不可用')))
            except subprocess.TimeoutExpired:
                proc.kill();proc.communicate(timeout=5);result=dict(ok=False,error='歌声转换达到480秒预算，已停止')
            except Exception as exc:result=dict(ok=False,error='歌声转换失败：'+type(exc).__name__)
            self.prepared.emit(dict(result,generation=generation,title=title))
        threading.Thread(target=work,daemon=True).start()
        return dict(ok=True,accepted=True,execution_pending=True,message='正在制作完整翻唱，尚未生成或播放')
    def _prepared(self,result):
        if result.get('generation')!=self.generation:return
        self.preparing=False;self.proc=None
        if result.get('ok'):self.sing(result['title'])
        else:self.performance_event.emit(dict(type='error',title=result.get('title'),error=result.get('error','转换失败')))
    def _state(self,state):
        active=state==QMediaPlayer.PlayingState
        changed=active!=self.playing;self.playing=active
        if changed:
            self._last_stage=(active,self.title)
            self.performance_event.emit(dict(type='playing' if active else 'stopped',title=self.title))
        if active:self.stage_sync.start()
        else:self.stage_sync.stop()
        if active and not self.repeat:self.interaction.start(75000)
        else:self.interaction.stop()
    def _sync_stage(self):
        # UI-chat events can be missed around a window reload. Reassert only on
        # actual media-state changes; this does not poll audio or call a model.
        current=(self.playing,self.title)
        if current==self._last_stage:return
        self._last_stage=current
        self.performance_event.emit(dict(type='playing' if self.playing else 'stopped',title=self.title))
    def _error(self,*args):
        self.requested=False
        self.performance_event.emit(dict(type='error',title=self.title,error=self.player.errorString()))
