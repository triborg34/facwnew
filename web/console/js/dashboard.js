/* Aman Negar console - monitoring dashboard: mosaic, detections, detail panel. */
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
    resTimer: null,        // resource-chart poll
    resPanel: null,        // window.Charts.resourcePanel() instance
    recent: {},            // "identified" | "unknown" -> recent-faces widget
    recentHost: null,
    recentBusy: false,
    statsPanel: null,      // window.Charts.statsPanel() instance
    statsTimer: null,      // overall-statistics poll
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
      // frozen).
      //
      // Two stacked <img> elements are used as a double buffer: the next frame
      // is decoded in the hidden one and only then swapped to the front.
      // Assigning .src on a single visible <img> makes the browser discard the
      // current bitmap immediately, which is the black flash every second.
      const img = h("img", { alt: cam.name, src: "", "data-cam": cam.id, decoding: "async", class: "frame-a front" });
      const imgAlt = h("img", { alt: "", src: "", "data-cam": cam.id, decoding: "async", class: "frame-b", "aria-hidden": "true" });

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
                  // the mosaic was rebuilt, so its old entries are gone: reopen
                  S.mosaicSig = null;
                  refreshMosaic();
                },
              }, flavour === "annotated" ? "\u{1F9E0}" : "\u{1F4F7}")
            : null,
          h("button", {
            class: "btn ghost sm",
            title: expanded ? "\u0628\u0633\u062A\u0646" : "\u0628\u0632\u0631\u06AF\u200C\u0646\u0645\u0627\u06CC\u06CC",
            onclick: (ev) => { ev.stopPropagation(); S.expanded = expanded ? null : cam.id; renderMosaic(root); },
          }, expanded ? "\u2715" : "\u26F6")
        ),
        h("div", { class: "video-box" }, imgAlt, img,
          state === "streaming" ? h("div", { class: "live-dot" }, h("i"), "LIVE") : null,
          state !== "streaming" ? h("div", { class: "overlay-msg" },
            state === "offline"
              ? (cam.last_error || "\u062F\u0648\u0631\u0628\u06CC\u0646 \u0622\u0641\u0644\u0627\u06CC\u0646 \u0627\u0633\u062A")
              : "\u062F\u0631 \u062D\u0627\u0644 \u0627\u062A\u0635\u0627\u0644\u2026") : null
        )
      );
      S.live[cam.id] = { cam: cam, el: img, alt: imgAlt, front: img };
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

  // ------------------------------------------------------- recent faces
  //
  // Two panels of the *stored* faces, newest first: the ones the system
  // recognised and the ones it could not.  They are deliberately separate
  // from the live detection panels above, which show the tracks currently on
  // screen (and carry no picture at all).
  const RECENT_STEP = 10;

  function recentFacesPanel(kind) {
    const st = { items: [], total: 0, limit: RECENT_STEP, loading: false };
    const grid = h("div", { class: "face-grid" });
    const badge = h("span", { class: "badge" }, I.num(0));
    const moreBtn = h("button", {
      class: "btn sm",
      onclick: () => { st.limit += RECENT_STEP; load(); },
    }, "بارگذاری بیشتر");
    const moreWrap = h("div", { class: "face-more" }, moreBtn);

    const el = h("div", { class: "card", "data-recent": kind },
      h("div", { class: "card-head" },
        h("span", {}, kind === "identified" ? "آخرین چهره‌های شناسایی‌شده" : "آخرین چهره‌های ناشناس"),
        badge,
        h("div", { class: "spacer" })),
      grid);

    async function load() {
      if (st.loading) return;
      st.loading = true;
      try {
        const q = kind === "identified" ? { identified_only: 1 } : { unknown_only: 1 };
        const res = await window.API.get("/api/events", Object.assign({}, q, {
          page: 1, page_size: st.limit, sort: "created_ts", order: "desc",
        }));
        st.items = res.items || [];
        st.total = res.total || 0;
      } catch (e) {
        st.items = [];
        st.total = 0;
      }
      st.loading = false;
      paint();
    }

    function paint() {
      badge.textContent = I.num(st.total);
      clear(grid);
      if (!st.items.length) {
        grid.appendChild(empty(kind === "identified"
          ? "هنوز چهره‌ای شناسایی نشده"
          : "چهره ناشناسی ثبت نشده"));
        return;
      }
      st.items.forEach((ev) => grid.appendChild(faceCell(ev)));
      if (st.items.length < st.total) grid.appendChild(moreWrap);
    }

    paint();
    return { el, load, state: st };
  }

  function faceCell(ev) {
    const src = window.API.mediaUrl(ev.face_file || ev.frame_file);
    const name = ev.identity_name === "unknown" ? L.unknown : ev.identity_name;
    const when = `${I.formatDate(ev.event_date)} – ${I.formatTime(ev.event_time)}`;
    return h("div", {
      class: "face-cell",
      title: `${name} · ${when} · ${ev.camera_source || ""}`,
      onclick: () => openRecent(ev),
    },
      src
        ? h("img", { src, alt: name, loading: "lazy" })
        : h("div", { class: "face-cell", style: "aspect-ratio:1/1;background:var(--bg-inset)" }),
      h("div", { class: "cap" },
        h("div", { class: "n" }, name),
        h("div", { class: "m" }, when),
        h("div", { class: "m" }, ev.camera_source || ""))
    );
  }

  function openRecent(ev) {
    const faceImg = window.API.mediaUrl(ev.face_file || "");
    const frameImg = window.API.mediaUrl(ev.frame_file || "");
    const name = ev.identity_name === "unknown" ? L.unknown : ev.identity_name;
    const row = (k, v) => h("tr", {}, h("td", { class: "muted" }, k), h("td", {}, v));

    modal({
      title: name,
      size: "lg",
      body: h("div", {},
        h("div", { class: "grid-2" },
          h("div", {},
            h("table", { class: "data" },
              h("tbody", {},
                row("نام", name),
                row("تاریخ", I.formatDate(ev.event_date)),
                row("ساعت", I.formatTime(ev.event_time)),
                row("دوربین", ev.camera_source || "—"),
                row("ناحیه", ev.zone_name || "—"),
                row("شباهت", I.pct(ev.score, 1)),
                row("نقش", L[ev.role] || ev.role || L.notset),
                row("جنسیت", L[ev.gender] || L.notset),
                row("سن", ev.age !== null && ev.age !== undefined ? I.num(ev.age) : "—"),
                row("شماره ملی", ev.national_number ? I.num(ev.national_number) : "—")
              ))),
          h("div", {},
            h("div", { class: "muted", style: "font-size:12px;margin-bottom:4px" }, "چهره ثبت‌شده"),
            h("div", { class: "img-box", style: "aspect-ratio:1/1" },
              faceImg
                ? h("img", { src: faceImg, alt: "face", style: "width:100%;height:100%;object-fit:contain;cursor:zoom-in", onclick: () => zoomImage(faceImg) })
                : empty("چهره‌ای ثبت نشده")))),
        frameImg
          ? h("div", { style: "margin-top:12px" },
              h("div", { class: "muted", style: "font-size:12px;margin-bottom:4px" }, "قاب کامل — چهره در این تصویر دیده شد"),
              h("div", { class: "img-box contain", style: "max-height:320px" },
                h("img", { src: frameImg, alt: "frame", style: "max-height:320px;cursor:zoom-in", onclick: () => zoomImage(frameImg) })))
          : null),
      actions: [{ label: "بستن", kind: "ghost", onClick: (c) => c() }],
    });
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
      // The face is shown above; this is the frame it was detected in, so the
      // pair reads as "this face, in that scene".
      body.appendChild(h("div", { style: "margin-top:12px" },
        h("div", { class: "muted", style: "font-size:12px;margin-bottom:4px" },
          "قاب كامل — چهره در این تصویر دیده شد"),
        h("div", { class: "img-box contain", style: "max-height:340px" },
          h("img", { src: frameImg, alt: "frame", style: "max-height:340px;cursor:zoom-in", onclick: () => zoomImage(frameImg) }))
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

  // The live mosaic and the detection list are two independent surfaces.
  // They used to share one refresh() entry point, so any code that repainted
  // the detections also ran the mosaic path - one wrong call and every tile
  // restarts.  They are now separated: refreshMosaic() owns the video tiles
  // (and their frame loops), refreshDetections() owns the list, and nothing
  // that merely updates detections can touch a running stream.
  function refreshMosaic() {
    if (!root) return;
    const mosaicHost = root.querySelector("#mosaic-host");
    if (!mosaicHost) return;
    const cams = store.state.cameras || [];
    const sig = cams.map((c) => c.id + ":" + c.name).join("|") + "|" + (S.expanded || "");
    if (S.mosaicSig === sig) return;   // structure unchanged: let frames run
    S.mosaicSig = sig;
    Object.keys(S.live).forEach((k) => stopLive(k));
    S.live = {};
    renderMosaic(mosaicHost);
    syncLiveLoops();
  }

  function refreshDetections() {
    if (!S.detHost) return;
    if (!S.detHost.querySelector("[data-panel]")) {
      clear(S.detHost);
      detectionPanels(S.detHost);
    }
    paintDetections();
  }

  // Kept for the places that legitimately want both (panel collapse toggle,
  // mount).  Detection-only callers use refreshDetections() directly.
  function refresh() {
    refreshMosaic();
    refreshDetections();
  }

  async function loadCameras() {
    try {
      const res = await window.API.get("/api/cameras");
      store.set({ cameras: res.items || [] });
      // Status poll: rebuild the mosaic only if the camera set actually
      // changed, otherwise just retouch the badges in place.  This must not
      // touch the detection list either - it has its own timer.
      refreshMosaic();
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
      } else {
        // not streaming: drop the LIVE badge and show the reason.  The earlier
        // version only updated an existing overlay, so a camera that stopped
        // kept its LIVE dot forever.
        // A tile whose stream is actually painting must never carry a
        // "connecting" label: the status field lags the video by up to one
        // poll, which left the live picture sitting under a permanent
        // "در حال اتصال…" caption.
        const streamingNow = tile.dataset.hasFrame === "1";
        const live = tile.querySelector(".live-dot");
        if (live) live.remove();
        const box = tile.querySelector(".video-box");
        if (box) {
          let msg = box.querySelector(".overlay-msg");
          if (streamingNow && state !== "offline") {
            if (msg) msg.remove();      // frames are flowing: no label at all
          } else {
            if (!msg) {
              msg = h("div", { class: "overlay-msg" });
              box.appendChild(msg);
            }
            msg.textContent = state === "offline"
              ? (cam.last_error || "دوربین آفلاین است")
              : "در حال اتصال…";
          }
        }
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

  // ---- resources & recent faces -------------------------------------------
  async function loadResources() {
    if (!S.resPanel) return;
    try {
      const res = await window.API.get("/api/resources");
      S.resPanel.update(res);
    } catch (e) { /* transient: the next tick retries */ }
  }

  async function loadRecent() {
    if (S.recentBusy) return;
    S.recentBusy = true;
    try {
      await Promise.all([
        S.recent.identified ? S.recent.identified.load() : null,
        S.recent.unknown ? S.recent.unknown.load() : null,
      ]);
    } catch (e) { /* transient */ }
    S.recentBusy = false;
  }

  // The overall numbers: how many faces have been detected since the start.
  async function loadStats() {
    if (!S.statsPanel) return;
    try {
      const res = await window.API.get("/api/stats");
      S.statsPanel.update(res);
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

    // System vitals: CPU, RAM and disk (three live charts).
    S.resPanel = window.Charts.resourcePanel();

    // The overall figures since the system started counting.
    S.statsPanel = window.Charts.statsPanel();

    // The last stored faces: recognised and unrecognised, newest first.
    const recentHost = h("div", { id: "recent-host", class: "recent-panels" });
    S.recent.identified = recentFacesPanel("identified");
    S.recent.unknown = recentFacesPanel("unknown");
    recentHost.appendChild(S.recent.identified.el);
    recentHost.appendChild(S.recent.unknown.el);

    const detHost = h("div", { id: "det-host", class: "det-panels", style: "margin-top:14px" });

    // Section order (top to bottom):
    //   1. the overall statistics      - the head-line numbers
    //   2. the camera mosaic            - what is happening right now
    //   3. identified / unknown tracks  - the live rows under the mosaic
    //   4. the last stored faces        - the recent-face panels
    //   5. system resources             - CPU / RAM / disk, at the bottom
    root.appendChild(S.statsPanel.el);
    root.appendChild(mosaicHost);
    root.appendChild(detHost);
    root.appendChild(recentHost);
    root.appendChild(S.resPanel.el);
    S.recentHost = recentHost;
    S.detHost = detHost;
    mounted = true;
    await loadCameras();
    await loadDetections();
    loadStats();
    loadResources();
    loadRecent();
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
    S.resPanel = null;
    S.recent = {};
    S.recentHost = null;
    // The mosaic signature must be cleared too.  Leaving it set made the next
    // mount believe the tiles were already built, so refreshMosaic() returned
    // early and the page stayed on its "loading" placeholder - the dashboard
    // looked permanently stuck after switching tabs and coming back.
    S.mosaicSig = null;
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
    if (badge) {
      const n = I.num(items.length);
      if (badge.textContent !== n) badge.textContent = n;  // avoid needless mutation
    }
    const list = panel.querySelector(".det-list");
    if (!list) return;
    if (!items.length) {
      // An empty panel must keep its placeholder: removing and re-adding it on
      // every poll (the old code did exactly that, 4x a second) makes the whole
      // list visibly blink.
      if (!list.querySelector("[data-empty]")) {
        clear(list);
        const e = empty(emptyText);
        e.dataset.empty = "1";
        list.appendChild(e);
      }
      S.rendered = {};
      return;
    }
    // Rows exist: drop the placeholder once, and only if it is still there.
    const body = list.querySelector("[data-empty]");
    if (body) body.remove();
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
  // Each tile holds ONE multipart/x-mixed-replace stream (/video/<id>), not a
  // series of snapshots.  The browser repaints a multipart <img> in place as
  // parts arrive, so there is no src swap and therefore no moment where the
  // element has no bitmap - the black flash was inherent to swapping .src.
  // One connection per camera also avoids the per-host connection limit that
  // made the old per-tile snapshot loops stall the whole console.
  function streamUrl(camId) {
    const isAdmin = store.state.user && store.state.user.role === "admin";
    const flavour = isAdmin ? (S.flavour[camId] || "annotated") : "raw";
    return `/video/${camId}?flavour=${flavour}`;
  }

  function startLive(camId) {
    const entry = S.live[camId];
    if (!entry || entry.streaming) return;
    const el = entry.el;
    if (!el || !el.isConnected) return;
    entry.streaming = true;
    const tileEl = el.closest("[data-tile]");
    if (tileEl) tileEl.dataset.hasFrame = "0";
    el.onload = () => {
      if (tileEl) tileEl.dataset.hasFrame = "1";
      const box = el.closest(".video-box");
      const msg = box && box.querySelector(".overlay-msg");
      if (msg) msg.remove();
    };
    // The stream fills the visible buffer; the hidden one stays empty while
    // streaming and is only used by the snapshot fallback below.
    el.classList.add("front");
    if (entry.alt) entry.alt.classList.remove("front");
    entry.front = el;

    const connect = () => {
      if (!mounted || !el.isConnected || entry.stopped) return;
      const url = streamUrl(camId) + "&t=" + Date.now();
      entry.started = Date.now();
      el.onerror = () => {
        // A failed/closed stream shows the last frame until the retry lands.
        if (entry.stopped) return;
        entry.retry = setTimeout(connect, 1500);
      };
      el.src = url;
    };
    entry.connect = connect;
    connect();
  }

  function stopLive(camId) {
    const entry = S.live[camId];
    if (!entry) return;
    entry.stopped = true;
    entry.streaming = false;
    if (entry.retry) clearTimeout(entry.retry);
    entry.retry = null;
    if (entry.timer) clearTimeout(entry.timer);
    entry.timer = null;
    // Detach the source so the browser closes the multipart connection and the
    // server's generator sees GeneratorExit -> stream.release().
    const el = entry.el;
    if (el) {
      el.onerror = null;
      el.removeAttribute("src");
    }
    if (entry.alt) entry.alt.removeAttribute("src");
  }

  // Switching a tile between raw and annotated needs a new stream: the flavour
  // is part of the URL, so the connection has to be reopened.
  function restartLive(camId) {
    const entry = S.live[camId];
    if (!entry) return;
    stopLive(camId);
    entry.stopped = false;
    startLive(camId);
  }

  function syncLiveLoops() {
    const wanted = new Set((store.state.cameras || []).map((c) => c.id));
    Object.keys(S.live).forEach((k) => {
      if (!wanted.has(Number(k)) || !S.live[k].el.isConnected) {
        stopLive(k);
        delete S.live[k];
      }
    });
    // startLive() is idempotent: it returns early when a stream is already up,
    // so this only opens connections for tiles that do not have one.
    Object.keys(S.live).forEach((k) => startLive(k));
  }

  // ---- timers -----------------------------------------------------------
  function startTimers() {
    stopTimers();
    // detections: 4 Hz - the backend answers "unchanged" when nothing moved
    S.detTimer = setInterval(() => { if (mounted) loadDetections(); }, 260);
    // resource charts (CPU / RAM / disk): every 3 s
    S.resTimer = setInterval(() => { if (mounted) loadResources(); }, 3000);
    // overall statistics: every 10 s - the totals move slowly
    S.statsTimer = setInterval(() => { if (mounted) loadStats(); }, 10000);
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
    if (S.resTimer) clearInterval(S.resTimer);
    if (S.camTimer) clearInterval(S.camTimer);
    if (S.statsTimer) clearInterval(S.statsTimer);
    S.detTimer = null;
    S.resTimer = null;
    S.camTimer = null;
    S.statsTimer = null;
  }

  // push updates: a new event or a camera change arrives over SSE
  const refreshRecent = window.UI.debounce(() => { if (mounted) loadRecent(); }, 800);
  const refreshStats = window.UI.debounce(() => { if (mounted) loadStats(); }, 1200);

  window.addEventListener("facw:change", (ev) => {
    if (!mounted) return;
    const d = ev.detail || {};
    if (d.topic === "events" && d.data && d.data.action === "created") {
      // instant: ask for the tracks right away instead of waiting for the tick
      loadDetections();
      refreshRecent();
      refreshStats();
      if (d.data.id && d.data.id !== S.lastEventId) S.lastEventId = d.data.id;
    }
    if (d.topic === "cameras") loadCameras();
    if (d.topic === "identities") loadDetections();
  });
  window.addEventListener("facw:alarm", () => alertChime());

  window.PageDashboard = { mount, unmount, loadDetections, state: S };
})();
