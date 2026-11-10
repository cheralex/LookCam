"""Network utilities: local addresses, gateway, ARP table, host discovery.

Standard library only. Works on Windows / Linux / macOS.
"""
from __future__ import annotations

import ipaddress
import os
import platform
import re
import socket
import subprocess
import sys
import threading

IS_WINDOWS = os.name == "nt"

# Subnets the camera brings up in access-point mode.
# Values are taken from the LookCam application itself (ApNetworkDetector.isApModeHostAddress).
DEVICE_AP_PREFIXES = ("192.168.100.", "192.168.10.", "192.168.169.")
# Additional typical camera subnets (heuristic, marked separately).
COMMON_CAMERA_PREFIXES = ("192.168.4.", "192.168.1.", "192.168.0.", "10.0.0.", "172.16.0.")

# Ports usually open on IP cameras (for fast host discovery).
CAMERA_PORTS = (80, 554, 8080, 8000, 8899, 90, 5000, 8554, 34567, 2020, 37777, 1024, 81)


def run(cmd: list[str], timeout: float = 6.0) -> str:
    """Run a command and return stdout (empty string on error)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           errors="replace")
        return (p.stdout or "") + (p.stderr or "")
    except Exception:
        return ""


def local_ipv4_for(remote: str = "8.8.8.8", port: int = 80) -> str | None:
    """Local IPv4 through which traffic to remote will go (without sending packets)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((remote, port))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def all_local_ipv4() -> list[str]:
    """All IPv4 addresses of the machine (best effort, no external dependencies)."""
    found: list[str] = []

    def add(ip: str) -> None:
        if ip and ip not in found and not ip.startswith("127.") and not ip.startswith("169.254."):
            found.append(ip)

    # 1) via hostname
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except Exception:
        pass

    # 2) via system utilities
    if IS_WINDOWS:
        out = run(["ipconfig"])
        for m in re.finditer(r"IPv4[^:]*:\s*([0-9.]+)", out):
            add(m.group(1))
    else:
        out = run(["ip", "-4", "addr"]) or run(["ifconfig"])
        for m in re.finditer(r"inet\s+(?:addr:)?([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", out):
            add(m.group(1))

    # 3) fallback — the address "facing outward"
    ip = local_ipv4_for()
    if ip:
        add(ip)
    return found


def default_gateways() -> list[str]:
    """Default gateways (in the camera's AP mode this is usually the camera itself)."""
    out = []
    if IS_WINDOWS:
        text = run(["ipconfig"])
        for line in text.splitlines():
            if "Gateway" in line or "Основной шлюз" in line or "Шлюз" in line:
                m = re.search(r"([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", line)
                if m and m.group(1) not in out:
                    out.append(m.group(1))
    else:
        text = run(["ip", "route"]) or run(["netstat", "-rn"])
        for m in re.finditer(r"default(?:\s+via)?\s+([0-9.]+)", text):
            if m.group(1) not in out:
                out.append(m.group(1))
    return out


def arp_table() -> dict[str, str]:
    """ARP table: ip -> mac."""
    table: dict[str, str] = {}
    if IS_WINDOWS:
        text = run(["arp", "-a"])
        for m in re.finditer(r"([0-9]+(?:\.[0-9]+){3})\s+([0-9a-fA-F-]{17})", text):
            table[m.group(1)] = m.group(2).lower().replace("-", ":")
    else:
        text = run(["ip", "neigh"])
        for m in re.finditer(r"([0-9]+(?:\.[0-9]+){3}).*?lladdr\s+([0-9a-fA-F:]{17})", text):
            table[m.group(1)] = m.group(2).lower()
        if not table:
            try:
                with open("/proc/net/arp", "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh.readlines()[1:]:
                        parts = line.split()
                        if len(parts) >= 4 and parts[3] != "00:00:00:00:00:00":
                            table[parts[0]] = parts[3].lower()
            except Exception:
                pass
    return table


def subnet_of(ip: str, prefix_len: int = 24) -> ipaddress.IPv4Network:
    return ipaddress.ip_network(f"{ip}/{prefix_len}", strict=False)


def classify_ap(ip: str | None) -> tuple[str, str]:
    """Classify an address: ('device_ap'|'common_camera'|'other', explanation)."""
    if not ip:
        return "other", "адрес не определён"
    if ip.startswith(DEVICE_AP_PREFIXES):
        return "device_ap", "подсеть точки доступа камеры (из приложения LookCam)"
    if ip.startswith(COMMON_CAMERA_PREFIXES):
        return "common_camera", "типовая подсеть камеры/роутера"
    return "other", "обычная сеть"


def tcp_open(host: str, port: int, timeout: float = 0.45) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def scan_ports(host: str, ports=CAMERA_PORTS, timeout: float = 0.45,
               workers: int = 32) -> list[int]:
    """Parallel TCP port scan (connect scan)."""
    open_ports: list[int] = []
    lock = threading.Lock()

    def work(port: int) -> None:
        if tcp_open(host, port, timeout):
            with lock:
                open_ports.append(port)

    threads = []
    queue = list(ports)
    idx = 0
    n = min(workers, max(1, len(queue)))
    while idx < len(queue) and n:
        chunk = queue[idx:idx + n]
        idx += n
        threads = [threading.Thread(target=work, args=(p,), daemon=True) for p in chunk]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout + 1.0)
    return sorted(open_ports)


