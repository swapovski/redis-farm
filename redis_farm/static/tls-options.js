/* redis-farm-tls-ui-hf5: presentation only; no API/Redis calls. */
(() => {
  "use strict";
  function setupTlsOptions() {
    const box = document.getElementById("seedTlsSettings");
    const toggle = document.getElementById("seedTls");
    const fields = document.getElementById("seedTlsFields");
    const summary = document.getElementById("seedTlsSummary");
    const button = document.getElementById("seedTlsHelpButton");
    const help = document.getElementById("seedTlsHelp");
    const dialog = document.getElementById("addDialog");
    if (!box || !toggle || !fields || !summary || !button || !help || !dialog) return;
    if (box.dataset.tlsUiBound === "true") return;
    box.dataset.tlsUiBound = "true";
    let pinned = false;
    function syncFields() {
      fields.disabled = !toggle.checked || toggle.disabled;
    }
    function showHelp() {
      help.hidden = false;
      button.setAttribute("aria-expanded", "true");
    }
    function hideHelp() {
      pinned = false;
      help.hidden = true;
      button.setAttribute("aria-expanded", "false");
    }
    toggle.addEventListener("change", syncFields);
    // The existing scan code disables and re-enables inputs. Track that state
    // without changing the discovery payload or its completion handlers.
    new MutationObserver(syncFields).observe(toggle, {attributes:true, attributeFilter:["disabled", "checked"]});
    document.getElementById("discoveryForm")?.addEventListener("reset", () => setTimeout(syncFields, 0));
    box.addEventListener("toggle", syncFields);
    summary.addEventListener("pointerenter", event => { if (event.pointerType === "mouse") showHelp(); });
    summary.addEventListener("pointerleave", () => { if (!pinned && document.activeElement !== button) hideHelp(); });
    button.addEventListener("focus", showHelp);
    button.addEventListener("blur", () => { if (!pinned) hideHelp(); });
    button.addEventListener("click", event => {
      event.preventDefault(); event.stopPropagation();
      pinned = !pinned;
      if (pinned) showHelp(); else hideHelp();
    });
    // Clicking help text must not toggle its parent <details>.
    help.addEventListener("click", event => { event.preventDefault(); event.stopPropagation(); });
    summary.addEventListener("click", () => hideHelp());
    document.addEventListener("click", event => { if (!summary.contains(event.target)) hideHelp(); });
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && !help.hidden && dialog.open) {
        hideHelp(); event.preventDefault(); event.stopPropagation();
      }
    }, true);
    dialog.addEventListener("close", hideHelp);
    syncFields();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", setupTlsOptions);
  else setupTlsOptions();
})();
