from datetime import datetime
from io import BytesIO
import json
import re
from zipfile import ZipFile
from zoneinfo import ZoneInfo

import pytest

from helpers import BASE, IDS, push, round_body, upload
from html_helpers import page
from test_marks import mark_body
from test_pages import WEB
from test_reads import get


def expected_local_time(value, zone):
    local = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(ZoneInfo(zone))
    value = local.isoformat(sep=" ", timespec="seconds")
    return value[:-6] + " " + value[-6:]


@pytest.mark.parametrize("zone,executed_at,literal", [
    ("Asia/Shanghai", "2020-01-15T08:09:10.123456+08:00", "2020-01-15 08:09:10 +08:00"),
    ("UTC", "2020-01-15T08:09:10.123456+08:00", "2020-01-15 00:09:10 +00:00"),
    ("Asia/Kathmandu", "2020-01-15T00:09:10.654321Z", "2020-01-15 05:54:10 +05:45"),
    ("America/New_York", "2020-01-15T08:09:10.123456+08:00", "2020-01-14 19:09:10 -05:00"),
    ("America/New_York", "2020-07-15T08:09:10.123456+08:00", "2020-07-14 20:09:10 -04:00"),
])
def test_page_times_use_server_timezone_while_api_and_export_keep_iso(board_factory, zone, executed_at, literal):
    """W3；SOP-06-P03：执行、提交、推送和观察时间统一，原接口与证据不变。"""
    board = board_factory(env={"TZ": zone})
    pushed, _ = push(board)
    body = round_body(pushed, upload(board))
    body["results"] = body["results"][:1]
    body["results"][0]["executed_at"] = executed_at
    assert board.request("POST", BASE + "/rounds", body)[0] == 200
    assert board.request("POST", BASE + "/marks", mark_body(pushed))[0] == 200
    agent, manual = get(board, BASE + "/rounds/1"), get(board, BASE + "/rounds/2")
    detail = page(board, WEB + "/s/" + IDS[0])
    overview = page(board, WEB)
    group = detail.one("section", **{"data-fingerprint": pushed["fingerprints"][IDS[0]]})
    assert group.one("time", datetime=executed_at).text == literal
    pushed_at = board.sql("SELECT pushed_at FROM versions")[0][0]
    assert overview.one("time", id="pushed-at").attrs["datetime"] == pushed_at
    for data in (agent, manual):
        row = overview.one("tr", **{"data-round": str(data["round"])})
        assert row.one("time").attrs["datetime"] == data["submitted_at"]
        assert detail.find_all("time", datetime=data["submitted_at"])
    times = detail.find_all("time") + overview.find_all("time")
    assert len(times) >= 7
    for element in times:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{2}:\d{2}", element.text)
        assert element.text == expected_local_time(element.attrs["datetime"], zone)
    # The generated observation is also displayed using local time, while its
    # original JSON bytes remain suitable for evidence download and export.
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", detail.text)
    assert agent["results"][0]["executed_at"] == executed_at
    summary = get(board, BASE)
    assert summary["pushed_at"] == pushed_at
    for result in (agent, manual):
        assert "T" in result["submitted_at"]
        raw = board.request("GET", BASE + f"/rounds/{result['round']}/export", auth=False)[1]
        with ZipFile(BytesIO(raw)) as archive:
            exported = json.loads(archive.read("results.jsonl"))
            assert exported["executed_at"] == result["results"][0]["executed_at"]
            if result["kind"] == "manual":
                observation = json.loads(archive.read(exported["evidence"][0]))
                assert observation["recorded_at"] == exported["executed_at"]
                assert "T" in observation["recorded_at"]
