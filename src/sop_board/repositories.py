"""授权路径核对与只读 Git 查询；不继承 Git 配置覆盖变量。"""
import os
import json
import re

import yaml
from pathlib import Path
import subprocess


def git(root, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env['GIT_OPTIONAL_LOCKS'] = '0'
    return subprocess.check_output(['git', '-C', str(root), *args], env=env,
                                   stderr=subprocess.DEVNULL, timeout=10)


def authorize(values, token, admin):
    if values and not admin.strip():
        raise ValueError('缺少非空 SOP_BOARD_ADMIN_TOKEN')
    if admin and admin == token:
        raise ValueError('两个令牌相同：开发者令牌必须不同于写入令牌')
    repos = {}
    for value in values:
        identity, separator, raw = value.partition('=')
        if not separator or not identity.strip() or not raw.strip():
            raise ValueError('参数应写成 <项目身份>=<本机仓库路径>')
        path = Path(raw).expanduser().resolve()
        try:
            root = Path(os.fsdecode(git(path, 'rev-parse', '--show-toplevel')).strip()).resolve()
            valid = root == path and git(path, 'rev-parse', '--is-inside-work-tree').strip() == b'true'
        except (OSError, subprocess.SubprocessError):
            valid = False
        if not valid:
            raise ValueError(f'路径不是 Git 工作区根目录：{raw}')
        if identity in repos and repos[identity] != path:
            raise ValueError(f'项目身份重复授权：{identity}')
        repos[identity] = path
    return repos


def product(root, scenario, entry):
    import hashlib
    import json
    names = set(git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').split(b'\0'))
    files = []
    excluded = {scenario.resolve(), entry.resolve()}
    for raw in sorted(names):
        if not raw:
            continue
        name = os.fsdecode(raw)
        path = root / name
        if name.startswith('docs/sop/') or path.resolve() in excluded:
            continue
        try:
            if path.is_symlink():
                data = os.fsencode(os.readlink(path))
            elif not path.exists():
                files.append(dict(path=name, deleted=True))
                continue
            else:
                data = path.read_bytes()
            files.append(dict(path=name, sha256=hashlib.sha256(data).hexdigest()))
        except FileNotFoundError:
            files.append(dict(path=name, deleted=True))
    digest = hashlib.sha256(json.dumps(files, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    try:
        commit = git(root, 'rev-parse', '--verify', 'HEAD').decode().strip()
    except subprocess.CalledProcessError:
        commit = 'unborn'
    return dict(product_digest=digest, commit=commit,
                dirty=bool(git(root, 'status', '--porcelain', '--untracked-files=all')))


def read_entries(root):
    """仅读取入口声明；名称用于场景的 verify 引用核对。"""
    from .errors import APIError
    path = root / 'docs/sop/entries.yaml'
    config = root / '.tw/config.json'
    if config.exists():
        path = config
    if not path.exists():
        return {}, path
    try:
        raw = path.read_text(encoding='utf-8')
        for token in yaml.scan(raw):
            if isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken, yaml.tokens.TagToken)):
                raise ValueError('入口声明不支持锚点、别名或显式标签')
        document = json.loads(raw) if path == config else yaml.safe_load(raw)
        entries = document.get('acceptance_entries' if path == config else 'entries')
        if not isinstance(entries, dict) or not entries:
            raise ValueError('入口声明应含非空 entries 对象')
        required = {'surface', 'operate', 'observe', 'evidence', 'isolation'}
        for name, entry in entries.items():
            if not isinstance(name, str) or not name.strip() or not isinstance(entry, dict):
                raise ValueError('入口名称应为非空字符串，入口内容应为对象')
            if not required <= entry.keys() or (path != config and entry.keys() - required - {'address_env'}):
                raise ValueError(f'入口 {name} 的字段不合格')
            for key in ('surface', 'operate', 'isolation'):
                if not isinstance(entry[key], str) or not entry[key].strip():
                    raise ValueError(f'入口 {name} 的 {key} 应为非空字符串')
            for key in ('observe', 'evidence'):
                if not isinstance(entry[key], list) or not entry[key] or not all(isinstance(v, str) and v.strip() for v in entry[key]):
                    raise ValueError(f'入口 {name} 的 {key} 应为非空字符串数组')
            if 'address_env' in entry and (not isinstance(entry['address_env'], str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', entry['address_env'])):
                raise ValueError(f'入口 {name} 的 address_env 应为环境变量名')
        return entries, path
    except (ValueError, AttributeError, yaml.YAMLError, RecursionError) as error:
        mark = getattr(error, 'problem_mark', None)
        raise APIError(422, 'scenario-file-invalid', [dict(field='entries', line=mark.line + 1 if mark else 1,
            problem=f'{path.relative_to(root)} 入口声明不合格：{error}')]) from error


def entry_names(root):
    entries, path = read_entries(root)
    return set(entries), path


def read_sop_sources(root, raw):
    """按已登记场景来源取仓库内原文，任务建立时冻结，模型只看到副本。"""
    import hashlib
    document = yaml.safe_load(raw) or {}
    sources = {}
    for source in document.get('metadata', {}).get('sources', []):
        for token in source['ref'].split():
            path = root / token
            if Path(token).is_absolute() or '..' in Path(token).parts or root not in path.resolve().parents or not path.is_file():
                continue
            content = path.read_bytes().decode('utf-8')
            sources[token] = dict(path=token, ref=source['ref'], content=content,
                                  sha256=hashlib.sha256(content.encode()).hexdigest(), confirmed_by=source['confirmed_by'])
            break
    return [sources[name] for name in sorted(sources)]


class Discovery:
    def __init__(self, service, repos):
        import threading
        self.service, self.repos = service, repos
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.run, name='workspace-discovery', daemon=True)

    def scan(self):
        with self.service.workspace_lock:
            self._scan()

    def _scan(self):
        from .errors import APIError
        from .scenario_file import load_scenarios
        from .service import fingerprint, project_key
        errors = {}
        for identity, root in self.repos.items():
            key = project_key(identity)
            for path in sorted((root / 'docs/sop').glob('*.yaml')):
                if path.name == 'entries.yaml':
                    continue
                batch = path.stem
                with self.service.store.connection() as db:
                    previous = db.execute('SELECT b.name FROM versions v JOIN batches b ON b.id=v.batch '
                                          'WHERE b.project=? AND v.path=? ORDER BY v.id DESC LIMIT 1',
                                          (key, str(path.relative_to(root)))).fetchone()
                    if previous:
                        batch = previous['name']
                try:
                    raw = path.read_bytes().decode('utf-8')
                    # Read only scalar metadata when the rest of the file is invalid.
                    try:
                        node = yaml.compose(raw, Loader=yaml.SafeLoader)
                        if isinstance(node, yaml.MappingNode):
                            for k, v in node.value:
                                if k.value == 'metadata' and isinstance(v, yaml.MappingNode):
                                    for mk, mv in v.value:
                                        if mk.value == 'batch' and isinstance(mv, yaml.ScalarNode) and mv.tag == 'tag:yaml.org,2002:str' and mv.value.strip():
                                            batch = mv.value
                    except (yaml.YAMLError, RecursionError):
                        pass
                    entries, entry_path = entry_names(root)
                    document = load_scenarios(raw, entries=entries)
                    batch = document['metadata']['batch']
                    # Include all history, including versions produced by the board itself.
                    with self.service.store.connection() as db:
                        found = db.execute('SELECT 1 FROM versions v JOIN batches b ON b.id=v.batch '
                                           'WHERE b.project=? AND b.name=? AND v.content=?', (key, batch, raw)).fetchone()
                    if found:
                        continue
                    info = product(root, path, entry_path)
                    self.service.push(key, batch, dict(project=dict(id=identity, name=root.name),
                        path=str(path.relative_to(root)), content=raw, pushed_by='工作区', **info,
                        fingerprints={s['id']: fingerprint(s) for n in document['sops'] for s in n['scenarios']}), preserve_project_name=True)
                except APIError as error:
                    errors.setdefault((key, batch), []).extend(dict(p, path=str(path.relative_to(root))) for p in error.problems)
                except (OSError, UnicodeError, subprocess.SubprocessError) as error:
                    errors.setdefault((key, batch), []).append(dict(field='content', problem=f'无法读取工作区：{type(error).__name__}', line=1, path=str(path.relative_to(root))))
        if errors:
            with self.service.store.transaction() as db:
                for (key, batch), problems in errors.items():
                    db.execute("INSERT OR IGNORE INTO batches(project,name,path,product_digest) VALUES (?,?,?,'')",
                               (key, batch, problems[0]['path']))
        self.service.workspace_problems = errors

    def run(self):
        while not self.stopping.wait(5):
            self.scan()

    def close(self):
        self.stopping.set()
        self.thread.join()
