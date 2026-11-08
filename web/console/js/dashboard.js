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

      // Tiles poll snapshots rather than holding one MJPEG connection each.
      // Browsers allow only ~6 connections per host, so six live multipart
      // streams starve every other API call (the console then appears frozen).
      const img = h("img", { alt: cam.name, src: "", "data-cam": cam.id });

      const stateDot = h("span", { class: "dot " + (state === "streaming" ? "live" : state === "offline" ? "danger" : "warn") });

      const tile = h("div", { class: "tile" + (expanded ? " expanded" : "") },
        h("div", { class: "tile-head" },
          stateDot,
          h("span", { class: "name" }, cam.name),
          cam.gate_label ? h("span", { class: "badge" }, cam.gate_label) : null,
          h("div", { class: "spacer" }),
          h("span", { class: "mono muted", style: "font-size:11.5px" }, I.num(cam.fps || 0, 1) + " fps"),
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
          state !== "streaming" ? h("div", { class: "overlay-msg" },
            state === "offline"
              ? (cam.last_error || "\u062F\u0648\u0631\u0628\u06CC\u0646 \u0622\u0641\u0644\u0627\u06CC\u0646 \u0627\u0633\u062A")
              : "\u062F\u0631 \u062D\u0627\u0644 \u0627\u062A\u0635\u0627\u0644\u2026") : null
        )
      );
      mosaic.appendChild(tile);
    });
    root.appendChild(mosaic);
  }

  // ------------------------------------------------------------ detections
  function detectionPanels(root) {
    const identified = S.detections.filter((d) => d.state === "identified");
    const unknown = S.detections.filter((d) => d.state === "unknown");
    const analysing = S.detections.filter((d) => d.state === "analysing");

    const panel = (key, title, items, emptyText) => {
      const collapsed = !store.state.detPanel[key];
      const visible = items.slice(0, S.shown[key]);
      const body = h("div", { class: "det-list" },
        collapsed ? null : items.length
          ? visible.map((d) => detItem(d)).concat(
              items.length > visible.length
                ? [h("div", { style: "text-align:center;padding:6px" },
                    h("button", { class: "btn sm", onclick: () => { S.shown[key] += 12; refresh(); } }, "\u0628\u06CC\u0634\u062A\u0631"))
                  ]
                : []
            )
          : [empty(emptyText)]
      );
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
        }, collapsed ? "\u06AF\u0633\u062A\u0631\u062F\u0646" : "\u062C\u0645\u0639 \u06A9\u0631\u062F\u0646")
      );
      return h("div", { class: "card" }, head, collapsed ? null : body);
    };

    root.appendChild(panel("identified", "\u0627\u0634\u062E\u0627\u0635 \u0634\u0646\u0627\u0633\u0627\u06CC\u06CC\u200C\u0634\u062F\u0647", identified, "\u0647\u0646\u0648\u0632 \u0634\u062E\u0635\u06CC \u0634\u0646\u0627\u0633\u0627\u06CC\u06CC \u0646\u0634\u062F\u0647"));
    root.appendChild(panel("unknown", "\u0627\u0634\u062E\u0627\u0635 \u0646\u0627\u0634\u0646\u0627\u0633", unknown.concat(analysing), "\u0641\u0639\u0644\u0627\u064B \u06A9\u0633\u06CC \u062F\u0631 \u062A\u0635\u0648\u06CC\u0631 \u0646\u06CC\u0633\u062A"));
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
  async function openDetail(d, cam) {
    const body = h("div", {}, loading());
    const m = modal({
      title: d.state === "identified" ? d.identity_name : d.state === "unknown" ? L.unknown : L.analysing,
      size: "lg",
      body,
      actions: [{ label: "\u0628\u0633\u062A\u0646", kind: "ghost", onClick: (c) => c() }],
    });

    // find the most recent stored event for this track to get artefacts
    let ev = null;
    try {
      const res = await window.API.get("/api/events", { page: 1, page_size: 10, camera_id: cam ? cam.id : undefined });
      ev = (res.items || []).find((e) => Number(e.track_id) === Number(d.track_id)) || (res.items || [])[0] || null;
    } catch (e) { /* detail still renders without artefacts */ }

    let identity = null;
    if (ev && ev.identity_id) {
      try { identity = (await window.API.get("/api/events/" + ev.id)).identity; } catch (e) { identity = null; }
    }

    clear(body);
    const faceImg = ev ? window.API.mediaUrl(ev.face_file) : "";
    const frameImg = ev ? window.API.mediaUrl(ev.frame_file) : "";
    const personImg = ev ? window.API.mediaUrl(ev.person_file) : "";
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
            h("div", { class: "img-box" }, faceImg ? h("img", { src: faceImg, alt: "detected face", style: "cursor:zoom-in", onclick: () => fullscreen(faceImg) }) : empty("\u0646\u0627\u0645\u0648\u062C\u0648\u062F"))),
          h("div", {}, h("div", { class: "muted", style: "font-size:12px;margin-bottom:4px" }, "\u0622\u0648\u0627\u062A\u0627\u0631 \u062B\u0628\u062A\u200C\u0634\u062F\u0647"),
            h("div", { class: "img-box" }, avatarImg ? h("img", { src: avatarImg, alt: "enrolled avatar" }) : empty("\u2014")))
        )
      )
    ));

    body.appendChild(h("div", { style: "margin-top:12px" },
      h("div", { class: "muted", style: "font-size:12px;margin-bottom:4px" }, "\u06A9\u0627\u062F\u0631 \u06A9\u0627\u0645\u0644"),
      h("div", { class: "img-box", style: "max-height:340px;overflow:hidden" },
        frameImg ? h("img", { src: frameImg, alt: "frame", style: "cursor:zoom-in;max-height:340px;object-fit:contain;width:100%", onclick: () => fullscreen(frameImg) }) : empty("\u0646\u0627\u0645\u0648\u062C\u0648\u062F"))
    ));

    const actions = h("div", { style: "display:flex;gap:8px;margin-top:12px;flex-wrap:wrap" },
      frameImg ? h("button", { class: "btn sm", onclick: () => fullscreen(frameImg) }, "\u0646\u0645\u0627\u06CC\u0634 \u062A\u0645\u0627\u0645\u200C\u0635\u0641\u062D\u0647") : null,
      ev ? h("a", { class: "btn sm", href: "/api/events/" + ev.id + "/download" }, "\u062F\u0627\u0646\u0644\u0648\u062F") : null,
      ev ? h("button", { class: "btn sm", onclick: () => zoomImage(frameImg) }, "\u0628\u0632\u0631\u06AF\u200C\u0646\u0645\u0627\u06CC\u06CC \u062A\u0635\u0648\u06CC\u0631") : null,
      ev && store.state.user && store.state.user.role === "admin"
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

  function fullscreen(src) {
    modal({
      title: "\u0646\u0645\u0627\u06CC\u0634 \u062A\u0645\u0627\u0645\u200C\u0635\u0641\u062D\u0647",
      size: "lg",
      body: h("div", { class: "img-box" }, h("img", { src, alt: "full", style: "width:100%;object-fit:contain" })),
      actions: [{ label: "\u0628\u0633\u062A\u0646", kind: "ghost", onClick: (c) => c() }],
    });
  }

  function zoomImage(src) {
    let scale = 1;
    const img = h("img", { src, alt: "zoom", style: "width:100%;transition:transform 120ms;transform-origin:center" });
    const label = h("span", { class: "badge" }, "100\u066A");
    const apply = () => {
      img.style.transform = `scale(${scale})`;
      label.textContent = I.toFa(Math.round(scale * 100)) + "\u066A";
    };
    const m = modal({
      title: "\u0628\u0632\u0631\u06AF\u200C\u0646\u0645\u0627\u06CC\u06CC",
      body: h("div", { class: "img-box", style: "overflow:auto;max-height:70vh" }, img),
      footerExtra: label,
      actions: [
        { label: "\u2212", onClick: () => { scale = Math.max(0.4, scale - 0.2); apply(); } },
        { label: "+", onClick: () => { scale = Math.min(4, scale + 0.2); apply(); } },
        { label: "\u0628\u0633\u062A\u0646", kind: "ghost", onClick: (c) => c() },
      ],
    });
    apply();
    return m;
  }

  // ------------------------------------------------------------------ page
  let mounted = false;
  let root;

  function refresh() {
    if (!root) return;
    const mosaicHost = root.querySelector("#mosaic-host");

    const detHost = root.querySelector("#det-host");
    if (detHost) {
      clear(detHost);
      detectionPanels(detHost);
    }
    if (mosaicHost && !S.expanded) {
      // avoid rebuilding the mosaic while a tile is expanded (it reconnects streams)
      renderMosaic(mosaicHost);
    } else if (mosaicHost) {
      renderMosaic(mosaicHost);
    }
  }

  async function loadCameras() {
    try {
      const res = await window.API.get("/api/cameras");
      store.set({ cameras: res.items || [] });
      refresh();
    } catch (e) {
      toast("\u062F\u0631\u06CC\u0627\u0641\u062A \u062F\u0648\u0631\u0628\u06CC\u0646\u200C\u0647\u0627 \u0646\u0627\u0645\u0648\u0641\u0642: " + e.message, "danger");
    }
  }

  async function loadDetections() {
    try {
      const res = await window.API.get("/api/live/detections");
      S.detections = res.tracks || [];
      const detHost = root && root.querySelector("#det-host");
      if (detHost) { clear(detHost); detectionPanels(detHost); }
    } catch (e) { /* transient */ }
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
    mounted = true;
    await loadCameras();
    startSnapshots();
    await loadDetections();
  }

  function unmount() {
    mounted = false;
    root = null;
    Object.values(S.timers || {}).forEach((t) => clearInterval(t));
    S.timers = {};
  }

  // ---- snapshot polling -------------------------------------------------
  // One timer per visible tile, but requests are spaced and skipped when a
  // previous request for that tile is still in flight.  This keeps the number
  // of concurrent connections bounded no matter how many cameras exist.
  S.timers = S.timers || {};
  S.inflight = S.inflight || {};

  function startSnapshots() {
    (store.state.cameras || []).forEach((cam) => {
      if (S.timers[cam.id]) return;
      const tick = () => {
        if (!mounted) return;
        if (S.inflight[cam.id]) return;
        const el = document.querySelector(`img[data-cam="${cam.id}"]`);
        if (!el) return;
        S.inflight[cam.id] = true;
        const flavour = (store.state.user && store.state.user.role === "admin" && S.flavour[cam.id]) || "raw";
        const url = `/video/${cam.id}/snapshot?flavour=${flavour}&t=${Date.now()}`;
        const probe = new Image();
        probe.onload = () => {
          S.inflight[cam.id] = false;
          if (el.isConnected) { el.src = probe.src; el.dataset.failed = ""; }
        };
        probe.onerror = () => {
          S.inflight[cam.id] = false;
          el.dataset.failed = "1";
        };
        probe.src = url;
      };
      tick();
      S.timers[cam.id] = setInterval(tick, 900);
    });
  }

  // live updates: cameras status + detections
  window.addEventListener("facw:change", (ev) => {
    if (!mounted) return;
    const d = ev.detail || {};
    if (d.topic === "events" && d.data && d.data.action === "created") {
      loadDetections();
      loadCameras();
    }
    if (d.topic === "cameras") loadCameras();
  });
  window.addEventListener("facw:alarm", () => alertChime());

  window.PageDashboard = { mount, unmount, loadDetections, state: S };
})();
