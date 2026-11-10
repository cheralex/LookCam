"""Extended live check of the service on camera G550856URFKG.

Run from lookcam_py:
    set LOOKCAM_PASSWORD=...
    python lookcam_deepsik/tools/live_full_check.py

Checks HTTP endpoints, the WebSocket player, RTSP TCP/UDP, reading settings and
SD, snapshot, recording and quality switching. It changes nothing on the camera
(except the stream quality, which is switched back to HD).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PKG))
import _bootstrap  # noqa: E402,F401
from lookcam_deepsik.vendor.lookcam import rtsp as rtsp_client  # noqa: E402
from lookcam_deepsik.service import config as config_mod  # noqa: E402
from lookcam_deepsik.service import fmp4  # noqa: E402
from lookcam_deepsik.service.main import ServiceApp  # noqa: E402
from lookcam_deepsik.tests import test_service as ts  # noqa: E402

DID = os.environ.get("LOOKCAM_DID", "G550856URFKG")
PASSWORD = os.environ.get("LOOKCAM_PASSWORD", "")
HOST = os.environ.get("LOOKCAM_HOST", "192.168.0.147")
if not PASSWORD:
    raise SystemExit("задайте LOOKCAM_PASSWORD (пароль камеры)")
TMP = os.path.join(HERE, "_live_full")
os.makedirs(TMP, exist_ok=True)
os.makedirs(TMP, exist_ok=True)

RESULTS = []


def check(name: str, cond, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), str(detail)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""), flush=True)


async def http(base: str, method: str, path: str, body=None, raw=False):
    return await asyncio.to_thread(ts.http_json, method, path, body, None, raw, base)


async def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config_mod.ConfigStore(os.path.join(TMP, "lookcam_service.json"), data={
        "http": {"bind": "127.0.0.1", "port": 0},
        "rtsp": {"enable": True, "bind": "127.0.0.1", "port": 0,
                 "password_hash": "", "realm": "LookCam"},
        "admin": {"auth": False},
        "cache_dir": TMP,
        "idle_ttl_s": 5,
        "cameras": [{"id": "live", "name": "G550856URFKG", "did": DID,
                     "password": PASSWORD, "host_hint": HOST,
                     "quality": "HD", "keep_alive": False}],
    }, autosave=False)
    app = ServiceApp(cfg, http_port=0, rtsp_port=0, bind="127.0.0.1",
                     pppp_port=32108)
    await app.start()
    base = f"http://127.0.0.1:{app.http.port}"
    print(f"HTTP {base}  RTSP :{app.rtsp.port}", flush=True)
    try:
        # 1. HTTP: list, info, settings, SD
        status, cams, _ = await http(base, "GET", "/api/cameras")
        check("HTTP /api/cameras", status == 200 and len(cams.get("cameras", [])) == 1,
              f"status={status}, {len(cams.get('cameras', []))}")
        status, info, _ = await http(base, "GET", "/api/cameras/live/info")
        dev = info.get("device") or {}
        check("HTTP /info: GetDevInfo/GetDevStream",
              status == 200 and dev.get("id") == "GHBB-550856-URFKG"
              and (info.get("streams") or {}).get("mainstream"),
              f"{dev.get('id')} / {dev.get('ver')}")
        check("HTTP /info: секрет wifipwd вырезан", dev.get("wifipwd") in (None, "***"),
              repr(dev.get("wifipwd")))
        for group in ("image", "light", "tf", "power"):
            status, data, _ = await http(base, "GET",
                                         f"/api/cameras/live/settings/{group}")
            check(f"HTTP настройки {group}", status == 200 and "values" in data,
                  f"status={status}, values={list((data.get('values') or {}).keys())[:4]}")
        status, sd, _ = await http(base, "GET", "/api/cameras/live/sd?page=1&count=5&all=0")
        files = sd.get("files") or []
        check("HTTP SD: список файлов", status == 200 and isinstance(files, list),
              f"status={status}, файлов {len(files)}")

        # 2. WS player
        ws = await ts.WS.connect(app.http.port, "/ws/live/live")
        msgs = await ts.collect(ws, 8)
        parsed = ts.ws_messages(msgs)
        cfgs = [m for m in parsed if m["kind"] == 2]
        videos = [m for m in parsed if m["kind"] == 1]
        cfg0 = json.loads(cfgs[0]["payload"]) if cfgs else {}
        check("WS: config с кодеком", bool(cfgs) and cfg0.get("codec", "").startswith("avc1."),
              cfg0.get("codec"))
        check("WS: идут кадры", len(videos) >= 8, f"{len(videos)} кадров")
        check("WS: есть ключевой кадр", any(m["flags"] & 1 for m in videos))

        # 3. Snapshot and manual recording (while WS keeps the stream)
        status, snap, _ = await http(base, "GET", "/api/cameras/live/snapshot.jpg", raw=True)
        check("HTTP /snapshot.jpg", status == 200 and snap.startswith(b"\xff\xd8\xff"),
              f"status={status}, {len(snap)} байт")
        status, rec, _ = await http(base, "POST", "/api/cameras/live/record", {})
        await asyncio.sleep(2.0)
        status2, rec2, _ = await http(base, "DELETE", "/api/cameras/live/record")
        row = (rec2 or {}).get("record") or {}
        rec_path = row.get("path", "")
        rec_data = open(rec_path, "rb").read() if rec_path and os.path.isfile(rec_path) else b""
        frags = fmp4.parse_fragments(rec_data) if rec_data else []
        check("запись: fMP4 старт/стоп", status == 201 and rec2.get("record")
              and len(frags) >= 3, f"кадров {len(frags)}, {len(rec_data)} байт")

        # 4. RTSP TCP and UDP on a live stream
        for transport in ("tcp", "udp"):
            url = f"rtsp://127.0.0.1:{app.rtsp.port}/live"
            ses = rtsp_client.RtspSession(url, transport=transport, timeout=10.0)
            try:
                media = await asyncio.to_thread(ses.open, "video")
                got = []
                gen = ses.frames()
                deadline = time.time() + 15
                while len(got) < 15 and time.time() < deadline:
                    try:
                        item = await asyncio.wait_for(asyncio.to_thread(next, gen), 8)
                    except (StopIteration, asyncio.TimeoutError):
                        break
                    got.append(item)
                nals = [d for k, d, _ in got if k == "annexb"]
                check(f"RTSP {transport}: NAL", media is not None and len(nals) >= 5,
                      f"{len(nals)} NAL, {media.codec if media else '—'}")
            except Exception as exc:
                check(f"RTSP {transport}", False, repr(exc))
            finally:
                await asyncio.to_thread(ses.teardown)

        # 5. Quality switch HD -> SD -> HD on the same WS
        status, q1, _ = await http(base, "POST", "/api/cameras/live/quality",
                                   {"quality": "SD"})
        sd_msgs = await ts.collect(ws, 7)
        sd_parsed = ts.ws_messages(sd_msgs)
        sd_cfgs = [json.loads(m["payload"]) for m in sd_parsed if m["kind"] == 2]
        sd_videos = [m for m in sd_parsed if m["kind"] == 1]
        check("смена качества SD: новый config и кадры",
              status == 200 and any(c.get("width", 0) < 1000 for c in sd_cfgs)
              and len(sd_videos) >= 3,
              f"cfg={[(c.get('width'), c.get('height')) for c in sd_cfgs]}, "
              f"кадров {len(sd_videos)}")
        status, q2, _ = await http(base, "POST", "/api/cameras/live/quality",
                                   {"quality": "HD"})
        hd_msgs = await ts.collect(ws, 7)
        hd_parsed = ts.ws_messages(hd_msgs)
        hd_cfgs = [json.loads(m["payload"]) for m in hd_parsed if m["kind"] == 2]
        hd_videos = [m for m in hd_parsed if m["kind"] == 1]
        check("возврат HD: новый config и кадры",
              status == 200 and any(c.get("width", 0) >= 1900 for c in hd_cfgs)
              and len(hd_videos) >= 3,
              f"cfg={[(c.get('width'), c.get('height')) for c in hd_cfgs]}, "
              f"кадров {len(hd_videos)}")
        await ws.close()

        # 6. Idle-TTL: release the camera after the viewers
        await asyncio.sleep(7)
        _, cams2, _ = await http(base, "GET", "/api/cameras")
        cam2 = (cams2.get("cameras") or [{}])[0]
        check("idle-TTL: камера отпущена", cam2.get("state") == "offline",
              f"state={cam2.get('state')}, err={cam2.get('error')}")
    finally:
        await app.close()

    failed = [r for r in RESULTS if not r[1]]
    print(f"\nитог: {len(RESULTS) - len(failed)}/{len(RESULTS)} прошло", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
