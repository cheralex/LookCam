"""Wizard for moving the camera from AP mode into the router network (spec §6.2).

Order: the PC is connected to the camera Wi-Fi → LAN discovery → LoginDev →
`searchWiFiList` (15 s) → choose an SSID → `OpenWifi{sid, wifiPwd, state:1}`
(no reply) → close the session → wait until the camera shows up in the router
network **by DID** (3 s pause, retries every 5 s, 60 s limit). The state lives on
the server: the page may lose connectivity for a while and then recover.

The home network password never enters the state or the logs.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from . import camera as camera_mod
from . import proto, session as session_mod

log = logging.getLogger("lookcam.provision")

SETTLE_DELAY = 3.0
RETRY_EVERY = 5.0
TOTAL_LIMIT = 60.0


class ProvisionManager:
    def __init__(self, config, manager: camera_mod.CameraManager, endpoint,
                 notify=None) -> None:
        self.config = config
        self.manager = manager
        self.endpoint = endpoint
        self.notify = notify or (lambda event: None)
        self.step = "idle"
        self.message = ""
        self.message_en = ""
        self.error = ""
        self.error_en = ""
        self.did = ""
        self.host_hint = None
        self.networks: list[dict] = []
        self.camera_password = ""
        self._session: session_mod.CameraSession | None = None
        self._task: asyncio.Task | None = None
        self._net_ssid = ""
        self._net_password = ""
        self.started_at = 0.0
        self.finished_at = 0.0
        self.resolved_ip = ""
        self._active = False

    # ------------------------------------------------------------------ state
    def state(self) -> dict:
        return {
            "step": self.step,
            "message": self.message,
            "message_en": self.message_en,
            "error": self.error,
            "error_en": self.error_en,
            "did": proto.real_from_virtual(self.did) if self.did else "",
            "networks": list(self.networks),
            "resolved_ip": self.resolved_ip,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "running": self._active,
        }

    @property
    def busy(self) -> bool:
        return self._active

    def _set(self, step: str, message: str = "", error: str = "",
             message_en: str = "", error_en: str = "") -> None:
        self.step = step
        self.message = message
        self.message_en = message_en
        if error:
            self.error = error
            self.error_en = error_en
        self.notify({"type": "provision", **self.state()})

    # ------------------------------------------------------------------ steps
    async def start(self, did: str, camera_password: str = "",
                    host_hint: str | None = None) -> dict:
        if self.busy:
            raise RuntimeError("мастер уже выполняется")
        self.did = proto.normalize_did(did)
        if not self.did:
            raise ValueError("не указан DID камеры")
        self.camera_password = camera_password or self._configured_password(self.did) \
            or "123456"
        self.host_hint = host_hint
        self.networks = []
        self.error = ""
        self.error_en = ""
        self.resolved_ip = ""
        self.started_at = time.time()
        self.finished_at = 0.0
        self._active = True
        self._set("prepare", "проверяю сеть и ищу камеру",
                 message_en="checking the network and looking for the camera")
        self._task = asyncio.create_task(self._prepare(), name="provision-scan")
        return self.state()

    def _configured_password(self, did: str) -> str:
        for cam in self.config.cameras:
            if proto.real_did(cam["did"]) == proto.real_did(did):
                return cam.get("password") or ""
        return ""

    async def _prepare(self) -> None:
        session = None
        try:
            self._ap_warning()
            self._set("connecting", "подключаюсь к камере",
                      message_en="connecting to the camera")
            session = session_mod.CameraSession(
                self.endpoint, self.did, self.camera_password,
                host_hint=self.host_hint, camera_id="provision",
            )
            await session.connect()
            self._session = session
            self._set("scanning", "запрашиваю список Wi-Fi сетей камеры (до 15 с)",
                      message_en="requesting the camera Wi-Fi list (up to 15 s)")
            msg = await session.bus.request({"cmd": "searchWiFiList"},
                                            "searchWiFiList", 15000)
            raw = msg.get("value") or []
            networks = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                ssid = str(item.get("ssid") or "")
                sig = proto.lenient_int(item.get("sig"), -1)
                networks.append({"ssid": ssid, "sig": sig,
                                 "other": (not ssid and sig == -1)})
            self.networks = networks
            self._set("choose", f"найдено сетей: {len(networks)}. Выберите домашнюю",
                      message_en=f"{len(networks)} networks found. Choose your home network")
        except Exception as exc:
            self.error = str(exc)
            self._set("error", "не удалось подготовить камеру", str(exc),
                      message_en="could not prepare the camera",
                      error_en=str(exc))
            if session is not None:
                await session.close()
            self._session = None
            self._active = False

    def _ap_warning(self) -> None:
        try:
            try:
                from ..vendor.lookcam import net as netmod
            except ImportError:  # `python -m service` from the package directory
                from vendor.lookcam import net as netmod
            env = netmod.environment_report()
            if env.get("ap_kind") != "device_ap":
                log.warning("Wi-Fi wizard: PC is not on the camera subnet (%s); trying discovery",
                            env.get("primary"))
        except Exception:
            pass

    async def scan(self, did: str | None = None, camera_password: str = "") -> dict:
        """Rescan networks on a camera that is already connected."""
        if self._session is None or not self._session.is_connected:
            return await self.start(did or self.did, camera_password, self.host_hint)
        self._set("scanning", "обновляю список сетей",
                  message_en="refreshing the network list")
        try:
            msg = await self._session.bus.request({"cmd": "searchWiFiList"},
                                                  "searchWiFiList", 15000)
            self.networks = [{"ssid": str(i.get("ssid") or ""),
                              "sig": proto.lenient_int(i.get("sig"), -1),
                              "other": (not i.get("ssid")
                                        and proto.lenient_int(i.get("sig"), 0) == -1)}
                             for i in (msg.get("value") or []) if isinstance(i, dict)]
            self._set("choose", f"найдено сетей: {len(self.networks)}",
                      message_en=f"{len(self.networks)} networks found")
        except Exception as exc:
            self._set("error", "не удалось получить список сетей", str(exc),
                      message_en="could not get the network list",
                      error_en=str(exc))
        return self.state()

    def apply(self, ssid: str, password: str) -> dict:
        if not ssid:
            raise ValueError("не указан SSID")
        if not self._active:
            raise ValueError("сначала запустите мастер")
        self._net_ssid = ssid
        self._net_password = password or ""
        self._task = asyncio.create_task(self._apply(), name="provision-apply")
        return self.state()

    async def _apply(self) -> None:
        session = self._session
        try:
            if session is None or not session.is_connected:
                raise RuntimeError("сессия с камерой потеряна — начните заново")
            self._set("applying", "передаю параметры домашней сети",
                      message_en="sending home network settings")
            await session.bus.send({"cmd": "OpenWifi", "sid": self._net_ssid,
                                    "wifiPwd": self._net_password, "state": 1})
            await asyncio.sleep(0.5)
            await session.close()
            self._session = None
            self._set("waiting", "жду камеру в сети роутера (до 60 с)",
                      message_en="waiting for the camera on the router network (up to 60 s)")
            ok = await self._wait_reconnect()
            if not ok:
                self._set("error", "камера не появилась в сети роутера",
                          "переподключение не удалось за 60 с",
                          message_en="the camera did not appear on the router network",
                          error_en="reconnect did not succeed within 60 s")
            else:
                self._set("done", f"камера снова онлайн: {self.resolved_ip}",
                          message_en=f"the camera is online again: {self.resolved_ip}")
        except Exception as exc:
            self.error = str(exc)
            self._set("error", "ошибка перевода в сеть роутера", str(exc),
                      message_en="could not move the camera to the router network",
                      error_en=str(exc))
            if self._session is not None:
                await self._session.close()
                self._session = None
        finally:
            self.finished_at = time.time()
            self._active = False

    async def _wait_reconnect(self) -> bool:
        await asyncio.sleep(SETTLE_DELAY)
        deadline = time.monotonic() + TOTAL_LIMIT
        while time.monotonic() < deadline:
            found = await self.endpoint.discover(self.did, None, timeout=RETRY_EVERY)
            if found is not None:
                self.resolved_ip = found.ip
                session = session_mod.CameraSession(
                    self.endpoint, self.did, self.camera_password,
                    host_hint=found.ip, camera_id="provision")
                try:
                    await session.connect()
                    await session.close()
                except Exception as exc:
                    log.info("post-Wi-Fi check: %s", exc)
                    with contextlib.suppress(Exception):
                        await session.close()
                    await asyncio.sleep(RETRY_EVERY)
                    continue
                self._save_address(found.ip)
                return True
            await asyncio.sleep(RETRY_EVERY)
        return False

    def _save_address(self, ip: str) -> None:
        for cam in list(self.config.cameras):
            if proto.real_did(cam["did"]) == proto.real_did(self.did):
                try:
                    self.config.update_camera(cam["id"], host_hint=ip)
                    self.manager.notify({"type": "camera", "id": cam["id"],
                                         "state": self.manager.cameras[cam["id"]].state
                                         if cam["id"] in self.manager.cameras else "offline",
                                         "host_hint": ip})
                except Exception:
                    log.exception("failed to save camera address")
                break

    async def cancel(self) -> dict:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._active = False
        self._set("idle", "мастер отменён", message_en="wizard cancelled")
        return self.state()
