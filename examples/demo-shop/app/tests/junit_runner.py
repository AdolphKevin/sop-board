"""用标准库 unittest 运行 tests/ 下的测试，并把结果写成 junit XML（守护命令用）。

python3 tests/junit_runner.py --junit <路径> [--lib <lib 仓库目录>] [--pattern test_*.py]

--pattern 选择测试文件：缺省 test_*.py（单元层）；库存演示项目的守护命令 api 用 api_*.py（经运行中的商城接口，读 APP_URL）。
"""
import argparse
from pathlib import Path
import sys
import unittest
from xml.sax.saxutils import quoteattr


class Recorder(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.records.append((test, 'passed', ''))

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.records.append((test, 'failure', self._exc_info_to_string(err, test)))

    def addError(self, test, err):
        super().addError(test, err)
        self.records.append((test, 'error', self._exc_info_to_string(err, test)))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.records.append((test, 'skipped', reason))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--junit', required=True)
    parser.add_argument('--lib', default=str(Path(__file__).resolve().parents[2] / 'lib'))
    parser.add_argument('--pattern', default='test_*.py')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    sys.path[:0] = [str(root.parent), args.lib]
    suite = unittest.defaultTestLoader.discover(str(root), pattern=args.pattern, top_level_dir=str(root.parent))
    result = unittest.TextTestRunner(resultclass=Recorder, verbosity=2).run(suite)
    cases = []
    for test, status, detail in result.records:
        classname, _, name = test.id().rpartition('.')
        inner = '' if status == 'passed' else (f'<skipped message={quoteattr(detail)}/>' if status == 'skipped'
                                               else f'<{status} message={quoteattr(detail[-500:])}/>')
        cases.append(f'<testcase classname={quoteattr(classname)} name={quoteattr(name)}>{inner}</testcase>')
    Path(args.junit).write_text(f'<testsuite tests="{len(cases)}">' + ''.join(cases) + '</testsuite>', encoding='utf-8')
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == '__main__':
    main()
