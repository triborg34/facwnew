/* FACW console - people: identity grid, enrolment dialog, person detail. */
(function () {
  "use strict";
  const { h, clear, toast, modal, loading, empty, errorState, store } = window.UI;
  const I = window.I18N;
  const L = window.LABELS;
  const MAX_PHOTOS = 10;

  let root = null;
  let page = 1;
  let search = "";

  const roleBadge = (role) => {
    const cls = role === "approve" ? "ok" : role === "deny" ? "danger" : role === "visitor" ? "warn" : "accent";
    return h("span", { class: "badge " + cls }, L[role] || role || L.notset);
  };

  async function loadGrid() {
    const host = root.querySelector("#people-host");
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
  function openEnrolment(target) {
    // target: undefined = new identity, or an identity id to add references to
    const photos = [];      // {file, preview, faces:[], chosen:0}
    const err = h("div", { class: "state error", style: "display:none" });
    const photoStrip = h("div", { class: "face-strip" });
    const faceStrip = h("div", { class: "face-strip" });
    const previewBox = h("div", { class: "img-box", style: "max-height:250px;overflow:hidden;display:none" });

    const meta = {
      name: h("input", { type: "text" }),
      family_name: h("input", { type: "text" }),
      age: h("input", { type: "number", min: 0, max: 120 }),
      gender: h("select", {}, [["unknown", L.notset], ["male", L.male], ["female", L.female]].map(([v, t]) => h("option", { value: v }, t))),
      role: h("select", {}, [["colleague", L.colleague], ["approve", L.approve], ["visitor", L.visitor], ["deny", L.deny]].map(([v, t]) => h("option", { value: v }, t))),
      national_number: h("input", { type: "text" }),
      description: h("textarea", {}),
    };

    const camSelect = h("select", {});
    (store.state.cameras || []).forEach((c) => camSelect.appendChild(h("option", { value: c.id }, c.name)));

    function renderPhotos() {
      clear(photoStrip);
      photos.forEach((p, i) => {
        const chosenFace = p.faces[p.chosen];
        photoStrip.appendChild(h("div", {
          class: "face-chip" + (p.chosen >= 0 ? " selected" : " unselected"),
          title: "کلیک برای انتخاب چهرهٔ این عکس",
          onclick: () => { cycleFace(i); },
        },
          h("img", { src: (chosenFace && chosenFace.thumbnail) || p.preview, alt: "" }),
          h("div", { class: "cap" }, p.faces.length
            ? `چهرهٔ ${I.num(p.chosen + 1)} از ${I.num(p.faces.length)}`
            : "بدون چهره"),
          h("button", { class: "btn sm ghost", style: "width:100%", onclick: (ev) => {
            ev.stopPropagation(); photos.splice(i, 1); renderPhotos(); renderFaces();
          } }, "حذف")
        ));
      });
    }

    function cycleFace(i) {
      const p = photos[i];
      if (!p.faces.length) return;
      p.chosen = (p.chosen + 1) % p.faces.length;
      renderPhotos(); renderFaces(i);
    }

    function renderFaces(activeIndex) {
      clear(faceStrip);
      const p = photos[activeIndex === undefined ? photos.length - 1 : activeIndex];
      if (!p) { previewBox.style.display = "none"; return; }
      if (p.previewFull) { previewBox.style.display = ""; clear(previewBox); previewBox.appendChild(h("img", { src: p.previewFull, alt: "preview" })); }
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
        if (photos.length >= MAX_PHOTOS) { toast(`حداکثر ${I.num(MAX_PHOTOS)} عکس مجاز است`, "warn"); break; }
        const fd = new FormData();
        fd.append("file", file);
        try {
          const res = await window.API.form("/api/enrolment/detect", fd);
          if (!res.faces || !res.faces.length) {
            toast(`در «${file.name}» چهره‌ای یافت نشد`, "warn");
            continue;
          }
          photos.push({ file, faces: res.faces, chosen: 0, previewFull: res.preview });
        } catch (e) { toast(`خطا در ${file.name}: ${e.message}`, "danger"); }
      }
      renderPhotos(); renderFaces();
    }

    async function captureStill() {
      const camId = camSelect.value;
      if (!camId) { toast("ابتدا یک دوربین انتخاب کنید", "warn"); return; }
      try {
        const res = await window.API.post("/api/enrolment/capture", { camera_id: Number(camId) });
        if (!res.faces || !res.faces.length) { toast("در تصویر دوربین چهره‌ای یافت نشد", "warn"); return; }
        photos.push({ file: null, cameraId: Number(camId), faces: res.faces, chosen: 0, previewFull: res.preview });
        renderPhotos(); renderFaces();
        toast("قاب دوربین گرفته شد", "ok");
      } catch (e) { toast("خطا: " + e.message, "danger"); }
    }

    const fileInput = h("input", { type: "file", accept: "image/*", multiple: true,
      onchange: (ev) => { addFiles(ev.target.files); ev.target.value = ""; } });

    const body = h("div", {},
      h("div", { class: "muted", style: "font-size:12.5px;margin-bottom:6px" },
        `حداکثر ${I.num(MAX_PHOTOS)} عکس. اگر چند چهره در یک عکس باشد، با کلیک روی کاشی‌ها چهرهٔ درست را انتخاب کنید.`),
      h("div", { class: "row" },
        h("div", { class: "field" }, h("label", {}, "افزودن عکس از فایل"), fileInput),
        h("div", { class: "field" }, h("label", {}, "گرفتن قاب از دوربین"), camSelect),
        h("div", { class: "field" }, h("label", {}, " "),
          h("button", { class: "btn", onclick: captureStill }, "\u{1F4F7} گرفتن قاب"))
      ),
      h("div", { class: "field", style: "margin-top:8px" }, h("label", {}, "عکس‌های افزوده‌شده"), photoStrip),
      previewBox,
      h("div", { class: "field", style: "margin-top:8px" }, h("label", {}, "چهره‌ها — یکی را انتخاب کنید"), faceStrip),
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

    modal({
      title: target ? "افزودن تصویر مرجع" : "ثبت شخص جدید",
      size: "lg",
      body,
      actions: [
        { label: "انصراف", kind: "ghost", onClick: (c) => c() },
        { label: "ذخیره", kind: "primary", onClick: async (c) => {
            if (!photos.length) { err.style.display = ""; err.textContent = "حداقل یک عکس لازم است"; return; }
            if (!target && !meta.name.value.trim()) { err.style.display = ""; err.textContent = "نام الزامی است"; return; }
            const fd = new FormData();
            const chosen = [];
            let missing = false;
            photos.forEach((p) => {
              if (!p.file) { missing = true; return; }
              fd.append("files", p.file);
              chosen.push(p.chosen);
            });
            if (missing) {
              // camera-captured stills take the dedicated path (one at a time)
              err.style.display = "";
              err.textContent = "قاب‌های دوربین را جداگانه ذخیره کنید یا به‌جای آن فایل بارگذاری کنید.";
              return;
            }
            fd.append("faces", JSON.stringify(chosen));
            fd.append("metadata", JSON.stringify({
              name: meta.name.value.trim(), family_name: meta.family_name.value.trim(),
              age: meta.age.value || null, gender: meta.gender.value, role: meta.role.value,
              national_number: meta.national_number.value.trim(), description: meta.description.value.trim(),
            }));
            if (target) fd.append("identity_id", String(target));
            try {
              await window.API.form("/api/enrolment/save", fd);
              toast("ثبت شد", "ok");
              c();
              if (target) openPerson(target); else loadGrid();
            } catch (e) { err.style.display = ""; err.textContent = e.message; }
          } },
      ],
    });
    err.style.display = "none";
    renderPhotos();
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

  window.PagePeople = { mount, openEnrolment, openPerson };
})();
