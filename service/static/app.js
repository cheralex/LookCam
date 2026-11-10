import { api, humanSize, humanTime, stateLabel, toast } from './api.js';
import './player-webcodecs.js';
import { applyStatic, bindLangSelects, getLang, labelOf, mountLangSwitch, t, translateServer } from './i18n.js';

const app = document.getElementById('app');
const topbar = document.getElementById('topbar');
const serverInfo = document.getElementById('server-info');

const S = {
  state: null,
  schema: null,
  cameras: [],
  events: null,
  eventTimer: null,
  pollTimer: null,
  refreshTimer: null,
};

const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));

function badge(state, busy) {
  const label = busy ? t('common.busy', { what: busy }) : stateLabel(state);
  const cls = busy ? 'busy' : (state || 'offline');
  return `<span class="badge ${cls}">${esc(label)}</span>`;
}

function rtspUrl(id) {
  const port = S.state?.rtsp?.port || 8554;
  return `rtsp://${location.hostname}:${port}/${id}`;
}

function go(hash) { location.hash = hash; }

async function loadState() {
  S.state = await api.get('/api/state');
  S.cameras = S.state.cameras || [];
  serverInfo.textContent = S.state.authorized
    ? t('cams.count', { n: S.cameras.length }) : '';
}

function clearTimers() {
  if (S.pollTimer) { clearInterval(S.pollTimer); S.pollTimer = null; }
  if (S.refreshTimer) { clearInterval(S.refreshTimer); S.refreshTimer = null; }
  if (S.eventTimer) { clearInterval(S.eventTimer); S.eventTimer = null; }
}

function setEvents() {
  if (S.events) return;
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const connect = () => {
    try {
      S.events = new WebSocket(`${proto}//${location.host}/ws/events`);
    } catch (e) { setTimeout(connect, 2000); return; }
    S.events.onmessage = (ev) => {
      if (typeof ev.data !== 'string') return;
      let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === 'camera') {
        applyCameraEvent(msg);
      } else if (msg.type === 'camera_added' || msg.type === 'camera_removed') {
        scheduleCamerasRefresh();
      } else if (msg.type === 'download') {
        const i = (S.downloads || []).findIndex((d) => d.n === msg.n);
        S.downloads = S.downloads || [];
        if (i >= 0) S.downloads[i] = msg; else S.downloads.push(msg);
        document.querySelectorAll(`[data-dl="${msg.n}"]`).forEach((el) => {
          el.textContent = `${msg.progress ?? 0}%${msg.state === 'error' ? ' — ' + translateServer(msg.error) : ''}`;
          const bar = el.parentElement?.querySelector('.progress > i');
          if (bar) bar.style.width = `${msg.progress ?? 0}%`;
        });
      } else if (msg.type === 'record') {
        const el = document.getElementById('record-state');
        if (el) el.textContent = msg.state === 'recording'
          ? t('view.recording', { frames: msg.frames, size: humanSize(msg.bytes) })
          : t('view.recordError', { state: msg.state, error: translateServer(msg.error) || '' });
      }
    };
    S.events.onclose = () => {
      S.events = null;
      setTimeout(connect, 2000);
    };
  };
  connect();
}

function scheduleCamerasRefresh() {
  if (S.refreshTimer) return;
  S.refreshTimer = setTimeout(async () => {
    S.refreshTimer = null;
    try {
      const data = await api.get('/api/cameras');
      S.cameras = data.cameras || [];
      if (routeIsGrid()) renderCameras();
      updateViewMeta();
    } catch (e) { /* 401 — show sign-in */ }
  }, 400);
}

function applyCameraEvent(msg) {
  let cam = S.cameras.find((c) => c.id === msg.id);
  if (!cam) { scheduleCamerasRefresh(); return; }
  cam.state = msg.state || cam.state;
  if (typeof msg.busy !== 'undefined') cam.busy = msg.busy;
  if (typeof msg.error !== 'undefined') cam.error = msg.error;
  const card = [...document.querySelectorAll('.card[data-id]')]
    .find((el) => el.dataset.id === msg.id);
  if (card) {
    const head = card.querySelector('.head');
    const old = head?.querySelector('.badge');
    if (old) {
      const wrap = document.createElement('span');
      wrap.innerHTML = badge(cam.state, cam.busy);
      old.replaceWith(wrap.firstElementChild);
    }
  }
  updateViewMeta();
}

