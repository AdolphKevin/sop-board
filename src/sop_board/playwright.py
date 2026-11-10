"""随包 Web 验收技能（playwright-acceptance）的命令入口：指引、报告转换与配对标签。"""
import argparse
import hashlib
import json
from pathlib import Path
import runpy
import sys

from .entries import web_evidence, web_skill
from .scenarios import fingerprint


def bundled_skill():
    return Path(__file__).parent / 'skills' / 'playwright-acceptance'


def write_scope(path, project, checkout, scenes, entries, scenario_file):
    """运行目录的 scope.json：绑定场景文件摘要；场景键 → 指纹，以及各场景的录制开关。"""
    scope = dict(project=project, checkout=checkout,
                 scenario_file_sha256=hashlib.sha256(Path(scenario_file).read_bytes()).hexdigest(),
                 scenarios={f"{scene['file']}#{scene['id']}": fingerprint(scene) for scene in scenes},
                 web_evidence={f"{scene['file']}#{scene['id']}": web_evidence(entries[scene['verify']])
                               for scene in scenes if entries.get(scene['verify'], {}).get('surface') == 'web'})
    path.write_text(json.dumps(scope, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return path


def guide():
    return f"""Flow Board Web 场景验收

内置技能：{bundled_skill() / 'SKILL.md'}
当前执行驱动：{web_skill() / 'SKILL.md'}
模板：{bundled_skill() / 'templates'}

浏览器：看板守护进程（sop-board serve）启动 Google Chrome for Testing，每个 检出×身份 一份持久 profile。
执行者只通过 run.env 中的 PW_CDP_ENDPOINT / PW_IDENTITIES 连接，不自己启动浏览器，也不运行 playwright install。
运行时：sop-board browser install 安装固定版本的 Playwright 与 Chrome for Testing 到 ~/.sop-board/runtime。
真实账号：sop-board browser open --identity <身份> --headed 打开有界面的窗口，由人登录；之后的运行沿用 profile。

结果转换：sop-board playwright convert --report <报告.json> --out <运行目录> --scope <scope.json> \\
  --scenario-file scenarios.yaml --executor <执行者>
然后 sop-board run submit 登记；不要另行登记第二份结果。
配对标签：sop-board playwright tags --pairs <pairs.json> --out <cases.json>
细则见内置技能 references/。
"""


def _script(name, arguments):
    argv = sys.argv
    try:
        script = bundled_skill() / 'scripts' / name
        sys.argv = [str(script), *arguments]
        try:
            runpy.run_path(str(script), run_name='__main__')
        except SystemExit as error:
            return error.code if isinstance(error.code, int) else 1
        return 0
    finally:
        sys.argv = argv


def command(arguments):
    if arguments and arguments[0] in ('convert', 'tags'):
        return _script('to_scenarios.py' if arguments[0] == 'convert' else 'case_tags.py', arguments[1:])
    parser = argparse.ArgumentParser(prog='sop-board playwright')
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('guide')
    sub.add_parser('convert', add_help=False, help='把 Playwright JSON 报告转换为 results.jsonl，参数用 convert --help 查看')
    sub.add_parser('tags', add_help=False, help='从配对文件生成标签，参数用 tags --help 查看')
    args = parser.parse_args(arguments)
    if args.action == 'guide':
        print(guide())
    return 0
