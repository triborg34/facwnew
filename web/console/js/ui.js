/* FACW console - shared UI helpers: DOM, modals, toasts, client state. */
(function () {
  "use strict";

  const h = (tag, attrs, ...children) => {
    const el = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (v === null || v === undefined || v === false) return;
      if (k === "class") el.className = v;
      else if (k === "html") el.innerHTML = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else if (v === true) el.setAttribute(k, "");
      else el.setAttribute(k, v);
    });
    (children || []).flat().forEach((c) => {
      if (c === null || c === undefined || c === false) return;
      el.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
    });
    return el;
  };

  const clear = (node) => { while (node && node.firstChild) node.removeChild(node.firstChild); };

  // ---- toasts --------------------------------------------------------
  function toast(message, kind) {
    let host = document.querySelector(".toasts");
    if (!host) {
      host = h("div", { class: "toasts" });
      document.body.appendChild(host);
    }
    const el = h("div", { class: "toast " + (kind || "") }, message);
    host.appendChild(el);
    setTimeout(() => {
      el.style.opacity = "0";
      el.style.transition = "opacity 200ms";
      setTimeout(() => el.remove(), 220);
    }, kind === "danger" ? 6000 : 3600);
  }

  // ---- modal ---------------------------------------------------------
  // Tracks open modals so Escape closes only the top one and focus is restored.
  const openModals = [];
  function modal(opts) {
    const body = h("div", { class: "modal-body" });
    if (typeof opts.body === "string") body.innerHTML = opts.body;
    else if (opts.body) body.appendChild(opts.body);

    const foot = h("div", { class: "modal-foot" });
    const modalEl = h("div", { class: "modal " + (opts.size || ""), role: "dialog", "aria-modal": "true" },
      h("div", { class: "modal-head" },
        h("h2", {}, opts.title || ""),
        h("div", { class: "spacer" }),
        h("button", { class: "btn ghost icon", "aria-label": "close", onclick: () => close() }, "\u2715")
      ),
      body,
      opts.footer === null ? null : foot
    );
    const backdrop = h("div", { class: "modal-backdrop" }, modalEl);
    const previous = document.activeElement;

    (opts.actions || []).forEach((a) => {
      foot.appendChild(h("button", { class: "btn " + (a.kind || ""), onclick: () => a.onClick && a.onClick(close) }, a.label));
    });
    if (opts.footerExtra) foot.appendChild(opts.footerExtra);
    if ((opts.actions || []).length) foot.appendChild(h("div", { class: "spacer" }));

    function close() {
      const i = openModals.indexOf(entry);
      if (i >= 0) openModals.splice(i, 1);
      backdrop.remove();
      document.removeEventListener("keydown", onKey);
      if (previous && previous.focus) previous.focus();
    }
    function onKey(ev) {
      if (ev.key === "Escape" && openModals[openModals.length - 1] === entry) {
        ev.stopPropagation();
        close();
      }
    }
    const entry = { close, el: modalEl };
    openModals.push(entry);
    document.addEventListener("keydown", onKey);
    document.body.appendChild(backdrop);
    return { close, body, modal: modalEl, footer: foot };
  }

  function confirmDialog(opts) {
    return new Promise((resolve) => {
      let done = false;
      const m = modal({
        title: opts.title || "\u062A\u0623\u06CC\u06CC\u062F",
        size: "sm",
        body: h("div", {}, opts.message || ""),
        actions: [
          { label: opts.cancelLabel || "\u0627\u0646\u0635\u0631\u0627\u0641", kind: "ghost", onClick: (c) => { done = true; c(); resolve(false); } },
          { label: opts.confirmLabel || "\u062A\u0623\u06CC\u06CC\u062F", kind: opts.danger ? "danger" : "primary", onClick: (c) => { done = true; c(); resolve(true); } },
        ],
      });
      const obs = new MutationObserver(() => {
        if (!document.body.contains(m.modal) && !done) { obs.disconnect(); resolve(false); }
      });
      obs.observe(document.body, { childList: true });
    });
  }

  // ---- states --------------------------------------------------------
  const loading = (label) => h("div", { class: "state" }, h("div", { class: "spinner" }), h("div", { class: "muted" }, label || "\u062F\u0631 \u062D\u0627\u0644 \u0628\u0627\u0631\u06AF\u0630\u0627\u0631\u06CC\u2026"));
  const empty = (label, sub) => h("div", { class: "state" }, h("div", { class: "icon" }, "\u{1F4ED}"), h("div", {}, label || "\u0645\u0648\u0631\u062F\u06CC \u06CC\u0627\u0641\u062A \u0646\u0634\u062F"), sub ? h("div", { class: "muted" }, sub) : null);
  const errorState = (msg, retry) => h("div", { class: "state error" }, h("div", { class: "icon" }, "\u26A0"), h("div", {}, msg || "\u062E\u0637\u0627"), retry ? h("button", { class: "btn sm", onclick: retry }, "\u062A\u0644\u0627\u0634 \u0645\u062C\u062F\u062F") : null);

  // ---- client state --------------------------------------------------
  const store = {
    state: {
      user: null,
      theme: localStorage.getItem("facw.theme") || "light",
      cameras: [],
      streamToken: "",
      page: "dashboard",
      detPanel: { identified: true, unknown: true },
    },
    listeners: [],
    set(patch) {
      Object.assign(this.state, patch);
      this.listeners.forEach((fn) => { try { fn(this.state); } catch (e) { /* ignore */ } });
    },
    subscribe(fn) { this.listeners.push(fn); return () => { this.listeners = this.listeners.filter((f) => f !== fn); }; },
  };

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    store.state.theme = theme;
    localStorage.setItem("facw.theme", theme);
  }
  applyTheme(store.state.theme);

  // ---- misc ----------------------------------------------------------
  const debounce = (fn, ms) => {
    let t;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms || 250); };
  };

  function alertChime() {
    // Bundled asset (never a remote URL).
    try {
      const audio = new Audio("/assets/alarm.wav");
      audio.volume = 0.55;
      const p = audio.play();
      if (p && p.catch) p.catch(() => { /* autoplay blocked until interaction */ });
    } catch (e) { /* ignore */ }
  }

  window.UI = { h, clear, toast, modal, confirmDialog, loading, empty, errorState, store, applyTheme, debounce, alertChime };
})();
