/* Local, installation-selected UI language. This never translates commands or data. */
"use strict";
(() => {
  const config = JSON.parse(document.getElementById('uiCatalog')?.textContent || '{}');
  const messages = config.messages || {}, errors = config.errors || {};
  function t(source, ...args) {
    if (Array.isArray(source) && source.raw) source = source.map((s,i)=>s+(i<args.length?`{${i}}`:'')).join('');
    let value = messages[source] ?? source;
    if (Object.prototype.hasOwnProperty.call(messages, source)) value = value.replace(/\badmin\b/g, ()=>config.admin_user || 'admin');
    return String(value).replace(/\{(\d+)\}/g,(m,i)=>Number(i)<args.length ? String(args[Number(i)]) : m);
  }
  function error(text, code) {
    if (['redis_error','authentication_required','authentication_failed','permission_denied','unsupported_command','connection_failed','parse_error','dns_error','dns_timeout','dns_busy','cli_error'].includes(code)) return text;
    return Object.prototype.hasOwnProperty.call(messages,text) ? t(text) : (errors[code] || text);
  }
  window.RFI18n = {t, error, locale:config.language==='ru'?'ru-RU':'en-US'};
  // Native required-field validation otherwise uses the browser's language.
  document.addEventListener('invalid', event => {
    const input = event.target;
    if (!input.setCustomValidity) return;
    if (input.validity.valueMissing) input.setCustomValidity(t(input.type==='password'?'Enter the password.':'Fill in this field.'));
    else if (!input.validity.valid) input.setCustomValidity(t('Check this value.'));
  }, true);
  document.addEventListener('input', event => event.target.setCustomValidity?.(''), true);
  document.addEventListener('change', event => event.target.setCustomValidity?.(''), true);
})();
