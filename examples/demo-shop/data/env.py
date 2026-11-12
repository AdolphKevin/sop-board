"""演示项目的 adapter.env：按检出启动演示商城（库存演示项目另有库存服务），就绪后输出入口地址，并按服务契约上报状态、响应停启。

看板守护进程在数据目录前台运行本脚本，结束时向整个进程组发 SIGTERM。
收到的 SOP_* 变量逐行打印到环境日志，并写入 SOP_ENV_LOG_DIR/received.json，供核对端口偏移与检出路径。

服务契约（config.yaml 的 adapter.services 声明 shop 与 stock 时由看板读取；没声明时看板忽略这些文件）：
- 状态写在 SOP_ENV_LOG_DIR/services/<名称>.state，JSON：{"state": "running|stopped|absent|crashed", "log": 日志路径}。
- 看板写 services/stock.request 请求停止库存服务、删除它请求启动；本脚本每 0.2 秒轮询一次，完成后改写状态文件。
- 库存服务只在 adapter.ports 有第二个端口时启用（库存演示项目）；本检出没有 stock 仓库时上报 absent（本机未搭建）。
- 库存服务意外退出时上报 crashed 并附日志，不重启、环境照常；商城退出时上报 crashed 后本脚本以同一退出码退出。

两个验收开关取自启动守护进程时的环境：DEMO_STOCK_IGNORE_STOP=1 时忽略停止请求；DEMO_STOCK_CRASH=1 时库存服务启动后立即异常退出。
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
services = log_dir / 'services'
services.mkdir(parents=True, exist_ok=True)

ports = json.loads(os.environ['SOP_PORTS'])
app, lib = Path(os.environ['SOP_REPO_APP']), Path(os.environ['SOP_REPO_LIB'])
stock_repo = os.environ.get('SOP_REPO_STOCK')
stock_port = ports[1] if len(ports) > 1 else None
stock_url = f'http://127.0.0.1:{stock_port}' if stock_port and stock_repo else ''
children = {}


def report(name, state, log=None):
    """原子改写状态文件：先写临时文件再换名，看板读不到半截内容。"""
    value = {'state': state}
    if log:
        value['log'] = str(log)
    temporary = services / f'.{name}.state.tmp'
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, services / f'{name}.state')
    print(f'service {name}: {state}', flush=True)


def answering(port):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
    try:
        connection.request('GET', '/')
        return connection.getresponse().status == 200
    except OSError:
        return False
    finally:
        connection.close()


def start(name, argv, port, timeout=20):
    """启动服务并等它应答；返回进程，启动即退出时返回 None。日志追加到 SOP_ENV_LOG_DIR/<名称>.log。"""
    log = log_dir / f'{name}.log'
    with log.open('ab') as stream:
        stream.write(f'--- {time.strftime("%Y-%m-%d %H:%M:%S")} 启动 {name} ---\n'.encode())
        stream.flush()
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT)
    children[name] = process
    deadline = time.time() + timeout
    while not answering(port):
        if process.poll() is not None:
            return None
        if time.time() > deadline:
            process.terminate()
            process.wait()
            return None
        time.sleep(0.2)
    return process


def stop(name):
    process = children.pop(name, None)
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def shutdown(*_):
    for name in list(children):
        stop(name)
    sys.exit(0)


signal.signal(signal.SIGTERM, shutdown)


def start_stock():
    argv = [sys.executable, str(Path(stock_repo) / 'stocksvc' / 'server.py'), '--port', str(stock_port),
            '--db', str(log_dir / 'stock.sqlite3')]
    if os.environ.get('DEMO_STOCK_CRASH') == '1':
        argv.append('--crash')
    process = start('stock', argv, stock_port)
    report('stock', 'running' if process else 'crashed', None if process else log_dir / 'stock.log')
    return process


if stock_port:
    if stock_url:
        start_stock()
    else:
        report('stock', 'absent')

shop_argv = [sys.executable, str(app / 'shop' / 'server.py'), '--port', str(ports[0]),
             '--db', str(log_dir / 'shop.sqlite3'), '--lib', str(lib)]
if stock_url:
    shop_argv += ['--stock-url', stock_url]
shop = start('shop', shop_argv, ports[0])
if shop is None:
    report('shop', 'crashed', log_dir / 'shop.log')
    stop('stock')
    sys.exit('演示商城没有启动起来；日志：' + str(log_dir / 'shop.log'))
report('shop', 'running')
url = f'http://127.0.0.1:{ports[0]}'
ready = {'APP_URL': url, 'WEB_URL': url}
if stock_url:
    ready['STOCK_URL'] = stock_url
print('sop-board: ready ' + json.dumps(ready), flush=True)

stopped_by_request = False
while True:
    time.sleep(0.2)
    if shop.poll() is not None:
        report('shop', 'crashed', log_dir / 'shop.log')
        stop('stock')
        sys.exit(shop.returncode or 1)
    if not stock_url:
        continue
    requested = (services / 'stock.request').exists()
    stock = children.get('stock')
    if requested and stock is not None and stock.poll() is None and not stopped_by_request:
        if os.environ.get('DEMO_STOCK_IGNORE_STOP') == '1':
            continue
        stop('stock')
        stopped_by_request = True
        report('stock', 'stopped')
    elif not requested and stopped_by_request:
        stopped_by_request = False
        start_stock()
    elif stock is not None and stock.poll() is not None and not stopped_by_request:
        children.pop('stock', None)
        report('stock', 'crashed', log_dir / 'stock.log')
