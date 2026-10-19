"""被复制到临时 PATH 的假 codex：预设与调用证据均在仓库之外。"""
import json
import os
from pathlib import Path
import stat
import sys
import time

base = Path(__file__).parent
config = json.loads((base / 'mode.json').read_text())
environment = json.loads(sys.argv.pop(1)) if sys.platform == 'darwin' else sorted(os.environ)
args = sys.argv[1:]
if args == ['--version']:
    sys.exit(config.get('version_exit', 0))
calls = base / 'invocations.jsonl'
count = len(calls.read_text().splitlines()) if calls.exists() else 0
presets = config.get('calls', [{}])
mode = presets[min(count, len(presets) - 1)] if presets else {}
output = Path(args[args.index('-o') + 1])
report = dict(argv=args, cwd=os.getcwd(), env=environment, stdin_eof=sys.stdin.buffer.read() == b'',
              stdin_null=stat.S_ISCHR(os.fstat(0).st_mode), started=time.time())
with calls.open('a') as stream:
    stream.write(json.dumps(report) + '\n')
if mode.get('before_thread'):
    sys.exit(2)
thread = args[args.index('resume') + 1] if 'resume' in args else 'TH-1'
print(json.dumps(dict(type='thread.started', thread_id=thread)), flush=True)
time.sleep(mode.get('delay', 0))
if mode.get('exit'):
    sys.exit(mode['exit'])
if mode.get('no_final'):
    sys.exit(0)
if not (output.parent / 'problems.json').exists():
    import yaml
    from datetime import datetime, timezone
    document = yaml.safe_load((output.parent / 'scenarios.yaml').read_text())
    selected = [s['id'] for n in document['sops'] for s in n['scenarios']]
    (output.parent / 'evidence/observed.txt').write_text('执行已核对')
    (output.parent / 'results.jsonl').write_text('\n'.join(json.dumps(dict(scenario=sid, status='pass', actual='核对通过',
        evidence=['evidence/observed.txt'], executed_at=datetime.now(timezone.utc).isoformat())) for sid in selected))
    sys.exit(0)
problems = json.loads((output.parent / 'problems.json').read_text())
categories = mode.get('categories', {})
answer = dict(root_cause='折扣计算共用错误逻辑', problems=[dict(batch=p['batch'], scenario=p['scenario'],
    category=categories.get(p['scenario'], 'product'), handling='修复' if categories.get(p['scenario'], 'product') == 'product' else '转开发者',
    reason='独立测试依据') for p in problems if p['scenario'] not in mode.get('omit', [])],
    changed_files=['src/app.py'], tests=[dict(command='python3 -m unittest', result='pass')], plan='请核实环境与计费规则')
if not mode.get('no_changes'):
    for name, content in mode.get('files', {'src/app.py': f'print({count + 2})\n'}).items():
        target = Path.cwd() / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
for field in mode.get('remove', []):
    answer.pop(field, None)
output.write_text(mode.get('raw_final', json.dumps(answer, ensure_ascii=False)))
