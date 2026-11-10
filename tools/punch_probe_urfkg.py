"""Raw PunchPkt/P2PRdy probe to camera URFKG: what exactly it replies."""
import os
import socket
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
import _bootstrap  # noqa: E402
from lookcam_deepsik.service import proto

TARGET = "192.168.0.147"


def pkt_hex(data: bytes) -> str:
    return data[:48].hex(" ")


s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("0.0.0.0", 32108))
s.settimeout(0.3)
print("socket", s.getsockname(), flush=True)
s.sendto(proto.lan_search_packet(), ("192.168.0.255", 32108))
end = time.time() + 2
cam = None
while time.time() < end:
    try:
        data, addr = s.recvfrom(4096)
    except socket.timeout:
        continue
    if addr[0] == TARGET:
        print("LANSEARCH", addr, pkt_hex(data), flush=True)
        cam = addr
        break
if cam is None:
    print("камера не ответила", flush=True)
    raise SystemExit(1)

parsed = proto.parse_pppp(data)
payload = parsed[1] if parsed else b""
value = struct.unpack_from(">I", payload, 8)[0] if len(payload) >= 12 else 0x40FE
print("ident", payload[:8], "field2", payload[12:20], "value", hex(value), flush=True)

variants = [
    ("standard-0x40fe", proto.punch_packet("G550856URFKG")),
    (f"value-{value:04x}", struct.pack(">BBH", 0xF1, 0x41, 20)
     + payload[:8] + struct.pack(">I", value) + payload[12:20]),
]
for name, pkt in variants:
    print("SEND", name, pkt.hex(" "), flush=True)
    for _ in range(10):
        s.sendto(pkt, cam)
        time.sleep(0.05)
    end = time.time() + 2.0
    got = False
    while time.time() < end:
        try:
            data, addr = s.recvfrom(4096)
        except socket.timeout:
            continue
        print("  GOT", addr, pkt_hex(data), flush=True)
        got = True
    print("  ответ:", got, flush=True)
s.close()
