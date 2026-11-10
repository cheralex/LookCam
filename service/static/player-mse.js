// Fallback MSE/fMP4 player: the server sends an init segment and moof/mdat.
// Needed where WebCodecs H.264 is unavailable (Firefox without a hardware path,
// Safari), and on iOS through ManagedMediaSource.
import { t, translateServer } from './i18n.js';

export class MsePlayer {
  constructor(host, cameraId) {
    this.host = host;
    this.cameraId = cameraId;
    this.wrap = host.shadowRoot.querySelector('.wrap');
    this.video = document.createElement('video');
    this.video.muted = true;
    this.video.autoplay = true;
    this.video.playsInline = true;
    this.video.controls = true;
    this.video.style.cssText = 'width:100%;height:100%;display:block;object-fit:contain';
    this.wrap.appendChild(this.video);
    this.ws = null;
    this.ms = null;
    this.sb = null;
    this.codec = '';
    this.queue = [];
    this.appending = false;
    this.stopped = false;
    this.timer = null;
    this.initDone = false;
  }

  start() {
    this.stopped = false;
    this.connect();
  }

  connect() {
    if (this.stopped) return;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${proto}//${location.host}/ws/live-mp4/${encodeURIComponent(this.cameraId)}`;
    this.host.setStatus(t('mse.connecting'));
    this.ws = new WebSocket(url);
    this.ws.binaryType = 'arraybuffer';
    this.ws.onopen = () => this.host.setStatus(t('mse.waitInit'));
    this.ws.onclose = () => {
      if (!this.stopped) this.scheduleReconnect();
    };
    this.ws.onerror = () => this.host.setStatus(t('mse.connectionError'));
    this.ws.onmessage = (ev) => this.onMessage(ev);
  }

  scheduleReconnect() {
    if (this.timer || this.stopped) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      this.connect();
    }, 1500);
  }

  onMessage(ev) {
    if (typeof ev.data === 'string') {
      let msg = {};
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === 'config') {
        this.codec = msg.codec || 'avc1.4D0028';
        this.host.setStatus(`MSE: ${this.codec}`);
        this.prepare();
      } else if (msg.type === 'reset') {
        this.restart();
      } else if (msg.type === 'error') {
        this.host.showMessage(translateServer(msg.error) || t('mse.error'));
      }
      return;
    }
    const buf = new Uint8Array(ev.data);
    if (!this.ms) return;
    if (!this.initDone) {
      this.initDone = true;
      this.append(buf);
    } else {
      this.append(buf);
    }
  }

  prepare() {
    this.initDone = false;
    this.queue = [];
    if (!this.codec) return;
    if (!this.video.parentNode) this.wrap.appendChild(this.video);
    if (typeof ManagedMediaSource !== 'undefined') {
      this.ms = new ManagedMediaSource();
    } else if (typeof MediaSource !== 'undefined') {
      this.ms = new MediaSource();
    } else {
      this.host.showMessage(t('mse.unsupported'));
      return;
    }
    this.video.src = URL.createObjectURL(this.ms);
    this.ms.addEventListener('sourceopen', () => {
      try {
        this.sb = this.ms.addSourceBuffer(`video/mp4; codecs="${this.codec}"`);
        this.sb.mode = 'segments';
        this.sb.addEventListener('updateend', () => {
          this.appending = false;
          this.pump();
        });
        this.sb.addEventListener('error', () => this.restart());
        this.video.play().catch(() => {});
        this.pump();
      } catch (e) {
        this.host.showMessage('MSE: ' + e.message);
      }
    }, { once: true });
  }

  append(data) {
    this.queue.push(data);
    this.pump();
  }

  pump() {
    if (!this.sb || this.appending) return;
    if (!this.queue.length) {
      if (this.ms && this.ms.readyState === 'open' && this.sb.buffered.length) {
        const end = this.sb.buffered.end(this.sb.buffered.length - 1);
        if (this.video.currentTime < end - 6) this.video.currentTime = end - 2;
      }
      return;
    }
    const chunk = this.queue.shift();
    try {
      this.appending = true;
      this.sb.appendBuffer(chunk);
    } catch (e) {
      this.appending = false;
      if (e.name === 'QuotaExceededError') {
        this.queue.unshift(chunk);
        this.trim();
      } else {
        this.host.setStatus('MSE append: ' + e.message);
      }
    }
  }

  trim() {
    if (!this.sb || this.sb.updating) return;
    try {
      if (this.sb.buffered.length) {
        const end = this.sb.buffered.end(this.sb.buffered.length - 1);
        this.sb.remove(0, Math.max(0, end - 20));
      }
    } catch (e) { /* noop */ }
  }

  restart() {
    this.stop(false);
    this.stopped = false;
    this.start();
  }

  stop(remove = false) {
    this.stopped = true;
    if (this.timer) { clearTimeout(this.timer); this.timer = null; }
    if (this.ws) { try { this.ws.close(); } catch (e) { /* noop */ } this.ws = null; }
    if (this.ms) {
      try {
        if (this.ms.readyState === 'open') this.ms.endOfStream();
      } catch (e) { /* noop */ }
      this.ms = null;
    }
    this.sb = null;
    this.queue = [];
    if (this.video) {
      try {
        this.video.removeAttribute('src');
        this.video.load();
        if (remove) this.video.remove();
      } catch (e) { /* noop */ }
    }
  }
}
