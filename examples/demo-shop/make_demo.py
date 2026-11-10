"""生成演示项目：app、lib 两个 Git 仓库（各有 origin 远端地址），可选登记为 sop-board 项目。

    python3 make_demo.py <目标目录>                        # 只生成仓库
    python3 make_demo.py <目标目录> --data <数据根目录>     # 另外 init 为项目 demo，并装好数据目录模板

生成的 lib 带一个预埋缺陷：refundlib.can_apply 多放行一天，签收第 8 天仍可申请退款（打败 SHOP-R1-N01）。
提交只用一次性的作者变量，不改动仓库或全局的 git 配置。
"""
import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ORIGIN = 'https://example.invalid/demo-shop/{}.git'
AUTHOR = dict(GIT_AUTHOR_NAME='demo-shop', GIT_AUTHOR_EMAIL='demo-shop@example.invalid',
              GIT_COMMITTER_NAME='demo-shop', GIT_COMMITTER_EMAIL='demo-shop@example.invalid')


def git(repo, *args):
    subprocess.run(['git', '-C', str(repo), *args], check=True, stdout=subprocess.DEVNULL, env=dict(os.environ, **AUTHOR))


def make_repo(name, target):
    shutil.copytree(HERE / name, target, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.DS_Store'))
    git(target, 'init', '-q', '-b', 'main')
    git(target, 'remote', 'add', 'origin', ORIGIN.format(name))
    git(target, 'add', '-A')
    git(target, 'commit', '-q', '-m', f'演示项目 {name} 初始版本')


def install_templates(data):
    """把 data/ 模板装进项目数据目录：config.yaml 保留 init 写入的 repos 与 identities，其余取模板。"""
    source = HERE / 'data'
    registered = (data / 'config.yaml').read_text(encoding='utf-8')
    body = '\n'.join(line for line in (source / 'config.yaml').read_text(encoding='utf-8').splitlines()
                     if not line.startswith('#'))
    (data / 'config.yaml').write_text(registered.rstrip('\n') + '\n' + body + '\n', encoding='utf-8')
    for name in ('env.py', 'worktree.py', 'seed.py'):
        shutil.copy2(source / name, data / name)
    shutil.copytree(source / 'skills', data / 'skills', dirs_exist_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('target', help='新建的目标目录（须不存在或为空）')
    parser.add_argument('--data', help='同时 init：sop-board 数据根目录（项目目录为 <根目录>/demo）')
    parser.add_argument('--sop-board', default='sop-board', help='调用 sop-board 的命令（缺省为 PATH 上的 sop-board）')
    args = parser.parse_args()
    target = Path(args.target).expanduser().resolve()
    if target.exists() and any(target.iterdir()):
        sys.exit(f'目标目录不为空：{target}')
    target.mkdir(parents=True, exist_ok=True)
    for name in ('app', 'lib'):
        make_repo(name, target / name)
    print(f'app：{target / "app"}', flush=True)
    print(f'lib：{target / "lib"}', flush=True)
    if args.data:
        command = shlex.split(args.sop_board) + ['init', 'demo', str(target / 'app'), str(target / 'lib'), '--data', args.data]
        subprocess.run(command, check=True)
        data = Path(args.data).expanduser().resolve() / 'demo'
        install_templates(data)
        print(f'项目 demo：{data}')
        print(f'下一步：在终端运行 sop-board serve demo --data {args.data} --lan，再运行 sop-board check --project demo --data {args.data}')


if __name__ == '__main__':
    main()
