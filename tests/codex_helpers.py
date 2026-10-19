"""仅在隔离目录运行的假 Codex；只记录环境变量名，不记录变量值。"""
import json
import sys
from pathlib import Path

import pytest

from conftest import ROOT
from helpers import BASE
from test_writable_repos import wait_for

ADMIN = {'Authorization': 'Bearer isolated-admin'}


@pytest.fixture
def fake_codex(tmp_path):
    directory = tmp_path / 'bin'
    directory.mkdir()
    executable = directory / 'codex'
    driver = directory / 'driver.py'
    driver.write_text('#!' + sys.executable + '\n' + r'''
import json, os, pathlib, stat, subprocess, sys, time
base = pathlib.Path(__file__).parent
config = json.loads((base/'mode.json').read_text())
entry_environment = json.loads(sys.argv.pop(1)) if sys.platform == 'darwin' else sorted(os.environ)
with (base/'calls.jsonl').open('a') as calls:
    calls.write(json.dumps(dict(argv=sys.argv[1:]))+'\n')
if sys.argv[1:] == ['--version']:
    print('codex fixture')
    sys.exit(config.get('version_exit', 0))
args = sys.argv[1:]
root = pathlib.Path.cwd()
report = dict(argv=args, cwd=str(root), env=entry_environment,
              values={k: os.environ.get(k) for k in ('LC_ALL', 'CODEX_HOME') if k in os.environ},
              addresses={k: os.environ[k] for k in config.get('address_names', []) if k in os.environ},
              stdin_eof=(sys.stdin.buffer.read() == b''),
              stdin_null=stat.S_ISCHR(os.fstat(0).st_mode) and os.fstat(0).st_rdev == os.stat(os.devnull).st_rdev,
              pid=os.getpid(), started=time.time())
(root/'.invocation.tmp').write_text(json.dumps(report))
(root/'.invocation.tmp').replace(root/'invocation.json')
thread = args[args.index('resume')+1] if 'resume' in args else config.get('thread_id', 'fixture-thread-'+root.name)
print(json.dumps(dict(type='thread.started', thread_id=thread)), flush=True)
print(json.dumps(dict(type='item.completed', item=dict(type='agent_message', text='not-the-thread'))), flush=True)
if config.get('child'):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'])
    (root/'child.pid').write_text(str(child.pid))
time.sleep(config.get('delay', 0))
import yaml
if 'resume' not in args and '-s' in args and args[args.index('-s')+1] == 'workspace-write':
    from datetime import datetime, timezone
    document = yaml.safe_load((root/'scenarios.yaml').read_text())
    selected = [s['id'] for n in document['sops'] for s in n['scenarios']]
    selected = config.get('result_ids', selected)
    if config.get('evidence_root'):
        (root/'evidence').rmdir()
        (root/'evidence').symlink_to(config['evidence_root'], target_is_directory=True)
    rows = []
    for sid in selected:
        status = config.get('status', 'pass')
        evidence = config.get('evidence_path', 'evidence/'+sid+'.txt')
        row = dict(scenario=sid, status=status, actual='fixture observed '+sid,
                   executed_at=datetime.now(timezone.utc).isoformat(), executor='fixture', evidence=[evidence])
        if status in ('blocked', 'skip') and not config.get('no_finding'):
            row['finding'] = 'fixture finding'
        if 'evidence_path' not in config:
            (root/evidence).write_text('observed '+sid)
        rows.append(row)
    if not config.get('no_results'):
        (root/'results.jsonl').write_text(config.get('raw_results', '\n'.join(json.dumps(r) for r in rows)))
    report['finished'] = time.time()
    (root/'invocation.json').write_text(json.dumps(report))
    sys.exit(config.get('exit', 0))
snapshot = json.loads((root/'input.json').read_text()) if (root/'input.json').exists() else {}
cases = snapshot.get('cases', [])
entries = snapshot.get('entries', {}).get('entries', {})
sections = list(dict.fromkeys(next((t[8:] for t in c['tags'] if t.startswith('section:')), '未分组') for c in cases))
original = yaml.safe_load(snapshot.get('scenarios', '')) or {}
answer = dict(cases=[dict(id=c['id'], actor='测试岗', verify=next(iter(entries), 'api'), negative_type='invalid-input', initial_state=['已准备']) for c in cases],
              nodes=[dict(section=s, actor='测试岗', entry='浏览器') for s in sections],
              scenarios=[dict(id=s['id'], action='kept') for n in original.get('sops', []) for s in n['scenarios'] if s['id'] not in {c['id'] for c in cases}],
              negative_exempt=[dict(section=s, reason='本组用例只有正向依据', inferred=False) for s in sections],
              gaps=config.get('gaps', []), notes=[], relocate={})
for name, content in config.get('files', {}).items():
    extra = root/name
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_text(content)
if not config.get('no_final'):
    pathlib.Path(args[args.index('-o')+1]).write_text(config.get('final', json.dumps(answer, ensure_ascii=False)))
print(json.dumps(dict(type='turn.completed', usage={})), flush=True)
print(config.get('stderr', 'fixture stderr'), file=sys.stderr)
report['finished'] = time.time()
(root/'.invocation.tmp').write_text(json.dumps(report))
(root/'.invocation.tmp').replace(root/'invocation.json')
sys.exit(config.get('exit', 0))
''')
    if sys.platform == 'darwin':
        # Perl captures the exec environment before Python adds its own macOS variable.
        executable.write_text('#!/usr/bin/perl\nuse JSON::PP; exec ' + json.dumps(sys.executable) +
                              ', ' + json.dumps(str(driver)) + ', encode_json([sort keys %ENV]), @ARGV;\n')
    else:
        executable.write_text(driver.read_text())
    executable.chmod(0o755)
    (directory / 'mode.json').write_text('{}')
    return directory


def mode(directory, **settings):
    (directory / 'mode.json').write_text(json.dumps(settings))


def task_board(board_factory, fake_codex, repo=None, **env):
    import os
    command = [str(ROOT / '.venv/bin/sop-board'), '--writable-repo', f'github.com/demo-org/demo-app={repo}'] if repo else None
    environment = {'SOP_BOARD_ADMIN_TOKEN': 'isolated-admin',
                              'PATH': str(fake_codex) + os.pathsep + os.defpath,
                              'JIRA_API_TOKEN': 'isolated-jira', **env}
    if repo:
        from prepared_board_helpers import prepared_board
        return prepared_board(board_factory, command, repo, environment)
    return board_factory(command=command, env=environment)


def create_task(board, base=BASE):
    from cases_helpers import upload_cases
    current = board.request('GET', base + '/cases', auth=False)[1]
    if not current.get('cases'):
        upload_cases(board, base=base)
        return [t for t in board.request('GET', '/api/v1/tasks?project=' + base.split('/')[4] + '&batch=' + base.split('/')[6], auth=False)[1]['tasks'] if t['kind'] == 'merge'][-1]
    status, response, _ = board.request('POST', base + '/merges', {}, headers=ADMIN)
    assert status == 200, response
    return response['task']


def read_task(board, task):
    status, body, _ = board.request('GET', '/api/v1/tasks/' + str(task['id']), auth=False)
    assert status == 200, body
    return body['task']


def finished(board, task, state='已完成'):
    return wait_for(lambda: (t if (t := read_task(board, task))['status'] == state else None))
