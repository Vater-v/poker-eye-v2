/* PokerEYE panel helper — paste into DevTools console on https://mobile.eye-panel.com
   after you are logged in.

   Native APK has no CSRegister. New game logins are created by POST /api/common/loginAccountReq
   with partnerAuthKey + unique x-android-id. Server returns deviceId + credentialsV2.

   Usage:
     await pe.tap()          // start logging API calls (secrets redacted)
     await pe.me()           // who am I / current account
     await pe.list()         // try account list endpoints
     await pe.registerOne()  // create ONE new login with a random android-id (asks confirm)
     await pe.registerMany(3)// create N new logins, 1.5s apart
     pe.untap()
*/
(() => {
  const redacted = (v) => {
    if (v == null) return v;
    if (typeof v === "string") {
      if (v.length > 12 && /:/.test(v)) return `<cred ${v.split(":")[0]}:***>`;
      if (/(pass|token|auth|secret|key)/i.test(v) && v.length > 8) return `<redacted ${v.length}>`;
      return v;
    }
    if (Array.isArray(v)) return v.slice(0, 30).map(redacted);
    if (typeof v === "object") {
      const o = {};
      for (const [k, val] of Object.entries(v)) {
        o[k] = /(pass|token|auth|secret|credential|cookie|key)/i.test(k)
          ? `<redacted>`
          : redacted(val);
      }
      return o;
    }
    return v;
  };

  const log = (...a) => console.log("%c[pe]", "color:#70a7ff;font-weight:700", ...a);

  async function parseBody(res) {
    const text = await res.text();
    try { return JSON.parse(text); } catch { return text.slice(0, 500); }
  }

  function androidId() {
    try { return window.Native && window.Native.getAndroidID && window.Native.getAndroidID(); }
    catch { return ""; }
  }

  function partnerKey(explicit) {
    if (explicit) return explicit;
    try {
      if (window.Native && window.Native.getPrefsString)
        return window.Native.getPrefsString("partnerAuthKey", "") || "";
    } catch {}
    return (
      sessionStorage.getItem("partnerAuthKey") ||
      localStorage.getItem("partnerAuthKey") ||
      ""
    );
  }

  function client() {
    return window.mirror || null;
  }

  async function api(path, { method = "POST", body, android } = {}) {
    const headers = { "content-type": "application/json" };
    const aid = android || androidId();
    if (aid) headers["x-android-id"] = aid;
    const url = path.startsWith("http") ? path : path;
    const res = await fetch(url, {
      method,
      headers,
      credentials: "include",
      body: method === "GET" || body == null ? undefined : JSON.stringify(body),
    });
    const data = await parseBody(res.clone ? res : { text: async () => "" });
    const out = { ok: res.ok, status: res.status, path, data: redacted(data), raw: data };
    log(method, path, res.status, out.data);
    return out;
  }

  let tapped = false;
  const origFetch = window.fetch.bind(window);

  async function tap() {
    if (tapped) return "already tapping";
    tapped = true;
    window.fetch = async function (input, init) {
      const url = String(input && input.url ? input.url : input);
      const method = (init && init.method) || (input && input.method) || "GET";
      const res = await origFetch(input, init);
      if (/eye-panel\.com|\/api\//i.test(url)) {
        try {
          const copy = res.clone();
          const body = await parseBody(copy);
          log(method, url.replace(/https?:\/\/[^/]+/, ""), copy.status, redacted(body));
        } catch (e) {
          log(method, url, "tap-error", String(e));
        }
      }
      return res;
    };
    log("network tap on. Do one UI action (open Accounts / login a room).");
    return "ok";
  }

  function untap() {
    window.fetch = origFetch;
    tapped = false;
    log("network tap off");
  }

  async function me() {
    const logged = await api("/api/isLoggedIn", { method: "GET" });
    const acc = await api("/api/accounts/loggedAccountDataReq", { body: {} });
    const partner = await api("/api/accounts/loggedPartnerDataReq", { body: {} });
    return { logged, acc, partner, android: androidId() || null, partnerAuthKey: partnerKey() ? "<present>" : "<missing>" };
  }

  async function list() {
    const bodies = [{}, { page: 1 }, { page: 0 }, { limit: 100 }, { offset: 0, limit: 100 }];
    const rows = [];
    for (const body of bodies) {
      const r = await api("/api/accounts/accountsReqN", { body });
      rows.push(r);
      if (r.ok && r.raw && (Array.isArray(r.raw.data) || r.raw.data?.accounts || r.raw.data?.list))
        return r;
    }
    const extra = await api("/api/accounts/accountsFromSessionsReqV2", { body: {} });
    return { tried: rows, extra };
  }

  function randomAndroid() {
    const hex = [...crypto.getRandomValues(new Uint8Array(8))]
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("");
    return hex;
  }

  async function registerOne({ key, android, dry = false } = {}) {
    const partnerAuthKey = partnerKey(key);
    if (!partnerAuthKey) {
      throw new Error("no partnerAuthKey — log into the panel first, or pe.registerOne({key:'YOUR_AGENT'})");
    }
    const id = android || randomAndroid();
    const body = { partnerAuthKey };
    log("loginAccountReq", { android: id, dry, body: { partnerAuthKey: "<present>" } });
    if (dry) return { dry: true, android: id, body };
    const r = await api("/api/common/loginAccountReq", { body, android: id });
    const data = r.raw && r.raw.data;
    const cred = data && data.credentialsV2;
    const [user] = String(cred || "").split(":");
    return {
      status: r.status,
      ok: r.ok,
      android: id,
      deviceId: data && data.deviceId,
      accId: data && data.accId,
      login: user || null,
      enabled: data && data.enabled,
      error: (data && (data.error || data.message || data.code)) || (!r.ok && r.data),
    };
  }

  async function registerMany(n = 1, opts = {}) {
    const out = [];
    for (let i = 0; i < n; i++) {
      const row = await registerOne(opts);
      out.push(row);
      log("created", row.login || row.error || row);
      if (i + 1 < n) await new Promise((r) => setTimeout(r, 1500));
    }
    return out;
  }

  window.pe = { tap, untap, me, list, registerOne, registerMany, api, partnerKey, androidId };
  log("ready. Next: await pe.me()  then  await pe.list()  then  await pe.registerOne()");
  return window.pe;
})();
