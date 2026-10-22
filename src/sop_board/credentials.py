"""入口凭据只保留进程内取值；任务材料使用变量名称。"""
from datetime import datetime, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import re
import subprocess
import threading
from zipfile import ZIP_DEFLATED, ZipFile


ENV_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')


def names(entries):
    return sorted({name for entry in entries.values() if isinstance(entry, dict)
                   for name in (entry.get('credential_env') if isinstance(entry.get('credential_env'), list) else [])
                   if isinstance(name, str) and ENV_NAME.fullmatch(name)})


def declared_in(value):
    """收集历史任务声明，用于配置更新或重启后的内网遮蔽。"""
    found = set()
    if isinstance(value, dict):
        if isinstance(value.get('credential_env'), list):
            found.update(names({'entry': value}))
        for child in value.values():
            found.update(declared_in(child))
    elif isinstance(value, list):
        for child in value:
            found.update(declared_in(child))
    return found


def statuses(entries):
    return [dict(entry=entry, name=name, status='已设置' if os.environ.get(name) else '未设置')
            for entry, value in entries.items() for name in names({entry: value})]


def blocked_results(scenes, entries, environment):
    rows = []
    for scene in scenes:
        missing = [name for name in names({scene['verify']: entries[scene['verify']]}) if not environment.get(name)]
        if missing:
            finding = '入口 ' + scene['verify'] + ' 缺少凭据环境变量：' + '、'.join(missing)
            row = dict(scenario=scene['id'], status='blocked', actual=finding, finding=finding,
                       executed_at=datetime.now(timezone.utc).isoformat(), executor='Flow Board 入口检查', evidence=[])
            if 'batch' in scene:
                row['batch'] = scene['batch']
            rows.append(row)
    return rows


