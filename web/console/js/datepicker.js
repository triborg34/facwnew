/* FACW console - Jalali date picker (locale-aware, RTL/LTR safe).
 * Emits an ISO Gregorian date ("YYYY-MM-DD") because the API filters by ISO,
 * but every displayed value is Jalali with localised digits.
 */
(function () {
  "use strict";
  const { h } = window.UI;
  const I = window.I18N;

  function picker(opts) {
    opts = opts || {};
    let selected = opts.value || "";          // ISO
    let parts = I.isoToJalaliParts(selected) || todayParts();
    let view = { jy: parts.jy, jm: parts.jm };
    const onPick = opts.onChange || (() => {});

    const popup = h("div", { class: "datepicker", style: "position:absolute; z-index:60; display:none;" });
    const input = h("input", {
      type: "text",
      value: selected ? I.formatDate(selected) : "",
      placeholder: "\u06CC\u06A9 \u062A\u0627\u0631\u06CC\u062E \u0627\u0646\u062A\u062E\u0627\u0628 \u06A9\u0646\u06CC\u062F",
      readonly: true,
      style: "cursor:pointer;",
    });
    const wrap = h("div", { style: "position:relative;" }, input, popup);

    function todayParts() {
      const now = new Date();
      return I.g2j(now.getFullYear(), now.getMonth() + 1, now.getDate());
    }

    function render() {
      window.UI.clear(popup);
      popup.appendChild(
        h("div", { class: "dp-head" },
          h("button", { type: "button", class: "btn ghost sm", onclick: () => { view.jm -= 1; if (view.jm < 1) { view.jm = 12; view.jy -= 1; } render(); } }, "\u2039"),
          h("div", { class: "title" }, `${I.months[view.jm - 1]} ${I.toFa(view.jy)}`),
          h("button", { type: "button", class: "btn ghost sm", onclick: () => { view.jm += 1; if (view.jm > 12) { view.jm = 1; view.jy += 1; } render(); } }, "\u203A")
        )
      );
      const grid = h("div", { class: "dp-grid" });
      // Persian week starts on Saturday: 0=Sat ... 6=Fri
      I.weekdays.forEach((d) => grid.appendChild(h("div", { class: "dow" }, d)));

      const len = I.jalaliMonthLength(view.jy, view.jm);
      const first = I.j2g(view.jy, view.jm, 1);
      const jsDay = new Date(first.gy, first.gm - 1, first.gd).getDay(); // 0=Sun
      const offset = (jsDay + 1) % 7; // Sat=0
      for (let i = 0; i < offset; i++) grid.appendChild(h("div", { class: "empty" }, ""));

      const today = todayParts();
      for (let day = 1; day <= len; day++) {
        const isSelected = selected && (() => {
          const p = I.isoToJalaliParts(selected);
          return p && p.jy === view.jy && p.jm === view.jm && p.jd === day;
        })();
        const isToday = today.jy === view.jy && today.jm === view.jm && today.jd === day;
        grid.appendChild(
          h("button", {
            type: "button",
            class: (isSelected ? "selected " : "") + (isToday ? "today" : ""),
            onclick: () => {
              selected = I.jalaliToIso(view.jy, view.jm, day);
              input.value = I.formatDate(selected);
              popup.style.display = "none";
              onPick(selected);
              render();
            },
          }, I.toFa(day))
        );
      }
      popup.appendChild(grid);
      popup.appendChild(
        h("div", { style: "display:flex; gap:6px; margin-top:8px;" },
          h("button", { class: "btn sm", type: "button", onclick: () => {
            const t = todayParts();
            view = { jy: t.jy, jm: t.jm };
            selected = I.jalaliToIso(t.jy, t.jm, t.jd);
            input.value = I.formatDate(selected);
            popup.style.display = "none";
            onPick(selected);
            render();
          } }, "\u0627\u0645\u0631\u0648\u0632"),
          h("button", { class: "btn sm ghost", type: "button", onclick: () => {
            selected = ""; input.value = ""; popup.style.display = "none"; onPick(""); render();
          } }, "\u067E\u0627\u06A9 \u06A9\u0631\u062F\u0646")
        )
      );
    }

    input.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const showing = popup.style.display === "block";
      document.querySelectorAll(".datepicker").forEach((p) => { p.style.display = "none"; });
      popup.style.display = showing ? "none" : "block";
      if (!showing) render();
    });
    const closer = () => { popup.style.display = "none"; };
    document.addEventListener("click", closer);
    window.addEventListener("facw:teardown", () => document.removeEventListener("click", closer));

    return {
      el: wrap,
      get value() { return selected; },
      set value(v) { selected = v || ""; parts = I.isoToJalaliParts(selected) || todayParts(); view = { jy: parts.jy, jm: parts.jm }; input.value = selected ? I.formatDate(selected) : ""; },
    };
  }

  window.DatePicker = picker;
})();
