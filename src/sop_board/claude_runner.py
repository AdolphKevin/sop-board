"""Claude Code 运行器（D92）。

调用 `claude -p --output-format stream-json`，把事件转换成看板已认识的事件格式写到标准输出：
会话开始、执行命令、读写文件、推理摘要、错误。结构化结果（或最终答复）作为最后一条 agent_message 输出，
并在给出 `-o` 时写入该文件。遇到容量满载、限流等临时错误时，等待后续接同一会话，最多重试 `--retries` 次。

用法：python -m sop_board.claude_runner --directory <任务目录> [--schema <结构>] [-o <输出>] [--model <模型>]
      [--effort <强度>] [--session <会话> [--resume]] [--add-dir <目录> …] [--work-directory <目录>] <提示词> -- <claude 其他参数…>
"""
import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

TRANSIENT = ('overloaded', 'capacity', 'rate limit', 'rate_limit', 'too many requests', 'temporarily unavailable', 'try again later')
RESUME_PROMPT = '上一次运行因临时错误中断。请从中断处继续完成原任务，按原要求写出全部结果，已完成的部分不要重复执行。'


def emit(event):
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + '\n')
    sys.stdout.flush()


PENDING = {}


def translate(event):
    """Claude 事件 → 看板事件；只转换用于“最近活动”的部分。工具调用被权限拒绝时另记一条错误，避免看起来已执行。"""
    if event.get('type') == 'user':
        for block in (event.get('message') or {}).get('content') or []:
            if isinstance(block, dict) and block.get('type') == 'tool_result' and block.get('is_error'):
                content = block.get('content')
                text = content if isinstance(content, str) else ' '.join(
                    str(part.get('text', '')) for part in content or [] if isinstance(part, dict))
                if 'permission' in text.lower() or "haven't granted" in text.lower():
                    name = PENDING.pop(block.get('tool_use_id'), '工具')
                    emit(dict(type='item.completed', item=dict(type='error', message=f'{name} 调用未获授权，没有执行')))
        return
    if event.get('type') != 'assistant':
        return
    for block in (event.get('message') or {}).get('content') or []:
        if not isinstance(block, dict):
            continue
        if block.get('type') == 'tool_use':
            PENDING[block.get('id')] = str(block.get('name') or '工具')
            name, value = block.get('name'), block.get('input') or {}
            if name == 'Bash':
                item = dict(type='command_execution', command=str(value.get('command', '')))
            elif name in ('Read', 'Grep', 'Glob'):
                item = dict(type='file_read', path=str(value.get('file_path') or value.get('path') or value.get('pattern') or ''))
            elif name in ('Write', 'Edit', 'NotebookEdit'):
                item = dict(type='file_change', changes=[dict(path=str(value.get('file_path', '')))])
            else:
                item = dict(type='mcp_tool_call', server='claude', tool=str(name))
            emit(dict(type='item.completed', item=item))
        elif block.get('type') == 'text' and str(block.get('text', '')).strip():
            # 中间答复只作推理摘要，最终消息另行输出，避免被当作最终结果。
            emit(dict(type='item.completed', item=dict(type='reasoning', text=str(block['text'])[:2000])))


def transient(result, stderr):
    text = str((result or {}).get('result') or '') + ' ' + stderr
    status = (result or {}).get('api_error_status')
    return status in (429, 500, 502, 503, 529) or any(word in text.lower() for word in TRANSIENT)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    extra = []
    if '--' in argv:
        index = argv.index('--')
        argv, extra = argv[:index], argv[index + 1:]
    parser = argparse.ArgumentParser(prog='sop_board.claude_runner')
    parser.add_argument('--directory', required=True)
    parser.add_argument('--schema')
    parser.add_argument('-o', dest='output')
    parser.add_argument('--model')
    parser.add_argument('--effort')
    parser.add_argument('--session')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--add-dir', action='append', default=[])
    parser.add_argument('--work-directory')
    parser.add_argument('--retries', type=int, default=3)
    parser.add_argument('--wait', type=float, default=60.0, help='首次重试前等待的秒数，之后逐次递增')
    parser.add_argument('prompt')
    args = parser.parse_args(argv)
    session = args.session or str(uuid.uuid4())
    base = ['claude', '-p', '--output-format', 'stream-json', '--verbose']
    if args.model:
        base += ['--model', args.model]
    if args.effort:
        base += ['--effort', args.effort]
    for path in args.add_dir:
        base += ['--add-dir', path]
    if args.schema:
        base += ['--json-schema', Path(args.schema).read_text(encoding='utf-8')]
    base += extra
    # runner 与 cwd 供看板给出正确的续接命令（claude --resume 须在原工作目录运行）；只在本机任务材料中。
    emit(dict(type='thread.started', thread_id=session, runner='claude', cwd=str(Path(args.work_directory or args.directory).resolve())))
    prompt, resume, attempt = args.prompt, args.resume, 0
    while True:
        command = base + (['--resume', session] if resume else ['--session-id', session]) + [prompt]
        try:
            process = subprocess.Popen(command, cwd=args.work_directory or args.directory, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except FileNotFoundError:
            emit(dict(type='error', message='未找到 claude 命令'))
            return 127
        result = None
        for line in process.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            translate(event)
            if event.get('type') == 'result':
                result = event
        stderr = process.stderr.read()
        sys.stderr.write(stderr)
        code = process.wait()
        if result and not result.get('is_error') and code == 0:
            if args.schema:
                if result.get('structured_output') is None:
                    emit(dict(type='error', message='Claude 没有返回符合结构的输出'))
                    return 1
                final = json.dumps(result['structured_output'], ensure_ascii=False)
            else:
                final = str(result.get('result') or '')
            emit(dict(type='item.completed', item=dict(type='agent_message', text=final)))
            if args.output:
                Path(args.output).write_text(final, encoding='utf-8')
            return 0
        lines = stderr.strip().splitlines()
        message = str((result or {}).get('result') or (lines[-1] if lines else '') or f'claude 退出码 {code}')
        if transient(result, stderr) and attempt < args.retries:
            attempt += 1
            delay = args.wait * attempt
            emit(dict(type='error', message=f'临时错误，{delay:g} 秒后续接同一会话（第 {attempt} 次）：{message[:200]}'))
            time.sleep(delay)
            prompt, resume = RESUME_PROMPT, True
            continue
        emit(dict(type='error', message=message[:300]))
        return code or 1


if __name__ == '__main__':
    sys.exit(main())
