import hashlib
import pytest

from helpers import BASE, IDS, assert_error, demo, push, push_body, snapshot


def test_push_persists_version_and_independent_fingerprints(board):
    """I2, D1; SOP-02-P01: two nodes, four scenarios, independent hashes."""
    body, response = push(board)
    assert response == dict(ok=True, version=1, added=IDS, changed=[], removed=[], unchanged=False)
    assert board.sql("SELECT key, identity, name, defect_prefix FROM projects") == [(
        "github-com-demo-org-demo-app", "github.com/demo-org/demo-app", "演示项目",
        "https://github.com/demo-org/demo-app/issues/")]
    assert board.sql("SELECT version, content, sha256, path, commit_id, dirty, pushed_by FROM versions") == [(
        1, body["content"], hashlib.sha256(body["content"].encode()).hexdigest(),
        "demo.yaml", "c1", 0, "开发岗")]
    assert dict(board.sql("SELECT scenario, fingerprint FROM scenarios")) == body["fingerprints"]
    assert board.sql("SELECT product_digest FROM batches") == [("product-1",)]


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic isolated-test"])
def test_all_write_endpoints_require_token(board, authorization):
    """I2; SOP-02-N01, authentication precedes parsing and persistence."""
    before = snapshot(board)
    for method, path in [("PUT", BASE + "/scenario-file"), ("POST", "/api/v1/evidence"),
                         ("POST", BASE + "/rounds"), ("POST", BASE + "/marks"),
                         ("PUT", "/api/v1/projects/github-com-demo-org-demo-app")]:
        headers = {"Authorization": authorization} if authorization else {}
        assert_error(board.request(method, path, b"invalid", auth=False, headers=headers),
                     401, "unauthorized")
    assert snapshot(board) == before


@pytest.mark.parametrize("kind", ["negative", "positive", "duplicate", "node-duplicate", "metadata",
                                  "source", "steps", "priority", "entry", "polarity", "negative-type",
                                  "regression", "yaml", "unsafe", "non-json", "recursive"])
def test_invalid_scenario_files_have_lines_and_no_writes(board, kind):
    """I2; SOP-02-N03: scenario contract, safe YAML, line diagnostics."""
    doc = demo()
    scenario = doc["sops"][0]["scenarios"][0]
    if kind == "negative":
        doc["sops"][0]["scenarios"].pop()
    elif kind == "positive":
        doc["sops"][0]["scenarios"].pop(0)
    elif kind == "duplicate":
        doc["sops"][1]["scenarios"][0]["id"] = scenario["id"]
    elif kind == "node-duplicate":
        doc["sops"][1]["id"] = "N1"
    elif kind == "metadata":
        del doc["metadata"]["owner"]
    elif kind == "source":
        doc["metadata"]["sources"][0]["confirmed_by"] = ""
    elif kind == "steps":
        scenario["steps"] = []
    elif kind == "priority":
        scenario["priority"] = ""
    elif kind == "entry":
        scenario["verify"] = " "
    elif kind == "polarity":
        scenario["polarity"] = "unknown"
    elif kind == "negative-type":
        del doc["sops"][0]["scenarios"][1]["negative_type"]
    elif kind == "regression":
        scenario["regression"] = {"check": "unit", "cases": []}
    body = push_body(doc)
    if kind in ("yaml", "unsafe", "non-json", "recursive"):
        body["content"] = {"yaml": "sops: [", "unsafe": "!!python/object/apply:os.system ['false']",
                           "non-json": "x: 2026-01-01", "recursive": "x: &loop [*loop]"}[kind]
    before = snapshot(board)
    problems = assert_error(board.request("PUT", BASE + "/scenario-file", body), 422,
                            "scenario-file-invalid")
    assert all(isinstance(p.get("line"), int) and p["line"] >= 1 for p in problems)
    if kind == "negative":
        assert "N1" in str(problems) and "反向" in str(problems)
    assert snapshot(board) == before


@pytest.mark.parametrize("kind", ["batch", "project", "fingerprint", "missing-fingerprint", "extra-fingerprint"])
def test_push_rejects_wrong_identity_or_fingerprints(board, kind):
    """I2, D1; SOP-02-N02/N03: failure creates no project or version."""
    body = push_body()
    if kind == "batch":
        body = push_body(content=body["content"].replace("batch: demo", "batch: other"))
    elif kind == "project":
        body["project"]["id"] = "github.com/somewhere/else"
    elif kind == "fingerprint":
        body["fingerprints"][IDS[0]] = "0" * 64
    elif kind == "missing-fingerprint":
        del body["fingerprints"][IDS[0]]
    else:
        body["fingerprints"]["unknown"] = "0" * 64
    before = snapshot(board)
    mismatch = kind in ("batch", "project")
    problems = assert_error(board.request("PUT", BASE + "/scenario-file", body),
                            422 if mismatch else 409,
                            "project-mismatch" if mismatch else "fingerprint-mismatch")
    if kind in ("fingerprint", "missing-fingerprint"):
        assert IDS[0] in str(problems)
    assert snapshot(board) == before


def test_duplicate_push_updates_product_without_new_version(board):
    """I2, D1; SOP-02-N04: equality is raw content, not push metadata."""
    body, _ = push(board)
    body.update(product_digest="product-2", pushed_by="另一人", commit="c2", dirty=True)
    _, response = push(board, body)
    assert response == dict(ok=True, version=1, added=[], changed=[], removed=[], unchanged=True)
    assert board.sql("SELECT COUNT(*) FROM versions") == [(1,)]
    assert board.sql("SELECT product_digest FROM batches") == [("product-2",)]


def test_versions_classify_changes_and_preserve_old_fingerprints(board):
    """I2, D1: added, changed, removed; metadata edits do not change fingerprints."""
    original, _ = push(board)
    doc = demo()
    doc["sops"][0]["scenarios"][0]["expected"] = ["新的预期"]
    doc["sops"][0]["scenarios"][1]["title"] = "只改标题"
    doc["sops"][1]["scenarios"].pop()
    doc["sops"][1]["negative_exempt"] = "演示不适用"
    from copy import deepcopy
    added = deepcopy(doc["sops"][1]["scenarios"][0])
    added["id"] = "N2-P02"
    doc["sops"][1]["scenarios"].append(added)
    _, response = push(board, push_body(doc))
    assert response == dict(ok=True, version=2, added=["N2-P02"], changed=["N1-P01"],
                            removed=["N2-N01"], unchanged=False)
    assert board.sql("SELECT version FROM versions ORDER BY version") == [(1,), (2,)]
    assert board.sql("SELECT fingerprint,current FROM scenarios WHERE scenario='N1-P01' ORDER BY id")[0] == (
        original["fingerprints"]["N1-P01"], 0)
    assert board.sql("SELECT current FROM scenarios WHERE scenario='N2-N01'") == [(0,)]
    assert board.sql("SELECT title FROM scenarios WHERE scenario='N1-N01'") == [("只改标题",)]
