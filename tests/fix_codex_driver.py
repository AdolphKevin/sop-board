"""被复制到临时 PATH 的假 codex：预设与调用证据均在仓库之外。"""
import json
import os
from pathlib import Path
import stat
import subprocess
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
previous = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
repair_index = sum('/validations/' not in c['argv'][c['argv'].index('-o') + 1] for c in previous)
presets = config.get('calls', [{}])
mode = presets[min(repair_index, len(presets) - 1)] if presets else {}
output = Path(args[args.index('-o') + 1])
report = dict(argv=args, cwd=os.getcwd(), env=environment, stdin_eof=sys.stdin.buffer.read() == b'',
              stdin_null=stat.S_ISCHR(os.fstat(0).st_mode), started=time.time())
if (output.parent / 'problems.json').exists():
    report['start_commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
time.sleep(config.get('invocation_delay', 0))
with calls.open('a') as stream:
    stream.write(json.dumps(report) + '\n')
if mode.get('before_thread'):
    sys.exit(2)
retest = (output.parent / 'retest.json').exists()
if retest:
    prior = [json.loads(line) for line in calls.read_text().splitlines()[:-1]]
    index = sum('/validations/' in c['argv'][c['argv'].index('-o') + 1] for c in prior)
    presets = config.get('retests', [{}])
    mode = presets[min(index, len(presets) - 1)] if presets else {}
thread = args[args.index('resume') + 1] if 'resume' in args else f'TH-R{index + 1}' if retest else 'TH-1'
print(json.dumps(dict(type='thread.started', thread_id=thread)), flush=True)
time.sleep(mode.get('delay', 0))
if mode.get('exit'):
    sys.exit(mode['exit'])
if mode.get('no_final'):
    sys.exit(0)
if retest:
    from datetime import datetime, timezone
    selected = json.loads((output.parent / 'retest.json').read_text())
    observation = '独立复测已核对'
    observed_status = 'pass'
    if mode.get('observe_http'):
        from urllib.request import build_opener, ProxyHandler
        entries = json.loads((output.parent / 'entries.json').read_text())
        observations = []
        for scene in selected:
            name = scene['scene']['verify']
            variable = entries['entries'][name]['address_env']
            with build_opener(ProxyHandler({})).open(os.environ[variable], timeout=5) as response:
                observations.append(json.loads(response.read()))
        observation = json.dumps(observations, ensure_ascii=False)
        observed_status = 'pass' if all(r['content'] == mode['expected_content'] for r in observations) else 'fail'
    if mode.get('observe_product'):
        entries = json.loads((output.parent / 'entries.json').read_text())
        target = entries['targets'][selected[0]['scene']['verify']]['directory']
        run = subprocess.run([sys.executable, 'src/app.py'], cwd=target, capture_output=True, text=True, timeout=5)
        observation = json.dumps(dict(command=[sys.executable, 'src/app.py'], cwd=target,
            code=run.returncode, stdout=run.stdout, stderr=run.stderr), ensure_ascii=False)
        observed_status = 'pass' if run.returncode == 0 and run.stdout == mode['expected_stdout'] else 'fail'
    (output.parent / 'evidence/observed.txt').write_text(observation)
    rows = []
    for scene in selected:
        status = mode.get('statuses', {}).get(scene['scenario'], observed_status)
        rows.append(dict(batch=scene['batch'], scenario=scene['scenario'], status=status, actual='独立复测结果 ' + status,
            evidence=['evidence/observed.txt'], executed_at=datetime.now(timezone.utc).isoformat(),
            **(dict(finding='未满足条件') if status in ('blocked', 'skip') else {})))
        rows[-1].update(mode.get('result_fields', {}))
    (output.parent / 'results.jsonl').write_text('\n'.join(json.dumps(row) for row in rows))
    output.write_text('复测完成')
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
