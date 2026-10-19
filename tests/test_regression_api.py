"""SOP-BOARD-1 场景版本 4：写入边界、证据与项目设置。"""

import pytest

from helpers import BASE, IDS, KEY, assert_error, demo, push, push_body, round_body, snapshot, upload
from test_marks import mark_body
from test_reads import get


def test_project_key_and_crlf_fingerprint_are_normalized(board):
    """SOP-02-P02：项目身份保留原文，项目键与换行统一后的指纹正确。"""
    identity = dict(id="GitHub.com/Demo_Org/Demo.App", name="演示应用")
    push(board, push_body(project=identity))
    project = get(board, "/api/v1/projects")["projects"][0]
    assert (project["key"], project["id"]) == (KEY, identity["id"])
    doc = demo()
    doc["sops"][0]["scenarios"][0]["expected"] = ["第一行\r\n第二行"]
    body = push_body(doc, project=identity)
    assert "\\r\\n" in body["content"]
    push(board, body)
    scenarios = get(board, BASE + "/scenarios")["scenarios"]
    scenario = next(row for row in scenarios if row["scenario"] == IDS[0])
    assert scenario["fingerprint"] == body["fingerprints"][IDS[0]]


@pytest.mark.parametrize("authorization", [None, "Bearer wrong"], ids=["missing", "wrong"])
def test_write_auth_preserves_existing_data_and_reads_need_no_token(board, authorization):
    """SOP-02-N05：五个写接口拒绝越权且无副作用，六个读接口可匿名访问。"""
    pushed, _ = push(board)
    evidence = upload(board)
    assert board.request("POST", BASE + "/rounds", round_body(pushed, evidence))[0] == 200
    headers = {"Authorization": authorization} if authorization else {}
    before = snapshot(board)
    for method, path, body in [
        ("PUT", BASE + "/scenario-file", push_body(product_digest="forbidden-product")),
        ("POST", "/api/v1/evidence", b"forbidden new evidence"),
        ("POST", BASE + "/rounds", round_body(pushed, evidence, key="forbidden-round")),
        ("POST", BASE + "/marks", mark_body(pushed)),
        ("PUT", f"/api/v1/projects/{KEY}", dict(defect_prefix="https://forbidden.example/")),
    ]:
        assert_error(board.request(method, path, body, auth=False, headers=headers), 401, "unauthorized")
        assert snapshot(board) == before
    for path in ("/api/v1/projects", BASE, BASE + "/scenarios", BASE + "/rounds/1",
                 BASE + "/rounds/1/export", "/api/v1/evidence/" + evidence["sha256"]):
        assert board.request("GET", path, auth=False)[0] == 200


def test_nonempty_custom_verify_is_preserved(board):
    """SOP-02-P03：任意非空验证入口名称可推送并原样读取。"""
    doc = demo()
    for node in doc["sops"]:
        for scenario in node["scenarios"]:
            scenario["verify"] = "浏览器-结账"
    push(board, push_body(doc))
    scenarios = get(board, BASE + "/scenarios")["scenarios"]
    assert len(scenarios) == 4
    assert all(row["verify"] == "浏览器-结账" for row in scenarios)


@pytest.mark.parametrize("kind", ["yaml-line-seven", "empty-verify", "identity"])
def test_push_diagnostics_and_empty_project_list(board, kind):
    """SOP-02-N06：拒绝三种无效推送，精确指出行或字段且不创建项目。"""
    doc = demo()
    if kind == "empty-verify":
        doc["sops"][0]["scenarios"][0]["verify"] = ""
    body = push_body(doc)
    if kind == "yaml-line-seven":
        body["content"] = ("metadata:\n  requirement: demo\n  batch: demo\n  version: 1\n"
                           "  owner: demo\n  sources: []\n x: invalid indentation\n")
    elif kind == "identity":
        body["project"]["id"] = "github.com/other/app"
    before = snapshot(board)
    problems = assert_error(board.request("PUT", BASE + "/scenario-file", body), 422,
                            "project-mismatch" if kind == "identity" else "scenario-file-invalid")
    if kind == "yaml-line-seven":
        assert any(p["line"] == 7 for p in problems)
    elif kind == "empty-verify":
        assert any(p["field"] == "sops.0.scenarios.0.verify" for p in problems)
    else:
        assert any(p["field"] == "project.id" for p in problems)
    assert get(board, "/api/v1/projects")["projects"] == []
    assert snapshot(board) == before


