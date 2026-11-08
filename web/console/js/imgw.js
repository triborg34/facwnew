/* FACW console - image helpers: zoom/pan stage and an interactive crop editor.
 *
 * Both are plain DOM components (no canvas dependency for the zoom stage) so
 * the very same widget serves the enrolment preview, the unknown-person
 * capture and the detection detail panel.
 */
(function () {
  "use strict";
  const { h } = window.UI;

  const MAX_SCALE = 6;
  const MIN_SCALE = 1;

  /**
   * zoomable(src, { maxHeight }) -> { el, setSrc, fit, setScale, scale }
   *
   * Wheel = zoom around the cursor, drag = pan, double-click = 100%/fit.
   * The image is only as large as the stage until the user zooms, so a huge
   * camera frame can never overflow the dialog.
   */
  function zoomable(src, opts) {
    opts = opts || {};
    const img = h("img", { src: src || "", alt: "preview", draggable: "false" });
    const inner = h("div", { class: "inner" }, img);
    const stage = h("div", { class: "zoom-stage", style: opts.maxHeight ? `max-height:${opts.maxHeight}` : null }, inner);
    const label = h("span", { class: "badge" }, "100\u066A");
    let scale = 1;
    let fitted = true;

    function apply(anchor) {
      const prev = scale;
      scale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, scale));
      inner.style.transform = `scale(${scale})`;
      fitted = scale <= MIN_SCALE + 0.001;
      const n = Math.round(scale * 100);
      label.textContent = (window.I18N ? window.I18N.toFa(n) : String(n)) + "\u066A";
      if (anchor && prev !== scale) {
        // keep the point under the cursor fixed
        stage.scrollLeft = (stage.scrollLeft + anchor.x) * (scale / prev) - anchor.x;
        stage.scrollTop = (stage.scrollTop + anchor.y) * (scale / prev) - anchor.y;
      }
      return scale;
    }

    function fit() {
      scale = 1;
      apply();
    }

    stage.addEventListener(
      "wheel",
      (ev) => {
        if (ev.deltaY === 0) return;
        ev.preventDefault();
        const rect = stage.getBoundingClientRect();
        const anchor = { x: ev.clientX - rect.left, y: ev.clientY - rect.top };
        scale += ev.deltaY < 0 ? 0.25 : -0.25;
        apply({ x: anchor.x + stage.scrollLeft, y: anchor.y + stage.scrollTop });
      },
      { passive: false }
    );
    let drag = null;
    stage.addEventListener("pointerdown", (ev) => {
      if (scale <= 1) return;
      drag = { x: ev.clientX, y: ev.clientY, sl: stage.scrollLeft, st: stage.scrollTop };
      stage.classList.add("dragging");
      stage.setPointerCapture(ev.pointerId);
    });
    stage.addEventListener("pointermove", (ev) => {
      if (!drag) return;
      stage.scrollLeft = drag.sl - (ev.clientX - drag.x);
      stage.scrollTop = drag.st - (ev.clientY - drag.y);
    });
    const stopDrag = () => {
      drag = null;
      stage.classList.remove("dragging");
    };
    stage.addEventListener("pointerup", stopDrag);
    stage.addEventListener("pointercancel", stopDrag);
    stage.addEventListener("dblclick", () => {
      if (fitted) {
        scale = 2;
        apply();
      } else {
        fit();
      }
    });

    const tools = h("div", { class: "zoom-tools" },
      h("button", { class: "btn sm", type: "button", title: "کوچک‌نمایی", onclick: () => { scale -= 0.5; apply(); } }, "\u2212"),
      label,
      h("button", { class: "btn sm", type: "button", title: "بزرگ‌نمایی", onclick: () => { scale += 0.5; apply(); } }, "+"),
      h("button", { class: "btn sm ghost", type: "button", onclick: fit }, "اندازهٔ اصلی"),
      h("button", { class: "btn sm ghost", type: "button", onclick: () => { scale = 3; apply(); } }, "3\u00D7")
    );
    tools.style.direction = "ltr";

    const el = h("div", {}, stage, tools);
    return {
      el,
      img,
      stage,
      label,
      setSrc(next) {
        img.src = next || "";
        fit();
      },
      fit,
      setScale(s) { scale = s; apply(); },
      zoomIn() { scale += 0.5; apply(); },
      zoomOut() { scale -= 0.5; apply(); },
      get scale() { return scale; },
    };
  }

  /**
   * cropEditor(opts) -> component
   *
   * Drag a rectangle over a *still frame* to cut it down to the face region
   * before face detection - the operator never has to fight a wide camera
   * shot.  Double-click resets to the full frame.  The result is exposed as
   * `dataUrl()` (JPEG) and reused by the enrolment form.
   */
  function cropEditor(opts) {
    opts = opts || {};
    const canvas = h("canvas", { width: 1, height: 1 });
    const stage = h("div", { class: "crop-stage" }, canvas);
    const info = h("div", { class: "muted", style: "font-size:11.5px" }, "برای برش، روی تصویر بکشید؛ دوبار کلیک = حذف برش");
    const el = h("div", { class: "crop-editor" }, stage, info);
    let img = null;          // source image (HTMLImageElement)
    let rect = null;         // normalised crop {x, y, w, h} or null
    let drag = null;

    function draw() {
      if (!img || !img.naturalWidth) return;
      const w = img.naturalWidth;
      const hgt = img.naturalHeight;
      const scale = Math.min(1, 640 / w, 400 / hgt);
      canvas.width = Math.max(1, Math.round(w * scale));
      canvas.height = Math.max(1, Math.round(hgt * scale));
      const ctx = canvas.getContext("2d");
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      if (rect) {
        const x = rect.x * canvas.width;
        const y = rect.y * canvas.height;
        const cw = rect.w * canvas.width;
        const ch = rect.h * canvas.height;
        ctx.fillStyle = "rgba(0,0,0,0.45)";
        ctx.fillRect(0, 0, canvas.width, y);
        ctx.fillRect(0, y + ch, canvas.width, canvas.height - y - ch);
        ctx.fillRect(0, y, x, ch);
        ctx.fillRect(x + cw, y, canvas.width - x - cw, ch);
        ctx.strokeStyle = "#4ade80";
        ctx.lineWidth = 2;
        ctx.strokeRect(x, y, cw, ch);
      }
    }

    function point(ev) {
      const r = canvas.getBoundingClientRect();
      return {
        x: Math.max(0, Math.min(1, (ev.clientX - r.left) / Math.max(1, r.width))),
        y: Math.max(0, Math.min(1, (ev.clientY - r.top) / Math.max(1, r.height))),
      };
    }

    canvas.addEventListener("pointerdown", (ev) => {
      drag = point(ev);
      canvas.setPointerCapture(ev.pointerId);
    });
    canvas.addEventListener("pointermove", (ev) => {
      if (!drag) return;
      const p = point(ev);
      rect = {
        x: Math.min(drag.x, p.x),
        y: Math.min(drag.y, p.y),
        w: Math.abs(p.x - drag.x),
        h: Math.abs(p.y - drag.y),
      };
      if (rect.w < 0.02 || rect.h < 0.02) rect = null;
      draw();
    });
    const end = () => {
      if (drag) info.textContent = rect ? "برش اعمال می‌شود؛ دوبار کلیک برای حذف" : "برای برش، روی تصویر بکشید";
      drag = null;
    };
    canvas.addEventListener("pointerup", end);
    canvas.addEventListener("pointercancel", end);
    canvas.addEventListener("dblclick", () => {
      rect = null;
      info.textContent = "برش حذف شد - کل قاب استفاده می‌شود";
      draw();
    });

    function dataUrl(maxSide) {
      if (!img || !img.naturalWidth) return "";
      const cap = maxSide || 1280;
      const box = rect || { x: 0, y: 0, w: 1, h: 1 };
      let sx = Math.round(box.x * img.naturalWidth);
      let sy = Math.round(box.y * img.naturalHeight);
      let sw = Math.max(8, Math.round(box.w * img.naturalWidth));
      let sh = Math.max(8, Math.round(box.h * img.naturalHeight));
      sx = Math.min(sx, img.naturalWidth - 1);
      sy = Math.min(sy, img.naturalHeight - 1);
      sw = Math.min(sw, img.naturalWidth - sx);
      sh = Math.min(sh, img.naturalHeight - sy);
      const scale = Math.min(1, cap / Math.max(sw, sh));
      const out = h("canvas", { width: Math.max(1, Math.round(sw * scale)), height: Math.max(1, Math.round(sh * scale)) });
      out.getContext("2d").drawImage(img, sx, sy, sw, sh, 0, 0, out.width, out.height);
      return out.toDataURL("image/jpeg", 0.92);
    }

    function load(src) {
      return new Promise((resolve, reject) => {
        const next = new Image();
        next.onload = () => {
          img = next;
          rect = null;
          draw();
          info.textContent = "برای برش، روی تصویر بکشید؛ دوبار کلیک = حذف برش";
          resolve(next);
        };
        next.onerror = () => reject(new Error("تصویر بارگذاری نشد"));
        next.src = src;
      });
    }

    return { el, stage, canvas, load, dataUrl, reset: () => { rect = null; draw(); }, get hasCrop() { return !!rect; } };
  }

  window.IMGW = { zoomable, cropEditor };
})();
