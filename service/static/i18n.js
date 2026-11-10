// Minimal client-side i18n: English/Russian UI, no build step.
const STORAGE_KEY = 'lookcam-lang';
const SUPPORTED = ['en', 'ru'];

const EN = {
  'nav.cameras': 'Cameras',
  'nav.add': 'Add camera',
  'nav.wifi': 'Camera Wi-Fi',
  'nav.logout': 'Sign out',
  'common.loading': 'Loading…',
  'common.save': 'Save',
  'common.cancel': 'Cancel',
  'common.retry': 'Retry',
  'common.refresh': 'Refresh',
  'common.backCameras': '← Cameras',
  'common.copy': 'Copy',
  'common.download': 'Download',
  'common.watch': 'Watch',
  'common.settings': 'Settings',
  'common.sd': 'SD card',
  'common.start': 'Start',
  'common.add': 'Add',
  'common.more': 'More',
  'common.delete': 'Delete',
  'common.deleteSelected': 'Delete selected',
  'common.none': 'None',
  'common.close': 'Close',
  'common.off': 'Off',
  'common.on': 'On',
  'common.seconds': '{n} s',
  'common.fps': '{n} fps',
  'common.kbps': '{n} kbit/s',
  'common.kb': '{n} KB',
  'common.pcs': '{n} pcs',
  'common.online': 'online',
  'common.offline': 'offline',
  'common.yes': 'yes',
  'common.no': 'no',
  'common.error': 'error',
  'common.busy': 'busy: {what}',
  'gate.setupTitle': 'First run: set the administrator password.',
  'gate.password': 'Password (at least 6 characters)',
  'gate.repeat': 'Repeat password',
  'gate.loginTitle': 'Administrator sign-in',
  'gate.login': 'Sign in',
  'gate.passwordMismatch': 'Passwords do not match',
  'gate.noService': 'Service is not responding: {error}',
  'cams.title': 'Cameras',
  'cams.count': '{n} pcs.',
  'cams.multi': 'multi-view',
  'cams.add': 'Add camera',
  'cams.empty': 'No cameras yet.',
  'cams.wifiWizard': 'Wi-Fi wizard',
  'cams.multiOff': 'multi-view is off',
  'cams.copyDone': 'Copied: {text}',
  'view.quality': 'Quality: {q}',
  'view.snapshot': 'Snapshot',
  'view.record': 'Record on server',
  'view.stopRecord': 'Stop recording',
  'view.settings': 'Settings',
  'view.sd': 'SD card',
  'view.led': 'LED',
  'view.ir': 'IR light',
  'view.flipH': 'Flip ↔',
  'view.flipV': 'Flip ↕',
  'view.rtspCopy': 'Copy RTSP',
  'view.snapshotUnavailable': 'Snapshot unavailable: {error}',
  'view.recordingFile': 'recording: {file}',
  'view.recording': 'recording: {frames} frames, {size}',
  'view.recordStopped': 'recording: stopped',
  'view.recordRunning': 'recording: running',
  'view.recordError': 'recording: {state} — {error}',
  'view.infoLoading': 'loading camera details…',
  'view.infoError': 'could not load details: {error}',
  'view.actionDone': 'Done: {action}',
  'view.stats': '{fps} fps  {width}×{height}',
  'info.did': 'DID',
  'info.firmware': 'firmware',
  'info.ip': 'Camera IP',
  'info.wifi': 'Wi-Fi',
  'info.battery': 'battery',
  'info.stream': 'stream',
  'info.fps': 'frames/s',
  'info.bitrate': 'bitrate',
  'info.brightness': 'brightness',
  'info.saturation': 'saturation',
  'info.session': 'session',
  'info.subscribers': 'subscribers',
  'settings.title': 'Settings',
  'settings.applies': 'changes are applied to the camera immediately',
  'settings.maintenance': 'Maintenance',
  'settings.loading': 'loading…',
  'settings.readError': 'could not read settings: {error}',
  'settings.noFields': 'no fields',
  'settings.save': 'Save',
  'settings.onlyChanged': 'only changed fields are sent',
  'settings.noChanges': 'No changes',
  'settings.saved': 'Saved: {values}',
  'settings.done': 'Done: {action}',
  'settings.commandSent': 'Command sent: {action}',
  'actions.timesync': 'Sync time',
  'actions.changePassword': 'Change camera password',
  'actions.reboot': 'Reboot camera',
  'actions.reset': 'Factory reset',
  'actions.restore': 'Restore default settings',
  'actions.warning': 'Reset and restore are irreversible. The stream will be stopped.',
  'actions.confirm': 'Run “{action}”? This action runs on the camera.',
  'actions.resetConfirm': 'Really reset the camera to factory settings?',
  'actions.currentPassword': 'Current camera password:',
  'actions.newPassword': 'New password (at least 6 characters):',
  'sd.title': 'SD card',
  'sd.loading': 'loading file list…',
  'sd.refresh': 'Refresh',
  'sd.format': 'Format',
  'sd.files': 'files: {n}',
  'sd.filesOf': 'files: {n} of {all}',
  'sd.file': 'file',
  'sd.time': 'time',
  'sd.size': 'size',
  'sd.noFiles': 'no files',
  'sd.loadError': 'Could not load the list: {error}',
  'sd.download': 'Download',
  'sd.queued': 'Queued: {name}',
  'sd.deleted': 'Deleted',
  'sd.noneSelected': 'Nothing selected',
  'sd.deleteConfirm': 'Delete files: {n}?',
  'sd.formatConfirm': 'Formatting will delete all files on the card. Continue?',
  'sd.formatConfirm2': 'Are you sure? This is irreversible.',
  'sd.formatting': 'The card is being formatted',
  'sd.downloads': 'Downloads',
  'sd.queueEmpty': 'queue is empty',
  'sd.play': 'Watch',
  'sd.cancel': 'Cancel',
  'sd.seek': 'Watch from…',
  'sd.seekPrompt': 'Start at second (0 = beginning):',
  'sd.seekPreparing': 'Preparing range…',
  'sd.seekError': 'Could not prepare playback: {error}',
  'player.preparing': 'Preparing video…',
  'player.preparingSec': 'Preparing video… {s} s',
  'player.loading': 'Loading video…',
  'player.buffering': 'Buffering…',
  'player.stalled': 'Waiting for data…',
  'player.playing': 'Playing',
  'player.ended': 'Finished',
  'player.buffered': 'Buffered {end} / {total}',
  'player.error': 'Playback error: {error}',
  'player.unknown': 'unknown error',
  'add.title': 'Add camera',
  'add.discover': 'LanSearch',
  'add.did': 'DID (virtual, as in the app)',
  'add.name': 'Name',
  'add.namePlaceholder': 'Garage',
  'add.partial': 'partial',
  'add.port': 'port',
  'add.password': 'Camera password',
  'add.quality': 'Quality',
  'add.host': 'Address hint (optional)',
  'add.add': 'Add',
  'add.apWizard': 'Camera in AP mode? Wi-Fi wizard →',
  'add.help': 'LanSearch returns the DID prefix, suffix and numeric middle; the service reconstructs the full DID (for example {example}). If reconstruction fails, enter the DID from the app/SSID manually. Use the camera password, not the service administrator password.',
  'add.found': 'Found: {n}',
  'add.alreadyAdded': 'already added: {id}',
  'add.fill': 'Fill in',
  'add.noReply': 'No one replied. Check the network and VPN.',
  'add.fillHelp': '“Fill in” sets the full DID and address when the service could reconstruct the DID from PunchPkt; otherwise enter the DID manually.',
  'add.didSet': 'DID and address filled in: {did}',
  'add.addressSet': 'Address filled in; enter the full DID and password',
  'add.added': 'Camera added',
  'wifi.title': 'Wi-Fi wizard',
  'wifi.loading': 'loading state…',
  'wifi.prepare': 'check',
  'wifi.connecting': 'connecting',
  'wifi.scanning': 'scanning',
  'wifi.choose': 'choose network',
  'wifi.applying': 'transfer',
  'wifi.waiting': 'waiting',
  'wifi.done': 'done',
  'wifi.needAp': 'Connect the computer to the camera Wi-Fi (its name is the camera DID), then enter the DID and camera password.',
  'wifi.did': 'Camera DID',
  'wifi.password': 'Camera password (or 123456)',
  'wifi.start': 'Start',
  'wifi.ssid': 'Home network SSID',
  'wifi.netPassword': 'Home network password',
  'wifi.apply': 'Move camera',
  'wifi.cancel': 'Cancel',
  'wifi.ip': 'Camera IP: {ip}',
  'wifi.back': '← Cameras',
  'wifi.error': 'Error: {error}',
  'wifi.searching': 'searching for cameras (3 s)…',
  'state.offline': 'offline',
  'state.connecting': 'connecting',
  'state.online': 'online',
  'state.streaming': 'live',
  'state.auth_error': 'wrong password',
  'state.error': 'error',
  'state.busy': 'busy',
  'unit.b': 'B',
  'unit.kb': 'KB',
  'unit.mb': 'MB',
  'unit.gb': 'GB',
  'lang.name': 'English',
  'player.noWebcodecs': 'WebCodecs H.264 is unavailable — switching to MSE',
  'player.mseConnecting': 'MSE player connecting…',
  'player.connecting': 'connecting…',
  'player.wsError': 'WS error',
  'player.openWaiting': 'stream open, waiting for frames',
  'player.closed': 'connection closed',
  'player.connectionError': 'connection error',
  'player.reset': 'reset: {reason}',
  'player.streamRestarted': 'stream restarted',
  'player.cameraError': 'camera error',
  'player.badConfig': 'broken config',
  'player.decoder': 'decoder: {error}',
  'player.decoderStart': 'could not start decoder: {error}',
  'mse.connecting': 'MSE: connecting…',
  'mse.waitInit': 'MSE: waiting for init',
  'mse.connectionError': 'MSE: connection error',
  'mse.error': 'MSE: error',
  'mse.unsupported': 'Browser does not support MSE',
  'worker.noWebcodecs': 'WebCodecs is not available in the Worker',
  'worker.noCanvas': 'no canvas',
};

