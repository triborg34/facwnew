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
  //
  // Discovery has three inputs the previous version silently ignored:
  //   * credentials  - a camera answers WS-Discovery without them but refuses
  //                    GetProfiles, so no stream address can be built;
  //   * subnet       - a camera on another VLAN is never reached by a /24 sweep;
  //   * ONVIF describe - the stream address lives in the WSDL services
  //                    (GetProfiles + GetStreamUri), not in the discovery reply.
  // All three are exposed here, and the device row shows the ONVIF profile that
  // produced the address so a wrong guess is visible instead of silent.
  let discCreds = { username: "", password: "", subnet: "" };

  function renderDiscovery() {
    const host = root && root.querySelector("#disc-host");
    if (!host) return;
    clear(host);
    const user = h("input", { type: "text", placeholder: "admin", value: discCreds.username, autocomplete: "off" });
    const pass = h("input", { type: "password", placeholder: "رمز دوربین", value: discCreds.password, autocomplete: "new-password" });
    const subnet = h("input", { type: "text", placeholder: "مثلاً 192.168.1.0/24 (اختیاری)", value: discCreds.subnet });
    const remember = () => {
      discCreds = { username: user.value.trim(), password: pass.value, subnet: subnet.value.trim() };
    };
    [user, pass, subnet].forEach((el) => el.addEventListener("input", remember));

    host.appendChild(h("div", { class: "card-head" },
      h("span", {}, "کشف دوربین در شبکه"),
      h("span", { class: "badge" + (discoveryRunning ? " warn" : "") }, discoveryRunning ? "در حال اسکن" : "آماده"),
      h("div", { class: "spacer" }),
      h("button", { class: "btn sm primary", disabled: discoveryRunning, onclick: () => startScan(remember) }, "شروع اسکن"),
      h("button", { class: "btn sm", disabled: !discoveryRunning, onclick: cancelScan }, "توقف")));

    const body = h("div", { class: "card-body" },
      h("div", { class: "grid-3", style: "margin-bottom:10px" },
        h("div", { class: "field" }, h("label", {}, "نام کاربری دوربین"), user),
        h("div", { class: "field" }, h("label", {}, "رمز عبور"), pass),
        h("div", { class: "field" }, h("label", {}, "شبکه (اختیاری)"), subnet)),
      h("div", { class: "muted", style: "font-size:12px;margin-bottom:10px" },
        "دوربین‌ها با WS-Discovery (ONVIF) پیدا می‌شوند؛ آدرس پخش از سرویس‌های ONVIF دوربین گرفته می‌شود. " +
        "بدون نام کاربری و رمز، بیشتر دوربین‌ها فقط «پیدا» می‌شوند و آدرس پخش نمی‌دهند."));

    if (!discoveryDevices.length) {
      body.appendChild(empty(discoveryRunning ? "در حال جست‌وجو…" : "دستگاهی یافت نشد",
        "نتایج حین اسکن زنده نمایش داده می‌شوند. اگر دوربینی پیدا نشد، شبکه را دستی وارد کنید."));
    } else {
      body.appendChild(h("div", { class: "table-wrap" }, h("table", { class: "data" },
        h("thead", {}, h("tr", {}, h("th", {}, "آدرس"), h("th", {}, "نام / مدل"),
          h("th", {}, "آدرس پخش"), h("th", {}, "منبع"), h("th", {}, "عملیات"))),
        h("tbody", {}, discoveryDevices.map((d) => h("tr", {},
          h("td", { class: "mono" }, d.address + (d.port && d.port !== 554 ? ":" + d.port : "")),
          h("td", {}, h("div", {}, d.name || "—"),
            h("div", { class: "muted", style: "font-size:11px" }, [d.vendor, d.model].filter(Boolean).join(" ") || "—")),
          h("td", { class: "mono", style: "max-width:230px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap",
            title: d.stream_address || "" }, d.stream_address || (d.note || "—")),
          h("td", {}, h("span", { class: "badge" }, (d.sources || []).join(", ") || "—")),
          h("td", { style: "white-space:nowrap" },
            h("button", { class: "btn sm", onclick: () => registerDevice(d, remember) }, "افزودن"),
            h("button", { class: "btn sm ghost", onclick: () => describeDevice(d, remember) }, "ONVIF"),
            h("button", { class: "btn sm ghost", onclick: () => testPreview(d, remember) }, "پیش‌نمایش"))
        ))))));
    }
    host.appendChild(body);
  }

  async function startScan(remember) {
    if (remember) remember();
    try {
      const r = await window.API.post("/api/discovery/start", {
        username: discCreds.username,
        password: discCreds.password,
        subnets: discCreds.subnet ? [discCreds.subnet] : undefined,
      });
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

  // Full WSDL describe: device identity, media service, profiles and the
  // stream address of each profile.  The operator picks the profile instead of
  // guessing a path, which is what made discovery look broken before.
  async function describeDevice(d, remember) {
    if (remember) remember();
    const body = h("div", {}, loading("در حال پرس‌وجوی سرویس‌های ONVIF…"));
    const m = modal({
      title: "سرویس‌های ONVIF — " + d.address,
      size: "lg",
      body,
      actions: [{ label: "بستن", kind: "ghost", onClick: (c) => c() }],
    });
    let res = null;
    try {
      res = await window.API.post("/api/discovery/onvif", {
        address: d.address,
        port: d.port && d.port !== 554 ? d.port : 80,
        service_url: d.service_url,
        username: discCreds.username,
        password: discCreds.password,
      });
    } catch (e) {
      clear(body);
      body.appendChild(errorState("سرویس ONVIF پاسخ نداد: " + e.message, () => { m.close(); describeDevice(d, remember); }));
      return;
    }
    clear(body);
    const info = res.info || {};
    body.appendChild(h("table", { class: "data", style: "margin-bottom:12px" }, h("tbody", {},
      h("tr", {}, h("td", { class: "muted" }, "سازنده"), h("td", {}, info.manufacturer || "—")),
      h("tr", {}, h("td", { class: "muted" }, "مدل"), h("td", {}, info.model || "—")),
      h("tr", {}, h("td", { class: "muted" }, "نسخه نرم‌افزار"), h("td", {}, info.firmware || "—")),
      h("tr", {}, h("td", { class: "muted" }, "سریال"), h("td", {}, info.serial || "—")),
      h("tr", {}, h("td", { class: "muted" }, "سرویس رسانه"), h("td", { class: "mono" }, res.media_url || "—")),
    )));
    const profiles = res.profiles || [];
    if (!profiles.length) {
      body.appendChild(empty("پروفایلی برنگشت",
        (res.errors || []).join(" · ") || "نام کاربری و رمز دوربین را بررسی کنید؛ بیشتر دوربین‌ها بدون احراز هویت GetProfiles را رد می‌کنند."));
      return;
    }
    profiles.forEach((p) => {
      body.appendChild(h("div", { class: "card", style: "margin-bottom:8px" },
        h("div", { class: "card-head" },
          h("span", {}, p.label || p.token),
          h("span", { class: "badge" }, p.encoding || "—"),
          h("div", { class: "spacer" }),
          h("button", { class: "btn sm", onclick: () => {
            d.stream_address = p.uri;
            d.sources = (d.sources || []).concat(["onvif:" + (p.name || p.token)]);
            toast("آدرس پخش این پروفایل انتخاب شد", "ok");
            m.close();
            renderDiscovery();
          } }, "انتخاب این پروفایل")),
        h("div", { class: "card-body mono", style: "font-size:12px;word-break:break-all" }, p.uri || "—")));
    });
  }

  // Try the address the way the pipeline does: decode a single frame.
  async function testPreview(d, remember) {
    if (remember) remember();
    if (!d.stream_address) {
      toast("ابتدا آدرس پخش را با دکمه ONVIF بدست آورید", "warn");
      return;
    }
    const body = h("div", {}, loading("در حال دریافت یک قاب…"));
    const m = modal({
      title: "پیش‌نمایش — " + d.address,
      size: "lg",
      body,
      actions: [{ label: "بستن", kind: "ghost", onClick: (c) => c() }],
    });
    try {
      // goes through the server so the browser never needs the camera itself
      const url = "/api/cameras/probe-preview?stream=" + encodeURIComponent(d.stream_address);
      const img = h("img", { src: url + "&t=" + Date.now(), style: "max-width:100%;border-radius:8px" });
      const probe = new Image();
      probe.onload = () => { clear(body); body.appendChild(img); };
      probe.onerror = () => {
        clear(body);
        body.appendChild(errorState("قاب دریافت نشد — آدرس پخش یا رمز اشتباه است.",
          () => { m.close(); describeDevice(d, remember); }));
      };
      probe.src = url + "&t=" + Date.now();
    } catch (e) {
      clear(body);
      body.appendChild(errorState(e.message));
    }
  }

  async function registerDevice(d, remember) {
    if (remember) remember();
    const name = h("input", { type: "text", value: d.name || ("camera-" + d.address.replace(/\./g, "-")) });
    const stream = h("input", { type: "text", class: "mono", value: d.stream_address || "" });
    modal({
      title: "افزودن دوربین",
      size: "md",
      body: h("div", {},
        h("div", { class: "field" }, h("label", {}, "نام نمایشی"), name),
        h("div", { class: "field" }, h("label", {}, "آدرس پخش (RTSP)"), stream),
        h("div", { class: "muted", style: "font-size:12px" },
          "اگر آدرس خالی باشد از نام کاربری و رمز همین صفحه استفاده می‌شود و مسیر استاندارد دوربین ساخته می‌شود.")),
      actions: [
        { label: "انصراف", kind: "ghost", onClick: (c) => c() },
        { label: "ثبت", kind: "primary", onClick: async (c) => {
            try {
              await window.API.post("/api/cameras", {
                name: name.value.trim() || ("camera-" + d.address.replace(/\./g, "-")),
                address: d.address,
                port: d.port || 554,
                stream_type: "rtsp",
                stream_address: stream.value.trim(),
                username: discCreds.username,
                password: discCreds.password,
              });
              toast("دوربین ثبت شد", "ok");
              c();
              await loadCameras();
            } catch (e) { toast("خطا: " + e.message, "danger"); }
          } },
      ],
    });
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
