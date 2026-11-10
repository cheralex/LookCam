# LookCam Service (`lookcam_deepsik`)

Сервис для 1–5 камер LookCam: **PPPP/PPCS без вендорского SDK**, веб-интерфейс с
живым просмотром, RTSP-мост для VLC/ffmpeg/Home Assistant/Frigate, настройки камеры
и SD-карта. Чистый Python 3.9+ (asyncio, стандартная библиотека), фронтенд —
ванильный JS без сборки.

Это реализация сервисной части проекта LookCam. Пакет **самодостаточен**:
протокольные помощники, фронтенд, конфиг, тесты и инструменты лежат внутри
репозитория; внешний пакет `lookcam` не требуется (его используемые модули
заморожены в `vendor/lookcam/`). Документация — `LookCam_app_spec.md`,
`spec/LookCam_protocol_en.md` и этот README.

## Что умеет

| Блок | Состояние |
|---|---|
| PPPP-ядро на asyncio: один UDP `:32108`, LanSearch, punch, команды, ack, Close | готово, тест на эмуляторе |
| Несколько камер, одна сессия на камеру, refcount зрителей, idle-TTL, backoff | готово |
| Веб: вход, сетка камер, просмотр, WebCodecs-плеер + Worker/OffscreenCanvas | готово |
| Двуязычный UI English/Русский, переключатель и `?lang=` | готово |
| MSE/fMP4-запасной плеер (`/ws/live-mp4`) | готово |
| RTSP 1.0: UDP и TCP-interleaved, RTP H.264/FU-A, RTCP SR, Digest | готово, тест на эмуляторе |
| Настройки камеры по реестру команд, действия, смена пароля, перезагрузка | готово |
| SD-карта: список, очередь скачиваний, докачка, Range-раздача, seek без полной загрузки, статус-бар буферизации в плеере | готово |
| Ручная запись в fMP4 и снимок (ffmpeg/cv2, иначе 501) | готово |
| Мастер Wi-Fi «AP → сеть роутера», LAN-поиск, добавление по DID | готово |
| Auth: PBKDF2-пароль администратора, HMAC-cookie, RTSP Digest | готово |
| Поставка: systemd, Docker (host network), задача Планировщика Windows | готово |

**Не входит в первую версию:** звук, talk-back, потоковое воспроизведение с SD,
4K, BLE-провижининг, ONVIF.

## Быстрый старт

Репозиторий **самодостаточен**: его корень — каталог `lookcam_deepsik`
(именно он клонируется из GitHub). Запуск из корня репозитория:

```bash
cd lookcam_deepsik
python -m service
# или
python run.py
```

Пакет не требует родительского каталога `lookcam_py` и внешнего `lookcam`.

Конфиг по умолчанию — `config/lookcam_service.json`; кэш — `lookcam_data`.
Другой конфиг: `--config путь.json`.

Откройте `http://<ПК>:8080/`: при первом запуске сервис попросит задать пароль
администратора. Добавьте камеру по DID (виртуальному, как в приложении) и паролю
камеры, либо нажмите «LanSearch».

Порты:

| Порт | Что | Обязательно |
|---|---|---|
| UDP 32108 | PPPP: LanSearch, punch, DRW-каналы 0/1/3 | да, камера отвечает только с этого порта |
| TCP 8080 | HTTP/WebSocket | да |
| TCP 8554 | RTSP | если нужен внешний NVR |

> UDP 32108 должен быть свободен: старый `lookcam p2p`/`watch` и второй экземпляр
> сервиса его занимают. Камера держит одну сессию, поэтому одновременно с сервисом
> официальное приложение не подключится.

## Обнаружение камер и добавление

Порт **UDP 32108 должен быть свободен**. Пока сервис запущен, отдельный
`--discover` не запустится — используйте UI/API уже запущенного сервиса.

**1. Быстрый поиск без сервиса** (порт 32108 свободен; в AP-режиме сначала
подключите ПК к Wi-Fi камеры):

