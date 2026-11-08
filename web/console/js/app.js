/* FACW console - application shell: auth, routing, live updates. */
(function () {
  "use strict";
  const { h, clear, toast, loading, store } = window.UI;

  const PAGES = [
    { id: "dashboard", label: "\u0645\u0627\u0646\u06CC\u062A\u0648\u0631\u06CC\u0646\u06AF", mount: () => window.PageDashboard.mount, unmount: () => window.PageDashboard.unmount, adminOnly: false },
    { id: "reports", label: "\u06AF\u0632\u0627\u0631\u0634\u200C\u0647\u0627", mount: () => window.PageReports.mount, adminOnly: false },
    { id: "cameras", label: "\u062F\u0648\u0631\u0628\u06CC\u0646\u200C\u0647\u0627", mount: () => window.PageCameras.mount, adminOnly: true },
    { id: "people", label: "\u0627\u0634\u062E\u0627\u0635", mount: () => window.PagePeople.mount, adminOnly: false },
    { id: "settings", label: "\u062A\u0646\u0637\u06CC\u0645\u0627\u062A", mount: () => window.PageSettings.mount, adminOnly: false },
  ];

  let current = null;
  let currentPageId = null;
  let es = null;

  function signInScreen() {
    const rootEl = document.getElementById("app");
    clear(rootEl);
    const err = h("div", { class: "state error", style: "display:none" });
    const login = h("input", { type: "text", autocomplete: "username", placeholder: "\u0646\u0627\u0645 \u06A9\u0627\u0631\u0628\u0631\u06CC" });
    const pass = h("input", { type: "password", autocomplete: "current-password", placeholder: "\u0631\u0645\u0632 \u0639\u0628\u0648\u0631" });
    const submit = async (ev) => {
      if (ev) ev.preventDefault();
      err.style.display = "none";
      const btnEl = rootEl.querySelector("#signin-btn");
      btnEl.disabled = true;
      try {
        const r = await window.API.login(login.value.trim(), pass.value);
        if (r && r.user) {
          store.set({ user: r.user });
          await boot();
        } else {
          err.style.display = ""; err.textContent = "\u0646\u0627\u0645 \u06A9\u0627\u0631\u0628\u0631\u06CC \u06CC\u0627 \u0631\u0645\u0632 \u0646\u0627\u062F\u0631\u0633\u062A \u0627\u0633\u062A";
        }
      } catch (e) {
        err.style.display = "";
        err.textContent = e.status === 429 ? "\u062A\u0639\u062F\u0627\u062F \u062A\u0644\u0627\u0634\u200C\u0647\u0627 \u0632\u06CC\u0627\u062F \u0628\u0648\u062F\u0647\u061B \u06A9\u0645\u06CC \u0628\u0639\u062F \u062F\u0648\u0628\u0627\u0631\u0647 \u0627\u0645\u062A\u062D\u0627\u0646 \u06A9\u0646\u06CC\u062F" : (e.message || "\u0648\u0631\u0648\u062F \u0646\u0627\u0645\u0648\u0641\u0642");
      } finally {
        btnEl.disabled = false;
      }
    };
    const form = h("form", { onsubmit: submit },
      h("div", { class: "card", style: "width:min(420px,100%);margin:8vh auto 0" },
        h("div", { class: "card-body" },
          h("div", { style: "text-align:center;margin-bottom:14px" },
            h("div", { class: "brand-mark", style: "margin:0 auto 10px;width:44px;height:44px;font-size:20px" }, "\u25C9"),
            h("h1", { style: "margin:0;font-size:19px" }, "FACW"),
            h("div", { class: "muted", style: "font-size:12.5px" }, "\u0633\u06CC\u0633\u062A\u0645 \u0634\u0646\u0627\u0633\u0627\u06CC\u06CC \u0686\u0647\u0631\u0647 \u0648 \u06A9\u0646\u062A\u0631\u0644 \u062F\u0633\u062A\u0631\u0633\u06CC")),
          h("div", { class: "field" }, h("label", {}, "\u0646\u0627\u0645 \u06A9\u0627\u0631\u0628\u0631\u06CC"), login),
          h("div", { class: "field", style: "margin-top:10px" }, h("label", {}, "\u0631\u0645\u0632 \u0639\u0628\u0648\u0631"), pass),
          h("div", { style: "margin-top:14px" },
            h("button", { id: "signin-btn", class: "btn primary", type: "submit", style: "width:100%" }, "\u0648\u0631\u0648\u062F")),
          err,
          h("div", { class: "muted", style: "font-size:11.5px;margin-top:12px;text-align:center" },
            "\u062f\u0633\u062a\u0631\u0633\u06cc \u0628\u0631 \u0627\u0633\u0627\u0633 \u0646\u0642\u0634: \u0645\u062f\u06CC\u0631 / \u0646\u0627\u0638\u0631")
        ))
    );
    rootEl.appendChild(form);
    login.focus();
  }

  function shell() {
    const rootEl = document.getElementById("app");
    clear(rootEl);
    const user = store.state.user;
    const nav = h("div", { class: "topnav" });
    PAGES.filter((p) => !p.adminOnly || (user && user.role === "admin")).forEach((p) => {
      nav.appendChild(h("button", {
        class: currentPageId === p.id ? "active" : "",
        onclick: () => navigate(p.id),
      }, p.label));
    });

    const themeBtn = h("button", { class: "btn ghost sm", title: "\u067E\u0648\u0633\u062A\u0647", onclick: () => {
      const next = store.state.theme === "dark" ? "light" : "dark";
      window.UI.applyTheme(next);
      themeBtn.textContent = next === "dark" ? "\u{1F319}" : "\u2600\uFE0F";
    } }, store.state.theme === "dark" ? "\u{1F319}" : "\u2600\uFE0F");

    const alarmBadge = h("span", { class: "badge", id: "alarm-badge", style: "display:none" }, "");

    rootEl.appendChild(h("div", { class: "topbar" },
      h("div", { class: "brand" },
        h("div", { class: "brand-mark" }, "\u25C9"),
        h("span", { class: "text" }, "FACW")),
      nav,
      h("div", { class: "topbar-spacer" }),
      alarmBadge,
      themeBtn,
      h("div", { class: "topbar-user" },
        h("span", { class: "name" }, (user && user.display_name) || ""),
        h("span", { class: "badge " + (user && user.role === "admin" ? "accent" : "") }, user && user.role === "admin" ? "\u0645\u062F\u06CC\u0631" : "\u0646\u0627\u0638\u0631"),
        h("button", { class: "btn sm ghost", onclick: signOut }, "\u062E\u0631\u0648\u062C"))
    ));
    const host = h("div", { class: "page", id: "page-host" });
    rootEl.appendChild(host);
    return host;
  }

  async function navigate(id) {
    const page = PAGES.find((p) => p.id === id) || PAGES[0];
    const user = store.state.user;
    if (page.adminOnly && (!user || user.role !== "admin")) {
      toast("\u062F\u0633\u062A\u0631\u0633\u06CC \u0628\u0647 \u0627\u06CC\u0646 \u0628\u062E\u0634 \u0641\u0642\u0637 \u0628\u0631\u0627\u06CC \u0645\u062F\u06CC\u0631 \u0645\u062C\u0627\u0632 \u0627\u0633\u062A", "warn");
      return;
    }
    if (currentPageId === id && current) return;
    // tear down the previous page so subscriptions/streams are released
    if (current && current.unmount) { try { current.unmount(); } catch (e) { /* ignore */ } }
    currentPageId = id;
    store.set({ page: id });
    const host = shell();
    const mod = page.mount();
    current = mod ? { mount: mod, unmount: PAGES.find((p) => p.id === id).unmount ? PAGES.find((p) => p.id === id).unmount() : null } : null;
    const target = host.appendChild(h("div", {}));
    if (mod) await mod(target);
  }

  async function signOut() {
    try { await window.API.logout(); } catch (e) { /* ignore */ }
    if (es) { es.close(); es = null; }
    if (window.PageDashboard && window.PageDashboard.unmount) window.PageDashboard.unmount();
    store.set({ user: null });
    signInScreen();
  }

  function startLive() {
    if (es) return;
    try {
      es = new EventSource("/api/events/stream");
      es.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          window.dispatchEvent(new CustomEvent("facw:change", { detail: msg }));
          if (msg.topic === "alarm") {
            const badge = document.getElementById("alarm-badge");
            if (badge) {
              badge.style.display = "";
              badge.className = "badge warn";
              badge.textContent = "\u0647\u0634\u062F\u0627\u0631: " + (msg.data.identity || "");
              setTimeout(() => { badge.style.display = "none"; }, 8000);
            }
            window.dispatchEvent(new CustomEvent("facw:alarm", { detail: msg.data }));
          }
        } catch (e) { /* ignore malformed frame */ }
      };
      es.onerror = () => {
        // browser retries automatically; a signed-out session stops it
        if (es && es.readyState === EventSource.CLOSED) { es = null; }
      };
    } catch (e) {
      toast("\u0627\u062A\u0635\u0627\u0644 \u0632\u0646\u062F\u0647 \u0628\u0631\u0642\u0631\u0627\u0631 \u0646\u0634\u062F", "warn");
    }
  }

  async function boot() {
    startLive();
    await navigate("dashboard");
  }

  window.addEventListener("facw:signedout", () => {
    store.set({ user: null });
    signInScreen();
  });

  // initial entry
  (async () => {
    const rootEl = document.getElementById("app");
    rootEl.appendChild(loading("\u062F\u0631 \u062D\u0627\u0644 \u0628\u0627\u0631\u06AF\u0630\u0627\u0631\u06CC\u2026"));
    try {
      const me = await window.API.me();
      if (me && me.user) {
        store.set({ user: me.user, streamToken: me.stream_token });
        await boot();
      } else {
        signInScreen();
      }
    } catch (e) {
      signInScreen();
    }
  })();
})();
