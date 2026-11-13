"""启动的会话的日志转写进程：从标准输入读会话输出，遮蔽后写进启动日志。

它随会话存活，不经守护进程，所以 sop-board restart 换守护进程时会话照常运行（已定决策 28）。
遮蔽范围与原来由守护进程转写时相同：config.yaml 里保存的共享库密码（已定决策 27）。
"""
import argparse
from pathlib import Path
import signal
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m sop_board.relay')
    parser.add_argument('--data', required=True, help='项目数据目录')
    parser.add_argument('--log', required=True, help='启动日志')
    args = parser.parse_args(argv)
    # 会话结束（写端全部关闭）时读到文件尾再退出；终端的中断信号不应截断日志。
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    from . import config
    from .redaction import StreamRedactor
    try:
        # 读取项目配置会登记已保存的共享库密码，供遮蔽使用。
        config.Project(Path(args.data))
    except Exception as error:  # noqa: BLE001 - 配置读不出时照样转写，原因写进日志开头
        notice = f'sop-board：读取项目配置失败，日志未按已保存的密码遮蔽：{type(error).__name__}\n'.encode()
    else:
        notice = b''
    redactor = StreamRedactor([])
    source = sys.stdin.buffer
    with open(args.log, 'wb') as output:
        if notice:
            output.write(notice)
        while True:
            chunk = source.read1(65536)
            output.write(redactor.feed(chunk, final=not chunk))
            output.flush()
            if not chunk:
                break
    return 0


if __name__ == '__main__':
    sys.exit(main())
