/* FACW console - cameras screen: inventory, add/edit, discovery. */
(function () {
  "use strict";
  const { h, clear, toast, modal, loading, empty, errorState, store } = window.UI;
  const I = window.I18N;
  const L = window.LABELS;

  let root = null;
  let discoveryDevices = [];
  let discoveryRunning = false;

  const statusBadge = (s) => {
    const map = { streaming: ["ok", "در حال پخش"], offline: ["danger", "آفلاین"], reconnecting: ["warn", "اتصال مجدد"], idle: ["", "آماده"], connecting: ["warn", "در حال اتصال"] };
    const [cls, txt] = map[s] || ["", s || "—"];
    return h("span", { class: "badge " + cls }, txt);
  };

  // ------------------------------------------------------------ inventory
  function renderInventory() {
    const host = root.querySelector("#inv-host");
    if (!host) return;
    clear(host);
    const cams = store.state.cameras || [];
    if (!cams.length) { host.appendChild(empty("دوربینی ثبت نشده است", "با دکمهٔ «افزودن دوربین» شروع کنید")); return; }

    host.appendChild(h("div", { class: "table-wrap" },
      h("table", { class: "data" },
        h("thead", {}, h("tr", {},
          h("th", {}, "#"), h("th", {}, "شناسه"), h("th", {}, "نام"), h("th", {}, "آدرس"),
          h("th", {}, "پورت"), h("th", {}, "دروازه"), h("th", {}, "آدرس پخش"),
          h("th", {}, "کاربری"), h("th", {}, "وضعیت"), h("th", {}, "عملیات")
        )),
        h("tbody", {}, cams.map((c, i) => h("tr", {},
          h("td", { class: "mono" }, I.num(i + 1)),
          h("td", { class: "mono" }, I.num(c.id)),
          h("td", {}, h("b", {}, c.name)),
          h("td", { class: "mono" }, c.address),
          h("td", { class: "mono" }, c.stream_type === "file" || c.stream_type === "webcam" ? "\u2014" : I.num(c.port)),
          h("td", {}, c.gate_label || "\u2014"),
          h("td", { class: "mono", style: "max-width:230px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" },
            redact(c.stream_address || "")),
          h("td", {}, c.username ? I.num(c.username) : "\u2014"),
          h("td", {}, statusBadge(c.status)),
          h("td", { style: "white-space:nowrap" },
            h("button", { class: "btn sm ghost", title: "ویرایش", onclick: () => openCameraForm(c) }, "\u270E"),
            h("button", { class: "btn sm ghost", title: "بررسی اتصال", onclick: () => checkCamera(c) }, "\u{1F50C}"),
            h("button", { class: "btn sm ghost", title: "حذف", onclick: () => removeCamera(c) }, "\u{1F5D1}")
          )
        )))
      )
    ));
  }

  function redact(url) {
    // credentials are never displayed in listings
    return String(url || "").replace(/\/\/([^:/@]+):([^@]+)@/, "//$1:***@");
  }

  async function checkCamera(c) {
    try {
      const r = await window.API.post(`/api/cameras/${c.id}/check`);
      toast(r.reachable ? `\u062F\u0648\u0631\u0628\u06CC\u0646 \u067E\u0627\u0633\u062E \u0645\u06CC\u200C\u062F\u0647\u062F (${r.host})` : `\u062F\u0633\u062A\u0631\u0633\u06CC \u0646\u06CC\u0633\u062A: ${r.detail}`,
        r.reachable ? "ok" : "warn");
    } catch (e) { toast("\u062E\u0637\u0627: " + e.message, "danger"); }
  }

  async function removeCamera(c) {
    const ok = await window.UI.confirmDialog({
      title: "حذف دوربین",
      message: `آیا دوربین «${c.name}» حذف شود؟ کاشی مربوط به آن از صفحهٔ مانیتورینگ حذف خواهد شد.`,
      danger: true, confirmLabel: "حذف",
    });
    if (!ok) return;
    try {
      await window.API.del("/api/cameras/" + c.id);
      toast("دوربین حذف شد", "ok");
      await loadCameras();
    } catch (e) { toast("خطا: " + e.message, "danger"); }
  }

  // ----------------------------------------------------------------- form
  function openCameraForm(existing) {
    const c = existing || {};
    const f = {
      name: h("input", { type: "text", value: c.name || "", placeholder: "دوربین ورودی" }),
      address: h("input", { type: "text", value: c.address || "", placeholder: "192.168.1.64" }),
      port: h("input", { type: "number", value: c.port !== undefined ? c.port : 554, min: 0, max: 65535 }),
      profile_name: h("input", { type: "text", value: c.profile_name || "main" }),
      stream_type: h("select", {},
        [["rtsp", "RTSP"], ["http", "HTTP (MJPEG)"], ["webcam", "دوربین محلی"], ["file", "فایل ویدئو"]]
          .map(([v, t]) => h("option", { value: v, selected: (c.stream_type || "rtsp") === v ? true : null }, t))),
      stream_address: h("input", { type: "text", value: c.stream_address || "", placeholder: "خالی بگذارید تا خودکار ساخته شود" }),
      gate_label: h("input", { type: "text", value: c.gate_label || "" }),
      username: h("input", { type: "text", value: c.username || "" }),
      password: h("input", { type: "password", value: "", placeholder: c.has_credentials ? "برای تغییر رمز، مقدار جدید وارد کنید" : "" }),
      analysis_enabled: h("input", { type: "checkbox", checked: (c.analysis_enabled === undefined ? true : c.analysis_enabled) ? true : null }),
    };
    const err = h("div", { class: "state error", style: "display:none" });

    const body = h("div", {},
      h("div", { class: "row" },
        h("div", { class: "field" }, h("label", {}, "نام نمایشی *"), f.name),
        h("div", { class: "field" }, h("label", {}, "نوع منبع"), f.stream_type)),
      h("div", { class: "row", style: "margin-top:10px" },
        h("div", { class: "field" }, h("label", {}, "آدرس دستگاه *"), f.address),
        h("div", { class: "field" }, h("label", {}, "پورت"), f.port),
        h("div", { class: "field" }, h("label", {}, "نام پروفایل"), f.profile_name)),
      h("div", { class: "row", style: "margin-top:10px" },
        h("div", { class: "field" }, h("label", {}, "نام کاربری"), f.username),
        h("div", { class: "field" }, h("label", {}, "رمز عبور"), f.password),
        h("div", { class: "field" }, h("label", {}, "برچسب دروازه"), f.gate_label)),
      h("div", { class: "field", style: "margin-top:10px" },
        h("label", {}, "آدرس کامل پخش (اختیاری)"), f.stream_address,
        h("span", { class: "hint" }, "در صورت خالی بودن از روی آدرس، پورت و پروفایل ساخته می‌شود.")),
      h("div", { class: "field", style: "margin-top:10px" },
        h("label", { class: "switch" }, f.analysis_enabled, h("span", { class: "track" }),
          h("span", {}, "تحلیل چهره برای این دوربین فعال باشد"))),
      err
    );

    modal({
      title: existing ? "ویرایش دوربین" : "افزودن دوربین",
      body,
      actions: [
        { label: "انصراف", kind: "ghost", onClick: (cl) => cl() },
        { label: "ذخیره", kind: "primary", onClick: async (cl) => {
            const payload = {
              name: f.name.value.trim(),
              address: f.address.value.trim(),
              port: f.port.value === "" ? 0 : Number(f.port.value),
              profile_name: f.profile_name.value.trim(),
              stream_type: f.stream_type.value,
              stream_address: f.stream_address.value.trim(),
              gate_label: f.gate_label.value.trim(),
              username: f.username.value.trim(),
              password: f.password.value,
              analysis_enabled: f.analysis_enabled.checked,
            };
            if (!payload.name || !payload.address) {
              err.style.display = ""; err.textContent = "نام و آدرس الزامی است";
              return;
            }
            try {
              if (existing) await window.API.put("/api/cameras/" + existing.id, payload);
              else await window.API.post("/api/cameras", payload);
              toast("ذخیره شد", "ok");
              cl();
              await loadCameras();
            } catch (e) {
              err.style.display = ""; err.textContent = e.message;
            }
          } },
      ],
    });
  }

  // ------------------------------------------------------------ discovery
  function renderDiscovery() {
    const host = root.querySelector("#disc-host");
    if (!host) return;
    clear(host);
    host.appendChild(h("div", { class: "card-head" },
      h("span", {}, "کشف دوربین در شبکه"),
      h("span", { class: "badge" + (discoveryRunning ? " warn" : "") }, discoveryRunning ? "در حال اسکن" : "آماده"),
      h("div", { class: "spacer" }),
      h("button", { class: "btn sm primary", disabled: discoveryRunning, onclick: startScan }, "شروع اسکن"),
      h("button", { class: "btn sm", disabled: !discoveryRunning, onclick: cancelScan }, "توقف")
    ));
    const body = h("div", { class: "card-body" });
    if (!discoveryDevices.length) {
      body.appendChild(empty(discoveryRunning ? "در حال جست‌وجو…" : "دستگاهی یافت نشد", "نتایج حین اسکن به‌صورت زنده نمایش داده می‌شوند"));
    } else {
      body.appendChild(h("div", { class: "table-wrap" }, h("table", { class: "data" },
        h("thead", {}, h("tr", {}, h("th", {}, "آدرس"), h("th", {}, "نام"), h("th", {}, "مدل"),
          h("th", {}, "آدرس پخش"), h("th", {}, "وضعیت"), h("th", {}, "عملیات"))),
        h("tbody", {}, discoveryDevices.map((d) => h("tr", {},
          h("td", { class: "mono" }, d.address),
          h("td", {}, d.name || "\u2014"),
          h("td", {}, d.model || "\u2014"),
          h("td", { class: "mono", style: "max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" }, d.stream_address || "\u2014"),
          h("td", {}, h("span", { class: "badge" }, (d.sources || []).join(", "))),
          h("td", {},
            h("button", { class: "btn sm", onclick: () => registerDevice(d) }, "افزودن"),
            h("button", { class: "btn sm ghost", onclick: () => fetchStream(d) }, "دریافت آدرس پخش"))
        )))
      )));
    }
    host.appendChild(body);
  }

  async function startScan() {
    try {
      const r = await window.API.post("/api/discovery/start", {});
      if (!r.started) { toast(r.detail || "اسکن آغاز نشد", "warn"); return; }
      discoveryRunning = true;
      discoveryDevices = [];
      renderDiscovery();
      toast("اسکن آغاز شد", "ok");
    } catch (e) { toast("خطا: " + e.message, "danger"); }
  }

  async function cancelScan() {
    try { await window.API.post("/api/discovery/cancel", {}); } catch (e) { /* ignore */ }
    discoveryRunning = false;
    renderDiscovery();
  }

  async function fetchStream(d) {
    const login = h("input", { type: "text", placeholder: "نام کاربری" });
    const pass = h("input", { type: "password", placeholder: "رمز عبور" });
    modal({
      title: "دریافت آدرس پخش از دستگاه",
      size: "sm",
      body: h("div", {},
        h("div", { class: "muted", style: "margin-bottom:8px" }, `دستگاه ${d.address} — از رابط مدیریت دوربین استفاده می‌شود.`),
        h("div", { class: "field" }, h("label", {}, "نام کاربری"), login),
        h("div", { class: "field" }, h("label", {}, "رمز عبور"), pass)),
      actions: [
        { label: "انصراف", kind: "ghost", onClick: (c) => c() },
        { label: "دریافت", kind: "primary", onClick: async (c) => {
            try {
              const r = await window.API.post("/api/discovery/stream-uri", {
                service_url: d.service_url, address: d.address, port: d.port,
                username: login.value, password: pass.value,
              });
              d.stream_address = r.stream_address;
              d.sources = (d.sources || []).concat([r.source]);
              toast("آدرس پخش دریافت شد", "ok");
              c(); renderDiscovery();
            } catch (e) { toast("خطا: " + e.message, "danger"); }
          } },
      ],
    });
  }

  async function registerDevice(d) {
    try {
      await window.API.post("/api/cameras", {
        name: d.name || ("camera-" + d.address.replace(/\./g, "-")),
        address: d.address,
        port: d.port || 554,
        stream_type: "rtsp",
        stream_address: d.stream_address || "",
      });
      toast("دوربین ثبت شد", "ok");
      await loadCameras();
    } catch (e) { toast("خطا: " + e.message, "danger"); }
  }

  async function loadCameras() {
    try {
      const res = await window.API.get("/api/cameras");
      store.set({ cameras: res.items || [] });
      renderInventory();
    } catch (e) {
      const host = root && root.querySelector("#inv-host");
      if (host) { clear(host); host.appendChild(errorState(e.message, loadCameras)); }
    }
  }

  async function loadDiscoveryState() {
    try {
      const r = await window.API.get("/api/discovery/results");
      discoveryDevices = r.devices || [];
      discoveryRunning = !!r.running;
      renderDiscovery();
    } catch (e) { /* ignore */ }
  }

  async function mount(host) {
    root = host;
    clear(root);
    root.appendChild(h("div", { class: "page-head" },
      h("h1", {}, "دوربین‌ها"),
      h("span", { class: "sub" }, "فهرست دوربین‌ها و کشف خودکار در شبکه"),
      h("div", { style: "flex:1" }),
      h("button", { class: "btn primary", onclick: () => openCameraForm(null) }, "+ افزودن دوربین")
    ));
    const inv = h("div", { class: "card" }, h("div", { class: "card-head" }, "فهرست دوربین‌ها"), h("div", { id: "inv-host" }, loading()));
    const disc = h("div", { class: "card", id: "disc-host", style: "margin-top:12px" });
    root.appendChild(inv);
    root.appendChild(disc);
    await loadCameras();
    await loadDiscoveryState();
  }

  window.addEventListener("facw:change", (ev) => {
    if (!root) return;
    const d = ev.detail || {};
    if (d.topic === "cameras") loadCameras();
    if (d.topic === "discovery") {
      const dev = d.data && d.data.device;
      if (dev) {
        const i = discoveryDevices.findIndex((x) => x.address === dev.address);
        if (i >= 0) discoveryDevices[i] = dev; else discoveryDevices.push(dev);
      }
      if (d.data && d.data.done) discoveryRunning = false;
      renderDiscovery();
    }
  });

  window.PageCameras = { mount };
})();
