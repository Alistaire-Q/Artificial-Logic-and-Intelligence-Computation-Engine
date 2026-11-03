"use strict";
/* ALICE telefon uygulaması: sohbet, bilgisayar kontrolü, canlı ekran, akış. Aynı sunucu, aynı anahtar. */

const TOKEN = document.querySelector('meta[name="alice-token"]').content;
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const TITLES = { chat: "Sohbet", pc: "Bilgisayar", screen: "Ekran", feed: "Akış" };
let tab = "chat";

async function api(path, body) {
  const opts = { headers: { "X-Alice-Token": TOKEN } };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  try {
    const r = await fetch(path, opts);
    return await r.json();
  } catch (e) {
    return { ok: false, error: "bilgisayara ulaşılamadı" };
  }
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

let toastTimer = null;
function toast(text) {
  const t = $("#toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 2400);
}

function confirmSheet(text) {
  return new Promise((resolve) => {
    $("#sheetText").textContent = text;
    $("#sheet").hidden = false;
    const done = (v) => { $("#sheet").hidden = true; resolve(v); };
    $("#sheetYes").onclick = () => done(true);
    $("#sheetNo").onclick = () => done(false);
    $("#sheet").onclick = (e) => { if (e.target.id === "sheet") done(false); };
  });
}

function haptic() { if (navigator.vibrate) navigator.vibrate(8); }

/* ------------------------------------------------------------------ sekmeler */
function show(name) {
  tab = name;
  $$(".tab").forEach((s) => s.classList.toggle("active", s.id === "tab-" + name));
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $("#title").textContent = TITLES[name];
  try { localStorage.setItem("alice-tab", name); } catch (e) { /* özel pencere */ }
  if (name === "pc" || name === "feed") refreshStatus();
  if (name === "screen") startShots(); else stopShots();
  if (name === "pc" && !appsLoaded) loadApps();
  window.scrollTo({ top: name === "chat" ? document.body.scrollHeight : 0 });
}
$$("#tabs button").forEach((b) => b.addEventListener("click", () => { haptic(); show(b.dataset.tab); }));

/* ------------------------------------------------------------------ sohbet */
const msgs = $("#msgs"), q = $("#q"), sendBtn = $("#sendBtn");
const md = (t) => DOMPurify.sanitize(marked.parse(t || ""));
let cur = null, busy = false, typing = null;

function scrollDown() { if (tab === "chat") window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" }); }
function add(node) { $("#chatEmpty")?.remove(); msgs.appendChild(node); scrollDown(); return node; }
function setTyping(on) {
  if (on && !typing) typing = add(el("div", "typing")), typing.append(el("i"), el("i"), el("i"));
  if (!on && typing) { typing.remove(); typing = null; }
}
function autosize() { q.style.height = "auto"; q.style.height = Math.min(q.scrollHeight, 140) + "px"; }
q.addEventListener("input", () => { autosize(); sendBtn.disabled = !q.value.trim(); });
q.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && matchMedia("(pointer: fine)").matches) {
    e.preventDefault();
    $("#composer").requestSubmit();
  }
});
$("#composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = q.value.trim();
  if (!text) return;
  if (busy) { toast("ALICE hâlâ bir önceki işte"); return; }
  haptic();
  const r = await api("/api/send", { text, images: [] });
  if (!r.ok) { add(el("div", "err", r.error || "gönderilemedi")); return; }
  q.value = ""; autosize(); sendBtn.disabled = true;
});
$("#newChat").addEventListener("click", async () => {
  if (!(await confirmSheet("Yeni sohbet başlasın mı?"))) return;
  await api("/api/new", {});
  msgs.innerHTML = "";
  const e = el("div", "empty"); e.id = "chatEmpty"; e.append(el("p", null, "Yeni sohbet."));
  msgs.appendChild(e);
});

