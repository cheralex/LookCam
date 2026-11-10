"""Quick PPPP LanSearch over subnet ARP hosts (unicast, to find the camera)."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
import _bootstrap  # noqa: E402
from lookcam_deepsik.vendor.lookcam import net, pppp

hosts = []
for ip in net.arp_table():
    if ip.startswith("192.168.0.") and not ip.endswith((".0", ".255")):
        hosts.append(ip)
hosts = sorted(set(hosts), key=lambda s: int(s.rsplit(".", 1)[1]))
print("хосты:", hosts, flush=True)
for ip in hosts:
    try:
        replies = pppp.lan_search(ip, timeout=0.8, broadcast=False,
                                  extra_packets=False)
    except Exception as exc:
        print(ip, "ошибка", exc, flush=True)
        continue
    for r in replies:
        d = r.decoded
        print(f"  {ip}: cmd=0x{r.cmd:02x} id={d.get('id')!r} "
              f"field2={d.get('field2')!r} value={d.get('value')}", flush=True)
    if not replies:
        print(f"  {ip}: тишина", flush=True)
