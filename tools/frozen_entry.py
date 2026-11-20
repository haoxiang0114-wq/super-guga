"""Standalone application entry point; also supports a bounded binary smoke test."""
import os,sys
from pathlib import Path
ROOT=Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[1]
os.chdir(ROOT)

def smoke(report):
    import json,wave
    from launcher import _prepare_qt
    _prepare_qt()
    from PyQt5.QtWidgets import QApplication
    from PyQt5.QtCore import QTimer
    from core.music_performance import MusicPerformance,song_entry,MUSIC
    from core.realtime_voice import load_settings,connection_url
    from core.ai_chat import AIChatEngine
    from core.realtime_agent import AgentRealtimeVoice
    from ui.sprite_animator import SpriteAnimator
    import sounddevice
    app=QApplication([]);cfg=AIChatEngine._load_config();voice=load_settings()
    record=dict(frozen=bool(getattr(sys,'frozen',False)),chat_configured=bool(cfg.get('api_key')),model=cfg.get('model'),voice_configured=bool(voice.get('api_key')),voice_model=voice.get('model'),voice_endpoint_valid=bool(connection_url(voice)),edge_tts_imported=True,audio_devices=len(sounddevice.query_devices()))
    sprite=SpriteAnimator(str(ROOT/'resources'),250);record['sprite_loaded']=sprite.width>0
    engine=AgentRealtimeVoice();engine._cfg=voice;commands=[];engine.fast_command.connect(commands.append)
    engine._accept_call(dict(call_id='packaged-music',name='perform_music',arguments='{"action":"sing"}'))
    record['voice_sing_routes_to_qingtian']=bool(commands and commands[0]['target']=='晴天')
    music=MusicPerformance();events=[];music.performance_event.connect(events.append)
    entry=song_entry('晴天');path=Path(entry['path']);path=path if path.is_absolute() else MUSIC/path
    with wave.open(str(path),'rb') as w:record['song_seconds']=round(w.getnframes()/w.getframerate(),2)
    record['sing_receipt']=music.sing('晴天').get('ok')
    def finish():
        record['actual_playback_started']=any(e.get('type')=='playing' for e in events)
        record['playback_position_ms']=music.player.position();music.stop()
        record['ok']=all(record.get(k) for k in ('chat_configured','voice_configured','voice_endpoint_valid','sprite_loaded','voice_sing_routes_to_qingtian','sing_receipt','actual_playback_started'))
        Path(report).write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8');app.quit()
    QTimer.singleShot(3500,finish);app.exec_()

if __name__=='__main__':
    if '--guga-mcp' in sys.argv:
        from tools.guga_mcp_server import serve
        sys.stdin.reconfigure(encoding='utf-8');sys.stdout.reconfigure(encoding='utf-8')
        serve()
    elif '--self-test' in sys.argv:
        report=Path(sys.argv[sys.argv.index('--self-test')+1]).resolve()
        try:smoke(report)
        except Exception as exc:
            import json,traceback
            report.write_text(json.dumps(dict(ok=False,error=type(exc).__name__,trace=traceback.format_exc()),ensure_ascii=False,indent=2),encoding='utf-8')
            sys.exit(1)
    else:
        from launcher import main
        main()
