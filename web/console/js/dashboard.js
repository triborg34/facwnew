/* FACW console - monitoring dashboard: mosaic, detections, detail panel. */
(function () {
  "use strict";
  const { h, clear, toast, modal, loading, empty, errorState, store, alertChime } = window.UI;
  const I = window.I18N;
  const L = window.LABELS;

  const S = {
    expanded: null,        // camera id being previewed large
    flavour: {},           // camera id -> annotated | raw
    detections: [],        // current live tracks
    selected: null,        // selected detection
    shown: { identified: 12, unknown: 12 },
    detailEvent: null,
    version: "",           // last /api/live/detections version we rendered
    live: {},              // camera id -> {timer, busy, el}
    detTimer: null,
    camTimer: null,
    rendered: {},          // track id -> {node, key, cls} for targeted repaints
    detHost: null,
    lastEventId: 0,
  };

  const roleBadge = (role) => {
    const cls = role === "approve" ? "ok" : role === "deny" ? "danger" : role === "visitor" ? "warn" : "accent";
    return h("span", { class: "badge " + cls }, L[role] || role || L.notset);
  };

  function stateBadge(state, name) {
    if (state === "identified") return h("span", { class: "badge ok" }, name || "\u0634\u0646\u0627\u0633\u0627\u06CC\u06CC \u0634\u062F");
    if (state === "unknown") return h("span", { class: "badge danger" }, L.unknown);
    // "analysing" is deliberately distinct from unknown
    return h("span", { class: "badge warn" }, L.analysing);
  }

  // ---------------------------------------------------------------- mosaic
  function renderMosaic(root) {
    const cams = store.state.cameras || [];
    clear(root);
    if (!cams.length) {
      root.appendChild(empty("\u062F\u0648\u0631\u0628\u06CC\u0646\u06CC \u062B\u0628\u062A \u0646\u0634\u062F\u0647 \u0627\u0633\u062A", "\u0627\u0632 \u0635\u0641\u062D\u0647 \u062F\u0648\u0631\u0628\u06CC\u0646\u200C\u0647\u0627 \u06CC\u06A9 \u062F\u0648\u0631\u0628\u06CC\u0646 \u0627\u0636\u0627\u0641\u0647 \u06A9\u0646\u06CC\u062F"));
      return;
    }
    const mosaic = h("div", { class: "mosaic" });
    cams.forEach((cam) => {
      const isAdmin = store.state.user && store.state.user.role === "admin";
      if (S.flavour[cam.id] === undefined) S.flavour[cam.id] = isAdmin ? "annotated" : "raw";
      const flavour = isAdmin ? S.flavour[cam.id] : "raw";
      const expanded = S.expanded === cam.id;
      const state = cam.status || "idle";

      // Tiles pull single JPEG frames in a tight loop (see startLive below).
      // Browsers allow only ~6 connections per host, so one long multipart
      // stream per tile starves every other API call (the console appears
      // frozen).  The loop below is throttled, skips when a frame is in
      // flight, and keeps the last good frame while the next one loads.
      const img = h("img", { alt: cam.name, src: "", "data-cam": cam.id, decoding: "async" });

      const stateDot = h("span", { class: "dot " + (state === "streaming" ? "live" : state === "offline" ? "danger" : "warn") });
      const fpsBadge = h("span", { class: "mono muted", style: "font-size:11.5px", "data-fps": cam.id },
        I.num(cam.fps || 0, 1) + " fps");

      const tile = h("div", { class: "tile" + (expanded ? " expanded" : ""), "data-tile": cam.id },
        h("div", { class: "tile-head" },
          stateDot,
          h("span", { class: "name" }, cam.name),
          cam.gate_label ? h("span", { class: "badge" }, cam.gate_label) : null,
          h("div", { class: "spacer" }),
          fpsBadge,
          isAdmin
            ? h("button", {
                class: "btn ghost sm",
                title: flavour === "annotated" ? "\u0646\u0645\u0627\u06CC\u0634 \u062E\u0627\u0645" : "\u0646\u0645\u0627\u06CC\u0634 \u062A\u062D\u0644\u06CC\u0644\u200C\u0634\u062F\u0647",
                onclick: (ev) => {
                  ev.stopPropagation();
                  S.flavour[cam.id] = flavour === "annotated" ? "raw" : "annotated";
                  renderMosaic(root);
                },
              }, flavour === "annotated" ? "\u{1F9E0}" : "\u{1F4F7}")
            : null,
          h("button", {
            class: "btn ghost sm",
            title: expanded ? "\u0628\u0633\u062A\u0646" : "\u0628\u0632\u0631\u06AF\u200C\u0646\u0645\u0627\u06CC\u06CC",
            onclick: (ev) => { ev.stopPropagation(); S.expanded = expanded ? null : cam.id; renderMosaic(root); },
          }, expanded ? "\u2715" : "\u26F6")
        ),
        h("div", { class: "video-box" }, img,
          state === "streaming" ? h("div", { class: "live-dot" }, h("i"), "LIVE") : null,
          state !== "streaming" ? h("div", { class: "overlay-msg" },
            state === "offline"
              ? (cam.last_error || "\u062F\u0648\u0631\u0628\u06CC\u0646 \u0622\u0641\u0644\u0627\u06CC\u0646 \u0627\u0633\u062A")
              : "\u062F\u0631 \u062D\u0627\u0644 \u0627\u062A\u0635\u0627\u0644\u2026") : null
        )
      );
      S.live[cam.id] = { cam: cam, el: img };
      mosaic.appendChild(tile);
    });
    root.appendChild(mosaic);
  }

  // ``element.dataset`` is a read-only accessor, so a placeholder is tagged by
  // writing one key into it (assigning the whole dataset object throws).
  function markEmpty(el) {
    el.dataset.empty = "1";
    return el;
  }

  // ------------------------------------------------------------ detections
  function detectionPanels(root) {
    const identified = S.detections.filter((d) => d.state === "identified");
    const unknown = S.detections.filter((d) => d.state === "unknown");
    const analysing = S.detections.filter((d) => d.state === "analysing");

    // ``store.state.detPanel`` is seeded by ui.js, but a page can be mounted
    // before that (or with older state), and reading a property off null threw
    // inside the builder - so the panels silently never appeared.
    if (!store.state.detPanel) store.state.detPanel = { identified: true, unknown: true };

    const panel = (key, title, items, emptyText) => {
      const collapsed = !store.state.detPanel[key];
      const visible = items.slice(0, S.shown[key]);
      const body = h("div", { class: "det-list" },
        collapsed ? null : items.length
          ? visible.map((d) => detItem(d)).concat(
              items.length > visible.length
                ? [h("div", { style: "text-align:center;padding:6px" },
                    h("button", { class: "btn sm", "data-more": "1", onclick: () => { S.shown[key] += 12; paintDetections(); } }, "بیشتر"))
                  ]
                : []
            )
          : [markEmpty(empty(emptyText))]
      );
      if (!collapsed) {
        // tag rows so the in-place updater can patch instead of rebuild
        body.querySelectorAll(".det-item").forEach((el, i) => {
          const d = visible[i];
          if (!d) return;
          el.dataset.track = String(d.track_id);
          el.dataset.key = trackRenderKey(d);
        });
      }
      const head = h("div", { class: "card-head" },
        h("span", {}, title),
        h("span", { class: "badge" + (items.length ? " accent" : "") }, I.num(items.length)),
        h("div", { class: "spacer" }),
        h("button", {
          class: "btn ghost sm",
          onclick: () => {
            store.state.detPanel[key] = !store.state.detPanel[key];
            refresh();
          },
        }, collapsed ? "گستردن" : "جمع کردن")
      );
      return h("div", { class: "card", "data-panel": key }, head, collapsed ? null : body);
    };

    root.appendChild(panel("identified", "اشخاص شناسایی‌شده", identified, "هنوز شخصی شناسایی نشده"));
    root.appendChild(panel("unknown", "اشخاص ناشناس", unknown.concat(analysing), "فعلاً کسی در تصویر نیست"));
  }

  function detItem(d) {
    const cam = (store.state.cameras || []).find((c) => String(c.address || c.id) === String(d.camera_key));
    const active = S.selected && S.selected.track_id === d.track_id;
    return h("div", {
      class: "det-item" + (active ? " active" : ""),
      onclick: () => { S.selected = d; openDetail(d, cam); },
    },
      h("div", { style: "width:46px;height:46px;border-radius:7px;background:var(--bg-inset);display:grid;place-items:center;flex:none;font-size:19px" },
        d.state === "identified" ? "\u{1F464}" : d.state === "unknown" ? "\u2753" : "\u{1F50D}"),
      h("div", { class: "info" },
        h("div", { class: "n" }, d.state === "identified" ? d.identity_name : d.state === "unknown" ? L.unknown : L.analysing),
        h("div", { class: "m" },
          `\u062A\u0631\u06A9 ${I.num(d.track_id)} \u00B7 ` +
          `${I.pct(d.score)} \u00B7 ` +
          `${cam ? cam.name : d.camera_key}` +
          (d.zone ? ` \u00B7 ${d.zone}` : "") +
          (d.state === "analysing" ? ` \u00B7 ${I.num(d.samples)} \u0646\u0645\u0648\u0646\u0647` : ""))
      ),
      stateBadge(d.state, d.identity_name)
    );
  }

  // --------------------------------------------------------- detail panel
  //
  // The panel shows the newest stored event for this track *of this camera*,
  // and nothing at all when the track has no event yet.  It used to fall back
  // to the newest event on any camera, so an unknown person's card showed the
  // face of a completely different person - and registering from that card
  // would have enrolled the wrong face.
  async function openDetail(d, cam) {
    const body = h("div", {}, loading());
    const isAdmin = store.state.user && store.state.user.role === "admin";
    const m = modal({
      title: d.state === "identified" ? d.identity_name : d.state === "unknown" ? L.unknown : L.analysing,
      size: "lg",
      body,
      actions: [{ label: "\u0628\u0633\u062A\u0646", kind: "ghost", onClick: (c) => c() }],
    });

    let ev = null;
    try {
      const res = await window.API.get("/api/events", {
        page: 1, page_size: 40, camera_id: cam ? cam.id : undefined,
      });
      const items = res.items || [];
      ev = items.find((e) => Number(e.track_id) === Number(d.track_id)) || null;
      // An identified person's event is written the moment the vote resolves;
      // for an "analysing" track there is nothing to show yet, and inventing
      // one would be worse than an empty panel.
      if (!ev && d.state === "identified") {
        ev = items.find((e) => e.identity_name === d.identity_name) || null;
      }
    } catch (e) { /* detail still renders without artefacts */ }

    let identity = null;
    if (ev && ev.identity_id) {
      try { identity = (await window.API.get("/api/events/" + ev.id)).identity; } catch (e) { identity = null; }
    }

    clear(body);
    const faceImg = ev ? window.API.mediaUrl(ev.face_file) : "";
    const frameImg = ev ? window.API.mediaUrl(ev.frame_file) : "";
    const avatarImg = identity ? window.API.mediaUrl(identity.avatar_file || identity.reference_file) : "";

    const row = (k, v) => h("tr", {}, h("td", { class: "muted" }, k), h("td", {}, v));

    body.appendChild(h("div", { class: "grid-2" },
      h("div", {},
        h("table", { class: "data" },
          h("tbody", {},
            row("\u0648\u0636\u0639\u06CC\u062A", stateBadge(d.state, d.identity_name)),
            row("\u0698\u062A\u0648\u0646", identity ? roleBadge(identity.role) : roleBadge("unknown")),
            row("\u0634\u0645\u0627\u0631\u0647 \u062A\u0631\u06A9", I.num(d.track_id)),
            row("\u0634\u0628\u0627\u0647\u062A", I.pct(d.score, 1)),
            row("\u0633\u0646 \u062A\u0642\u0631\u06CC\u0628\u06CC", d.age !== null && d.age !== undefined ? I.num(d.age) : "\u2014"),
            row("\u062C\u0646\u0633\u06CC\u062A", L[d.gender] || L.notset),
            row("\u062F\u0648\u0631\u0628\u06CC\u0646", cam ? cam.name : d.camera_key),
            row("\u0646\u0627\u062D\u06CC\u0647", d.zone || "\u2014"),
            ev ? row("\u0632\u0645\u0627\u0646", I.formatDateTime(ev.event_date, ev.event_time)) : null,
            row("\u0634\u0645\u0627\u0631\u0647 \u0645\u0644\u06CC", identity && identity.national_number ? I.num(identity.national_number) : "\u2014"),
            row("\u062A\u0648\u0636\u06CC\u062D", identity && identity.description ? identity.description : "\u2014"),
          )
        )
      ),
      h("div", {},
        h("div", { class: "grid-2" },
          h("div", {}, h("div", { class: "muted", style: "font-size:12px;margin-bottom:4px" }, "\u0686\u0647\u0631\u0647 \u0645\u0634\u0627\u0647\u062F\u0647\u200C\u0634\u062F\u0647"),
            h("div", { class: "img-box", style: "aspect-ratio:1/1" },
              faceImg
                ? h("img", { src: faceImg, alt: "detected face", style: "width:100%;height:100%;object-fit:contain;cursor:zoom-in", onclick: () => zoomImage(faceImg) })
                : empty("\u062F\u0631 \u0627\u0646\u062A\u0638\u0627\u0631 \u062A\u062B\u0628\u062A \u0631\u0648\u06CC\u062F\u0627\u062F"))),
          h("div", {}, h("div", { class: "muted", style: "font-size:12px;margin-bottom:4px" }, "\u0622\u0648\u0627\u062A\u0627\u0631 \u062B\u0628\u062A\u200C\u0634\u062F\u0647"),
            h("div", { class: "img-box", style: "aspect-ratio:1/1" },
              avatarImg
                ? h("img", { src: avatarImg, alt: "enrolled avatar", style: "width:100%;height:100%;object-fit:cover" })
                : empty("\u2014")))
        )
      )
    ));

    if (frameImg) {
      body.appendChild(h("div", { style: "margin-top:12px" },
        h("div", { class: "muted", style: "font-size:12px;margin-bottom:4px" }, "\u06A9\u0627\u062F\u0631 \u06A9\u0627\u0645\u0644"),
        h("div", { class: "img-box contain", style: "max-height:320px" },
          h("img", { src: frameImg, alt: "frame", style: "max-height:320px;cursor:zoom-in", onclick: () => zoomImage(frameImg) }))
      ));
    }

    const actions = h("div", { style: "display:flex;gap:8px;margin-top:12px;flex-wrap:wrap" },
      frameImg ? h("button", { class: "btn sm", onclick: () => fullscreen(frameImg) }, "\u0646\u0645\u0627\u06CC\u0634 \u062A\u0645\u0627\u0645\u200C\u0635\u0641\u062D\u0647") : null,
      faceImg ? h("button", { class: "btn sm", onclick: () => zoomImage(faceImg) }, "\u0628\u0632\u0631\u06AF\u200C\u0646\u0645\u0627\u06CC\u06CC \u0686\u0647\u0631\u0647") : null,
      ev ? h("a", { class: "btn sm", href: "/api/events/" + ev.id + "/download" }, "\u062F\u0627\u0646\u0644\u0648\u062F") : null,
      // Register the person straight from here.  Works for an unknown (or still
      // analysing) track: the frame is captured from the camera right now and
      // its faces detected, so the operator picks the face they can see.
      isAdmin
        ? h("button", { class: "btn sm primary", onclick: () => registerFromDetection(d, cam, ev) },
            "\u2795 \u062B\u0628\u062A \u0627\u06CC\u0646 \u0634\u062E\u0635 \u062F\u0631 \u0641\u0647\u0631\u0633\u062A")
        : null,
      ev && identity && isAdmin
        ? h("button", { class: "btn sm", onclick: () => { m.close(); window.PagePeople.openPerson(identity.id); } },
            "\u06A9\u0627\u0631\u062A \u0634\u062E\u0635")
        : null,
      ev && isAdmin
        ? h("button", { class: "btn sm danger", onclick: async () => {
            const ok = await window.UI.confirmDialog({
              title: "\u062D\u0630\u0641 \u0631\u0648\u06CC\u062F\u0627\u062F",
              message: "\u0622\u06CC\u0627 \u0627\u0632 \u062D\u0630\u0641 \u0627\u06CC\u0646 \u0631\u0648\u06CC\u062F\u0627\u062F \u0645\u0637\u0645\u0626\u0646 \u0647\u0633\u062A\u06CC\u062F\u061F \u0627\u06CC\u0646 \u06A9\u0627\u0631 \u0628\u0627\u0632\u06AF\u0634\u062A\u200C\u0646\u0627\u067E\u0630\u06CC\u0631 \u0627\u0633\u062A.",
              danger: true, confirmLabel: "\u062D\u0630\u0641",
            });
            if (!ok) return;
            try {
              await window.API.del("/api/events/" + ev.id);
              toast("\u0631\u0648\u06CC\u062F\u0627\u062F \u062D\u0630\u0641 \u0634\u062F", "ok");
              m.close();
            } catch (err) { toast("\u062E\u0637\u0627: " + err.message, "danger"); }
          } }, "\u062D\u0630\u0641 \u0631\u0648\u06CC\u062F\u0627\u062F")
        : null
    );
    body.appendChild(actions);
  }

  // Grab the current frame of this camera, detect its faces and hand them to
  // the enrolment dialog - the operator never leaves the dashboard.
  async function registerFromDetection(d, cam, ev) {
    const camId = cam ? cam.id : Number(d.camera_id || 0);
    let faces = [];
    let preview = "";
    let still = "";
    if (camId) {
      try {
        const res = await window.API.post("/api/enrolment/capture", { camera_id: camId });
        faces = res.faces || [];
        preview = res.preview || "";
        still = res.still || "";
      } catch (e) {
        toast("\u06AF\u0631\u0641\u062A\u0646 \u0642\u0627\u0628 \u062F\u0648\u0631\u0628\u06CC\u0646 \u0646\u0627\u0645\u0648\u0641\u0642: " + e.message, "warn");
      }
    }
    if (!still && ev && ev.frame_file) {
      // camera unreachable: fall back to the stored frame of this event
      const url = window.API.mediaUrl(ev.frame_file);
      try {
        const blob = await (await fetch(url, { credentials: "same-origin" })).blob();
        still = await new Promise((resolve) => {
          const r = new FileReader();
          r.onload = () => resolve(String(r.result || ""));
          r.onerror = () => resolve("");
          r.readAsDataURL(blob);
        });
      } catch (e) { /* nothing to fall back to */ }
    }
    if (!still) {
      toast("\u0642\u0627\u0628\u06CC \u0628\u0631\u0627\u06CC \u062B\u0628\u062A \u062F\u0631 \u062F\u0633\u062A\u0631\u0633 \u0646\u06CC\u0633\u062A", "warn");
      return;
    }
    window.PagePeople.openEnrolFromDetection({
      still: still,
      faces: faces,
      preview: preview,
      cameraId: camId,
      age: d.age,
      gender: d.gender,
      onSaved: () => { loadDetections(); },
    });
  }

  function fullscreen(src) {
    modal({
      title: "\u0646\u0645\u0627\u06CC\u0634 \u062A\u0645\u0627\u0645\u200C\u0635\u0641\u062D\u0647",
      size: "lg",
      body: h("div", { class: "img-box" }, h("img", { src, alt: "full", style: "width:100%;object-fit:contain" })),
      actions: [{ label: "\u0628\u0633\u062A\u0646", kind: "ghost", onClick: (c) => c() }],
    });
  }

  function zoomImage(src) {
    if (!src) return null;
    // the shared zoom stage: wheel to zoom, drag to pan, double-click to fit
    const z = window.IMGW.zoomable(src, { maxHeight: "62vh" });
    return modal({
      title: "بزرگ‌نمایی",
      size: "lg",
      body: z.el,
      actions: [{ label: "بستن", kind: "ghost", onClick: (c) => c() }],
    });
  }

  // ------------------------------------------------------------------ page
  let mounted = false;
  let root;

  function refresh() {
    if (!root) return;
    const mosaicHost = root.querySelector("#mosaic-host");
    // The mosaic is rebuilt only when its structure changes.  Rebuilding it on
    // every status poll used to restart every live frame (the "stuttering").
    const cams = store.state.cameras || [];
    const sig = cams.map((c) => c.id + ":" + c.name).join("|") + "|" + (S.expanded || "");
    if (mosaicHost && S.mosaicSig !== sig) {
      S.mosaicSig = sig;
      Object.keys(S.live).forEach((k) => stopLive(k));
      S.live = {};
      renderMosaic(mosaicHost);
      syncLiveLoops();
    }
    if (S.detHost) {
      if (!S.detHost.querySelector("[data-panel]")) {
        clear(S.detHost);
        detectionPanels(S.detHost);
      }
      paintDetections();
    }
  }

  async function loadCameras() {
    try {
      const res = await window.API.get("/api/cameras");
      store.set({ cameras: res.items || [] });
      refresh();
      syncLiveLoops();
      updateCameraBadges();
    } catch (e) {
      toast("\u062F\u0631\u06CC\u0627\u0641\u062A \u062F\u0648\u0631\u0628\u06CC\u0646\u200C\u0647\u0627 \u0646\u0627\u0645\u0648\u0641\u0642: " + e.message, "danger");
    }
  }

  // Camera status changed (or was polled): refresh the tile header in place so
  // the mosaic is not rebuilt and its frames never stop.
  function updateCameraBadges() {
    (store.state.cameras || []).forEach((cam) => {
      const tile = root && root.querySelector(`[data-tile="${cam.id}"]`);
      if (!tile) return;
      const dot = tile.querySelector(".tile-head .dot");
      const state = cam.status || "idle";
      if (dot) dot.className = "dot " + (state === "streaming" ? "live" : state === "offline" ? "danger" : "warn");
      const fps = tile.querySelector(`[data-fps="${cam.id}"]`);
      if (fps) fps.textContent = I.num(cam.fps || 0, 1) + " fps";
      const overlay = tile.querySelector(".overlay-msg");
      if (state === "streaming") {
        if (overlay) overlay.remove();
        if (!tile.querySelector(".live-dot")) {
          const box = tile.querySelector(".video-box");
          if (box) box.insertBefore(h("div", { class: "live-dot" }, h("i"), "LIVE"), box.firstChild);
        }
      } else if (overlay) {
        overlay.textContent = state === "offline" ? (cam.last_error || "دوربین آفلاین است") : "در حال اتصال…";
      }
    });
  }

  async function loadDetections() {
    try {
      const res = await window.API.get("/api/live/detections");
      if (res.changed === false) return;
      if (res.version && res.version === S.version) return;
      S.version = res.version || "";
      S.detections = (res.tracks || []).filter((t) => !t.stale);
      paintDetections();
    } catch (e) { /* transient: the next tick retries */ }
  }

  async function mount(host) {
    root = host;
    clear(root);
    root.appendChild(h("div", { class: "page-head" },
      h("h1", {}, "\u0645\u0627\u0646\u06CC\u062A\u0648\u0631\u06CC\u0646\u06AF"),
      h("span", { class: "sub" }, "\u0646\u0645\u0627\u06CC\u0634 \u0632\u0646\u062F\u0647 \u062F\u0648\u0631\u0628\u06CC\u0646\u200C\u0647\u0627 \u0648 \u062A\u062D\u0644\u06CC\u0644 \u0647\u0648\u06CC\u062A"),
      h("div", { style: "flex:1" }),
      h("button", { class: "btn sm", onclick: loadCameras }, "\u0628\u0627\u0632\u0628\u0627\u0631\u06AF\u0630\u0627\u0631\u06CC")
    ));
    const mosaicHost = h("div", { id: "mosaic-host" }, loading());
    const detHost = h("div", { id: "det-host", class: "det-panels", style: "margin-top:14px" });
    root.appendChild(mosaicHost);
    root.appendChild(detHost);
    S.detHost = detHost;
    mounted = true;
    await loadCameras();
    await loadDetections();
    startTimers();
  }

  function unmount() {
    mounted = false;
    stopTimers();
    // The live loops are bound to DOM nodes that are about to disappear; stop
    // them so a page switch cannot leave timers or fetches running (that leak
    // was why a long session slowly crawled).
    Object.keys(S.live).forEach((k) => stopLive(k));
    S.live = {};
    S.rendered = {};
    S.version = "";
    S.detHost = null;
    root = null;
  }

  // ---- detection panels: targeted repaint ------------------------------
  // Rebuilding the whole list on every poll threw away scroll position and
  // focus, so a track that moved by two pixels made the panel jump.  Rows are
  // now keyed by track id and only the changed text nodes are touched.
  function panelFor(key) {
    if (!S.detHost) return null;
    return S.detHost.querySelector(`[data-panel="${key}"]`);
  }

  function paintDetections() {
    if (!S.detHost) return;
    const identified = S.detections.filter((d) => d.state === "identified");
    const unknown = S.detections.filter((d) => d.state === "unknown");
    const analysing = S.detections.filter((d) => d.state === "analysing");

    if (!S.detHost.querySelector("[data-panel]")) {
      // First paint (or a mount): build the two panels, then fill them - the
      // builder already renders the rows from S.detections.
      clear(S.detHost);
      detectionPanels(S.detHost);
      return;
    }
    updatePanel("identified", identified, "\u0647\u0646\u0648\u0632 \u0634\u062E\u0635\u06CC \u0634\u0646\u0627\u0633\u0627\u06CC\u06CC \u0646\u0634\u062F\u0647");
    updatePanel("unknown", unknown.concat(analysing), "\u0641\u0639\u0644\u0627\u064B \u06A9\u0633\u06CC \u062F\u0631 \u062A\u0635\u0648\u06CC\u0631 \u0646\u06CC\u0633\u062A");
  }

  function updatePanel(key, items, emptyText) {
    const panel = panelFor(key);
    if (!panel) return;
    const badge = panel.querySelector(".card-head .badge");
    if (badge) badge.textContent = I.num(items.length);
    const list = panel.querySelector(".det-list");
    if (!list) return;
    const body = list.querySelector("[data-empty]");
    if (body) body.remove();
    if (!items.length) {
      const prior = list.querySelector("[data-empty]");
      if (!prior) {
        clear(list);
        const e = empty(emptyText);
        e.dataset.empty = "1";
        list.appendChild(e);
      }
      S.rendered = {};
      return;
    }
    // drop rows whose track disappeared
    const alive = new Set(items.map((d) => String(d.track_id)));
    Array.from(list.children).forEach((el) => {
      if (el.dataset && el.dataset.track && !alive.has(el.dataset.track)) el.remove();
    });
    const visible = items.slice(0, S.shown[key]);
    visible.forEach((d) => {
      const tip = trackRenderKey(d);
      const existing = list.querySelector(`[data-track="${d.track_id}"]`);
      if (!existing) {
        const node = detItem(d);
        node.dataset.track = String(d.track_id);
        node.dataset.key = tip;
        list.appendChild(node);
        return;
      }
      if (existing.dataset.key !== tip) {
        const fresh = detItem(d);
        fresh.dataset.track = String(d.track_id);
        fresh.dataset.key = tip;
        list.replaceChild(fresh, existing);
      }
    });
    // "more" button
    const more = list.querySelector("[data-more]");
    if (items.length > visible.length) {
      if (!more) {
        const btn = h("button", { class: "btn sm", "data-more": "1", onclick: () => { S.shown[key] += 12; paintDetections(); } }, "\u0628\u06CC\u0634\u062A\u0631");
        list.appendChild(h("div", { style: "text-align:center;padding:6px" }, btn));
      }
    } else if (more) {
      more.parentElement.remove();
    }
  }

  // Only values whose change actually alters the rendered row belong here.
  // ``samples``/``updated`` change every tick while a person is on screen, so
  // including them made every row "dirty" on every poll and the list was
  // rebuilt twice a second - the jumpiness this repaint was meant to fix.
  function trackRenderKey(d) {
    const shown = d.state === "identified" ? d.identity_name || "" : d.state;
    return [d.state, shown, d.track_id, Math.round((d.score || 0) * 100), d.zone || ""].join("|");
  }

  // ---- live frames ------------------------------------------------------
  // One short JPEG request loop per tile: a frame is requested only when the
  // previous one has arrived (never more than one in flight), so a slow camera
  // cannot pile up requests.  The last good frame stays on screen while the
  // next one loads - no flicker, no "white tile".
  function frameInterval() {
    const n = Math.max(1, Object.keys(S.live).length);
    if (n <= 1) return 70;
    if (n <= 4) return 110;
    return 160;
  }

  function startLive(camId) {
    const entry = S.live[camId];
    if (!entry || entry.timer) return;
    const tick = () => {
      if (!mounted || entry.busy) return;
      const el = entry.el;
      if (!el || !el.isConnected) return;
      entry.busy = true;
      const isAdmin = store.state.user && store.state.user.role === "admin";
      const flavour = isAdmin ? (S.flavour[camId] || "annotated") : "raw";
      const probe = new Image();
      probe.decoding = "async";
      const done = () => { entry.busy = false; };
      probe.onload = () => {
        done();
        if (!mounted || !el.isConnected) return;
        el.src = probe.src;
      };
      probe.onerror = () => { done(); };
      probe.src = `/video/${camId}/snapshot?flavour=${flavour}&t=${Date.now()}`;
      entry.timer = setTimeout(tick, frameInterval());
    };
    entry.timer = setTimeout(tick, 10);
  }

  function stopLive(camId) {
    const entry = S.live[camId];
    if (!entry) return;
    if (entry.timer) clearTimeout(entry.timer);
    entry.timer = null;
    entry.busy = false;
  }

  function syncLiveLoops() {
    const wanted = new Set((store.state.cameras || []).map((c) => c.id));
    Object.keys(S.live).forEach((k) => {
      if (!wanted.has(Number(k)) || !S.live[k].el.isConnected) {
        stopLive(k);
        delete S.live[k];
      }
    });
    Object.keys(S.live).forEach((k) => startLive(k));
  }

  // ---- timers -----------------------------------------------------------
  function startTimers() {
    stopTimers();
    // detections: 4 Hz - the backend answers "unchanged" when nothing moved
    S.detTimer = setInterval(() => { if (mounted) loadDetections(); }, 260);
    // camera status (fps / online): every 4 s, in place
    S.camTimer = setInterval(async () => {
      if (!mounted) return;
      try {
        const res = await window.API.get("/api/cameras");
        store.set({ cameras: res.items || [] });
        updateCameraBadges();
        syncLiveLoops();
      } catch (e) { /* transient */ }
    }, 4000);
  }

  function stopTimers() {
    if (S.detTimer) clearInterval(S.detTimer);
    if (S.camTimer) clearInterval(S.camTimer);
    S.detTimer = null;
    S.camTimer = null;
  }

  // push updates: a new event or a camera change arrives over SSE
  window.addEventListener("facw:change", (ev) => {
    if (!mounted) return;
    const d = ev.detail || {};
    if (d.topic === "events" && d.data && d.data.action === "created") {
      // instant: ask for the tracks right away instead of waiting for the tick
      loadDetections();
      if (d.data.id && d.data.id !== S.lastEventId) S.lastEventId = d.data.id;
    }
    if (d.topic === "cameras") loadCameras();
    if (d.topic === "identities") loadDetections();
  });
  window.addEventListener("facw:alarm", () => alertChime());

  window.PageDashboard = { mount, unmount, loadDetections, state: S };
})();
