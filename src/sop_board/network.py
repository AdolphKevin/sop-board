"""通过标准库读取本机网卡地址；不发送探测请求或运行外部命令。"""

import ctypes
import ipaddress
import socket
import sys


def is_loopback(address):
    """只信任连接对端的 IP；IPv4 映射地址按其 IPv4 部分判断。"""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
        parsed = parsed.ipv4_mapped
    return parsed.is_loopback


class Interface(ctypes.Structure):
    pass


Interface._fields_ = [
    ("next", ctypes.POINTER(Interface)), ("name", ctypes.c_char_p),
    ("flags", ctypes.c_uint), ("address", ctypes.c_void_p),
    ("netmask", ctypes.c_void_p), ("destination", ctypes.c_void_p), ("data", ctypes.c_void_p),
]


def interface_ipv4_addresses():
    # getifaddrs exposes every address, including aliases on the same adapter.
    # Layout follows the platform's <ifaddrs.h> and <netinet/in.h>.
    library = ctypes.CDLL(None, use_errno=True)
    head = ctypes.POINTER(Interface)()
    library.getifaddrs.argtypes = [ctypes.POINTER(ctypes.POINTER(Interface))]
    library.getifaddrs.restype = ctypes.c_int
    library.freeifaddrs.argtypes = [ctypes.POINTER(Interface)]
    library.freeifaddrs.restype = None
    if library.getifaddrs(ctypes.byref(head)) != 0:
        raise OSError(ctypes.get_errno(), "无法读取网卡地址")
    try:
        current = head
        while current:
            item = current.contents
            if item.address and item.flags & 1:  # IFF_UP
                header = ctypes.string_at(item.address, 2)
                family = header[1] if sys.platform.startswith(("darwin", "freebsd", "openbsd", "netbsd")) else int.from_bytes(header, sys.byteorder)
                if family == socket.AF_INET:
                    yield socket.inet_ntop(socket.AF_INET, ctypes.string_at(item.address, 8)[4:8])
            current = item.next
    finally:
        library.freeifaddrs(head)


def local_ipv4_addresses():
    try:
        values = list(interface_ipv4_addresses())
    except (OSError, AttributeError):
        # Platforms without getifaddrs can use their hostname resolver.
        try:
            values = [item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
        except OSError:
            values = []
    addresses = {ipaddress.IPv4Address(value) for value in values}
    return [str(address) for address in sorted(addresses)
            if not address.is_loopback and not address.is_unspecified and not address.is_multicast]
