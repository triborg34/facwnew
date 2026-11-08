/* FACW console - settings: engine tuning, access control, users, theme. */
(function () {
  "use strict";
  const { h, clear, toast, modal, loading, empty, errorState, store, applyTheme } = window.UI;
  const I = window.I18N;

  let root = null;
  let settings = null;
  let dirty = {};

  const SLIDERS = [
    ["match_threshold", "\u0622\u0633\u062A\u0627\u0646\u0647\u0654 \u062A\u0637\u0628\u06CC\u0642 \u0647\u0648\u06CC\u062A", 0.05, 0.99, 0.01, "0.00"],
    ["person_confidence", "\u0622\u0633\u062A\u0627\u0646\u0647\u0654 \u0627\u0637\u0645\u06CC\u0646\u0627\u0646 \u062A\u0634\u062E\u06CC\u0635 \u0627\u0634\u062E\u0627\u0635", 0.05, 0.99, 0.01, "0.00"],
    ["face_accept_threshold", "\u0622\u0633\u062A\u0627\u0646\u0647\u0654 \u067E\u0630\u06CC\u0631\u0634 \u0686\u0647\u0631\u0647", 0.1, 0.99, 0.01, "0.00"],
    ["ambiguity_margin", "\u062D\u0627\u0634\u06CC\u0647\u0654 \u0627\u0628\u0647\u0627\u0645", 0.0, 0.5, 0.005, "0.000"],
    ["min_face_score", "\u062D\u062F\u0627\u0642\u0644 \u0627\u0645\u062A\u06CC\u0627\u0632 \u0686\u0647\u0631\u0647", 0.05, 0.99, 0.01, "0.00"],
    ["overlap_threshold", "\u0622\u0633\u062A\u0627\u0646\u0647\u0654 \u0647\u0645\u200C\u067E\u0648\u0634\u0627\u0646\u06CC \u0631\u062F\u06CC\u0627\u0628", 0.05, 0.95, 0.01, "0.00"],
  ];
  const NUMBER_FIELDS = [
    ["min_face_width", "\u062D\u062F\u0627\u0642\u0644 \u0639\u0631\u0636 \u0686\u0647\u0631\u0647 (\u067E\u06CC\u06A9\u0633\u0644)", 16, 512],
    ["face_crop_padding", "\u062D\u0627\u0634\u06CC\u0647\u0654 \u0628\u0631\u0634 \u0686\u0647\u0631\u0647 (\u067E\u06CC\u06A9\u0633\u0644)", 0, 200],
    ["snapshot_quality", "\u06A9\u06CC\u0641\u06CC\u062A \u062A\u0635\u0648\u06CC\u0631 \u0630\u062E\u06CC\u0631\u0647", 30, 100],
    ["samples_required", "\u062A\u0639\u062F\u0627\u062F \u0646\u0645\u0648\u0646\u0647 \u0628\u0631\u0627\u06CC \u062A\u0635\u0645\u06CC\u0645", 1, 50],
    ["fast_sample_ms", "\u0641\u0627\u0635\u0644\u0647\u0654 \u0646\u0645\u0648\u0646\u0647\u200C\u0628\u0631\u062F\u0627\u0631\u06CC \u0633\u0631\u06CC\u0639 (ms)", 30, 5000],
    ["slow_sample_ms", "\u0641\u0627\u0635\u0644\u0647\u0654 \u0646\u0645\u0648\u0646\u0647\u200C\u0628\u0631\u062F\u0627\u0631\u06CC \u06A9\u0646\u062F (ms)", 60, 10000],
    ["dedup_window_seconds", "\u0628\u0627\u0632\u0647\u0654 \u062D\u0630\u0641 \u062A\u06A9\u0631\u0627\u0631 (\u062B\u0627\u0646\u06CC\u0647)", 0, 3600],
    ["track_ttl_seconds", "\u0645\u062F\u062A \u0628\u0627\u0642\u06CC \u0645\u0627\u0646\u062F\u0646 \u062A\u0631\u06A9 (\u062B\u0627\u0646\u06CC\u0647)", 1, 60],
    ["retention_days", "\u0646\u06AF\u0647\u062F\u0627\u0631\u06CC \u0631\u0648\u06CC\u062F\u0627\u062F (\u0631\u0648\u0632\u060C 0 = \u0647\u0645\u06CC\u0634\u0647)", 0, 3650],
  ];

  function mark(key, value) {
    dirty[key] = value;
  }

  function renderSettings() {
    const host = root.querySelector("#settings-host");
    clear(host);
    const s = settings;

    const slider = ([key, label, min, max, step, digits]) => {
      const val = dirty[key] !== undefined ? dirty[key] : s[key];
      const out = h("span", { class: "slider-value" }, I.toFa(Number(val).toFixed(digits)));
      return h("div", { class: "field slider" },
        h("label", {}, label),
        h("input", { type: "range", min, max, step, value: val,
          oninput: (ev) => { const v = Number(ev.target.value); out.textContent = I.toFa(v.toFixed(digits)); mark(key, v); } }),
        out);
    };

    const numField = ([key, label, min, max]) =>
      h("div", { class: "field" }, h("label", {}, label),
        h("input", { type: "number", min, max, value: dirty[key] !== undefined ? dirty[key] : s[key],
          onchange: (ev) => mark(key, ev.target.value === "" ? null : Number(ev.target.value)) }));

    const toggle = (key, label) =>
      h("label", { class: "switch" },
        h("input", { type: "checkbox", checked: (dirty[key] !== undefined ? dirty[key] : s[key]) ? true : null,
          onchange: (ev) => mark(key, ev.target.checked) }),
        h("span", { class: "track" }), h("span", {}, label));

    const insight = h("div", { class: "badge accent" }, "اعمال بدون راه‌اندازی مجدد");
    host.appendChild(h("div", { class: "grid-2" },
      // engine
      h("div", { class: "card" },
        h("div", { class: "card-head" }, "تنظیم موتور", h("div", { class: "spacer" }), insight),
        h("div", { class: "card-body" },
          SLIDERS.map(slider),
          h("div", { class: "row", style: "margin-top:10px" }, NUMBER_FIELDS.map(numField)),
          h("div", { class: "row", style: "margin-top:10px" },
            h("div", { class: "field" }, h("label", {}, "پردازش"), h("select", { onchange: (ev) => mark("processing_device", ev.target.value) },
              [["auto", "خودکار"], ["cpu", "پردازنده (CPU)"], ["gpu", "شتاب‌دهنده (GPU)"]]
                .map(([v, t]) => h("option", { value: v, selected: (dirty.processing_device || s.processing_device) === v ? true : null }, t)))),
            h("div", { class: "field" }, h("label", {}, "سطح گزارش"), h("select", { onchange: (ev) => mark("log_level", ev.target.value) },
              [["info", "info"], ["debug", "debug"], ["warning", "warning"], ["error", "error"]]
                .map(([v, t]) => h("option", { value: v, selected: (dirty.log_level || s.log_level) === v ? true : null }, t))))
          )
        )
      ),
      h("div", {},
        // access control
        h("div", { class: "card" },
          h("div", { class: "card-head" }, "کنترل دسترسی"),
          h("div", { class: "card-body" },
            h("div", { style: "display:flex;gap:18px;flex-wrap:wrap" },
              toggle("access_control", "فعال‌سازی رله"),
              toggle("zone_mode", "حالت ناحیه"),
              toggle("alarm_enabled", "هشدار صوتی"),
              toggle("report_lock", "قفل گزارش")),
            h("div", { class: "row", style: "margin-top:12px" },
              h("div", { class: "field" }, h("label", {}, "آدرس رله"),
                h("input", { type: "text", value: dirty.relay_host !== undefined ? dirty.relay_host : s.relay_host, onchange: (ev) => mark("relay_host", ev.target.value) })),
              h("div", { class: "field" }, h("label", {}, "پورت رله"),
                h("input", { type: "number", value: dirty.relay_port !== undefined ? dirty.relay_port : s.relay_port, onchange: (ev) => mark("relay_port", Number(ev.target.value)) })),
              h("div", { class: "field" }, h("label", {}, "طول پالس (ms)"),
                h("input", { type: "number", value: dirty.relay_pulse_ms !== undefined ? dirty.relay_pulse_ms : s.relay_pulse_ms, onchange: (ev) => mark("relay_pulse_ms", Number(ev.target.value)) }))),
            h("div", { class: "row", style: "margin-top:10px" },
              h("div", { class: "field" }, h("label", {}, "کانال ۱"),
                h("input", { type: "text", value: dirty.relay_channel_1 !== undefined ? dirty.relay_channel_1 : s.relay_channel_1, onchange: (ev) => mark("relay_channel_1", ev.target.value) })),
              h("div", { class: "field" }, h("label", {}, "کانال ۲ (خالی = تک‌کانال)"),
                h("input", { type: "text", value: dirty.relay_channel_2 !== undefined ? dirty.relay_channel_2 : s.relay_channel_2, onchange: (ev) => mark("relay_channel_2", ev.target.value) }))),
            h("div", { style: "display:flex;gap:8px;margin-top:12px" },
              h("button", { class: "btn sm", onclick: testRelay }, "تست اتصال"),
              h("button", { class: "btn sm", onclick: () => pulseRelay("") }, "پالس کانال‌های عمومی"))
          )
        ),
        // appearance + notification
        h("div", { class: "card", style: "margin-top:12px" },
          h("div", { class: "card-head" }, "نمایش و اطلاع‌رسانی"),
          h("div", { class: "card-body" },
            h("div", { class: "row" },
              h("div", { class: "field" }, h("label", {}, "پوسته"),
                h("button", { class: "btn", onclick: () => {
                  const next = store.state.theme === "dark" ? "light" : "dark";
                  applyTheme(next);
                  toast(next === "dark" ? "پوستهٔ تیره" : "پوستهٔ روشن", "ok");
                  renderSettings();
                } }, store.state.theme === "dark" ? "\u{1F319} تیره \u2014 تغییر به روشن" : "\u2600\uFE0F روشن \u2014 تغییر به تیره")),
              h("div", { class: "field" }, h("label", {}, "گیرندهٔ اطلاع‌رسانی"),
                h("input", { type: "email", value: dirty.notify_recipient !== undefined ? dirty.notify_recipient : s.notify_recipient, onchange: (ev) => mark("notify_recipient", ev.target.value) })),
              h("div", { class: "field" }, h("label", {}, "روش ارسال"),
                h("select", { onchange: (ev) => mark("notify_method", ev.target.value) },
                  [["log", "ثبت در گزارش"], ["smtp", "ایمیل (SMTP)"]]
                    .map(([v, t]) => h("option", { value: v, selected: (dirty.notify_method || s.notify_method) === v ? true : null }, t))))
            )
          )
        )
      )
    ));

    if (Object.keys(dirty).length) {
      host.appendChild(h("div", { class: "card", style: "margin-top:12px" },
        h("div", { class: "card-body", style: "display:flex;gap:10px;align-items:center" },
          h("span", { class: "badge warn" }, I.num(Object.keys(dirty).length) + " تغییر ذخیره‌نشده"),
          h("div", { style: "flex:1" }),
          h("button", { class: "btn sm ghost", onclick: () => { dirty = {}; renderSettings(); } }, "لغو"),
          h("button", { class: "btn sm primary", onclick: saveSettings }, "ذخیره و اعمال")
        )));
    }
  }

  async function saveSettings() {
    try {
      const r = await window.API.put("/api/settings", dirty);
      settings = r.settings;
      dirty = {};
      store.set({ settings });
      toast("تنظیمات اعمال شد", "ok");
      renderSettings();
    } catch (e) { toast("خطا: " + e.message, "danger"); }
  }

  async function testRelay() {
    try {
      const r = await window.API.post("/api/settings/relay-test", {});
      toast(r.ok ? `اتصال برقرار است (${r.host || ""}:${r.port || ""})` : `اتصال برقرار نشد: ${r.detail}`, r.ok ? "ok" : "warn");
    } catch (e) { toast("خطا: " + e.message, "danger"); }
  }

  async function pulseRelay(channel) {
    try {
      const r = await window.API.post("/api/settings/relay-pulse", { channel });
      const all = (r.results || []).map((x) => `${x.channel}: ${x.ok ? "OK" : "FAIL"}`).join(" \u00B7 ");
      toast(all || "هیچ کانالی پیکربندی نشده", (r.results || []).every((x) => x.ok) ? "ok" : "warn");
    } catch (e) { toast("خطا: " + e.message, "danger"); }
  }

  // ------------------------------------------------------------------ users
  async function renderUsers() {
    const host = root.querySelector("#users-host");
    if (!host) return;
    clear(host);
    host.appendChild(loading());
    try {
      const res = await window.API.get("/api/users");
      clear(host);
      host.appendChild(h("div", { class: "card-head" }, "کاربران",
        h("div", { class: "spacer" }),
        h("button", { class: "btn sm primary", onclick: () => openUserForm(null) }, "+ کاربر جدید")));
      host.appendChild(h("div", { class: "table-wrap" }, h("table", { class: "data" },
        h("thead", {}, h("tr", {}, h("th", {}, "نام"), h("th", {}, "نام کاربری"), h("th", {}, "ایمیل"), h("th", {}, "نقش"), h("th", {}, "عملیات"))),
        h("tbody", {}, res.items.map((u) => h("tr", {},
          h("td", {}, u.display_name),
          h("td", { class: "mono" }, u.login_name),
          h("td", {}, u.email || "\u2014"),
          h("td", {}, h("span", { class: "badge " + (u.role === "admin" ? "accent" : "") }, u.role === "admin" ? "مدیر" : "ناظر")),
          h("td", {},
            h("button", { class: "btn sm ghost", onclick: () => openUserForm(u) }, "\u270E"),
            h("button", { class: "btn sm ghost", onclick: () => removeUser(u) }, "\u{1F5D1}"))
        )))
      )));
    } catch (e) {
      clear(host);
      host.appendChild(errorState(e.message, renderUsers));
    }
  }

  function openUserForm(user) {
    const f = {
      display_name: h("input", { type: "text", value: user ? user.display_name : "" }),
      login_name: h("input", { type: "text", value: user ? user.login_name : "", disabled: user ? true : null }),
      email: h("input", { type: "email", value: user ? user.email : "" }),
      password: h("input", { type: "password", placeholder: user ? "برای تغییر رمز پر کنید" : "حداقل ۶ نویسه" }),
      role: h("select", {}, [["observer", "ناظر"], ["admin", "مدیر"]].map(([v, t]) => h("option", { value: v, selected: user && user.role === v ? true : null }, t))),
    };
    const err = h("div", { class: "state error", style: "display:none" });
    modal({
      title: user ? "ویرایش کاربر" : "کاربر جدید",
      size: "sm",
      body: h("div", {},
        h("div", { class: "field" }, h("label", {}, "نام نمایشی"), f.display_name),
        h("div", { class: "field", style: "margin-top:8px" }, h("label", {}, "نام کاربری"), f.login_name),
        h("div", { class: "field", style: "margin-top:8px" }, h("label", {}, "ایمیل"), f.email),
        h("div", { class: "field", style: "margin-top:8px" }, h("label", {}, "رمز عبور"), f.password),
        h("div", { class: "field", style: "margin-top:8px" }, h("label", {}, "نقش"), f.role),
        err),
      actions: [
        { label: "انصراف", kind: "ghost", onClick: (c) => c() },
        { label: "ذخیره", kind: "primary", onClick: async (c) => {
            const payload = {
              display_name: f.display_name.value.trim(),
              email: f.email.value.trim(),
              role: f.role.value,
            };
            if (f.password.value) payload.password = f.password.value;
            try {
              if (user) await window.API.put("/api/users/" + user.id, payload);
              else {
                payload.login_name = f.login_name.value.trim();
                if (!payload.password) throw new Error("رمز عبور الزامی است");
                await window.API.post("/api/users", payload);
              }
              toast("ذخیره شد", "ok");
              c(); renderUsers();
            } catch (e) { err.style.display = ""; err.textContent = e.message; }
          } },
      ],
    });
  }

  async function removeUser(u) {
    const ok = await window.UI.confirmDialog({
      title: "حذف کاربر", danger: true, confirmLabel: "حذف",
      message: `کاربر «${u.display_name}» حذف شود؟ نشست‌های فعال او نیز بسته می‌شود.`,
    });
    if (!ok) return;
    try { await window.API.del("/api/users/" + u.id); toast("حذف شد", "ok"); renderUsers(); }
    catch (e) { toast("خطا: " + e.message, "danger"); }
  }

  async function mount(host) {
    root = host;
    clear(root);
    const isAdmin = store.state.user && store.state.user.role === "admin";
    root.appendChild(h("div", { class: "page-head" },
      h("h1", {}, "تنظیمات"),
      h("span", { class: "sub" }, isAdmin ? "پارامترهای موتور، کنترل دسترسی و کاربران" : "تنظیمات فقط خواندنی برای ناظر")
    ));
    try {
      const r = await window.API.get("/api/settings");
      settings = r.settings;
      store.set({ settings });
    } catch (e) {
      root.appendChild(errorState(e.message));
      return;
    }
    const hostEl = h("div", { id: "settings-host" });
    root.appendChild(hostEl);
    root.appendChild(h("div", { id: "accel-host" }));
    renderAccelerator();
    if (isAdmin) { renderSettings(); }
    else {
      hostEl.appendChild(h("div", { class: "card" }, h("div", { class: "card-body" },
        h("div", { class: "state" }, "تغییر تنظیمات فقط برای مدیر مجاز است."),
        h("table", { class: "data" }, h("tbody", {}, Object.keys(settings).slice(0, 20).map((k) =>
          h("tr", {}, h("td", { class: "muted" }, k), h("td", { class: "mono" }, String(settings[k])))))))));
    }
    if (isAdmin) {
      const users = h("div", { class: "card", id: "users-host", style: "margin-top:12px" });
      root.appendChild(users);
      renderUsers();
    }
  }

  // ---- accelerator status ------------------------------------------------
  // The "processing device" selector says what was requested; this card says
  // what is actually running, so "is the GPU in use?" is answerable without
  // reading logs or guessing from a settings value.
  async function renderAccelerator() {
    const host = root && root.querySelector("#accel-host");
    if (!host) return;
    let accel = null;
    try {
      const h0 = await window.API.get("/api/health/full");
      accel = h0.accelerator || null;
    } catch (e) { /* status card is optional */ }
    clear(host);
    if (!accel) return;
    const label = { cuda: "CUDA (GPU)", opencl: "OpenCL (GPU)", cpu: "پردازنده (CPU)" };
    const row = (k, v) => h("tr", {}, h("td", { class: "muted" }, k), h("td", {}, v));
    host.appendChild(h("div", { class: "card", style: "margin-top:12px" },
      h("div", { class: "card-head" },
        h("span", {}, "شتاب‌دهنده پردازش"),
        h("div", { class: "spacer" }),
        h("span", { class: "badge " + (accel.using_gpu ? "ok" : "warn") },
          accel.using_gpu ? "GPU فعال" : "اجرا روی CPU")),
      h("div", { class: "card-body" },
        h("table", { class: "data" }, h("tbody", {},
          row("درخواست‌شده", label[accel.requested] || accel.requested || "—"),
          row("در حال اجرا", label[accel.device] || accel.device || "—"),
          row("تشخیص چهره", accel.face_backend === "onnxruntime" ? "ONNX Runtime (GPU)" : "OpenCV"),
          row("تشخیص بدن", accel.person_backend || "—"),
          row("دستگاه‌های CUDA", I.num(accel.cuda_devices || 0)),
          row("دلیل", accel.reason || "—"))),
        accel.using_gpu ? null : h("div", { class: "muted", style: "font-size:12px;margin-top:8px" },
          "برای استفاده از GPU به کارت گرافیک NVIDIA با درایور CUDA و بستهٔ onnxruntime-gpu نیاز است. " +
          "بدون آن، سامانه روی CPU کار می‌کند و همهٔ امکانات در دسترس است."))));
  }

  window.PageSettings = { mount, renderAccelerator };
})();