def sweep_tcp(host: str, start: int = 1, end: int = 10000, timeout: float = 0.12,
              workers: int = 512, on_progress=None) -> list[int]:
    """Full TCP port sweep (connect scan) — looks for services on non-standard ports."""
    from collections import deque

    open_ports: list[int] = []
    lock = threading.Lock()
    queue = deque(range(start, end + 1))
    total = len(queue)
    done = [0]

    def worker() -> None:
        while True:
            try:
                port = queue.popleft()
            except IndexError:
                return
            try:
                if tcp_open(host, port, timeout):
                    with lock:
                        open_ports.append(port)
            finally:
                with lock:
                    done[0] += 1
                    if on_progress and done[0] % 2000 == 0:
                        on_progress(done[0], total)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sorted(open_ports)


# A small vendor table by the first three MAC bytes — for quick identification.
OUI_VENDORS = {
    "90:7a:da": "CHINA DRAGON TECHNOLOGY (китайский ODM камер)",
    "44:19:b6": "Hangzhou Hikvision",
    "3c:ef:8c": "Dahua Technology",
    "00:12:16": "Xiongmai (XMEye)",
    "74:83:c2": "Hikvision",
    "bc:14:85": "Hikvision",
    "ec:4d:3e": "Hikvision",
    "34:96:72": "TCP-Camera / ODM",
    "b0:95:75": "TP-Link",
    "00:0c:29": "VMware",
    "0a:00:27": "VirtualBox",
    "08:00:27": "VirtualBox",
}


def mac_vendor(mac: str | None) -> str:
    if not mac:
        return ""
    return OUI_VENDORS.get(mac.lower()[:8], "неизвестный производитель")


def find_hosts(net: ipaddress.IPv4Network, probe_ports=CAMERA_PORTS[:4],
                timeout: float = 0.4, workers: int = 64) -> list[str]:
    """Find live hosts in a subnet: ARP + fast TCP probing of typical ports."""
    alive: set[str] = set()
    arp = arp_table()
    for ip in arp:
        try:
            if ipaddress.ip_address(ip) in net:
                alive.add(ip)
        except ValueError:
            pass

    hosts = [str(h) for h in net.hosts()]
    if len(hosts) > 1024:  # don't blow up on a /16
        hosts = hosts[:1024]

    lock = threading.Lock()
    from collections import deque
    queue = deque(hosts)

    def worker() -> None:
        while True:
            try:
                host = queue.popleft()
            except IndexError:
                return
            for p in probe_ports:
                if tcp_open(host, p, timeout):
                    with lock:
                        alive.add(host)
                    break

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # the gateway is almost certainly the camera in AP mode; add it if it is in this subnet
    for gw in default_gateways():
        try:
            if ipaddress.ip_address(gw) in net:
                alive.add(gw)
        except ValueError:
            pass

    # drop broadcast/network addresses: they are easy to pick up from the ARP table
    clean = []
    for ip in alive:
        last = int(ip.split(".")[-1])
        if last in (0, 255):
            continue
        clean.append(ip)
    return sorted(clean, key=lambda s: tuple(int(x) for x in s.split(".")))


