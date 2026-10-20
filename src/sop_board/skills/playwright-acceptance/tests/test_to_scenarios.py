import io
import base64
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))

import to_scenarios  # noqa: E402

FIXTURES = ROOT / 'tests' / 'fixtures'
REPORT = FIXTURES / 'report.json'
FINDINGS = ['--finding', 'SOP-02-P02=Q3', '--finding', 'SOP-02-P04=Q4']


def load_report():
    return json.loads(REPORT.read_text(encoding='utf-8'))


def run(out, extra=()):
    args = ['--report', str(REPORT), '--out', str(out), '--executor', 'tester-a', *FINDINGS, *extra]
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        code = to_scenarios.main(args)
    return code, stderr.getvalue()


def read_rows(out):
    return [json.loads(line) for line in (Path(out) / 'results.jsonl').read_text(encoding='utf-8').splitlines()]


class HelperTests(unittest.TestCase):
    def test_scenario_id_is_first_token(self):
        self.assertEqual(to_scenarios.scenario_id('SOP-02-P01 人工接管'), 'SOP-02-P01')
        self.assertEqual(to_scenarios.scenario_id('REG-7 回归'), 'REG-7')
        self.assertIsNone(to_scenarios.scenario_id('没有编号的用例'))
        self.assertIsNone(to_scenarios.scenario_id(''))

    def test_classify_blocked_prefix_and_ansi(self):
        self.assertEqual(to_scenarios.classify({'status': 'passed'}), 'pass')
        self.assertEqual(to_scenarios.classify({'status': 'skipped'}), 'skip')
        self.assertEqual(to_scenarios.classify({'status': 'timedOut', 'error': {'message': 'Test timeout'}}), 'fail')
        blocked = {'status': 'failed', 'error': {'message': 'Error: \x1b[31m受阻：人机验证\x1b[39m'}}
        self.assertEqual(to_scenarios.classify(blocked), 'blocked')
        with self.assertRaises(to_scenarios.ConvertError):
            to_scenarios.classify({'status': 'weird'})

    def test_executed_at_is_end_time_with_timezone(self):
        value = to_scenarios.executed_at({'startTime': '2026-10-19T06:14:02.860Z', 'duration': 2500})
        self.assertEqual(value, '2026-10-19T06:14:05+00:00')
        parsed = datetime.fromisoformat(value)
        self.assertIsNotNone(parsed.tzinfo)


