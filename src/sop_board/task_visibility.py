"""任务及共享附件的声明式内网投影；不把代理材料作为业务字段公开。"""
from dataclasses import dataclass
from pathlib import Path
import re

from .kernel import S


def fields(*names):
    return {name: True for name in names}


PROGRESS = fields('id', 'status', 'started_at', 'finished_at', 'timeout', 'elapsed', 'ahead',
                  'max_running', 'last_activity', 'error', 'ownership', 'gap_count', 'held_count', 'auto_execute')
COMMON = fields('id', 'kind', 'workspace', 'batch', 'status', 'trigger', 'created_at', 'started_at',
                'finished_at', 'attempt', 'error', 'void_reason', 'ownership') | {'progress': PROGRESS}
SCOPE = [fields('batch', 'scenario', 'reason')]
GAPS = [fields('id', 'kind', 'title', 'scenario', 'current', 'missing', 'question', 'known') |
        {'options': [True], 'cases': [True]}]
HELD = [fields('case', 'retention') | {'reasons': [True], 'gaps': [True], 'scenarios': [True]}]
MERGE_RESULT = fields('ok', 'workspace_changed', 'reply', 'session_note') | {
    'responses': [fields('message', 'action', 'note')],
    'pairs': [fields('case', 'kind', 'kind_reason', 'note') | {'scenarios': [True]}],
    'scenarios': [fields('id', 'action', 'target', 'reason')],
    'inferences': [fields('id', 'field', 'content')], 'gaps': GAPS, 'held': HELD,
}
FIX_RESULT = fields('root_cause', 'plan')
FIX_PROBLEM = fields('batch', 'scenario', 'category', 'root_repo', 'handling', 'reason', 'plan', 'defect_url')
FIX_REPO = fields('alias', 'identity', 'baseline_branch', 'repair_commit', 'merged_commit', 'merge_status') | {
    'changed_files': [True]}
FIX_ATTEMPT = fields('number', 'started_at', 'finished_at', 'outcome') | {
    'result': FIX_RESULT, 'validation': fields('status') | {'scenarios': [fields('batch', 'scenario')]},
    'commits': {'*': True}, 'merged_commits': {'*': True}}

GENERATE_OUTPUT = {
    'rules': [fields('id','section','excerpt')],
    'cases': [fields('id','title','priority','type','description','auto','basis','basis_note') |
              {key:[True] for key in ('preconditions','steps','expected_results','tags','rules')}],
    'coverage': [fields('rule','negative_exempt') | {'positive':[True], 'negative':[True]}],
    'questions': [fields('id','text','rule')], 'assumptions': [fields('text') | {'cases':[True]}],
    'obsolete': [fields('case','reason')], 'responses': [fields('annotation','action','note')], 'notes':[True],
}
GENERATE_VERSION = fields('id','draft','number','task','sop_version','created_at') | {'output': GENERATE_OUTPUT}


def select(value, schema):
    if value is None:
        return None
    if schema is True:
        return value if value is None or isinstance(value, (str, int, float, bool)) else None
    if isinstance(schema, list):
        if not isinstance(value, list):
            return []
        return [select(item, schema[0]) if isinstance(item, dict) or schema[0] is True else item
                for item in value if isinstance(item, dict) or schema[0] is True and isinstance(item, (str, int, float, bool))
                or schema is GAPS and isinstance(item, str)]
    if not isinstance(value, dict):
        return {}
    if '*' in schema:
        return {key: select(item, schema['*']) for key, item in value.items()
                if isinstance(key, str) and re.fullmatch('[a-z][a-z0-9-]*', key)}
    return {key: select(value[key], child) for key, child in schema.items() if key in value}


