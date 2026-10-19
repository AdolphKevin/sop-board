"""Run the packaged board.js against HTTP-rendered pages in isolated Node.js."""

import json
from pathlib import Path
import subprocess

import pytest

from helpers import BASE, IDS, push, round_body, upload
from html_helpers import page
from test_pages import WEB


def run_client(board, case, saved=None, *, detail=False, query=""):
    pushed, _ = push(board)
    assert board.request("POST", BASE + "/rounds", round_body(pushed, upload(board)))[0] == 200
    url = WEB + ("/s/" + IDS[0] if detail else "/scenarios") + query
    return run_page_client(board, case, url, saved=saved)


def run_page_client(board, case, url, *, saved=None, network=False, context=None):
    document = page(board, url)

    def serialize(node):
        if isinstance(node, str):
            return node
        return dict(tag=node.tag, attrs=node.attrs, children=[serialize(child) for child in node.children])

    status, script, _ = board.request("GET", "/assets/board.js", auth=False)
    assert status == 200
    payload = dict(case=case, document=serialize(document), location="http://board.test" + url,
                   script=script.decode(), saved=saved or {}, **(context or {}))
    if network:
        payload["serverOrigin"] = f"http://127.0.0.1:{board.port}"
    result = subprocess.run(["node", str(Path(__file__).with_name("client_form_runner.js"))],
                            input=json.dumps(payload), text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_client_restores_identity_and_saves_changes_without_network(board):
    """SOP-21-N03, I1: identity is stored once, can be changed without a request."""
    run_client(board, "identity", {"sop-board.marker": "Saved Tester", "sop-board.token": "local-test-value"})


def test_client_three_buttons_expand_switch_and_cancel_without_request(board):
    """SOP-21-P04/P05, I1: both nonpass states open the same two fields; cancel clears."""
    run_client(board, "expand")


@pytest.mark.parametrize("status", ["pass", "fail", "blocked"])
def test_client_missing_identity_blocks_all_review_buttons(board, status):
    """SOP-21-N03: even opening a review asks for identity before proceeding."""
    run_client(board, "missing-identity-" + status)


@pytest.mark.parametrize("status", ["fail", "blocked"])
def test_client_empty_defect_keeps_note_without_request(board, status):
    """SOP-21-N01: empty or whitespace links never create a request."""
    run_client(board, "empty-defect-" + status)


@pytest.mark.parametrize("status", ["pass", "fail", "blocked"])
def test_client_submits_allowed_fields_and_updates_result_queue_counts_without_navigation(board, status):
    """SOP-21-P03/P04: one request, exact new fields, in-place state and counts."""
    run_client(board, "success-" + status)


@pytest.mark.parametrize("reason", ["defect", "unauthorized", "stale", "network"])
def test_client_preserves_input_and_allows_retry_after_rejection(board, reason):
    """SOP-21-N02: the reason is text, content stays and retry is possible."""
    run_client(board, "reject-" + reason)


def test_client_details_append_manual_history_in_place(board):
    """SOP-21-P05: details append the generated result and evidence without navigation."""
    run_client(board, "detail-success", detail=True)


@pytest.mark.parametrize("filter", ["all", "unreviewed", "agent-pass", "fail", "blocked", "invalid"])
def test_client_filters_locally_and_restores_shared_url_and_back_button(board, filter):
    """SOP-21-P02: query, click and browser back use the same local filter."""
    run_client(board, "filters", query="?filter=" + filter + "&keep=yes#anchor")


def test_client_success_removes_reviewed_card_from_unreviewed_filter(board):
    """SOP-21-P03: the active filter and its node visibility react to a saved mark."""
    run_client(board, "filtered-success", query="?filter=unreviewed")


def test_client_saves_identity_while_typing_and_restores_after_reload(board):
    """SOP-21-P12: identity survives reload without a separate save or submission."""
    run_client(board, "identity-auto")