function choiceBox(ev, question, options, endpoint) {
  const box = el("div", "perm", question);
  box.id = "perm-" + ev.id;
  const row = el("div", "row-btns tight");
  for (const [answer, label] of options) {
    const b = el("button", "text-btn", label);
    b.onclick = () => { haptic(); api(endpoint, { id: ev.id, answer }); };
    row.appendChild(b);
  }
  box.appendChild(row);
  add(box);
}

const handlers = {
  status: () => {},
  user: (ev) => add(el("div", "msg me", ev.text)),
  busy: (ev) => { busy = !!ev.value; setTyping(busy && !cur); },
  stream_start: () => { cur = null; },
  delta: (ev) => {
    setTyping(false);
    if (!cur) cur = { el: add(el("div", "msg")), text: "" };
    cur.text += ev.text;
    cur.el.innerHTML = md(cur.text);
    scrollDown();
  },
  stream_reset: () => { if (cur) { cur.el.remove(); cur = null; } },
  stream_end: (ev) => {
    setTyping(false);
    if (!cur && ev.text) cur = { el: add(el("div", "msg")), text: ev.text };
    if (cur) cur.el.innerHTML = md(ev.text || cur.text);
    cur = null;
    scrollDown();
  },
  tool_start: (ev) => add(el("div", "tool", `${ev.name} ${ev.summary || ""}`.slice(0, 120))),
  tool_end: (ev) => { if (ev.is_error) add(el("div", "err", `${ev.name}: ${(ev.text || "").slice(0, 160)}`)); },
  permission: (ev) => choiceBox(ev, `İzin: ${ev.name} — ${ev.summary}`,
                                [["y", "Evet"], ["n", "Hayır"], ["a", "Hep izin ver"]], "/api/permission"),
  permission_done: (ev) => $("#perm-" + ev.id)?.remove(),
  ask_choice: (ev) => choiceBox(ev, ev.question, (ev.options || []).map((o) => [o, String(o).split(" — ")[0]]),
                                "/api/choice"),
  ask_choice_done: (ev) => $("#perm-" + ev.id)?.remove(),
  info: (ev) => { if (/hata|başarısız|veremedi/i.test(ev.text || "")) add(el("div", "err", ev.text)); },
};

let es = null;
function connect() {
  es = new EventSource("/api/events?t=" + encodeURIComponent(TOKEN));
  es.onopen = () => { $("#conn").hidden = true; };
  es.onerror = () => {
    $("#conn").hidden = false;
    if (es.readyState === EventSource.CLOSED) { es.close(); setTimeout(connect, 2500); }
  };
  es.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch (e) { return; }
    const h = handlers[ev.type];
    if (h) h(ev);
  };
}

async function loadHistory() {
  const h = await api("/api/history");
  const items = Array.isArray(h) ? h : (h.messages || []);
  for (const m of items.filter((x) => x.type === "user" || x.type === "assistant").slice(-30)) {
    if (!m.text) continue;
    if (m.type === "user") add(el("div", "msg me", m.text));
    else add(el("div", "msg")).innerHTML = md(m.text);
  }
}

/* ------------------------------------------------------------------ bilgisayar */
const MODE_NAMES = { normal: "Normal", flight: "Uçuş", quiet: "Sessiz", dev: "Geliştirme" };
let lastStatus = null;

function gauge(k, v) {
  const g = document.querySelector(`.gauge[data-k="${k}"]`);
  if (!g) return;
  const max = Number(g.dataset.max || 100), unit = g.dataset.unit || "%";
  const pct = v == null ? 0 : Math.max(0, Math.min(100, (v / max) * 100));
  g.querySelector(".val").style.strokeDasharray = `${(pct * 97.4) / 100} 100`;
  g.querySelector("b").textContent = v == null ? "–" : Math.round(v) + unit;
  const hot = k === "gpu_temp" ? v >= 90 : v >= 95, warm = k === "gpu_temp" ? v >= 80 : v >= 80;
  g.classList.toggle("crit", !!(v != null && hot));
  g.classList.toggle("warn", !!(v != null && warm && !hot));
}

