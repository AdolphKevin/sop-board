"""Git 调用、按内容计算的产品快照与检出识别所需的仓库信息。

快照只依据内容：已跟踪且未改动的文件取索引中的 blob 编号，改动或未跟踪的文件按 Git 的
blob 算法现算，因此同一内容无论是否提交都得到同一摘要，也不必读取全部文件。
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess


def git(root, *args, check=True, timeout=60):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env['GIT_OPTIONAL_LOCKS'] = '0'
    result = subprocess.run(['git', '-C', str(root), *args], env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if check and result.returncode:
        raise subprocess.CalledProcessError(result.returncode, ['git', *args], result.stdout, result.stderr)
    return result.stdout


def text(root, *args):
    return os.fsdecode(git(root, *args)).strip()


def toplevel(path):
    """返回 (工作树根目录, git common dir)；不在 Git 工作树中时返回 None。"""
    try:
        lines = os.fsdecode(git(path, 'rev-parse', '--show-toplevel', '--git-common-dir')).splitlines()
    except (OSError, subprocess.SubprocessError):
        return None
    if len(lines) != 2:
        return None
    top = Path(lines[0]).resolve()
    common = Path(lines[1])
    if not common.is_absolute():
        common = (Path(path) / common)
    return top, common.resolve()


def common_dir(root):
    found = toplevel(root)
    return found[1] if found else None


def head(root):
    try:
        return text(root, 'rev-parse', '--verify', '--quiet', 'HEAD') or None
    except subprocess.CalledProcessError:
        return None


def branch(root):
    try:
        return text(root, 'symbolic-ref', '--quiet', '--short', 'HEAD') or None
    except subprocess.CalledProcessError:
        return None


def blob_id(raw):
    return hashlib.sha1(b'blob %d\0' % len(raw) + raw).hexdigest()


def excluded(name, prefixes):
    path = Path(name)
    return any(path == prefix or path.is_relative_to(prefix) for prefix in prefixes)


def _records(raw):
    return [record for record in raw.split(b'\0') if record]


def head_manifest(root, exclusions=()):
    """HEAD 中的 路径 → 模式:对象编号，不读取文件内容。"""
    if head(root) is None:
        return {}
    result = {}
    for record in _records(git(root, 'ls-tree', '-rz', '--full-tree', 'HEAD')):
        info, raw = record.split(b'\t', 1)
        name = os.fsdecode(raw)
        if excluded(name, exclusions):
            continue
        mode, _, object_id = info.split()
        result[name] = mode.decode() + ':' + object_id.decode()
    return result


def manifest(root, exclusions=()):
    """工作区当前内容的 路径 → 模式:对象编号（含未跟踪、未忽略的文件）。"""
    root = Path(root)
    index = {}
    for record in _records(git(root, 'ls-files', '-s', '-z')):
        info, raw = record.split(b'\t', 1)
        mode, object_id, stage = info.split()
        if stage == b'0':
            index[os.fsdecode(raw)] = (mode.decode(), object_id.decode())
    dirty = set()
    entries = _records(git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=all', '--ignore-submodules=none'))
    position = 0
    while position < len(entries):
        entry = entries[position]
        code, name = entry[:2], os.fsdecode(entry[3:])
        dirty.add(name)
        if code[:1] in (b'R', b'C'):
            # 重命名与复制的下一项是原路径。
            position += 1
            dirty.add(os.fsdecode(entries[position]))
        position += 1
    result = {}
    for name in sorted(set(index) | dirty):
        if excluded(name, exclusions):
            continue
        path = root / name
        if name in index and name not in dirty:
            mode, object_id = index[name]
            result[name] = mode + ':' + object_id
            continue
        if path.is_symlink():
            result[name] = '120000:' + blob_id(os.fsencode(os.readlink(path)))
        elif path.is_dir():
            # 子模块或嵌套仓库：记录其检出的提交。
            commit = head(path)
            if commit:
                result[name] = '160000:' + commit
            elif name in index:
                result[name] = ':'.join(index[name])
        elif path.is_file():
            try:
                raw = path.read_bytes()
            except FileNotFoundError:
                continue
            mode = '100755' if path.stat().st_mode & 0o111 else '100644'
            result[name] = mode + ':' + blob_id(raw)
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def changed_paths(before, after):
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


def snapshot(paths, exclusions=None, *, files=False):
    """paths：别名 → 仓库根目录；exclusions：别名 → 仓内相对路径列表。

    返回各仓提交、内容摘要与相对 HEAD 的改动路径；files=True 时附带逐文件清单，供运行前后比较。"""
    exclusions = exclusions or {}
    repos, changed, manifests = {}, [], {}
    for alias in sorted(paths):
        root = Path(paths[alias])
        prefixes = [Path(p) for p in exclusions.get(alias, [])]
        current = manifest(root, prefixes)
        committed = head_manifest(root, prefixes)
        dirty = changed_paths(committed, current)
        repos[alias] = dict(commit=head(root), digest=digest(current), dirty=len(dirty))
        changed += [alias + '/' + path for path in dirty]
        manifests[alias] = current
    result = dict(repos=repos, product_digest=digest({alias: info['digest'] for alias, info in repos.items()}),
                  dirty=bool(changed), changed=changed)
    if files:
        result['files'] = manifests
    return result


def uncommitted(paths):
    """各仓库工作区的未提交改动数（git status，含未跟踪、未忽略的文件）：别名 → 数量，只列有改动的仓库。"""
    result = {}
    for alias in sorted(paths):
        try:
            entries = _records(git(paths[alias], 'status', '--porcelain=v1', '-z', '--untracked-files=all'))
        except (OSError, subprocess.SubprocessError):
            continue
        count, position = 0, 0
        while position < len(entries):
            # 重命名与复制的下一项是原路径，不另计。
            position += 2 if entries[position][:1] in (b'R', b'C') else 1
            count += 1
        if count:
            result[alias] = count
    return result


def changed_between(before, after):
    """比较两份 snapshot(files=True) 的逐文件清单，返回 别名/路径 列表。"""
    result = []
    for alias in sorted(set(before) | set(after)):
        result += [alias + '/' + path for path in changed_paths(before.get(alias, {}), after.get(alias, {}))]
    return result


def diff_names(root, base, *, exclusions=()):
    """base 提交到当前工作区的改动路径（含未提交与未跟踪）。"""
    names = set()
    for record in _records(git(root, 'diff', '--name-only', '-z', base, '--')):
        names.add(os.fsdecode(record))
    for record in _records(git(root, 'ls-files', '-z', '--others', '--exclude-standard')):
        names.add(os.fsdecode(record))
    return sorted(name for name in names if not excluded(name, exclusions))


def user_name(root=None):
    try:
        return text(root or Path.home(), 'config', 'user.name') or None
    except (OSError, subprocess.SubprocessError):
        return None
