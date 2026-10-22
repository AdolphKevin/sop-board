"""开发会话在本机登记回归报告；不提供远程写入入口。"""
import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from .adapters import Environment
from .errors import APIError
from .repositories import Discovery
from .service import Service
from .storage import Store
from .workspace import Workspace


def unique_fields(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('报告含重复字段：' + key)
        value[key] = item
    return value


def invalid_constant(value):
    raise ValueError('报告不接受非有限数字')


def command(arguments):
    parser = argparse.ArgumentParser(prog='sop-board distill', description='在本机登记场景与回归测试的关联')
    submit = parser.add_subparsers(dest='action', required=True).add_parser('submit')
    submit.add_argument('workspace')
    submit.add_argument('--report', required=True, type=Path)
    submit.add_argument('--data', type=Path)
    args = parser.parse_args(arguments)
    try:
        report = json.loads(args.report.read_text(encoding='utf-8'), object_pairs_hook=unique_fields, parse_constant=invalid_constant)
        workspace = Workspace(args.workspace, args.data)
        if workspace.name != args.workspace:
            raise ValueError('工作区名称与数据目录登记不一致')
        if not workspace.configured:
            raise ValueError('工作区配置不合格：' + '；'.join(p['field'] + ' ' + p['problem'] for p in workspace.problems))
        store = Store(workspace.data)
        with store.transaction() as db:
            db.execute('INSERT INTO workspaces(key,root,name) VALUES (?,?,?) '
                'ON CONFLICT(key) DO UPDATE SET root=excluded.root,name=excluded.name',
                (workspace.name, str(workspace.data), workspace.name))
        service = Service(store, workspace, Environment(workspace))
        Discovery(service).scan()
        if service.workspace_problems:
            raise ValueError('当前场景无法同步：' + '；'.join(p['problem'] for ps in service.workspace_problems.values() for p in ps))
        result = service.submit_regressions(report)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except APIError as error:
        print('登记被拒绝：' + '；'.join(p['field'] + '：' + p['problem'] for p in error.problems), file=sys.stderr)
    except (OSError, ValueError, RecursionError, sqlite3.Error, subprocess.SubprocessError) as error:
        print('登记被拒绝：' + str(error), file=sys.stderr)
    return 1