function rel(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "şimdi";
  if (s < 3600) return Math.round(s / 60) + " dk";
  if (s < 86400) return Math.round(s / 3600) + " sa";
  return new Date(ts * 1000).toLocaleDateString("tr-TR", { day: "numeric", month: "short" });
}

async function refreshStatus() {
  const s = await api("/api/remote/status");
  if (s.ok === false) { toast(s.error); return; }
  lastStatus = s;
  ["cpu", "ram", "gpu", "gpu_temp"].forEach((k) => gauge(k, s[k]));
  $$("#modeSeg button").forEach((b) => b.classList.toggle("on", b.dataset.mode === (s.forced || "auto")));
  const parts = [];
  if (!s.core) parts.push("Çekirdek kapalı");
  else parts.push("Mod: " + (MODE_NAMES[s.mode] || s.mode || "–"));
  if (s.app) parts.push("Önde: " + s.app);
  if (s.voice) parts.push("Ses: " + (s.voice === "listening" ? "dinliyor" : s.voice));
  $("#nowLine").textContent = parts.join(" · ");
  const pl = s.playing;
  $("#playing").hidden = !pl;
  if (pl) {
    $("#playTitle").textContent = pl.title || pl.artist;
    const state = { playing: "çalıyor", paused: "duraklatıldı", stopped: "durdu" }[pl.status] || "";
    $("#playMeta").textContent = [pl.title ? pl.artist : "", pl.app, state].filter(Boolean).join(" · ");
  }

  const ins = $("#insight");
  ins.innerHTML = "";
  const n = s.now || {};
  const rows = [];
  if (n.flights_today) rows.push(["Bugün", `${n.flights_today} uçuş` + (n.avg_min_today ? `, ortalama ${n.avg_min_today.toFixed(1)} dk` : "")]);
  if (n.next_game) rows.push(["Önsezi", "War Thunder'ı genelde " + new Date(n.next_game * 1000).toLocaleTimeString("tr-TR", { hour: "2-digit", minute: "2-digit" }) + " gibi açıyorsun"]);
  for (const a of n.advice || []) rows.push(["Öneri", a]);
  if (s.ready) rows.push(["Hazır", s.ready]);
  for (const [k, v] of rows) {
    const c = el("div", "cell");
    const d = el("div"); d.append(el("span", null, v));
    c.append(d, el("span", "meta", k));
    ins.appendChild(c);
  }
  ins.hidden = !rows.length;
  renderFeed(s.events || []);
}

function renderFeed(events) {
  const f = $("#feed");
  f.innerHTML = "";
  if (!events.length) { f.appendChild(el("div", "cell", "Henüz bir şey yok.")); return; }
  for (const e of events) {
    const c = el("div", "cell ev");
    const d = el("div");
    d.append(el("span", null, e.title));
    if (e.body && e.body !== e.title) d.append(el("small", null, e.body));
    c.append(el("span", "ev-dot " + (e.severity || "")), d, el("span", "meta", rel(e.ts)));
    f.appendChild(c);
  }
}

$$("#modeSeg button").forEach((b) => b.addEventListener("click", async () => {
  haptic();
  $$("#modeSeg button").forEach((x) => x.classList.toggle("on", x === b));
  const r = await api("/api/system/action", { action: "mode", mode: b.dataset.mode });
  if (r.ok === false) toast(r.error || r.message || "mod değişmedi");
  setTimeout(refreshStatus, 600);
}));

async function act(action, extra = {}) {
  const r = await api("/api/remote/action", { action, ...extra });
  if (!r.ok) { toast(r.error || "olmadı"); return false; }
  if (r.text && r.text !== "tamam") toast(r.text);
  return true;
}