const RU = {
  'nav.cameras': 'Камеры',
  'nav.add': 'Добавить',
  'nav.wifi': 'Wi-Fi камеры',
  'nav.logout': 'Выйти',
  'common.loading': 'загрузка…',
  'common.save': 'Сохранить',
  'common.cancel': 'Отмена',
  'common.retry': 'Повторить',
  'common.refresh': 'Обновить',
  'common.backCameras': '← Камеры',
  'common.copy': 'Копировать',
  'common.download': 'Скачать',
  'common.watch': 'Смотреть',
  'common.settings': 'Настройки',
  'common.sd': 'SD-карта',
  'common.start': 'Начать',
  'common.add': 'Добавить',
  'common.more': 'Ещё',
  'common.delete': 'Удалить',
  'common.deleteSelected': 'Удалить выбранные',
  'common.none': 'Нет',
  'common.close': 'Закрыть',
  'common.off': 'выключено',
  'common.on': 'включено',
  'common.seconds': '{n} с',
  'common.fps': '{n} кадр/с',
  'common.kbps': '{n} кбит/с',
  'common.kb': '{n} КБ',
  'common.pcs': '{n} шт.',
  'common.online': 'онлайн',
  'common.offline': 'офлайн',
  'common.yes': 'да',
  'common.no': 'нет',
  'common.error': 'ошибка',
  'common.busy': 'занята: {what}',
  'gate.setupTitle': 'Первый запуск: задайте пароль администратора.',
  'gate.password': 'Пароль (не короче 6 символов)',
  'gate.repeat': 'Повторите пароль',
  'gate.loginTitle': 'Вход администратора',
  'gate.login': 'Войти',
  'gate.passwordMismatch': 'Пароли не совпадают',
  'gate.noService': 'Сервис не отвечает: {error}',
  'cams.title': 'Камеры',
  'cams.count': '{n} шт.',
  'cams.multi': 'мульти-просмотр',
  'cams.add': 'Добавить камеру',
  'cams.empty': 'Камер пока нет.',
  'cams.wifiWizard': 'мастер Wi-Fi',
  'cams.multiOff': 'мульти-просмотр выключен',
  'cams.copyDone': 'Скопировано: {text}',
  'view.quality': 'Качество: {q}',
  'view.snapshot': 'Снимок',
  'view.record': 'Запись на сервере',
  'view.stopRecord': 'Остановить запись',
  'view.settings': 'Настройки',
  'view.sd': 'SD-карта',
  'view.led': 'LED',
  'view.ir': 'Подсветка',
  'view.flipH': 'Отразить ↔',
  'view.flipV': 'Отразить ↕',
  'view.rtspCopy': 'Копировать RTSP',
  'view.snapshotUnavailable': 'Снимок недоступен: {error}',
  'view.recordingFile': 'запись: {file}',
  'view.recording': 'запись: {frames} кадров, {size}',
  'view.recordStopped': 'запись: остановлена',
  'view.recordRunning': 'запись: идёт',
  'view.recordError': 'запись: {state} — {error}',
  'view.infoLoading': 'загружаю сведения о камере…',
  'view.infoError': 'не удалось получить сведения: {error}',
  'view.actionDone': 'Готово: {action}',
  'view.stats': '{fps} кадр/с  {width}×{height}',
  'info.did': 'DID',
  'info.firmware': 'прошивка',
  'info.ip': 'IP камеры',
  'info.wifi': 'Wi-Fi',
  'info.battery': 'батарея',
  'info.stream': 'поток',
  'info.fps': 'кадров/с',
  'info.bitrate': 'битрейт',
  'info.brightness': 'яркость',
  'info.saturation': 'насыщенность',
  'info.session': 'сессия',
  'info.subscribers': 'подписчики',
  'settings.title': 'Настройки',
  'settings.applies': 'изменения применяются к камере сразу',
  'settings.maintenance': 'Обслуживание',
  'settings.loading': 'загружаю…',
  'settings.readError': 'не удалось прочитать настройки: {error}',
  'settings.noFields': 'полей нет',
  'settings.save': 'Сохранить',
  'settings.onlyChanged': 'меняются только тронутые поля',
  'settings.noChanges': 'Нет изменений',
  'settings.saved': 'Сохранено: {values}',
  'settings.done': 'Готово: {action}',
  'settings.commandSent': 'Команда отправлена: {action}',
  'actions.timesync': 'Синхронизировать время',
  'actions.changePassword': 'Сменить пароль камеры',
  'actions.reboot': 'Перезагрузить камеру',
  'actions.reset': 'Сброс к заводским настройкам',
  'actions.restore': 'Восстановить настройки по умолчанию',
  'actions.warning': 'Сброс и восстановление необратимы. Поток будет остановлен.',
  'actions.confirm': 'Выполнить «{action}»? Это действие на камере.',
  'actions.resetConfirm': 'Точно сбросить камеру к заводским настройкам?',
  'actions.currentPassword': 'Текущий пароль камеры:',
  'actions.newPassword': 'Новый пароль (не короче 6 символов):',
  'sd.title': 'SD-карта',
  'sd.loading': 'загружаю список файлов…',
  'sd.refresh': 'Обновить',
  'sd.format': 'Форматировать',
  'sd.files': 'файлов: {n}',
  'sd.filesOf': 'файлов: {n} из {all}',
  'sd.file': 'файл',
  'sd.time': 'время',
  'sd.size': 'размер',
  'sd.noFiles': 'файлов нет',
  'sd.loadError': 'Не удалось получить список: {error}',
  'sd.download': 'Скачать',
  'sd.queued': 'В очередь: {name}',
  'sd.deleted': 'Удалено',
  'sd.noneSelected': 'Ничего не выбрано',
  'sd.deleteConfirm': 'Удалить файлов: {n}?',
  'sd.formatConfirm': 'Форматирование удалит все файлы на карте. Продолжить?',
  'sd.formatConfirm2': 'Точно? Это необратимо.',
  'sd.formatting': 'Карта форматируется',
  'sd.downloads': 'Скачивания',
  'sd.queueEmpty': 'очередь пуста',
  'sd.play': 'Смотреть',
  'sd.cancel': 'Отмена',
  'sd.seek': 'Смотреть с…',
  'sd.seekPrompt': 'Начать с секунды (0 — с начала):',
  'sd.seekPreparing': 'Готовлю диапазон…',
  'sd.seekError': 'Не удалось подготовить воспроизведение: {error}',
  'player.preparing': 'Готовлю видео…',
  'player.preparingSec': 'Готовлю видео… {s} с',
  'player.loading': 'Загружаю видео…',
  'player.buffering': 'Буферизация…',
  'player.stalled': 'Жду данные…',
  'player.playing': 'Воспроизведение',
  'player.ended': 'Завершено',
  'player.buffered': 'Буферизовано {end} / {total}',
  'player.error': 'Ошибка воспроизведения: {error}',
  'player.unknown': 'неизвестная ошибка',
  'add.title': 'Добавить камеру',
  'add.discover': 'LanSearch',
  'add.did': 'DID (виртуальный, как в приложении)',
  'add.name': 'Имя',
  'add.namePlaceholder': 'Гараж',
  'add.partial': 'частично',
  'add.port': 'порт',
  'add.password': 'Пароль камеры',
  'add.quality': 'Качество',
  'add.host': 'Адрес-подсказка (необязательно)',
  'add.add': 'Добавить',
  'add.apWizard': 'Камера в режиме AP? Мастер Wi-Fi →',
  'add.help': 'LanSearch возвращает префикс, суффикс и числовую середину DID; сервис собирает полный DID (например {example}). Если собрать не удалось, введите DID из приложения/SSID вручную. Нужен пароль камеры, не администратора сервиса.',
  'add.found': 'Найдено: {n}',
  'add.alreadyAdded': 'уже добавлена: {id}',
  'add.fill': 'Заполнить',
  'add.noReply': 'Никто не ответил. Проверьте сеть и VPN.',
  'add.fillHelp': '«Заполнить» подставляет полный DID и адрес, если сервис смог восстановить DID по PunchPkt; иначе введите DID вручную.',
  'add.didSet': 'DID и адрес подставлены: {did}',
  'add.addressSet': 'Адрес подставлен; введите полный DID и пароль',
  'add.added': 'Камера добавлена',
  'wifi.title': 'Мастер Wi-Fi',
  'wifi.loading': 'загружаю состояние…',
  'wifi.prepare': 'проверка',
  'wifi.connecting': 'подключение',
  'wifi.scanning': 'поиск сетей',
  'wifi.choose': 'выбор сети',
  'wifi.applying': 'передача',
  'wifi.waiting': 'ожидание',
  'wifi.done': 'готово',
  'wifi.needAp': 'Подключите компьютер к Wi-Fi камеры (он называется как её DID), затем укажите DID и пароль камеры.',
  'wifi.did': 'DID камеры',
  'wifi.password': 'Пароль камеры (или 123456)',
  'wifi.start': 'Начать',
  'wifi.ssid': 'SSID домашней сети',
  'wifi.netPassword': 'Пароль домашней сети',
  'wifi.apply': 'Перевести камеру',
  'wifi.cancel': 'Отмена',
  'wifi.ip': 'IP камеры: {ip}',
  'wifi.back': '← Камеры',
  'wifi.error': 'Ошибка: {error}',
  'wifi.searching': 'ищу камеры (3 с)…',
  'state.offline': 'офлайн',
  'state.connecting': 'подключение',
  'state.online': 'онлайн',
  'state.streaming': 'в эфире',
  'state.auth_error': 'неверный пароль',
  'state.error': 'ошибка',
  'state.busy': 'занята',
  'unit.b': 'Б',
  'unit.kb': 'КБ',
  'unit.mb': 'МБ',
  'unit.gb': 'ГБ',
  'lang.name': 'Русский',
  'player.noWebcodecs': 'WebCodecs H.264 недоступен — переключаюсь на MSE',
  'player.mseConnecting': 'MSE-плеер подключается…',
  'player.connecting': 'подключение…',
  'player.wsError': 'ошибка WS',
  'player.openWaiting': 'поток открыт, ждём кадры',
  'player.closed': 'соединение закрыто',
  'player.connectionError': 'ошибка соединения',
  'player.reset': 'сброс: {reason}',
  'player.streamRestarted': 'поток перезапущен',
  'player.cameraError': 'ошибка камеры',
  'player.badConfig': 'битый config',
  'player.decoder': 'декодер: {error}',
  'player.decoderStart': 'Не удалось запустить декодер: {error}',
  'mse.connecting': 'MSE: подключение…',
  'mse.waitInit': 'MSE: ждём init',
  'mse.connectionError': 'MSE: ошибка соединения',
  'mse.error': 'MSE: ошибка',
  'mse.unsupported': 'Браузер не поддерживает MSE',
  'worker.noWebcodecs': 'WebCodecs недоступен в Worker',
  'worker.noCanvas': 'нет canvas',
};

