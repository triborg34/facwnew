/* Aman Negar console - zones: draw the regions of a camera and bind a relay
 * to each of them.
 *
 * A zone is an area of the picture in which the pipeline does its work: with
 * zone mode on, everything outside the drawn regions is masked away and only
 * the people inside a region are processed.  Every region may carry its own
 * relay board and relay number, so one region opens a different door than
 * another - that is the whole point of the "region mode" of the original
 * system.
 *
 * The console has no build step and no drawing library, so the regions are
 * drawn on a plain canvas over the camera snapshot.  The points are stored
 * normalised (0..1), exactly like the server expects, so the geometry never
 * drifts when the stream resolution changes.
 */
(function () {
  "use strict";
  const { h, clear, toast, modal, loading, empty, errorState, store, confirmDialog } = window.UI;

  const COLOURS = ["#38bdf8", "#34d399", "#f59e0b", "#f472b6", "#a78bfa", "#fb7185", "#22d3ee"];
  const MODES = [
    { id: "polygon", label: "چندضلعی" },
    { id: "rect", label: "مستطیل" },
    { id: "line", label: "خط" },
  ];

  const S = {
    cameras: [],
    cameraId: 0,
    zones: [],        // the working copy of the camera's regions
    selected: -1,     // index into S.zones, -1 = none
    mode: "polygon",
    points: [],       // the region currently being drawn (normalised)
    dragging: false,
    dragStart: null,
    dragNow: null,
    imgW: 0,
    imgH: 0,
    canvas: null,
    ctx: null,
    img: null,
    mounted: false,
  };

  // ---------------------------------------------------------------- helpers
  const kindLabel = (kind) => (MODES.find((m) => m.id === kind) || MODES[0]).label;

  function normToPx(p) {
    const box = S.img ? S.img.getBoundingClientRect() : null;
    if (!box || !box.width || !box.height) return [0, 0];
    return [p[0] * box.width, p[1] * box.height];
  }

  function eventToNorm(ev) {
    const box = S.img.getBoundingClientRect();
    if (!box.width || !box.height) return [0, 0];
    const x = Math.min(1, Math.max(0, (ev.clientX - box.left) / box.width));
    const y = Math.min(1, Math.max(0, (ev.clientY - box.top) / box.height));
    return [x, y];
  }

  // ---------------------------------------------------------------- painting
  function paintZone(ctx, zone, colour, dashed) {
    const pts = (zone.points || []).map(normToPx);
    if (!pts.length) return;
    ctx.save();
    ctx.strokeStyle = colour;
    ctx.fillStyle = colour;
    ctx.lineWidth = 2;
    if (dashed) ctx.setLineDash([5, 4]);
    if (zone.kind === "rect" && pts.length >= 4) {
      const [x1, y1] = pts[0];
      const [x2, y2] = pts[2];
      ctx.strokeRect(Math.min(x1, x2), Math.min(y1, y2), Math.abs(x2 - x1), Math.abs(y2 - y1));
    } else if (zone.kind === "line" && pts.length >= 2) {
      ctx.beginPath();
      ctx.moveTo(pts[0][0], pts[0][1]);
      ctx.lineTo(pts[1][0], pts[1][1]);
      ctx.stroke();
    } else if (pts.length >= 3) {
      ctx.beginPath();
      ctx.moveTo(pts[0][0], pts[0][1]);
      for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
      ctx.closePath();
      ctx.stroke();
    }
    // the name in the middle of the region
    let cx = 0, cy = 0;
    pts.forEach((p) => { cx += p[0]; cy += p[1]; });
    cx /= pts.length;
    cy /= pts.length;
    ctx.setLineDash([]);
    ctx.font = "bold 12px Vazirmatn, system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    const label = zone.name || "ناحیه";
    const w = ctx.measureText(label).width;
    ctx.fillStyle = "rgba(10,16,28,.72)";
    ctx.fillRect(cx - w / 2 - 5, cy - 10, w + 10, 20);
    ctx.fillStyle = colour;
    ctx.fillText(label, cx, cy);
    ctx.restore();
  }

  function paint() {
    if (!S.ctx || !S.canvas) return;
    const box = S.img ? S.img.getBoundingClientRect() : null;
    const dpr = window.devicePixelRatio || 1;
    const w = box ? Math.round(box.width) : 0;
    const hgt = box ? Math.round(box.height) : 0;
    if (!w || !hgt) return;
    if (S.canvas.width !== Math.round(w * dpr) || S.canvas.height !== Math.round(hgt * dpr)) {
      S.canvas.width = Math.round(w * dpr);
      S.canvas.height = Math.round(hgt * dpr);
      S.canvas.style.width = w + "px";
      S.canvas.style.height = hgt + "px";
    }
    const ctx = S.ctx;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, hgt);

    S.zones.forEach((z, i) => {
      paintZone(ctx, z, i === S.selected ? "#ffffff" : (z.colour || "#38bdf8"), i !== S.selected);
    });
    if (S.points.length) {
      const draft = { kind: S.mode, points: S.points, name: "" };
      paintZone(ctx, draft, "#ffffff", true);
      // the points themselves
      ctx.save();
      ctx.fillStyle = "#ffffff";
      S.points.forEach((p) => {
        const [x, y] = normToPx(p);
        ctx.beginPath();
        ctx.arc(x, y, 3.5, 0, Math.PI * 2);
        ctx.fill();
      });
      ctx.restore();
    }
  }

  // ---------------------------------------------------------------- editing
  function newZone(kind) {
    return {
      id: "z" + Date.now().toString(36),
      name: "ناحیه " + (S.zones.length + 1),
      description: "",
      kind: kind || S.mode,
      colour: COLOURS[S.zones.length % COLOURS.length],
      points: [],
      relay_host: "",
      relay_port: 23,
      relay_number: 1,
      relay_user: "admin",
      relay_password: "",
      has_relay_password: false,
      image_w: S.imgW,
      image_h: S.imgH,
    };
  }

  function onCanvasClick(ev) {
    if (ev.button === 2) return;   // right click closes a polygon
    if (S.mode === "polygon") {
      S.points.push(eventToNorm(ev));
      paint();
    }
  }

  function onCanvasDblClick(ev) {
    if (S.mode !== "polygon") return;
    ev.preventDefault();
    finishPolygon();
  }

  function finishPolygon() {
    if (S.points.length < 3) {
      toast("برای بستن ناحیه حداقل سه نقطه لازم است", "warn");
      return;
    }
    const zone = newZone("polygon");
    zone.points = S.points.slice();
    S.zones.push(zone);
    S.selected = S.zones.length - 1;
    S.points = [];
    paint();
    renderList();
    renderForm();
  }

  function onMouseDown(ev) {
    if (S.mode === "polygon") return;
    S.dragging = true;
    S.dragStart = eventToNorm(ev);
    S.dragNow = S.dragStart;
  }

  function onMouseMove(ev) {
    if (!S.dragging) return;
    S.dragNow = eventToNorm(ev);
    paint();
  }

  function onMouseUp() {
    if (!S.dragging) return;
    S.dragging = false;
    const a = S.dragStart;
    const b = S.dragNow;
    if (!a || !b) return;
    const zone = newZone(S.mode);
    if (S.mode === "rect") {
      zone.points = [
        [a[0], a[1]], [b[0], a[1]], [b[0], b[1]], [a[0], b[1]],
      ];
    } else {
      zone.points = [[a[0], a[1]], [b[0], b[1]]];
    }
    S.zones.push(zone);
    S.selected = S.zones.length - 1;
    S.dragStart = null;
    S.dragNow = null;
    paint();
    renderList();
    renderForm();
  }

  function undoDraft() {
    if (!S.points.length) return;
    S.points.pop();
    paint();
  }

  function deleteSelected() {
    if (S.selected < 0 || S.selected >= S.zones.length) return;
    const zone = S.zones[S.selected];
    S.zones.splice(S.selected, 1);
    S.selected = -1;
    paint();
    renderList();
    renderForm();
    toast("ناحیه «" + zone.name + "» حذف شد (برای ثبت نهایی ذخیره کنید)", "ok");
  }

  // ---------------------------------------------------------------- the form
  function renderForm() {
    const host = document.getElementById("zone-form");
    if (!host) return;
    clear(host);
    const zone = S.selected >= 0 ? S.zones[S.selected] : null;
    if (!zone) {
      host.appendChild(empty("ناحیه‌ای انتخاب نشده", "یک ناحیه بکشید یا از فهرست انتخاب کنید"));
      return;
    }
    const name = h("input", { type: "text", value: zone.name || "" });
    const uid = h("input", { type: "text", value: zone.id || "" });
    const desc = h("textarea", {}, zone.description || "");
    const colour = h("input", { type: "color", value: zone.colour || "#38bdf8" });
    const relayHost = h("input", { type: "text", placeholder: "192.168.1.200", value: zone.relay_host || "" });
    const relayPort = h("input", { type: "number", min: 1, max: 65535, value: zone.relay_port || 23 });
    const relayNumber = h("input", { type: "number", min: 1, max: 64, value: zone.relay_number || 1 });
    const relayUser = h("input", { type: "text", value: zone.relay_user || "admin" });
    const relayPass = h("input", {
      type: "password",
      placeholder: zone.has_relay_password ? "ذخیره شده — برای تغییر بنویسید" : "admin",
      autocomplete: "new-password",
    });
    const testOut = h("div", { class: "muted", style: "font-size:11.5px;margin-top:6px" });

    const apply = () => {
      zone.name = name.value.trim() || zone.name;
      zone.id = uid.value.trim() || zone.id;
      zone.description = desc.value.trim();
      zone.colour = colour.value;
      zone.relay_host = relayHost.value.trim();
      zone.relay_port = Number(relayPort.value) || 23;
      zone.relay_number = Number(relayNumber.value) || 1;
      zone.relay_user = relayUser.value.trim() || "admin";
      if (relayPass.value) {
        zone.relay_password = relayPass.value;
        zone.has_relay_password = true;
      }
      paint();
      renderList();
    };

    const testRelay = async () => {
      apply();
      if (!zone.relay_host) { toast("آدرس رله را وارد کنید", "warn"); return; }
      testOut.textContent = "در حال بررسی…";
      try {
        const res = await window.API.post("/api/relay/test", {
          host: zone.relay_host,
          port: zone.relay_port,
          username: zone.relay_user,
          password: relayPass.value,
        });
        testOut.textContent = res.ok ? "رله پاسخ داد ✓" : ("خطا: " + (res.detail || "بدون پاسخ"));
      } catch (e) {
        testOut.textContent = "خطا: " + e.message;
      }
    };

    host.appendChild(h("div", { class: "row" },
      h("div", { class: "field" }, h("label", {}, "نام ناحیه *"), name),
      h("div", { class: "field" }, h("label", {}, "شناسه"), uid)));
    host.appendChild(h("div", { class: "row", style: "margin-top:10px" },
      h("div", { class: "field" }, h("label", {}, "توضیحات"), desc),
      h("div", { class: "field" }, h("label", {}, "رنگ"), colour)));
    host.appendChild(h("div", { class: "muted", style: "font-size:12px;margin-top:14px;font-weight:700" }, "رله‌ی این ناحیه"));
    host.appendChild(h("div", { class: "row", style: "margin-top:6px" },
      h("div", { class: "field" }, h("label", {}, "آدرس رله"), relayHost),
      h("div", { class: "field" }, h("label", {}, "پورت"), relayPort)));
    host.appendChild(h("div", { class: "row", style: "margin-top:10px" },
      h("div", { class: "field" }, h("label", {}, "شماره رله"), relayNumber),
      h("div", { class: "field" }, h("label", {}, "نام کاربری"), relayUser),
      h("div", { class: "field" }, h("label", {}, "رمز عبور"), relayPass)));
    host.appendChild(h("div", { class: "row", style: "margin-top:10px;align-items:center" },
      h("button", { class: "btn sm", onclick: testRelay }, "آزمایش اتصال رله"),
      h("div", { style: "flex:1" }, testOut)));
    host.appendChild(h("div", { class: "row", style: "margin-top:14px" },
      h("button", { class: "btn primary sm", onclick: () => { apply(); toast("تغییرات ناحیه اعمال شد", "ok"); } }, "اعمال تغییرات"),
      h("button", { class: "btn sm danger", onclick: deleteSelected }, "حذف ناحیه")));
  }

  function renderList() {
    const host = document.getElementById("zone-list");
    if (!host) return;
    clear(host);
    if (!S.zones.length) {
      host.appendChild(h("div", { class: "muted", style: "padding:10px;font-size:12px" }, "هنوز ناحیه‌ای تعریف نشده"));
      return;
    }
    S.zones.forEach((z, i) => {
      const relay = z.relay_host ? (z.relay_host + " #" + (z.relay_number || 1)) : "بدون رله";
      host.appendChild(h("div", {
        class: "zone-item" + (i === S.selected ? " active" : ""),
        onclick: () => { S.selected = i; renderList(); renderForm(); paint(); },
      },
        h("span", { class: "dot", style: "background:" + (z.colour || "#38bdf8") }),
        h("span", { class: "n" }, z.name || "ناحیه"),
        h("span", { class: "badge" }, kindLabel(z.kind)),
        h("span", { class: "muted", style: "font-size:11px" }, relay)));
    });
  }

  // ---------------------------------------------------------------- saving
  async function save() {
    if (!S.cameraId) return;
    const payload = S.zones.map((z) => {
      const out = {
        id: z.id,
        name: z.name,
        description: z.description,
        kind: z.kind,
        colour: z.colour,
        points: z.points,
        image_w: z.image_w || S.imgW,
        image_h: z.image_h || S.imgH,
        relay_host: z.relay_host || "",
        relay_port: z.relay_port || 23,
        relay_number: z.relay_number || 1,
        relay_user: z.relay_user || "admin",
      };
      // an empty password field must not clear the stored one
      if (z.relay_password) out.relay_password = z.relay_password;
      // tells the server that a credential is stored for this zone
      out.has_relay_password = !!z.has_relay_password;
      return out;
    });
    try {
      const res = await window.API.put("/api/zones/" + S.cameraId, { zones: payload });
      toast("ذخیره شد — " + res.count + " ناحیه", "ok");
      await loadZones();
      store.set({ cameras: (await window.API.get("/api/cameras")).items || store.state.cameras });
    } catch (e) {
      toast("خطا در ذخیره: " + e.message, "danger");
    }
  }

  async function loadZones() {
    if (!S.cameraId) { S.zones = []; return; }
    try {
      const res = await window.API.get("/api/zones/" + S.cameraId);
      S.zones = (res.zones || []).map((z) => Object.assign({}, z, { points: z.points || [] }));
    } catch (e) {
      toast("خطا در خواندن نواحی: " + e.message, "danger");
      S.zones = [];
    }
  }

  function refreshSnapshot() {
    if (!S.cameraId || !S.img) return;
    S.img.src = window.API.snapshotUrl(S.cameraId) + "&t=" + Date.now();
  }

  // ---------------------------------------------------------------- the page
  async function mount(host) {
    root = host;
    clear(root);
    root.appendChild(h("div", { class: "page-head" },
      h("h1", {}, "نواحی"),
      h("span", { class: "sub" }, "ناحیه‌ی فعال هر دوربین و رله‌ی هر ناحیه"),
      h("div", { style: "flex:1" }),
      h("button", { class: "btn sm", onclick: refreshSnapshot }, "به‌روزرسانی تصویر"),
      h("button", { class: "btn primary sm", onclick: save }, "ذخیره نواحی")));

    const camSelect = h("select", {}, []);
    const stage = h("div", { class: "zone-stage" });
    const listHost = h("div", { id: "zone-list", class: "zone-list" });
    const formHost = h("div", { id: "zone-form", class: "zone-form" });

    const modeBar = h("div", { class: "zone-modes" });
    MODES.forEach((m) => {
      modeBar.appendChild(h("button", {
        class: "btn sm" + (S.mode === m.id ? " primary" : ""),
        onclick: (ev) => {
          S.mode = m.id;
          S.points = [];
          modeBar.querySelectorAll("button").forEach((b) => b.classList.remove("primary"));
          ev.currentTarget.classList.add("primary");
          paint();
        },
      }, m.label));
    });
    modeBar.appendChild(h("button", { class: "btn sm", onclick: undoDraft }, "واگرد آخرین نقطه"));
    modeBar.appendChild(h("span", { class: "muted", style: "font-size:11.5px;margin-inline-start:auto" },
      "چندضلعی: کلیک برای افزودن نقطه، دوبار کلیک برای بستن · مستطیل و خط: بکشید"));

    root.appendChild(h("div", { class: "zone-toolbar" },
      h("div", { class: "field", style: "max-width:280px" }, h("label", {}, "دوربین"), camSelect),
      modeBar));

    root.appendChild(h("div", { class: "zone-layout" },
      h("div", { class: "card" },
        h("div", { class: "card-body" }, stage)),
      h("div", { class: "zone-side" },
        h("div", { class: "card" },
          h("div", { class: "card-head" }, h("span", {}, "نواحی این دوربین")),
          h("div", { class: "card-body" }, listHost)),
        h("div", { class: "card", style: "margin-top:12px" },
          h("div", { class: "card-head" }, h("span", {}, "تنظیمات ناحیه")),
          h("div", { class: "card-body" }, formHost)))));

    // the drawing stage: a snapshot with a canvas on top of it
    S.canvas = h("canvas", { class: "zone-canvas" });
    S.ctx = S.canvas.getContext("2d");
    S.img = h("img", { class: "zone-image", alt: "" });
    stage.appendChild(S.img);
    stage.appendChild(S.canvas);
    S.canvas.addEventListener("click", onCanvasClick);
    S.canvas.addEventListener("dblclick", onCanvasDblClick);
    S.canvas.addEventListener("mousedown", onMouseDown);
    S.canvas.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
    S.canvas.addEventListener("contextmenu", (ev) => {
      ev.preventDefault();
      if (S.mode === "polygon") finishPolygon();
    });
    S.img.addEventListener("load", () => {
      S.imgW = S.img.naturalWidth || 0;
      S.imgH = S.img.naturalHeight || 0;
      paint();
    });

    S.mounted = true;

    // the camera list
    try {
      const res = await window.API.get("/api/cameras");
      S.cameras = res.items || [];
    } catch (e) {
      S.cameras = [];
    }
    if (!S.cameras.length) {
      stage.appendChild(empty("دوربینی ثبت نشده است",
        "از صفحهٔ «دوربین‌ها» یک دوربین اضافه کنید تا بتوانید برای آن ناحیه تعریف کنید"));
      renderList();
      renderForm();
      return;
    }
    S.cameras.forEach((c) => {
      camSelect.appendChild(h("option", { value: String(c.id) }, c.name + " — " + c.address));
    });
    S.cameraId = S.cameras[0].id;
    camSelect.value = String(S.cameraId);
    camSelect.onchange = async () => {
      S.cameraId = Number(camSelect.value);
      S.selected = -1;
      S.points = [];
      await loadZones();
      refreshSnapshot();
      renderList();
      renderForm();
      paint();
    };
    await loadZones();
    refreshSnapshot();
    renderList();
    renderForm();
    window.addEventListener("resize", paint);
  }

  let root = null;

  function unmount() {
    S.mounted = false;
    window.removeEventListener("mouseup", onMouseUp);
    window.removeEventListener("resize", paint);
    S.canvas = null;
    S.ctx = null;
    S.img = null;
  }

  window.PageZones = { mount, unmount, state: S };
})();