$$("[data-act]").forEach((b) => b.addEventListener("click", async () => {
  haptic();
  if (b.dataset.confirm && !(await confirmSheet(b.dataset.confirm))) return;
  const ok = await act(b.dataset.act);
  if (ok && b.dataset.act === "shutdown") $("#cancelShut").hidden = false;
  if (ok && b.dataset.act === "cancel_shutdown") $("#cancelShut").hidden = true;
}));

let volTimer = null;
$("#vol").addEventListener("input", (e) => {
  clearTimeout(volTimer);
  volTimer = setTimeout(() => act("set_volume", { percent: Number(e.target.value) }), 250);
});

$("#typeBtn").addEventListener("click", async () => {
  const t = $("#sendText").value;
  if (!t.trim()) return toast("Önce bir şey yaz");
  if (await act("type", { text: t })) $("#sendText").value = "";
});
$("#clipBtn").addEventListener("click", async () => {
  const t = $("#sendText").value;
  if (!t.trim()) return toast("Önce bir şey yaz");
  if (await act("clipboard", { text: t })) $("#sendText").value = "";
});

let apps = [], appsLoaded = false;
async function loadApps() {
  appsLoaded = true;
  const r = await api("/api/remote/apps");
  apps = r.apps || [];
}
function renderApps() {
  const term = $("#appSearch").value.trim().toLocaleLowerCase("tr");
  const list = $("#appList");
  list.innerHTML = "";
  if (!term) return;
  const hits = apps.filter((a) => a.toLocaleLowerCase("tr").includes(term)).slice(0, 8);
  const looksUrl = /\.[a-z]{2,}(\/|$)/i.test(term) || term.startsWith("http");
  const options = looksUrl ? [term] : hits;
  for (const name of options) {
    const c = el("button", "cell");
    c.append(el("span", null, name), el("i", null, "›"));
    c.onclick = async () => { haptic(); if (await act("open", { target: name })) { $("#appSearch").value = ""; renderApps(); } };
    list.appendChild(c);
  }
}
$("#appSearch").addEventListener("input", renderApps);
$("#appSearch").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); const first = $("#appList .cell"); if (first) first.click(); }
});

/* ------------------------------------------------------------------ ekran */
let shotTimer = null, shotPaused = false, shotUrl = null, shotBusy = false;
async function grabShot() {
  if (shotBusy || shotPaused || document.hidden) return;
  shotBusy = true;
  try {
    const r = await fetch("/api/remote/screen", { headers: { "X-Alice-Token": TOKEN } });
    if (!r.ok) throw new Error();
    const url = URL.createObjectURL(await r.blob());
    $("#shot").src = url;
    if (shotUrl) URL.revokeObjectURL(shotUrl);
    shotUrl = url;
    $("#shotInfo").textContent = "Canlı · " + new Date().toLocaleTimeString("tr-TR");
  } catch (e) {
    $("#shotInfo").textContent = "Ekran alınamadı (bilgisayar kilitli olabilir).";
  } finally {
    shotBusy = false;
  }
}
function startShots() { grabShot(); clearInterval(shotTimer); shotTimer = setInterval(grabShot, 1000); }
function stopShots() { clearInterval(shotTimer); shotTimer = null; }
$("#shotPause").addEventListener("click", () => {
  shotPaused = !shotPaused;
  $("#shotPause").textContent = shotPaused ? "Devam" : "Durdur";
  if (!shotPaused) grabShot();
});
$("#shotFull").addEventListener("click", () => {
  const img = $("#shot");
  if (img.requestFullscreen) img.requestFullscreen().catch(() => toast("Tam ekran açılamadı"));
});

/* ------------------------------------------------------------------ açılış */
setInterval(() => { if (!document.hidden && (tab === "pc" || tab === "feed")) refreshStatus(); }, 5000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && (tab === "pc" || tab === "feed")) refreshStatus();
});
let saved = "chat";
try { saved = localStorage.getItem("alice-tab") || "chat"; } catch (e) { /* özel pencere */ }
connect();
loadHistory().then(() => show(TITLES[saved] ? saved : "chat"));