let lang = detect();
if (typeof document !== 'undefined') {
  document.documentElement.lang = lang;
}

function detect() {
  try {
    const requested = new URLSearchParams(location.search).get('lang');
    if (SUPPORTED.includes(requested)) return requested;
  } catch (e) { /* not a browser URL */ }
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (SUPPORTED.includes(saved)) return saved;
  } catch (e) { /* storage may be unavailable */ }
  return String(navigator.language || 'en').toLowerCase().startsWith('ru') ? 'ru' : 'en';
}

export function getLang() {
  return lang;
}

export function locale() {
  return lang === 'ru' ? 'ru-RU' : 'en-US';
}

export function setLang(next) {
  if (!SUPPORTED.includes(next) || next === lang) return;
  lang = next;
  try { localStorage.setItem(STORAGE_KEY, lang); } catch (e) { /* noop */ }
  document.documentElement.lang = lang;
  window.dispatchEvent(new CustomEvent('lookcam:lang', { detail: lang }));
}

export function t(key, vars = {}) {
  const dict = lang === 'ru' ? RU : EN;
  let out = dict[key] || EN[key] || key;
  for (const [name, value] of Object.entries(vars)) {
    out = out.split(`{${name}}`).join(String(value ?? ''));
  }
  return out;
}

// Translate a phrase that comes from the server schema (or any external source).
export function L(ru, en) {
  return lang === 'ru' ? ru : (en || ru);
}

