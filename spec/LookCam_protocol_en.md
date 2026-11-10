# LookCam V1.3.10 — camera interaction protocol

Protocol specification for a third-party client that talks to a LookCam camera without the
vendor SDK: transport, framing, session, command set, live media, SD-card files and Wi-Fi
provisioning. Derived from the LookCam V1.3.10 Android app (8 DEX of bytecode) and live-camera
experiments; written 2026-11-05, translated and condensed 2026-11-10.

Confidence markers: **[B]** — bytecode or constants; **[V]** — verified on a live camera;
**[P]** — probable (neighbouring code, logs, names); **[H]** — hypothesis; **[?]** — unknown.
Statements without a marker are **[B]**.

**Contents**

1. Protocol stack and channels
2. Device identity, connection and session
3. Command reference
4. Live media
5. SD-card files
6. Wi-Fi provisioning over PPPP
7. Session invariants and camera quirks
8. Unresolved questions
- Appendix A. Constants
- Appendix B. Out of scope

---

## 1. Protocol stack and channels

### 1.1 Transport and encapsulation

```
UDP   ── PPPP (F1 <cmd> <len>)                        transport: LAN search, punch, DRW, ack, Close
         └─ DRW 0xD0: D1 <channel> <index u16 BE>      reliable per-channel delivery
              └─ Lu frame (A0 AF AF AF … F4 F3 F2 F1)  application frame: JSON or media
                   └─ JSON {"cmd":"…", …}              command / reply / event
```

Direct-LAN traffic is UDP. Discovery uses port **32108** on both sides; the LanSearch reply
comes from an arbitrary camera port (e.g. 19572), and that address and port carry the rest of
the session.

Every packet starts with a 4-byte PPP header:

| Offset | Type | Meaning |
|---|---|---|
| 0 | u8 | magic `0xF1` |
| 1 | u8 | command |
| 2 | u16 BE | payload length |

Commands used here:

| cmd | Name | Purpose |
|---|---|---|
| `0x30` | LanSearch | discover cameras (empty payload) |
| `0x41` | PunchPkt | NAT punch / session request |
| `0x42` | P2PRdy | session ready (camera reply) |
| `0xD0` | data | command or media payload (DRW) |
| `0xD1` | ack | acknowledge received fragments (DRW) |
| `0xE0` / `0xE1` | Alive / AliveAck | keep-alive |
| `0xF0` | Close | close the session |

**Discovery.** Send `F1 30 00 00` from UDP source port 32108 to the **directed subnet
broadcast** (e.g. `192.168.0.255`) on port 32108. A limited broadcast (`255.255.255.255`) is
sent through the default-route interface (often a VPN) and the camera does not see it; unicast
and ephemeral source ports also get no answer.

**Punch.** The camera answers LanSearch with a `PunchPkt` (cmd `0x41`, 20-byte payload):

| Offset | Type | Meaning |
|---|---|---|
| 0 | 8 bytes | first DID part (e.g. `GHBB` + zeros) |
| 8 | u32 BE | service value (camera-specific) |
| 12 | 8 bytes | second DID part (e.g. `JHDXM` + zeros) |

The service value is not a constant — `0x40FE` on one camera, `0x000867C8` on another; it is
the numeric middle of the DID and must be echoed. Send the same `PunchPkt` (cmd `0x41`, same
value) to the address and port the reply came from; the camera answers `P2PRdy` (`F1 42 …`)
and the session is up.

**Data and acknowledgements.** Command and media payloads travel as `F1 D0 <len BE>` followed
by a 4-byte DRW sub-header `D1 <channel> <index u16 BE>`. On channel 1 the index is the
command's `msg_id`; it must increment for every new command. Received fragments are
acknowledged with `F1 D1 <len BE>` + `D1 <channel> <count u16 BE>` + `count` × u16 BE fragment
indices.

**Keep-alive and close.** An idle session receives `Alive` (`F1 E0`) about every 2 s; replying
`AliveAck` (`F1 E1`) keeps it alive for at least 60 s without any command. `Close` is
`F1 F0 00 00` and is sent twice before exit.

The vendor SDK hides DRW and acks: the app only sees `PPCS_Write(sid, channel, bytes)` and
`PPCS_Read(sid, channel, buffer, &len, timeout)`. An independent implementation performs both
itself.

### 1.2 Channels

| Channel | Direction | Payload | Reader in the app |
|---|---|---|---|
| 0 | camera → client | media: video and audio frames, live and SD playback | `LivePreviewControllerImpl`, `PPCS_Read(…, 0, 256 KiB, 10 ms)` |
| 1 | both | JSON commands, replies, events; **outbound talk-back audio** | `PpcsSessionCmdBusImpl`, `PPCS_Read(…, 1, 256 KiB, 10 ms)` |
| 3 | camera → client | data of a file being downloaded from the SD card | `PpcsTfFileDownloadRemoteDataSource`, `PPCS_Read(…, 3, 256 KiB, 10 ms)` |

Channels 2 and 4–7 are unused by the application.

At the PPPP level the channel is the second byte of the DRW sub-header: commands are
`D1 01 …`, media is `D1 00 …`. Download data therefore arrives as `D1 03 <index> …` and is
acknowledged with `F1 D1 <len> | D1 03 <N> | N × index` **[P]** — by analogy with channel 0
(the channel-0 ack format is **[V]**).

### 1.3 Lu frame formats

Recovered by disassembling the app's native frame parser (`encodeData`, `encodeAudioData`,
`LuPPCSParseManager::append{Cmd,Media,Download}Data`). The library is built without
optimisation and is not stripped, so everything below is **[B]** unless stated otherwise.
All numbers are little-endian. Every frame ends with the trailer `F4 F3 F2 F1`.

Parsers accumulate channel bytes in a buffer (512 KiB for download), look for the magic, wait
for a complete frame, verify the trailer and hand the payload to the callback, moving the tail
to the start of the buffer. The order in which bytes arrive within a channel is guaranteed by
DRW.

#### 1.3.1 Command frame (channel 1, both directions) — 26-byte header

| Offset | Type | Value |
|---|---|---|
| 0 | u32 | magic `0xAFAFAFA0` (`A0 AF AF AF`) |
| 4 | u8 | 0 |
| 5 | u8 | time-zone offset in hours (`tzHours`); logged as `command timezone is %d` |
| 6 | u64 | **local** time, seconds: `gettimeofday().tv_sec + tzHours × 3600` |
| 14 | 8 bytes | zeros |
| 22 | u32 | JSON length |
| 26 | — | JSON (UTF-8, no trailing NUL) |
| 26+len | u32 | trailer `0xF1F2F3F4` |

Receive (`appendCmdData`) reads the same format and passes `handleCmdData(id, json)`. Byte 5
was previously read as `type=3` — it is the time-zone offset (the user is at UTC+3). In camera
replies bytes 4–5 are `01 EC` (`seq=1`, `type=0xEC`) instead **[V]**; both bytes are ignored
when parsing.

