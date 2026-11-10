"""Command bus of channel 1 and the camera command registry.

Three rules without which the camera does not answer:

* ``msg_id`` increases for every new command;
* every DRW packet of a command is sent 10 times with an interval of ~11 ms, with one
  ``msg_id`` and one timestamp;
* a typed command waits for the first incoming JSON frame with a matching ``cmd`` — the
  protocol has no other correlators, so identical typed commands are serialized.

The `SETTING_GROUPS` registry is the single source of truth both for REST validation and
for the forms in the browser (`GET /api/settings/schema`).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from . import proto

log = logging.getLogger("lookcam.cmd")


class CommandError(RuntimeError):
    """Generic command channel error."""


class NotConnected(CommandError):
    pass


class CommandTimeout(CommandError):
    pass


class DeviceRejected(CommandError):
    def __init__(self, message: str, state: int | None = None):
        super().__init__(message)
        self.state = state


class AuthError(CommandError):
    """Wrong camera password — retrying is pointless."""


class CommandBus:
    """Send Lu commands and wait for responses by the ``cmd`` name."""

    def __init__(self, session) -> None:
        self.session = session
        self.msg_id = 0
        self.info: dict = {}
        self.events: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._send_lock = asyncio.Lock()
        self._typed_locks: dict[str, asyncio.Lock] = {}
        self._waiters: dict[str, asyncio.Future] = {}
        self.sent = 0
        self.received = 0
        self.timeouts = 0

    # ------------------------------------------------------------------ sending
    async def send(self, obj: dict) -> int:
        """Send a one-way command (10 repeats, one msg_id)."""
        if not self.session.is_connected:
            raise NotConnected("сессия с камерой не установлена")
        async with self._send_lock:
            used = self.msg_id
            self.msg_id = (self.msg_id + 1) & 0xFFFF
            await self.session.send_lu(obj, used)
            self.sent += 1
            return used

    async def request(self, obj: dict, expect: str | None = None,
                      timeout_ms: int = 8000) -> dict:
        """Typed command: send it and wait for a response with ``cmd == expect``."""
        expect = expect or str(obj.get("cmd") or "")
        if not expect:
            raise CommandError("у команды нет поля cmd")
        if not self.session.is_connected:
            raise NotConnected("сессия с камерой не установлена")
        lock = self._typed_locks.setdefault(expect, asyncio.Lock())
        loop = asyncio.get_running_loop()
        async with lock:
            future: asyncio.Future = loop.create_future()
            self._waiters[expect] = future
            try:
                await self.send(obj)
                try:
                    return await asyncio.wait_for(future, timeout_ms / 1000.0)
                except asyncio.TimeoutError:
                    self.timeouts += 1
                    raise CommandTimeout(
                        f"камера не ответила на {expect} за {timeout_ms} мс")
            finally:
                if self._waiters.get(expect) is future:
                    self._waiters.pop(expect, None)

    # ------------------------------------------------------------------ receive
    def dispatch(self, msg: dict) -> None:
        """A channel 1 JSON frame arrived: wake the waiter or put it into the events."""
        if not isinstance(msg, dict):
            return
        self.received += 1
        cmd = str(msg.get("cmd") or "")
        waiter = self._waiters.get(cmd)
        if waiter is not None and not waiter.done():
            waiter.set_result(msg)
        else:
            try:
                self.events.put_nowait(msg)
            except asyncio.QueueFull:
                try:
                    self.events.get_nowait()
                    self.events.put_nowait(msg)
                except Exception:
                    pass
        on_event = getattr(self.session, "on_command_event", None)
        if on_event is not None:
            try:
                on_event(msg)
            except Exception:
                log.exception("camera event handler")
    # ------------------------------------------------------------------ session
    async def login(self, password: str, timeout_ms: int = 8000) -> dict:
        """LoginDev; `result != 0` — wrong password (retries are pointless)."""
        msg = await self.request({"cmd": "LoginDev", "pwd": password},
                                 "LoginDev", timeout_ms)
        result = proto.lenient_int(msg.get("result"), -999)
        if result != 0:
            raise AuthError(f"камера отвергла пароль (result={result})")
        self.info = {k: v for k, v in msg.items() if k != "cmd"}
        return msg

    async def appoint_time(self, tz_hours: int | None = None) -> None:
        """AppointDev{state:3}: time synchronization after login (500 ms pause)."""
        tz = proto.local_tz_hours() if tz_hours is None else int(tz_hours)
        await self.send({
            "cmd": "AppointDev",
            "state": 3,
            "pwd": getattr(self.session, "password", ""),
            "zone": tz,
            "zone_s": tz * 3600,
            "time": int(time.time()),
        })
        await asyncio.sleep(0.5)

    async def appoint(self, state: int, timeout_ms: int = 8000) -> dict | None:
        """AppointDev{state}: 1 reboot, 2 factory reset, 4 restore settings."""
        return await self.request({"cmd": "AppointDev", "state": state},
                                  "AppointDev", timeout_ms)

    async def live_check(self, timeout_ms: int = 5000) -> bool:
        """Check that a warm session is still alive — a repeated LoginDev."""
        try:
            await self.request({"cmd": "LoginDev",
                                "pwd": getattr(self.session, "password", "")},
                               "LoginDev", timeout_ms)
            return True
        except (CommandTimeout, NotConnected):
            return False

    def clear_waiters(self) -> None:
        for fut in list(self._waiters.values()):
            if not fut.done():
                fut.set_exception(NotConnected("сессия закрыта"))
        self._waiters.clear()


# ---------------------------------------------------------------------- registry

def _int(key: str, label: str, lo: int, hi: int, default: int = 0,
         hint: str = "") -> dict:
    return {"key": key, "label": label, "type": "int", "min": lo, "max": hi,
            "default": default, "hint": hint}


def _bool(key: str, label: str, default: int = 0) -> dict:
    return {"key": key, "label": label, "type": "bool", "min": 0, "max": 1,
            "default": default}


def _time(key: str, label: str, default: str = "00:00") -> dict:
    return {"key": key, "label": label, "type": "time", "default": default}


TIME_RE = re.compile(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$")


def _valid_time(value: Any) -> str:
    m = TIME_RE.match(str(value or ""))
    if not m:
        raise ValueError(f"время «{value}» не в формате HH:MM")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _minutes(value: str) -> int:
    h, m = value.split(":")
    return int(h) * 100 + int(m)


SETTING_GROUPS: dict[str, dict] = {
    "image": {
        "title": "Изображение",
        "get": "GetDevVideoInfo",
        "set": "SetDevVideoInfo",
        "one_way_set": True,          # the camera does not answer SetDevVideoInfo (spec §3.3)
        "fields": [
            _int("brightness", "Яркость", 0, 63, 30),
            _int("saturation", "Насыщенность", 0, 63, 28),
            _int("contrast", "Контраст", 0, 63, 32),
            _int("hue", "Оттенок", 0, 63, 32),
            _int("rate", "Частота кадров", 0, 60, 0),
        ],
        "actions": [
            {"key": "flip_h", "label": "Отразить по горизонтали", "cmd": "LevelFlip"},
            {"key": "flip_v", "label": "Отразить по вертикали", "cmd": "VerticalFlip"},
        ],
    },
    "light": {
        "title": "Подсветка и LED",
        "get": "GetLedInfo",
        "set": "SetLedInfo",
        "fields": [
            _int("control", "Режим", 0, 1, 0, "0 — вручную, 1 — по расписанию"),
            _int("flag", "Флаг", 0, 1, 0),
            _int("time", "Расписание", 0, 1, 0),
            _time("starttime", "Начало"),
            _time("endtime", "Конец"),
            _time("twotime", "Второй интервал с"),
            _time("twoend", "Второй интервал по"),
        ],
        "actions": [
            {"key": "led", "label": "LED", "cmd": "SetLed", "field": "ledstatus"},
            {"key": "light", "label": "ИК-подсветка", "cmd": "SetLight",
             "field": "lightstatus"},
        ],
    },
    "alarm": {
        "title": "Тревога",
        "get": "GetAlarmInfo",
        "set": "SetAlarmInfo",
        "fields": [
            _int("flag", "Режим", 0, 255, 0),
            _int("time", "Расписание", 0, 1, 0),
            _time("starttime", "Начало"),
            _time("endtime", "Конец"),
            _time("twotime", "Второй интервал с"),
            _time("twoend", "Второй интервал по"),
            _int("audio", "Звуковой сигнал", 0, 1, 0),
            _int("putalarm", "Push-уведомление", 0, 1, 0),
            _int("osd", "Метка на видео", 0, 1, 0),
            _int("goptime", "Интервал тревог, с", 0, 3600, 0),
            _int("sensitivity", "Чувствительность", 0, 3, 1),
        ],
    },
    "tf": {
        "title": "SD-запись",
        "get": "GetDevTfParam",
        "set": "SetDevTfParam",
        "fields": [
            _int("openRes", "Запись включена", 0, 1, 1),
            _int("alarmRes", "Запись по тревоге", 0, 1, 0),
            _int("loopRes", "Циклическая запись", 0, 1, 1),
            _int("audio", "Писать звук", 0, 1, 0),
            _int("videoRes", "Разрешение", 0, 1, 0, "0 — HD, 1 — MD"),
            _int("videoCycle", "Длина ролика, с", 0, 1800, 300,
                 "180 / 300 / 600 / 1800"),
        ],
    },
    "power": {
        "title": "Автоотключение",
        "get": "GetCloseTime",
        "set": "SetCloseTime",
        "fields": [_int("values", "Значение", 0, 86400, 0)],
    },
    "exposure": {
        "title": "Экспозиция",
        "get": "GetDevExInfo",
        "set": "SetDevExInfo",
        "fields": [
            _int("exposure", "Экспозиция", 0, 255, 0),
            _int("shutter", "Затвор", 0, 255, 0),
            _int("AnalogGain", "Аналоговое усиление", 0, 255, 0),
            _int("DigitalGain", "Цифровое усиление", 0, 255, 0),
            _int("GainFlag", "Флаг усиления", 0, 1, 0),
            _int("ExGain", "ExGain", 0, 255, 0),
            _int("MaxAnalog", "MaxAnalog", 0, 255, 0),
            _int("MinAnalog", "MinAnalog", 0, 255, 0),
            _int("MaxNumber", "MaxNumber", 0, 255, 0),
            _int("MinNumber", "MinNumber", 0, 255, 0),
            _int("ExCompens", "Компенсация", 0, 255, 0),
        ],
    },
}

ACTIONS: dict[str, dict] = {
    "reboot": {"label": "Перезагрузить", "appoint": 1, "confirm": True},
    "reset": {"label": "Сброс к заводским настройкам", "appoint": 2,
              "confirm": True, "danger": True},
    "restore": {"label": "Восстановить настройки по умолчанию", "appoint": 4,
                "confirm": True},
    "timesync": {"label": "Синхронизировать время", "appoint": 3},
    "password": {"label": "Сменить пароль камеры", "cmd": "ModifyPwd",
                 "fields": [
                     {"key": "pwd", "label": "Текущий пароль", "type": "password"},
                     {"key": "newpwd", "label": "Новый пароль", "type": "password"},
                 ]},
    "connect": {"label": "Запустить поток"},
    "disconnect": {"label": "Остановить поток"},
}

# English labels exposed alongside the Russian ones for the bilingual UI.
# Keys are stable; the API returns both `label` and `label_en`.
_GROUPS_EN = {
    "image": {
        "title": "Image",
        "fields": {
            "brightness": "Brightness", "saturation": "Saturation",
            "contrast": "Contrast", "hue": "Hue", "rate": "Frame rate",
        },
        "actions": {"flip_h": "Flip horizontally", "flip_v": "Flip vertically"},
    },
    "light": {
        "title": "Light and LED",
        "fields": {
            "control": "Mode", "flag": "Flag", "time": "Schedule",
            "starttime": "Start", "endtime": "End",
            "twotime": "Second interval from", "twoend": "Second interval to",
        },
        "hints": {"control": "0 — manual, 1 — schedule"},
        "actions": {"led": "LED", "light": "IR light"},
    },
    "alarm": {
        "title": "Alarm",
        "fields": {
            "flag": "Mode", "time": "Schedule", "starttime": "Start",
            "endtime": "End", "twotime": "Second interval from",
            "twoend": "Second interval to", "audio": "Sound alarm",
            "putalarm": "Push notification", "osd": "OSD label",
            "goptime": "Alarm interval, s", "sensitivity": "Sensitivity",
        },
    },
    "tf": {
        "title": "SD recording",
        "fields": {
            "openRes": "Recording enabled", "alarmRes": "Alarm recording",
            "loopRes": "Loop recording", "audio": "Record audio",
            "videoRes": "Resolution", "videoCycle": "Clip length, s",
        },
        "hints": {"videoRes": "0 — HD, 1 — MD"},
    },
    "power": {"title": "Auto power off", "fields": {"values": "Value"}},
    "exposure": {
        "title": "Exposure",
        "fields": {
            "exposure": "Exposure", "shutter": "Shutter",
            "AnalogGain": "Analog gain", "DigitalGain": "Digital gain",
            "GainFlag": "Gain flag", "ExGain": "ExGain",
            "MaxAnalog": "MaxAnalog", "MinAnalog": "MinAnalog",
            "MaxNumber": "MaxNumber", "MinNumber": "MinNumber",
            "ExCompens": "Compensation",
        },
    },
}

_ACTIONS_EN = {
    "reboot": "Reboot", "reset": "Factory reset",
    "restore": "Restore defaults", "timesync": "Sync time",
    "password": "Change camera password", "connect": "Start stream",
    "disconnect": "Stop stream",
}

for _key, _spec in SETTING_GROUPS.items():
    _tr = _GROUPS_EN.get(_key, {})
    _spec["title_en"] = _tr.get("title", _spec["title"])
    _hints = _tr.get("hints", {})
    for _field in _spec.get("fields", []):
        _field["label_en"] = _tr.get("fields", {}).get(
            _field["key"], _field["label"])
        if _field["key"] in _hints:
            _field["hint_en"] = _hints[_field["key"]]
    for _action in _spec.get("actions", []):
        _action["label_en"] = _tr.get("actions", {}).get(
            _action["key"], _action["label"])

for _key, _action in ACTIONS.items():
    _action["label_en"] = _ACTIONS_EN.get(_key, _action["label"])
    for _field in _action.get("fields", []):
        _field["label_en"] = {
            "pwd": "Current password", "newpwd": "New password",
        }.get(_field["key"], _field["label"])


def schema() -> dict:
    """Registry for the UI and documentation."""
    return {
        "groups": SETTING_GROUPS,
        "actions": ACTIONS,
        "qualities": [
            {"key": "HD", "label": "HD", "state": 1, "stream": 1},
            {"key": "SD", "label": "SD", "state": 2, "stream": 2},
            {"key": "VGA", "label": "VGA", "state": 2, "stream": 3},
        ],
    }


def validate_values(group: str, values: dict) -> dict:
    """Validate and coerce form values to the wire types."""
    spec = SETTING_GROUPS.get(group)
    if spec is None:
        raise ValueError(f"неизвестная группа настроек {group}")
    by_key = {f["key"]: f for f in spec["fields"]}
    out: dict = {}
    for key, raw in (values or {}).items():
        field = by_key.get(key)
        if field is None:
            raise ValueError(f"у группы {group} нет поля {key}")
        if field["type"] == "bool":
            if isinstance(raw, str):
                low = raw.strip().lower()
                if low in ("1", "true", "on", "yes", "да"):
                    out[key] = 1
                elif low in ("0", "false", "off", "no", "нет", ""):
                    out[key] = 0
                else:
                    raise ValueError(f"{key}: ожидалось 0/1")
            else:
                out[key] = 1 if raw else 0
        elif field["type"] == "int":
            try:
                number = int(raw)
            except (TypeError, ValueError):
                raise ValueError(f"{key}: ожидалось целое число")
            if number < field["min"] or number > field["max"]:
                raise ValueError(
                    f"{key}: {number} вне диапазона {field['min']}..{field['max']}")
            out[key] = number
        elif field["type"] == "time":
            out[key] = _valid_time(raw)
        else:
            out[key] = raw
    # schedule: start not later than end (HHMM comparison, as in the app)
    for start, end in (("starttime", "endtime"), ("twotime", "twoend")):
        if start in out and end in out and _minutes(out[start]) > _minutes(out[end]):
            raise ValueError(f"{start} позже {end}")
    return out


def defaults(group: str) -> dict:
    spec = SETTING_GROUPS.get(group) or {}
    return {f["key"]: f.get("default") for f in spec.get("fields", [])}
