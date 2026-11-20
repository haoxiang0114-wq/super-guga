"""Observe the exact DSH session Guga dispatched; new text alone is not completion."""
import threading
import time
import uuid
from PyQt5.QtCore import QObject, QTimer, pyqtSignal


def turn_facts(view, rows, baseline):
    replies = [row for row in rows if row.get('role') == 'assistant' and int(row.get('seq') or 0) > baseline]
    running = bool(view.get('running'))
    return dict(state='running' if running else 'completed' if replies else 'queued',
                running=running, summary=str(replies[-1].get('text') or '') if replies else '',
                seq=int(replies[-1].get('seq') or 0) if replies else baseline)


class BoundDshMonitor(QObject):
    update = pyqtSignal(dict)
    notice = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = {}
        self._busy = False
        self._closed = False
        self._generation = ''
        self.reporter = None
        self.timer = QTimer(self)
        self.timer.setInterval(3000)
        self.timer.timeout.connect(self.poll)
        self.update.connect(self._receive)

    def start(self, conversation_id, session_id, baseline, reporting=None,goal=''):
        from core.dsh_progress import MinuteReporter
        self._generation = uuid.uuid4().hex
        self.reporter=MinuteReporter(reporting)
        self._state = dict(conversation_id=conversation_id, session_id=session_id,
                           baseline=baseline, state='queued', failures=0,started_at=time.time(),goal=str(goal))
        self.timer.start()
        self.poll()

    def status(self):
        return dict(self._state,report_interval_seconds=(self.reporter.config.get('interval_seconds',60) if self.reporter else 60))

    def poll(self):
        if self._closed or self._busy or not self._state:
            return
        self._busy = True
        current, generation = dict(self._state), self._generation
        def work():
            try:
                from core.super_agent.dsh_gateway import DshGateway
                from core.super_agent.dsh_transcript import read_transcript
                view = DshGateway(timeout=5).session_view(current['session_id'])
                rows = read_transcript(current['session_id'], limit=200)
                state = dict(current, **turn_facts(view, rows, current['baseline']), failures=0)
            except Exception as exc:
                failures = current.get('failures', 0) + 1
                state = dict(current, failures=failures, error=str(exc)[:300])
                if failures >= 3:
                    state['state'] = 'disconnected'
            if not self._closed:
                self.update.emit(dict(state, generation=generation))
            self._busy = False
        threading.Thread(target=work, name='guga-dsh-bound-monitor', daemon=True).start()

    def _receive(self, state):
        if state.get('generation') != self._generation:
            return
        self._state = state
        if self.reporter:
            notice=self.reporter.take(state)
            if notice:
                self._state.update(last_report_at=notice['at'],last_report_text=notice['text'],report_count=notice['number'])
                self.notice.emit(notice)
        if state.get('state') in ('completed', 'disconnected'):
            self.timer.stop()

    def close(self):
        self._closed = True
        self.timer.stop()
