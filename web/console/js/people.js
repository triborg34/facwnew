/* FACW console - people: identity grid, enrolment dialog, person detail. */
(function () {
  "use strict";
  const { h, clear, toast, modal, loading, empty, errorState, store } = window.UI;
  const I = window.I18N;
  const L = window.LABELS;
  const MAX_PHOTOS = 10;

  let root = null;

  let mounted = false;
  let page = 1;
  let search = "";

  const roleBadge = (role) => {
    const cls = role === "approve" ? "ok" : role === "deny" ? "danger" : role === "visitor" ? "warn" : "accent";
    return h("span", { class: "badge " + cls }, L[role] || role || L.notset);
  };

  async function loadGrid() {
    const host = root && root.querySelector("#people-host");
    if (!host) return;   // not on the people page (dialog opened elsewhere)
    clear(host);
    host.appendChild(loading());
    try {
      const res = await window.API.get("/api/identities", { page, page_size: 24, search });
      clear(host);
      if (!res.items.length) {
        host.appendChild(empty("شخصی ثبت نشده است", "با دکمهٔ «ثبت شخص جدید» شروع کنید"));
        return;
      }
      const grid = h("div", { class: "people-grid" });
      res.items.forEach((p) => {
        const img = window.API.mediaUrl(p.avatar_file || p.reference_file);
        grid.appendChild(h("div", { class: "person-card", onclick: () => openPerson(p.id) },
          h("div", { class: "avatar" }, img ? h("img", { src: img, alt: p.name, loading: "lazy" }) : h("span", { style: "font-size:30px;opacity:.5" }, "\u{1F464}")),
          h("div", { class: "meta" },
            h("div", { class: "n" }, p.name + (p.family_name ? " " + p.family_name : "")),
            h("div", { class: "r" }, roleBadge(p.role), h("span", { class: "badge" }, I.num(p.ref_count) + " تصویر"))
          )
        ));
      });
      host.appendChild(grid);
      const pages = Math.max(1, Math.ceil(res.total / res.page_size));
      host.appendChild(h("div", { class: "pager" },
        h("span", { class: "pages" }, `صفحه ${I.num(page)} از ${I.num(pages)} \u00B7 ${I.num(res.total)} نفر`),
        h("div", { class: "spacer" }),
        h("button", { class: "btn sm", disabled: page <= 1, onclick: () => { page -= 1; loadGrid(); } }, "قبلی"),
        h("button", { class: "btn sm", disabled: page >= pages, onclick: () => { page += 1; loadGrid(); } }, "بعدی")
      ));
    } catch (e) {
      clear(host);
      host.appendChild(errorState(e.message, loadGrid));
    }
  }

  // ------------------------------------------------------- enrolment dialog
  //
  // One dialog registers a person from ANY mix of sources:
  //   * file uploads ("افزودن عکس از فایل")
  //   * a live camera preview with a "capture" button (small, fitted, zoomable)
  //   * a single still frame that can be dragged to crop before face detection
  //   * the detected face of an unknown person on the dashboard (opts.still)
  //
  // Every image carries its own face candidates and the operator's chosen one,
  // so the save call sends files and camera frames in a single request
  // (/api/enrolment/save accepts `files` + `stills`).
  function openEnrolment(target, opts) {
    opts = opts || {};
    const photos = [];      // {file|null, still, cameraId, faces:[], chosen, previewFull, source}
    const err = h("div", { class: "state error", style: "display:none" });
    const photoStrip = h("div", { class: "face-strip" });
    const faceStrip = h("div", { class: "face-strip" });
    const live = h("div", { class: "field", style: "margin-top:10px;display:none" },
      h("label", {}, "چهره‌ها — یکی را انتخاب کنید"), faceStrip);

    // --- live camera preview -------------------------------------------
    const camSelect = h("select", {});
    (store.state.cameras || []).forEach((c) => camSelect.appendChild(h("option", { value: c.id }, c.name)));
    const camImg = h("img", { alt: "live camera", draggable: "false" });
    const camFrame = h("div", { class: "frame" }, camImg, h("div", { class: "hint" }, "پیش‌نمایش زنده"));
    const camNoSignal = h("div", { class: "no-signal", style: "display:none" }, "دوربین تصویری نمی‌فرستد");
    const camPreview = h("div", { class: "cam-preview" }, camFrame, camNoSignal);
    const liveState = h("span", { class: "badge" }, "غیرفعال");
    const liveBtn = h("button", { class: "btn sm", type: "button" }, "پیش‌نمایش زنده");
    const captureBtn = h("button", { class: "btn sm primary", type: "button" }, "\u{1F4F7} گرفتن قاب");
    const camZoomLabel = h("span", { class: "badge", style: "min-width:52px;text-align:center" }, "100\u066A");
    const camZoom = h("div", { class: "zoom-tools" },
      h("button", { class: "btn sm ghost", type: "button", title: "کوچک‌نمایی", onclick: () => setZoom(camScale - 0.25) }, "\u2212"),
      camZoomLabel,
      h("button", { class: "btn sm ghost", type: "button", title: "بزرگ‌نمایی", onclick: () => setZoom(camScale + 0.25) }, "+"),
      h("button", { class: "btn sm ghost", type: "button", onclick: () => setZoom(1) }, "تناسب"));
    let camScale = 1;
    let camTimer = null;
    let camInflight = false;

    function setZoom(next) {
      camScale = Math.max(1, Math.min(3, next));
      camImg.style.transform = `scale(${camScale})`;
      camFrame.style.overflow = camScale > 1 ? "auto" : "hidden";
      camZoomLabel.textContent = I.num(Math.round(camScale * 100)) + "\u066A";
    }

    function refreshLiveFrame() {
      if (!camTimer || camInflight) return;
      const camId = camSelect.value;
      if (!camId) return;
      camInflight = true;
      const probe = new Image();
      probe.onload = () => {
        camInflight = false;
        if (!camTimer) return;
        camImg.src = probe.src;
        camNoSignal.style.display = "none";
        camFrame.style.display = "";
        liveState.className = "badge ok";
        liveState.textContent = "زنده";
      };
      probe.onerror = () => {
        camInflight = false;
        if (!camTimer) return;
        camNoSignal.style.display = "";
        camFrame.style.display = "none";
        liveState.className = "badge danger";
        liveState.textContent = "بدون سیگنال";
      };
      // RAW frames: this preview is only for aiming the camera at a face.
      probe.src = `/video/${camId}/snapshot?flavour=raw&t=${Date.now()}`;
    }

    function startLive() {
      if (camTimer) return;
      if (!camSelect.value) { toast("ابتدا یک دوربین ثبت کنید", "warn"); return; }
      refreshLiveFrame();
      camTimer = setInterval(refreshLiveFrame, 260);
      liveBtn.textContent = "توقف پیش‌نمایش";
      liveState.className = "badge warn";
      liveState.textContent = "در حال اتصال…";
      camSelect.disabled = true;
    }

    function stopLive() {
      if (camTimer) clearInterval(camTimer);
      camTimer = null;
      camInflight = false;
      liveBtn.textContent = "پیش‌نمایش زنده";
      liveState.className = "badge";
      liveState.textContent = "غیرفعال";
      camSelect.disabled = false;
    }

    function closeLive() {
      stopLive();
      camImg.removeAttribute("src");
    }

    liveBtn.onclick = () => (camTimer ? stopLive() : startLive());
    camSelect.onchange = () => { if (camTimer) { stopLive(); startLive(); } };

    // --- still + crop ---------------------------------------------------
    const crop = window.IMGW.cropEditor();
    const stillBox = h("div", { style: "display:none;margin-top:10px" },
      h("div", { class: "row", style: "align-items:flex-end" },
        h("div", { class: "field", style: "flex:1" }, h("label", {}, "قاب گرفته‌شده (برای برش بکشید)"), crop.el),
        h("div", { class: "field", style: "width:auto;flex:none" },
          h("label", {}, "\u00A0"),
          h("button", { class: "btn sm", type: "button", onclick: () => addStill(crop.dataUrl(), "برش دستی") }, "استفاده از این قاب"),
          h("button", { class: "btn sm ghost", type: "button", style: "margin-top:6px", onclick: () => { stillBox.style.display = "none"; } }, "بستن")))
    );
    let lastStillCamera = 0;

    async function captureFromCamera() {
      const camId = camSelect.value;
      if (!camId) { toast("ابتدا یک دوربین انتخاب کنید", "warn"); return; }
      if (!camTimer) startLive();
      captureBtn.disabled = true;
      try {
        const res = await window.API.post("/api/enrolment/capture", { camera_id: Number(camId) });
        if (!res.still) { toast("قاب دوربین دریافت نشد", "warn"); return; }
        lastStillCamera = Number(camId);
        if (res.faces && res.faces.length) {
          // faces were detected on exactly this frame - keep the same frame
          pushPhoto({ still: res.still, cameraId: Number(camId), faces: res.faces, previewFull: res.preview, source: "دوربین" });
          toast("قاب دوربین ثبت شد", "ok");
          stillBox.style.display = "none";
        } else {
          // no face on the whole frame: let the operator crop the region
          await crop.load(res.still);
          stillBox.style.display = "";
          toast("در کل قاب چهره‌ای یافت نشد؛ ناحیهٔ چهره را با ماوس انتخاب کنید", "warn");
        }
      } catch (e) {
        toast("خطا: " + e.message, "danger");
      } finally {
        captureBtn.disabled = false;
      }
    }
    captureBtn.onclick = captureFromCamera;

    async function addStill(dataUrl, label) {
      if (!dataUrl) return;
      if (photos.length >= MAX_PHOTOS) { toast(`حداکثر ${I.num(MAX_PHOTOS)} تصویر مجاز است`, "warn"); return; }
      try {
        const res = await window.API.post("/api/enrolment/detect-b64", { image: dataUrl });
        if (!res.faces || !res.faces.length) {
          toast("در این ناحیه چهره‌ای یافت نشد؛ ناحیهٔ بزرگ‌تری را انتخاب کنید", "warn");
          return;
        }
        pushPhoto({ still: dataUrl, cameraId: lastStillCamera, faces: res.faces, previewFull: res.preview, source: label || "دوربین" });
        stillBox.style.display = "none";
        toast("قاب دوربین ثبت شد", "ok");
      } catch (e) {
        toast("خطا: " + e.message, "danger");
      }
    }

    // --- photo list -----------------------------------------------------
    function pushPhoto(p) {
      p.source = p.source || "فایل";
      p.file = p.file || null;
      p.chosen = 0;
      photos.push(p);
      renderPhotos();
      renderFaces(photos.length - 1);
    }

    function thumbOf(p) {
      const f = p.faces[p.chosen];
      return (f && f.thumbnail) || p.previewFull || "";
    }

    function sourceLabel(p) {
      if (p.file) return p.file.name.length > 16 ? p.file.name.slice(0, 14) + "…" : p.file.name;
      return p.source;
    }

    function renderPhotos() {
      clear(photoStrip);
      if (!photos.length) {
        photoStrip.appendChild(h("div", { class: "muted", style: "font-size:12px;padding:6px 0" },
          "هنوز تصویری افزوده نشده است: فایل بارگذاری کنید یا از دوربین قاب بگیرید."));
        return;
      }
      photos.forEach((p, i) => {
        photoStrip.appendChild(h("div", {
          class: "face-chip" + (p.chosen >= 0 ? " selected" : " unselected"),
          title: "کلیک = انتخاب چهرهٔ بعدی در همین تصویر",
          onclick: () => cycleFace(i),
        },
          h("img", { src: thumbOf(p), alt: "" }),
          h("div", { class: "cap" }, p.faces.length
            ? `چهرهٔ ${I.num(p.chosen + 1)} از ${I.num(p.faces.length)}`
            : "بدون چهره"),
          h("div", { class: "cap", style: "opacity:.72" }, sourceLabel(p)),
          h("button", {
            class: "btn sm ghost", style: "width:100%",
            onclick: (ev) => { ev.stopPropagation(); photos.splice(i, 1); renderPhotos(); renderFaces(photos.length - 1); },
          }, "حذف")
        ));
      });
    }

    function cycleFace(i) {
      const p = photos[i];
      if (!p.faces.length) return;
      p.chosen = (p.chosen + 1) % p.faces.length;
      renderPhotos();
      renderFaces(i);
    }

    function renderFaces(activeIndex) {
      clear(faceStrip);
      const p = photos[activeIndex === undefined || activeIndex < 0 ? photos.length - 1 : activeIndex];
      if (!p || !p.faces.length) { live.style.display = "none"; return; }
      live.style.display = "";
      p.faces.forEach((f, fi) => {
        faceStrip.appendChild(h("div", {
          class: "face-chip " + (fi === p.chosen ? "selected" : "unselected"),
          onclick: () => { p.chosen = fi; renderPhotos(); renderFaces(photos.indexOf(p)); },
        },
          h("img", { src: f.thumbnail, alt: "" }),
          h("div", { class: "cap" }, `${I.pct(f.score, 0)} \u00B7 ${I.num(Math.round(f.width))}px`),
          f.passed_gate ? null : h("div", { class: "cap", style: "color:var(--warn)" }, "کیفیت کم")
        ));
      });
    }

    async function addFiles(files) {
      for (const file of Array.from(files)) {
        if (photos.length >= MAX_PHOTOS) { toast(`حداکثر ${I.num(MAX_PHOTOS)} تصویر مجاز است`, "warn"); break; }
        const fd = new FormData();
        fd.append("file", file);
        try {
          const res = await window.API.form("/api/enrolment/detect", fd);
          if (!res.faces || !res.faces.length) {
            toast(`در «${file.name}» چهره‌ای یافت نشد`, "warn");
            continue;
          }
          pushPhoto({ file, faces: res.faces, previewFull: res.preview, source: "فایل" });
        } catch (e) { toast(`خطا در ${file.name}: ${e.message}`, "danger"); }
      }
    }

    const fileInput = h("input", {
      type: "file", accept: "image/*", multiple: true,
      onchange: (ev) => { addFiles(ev.target.files); ev.target.value = ""; },
    });

    // --- identity fields ------------------------------------------------
    const meta = {
      name: h("input", { type: "text", value: opts.name || "" }),
      family_name: h("input", { type: "text", value: opts.family_name || "" }),
      age: h("input", { type: "number", min: 0, max: 120, value: opts.age || "" }),
      gender: h("select", {}, [["unknown", L.notset], ["male", L.male], ["female", L.female]]
        .map(([v, t]) => h("option", { value: v, selected: opts.gender === v ? true : null }, t))),
      role: h("select", {}, [["colleague", L.colleague], ["approve", L.approve], ["visitor", L.visitor], ["deny", L.deny]]
        .map(([v, t]) => h("option", { value: v, selected: opts.role === v ? true : null }, t))),
      national_number: h("input", { type: "text" }),
      description: h("textarea", {}),
    };

    // --- source tabs ----------------------------------------------------
    // The dialog used to show every way of adding a photo at once (file,
    // camera, live preview, crop, chip list), which read as a wall of
    // controls.  Each source now gets its own small tab; chosen photos stay in
    // a shared strip below so they remain visible from any tab.
    const filePane = h("div", {},
      h("div", { class: "field" }, h("label", {}, "انتخاب تصویر از فایل"), fileInput),
      h("div", { class: "muted", style: "font-size:12px;margin-top:6px" },
        "می‌توانید چند فایل را هم‌زمان انتخاب کنید."));

    const cameraPane = h("div", {},
      h("div", { class: "row", style: "align-items:flex-end" },
        h("div", { class: "field", style: "flex:1" }, h("label", {}, "دوربین"), camSelect),
        h("div", { class: "field", style: "flex:none;width:auto" }, h("label", {}, " "),
          h("div", { style: "display:flex;gap:6px;align-items:center" }, liveBtn, captureBtn))),
      h("div", { style: "margin-top:8px" },
        h("div", { style: "display:flex;align-items:center;gap:8px;margin-bottom:4px" },
          h("span", { class: "muted", style: "font-size:12px" }, "پیش‌نمایش دوربین"), liveState,
          h("div", { style: "flex:1" })),
        camPreview, camZoom));

    // Stills handed over by another screen (report row / dashboard card).
    const inboxPane = h("div", {});
    const inboxBadge = h("span", { class: "b", style: "display:none" });
    let activeTab = "file";

    const panes = { file: filePane, camera: cameraPane, inbox: inboxPane };

    function setTab(name) {
      if (!panes[name]) return;
      activeTab = name;
      Object.keys(panes).forEach((k) => { panes[k].style.display = k === name ? "" : "none"; });
      tabButtons.forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
      if (name !== "camera") stopLive();   // do not keep polling from a hidden tab
    }

    const tabButtons = [
      { key: "file", label: "از فایل" },
      { key: "camera", label: "از دوربین" },
      { key: "inbox", label: "قاب ارسالی", badge: inboxBadge },
    ].map((t) => h("button", { type: "button", "data-tab": t.key, onclick: () => setTab(t.key) },
      t.label, t.badge || null));

    const body = h("div", {},
      h("div", { class: "mini-tabs" }, tabButtons),
      filePane, cameraPane, inboxPane,
      stillBox,
      h("div", { class: "field", style: "margin-top:10px" }, h("label", {}, "تصاویر افزوده‌شده"), photoStrip),
      live,
      h("div", { class: "card", style: "margin-top:12px" }, h("div", { class: "card-head" }, "اطلاعات هویتی"),
        h("div", { class: "card-body" },
          h("div", { class: "row" },
            h("div", { class: "field" }, h("label", {}, "نام *"), meta.name),
            h("div", { class: "field" }, h("label", {}, "نام خانوادگی"), meta.family_name)),
          h("div", { class: "row", style: "margin-top:10px" },
            h("div", { class: "field" }, h("label", {}, "سن"), meta.age),
            h("div", { class: "field" }, h("label", {}, "جنسیت"), meta.gender),
            h("div", { class: "field" }, h("label", {}, "وضعیت مجوز"), meta.role)),
          h("div", { class: "row", style: "margin-top:10px" },
            h("div", { class: "field" }, h("label", {}, "شماره ملی"), meta.national_number),
            h("div", { class: "field" }, h("label", {}, "توضیحات"), meta.description))
        )),
      err
    );

    const dialog = modal({
      title: target ? "افزودن تصویر مرجع" : (opts.title || "ثبت شخص جدید"),
      size: "lg",
      body,
      onClose: closeLive,
      actions: [
        { label: "انصراف", kind: "ghost", onClick: (c) => { closeLive(); c(); } },
        { label: "ذخیره", kind: "primary", onClick: async (c) => {
            err.style.display = "none";
            if (!photos.length) { err.style.display = ""; err.textContent = "حداقل یک تصویر لازم است"; return; }
            if (!target && !meta.name.value.trim()) { err.style.display = ""; err.textContent = "نام الزامی است"; return; }
            const fd = new FormData();
            // The server rebuilds its image list as "all uploads, then all
            // camera stills" (api_routes.enrolment_save), so the face indices
            // MUST be built in that same order.  Iterating photos once and
            // pushing chosen[] in display order sent an index belonging to a
            // still to a different uploaded file - the "face index N out of
            // range" error whenever the two kinds were mixed.
            const filePhotos = photos.filter((p) => p.file);
            const stillPhotos = photos.filter((p) => !p.file && p.still);
            const chosen = filePhotos.concat(stillPhotos).map((p) => p.chosen);
            filePhotos.forEach((p) => fd.append("files", p.file));
            fd.append("faces", JSON.stringify(chosen));
            fd.append("stills", JSON.stringify(stillPhotos.map((p) => p.still)));
            fd.append("metadata", JSON.stringify({
              name: meta.name.value.trim(), family_name: meta.family_name.value.trim(),
              age: meta.age.value || null, gender: meta.gender.value, role: meta.role.value,
              national_number: meta.national_number.value.trim(), description: meta.description.value.trim(),
            }));
            if (target) fd.append("identity_id", String(target));
            try {
              await window.API.form("/api/enrolment/save", fd);
              toast(target ? "تصویر مرجع افزوده شد" : "شخص ثبت شد", "ok");
              closeLive();
              c();
              // loadGrid() only makes sense when the people page is mounted.
              // This dialog is also opened from the dashboard and from the
              // report screen, where root is null (or a different page) and
              // refreshing it threw "Cannot read properties of null".
              if (target) {
                if (root) openPerson(target);
              } else if (root && root.querySelector("#people-host")) {
                loadGrid();
              }
              if (opts.onSaved) opts.onSaved();
            } catch (e) {
              err.style.display = "";
              err.textContent = e.message;
            }
          } },
      ],
    });
    err.style.display = "none";
    renderPhotos();
    renderFaces(-1);

    // A still handed over by another screen (dashboard card or report row).
    // It lands in its own tab so it is obvious where it came from, and it is
    // added to the photo strip straight away unless it still needs cropping.
    function receiveStill(still, o) {
      o = o || {};
      if (!still) return;
      lastStillCamera = o.cameraId || 0;
      clear(inboxPane);
      const faces = o.faces || [];
      const label = o.source || "قاب ارسالی";
      inboxPane.appendChild(h("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" }, label));
      inboxPane.appendChild(h("div", { class: "img-box", style: "max-height:260px" },
        h("img", { src: still, alt: "", style: "width:100%;object-fit:contain" })));
      if (faces.length) {
        inboxBadge.style.display = "";
        inboxBadge.textContent = I.num(faces.length);
        pushPhoto({ still: still, cameraId: o.cameraId || 0, faces: faces,
                    previewFull: o.preview || "", source: label });
        setTab("inbox");
      } else {
        // No face found on the full frame: hand it to the crop editor.
        crop.load(still).then(() => { stillBox.style.display = ""; }).catch(() => { /* ignore */ });
        setTab("inbox");
      }
    }
    receiveStill(opts.still, opts);
    setTab(opts.still ? "inbox" : "file");
    return dialog;
  }

  // Entry point used by the dashboard for an unknown detection: enrol the
  // exact frame the operator is looking at.
  function openEnrolFromEvent(ev, info) {
    info = info || {};
    return openEnrolment(null, {
      title: info.title || "ثبت شخص از گزارش",
      still: info.still || "",
      faces: info.faces || [],
      preview: info.preview || "",
      cameraId: info.cameraId || 0,
      age: ev ? ev.age : undefined,
      gender: ev ? ev.gender : undefined,
      role: info.role || "visitor",
      source: info.source || "گزارش‌ها",
      onSaved: info.onSaved,
    });
  }

  function openEnrolFromDetection(info) {
    info = info || {};
    return openEnrolment(null, {
      title: "ثبت شخص ناشناس",
      still: info.still || "",
      faces: info.faces || [],
      preview: info.preview || "",
      cameraId: info.cameraId || 0,
      age: info.age,
      gender: info.gender,
      role: info.role || "visitor",
      source: "ناشناس",
      onSaved: info.onSaved,
    });
  }
  // ------------------------------------------------------- person detail
  async function openPerson(id) {
    const body = h("div", {}, loading());
    const m = modal({ title: "جزئیات شخص", size: "lg", body, actions: [{ label: "بستن", kind: "ghost", onClick: (c) => c() }] });
    let data;
    try { data = await window.API.get("/api/identities/" + id); }
    catch (e) { clear(body); body.appendChild(errorState(e.message, () => openPerson(id))); return; }
    const p = data.identity;
    const isAdmin = store.state.user && store.state.user.role === "admin";
    clear(body);

    const avatar = window.API.mediaUrl(p.avatar_file || p.reference_file);
    const refs = data.references || [];

    body.appendChild(h("div", { class: "grid-2" },
      h("div", {},
        h("div", { class: "img-box", style: "max-width:220px" }, avatar ? h("img", { src: avatar, alt: p.name }) : empty("\u2014")),
        h("div", { style: "margin-top:10px" },
          h("h3", { style: "margin:0 0 6px" }, p.name + (p.family_name ? " " + p.family_name : "")),
          roleBadge(p.role),
          h("span", { class: "badge", style: "margin-inline-start:6px" }, I.num(p.ref_count) + " تصویر مرجع")
        )
      ),
      h("table", { class: "data" }, h("tbody", {},
        h("tr", {}, h("td", { class: "muted" }, "سن"), h("td", {}, p.age === null ? "\u2014" : I.num(p.age))),
        h("tr", {}, h("td", { class: "muted" }, "جنسیت"), h("td", {}, L[p.gender] || L.notset)),
        h("tr", {}, h("td", { class: "muted" }, "شماره ملی"), h("td", { class: "mono" }, p.national_number ? I.num(p.national_number) : "\u2014")),
        h("tr", {}, h("td", { class: "muted" }, "ثبت‌کننده"), h("td", {}, p.registered_by || "\u2014")),
        h("tr", {}, h("td", { class: "muted" }, "توضیحات"), h("td", {}, p.description || "\u2014")),
        h("tr", {}, h("td", { class: "muted" }, "تاریخ ثبت"), h("td", { class: "mono" }, I.formatDate((p.created_at || "").slice(0, 10)))),
      ))
    ));

    body.appendChild(h("div", { style: "margin-top:14px" },
      h("div", { class: "card-head" }, "تصاویر مرجع",
        h("div", { class: "spacer" }),
        isAdmin ? h("button", { class: "btn sm", onclick: () => { m.close(); openEnrolment(p.id); } }, "افزودن تصویر مرجع") : null),
      h("div", { class: "face-strip" }, refs.length ? refs.map((r, i) => {
        const img = window.API.mediaUrl(r.image_file);
        return h("div", { class: "face-chip unselected", style: "width:120px" },
          img ? h("img", { src: img, alt: "ref", style: "height:100px" }) : h("div", { class: "skeleton", style: "height:100px" }),
          h("div", { class: "cap" }, `#${I.num(i + 1)} \u00B7 ${I.num(Math.round(r.face_width))}px`),
          isAdmin ? h("button", { class: "btn sm ghost", style: "width:100%", onclick: async () => {
            const ok = await window.UI.confirmDialog({ title: "حذف تصویر مرجع", message: `تصویر مرجع شمارهٔ ${i + 1} حذف شود؟`, danger: true, confirmLabel: "حذف" });
            if (!ok) return;
            try {
              const r2 = await window.API.del(`/api/identities/${p.id}/references/${i + 1}`);
              toast("تصویر حذف شد", "ok");
              m.close();
              openPerson(p.id);
            } catch (e) { toast("خطا: " + e.message, "danger"); }
          } }, "\u{1F5D1}") : null
        );
      }) : [empty("تصویر مرجعی ثبت نشده")])
    ));

    // detection history for this person
    if (data.events && data.events.items && data.events.items.length) {
      body.appendChild(h("div", { style: "margin-top:14px" },
        h("div", { class: "card-head" }, "تاریخچهٔ شناسایی"),
        h("table", { class: "data" }, h("tbody", {}, data.events.items.slice(0, 8).map((e) => h("tr", {},
          h("td", { class: "mono" }, I.formatDate(e.event_date)),
          h("td", { class: "mono" }, I.formatTime(e.event_time)),
          h("td", {}, e.camera_source),
          h("td", { class: "mono" }, I.pct(e.score, 1))
        ))))
      ));
    }

    if (isAdmin) {
      body.appendChild(h("div", { style: "display:flex;gap:8px;margin-top:14px" },
        h("button", { class: "btn sm", onclick: () => { m.close(); openEdit(p); } }, "ویرایش اطلاعات"),
        h("button", { class: "btn sm danger", onclick: async () => {
          const ok = await window.UI.confirmDialog({
            title: "حذف شخص", danger: true, confirmLabel: "حذف",
            message: `«${p.name}» همراه با همهٔ تصاویر مرجع حذف شود؟ از فهرست تطبیق بلافاصله خارج می‌شود.`,
          });
          if (!ok) return;
          try {
            await window.API.del("/api/identities/" + p.id);
            toast("شخص حذف شد", "ok");
            m.close(); loadGrid();
          } catch (e) { toast("خطا: " + e.message, "danger"); }
        } }, "حذف شخص")
      ));
    }
  }

  function openEdit(p) {
    const f = {
      name: h("input", { type: "text", value: p.name || "" }),
      family_name: h("input", { type: "text", value: p.family_name || "" }),
      age: h("input", { type: "number", value: p.age === null ? "" : p.age }),
      gender: h("select", {}, [["unknown", L.notset], ["male", L.male], ["female", L.female]].map(([v, t]) => h("option", { value: v, selected: p.gender === v ? true : null }, t))),
      role: h("select", {}, [["colleague", L.colleague], ["approve", L.approve], ["visitor", L.visitor], ["deny", L.deny]].map(([v, t]) => h("option", { value: v, selected: p.role === v ? true : null }, t))),
      national_number: h("input", { type: "text", value: p.national_number || "" }),
      description: h("textarea", {}, p.description || ""),
      registered_by: h("input", { type: "text", value: p.registered_by || "" }),
    };
    const err = h("div", { class: "state error", style: "display:none" });
    modal({
      title: "ویرایش شخص",
      body: h("div", {},
        h("div", { class: "row" },
          h("div", { class: "field" }, h("label", {}, "نام"), f.name),
          h("div", { class: "field" }, h("label", {}, "نام خانوادگی"), f.family_name)),
        h("div", { class: "row", style: "margin-top:10px" },
          h("div", { class: "field" }, h("label", {}, "سن"), f.age),
          h("div", { class: "field" }, h("label", {}, "جنسیت"), f.gender),
          h("div", { class: "field" }, h("label", {}, "وضعیت مجوز"), f.role)),
        h("div", { class: "row", style: "margin-top:10px" },
          h("div", { class: "field" }, h("label", {}, "شماره ملی"), f.national_number),
          h("div", { class: "field" }, h("label", {}, "ثبت‌کننده"), f.registered_by)),
        h("div", { class: "field", style: "margin-top:10px" }, h("label", {}, "توضیحات"), f.description),
        err),
      actions: [
        { label: "انصراف", kind: "ghost", onClick: (c) => c() },
        { label: "ذخیره", kind: "primary", onClick: async (c) => {
            try {
              await window.API.put("/api/identities/" + p.id, {
                name: f.name.value.trim(), family_name: f.family_name.value.trim(),
                age: f.age.value || null, gender: f.gender.value, role: f.role.value,
                national_number: f.national_number.value.trim(),
                description: f.description.value.trim(), registered_by: f.registered_by.value.trim(),
              });
              toast("ذخیره شد", "ok");
              c(); loadGrid(); openPerson(p.id);
            } catch (e) { err.style.display = ""; err.textContent = e.message; }
          } },
      ],
    });
  }

  async function mount(host) {
    mounted = true;
    window.addEventListener("facw:change", onIdentityChange);
    root = host;
    clear(root);
    root.appendChild(h("div", { class: "page-head" },
      h("h1", {}, "اشخاص"),
      h("span", { class: "sub" }, "هویت‌های ثبت‌شده و تصاویر مرجع"),
      h("div", { style: "flex:1" }),
      h("input", { type: "search", placeholder: "جست‌وجوی نام / شماره ملی", style: "max-width:260px",
        oninput: window.UI.debounce((ev) => { search = ev.target.value; page = 1; loadGrid(); }, 300) }),
      h("button", { class: "btn primary", onclick: () => openEnrolment(null) }, "+ ثبت شخص جدید")
    ));
    const hostEl = h("div", { id: "people-host" }, loading());
    root.appendChild(hostEl);
    await loadGrid();
  }

  // A person enrolled from anywhere (this page, the dashboard or the report
  // screen) must show up at once.  The index itself is rebuilt on the server
  // when saving, but this list only refreshed on navigation, so a new person
  // appeared to need an app restart.
  function onIdentityChange(ev) {
    if (!mounted) return;
    const d = ev.detail || {};
    if (d.topic !== "identities") return;
    const act = (d.data || {}).action;
    if (act === "ref_added" || act === "created" || act === "deleted" || act === "updated") {
      loadGrid();
    }
  }

  function unmount() {
    mounted = false;
    window.removeEventListener("facw:change", onIdentityChange);
    root = null;
  }

  window.PagePeople = { mount, unmount, openEnrolment, openPerson, openEnrolFromDetection, openEnrolFromEvent };
})();