function routeIsGrid() {
  const h = location.hash.replace(/^#/, '') || '/';
  return h === '/' || h === '';
}

// ------------------------------------------------------------------ sign-in
function renderGate() {
  topbar.hidden = true;
  clearTimers();
  if (S.state.needs_setup) {
    app.innerHTML = `<div class="login-wrap"><div class="login card">
      <div class="body col">
        <h1>Look<span style="color:var(--accent)">Cam</span> Service</h1>
        <div class="row"><span class="spacer"></span>
          <label class="lang-switch" title="Language / Язык">
            <select data-lang aria-label="Language / Язык"></select>
          </label>
        </div>
        <p class="muted">${t('gate.setupTitle')}</p>
        <label class="field"><span>${t('gate.password')}</span>
          <input id="pass" type="password" autocomplete="new-password"></label>
        <label class="field"><span>${t('gate.repeat')}</span>
          <input id="pass2" type="password" autocomplete="new-password"></label>
        <button class="primary" id="setup">${t('common.save')}</button>
      </div></div></div>`;
    bindLangSelects(app);
    document.getElementById('setup').onclick = async () => {
      const p1 = document.getElementById('pass').value;
      const p2 = document.getElementById('pass2').value;
      if (p1 !== p2) { toast(t('gate.passwordMismatch'), true); return; }
      try {
        await api.post('/api/setup', { password: p1 });
        await boot();
      } catch (e) { toast(e.message, true); }
    };
    return;
  }
  app.innerHTML = `<div class="login-wrap"><div class="login card">
    <div class="body col">
      <h1>Look<span style="color:var(--accent)">Cam</span> Service</h1>
      <div class="row"><span class="spacer"></span>
        <label class="lang-switch" title="Language / Язык">
          <select data-lang aria-label="Language / Язык"></select>
        </label>
      </div>
      <p class="muted">${t('gate.loginTitle')}</p>
      <label class="field"><span>${t('gate.password').replace(/ \(.*\)$/, '')}</span>
        <input id="pass" type="password" autocomplete="current-password"></label>
      <button class="primary" id="login">${t('gate.login')}</button>
    </div></div></div>`;
  bindLangSelects(app);
  const doLogin = async () => {
    try {
      await api.post('/api/login', { password: document.getElementById('pass').value });
      await boot();
    } catch (e) { toast(e.message, true); }
  };
  document.getElementById('login').onclick = doLogin;
  document.getElementById('pass').onkeydown = (e) => { if (e.key === 'Enter') doLogin(); };
}

// ------------------------------------------------------------------ cameras
function renderCameras() {
  const multi = localStorage.getItem('lookcam-multi') !== '0';
  const cards = S.cameras.map((cam) => `
    <div class="card" data-id="${esc(cam.id)}">
      <div class="head">
        <b>${esc(cam.name || cam.id)}</b>
        ${badge(cam.state, cam.busy)}
        <span class="spacer"></span>
        <span class="muted small mono">${esc(cam.id)}</span>
      </div>
      <div class="body">
        ${multi
          ? `<live-player camera-id="${esc(cam.id)}"></live-player>`
          : `<div class="muted small">${t('cams.multiOff')}</div>`}
      </div>
      <div class="foot">
        <button data-open="${esc(cam.id)}">${t('common.watch')}</button>
        <button data-settings="${esc(cam.id)}">${t('common.settings')}</button>
        <button data-sd="${esc(cam.id)}">${t('common.sd')}</button>
        <button data-copy="${esc(cam.id)}">RTSP</button>
        <span class="spacer"></span>
        <span class="muted small">${cam.fps ? t('common.fps', { n: cam.fps }) : ''}
          ${cam.bitrate_kbps ? t('common.kbps', { n: cam.bitrate_kbps }) : ''}</span>
      </div>
    </div>`).join('');
  app.innerHTML = `<div class="wrap">
    <div class="row" style="margin-bottom:14px">
      <h2 style="margin:0">${t('cams.title')}</h2>
      <span class="muted">${t('cams.count', { n: S.cameras.length })}</span>
      <span class="spacer"></span>
      <label class="small muted"><input type="checkbox" id="multi"
        ${multi ? 'checked' : ''}> ${t('cams.multi')}</label>
      <button class="green" id="add-cam">${t('cams.add')}</button>
    </div>
    ${S.cameras.length ? `<div class="grid">${cards}</div>`
      : `<div class="card"><div class="body">
          <p>${t('cams.empty')}</p>
          <button class="primary" id="add-cam2">${t('cams.add')}</button>
          <a href="#/wifi" style="margin-left:10px">${t('cams.wifiWizard')}</a>
        </div></div>`}
  </div>`;
  const add = () => go('/add');
  document.getElementById('add-cam')?.addEventListener('click', add);
  document.getElementById('add-cam2')?.addEventListener('click', add);
  document.getElementById('multi')?.addEventListener('change', (e) => {
    localStorage.setItem('lookcam-multi', e.target.checked ? '1' : '0');
    renderCameras();
  });
  app.querySelectorAll('[data-open]').forEach((b) =>
    b.addEventListener('click', () => go(`/cam/${b.dataset.open}`)));
  app.querySelectorAll('[data-settings]').forEach((b) =>
    b.addEventListener('click', () => go(`/cam/${b.dataset.settings}/settings`)));
  app.querySelectorAll('[data-sd]').forEach((b) =>
    b.addEventListener('click', () => go(`/cam/${b.dataset.sd}/sd`)));
  app.querySelectorAll('[data-copy]').forEach((b) =>
    b.addEventListener('click', () => copyText(rtspUrl(b.dataset.copy))));
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast(t('cams.copyDone', { text }));
  } catch (e) {
    toast(text);
  }
}

function setToggle(id, on) {
  const button = document.getElementById(id);
  if (!button) return;
  button.classList.toggle('active', !!on);
  button.setAttribute('aria-pressed', on ? 'true' : 'false');
}

function mediaTime(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h
    ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
    : `${m}:${String(s).padStart(2, '0')}`;
}