def udp_state(host: str, port: int, timeout: float = 0.3,
              payload: bytes = b"\x00" * 8) -> tuple[str, bytes]:
    """State of a UDP port: ('open'|'closed'|'silent', reply).

    On Windows/Linux a closed port sends ICMP port unreachable, and recv()
    fails with ConnectionResetError/ConnectionRefusedError. A timeout means
    the port is listening (or filtered) — that is, a service is there.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        s.send(payload)
        try:
            data = s.recv(2048)
            return "open", data
        except ConnectionResetError:
            return "closed", b""
        except ConnectionRefusedError:
            return "closed", b""
        except socket.timeout:
            return "silent", b""
        except OSError as exc:
            if getattr(exc, "errno", None) in (111, 104, 10054, 10061):
                return "closed", b""
            return "silent", b""
    finally:
        s.close()


def udp_sweep(host: str, ports, timeout: float = 0.3, workers: int = 128,
              baseline_ports=(9, 54321)) -> tuple[dict[int, str], bool]:
    """Scan UDP ports. Returns ({port: state}, whether_it_is_informative).

    If even known-closed ports (baseline) do not answer with ICMP, the method
    is useless: all ports will be "silent". Then the second value is False.
    """
    from collections import deque

    informative = True
    for port in baseline_ports:
        state, _ = udp_state(host, port, timeout)
        if state == "silent":
            informative = False
            break

    result: dict[int, str] = {}
    lock = threading.Lock()
    queue = deque(ports)

    def worker() -> None:
        while True:
            try:
                port = queue.popleft()
            except IndexError:
                return
            state, _data = udp_state(host, port, timeout)
            if state != "closed":
                with lock:
                    result[port] = state

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return dict(sorted(result.items())), informative


def udp_range(start: int = 1, end: int = 4096):
    return range(start, end + 1)


def _same_subnet(a: str, b: str) -> bool:
    try:
        na = ipaddress.ip_network(f"{a}/24", strict=False)
        return ipaddress.ip_address(b) in na
    except ValueError:
        return False


def route_check(target: str) -> dict:
    """Which address traffic to target will use, and whether it is intercepted by a VPN."""
    src = local_ipv4_for(target)
    candidates = [ip for ip in all_local_ipv4() if _same_subnet(ip, target)]
    return {
        "target": target,
        "source_ip": src,
        "same_subnet": bool(src and _same_subnet(src, target)),
        "candidate_local": candidates,
    }


def route_warning(target: str) -> str | None:
    """Warning if traffic to the camera leaves through the wrong network.

    Two different cases: (1) the machine has no address in the camera's network at all —
    usually this means the PC is not connected to the camera's access point;
    (2) the address exists, but VPN routes divert the traffic.
    """
    info = route_check(target)
    if info["same_subnet"] or not info["source_ip"]:
        return None
    if not info["candidate_local"]:
        return (
            f"на машине нет адреса в сети камеры ({target}/24): трафик пойдёт через "
            f"{info['source_ip']} и до камеры не дойдёт.\n"
            f"  Обычно это значит, что ПК не подключён к точке доступа камеры "
            f"(Wi-Fi выключен или выбрана другая сеть).\n"
            f"  Что делать: подключиться к Wi-Fi камеры — профиль обычно называется "
            f"как её DID."
        )
    local = info["candidate_local"][0]
    return (
        f"маршрут к {target} идёт с адреса {info['source_ip']}, а не с {local}.\n"
        f"  Скорее всего активен VPN и перехватывает подсеть камеры — пакеты до неё не дойдут.\n"
        f"  Что делать: отключить VPN либо добавить маршрут (нужны права администратора):\n"
        f"    route add {target} mask 255.255.255.255 {local} metric 1"
    )


def wifi_ssid() -> str | None:
    """Name of the Wi-Fi network the machine is connected to (if it can be determined)."""
    if IS_WINDOWS:
        text = run(["netsh", "wlan", "show", "interfaces"])
        m = re.search(r"^\s*SSID\s*:\s*(.+)$", text, re.M)
        if m and m.group(1).strip():
            return m.group(1).strip()
    elif sys.platform == "darwin":
        text = run(["networksetup", "-getairportnetwork", "en0"])
        m = re.search(r":\s*(.+)$", text)
        if m:
            return m.group(1).strip()
    else:
        text = run(["iwgetid", "-r"]) or run(["iw", "dev"])
        line = text.strip().splitlines()[0] if text.strip() else ""
        if line and " " not in line:
            return line
        m = re.search(r"SSID:\s*(.+)", text)
        if m:
            return m.group(1).strip()
    return None


def environment_report() -> dict:
    """Summary of the network environment: addresses, gateways, whether the camera AP is connected."""
    ips = all_local_ipv4()
    gws = default_gateways()
    detailed = []
    ap_ip = None
    for ip in ips:
        kind, note = classify_ap(ip)
        detailed.append({"ip": ip, "kind": kind, "note": note})
        if kind == "device_ap" and ap_ip is None:
            ap_ip = ip
    primary = ap_ip or (ips[0] if ips else None)
    kind, note = classify_ap(primary)
    return {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "local_ipv4": ips,
        "interfaces": detailed,
        "primary": primary,
        "ap_ip": ap_ip,
        "ssid": wifi_ssid(),
        "gateways": gws,
        "ap_kind": kind,
        "ap_note": note,
        "arp_entries": len(arp_table()),
    }
