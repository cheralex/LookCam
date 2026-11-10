"""Service startup: config, PPPP endpoint, cameras, HTTP/WebSocket and RTSP.

    cd lookcam_py
    python -m lookcam_deepsik.service

    cd lookcam_py/lookcam_deepsik
    python -m service          # or: python run.py

One process for 1–5 cameras. PPPP listens on UDP :32108 (the camera answers
LanSearch only from this port), HTTP — 8080, RTSP — 8554. The default config
lives inside the package: `lookcam_deepsik/config/lookcam_service.json`.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import logging.handlers
import os
import sys

from . import api as api_mod
from . import auth as auth_mod
from . import camera as camera_mod
from . import config as config_mod
from . import endpoint as endpoint_mod
from . import http as http_mod
from . import provision as provision_mod
from . import recorder as recorder_mod
from . import rtsp_server
from . import sdcard, snapshot as snapshot_mod

log = logging.getLogger("lookcam.service")


def default_config_path() -> str:
    """The default config lives inside the package: lookcam_deepsik/config/."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "config", "lookcam_service.json")


class ServiceApp:
    def __init__(self, config: config_mod.ConfigStore, http_port: int | None = None,
                 rtsp_port: int | None = None, bind: str | None = None,
                 pppp_port: int = 32108, static_dir: str | None = None,
                 public_host: str = "") -> None:
        self.config = config
        self.http_port = (config.data["http"].get("port", 8080)
                          if http_port is None else http_port)
        self.rtsp_port = (config.data["rtsp"].get("port", 8554)
                          if rtsp_port is None else rtsp_port)
        self.bind = bind or config.data["http"].get("bind") or "0.0.0.0"
        self.rtsp_bind = (bind if bind is not None
                          else (config.data["rtsp"].get("bind") or "127.0.0.1"))
        self.pppp_port = pppp_port
        self.public_host = public_host or ""
        here = os.path.dirname(os.path.abspath(__file__))
        self.static_dir = static_dir or os.path.join(here, "static")
        self.endpoint: endpoint_mod.PpppEndpoint | None = None
        self.manager: camera_mod.CameraManager | None = None
        self.auth = auth_mod.Auth(config)
        self.downloads: sdcard.SDCard | None = None
        self.recorder: recorder_mod.RecorderManager | None = None
        self.snapshot: snapshot_mod.SnapshotService | None = None
        self.provision: provision_mod.ProvisionManager | None = None
        self.api: api_mod.ServiceAPI | None = None
        self.http: http_mod.HTTPServer | None = None
        self.rtsp: rtsp_server.RtspServer | None = None
        self._started = False

    # ------------------------------------------------------------------ start
    async def start(self) -> "ServiceApp":
        self.config.ensure_dirs()
        rtsp_cfg = self.config.data["rtsp"]
        if self.rtsp_port is not None:
            rtsp_cfg["port"] = self.rtsp_port
        self.endpoint = endpoint_mod.PpppEndpoint(bind="0.0.0.0", port=self.pppp_port)
        await self.endpoint.start()

        self.manager = camera_mod.CameraManager(
            self.config, self.endpoint,
            idle_ttl=self.config.data.get("idle_ttl_s", 30),
            rtsp_host=self.public_host,
            rtsp_port=self.rtsp_port if self.rtsp_port is not None else 8554)
        self.downloads = sdcard.SDCard(self.config, self.manager)
        await self.downloads.start()
        self.recorder = recorder_mod.RecorderManager(self.config, self.manager)
        self.snapshot = snapshot_mod.SnapshotService(self.config.cache_dir())
        self.provision = provision_mod.ProvisionManager(
            self.config, self.manager, self.endpoint)
        self.rtsp = rtsp_server.RtspServer(self.config, self.manager,
                                           bind=self.rtsp_bind,
                                           port=self.rtsp_port
                                           if self.rtsp_port is not None else 8554)
        await self.rtsp.start()
        if self.manager is not None:
            self.manager.rtsp_port = self.rtsp.port
            self.manager.rtsp_host = self.public_host
        self.api = api_mod.ServiceAPI(
            self.config, self.manager, self.auth, self.endpoint,
            downloads=self.downloads, provision=self.provision,
            recorder=self.recorder, snapshot=self.snapshot, rtsp=self.rtsp)
        self.manager.on_event = self.api.broadcast
        self.provision.notify = self.api.broadcast
        self.http = http_mod.HTTPServer(self.api.router, host=self.bind,
                                        port=self.http_port
                                        if self.http_port is not None else 8080,
                                        static_dir=self.static_dir)
        await self.http.start()
        await self.manager.start()
        self._started = True
        return self

    async def close(self) -> None:
        if not self._started:
            return
        self._started = False
        if self.http is not None:
            with contextlib.suppress(Exception):
                await self.http.close()
        if self.rtsp is not None:
            with contextlib.suppress(Exception):
                await self.rtsp.stop()
        if self.recorder is not None:
            with contextlib.suppress(Exception):
                await self.recorder.stop_all()
        if self.downloads is not None:
            with contextlib.suppress(Exception):
                await self.downloads.stop()
        if self.provision is not None:
            with contextlib.suppress(Exception):
                await self.provision.cancel()
        if self.manager is not None:
            with contextlib.suppress(Exception):
                await self.manager.stop()
        if self.endpoint is not None:
            with contextlib.suppress(Exception):
                await self.endpoint.close()

    # ------------------------------------------------------------------ addresses
    def urls(self) -> dict:
        host = self.public_host
        if not host and self.bind in ("127.0.0.1", "localhost"):
            host = "127.0.0.1"
        if not host and self.bind not in ("0.0.0.0", "", "::"):
            host = self.bind
        if not host:
            try:
                try:
                    from ..vendor.lookcam import net as netmod
                except ImportError:  # `python -m service` from the package directory
                    from vendor.lookcam import net as netmod
                host = netmod.local_ipv4_for() or ""
            except Exception:
                host = ""
        host = host or "127.0.0.1"
        rtsp_host = self.public_host or (
            "127.0.0.1" if self.rtsp_bind in ("127.0.0.1", "localhost") else host)
        return {
            "http": f"http://{host}:{self.http.port if self.http else self.http_port}/",
            "rtsp": f"rtsp://{rtsp_host}:{self.rtsp.port if self.rtsp else self.rtsp_port}/<id>",
            "pppp": f"udp :{self.endpoint.port if self.endpoint else self.pppp_port}",
            "config": self.config.path,
            "cache": self.config.cache_dir(),
        }

    async def wait(self) -> None:
        while True:
            await asyncio.sleep(3600)


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    if not root.handlers:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        root.addHandler(stream)
    if log_file:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        handler.setFormatter(fmt)
        root.addHandler(handler)


