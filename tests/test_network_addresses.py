import ipaddress
from pathlib import Path
import re
import subprocess
import sys

from conftest import OPENER, ROOT


def operating_system_ipv4_addresses():
    """独立读取操作系统的接口/路由清单，不使用应用的地址枚举实现。"""
    if sys.platform == "darwin":
        output = subprocess.check_output(["/sbin/ifconfig"], text=True)
        addresses = []
        up = False
        for line in output.splitlines():
            if line and not line[0].isspace():
                up = "UP" in line.split("<", 1)[-1].split(">", 1)[0].split(",")
            match = re.match(r"\s+inet ([0-9.]+)\s", line)
            if up and match:
                addresses.append(match[1])
    elif sys.platform.startswith("linux"):
        addresses = re.findall(r"\|-- ([0-9.]+)\s*\n\s*/32 host LOCAL", Path("/proc/net/fib_trie").read_text())
    else:
        import socket
        addresses = socket.gethostbyname_ex(socket.gethostname())[2]
    return {str(address) for value in addresses if not (address := ipaddress.IPv4Address(value)).is_loopback
            and not address.is_unspecified and not address.is_multicast}


def test_shared_startup_reports_each_ipv4_and_prints_loopback(board_factory):
    """W2；SOP-01-P01：主地址可在本机打开，逐行给出每个真实接口的 IPv4。"""
    expected = operating_system_ipv4_addresses()
    assert expected, "本机需要至少一个内网接口以核对真实监听"
    board = board_factory("0.0.0.0")
    lines = board.log.read_text().splitlines()
    assert lines[0] == f"看板地址：http://127.0.0.1:{board.port}/"
    links = [line.removeprefix("内网地址：") for line in lines if line.startswith("内网地址：")]
    assert len(links) == len(expected)
    assert set(links) == {f"http://{address}:{board.port}/" for address in expected}
    assert not any("0.0.0.0" in line for line in lines)
    for link in links:
        with OPENER.open(link, timeout=3) as response:
            assert response.status == 200


def test_shared_startup_prints_multiple_adapter_addresses(board_factory):
    """W2：在实际 CLI 启动中用两张网卡的地址核对逐行输出。"""
    script = ("from sop_board import cli; "
              "cli.local_ipv4_addresses = lambda: ['10.20.30.40', '192.168.50.60']; "
              "raise SystemExit(cli.main())")
    board = board_factory("0.0.0.0", command=[str(ROOT / ".venv/bin/python"), "-c", script])
    assert board.log.read_text().splitlines() == [f"看板地址：http://127.0.0.1:{board.port}/",
        f"内网地址：http://10.20.30.40:{board.port}/", f"内网地址：http://192.168.50.60:{board.port}/"]
