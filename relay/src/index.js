// ALICE aktarma sunucusu (Cloudflare Workers, ücretsiz plan).
//
// Telefon evin dışındayken bilgisayara doğrudan ulaşamaz (CGNAT, kapalı portlar). Bilgisayardaki ALICE bu sunucuya
// kendisi bağlanır ve bağlantıyı açık tutar (/_alice/pc, WebSocket, PC_KEY ile). Telefonun buraya gelen her isteği o
// bağlantı üzerinden bilgisayara iletilir, cevap parça parça geri akar (canlı olay akışı ve arama sesi dahil).
// Kimlik doğrulama değişmez: telefonun anahtarını (t / X-Alice-Token) bilgisayardaki ALICE kontrol eder.
//
// Kurulum: `alice relay deploy` (bkz. alice/app/relay.py). Tek kullanıcılık: her kurulum tek bir bilgisayara aittir.

const CHUNK = 256 * 1024; // WebSocket mesajı başına gövde parçası
const MAX_BODY = 25 * 1024 * 1024;
const HEAD_TIMEOUT = 45000;
const DROP_REQ = new Set(["connection", "keep-alive", "upgrade", "transfer-encoding", "accept-encoding", "cf-connecting-ip",
  "cf-ipcountry", "cf-ray", "cf-visitor", "x-forwarded-proto", "x-real-ip", "cdn-loop"]);
const DROP_RES = new Set(["connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding"]);
const NO_BODY = new Set([101, 204, 205, 304]);

function b64(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}
function unb64(s) {
  const bin = atob(s || "");
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}
function sameSecret(a, b) {
  const x = new TextEncoder().encode(a || "");
  const y = new TextEncoder().encode(b || "");
  if (!y.length || x.length !== y.length) return false;
  return crypto.subtle.timingSafeEqual(x, y);
}
function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" } });
}
const OFFLINE = `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ALICE</title><body style="font:16px system-ui;background:#0d0f14;color:#e8e8ea;display:grid;place-items:center;height:90vh;text-align:center">
<div><h2>Bilgisayar çevrimdışı</h2><p>ALICE'in çalıştığı bilgisayar şu an aktarma sunucusuna bağlı değil.<br>Kapalı ya da internetsiz olabilir.</p>
<button onclick="location.reload()" style="font:inherit;padding:10px 18px;border-radius:10px;border:0">Yeniden dene</button></div>`;

export class Hub {
  constructor(state, env) {
    this.state = state;
    this.env = env;
    this.pending = new Map();
    this.seq = 0;
    // Bilgisayarın canlılık yoklaması nesneyi uyandırmadan cevaplansın (ücretsiz planda süre sayılmasın).
    state.setWebSocketAutoResponse(new WebSocketRequestResponsePair("ping", "pong"));
  }

  pc() {
    const all = this.state.getWebSockets("pc");
    return all.length ? all[all.length - 1] : null;
  }

