"""I4 的独立样本；通过 HTTP 上传和读取，不调用组装内部方法。"""
import json

from cases_helpers import cases_document, cases_body, upload_cases
from codex_helpers import read_task
from helpers import BASE
from test_writable_repos import wait_for


def merge_cases():
    document = cases_document()
    document['test_cases'][0]['preconditions'] = ['已注册账号 alice']
    document['test_cases'][0]['steps'] = ['输入 "admin" 并登录']
    document['test_cases'][1]['tags'].append('negative')
    document['test_cases'][1]['expected_results'] = ['拒绝登录']
    return document


def answer(document=None, original=None, verify='自定义入口'):
    cases = (document or merge_cases())['test_cases']
    sections = list(dict.fromkeys(next((t[8:] for t in c['tags'] if t.startswith('section:')), '未分组')
                                 for c in sorted(cases, key=lambda c: c['id'])))
    return dict(cases=[dict(id=c['id'], actor='测试岗', verify=verify,
                           **({'negative_type': 'invalid-input'} if 'negative' in c['tags'] else {}),
                           **({'initial_state': ['访客未登录']} if not c.get('preconditions') else {})) for c in cases],
                nodes=[dict(section=s, actor='测试岗', entry='浏览器登录页') for s in sections],
                scenarios=[dict(id=s['id'], action='kept', reason='用例未覆盖的业务保证')
                           for n in (original or {}).get('sops', []) for s in n['scenarios']
                           if s['id'] not in {c['id'] for c in cases}],
                negative_exempt=[dict(section=s, reason='仅有正向依据', inferred=False) for s in sections],
                gaps=[], notes=[], relocate={})


def upload_merge(board, document=None, base=BASE):
    upload_cases(board, cases_body(document or merge_cases()), base=base)
    return [t for t in board.request('GET', '/api/v1/tasks?project=' + base.split('/')[4] + '&batch=' + base.split('/')[6], auth=False)[1]['tasks'] if t['kind'] == 'merge'][-1]


def settled(board, task):
    return wait_for(lambda: t if (t := read_task(board, task))['status'] not in ('排队中', '运行中') else None)


def checked(board, task):
    return json.loads(board.request('GET', task['files']['check.json'], auth=False)[1])


def download(board, task, suffix='scenario-file', base=BASE):
    return board.request('GET', base + f"/merges/{task['id']}/" + suffix, auth=False)