#### 1.3.2 Video frame (channel 0) — 83-byte header

There is an outer header (45 bytes, `F` is the frame start) and an inner one (`P = F + 45`):

| Offset from F | Type | Value | `handleVideoData` argument |
|---|---|---|---|
| 0 | u32 | magic `0xAFAFAF01` (`01 AF AF AF`) | — |
| 4 | u8 | recipient id: on playback the `userid` from `PlaybackFile`; in live our camera always sends 123 **[V]** | `uid` |
| 21 | u32 | `params[0]` (0 on our camera) | `params[0]` |
| 25 | float32 | battery: `params[2] = (int)value`; 99 means no battery **[V]** | `params[2]` |
| 41 | u32 | total frame length − 49 | — |
| 46 | u32 | frame timestamp, ms (camera clock) | `ts` (low 32 bits) |
| 50 | u32 | duration (on playback; 0 in live) | `dur`; **also the high 32 bits of `ts`** — `ts` is read as u64 at F+46 |
| 58 | u32 | frame number | `frameNum` |
| 62 | u8 | frame type: 1 key frame, 0 P frame | `frameType` |
| 63 | u64, bit fields | bits 0–2 codec (`params[3]`: 0/1/2 H.264, 3 H.265); 3–9 `params[1]`; 17–29 width; 30–42 height | `w`, `h`, `params[1]`, `params[3]` |
| 79 | u32 | video data length `len` | `len` |
| 83 | — | Annex B (H.264/H.265) | `data` |
| 83+len | u32 | trailer | — |

Verified on two live-camera recordings: 593/593 and 592/592 frames parse, trailers are correct,
`frameType=1` exactly on IDR frames, the timestamp grows by ~100 ms.

**Width/height in the header are unreliable**: the camera writes 1280×720 while the SPS says
1920×1080 — take the size from the SPS. The codec hint is 0 on our camera with the Main
profile.

#### 1.3.3 Audio frame (channel 0)

Same outer header (45 bytes, total length = `u32@41 + 49`), magic `0xAFAFAF02`
(`02 AF AF AF`). Inner header (`P = F + 45`):

| Offset from F | Type | Value | `handleAudioData` argument |
|---|---|---|---|
| 58 | u32 | low 5 bits — audio type/format | `p4` |
| 74 | u32 | audio data length `len` | `len` |
| 78 | — | audio data (codec per `audioType` from `LoginDev`) | `data` |
| 78+len | u32 | trailer | — |

The fifth callback argument (`long`) is always 0. Audio frames are interleaved with video
frames only after `OpenAudio{state:1}`; our captures contain none because the client never
enabled sound.

**A frame assembler must distinguish magic `01` from `02`**: our client looks only for
`01 AF AF AF`, and an audio frame would be glued to the previous video frame — its length sits
at `u32@74`, not at the video's `u32@79`.

#### 1.3.4 Download frame (channel 3) — 25-byte header

| Offset | Type | Value | `handleDownloadData` argument |
|---|---|---|---|
| 0 | u32 | magic `0xAFAFAFA0` (the same as commands) | — |
| 4 | u32 | `userid` from the `DownloadFile` request | `p5` |
| 8 | i32 | **block number from 1** (+1 per block, back to 1 after a resume); **−1000** on the last block | `p4` |
| 12 | u8 | `0x08` in all blocks **[V]**, meaning **[?]** | — |
| 13 | 8 bytes | zeros **[V]** | — |
| 21 | u32 | data length `len`: **6096** in all blocks except the last **[V]** | `len` |
| 25 | — | file bytes | `data` |
| 25+len | u32 | trailer | — |

**Verified on a live camera (2026-11-06)**: an 8 377 488-byte file arrived in 1375 blocks and
was byte-exact — MP4 (`ftyp 3gp5`, `mdat`, `moov` at the end), 1920×1080, 3000 frames decode.
**The last block (−1000) carries data** (1584 bytes) — it must be written. The data is the
SD-card file as is; resuming by `pos` yields a file with the same SHA-1 as a full download.

If the trailer does not match, the native parser does not advance and loops forever (a
library bug) — an independent implementation should skip the magic and look for the next one
on a corrupt frame.

#### 1.3.5 Talk-back packet (channel 1, client → camera)

Built by `encodeAudioData`; the Java call is `encodeAudioData(data, ch=1, bits=16, rate, type)`.
Total length = `len + 54`.

| Offset | Type | Value |
|---|---|---|
| 0 | u32 | magic `0xAFAFAFA1` (`A1 AF AF AF`) |
| 4 | u8 | 2 |
| 5 | u8 | 0 |
| 6 | u64 | 0 (no timestamp) |
| 14 | 8 bytes | zeros |
| 22 | u32 | `len + 24` |
| 26 | u8 | packet type: 1 data, 0 close |
| 27 | u8 | 1 (codec? the app always sends G.711A **[P]**) |
| 28 | u8 | channels (1) |
| 29 | u8 | bits per sample (16 — the app sends this although the data is already G.711) |
| 30 | u32 | sample rate (`SampleRate`, 8000) |
| 34 | u32 | 0 |
| 38 | u64 | packet counter: global, +1 per packet since the library was loaded |
| 46 | u32 | `len` |
| 50 | — | G.711 A-law data |
| 50+len | u32 | trailer |

Close packet: `type=0`, `len=0`.

---

## 2. Device identity, connection and session

### 2.1 Device ID

* The user sees a **virtual DID** (`G016638JHDXM`); the session is established with the
  **real DID** (`GHBB016638JHDXM`). The one-character virtual prefix is expanded to the
  four-character real prefix, the rest of the string is unchanged:

  | Virtual prefix | Real DID prefix |
  |---|---|
  | `9` | `AHCC` |
  | `B` | `BHCC` |
  | `C` | `CHCC` |
  | `F` | `FHBB` |
  | `G` | `GHBB` |
  | `H` | `HHBB` |
  | `I` | `IHBB` |
  | `J` | `JHBB` |
  | `K` | `KHBB` |
  | `T` | `THBB` |
  | `X` | `XHBB` |

  Strings starting with `PPCS-` or `RTOS-` are not converted.
* A valid ID when adding a camera: 12 characters `[A-Za-z0-9]`, or ≥12 characters with a dash
  and a letter.
* In AP mode the camera SSID equals its virtual DID; the app takes the DID from the SSID and
  checks that the length is 12 (`AP_DEVICE_ID_LENGTH`).

### 2.2 Connection

| Parameter | Value |
|---|---|
| Route | Direct for real DIDs starting with `FHBB`, `GHBB`, `HHBB`, `IHBB`, `JHBB`, `KHBB`, `THBB` or `XHBB`; every other DID goes through the vendor server (`ByServer`). A LAN client always uses the direct LAN path |
| `bEnableLanSearch` | bit mask: **126 (0x7E)** normally (`DEFAULT_LAN_SEARCH`), **63 (0x3F)** when connecting to a camera in AP mode (`AP_LAN_SEARCH`) |
| Connect timeout | `0` = SDK timeout; in the AP scenario an external timeout of 15 000 ms |
| Single session | the camera holds one active connection; without `Close` the next one hangs **[V]** |