// Pick an English label when available and the UI is in English.
export function labelOf(obj, field = 'label') {
  if (!obj) return '';
  if (lang === 'en') return obj[`${field}_en`] || obj[field] || '';
  return obj[field] || '';
}

const SERVER_EN = {
  'требуется вход': 'sign-in required',
  'неверный пароль': 'wrong password',
  'первичная настройка уже выполнена': 'initial setup is already complete',
  'первичная настройка доступна только с localhost':
    'initial setup is available only from localhost',
  'качество должно быть HD/SD/VGA': 'quality must be HD/SD/VGA',
  'PPPP-endpoint не запущен': 'PPPP endpoint is not running',
  'debug_raw выключен в конфиге': 'debug_raw is disabled in the config',
  'SD-менеджер не запущен': 'SD manager is not running',
  'неверный номер': 'invalid number',
  'файл не найден в кэше': 'file not found in cache',
  'запись не найдена': 'recording not found',
  'запись не запущена': 'recording is not running',
  'нет ffmpeg/cv2 — снимок на сервере недоступен':
    'ffmpeg/cv2 is unavailable — server-side snapshot is not available',
  'мастер Wi-Fi не запущен': 'Wi-Fi wizard is not running',
  'RTSP on a non-loopback bind requires a configured password':
    'RTSP on a non-loopback address requires a configured password',
  'нет такой камеры': 'no such camera',
  'нет SPS/PPS от камеры': 'no SPS/PPS from the camera',
  'сессия с камерой не установлена': 'camera session is not established',
  'сессия закрыта': 'session is closed',
  'камера не появилась в сети роутера': 'the camera did not appear on the router network',
  'камера не отдала файл': 'the camera did not send the file',
  'камера не ответила': 'the camera did not respond',
  'камера занята': 'the camera is busy',
  'слишком много попыток входа': 'too many sign-in attempts',
};

