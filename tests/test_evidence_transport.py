"""大证据仍经真实 HTTP 上传，避免一次 socket 写入耗尽本机缓冲区。"""
import errno
import socket

from helpers import upload


def test_large_evidence_streams_under_socket_buffer_limit(board, monkeypatch):
    """SOP-03-P01：模拟整轮中出现的 ENOBUFS，64 MiB 原文仍完整持久保存。"""
    original = socket.socket.sendall
    sizes = []

    def bounded_send(sock, data, *args, **kwargs):
        sizes.append(len(data))
        if len(data) > 1024 * 1024:
            raise OSError(errno.ENOBUFS, 'No buffer space available')
        return original(sock, data, *args, **kwargs)

    monkeypatch.setattr(socket.socket, 'sendall', bounded_send)
    raw = b'x' * (64 * 1024 * 1024)
    evidence = upload(board, raw, 'large-stream.bin')
    assert board.sql('SELECT size FROM evidence') == [(len(raw),)]
    saved = board.data / 'evidence' / evidence['sha256'][:2] / evidence['sha256']
    assert saved.read_bytes() == raw
    assert max(sizes) <= 1024 * 1024