SDK return codes (`PpcsSessionErrors`) and their meaning:

| Code | Name | App state |
|---|---|---|
| −3 | TIME_OUT | Offline (normal on read, "no data") |
| −4 | INVALID_ID | InvalidId |
| −5 | INVALID_PARAMETER | InvalidId |
| −6 | DEVICE_NOT_ONLINE | Offline |
| −8 | INVALID_PREFIX | InvalidId |
| −15 | REMOTE_SITE_BUFFER_FULL | ConnectOver (do not retry) |
| −19 | USER_CONNECT_BREAK | ConnectionLost |
| −1 | (SDK not ready) | PpcsUnavailable |
| −11…−14, −16 | (session broken) | ConnectionLost |

### 2.3 Login and post-login actions

1. `LoginDev{pwd}` — typed, 8000 ms (§3.1). `result == 0` means success; otherwise
   **PasswordError** and the session is closed. The access password is ≥ 6 characters
   (`DeviceAccessPasswordPolicy.MIN_LENGTH`); the default password of a new camera is
   `123456`.
2. The reply is stored: `devtype` (a string, e.g. `ipc`, `ipcaudio`), `audioType`,
   `SampleRate`, `csType` (cloud), `timeStatus`, `connectNum`.
3. **Time sync** (`PpcsAutoTimeSyncPostLoginAction`) runs after every successful login,
   except for firmware dated ≤ 2021 (`LAST_LEGACY_FIRMWARE_YEAR`):
   `AppointDev{state:3, pwd, zone:<hours UTC offset>, zone_s:<offset, s>, time:<unix, s>}`.
   It is sent without waiting for a reply, then a 500 ms pause
   (`CONFIG_FIRE_AND_FORGET_SETTLE_MS`). The camera still replies
   `{"cmd":"AppointDev","state":0}` **[V]**.
4. The working mode is determined: `PPCS_Check` → mode `0x3F` and the phone is not in the
   camera AP → **Ap**; mode ≠ 0 → **Relay**; otherwise **P2p**.

### 2.4 Command bus

**Sending.** A JSON object is serialised (kotlinx, `encodeDefaults=true`,
`explicitNulls=false`): fields with default values **are written**, `null` fields **are
omitted**. That is why `Get*` requests with `unused:Integer? = null` go out as
`{"cmd":"GetDevInfo"}`. Then `encodeJson(json, len(json), tzHours)` → `PPCS_Write(sid, 1, …)`.

* **`tzHours`** is the phone's current time-zone offset in whole hours
  (`TimeUtil.currentTimezoneOffsetHours`). This is byte 5 of the Lu header, earlier read as
  `type=3` (the user is at UTC+3); the time there is **local**, `unix + tz × 3600` (§1.3.1).
  An implementation should pass its own offset, not the constant 3. Whether this field affects
  the camera is **[?]**.
* `PPCS_Write` returning ≥ 0 means success. There are no application-level retries:
  reliability is the SDK's job. In an independent implementation each DRW packet of a
  command is sent **10 times ~11 ms apart** with the same index, and the index increments per
  command **[V]**.

**Receiving.** A dedicated thread reads channel 1 and feeds the bytes to the Lu parser; each
JSON frame is parsed as a `{cmd, …}` envelope. **A reply may span several DRW packets**
(≤1024 data bytes each): `GetTfVideoList` for 30 files is 5 packets **[V]**. Channel 1 must
therefore be assembled as a stream — in DRW index order, deduplicated, with acks — and only
then split into Lu frames. A naive "one packet = one frame" parser loses such replies. Reply
parsing: `ignoreUnknownKeys`, `isLenient`, `coerceInputValues`, and "lenient" numeric fields
(`PpcsLenientInt/Long/String`): a number may arrive as a string and vice versa. An
implementation must accept both.