```bash
cd lookcam_deepsik
python -m service --discover --timeout 5
```

Старый `python -m lookcam pppp --host 192.168.0.255` тоже работает, но он внешний
по отношению к пакету и печатает только части DID (`GHBB…URFKG`). Сервис собирает
полный DID и печатает его в `--discover`.

**2. В веб-интерфейсе** (сервис уже запущен): «Добавить» → «LanSearch».
Сервис собирает полный DID из PunchPkt (`prefix`, `suffix` и числовая середина
`value`: `GHBB…URFKG` + `0x867C8 = 550856` → `G550856URFKG`). Кнопка
«Заполнить» подставляет DID и `host_hint`, остаётся ввести пароль камеры.
Если камера уже добавлена, строка помечается «уже добавлена».

**3. Через API** (cookie после `/api/login`, либо `"auth": false`):

```bash
curl -c cookies -X POST http://127.0.0.1:8080/api/login \
     -H 'Content-Type: application/json' -d '{"password":"пароль-админа"}'

curl -b cookies -X POST http://127.0.0.1:8080/api/discover \
     -H 'Content-Type: application/json' -d '{"timeout":3}'

# ответ devices: ip, port, did="G550856URFKG", did_partial="GHBB…URFKG",
#                 prefix, suffix, value, camera_id?
curl -b cookies -X POST http://127.0.0.1:8080/api/cameras \
     -H 'Content-Type: application/json' \
     -d '{"id":"urfkg","name":"Камера","did":"G550856URFKG",
          "password":"265611","host_hint":"192.168.0.147","quality":"HD"}'
```

`did` — виртуальный DID из приложения/SSID (12 символов); discovery сам собирает
его из трёх частей PunchPkt. `host_hint` необязателен: камера ищется по DID, а
адрес после DHCP может измениться. Полностью автоматически добавить камеру
нельзя только из-за пароля — его вводит пользователь (по умолчанию у новой
камеры `123456`). Если DID собрать не удалось (нестандартный префикс/значение),
он вводится вручную; в AP-режиме SSID = виртуальный DID.

## Конфиг

`config/lookcam_service.example.json` — пример. Сервис создаёт
`config/lookcam_service.json` внутри пакета, если файла нет. Пароли камер хранятся
в открытом виде (нужны для `LoginDev`), файл пишется атомарно с правами `0600`
на Linux и владельческим ACL на Windows. **В git его не коммитить.**
Относительный `cache_dir` считается от каталога конфига, поэтому
`"../lookcam_data"` даёт `lookcam_deepsik/lookcam_data`.

```json
{
  "http": {"bind": "0.0.0.0", "port": 8080},
  "rtsp": {"enable": true, "bind": "127.0.0.1", "port": 8554,
           "user": "viewer", "password_hash": ""},
  "admin": {"password_hash": "", "auth": true},
  "cache_dir": "../lookcam_data",
  "cache_limit_mb": 4096,
  "idle_ttl_s": 30,
  "cameras": [
    {"id": "garage", "name": "Гараж", "did": "G016638JHDXM",
     "password": "…", "host_hint": "192.168.100.1", "quality": "HD",
     "keep_alive": false}
  ]
}
```

* `quality`: `HD` (`state=1,stream=1`, 1080p), `SD` (`2/2`, 640×360),
  `VGA` (`2/3`). Смена качества останавливает поток на ~350 мс и
  переоткрывает его; подписчики получают новый SPS.
* `keep_alive`: держать PPPP-сессию постоянно, даже без зрителей.
* `idle_ttl_s`: сколько секунд держать тёплой сессию после ухода последнего
  зрителя.
* `admin.auth=false` — выключить вход (только доверенная сеть).
* `debug_raw=true` — включить `POST /api/cameras/{id}/raw` с произвольной командой.