function ensurePlayer() {
  let dialog = document.getElementById('player-dialog');
  if (dialog) return dialog._playerUi;
  dialog = document.createElement('dialog');
  dialog.id = 'player-dialog';
  dialog.className = 'player-dialog';
  dialog.innerHTML = `<div class="dlg-head"><span id="player-title"></span>
    <button class="small" id="dlg-close" aria-label="${t('common.close')}">×</button></div>
    <div class="player-stage">
      <video controls autoplay playsinline></video>
      <div class="player-status" id="player-status" data-state="loading">
        <span class="player-spinner"></span>
        <span id="player-status-text"></span>
        <span class="spacer"></span>
        <span id="player-buffer-text" class="muted small"></span>
      </div>
      <div class="progress player-progress"><i id="player-buffer-bar"></i></div>
    </div>`;
  document.body.appendChild(dialog);
  const ui = {
    dialog,
    video: dialog.querySelector('video'),
    title: dialog.querySelector('#player-title'),
    status: dialog.querySelector('#player-status'),
    statusText: dialog.querySelector('#player-status-text'),
    bufferText: dialog.querySelector('#player-buffer-text'),
    bufferBar: dialog.querySelector('#player-buffer-bar'),
    prepTimer: null,
  };
  dialog._playerUi = ui;
  dialog.querySelector('#dlg-close').onclick = () => closePlayer();
  dialog.addEventListener('click', (e) => {
    if (e.target === dialog) closePlayer();
  });
  dialog.addEventListener('close', () => {
    clearInterval(ui.prepTimer);
    ui.prepTimer = null;
    ui.video.pause();
    ui.video.removeAttribute('src');
    ui.video.load();
    ui.bufferText.textContent = '';
    ui.bufferBar.style.width = '0%';
  });
  const video = ui.video;
  video.addEventListener('loadstart', () => {
    if (video.getAttribute('src')) setPlayerState(ui, 'loading');
  });
  video.addEventListener('waiting', () => {
    if (video.getAttribute('src')) setPlayerState(ui, 'buffering');
  });
  video.addEventListener('seeking', () => {
    if (video.getAttribute('src')) setPlayerState(ui, 'buffering');
  });
  video.addEventListener('seeked', () => {
    if (video.getAttribute('src') && !video.paused && video.readyState >= 2) {
      setPlayerState(ui, 'playing');
    }
  });
  video.addEventListener('stalled', () => {
    if (video.getAttribute('src')) setPlayerState(ui, 'stalled');
  });
  video.addEventListener('playing', () => {
    if (video.getAttribute('src')) setPlayerState(ui, 'playing');
  });
  video.addEventListener('ended', () => {
    if (video.getAttribute('src')) setPlayerState(ui, 'ended');
  });
  video.addEventListener('canplay', () => {
    if (video.getAttribute('src') && ui.status.dataset.state !== 'playing') {
      setPlayerState(ui, 'buffering');
    }
  });
  video.addEventListener('loadedmetadata', () => {
    const start = Number(ui.startAt) || 0;
    if (start > 0 && Number.isFinite(start)) video.currentTime = start;
    updatePlayerBuffer(ui);
    video.play().catch(() => {});
  });
  video.addEventListener('progress', () => updatePlayerBuffer(ui));
  video.addEventListener('timeupdate', () => updatePlayerBuffer(ui));
  video.addEventListener('durationchange', () => updatePlayerBuffer(ui));
  video.addEventListener('error', () => {
    if (video.getAttribute('src')) {
      const err = video.error;
      setPlayerState(ui, 'error', t('player.error', {
        error: (err && (err.message || err.code)) || t('player.unknown'),
      }));
    }
  });
  return ui;
}

function setPlayerState(ui, state, message = '') {
  ui.status.dataset.state = state;
  ui.statusText.textContent = message || t(`player.${state}`);
  ui.status.querySelector('.player-spinner').style.display =
    state === 'playing' || state === 'ended' || state === 'error' ? 'none' : '';
  ui.status.classList.toggle('error', state === 'error');
}

function updatePlayerBuffer(ui) {
  const video = ui.video;
  const duration = Number(video.duration) || 0;
  let end = 0;
  try {
    if (video.buffered.length) end = video.buffered.end(video.buffered.length - 1);
  } catch (e) { /* buffered may be unavailable */ }
  if (!duration || !Number.isFinite(duration)) {
    ui.bufferText.textContent = end ? mediaTime(end) : '';
    ui.bufferBar.style.width = '0%';
    return;
  }
  ui.bufferText.textContent = t('player.buffered', {
    end: mediaTime(end), total: mediaTime(duration),
  });
  ui.bufferBar.style.width = `${Math.min(100, (end / duration) * 100)}%`;
}

function openPlayer(src, startAt = 0, options = {}) {
  const ui = ensurePlayer();
  clearInterval(ui.prepTimer);
  ui.prepTimer = null;
  ui.startAt = Number(startAt) || 0;
  ui.title.textContent = options.title || t('sd.play');
  ui.video.pause();
  ui.video.removeAttribute('src');
  ui.video.load();
  ui.bufferText.textContent = '';
  ui.bufferBar.style.width = '0%';
  if (!ui.dialog.open) ui.dialog.showModal();
  if (src) {
    ui.video.src = src;
    setPlayerState(ui, 'loading');
    ui.video.load();
  } else {
    setPlayerState(ui, 'preparing', options.status || t('player.preparing'));
  }
  return ui;
}

function setPlayerText(ui, text) {
  ui.statusText.textContent = text;
}

function closePlayer() {
  const dialog = document.getElementById('player-dialog');
  if (dialog && dialog.open) dialog.close();
}

// ------------------------------------------------------------------ live view
function cameraById(id) {
  return S.cameras.find((c) => c.id === id);
}

