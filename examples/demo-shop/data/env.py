"""演示项目的 adapter.env：按检出启动演示商城，就绪后输出入口地址。

看板守护进程在数据目录前台运行本脚本，结束时向整个进程组发 SIGTERM。
收到的 SOP_* 变量逐行打印到环境日志，并写入 SOP_ENV_LOG_DIR/received.json，供核对端口偏移与检出路径。
"""
import http.client
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

received = {k: v for k, v in sorted(os.environ.items()) if k.startswith('SOP_')}
for name, value in received.items():
    print(f'{name}={value}', flush=True)
log_dir = Path(os.environ['SOP_ENV_LOG_DIR'])
(log_dir / 'received.json').write_text(json.dumps(received, ensure_ascii=False, indent=2), encoding='utf-8')

port = json.loads(os.environ['SOP_PORTS'])[0]
app, lib = Path(os.environ['SOP_REPO_APP']), Path(os.environ['SOP_REPO_LIB'])
server = subprocess.Popen([sys.executable, str(app / 'shop' / 'server.py'), '--port', str(port),
                           '--db', str(log_dir / 'shop.sqlite3'), '--lib', str(lib)])
signal.signal(signal.SIGTERM, lambda *_: (server.terminate(), sys.exit(0)))


def answering():
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
    try:
        connection.request('GET', '/')
        return connection.getresponse().status == 200
    except OSError:
        return False
    finally:
        connection.close()


deadline = time.time() + 20
while not answering():
    if server.poll() is not None:
        sys.exit(f'演示商城已退出，退出码 {server.returncode}')
    if time.time() > deadline:
        server.terminate()
        sys.exit('演示商城 20 秒内没有应答')
    time.sleep(0.2)
url = f'http://127.0.0.1:{port}'
print('sop-board: ready ' + json.dumps({'APP_URL': url, 'WEB_URL': url}), flush=True)
sys.exit(server.wait())