  async fetch(req) {
    const url = new URL(req.url);
    if (url.pathname === "/_alice/pc") return this.connectPc(req);
    if (url.pathname === "/_alice/status") return json({ ok: true, pc: !!this.pc() });
    const pc = this.pc();
    if (!pc) {
      const wantsHtml = (req.headers.get("Accept") || "").includes("text/html");
      return wantsHtml ? new Response(OFFLINE, { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } })
        : json({ ok: false, error: "bilgisayar çevrimdışı", offline: true }, 503);
    }
    let body = null;
    if (req.method !== "GET" && req.method !== "HEAD") {
      body = new Uint8Array(await req.arrayBuffer());
      if (body.length > MAX_BODY) return json({ ok: false, error: "gövde çok büyük" }, 413);
    }
    const headers = {};
    for (const [k, v] of req.headers) if (!DROP_REQ.has(k.toLowerCase())) headers[k] = v;
    const rid = String(++this.seq) + "-" + crypto.randomUUID().slice(0, 8);
    const { readable, writable } = new TransformStream();
    const writer = writable.getWriter();
    let gotHead;
    const head = new Promise((r) => { gotHead = r; });
    this.pending.set(rid, { writer, gotHead, pc });
    try {
      pc.send(JSON.stringify({ t: "req", rid, m: req.method, p: url.pathname + url.search, h: headers, n: body ? body.length : 0 }));
      if (body) for (let i = 0; i < body.length; i += CHUNK) pc.send(JSON.stringify({ t: "body", rid, b: b64(body.subarray(i, i + CHUNK)) }));
      pc.send(JSON.stringify({ t: "bend", rid }));
    } catch (e) {
      this.pending.delete(rid);
      return json({ ok: false, error: "bilgisayara iletilemedi" }, 502);
    }
    const timer = setTimeout(() => gotHead({ s: 504, h: {}, timeout: true }), HEAD_TIMEOUT);
    const h = await head;
    clearTimeout(timer);
    if (h.timeout || h.gone) {
      this.pending.delete(rid);
      try { pc.send(JSON.stringify({ t: "cancel", rid })); } catch (e) { /* kapandı */ }
      return json({ ok: false, error: h.gone ? "bilgisayar bağlantısı koptu" : "bilgisayar cevap vermedi" }, h.gone ? 502 : 504);
    }
    const out = new Headers();
    for (const [k, v] of Object.entries(h.h || {})) if (!DROP_RES.has(k.toLowerCase())) out.append(k, v);
    if (NO_BODY.has(h.s) || req.method === "HEAD") {
      this.pending.delete(rid);
      try { writer.close(); } catch (e) { /* yok */ }
      return new Response(null, { status: h.s, headers: out });
    }
    return new Response(readable, { status: h.s, headers: out });
  }

  connectPc(req) {
    if ((req.headers.get("Upgrade") || "").toLowerCase() !== "websocket") return json({ ok: false, error: "WebSocket gerekli" }, 426);
    if (!sameSecret(req.headers.get("X-Relay-Key"), this.env.PC_KEY)) return json({ ok: false, error: "yetkisiz" }, 401);
    for (const old of this.state.getWebSockets("pc")) { try { old.close(4000, "yeni bağlantı"); } catch (e) { /* yok */ } }
    this.failAll();
    const [client, server] = Object.values(new WebSocketPair());
    this.state.acceptWebSocket(server, ["pc"]);
    return new Response(null, { status: 101, webSocket: client });
  }

  failAll(ws) {
    // ws verilirse yalnızca o bağlantıdan bekleyenler (eski bağlantının geç gelen kapanışı yenisini bozmasın)
    for (const [rid, p] of this.pending) {
      if (ws && p.pc !== ws) continue;
      this.pending.delete(rid);
      p.gotHead({ s: 502, h: {}, gone: true });
      try { p.writer.close(); } catch (e) { /* yok */ }
    }
  }

  async webSocketMessage(ws, data) {
    let m;
    try { m = JSON.parse(typeof data === "string" ? data : new TextDecoder().decode(data)); } catch (e) { return; }
    const p = this.pending.get(m.rid);
    if (!p) return;
    if (m.t === "head") p.gotHead({ s: m.s | 0 || 502, h: m.h || {} });
    else if (m.t === "data") {
      p.writer.write(unb64(m.b)).catch(() => {
        // telefon bağlantıyı kapattı (ör. olay akışı): bilgisayar akıtmayı bıraksın
        this.pending.delete(m.rid);
        try { ws.send(JSON.stringify({ t: "cancel", rid: m.rid })); } catch (e) { /* yok */ }
      });
    } else if (m.t === "end") {
      this.pending.delete(m.rid);
      p.writer.close().catch(() => {});
    }
  }

  async webSocketClose(ws) { this.failAll(ws); }
  async webSocketError(ws) { this.failAll(ws); }
}

export default {
  async fetch(req, env) {
    const hub = env.HUB.get(env.HUB.idFromName("alice"));
    return hub.fetch(req);
  },
};