**Matching.** A typed command waits for the first incoming frame whose `cmd` equals
`waitForCmd` (usually the command's own name). There are no other correlators: two identical
typed commands must not be in flight at the same time.

**Command kinds:**

* **typed** (`PpcsTypedCommandSpec(cmd, timeoutMs, decoder)`) — wait for a reply; result:
  Success / Timeout / NotConnected / DecodeFailed;
* **one-way** (`PpcsOneWayCommandSpec(timezoneHours?)`) — no reply is awaited;
* **events** — frames from the camera without a request (§2.6).

**Timeouts** (`PpcsCommandTimeoutPolicy`): standard 8000, login 8000, session liveness check
5000, Wi-Fi scan 15 000, file deletion 20 000, long operations 30 000, download control
5000 ms.

### 2.5 Session lifecycle

* **Warm sessions.** After leaving a screen the session is not closed immediately but kept for
  `DEFAULT_WARM_SESSION_TTL_MS = 300000` (5 min); the number is unlimited. Re-entering the
  same camera reuses it.
* **Liveness check** of a warm session — a repeated `LoginDev` with a 5000 ms timeout; on
  failure, reconnect. There is no application-level keep-alive; at the transport level it is
  the `Alive`/`AliveAck` exchange (§1.1).
* **Closing.** `PPCS_Close(sid)`; wait 500 ms for the read thread to stop
  (`CLOSE_WAIT_TIMEOUT_MS`). On the wire — `F1 F0 00 00`, sent twice in our implementation
  **[V]**. A session abandoned without `Close` keeps the camera busy for ~13 s: a new session
  starts only after that.

### 2.6 Unsolicited events (channel 1)

| `cmd` | Fields | What the app does |
|---|---|---|
| `PlaybackFile` | `state`, `result` | drives SD playback (§5.3) |
| `PauseLiveview` | `value` | parsed, no consumer |
| `DoCapture` | `value` | parsed, no consumer |
| `vlMessage` | `sonCmd`, `usbStatus`, `result` | parsed, no consumer |

The reply to `DoCapture{"value":1}` sent by our client does not arrive in the JSON channel
**[V]**, which is consistent with the app never sending this command.

---

## 3. Command reference

Type notation: `int`, `long`, `str`, `int?` — may be absent. "Kind": **T** — typed (timeout,
ms), **O** — one-way, **E** — incoming event. "Used" — whether the command is called by the
app (✔) or only declared (—). Field names are the exact names on the wire.

### 3.1 Session and time

| cmd | Request | Reply | Kind | Used |
|---|---|---|---|---|
| `LoginDev` | `pwd: str` | `result: int`, `connectNum: int`, `devtype: str`, `audioType: int`, `SampleRate: int`, `time: int`, `timeStatus: int`, `csType: int` | T 8000 (liveness 5000) | ✔ |
| `AppointDev` | `state: int`, `pwd: str`, `zone: int?`, `zone_s: int?`, `time: long?` | `state` | O (+500 ms) | ✔ |
| `TimeFlag` | `status: int` | `state, result` | T 8000 | — |

* `LoginDev.result`: 0 — success, −1 — wrong password **[V]**. **But −1 also occurs with a
  correct password** — right after a closed/hung session; 1–20 s later the same password
  returns 0 **[V]** (2026-11-09). Report "wrong password" only after several −1 replies with
  a pause between them.
* `audioType`: 0 G711A, 1 PCM (also the default for an unknown value), 2 AAC, 3 G711U. Our
  camera reports **2 (AAC)**, `SampleRate` 8000 **[V]**.
* `AppointDev.state` (`PpcsMaintenanceAction`): **1** reboot, **2** factory reset, **3** time
  sync, **4** restore default settings. Whether the time fields are filled for 1/2/4 is
  **[?]** (the code only shows them being sent for 3). `{cmd, pwd, state:1}` without time
  fields is **a reboot [V]**: the reply `{"state":1}` arrives in 0.2 s, the camera disappears
  from LanSearch after ~2 s and answers again after ~34 s; `LoginDev` succeeds after 35 s.
* `TimeFlag.status`: 1 on, 2 off (`PpcsTimeStatus`). The meaning is probably an automatic
  time / daylight-saving flag **[H]**.

### 3.2 Device information

| cmd | Request | Reply | Kind | Used |
|---|---|---|---|---|
| `GetDevInfo` | — | `id: str`, `ver: str`, `ip: str`, `wifissid: str`, `wifipwd: str`, `wifisig: int`, `4G: int`, `4GHotspot: int`, `4Gssid: str`, `4Gpwd: str`, `4Gsig: int`, `lock: int`, `iccid: str`, `imei: str`, `ledstatus: int`, `lightstatus: int` | T 8000 | ✔ |
| `GetDevStream` | — | `mainstream: str`, `substream: str`, `threestream: str` | T 8000 | ✔ |
| `GetDevVideoInfo` | — | `brightness`, `contrast`, `stream`, `hue`, `saturation`, `rate` (all `int`) | T 8000 | ✔ |
| `GetDevExInfo` | — | `exposure`, `shutter`, `AnalogGain`, `DigitalGain`, `GainFlag`, `ExGain`, `MaxAnalog`, `MinAnalog`, `MaxNumber`, `MinNumber`, `ExCompens` (all `int`) | T 8000 | ✔ |

* `ver` is the firmware build-date string, e.g. `Mar 16 2026 14:28:19` **[V]**. The app uses
  it to enable or disable features (§3.7, §2.3).
* `wifipwd` is the **home network password**; do not log or store it.
* `GetDevStream` values are labels (`4K`, `2K`, `1080P`), not codec parameters. An empty
  string means the stream is absent.

### 3.3 Live stream and image

| cmd | Request | Reply | Kind | Used |
|---|---|---|---|---|
| `OpenVideo` | `state: int`, `pwd: str`, `stream: int`, `userid: int` | none, media starts | O | ✔ |
| `OpenAudio` | `pwd: str`, `state: int` | none | O | ✔ |
| `OpenDevVoice` | `state: int` | none | O | — |
| `SetDevVideoInfo` | `brightness?`, `contrast?`, `stream?`, `hue?`, `saturation?`, `rate?` | none | O | ✔ |
| `LevelFlip` / `VerticalFlip` | — | none | O | ✔ |
| `SetLed` | `ledstatus: int` (0/1) | none | O | ✔ |
| `SetLight` | `lightstatus: int` (0/1) | none | O | ✔ |
| `SwitchLight` | `value: int` | none | O | — |
| `DoCapture` | `value: int` | — | E | — |
| `PauseLiveview` | `value: int` | — | E | — |

* `OpenVideo.state` — level: **0** close, **1** main, **2** secondary. `stream` — stream
  number **1/2/3** (main/sub/third). `userid` — a random 1..100, new for every command.
  Closing: `{state:0, stream:0}` (the `userid` field is still sent).
* `OpenAudio.state`: 1 — enable audio reception, 0 — disable.
* `SetDevVideoInfo` is sent **with a single field**: `{brightness:N}` or `{saturation:N}`,
  N ∈ 0..63. The app does not change contrast, hue or frame rate.
* `GetDevVideoInfo` values on our camera: `brightness 30, saturation 28` **[V]**
  (2026-11-09: `30, 30`; the camera returns no other fields).
* **`SetDevVideoInfo` with no running video hangs the command channel [V]** (2026-11-09,
  video was not running): DRW packets are acked, but no further command in the session gets a
  reply; after ~1 min the camera reboots by itself. `SetLed` works normally in the same state.
  On a healthy camera (`G550856URFKG`, 2026-11-10) `SetDevVideoInfo` works, sends no reply, and
  **quantises** the value: wrote 34 — reads 33, 31 → 30, 30 → 30 **[V]**.
* `SetLight` **replies** `{"cmd":"SetLight","state":0}` (not one-way); `SetLed` does not
  reply; `SetCloseTime` → `{"state":-1}` (unsupported) **[V]**.
* Replies that differ from the tables above **[V]**: `GetDevExInfo` → `{exposure, state}`;
  `GetCloseTime` and `GetPTZLight` → `{"state":-1}` (unsupported); `GetAlarmInfo` and
  `GetLedInfo` include an `id` field (DID); `GetDevInfo` on a router network returns
  `wifisig: 0` and `lock: 2` (meaning of `lock` **[?]**).

### 3.4 SD card

| cmd | Request | Reply | Kind | Used |
|---|---|---|---|---|
| `GetDevTfParam` | — | `state`, `totalSpace`, `freeSpace`, `openRes`, `alarmRes`, `loopRes`, `audio`, `videoRes`, `videoCycle` (all `int`) | T 8000 | ✔ |
| `SetDevTfParam` | `openRes`, `alarmRes`, `loopRes`, `audio`, `videoRes`, `videoCycle` (all `int`) | `state, result` | T 8000 | ✔ |
| `GetTfVideoList` | `page: int`, `count: int` | `page`, `count`, `allCount`, `result`, `value: [{name: str, patch: str, size: long, time: long}]` | T 30 000 | ✔ |
| `DownloadFile` | `pwd: str`, `userid: int?`, `patch: str?`, `pos: long?`, `state: int` | `state` | T 5000 | ✔ |
| `PlaybackFile` | `pwd: str`, `userid: int`, `state: int`, `pos: long`, `patch: str` | event `{state, result}` | O + E | ✔ |
| `DeleteTfFile` | `count: int`, `value: [{patch: str}]` | `state, result` | T 20 000 | ✔ |
| `FormatCard` | — | `state` | T 30 000 | ✔ |

* `GetDevTfParam.state` (the card): **0** no card, **1** ready, **2** error.
  `totalSpace`, `freeSpace` — MB. `openRes`/`alarmRes`/`loopRes`/`audio` — 0/1 flags
  (record when enabled / on alarm / cyclic / with sound **[P]**). `videoRes`: **0** HD,
  **1** MD. `videoCycle` — clip length in seconds: 180, 300, 600 or 1800.
* `SetDevTfParam`: success when `state ∈ {0, 1}` **[P]**.
* `GetTfVideoList.result`: **0** ok, **−1** no files, **−2** no card, **−3** card error.
  `page` ≥ 1, `count` 1..100. `size` — bytes, `time` — seconds: **camera local time recorded
  as UTC** (`2026-1104-205905.mp4` ↔ `time=1793825945` = 2026-11-04 20:59:05 UTC) **[V]**.
  `patch` — the file path on the card, the key for all file commands, e.g.
  `/mnt/CYC_DV/2026/11/04/2026-1104-205905.00302486.mp4`; `name` — the name without the
  directory and without the middle part **[V]**. Cyclic-recording files are 300 s MP4,
  8–35 MB **[V]**.
* `DownloadFile.state` in the request: **1** start, **2** stop/pause. In the reply: **0**
  success, **1** accepted (older firmware), **2** file not found, **3** — the camera is busy
  with a previous transfer (observed right after a stop; 0 again after 20 s) **[V]**, **−1**
  error. The app does not know code 3 and treats it as a failure.
* `PlaybackFile.state`: **1** start, **2** pause, **3** resume, **4** seek, **5** stop.
  Events: `state` **0** ok, **−1** start failed, **4** end of file; `result ≠ 0` — error.
* `FormatCard.state`: **0** success, **1** success but a reboot is required; anything else is
  an error.

### 3.5 Wi-Fi and 4G

| cmd | Request | Reply | Kind | Used |
|---|---|---|---|---|
| `searchWiFiList` | — | `value: [{ssid: str, sig: int}]` | T 15 000 | ✔ |
| `OpenWifi` | `sid: str`, `wifiPwd: str`, `state: int` | none | O | ✔ |
| `4GWifi` | `sid: str`, `wifiPwd: str`, `pwd: str`, `state: int` | none | O | — |
| `Open4G` | `state: int` | none | O | — |

* The names are exactly `searchWiFiList` (lower-case `s`) and `4GWifi`.
* `OpenWifi.state` is always **1**. `sid` — the home network SSID, `wifiPwd` — its password.
* A list entry with an empty `ssid` and `sig == −1` means "other network" (manual input).
* `4GWifi`, `Open4G`: meaning of `state` and `pwd` **[?]** (there are no calls in the app).

### 3.6 Alarm and light settings

| cmd | Request | Reply | Kind | Used |
|---|---|---|---|---|
| `GetAlarmInfo` | — | `flag`, `time`, `starttime`, `endtime`, `twotime`, `twoend`, `audio`, `putalarm`, `osd`, `goptime`, `sensitivity` | T 8000 | ✔ |
| `SetAlarmInfo` | the same fields | `state` (0 — success) | T 8000 | ✔ |
| `GetLedInfo` | — | `control`, `starttime`, `endtime`, `flag`, `twotime`, `twoend` | T 8000 | ✔ |
| `SetLedInfo` | the same fields | `state, result` | T 8000 | ✔ |
| `SetAudioAlarm` | `control: int`, `sensitivity: int` | none | O | — |

Alarm field types and meanings:

| Field | Type | Meaning |
|---|---|---|
| `flag` | int | alarm mode/flag; passed through unchanged **[?]** |
| `time` | int (0/1) | schedule enabled |
| `starttime`, `endtime` | str `"HH:MM"` | first interval |
| `twotime`, `twoend` | str `"HH:MM"` | second interval |
| `audio` | int (0/1) | sounder on the camera |
| `putalarm` | int (0/1) | push notification |
| `osd` | int (0/1) | overlay on the video |
| `goptime` | int, s | interval between alarms (entered as a number) |
| `sensitivity` | int 0..3 | four sensitivity levels |

* An interval is valid if `HHMM(start) ≤ HHMM(end)` (numeric comparison without the colon).
* Firmware `Feb 5 2023 18:37:04` has a reduced option set (`LegacyAlarmSettingsCapabilityPolicy`):
  the alarm interval, push, schedule and sounder are hidden **[P]**.
* `SetLedInfo.control`: **0** manual mode, **1** scheduled; the time format is the same.

### 3.7 Maintenance and miscellaneous

| cmd | Request | Reply | Kind | Used |
|---|---|---|---|---|
| `ModifyPwd` | `pwd: str` (old), `newpwd: str` | `result` (0 — success) | T 8000 | ✔ |
| `SetImei` | `imei: str` | `state, result` | T 8000 | ✔ |
| `GetCloseTime` | — | `values: int`, `state`, `result` | T 8000 | ✔ |
| `SetCloseTime` | `values: int` | `state, result` | T 8000 | ✔ |
| `SetDevExInfo` | exposure fields (§3.2) | `state, result` | T 8000 | ✔ |
| `selfDestruct` | `pwd: str` | `state, result` | T 5000 | ✔ |
| `SetDevCsFun` | `values: int` | none | O | — |
| `PTZControl` | `speed: int`, `direct: int`, `mode: int`, `xyvalue: str` | none | O | — |
| `SetPtz` | `value: int` | none | O | — |
| `GetPTZLight` | — | `value: int`, `autoClose: int` | T 8000 | — |
| `SetNight` | `control: int` | none | O | — |
| `SetAudioSize` | `size: int` | none | O | — |
| `RandControl` | `rand1`, `rand2`, `rand3`, `randTime`, `time`, `cycleMs`, `randMs`, `rectMs` (int) | none | O | — |
| `OpenLock` | `lockpwd: str`, `value: int` | `state, result` | T 2500 | ✔ |
| `OpenLockVideo` | `value: int` | `state, result` | T 2500 | — |
| `updaetLockPwd` | `oldpwd: str`, `newpwd: str` | `state, result` | T 2000 | — |

* `GetCloseTime.values` — camera auto-shutdown, units **[?]**.
* `selfDestruct` — the "destroy device" settings item (`DESTROY_DEVICE`); the exact camera
  action is **[?]**. Dangerous: do not try it on a live camera.
* Reboot and reset are not separate commands but `AppointDev{state:1|2|4}` (§3.1).
* PTZ direction codes, `SetNight` modes and the `SetAudioSize` range are absent from the app.

#### 3.7.1 No firmware update over this protocol

Checked against V1.3.10 (2026-11-09) **[P]**: the app **only displays** the firmware version;
it cannot update it. The version is `GetDevInfo.ver` and is used solely as a build date for
policies — post-login time sync (≤2021 — skipped, §2.3) and the reduced alarm options
(`Feb 5 2023`, §3.6). There is no image download/apply command (`Upgrade`, `Update…`, `Ota…`)
among all `cmd` values; "maintenance" is limited to `AppointDev{1|2|3|4}`, `FormatCard` and
`selfDestruct`. The native libraries contain no upgrade/firmware/OTA strings; BLE only reads
`2a26` (Firmware Revision). If the camera updates at all, it does so on its own or with a
vendor tool, not through LookCam.

---

## 4. Live media

Source: `LivePreviewControllerImpl` (≈8000 lines of bytecode), `LivePreviewInfoReader`,
`LiveViewViewModel`.

### 4.1 Stream selection

After login: `GetDevStream` → a list of options `LiveStreamOption(title, stream, level)`:

| Reply field non-empty | `stream` | `level` (= `OpenVideo.state`) | UI label |
|---|---|---|---|
| `mainstream` | 1 | 1 | HD |
| `substream` | 2 | 2 | SD |
| `threestream` | 3 | 2 | VGA |

The default is `LivePreviewConfig(level=2, stream=2)` — SD.

Measured on our camera **[V]**: `state=1` → 1920×1080, `state=2` → 640×360 for any `stream`;
4K was not obtained with any combination. The discrepancy with the app's table (where `level`
is a function of `stream`) is unexplained.

