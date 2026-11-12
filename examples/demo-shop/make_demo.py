"""生成演示项目：app、lib 两个 Git 仓库（各有 origin 远端地址），可选登记为 sop-board 项目。

    python3 make_demo.py <目标目录>                        # 只生成仓库
    python3 make_demo.py <目标目录> --data <数据根目录>     # 另外 init 为项目 demo，并装好数据目录模板

库存演示项目（--with-stock）另生成第三个仓库 stock（库存服务，商城下单依赖它），app 多出场景文件 stock、
SOP stock.md 与守护命令 api，配置声明 shop、stock 两个服务（adapter.services）：

    python3 make_demo.py <目标目录> --with-stock --data <数据根目录>                         # 登记 app、lib、stock
    python3 make_demo.py <目标目录> --with-stock --reuse --shop-only --data <另一个数据根目录>  # 同一份仓库只登记 app、lib（只搭商城）

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
STOCK = HERE / 'with-stock'
ORIGIN = 'https://example.invalid/demo-shop/{}.git'
AUTHOR = dict(GIT_AUTHOR_NAME='demo-shop', GIT_AUTHOR_EMAIL='demo-shop@example.invalid',
              GIT_COMMITTER_NAME='demo-shop', GIT_COMMITTER_EMAIL='demo-shop@example.invalid')
IGNORE = shutil.ignore_patterns('__pycache__', '*.pyc', '.DS_Store')


def git(repo, *args):
    subprocess.run(['git', '-C', str(repo), *args], check=True, stdout=subprocess.DEVNULL, env=dict(os.environ, **AUTHOR))


def make_repo(name, target, with_stock=False):
    shutil.copytree(HERE / name, target, ignore=IGNORE)
    if name == 'app' and with_stock:
        # 库存演示项目：场景文件 stock、SOP stock.md、带 api 守护命令的 regression.yaml，手册追加下单与库存一节。
        shutil.copytree(STOCK / 'app', target, ignore=IGNORE, dirs_exist_ok=True)
        manual = target / 'docs' / 'operations' / 'verification.md'
        manual.write_text(manual.read_text(encoding='utf-8') + (STOCK / 'verification-stock.md').read_text(encoding='utf-8'),
                          encoding='utf-8')
    git(target, 'init', '-q', '-b', 'main')
    git(target, 'remote', 'add', 'origin', ORIGIN.format(name))
    git(target, 'add', '-A')
    git(target, 'commit', '-q', '-m', f'演示项目 {name} 初始版本')


def install_templates(data, with_stock=False):
    """把 data/ 模板装进项目数据目录：config.yaml 保留 init 写入的 repos 与 identities，其余取模板。"""
    source = HERE / 'data'
    registered = (data / 'config.yaml').read_text(encoding='utf-8')
    template = (STOCK if with_stock else source) / 'config.yaml'
    body = '\n'.join(line for line in template.read_text(encoding='utf-8').splitlines() if not line.startswith('#'))
    (data / 'config.yaml').write_text(registered.rstrip('\n') + '\n' + body + '\n', encoding='utf-8')
    for name in ('env.py', 'worktree.py', 'seed.py'):
        shutil.copy2(source / name, data / name)
    shutil.copytree(source / 'skills', data / 'skills', dirs_exist_ok=True)
    if with_stock:
        skill = data / 'skills' / 'demo-verification' / 'SKILL.md'
        skill.write_text(skill.read_text(encoding='utf-8') + (STOCK / 'skill-stock.md').read_text(encoding='utf-8'), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('target', help='新建的目标目录（须不存在或为空；--reuse 时为已生成的目录）')
    parser.add_argument('--data', help='同时 init：sop-board 数据根目录（项目目录为 <根目录>/demo）')
    parser.add_argument('--sop-board', default='sop-board', help='调用 sop-board 的命令（缺省为 PATH 上的 sop-board）')
    parser.add_argument('--with-stock', action='store_true', help='库存演示项目：另生成 stock 仓库，配置声明 shop、stock 两个服务')
    parser.add_argument('--shop-only', action='store_true', help='与 --with-stock、--data 同用：只登记 app、lib（只搭商城），stock 为本机未搭建')
    parser.add_argument('--reuse', action='store_true', help='目标目录已由本脚本生成：不再生成仓库，只登记并装模板')
    args = parser.parse_args()
    if args.shop_only and not (args.with_stock and args.data):
        sys.exit('--shop-only 须与 --with-stock、--data 同用')
    target = Path(args.target).expanduser().resolve()
    names = ['app', 'lib'] + (['stock'] if args.with_stock else [])
    if args.reuse:
        missing = [name for name in names if not (target / name / '.git').exists()]
        if missing:
            sys.exit(f'--reuse 须指向已生成的目录，缺少仓库：{"、".join(missing)}')
        if args.with_stock and not (target / 'app' / 'docs' / 'sop' / 'stock.yaml').is_file():
            sys.exit('--reuse --with-stock 须指向用 --with-stock 生成的目录')
    else:
        if target.exists() and any(target.iterdir()):
            sys.exit(f'目标目录不为空：{target}')
        target.mkdir(parents=True, exist_ok=True)
        for name in names:
            make_repo(name, target / name, args.with_stock)
    for name in names:
        print(f'{name}：{target / name}', flush=True)
    if args.data:
        registered = ['app', 'lib'] if args.shop_only else names
        command = shlex.split(args.sop_board) + ['init', 'demo', *(str(target / name) for name in registered), '--data', args.data]
        subprocess.run(command, check=True)
        data = Path(args.data).expanduser().resolve() / 'demo'
        install_templates(data, args.with_stock)
        print(f'项目 demo：{data}（登记 {"、".join(registered)}）')
        print(f'下一步：在终端运行 sop-board serve demo --data {args.data} --lan，再运行 sop-board check --project demo --data {args.data}')


if __name__ == '__main__':
    main()
