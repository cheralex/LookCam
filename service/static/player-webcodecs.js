// WebCodecs player: the server sends Annex B, the browser decodes in hardware.
// WS format: [u8 type][u8 flags][u32 ts_ms][payload]; type 1 = video, 2 = config.
// Pass OffscreenCanvas to the Worker; do not call flush() — see webcodecs.PAGE.
import { getLang, t, translateServer } from './i18n.js';

const WS_VIDEO = 1;
const WS_CONFIG = 2;
const WS_AUDIO = 3;

export class LivePlayer extends HTMLElement {
  static get observedAttributes() { return ['camera-id']; }

  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.shadowRoot.innerHTML = `
      <style>
        :host { display:block; background:#000; }
        .wrap { position:relative; aspect-ratio:16/9; background:#000; overflow:hidden; }
        canvas { width:100%; height:100%; display:block; object-fit:contain; }
        .status { position:absolute; left:8px; top:8px; padding:2px 7px;
          border-radius:6px; background:#000a; color:#ddd;
          font:12px ui-monospace,Consolas,monospace; pointer-events:none; }
        .msg { position:absolute; inset:0; display:flex; align-items:center;
          justify-content:center; color:#8b949e; text-align:center; padding:20px; }
      </style>
      <div class="wrap">
        <canvas></canvas>
        <div class="status">…</div>
        <div class="msg" hidden></div>
      </div>`;
    this.canvas = this.shadowRoot.querySelector('canvas');
    this.statusEl = this.shadowRoot.querySelector('.status');
    this.msgEl = this.shadowRoot.querySelector('.msg');
    this.ws = null;
    this.worker = null;
    this.decoder = null;
    this.codec = '';
    this.stopped = true;
    this.reconnectTimer = null;
    this.frames = 0;
    this.lastStats = { fps: 0, width: 0, height: 0 };
    this._snapshotResolve = null;
    this._lastFrameAt = 0;
  }

  connectedCallback() { this.start(); }
  disconnectedCallback() { this.stop(); }
  attributeChangedCallback(name, oldv, newv) {
    if (oldv !== newv && this.isConnected) this.restart();
  }

  get cameraId() { return this.getAttribute('camera-id') || ''; }
  get quality() { return this.getAttribute('quality') || ''; }

  showMessage(text) {
    this.msgEl.textContent = text;
    this.msgEl.hidden = !text;
  }

  setStatus(text) { this.statusEl.textContent = text; }

  restart() {
    this.stop();
    this.start();
  }

  start() {
    if (!this.cameraId || this.stopped === false) return;
    this.stopped = false;
    this.frames = 0;
    this.showMessage('');
    if (!('VideoDecoder' in window)) {
      this.showMessage(t('player.noWebcodecs'));
      this.useMseFallback();
      return;
    }
    this.connect();
  }

  useMseFallback() {
    this.showMessage(t('player.mseConnecting'));
    import('./player-mse.js').then(({ MsePlayer }) => {
      if (this.stopped) return;
      if (!this.mse) {
        this.mse = new MsePlayer(this, this.cameraId);
        this.mse.start();
      }
    }).catch((e) => this.showMessage('MSE: ' + e.message));
  }

  connect() {
    if (this.stopped) return;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${proto}//${location.host}/ws/live/${encodeURIComponent(this.cameraId)}`;
    this.setStatus(t('player.connecting'));
    try {
      this.ws = new WebSocket(url);
    } catch (e) {
      this.setStatus(t('player.wsError'));
      this.scheduleReconnect();
      return;
    }
    this.ws.binaryType = 'arraybuffer';
    this.ws.onopen = () => { this.setStatus(t('player.openWaiting')); this.setupDecoder(); };
    this.ws.onclose = () => {
      this.setStatus(t('player.closed'));
      if (!this.stopped) this.scheduleReconnect();
    };
    this.ws.onerror = () => { this.setStatus(t('player.connectionError')); };
    this.ws.onmessage = (ev) => this.onMessage(ev);
  }

  scheduleReconnect() {
    if (this.reconnectTimer || this.stopped) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, 1200);
  }

  setupDecoder() {
    // A repeated WS-open must not create a second Worker: OffscreenCanvas can
    // be transferred only once, and the decoder survives a reconnect.
    if (this.worker || this.decoder) return;
    const canWorker = typeof Worker !== 'undefined' &&
      typeof this.canvas.transferControlToOffscreen === 'function' &&
      typeof OffscreenCanvas !== 'undefined';
    if (canWorker) {
      try {
        const offscreen = this.canvas.transferControlToOffscreen();
        this.worker = new Worker('/player-worker.js');
        this.worker.onmessage = (e) => this.onWorkerMessage(e.data);
        this.worker.postMessage({ type: 'init', canvas: offscreen, lang: getLang() }, [offscreen]);
        this.mode = 'worker';
        return;
      } catch (e) {
        this.worker = null;
      }
    }
    this.mode = 'main';
    this.ctx = this.canvas.getContext('2d', { desynchronized: true });
  }

  onMessage(ev) {
    if (typeof ev.data === 'string') {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'reset') {
          this.setStatus(t('player.reset', {
            reason: msg.reason || t('player.streamRestarted'),
          }));
          this.resetDecoder();
        } else if (msg.type === 'error') {
          this.showMessage(translateServer(msg.error) || t('player.cameraError'));
        }
      } catch (e) { /* not our payload */ }
      return;
    }
    const buf = ev.data;
    if (!(buf instanceof ArrayBuffer) || buf.byteLength < 6) return;
    const view = new DataView(buf);
    const type = view.getUint8(0);
    const flags = view.getUint8(1);
    const ts = view.getUint32(2);
    const payload = buf.slice(6);
    if (type === WS_CONFIG) {
      try {
        const cfg = JSON.parse(new TextDecoder().decode(payload));
        this.applyConfig(cfg);
      } catch (e) {
        this.setStatus(t('player.badConfig'));
      }
    } else if (type === WS_VIDEO) {
      this.decode(new Uint8Array(payload), !!flags, ts);
    } else if (type === WS_AUDIO) {
      // Audio arrives in a separate phase; frames do not block video.
    }
  }

  applyConfig(cfg) {
    let codec = cfg.codec || '';
    if (!codec && cfg.sps) {
      const b = hexBytes(cfg.sps);
      if (b.length >= 4) codec = 'avc1.' + [b[1], b[2], b[3]]
        .map((x) => x.toString(16).padStart(2, '0')).join('');
    }
    if (!codec) return;
    this.codec = codec;
    this.setStatus(`${codec} ${cfg.width || '?'}×${cfg.height || '?'}`);
    if (this.mode === 'worker' && this.worker) {
      this.worker.postMessage({ type: 'config', codec, sps: cfg.sps, pps: cfg.pps });
    } else {
      this.configureMainDecoder(codec);
    }
    this.dispatchEvent(new CustomEvent('config', { detail: cfg }));
  }

  configureMainDecoder(codec) {
    this.resetMainDecoder();
    try {
      this.decoder = new VideoDecoder({
        output: (frame) => this.drawMain(frame),
        error: (e) => { this.setStatus(t('player.decoder', { error: e.message })); },
      });
      this.decoder.configure({ codec, optimizeForLatency: true });
    } catch (e) {
      this.showMessage(t('player.decoderStart', { error: e.message }));
    }
  }

  resetMainDecoder() {
    if (this.decoder) {
      try { this.decoder.close(); } catch (e) { /* noop */ }
      this.decoder = null;
    }
  }

  resetDecoder() {
    if (this.mode === 'worker' && this.worker) {
      this.worker.postMessage({ type: 'reset' });
    } else {
      this.resetMainDecoder();
    }
  }

  drawMain(frame) {
    if (this.ctx) {
      if (this.canvas.width !== frame.displayWidth || this.canvas.height !== frame.displayHeight) {
        this.canvas.width = frame.displayWidth;
        this.canvas.height = frame.displayHeight;
      }
      this.ctx.drawImage(frame, 0, 0, this.canvas.width, this.canvas.height);
    }
    frame.close();
    this.onFrame(frame.displayWidth, frame.displayHeight);
    this.updateStats();
  }

  decode(bytes, key, ts) {
    if (this.mode === 'worker' && this.worker) {
      const copy = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
      this.worker.postMessage({ type: 'video', data: copy, key, ts }, [copy]);
      return;
    }
    if (!this.decoder || this.decoder.state !== 'configured') return;
    let timestamp = Math.round(ts * 1000);
    if (!timestamp || timestamp <= (this.lastTs || 0)) timestamp = (this.lastTs || 0) + 1000;
    this.lastTs = timestamp;
    try {
      this.decoder.decode(new EncodedVideoChunk({
        type: key ? 'key' : 'delta', timestamp, data: bytes,
      }));
    } catch (e) {
      this.setStatus('decode: ' + e.message);
    }
  }

  onFrame(width, height) {
    this.frames++;
    this._lastFrameAt = performance.now();
    this.dispatchEvent(new CustomEvent('frame', { detail: { width, height } }));
  }

  onWorkerMessage(msg) {
    if (msg.type === 'stats') {
      this.onFrame(msg.width, msg.height);
      this.lastLatency = msg.latency;
      this.updateStats();
    } else if (msg.type === 'error') {
      this.setStatus(t('player.decoder', { error: msg.error }));
    } else if (msg.type === 'configured') {
      this.setStatus(t('player.decoder', { error: msg.codec }));
    } else if (msg.type === 'snapshot') {
      if (this._snapshotResolve) {
        if (msg.error) {
          this._snapshotResolve(null);
        } else {
          this._snapshotResolve(new Blob([msg.data], { type: 'image/jpeg' }));
        }
        this._snapshotResolve = null;
      }
    }
  }

  updateStats() {
    const now = performance.now();
    if (!this._statsAt) this._statsAt = now;
    if (now - this._statsAt < 1000) return;
    const fps = (this.frames * 1000 / (now - this._statsAt)).toFixed(1);
    this._statsAt = now;
    this.frames = 0;
    this.lastStats = { fps: Number(fps), width: this.canvas.width,
                       height: this.canvas.height, latency: this.lastLatency || 0 };
    this.setStatus(` ${t('common.fps', { n: fps })}  ${this.codec || ''}`);
    this.dispatchEvent(new CustomEvent('stats', { detail: this.lastStats }));
  }

  async snapshot() {
    if (this.mode === 'worker' && this.worker) {
      return new Promise((resolve) => {
        this._snapshotResolve = resolve;
        this.worker.postMessage({ type: 'snapshot' });
        setTimeout(() => {
          if (this._snapshotResolve === resolve) { this._snapshotResolve = null; resolve(null); }
        }, 2000);
      });
    }
    if (!this.canvas.width) return null;
    return new Promise((resolve) => this.canvas.toBlob((b) => resolve(b), 'image/jpeg', 0.92));
  }

  stop() {
    this.stopped = true;
    if (this.reconnectTimer) { clearTimeout(this.reconnectTimer); this.reconnectTimer = null; }
    if (this.ws) { try { this.ws.close(); } catch (e) { /* noop */ } this.ws = null; }
    if (this.worker) {
      this.worker.terminate();
      this.worker = null;
      // The canvas has already been transferred to the Worker — the next start
      // needs a new canvas.
      this.replaceCanvas();
    }
    this.resetMainDecoder();
    if (this.mse) { this.mse.stop(true); this.mse = null; }
  }

  replaceCanvas() {
    const fresh = document.createElement('canvas');
    fresh.className = this.canvas.className;
    this.canvas.replaceWith(fresh);
    this.canvas = fresh;
    this.ctx = null;
  }
}

function hexBytes(hex) {
  const clean = (hex || '').replace(/[^0-9a-fA-F]/g, '');
  const out = new Uint8Array(clean.length >> 1);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(clean.substr(i * 2, 2), 16);
  return out;
}

if (!customElements.get('live-player')) customElements.define('live-player', LivePlayer);
