/* FACW console - reports: filters, pagination, export, row actions. */
(function () {
  "use strict";
  const { h, clear, toast, modal, loading, empty, errorState, store, debounce } = window.UI;
  const I = window.I18N;
  const L = window.LABELS;

  const state = {
    filters: { identified_only: "" },
    page: 1,
    page_size: 30,
    sort: "created_ts",
    order: "desc",
    data: null,
    loading: false,
  };

  let root = null;

  const roleBadge = (role) => {
    const cls = role === "approve" ? "ok" : role === "deny" ? "danger" : role === "visitor" ? "warn" : "accent";
    return h("span", { class: "badge " + cls }, L[role] || role || L.notset);
  };

  function filterBar(reload) {
    const f = state.filters;
    const dateFrom = window.DatePicker({ value: f.date_from || "", onChange: (v) => { f.date_from = v; state.page = 1; reload(); } });
    const dateTo = window.DatePicker({ value: f.date_to || "", onChange: (v) => { f.date_to = v; state.page = 1; reload(); } });

    const text = (key, label, ph) =>
      h("div", { class: "field" }, h("label", {}, label),
        h("input", { type: "search", placeholder: ph || "", value: f[key] || "",
          oninput: debounce((ev) => { f[key] = ev.target.value; state.page = 1; reload(); }, 320) }));

    const sel = (key, label, options) =>
      h("div", { class: "field" }, h("label", {}, label),
        h("select", { onchange: (ev) => { f[key] = ev.target.value; state.page = 1; reload(); } },
          options.map(([v, t]) => h("option", { value: v, selected: (f[key] || "") === v ? true : null }, t))));

    return h("div", { class: "card" },
      h("div", { class: "card-head" }, "فیلترها",
        h("div", { class: "spacer" }),
        h("button", { class: "btn sm ghost", onclick: () => {
          state.filters = { identified_only: "" }; state.page = 1; mount(root);
        } }, "پاک‌کردن فیلترها")),
      h("div", { class: "card-body" },
        h("div", { class: "row", style: "margin-bottom:10px" },
          text("name", "نام / شماره ملی"),
          text("national_number", "شماره ملی"),
          sel("gender", "جنسیت", [["", "همه"], ["male", "مرد"], ["female", "زن"], ["unknown", "نامشخص"]]),
          sel("role", "وضعیت مجوز", [["", "همه"], ["approve", "مجاز"], ["deny", "غیرمجاز"], ["visitor", "مهمان"], ["colleague", "همکار"]])
        ),
        h("div", { class: "row", style: "margin-bottom:10px" },
          h("div", { class: "field" }, h("label", {}, "از سن (شامل)"),
            h("input", { type: "number", value: f.age_min || "", min: 0, oninput: debounce((ev) => { f.age_min = ev.target.value; state.page = 1; reload(); }, 320) })),
          h("div", { class: "field" }, h("label", {}, "تا سن (غیرشامل)"),
            h("input", { type: "number", value: f.age_max || "", min: 0, oninput: debounce((ev) => { f.age_max = ev.target.value; state.page = 1; reload(); }, 320) })),
          h("div", { class: "field" }, h("label", {}, "از تاریخ"), dateFrom.el),
          h("div", { class: "field" }, h("label", {}, "تا تاریخ"), dateTo.el)
        ),
        h("div", { class: "row" },
          h("div", { class: "field" }, h("label", {}, "از ساعت"),
            h("input", { type: "time", value: f.time_from || "", onchange: (ev) => { f.time_from = ev.target.value; state.page = 1; reload(); } })),
          h("div", { class: "field" }, h("label", {}, "تا ساعت"),
            h("input", { type: "time", value: f.time_to || "", onchange: (ev) => { f.time_to = ev.target.value; state.page = 1; reload(); } })),
          h("div", { class: "field" }, h("label", {}, "فقط شناسایی‌شده"),
            h("label", { class: "switch", style: "margin-top:6px" },
              h("input", { type: "checkbox", checked: f.identified_only === "1" ? true : null,
                onchange: (ev) => { f.identified_only = ev.target.checked ? "1" : ""; state.page = 1; reload(); } }),
              h("span", { class: "track" }), h("span", {}, "فقط اشخاص شناخته‌شده"))),
          h("div", { class: "field" }, h("label", {}, "جست‌وجو با تصویر"),
            // Hidden native input behind a Persian button: the browser's built-in
            // file control renders English text and breaks the RTL interface.
            (() => {
              const input = h("input", { type: "file", accept: "image/*", style: "display:none",
                onchange: (ev) => {
                  const file = ev.target.files && ev.target.files[0];
                  if (!file) return;
                  imageSearch(file, reload);
                  ev.target.value = "";
                } });
              const btn = h("button", { type: "button", class: "btn sm",
                onclick: () => input.click() }, "\u{1F4F7} جست‌وجو با تصویر");
              return h("div", { style: "display:flex;gap:6px;align-items:center" }, btn, input);
            })())
        )
      )
    );
  }

  async function imageSearch(file, reload) {
    const fd = new FormData();
    fd.append("file", file);
    try {
      const res = await window.API.form("/api/events/search-image", fd);
      const ids = (res.matches || []).map((m) => m.event_id);
      if (!ids.length) { toast("تصویر مشابهی یافت نشد", "warn"); return; }
      state.filters.event_ids = ids;
      state.page = 1;
      toast(`تعداد ${I.num(ids.length)} رویداد مشابه یافت شد`, "ok");
      reload();
    } catch (e) {
      toast("جست‌وجو ناموفق: " + e.message, "danger");
    }
  }

  function params() {
    const p = Object.assign({}, state.filters, {
      page: state.page, page_size: state.page_size, sort: state.sort, order: state.order,
    });
    if (p.event_ids) p.event_ids = undefined; // sent separately below
    return p;
  }

  async function load() {
    state.loading = true;
    renderTable();
    try {
      let res;
      if (state.filters.event_ids && state.filters.event_ids.length) {
        // image search: fetch each hit set through the ids filter
        const all = [];
        const chunk = state.filters.event_ids.slice(0, 40);
        for (const id of chunk) {
          try { const r = await window.API.get("/api/events/" + id); all.push(r.event); } catch (e) { /* skip */ }
        }
        res = { items: all, total: all.length, page: 1, pages: 1 };
      } else {
        res = await window.API.get("/api/events", params());
      }
      state.data = res;
    } catch (e) {
      state.data = { error: e.message };
    } finally {
      state.loading = false;
      renderTable();
    }
  }

  function sortHeader(key, label) {
    const active = state.sort === key;
    return h("th", {
      style: "cursor:pointer; white-space:nowrap",
      onclick: () => {
        if (active) state.order = state.order === "desc" ? "asc" : "desc";
        else { state.sort = key; state.order = "desc"; }
        load();
      },
    }, label + (active ? (state.order === "desc" ? " \u25BC" : " \u25B2") : ""));
  }

  let tableHost = null;

  function renderTable() {
    if (!tableHost) return;
    clear(tableHost);
    if (state.loading) { tableHost.appendChild(loading()); return; }
    const d = state.data;
    if (!d) { tableHost.appendChild(empty("نتیجه‌ای برای نمایش نیست")); return; }
    if (d.error) { tableHost.appendChild(errorState(d.error, load)); return; }
    if (!d.items || !d.items.length) { tableHost.appendChild(empty("هیچ رویدادی با این فیلترها یافت نشد")); return; }

    const isAdmin = store.state.user && store.state.user.role === "admin";
    const rows = d.items.map((ev) => h("tr", {},
      h("td", {}, window.API.mediaUrl(ev.frame_file)
        ? h("img", { class: "thumb", src: window.API.mediaUrl(ev.frame_file), alt: "", loading: "lazy", style: "cursor:pointer",
            onclick: () => openEvent(ev) })
        : h("div", { class: "thumb skeleton" }, "")),
      h("td", {}, h("b", {}, ev.identity_name === "unknown" ? L.unknown : ev.identity_name)),
      h("td", { class: "mono" }, I.pct(ev.score, 1)),
      h("td", {}, L[ev.gender] || L.notset),
      h("td", { class: "mono" }, ev.age === null || ev.age === undefined ? "\u2014" : I.num(ev.age)),
      h("td", {}, roleBadge(ev.role)),
      h("td", { class: "mono" }, ev.national_number ? I.num(ev.national_number) : "\u2014"),
      h("td", {}, ev.camera_source),
      h("td", { class: "mono" }, I.formatDate(ev.event_date)),
      h("td", { class: "mono" }, I.formatTime(ev.event_time)),
      h("td", { style: "white-space:nowrap" },
        h("button", { class: "btn sm ghost", title: "نمایش", onclick: () => openEvent(ev) }, "\u{1F441}"),
        h("a", { class: "btn sm ghost", title: "دانلود", href: "/api/events/" + ev.id + "/download" }, "\u2B07"),
        isAdmin ? h("button", { class: "btn sm ghost", title: "حذف", onclick: () => removeEvent(ev) }, "\u{1F5D1}") : null
      )
    ));

    tableHost.appendChild(h("div", { class: "table-wrap" },
      h("table", { class: "data" },
        h("thead", {}, h("tr", {},
          h("th", {}, "تصویر"), sortHeader("identity_name", "نام"), sortHeader("score", "شباهت"),
          h("th", {}, "جنسیت"), h("th", {}, "سن"), h("th", {}, "وضعیت"),
          h("th", {}, "شماره ملی"), h("th", {}, "دوربین"),
          sortHeader("event_date", "تاریخ"), sortHeader("event_time", "ساعت"), h("th", {}, "عملیات")
        )),
        h("tbody", {}, rows)
      )
    ));

    const pages = d.pages || 1;
    tableHost.appendChild(h("div", { class: "pager" },
      h("span", { class: "pages" }, `صفحه ${I.num(d.page || 1)} از ${I.num(pages)} \u00B7 ${I.num(d.total || 0)} مورد`),
      h("div", { class: "spacer" }),
      h("button", { class: "btn sm", disabled: (d.page || 1) <= 1, onclick: () => { state.page = (d.page || 1) - 1; load(); } }, "قبلی"),
      h("button", { class: "btn sm", disabled: (d.page || 1) >= pages, onclick: () => { state.page = (d.page || 1) + 1; load(); } }, "بعدی")
    ));
  }

  async function openEvent(ev) {
    const body = h("div", {}, loading());
    // An unknown person in the report can be registered without leaving the
    // screen: the same enrolment dialog the dashboard uses, fed with this
    // event's stored frame.
    const isAdmin = store.state.user && store.state.user.role === "admin";
    const registerFromEvent = async (c) => {
      let still = "";
      let faces = [];
      let preview = "";
      // Prefer a fresh frame from the camera; fall back to the stored one.
      const camId = ev.camera_id || 0;
      if (camId) {
        try {
          const res = await window.API.post("/api/enrolment/capture", { camera_id: camId });
          still = res.still || "";
          faces = res.faces || [];
          preview = res.preview || "";
        } catch (e) { /* camera unreachable - use the stored frame */ }
      }
      if (!still && ev.frame_file) {
        try {
          const blob = await (await fetch(window.API.mediaUrl(ev.frame_file), { credentials: "same-origin" })).blob();
          still = await new Promise((resolve) => {
            const r = new FileReader();
            r.onload = () => resolve(String(r.result || ""));
            r.onerror = () => resolve("");
            r.readAsDataURL(blob);
          });
          // The stored frame has not been through the candidate detector yet,
          // so the dialog would fall back to the crop editor even though the
          // person is plainly visible.  Detect on it before handing it over.
          try {
            const det = await window.API.post("/api/enrolment/detect-b64", { image: still });
            faces = det.faces || [];
            preview = det.preview || "";
          } catch (e) { /* let the operator crop manually */ }
        } catch (e) { /* nothing to fall back to */ }
      }
      if (!still) { toast("تصویری برای ثبت در دسترس نیست", "warn"); return; }
      c();
      window.PagePeople.openEnrolFromEvent(ev, {
        still, faces, preview, cameraId: camId,
        source: "گزارش‌ها / " + (ev.camera_source || ""),
        onSaved: () => load(),
      });
    };

    const actions = [{ label: "بستن", kind: "ghost", onClick: (c) => c() }];
    if (isAdmin && ev.identity_name === "unknown") {
      actions.unshift({ label: "➕ ثبت این شخص در فهرست", kind: "primary", onClick: registerFromEvent });
    }
    const m = modal({ title: ev.identity_name === "unknown" ? L.unknown : ev.identity_name, size: "lg", body,
      actions });
    try {
      const detail = await window.API.get("/api/events/" + ev.id);
      const e = detail.event;
      const idn = detail.identity;
      clear(body);
      const row = (k, v) => h("tr", {}, h("td", { class: "muted" }, k), h("td", {}, v));
      body.appendChild(h("div", { class: "grid-2" },
        h("table", { class: "data" }, h("tbody", {},
          row("نام", e.identity_name === "unknown" ? L.unknown : e.identity_name),
          row("شباهت", I.pct(e.score, 1)),
          row("سن", e.age === null ? "\u2014" : I.num(e.age)),
          row("جنسیت", L[e.gender] || L.notset),
          row("وضعیت", roleBadge(e.role)),
          row("شماره ملی", e.national_number ? I.num(e.national_number) : "\u2014"),
          row("دوربین", e.camera_source),
          row("تاریخ", I.formatDate(e.event_date)),
          row("ساعت", I.formatTime(e.event_time)),
          row("شماره ترک", I.num(e.track_id)),
          idn ? row("توضیحات", idn.description || "\u2014") : null
        )),
        h("div", {},
          h("div", { class: "img-box" }, window.API.mediaUrl(e.frame_file) ? h("img", { src: window.API.mediaUrl(e.frame_file), alt: "frame" }) : empty("تصویر موجود نیست"))
        )
      ));
    } catch (err) {
      clear(body); body.appendChild(errorState(err.message));
    }
  }

  async function removeEvent(ev) {
    const ok = await window.UI.confirmDialog({
      title: "حذف رویداد",
      message: `آیا از حذف رویداد «${ev.identity_name}» در تاریخ ${I.formatDate(ev.event_date)} مطمئن هستید؟`,
      danger: true, confirmLabel: "حذف",
    });
    if (!ok) return;
    try {
      await window.API.del("/api/events/" + ev.id);
      toast("رویداد حذف شد", "ok");
      load();      // the list updates immediately
    } catch (e) { toast("خطا: " + e.message, "danger"); }
  }

  function exportReport() {
    const f = Object.assign({}, state.filters);
    delete f.event_ids;
    const locked = store.state.settings && store.state.settings.report_lock;
    if (!locked) {
      window.location.href = window.API.exportUrl(f);
      return;
    }
    // report-lock: require the administrator password
    const login = h("input", { type: "text", placeholder: "نام کاربری مدیر" });
    const pass = h("input", { type: "password", placeholder: "رمز عبور مدیر" });
    modal({
      title: "قفل گزارش",
      size: "sm",
      body: h("div", {},
        h("div", { class: "lock-note" }, "\u{1F512} قفل گزارش فعال است. برای خروجی گرفتن رمز مدیر لازم است."),
        h("div", { class: "field", style: "margin-top:10px" }, h("label", {}, "نام کاربری"), login),
        h("div", { class: "field" }, h("label", {}, "رمز عبور"), pass)),
      actions: [
        { label: "انصراف", kind: "ghost", onClick: (c) => c() },
        { label: "دریافت گزارش", kind: "primary", onClick: (c) => {
            const p = Object.assign({}, f, { admin_login: login.value, admin_password: pass.value });
            window.location.href = window.API.exportUrl(p);
            c();
          } },
      ],
    });
  }

  async function mount(host) {
    root = host;
    clear(root);
    root.appendChild(h("div", { class: "page-head" },
      h("h1", {}, "گزارش‌ها"),
      h("span", { class: "sub" }, "رویدادهای شناسایی با فیلترهای ترکیبی"),
      h("div", { style: "flex:1" }),
      h("button", { class: "btn primary", onclick: exportReport }, "\u{1F5A8} خروجی چاپی")
    ));
    const reload = () => load();
    root.appendChild(filterBar(reload));
    tableHost = h("div", { class: "card", style: "margin-top:12px" });
    root.appendChild(tableHost);
    // settings feed the report-lock state
    try {
      const s = await window.API.get("/api/settings");
      store.set({ settings: s.settings });
    } catch (e) { /* ignore */ }
    load();
  }

  window.PageReports = { mount, state };
})();