async function renderView(id) {
  const cam = cameraById(id);
  if (!cam) { go('/'); return; }
  app.innerHTML = `<div class="wrap big-player">
    <div class="row" style="margin-bottom:10px">
      <button id="back">${t('common.backCameras')}</button>
      <h2 style="margin:0">${esc(cam.name || cam.id)}</h2>
      <span id="view-badge">${badge(cam.state, cam.busy)}</span>
      <span class="spacer"></span>
      <span class="muted small mono">${esc(rtspUrl(cam.id))}</span>
      <button data-copy>${t('view.rtspCopy')}</button>
    </div>
    <div class="player card" style="overflow:hidden">
      <live-player id="big" camera-id="${esc(cam.id)}"></live-player>
    </div>
    <div class="toolbar">
      <span class="chips" id="qualities">
        ${['HD', 'SD', 'VGA'].map((q) =>
          `<span class="chip ${q === cam.quality ? 'active' : ''}" data-q="${q}">${q}</span>`).join('')}
      </span>
      <button id="snap">${t('view.snapshot')}</button>
      <button id="record">${t('view.record')}</button>
      <button id="settings">${t('view.settings')}</button>
      <button id="sd">${t('view.sd')}</button>
      <span class="spacer"></span>
      <button id="led">LED</button>
      <button id="light">${t('view.ir')}</button>
      <button id="flip-h">${t('view.flipH')}</button>
      <button id="flip-v">${t('view.flipV')}</button>
    </div>
    <div class="row small muted" style="margin-top:10px">
      <span id="player-stats">—</span>
      <span class="spacer"></span>
      <span id="record-state"></span>
    </div>
    <div class="card" style="margin-top:14px"><div class="body">
      <div class="kv" id="info"><span class="muted">${t('view.infoLoading')}</span></div>
    </div></div>
  </div>`;
  document.getElementById('back').onclick = () => go('/');
  document.querySelector('[data-copy]').onclick = () => copyText(rtspUrl(cam.id));
  const player = document.getElementById('big');
  document.querySelectorAll('[data-q]').forEach((chip) => {
    chip.addEventListener('click', async () => {
      try {
        await api.post(`/api/cameras/${cam.id}/quality`, { quality: chip.dataset.q });
        document.querySelectorAll('[data-q]').forEach((c) => c.classList.remove('active'));
        chip.classList.add('active');
        toast(t('view.quality', { q: chip.dataset.q }));
      } catch (e) { toast(e.message, true); }
    });
  });
  player.addEventListener('stats', (e) => {
    const st = e.detail || {};
    document.getElementById('player-stats').textContent = t('view.stats', {
      fps: st.fps || 0,
      width: st.width || '',
      height: st.height || '',
    });
  });
  document.getElementById('snap').onclick = async () => {
    let blob = await player.snapshot();
    if (!blob) {
      try {
        const res = await fetch(`/api/cameras/${cam.id}/snapshot.jpg`);
        if (!res.ok) throw new Error(await res.text());
        blob = await res.blob();
      } catch (e) {
        toast(t('view.snapshotUnavailable', { error: translateServer(e.message) }), true);
        return;
      }
    }
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${cam.id}-${Date.now()}.jpg`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  };
  const recBtn = document.getElementById('record');
  const recState = document.getElementById('record-state');
  let recording = false;
  const updateRecord = (text, on) => {
    recording = on;
    recState.textContent = text || '';
    recBtn.textContent = on ? t('view.stopRecord') : t('view.record');
  };
  recBtn.onclick = async () => {
    try {
      if (recording) {
        const data = await api.del(`/api/cameras/${cam.id}/record`);
        updateRecord(t('view.recordingFile', {
          file: data.record?.file || t('view.recordStopped'),
        }), false);
      } else {
        const data = await api.post(`/api/cameras/${cam.id}/record`, {});
        updateRecord(t('view.recordingFile', {
          file: data.record?.file || t('view.recordRunning'),
        }), true);
      }
    } catch (e) { toast(e.message, true); }
  };
  document.getElementById('settings').onclick = () => go(`/cam/${cam.id}/settings`);
  document.getElementById('sd').onclick = () => go(`/cam/${cam.id}/sd`);
  const doAction = async (action, body = {}) => {
    try {
      const result = await api.post(`/api/cameras/${cam.id}/actions/${action}`, body);
      if ((action === 'led' || action === 'light')
          && result && typeof result.value !== 'undefined') {
        setToggle(action, Number(result.value) === 1);
      }
      toast(t('view.actionDone', { action }));
    } catch (e) { toast(e.message, true); }
  };
  document.getElementById('led').onclick = () => doAction('led', {});
  document.getElementById('light').onclick = () => doAction('light', {});
  document.getElementById('flip-h').onclick = () => doAction('flip_h', {});
  document.getElementById('flip-v').onclick = () => doAction('flip_v', {});
  loadInfo(cam.id);
  S.pollTimer = setInterval(() => loadInfo(cam.id, true), 8000);
}

async function loadInfo(id, quiet = false) {
  const el = document.getElementById('info');
  if (!el) return;
  try {
    const data = await api.get(`/api/cameras/${id}/info`);
    const dev = data.device || {};
    const vid = data.video || {};
    const ses = data.session || {};
    const hub = data.hub || {};
    setToggle('led', Number(dev.ledstatus) === 1);
    setToggle('light', Number(dev.lightstatus) === 1);
    const rows = [
      ['info.did', dev.id || data.camera?.did || '—'],
      ['info.firmware', dev.ver || '—'],
      ['info.ip', dev.ip || ses.addr || '—'],
      ['info.wifi', dev.wifissid ? `${dev.wifissid} (${dev.wifisig ?? '?'})` : '—'],
      ['info.battery', typeof dev.battery !== 'undefined' ? dev.battery : '—'],
      ['info.stream', `${hub.config?.codec || '—'} ${hub.config?.width || '?'}×${hub.config?.height || '?'}`],
      ['info.fps', hub.fps ?? '—'],
      ['info.bitrate', `${hub.fps ? Math.round((hub.bytes || 0) / 1024) + ' ' + t('unit.kb') : '—'}`],
      ['info.brightness', vid.brightness ?? '—'],
      ['info.saturation', vid.saturation ?? '—'],
      ['info.session', ses.connected
        ? `${t('common.online')}, ${t('common.seconds', { n: ses.uptime_s })}`
        : t('common.no')],
      ['info.subscribers', hub.subscribers ?? 0],
    ];
    el.innerHTML = rows.map(([k, v]) =>
      `<b>${esc(t(k))}</b><span>${esc(v)}</span>`).join('');
  } catch (e) {
    if (!quiet) el.innerHTML = `<span class="muted">${t('view.infoError', { error: esc(e.message) })}</span>`;
  }
}

function updateViewMeta() {
  const el = document.getElementById('view-badge');
  if (!el) return;
  const id = (location.hash.match(/^#\/cam\/([^/]+)/) || [])[1];
  const cam = cameraById(id);
  if (cam) el.innerHTML = badge(cam.state, cam.busy);
}

// ------------------------------------------------------------------ settings
async function renderSettings(id) {
  const cam = cameraById(id);
  if (!cam) { go('/'); return; }
  if (!S.schema) S.schema = await api.get('/api/schema');
  const groups = S.schema.groups || {};
  const names = Object.keys(groups);
  app.innerHTML = `<div class="wrap">
    <div class="row" style="margin-bottom:12px">
      <button id="back">← ${esc(cam.name || cam.id)}</button>
      <h2 style="margin:0">${t('settings.title')}</h2>
      <span class="spacer"></span>
      <span class="muted small">${t('settings.applies')}</span>
    </div>
    <div class="tabs" id="tabs">
      ${names.map((g, i) => `<button data-g="${g}" class="${i === 0 ? 'active' : ''}">${esc(labelOf(groups[g], 'title') || g)}</button>`).join('')}
      <button data-g="__actions">${t('settings.maintenance')}</button>
    </div>
    <div id="group-body"></div>
  </div>`;
  document.getElementById('back').onclick = () => go(`/cam/${id}`);
  const body = document.getElementById('group-body');
  const show = async (group) => {
    if (group === '__actions') { renderActions(body, cam); return; }
    body.innerHTML = `<div class="muted">${t('settings.loading')}</div>`;
    try {
      const data = await api.get(`/api/cameras/${id}/settings/${group}`);
      renderGroupForm(body, cam, group, groups[group], data.values || {});
    } catch (e) {
      body.innerHTML = `<div class="card"><div class="body">
        <p class="muted">${t('settings.readError', { error: esc(e.message) })}</p>
        <button id="retry">${t('common.retry')}</button></div></div>`;
      document.getElementById('retry').onclick = () => show(group);
    }
  };
  document.querySelectorAll('[data-g]').forEach((btn) => {
    btn.onclick = () => {
      document.querySelectorAll('[data-g]').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      show(btn.dataset.g);
    };
  });
  show(names[0] || '__actions');
}

function renderGroupForm(body, cam, group, spec, values) {
  const fields = (spec.fields || []).map((f) => {
    const val = values[f.key] ?? f.default ?? '';
    const label = esc(labelOf(f));
    const hint = getLang() === 'en' && f.hint_en ? f.hint_en : f.hint;
    if (f.type === 'bool') {
      return `<label class="field"><span>${label}</span>
        <select data-key="${esc(f.key)}" data-type="bool">
          <option value="1" ${Number(val) ? 'selected' : ''}>${t('common.on')}</option>
          <option value="0" ${!Number(val) ? 'selected' : ''}>${t('common.off')}</option>
        </select></label>`;
    }
    if (f.type === 'time') {
      return `<label class="field"><span>${label}</span>
        <input data-key="${esc(f.key)}" data-type="time" value="${esc(val || '00:00')}"
          pattern="[0-2][0-9]:[0-5][0-9]" placeholder="HH:MM"></label>`;
    }
    return `<label class="field"><span>${label}${hint ? ' — ' + esc(hint) : ''}</span>
      <input type="number" data-key="${esc(f.key)}" data-type="int"
        min="${f.min ?? ''}" max="${f.max ?? ''}" value="${esc(val)}"></label>`;
  }).join('');
  const actions = (spec.actions || []).map((a) =>
    `<button data-action="${esc(a.key)}">${esc(labelOf(a))}</button>`).join('');
  body.innerHTML = `<div class="card"><div class="body col">
    <div class="form-grid">${fields || `<span class="muted">${t('settings.noFields')}</span>`}</div>
    <div class="row"><button class="primary" id="save">${t('settings.save')}</button>
      <span class="muted small">${t('settings.onlyChanged')}</span></div>
    ${actions ? `<hr><div class="row">${actions}</div>` : ''}
  </div></div>`;
  document.getElementById('save').onclick = async () => {
    const changed = {};
    body.querySelectorAll('[data-key]').forEach((el) => {
      const key = el.dataset.key;
      const type = el.dataset.type;
      let value = el.value;
      const old = String(values[key] ?? '');
      if (String(value) === old) return;
      if (type === 'int') value = Number(value);
      if (type === 'bool') value = Number(value);
      changed[key] = value;
    });
    if (!Object.keys(changed).length) { toast(t('settings.noChanges')); return; }
    try {
      const res = await api.put(`/api/cameras/${cam.id}/settings/${group}`,
        { values: changed });
      toast(t('settings.saved', { values: JSON.stringify(res.sent || changed) }));
      showGroupAgain();
    } catch (e) { toast(e.message, true); }
  };
  const showGroupAgain = () => {
    if (group) {
      api.get(`/api/cameras/${cam.id}/settings/${group}`)
        .then((data) => renderGroupForm(body, cam, group, spec, data.values || {}))
        .catch(() => {});
    }
  };
  body.querySelectorAll('[data-action]').forEach((btn) => {
    btn.onclick = async () => {
      const action = btn.dataset.action;
      const bodyData = {};
      try {
        await api.post(`/api/cameras/${cam.id}/actions/${action}`, bodyData);
        toast(t('settings.done', { action }));
      } catch (e) { toast(e.message, true); }
    };
  });
}

function renderActions(body, cam) {
  body.innerHTML = `<div class="card"><div class="body col">
    <div class="row">
      <button id="timesync">${t('actions.timesync')}</button>
      <button id="password">${t('actions.changePassword')}</button>
    </div>
    <hr>
    <div class="row">
      <button id="reboot">${t('actions.reboot')}</button>
      <button class="danger" id="reset">${t('actions.reset')}</button>
      <button class="danger" id="restore">${t('actions.restore')}</button>
    </div>
    <p class="muted small">${t('actions.warning')}</p>
  </div></div>`;
  const act = async (name, payload = {}, needConfirm = false) => {
    const actionKey = `actions.${name}`;
    const actionLabel = t(actionKey) === actionKey ? name : t(actionKey);
    if (needConfirm && !confirm(t('actions.confirm', { action: actionLabel }))) return;
    try {
      await api.post(`/api/cameras/${cam.id}/actions/${name}`, payload);
      toast(t('settings.commandSent', { action: actionLabel }));
    } catch (e) { toast(e.message, true); }
  };
  document.getElementById('timesync').onclick = () => act('timesync');
  document.getElementById('reboot').onclick = () => act('reboot', {}, true);
  document.getElementById('reset').onclick = () => {
    if (confirm(t('actions.resetConfirm')))
      act('reset', {}, false);
  };
  document.getElementById('restore').onclick = () => act('restore', {}, true);
  document.getElementById('password').onclick = async () => {
    const oldpwd = prompt(t('actions.currentPassword'));
    if (oldpwd === null) return;
    const newpwd = prompt(t('actions.newPassword'));
    if (!newpwd) return;
    await act('password', { pwd: oldpwd, newpwd });
  };
}

// ------------------------------------------------------------------ SD card
async function renderSd(id) {
  const cam = cameraById(id);
  if (!cam) { go('/'); return; }
  app.innerHTML = `<div class="wrap">
    <div class="row" style="margin-bottom:12px">
      <button id="back">← ${esc(cam.name || cam.id)}</button>
      <h2 style="margin:0">${t('sd.title')}</h2>
      <span class="spacer"></span>
      <button id="refresh">${t('sd.refresh')}</button>
      <button class="danger" id="format">${t('sd.format')}</button>
    </div>
    <div class="card"><div class="body" id="sd-body">
      <div class="muted">${t('sd.loading')}</div>
    </div></div>
    <div class="card" style="margin-top:14px"><div class="body">
      <h3 style="margin:0 0 8px">${t('sd.downloads')}</h3>
      <div id="downloads" class="col"></div>
    </div></div>
  </div>`;
  document.getElementById('back').onclick = () => go(`/cam/${id}`);
  let page = 1;
  const all = [];
  const load = async (reset = true) => {
    const body = document.getElementById('sd-body');
    if (reset) body.innerHTML = `<div class="muted">${t('common.loading')}</div>`;
    try {
      const data = await api.get(`/api/cameras/${id}/sd?page=${page}&count=30&all=0`);
      if (reset) all.length = 0;
      for (const f of data.files || []) {
        if (!all.find((x) => x.patch === f.patch)) all.push(f);
      }
      renderRows(body, all, data);
    } catch (e) {
      body.innerHTML = `<p class="muted">${t('sd.loadError', { error: esc(e.message) })}</p>`;
    }
  };
  const renderRows = (body, rows, data) => {
    const trs = rows.map((f, i) => `<tr>
      <td><input type="checkbox" data-sel="${i}"></td>
      <td>${esc(f.name || f.patch)}</td>
      <td>${esc(f.time_text || humanTime(f.time))}</td>
      <td>${humanSize(f.size)}</td>
      <td><button data-dl-file="${i}">${t('sd.download')}</button></td>
      <td><button data-seek="${i}">${t('sd.seek')}</button></td>
    </tr>`).join('');
    body.innerHTML = `<div class="row" style="margin-bottom:8px">
      <span class="muted">${data?.allCount
        ? t('sd.filesOf', { n: rows.length, all: data.allCount })
        : t('sd.files', { n: rows.length })}</span>
      <span class="spacer"></span>
      <button id="del">${t('common.deleteSelected')}</button>
      ${data?.allCount && rows.length < data.allCount ? `<button id="more">${t('common.more')}</button>` : ''}
    </div>
    <table><thead><tr><th></th><th>${t('sd.file')}</th><th>${t('sd.time')}</th><th>${t('sd.size')}</th><th></th><th></th></tr></thead>
      <tbody>${trs || `<tr><td colspan="6" class="muted">${t('sd.noFiles')}</td></tr>`}</tbody></table>`;
    body.querySelectorAll('[data-dl-file]').forEach((b) => {
      b.onclick = async () => {
        const f = rows[Number(b.dataset.dlFile)];
        try {
          await api.post(`/api/cameras/${id}/sd/download`,
            { patch: f.patch, name: f.name, size: f.size });
          toast(t('sd.queued', { name: f.name || f.patch }));
          refreshDownloads();
        } catch (e) { toast(e.message, true); }
      };
    });
    body.querySelectorAll('[data-seek]').forEach((b) => {
      b.onclick = async () => {
        const f = rows[Number(b.dataset.seek)];
        const answer = prompt(t('sd.seekPrompt'));
        if (answer === null) return;
        const seconds = Number(String(answer).replace(',', '.'));
        if (!Number.isFinite(seconds) || seconds < 0) return;
        const ui = openPlayer(null, 0, {
          title: f.name || f.patch,
          status: t('player.preparing'),
        });
        const started = Date.now();
        ui.seekSeq = (ui.seekSeq || 0) + 1;
        const seq = ui.seekSeq;
        clearInterval(ui.prepTimer);
        ui.prepTimer = setInterval(() => {
          if (!ui.dialog.open) {
            clearInterval(ui.prepTimer);
            ui.prepTimer = null;
            return;
          }
          const sec = Math.max(0, Math.round((Date.now() - started) / 1000));
          setPlayerText(ui, t('player.preparingSec', { s: sec }));
        }, 1000);
        try {
          const data = await api.post(`/api/cameras/${id}/sd/seek`, {
            patch: f.patch, name: f.name, size: f.size,
            pos_ms: Math.round(seconds * 1000),
          });
          clearInterval(ui.prepTimer);
          ui.prepTimer = null;
          if (!ui.dialog.open || ui.seekSeq !== seq) return;
          openPlayer(data.url, (data.full ? data.pos_ms : data.keyframe_ms) / 1000,
            { title: f.name || f.patch });
        } catch (e) {
          clearInterval(ui.prepTimer);
          ui.prepTimer = null;
          if (!ui.dialog.open || ui.seekSeq !== seq) return;
          setPlayerState(ui, 'error', t('player.error', { error: e.message }));
          toast(t('sd.seekError', { error: e.message }), true);
        }
      };
    });
    document.getElementById('del').onclick = async () => {
      const patches = [...body.querySelectorAll('[data-sel]:checked')]
        .map((el) => rows[Number(el.dataset.sel)]?.patch).filter(Boolean);
      if (!patches.length) { toast(t('sd.noneSelected')); return; }
      if (!confirm(t('sd.deleteConfirm', { n: patches.length }))) return;
      try {
        await api.post(`/api/cameras/${id}/sd/delete`, { patches });
        toast(t('sd.deleted'));
        load(true);
      } catch (e) { toast(e.message, true); }
    };
    document.getElementById('more')?.addEventListener('click', () => {
      page += 1;
      load(false);
    });
  };
  document.getElementById('refresh').onclick = () => { page = 1; load(true); };
  document.getElementById('format').onclick = async () => {
    if (!confirm(t('sd.formatConfirm'))) return;
    if (!confirm(t('sd.formatConfirm2'))) return;
    try {
      await api.post(`/api/cameras/${id}/sd/format`, {});
      toast(t('sd.formatting'));
    } catch (e) { toast(e.message, true); }
  };
  const refreshDownloads = async () => {
    try {
      const data = await api.get('/api/downloads');
      S.downloads = data.downloads || [];
      const box = document.getElementById('downloads');
      if (!box) return;
      const mine = S.downloads;
      box.innerHTML = mine.length ? mine.map((d) => `<div class="col" style="gap:4px">
        <div class="row small"><b>${esc(d.name)}</b>
          <span class="muted">${humanSize(d.downloaded)} / ${humanSize(d.size)}</span>
          <span class="spacer"></span>
          <span data-dl="${d.n}">${d.progress}%${d.state === 'error' ? ' — ' + esc(translateServer(d.error)) : ''}</span>
          ${d.state === 'done' ? `<button data-play="${esc(d.file)}">${t('sd.play')}</button>` : ''}
          ${d.state === 'running' || d.state === 'queued' ? `<button data-cancel="${d.n}">${t('sd.cancel')}</button>` : ''}
        </div>
        <div class="progress"><i style="width:${d.progress}%"></i></div>
      </div>`).join('') : `<span class="muted small">${t('sd.queueEmpty')}</span>`;
      box.querySelectorAll('[data-play]').forEach((b) => {
        b.onclick = () => {
          openPlayer(`/media/${encodeURIComponent(id)}/${encodeURIComponent(b.dataset.play)}`,
            0, { title: b.dataset.play });
        };
      });
      box.querySelectorAll('[data-cancel]').forEach((b) => {
        b.onclick = async () => {
          try { await api.del(`/api/downloads/${b.dataset.cancel}`); refreshDownloads(); }
          catch (e) { toast(e.message, true); }
        };
      });
    } catch (e) { /* non-critical */ }
  };
  await load(true);
  await refreshDownloads();
  S.pollTimer = setInterval(refreshDownloads, 1500);
}

// ------------------------------------------------------------------ add camera
async function renderAdd() {
  app.innerHTML = `<div class="wrap">
    <div class="row" style="margin-bottom:12px">
      <button id="back">${t('common.backCameras')}</button><h2 style="margin:0">${t('add.title')}</h2>
      <span class="spacer"></span><button id="discover">${t('add.discover')}</button>
    </div>
    <div class="card"><div class="body col">
      <div class="form-grid">
        <label class="field"><span>${t('add.did')}</span>
          <input id="did" placeholder="G016638JHDXM"></label>
        <label class="field"><span>${t('add.name')}</span><input id="name" placeholder="${t('add.namePlaceholder')}"></label>
        <label class="field"><span>${t('add.password')}</span>
          <input id="password" type="password" placeholder="265600"></label>
        <label class="field"><span>${t('add.quality')}</span>
          <select id="quality"><option>HD</option><option>SD</option><option>VGA</option></select></label>
        <label class="field"><span>${t('add.host')}</span>
          <input id="host" placeholder="192.168.100.1"></label>
      </div>
      <div class="row"><button class="primary" id="add">${t('add.add')}</button>
        <a href="#/wifi">${t('add.apWizard')}</a></div>
      <p class="muted small">${t('add.help', {
        example: '<span class="mono">GHBB…URFKG → G550856URFKG</span>',
      })}</p>
    </div></div>
    <div id="found" style="margin-top:16px"></div>
  </div>`;
  document.getElementById('back').onclick = () => go('/');
  const add = async (fields) => {
    try {
      await api.post('/api/cameras', fields);
      toast(t('add.added'));
      await loadState();
      go('/');
    } catch (e) { toast(e.message, true); }
  };
  document.getElementById('add').onclick = () => add({
    did: document.getElementById('did').value.trim(),
    name: document.getElementById('name').value.trim(),
    password: document.getElementById('password').value,
    quality: document.getElementById('quality').value,
    host_hint: document.getElementById('host').value.trim() || undefined,
  });
  document.getElementById('discover').onclick = async () => {
    const box = document.getElementById('found');
    box.innerHTML = `<div class="muted">${t('wifi.searching')}</div>`;
    try {
      const data = await api.post('/api/discover', { timeout: 3 });
      const devices = data.devices || [];
      box.innerHTML = `<div class="card"><div class="body">
        <h3 style="margin-top:0">${t('add.found', { n: devices.length })}</h3>
        ${devices.length ? `<table><thead><tr><th>IP</th><th>DID (${t('add.partial')})</th><th>${t('add.port')}</th><th></th></tr></thead><tbody>
          ${devices.map((d) => `<tr><td>${esc(d.ip)}</td><td class="mono">${esc(d.did)}</td>
            <td>${esc(d.port)}</td><td>${d.camera_id
              ? `<span class="muted small">${t('add.alreadyAdded', { id: esc(d.camera_id) })}</span>`
              : `<button data-pick="${esc(d.ip)}" data-did="${esc(d.did)}">${t('add.fill')}</button>`}</td></tr>`).join('')}
        </tbody></table>` : `<p class="muted">${t('add.noReply')}</p>`}
        <p class="muted small">${t('add.fillHelp')}</p>
      </div></div>`;
      box.querySelectorAll('[data-pick]').forEach((b) => {
        b.onclick = () => {
          document.getElementById('host').value = b.dataset.pick;
          const did = b.dataset.did || '';
          if (did && !did.includes('…')) {
            document.getElementById('did').value = did;
            toast(t('add.didSet', { did }));
          } else {
            document.getElementById('did').focus();
            toast(t('add.addressSet'));
          }
        };
      });
    } catch (e) {
      box.innerHTML = `<div class="card"><div class="body muted">${esc(e.message)}</div></div>`;
    }
  };
}

// ------------------------------------------------------------------ Wi-Fi wizard
async function renderWifi() {
  app.innerHTML = `<div class="wrap">
    <div class="row" style="margin-bottom:12px">
      <button id="back">${t('wifi.back')}</button><h2 style="margin:0">${t('wifi.title')}</h2>
    </div>
    <div class="card"><div class="body col">
      <div class="wizard-steps" id="steps"></div>
      <div id="wifi-body"><div class="muted">${t('wifi.loading')}</div></div>
    </div></div>
  </div>`;
  document.getElementById('back').onclick = () => go('/');
  const refresh = async () => {
    try {
      const st = await api.get('/api/provision/state');
      renderWifiState(st);
      if (S.pollTimer) { clearInterval(S.pollTimer); S.pollTimer = null; }
      if (st.running) S.pollTimer = setInterval(refresh, 1200);
    } catch (e) {
      document.getElementById('wifi-body').innerHTML =
        `<p class="muted">${esc(translateServer(e.message))}</p>`;
    }
  };
  const provText = (st, field) => {
    const key = getLang() === 'en' ? `${field}_en` : field;
    return translateServer(st[key] || st[field] || '');
  };
  const renderWifiState = (st) => {
    const steps = ['prepare', 'connecting', 'scanning', 'choose', 'applying', 'waiting', 'done'];
    document.getElementById('steps').innerHTML = steps.map((s) =>
      `<span class="step ${st.step === s ? 'active' : ''}">${t(`wifi.${s}`)}</span>`).join('');
    const body = document.getElementById('wifi-body');
    if (st.step === 'idle' || st.step === 'error') {
      body.innerHTML = `${st.error ? `<p style="color:#ff7b72">${esc(provText(st, 'error'))}</p>` : ''}
        <p class="muted">${t('wifi.needAp')}</p>
        <div class="form-grid">
          <label class="field"><span>${t('wifi.did')}</span><input id="w-did" placeholder="G016638JHDXM"></label>
          <label class="field"><span>${t('wifi.password')}</span>
            <input id="w-pwd" type="password"></label>
        </div>
        <div class="row"><button class="primary" id="w-start">${t('wifi.start')}</button></div>`;
      document.getElementById('w-start').onclick = async () => {
        try {
          await api.post('/api/provision/start', {
            did: document.getElementById('w-did').value.trim(),
            camera_password: document.getElementById('w-pwd').value,
          });
          refresh();
        } catch (e) { toast(e.message, true); }
      };
      return;
    }
    if (st.step === 'choose') {
      const nets = st.networks || [];
      body.innerHTML = `<p>${esc(provText(st, 'message'))}</p>
        <div class="col" id="nets">${nets.map((n, i) => `<label class="row">
          <input type="radio" name="net" value="${i}">
          <span>${esc(n.ssid || '—')} ${n.sig >= 0 ? `(${n.sig})` : ''}</span></label>`).join('')}
        </div>
        <div class="form-grid">
          <label class="field"><span>${t('wifi.ssid')}</span><input id="w-ssid"></label>
          <label class="field"><span>${t('wifi.netPassword')}</span>
            <input id="w-netpwd" type="password"></label>
        </div>
        <div class="row"><button class="primary" id="w-apply">${t('wifi.apply')}</button>
          <button id="w-cancel">${t('wifi.cancel')}</button></div>`;
      document.querySelectorAll('input[name=net]').forEach((r) => {
        r.onchange = () => {
          const n = nets[Number(r.value)];
          if (n) document.getElementById('w-ssid').value = n.ssid || '';
        };
      });
      document.getElementById('w-apply').onclick = async () => {
        try {
          await api.post('/api/provision/apply', {
            ssid: document.getElementById('w-ssid').value.trim(),
            password: document.getElementById('w-netpwd').value,
          });
          refresh();
        } catch (e) { toast(e.message, true); }
      };
      document.getElementById('w-cancel').onclick = async () => {
        await api.post('/api/provision/cancel', {}); refresh();
      };
      return;
    }
    body.innerHTML = `<p>${esc(provText(st, 'message') || st.step)}</p>
      ${st.error ? `<p style="color:#ff7b72">${esc(provText(st, 'error'))}</p>` : ''}
      ${st.step === 'done' ? `<p class="muted">${t('wifi.ip', { ip: esc(st.resolved_ip) })}</p>
        <a href="#/">${t('common.backCameras')} →</a>` : '<div class="progress"><i style="width:60%"></i></div>'}
      ${st.step !== 'done' ? `<div class="row"><button id="w-cancel2">${t('wifi.cancel')}</button></div>` : ''}`;
    document.getElementById('w-cancel2')?.addEventListener('click', async () => {
      await api.post('/api/provision/cancel', {}); refresh();
    });
  };
  await refresh();
}

// ------------------------------------------------------------------ routes
async function route() {
  if (!S.state || !S.state.authorized) return;
  clearTimers();
  const hash = location.hash.replace(/^#/, '') || '/';
  const parts = hash.split('/').filter(Boolean);
  try {
    if (!parts.length) {
      await loadState();
      renderCameras();
    } else if (parts[0] === 'cam' && parts[1]) {
      if (parts[2] === 'settings') await renderSettings(parts[1]);
      else if (parts[2] === 'sd') await renderSd(parts[1]);
      else await renderView(parts[1]);
    } else if (parts[0] === 'add') {
      await renderAdd();
    } else if (parts[0] === 'wifi') {
      await renderWifi();
    } else {
      go('/');
    }
  } catch (e) {
    if (e.status === 401) { await boot(); return; }
    toast(e.message, true);
  }
}

async function boot() {
  try {
    await loadState();
  } catch (e) {
    app.innerHTML = `<div class="wrap"><p class="muted">${t('gate.noService', { error: esc(e.message) })}</p></div>`;
    return;
  }
  if (S.state.needs_setup || !S.state.authorized) {
    renderGate();
    return;
  }
  topbar.hidden = false;
  setEvents();
  await route();
}

document.getElementById('logout').onclick = async () => {
  try { await api.post('/api/logout', {}); } catch (e) { /* noop */ }
  if (S.events) { try { S.events.close(); } catch (e) { /* noop */ } }
  S.state = null;
  await boot();
};

window.addEventListener('hashchange', () => { route(); });
window.addEventListener('lookcam:lang', async () => {
  applyStatic();
  await boot();
});
mountLangSwitch();
applyStatic();
boot();