const SERVER_RULES = [
  [/^камера не ответила на (.+?) за (\d+) мс$/, 'camera did not respond to $1 within $2 ms'],
  [/^камера отвергла пароль \(result=(-?\d+)\)$/, 'the camera rejected the password (result=$1)'],
  [/^нет такой камеры: (.+)$/, 'no such camera: $1'],
  [/^камера занята: (.+)$/, 'the camera is busy: $1'],
  [/^скачивание (.+?): нет данных (\d+) с$/, 'download $1: no data for $2 s'],
  [/^нет данных (\d+) с$/, 'no data for $1 s'],
];

// Translate technical server messages that reach the UI. Unknown messages are
// returned unchanged (they may contain user data or protocol details).
export function translateServer(text) {
  if (lang !== 'en' || !text) return text;
  const raw = String(text).trim();
  if (SERVER_EN[raw]) return SERVER_EN[raw];
  for (const [re, replacement] of SERVER_RULES) {
    if (re.test(raw)) return raw.replace(re, replacement);
  }
  return text;
}

export function applyStatic() {
  document.title = 'LookCam Service';
  document.querySelectorAll('[data-i18n]').forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  bindLangSelects();
}

function fillLangOptions(select) {
  if (select.options.length) return;
  for (const [code, name] of [['en', 'English'], ['ru', 'Русский']]) {
    const opt = document.createElement('option');
    opt.value = code;
    opt.textContent = name;
    select.appendChild(opt);
  }
}

// Bind every language selector on the page (top bar and login cards).
export function bindLangSelects(root = document) {
  root.querySelectorAll('select[data-lang], #lang-switch').forEach((select) => {
    fillLangOptions(select);
    select.value = lang;
    if (select.dataset.langBound) return;
    select.dataset.langBound = '1';
    select.addEventListener('change', () => setLang(select.value));
  });
}

export function mountLangSwitch() {
  // The visible selector lives in the top bar; login cards add their own
  // select[data-lang] so it is never fixed over the video area.
  bindLangSelects();
}