class ConvertTests(unittest.TestCase):
    def test_statuses_actual_and_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'round-1'
            code, stderr = run(out)
            self.assertEqual(code, 0, stderr)
            rows = {row['scenario']: row for row in read_rows(out)}
            self.assertEqual(sorted(rows), ['SOP-02-P01', 'SOP-02-P02', 'SOP-02-P03', 'SOP-02-P04'])
            self.assertIn('没有编号的用例', stderr)

            p01 = rows['SOP-02-P01']
            self.assertEqual(p01['status'], 'pass')
            self.assertEqual(p01['executor'], 'tester-a')
            self.assertEqual(p01['executed_at'], '2026-10-19T06:14:05+00:00')
            self.assertIn('已完成 3 步', p01['actual'])
            self.assertIn('发送 123456789', p01['actual'])
            self.assertEqual(p01['evidence'], ['evidence/SOP-02-P01/01-home.png', 'evidence/SOP-02-P01/02-video.webm'])
            for ref in p01['evidence']:
                self.assertTrue((out / ref).is_file(), ref)
            self.assertNotIn('finding', p01)

            p02 = rows['SOP-02-P02']
            self.assertEqual(p02['status'], 'blocked')
            self.assertEqual(p02['finding'], 'Q3')
            self.assertTrue(p02['actual'].startswith('受阻：客户离线'))
            self.assertEqual(p02['evidence'], ['evidence/SOP-02-P02/01-error-context.md'])

            p03 = rows['SOP-02-P03']
            self.assertEqual(p03['status'], 'fail')
            self.assertIn('第 2 步「核对会话列表」失败', p03['actual'])
            self.assertNotIn('\x1b', p03['actual'])
            self.assertIn('toBeVisible', p03['actual'])
            self.assertEqual(p03['evidence'], ['evidence/SOP-02-P03/01-list.png'])
            self.assertEqual((out / p03['evidence'][0]).read_bytes(), b'png')

            p04 = rows['SOP-02-P04']
            self.assertEqual(p04['status'], 'skip')
            self.assertEqual(p04['finding'], 'Q4')
            self.assertEqual(p04['evidence'], [])
            self.assertIn('测试店铺没有离线客户', p04['actual'])

            for row in rows.values():
                self.assertFalse(set(row) - to_scenarios_keys())

    def test_blocked_without_finding_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'r'
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = to_scenarios.main(['--report', str(REPORT), '--out', str(out), '--executor', 'x', '--finding', 'SOP-02-P04=Q4'])
            self.assertEqual(code, 2)
            self.assertIn('SOP-02-P02', stderr.getvalue())
            self.assertFalse((out / 'results.jsonl').exists())

    def test_pass_without_evidence_fails(self):
        report = load_report()
        spec = report['suites'][0]['specs'][0]
        spec['tests'][0]['results'][0]['attachments'] = []
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(to_scenarios.ConvertError) as ctx:
                to_scenarios.convert(report, Path(tmp) / 'r', 'x', {'SOP-02-P02': 'Q3', 'SOP-02-P04': 'Q4'}, report_dir=FIXTURES)
            self.assertIn('SOP-02-P01', str(ctx.exception))

    def test_pending_cross_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'r'
            code, stderr = run(out, ['--pending', str(FIXTURES / 'pending.json'), '--allow-partial', '--session', 'round-1'])
            self.assertEqual(code, 0, stderr)
            self.assertIn('SOP-02-P05', stderr)
            self.assertTrue(all(row['session'] == 'round-1' for row in read_rows(out)))
            coverage = json.loads((out / 'coverage.json').read_text())
            self.assertEqual(coverage['missing'], ['SOP-02-P05'])
            self.assertEqual(coverage['mode'], 'partial')
        report = load_report()
        report['suites'][0]['specs'][0]['title'] = 'SOP-99-X01 不在清单'
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(to_scenarios.ConvertError) as ctx:
                to_scenarios.convert(report, Path(tmp) / 'r', 'x', {'SOP-02-P02': 'Q3', 'SOP-02-P04': 'Q4'},
                                     pending_ids=['SOP-02-P01', 'SOP-02-P02', 'SOP-02-P03', 'SOP-02-P04'], report_dir=FIXTURES)
            self.assertIn('SOP-99-X01', str(ctx.exception))

    def test_multiple_tests_per_scenario_take_worst(self):
        report = load_report()
        specs = report['suites'][0]['specs']
        specs[1]['title'] = 'SOP-02-P01 第二条路径'
        with tempfile.TemporaryDirectory() as tmp:
            rows, _ = to_scenarios.convert(report, Path(tmp) / 'r', 'x', {'SOP-02-P01': 'Q3', 'SOP-02-P04': 'Q4'}, report_dir=FIXTURES)
            row = {r['scenario']: r for r in rows}['SOP-02-P01']
            self.assertEqual(row['status'], 'blocked')
            self.assertIn('第二条路径', row['actual'])
            self.assertEqual(len(row['evidence']), 3)
            self.assertEqual(row['executed_at'], '2026-10-19T06:14:11+00:00')

    def test_same_scenario_same_attachment_names_preserve_both_contents(self):
        spec = load_report()['suites'][0]['specs'][0]
        specs = []
        for text in (b'first screenshot', b'second screenshot'):
            item = copy.deepcopy(spec)
            item['tests'][0]['results'][0]['attachments'] = [{'name': 'screenshot', 'contentType': 'image/png', 'body': base64.b64encode(text).decode()}]
            specs.append(item)
        report = {'suites': [{'specs': specs}]}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'r'
            rows, _ = to_scenarios.convert(report, out, 'x', {})
            refs = rows[0]['evidence']
            self.assertEqual(len(set(refs)), 2)
            self.assertEqual([(out / ref).read_bytes() for ref in refs], [b'first screenshot', b'second screenshot'])

    def test_missing_pending_rejects_complete_execution_without_results_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'r'
            code, stderr = run(out, ['--pending', str(FIXTURES / 'pending.json')])
            self.assertEqual(code, 2)
            self.assertIn('SOP-02-P05', stderr)
            self.assertFalse((out / 'results.jsonl').exists())

    def test_invalid_pending_entry_is_not_silently_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'pending.json'
            for value in ({'pending': [{'title': 'missing id'}]}, {'pending': ['A-1', 'A-1']}):
                p.write_text(json.dumps(value))
                with self.assertRaises(to_scenarios.ConvertError):
                    to_scenarios.load_pending(p)

    def test_scope_rejects_other_batch_fingerprint_and_missing_annotation(self):
        report = load_report()
        scope = {'project': 'repo', 'batch': 'original', 'scenario_file_sha256': 'a' * 64,
                 'scenarios': {'SOP-02-P0'+str(i): 'b' * 64 for i in range(1, 5)}}
        for spec in report['suites'][0]['specs']:
            sid = to_scenarios.scenario_id(spec['title'])
            if not sid:
                continue
            binding = {key: scope[key] for key in ('project', 'batch', 'scenario_file_sha256')}
            binding.update(scenario=sid, fingerprint=scope['scenarios'][sid])
            for test in spec['tests']:
                test.setdefault('annotations', []).append({'type': 'sop-scope', 'description': json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(',', ':'))})
        findings = {'SOP-02-P02': 'Q3', 'SOP-02-P04': 'Q4'}
        with tempfile.TemporaryDirectory() as tmp:
            rows, _ = to_scenarios.convert(report, Path(tmp)/'good', 'x', findings, scope=scope, report_dir=FIXTURES)
            self.assertEqual(len(rows), 4)
            with self.assertRaises(to_scenarios.ConvertError):
                to_scenarios.convert(report, Path(tmp)/'omitted-scope', 'x', findings, report_dir=FIXTURES)
            skipped = copy.deepcopy(report)
            skipped['config'] = {'metadata': {'sopScope': scope}}
            skip_test = skipped['suites'][0]['specs'][3]['tests'][0]
            skip_test['annotations'] = [a for a in skip_test.get('annotations', []) if a.get('type') != 'sop-scope']
            rows, _ = to_scenarios.convert(skipped, Path(tmp)/'declared-skip', 'x', findings, scope=scope, report_dir=FIXTURES)
            self.assertEqual(len(rows), 4)
            for change in ('batch', 'fingerprint', 'missing'):
                changed = copy.deepcopy(report)
                annotations = changed['suites'][0]['specs'][0]['tests'][0]['annotations']
                if change == 'missing':
                    annotations.clear()
                else:
                    binding = json.loads(annotations[-1]['description'])
                    binding[change] = 'different'
                    annotations[-1]['description'] = json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                with self.assertRaises(to_scenarios.ConvertError):
                    to_scenarios.convert(changed, Path(tmp)/change, 'x', findings, scope=scope, report_dir=FIXTURES)

    def test_scope_rejects_changed_source_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, manifest = Path(tmp)/'source.yaml', Path(tmp)/'scope.json'
            source.write_text('original')
            manifest.write_text(json.dumps({'project': 'repo', 'batch': 'batch', 'scenario_file_sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'scenarios': {'SOP-1': 'b'*64}}))
            self.assertEqual(to_scenarios.load_scope(manifest, source)['batch'], 'batch')
            source.write_text('changed')
            with self.assertRaises(to_scenarios.ConvertError):
                to_scenarios.load_scope(manifest, source)

    def test_large_evidence(self):
        report = load_report()
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / 'big.webm'
            with big.open('wb') as handle:
                handle.truncate(to_scenarios.EVIDENCE_LIMIT_BYTES + 1)
            spec = report['suites'][0]['specs'][0]
            spec['tests'][0]['results'][0]['attachments'].append({'name': 'big', 'contentType': 'video/webm', 'path': str(big)})
            findings = {'SOP-02-P02': 'Q3', 'SOP-02-P04': 'Q4'}
            with self.assertRaises(to_scenarios.ConvertError):
                to_scenarios.convert(report, Path(tmp) / 'a', 'x', findings, report_dir=FIXTURES)
            rows, warnings = to_scenarios.convert(report, Path(tmp) / 'b', 'x', findings, report_dir=FIXTURES, skip_large=True)
            self.assertTrue(any('64 MiB' in w for w in warnings))
            self.assertEqual(len({r['scenario']: r for r in rows}['SOP-02-P01']['evidence']), 2)

    def test_global_setup_error_is_reported(self):
        report = {'config': {}, 'suites': [], 'errors': [{'message': 'Error: 环境检查未通过，本轮未执行任何场景：\n- 录屏需要 ffmpeg'}], 'stats': {}}
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(to_scenarios.ConvertError) as ctx:
                to_scenarios.convert(report, Path(tmp) / 'r', 'x', {})
            self.assertIn('环境检查未通过', str(ctx.exception))

    def test_bad_finding_format(self):
        with self.assertRaises(to_scenarios.ConvertError):
            to_scenarios.parse_findings(['SOP-02-P02'])
        with self.assertRaises(to_scenarios.ConvertError):
            to_scenarios.parse_findings(['=Q3'])


def to_scenarios_keys():
    return {'scenario', 'status', 'actual', 'executed_at', 'executor', 'evidence', 'session', 'finding', 'note'}


if __name__ == '__main__':
    unittest.main()
