/* Same-origin login preflight. No password is sent by fetch or stored here. */
"use strict";
const login_session_js_tr = window.RFI18n.t;
(() => {
  const form = document.getElementById("loginForm");
  if (!form) return;
  const submit = form.querySelector('button[type="submit"]');
  const csrf = form.querySelector('[name="csrf"]');
  const error = document.getElementById("loginError");
  let busy = false;
  function fail() {
    error.textContent = login_session_js_tr("Не удалось обновить форму входа. Повторите попытку.");
    error.dataset.code = "login_refresh";
    error.hidden = false;
  }
  async function refresh() {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch("/login/state", {
        method:"GET", credentials:"same-origin", cache:"no-store",
        headers:{"Accept":"application/json", "X-RF-Login":"refresh"},
        signal:controller.signal
      });
      if (!response.ok) throw new Error("login refresh failed");
      const state = await response.json();
      if (state.authenticated === true) { window.location.replace("/"); return false; }
      if (typeof state.csrf !== "string" || !state.csrf || state.csrf.length > 1024) throw new Error("invalid token");
      csrf.value = state.csrf;
      return true;
    } finally { clearTimeout(timer); }
  }
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (busy) return;
    busy = true; submit.disabled = true; error.hidden = true;
    try {
      if (await refresh()) {
        // One native POST follows this deliberate click. Never replay an LDAP
        // bind after an error and never submit credentials in background tasks.
        HTMLFormElement.prototype.submit.call(form);
      }
    } catch (_) { fail(); busy = false; submit.disabled = false; }
  });
  window.addEventListener("pageshow", event => {
    // Some browsers restore the form's disabled state and hidden input from
    // history. Recreate only its token; passwords are not stored by this script.
    busy = false; submit.disabled = false;
    if (event.persisted) {
      form.querySelector('[name="password"]').value = "";
      refresh().catch(fail);
    }
  });
})();