### 4.2 Command sequences

**Start.**

1. Take a warm session or open a new one (§2.3).
2. Register the media parser; start three threads: events (channel 1), read (channel 0),
   decoding.
3. `OpenVideo{state:level, pwd, stream, userid:rand(1..100)}`.
4. Wait for the first frame up to 30 000 ms. The first frame is the nearest IDR: 3.3–3.6 s
   **[V]**. There is no "request a key frame" command in the app.
5. On failure — one automatic restart.

**Pause (app goes to background)** — `pausePreviewStreamSoft`:

1. `OpenVideo{state:0, stream:0}`.
2. If sound is on: `OpenAudio{state:0}`.
3. Stop audio playback. The session remains.

**Resume** — `resumePreviewStreamSoft`: `OpenVideo(level, stream)`, then `OpenAudio{state:1}`
if sound was on. If no frames arrive within 2500 ms — a full restart.

**Quality switch** — `switchPreviewStream`, under a mutex:

1. If the current option is selected and video is running — do nothing.
2. Stop talk-back.
3. Soft pause: `OpenVideo{state:0, stream:0}` (+ `OpenAudio{state:0}`).
4. Pause **350 ms** (`PREVIEW_STREAM_SWITCH_STABILIZE_MS`).
5. Reset the parser cache and the decoder (a new resolution means a new SPS).
6. `OpenVideo(level, stream)`; `OpenAudio{state:1}` if sound was on.
7. The UI waits another 500 ms; on error — a full restart.