@dataclass(frozen=True)
class Visibility:
    public: dict
    attachments: frozenset = frozenset()
    directories: frozenset = frozenset()

    def draft_versions(self, versions, *, local):
        if local:
            return versions
        return [dict(select(version, GENERATE_VERSION), revision_note=(version.get('revision_note')
                if version.get('revision_note') in ('上传 SOP', '按反馈修订') else '草稿版本已登记')) for version in versions]

    def shared(self, relative):
        parts = Path(relative).parts
        return (len(parts) == 1 and parts[0] in self.attachments or
                len(parts) > 1 and parts[0] in self.directories)

    def project(self, task):
        if task['kind'] == 'fix':
            task = dict(task, problems=[dict(problem, defect_url=problem.get('input', {}).get('defect_url', ''))
                for problem in task.get('problems', [])])
        result = select(task, self.public)
        # 原始运行错误可能含命令、会话或路径；内网只说明可采取的业务动作。
        error = task.get('error', '')
        if not error and task['status'] == S.NEEDS_SYNC:
            error = '任务输入已变化，本轮结果未写入；看板未自动发起新合并时，请开发者在本机核对后重新合并'
        safe = bool(re.fullmatch(r'等待(?:合并|草稿|执行|修复|沉淀|编写 SOP|梳理交互)任务 #[0-9]+ 结束', error))
        if error and not safe and error not in ('等待 Codex 名额', '等待浏览器', '等待验收浏览器', '等待工作区自检通过'):
            error = {S.QUEUED: '任务已排队，等待开始', S.RUNNING: '任务正在运行',
                     S.ANALYZING: '正在分析问题原因', S.FIXING: '正在修复问题',
                     S.VERIFYING: '正在独立复验修复结果', S.PROMOTING: '复验已通过，正在核对并合入修复',
                     S.DISTILLING: '正在整理回归验证', S.SESSION: '正在由会话处理',
                     S.COMPLETED: '任务已完成', S.MERGED: '修复已完成并合入', S.HANDLED: '任务已处理',
                     S.ABANDONED: '任务已放弃', S.VOID: '任务输入已变化，本次结果作废',
                     S.NEEDS_INPUT: '仍有信息待补充，请查看当前问题',
                     S.WAIT_BROWSER: '等待浏览器',
                     S.WAIT_COMMIT: '等待开发者提交产品改动', S.WAIT_VALIDATION: '等待当前产品验证通过',
                     S.NEEDS_SYNC: '任务输入已变化，本轮结果未写入；看板未自动发起新合并时，请开发者在本机核对后重新合并',
                     S.WAIT_MERGE: '等待合并结束', S.INTERRUPTED: '任务已中断，可由开发者在本机重试'}.get(
                         task['status'], '请开发者在本机查看任务原因')
        if 'error' in result:
            result['error'] = error
        if 'void_reason' in result:
            result['void_reason'] = '任务输入已变化，请重新发起' if task.get('void_reason') else ''
        result['ownership'] = '由会话处理' if task.get('session_task') or task.get('ownership') else ''
        if 'progress' in result:
            result['progress'].update(error=error, ownership=result['ownership'])
        return result


MERGE = Visibility(COMMON | {'result': MERGE_RESULT, 'gaps': GAPS, 'reused_count': True},
    frozenset(('cases.yaml', 'case_choices.json', 'case_confirmations.json', 'scenarios.yaml',
               'scenario-file.yaml', 'nodes.json', 'pairs.json')), frozenset(('sop',)))
GENERATE = Visibility(COMMON | {'result': fields('ok', 'version'), 'gaps': GAPS, 'reused_count': True},
    frozenset(('sop.md', 'original-sop.md', 'sop-before.md', 'sop.diff', 'draft.json', 'annotations.json',
               'all-annotations.json', 'cases.yaml', 'cases-generated.yaml', 'scenarios.yaml')))
AUTHOR_SOP = Visibility(COMMON | {'result': fields('ok', 'version', 'sha256', 'confirmed_by')})
EXECUTE = Visibility(COMMON | {'scope': SCOPE, 'rounds': [True], 'reused_count': True, 'rerun_count': True,
                               'result': fields('ok', 'reused_count', 'rerun_count') | {'product_changed': [True], 'voided_scenarios': [True], 'not_executed': [True]}},
                    frozenset(('scenarios.yaml', 'cases.json')))
DISTILL = Visibility(COMMON | {'scope': SCOPE, 'validation_scenarios': [True]})
FIX = Visibility(COMMON | fields('repair_origin') | {'scope': SCOPE, 'result': FIX_RESULT, 'problems': [FIX_PROBLEM],
    'repositories': [FIX_REPO], 'attempts': [FIX_ATTEMPT], 'attention': [FIX_PROBLEM],
    'jira': [fields('issue', 'comment', 'transition')], 'rounds': [fields('batch', 'round')]})

POLICIES = dict(merge=MERGE, generate=GENERATE, execute=EXECUTE, fix=FIX, distill=DISTILL)
POLICIES['author-sop'] = AUTHOR_SOP
ATLAS = Visibility(COMMON | {'result': fields('ok', 'entry', 'version', 'pages', 'controls', 'feedback_handled')})
POLICIES['atlas'] = ATLAS


def project(task, *, local):
    return task if local else POLICIES[task['kind']].project(task)
