import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))

import case_tags  # noqa: E402

FIXTURES = ROOT / 'tests' / 'fixtures'


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding='utf-8'))


class BuildTests(unittest.TestCase):
    def test_maps_scenarios_and_cases_both_ways(self):
        mapping, conflicts = case_tags.build(load('pairs.json'))
        self.assertEqual(conflicts, [])
        self.assertEqual(mapping['scenarios'], {
            'SOP-02-P01': ['CASE-037', 'CASE-038'],
            'SOP-02-P02': ['CASE-038'],
        })
        self.assertEqual(mapping['cases'], {
            'CASE-037': ['SOP-02-P01'],
            'CASE-038': ['SOP-02-P01', 'SOP-02-P02'],
        })
        self.assertEqual(mapping['disposed'], {
            'CASE-040': {'state': 'deferred', 'reason': '依赖邮件通道', 'confirmed_by': 'tester-a'},
        })

    def test_conflict_cases_are_reported_and_not_tagged(self):
        mapping, conflicts = case_tags.build(load('pairs-conflict.json'))
        self.assertEqual(conflicts, ['CASE-041'])
        self.assertNotIn('SOP-02-P03', mapping['scenarios'])
        self.assertNotIn('CASE-041', mapping['cases'])

    def test_rejects_bad_shapes(self):
        with self.assertRaises(case_tags.PairsError):
            case_tags.build([])
        with self.assertRaises(case_tags.PairsError):
            case_tags.build({'covered': [{'case': 'CASE 1', 'scenarios': ['SOP-1']}]})
        with self.assertRaises(case_tags.PairsError):
            case_tags.build({'covered': [{'case': 'CASE-1', 'scenarios': []}]})
        with self.assertRaises(case_tags.PairsError):
            case_tags.build({'covered': [], 'extra': []})


class MainTests(unittest.TestCase):
    def test_writes_cases_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'nested' / 'cases.json'
            code = case_tags.main(['--pairs', str(FIXTURES / 'pairs.json'), '--out', str(out)])
            self.assertEqual(code, 0)
            data = json.loads(out.read_text(encoding='utf-8'))
            self.assertEqual(data['scenarios']['SOP-02-P01'], ['CASE-037', 'CASE-038'])

    def test_conflict_blocks_output_unless_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'cases.json'
            args = ['--pairs', str(FIXTURES / 'pairs-conflict.json'), '--out', str(out)]
            self.assertEqual(case_tags.main(args), 2)
            self.assertFalse(out.exists())
            self.assertEqual(case_tags.main(args + ['--allow-conflict']), 0)
            self.assertTrue(out.exists())

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = case_tags.main(['--pairs', str(Path(tmp) / 'none.json'), '--out', str(Path(tmp) / 'o.json')])
            self.assertEqual(code, 2)


if __name__ == '__main__':
    unittest.main()