async def run_service(args) -> int:
    config = config_mod.ConfigStore(args.config)
    if args.no_auth:
        config.data["admin"]["auth"] = False
    if args.cache_dir:
        config.data["cache_dir"] = os.path.abspath(args.cache_dir)
    setup_logging(args.log_level, args.log_file or config.data.get("log_file"))
    app = ServiceApp(config, http_port=args.http, rtsp_port=args.rtsp,
                     bind=args.bind, pppp_port=args.pppp_port,
                     public_host=args.public_host or "")
    await app.start()
    urls = app.urls()
    print("LookCam Service started", flush=True)
    print(f"  web    : {urls['http']}", flush=True)
    print(f"  RTSP   : {urls['rtsp']}", flush=True)
    print(f"  PPPP   : {urls['pppp']}", flush=True)
    print(f"  config : {urls['config']}", flush=True)
    if config.needs_setup:
        print("  login  : administrator password is not set — open the web page",
              flush=True)
    try:
        await app.wait()
    finally:
        await app.close()
    return 0


async def run_discover(args) -> int:
    setup_logging(args.log_level)
    ep = endpoint_mod.PpppEndpoint(port=args.pppp_port)
    await ep.start()
    try:
        items = await ep.discover_all(timeout=args.timeout)
    finally:
        await ep.close()
    if not items:
        print("no cameras responded to LanSearch", flush=True)
        print("  check that the PC is on the camera network and VPN is not intercepting the route",
              flush=True)
        return 2
    for item in items:
        print(f"  {item.ip:<15} port {item.addr[1]:<6} "
              f"DID {item.full_did or item.label}", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    prog = ("python -m service" if __package__ == "service"
            else "python -m lookcam_deepsik.service")
    p = argparse.ArgumentParser(
        prog=prog,
        description="LookCam: PPPP core, web UI and RTSP bridge")
    p.add_argument("--config", default=default_config_path(),
                   help="JSON config path (default: "
                        "lookcam_deepsik/config/lookcam_service.json)")
    p.add_argument("--http", type=int, default=None, help="HTTP port (from config)")
    p.add_argument("--rtsp", type=int, default=None, help="RTSP port (from config)")
    p.add_argument("--bind", default=None, help="HTTP/RTSP bind address (from config)")
    p.add_argument("--pppp-port", type=int, default=32108,
                   help="PPPP UDP port (the camera expects 32108)")
    p.add_argument("--public-host", default="", help="host/IP in printed URLs")
    p.add_argument("--cache-dir", default=None, help="cache directory")
    p.add_argument("--no-auth", action="store_true",
                   help="disable administrator sign-in (trusted network only)")
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--log-file", default=None)
    p.add_argument("--discover", action="store_true",
                   help="only run LanSearch on all interfaces and exit")
    p.add_argument("--timeout", type=float, default=3.0)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.discover:
            return asyncio.run(run_discover(args))
        return asyncio.run(run_service(args))
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
        return 0
    except config_mod.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr, flush=True)
        return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 2
    except OSError as exc:
        print(f"network error: {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