def test_png_and_text_evidence_deduplicate_and_download_with_original_types(board):
    """SOP-03-P02：三次上传核对摘要、大小、首次文件名和两种下载类型。"""
    png, text = b"\x89PNG\r\n\x1a\npicture evidence", "原始文本证据\n".encode()
    first = upload(board, png, "截图.png")
    second = upload(board, png, "another.png")
    third = upload(board, text, "run.txt")
    assert first["sha256"] == second["sha256"]
    assert board.sql("SELECT name,size FROM evidence ORDER BY name") == [
        ("run.txt", len(text)), ("截图.png", len(png))]
    files = {p.relative_to(board.data).as_posix(): p.read_bytes()
             for p in (board.data / "evidence").rglob("*") if p.is_file()}
    assert files == {f"evidence/{item['sha256'][:2]}/{item['sha256']}": raw
                     for item, raw in ((first, png), (third, text))}
    for item, raw, content_type in ((first, png, "image/png"), (third, text, "text/plain")):
        status, downloaded, headers = board.request("GET", "/api/v1/evidence/" + item["sha256"], auth=False)
        assert status == 200 and downloaded == raw
        assert headers["Content-Type"].split(";", 1)[0] == content_type


@pytest.mark.parametrize("path,code", [
    ("/api/v1/projects/no-project/batches/demo", "project-not-found"),
    ("/api/v1/projects/no-project/batches/demo/scenarios", "project-not-found"),
    (f"/api/v1/projects/{KEY}/batches/no-batch", "batch-not-found"),
    (f"/api/v1/projects/{KEY}/batches/no-batch/rounds/1/export", "batch-not-found"),
    (BASE + "/rounds/99", "round-not-found"),
    ("/api/v1/evidence/" + "0" * 64, "evidence-not-found"),
], ids=["project-summary", "project-scenarios", "batch-summary", "batch-export", "round", "evidence"])
def test_missing_reads_return_resource_specific_errors(board, path, code):
    """SOP-06-N02：已有正常轮次时，六种缺失资源读取均返回对应 404。"""
    pushed, _ = push(board)
    assert board.request("POST", BASE + "/rounds", round_body(pushed, upload(board)))[0] == 200
    assert_error(board.request("GET", path, auth=False), 404, code)


def test_project_list_contains_identity_name_and_batch(board):
    """SOP-06-P11：匿名项目列表保留项目键、身份、名称与批次。"""
    push(board, push_body(project=dict(id="github.com/demo-org/demo-app", name="演示应用")))
    project = get(board, "/api/v1/projects")["projects"][0]
    assert (project["key"], project["id"], project["name"]) == (
        KEY, "github.com/demo-org/demo-app", "演示应用")
    assert [batch["name"] for batch in project["batches"]] == ["demo"]


def test_default_and_custom_defect_prefix_control_marks(board):
    """SOP-07-P01：默认链接可用，更换前缀后只接受新前缀的缺陷链接。"""
    pushed, _ = push(board)
    github = "https://github.com/demo-org/demo-app/issues/1"
    assert board.request("POST", BASE + "/marks", mark_body(pushed, "fail", defect_url=github))[0] == 200
    assert get(board, BASE + "/rounds/1")["results"][0]["finding"] == github
    prefix = "https://jira.example.com/browse/"
    status, response, _ = board.request("PUT", f"/api/v1/projects/{KEY}", dict(defect_prefix=prefix))
    assert status == 200 and response == dict(ok=True, defect_prefix=prefix)
    body = mark_body(pushed, "fail", scenario=IDS[2], fingerprint=pushed["fingerprints"][IDS[2]],
                     defect_url=prefix + "ABC-1")
    assert board.request("POST", BASE + "/marks", body)[0] == 200
    body["defect_url"] = "https://github.com/demo-org/demo-app/issues/2"
    before = snapshot(board)
    assert_error(board.request("POST", BASE + "/marks", body), 422, "defect-link-required")
    assert snapshot(board) == before


def test_invalid_prefixes_preserve_default_and_non_github_requires_setting(board):
    """SOP-07-N01：非法前缀均拒绝且保留默认值，无默认前缀的项目不能标失败。"""
    pushed, _ = push(board)
    for prefix in ("http://jira.example.com/browse/", "jira.example.com/browse/", ""):
        before = snapshot(board)
        status, response, _ = board.request("PUT", f"/api/v1/projects/{KEY}", dict(defect_prefix=prefix))
        assert 400 <= status < 500 and response["ok"] is False
        assert any(p["field"] == "defect_prefix" for p in response["problems"])
        assert snapshot(board) == before
    link = "https://github.com/demo-org/demo-app/issues/9"
    assert board.request("POST", BASE + "/marks", mark_body(pushed, "fail", defect_url=link))[0] == 200
    assert board.sql("SELECT defect_prefix FROM projects WHERE key=?", (KEY,)) == [
        ("https://github.com/demo-org/demo-app/issues/",)]
    other = push_body(project=dict(id="gitlab.example.com/team/app", name="其他项目"))
    other_base = "/api/v1/projects/gitlab-example-com-team-app/batches/demo"
    assert board.request("PUT", other_base + "/scenario-file", other)[0] == 200
    before = snapshot(board)
    assert_error(board.request("POST", other_base + "/marks", mark_body(other, "fail",
                 defect_url="https://gitlab.example.com/team/app/-/issues/1")), 422, "defect-prefix-unset")
    assert snapshot(board) == before