def append_blocked(directory, rows):
    if not rows:
        return
    path = directory / 'results.jsonl'
    if path.is_symlink():
        raise ValueError('results.jsonl 不能是符号链接')
    previous = path.read_text(encoding='utf-8').rstrip() + '\n' if path.exists() else ''
    path.write_text(previous + ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')


class AgentEnvironment(dict):
    """附带日志脱敏信息，不把它编码为环境变量或任务配置。"""
    def __init__(self, values, credential_names):
        super().__init__(values)
        self.credential_names = tuple(credential_names)
        self.secrets = tuple(values[name] for name in credential_names if values.get(name))


def shell_options(environment):
    # Codex 的命令环境还会再次过滤继承变量；这里只继承看板已筛选的进程环境。
    # 用户显式配置的过滤规则继续生效，取值始终不放进命令参数。
    if getattr(environment, 'credential_names', ()):
        return ['-c', 'shell_environment_policy.inherit="all"',
                '-c', 'shell_environment_policy.ignore_default_excludes=true']
    return []


def patterns(secrets):
    variants = set()
    for value in secrets:
        if value:
            escaped = {value}
            for _ in range(2):
                escaped |= {json.dumps(item, ensure_ascii=ascii_only)[1:-1] for item in escaped for ascii_only in (True, False)}
            variants.update(item.encode() for item in escaped)
    return sorted(variants, key=len, reverse=True)


def redact(raw, secrets):
    """遮蔽原值及 JSON 转义形式；凭据不进入看板收集的输出。"""
    for pattern in patterns(secrets):
        raw = raw.replace(pattern, b'[credential]')
    return raw


class StreamRedactor:
    """保留可能跨读块的凭据前缀，完整取值在写入日志前替换。"""
    def __init__(self, secrets):
        self.values = patterns(secrets)
        self.expression = re.compile(b'|'.join(re.escape(v) for v in self.values)) if self.values else None
        self.pending = b''

    def feed(self, raw, *, final=False):
        if self.expression is None:
            return raw
        self.pending += raw
        output = []
        while (match := self.expression.search(self.pending)) is not None:
            output.append(self.pending[:match.start()] + b'[credential]')
            self.pending = self.pending[match.end():]
        keep = 0
        if not final:
            for value in self.values:
                position = self.pending.find(value[:1], max(0, len(self.pending) - len(value) + 1))
                while position != -1:
                    if value.startswith(self.pending[position:]):
                        keep = max(keep, len(self.pending) - position)
                        break
                    position = self.pending.find(value[:1], position + 1)
        count = len(self.pending) - keep
        output.append(self.pending[:count])
        self.pending = self.pending[count:]
        return b''.join(output)


def name_pattern(name):
    """已声明变量名的常见编码：每个字符可为原文或任意层转义的 \\u 写法（十六进制大小写均可）。"""
    parts = []
    for c in name:
        code = f'{ord(c):04x}'.encode()
        parts.append(rb'(?:' + re.escape(c.encode()) + rb'|\\+u' + code + rb')')
    return re.compile(b''.join(parts), re.IGNORECASE)


def hide_names(raw, credential_names):
    # 变量名出现即遮蔽，不依赖前后字符（转义的中文、终端控制符之后同样遮蔽）。
    for name in sorted(credential_names, key=len, reverse=True):
        raw = name_pattern(name).sub(b'[credential variable]', raw)
    return raw


# 本机路径的常见编码：任意层数的反斜杠转义斜杠、URL 编码的斜杠，大小写不敏感（macOS 默认不区分大小写）。
SLASH = rb'(?:\\*/|%2[fF])'
PATH_TAIL = rb'(?:[A-Za-z0-9._~+@-]|' + SLASH + rb'|%(?![0-9a-fA-F]{2}))*'
COMMON_ROOTS = ('/Users/', '/home/', '/private/', '/tmp/', '/var/folders/', '/Volumes/')


def path_pattern(root):
    parts = [SLASH if c == '/' else re.escape(c.encode()) for c in root.rstrip('/')]
    return b''.join(parts) + PATH_TAIL


LOCAL_PATH = re.compile(b'|'.join(rb'(?:' + path_pattern(r) + rb')' for r in COMMON_ROOTS), re.IGNORECASE)


def hide_local_paths(raw, local_roots=()):
    """内网响应遮蔽已知本机目录（看板数据目录、仓库目录、用户目录、临时目录）与常见用户及临时路径，连同其后的路径整段替换。"""
    import tempfile
    placeholder = '<本机路径>'.encode()
    roots = {str(r) for r in (*local_roots, Path.home(), tempfile.gettempdir(), Path(tempfile.gettempdir()).resolve()) if r}
    for root in sorted(roots, key=len, reverse=True):
        raw = re.sub(path_pattern(root), placeholder, raw, flags=re.IGNORECASE)
    return LOCAL_PATH.sub(placeholder, raw)


def response_bytes(raw, content_type, credential_names, *, local, local_roots=()):
    secrets = [os.environ[name] for name in credential_names if os.environ.get(name)]
    def clean(value, text=True):
        value = redact(value, secrets)
        if local:
            return value
        if not text:
            return value
        return hide_local_paths(hide_names(value, credential_names), local_roots)
    if content_type == 'application/zip' and (credential_names or not local):
        output = BytesIO()
        with ZipFile(BytesIO(raw)) as source, ZipFile(output, 'w', ZIP_DEFLATED) as target:
            for entry in source.infolist():
                from .evidence_types import content_type as guess, is_text
                text = is_text(guess(entry.filename))
                target.writestr(clean(entry.filename.encode()).decode(), clean(source.read(entry.filename), text))
            target.comment = clean(source.comment)
        return output.getvalue()
    from .evidence_types import is_text
    return clean(raw, is_text(content_type or ''))


def clean_task_output(directory, secrets):
    """只处理任务目录的文本材料，不进入工作树或符号链接。"""
    extensions = {'.json', '.jsonl', '.txt', '.log', '.md', '.yaml', '.yml', '.html', '.xml', '.csv', '.py', '.js', '.mjs', '.sh'}
    for root, folders, files in os.walk(directory, followlinks=False):
        current = Path(root)
        if (current / '.git').exists():
            folders.clear()
            continue
        folders[:] = [name for name in folders if name not in ('repos', 'worktrees') and not (current / name).is_symlink()]
        for name in files:
            path = current / name
            if path.is_symlink() or not path.is_file() or path.suffix not in extensions:
                continue
            raw = path.read_bytes()
            safe = redact(raw, secrets)
            if safe != raw:
                path.write_bytes(safe)


class RedactedProcess:
    """管道先脱敏再落日志；最终答复从同一 JSON 事件中取得。"""
    def __init__(self, process, directory, secrets):
        self.process, self.directory, self.secrets = process, directory, secrets
        self.final = None
        self.errors = []
        self.finished = False
        self.lock = threading.Lock()
        self.readers = []
        for pipe, name in ((process.stdout, 'events.jsonl'), (process.stderr, 'stderr.txt')):
            reader = threading.Thread(target=self._read, args=(pipe, name), daemon=True)
            reader.start()
            self.readers.append(reader)

    def __getattr__(self, name):
        return getattr(self.process, name)

    def _read(self, pipe, name):
        redactor = StreamRedactor(self.secrets)
        lines = b''
        try:
            with (self.directory / name).open('wb') as output:
                def emit(raw):
                    nonlocal lines
                    output.write(raw)
                    output.flush()
                    if name == 'events.jsonl':
                        lines += raw
                        while b'\n' in lines:
                            line, lines = lines.split(b'\n', 1)
                            self._event(line)
                while True:
                    chunk = pipe.read1(65536)
                    emit(redactor.feed(chunk, final=not chunk))
                    if not chunk:
                        if lines:
                            self._event(lines)
                        break
        except (OSError, ValueError) as error:
            self.errors.append(type(error).__name__)
        finally:
            pipe.close()

    def _event(self, line):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            return
        item = event.get('item') if isinstance(event, dict) else None
        if isinstance(event, dict) and event.get('type') == 'item.completed' and isinstance(item, dict) and item.get('type') == 'agent_message' and isinstance(item.get('text'), str):
            self.final = item['text']

    def _finish(self):
        with self.lock:
            if self.finished:
                return
            for reader in self.readers:
                reader.join(timeout=5)
                if reader.is_alive():
                    raise OSError('会话输出未关闭，无法完成日志收集')
            if self.errors:
                raise OSError('会话日志无法安全保存：' + '、'.join(self.errors))
            if self.final is not None:
                (self.directory / 'final.json').write_text(self.final, encoding='utf-8')
            clean_task_output(self.directory, self.secrets)
            self.finished = True

    def wait(self, timeout=None):
        result = self.process.wait(timeout=timeout)
        self._finish()
        return result

    def poll(self):
        result = self.process.poll()
        if result is not None:
            self._finish()
        return result


def launch(argv, directory, environment, work_directory):
    secrets = getattr(environment, 'secrets', ())
    if secrets:
        # -o 会让子进程直接写未经脱敏的最终答复；改从 stdout 的最终消息落盘。
        position = argv.index('-o')
        argv = argv[:position] + argv[position + 2:]
        process = subprocess.Popen(argv, cwd=work_directory, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        return RedactedProcess(process, directory, secrets)
    with (directory / 'events.jsonl').open('wb') as events, (directory / 'stderr.txt').open('wb') as stderr:
        return subprocess.Popen(argv, cwd=work_directory, env=environment, stdin=subprocess.DEVNULL,
                                stdout=events, stderr=stderr, start_new_session=True)
