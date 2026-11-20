"""User-only local named pipe. MCP stdio proxy forwards into Qt's main-thread runtime."""
import json
import threading
import time
from PyQt5.QtCore import QObject, QTimer, pyqtSignal
from PyQt5.QtNetwork import QLocalServer
from core.mcp_contract import pipe_name, self_tools


class SelfMCPBridge(QObject):
    worker_result=pyqtSignal(object,object)
    def __init__(self, pet):
        super().__init__(pet)
        self.pet = pet
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.UserAccessOption)
        self.server.newConnection.connect(self._connected)
        self.available = self.server.listen(pipe_name())
        self.clients = set()
        self.worker_result.connect(self._respond)

    def _connected(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            self.clients.add(socket)
            socket.readyRead.connect(lambda s=socket: self._read(s))
            socket.disconnected.connect(lambda s=socket: self._release(s))
            QTimer.singleShot(145000, lambda s=socket: s.abort() if s in self.clients else None)

    def _release(self, socket):
        self.clients.discard(socket)
        socket.deleteLater()

    def _read(self, socket):
        if socket.bytesAvailable() > 100000:
            socket.abort()
            return
        if not socket.canReadLine():
            return
        try:
            request = json.loads(bytes(socket.readLine()).decode('utf-8'))
            name, args = request['name'], request.get('arguments', {})
            if name=='pet_control':
                # 桌面快捷方式开关用：走管道退出，让 _quit() 把位置/状态存盘，
                # 而不是直接杀进程（强杀会丢位置和数值）。
                if str((args or {}).get('action') or '')!='quit':
                    raise ValueError('pet_control 只支持 quit')
                self._respond(socket,dict(ok=True,result=dict(action='quit')))
                QTimer.singleShot(200,self.pet._quit)
                return
            if name=='dsh_host_event':
                if not request.get('plugin_event') or not isinstance(args,dict):raise ValueError('需要咕嘎原生 DSH 插件回执通道')
                result=self.pet._ensure_agent_panel().runtime.dsh_link.receive(args)
                self._respond(socket,dict(ok=True,result=result));return
            if request.get('dsh_job'):
                from core.mcp_contract import dsh_tools
                if name not in {t['name'] for t in dsh_tools()} or not isinstance(args,dict):
                    raise ValueError('未知DSH工具或参数无效')
                panel=self.pet._ensure_agent_panel()
                def work():
                    response=panel.runtime.dsh.execute_tool(request['dsh_job'],name,args)
                    self.worker_result.emit(socket,response)
                threading.Thread(target=work,daemon=True).start()
                return
            if name not in {t['name'] for t in self_tools()} or not isinstance(args, dict):
                raise ValueError('未知工具或参数类型无效')
            panel = self.pet._ensure_agent_panel()
            if name in ('computer','wechat','browser') or (name=='applications' and args.get('action') in ('open','list')) or (name=='desktop_command' and args.get('action') in ('open','focus','list')):
                def external_work():
                    try:
                        value=panel.runtime.execute(name,args)
                        response=dict(ok=value.get('ok') is not False,result=value)
                    except Exception as exc:response=dict(ok=False,error=str(exc))
                    self.worker_result.emit(socket,response)
                threading.Thread(target=external_work,name='guga-external-tool',daemon=True).start()
                return
            result = panel.runtime.execute(name, args)
            if name=='applications' and args.get('action')=='command' and result.get('execution_pending'):
                receipt=result.get('receipt_id');deadline=time.monotonic()+8
                timer=QTimer(socket);timer.setInterval(80)
                def check_receipt():
                    if socket not in self.clients:timer.stop();return
                    current=panel._last_fast_result or {}
                    if current.get('receipt_id')==receipt and not current.get('execution_pending'):
                        timer.stop();self._respond(socket,dict(ok=current.get('ok') is True,result=current));return
                    if time.monotonic()>=deadline:
                        timer.stop();self._respond(socket,dict(ok=False,error='8秒内未收到本次命令的实际回执；没有自动重试',receipt_id=receipt))
                timer.timeout.connect(check_receipt);timer.start()
                return
            if name not in ('app_state','pet_say') and not (name=='chat_control' and args.get('action')=='state'):
                panel.store.append(panel.active_id, 'tool', 'MCP 外部调用：' + name + '\n' + json.dumps(args, ensure_ascii=False))
                panel.render()
            response = dict(ok=result.get('ok') is not False, result=result)
        except Exception as exc:
            response = dict(ok=False, error=str(exc))
        self._respond(socket,response)

    def _respond(self,socket,response):
        if socket not in self.clients:return
        socket.write((json.dumps(response, ensure_ascii=False) + '\n').encode('utf-8'))
        socket.flush()
        socket.disconnectFromServer()

    def close(self):
        for socket in list(self.clients):
            socket.abort()
        self.server.close()
