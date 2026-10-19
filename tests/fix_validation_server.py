"""仅供 T1 测试的 T2 接口替代；服务及修复链仍用真实实现。"""
import json
import os
from pathlib import Path
import time

from sop_board.fixes import Fixes
from sop_board.cli import main


def validate(self, task, problems):
    preset = json.loads(Path(os.environ['FIX_TEST_VALIDATION']).read_text())
    result = preset[min(task['attempt'] - 1, len(preset) - 1)]
    time.sleep(result.get('delay', 0))
    return dict(result, scenarios=[dict(batch=p['batch'], scenario=p['scenario']) for p in problems])


Fixes.validate = validate
raise SystemExit(main())
