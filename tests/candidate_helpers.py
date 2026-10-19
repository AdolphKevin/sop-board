"""通过 SOP_PORT 监听的最小候选服务，支持退出、未就绪与子进程清理。"""
import json
import sys

from check_helpers import record_commit, declarations, save_declarations


SERVER = r'''
import json, os, pathlib, subprocess, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
if sys.argv[1] == 'child':
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status = int(sys.argv[2]) if len(sys.argv) > 2 else 500
            self.send_response(status)
            if status == 302:
                self.send_header('Location', 'http://127.0.0.1:1/unavailable')
            self.end_headers()
        def log_message(self, *args): pass
    HTTPServer(('127.0.0.1', int(os.environ['SOP_PORT'])), Handler).serve_forever()
preset = pathlib.Path(sys.argv[1])
config = json.loads(preset.read_text())
entry_env = json.loads(sys.argv[2]) if sys.platform == 'darwin' else sorted(os.environ)
call = dict(cwd=str(pathlib.Path.cwd()), env=entry_env, port=os.environ['SOP_PORT'],
            address=os.environ['APP_URL'], pid=os.getpid())
if config.get('mode') == 'exit':
    preset.with_suffix('.call').write_text(json.dumps(call))
    print('listen failed: address in use', flush=True)
    sys.exit(2)
child = subprocess.Popen([sys.executable, __file__, 'child', str(config.get('status', 500))] if config.get('mode') != 'timeout' else
                         [sys.executable, '-c', 'import time; time.sleep(300)'])
call['child'] = child.pid
preset.with_suffix('.call').write_text(json.dumps(call))
print('candidate starting', flush=True)
time.sleep(300)
'''


def start_entry(repo, tmp_path):
    preset = tmp_path / 'candidate.json'
    preset.write_text('{}')
    (repo / 'serve.py').write_text(SERVER)
    if sys.platform == 'darwin':
        launcher = repo / 'serve'
        launcher.write_text('#!/usr/bin/perl\nuse JSON::PP; use FindBin; exec '+json.dumps(sys.executable)+
            ', "$FindBin::Bin/serve.py", @ARGV, encode_json([sort keys %ENV]);\n')
        launcher.chmod(0o755)
        command = ['./serve', str(preset)]
    else:
        command = [sys.executable, 'serve.py', str(preset)]
    value = declarations(repo)
    template = dict(value['entries']['自定义入口'])
    value['entries']['api'] = dict(template, surface='http', candidate='start', start=command, address_env='APP_URL')
    value['entries']['live'] = dict(template, surface='http', candidate='in-place', address_env='LIVE_URL')
    save_declarations(repo, value)
    record_commit(repo)
    return preset
