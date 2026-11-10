"""End-to-end service test: PPPP emulator → service → HTTP/WS/RTSP clients.

Run from the repository/package root:
    python tests/test_service.py

No external hardware is needed: fake cameras run on loopback, the PPPP endpoint
uses a free port; fixtures live in tests/fixtures.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import struct
import sys
import time
import types
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
ROOT = PKG
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
# The repository root may be cloned under any directory name (LookCam,
# lookcam_deepsik, ...). Expose it as the `lookcam_deepsik` package so the
# existing absolute imports and relative `..vendor` imports work standalone.
if "lookcam_deepsik" not in sys.modules:
    _pkg = types.ModuleType("lookcam_deepsik")
    _pkg.__path__ = [ROOT]
    _pkg.__package__ = "lookcam_deepsik"
    sys.modules["lookcam_deepsik"] = _pkg

from fake_pppp_service import CAM_PPS, CAM_SPS, FakePpppCamera  # noqa: E402
from lookcam_deepsik.vendor.lookcam import rtsp as rtsp_client  # noqa: E402
from lookcam_deepsik.vendor.lookcam import sps as sps_mod  # noqa: E402
from lookcam_deepsik.service import auth, commands, config as config_mod  # noqa: E402
from lookcam_deepsik.service import api as api_mod  # noqa: E402
from lookcam_deepsik.service import fmp4, frames, hub, proto  # noqa: E402
from lookcam_deepsik.service import mp4_index  # noqa: E402
from lookcam_deepsik.service import rtsp_server  # noqa: E402
from lookcam_deepsik.service import sdcard as sdcard_mod  # noqa: E402
from lookcam_deepsik.service import snapshot as snapshot_mod  # noqa: E402
from lookcam_deepsik.service.main import ServiceApp  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
TMP = os.path.join(HERE, "_service_tmp")


def check(name: str, cond, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), str(detail)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""), flush=True)


# ---------------------------------------------------------------------- HTTP

BASE = ""


def http_json(method: str, path: str, body=None, headers=None, raw=False,
              base: str | None = None):
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request((base or BASE) + path, data=data, method=method,
                                 headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read()
            if raw:
                return resp.status, payload, dict(resp.headers)
            return resp.status, json.loads(payload or b"{}"), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        if raw:
            return exc.code, payload, dict(exc.headers)
        try:
            return exc.code, json.loads(payload or b"{}"), dict(exc.headers)
        except ValueError:
            return exc.code, {"error": payload.decode("utf-8", "replace")}, dict(exc.headers)


async def ahttp(method: str, path: str, body=None, headers=None, raw=False,
                base: str | None = None):
    return await asyncio.to_thread(http_json, method, path, body, headers, raw, base)


# ---------------------------------------------------------------------- WebSocket

class WS:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.closed = False

    @classmethod
    async def connect(cls, port: int, path: str):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                   f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        writer.write(request.encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        if b" 101 " not in head:
            raise RuntimeError(f"WS handshake: {head[:120]!r}")
        return cls(reader, writer)

    async def _send(self, opcode: int, data: bytes = b"") -> None:
        header = bytearray([0x80 | opcode])
        if len(data) < 126:
            header.append(0x80 | len(data))
        else:
            header.append(0x80 | 126)
            header += struct.pack(">H", len(data))
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
        self.writer.write(bytes(header) + mask + masked)
        await self.writer.drain()

    async def recv(self, timeout: float = 5.0):
        while True:
            head = await asyncio.wait_for(self.reader.readexactly(2), timeout)
            b0, b1 = head[0], head[1]
            opcode = b0 & 0x0F
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", await self.reader.readexactly(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", await self.reader.readexactly(8))[0]
            data = await self.reader.readexactly(length) if length else b""
            if opcode == 0x9:
                await self._send(0xA, data)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                self.closed = True
                return None
            return opcode, data

    async def close(self) -> None:
        if self.closed:
            return
        try:
            await self._send(0x8, struct.pack(">H", 1000))
        except Exception:
            pass
        self.closed = True
        try:
            self.writer.close()
        except Exception:
            pass


async def collect(ws: WS, seconds: float) -> list[tuple[int, bytes]]:
    out = []
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while loop.time() < end:
        try:
            item = await ws.recv(max(0.2, min(2.0, end - loop.time())))
        except asyncio.TimeoutError:
            continue                    # silence is normal (the camera is offline)
        except (asyncio.IncompleteReadError, ConnectionError):
            break
        if item is None:
            break
        out.append(item)
    return out


def ws_messages(msgs: list[tuple[int, bytes]]) -> list[dict]:
    """Parse binary [type][flags][ts] messages of the WS player."""
    out = []
    for opcode, data in msgs:
        if opcode != 0x2 or len(data) < 6:
            continue
        kind, flags, ts = struct.unpack(">BBI", data[:6])
        out.append({"kind": kind, "flags": flags, "ts": ts, "payload": data[6:]})
    return out


# ---------------------------------------------------------------------- helpers

def au_from_frame(frame: bytes) -> tuple[int, bool, bytes]:
    hdr = proto.parse_video_header(frame)
    assert hdr is not None
    return hdr.ts_ms, hdr.key, frame[proto.VIDEO_HEADER:proto.VIDEO_HEADER + hdr.payload_len]


# ---------------------------------------------------------------------- test

async def main_async() -> int:
    global BASE
    if os.path.isdir(TMP):
        shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)

    sd_data = bytes((i * 37 + 11) & 0xFF for i in range(30000))
    sd_name = "/mnt/CYC_DV/2026/11/04/2026-1104-205905.00302486.mp4"
    cam_a = FakePpppCamera(did="GHBB016638JHDXM", password="265600",
                           sd_files={sd_name: sd_data},
                           value=0x40FE).start()
    cam_b = FakePpppCamera(did="GHBB099999ABCDE", password="111111",
                           value=99999).start()
    print(f"эмуляторы: A :{cam_a.port}, B :{cam_b.port}", flush=True)

    cfg = config_mod.ConfigStore(os.path.join(TMP, "lookcam_service.json"), data={
        "http": {"bind": "127.0.0.1", "port": 0},
        "rtsp": {"enable": True, "bind": "127.0.0.1", "port": 0,
                 "user": "viewer", "password_hash": "", "realm": "LookCam"},
        "admin": {"auth": False},
        "cache_dir": TMP,
        "idle_ttl_s": 2,
        "cameras": [
            {"id": "cam-a", "name": "Гараж", "did": "G016638JHDXM",
             "password": "265600", "quality": "HD", "keep_alive": False},
            {"id": "cam-b", "name": "Двор", "did": "G099999ABCDE",
             "password": "111111", "quality": "SD", "keep_alive": False},
        ],
    }, autosave=False)
    app = ServiceApp(cfg, http_port=0, rtsp_port=0, bind="127.0.0.1",
                     pppp_port=0, public_host="127.0.0.1")
    await app.start()
    app.endpoint.add_target("127.0.0.1", cam_a.port)
    app.endpoint.add_target("127.0.0.1", cam_b.port)
    BASE = f"http://127.0.0.1:{app.http.port}"
    try:
        await run_checks(app, cam_a, cam_b, sd_data, sd_name)
    finally:
        await app.close()
        cam_a.stop()
        cam_b.stop()
    # Shutdown must not leave camera timers pending when asyncio.run closes
    # the loop (the Ctrl+C "Task was destroyed but it is pending" case).
    cfg_shutdown = config_mod.ConfigStore(
        os.path.join(TMP, "shutdown.json"),
        data={"http": {"bind": "127.0.0.1", "port": 0},
              "rtsp": {"enable": False, "port": 0},
              "admin": {"auth": False}, "cache_dir": TMP, "cameras": []},
        autosave=False)
    app_shutdown = ServiceApp(cfg_shutdown, http_port=0, rtsp_port=0,
                              bind="127.0.0.1", pppp_port=0)
    await app_shutdown.start()
    pending = app_shutdown.manager.schedule(asyncio.sleep(60))
    await app_shutdown.close()
    check("shutdown: pending manager task отменён и дождан",
          pending.cancelled())
    return 0


async def run_checks(app: ServiceApp, cam_a: FakePpppCamera, cam_b: FakePpppCamera,
                     sd_data: bytes, sd_name: str) -> None:
    # 1. HTTP/state/cameras
    status, state, _ = await ahttp("GET", "/api/state")
    check("HTTP /api/state отвечает", status == 200 and state.get("authorized") is True,
          f"status={status}")
    status, cams, _ = await ahttp("GET", "/api/cameras")
    check("список камер: две", status == 200 and len(cams.get("cameras") or []) == 2,
          str(len(cams.get("cameras") or [])))

    # 2. Two WS players in parallel
    ws_a = await WS.connect(app.http.port, "/ws/live/cam-a")
    ws_b = await WS.connect(app.http.port, "/ws/live/cam-b")
    msgs_a, msgs_b = await asyncio.gather(collect(ws_a, 6), collect(ws_b, 6))
    parsed_a = ws_messages(msgs_a)
    parsed_b = ws_messages(msgs_b)
    cfg_a = next((m for m in parsed_a if m["kind"] == 2), None)
    cfg_b = next((m for m in parsed_b if m["kind"] == 2), None)
    check("WS camA: пришёл config с кодеком",
          bool(cfg_a) and json.loads(cfg_a["payload"]).get("codec", "").startswith("avc1."),
          cfg_a["payload"][:80].decode("utf-8", "replace") if cfg_a else "нет")
    check("WS camB: пришёл config", bool(cfg_b),
          str(json.loads(cfg_b["payload"]).get("codec")) if cfg_b else "нет")
    video_a = [m for m in parsed_a if m["kind"] == 1]
    video_b = [m for m in parsed_b if m["kind"] == 1]
    check("WS camA: идут видеокадры", len(video_a) >= 5, f"{len(video_a)} шт.")
    check("WS camB: идут видеокадры параллельно", len(video_b) >= 3, f"{len(video_b)} шт.")
    check("WS camA: есть ключевой кадр", any(m["flags"] & 1 for m in video_a))
    check("WS camB: есть ключевой кадр", any(m["flags"] & 1 for m in video_b))

    # 2.1. MSE path: init segment + fMP4 fragments
    ws_mp4 = await WS.connect(app.http.port, "/ws/live-mp4/cam-a")
    mp4_msgs = await collect(ws_mp4, 3.0)
    await ws_mp4.close()
    binaries = [data for opcode, data in mp4_msgs if opcode == 0x2]
    check("WS live-mp4: пришёл init-сегмент ftyp+moov",
          bool(binaries) and binaries[0][4:8] == b"ftyp"
          and fmp4.find_box(binaries[0], b"moov") is not None,
          f"{len(binaries)} бинарных сообщений")
    check("WS live-mp4: пришли moof/mdat-фрагменты",
          any(data[4:8] == b"moof" for data in binaries[1:]),
          f"{len(binaries) - 1} фрагмент(ов)")


    # 3. Byte-for-byte identity of P-frames with the camera's source AUs
    fake_au: dict[int, tuple[bool, bytes]] = {}
    for frame in cam_a.sent_video:
        ts, key, payload = au_from_frame(frame)
        fake_au[ts] = (key, payload)
    matched = 0
    mismatched = 0
    mismatch_detail = ""
    for m in video_a:
        item = fake_au.get(m["ts"])
        if not item:
            continue
        key, payload = item
        if key:
            continue
        if m["payload"] == payload:
            matched += 1
        else:
            mismatched += 1
            if not mismatch_detail:
                mismatch_detail = (f"ts={m['ts']} flags={m['flags']} "
                                   f"{len(m['payload'])}/{len(payload)} "
                                   f"{m['payload'][:8].hex()}/{payload[:8].hex()}")
    check("WS camA: P-кадры совпадают с AU камеры побайтно",
          matched >= 3 and mismatched == 0,
          f"совпало {matched}, нет {mismatched} {mismatch_detail}")

    # 3.1. Manual fMP4 recording: start → frames → stop → the file parses
    status, rec_start, _ = await ahttp("POST", "/api/cameras/cam-a/record", {})
    check("запись: старт принят", status == 201, f"status={status}")
    await asyncio.sleep(1.5)
    status, rec_stop, _ = await ahttp("DELETE", "/api/cameras/cam-a/record")
    rec = (rec_stop or {}).get("record") or {}
    rec_path = rec.get("path", "")
    rec_data = b""
    if rec_path and os.path.isfile(rec_path):
        with open(rec_path, "rb") as fh:
            rec_data = fh.read()
    rec_info = fmp4.parse_init(rec_data) if rec_data else {}
    rec_frags = fmp4.parse_fragments(rec_data) if rec_data else []
    check("запись: файл fMP4 с init и кадрами",
          status == 200 and rec.get("state") == "done" and rec_info.get("sps")
          and len(rec_frags) >= 3,
          f"status={status}, кадров {len(rec_frags)}, SPS {len(rec_info.get('sps', b''))} б")


    # 4. Settings: read → write → read
    status, image, _ = await ahttp("GET", "/api/cameras/cam-a/settings/image")
    check("настройки image прочитаны", status == 200
          and image.get("values", {}).get("brightness") == 30,
          str(image.get("values")))
    status, res, _ = await ahttp("PUT", "/api/cameras/cam-a/settings/image",
                                 {"values": {"brightness": 31}})
    check("настройки image записаны", status == 200
          and res.get("sent", {}).get("brightness") == 31,
          f"status={status} {json.dumps(res, ensure_ascii=False)[:200]}")
    _, image2, _ = await ahttp("GET", "/api/cameras/cam-a/settings/image")
    check("настройки image прочитаны обратно", image2.get("values", {}).get("brightness") == 31,
          str(image2.get("values")))
    status, info, _ = await ahttp("GET", "/api/cameras/cam-a/info")
    dev = info.get("device") or {}
    check("сведения о камере: GetDevInfo/GetDevStream",
          status == 200 and dev.get("ver") and info.get("streams", {}).get("mainstream"),
          str(dev.get("ver")))
    check("секрет wifipwd вырезан из API", dev.get("wifipwd") in (None, "***"),
          repr(dev.get("wifipwd")))
    toggle_ok = True
    for action in ("led", "light"):
        _, first, _ = await ahttp("POST", f"/api/cameras/cam-a/actions/{action}", {})
        _, second, _ = await ahttp("POST", f"/api/cameras/cam-a/actions/{action}", {})
        toggle_ok = toggle_ok and first.get("value") == 0 and second.get("value") == 1
    check("LED/IR: кнопки переключают состояние", toggle_ok)

    # close the players: live video would interfere with exclusive download
    await ws_a.close()
    await ws_b.close()
    await asyncio.sleep(0.6)

    # 5. SD: list → download → SHA-1 → Range
    status, listing, _ = await ahttp("GET", "/api/cameras/cam-a/sd")
    files = listing.get("files") or []
    check("SD: список файлов", status == 200 and len(files) == 1
          and files[0].get("size") == len(sd_data), str(files)[:160])
    status, dl, _ = await ahttp("POST", "/api/cameras/cam-a/sd/download",
                                {"patch": sd_name, "name": "record.mp4",
                                 "size": len(sd_data)})
    number = (dl.get("download") or {}).get("n")
    check("SD: скачивание поставлено в очередь", status == 202 and number,
          str(dl)[:160])
    done = None
    for _ in range(120):
        _, downloads, _ = await ahttp("GET", "/api/downloads")
        row = next((d for d in downloads.get("downloads", []) if d["n"] == number), None)
        if row and row.get("state") in ("done", "error"):
            done = row
            break
        await asyncio.sleep(0.25)
    check("SD: скачивание завершилось", bool(done) and done.get("state") == "done",
          str(done)[:200] if done else "таймаут")
    if done:
        path = os.path.join(app.config.cache_dir(), "sd", "cam-a", done["file"])
        got = open(path, "rb").read()
        check("SD: SHA-1 совпадает с исходным файлом",
              hashlib.sha1(got).hexdigest() == hashlib.sha1(sd_data).hexdigest(),
              hashlib.sha1(got).hexdigest())
        status, body, headers = await ahttp(
            "GET", f"/media/cam-a/{done['file']}",
            headers={"Range": "bytes=0-99"}, raw=True)
        check("SD: /media отдаёт Range 206 и верные байты",
              status == 206 and body == sd_data[:100],
              f"status={status}, {len(body)} байт")

    # 5.1. Acks for channels 0/1/3 and the batch size limit
    ack_channels = {ch for ch, nums in cam_a.acks if nums}
    too_big = [len(nums) for _ch, nums in cam_a.acks if len(nums) > 32]
    check("ack: каналы 0/1/3 подтверждаются, пачки <=32",
          {0, 1, 3} <= ack_channels and not too_big,
          f"каналы {sorted(ack_channels)}, слишком большие {too_big[:5]}")

    # 6. RTSP: TCP and UDP, byte-for-byte NAL comparison
    fake_p: set[bytes] = set()
    for frame in cam_a.sent_video:
        _ts, key, payload = au_from_frame(frame)
        if not key:
            fake_p.add(payload)
    for transport in ("tcp", "udp"):
        url = f"rtsp://127.0.0.1:{app.rtsp.port}/cam-a"
        session = rtsp_client.RtspSession(url, transport=transport, timeout=8.0)
        try:
            media = await asyncio.to_thread(session.open, "video")
            rows = []
            deadline = time.monotonic() + 30
            gen = session.frames()
            while len(rows) < 12 and time.monotonic() < deadline:
                try:
                    item = await asyncio.wait_for(asyncio.to_thread(next, gen), 8)
                except StopIteration:
                    break
                except asyncio.TimeoutError:
                    break
                rows.append(item)
            annexb = [data for kind, data, _ts in rows if kind == "annexb"]
            check(f"RTSP {transport}: DESCRIBE/SETUP/PLAY и кадры",
                  media is not None and len(annexb) >= 5,
                  f"{len(annexb)} NAL, кодек {media.codec if media else '—'}")
            check(f"RTSP {transport}: P-NAL совпадает с AU камеры",
                  any(data in fake_p for data in annexb),
                  f"из {len(annexb)}")
        except Exception as exc:
            check(f"RTSP {transport}: подключение", False, repr(exc))
        finally:
            await asyncio.to_thread(session.teardown)

    # 7. fMP4: init and reverse parsing of fragments
    frame = cam_a.sent_video[-1]
    _ts, key, payload = au_from_frame(frame)
    writer = fmp4.FMP4Writer(CAM_SPS, CAM_PPS, 1920, 1080)
    frag = writer.fragment(payload, key)
    info = fmp4.parse_init(writer.init)
    parsed = fmp4.parse_fragments(frag)
    check("fMP4: avcC содержит SPS/PPS и размеры",
          info["sps"] == CAM_SPS and info["pps"] == CAM_PPS
          and (info["width"], info["height"]) == (1920, 1080),
          f"{info['width']}x{info['height']}, SPS {len(info['sps'])} б")
    check("fMP4: moof/mdat разбираются обратно в тот же AU",
          bool(parsed) and fmp4.avcc_to_annexb(parsed[0]["sample"]) == payload,
          f"{len(parsed)} фрагмент(ов)")

    # 8. Hub: queue policy
    h = hub.StreamHub("test")
    key_au = (b"\x00\x00\x00\x01" + CAM_SPS + b"\x00\x00\x00\x01" + CAM_PPS
              + b"\x00\x00\x00\x01\x25x")
    h.publish(frames.AccessUnit(kind="video", payload=key_au,
                                key=True, ts_ms=1, width=1920, height=1088))
    sub = h.subscribe("slow")
    for i in range(40):
        h.publish(frames.AccessUnit(kind="video", payload=b"\x00\x00\x00\x01\x21" + bytes([i]) * 10,
                                    key=False, ts_ms=10 + i))
    check("хаб: медленный подписчик дропает и ждёт IDR",
          sub.wait_key and sub.dropped > 0,
          f"wait_key={sub.wait_key}, dropped={sub.dropped}")

    # 9. Auth: PBKDF2 and cookie signing
    stored = auth.hash_password("secret123")
    check("auth: пароль PBKDF2 проверяется",
          auth.verify_password("secret123", stored)
          and not auth.verify_password("wrong", stored))
    cookie = auth.sign_cookie("k" * 32, 60)
    check("auth: cookie подписана и проверяется",
          auth.verify_cookie("k" * 32, cookie)
          and not auth.verify_cookie("x" * 32, cookie))
    check("auth: setup ограничен loopback",
          api_mod._is_loopback("127.0.0.1")
          and api_mod._is_loopback("::1")
          and api_mod._is_loopback("::ffff:127.0.0.1")
          and not api_mod._is_loopback("192.168.0.10"))
    cfg_rtsp = config_mod.ConfigStore(
        os.path.join(TMP, "rtsp_policy.json"),
        data={"rtsp": {"enable": True, "bind": "0.0.0.0",
                       "port": 0, "password_hash": ""}}, autosave=False)
    blocked = False
    try:
        await rtsp_server.RtspServer(
            cfg_rtsp, None, bind="0.0.0.0", port=0).start()
    except rtsp_server.RtspError:
        blocked = True
    check("RTSP: неслушающий loopback без пароля запрещён", blocked)

    # 9.1. RTSP Digest server ↔ client
    ha1 = auth.digest_ha1("viewer", "LookCam", "секрет")
    realm = auth.DigestRealm("viewer", ha1, "LookCam")
    nonce = realm.new_nonce()
    uri = "rtsp://127.0.0.1:8554/cam-a"
    response = auth.digest_response(ha1, "DESCRIBE", uri, nonce, "auth",
                                    "00000001", "abcdef")
    header = (f'Digest username="viewer", realm="LookCam", nonce="{nonce}", '
              f'uri="{uri}", qop=auth, nc=00000001, cnonce="abcdef", '
              f'response="{response}"')
    check("auth: RTSP Digest-ответ проверяется", realm.verify("DESCRIBE", uri, header))
    check("auth: неверный Digest отклоняется",
          not realm.verify("DESCRIBE", uri, header.replace('viewer', 'other')))

    # 9.2. Config: first run, admin password, secrets in public()
    cfg2 = config_mod.ConfigStore(os.path.join(TMP, "auth_service.json"), data={
        "admin": {"auth": True},
        "cameras": [{"id": "cam-a", "did": "G016638JHDXM", "password": "265600"}],
    }, autosave=False)
    a2 = auth.Auth(cfg2)
    check("config: первый запуск требует пароль", a2.needs_setup)
    cfg2.set_admin_password("secret123")
    check("config: пароль админа сохранён и проверяется",
          not cfg2.needs_setup and a2.check_password("secret123"))
    public = cfg2.public()
    check("config: public() не отдаёт секреты",
          "password" not in (public["cameras"][0] if public["cameras"] else {})
          and "password_hash" not in public["admin"])


    # 10. Idle-TTL: all viewers left → session closed, Close sent
    deadline = time.time() + 10
    cam_state = {}
    while time.time() < deadline:
        _, cams, _ = await ahttp("GET", "/api/cameras")
        cam_state = next((c for c in cams.get("cameras", [])
                          if c["id"] == "cam-a"), {})
        if cam_a.close_count >= 2 and cam_state.get("state") == "offline":
            break
        await asyncio.sleep(0.3)
    check("idle-TTL: после ухода зрителей камера получила Close дважды",
          cam_a.close_count >= 2, f"Close: {cam_a.close_count}")
    check("idle-TTL: камера в состоянии offline", cam_state.get("state") == "offline",
          str(cam_state.get("state")))

    # 10.1. Wi-Fi wizard: start → network list → apply → camera is online again
    status, prov, _ = await ahttp("POST", "/api/provision/start",
                                  {"did": "G099999ABCDE",
                                   "camera_password": "111111"})
    check("Wi-Fi мастер: старт принят", status == 200, f"status={status}")
    prov_state = {}
    deadline = time.time() + 20
    while time.time() < deadline:
        _, prov_state, _ = await ahttp("GET", "/api/provision/state")
        if prov_state.get("step") in ("choose", "error"):
            break
        await asyncio.sleep(0.4)
    before_packets = app.endpoint.packets_in
    app.endpoint.send_lan_search()
    await asyncio.sleep(0.5)
    check("Wi-Fi мастер: камера отдала список сетей",
          prov_state.get("step") == "choose"
          and len(prov_state.get("networks") or []) >= 2,
          f"step={prov_state.get('step')}, сетей "
          f"{len(prov_state.get('networks') or [])}, "
          f"fake_b alive={cam_b._thread.is_alive() if cam_b._thread else None}, "
          f"last={cam_b.last_command}, req={len(cam_b.requests)}, "
          f"ep={app.endpoint.port}, in={app.endpoint.packets_in - before_packets}")
    status, _, _ = await ahttp("POST", "/api/provision/apply",
                               {"ssid": "HomeNet", "password": "secret-net"})
    check("Wi-Fi мастер: apply принят", status == 200, f"status={status}")
    deadline = time.time() + 40
    while time.time() < deadline:
        _, prov_state, _ = await ahttp("GET", "/api/provision/state")
        if prov_state.get("step") in ("done", "error"):
            break
        await asyncio.sleep(0.5)
    check("Wi-Fi мастер: камера переподключилась после OpenWifi",
          prov_state.get("step") == "done",
          f"step={prov_state.get('step')}, error={prov_state.get('error')}")
    check("Wi-Fi мастер: OpenWifi ушёл с нужным SSID",
          cam_b.wifi_requested is not None
          and cam_b.wifi_requested.get("sid") == "HomeNet",
          str(cam_b.wifi_requested))

    # 10.2. Camera restart: the service reconnects itself and restores the stream
    ws_re = await WS.connect(app.http.port, "/ws/live/cam-a")
    before = ws_messages(await collect(ws_re, 3.0))
    check("переподключение: поток был до перезапуска",
          len([m for m in before if m["kind"] == 1]) >= 2,
          f"{len([m for m in before if m['kind'] == 1])} кадров")
    cam_a._sendto(proto.close_packet(), cam_a.session_addr)
    cam_a.stop()
    await asyncio.sleep(1.0)
    cam_a2 = FakePpppCamera(did="GHBB016638JHDXM", password="265600",
                            value=0x40FE, port=cam_a.port).start()
    try:
        after = ws_messages(await collect(ws_re, 12.0))
        video_after = [m for m in after if m["kind"] == 1]
        _, cams_now, _ = await ahttp("GET", "/api/cameras")
        state_now = next((c for c in cams_now.get("cameras", [])
                          if c["id"] == "cam-a"), {})
        check("переподключение: поток вернулся после перезапуска камеры",
              len(video_after) >= 3 and cam_a2.frames_sent > 0,
              f"WS {len(video_after)} кадров, камера {cam_a2.frames_sent}, "
              f"state={state_now.get('state')}, err={state_now.get('error')}")
    finally:
        cam_a2.stop()
        await ws_re.close()


    # 11. HTTP auth of a separate instance: 401 → login → cookie → 200
    cfg_auth = config_mod.ConfigStore(os.path.join(TMP, "auth_http.json"), data={
        "http": {"bind": "127.0.0.1", "port": 0},
        "rtsp": {"enable": False, "port": 0},
        "admin": {"auth": True},
        "cache_dir": TMP,
        "cameras": [],
    }, autosave=False)
    cfg_auth.set_admin_password("secret123")
    app_auth = ServiceApp(cfg_auth, http_port=0, rtsp_port=0, bind="127.0.0.1",
                          pppp_port=0)
    await app_auth.start()
    base2 = f"http://127.0.0.1:{app_auth.http.port}"
    try:
        status, _, _ = await ahttp("GET", "/api/cameras", base=base2)
        check("auth HTTP: без cookie — 401", status == 401, f"status={status}")
        status, _, headers = await ahttp("POST", "/api/login",
                                         {"password": "secret123"}, base=base2)
        cookie = ""
        for key, value in (headers or {}).items():
            if key.lower() == "set-cookie":
                cookie = value.split(";")[0]
                break
        check("auth HTTP: вход выдаёт cookie", status == 200 and bool(cookie),
              f"status={status}, cookie={bool(cookie)}")
        status, cams_auth, _ = await ahttp("GET", "/api/cameras",
                                           headers={"Cookie": cookie}, base=base2)
        check("auth HTTP: с cookie доступ разрешён", status == 200
              and isinstance(cams_auth.get("cameras"), list), f"status={status}")
        status, _, _ = await ahttp("POST", "/api/login",
                                   {"password": "wrong"}, base=base2)
        check("auth HTTP: неверный пароль — 401", status == 401, f"status={status}")
    finally:
        await app_auth.close()



def unit_checks() -> None:
    os.makedirs(TMP, exist_ok=True)
    # msg_id and packets
    pkt = proto.data_packet({"cmd": "GetDevInfo"}, msg_id=7)
    check("proto: data_packet F1 D0 и msg_id", pkt[0] == 0xF1 and pkt[1] == 0xD0
          and pkt[7] == 7, pkt[:8].hex(" "))
    punch = proto.punch_packet("G016638JHDXM")
    check("proto: punch_packet из DID", punch == bytes.fromhex(
        "f1410014" "47484242" "00000000" "000040fe" "4a484458" "4d000000"),
        punch.hex(" "))
    acks = proto.ack_packets(range(70))
    check("proto: ack режется на пачки <=32", len(acks) == 3
          and all(len(a) <= 72 for a in acks),
          str([len(a) for a in acks]))
    check("proto: lenient_int принимает строку", proto.lenient_int("42") == 42
          and proto.lenient_int("4.9") == 4 and proto.lenient_int(None, 5) == 5)
    check("proto: DID виртуальный -> реальный",
          proto.real_did("G016638JHDXM") == "GHBB016638JHDXM")
    check("proto: DID восстанавливается из PunchPkt",
          proto.did_from_punch(b"GHBB", b"JHDXM", 0x40FE) == "G016638JHDXM"
          and proto.did_from_punch(b"GHBB", b"URFKG", 0x867C8) == "G550856URFKG",
          proto.did_from_punch(b"GHBB", b"URFKG", 0x867C8))
    from fake_pppp_service import make_media_frame
    offsets = {proto.annexb_offset(make_media_frame(False, i, 123456 + i * 100))
               for i in range(20)}
    check("proto: находит Annex B за заголовком 83 при ложных start-кодах",
          offsets == {83}, str(offsets))
    check("proto: секреты вырезаются",
          proto.scrub_secrets({"wifipwd": "x", "a": {"pwd": "y", "b": 1}}) ==
          {"wifipwd": "***", "a": {"pwd": "***", "b": 1}})

    # SPS is patched (already checked in test_units; here we check the hub does it)
    fixed = sps_mod.patch(CAM_SPS)
    check("sps: патч объявляет max_num_reorder_frames=0",
          sps_mod.describe(fixed).get("max_num_reorder_frames") == 0)

    # command registry
    check("commands: image валидирует диапазон",
          commands.validate_values("image", {"brightness": 30})["brightness"] == 30)
    try:
        commands.validate_values("image", {"brightness": 99})
        bad = False
    except ValueError:
        bad = True
    check("commands: диапазон вне границ отклоняется", bad)
    check("commands: реестр содержит группы",
          {"image", "light", "alarm", "tf", "power", "exposure"} <=
          set(commands.schema()["groups"]))
    schema = commands.schema()
    check("commands: английские подписи для UI",
          all(g.get("title_en") for g in schema["groups"].values())
          and all(f.get("label_en") for g in schema["groups"].values()
                  for f in g.get("fields", []))
          and all(a.get("label_en") for a in schema["actions"].values()))

    # resume .temp: an incomplete block is cut off, otherwise the MP4 is glued with a shift
    temp = os.path.join(TMP, "resume.temp")
    with open(temp, "wb") as fh:
        fh.write(b"x" * (sdcard_mod.DOWNLOAD_BLOCK * 2 + 100))
    aligned = sdcard_mod.SDCard._resume_offset(temp, 10 * 1024 * 1024)
    check("SD: докачка выравнивается по 6096-байтному блоку",
          aligned == sdcard_mod.DOWNLOAD_BLOCK * 2
          and os.path.getsize(temp) == aligned,
          f"{aligned}")
    os.remove(temp)

    # Sparse seek: range bookkeeping and MP4 index parsing.
    ranges = sdcard_mod.SDCard._merge_range([], 100, 200)
    ranges = sdcard_mod.SDCard._merge_range(ranges, 150, 300)
    ranges = sdcard_mod.SDCard._merge_range(ranges, 400, 500)
    check("seek: диапазоны объединяются",
          ranges == [[100, 300], [400, 500]]
          and sdcard_mod.SDCard._has_range(ranges, 120, 280)
          and not sdcard_mod.SDCard._has_range(ranges, 350, 450))

    def box(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload) + 8) + kind + payload

    ftyp = box(b"ftyp", b"3gp5" + b"\0\0\0\0")
    mdat_payload = b"a" * 100 + b"b" * 200 + b"c" * 300
    mdat = box(b"mdat", mdat_payload)
    sample_off = len(ftyp) + 8
    mdhd = box(b"mdhd", struct.pack(">IIIII", 0, 0, 0, 1000, 3000))
    hdlr = box(b"hdlr", b"\0" * 4 + b"\0" * 4 + b"vide" + b"\0" * 12)
    stsz = box(b"stsz", struct.pack(">III", 0, 0, 3)
               + struct.pack(">III", 100, 200, 300))
    stco = box(b"stco", struct.pack(">II", 0, 1)
               + struct.pack(">I", sample_off))
    stsc = box(b"stsc", struct.pack(">III", 0, 1, 1)
               + struct.pack(">II", 3, 1))
    stts = box(b"stts", struct.pack(">II", 0, 1) + struct.pack(">II", 3, 1000))
    stss = box(b"stss", struct.pack(">II", 0, 2) + struct.pack(">II", 1, 3))
    stbl = box(b"stbl", stsz + stco + stsc + stts + stss)
    minf = box(b"minf", stbl)
    mdia = box(b"mdia", mdhd + hdlr + minf)
    trak = box(b"trak", mdia)
    moov = box(b"moov", trak)
    fake_mp4 = ftyp + mdat + moov
    parsed = mp4_index.parse_moov(fake_mp4, 0, len(fake_mp4))
    check("seek: MP4 index находит ключевые кадры",
          len(parsed.samples) == 3 and len(parsed.keyframes) == 2
          and parsed.duration_ms == 3000
          and parsed.keyframe_for(1500).time_ms == 0
          and parsed.keyframe_for(2500).time_ms == 2000,
          f"{len(parsed.samples)}/{len(parsed.keyframes)}")
    packet = proto.data_packet({"cmd": "GetDevInfo"}, 300)
    sub = packet[4:]
    check("commands: msg_id 16-битный",
          sub[0:2] == b"\xd1\x01" and struct.unpack_from(">H", sub, 2)[0] == 300,
          sub[:4].hex(" "))

    # Compare the new assembler with the reference FrameWriter on a real capture.
    from lookcam_deepsik.vendor.lookcam import p2p, webcodecs
    capture = os.path.join(HERE, "fixtures", "media_capture.lcraw")
    if os.path.isfile(capture):
        records = list(p2p.read_raw(capture))
        old: list[bytes] = []
        writer = webcodecs.FrameWriter(old.append)
        for _ms, frag, data in records:
            writer.feed(frag, data)
        writer.close()
        new: list[bytes] = []
        assembler = frames.FrameAssembler()
        for _ms, frag, data in records:
            new.extend(au.payload for au in assembler.feed(frag, data))
        new.extend(au.payload for au in assembler.flush())
        # FrameAssembler deliberately cuts off the 4-byte container trailer:
        # otherwise it would leak into the NAL at the RTSP/fMP4 output. The
        # reference FrameWriter keeps it glued to the AU (verified on a live capture).
        bad = [i for i, (a, b) in enumerate(zip(old, new))
               if not (len(a) == len(b) + 4 and a[:len(b)] == b
                       and a[len(b):] == b"\xf4\xf3\xf2\xf1")]
        check("frames: AU = эталонный FrameWriter без трейлера контейнера",
              len(old) == len(new) and not bad,
              f"эталон {len(old)}, новый {len(new)}, расхождений {len(bad)}"
              + (f", первое {bad[0]}" if bad else ""))
    else:
        check("frames: эталонная запись tests/fixtures/media_capture.lcraw",
              True, "пропущено")

    # Snapshot: the last IDR → JPEG via cv2 (if it is available in this Python).
    media_file = os.path.join(HERE, "fixtures", "media.h264")
    snap = snapshot_mod.SnapshotService(TMP)
    if os.path.isfile(media_file) and snap._has_cv2():
        raw = open(media_file, "rb").read()
        sps_positions = []
        pos = raw.find(proto.START_CODE)
        while pos >= 0:
            if pos + 4 < len(raw) and (raw[pos + 4] & 0x1F) == 7:
                sps_positions.append(pos)
            pos = raw.find(proto.START_CODE, pos + 4)
        jpeg = b""
        if len(sps_positions) >= 2:
            part = raw[sps_positions[0]:sps_positions[1]]
            snap_path = os.path.join(TMP, "snapshot_probe.h264")
            with open(snap_path, "wb") as fh:
                fh.write(part)
            jpeg = snap._cv2_frame(snap_path)
        check("снимок: IDR с живой записи декодируется в JPEG",
              jpeg.startswith(b"\xff\xd8\xff") and jpeg.rstrip().endswith(b"\xff\xd9"),
              f"{len(jpeg)} байт")
    else:
        check("снимок: cv2/запись доступны", True, "пропущено")


def main() -> int:
    import logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    unit_checks()
    try:
        asyncio.run(asyncio.wait_for(main_async(), 180))
    except Exception as exc:
        import traceback
        traceback.print_exc()
        check("сквозной сценарий без исключений", False, repr(exc))
    failed = [r for r in RESULTS if not r[1]]
    print(f"\nитог: {len(RESULTS) - len(failed)}/{len(RESULTS)} прошло", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

