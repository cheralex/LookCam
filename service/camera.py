"""Camera as the single owner of the PPPP session and the manager of all cameras.

A camera holds one active session (HANDOFF §2). All consumers — browsers,
RTSP clients, downloads, settings — get it through `Camera.acquire()`.
`OpenVideo` is sent only while there is a video consumer; after the last one
leaves, the stream stops, while the session lives for another `idle_ttl_s`
seconds so that the official app can connect.

State machine:

    Offline -> Connecting -> Online -> Streaming
                    |            ^          |
                    |            +----------+ (last viewer)
                    +-> AuthError / Backoff
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field

from . import commands, config as config_mod, frames, hub, proto, session as session_mod

log = logging.getLogger("lookcam.camera")

QUALITY_MAP = {
    "HD": (1, 1),       # OpenVideo state=1 stream=1 -> 1920x1080
    "SD": (2, 2),       # state=2 stream=2 -> 640x360
    "VGA": (2, 3),      # state=2 stream=3 (this camera returns 640x360)
}
BACKOFF = (2, 4, 8, 16, 32, 60)
NO_FRAME_RESTART = 30.0
LIVE_CHECK_INTERVAL = 60.0

STATE_OFFLINE = "offline"
STATE_CONNECTING = "connecting"
STATE_ONLINE = "online"
STATE_STREAMING = "streaming"
STATE_AUTH = "auth_error"
STATE_ERROR = "error"


class CameraError(RuntimeError):
    pass


class CameraBusy(CameraError):
    def __init__(self, reason: str):
        super().__init__(f"камера занята: {reason}")
        self.reason = reason


@dataclass
class CameraStateEvent:
    camera_id: str
    state: str
    error: str = ""
    busy: str = ""
    extra: dict = field(default_factory=dict)


class Camera:
    """One camera: config, session, hub, consumer counter, reconnection."""

    def __init__(self, manager: "CameraManager", cfg: dict) -> None:
        self.manager = manager
        self.cfg = dict(cfg)
        self.id = cfg["id"]
        self.name = cfg.get("name") or cfg["id"]
        self.did = cfg["did"]
        self.password = cfg.get("password") or ""
        self.host_hint = cfg.get("host_hint")
        self.quality = cfg.get("quality") or "HD"
        self.keep_alive = bool(cfg.get("keep_alive"))
        self.enabled = bool(cfg.get("enabled", True))

        self.hub = hub.StreamHub(self.id)
        self.session: session_mod.CameraSession | None = None
        self.state = STATE_OFFLINE
        self.last_error = ""
        self.last_state_at = time.time()
        self.streaming = False
        self.busy_reason = ""
        self.consumers: dict[str, int] = {"video": 0, "control": 0}
        self.last_consumer_release = 0.0
        self.bytes_total = 0
        self.download_callback = None
        self._wanted = self.keep_alive
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._connect_lock = asyncio.Lock()
        self._stream_lock = asyncio.Lock()
        self._exclusive = asyncio.Lock()
        self._auth_failed = False
        self._connecting = False
        self._retry_at = 0.0
        self._backoff_index = 0
        self._last_live_check = 0.0
        self._last_frame_watch = 0.0
        self._last_restart = 0.0
        self._last_resume_attempt = 0.0
        self._online_event = asyncio.Event()

    # ------------------------------------------------------------------ basic
    @property
    def consumers_total(self) -> int:
        return sum(self.consumers.values())

    def snapshot(self) -> dict:
        return {
            "id": self.id, "name": self.name, "did": proto.real_from_virtual(self.did),
            "virtual_did": self.did, "quality": self.quality,
            "keep_alive": self.keep_alive, "enabled": self.enabled,
            "state": self.state, "error": self.last_error, "busy": self.busy_reason,
            "consumers": dict(self.consumers), "streaming": self.streaming,
            "host_hint": self.host_hint,
            "session": self.session.stats() if self.session else None,
            "hub": self.hub.stats(),
            "rtsp": f"rtsp://{self.manager.rtsp_host or ''}:{self.manager.rtsp_port}/{self.id}",
        }

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._supervise(), name=f"camera-{self.id}")

    async def stop(self) -> None:
        self._stop.set()
        if self.session is not None:
            await self._disconnect("сервис останавливается")
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def _emit(self, error: str = "", extra: dict | None = None) -> None:
        event = CameraStateEvent(self.id, self.state, error or self.last_error,
                                 self.busy_reason, extra or {})
        self.manager.notify({"type": "camera", "id": self.id, "state": self.state,
                             "error": event.error, "busy": event.busy,
                             **event.extra})

    def _set_state(self, state: str, error: str = "") -> None:
        if self.state == state and not error:
            return
        self.state = state
        self.last_state_at = time.time()
        if error:
            self.last_error = error
        if state in (STATE_ONLINE, STATE_STREAMING):
            self._backoff_index = 0
            self._retry_at = 0.0
        self._emit(error)

    # ------------------------------------------------------------------ config
    def apply_config(self, cfg: dict) -> None:
        """Apply an updated config (password, quality, keep-alive, address)."""
        self.cfg = dict(cfg)
        self.name = cfg.get("name") or self.id
        self.did = cfg.get("did") or self.did
        self.host_hint = cfg.get("host_hint")
        self.password = cfg.get("password") or self.password
        self.keep_alive = bool(cfg.get("keep_alive"))
        self.enabled = bool(cfg.get("enabled", True))
        self.quality = cfg.get("quality") or self.quality
        if self.password:
            self._auth_failed = False
        if self.keep_alive:
            self._wanted = True

    # ------------------------------------------------------------------ consumers
    def _add_consumer(self, kind: str) -> None:
        self.consumers[kind] = self.consumers.get(kind, 0) + 1
        self._wanted = True
        self._emit()

    def _remove_consumer(self, kind: str) -> None:
        self.consumers[kind] = max(0, self.consumers.get(kind, 0) - 1)
        self.last_consumer_release = time.time()
        self._emit()

    @contextlib.asynccontextmanager
    async def acquire(self, kind: str = "video"):
        """Take the camera as a consumer: guarantees a session (and a stream for video)."""
        if kind not in ("video", "control"):
            kind = "control"
        if not self.enabled:
            raise CameraError("камера выключена в конфиге")
        self._add_consumer(kind)
        try:
            if kind == "video":
                await self.ensure_streaming()
            else:
                await self.ensure_online()
            yield self
        finally:
            self._remove_consumer(kind)
            if kind == "video" and self.consumers.get("video", 0) == 0:
                self.manager.schedule(self._stop_video_if_idle())

    async def _stop_video_if_idle(self) -> None:
        if self.consumers.get("video", 0) == 0 and self.streaming:
            await self.stop_video("нет зрителей")

    # ------------------------------------------------------------------ connection
    async def ensure_online(self, timeout: float = 35.0) -> None:
        self._wanted = True
        if self.session is not None and self.session.is_connected:
            return
        if self._auth_failed:
            raise CameraError(self.last_error or "неверный пароль камеры")
        try:
            await asyncio.wait_for(self._connect_once(), timeout)
        except asyncio.TimeoutError:
            raise CameraError("не удалось подключиться к камере за %.0f с" % timeout)

    async def _connect_once(self) -> None:
        async with self._connect_lock:
            if self.session is not None and self.session.is_connected:
                return
            if self._connecting:
                return
            self._connecting = True
            self._set_state(STATE_CONNECTING)
            cam_session = session_mod.CameraSession(
                self.manager.endpoint, self.did, self.password,
                host_hint=self.host_hint, camera_id=self.id,
                on_au=self._on_au, on_download=self._on_download,
                on_close=self._on_session_close,
            )
            try:
                await cam_session.connect()
            except commands.AuthError as exc:
                self._auth_failed = True
                self.last_error = str(exc)
                self._set_state(STATE_AUTH, str(exc))
                await cam_session.close()
                self._connecting = False
                raise CameraError(str(exc)) from exc
            except Exception as exc:
                self._connecting = False
                self.last_error = str(exc)
                self._set_state(STATE_ERROR, str(exc))
                with contextlib.suppress(Exception):
                    await cam_session.close()
                self._schedule_backoff()
                raise CameraError(str(exc)) from exc
            self.session = cam_session
            self._connecting = False
            self._online_event.set()
            self._set_state(STATE_ONLINE)
            log.info("%s: online (%s)", self.id, cam_session.addr_text)
            if self.streaming:
                self.streaming = False

    def _schedule_backoff(self) -> None:
        delay = BACKOFF[min(self._backoff_index, len(BACKOFF) - 1)]
        self._backoff_index += 1
        self._retry_at = time.time() + delay
        log.info("%s: reconnecting in %s s", self.id, delay)
    async def _disconnect(self, reason: str = "", send_close: bool = True) -> None:
        if self.session is None:
            return
        cam_session, self.session = self.session, None
        self.streaming = False
        self.hub.drop_all(reason)
        try:
            await cam_session.close(send_close=send_close)
        except Exception:
            log.exception("%s: error closing session", self.id)
        self._online_event.clear()
        self._set_state(STATE_OFFLINE, "" if reason == "idle" else reason)

    def _on_session_close(self) -> None:
        if self.session is not None and not self.session.is_connected:
            self._set_state(STATE_OFFLINE, "сессия закрыта")
        self.streaming = False
        self.hub.drop_all("сессия камеры закрыта")
        self._online_event.clear()

    async def disconnect(self) -> None:
        self._wanted = False
        await self._disconnect("по запросу")
        self._set_state(STATE_OFFLINE)

    # ------------------------------------------------------------------ stream
    async def ensure_streaming(self, timeout: float = session_mod.FIRST_FRAME_TIMEOUT) -> None:
        if self.streaming and self.session is not None:
            return
        await self.ensure_online()
        async with self._stream_lock:
            if self.streaming and self.session is not None:
                return
            if self.session is None:
                raise CameraError("нет сессии")
            state, stream = QUALITY_MAP.get(self.quality, QUALITY_MAP["HD"])
            self.hub.drop_all("открытие потока")
            await self.session.open_video(state, stream)
            ok = await self.session.wait_first_frame(timeout)
            if not ok:
                log.warning("%s: no first frame for %.0f s — restarting OpenVideo",
                            self.id, timeout)
                await self.session.close_video()
                await asyncio.sleep(0.35)
                self.session.assembler.reset(keep_next=True)
                self.hub.drop_all("повторное открытие")
                await self.session.open_video(state, stream)
                ok = await self.session.wait_first_frame(timeout)
            if not ok:
                self._schedule_backoff()
                raise CameraError("камера не отдала ни одного кадра")
            self.streaming = True
            self._last_frame_watch = time.time()
            self._set_state(STATE_STREAMING)

    async def stop_video(self, reason: str = "") -> None:
        self.streaming = False
        self.hub.drop_all(reason or "поток остановлен")
        if self.session is not None:
            try:
                await self.session.close_video()
            except Exception:
                pass
        if self.state == STATE_STREAMING:
            self._set_state(STATE_ONLINE)

    async def _switch_quality(self, quality: str) -> None:
        quality = (quality or "HD").upper()
        if quality not in QUALITY_MAP:
            raise CameraError(f"неизвестное качество {quality}")
        async with self._stream_lock:
            self.quality = quality
            if not self.streaming or self.session is None:
                return
            log.info("%s: changing quality to %s", self.id, quality)
            await self.session.close_video()
            await asyncio.sleep(0.35)
            self.hub.drop_all("смена качества")
            self.session.assembler.reset(keep_next=True)
            state, stream = QUALITY_MAP[quality]
            await self.session.open_video(state, stream)
            if not await self.session.wait_first_frame(session_mod.FIRST_FRAME_TIMEOUT):
                await self.session.close_video()
                await self.sleep_disconnect()

    async def sleep_disconnect(self) -> None:
        self.streaming = False
        await self._disconnect("камера не отдала поток", send_close=True)
        self._schedule_backoff()

    # ------------------------------------------------------------------ callbacks
    def _on_au(self, au: frames.AccessUnit) -> None:
        self.hub.publish(au)
        self._last_frame_watch = time.time()
        if au.kind == "video":
            self.bytes_total += len(au.payload)

    def _on_download(self, header, data) -> None:
        cb = self.download_callback or self.manager.download_callback
        if cb is not None:
            try:
                cb(self, header, data)
            except Exception:
                log.exception("%s: download receiver", self.id)
    # ------------------------------------------------------------------ exclusive
    @contextlib.asynccontextmanager
    async def exclusive(self, reason: str, pause_live: bool = True):
        """Exclusive operation: download, format, reboot, Wi-Fi."""
        if self._exclusive.locked():
            raise CameraBusy(self.busy_reason or "эксклюзивная операция")
        await self._exclusive.acquire()
        # an exclusive operation is also a consumer: otherwise the idle TTL closes
        # the session right in the middle of a download/format
        self._add_consumer("control")
        self.busy_reason = reason
        self._emit()
        was_streaming = self.streaming
        try:
            if pause_live and was_streaming:
                await self.stop_video(f"пауза: {reason}")
            yield self
        finally:
            self.busy_reason = ""
            self._remove_consumer("control")
            self._emit()
            if pause_live and was_streaming and self.consumers.get("video", 0) > 0:
                self.manager.schedule(self._resume_after_exclusive())
            self._exclusive.release()

    async def _resume_after_exclusive(self) -> None:
        try:
            await self.ensure_streaming()
        except Exception as exc:
            log.warning("%s: failed to resume live after operation: %s",
                        self.id, exc)
    # ------------------------------------------------------------------ API commands
    async def device_info(self) -> dict:
        await self.ensure_online()
        assert self.session is not None
        bus = self.session.bus
        out: dict = {"state": self.state, "quality": self.quality}
        for key, obj in (("device", {"cmd": "GetDevInfo"}),
                         ("streams", {"cmd": "GetDevStream"}),
                         ("video", {"cmd": "GetDevVideoInfo"})):
            try:
                out[key] = await bus.request(obj, obj["cmd"], 8000)
            except Exception as exc:
                out[key] = {"error": str(exc)}
        out["session"] = self.session.stats()
        out["hub"] = self.hub.stats()
        return proto.scrub_secrets(out)

    async def get_settings(self, group: str) -> dict:
        spec = commands.SETTING_GROUPS.get(group)
        if spec is None:
            raise CameraError(f"неизвестная группа {group}")
        await self.ensure_online()
        assert self.session is not None
        values = commands.defaults(group)
        try:
            msg = await self.session.bus.request({"cmd": spec["get"]},
                                                 spec["get"], 8000)
            for field in spec["fields"]:
                if field["key"] in msg:
                    values[field["key"]] = msg[field["key"]]
        except Exception as exc:
            return {"group": group, "values": values, "error": str(exc),
                    "support": False}
        return {"group": group, "values": proto.scrub_secrets(values), "support": True}

    async def set_settings(self, group: str, values: dict) -> dict:
        spec = commands.SETTING_GROUPS.get(group)
        if spec is None:
            raise CameraError(f"неизвестная группа {group}")
        prepared = commands.validate_values(group, values)
        if not prepared:
            raise CameraError("нет полей для изменения")
        await self.ensure_online()
        assert self.session is not None
        request = {"cmd": spec["set"], **prepared}
        if spec.get("one_way_set"):
            await self.session.bus.send(request)
            return proto.scrub_secrets({"group": group, "sent": prepared,
                                        "response": None})
        msg = await self.session.bus.request(request, spec["set"], 8000)
        return proto.scrub_secrets({"group": group, "sent": prepared,
                                    "response": msg})

    async def run_action(self, action: str, payload: dict) -> dict:
        if action == "connect":
            await self.ensure_online()
            return {"state": self.state}
        if action == "disconnect":
            await self.disconnect()
            return {"state": self.state}
        if action == "timesync":
            await self.ensure_online()
            assert self.session
            await self.session.bus.appoint_time()
            return {"ok": True}
        if action in ("reboot", "reset", "restore"):
            states = {"reboot": 1, "reset": 2, "restore": 4}
            async with self.exclusive(action, pause_live=True):
                if self.session is None:
                    raise CameraError("камера не подключена")
                await self.session.bus.appoint(states[action], timeout_ms=8000)
                await self._disconnect(action, send_close=False)
            return {"ok": True, "note": "камера уйдёт в перезагрузку"}
        if action == "password":
            old = str(payload.get("pwd") or "")
            new = str(payload.get("newpwd") or "")
            if len(new) < 6:
                raise CameraError("новый пароль короче 6 символов")
            await self.ensure_online()
            assert self.session
            msg = await self.session.bus.request(
                {"cmd": "ModifyPwd", "pwd": old, "newpwd": new}, "ModifyPwd", 8000)
            if proto.lenient_int(msg.get("result"), -1) != 0:
                raise CameraError(f"камера отклонила смену пароля: {msg}")
            self.password = new
            if self.session is not None:
                self.session.password = new
            self.manager.save_camera_password(self.id, new)
            return {"ok": True}
        if action in ("led", "light"):
            field = "ledstatus" if action == "led" else "lightstatus"
            cmd = "SetLed" if action == "led" else "SetLight"
            await self.ensure_online()
            assert self.session
            if "value" in payload and payload.get("value") is not None:
                value = 1 if payload.get("value") else 0
            else:
                # UI buttons are toggles: read the current state from the camera
                # instead of sending value=1 on every click.
                info = await self.session.bus.request(
                    {"cmd": "GetDevInfo"}, "GetDevInfo", 8000)
                current = proto.lenient_int(info.get(field), -1)
                value = 0 if current == 1 else 1
            await self.session.bus.send({"cmd": cmd, field: value})
            return {"ok": True, "field": field, "value": value}
        if action in ("flip_h", "flip_v"):
            cmd = "LevelFlip" if action == "flip_h" else "VerticalFlip"
            await self.ensure_online()
            assert self.session
            await self.session.bus.send({"cmd": cmd})
            return {"ok": True}
        raise CameraError(f"неизвестное действие {action}")

    async def raw_command(self, obj: dict, timeout_ms: int = 8000) -> dict:
        await self.ensure_online()
        assert self.session
        if not isinstance(obj, dict) or not obj.get("cmd"):
            raise CameraError("нужен JSON-объект с полем cmd")
        msg = await self.session.bus.request(obj, str(obj["cmd"]), timeout_ms)
        return proto.scrub_secrets(msg)

    # ------------------------------------------------------------------ watchdog
    async def _supervise(self) -> None:
        while not self._stop.is_set():
            try:
                await self._supervise_step()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s: watchdog loop error", self.id)
            try:
                await asyncio.wait_for(self._stop.wait(), 0.5)
            except asyncio.TimeoutError:
                pass

    async def _supervise_step(self) -> None:
        now = time.time()
        if self.session is not None and not self.session.is_connected:
            dead, self.session = self.session, None
            self.streaming = False
            self.hub.drop_all("сессия закрыта")
            self._online_event.clear()
            with contextlib.suppress(Exception):
                await dead.close(send_close=False)
            if self.state in (STATE_ONLINE, STATE_STREAMING):
                self._set_state(STATE_OFFLINE, "сессия закрыта")
            self._schedule_backoff()

        wanted = self.consumers_total > 0 or self.keep_alive
        self._wanted = wanted
        if (wanted and self.session is None and not self._auth_failed
                and not self._connecting and now >= self._retry_at):
            try:
                await self._connect_once()
            except CameraError:
                pass
            except Exception:
                log.exception("%s: connect", self.id)
        if (not wanted and self.session is not None
                and now - self.last_consumer_release > self.manager.idle_ttl):
            if self.streaming:
                await self.stop_video("idle")
            await self._disconnect("idle")
            self._set_state(STATE_OFFLINE)

        if (self.session is not None and self.session.is_connected
                and self.consumers.get("video", 0) > 0
                and not self.streaming
                and not self._stream_lock.locked()
                and not self._exclusive.locked()
                and now - self._last_resume_attempt > 5.0):
            # the camera reconnected while viewers were waiting: reopen the stream
            self._last_resume_attempt = now
            self.manager.schedule(self._resume_after_reconnect())

        # frame watchdog: 30 s without a frame -> one OpenVideo restart, then backoff
        if (self.streaming and self.session is not None
                and now - self._last_frame_watch > NO_FRAME_RESTART):
            if now - self._last_restart > NO_FRAME_RESTART:
                self._last_restart = now
                log.warning("%s: no frames for 30 s — restarting OpenVideo", self.id)
                self.streaming = False
                self._last_frame_watch = now
                self.manager.schedule(self._restart_stream())
            else:
                await self._disconnect("поток встал", send_close=True)
                self._schedule_backoff()

        # liveness of a warm session
        if (self.session is not None and self.state == STATE_ONLINE
                and not self.streaming and wanted
                and now - self._last_live_check > LIVE_CHECK_INTERVAL):
            self._last_live_check = now
            if not await self.session.bus.live_check(5000):
                log.warning("%s: no reply to liveness check", self.id)
                await self._disconnect("нет ответа на LoginDev")

    async def _restart_stream(self) -> None:
        try:
            await self.stop_video("перезапуск")
            async with self._stream_lock:
                if self.consumers.get("video", 0) == 0:
                    return
                await self.ensure_streaming()
        except Exception as exc:
            log.warning("%s: stream restart failed: %s", self.id, exc)
            await self._disconnect("поток не восстановился")
            self._schedule_backoff()

    async def _resume_after_reconnect(self) -> None:
        try:
            await self.ensure_streaming()
        except Exception as exc:
            log.info("%s: stream did not open after reconnect: %s", self.id, exc)

class CameraManager:
    """Lifecycle of all cameras and the shared endpoint."""

    def __init__(self, config: config_mod.ConfigStore, endpoint,
                 idle_ttl: float | None = None, on_event=None,
                 rtsp_host: str = "", rtsp_port: int = 8554) -> None:
        self.config = config
        self.endpoint = endpoint
        self.idle_ttl = float(idle_ttl if idle_ttl is not None
                              else config.data.get("idle_ttl_s", 30))
        self.on_event = on_event
        self.cameras: dict[str, Camera] = {}
        self.download_callback = None
        self.rtsp_host = rtsp_host
        self.rtsp_port = rtsp_port
        self._tasks: set[asyncio.Task] = set()
        self._closing = False

    # ------------------------------------------------------------------ events
    def notify(self, event: dict) -> None:
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:
                log.exception("event subscriber")
    def schedule(self, coro) -> asyncio.Task | None:
        if self._closing:
            # A late consumer callback during shutdown must not create a task
            # that asyncio.run() destroys while it is still pending.
            coro.close()
            return None
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------------ startup
    async def start(self) -> None:
        for cfg in self.config.cameras:
            await self.add(Camera(self, cfg), save=False)
            if cfg.get("keep_alive"):
                cam = self.cameras[cfg["id"]]
                cam._wanted = True
        log.info("cameras in service: %d", len(self.cameras))

    async def stop(self) -> None:
        self._closing = True
        for cam in list(self.cameras.values()):
            await cam.stop()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            # Cancelling is not enough: asyncio.run() closes the loop right
            # after run_service() returns, and an awaited cancellation prevents
            # "Task was destroyed but it is pending" from idle video timers.
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self.cameras.clear()

    async def add(self, camera: Camera, save: bool = True) -> Camera:
        self.cameras[camera.id] = camera
        await camera.start()
        if save:
            self.config.save()
        self.notify({"type": "camera_added", "id": camera.id})
        return camera

    def get(self, camera_id: str) -> Camera | None:
        return self.cameras.get(camera_id)

    def require(self, camera_id: str) -> Camera:
        cam = self.get(camera_id)
        if cam is None:
            raise CameraError(f"нет камеры {camera_id}")
        return cam

    async def add_camera(self, **fields) -> Camera:
        cfg = self.config.add_camera(**fields)
        cam = Camera(self, cfg)
        return await self.add(cam, save=False)

    async def update_camera(self, camera_id: str, **fields) -> Camera:
        cam = self.require(camera_id)
        old_quality = cam.quality
        cfg = self.config.update_camera(camera_id, **fields)
        cam.apply_config(cfg)
        new_quality = cfg.get("quality") or old_quality
        if new_quality != old_quality and cam.streaming:
            await cam._switch_quality(new_quality)
        return cam

    async def remove_camera(self, camera_id: str) -> bool:
        cam = self.cameras.pop(camera_id, None)
        if cam is not None:
            await cam.stop()
        changed = self.config.remove_camera(camera_id)
        self.notify({"type": "camera_removed", "id": camera_id})
        return changed

    async def select_quality(self, camera_id: str, quality: str) -> Camera:
        cam = self.require(camera_id)
        old_quality = cam.quality
        cfg = self.config.update_camera(camera_id, quality=quality)
        cam.apply_config(cfg)
        if cam.streaming and (cfg.get("quality") or old_quality) != old_quality:
            await cam._switch_quality(cfg.get("quality") or old_quality)
        return cam

    def save_camera_password(self, camera_id: str, password: str) -> None:
        try:
            self.config.update_camera(camera_id, password=password)
        except Exception:
            log.exception("failed to save camera password %s", camera_id)
    # ------------------------------------------------------------------ summary
    def snapshot(self) -> list[dict]:
        return [cam.snapshot() for cam in self.cameras.values()]

    def list_cameras(self) -> list[dict]:
        return [
            {
                "id": cam.id, "name": cam.name,
                "did": proto.real_from_virtual(cam.did),
                "virtual_did": cam.did, "quality": cam.quality,
                "keep_alive": cam.keep_alive, "enabled": cam.enabled,
                "host_hint": cam.host_hint, "state": cam.state,
                "streaming": cam.streaming, "busy": cam.busy_reason,
                "error": cam.last_error,
                "consumers": dict(cam.consumers),
                "fps": cam.hub.fps(), "bitrate_kbps": cam.hub.bitrate_kbps(),
                "age_ms": cam.hub.age_ms(),
                "rtsp": f"rtsp://{self.rtsp_host or ''}:{self.rtsp_port}/{cam.id}",
            }
            for cam in self.cameras.values()
        ]

    def states(self) -> dict:
        return {cam.id: cam.state for cam in self.cameras.values()}