Первый запуск (`/api/setup`) разрешён **только с localhost**: откройте
`http://127.0.0.1:8080/` на самой машине (или выполните запрос через SSH-туннель).
С другого LAN-хоста создать администратора нельзя — это защита от захвата
аккаунта.

## RTSP

По умолчанию RTSP слушает **только `127.0.0.1:8554`**. Для доступа с другого
хоста задайте `rtsp.bind: "0.0.0.0"` **и обязательно** Digest-пароль; сервер
откажется стартовать с не-loopback bind без пароля.

```
rtsp://<сервер>:8554/<id>
rtsp://viewer:<пароль>@<сервер>:8554/<id>
```

Для клиента с **нулевым сетевым буфером** (VLC `network-caching=0`,
`ffplay -fflags nobuffer`) RTSP по умолчанию работает в low-latency режиме:
сервер не проигрывает накопленный GOP из кэша, а ждёт ближайший IDR и отдаёт
только живой поток. Старт занимает до длины GOP (~3.5 с), зато нет залпового
выброса старых кадров, на котором zero-buffer плеер зависает.

Если нужен мгновенный старт с GOP-кэшем (NVR/клиент с запасом буфера), отключите
low-latency явно:

```
rtsp://<сервер>:8554/<id>?lowlatency=0
```

или поставьте в конфиге `"rtsp": {"low_latency": false}`.

`<id>` — короткое имя камеры из конфига. DESCRIBE поднимает сессию и ждёт SPS до
8 с; в режиме с кэшем новый клиент получает поток с последнего IDR. SPS/PPS идут и в
SDP (`sprop-parameter-sets`), и в потоке перед IDR — это переживает смену качества
без перезапуска `ffplay`/VLC. В ответах объявляется `Session: …;timeout=60`, а
молчащий PLAY-клиент больше не отключается через 60 с: RTSP-сессия живёт до
TEARDOWN/закрытия соединения, RTP при этом продолжает идти. Проверено в тесте
встроенным RTSP-клиентом (`vendor/lookcam/rtsp.py`) по TCP и UDP, побайтная
сверка NAL.

Чтобы включить Digest, задайте пользователя и пароль:

```bash
cd lookcam_deepsik
python - <<'PY'
from service.config import ConfigStore
c = ConfigStore("config/lookcam_service.json")
c.set_rtsp_access("viewer", "секрет")
PY
```

После этого можно поставить `rtsp.bind: "0.0.0.0"` и открыть порт для NVR.

## HTTP/WebSocket API

Всё кроме `/api/state`, `/api/login`, `/api/setup`, `/api/health` и статики —
за cookie-сессией администратора.

| Метод и путь | Что |
|---|---|
| `POST /api/login`, `POST /api/logout`, `POST /api/setup` | вход/выход/первый запуск |
| `GET /api/cameras`, `POST /api/cameras`, `PATCH/DELETE /api/cameras/{id}` | камеры |
| `POST /api/discover` | LanSearch по всем интерфейсам |
| `GET /api/cameras/{id}/info` | `GetDevInfo` + `GetDevStream` + статистика |
| `GET/PUT /api/cameras/{id}/settings/{group}` | `image`, `light`, `alarm`, `tf`, `power`, `exposure` |
| `POST /api/cameras/{id}/actions/{action}` | `reboot`, `reset`, `restore`, `timesync`, `password`, `connect`, `disconnect`, `led`, `light`, `flip_h`, `flip_v` |
| `GET /api/cameras/{id}/sd` | `GetTfVideoList` с дедупликацией |
| `POST /api/cameras/{id}/sd/download`, `GET /api/downloads`, `DELETE /api/downloads/{n}` | очередь скачиваний |
| `POST /api/cameras/{id}/sd/seek` | подготовить просмотр с `pos_ms` (без полной загрузки) |
| `GET /media/{id}/{file}` | файл из кэша с Range; для seek — догрузка недостающих диапазонов |
| `POST/DELETE /api/cameras/{id}/record` | ручная запись в MP4 |
| `GET /api/cameras/{id}/snapshot.jpg` | снимок (нужен ffmpeg или cv2, иначе 501) |
| `GET /api/provision/state`, `POST /api/provision/{start,scan,apply,cancel}` | мастер Wi-Fi |
| `GET /ws/live/{id}` | бинарный поток WebCodecs |
| `GET /ws/live-mp4/{id}` | fMP4 для MSE |
| `GET /ws/events` | состояния камер, скачивания, занятость |

