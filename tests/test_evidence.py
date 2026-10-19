import hashlib

import pytest

from conftest import OPENER

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


@pytest.mark.parametrize("name", ["中文.txt", "中文.json", "中文.ndjson", "中文.md", "中文.log"])
def test_text_download_has_utf8_charset_and_exact_bytes(board, name):
    """SOP-21-P06: direct downloads display Chinese correctly for every declared text type."""
    raw = "中文原始证据\n".encode()
    evidence = upload(board, raw, name)
    with OPENER.open(f"http://127.0.0.1:{board.port}/api/v1/evidence/{evidence['sha256']}", timeout=5) as response:
        assert response.status == 200
        assert response.read() == raw
        assert "charset=utf-8" in response.headers["Content-Type"]


def test_six_evidence_files_preview_lengths_download_links_utf8_and_original_bytes(board):
    """SOP-21-P06: all six files are checked together, including the untruncated Markdown."""
    import base64
    from helpers import BASE, IDS, push, round_body
    from html_helpers import page
    from test_pages import WEB

    pushed, _ = push(board)
    log_lines = [f"日志中文第 {i} 行" for i in range(1, 301)]
    md_lines = [f"笔记中文第 {i} 行" for i in range(1, 151)]
    originals = [
        ("run.log", ("\n".join(log_lines) + "\n").encode()),
        ("notes.md", ("\n".join(md_lines) + "\n").encode()),
        ("data.json", '{"中文":"证据"}\n'.encode()),
        ("events.ndjson", '{"事件":"一"}\n{"事件":"二"}\n'.encode()),
        ("plain.txt", "普通中文证据\n".encode()),
        ("image.png", base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j6QAAAABJRU5ErkJggg==")),
    ]
    evidence = [upload(board, raw, name) for name, raw in originals]
    body = round_body(pushed, evidence[0], statuses=("pass",))
    body["results"][0]["evidence"] = evidence
    assert board.request("POST", BASE + "/rounds", body)[0] == 200
    detail = page(board, WEB + "/s/" + IDS[0])
    log = detail.one("pre", **{"data-evidence": evidence[0]["sha256"]})
    md = detail.one("pre", **{"data-evidence": evidence[1]["sha256"]})
    assert log.text.splitlines() == log_lines[:200]
    assert md.text.splitlines() == md_lines
    log_figure = next(f for f in detail.find_all("figure") if f.find_all("pre", **{"data-evidence": evidence[0]["sha256"]}))
    md_figure = next(f for f in detail.find_all("figure") if f.find_all("pre", **{"data-evidence": evidence[1]["sha256"]}))
    assert log_figure.one("figcaption").text == "run.log（只显示前 200 行）"
    assert md_figure.one("figcaption").text == "notes.md"
    assert "只显示前 200 行" not in md_figure.text
    assert detail.one("img", src="/api/v1/evidence/" + evidence[-1]["sha256"])
    for (name, raw), item in zip(originals, evidence):
        href = "/api/v1/evidence/" + item["sha256"]
        assert detail.one("a", href=href).attrs["download"] == name
        with OPENER.open(f"http://127.0.0.1:{board.port}" + href, timeout=5) as response:
            assert response.status == 200 and response.read() == raw
            if name != "image.png":
                assert "charset=utf-8" in response.headers["Content-Type"]
            else:
                assert response.headers["Content-Type"] == "image/png"
