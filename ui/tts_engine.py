# -*- coding: utf-8 -*-
"""TTS 语音合成引擎 — 咕嘎企鹅版（edge-tts 软萌音优先 + 豆包兜底）

v2 修复（针对崩溃/磁盘泄漏）:
- 后台线程只合成 mp3 文件, 通过 Qt 信号把"已就绪"的文件路径传回主线程,
  QMediaPlayer 的 setMedia/play 只在主线程调用（QObject 必须主线程操作）。
- 临时 mp3 用完即删; 启动时清理本程序遗留的旧 mp3; _temp_files 限长防堆积。
- 合成加超时保护, 防止 edge-tts asyncio 在网络异常时永久挂起。
- 用 speak token 区分每次说话, stop()/新 speak 后旧合成完成不会误播。
"""
import os
import sys
import asyncio
import threading
import tempfile
import time
import uuid
import json
import base64
import re
import glob
from urllib.parse import urlsplit
from PyQt5.QtCore import QObject, pyqtSignal, QUrl
from PyQt5.QtMultimedia import QMediaPlayer, QMediaContent

# 朗读前去除标点（TTS 不读标点符号）
_PUNCT_RE = re.compile(r'[，。！？、；：“”‘’（）【】《》〈〉…—·｜|%％~\-_]+')

# 临时文件前缀（用于启动时清理残留）+ 同时限长
_TMP_PREFIX = 'guga_tts_'
_MAX_TEMP_FILES = 4
_SYNTH_TIMEOUT = 20  # 单次合成最长 20 秒


def configure_edge_transport():
    """Keep Edge's voice/protocol; use Microsoft's reachable read-aloud host.

    App-process only: do not edit site-packages or system proxy settings.
    Endpoint documented in upstream edge-tts issue #490. The installed SDK
    already supplies its v2 handshake; its legacy Bing host times out locally.
    """
    import importlib
    transport = importlib.import_module('edge_tts.communicate')
    if urlsplit(transport.WSS_URL).hostname == 'speech.platform.bing.com':
        from edge_tts.constants import TRUSTED_CLIENT_TOKEN
        transport.WSS_URL = ('wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1'
                             '?Ocp-Apim-Subscription-Key=' + TRUSTED_CLIENT_TOKEN)
    return urlsplit(transport.WSS_URL).hostname