**Stop** — `fullyStopPreview`:

1. `OpenVideo{state:0, stream:0}` — immediately, via `PPCS_Write` to channel 1.
2. Close the session or return it to the warm pool.

**Channel 0 read errors:** `−3` — no data (normal); `−1` → PpcsUnavailable; `−4`, `−8` →
InvalidDeviceId; `−6`, `−11…−14`, `−16`, `−19` → ConnectionLost. Any of them stops the view
with an error.

### 4.3 Media channel

* **Reading:** channel 0, buffer 262 144 bytes, timeout 10 ms, tight loop. An independent
  implementation receives DRW `D1 00 <frag>`, acks every fragment and deduplicates repeats.
  A media packet is usually 1032 bytes: 4 (PPP) + 4 (DRW) + 1024 (data). The official app acks
  every received fragment, including duplicates, in batches of 1–17 indices about every 83 ms.
  The fragment index is continuous within a session and does not reset at frame boundaries — do
  not detect frame starts by `index == 0`.
* **Frame:** starts with magic `01 AF AF AF`; frame length — `u32LE@79 + 87` (duplicate
  `u32LE@41 + 49`); camera clock, ms — `u32LE@46`; frame number — `u32LE@58`; Annex B — from
  offset 83 (sometimes 43) **[V]**. The SPS type is `0x27` **[V]**. The start code
  `00 00 00 01` occasionally occurs inside the frame header as a length or counter field (2
  frames out of 592); locate the NAL start by a plausible NAL type, not by the first start
  code.
* **Frame metadata** — header layout in §1.3.2. The app uses: `frameType` (**1** — key
  frame), `params[3]` — codec (**3 — H.265**, otherwise H.264), `params[2]` — battery
  (99 → 99, >0 → `params[2] − 1`, otherwise −1). `params[0..1]` are not read. Without a
  hint, the codec is determined from the first NAL: H.264 types 5/7/8 → H.264, H.265 types
  19/20/32/33/34 → H.265.
* **Decoder queue:** with > 15 frames, non-key frames are dropped and a "key frame needed"
  flag is set; with > 30 the queue is cleared entirely, and the key frame is still queued.
  While the flag is set, all frames are skipped until the nearest IDR. There is no
  buffering — only dropping.
* **Decoding:** a persistent software decoder (ffmpeg), H.264 = type 10, H.265 = 13. In live,
  the frame time comes from the arrival clock, not from the camera timestamp.

Note for browser decoders (WebCodecs): the camera does not declare
`bitstream_restriction_flag`, so the SPS must be rewritten with `max_num_reorder_frames = 0`,
otherwise the decoder holds ~4 frames.

### 4.4 Audio reception

1. `OpenAudio{pwd, state:1}`; audio travels on channel 0 together with video.
2. Format: `audioType` and `SampleRate` from `LoginDev`. If the type is unknown or absent —
   PCM.
3. Decoding: PCM as is; otherwise to 16-bit mono PCM (native decoder codes: G711A 0,
   G711U 1, AAC 4, other 5). Output — AudioTrack, mono, 16 bit.
4. During talk-back the incoming sound is discarded (there is no echo cancellation).

An audio frame differs from video by the magic `02 AF AF AF` (§1.3.3). With `audioType=2` the
data is probably AAC ADTS **[H]**; to confirm, enable sound. The earlier observation of
"frames of 1–18 bytes" was an artefact of the magic search: recordings made without
`OpenAudio` contain no audio frames **[V]**.

### 4.5 Talk-back (sending sound to the camera)

1. Capture: microphone, `SampleRate` from `LoginDev` (8000), mono, 16-bit PCM; buffer
   `2 × minBufferSize`; read in chunks of `minBufferSize`.
2. The chunk is trimmed to an even length and encoded to **G.711 A-law** (n/2 bytes).
3. Packaging: `encodeAudioData(g711, channels=1, bits=16, rate, type=1 DATA)` →
   `PPCS_Write(sid, channel 1, …)` — the same channel as commands.
4. Stop: `encodeAudioData(null, 1, 16, rate, type=0 CLOSE)`.
5. The pace is set by the microphone, with no timer of its own.

The `OpenDevVoice` command is **not sent** — talk-back starts with the first audio packet.
Packet format — §1.3.5 (magic `A1 AF AF AF`, 24-byte audio sub-header).

---

## 5. SD-card files

### 5.1 File list

1. `GetTfVideoList{page, count}`: in the UI `count = 30`, `page` from 1.
2. Reply analysis: `result` < 0 → "no files" (−1), "no card" (−2), "card error" (−3).
3. Pages are merged with deduplication by `patch`.
4. There are more pages if `allCount > 0`, the accumulated count is < `allCount`, and the last
   page is not empty.
