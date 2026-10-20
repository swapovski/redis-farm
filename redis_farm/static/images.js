/* Lift the radish by the leaf base; the body swings beneath the grip. */
"use strict";
(() => {
  const triggers = [...document.querySelectorAll("[data-image-hold]")];
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let active = null;
  let frame = 0;
  const inside = (r, x, y) => r.width > 0 && r.height > 0 &&
    x >= r.left && x < r.right && y >= r.top && y < r.bottom;
  const clamp = value => Math.max(-1, Math.min(1, value));

  function stop() {
    if (!active) return;
    const old = active;
    active = null;
    // Restore the source exactly when the held preview ends.
    if (old.original) {
      if (old.originalVisibilityValue) {
        old.original.style.setProperty("visibility", old.originalVisibilityValue, old.originalVisibilityPriority);
      } else {
        old.original.style.removeProperty("visibility");
      }
    }
    cancelAnimationFrame(frame);
    frame = 0;
    // Clear state before releasing capture: lostpointercapture can re-enter.
    old.layer.hidden = true;
    if (old.popover) {
      try { old.layer.hidePopover(); } catch (_) { /* already closed */ }
    }
    old.layer.remove();
    try {
      if (old.original.hasPointerCapture?.(old.pointerId)) {
        old.original.releasePointerCapture(old.pointerId);
      }
    } catch (_) { /* Pointer ended, element detached, or API unavailable. */ }
  }

  function sourceAvailable(s) {
    return s.original.isConnected && (!s.dialog || s.dialog.open) &&
      (!s.details || s.details.open);
  }

  function position(x, y) {
    const s = active;
    if (!s) return;
    s.x = x;
    s.y = y;
    // Natural pixels, exact fractional anchor, and no lag or edge clamping.
    s.layer.style.left = `${x - s.anchorX}px`;
    s.layer.style.top = `${y - s.anchorY}px`;
    // React to short, recent movements anywhere, not distance across the viewport.
    // The preview's translation still follows the cursor without interpolation.
    const now = performance.now();
    const dt = Math.max(8, Math.min(64, now - s.lastMove));
    const vx = clamp((x - s.moveX) / dt / 1.1);
    const vy = clamp((y - s.moveY) / dt / 1.1);
    if (x !== s.moveX || y !== s.moveY) s.drive = [-vy * 5, vx * 5, -vx * 17];
    s.moveX = x;
    s.moveY = y;
    s.lastMove = now;
  }

  function animate(now) {
    const s = active;
    if (!s) return;
    // Leaving the original image is no longer an exit condition.
    if (!sourceAvailable(s)) { stop(); return; }
    const elapsed = Math.min(64, Math.max(0, now - s.last));
    s.last = now;
    if (reducedMotion.matches) {
      s.angles = [0, 0, 0];
      s.image.style.transform = "none";
    } else {
      const t = (now - s.started) / 1000;
      const entry = 1 - Math.exp(-t / 0.16);
      const decay = Math.exp(-elapsed / 190);
      s.drive = s.drive.map(angle => angle * decay);
      // Gentle continuous roll remains visible when a long gesture stops.
      // It exists only during a hold; a normal hover never starts animation.
      const sway = [Math.sin(t * 2.5) * 2.2, Math.sin(t * 2.1 + 0.5) * 2.8,
                    Math.sin(t * 2.8) * 3.5];
      const target = s.drive.map((angle, i) => angle + sway[i] * entry);
      const blend = 1 - Math.exp(-elapsed / 70);
      s.angles = s.angles.map((angle, i) => angle + (target[i] - angle) * blend);
      const [rx, ry, rz] = s.angles;
      s.image.style.transform = `perspective(900px) rotateX(${rx.toFixed(3)}deg) rotateY(${ry.toFixed(3)}deg) rotateZ(${rz.toFixed(3)}deg)`;
    }
    frame = requestAnimationFrame(animate);
  }

  function begin(event, original, trigger) {
    if (event.pointerType !== "mouse" || event.button !== 0 ||
        event.isPrimary === false || !(event.buttons & 1)) return;
    stop();
    if (!original.complete || original.naturalWidth <= 0 || original.naturalHeight <= 0) return;
    if (!inside(original.getBoundingClientRect(), event.clientX, event.clientY)) return;
    let url;
    try { url = new URL(original.currentSrc || original.src, location.href); }
    catch (_) { return; }
    const embedded = /^data:image\/(?:png|webp|jpeg);base64,/i.test(url.href);
    if (!embedded && (url.origin !== location.origin || !["http:", "https:"].includes(url.protocol))) return;
    event.preventDefault(); // No native drag or focus change.
    const width = original.naturalWidth;
    const height = original.naturalHeight;
    // Grab just above the leaf base. 19 CSS px is about 0.5 CSS cm,
    // not a promise of physical centimetres on every monitor / zoom level.
    const fx = Number(trigger.dataset.leafX || "0.5");
    const fy = Number(trigger.dataset.leafY || "0.31");
    const anchorX = width * Math.max(0, Math.min(1, fx));
    const anchorY = Math.max(0, height * Math.max(0, Math.min(1, fy)) - 19);
    const layer = document.createElement("div");
    layer.className = "image-hold-preview";
    layer.setAttribute("aria-hidden", "true");
    layer.hidden = true;
    layer.style.width = `${width}px`;
    layer.style.height = `${height}px`;
    const image = document.createElement("img");
    image.alt = "";
    image.draggable = false;
    image.src = url.href;
    image.width = width;
    image.height = height;
    image.style.width = `${width}px`;
    image.style.height = `${height}px`;
    // Leaf grip remains exactly under the cursor while the body swings below.
    image.style.transformOrigin = `${anchorX}px ${anchorY}px`;
    layer.append(image);
    const dialog = original.closest("dialog[open]");
    const details = original.closest("details");
    const popover = typeof layer.showPopover === "function";
    if (popover) layer.setAttribute("popover", "manual");
    (dialog || document.body).append(layer);
    active = {
      original, layer, image, dialog, details, popover, pointerId: event.pointerId,
      originalVisibilityValue: original.style.getPropertyValue("visibility"),
      originalVisibilityPriority: original.style.getPropertyPriority("visibility"),
      x: event.clientX, y: event.clientY, startX: event.clientX, startY: event.clientY,
      anchorX, anchorY, moveX: event.clientX, moveY: event.clientY,
      drive: [0, 0, 0], angles: [0, 0, 0], last: performance.now(),
      started: performance.now(), lastMove: performance.now()
    };
    position(event.clientX, event.clientY);
    layer.hidden = false;
    if (popover) {
      try { layer.showPopover(); } catch (_) { stop(); return; }
    }
    // The source keeps its layout box but visually leaves its place while held.
    original.style.setProperty("visibility", "hidden");
    // Keep receiving movement/release outside the thumbnail and over controls.
    // Global listeners still work in browsers without this optional API.
    try { original.setPointerCapture?.(event.pointerId); } catch (_) { /* fallback */ }
    frame = requestAnimationFrame(animate);
  }

  for (const trigger of triggers) {
    const original = trigger.matches("img") ? trigger : trigger.querySelector("img");
    if (!original) continue;
    original.draggable = false;
    original.addEventListener("pointerdown", event => begin(event, original, trigger));
    original.addEventListener("lostpointercapture", event => {
      if (active?.original === original && event.pointerId === active.pointerId) stop();
    });
    original.addEventListener("dragstart", event => event.preventDefault());
    // Hover and keyboard focus still do nothing. Release does not click forms.
    trigger.addEventListener("click", event => { event.preventDefault(); event.stopPropagation(); });
  }
  window.addEventListener("pointermove", event => {
    if (!active || event.pointerId !== active.pointerId) return;
    if (!(event.buttons & 1)) { stop(); return; }
    position(event.clientX, event.clientY);
  }, true);
  window.addEventListener("pointerup", event => {
    if (active && event.pointerId === active.pointerId &&
        (event.button === 0 || !(event.buttons & 1))) stop();
  }, true);
  window.addEventListener("pointercancel", event => {
    if (active && event.pointerId === active.pointerId) stop();
  }, true);
  window.addEventListener("blur", stop);
  window.addEventListener("pagehide", stop);
  window.addEventListener("resize", stop);
  // Scrolling does not drop an active hold: viewport coordinates stay valid.
  document.addEventListener("visibilitychange", () => { if (document.hidden) stop(); });
  document.addEventListener("close", stop, true);
  document.addEventListener("keydown", event => { if (event.key === "Escape") stop(); }, true);
})();


