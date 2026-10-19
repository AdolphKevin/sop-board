import hashlib

from helpers import assert_error, snapshot, upload


def test_evidence_content_addressing_dedup_download_and_first_filename(board):
    """I2, I3, D1; SOP-03-P01: bytes, SHA, MIME and first filename."""
    raw = b"\x89PNG\r\n\x1a\nexample bytes"
    evidence = upload(board, raw, "截图.png")
    upload(board, raw, "renamed.txt")
    sha = hashlib.sha256(raw).hexdigest()
    assert evidence["sha256"] == sha
    assert board.sql("SELECT sha256,size,name FROM evidence") == [(sha, len(raw), "截图.png")]
    files = list((board.data / "evidence").rglob("*"))
    assert [p.relative_to(board.data).as_posix() for p in files if p.is_file()] == [f"evidence/{sha[:2]}/{sha}"]
    status, content, headers = board.request("GET", f"/api/v1/evidence/{sha}", auth=False)
    assert status == 200 and content == raw
    assert headers["Content-Type"] == "image/png"
    assert "filename*=" in headers["Content-Disposition"]


def test_evidence_limit_rejects_without_writes(board):
    """I2: 64 MiB limit checked before saving files or database rows."""
    before = snapshot(board)
    # Advertised oversized request needs no 64 MiB allocation or transfer.
    assert_error(board.request("POST", "/api/v1/evidence", b"", headers={
        "Content-Length": str(64 * 1024 * 1024 + 1), "X-Evidence-Name": "huge.bin"}),
        413, "evidence-too-large")
    assert snapshot(board) == before


def test_exactly_64_mib_evidence_is_accepted(board):
    """I2: the size limit is inclusive."""
    raw = b"x" * (64 * 1024 * 1024)
    evidence = upload(board, raw, "limit.bin")
    sha = evidence["sha256"]
    assert board.sql("SELECT size FROM evidence") == [(64 * 1024 * 1024,)]
    assert (board.data / "evidence" / sha[:2] / sha).stat().st_size == len(raw)
