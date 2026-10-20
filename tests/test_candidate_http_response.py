"""候选就绪只核对本次 HTTP 响应，不追随重定向。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

from candidate_helpers import start_entry
from codex_helpers import fake_codex, mode
from execution_helpers import checkout, execution_board, start_execution, settle, rounds, task_directory
from test_execution_candidates import select_entries
from test_writable_repos import repo, wait_for


def test_redirect_response_is_ready_without_following_location(board_factory, repo, fake_codex, tmp_path):
    """SOP-43-P02：收到 302 即就绪，目标不被访问；后续会话耗时不改变就绪结论。"""
    requests = []

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(503)
            self.end_headers()

        def log_message(self, *args):
            pass

    target = ThreadingHTTPServer(('127.0.0.1', 0), Target)
    thread = threading.Thread(target=target.serve_forever, kwargs=dict(poll_interval=.02))
    thread.start()
    try:
        checkout(repo)
        preset = start_entry(repo, tmp_path)
        preset.write_text('{"status":302}')
        server = repo / 'serve.py'
        server.write_text(server.read_text().replace('http://127.0.0.1:1/unavailable',
            f'http://127.0.0.1:{target.server_port}/unavailable'))
        select_entries(repo, ['api'] * 4)
        mode(fake_codex, delay=4)
        board = execution_board(board_factory, fake_codex, repo)
        task = start_execution(board)
        # 会话已启动证明候选已就绪；直接核对目标未被访问，不用整个任务耗时推断跳转行为。
        wait_for(lambda: (task_directory(board, task) / 'invocation.json').exists())
        assert requests == []
        done = settle(board, task)
        assert done['status'] == '已完成' and all(r['status'] == 'pass' for r in rounds(board, done)[0]['results']), done
        assert requests == []
    finally:
        target.shutdown()
        target.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