`/ws/live/{id}`: `[u8 тип][u8 флаги][u32 ts_ms BE][payload]`, тип 1 — Annex B AU,
2 — JSON-конфиг (`codec`, `width`, `height`, `sps`, `pps` hex), 3 — аудио
(зарезервировано). Управляющие события (`reset` при смене качества) — текст JSON.

### SD: просмотр с произвольного места

```
POST /api/cameras/{id}/sd/seek
{"patch": "/mnt/.../2026-1110-122648.00302486.mp4",
 "name": "2026-1110-122648.mp4", "size": 19567619, "pos_ms": 60000}
```

Сервис скачивает хвост с `moov`, переносит `moov` в начало fast-start файла,
сдвигает `stco`/`co64`, качает head и около 3 МБ с keyframe ≤ `pos_ms`, затем
отдаёт `/media/{id}/{name}`. Браузер может сразу установить `currentTime` на
`keyframe_ms`; недостающие диапазоны `/media` догружает через `DownloadFile.pos`
по мере Range-запросов. Полный файл для старта не нужен.

Кэш — `<имя>.seek` + `.seek.json`; keyframe выбирается не позже запрошенного
времени (сетка ~3 с), поэтому фактический старт может быть чуть раньше `pos_ms`.
В UI на странице SD у каждого файла есть кнопка «Смотреть с…» / «Watch from…».
Плеер открывается сразу: под видео показывается строка «Готовлю видео… N с»,
затем «Буферизация…», процент буферизации и время; для уже скачанного файла —
«Загружаю видео…»/«Буферизация…».

## Архитектура

```
lookcam_deepsik/        корень git-репозитория
  service/       PPPP-ядро, HTTP/WebSocket, RTSP, камеры, SD, API
  service/static/ index.html, app.js, i18n.js, player-*.js
  service/mp4_index.py  разбор moov/stco/stss для remote seek
  vendor/lookcam/ замороженные sps/net/rtsp/p2p/webcodecs/pppp
  config/        lookcam_service.json и пример
  lookcam_data/  кэш (SD, записи, снимки)
  tests/         эмулятор и test_service.py + фикстуры
  tools/         живые проверки и check_service_js.py
  deploy/        systemd, Docker, задача Windows
  run.py         запуск из каталога пакета
```

Прикладные примитивы сервиса написаны заново; из старого клиента завендорены
только проверенные `sps` (патч SPS), `net` (интерфейсы/маршруты) и клиенты
`rtsp`/`p2p`/`webcodecs` для тестов. Внешний пакет `lookcam` не импортируется.

## Язык интерфейса

Интерфейс двуязычный: **English / Русский**. Переключатель — в верхней панели
(на экране входа — в карточке входа), выбор сохраняется в `localStorage`; при
первом входе язык берётся из браузера. Можно принудительно открыть страницу с
`?lang=en` или `?lang=ru`.

Строки UI лежат в `service/static/i18n.js`; подписи настроек и действий API
отдаёт сразу на двух языках (`label` + `label_en`, `title_en`, `hint_en`).
Технические сообщения сервера, для которых есть перевод, проходят через
`translateServer()`; неизвестные показываются как есть.

Код, комментарии/докстринги, **логи и CLI-вывод** — на английском; русские
строки остались в сообщениях API/протокола (для русского UI) и в тестах.

## Тесты