/* HF14: opening installation details reveals the expanded content. */
(() => {
  const details = document.querySelector('#aboutDialog details[data-installation-reveal]');
  const dialog = document.getElementById('aboutDialog');
  if (!details || !dialog) return;
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
  let dispose = null;

  function cancel() { if (dispose) { const fn = dispose; dispose = null; fn(); } }
  details.addEventListener('toggle', () => {
    cancel();
    if (!details.open || !dialog.open) return;
    let firstFrame = 0, secondFrame = 0, timer = 0, observer = null;
    let previousHeight = -1, stopped = false;
    const scroll = () => {
      if (stopped || !details.open || !dialog.open) return;
      const height = dialog.scrollHeight;
      if (height === previousHeight) return;
      previousHeight = height;
      dialog.scrollTo({top: height, behavior: reduced.matches ? 'instant' : 'smooth'});
    };
    const schedule = () => {
      cancelAnimationFrame(firstFrame);
      cancelAnimationFrame(secondFrame);
      firstFrame = requestAnimationFrame(() => {
        secondFrame = requestAnimationFrame(scroll);
      });
    };
    const interrupt = event => {
      if (event.type !== 'keydown' ||
          ['ArrowUp','ArrowDown','PageUp','PageDown','Home','End','Escape',' '].includes(event.key)) {
        cancel();
      }
    };
    dispose = () => {
      stopped = true;
      cancelAnimationFrame(firstFrame); cancelAnimationFrame(secondFrame);
      clearTimeout(timer); observer?.disconnect();
      for (const type of ['wheel','touchstart','pointerdown','keydown']) {
        dialog.removeEventListener(type, interrupt, true);
      }
    };
    // Late rendering of environment values or the local image may grow the
    // section once. Track that short window, not every later DOM update.
    if (typeof ResizeObserver === 'function') {
      observer = new ResizeObserver(schedule);
      observer.observe(details);
    }
    for (const type of ['wheel','touchstart','pointerdown','keydown']) {
      dialog.addEventListener(type, interrupt, {capture: true, passive: true});
    }
    schedule();
    timer = setTimeout(cancel, 900);
  });
  dialog.addEventListener('close', cancel);
  window.addEventListener('pagehide', cancel);
})();
