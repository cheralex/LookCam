"""JSON config of the service: cameras, ports, accounts, cache.

The file contains camera passwords in plaintext (they are needed for `LoginDev`),
so it is written atomically and with owner-only permissions (`0600` on Linux, an
owner ACL on Windows). It never gets into git — see the service's `.gitignore`.

A camera is identified by **DID**, the address is only a hint: the camera's IP
changes between access-point mode and DHCP in the router's network.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import secrets
import stat

from . import proto

log = logging.getLogger("lookcam.config")

DEFAULTS: dict = {
    "http": {"bind": "0.0.0.0", "port": 8080},
    "rtsp": {"enable": True, "bind": "127.0.0.1", "port": 8554,
             "user": "viewer", "password_hash": "", "realm": "LookCam",
             "low_latency": True},
    "admin": {"password_hash": "", "auth": True, "cookie_secret": "",
              "session_hours": 72, "login_attempts": 8, "login_window_s": 300},
    "cache_dir": "./lookcam_data",
    "cache_limit_mb": 4096,
    "idle_ttl_s": 30,
    "log_level": "INFO",
    "debug_raw": False,
    "webrtc_lock": True,
    "cameras": [],
}

QUALITY_KEYS = ("HD", "SD", "VGA")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
DID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{8,31}$")


class ConfigError(ValueError):
    pass


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


class ConfigStore:
    """Loading/saving `lookcam_service.json` and camera operations."""

    def __init__(self, path: str = "lookcam_service.json", data: dict | None = None,
                 autosave: bool = True) -> None:
        self.path = os.path.abspath(path)
        self.autosave = autosave
        self.data: dict = _merge(DEFAULTS, data or {})
        self._normalize()
        if data is None:
            self.load()

    # ------------------------------------------------------------------ file
    def load(self) -> dict:
        if not os.path.exists(self.path):
            if self.autosave:
                self.save()
                log.info("created config %s", self.path)
            return self.data
        try:
            with open(self.path, "r", encoding="utf-8-sig") as fh:
                loaded = json.load(fh)
        except (OSError, ValueError) as exc:
            raise ConfigError(f"не удалось прочитать {self.path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError(f"{self.path}: ожидался JSON-объект")
        need_secret = not (loaded.get("admin") or {}).get("cookie_secret")
        self.data = _merge(DEFAULTS, loaded)
        self._normalize()
        self._check_permissions()
        if need_secret and self.autosave:
            try:
                self.save()                  # otherwise cookies are invalidated on restart
            except OSError as exc:
                log.warning("failed to save cookie_secret to %s: %s",
                            self.path, exc)
        return self.data

    def save(self) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        text = json.dumps(self.data, ensure_ascii=False, indent=2) + "\n"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, self.path)
        self._set_private(self.path)

    @staticmethod
    def _set_private(path: str) -> None:
        try:
            if os.name == "posix":
                os.chmod(path, 0o600)
            else:  # Windows: drop inheritance, keep the owner
                import subprocess
                user = os.environ.get("USERNAME") or ""
                if user:
                    subprocess.run(["icacls", path, "/inheritance:r",
                                    "/grant:r", f"{user}:F"],
                                   capture_output=True, timeout=5)
        except Exception:
            pass

    def _check_permissions(self) -> None:
        try:
            mode = stat.S_IMODE(os.stat(self.path).st_mode)
            if os.name == "posix" and mode & 0o077:
                log.warning("config %s is not owner-only (mode %o)",
                            self.path, mode)
        except OSError:
            pass

    # ------------------------------------------------------------------ normalization
    def _normalize(self) -> None:
        for key in ("http", "rtsp", "admin"):
            if not isinstance(self.data.get(key), dict):
                self.data[key] = copy.deepcopy(DEFAULTS[key])
        self.data["idle_ttl_s"] = max(1, int(self.data.get("idle_ttl_s") or 30))
        self.data["cache_limit_mb"] = max(64, int(self.data.get("cache_limit_mb") or 4096))
        self.data["cache_dir"] = str(self.data.get("cache_dir") or "./lookcam_data")
        if not self.data["admin"].get("cookie_secret"):
            self.data["admin"]["cookie_secret"] = secrets.token_hex(32)
        cameras = self.data.get("cameras")
        if not isinstance(cameras, list):
            cameras = []
        seen: set[str] = set()
        clean = []
        for raw in cameras:
            if not isinstance(raw, dict):
                continue
            cam = self._normalize_camera(raw, seen)
            if cam is not None:
                clean.append(cam)
                seen.add(cam["id"])
        self.data["cameras"] = clean

    @staticmethod
    def _normalize_camera(raw: dict, seen: set[str]) -> dict | None:
        did = str(raw.get("did") or "").strip().upper()
        if not did:
            return None
        cam = {
            "id": str(raw.get("id") or "").strip().lower(),
            "name": str(raw.get("name") or did).strip(),
            "did": did,
            "password": str(raw.get("password") or ""),
            "host_hint": str(raw.get("host_hint") or "").strip() or None,
            "quality": str(raw.get("quality") or "HD").upper(),
            "keep_alive": bool(raw.get("keep_alive")),
            "enabled": bool(raw.get("enabled", True)),
        }
        if cam["quality"] not in QUALITY_KEYS:
            cam["quality"] = "HD"
        if not ID_RE.match(cam["id"] or ""):
            cam["id"] = camera_id_for(did)
        base = cam["id"]
        index = 2
        while cam["id"] in seen:
            cam["id"] = f"{base}-{index}"
            index += 1
        return cam

    # ------------------------------------------------------------------ cameras
    @property
    def cameras(self) -> list[dict]:
        return self.data["cameras"]

    def camera(self, camera_id: str) -> dict | None:
        for cam in self.cameras:
            if cam["id"] == camera_id:
                return cam
        return None

    def add_camera(self, **fields) -> dict:
        did = str(fields.get("did") or "").strip().upper()
        if not did:
            raise ConfigError("не указан DID камеры")
        if not DID_RE.match(did):
            raise ConfigError("DID должен быть 9–32 символами [A-Za-z0-9-]")
        for cam in self.cameras:
            if proto.real_did(cam["did"]) == proto.real_did(did):
                raise ConfigError("камера с таким DID уже добавлена")
        cam = self._normalize_camera(fields, {c["id"] for c in self.cameras})
        if cam is None:
            raise ConfigError("не удалось разобрать параметры камеры")
        self.cameras.append(cam)
        self.save()
        return cam

    def update_camera(self, camera_id: str, **fields) -> dict:
        cam = self.camera(camera_id)
        if cam is None:
            raise ConfigError(f"нет камеры {camera_id}")
        for key in ("name", "password", "host_hint", "quality", "keep_alive",
                    "enabled", "did"):
            if key not in fields or fields[key] is None:
                continue
            if key == "quality":
                q = str(fields[key]).upper()
                if q not in QUALITY_KEYS:
                    raise ConfigError(f"качество должно быть одно из {QUALITY_KEYS}")
                cam["quality"] = q
            elif key == "keep_alive" or key == "enabled":
                cam[key] = bool(fields[key])
            elif key == "did":
                new = str(fields[key]).strip().upper()
                if not new:
                    raise ConfigError("пустой DID")
                if not DID_RE.match(new):
                    raise ConfigError("DID должен быть 9–32 символами [A-Za-z0-9-]")
                cam["did"] = new
            elif key == "host_hint":
                cam["host_hint"] = str(fields[key]).strip() or None
            else:
                cam[key] = str(fields[key]).strip()
        self.save()
        return cam

    def remove_camera(self, camera_id: str) -> bool:
        before = len(self.cameras)
        self.data["cameras"] = [c for c in self.cameras if c["id"] != camera_id]
        changed = len(self.cameras) != before
        if changed:
            self.save()
        return changed

    # ------------------------------------------------------------------ accounts
    def set_admin_password(self, password: str) -> None:
        from . import auth
        if len(password or "") < 6:
            raise ConfigError("пароль администратора короче 6 символов")
        self.data["admin"]["password_hash"] = auth.hash_password(password)
        self.save()

    def set_rtsp_access(self, user: str, password: str | None) -> None:
        from . import auth
        realm = self.data["rtsp"].get("realm") or "LookCam"
        self.data["rtsp"]["user"] = user or "viewer"
        if password:
            ha1 = auth.digest_ha1(self.data["rtsp"]["user"], realm, password)
            self.data["rtsp"]["password_hash"] = ha1
        elif password == "":
            self.data["rtsp"]["password_hash"] = ""
        self.save()

    @property
    def needs_setup(self) -> bool:
        admin = self.data["admin"]
        return bool(admin.get("auth", True)) and not admin.get("password_hash")

    # ------------------------------------------------------------------ paths
    def cache_dir(self) -> str:
        raw = str(self.data["cache_dir"] or "./lookcam_data")
        if os.path.isabs(raw):
            path = os.path.abspath(raw)
        else:
            # a relative path is resolved from the config directory, not from cwd:
            # this way the package stays self-contained when run from anywhere
            path = os.path.abspath(os.path.join(os.path.dirname(self.path), raw))
        os.makedirs(path, exist_ok=True)
        return path

    def path_in_cache(self, *parts: str) -> str:
        root = self.cache_dir()
        path = os.path.abspath(os.path.join(root, *parts))
        if os.path.commonpath([root, path]) != root:
            raise ConfigError("путь выходит за каталог кэша")
        return path

    def ensure_dirs(self) -> None:
        for sub in ("sd", "records", "snapshots", "downloads"):
            os.makedirs(os.path.join(self.cache_dir(), sub), exist_ok=True)

    def public(self) -> dict:
        """Config without secrets — for the API."""
        data = copy.deepcopy(self.data)
        data["admin"].pop("password_hash", None)
        data["admin"].pop("cookie_secret", None)
        data["rtsp"].pop("password_hash", None)
        for cam in data["cameras"]:
            cam.pop("password", None)
        return data


def camera_id_for(did: str) -> str:
    """Short id from a DID: the last 5 characters, lowercased."""
    real = proto.real_from_virtual(did)
    tail = re.sub(r"[^a-z0-9]", "", real.lower())[-5:]
    return tail or "cam"


def summarize(cfg: ConfigStore) -> str:
    return (f"камер {len(cfg.cameras)}, http {cfg.data['http']['port']}, "
            f"rtsp {cfg.data['rtsp']['port']}, кэш {cfg.data['cache_dir']}")
