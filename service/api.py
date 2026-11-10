"""REST and WebSocket API of the service.

All endpoints except `/api/state`, `/api/login`, `/api/setup` and statics are
guarded by the administrator cookie session. Camera secrets (`wifipwd`,
`4Gpwd`, passwords) are stripped by `CommandBus`/`proto.scrub_secrets`, not the UI.

WebSocket `/ws/live/{id}` sends binary messages:
``[u8 type][u8 flags][u32 ts_ms BE][payload]``, where type 1 is video (payload
is an Annex B access unit), 2 is config (JSON with codec and SPS/PPS), 3 is audio.
Control events (reset on quality change) go as JSON text.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import re
import struct
import time

from . import auth as auth_mod
from . import camera as camera_mod
from . import commands
from . import config as config_mod
from . import fmp4, frames, hub as hub_mod
from . import http as web
from . import proto
from . import sdcard as sdcard_mod

log = logging.getLogger("lookcam.api")

WS_VIDEO = 1
WS_CONFIG = 2
WS_AUDIO = 3


def _unauthorized() -> web.Response:
    return web.Response.error(401, "требуется вход")


def _is_loopback(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(str(ip).split("%")[0])
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        return addr.is_loopback
    except ValueError:
        return False


def _bad(exc: Exception) -> web.Response:
    if isinstance(exc, auth_mod.TooManyAttempts):
        return web.Response.error(429, str(exc))
    if isinstance(exc, camera_mod.CameraBusy):
        return web.Response.error(409, str(exc))
    if isinstance(exc, commands.AuthError):
        return web.Response.error(403, str(exc))
    if isinstance(exc, (config_mod.ConfigError, commands.CommandError,
                        camera_mod.CameraError, sdcard_mod.SDError, ValueError)):
        return web.Response.error(400, str(exc))
    log.exception("API: unhandled error")
    return web.Response.error(500, str(exc))


class ServiceAPI:
    def __init__(self, config, manager, auth, endpoint,
                 downloads=None, provision=None, recorder=None, snapshot=None,
                 rtsp=None) -> None:
        self.config = config
        self.manager = manager
        self.auth = auth
        self.endpoint = endpoint
        self.downloads = downloads
        self.provision = provision
        self.recorder = recorder
        self.snapshot = snapshot
        self.rtsp = rtsp
        self.router = web.Router()
        self._event_clients: set[asyncio.Queue] = set()
        self.started_at = time.time()
        self.request_count = 0
        self._build()

    # ------------------------------------------------------------------ protection
    def guarded(self, handler):
        async def wrapper(req: web.Request, **params):
            self.request_count += 1
            if not self.auth.authorized(req.headers):
                return _unauthorized()
            try:
                return await handler(req, **params)
            except Exception as exc:
                return _bad(exc)
        return wrapper

    # ------------------------------------------------------------------ routes
    def _build(self) -> None:
        r = self.router
        r.get("/api/state", self.state)
        r.post("/api/login", self.login)
        r.post("/api/logout", self.logout)
        r.post("/api/setup", self.setup)
        r.get("/api/health", self.health)

        r.get("/api/cameras", self.guarded(self.cameras_list))
        r.post("/api/cameras", self.guarded(self.cameras_add))
        r.patch("/api/cameras/{camera_id}", self.guarded(self.camera_patch))
        r.delete("/api/cameras/{camera_id}", self.guarded(self.camera_delete))
        r.post("/api/discover", self.guarded(self.discover))
        r.get("/api/environment", self.guarded(self.environment))
        r.get("/api/stats", self.guarded(self.stats))
        r.get("/api/schema", self.guarded(self.schema))

        r.get("/api/cameras/{camera_id}/info", self.guarded(self.camera_info))
        r.post("/api/cameras/{camera_id}/quality", self.guarded(self.camera_quality))
        r.get("/api/cameras/{camera_id}/settings/{group}", self.guarded(self.settings_get))
        r.put("/api/cameras/{camera_id}/settings/{group}", self.guarded(self.settings_put))
        r.post("/api/cameras/{camera_id}/actions/{action}", self.guarded(self.action))
        r.post("/api/cameras/{camera_id}/raw", self.guarded(self.raw))

        r.get("/api/cameras/{camera_id}/sd", self.guarded(self.sd_list))
        r.post("/api/cameras/{camera_id}/sd/download", self.guarded(self.sd_download))
        r.post("/api/cameras/{camera_id}/sd/seek", self.guarded(self.sd_seek))
        r.post("/api/cameras/{camera_id}/sd/delete", self.guarded(self.sd_delete))
        r.post("/api/cameras/{camera_id}/sd/format", self.guarded(self.sd_format))
        r.get("/api/downloads", self.guarded(self.downloads_list))
        r.delete("/api/downloads/{n}", self.guarded(self.download_cancel))

        r.get("/api/cameras/{camera_id}/snapshot.jpg", self.guarded(self.snapshot_jpg))
        r.post("/api/cameras/{camera_id}/record", self.guarded(self.record_start))
        r.delete("/api/cameras/{camera_id}/record", self.guarded(self.record_stop))
        r.get("/api/records", self.guarded(self.records_list))

        r.get("/api/provision/state", self.guarded(self.provision_state))
        r.post("/api/provision/start", self.guarded(self.provision_start))
        r.post("/api/provision/scan", self.guarded(self.provision_scan))
        r.post("/api/provision/apply", self.guarded(self.provision_apply))
        r.post("/api/provision/cancel", self.guarded(self.provision_cancel))

        r.get("/media/records/{name}", self.guarded(self.media_record))
        r.get("/media/{camera_id}/{name}", self.guarded(self.media_sd))

        r.get("/ws/live/{camera_id}", self.ws_live)
        r.get("/ws/live-mp4/{camera_id}", self.ws_live_mp4)
        r.get("/ws/events", self.ws_events)

    # ------------------------------------------------------------------ events
    def broadcast(self, event: dict) -> None:
        for queue in list(self._event_clients):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except Exception:
                    pass

    def notify(self, event: dict) -> None:
        self.broadcast(event)

    # ------------------------------------------------------------------ common
    def _manager_snapshot(self) -> list[dict]:
        return self.manager.list_cameras()

    async def state(self, req: web.Request) -> web.Response:
        authorized = self.auth.authorized(req.headers)
        data = {
            "needs_setup": self.auth.needs_setup,
            "auth": self.auth.enabled,
            "authorized": authorized,
            "version": "0.1.0",
            "rtsp": {"enable": bool(self.config.data["rtsp"].get("enable", True)),
                     "port": (self.rtsp.port if self.rtsp is not None
                              else self.config.data["rtsp"].get("port", 8554)),
                     "auth": bool(self.config.data["rtsp"].get("password_hash"))},
        }
        if authorized:
            data["cameras"] = self._manager_snapshot()
            data["server"] = {
                "http_port": self.config.data["http"].get("port"),
                "uptime_s": round(time.time() - self.started_at, 1),
            }
        return web.Response.json(data)

    async def health(self, req: web.Request) -> web.Response:
        return web.Response.json({"ok": True, "uptime_s": round(time.time() - self.started_at, 1),
                                  "cameras": len(self.manager.cameras)})

    async def login(self, req: web.Request) -> web.Response:
        data = req.json()
        try:
            token = self.auth.login(req.ip, str(data.get("password") or ""))
        except auth_mod.TooManyAttempts as exc:
            return web.Response.error(429, str(exc))
        except auth_mod.AuthError as exc:
            return web.Response.error(401, str(exc))
        return web.Response.json({"ok": True}, headers={
            "Set-Cookie": f"{auth_mod.COOKIE_NAME}={token}; Path=/; HttpOnly; "
                          f"SameSite=Strict; Max-Age={int(self.config.data['admin'].get('session_hours', 72)) * 3600}"})

    async def logout(self, req: web.Request) -> web.Response:
        return web.Response.json({"ok": True}, headers={
            "Set-Cookie": auth_mod.Auth.clear_cookie_header()})

    async def setup(self, req: web.Request) -> web.Response:
        # First-run admin creation is allowed only from the machine itself.
        # A remote LAN client must not be able to claim the admin account.
        if not _is_loopback(req.ip):
            return web.Response.error(
                403, "первичная настройка доступна только с localhost")
        if not self.auth.needs_setup:
            return web.Response.error(409, "первичная настройка уже выполнена")
        data = req.json()
        password = str(data.get("password") or "")
        try:
            self.config.set_admin_password(password)
        except (config_mod.ConfigError, ValueError) as exc:
            return web.Response.error(400, str(exc))
        token = self.auth.login(req.ip, password)
        ttl = int(self.config.data["admin"].get("session_hours", 72)) * 3600
        return web.Response.json({"ok": True}, headers={
            "Set-Cookie": f"{auth_mod.COOKIE_NAME}={token}; Path=/; HttpOnly; "
                          f"SameSite=Strict; Max-Age={ttl}"})

    async def schema(self, req: web.Request) -> web.Response:
        return web.Response.json(commands.schema())

    async def stats(self, req: web.Request) -> web.Response:
        return web.Response.json({
            "endpoint": self.endpoint.stats() if self.endpoint else {},
            "cameras": self.manager.snapshot(),
            "downloads": self.downloads.list_downloads() if self.downloads else [],
            "records": self.recorder.list() if self.recorder else [],
            "provision": self.provision.state() if self.provision else {},
            "requests": self.request_count,
        })

    # ------------------------------------------------------------------ cameras
    async def cameras_list(self, req: web.Request) -> web.Response:
        return web.Response.json({"cameras": self._manager_snapshot()})

    async def cameras_add(self, req: web.Request) -> web.Response:
        data = req.json()
        allowed = {k: data[k] for k in
                   ("name", "did", "password", "host_hint", "quality",
                    "keep_alive", "enabled") if k in data}
        cam = await self.manager.add_camera(**allowed)
        return web.Response.json({"camera": cam.snapshot()}, 201)

    async def camera_patch(self, req: web.Request, camera_id: str) -> web.Response:
        data = req.json()
        allowed = {k: data[k] for k in
                   ("name", "did", "password", "host_hint", "quality",
                    "keep_alive", "enabled") if k in data}
        cam = await self.manager.update_camera(camera_id, **allowed)
        return web.Response.json({"camera": cam.snapshot()})

    async def camera_delete(self, req: web.Request, camera_id: str) -> web.Response:
        await self.manager.remove_camera(camera_id)
        return web.Response.json({"ok": True})

    async def camera_quality(self, req: web.Request, camera_id: str) -> web.Response:
        data = req.json()
        quality = str(data.get("quality") or "").upper()
        if quality not in camera_mod.QUALITY_MAP:
            return web.Response.error(400, "качество должно быть HD/SD/VGA")
        cam = await self.manager.select_quality(camera_id, quality)
        return web.Response.json({"camera": cam.snapshot()})

    async def camera_info(self, req: web.Request, camera_id: str) -> web.Response:
        cam = self.manager.require(camera_id)
        async with cam.acquire("control"):
            info = await cam.device_info()
        info["camera"] = cam.snapshot()
        return web.Response.json(info)

    async def discover(self, req: web.Request) -> web.Response:
        timeout = 3.0
        with contextlib.suppress(Exception):
            data = req.json()
            timeout = float(data.get("timeout") or 3.0)
        if self.endpoint is None:
            return web.Response.error(501, "PPPP-endpoint не запущен")
        items = await self.endpoint.discover_all(timeout=min(max(timeout, 1.0), 10.0))
        configured = list(self.config.cameras)
        found = []
        for item in items:
            match = next((c for c in configured
                          if proto.did_matches(item.ident, item.field2, c["did"])), None)
            row = item.to_dict()
            if match is not None:
                row["camera_id"] = match["id"]
                row["name"] = match.get("name") or match["id"]
            found.append(row)
        return web.Response.json({"devices": found})

    async def environment(self, req: web.Request) -> web.Response:
        try:
            try:
                from ..vendor.lookcam import net as netmod
            except ImportError:  # `python -m service` from the package directory
                from vendor.lookcam import net as netmod
            env = netmod.environment_report()
        except Exception as exc:
            return web.Response.json({"error": str(exc)})
        warnings = []
        for cam in self.config.cameras:
            target = cam.get("host_hint")
            if target:
                try:
                    w = netmod.route_warning(target)
                    if w:
                        warnings.append({"camera_id": cam["id"], "warning": w})
                except Exception:
                    pass
        env["warnings"] = warnings
        return web.Response.json(env)

    # ------------------------------------------------------------------ settings
    async def settings_get(self, req: web.Request, camera_id: str,
                           group: str) -> web.Response:
        cam = self.manager.require(camera_id)
        async with cam.acquire("control"):
            data = await cam.get_settings(group)
        return web.Response.json(data)

    async def settings_put(self, req: web.Request, camera_id: str,
                           group: str) -> web.Response:
        cam = self.manager.require(camera_id)
        data = req.json()
        values = data.get("values") if isinstance(data.get("values"), dict) else data
        async with cam.acquire("control"):
            result = await cam.set_settings(group, values)
        self.broadcast({"type": "settings", "camera_id": camera_id, "group": group})
        return web.Response.json(result)

    async def action(self, req: web.Request, camera_id: str,
                     action: str) -> web.Response:
        cam = self.manager.require(camera_id)
        data = req.json()
        async with cam.acquire("control"):
            result = await cam.run_action(action, data)
        return web.Response.json(result)

    async def raw(self, req: web.Request, camera_id: str) -> web.Response:
        if not self.config.data.get("debug_raw"):
            return web.Response.error(403, "debug_raw выключен в конфиге")
        cam = self.manager.require(camera_id)
        data = req.json()
        async with cam.acquire("control"):
            msg = await cam.raw_command(data)
        return web.Response.json({"response": msg})

    # ------------------------------------------------------------------ SD
    def _sd(self):
        if self.downloads is None:
            raise camera_mod.CameraError("SD-менеджер не запущен")
        return self.downloads

    async def sd_list(self, req: web.Request, camera_id: str) -> web.Response:
        cam = self.manager.require(camera_id)
        page = req.int_arg("page", 1)
        count = req.int_arg("count", 30)
        result = await self._sd().list_files(cam, page, count,
                                             all_pages=req.query.get("all", "1") != "0")
        return web.Response.json(result)

    async def sd_download(self, req: web.Request, camera_id: str) -> web.Response:
        cam = self.manager.require(camera_id)
        data = req.json()
        task = self._sd().request(cam, str(data.get("patch") or ""),
                                  str(data.get("name") or ""),
                                  proto.lenient_int(data.get("size"), 0))
        return web.Response.json({"download": task.to_dict()}, 202)

    async def sd_seek(self, req: web.Request, camera_id: str) -> web.Response:
        cam = self.manager.require(camera_id)
        data = req.json()
        pos_ms = float(data.get("pos_ms") or 0)
        result = await self._sd().prepare_seek(
            cam, str(data.get("patch") or ""),
            str(data.get("name") or ""),
            proto.lenient_int(data.get("size"), 0), pos_ms)
        return web.Response.json(result)

    async def sd_delete(self, req: web.Request, camera_id: str) -> web.Response:
        cam = self.manager.require(camera_id)
        data = req.json()
        patches = data.get("patches") or ([data["patch"]] if data.get("patch") else [])
        return web.Response.json(await self._sd().delete_files(cam, list(patches)))

    async def sd_format(self, req: web.Request, camera_id: str) -> web.Response:
        cam = self.manager.require(camera_id)
        return web.Response.json(await self._sd().format_card(cam))

    async def downloads_list(self, req: web.Request) -> web.Response:
        return web.Response.json({"downloads": self._sd().list_downloads()})

    async def download_cancel(self, req: web.Request, n: str) -> web.Response:
        try:
            number = int(n)
        except ValueError:
            return web.Response.error(400, "неверный номер")
        ok = self._sd().cancel(number)
        return web.Response.json({"ok": ok})

    # ------------------------------------------------------------------ media
    async def media_sd(self, req: web.Request, camera_id: str,
                       name: str) -> web.Response:
        safe = os.path.basename(name)
        path = os.path.join(self.config.cache_dir(), "sd", camera_id, safe)
        if os.path.isfile(path):
            return web.Response.file(req, path, content_type="video/mp4")
        if self.downloads is None:
            return web.Response.error(404, "файл не найден в кэше")
        meta = self.downloads.seek_info(camera_id, safe)
        if meta is None:
            return web.Response.error(404, "файл не найден в кэше")
        size = int(meta.get("size") or 0)
        start, end = 0, min(size - 1, sdcard_mod.SEEK_RANGE_CHUNK - 1)
        header = req.header("range")
        if header:
            m = re.match(r"bytes=(\d*)-(\d*)", header.strip())
            if not m:
                return web.Response(416, b"", {"Content-Range": f"bytes */{size}"})
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
                else:
                    end = min(size - 1, start + sdcard_mod.SEEK_RANGE_CHUNK - 1)
            elif m.group(2):
                start = max(0, size - int(m.group(2)))
                end = size - 1
        if not (0 <= start < size):
            return web.Response(416, b"", {"Content-Range": f"bytes */{size}"})
        end = min(max(start, end), start + sdcard_mod.SEEK_RANGE_CHUNK - 1, size - 1)
        data_start = (int(meta.get("ftyp_size") or 0)
                      + int(meta.get("delta") or 0))
        if start < data_start <= end:
            # ftyp+moov are already on disk; don't pull media bytes just to fill
            # the rest of a browser's initial "bytes=0-" request.
            end = data_start - 1
        cam = self.manager.require(camera_id)
        try:
            meta = await self.downloads.ensure_media_range(cam, safe, start, end)
        except Exception as exc:
            return _bad(exc)
        if meta is None:
            return web.Response.error(404, "файл не найден в кэше")
        seek_path, _ = self.downloads.seek_paths(camera_id, safe)
        return web.Response.file(req, seek_path, content_type="video/mp4",
                                 range_override=(start, end))

    async def media_record(self, req: web.Request, name: str) -> web.Response:
        safe = os.path.basename(name)
        path = os.path.join(self.config.cache_dir(), "records", safe)
        if not os.path.isfile(path):
            return web.Response.error(404, "запись не найдена")
        return web.Response.file(req, path, content_type="video/mp4",
                                 download_name=safe)

    # ------------------------------------------------------------------ recording
    async def record_start(self, req: web.Request, camera_id: str) -> web.Response:
        if self.recorder is None:
            return web.Response.error(501, "запись не запущена")
        cam = self.manager.require(camera_id)
        rec = await self.recorder.start(cam)
        return web.Response.json({"record": rec.to_dict()}, 201)

    async def record_stop(self, req: web.Request, camera_id: str) -> web.Response:
        if self.recorder is None:
            return web.Response.error(501, "запись не запущена")
        rec = await self.recorder.stop(camera_id)
        return web.Response.json({"record": rec.to_dict() if rec else None})

    async def records_list(self, req: web.Request) -> web.Response:
        return web.Response.json({"records": self.recorder.list() if self.recorder else []})

    # ------------------------------------------------------------------ snapshot
    async def snapshot_jpg(self, req: web.Request, camera_id: str) -> web.Response:
        if self.snapshot is None or not self.snapshot.available:
            return web.Response.error(501, "нет ffmpeg/cv2 — снимок на сервере недоступен")
        cam = self.manager.require(camera_id)
        async with cam.acquire("video"):
            try:
                data = await self.snapshot.capture(cam)
            except Exception as exc:
                return web.Response.error(501, str(exc))
        return web.Response(200, data, {"Content-Type": "image/jpeg",
                                        "Cache-Control": "no-store"})

    # ------------------------------------------------------------------ Wi-Fi
    def _prov(self):
        if self.provision is None:
            raise camera_mod.CameraError("мастер Wi-Fi не запущен")
        return self.provision

    async def provision_state(self, req: web.Request) -> web.Response:
        return web.Response.json(self._prov().state())

    async def provision_start(self, req: web.Request) -> web.Response:
        data = req.json()
        return web.Response.json(await self._prov().start(
            str(data.get("did") or ""), str(data.get("camera_password") or ""),
            str(data.get("host_hint") or "") or None))

    async def provision_scan(self, req: web.Request) -> web.Response:
        data = req.json()
        return web.Response.json(await self._prov().scan(
            str(data.get("did") or "") or None,
            str(data.get("camera_password") or "")))

    async def provision_apply(self, req: web.Request) -> web.Response:
        data = req.json()
        return web.Response.json(self._prov().apply(
            str(data.get("ssid") or ""), str(data.get("password") or "")))

    async def provision_cancel(self, req: web.Request) -> web.Response:
        return web.Response.json(await self._prov().cancel())

    # ------------------------------------------------------------------ WebSocket
    async def _ws_auth(self, req: web.Request) -> bool:
        if self.auth.authorized(req.headers):
            return True
        ws = req.websocket
        if ws is not None:
            await ws.close(4401, "unauthorized")
        return False

    @staticmethod
    def _pack(kind: int, flags: int, ts_ms: int, payload: bytes) -> bytes:
        return struct.pack(">BBI", kind & 0xFF, flags & 0xFF,
                           int(ts_ms) & 0xFFFFFFFF) + payload

    @staticmethod
    def _config_payload(cfg: hub_mod.StreamConfig) -> bytes:
        return json.dumps(cfg.to_dict(), ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")

    async def _ws_reader(self, ws: web.WebSocket) -> None:
        while not ws.closed:
            msg = await ws.recv()
            if msg is None:
                return

    async def _wait_close(self, ws: web.WebSocket, sub, timeout: float,
                          reader: asyncio.Task | None = None):
        """Wait for a hub event, but wake up immediately if the client closed the WS."""
        getter = asyncio.ensure_future(sub.get())
        tasks = {getter}
        if reader is not None:
            tasks.add(reader)
        try:
            done, _pending = await asyncio.wait(
                tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                return getter.result()
            return None
        except asyncio.CancelledError:
            raise
        finally:
            if not getter.done():
                getter.cancel()

    async def ws_live(self, req: web.Request, camera_id: str) -> web.Response:
        if not await self._ws_auth(req):
            return web.Response(204)
        ws = req.websocket
        cam = self.manager.get(camera_id)
        if cam is None:
            await ws.send_json({"type": "error", "error": "нет такой камеры"})
            await ws.close()
            return web.Response(204)
        reader = asyncio.create_task(self._ws_reader(ws))
        sub = None
        cfg_sent = False
        try:
            async with cam.acquire("video"):
                sub = cam.hub.subscribe(f"ws-{id(ws)}")
                while not ws.closed and not reader.done():
                    event = await self._wait_close(ws, sub, 10.0, reader)
                    if event is None:
                        if not await ws.send_json({"type": "ping",
                                                   "age_ms": cam.hub.age_ms()}):
                            break
                        continue
                    if isinstance(event, hub_mod.ConfigEvent):
                        cfg = event.config
                        if cfg is not None:
                            ok = await ws.send_bytes(self._pack(
                                WS_CONFIG, 0, 0, self._config_payload(cfg)))
                            cfg_sent = ok
                            if not ok:
                                break
                    elif isinstance(event, hub_mod.ResetEvent):
                        cfg_sent = False
                        if not await ws.send_json({"type": "reset",
                                                   "reason": event.reason}):
                            break
                    elif isinstance(event, frames.AccessUnit):
                        if not cfg_sent and cam.hub.config is not None:
                            cfg_sent = await ws.send_bytes(self._pack(
                                WS_CONFIG, 0, 0,
                                self._config_payload(cam.hub.config)))
                            if not cfg_sent:
                                break
                        if event.kind == "video":
                            ok = await ws.send_bytes(self._pack(
                                WS_VIDEO, 1 if event.key else 0, event.ts_ms,
                                event.payload))
                        else:
                            ok = await ws.send_bytes(self._pack(
                                WS_AUDIO, 0, event.ts_ms, event.payload))
                        if not ok:
                            break
        except Exception as exc:
            log.warning("ws/live %s: %s", camera_id, exc)
            with contextlib.suppress(Exception):
                await ws.send_json({"type": "error", "error": str(exc)})
        finally:
            reader.cancel()
            if sub is not None:
                cam.hub.unsubscribe(sub)
            await ws.close()
        return web.Response(204)

    async def ws_live_mp4(self, req: web.Request, camera_id: str) -> web.Response:
        if not await self._ws_auth(req):
            return web.Response(204)
        ws = req.websocket
        cam = self.manager.get(camera_id)
        if cam is None:
            await ws.close()
            return web.Response(204)
        reader = asyncio.create_task(self._ws_reader(ws))
        sub = None
        writer: fmp4.FMP4Writer | None = None
        last_ts = 0
        try:
            async with cam.acquire("video"):
                sub = cam.hub.subscribe(f"ws-mp4-{id(ws)}")
                deadline = time.monotonic() + 10.0
                while writer is None and not ws.closed and time.monotonic() < deadline:
                    event = await self._wait_close(ws, sub, 1.0, reader)
                    if isinstance(event, hub_mod.ConfigEvent) and event.config \
                            and event.config.ready:
                        cfg = event.config
                        await ws.send_json({"type": "config", "codec": cfg.codec,
                                            "width": cfg.width, "height": cfg.height})
                        writer = fmp4.FMP4Writer(cfg.sps, cfg.pps, cfg.width,
                                                 cfg.height, cam.hub.fps() or 10.0)
                        if not await ws.send_bytes(writer.init):
                            break
                if writer is None and cam.hub.config is not None \
                        and cam.hub.config.ready:
                    cfg = cam.hub.config
                    await ws.send_json({"type": "config", "codec": cfg.codec,
                                        "width": cfg.width, "height": cfg.height})
                    writer = fmp4.FMP4Writer(cfg.sps, cfg.pps, cfg.width,
                                             cfg.height, cam.hub.fps() or 10.0)
                    await ws.send_bytes(writer.init)
                if writer is None:
                    await ws.send_json({"type": "error",
                                        "error": "нет SPS/PPS от камеры"})
                    return web.Response(204)
                while not ws.closed and not reader.done():
                    event = await self._wait_close(ws, sub, 10.0, reader)
                    if event is None:
                        if not await ws.send_json({"type": "ping"}):
                            break
                        continue
                    if isinstance(event, hub_mod.ResetEvent):
                        await ws.send_json({"type": "reset", "reason": event.reason})
                        break
                    if isinstance(event, hub_mod.ConfigEvent):
                        await ws.send_json({"type": "reset", "reason": "качество"})
                        break
                    if not isinstance(event, frames.AccessUnit) or event.kind != "video":
                        continue
                    duration = (event.ts_ms - last_ts) if last_ts else 0
                    if duration < 0 or duration > 1000:
                        duration = 0
                    data = writer.fragment(event.payload, event.key, duration)
                    last_ts = event.ts_ms
                    if data and not await ws.send_bytes(data):
                        break
        except Exception as exc:
            log.warning("ws/live-mp4 %s: %s", camera_id, exc)
        finally:
            reader.cancel()
            if sub is not None:
                cam.hub.unsubscribe(sub)
            await ws.close()
        return web.Response(204)

    async def ws_events(self, req: web.Request) -> web.Response:
        if not await self._ws_auth(req):
            return web.Response(204)
        ws = req.websocket
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._event_clients.add(queue)
        reader = asyncio.create_task(self._ws_reader(ws))
        try:
            await ws.send_json({"type": "hello", "cameras": self._manager_snapshot(),
                                "server": {"uptime_s": round(time.time() - self.started_at, 1)}})
            while not ws.closed and not reader.done():
                try:
                    event = await asyncio.wait_for(queue.get(), 15.0)
                except asyncio.TimeoutError:
                    if not await ws.send_json({"type": "ping"}):
                        break
                    continue
                if not await ws.send_json(event):
                    break
        finally:
            reader.cancel()
            self._event_clients.discard(queue)
            await ws.close()
        return web.Response(204)
