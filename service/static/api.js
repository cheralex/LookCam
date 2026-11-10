// Thin REST API wrapper: JSON, cookie session, errors.
import { t, locale, translateServer } from './i18n.js';

export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function request(method, path, body) {
  const opts = { method, headers: {}, credentials: 'same-origin' };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const type = res.headers.get('content-type') || '';
  const data = type.includes('json') ? await res.json().catch(() => ({})) : await res.text();
  if (!res.ok) {
    const msg = translateServer((data && data.error) ||
                (typeof data === 'string' ? data : '') || `HTTP ${res.status}`);
    throw new ApiError(res.status, msg);
  }
  return data;
}

export const api = {
  get: (p) => request('GET', p),
  post: (p, b) => request('POST', p, b),
  put: (p, b) => request('PUT', p, b),
  patch: (p, b) => request('PATCH', p, b),
  del: (p) => request('DELETE', p),
};

export function humanSize(bytes) {
  if (!bytes) return `0 ${t('unit.b')}`;
  const units = [t('unit.b'), t('unit.kb'), t('unit.mb'), t('unit.gb')];
  let i = 0, n = bytes;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

export function humanTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.toLocaleString(locale(), { hour12: false });
}

export function stateLabel(state) {
  return t(`state.${state}`) === `state.${state}`
    ? (state || '—') : t(`state.${state}`);
}

export function toast(message, isError = false) {
  const el = document.createElement('div');
  el.className = `toast${isError ? ' err' : ''}`;
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), isError ? 7000 : 3500);
}
