/* FACW console - internationalisation.
 *
 * Right-to-left layout is the default.  Dates are shown in the Persian
 * (Jalali) calendar, digits are localised to Persian, and both directions are
 * handled correctly (the layout mirrors, the values do not).
 */
(function () {
  "use strict";

  const FA_DIGITS = ["\u06F0", "\u06F1", "\u06F2", "\u06F3", "\u06F4", "\u06F5", "\u06F6", "\u06F7", "\u06F8", "\u06F9"];
  const MONTHS_FA = ["\u0641\u0631\u0648\u0631\u062F\u06CC\u0646", "\u0627\u0631\u062F\u06CC\u0628\u0647\u0634\u062A", "\u062E\u0631\u062F\u0627\u062F", "\u062A\u06CC\u0631", "\u0645\u0631\u062F\u0627\u062F", "\u0634\u0647\u0631\u06CC\u0648\u0631", "\u0645\u0647\u0631", "\u0622\u0628\u0627\u0646", "\u0622\u0630\u0631", "\u062F\u06CC", "\u0628\u0647\u0645\u0646", "\u0627\u0633\u0641\u0646\u062F"];
  const WEEKDAYS_FA = ["\u0634", "\u06CC", "\u062F", "\u0633", "\u0686", "\u067E", "\u062C"];

  // --- digit conversion ---------------------------------------------------
  function toFa(value) {
    return String(value).replace(/[0-9]/g, (d) => FA_DIGITS[+d]);
  }
  function toEn(value) {
    return String(value)
      .replace(/[\u06F0-\u06F9]/g, (d) => String(d.charCodeAt(0) - 0x06f0))
      .replace(/[\u0660-\u0669]/g, (d) => String(d.charCodeAt(0) - 0x0660));
  }

  // --- Gregorian <-> Jalali ----------------------------------------------
  // Standard algorithm (Borkowski).  Verified by the assertions in i18n.test.
  function g2j(gy, gm, gd) {
    const g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334];
    let jy = gy <= 1600 ? 0 : 979;
    gy -= gy <= 1600 ? 621 : 1600;
    const gy2 = gm > 2 ? gy + 1 : gy;
    let days =
      365 * gy +
      Math.floor((gy2 + 3) / 4) -
      Math.floor((gy2 + 99) / 100) +
      Math.floor((gy2 + 399) / 400) -
      80 +
      gd +
      g_d_m[gm - 1];
    jy += 33 * Math.floor(days / 12053);
    days %= 12053;
    jy += 4 * Math.floor(days / 1461);
    days %= 1461;
    if (days > 365) {
      jy += Math.floor((days - 1) / 365);
      days = (days - 1) % 365;
    }
    const jm = days < 186 ? 1 + Math.floor(days / 31) : 7 + Math.floor((days - 186) / 30);
    const jd = 1 + (days < 186 ? days % 31 : (days - 186) % 30);
    return { jy: jy, jm: jm, jd: jd };
  }

  function j2g(jy, jm, jd) {
    let gy = jy <= 979 ? 621 : 1600;
    jy -= jy <= 979 ? 0 : 979;
    let days =
      365 * jy +
      Math.floor(jy / 33) * 8 +
      Math.floor(((jy % 33) + 3) / 4) +
      78 +
      jd +
      (jm < 7 ? (jm - 1) * 31 : (jm - 7) * 30 + 186);
    gy += 400 * Math.floor(days / 146097);
    days %= 146097;
    if (days > 36524) {
      gy += 100 * Math.floor(--days / 36524);
      days %= 36524;
      if (days >= 365) days++;
    }
    gy += 4 * Math.floor(days / 1461);
    days %= 1461;
    if (days > 365) {
      gy += Math.floor((days - 1) / 365);
      days = (days - 1) % 365;
    }
    let gd = days + 1;
    const leap = (gy % 4 === 0 && gy % 100 !== 0) || gy % 400 === 0;
    const sal = [0, 31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    let gm = 0;
    for (gm = 1; gm <= 12; gm++) {
      if (gd <= sal[gm]) break;
      gd -= sal[gm];
    }
    return { gy: gy, gm: gm, gd: gd };
  }

  function isJalaliLeap(jy) {
    return ((25 * jy + 11) % 33) < 8;
  }

  function jalaliMonthLength(jy, jm) {
    if (jm <= 6) return 31;
    if (jm <= 11) return 30;
    return isJalaliLeap(jy) ? 30 : 29;
  }

  // --- formatting ---------------------------------------------------------
  // "2024-03-21" -> "1403/01/02" (Persian digits)
  function formatDate(iso, opts) {
    opts = opts || {};
    if (!iso) return "\u2014";
    const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (!m) return String(iso);
    const j = g2j(+m[1], +m[2], +m[3]);
    const text = opts.digits === false
      ? `${j.jy}/${String(j.jm).padStart(2, "0")}/${String(j.jd).padStart(2, "0")}`
      : toFa(`${j.jy}/${String(j.jm).padStart(2, "0")}/${String(j.jd).padStart(2, "0")}`);
    if (opts.long) {
      const long = `${toFa(j.jd)} ${MONTHS_FA[j.jm - 1]} ${toFa(j.jy)}`;
      return text.length ? long : long;
    }
    return text;
  }

  function formatTime(hms, opts) {
    opts = opts || {};
    if (!hms) return "\u2014";
    const s = String(hms).slice(0, 8);
    return opts.digits === false ? s : toFa(s);
  }

  function formatDateTime(dateStr, timeStr, opts) {
    return `${formatDate(dateStr, opts)} \u2013 ${formatTime(timeStr, opts)}`;
  }

  function num(value, digits) {
    if (value === null || value === undefined || value === "") return "\u2014";
    if (typeof value === "number") {
      const fixed = digits === undefined ? String(value) : value.toFixed(digits);
      return toFa(fixed);
    }
    return toFa(value);
  }

  function pct(value, digits) {
    if (value === null || value === undefined) return "\u2014";
    return toFa((Number(value) * 100).toFixed(digits === undefined ? 0 : digits)) + "\u066A";
  }

  // --- Jalali <-> ISO for API filters -------------------------------------
  function jalaliToIso(jy, jm, jd) {
    const g = j2g(+jy, +jm, +jd);
    return `${g.gy}-${String(g.gm).padStart(2, "0")}-${String(g.gd).padStart(2, "0")}`;
  }
  function isoToJalaliParts(iso) {
    const m = String(iso || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (!m) return null;
    return g2j(+m[1], +m[2], +m[3]);
  }

  window.I18N = {
    dir: "rtl",
    locale: "fa-IR",
    toFa: toFa,
    toEn: toEn,
    g2j: g2j,
    j2g: j2g,
    formatDate: formatDate,
    formatTime: formatTime,
    formatDateTime: formatDateTime,
    num: num,
    pct: pct,
    months: MONTHS_FA,
    weekdays: WEEKDAYS_FA,
    jalaliMonthLength: jalaliMonthLength,
    jalaliToIso: jalaliToIso,
    isoToJalaliParts: isoToJalaliParts,
  };
})();

/* Translucent status labels (English keys -> Persian UI text). */
window.LABELS = {
  approve: "\u0645\u062C\u0627\u0632",
  deny: "\u063A\u06CC\u0631\u0645\u062C\u0627\u0632",
  visitor: "\u0645\u0647\u0645\u0627\u0646",
  colleague: "\u0647\u0645\u06A9\u0627\u0631",
  unknown: "\u0646\u0627\u0634\u0646\u0627\u0633",
  analysing: "\u062F\u0631 \u062D\u0627\u0644 \u062A\u062D\u0644\u06CC\u0644",
  male: "\u0645\u0631\u062F",
  female: "\u0632\u0646",
  notset: "\u0645\u0634\u062E\u0635 \u0646\u06CC\u0633\u062A",
};
