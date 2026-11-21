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

  // ---- overall statistics -------------------------------------------------
  // A KPI tile: label, the big number, and a small note underneath.
  function statTile(label, value, sub, tone) {
    return h("div", { class: "stat-tile" + (tone ? " stat-" + tone : "") },
      h("div", { class: "stat-label" }, label),
      h("div", { class: "stat-value" }, value),
      h("div", { class: "stat-sub" }, sub || ""));
  }

  // Per-day detection bars.  Plain HTML instead of SVG: with thirty bars the
  // rectangles are thin, and HTML keeps the date labels crisp instead of
  // stretching them with the SVG view box.
  function dailyBars(days) {
    const list = days || [];
    const max = Math.max(1, ...list.map((d) => d.total || 0));
    const wrap = h("div", { class: "bars" });
    list.forEach((d) => {
      const idH = d.total ? ((d.identified || 0) / max) * 100 : 0;
      const unkH = d.total ? ((d.total - (d.identified || 0)) / max) * 100 : 0;
      wrap.appendChild(
        h("div", {
          class: "bar-col",
          title: I.formatDate(d.date) + " — " + I.num(d.total) + " تشخیص",
        },
          h("div", { class: "bar-stack" },
            h("div", { class: "bar-seg bar-unknown", style: "height:" + unkH.toFixed(2) + "%" }),
            h("div", { class: "bar-seg bar-identified", style: "height:" + idH.toFixed(2) + "%" })))
      );
    });
    return wrap;
  }

  // The dashboard summary: how many faces have been seen in total, how many
  // were recognised, and the per-day series behind those numbers.
  function statsPanel() {
    const tiles = {
      total: statTile("کل تشخیص‌ها", "—", ""),
      identified: statTile("چهره‌های شناسایی‌شده", "—", "", "ok"),
      unknown: statTile("چهره‌های ناشناس", "—", "", "dim"),
      people: statTile("افراد یکتای دیده‌شده", "—", ""),
      registered: statTile("افراد ثبت‌شده", "—", ""),
      today: statTile("امروز", "—", ""),
      day: statTile("۲۴ ساعت اخیر", "—", ""),
      score: statTile("میانگین امتیاز تطابق", "—", ""),
    };
    const splitValue = h("div", { class: "chart-val" }, "—");
    const splitBody = h("div", { class: "chart-body chart-body-donut" });
    const splitNote = h("div", { class: "chart-note muted" }, "—");

    const barsHost = h("div", { class: "bars-host" });
    const barsAxis = h("div", { class: "bars-axis" });
    const barsNote = h("div", { class: "chart-note muted" }, "—");

    const el = h("div", { class: "card stats-card" },
      h("div", { class: "card-head" },
        h("span", {}, "آمار کلی"),
        h("div", { class: "spacer" }),
        h("span", { class: "badge" }, "از ابتدای کار")),
      h("div", { class: "stat-grid" },
        tiles.total, tiles.identified, tiles.unknown, tiles.people,
        tiles.registered, tiles.today, tiles.day, tiles.score),
      h("div", { class: "stats-charts" },
        h("div", { class: "chart-cell" },
          h("div", { class: "chart-head" }, h("span", { class: "chart-title" }, "تفکیک شناسایی"), splitValue),
          splitBody,
          splitNote),
        h("div", { class: "chart-cell" },
          h("div", { class: "chart-head" }, h("span", { class: "chart-title" }, "تشخیص‌های روزانه")),
          barsHost,
          barsAxis,
          barsNote)));

    function setTile(t, value, sub) {
      t.querySelector(".stat-value").textContent = value;
      t.querySelector(".stat-sub").textContent = sub || "";
    }

    function update(s) {
      if (!s) return;
      const total = Number(s.total) || 0;
      const identified = Number(s.identified) || 0;
      const unknown = Number(s.unknown) || 0;
      const since = s.first_date ? I.formatDate(s.first_date) : "";

      setTile(tiles.total, I.num(total), since ? "از " + since : "—");
      setTile(tiles.identified, I.num(identified), "نرخ شناسایی " + I.num(s.identify_rate, 1) + "٪");
      setTile(tiles.unknown, I.num(unknown), total ? I.num(Math.round((unknown / total) * 100)) + "٪ از کل" : "—");
      setTile(tiles.people, I.num(s.unique_people), "نفر متفاوت");
      setTile(tiles.registered, I.num(s.identities), I.num(s.reference_photos) + " عکس مرجع");
      setTile(tiles.today, I.num(s.today), "تشخیص");
      setTile(tiles.day, I.num(s.last_24h), I.num(s.per_minute, 2) + " در دقیقه");
      setTile(tiles.score, s.avg_score ? I.num((Number(s.avg_score) * 100).toFixed(0)) + "٪" : "—", "میانگین شباهت");

      const rate = Number(s.identify_rate) || 0;
      splitValue.textContent = pct100(rate, 1);
      splitBody.replaceChildren(donut(rate, { color: "#34d399", label: "نرخ شناسایی" }));
      splitNote.textContent = I.num(identified) + " شناسایی‌شده · " + I.num(unknown) + " ناشناس";

      const daily = s.daily || [];
      barsHost.replaceChildren(dailyBars(daily));
      if (daily.length) {
        const peak = daily.reduce((a, b) => ((b.total || 0) > (a.total || 0) ? b : a), daily[0]);
        const inWindow = daily.reduce((sum, d) => sum + (d.total || 0), 0);
        barsAxis.replaceChildren(
          h("span", {}, I.formatDate(daily[0].date)),
          h("span", {}, I.formatDate(daily[daily.length - 1].date)));
        barsNote.textContent = daily.length + " روز اخیر · " + I.num(inWindow) + " تشخیص"
          + (peak.total ? " · بیشترین: " + I.num(peak.total) + " در " + I.formatDate(peak.date) : "");
      } else {
        barsAxis.replaceChildren();
        barsNote.textContent = "—";
      }
    }

    return { el, update };
  }

  window.Charts = { areaChart, donut, resourcePanel, statsPanel, statTile, dailyBars, pct100, gb };
})();