5. The next page is loaded when 4 items remain before the end of the list.
6. Time: `time × 1000 − time-zone offset` → local time **[P]**.
7. The card may also hold audio files (`.mp3`, `.wav`) — the UI distinguishes them by
   extension.

### 5.2 Download

#### 5.2.1 Parameters

| Constant | Value |
|---|---|
| Channel | **3** (`CHANNEL_DOWNLOAD`) |
| Read buffer | 262 144 bytes |
| Read timeout | 10 ms |
| No data — error | 12 000 ms (`NO_DATA_TIMEOUT_MS`) |
| Command reply timeout | 5000 ms |
| Concurrency | strictly one SD download at a time |
| Speed (AP mode) | **~375 KiB/s**, 8 MB in ~22 s — 6× faster than live video **[V]** |

**Channel-3 acknowledgements are mandatory [V].** Without acks the camera sends a window of
exactly 120 DRW packets and then only retransmits them. **No more than 64 indices per ack
packet [V]:** for acks with 77 and 108 indices the camera sends `Close` (`F1 F0`) and drops
the session in 1–2 s; with 64 or fewer, 8 MB downloads without errors. The app sends batches
of up to ~17 indices; up to 32 is safe. Whether to answer `ALIVE` (`F1 E0`) with `F1 E1` does
not matter for download: during active exchange the camera does not send ALIVE.

#### 5.2.2 Sequence

```
client                                                camera
  │ [session + LoginDev]                               │
  │ DownloadFile{pwd, userid:U, patch:P, pos:O, state:1}  ─►
  │ ◄─ {"cmd":"DownloadFile","state":0|1}              │   2 → file not found, −1 → error
  │ ◄══ channel 3: blocks of file data ══════════════  │   (ack every DRW fragment [P])
  │      … until the end marker (p4 == −1000) …        │
  │ DownloadFile{pwd, state:2}  ─►                     │   stop — on ANY exit path
  │ [close the session]                                │
```

* `U` — a random 1..100; the camera tags data blocks with it, and foreign blocks are dropped.
* `O` — the byte offset to resume from (0 — from the beginning).
* The stop request contains **only** `pwd` and `state:2`: `userid`, `patch`, `pos` are null
  and are not serialised.

#### 5.2.3 Block handling (`handleDownloadData(id, data, len, p4, p5)`)

1. Drop if `id` is not ours or `p5 != U`.
2. If `len > 0`: append to the file, `flush`, `downloaded += len`, report progress.
3. Done if `p4 == −1000` or (`size ≥ 1` and `downloaded ≥ size`), where `size` comes from the
   list.

#### 5.2.4 Completion and errors

| Situation | Result |
|---|---|
| Reply `state = 2` | FileNotFound (DeviceRejected) |
| Reply `state = −1` / other | DeviceRejected(state) |
| 12 s without data | DownloadTimeout |
| File write error | DownloadWriteFailed |
| Loop ended without an end marker | DownloadFailed (`download.incomplete`) |
| `PPCS_Read` error: −1 | CommandFailed |
| −3 (prolonged), −6 | Offline |
| −4, −8 | InvalidId |
| −16, −19, others | ReadFailed(code) |
| Cancelled by the user | DownloadCanceled |

After any outcome a stop (`state:2`) is sent and the session is closed.

#### 5.2.5 Channel 3 format and stop semantics

Blocks are Lu frames with a 25-byte header (§1.3.4): `userid` in u32@4, block number in
i32@8 (−1000 — the last one), length in u32@21, then the file bytes. Transport — DRW
`D1 03 <idx>`, indices from 0; the ack is mandatory and no longer than 64 indices (§5.2.1)
**[V]**. The content is exactly what the frames carry: the app neither transcodes it nor wraps
it in a container, and a resume by byte offset implies the card file byte for byte **[P]**.

After the stop (`state:2`) the camera **does not reply** to it and considers the transfer
active for another ~10–20 s: a new `DownloadFile` during that window gets `state:3` **[V]**.
An implementation should retry the start after a pause when it sees code 3.

### 5.3 Playback of a card file

The same controller as live, but instead of `OpenVideo`:

| Action | `PlaybackFile` request |
|---|---|
| start | `{pwd, userid:U, state:1, pos:startPos, patch:P}` |
| pause | `{…, state:2, pos:0}` |
| resume | `{…, state:3, pos:0}` |
| seek | `{…, state:4, pos:<position, ms>}` |
| stop | `{…, state:5, pos:0}` |

* `U` — a random 1..100; on a retry a value different from the previous one is used. Media
  frames are filtered by it (`handleVideoData`, second argument).
* Media flows on channel 0 in the same format as live; the frames carry a timestamp and a
  duration.
* `PlaybackFile` event (§2.6): `result ≠ 0` → error; `state = −1` before the first frame →
  one retry after 800 ms; `state = 4` → end of file.
* Timeouts: start — 30 000 ms, first frame — 12 000 ms.
* The end is also detected by a watchdog: polled once per 1000 ms; if the stream stalls for
  2000 ms within the last 4000 ms of the file — end of file.
* Position is computed from frame timestamps; the app picks the divisor (ms/µs)
  automatically from the step between frames — the timestamp units are not known in advance
  **[?]**. The units of `startPos` at start are **[?]**.

### 5.4 Deletion, formatting, settings

* **Deletion:** `DeleteTfFile{count:N, value:[{patch}…]}`, timeout 20 s, success
  `state == 0` **[P]**. The list is re-read after deletion.
* **Formatting:** `FormatCard`, timeout 30 s; `state` 0 — done, 1 — done but the camera must
  be rebooted (`AppointDev{state:1}`).
* **Recording parameters:** `GetDevTfParam` → form → `SetDevTfParam` with all six fields.

---

## 6. Wi-Fi provisioning over PPPP

Only the PPPP part is described. Bluetooth LE provisioning (chip AltoBeam ATBM6162,
GATT `181c`/`2a8a`/`2a90`) is a separate protocol between phone and camera and is out of
scope.

**AP network detection.** The phone takes the IPv4 address of its Wi-Fi interface (VPN
interfaces are skipped). An address starting with `192.168.100.`, `192.168.10.` or
`192.168.169.` means the phone is in the camera's **AP**; otherwise it is on the router
network; no address means no Wi-Fi. The SSID is normalised: quotes are stripped,
`<unknown ssid>` becomes empty.

**Wire sequence (AP → router):**

1. The phone is connected to the camera's Wi-Fi; SSID = virtual DID.
2. Checks: the phone is in the AP; DID = `realToVirtual(SSID)`, length 12; otherwise
   "device not found".
3. Password: from the local database or `123456`.
4. Connect with `bEnableLanSearch = 63`, timeout 15 000 ms; `LoginDev`.
5. `searchWiFiList` (15 s) → the list of networks visible to the camera. The password can be
   pre-filled from the saved value.
