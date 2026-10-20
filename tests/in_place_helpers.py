"""SOP-55 的运行环境：服务始终读取开发者工作区，由测试负责启停。"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading

import yaml

from check_helpers import record_commit
from test_check_entries import declarations, save_declarations
from test_writable_repos import git


SCRIPT = '''
import ast, json, os, pathlib, subprocess, sys, time
kind, control = sys.argv[1], pathlib.Path(sys.argv[2])
mode = json.loads((control / 'mode.json').read_text())
def trace(event):
    with (control / 'trace.jsonl').open('a') as stream:
        stream.write(json.dumps(dict(event=event, cwd=os.getcwd(), env=sorted(os.environ),
            commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
            baseline=subprocess.check_output(['git', '-C', (control / 'root.txt').read_text(), 'rev-parse', 'HEAD'], text=True).strip(),
            started=time.time())) + '\\n')
trace(kind + '-start')
time.sleep(mode.get(kind + '_delay', 0))
if kind == 'check':
    ast.parse(pathlib.Path('src/app.py').read_text())
    failure = '<failure/>' if mode.get('check_fail') else ''
    pathlib.Path(sys.argv[3], 'report.xml').write_text(
        '<testsuite><testcase classname="app" name="valid">' + failure + '</testcase></testsuite>')
elif mode.get('reload_fail'):
    print('reload failed', flush=True)
    trace('reload-failed')
    sys.exit(3)
trace(kind + '-end')
'''


def prepare_in_place(root, control, *, reload=True):
    control.mkdir()
    (control / 'mode.json').write_text('{}')
    (control / 'root.txt').write_text(str(root))
    (root / 'scripts').mkdir(exist_ok=True)
    (root / 'scripts/runtime.py').write_text(SCRIPT)
    value = declarations(root)
    entry = value['entries']['自定义入口']
    entry.update(surface='http', candidate='in-place', address_env='APP_URL')
    if reload:
        entry['reload'] = [sys.executable, 'scripts/runtime.py', 'reload', str(control)]
    value['checks'] = dict(guard=dict(argv=[sys.executable, 'scripts/runtime.py', 'check', str(control), '{output}'],
        format='junit', report='{output}/report.xml'))
    save_declarations(root, value)
    path = root / 'docs/sop/demo.yaml'
    document = yaml.safe_load(path.read_text())
    document['sops'][0]['scenarios'][1]['regression'] = dict(check='guard', cases=['app::valid'])
    path.write_text(yaml.safe_dump(document, allow_unicode=True))
    record_commit(root)


def trace(control):
    path = control / 'trace.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@contextmanager
def runtime(root, control):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(dict(content=(root / 'src/app.py').read_text(),
                commit=git(root, 'rev-parse', 'HEAD'))).encode()
            with (control / 'requests.jsonl').open('a') as stream:
                stream.write(body.decode() + '\n')
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
