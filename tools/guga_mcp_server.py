"""MCP stdio entrypoint. stdout contains protocol messages only; start the pet separately."""
import json
from pathlib import Path
import sys
import os,time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from launcher import _prepare_qt
_prepare_qt()
from PyQt5.QtCore import QCoreApplication
from PyQt5.QtNetwork import QLocalSocket
from core.mcp_contract import pipe_name, self_tools, dsh_tools,plugin_tools

VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')


def invoke(name, arguments):
    socket = QLocalSocket()
    socket.connectToServer(pipe_name())
    if not socket.waitForConnected(2000):
        return dict(ok=False, error='咕嘎尚未运行，请先启动桌宠')
    try:
        job=os.environ.get('GUGA_DSH_JOB') if '--dsh-tools' in sys.argv else None
        socket.write((json.dumps(dict(name=name, arguments=arguments,dsh_job=job,
            plugin_event=name=='dsh_host_event' and '--plugin-events' in sys.argv), ensure_ascii=False) + '\n').encode('utf-8'))
        socket.flush()
        socket.waitForBytesWritten(2000)
        deadline=time.monotonic()+(140 if job else 45)
        while not socket.canReadLine():
            if time.monotonic()>=deadline or not socket.waitForReadyRead(1000) and socket.state()==QLocalSocket.UnconnectedState:
                return dict(ok=False, error='咕嘎工具响应超时或连接已关闭；不要盲目重放输入')
        return json.loads(bytes(socket.readAll()).decode('utf-8'))
    finally:
        socket.abort()


def serve():
    _app = QCoreApplication([])
    initialized = False
    negotiated = False
    definitions=plugin_tools() if '--plugin-events' in sys.argv else dsh_tools() if '--dsh-tools' in sys.argv else self_tools()
    for line in sys.stdin:
        request = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError('Invalid request')
            if 'id' not in request:
                if request.get('method') == 'notifications/initialized' and negotiated:
                    initialized = True
                continue
            method = request.get('method')
            params = request.get('params') or {}
            if method == 'initialize':
                version = params.get('protocolVersion')
                negotiated = True
                result = dict(protocolVersion=version if version in VERSIONS else VERSIONS[0],
                    capabilities=dict(tools={}), serverInfo=dict(name='guga-self', version='2.0.0'))
            elif method == 'ping':
                result = {}
            elif not initialized:
                raise ValueError('Initialize first')
            elif method == 'tools/list':
                result = dict(tools=definitions)
            elif method == 'tools/call':
                names = {t['name'] for t in definitions}
                if params.get('name') not in names:
                    raise ValueError('Unknown tool')
                value = invoke(params['name'], params.get('arguments', {}))
                picture=value.pop('_image_url',None)
                if isinstance(value.get('result'),dict):picture=value['result'].pop('_image_url',None) or picture
                blocks=[dict(type='text',text=json.dumps(value,ensure_ascii=False))]
                if picture and picture.startswith('data:image/'):
                    header,data=picture.split(',',1)
                    blocks.append(dict(type='image',mimeType=header[5:].split(';')[0],data=data))
                result = dict(content=blocks,
                              isError=not value.get('ok', False) or (isinstance(value.get('result'),dict) and value['result'].get('ok') is False))
            else:
                response = dict(jsonrpc='2.0', id=request['id'], error=dict(code=-32601, message='Method not found'))
                print(json.dumps(response), flush=True)
                continue
            response = dict(jsonrpc='2.0', id=request['id'], result=result)
        except Exception as exc:
            response = dict(jsonrpc='2.0', id=request.get('id') if isinstance(request, dict) else None,
                            error=dict(code=-32602 if request is not None else -32700, message=str(exc)))
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    serve()
