// WebCodecs decoder in a Worker: hardware decoding and OffscreenCanvas.
// Annex B is passed through (no description needed); the service already patched
// SPS (max_num_reorder_frames=0). Do not call flush() on every frame — it requires
// the next keyframe.

const TEXT = {
  en: { noWebcodecs: 'WebCodecs is not available in the Worker', noCanvas: 'no canvas' },
  ru: { noWebcodecs: 'WebCodecs недоступен в Worker', noCanvas: 'нет canvas' },
};
let lang = 'en';
function wt(key) { return (TEXT[lang] || TEXT.en)[key]; }

let canvas = null;
let ctx = null;
let decoder = null;
let codec = '';
let decoded = 0;
let lastTs = 0;
let submitted = 0;
const submittedAt = [];

self.onmessage = (event) => {
  const msg = event.data || {};
  if (msg.type === 'init') {
    canvas = msg.canvas;
    lang = msg.lang === 'ru' ? 'ru' : 'en';
    ctx = canvas ? canvas.getContext('2d', { desynchronized: true }) : null;
    postMessage({ type: 'ready', worker: true });
  } else if (msg.type === 'config') {
    configure(msg);
  } else if (msg.type === 'video') {
    decode(msg);
  } else if (msg.type === 'reset') {
    closeDecoder();
  } else if (msg.type === 'snapshot') {
    snapshot();
  }
};

function closeDecoder() {
  if (decoder) {
    try { decoder.close(); } catch (e) { /* already closed */ }
    decoder = null;
  }
}

function configure(msg) {
  closeDecoder();
  codec = msg.codec || '';
  submitted = 0;
  submittedAt.length = 0;
  if (!('VideoDecoder' in self) || !codec) {
    postMessage({ type: 'error', error: wt('noWebcodecs') });
    return;
  }
  try {
    decoder = new VideoDecoder({
      output: draw,
      error: (e) => postMessage({ type: 'error', error: e.message }),
    });
    decoder.configure({ codec, optimizeForLatency: true });
    postMessage({ type: 'configured', codec });
  } catch (e) {
    postMessage({ type: 'error', error: 'configure: ' + e.message });
  }
}

function decode(msg) {
  if (!decoder || decoder.state !== 'configured') return;
  const bytes = new Uint8Array(msg.data);
  if (!bytes.length) return;
  let ts = Math.round((msg.ts || 0) * 1000);
  if (!ts || ts <= lastTs) ts = lastTs + 1000;
  lastTs = ts;
  try {
    decoder.decode(new EncodedVideoChunk({
      type: msg.key ? 'key' : 'delta', timestamp: ts, data: bytes,
    }));
    submitted++;
    submittedAt.push(performance.now());
    if (submittedAt.length > 60) submittedAt.shift();
  } catch (e) {
    postMessage({ type: 'error', error: 'decode: ' + e.message });
  }
}

function draw(frame) {
  if (ctx && canvas) {
    if (canvas.width !== frame.displayWidth || canvas.height !== frame.displayHeight) {
      canvas.width = frame.displayWidth;
      canvas.height = frame.displayHeight;
    }
    ctx.drawImage(frame, 0, 0, canvas.width, canvas.height);
  }
  const at = submittedAt.shift();
  const latency = at ? Math.max(0, Math.round(performance.now() - at)) : 0;
  frame.close();
  decoded++;
  if ((decoded & 7) === 0) {
    postMessage({ type: 'stats', decoded, submitted, latency,
                  width: canvas ? canvas.width : 0,
                  height: canvas ? canvas.height : 0 });
  }
}

async function snapshot() {
  if (!canvas) { postMessage({ type: 'snapshot', error: wt('noCanvas') }); return; }
  try {
    const blob = await canvas.convertToBlob({ type: 'image/jpeg', quality: 0.92 });
    const buf = await blob.arrayBuffer();
    postMessage({ type: 'snapshot', data: buf }, [buf]);
  } catch (e) {
    postMessage({ type: 'snapshot', error: e.message });
  }
}