```bash
cd lookcam_deepsik
python tests/test_service.py          # сквозной: 2 эмулятора → сервис
python tools/check_service_js.py      # node --check JS-модулей
node tools/check_i18n.mjs             # словари EN/RU без браузера
```

Регресс старого клиента (`lookcam_py/tests/...`) в этот репозиторий не входит;
сервис от него не зависит. `tests/test_service.py` (74 проверки) проверяет: две камеры параллельно, config и
видеокадры по WS, побайтовое совпадение P-кадров с AU камеры, MSE/fMP4-поток,
настройки «чтение → запись → чтение», скачивание SD с SHA-1, Range, ack каналов
0/1/3, RTSP TCP и UDP с побайтовой сверкой NAL, fMP4 обратным разбором и запись,
политику очередей хаба, снимок через cv2, Wi-Fi-мастер, перезапуск камеры с
автоматическим возвратом потока, auth и idle-TTL с двумя `Close`.

## Поставка

* `deploy/lookcam.service` — systemd (Linux): пакет ставится в
  `/opt/lookcam/lookcam_deepsik`, `WorkingDirectory=/opt/lookcam`;
* `deploy/Dockerfile`, `deploy/docker-compose.yml` — контекст сборки сам пакет
  `lookcam_deepsik`, только `network_mode: host`, иначе broadcast LanSearch не
  дойдёт до камеры; конфиг и `lookcam_data` монтируются из пакета;
* `deploy/install_windows_task.ps1` — задача Планировщика Windows (рабочий
  каталог — родитель пакета, конфиг — `lookcam_deepsik\config\...`);
* `.dockerignore` исключает кэш и секретный `config/lookcam_service.json`;
* логирование с ротацией: `--log-file`, уровень `--log-level`.

## Живая камера: результаты и что осталось

На эмуляторе всё зелёное. **2026-11-10 проверено на живой `G550856URFKG`
(`192.168.0.147`, пароль из локального конфига): 18/18** — HTTP/WS, RTSP TCP и
UDP, чтение настроек, список SD, снимок, запись fMP4, смена качества
HD→SD→HD и idle-TTL. Скрипты: `tools/live_service_check.py`,
`tools/live_full_check.py`, `tools/live_sd_check.py` (пароль — через
`LOOKCAM_PASSWORD`).

Отдельная ловушка: **`PunchPkt.value` у разных камер разный** (`0x40FE` у старой,
`0x867C8` у URFKG), и с чужим значением камера не отвечает `P2PRdy`. Сервис берёт
значение из ответа LanSearch; при провале punch повторяет поиск заново.

Для SD: канал 3 собирается как поток (блок 6096 байт приходит несколькими
DRW-пакетами), а `.temp` перед докачкой выравнивается по 6096 байт — иначе MP4
склеивается со сдвигом. Проверено на живом файле 20 МБ: полное скачивание и
докачка дают одинаковый SHA-1, 3000 кадров декодируются.

Перед боевым использованием осталось проверить:

1. `ffplay -rtsp_transport tcp rtsp://…:8554/<id>` и `ffmpeg -rtsp_transport udp
   -i … -c copy out.mp4` (файл должен проигрываться целиком), VLC/Frigate/go2rtc.
2. Смену качества из UI при подключённом `ffplay` (в тесте проверено на WS).
3. Скачивание SD во время live: после серии скачиваний камера могла перестать
   отдавать live — сервис на время скачивания ставит поток на паузу и снимает
   её после, а после SD-выгрузки закрывает сессию.
4. Перезагрузку камеры из UI: сервис переподключается с backoff.
5. Пять камер одновременно и нагрузку CPU/сети.
6. WebCodecs в настоящем браузере (headless-Chrome в песочнице не запускается).
7. «Занятость»: пока сервис держит сессию, официальное приложение не подключится
   (это видно в UI); после `idle_ttl_s` сессия закрывается двумя `Close`.
