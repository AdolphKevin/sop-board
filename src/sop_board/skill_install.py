"""把随包技能链接到客户端目录；仅更新本安装器拥有的链接。"""
import argparse
from importlib.metadata import version
import json
import os
from pathlib import Path
import sys
from uuid import uuid4


SKILLS = ('sop-board', 'regression-distill', 'playwright-acceptance')
CLIENTS = (('Codex', '.agents'), ('Claude Code', '.claude'))


def install():
    package = Path(__file__).resolve().parent / 'skills'
    release = version('sop-board')
    for client, folder in CLIENTS:
        directory = Path.home() / folder / 'skills'
        directory.mkdir(parents=True, exist_ok=True)
        registry = directory / '.sop-board-links.json'
        owned = json.loads(registry.read_text(encoding='utf-8')) if registry.exists() else {}
        if not isinstance(owned, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in owned.items()):
            raise ValueError('技能链接记录不合格：' + str(registry))
        for name in SKILLS:
            source, target = package / name, directory / name
            if not (source / 'SKILL.md').is_file():
                raise ValueError('随包技能缺失：' + str(source))
            try:
                resolved = target.resolve()
            except (OSError, RuntimeError):
                resolved = None  # 其他来源的循环链接也保留，不能阻止其余技能安装。
            ours = target.is_symlink() and (
                resolved == source.resolve() or os.readlink(target) == owned.get(name))
            if (target.exists() or target.is_symlink()) and not ours:
                origin = str(resolved) if resolved is not None else (os.readlink(target) if target.is_symlink() else str(target)) + '（无法解析）'
                print(f'{client} · {name}：跳过其他来源 · {target} → {origin} · 版本未知（非本包）')
                continue
            action = '已安装'
            if ours and resolved == source.resolve():
                action = '已是当前版本'
            else:
                temporary = directory / ('.sop-board-link-' + uuid4().hex)
                try:
                    temporary.symlink_to(source, target_is_directory=True)
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
                if ours:
                    action = '已更新'
            owned[name] = os.readlink(target)
            temporary = directory / ('.sop-board-registry-' + uuid4().hex)
            try:
                temporary.write_text(json.dumps(owned, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                temporary.replace(registry)
            finally:
                temporary.unlink(missing_ok=True)
            print(f'{client} · {name}：{action} · {target} → {target.resolve()} · sop-board {release}')


def command(arguments):
    parser = argparse.ArgumentParser(prog='sop-board skills', description='安装随包技能到当前 HOME 下的客户端目录')
    parser.add_subparsers(dest='action', required=True).add_parser('install', help='安装或更新本包拥有的技能链接')
    parser.parse_args(arguments)
    try:
        install()
        return 0
    except (OSError, ValueError) as error:
        print('无法安装技能：' + str(error), file=sys.stderr)
        return 1
