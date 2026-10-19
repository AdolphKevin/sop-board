import pytest

from helpers import BASE, assert_error, push_body, snapshot


@pytest.mark.parametrize("value", [None, [], {}, {"project": []}, "text"])
def test_incomplete_push_envelope_has_structured_failure_and_no_writes(board, value):
    """I2: malformed JSON shapes cannot escape validation or partly create a project."""
    import json
    before = snapshot(board)
    problems = assert_error(board.request("PUT", BASE + "/scenario-file", json.dumps(value).encode()),
                            422, "scenario-file-invalid")
    assert all(p.get("line") == 1 for p in problems)
    assert snapshot(board) == before


@pytest.mark.parametrize("field,value", [("dirty", "false"), ("fingerprints", []), ("path", ""),
                                      ("content", 42), ("project", dict(id="a", name="")), ("pushed_by", " ")])
def test_push_field_types_are_checked(board, field, value):
    """I2: required envelope fields retain their declared data types."""
    body = push_body()
    body[field] = value
    before = snapshot(board)
    assert_error(board.request("PUT", BASE + "/scenario-file", body), 422, "scenario-file-invalid")
    assert snapshot(board) == before


@pytest.mark.parametrize("method,path", [("PUT", BASE + "/scenario-file"), ("POST", BASE + "/rounds"),
                                        ("POST", BASE + "/marks"), ("PUT", "/api/v1/projects/example")])
def test_bad_json_is_reported_without_writes(board, method, path):
    """HTTP JSON boundary: invalid-request instead of an unhandled exception."""
    before = snapshot(board)
    for raw in (b"{", b"\xff", b'{"value": NaN}'):
        assert_error(board.request(method, path, raw), 400, "invalid-request")
        assert snapshot(board) == before


@pytest.mark.parametrize("fragment", ["x: &a [1]\ny: *a", "x: 1\nx: 2", "x: .nan", "? [a,b]\n: c"])
def test_yaml_subset_rejects_aliases_duplicate_keys_and_non_json_types(board, fragment):
    """Scenario format is the JSON-compatible YAML subset, with useful line numbers."""
    body = push_body(content=fragment)
    before = snapshot(board)
    problems = assert_error(board.request("PUT", BASE + "/scenario-file", body), 422, "scenario-file-invalid")
    assert all(p["line"] >= 1 for p in problems)
    assert snapshot(board) == before
