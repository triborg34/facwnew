/* Aman Negar console - charts: dependency-free SVG gauges for CPU, RAM and disk.
 *
 * The console has no build step and no chart library, so the three resource
 * charts are drawn as plain SVG.  Everything is rebuilt from the sample
 * history on every poll, which is cheap for a few dozen points and avoids the
 * bookkeeping a canvas renderer would need.
 */
(function () {
  "use strict";
  const { h } = window.UI;
  const I = window.I18N;
  const NS = "http://www.w3.org/2000/svg";

  const svgEl = (tag, attrs) => {
    const el = document.createElementNS(NS, tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (v === null || v === undefined || v === false) return;
      el.setAttribute(k, String(v));
    });
    return el;
  };

  // A percentage already expressed as 0..100 (the API reports it that way),
  // rendered with Persian digits.
  const pct100 = (v, digits) =>
    (v === null || v === undefined || v === "") ? "—" : I.num(Number(v).toFixed(digits === undefined ? 1 : digits)) + "٪";

  // ---- area chart --------------------------------------------------------
  // Percentage series over time.  The SVG is stretched to the cell width
  // (preserveAspectRatio="none"), so the stroke width is pinned with
  // vector-effect to keep the line crisp.
  function areaChart(values, opts) {
    const o = opts || {};
    const W = 260, H = 86, PAD = 5;
    const max = o.max || 100;
    const color = o.color || "#38bdf8";
    const svg = svgEl("svg", {
      viewBox: `0 0 ${W} ${H}`,
      class: "chart-svg",
      preserveAspectRatio: "none",
      role: "img",
      "aria-label": o.label || "",
    });

    [0, 0.5, 1].forEach((f) => {
      const y = PAD + (H - 2 * PAD) * f;
      svg.appendChild(svgEl("line", { x1: 0, x2: W, y1: y, y2: y, class: "chart-grid" }));
    });

    const n = (values || []).length;
    if (n) {
      const step = n > 1 ? (W - 2 * PAD) / (n - 1) : 0;
      const pts = values.map((v, i) => {
        const x = PAD + i * step;
        const clamped = Math.max(0, Math.min(max, Number(v) || 0));
        const y = PAD + (H - 2 * PAD) * (1 - clamped / max);
        return [x, y];
      });
      const line = pts
        .map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1))
        .join(" ");

      const gid = "cg" + Math.random().toString(36).slice(2, 9);
      const defs = svgEl("defs", {});
      const grad = svgEl("linearGradient", { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 });
      grad.appendChild(svgEl("stop", { offset: "0%", "stop-color": color, "stop-opacity": "0.38" }));
      grad.appendChild(svgEl("stop", { offset: "100%", "stop-color": color, "stop-opacity": "0.02" }));
      defs.appendChild(grad);
      svg.appendChild(defs);

      const area = `${line} L ${pts[n - 1][0].toFixed(1)} ${H - PAD} L ${pts[0][0].toFixed(1)} ${H - PAD} Z`;
      svg.appendChild(svgEl("path", { d: area, fill: `url(#${gid})`, stroke: "none" }));
      svg.appendChild(svgEl("path", {
        d: line, fill: "none", stroke: color, "stroke-width": 2,
        "stroke-linejoin": "round", "stroke-linecap": "round",
        "vector-effect": "non-scaling-stroke",
      }));
      // "now" marker
      svg.appendChild(svgEl("line", {
        x1: pts[n - 1][0], x2: pts[n - 1][0], y1: PAD, y2: H - PAD,
        class: "chart-now", stroke: color, "vector-effect": "non-scaling-stroke",
      }));
    }
    return svg;
  }

  // ---- donut gauge -------------------------------------------------------
  function donut(percent, opts) {
    const o = opts || {};
    const size = o.size || 112;
    const stroke = o.stroke || 10;
    const r = size / 2 - stroke / 2 - 1;
    const c = 2 * Math.PI * r;
    const p = Math.max(0, Math.min(100, Number(percent) || 0));
    const color = o.color || "#38bdf8";
    const svg = svgEl("svg", { viewBox: `0 0 ${size} ${size}`, class: "chart-donut", role: "img", "aria-label": o.label || "" });
    svg.appendChild(svgEl("circle", {
      cx: size / 2, cy: size / 2, r, fill: "none",
      stroke: "var(--border)", "stroke-width": stroke,
    }));
    svg.appendChild(svgEl("circle", {
      cx: size / 2, cy: size / 2, r, fill: "none", stroke: color,
      "stroke-width": stroke, "stroke-linecap": "round",
      "stroke-dasharray": `${((c * p) / 100).toFixed(2)} ${c.toFixed(2)}`,
      transform: `rotate(-90 ${size / 2} ${size / 2})`,
    }));
    return svg;
  }

  // ---- resource card -----------------------------------------------------
  const MAX_SAMPLES = 40;

  function gb(mb) {
    if (mb === null || mb === undefined) return "—";
    return I.num((Number(mb) / 1024).toFixed(1)) + " گیگابایت";
  }

  // Returns { el, update }.  ``update`` takes the /api/resources payload and
  // repaints the three charts; the CPU/RAM series keep a rolling history.
  function resourcePanel() {
    const hist = { cpu: [], ram: [] };

    const cpuValue = h("div", { class: "chart-val" }, "—");
    const ramValue = h("div", { class: "chart-val" }, "—");
    const diskValue = h("div", { class: "chart-val" }, "—");
    const cpuBody = h("div", { class: "chart-body" });
    const ramBody = h("div", { class: "chart-body" });
    const diskBody = h("div", { class: "chart-body chart-body-donut" });
    const diskNote = h("div", { class: "chart-note muted" }, "—");

    const cell = (title, valueEl, bodyEl, noteEl) =>
      h("div", { class: "chart-cell" },
        h("div", { class: "chart-head" }, h("span", { class: "chart-title" }, title), valueEl),
        bodyEl,
        noteEl || null);

    const el = h("div", { class: "card res-card" },
      h("div", { class: "card-head" },
        h("span", {}, "منابع سیستم"),
        h("div", { class: "spacer" }),
        h("span", { class: "badge" }, "زنده")),
      h("div", { class: "res-grid" },
        cell("پردازنده (CPU)", cpuValue, cpuBody),
        cell("حافظه (RAM)", ramValue, ramBody),
        cell("دیسک", diskValue, diskBody, diskNote)));

    function update(res) {
      if (!res) return;
      const cpu = res.cpu_system_percent;
      const ram = res.memory_percent;

      if (cpu !== null && cpu !== undefined) {
        hist.cpu.push(Number(cpu));
        if (hist.cpu.length > MAX_SAMPLES) hist.cpu.shift();
      }
      if (ram !== null && ram !== undefined) {
        hist.ram.push(Number(ram));
        if (hist.ram.length > MAX_SAMPLES) hist.ram.shift();
      }

      cpuValue.textContent = pct100(cpu, 1);
      ramValue.textContent = pct100(ram, 1);

      cpuBody.replaceChildren(areaChart(hist.cpu, { color: "#38bdf8", label: "CPU" }));
      ramBody.replaceChildren(areaChart(hist.ram, { color: "#a78bfa", label: "RAM" }));

      const disk = res.disk;
      if (disk) {
        diskValue.textContent = pct100(disk.percent, 1);
        diskBody.replaceChildren(donut(disk.percent, { color: "#34d399", label: "دیسک" }));
        diskNote.textContent =
          `${gb(disk.used_mb)} از ${gb(disk.total_mb)} — ${gb(disk.free_mb)} آزاد`;
      } else {
        diskValue.textContent = "—";
        diskBody.replaceChildren(h("div", { class: "muted" }, "اطلاعات دیسک در دسترس نیست"));
        diskNote.textContent = "—";
      }
    }

    return { el, update, history: hist };
  }

  window.Charts = { areaChart, donut, resourcePanel, pct100, gb };
})();
