// Smoke test of the i18n dictionary without a browser.
// Usage: node lookcam_deepsik/tools/check_i18n.mjs
globalThis.localStorage = { getItem: () => null, setItem: () => {} };
globalThis.navigator = { language: 'en-US' };
globalThis.document = { documentElement: {}, body: {}, getElementById: () => null };
globalThis.window = { dispatchEvent: () => {} };
globalThis.CustomEvent = class {};

import { readFileSync } from 'node:fs';
const source = readFileSync(new URL('../service/static/i18n.js', import.meta.url), 'utf8');
const i18n = await import(
  'data:text/javascript;base64,' + Buffer.from(source).toString('base64')
);
const check = (ok, message) => {
  if (!ok) {
    console.error('FAIL', message);
    process.exit(1);
  }
};

check(i18n.getLang() === 'en', 'default English');
check(i18n.t('nav.cameras') === 'Cameras', 'English navigation');
check(i18n.t('common.pcs', { n: 2 }) === '2 pcs', 'English interpolation');
check(i18n.t('state.streaming') === 'live', 'English state');
i18n.setLang('ru');
check(i18n.getLang() === 'ru', 'switch to Russian');
check(i18n.t('nav.cameras') === 'Камеры', 'Russian navigation');
check(i18n.t('common.fps', { n: 10 }) === '10 кадр/с', 'Russian units');
check(i18n.translateServer('неверный пароль') === 'неверный пароль',
      'Russian server text unchanged');
i18n.setLang('en');
check(i18n.translateServer('неверный пароль') === 'wrong password',
      'English server text');
check(i18n.labelOf({ label: 'Яркость', label_en: 'Brightness' }) === 'Brightness',
      'English schema label');
i18n.setLang('ru');
check(i18n.labelOf({ label: 'Яркость', label_en: 'Brightness' }) === 'Яркость',
      'Russian schema label');
console.log('PASS i18n en/ru');