6. `OpenWifi{sid, wifiPwd, state:1}` — without waiting for a reply.
7. Wait for reconnection (`WifiReconnectPolicy`):
   1. wait until the phone leaves the camera AP — the camera shuts the AP down after
      switching;
   2. tear down the old session;
   3. pause 3000 ms (`SETTLE_DELAY_MS`);
   4. every 5000 ms — connect and log in through the router network;
   5. total limit 60 000 ms; on expiry — error 10001 and a "wait more / go to main screen"
      choice;
   6. PasswordError, InvalidId, Denied, ConnectOver — fail immediately, without retries.

App-level error codes: 48101 — transport failure while writing, 48102 — reconnection failed,
48103 — read failure. Whether the camera reboots after `OpenWifi` is not visible in the code.
Observation: when the camera is connected to a router, its AP is off **[V]**.

---

## 7. Session invariants and camera quirks

Hard-won rules; a violation looks like "the camera is silent" or "video does not play", not
like an explicit error.

1. **A channel-1 reply may span several DRW packets.** Assemble the stream (index order,
   dedup, ack) before splitting it into Lu frames; "one packet = one frame" drops multi-packet
   replies (`GetTfVideoList` for 30 files = 5 packets) **[V]**.
2. **Every command is sent 10 times ~11 ms apart** with the same DRW index; the index
   increments per command **[V]**.
3. **Media on channel 0 must be acked.** Without acks the camera keeps retransmitting the
   same fragments and the stream stalls.
4. **Channel 3: acks are mandatory and at most 64 indices per ack packet**; larger acks make
   the camera send `Close` and drop the session in 1–2 s **[V]**.
5. **Audio and video frames share channel 0.** Distinguish magics `01` and `02`; the audio
   length is at `u32@74` (the video's is at `u32@79`), and a parser that only checks the video
   layout would glue the audio frame to the previous video frame.
6. **The frame length is in the frame header** (`u32LE@79 + 87`, duplicate `u32LE@41 + 49`).
   Do not wait for the next `01 AF AF AF` magic to emit a frame.
7. **The SPS is `0x27`, not the usual `0x67`.** Do not patch SPS bytes assuming `0x67`.
8. **Width/height in the frame header are unreliable** (1280×720 while the SPS says
   1920×1080) — take the size from the SPS.
9. **Send `Close` (`F1 F0 00 00`) twice** before exiting, otherwise the camera stays busy
   **[V]**.
10. **The camera holds one active session.** Without `Close` the next connection — including
    the official app — hangs **[V]**; after an abandoned session the camera needs ~13 s.
11. **There is no "request key frame" command.** The stream starts at the nearest IDR, hence
    the 3.3–3.6 s startup **[V]**.
12. **`GetTfVideoList.time` is camera local time encoded as UTC.** Subtract the time-zone
    offset to display it.
13. **`LoginDev.result = −1` does not always mean a wrong password** — retry after 1–20 s.
14. **On a trailer mismatch, skip the magic and resync.** The native parser does not advance
    and loops forever.

---

## 8. Unresolved questions

Closed by disassembling the native frame parser (2026-11-06): channel-3 format (§1.3.4),
audio-frame headers (§1.3.3) and talk-back (§1.3.5), the time-zone byte (§1.3.1), the
video-frame header layout and `params[]` (§1.3.2).

Closed on a live camera (2026-11-06): channel-3 acks are mandatory, the 64-index ack limit,
the block format, resume, the busy code 3, multi-packet channel-1 replies (§1.3.4, §2.4,
§5.2).

| # | Question | Needed for | How to close |
|---|---|---|---|
| 1 | Meaning of byte `0x08` @12 of a download frame; how long exactly until the camera frees itself after a stop | §1.3.4, §5.2.5 | experiment |
| 2 | Audio data at `audioType=2` — AAC ADTS? What is in the 5-bit field u32@58 | §4.4 | `OpenAudio{state:1}` on a live camera |
| 3 | Does the camera accept talk-back from our client | §4.5 | experiment |
| 4 | Does the time-zone byte affect the camera | §2.4 | command with a different value |
| 5 | Units of `pos` during playback (`ts` is ms, `dur` is in the high bits) | §5.3 | experiment |
| 6 | Why 4K does not open; why the frame header says 1280×720 | §4.1 | try all `state`/`stream` combinations |
| 7 | Meaning of `flag` (alarm/LED), units of `GetCloseTime.values` | §3.6–3.7 | read from the camera |

---

## Appendix A. Constants

| Item | Values |
|---|---|
| Channels | 0 media, 1 commands/talk-back, 3 download |
| `bEnableLanSearch` | 126 normally, 63 in AP mode |
| Default camera password | `123456` (minimum length 6) |
| `OpenVideo.state` | 0 close, 1 main, 2 secondary; `stream` 1/2/3 |
| Quality in UI | HD = 1/1, SD = 2/2 (default), VGA = 2/3 |
| `MediaSwitch` | 0 off, 1 on |
| `audioType` | 0 G711A, 1 PCM, 2 AAC, 3 G711U; default `SampleRate` 8000 |
| Codec `params[3]` | 0/1/2 H.264 Baseline/Main/High, 3 H.265; key frame `frameType=1` |
| `PlaybackFile.state` | 1 start, 2 pause, 3 resume, 4 seek, 5 stop |
| `PlaybackFile` event | 0 ok, −1 start failed, 4 end |
| `DownloadFile.state` | request 1 start, 2 stop; reply 0 ok, 1 accepted, 2 no file, −1 error |
| End marker | `p4 = −1000` |
| TF card | 0 absent, 1 ready, 2 error |
| `GetTfVideoList.result` | 0 ok, −1 no files, −2 no card, −3 card error |
| `FormatCard.state` | 0 ok, 1 ok + reboot |
| `videoRes` | 0 HD, 1 MD; `videoCycle` 180/300/600/1800 s |
| `AppointDev.state` | 1 reboot, 2 factory reset, 3 time sync, 4 defaults |
| `TimeFlag.status` | 1 on, 2 off |
| `SetLedInfo.control` | 0 manual, 1 scheduled |
| `sensitivity` | 0..3 |
| Timeouts, ms | command 8000, login 8000, liveness 5000, Wi-Fi scan 15 000, deletion 20 000, list/format 30 000, `DownloadFile` 5000, `OpenLock` 2500, `updaetLockPwd` 2000, `selfDestruct` 5000 |
| Live | first frame 30 000, quality switch 350 + 500, resume 2500, warm session 300 000, decoder queue 15/30 frames |
| SD | page 30, prefetch at 4 left, no data 12 000, playback start 30 000, first frame 12 000, retry 800 |
| Wi-Fi | AP connect 15 000; after `OpenWifi` 3000 + poll 5000, total 60 000 |
| LAN search | total 9000, no new devices 4000, stall 2500, stop 3000 |

---

## Appendix B. Out of scope

Deliberately not described: the application's UI flows, local snapshot and recording, the
cloud API and push notifications, Bluetooth LE provisioning (a separate protocol between
phone and camera), QR-code parsing, and relay/server transport beyond the direct LAN path.
Firmware update is covered only as the negative finding in §3.7.1.
