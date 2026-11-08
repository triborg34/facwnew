/* FACW console - API client.
 * One place for transport, error decoding and 401 handling.
 */
(function () {
  "use strict";

  class ApiError extends Error {
    constructor(message, status, detail) {
      super(message);
      this.status = status;
      this.detail = detail;
    }
  }

  async function request(method, path, body, opts) {
    opts = opts || {};
    const init = { method, credentials: "same-origin", headers: {} };
    if (body instanceof FormData) {
      init.body = body;
    } else if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, init);
    } catch (err) {
      throw new ApiError("network unreachable", 0, String(err));
    }
    if (res.status === 401 && !opts.allow401) {
      // A 401 during the initial /auth/me probe is the normal "not signed in
      // yet" case; it must not fire a sign-out cascade (spurious console noise).
      if (!opts.probe) window.dispatchEvent(new CustomEvent("facw:signedout"));
      throw new ApiError("not signed in", 401, "not signed in");
    }
    const ct = res.headers.get("content-type") || "";
    if (!res.ok) {
      let detail = res.statusText;
      if (ct.includes("application/json")) {
        try {
          const j = await res.json();
          detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail || j);
        } catch (e) { /* keep statusText */ }
      }
      throw new ApiError(detail || "request failed", res.status, detail);
    }
    if (ct.includes("application/json")) return res.json();
    return res;
  }

  function qs(params) {
    const p = new URLSearchParams();
    Object.keys(params || {}).forEach((k) => {
      const v = params[k];
      if (v !== undefined && v !== null && v !== "") p.set(k, v);
    });
    const s = p.toString();
    return s ? "?" + s : "";
  }

  window.API = {
    ApiError,
    get: (p, params) => request("GET", p + qs(params)),
    post: (p, body) => request("POST", p, body === undefined ? {} : body),
    put: (p, body) => request("PUT", p, body),
    del: (p) => request("DELETE", p),
    form: (p, formData) => request("POST", p, formData),
    me: () => request("GET", "/api/auth/me", undefined, { allow401: true, probe: true }),
    login: (login_name, password) =>
      request("POST", "/api/auth/login-cookie", { login_name, password }, { allow401: true, probe: true }),
    logout: () => request("POST", "/api/auth/logout", {}, { allow401: true }),
    exportUrl: (params) => "/api/events/export" + qs(params),
    videoUrl: (id, flavour) => `/video/${id}?flavour=${flavour || "annotated"}`,
    mediaUrl: (rel) => {
      if (!rel) return "";
      const parts = String(rel).split("/");
      if (parts.length !== 2) return "";
      return `/media/${parts[0]}/${parts[1]}`;
    },
  };
})();