def strip_punct(text: str) -> str:
    text = _PUNCT_RE.sub('', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


class TTSEngine(QObject):
    """语音合成引擎

    优先级: edge-tts（软萌少女音）> 豆包语音V3 > 静默
    - edge-tts: 免费，微软神经语音，企鹅用软萌音色 + 提高音调
    - 豆包语音V3: 兜底
    线程模型:
      speak() → 后台线程合成 mp3 → 发 ready 信号 → 主线程播放
      QMediaPlayer 永远只在主线程操作, 杜绝跨线程崩溃
    """

    speak_finished = pyqtSignal()
    speak_started = pyqtSignal(str)
    playback_started = pyqtSignal(str)
    speech_failed = pyqtSignal(str)

    EDGE_VOICE = "zh-CN-XiaoyiNeural"   # 软萌少女音
    EDGE_RATE = "+15%"
    EDGE_RATE_TUTOR = "+5%"             # 助教模式慢一点
    EDGE_PITCH = "+14Hz"                # 提高音调 → 更软萌
    _tutor_mode = False

    def set_tutor_mode(self, on: bool):
        self._tutor_mode = bool(on)

    def _current_rate(self):
        return self.EDGE_RATE_TUTOR if self._tutor_mode else self.EDGE_RATE

    DOUBAO_API_KEY = ""
    DOUBAO_VOICE = "zh_female_mizai_saturn_bigtts"
    DOUBAO_SPEED = -15
    DOUBAO_RESOURCE = "seed-tts-2.0"

    # 主线程接收: (token, path) — 合成完毕, 主线程负责播放
    _synth_ready = pyqtSignal(int, str)
    # 主线程接收: (token,) — 合成失败, 主线程负责收尾
    _synth_failed = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self._player = QMediaPlayer()
        self._player.setVolume(80)
        self._player.stateChanged.connect(self._on_state_changed)
        self._temp_files = []   # 主线程持有, 仅主线程修改
        self._is_speaking = False
        self._session = None
        self._speak_token = 0   # 每次 speak 自增, 用于失效旧请求
        self._current_token = 0  # 当前正在播放的 token
        self._display_text = ''
        self._playback_announced = False

        try:
            cfg_path = os.path.join(os.path.dirname(__file__), '..', 'ai_config.json')
            with open(cfg_path, encoding='utf-8-sig') as f:
                self.DOUBAO_API_KEY = json.load(f).get('doubao_tts_api_key', '')
        except Exception:
            self.DOUBAO_API_KEY = ''
        self._use_doubao = bool(self.DOUBAO_API_KEY)
        self._use_edge = self._check_edge_tts()
        self.reload_settings()

        if self._use_doubao:
            try:
                import requests
                self._session = requests.Session()
            except ImportError:
                self._use_doubao = False

        # 信号 → 主线程槽
        self._synth_ready.connect(self._on_synth_ready)
        self._synth_failed.connect(self._on_synth_failed)

        # 启动时清理上次崩溃残留的临时 mp3
        self._cleanup_stale_tmp()

        print(f"[TTS] edge-tts(软萌音): {'可用' if self._use_edge else '不可用'}")
        print(f"[TTS] 豆包语音V3: {'可用' if self._use_doubao else '未配置'}")
        print(f"[TTS] 千问语音({self.qwen.get('tts_model')}/{self.qwen.get('tts_voice')}): "
              f"{'可用' if self.qwen_ready else '未配置'}")

    # ---- 依赖检查 ----
    def reload_settings(self):
        from core.model_settings import read_raw
        from core import qwen_speech
        cfg=read_raw()
        self.provider=cfg.get('tts_provider') or 'edge'
        self.EDGE_VOICE=cfg.get('edge_voice') or type(self).EDGE_VOICE
        self.EDGE_RATE=cfg.get('edge_rate') or type(self).EDGE_RATE
        self.EDGE_PITCH=cfg.get('edge_pitch') or type(self).EDGE_PITCH
        self.qwen=qwen_speech.speech_settings(cfg)
        self.qwen_ready=qwen_speech.available(self.qwen)

    def _check_edge_tts(self):
        try:
            import edge_tts  # noqa
            return True
        except ImportError:
            return False

    # ---- 对外: 说话 ----
    def speak(self, text: str, voice_profile=None):
        if not text.strip():
            return
        text = strip_punct(text)
        if not text.strip():
            return
        # 中断当前播放并递增 token, 让进行中的旧合成即使完成也不会被播放
        self._stop_playback()
        self._speak_token += 1
        token = self._speak_token
        self._is_speaking = True
        self._display_text = text
        self._playback_announced = False
        self.speak_started.emit(text)
        threading.Thread(
            target=self._synth_worker,
            args=(token,text,dict(voice_profile)) if voice_profile else (token,text),
            daemon=True,
        ).start()

    def stop(self):
        """外部停止: 停播放 + 失效当前 token"""
        self._speak_token += 1  # 让尚未完成的合成无效
        self._stop_playback()

    # ---- 主线程: 停止播放 ----
    def _stop_playback(self):
        if self._is_speaking:
            try:
                self._player.stop()
            except Exception:
                pass
        self._is_speaking = False
        self._display_text = ''
        self._playback_announced = False
        self._cleanup_temp()

    # ---- 后台线程: 合成 ----
    def _synth_worker(self, token: int, text: str, voice_profile=None):
        """后台线程只负责生成 mp3 文件; 完成后发信号回主线程"""
        tmp_path = None
        try:
            tmp = tempfile.NamedTemporaryFile(
                prefix=_TMP_PREFIX, suffix='.mp3', delete=False,
                dir=tempfile.gettempdir())
            tmp_path = tmp.name
            tmp.close()

            success = False
            provider = getattr(self, 'provider', 'edge')
            if provider == 'qianwen' and not voice_profile:
                # 千问 qwen3-tts-flash：返回真实后缀的音频文件，替换掉预建的空 mp3
                qwen_path = self._synth_qwen(text, os.path.dirname(tmp_path))
                if qwen_path:
                    self._safe_remove(tmp_path)
                    tmp_path = qwen_path
                    success = True
                elif self._use_edge:
                    # 在线合成失败不要变哑巴，退回 Edge 音色继续说话
                    success = self._synth_edge(text, tmp_path)
            elif self._use_edge and (provider == 'edge' or voice_profile):
                success = self._synth_edge(text,tmp_path,voice_profile) if voice_profile else self._synth_edge(text,tmp_path)
            # A failed companion voice must not silently become Guga's fallback.
            if not success and self._use_doubao and not voice_profile and provider=='doubao':
                success = self._synth_doubao(text, tmp_path)

            if not success:
                # 失败: 清掉空文件, 通知主线程收尾
                self._safe_remove(tmp_path)
                self._synth_failed.emit(token)
                return

            # 校验文件有效
            if not (os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 100):
                self._safe_remove(tmp_path)
                self._synth_failed.emit(token)
                return

            # 成功: 通知主线程播放
            self._synth_ready.emit(token, tmp_path)
        except Exception as e:
            print(f"[TTS] synth_worker 错误: {e}", file=sys.stderr)
            self._safe_remove(tmp_path)
            try:
                self._synth_failed.emit(token)
            except Exception:
                pass

    def _synth_qwen(self, text: str, out_dir: str):
        """千问语音合成（qwen3-tts-flash）→ 本地音频路径；失败返回 None"""
        try:
            from core import qwen_speech
            return qwen_speech.synthesize(text, out_dir=out_dir, settings=self.qwen)
        except Exception as e:
            print(f"[TTS] 千问语音错误: {e}", file=sys.stderr)
            return None

    def _synth_edge(self, text: str, out_path: str, voice_profile=None) -> bool:
        """edge-tts 合成, 加超时防止网络卡死"""
        try:
            import edge_tts
            configure_edge_transport()

            async def _synth():
                communicate = edge_tts.Communicate(
                    text, (voice_profile or {}).get('edge_voice',self.EDGE_VOICE),
                    rate=(voice_profile or {}).get('edge_rate',self._current_rate()),
                    pitch=(voice_profile or {}).get('edge_pitch',self.EDGE_PITCH),
                    volume="+0%", connect_timeout=5, receive_timeout=10
                )
                await communicate.save(out_path)

            try:
                # asyncio 超时: 防止 edge-tts 网络异常时永久挂起
                asyncio.run(asyncio.wait_for(_synth(), timeout=_SYNTH_TIMEOUT))
            except asyncio.TimeoutError:
                print("[TTS] edge-tts 合成超时", file=sys.stderr)
                self._safe_remove(out_path)
                return False
            return os.path.exists(out_path) and os.path.getsize(out_path) > 100

        except Exception as e:
            print(f"[TTS] edge-tts 错误: {e}", file=sys.stderr)
            self._safe_remove(out_path)
            return False

    def _synth_doubao(self, text: str, out_path: str) -> bool:
        try:
            import requests
            sess = self._session or requests
            url = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
            headers = {
                "Content-Type": "application/json",
                "X-Api-Key": self.DOUBAO_API_KEY,
                "X-Api-Resource-Id": self.DOUBAO_RESOURCE,
                "X-Api-Request-Id": str(uuid.uuid4()),
            }
            body = {
                "user": {"uid": "black_cat_pet"},
                "req_params": {
                    "text": text,
                    "speaker": self.DOUBAO_VOICE,
                    "audio_params": {
                        "format": "mp3",
                        "sample_rate": 24000,
                        "speech_rate": self.DOUBAO_SPEED,
                    }
                }
            }

            resp = sess.post(url, headers=headers, json=body, timeout=30, stream=True)
            audio_parts = []
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    j = json.loads(line.decode('utf-8'))
                    code = j.get("code", -1)
                    if code == 0:
                        d = j.get("data")
                        if d:
                            audio_parts.append(base64.b64decode(d))
                    elif code == 20000000:
                        break
                    elif code not in (0, 20000000):
                        msg = j.get("message", "")
                        print(f"[TTS] 豆包语音响应错误 code={code}: {msg[:100]}",
                              file=sys.stderr)
                except Exception:
                    continue

            if audio_parts:
                audio_data = b"".join(audio_parts)
                with open(out_path, 'wb') as f:
                    f.write(audio_data)
                return True
            return False

        except Exception as e:
            print(f"[TTS] 豆包语音错误: {e}", file=sys.stderr)
            self._safe_remove(out_path)
            return False

    # ---- 主线程槽: 合成完成 ----
    def _on_synth_ready(self, token: int, path: str):
        # 若已经被新的 speak / stop 失效, 直接清理不播放
        if token != self._speak_token:
            self._safe_remove(path)
            return
        # 若 _is_speaking 已被 stop 清掉, 也不要播
        if not self._is_speaking:
            self._safe_remove(path)
            return
        try:
            # 临时文件列表限长: 超出 _MAX_TEMP_FILES 删最旧
            self._temp_files.append(path)
            while len(self._temp_files) > _MAX_TEMP_FILES:
                old = self._temp_files.pop(0)
                self._safe_remove(old)

            url = QUrl.fromLocalFile(path)
            content = QMediaContent(url)
            self._current_token = token
            self._player.setMedia(content)
            self._player.play()
        except Exception as e:
            print(f"[TTS] 播放错误: {e}", file=sys.stderr)
            self._safe_remove(path)
            self._is_speaking = False
            self.speech_failed.emit('本机语音播放失败')
            try:
                self.speak_finished.emit()
            except Exception:
                pass

    def _on_synth_failed(self, token: int):
        if token != self._speak_token:
            return
        self._is_speaking = False
        self.speech_failed.emit('本机语音合成失败')
        try:
            self.speak_finished.emit()
        except Exception:
            pass

    def _on_state_changed(self, state):
        if state == QMediaPlayer.PlayingState and self._is_speaking and not self._playback_announced:
            self._playback_announced = True
            try:self.playback_started.emit(self._display_text)
            except Exception:pass
        if state == QMediaPlayer.StoppedState and self._is_speaking:
            self._is_speaking = False
            self._display_text = ''
            self._playback_announced = False
            self._cleanup_temp()
            try:
                self.speak_finished.emit()
            except Exception:
                pass

    # ---- 临时文件清理 ----
    def _cleanup_temp(self):
        """主线程调用: 清掉所有已记录的临时 mp3"""
        for f in self._temp_files[:]:
            self._safe_remove(f)
        self._temp_files.clear()

    @staticmethod
    def _safe_remove(path):
        if not path:
            return
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    def _cleanup_stale_tmp(self):
        """启动时清理上次崩溃/强杀残留的临时 mp3 (只清前缀 _TMP_PREFIX)"""
        try:
            tmp_dir = tempfile.gettempdir()
            now = time.time()
            for f in glob.glob(os.path.join(tmp_dir, _TMP_PREFIX + '*')):
                try:
                    # 只清 1 天以内的(避免误删他人文件), 老文件系统自己会清
                    if now - os.path.getmtime(f) < 86400:
                        os.remove(f)
                except Exception:
                    continue
        except Exception:
            pass

    @property
    def is_speaking(self):
        return self._is_speaking
