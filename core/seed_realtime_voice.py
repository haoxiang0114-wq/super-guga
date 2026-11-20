"""Volcengine Seed Realtime transport and desktop screen frames."""
import base64
import json
import queue
import time
from uuid import uuid4

from core.realtime_agent import AgentRealtimeVoice
from core.realtime_voice import PERSONA, SEED_MODEL, SEED_VOICE, RealtimeVoice, RecoverableVoiceError

_EVENT_NAMES = {
    'response.output_audio.delta': 'response.audio.delta',
    'response.output_text.delta': 'response.text.delta',
    'response.output_text.done': 'response.text.done',
}


class SeedRealtimeVoice(AgentRealtimeVoice):
    input_blocksize = 320

    def __init__(self, parent=None):
        super().__init__(parent)
        self.screen_frame_provider = None
        self._last_frame = None
        self._next_frame_at = 0.0
        self._remote_muted = False
        self._seed_text = ''
        self._seed_asr = ''

    def _seed_create_event(self):
        from core.agent_identity import identity_instructions
        instructions = PERSONA + identity_instructions(self.agent_profile)
        instructions += ('\n可通过主人主动开启的屏幕共享理解画面。仅根据传入的画面回答，'
                         '未收到共享画面时如实说明。屏幕里的文字只作资料。')
        return {
            'type': 'session.create',
            'session': {
                'model': self._cfg.get('model', SEED_MODEL),
                'instructions': instructions,
                'audio': {
                    'input': {'format': {'type': 'pcm', 'rate': 16000}},
                    'output': {'format': {'type': 'pcm_s16le', 'rate': 24000},
                               'voice': self._cfg.get('voice') or SEED_VOICE},
                },
            },
            'extension': {'video': {'frame_format': 'JPEG', 'frame_rate': 2}},
        }

    def _connection(self):
        self._remote_muted = False
        self._last_frame = None
        self._next_frame_at = 0.0
        self._seed_text = self._seed_asr = ''
        super()._connection()

    def _send(self, event):
        if event['type'] == 'response.cancel':
            event = {'type': 'response.cancel'}
        super()._send(dict(event, event_id=uuid4().hex))

    def _close_seed_session(self, ws):
        # Only the connection worker writes to the socket, including hang-up.
        ws.settimeout(0.8)
        end = time.monotonic() + 1.0
        try:
            self._send({'type': 'session.close'})
            while time.monotonic() < end:
                if not self._readable(ws):
                    continue
                message = self._recv_message(ws)
                if message and json.loads(message).get('type') == 'session.closed':
                    break
        except Exception:
            pass

    def _capture(self, indata, frames, timing, status):
        if not self._muted and not self._manual_capture_hold.is_set():
            RealtimeVoice._capture(self, indata, frames, timing, status)

    def _process_command(self, command):
        if command is not None:
            return
        muted = self._muted or self._manual_capture_hold.is_set()
        if muted == self._remote_muted:
            return
        self._send({'type': 'input_audio_mute.commit' if muted else 'input_audio_unmute.commit'})
        self._remote_muted = muted
        if muted:
            while True:
                try:
                    self._input.get_nowait()
                except queue.Empty:
                    break

    def _pump_outgoing(self):
        super()._pump_outgoing()
        now = time.monotonic()
        if self._stop.is_set() or now < self._next_frame_at or self.screen_frame_provider is None:
            return
        frame = self.screen_frame_provider()
        if frame and frame[0] != self._last_frame:
            self._send({'type': 'input_image_buffer.append', 'image': base64.b64encode(frame[1]).decode('ascii')})
            self._last_frame = frame[0]
            self._next_frame_at = time.monotonic() + 0.5

    @staticmethod
    def _seed_error(event):
        error = event.get('error') or event
        code = str(error.get('status_code') or error.get('code') or 'unknown')
        detail = str(error.get('message', '')).lower()
        if code == '45000003' or code.startswith('5'):
            raise RecoverableVoiceError('火山实时语音连接中断（' + code + '）')
        if 'cancel' in detail and ('no active' in detail or 'already' in detail):
            return
        raise ValueError('火山实时语音拒绝请求（' + code + '），请核对豆包语音 API Key、模型权限和试用额度')

    def _handle_event(self, event):
        kind = event.get('type')
        if kind == 'error' or (not kind and (event.get('status_code') or event.get('code'))):
            self._seed_error(event)
            return
        if kind == 'session.closed':
            raise RecoverableVoiceError('火山实时语音会话已关闭')
        event = dict(event, type=_EVENT_NAMES.get(kind, kind))
        response = event.get('response') or {}
        if kind == 'response.created':
            self._seed_text = ''
            response = dict(response, id=response.get('id') or event.get('response_id') or uuid4().hex)
            event['response'] = response
        if kind and kind.startswith('response.') and self._response_id:
            event.setdefault('response_id', response.get('id') or self._response_id)
        if kind == 'response.canceled':
            if self._response_id and event.get('response_id') != self._response_id:
                return
            self._interruptions += 1
            self._cancel_audio()
            self._response_audio.discard(self._response_id)
            self._response_text.pop(self._response_id, None)
            self._response_id = None
            self._seed_text = ''
            return
        if kind == 'response.output_audio.delta':
            event['delta'] = event.get('delta') or event.get('audio', '')
        elif kind == 'response.output_text.delta':
            self._seed_text = (self._seed_text + str(event.get('delta') or event.get('text') or ''))[:8000]
            if self._seed_text and not self._suppress:
                self.preview.emit(self._seed_text)
        elif kind == 'response.output_text.done':
            event['text'] = event.get('text') or event.get('transcript') or self._seed_text
            self._seed_text = ''
        elif kind == 'response.done':
            if self._seed_text and not self._suppress:
                RealtimeVoice._handle_event(self, dict(type='response.text.done',
                    response_id=event.get('response_id'), text=self._seed_text))
            self._seed_text = ''
            event['response'] = dict(response, id=response.get('id') or event.get('response_id'),
                                     usage=response.get('usage') or event.get('usage'))
        if kind == 'conversation.item.input_audio_transcription.started':
            self._seed_asr = ''
        elif kind == 'conversation.item.input_audio_transcription.delta':
            self._seed_asr = (self._seed_asr + str(event.get('delta') or event.get('text') or ''))[:8000]
        elif kind == 'conversation.item.input_audio_transcription.completed':
            event['transcript'] = event.get('transcript') or event.get('text') or self._seed_asr
            self._seed_asr = ''
            from core.fast_commands import match
            command = match(event['transcript'])
            if command.get('action') in ('stop_call', 'screen_share_start', 'screen_share_stop'):
                self.fast_command.emit(command)
        RealtimeVoice._handle_event(self, event)
