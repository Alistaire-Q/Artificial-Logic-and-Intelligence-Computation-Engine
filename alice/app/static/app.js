"use strict";

const TOKEN = document.querySelector('meta[name="alice-token"]').content;
const $ = (s) => document.querySelector(s);
const app = $("#app");
const chat = $("#chat");
const list = $("#messages");
const input = $("#input");

let cur = null;          // akmakta olan asistan mesajı {root, md, text}
let rafPending = false;
let busy = false;
let status = {};
const tools = new Map(); // call id -> kart
const perms = new Map(); // izin id -> kart

// ------------------------------------------------------------------ yardımcılar
async function api(path, body) {
  const opts = { headers: { "X-Alice-Token": TOKEN } };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  try { return await r.json(); } catch { return { ok: false, error: `HTTP ${r.status}` }; }
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

marked.setOptions({ gfm: true, breaks: false });
function renderMd(text) {
  return DOMPurify.sanitize(marked.parse(text || ""), {
    FORBID_TAGS: ["style", "form", "input", "button", "iframe", "object", "embed"],
    FORBID_ATTR: ["style"],
  });
}

function nearBottom() { return chat.scrollHeight - chat.scrollTop - chat.clientHeight < 120; }
function scrollDown(force) {
  if (force || nearBottom()) chat.scrollTop = chat.scrollHeight;
}
function add(node, force) {
  const stick = force || nearBottom();
  list.appendChild(node);
  $("#empty").classList.add("hidden");
  if (stick) chat.scrollTop = chat.scrollHeight;
  return node;
}
function clearChat() {
  list.innerHTML = "";
  tools.clear();
  perms.clear();
  cur = null;
  $("#empty").classList.remove("hidden");
}
function shortPath(p) {
  return p.length > 60 ? "…" + p.slice(-59) : p;
}

// ----------------------------------------------------------------- mesajlar
function addUser(text) {
  const m = el("div", "msg user");
  m.appendChild(el("div", "bubble", text));
  add(m, true);
}

function thinkingDots() {
  const t = el("span", "thinking");
  t.append(el("i"), el("i"), el("i"));
  return t;
}

function startAssistant() {
  closeEmptyAssistant();
  const root = el("div", "msg assistant");
  const md = el("div", "md");
  md.appendChild(thinkingDots());
  root.appendChild(md);
  add(root);
  cur = { root, md, text: "" };
}

function closeEmptyAssistant() {
  if (cur && !cur.text) cur.root.remove();
  cur = null;
}

function onDelta(t) {
  if (!cur) startAssistant();
  cur.text += t;
  if (!rafPending) {
    rafPending = true;
    requestAnimationFrame(() => {
      rafPending = false;
      if (!cur) return;
      const stick = nearBottom();
      cur.md.innerHTML = renderMd(cur.text);
      cur.md.classList.add("cursor");
      if (stick) chat.scrollTop = chat.scrollHeight;
    });
  }
}

function onStreamEnd(text, source) {
  if (!text) { closeEmptyAssistant(); return; }
  if (!cur) startAssistant();
  cur.text = text;
  cur.md.innerHTML = renderMd(text);
  cur.md.classList.remove("cursor");
  if (source) cur.root.appendChild(el("div", "source", source));
  cur = null;
  scrollDown();
}

function addAssistantStatic(text) {
  const root = el("div", "msg assistant");
  const md = el("div", "md");
  md.innerHTML = renderMd(text);
  root.appendChild(md);
  add(root);
}

function addInfo(text, warn) {
  add(el("div", warn ? "info warn" : "info", text));
}

// --------------------------------------------------------------------- araçlar
const CHEV = '<svg class="chev" viewBox="0 0 24 24" width="14" height="14"><path d="M9 6l6 6-6 6" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round"/></svg>';
const OK = '<svg viewBox="0 0 24 24" width="15" height="15"><path d="M5 12l5 5L20 7" stroke="currentColor" stroke-width="2.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const ERR = '<svg viewBox="0 0 24 24" width="15" height="15"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/></svg>';

function toolCard(id, name, summary) {
  closeEmptyAssistant();
  const card = el("div", "tool");
  const head = el("div", "tool-head");
  const icon = el("span", "tool-icon");
  icon.appendChild(el("span", "spin"));
  const stat = el("span", "tool-stat");
  head.append(icon, el("span", "tool-name", name), el("span", "tool-sum mono", summary || ""), stat);
  head.insertAdjacentHTML("beforeend", CHEV);
  const body = el("div", "tool-body");
  card.append(head, body);
  head.addEventListener("click", () => card.classList.toggle("open"));
  card._icon = icon; card._body = body; card._stat = stat;
  if (id) tools.set(id, card);
  return add(card);
}

function renderDiff(diff) {
  const box = el("div", "diff mono");
  let a = 0, d = 0;
  for (const line of diff.split("\n")) {
    if (line.startsWith("+++") || line.startsWith("---")) continue;
    let cls = "ctx";
    if (line.startsWith("+")) { cls = "add"; a++; }
    else if (line.startsWith("-")) { cls = "del"; d++; }
    else if (line.startsWith("@@")) cls = "hunk";
    box.appendChild(el("div", cls, line || " "));
  }
  return { box, a, d };
}

function finishTool(card, ev) {
  card._icon.innerHTML = ev.is_error ? ERR : OK;
  card._icon.className = "tool-icon " + (ev.is_error ? "err" : "ok");
  if (ev.is_error) card.classList.add("error");
  const body = card._body;
  body.innerHTML = "";
  if (ev.diff) {
    const { box, a, d } = renderDiff(ev.diff);
    body.appendChild(box);
    card._stat.innerHTML = "";
    card._stat.append(el("span", "a", `+${a}`), document.createTextNode(" "), el("span", "d", `-${d}`));
    card.classList.add("open");
  }
  if (ev.text && (!ev.diff || ev.is_error)) body.appendChild(el("pre", "tool-out", ev.text));
  if (ev.images && ev.images.length) {
    const imgs = el("div", "imgs");
    for (const b64 of ev.images) {
      const img = el("img");
      img.src = "data:image/png;base64," + b64;
      img.addEventListener("click", () => showDialog((d) => {
        const full = el("img", "full");
        full.src = img.src;
        d.appendChild(full);
      }));
      imgs.appendChild(img);
    }
    body.appendChild(imgs);
    card.classList.add("open");
  }
}

function onToolStart(ev) { toolCard(ev.id, ev.name, ev.summary); }
function onToolEnd(ev) {
  const card = tools.get(ev.id) || toolCard(ev.id, ev.name, "");
  finishTool(card, ev);
  scrollDown();
}

// ---------------------------------------------------------------------- izin
function onPermission(ev) {
  if (perms.has(ev.id)) return;
  closeEmptyAssistant();
  const card = el("div", "perm" + (ev.risk === "danger" ? " danger" : ""));
  const title = el("div", "perm-title", (ev.risk === "danger" ? "⚠ Tehlikeli işlem: " : "İzin gerekiyor: ") + ev.name);
  title.appendChild(el("span", "tag", ev.risk));
  const actions = el("div", "perm-actions");
  const mk = (label, ans, primary) => {
    const b = el("button", "btn" + (primary ? " primary" : ""), label);
    b.addEventListener("click", () => api("/api/permission", { id: ev.id, answer: ans }));
    return b;
  };
  actions.append(mk("İzin ver", "y", true), mk("Reddet", "n"), mk("Bu araca hep izin ver", "a"));
  card.append(title, el("pre", "", ev.summary), actions);
  card._actions = actions;
  perms.set(ev.id, card);
  add(card, true);
}

function onPermissionDone(ev) {
  const card = perms.get(ev.id);
  if (!card) return;
  const txt = { y: "İzin verildi", a: "Bu araca oturum boyunca izin verildi", n: "Reddedildi" }[ev.answer] || ev.answer;
  card._actions.replaceWith(el("div", "perm-done", txt));
}

// --------------------------------------------------------------------- durum
function setBusy(v) {
  busy = !!v;
  app.classList.toggle("busy", busy);
  const b = $("#stBusy");
  b.textContent = busy ? "● çalışıyor" : "hazır";
  b.classList.toggle("on", busy);
  $("#sendBtn").title = busy ? "Durdur (Esc)" : "Gönder (Enter)";
}

function applyStatus(s) {
  status = { ...status, ...s };
  const st = status;
  if (st.cwd !== undefined) { $("#cwd").textContent = shortPath(st.cwd); $("#cwdBtn").title = st.cwd; }
  if (st.version) $("#ver").textContent = "ALICE v" + st.version;
  document.querySelectorAll("#modeSeg button").forEach((b) => b.classList.toggle("on", b.dataset.mode === st.mode));
  const model = $("#stModel");
  model.querySelector("b").textContent = st.model || "model yok";
  model.querySelector(".dot").classList.toggle("off", !st.model);
  if (st.ctx_limit) {
    const pct = Math.min(100, (100 * st.ctx_tokens) / st.ctx_limit);
    const bar = $("#stCtx .bar");
    bar.firstElementChild.style.width = pct + "%";
    bar.classList.toggle("hot", pct > 75);
    $("#stCtx b").textContent = `${(st.ctx_tokens / 1000).toFixed(1)}k / ${Math.round(st.ctx_limit / 1000)}k`;
  }
  if (st.today_requests !== undefined) $("#stReq").textContent = `bugün ${st.today_requests} istek`;
  if (st.decider) $("#stDec").textContent = `karar: ${st.decider}`;
  if (st.busy !== undefined) setBusy(st.busy);
  for (const p of st.pending || []) onPermission(p);
  highlightSession();
}

async function refreshStatus() {
  const s = await api("/api/state");
  if (s && s.version) applyStatus(s);
}

// ------------------------------------------------------------------- geçmiş
async function loadHistory() {
  const items = await api("/api/history");
  renderHistory(items);
}

function renderHistory(items) {
  clearChat();
  for (const it of items || []) {
    if (it.type === "user") addUser(it.text);
    else if (it.type === "assistant") addAssistantStatic(it.text);
    else if (it.type === "info") addInfo(it.text);
    else if (it.type === "tool") {
      const card = toolCard(null, it.name, it.summary);
      finishTool(card, { is_error: it.is_error, text: it.text });
    }
  }
  scrollDown(true);
}

async function loadSessions() {
  const box = $("#sessions");
  const rows = await api("/api/sessions");
  box.innerHTML = "";
  for (const s of rows || []) {
    const b = el("button", "sess", s.title || "(boş)");
    b.dataset.id = s.id;
    const d = s.id.slice(0, 8);
    b.appendChild(el("small", "", `${d.slice(6, 8)}.${d.slice(4, 6)}.${d.slice(0, 4)} ${s.id.slice(9, 11)}:${s.id.slice(11, 13)}`));
    b.title = s.cwd;
    b.addEventListener("click", () => resume(s.id));
    box.appendChild(b);
  }
  highlightSession();
}

function highlightSession() {
  document.querySelectorAll(".sess").forEach((b) => b.classList.toggle("active", b.dataset.id === status.session));
}

async function resume(id) {
  if (busy) { addInfo("Önce mevcut işi durdur.", true); return; }
  const r = await api("/api/resume", { id });
  if (!r.ok) { addInfo(r.error || "oturum yüklenemedi", true); return; }
  renderHistory(r.history);
  applyStatus(r);
}

// ------------------------------------------------------------------- dialog
function showDialog(build) {
  const ov = $("#overlay");
  const d = $("#dialog");
  d.innerHTML = "";
  build(d);
  ov.hidden = false;
}
function closeDialog() { $("#overlay").hidden = true; }
$("#overlay").addEventListener("click", (e) => { if (e.target.id === "overlay") closeDialog(); });

function askCwd() {
  showDialog((d) => {
    d.appendChild(el("h2", "", "Çalışma klasörü"));
    d.appendChild(el("p", "", "ALICE komutları ve dosya yollarını bu klasöre göre çalıştırır."));
    const inp = el("input");
    inp.type = "text";
    inp.value = status.cwd || "";
    const row = el("div", "row");
    const ok = el("button", "btn primary", "Değiştir");
    const cancel = el("button", "btn", "Vazgeç");
    const err = el("div", "info warn");
    const submit = async () => {
      const r = await api("/api/cwd", { path: inp.value });
      if (!r.ok) { err.textContent = r.error; return; }
      applyStatus(r);
      closeDialog();
    };
    ok.addEventListener("click", submit);
    cancel.addEventListener("click", closeDialog);
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); if (e.key === "Escape") closeDialog(); });
    row.append(cancel, ok);
    d.append(inp, err, row);
    setTimeout(() => inp.focus(), 0);
  });
}

async function showModels() {
  const data = await api("/api/models");
  showDialog((d) => {
    d.appendChild(el("h2", "", "Modeller"));
    d.appendChild(el("p", "", "Her rol soldan sağa denenir; sınıra takılan model atlanır. 'Sabitle' o rolü tek modele kilitler."));
    for (const [role, rows] of Object.entries(data)) {
      const box = el("div", "role");
      const h = el("h3", "", role);
      box.appendChild(h);
      for (const r of rows) {
        const row = el("div", "mrow");
        row.append(el("span", "ref", r.ref), el("span", "stt" + (r.status === "hazır" ? " ready" : ""), r.status));
        const pin = el("button", "btn", "Sabitle");
        pin.addEventListener("click", async () => { await api("/api/model", { role, ref: r.ref }); showModels(); });
        row.appendChild(pin);
        box.appendChild(row);
      }
      if (rows.length === 1) {
        const reset = el("button", "btn", "Config zincirine dön");
        reset.addEventListener("click", async () => { await api("/api/model", { role, ref: "reset" }); showModels(); });
        box.appendChild(reset);
      }
      d.appendChild(box);
    }
    const row = el("div", "row");
    const close = el("button", "btn primary", "Kapat");
    close.addEventListener("click", closeDialog);
    row.appendChild(close);
    d.appendChild(row);
  });
}

// ----------------------------------------------------------------- composer
function autosize() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 260) + "px";
}

async function send(text) {
  text = (text ?? input.value).trim();
  if (!text) return;
  const r = await api("/api/send", { text });
  if (!r.ok) { addInfo(r.error || "gönderilemedi", true); return; }
  input.value = "";
  autosize();
}

function stop() { api("/api/stop", {}); }

input.addEventListener("input", autosize);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    if (!busy) send();
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (!$("#overlay").hidden) closeDialog();
    else if (busy) stop();
  }
});
$("#sendBtn").addEventListener("click", () => (busy ? stop() : send()));
document.querySelectorAll(".ex").forEach((b) => b.addEventListener("click", () => send(b.textContent)));
document.querySelectorAll("#modeSeg button").forEach((b) =>
  b.addEventListener("click", async () => applyStatus(await api("/api/mode", { mode: b.dataset.mode }))));
$("#cwdBtn").addEventListener("click", askCwd);
$("#modelsBtn").addEventListener("click", showModels);
$("#toggleSide").addEventListener("click", () => {
  app.classList.toggle(window.innerWidth <= 760 ? "side-open" : "side-hidden");
});
$("#newChat").addEventListener("click", async () => {
  if (busy) { addInfo("Önce mevcut işi durdur.", true); return; }
  const r = await api("/api/new", {});
  if (r.ok) { clearChat(); applyStatus(r); loadSessions(); input.focus(); }
});

// Model çıktısındaki linkler uygulama penceresini değil, varsayılan tarayıcıyı açsın.
list.addEventListener("click", (e) => {
  const a = e.target.closest("a[href]");
  if (!a) return;
  e.preventDefault();
  api("/api/open", { url: a.href });
});

// ------------------------------------------------------------------- olaylar
const handlers = {
  status: applyStatus,
  user: (ev) => addUser(ev.text),
  stream_start: () => startAssistant(),
  delta: (ev) => onDelta(ev.text),
  stream_reset: () => { if (cur) { cur.text = ""; cur.md.innerHTML = ""; cur.md.appendChild(thinkingDots()); } },
  stream_end: (ev) => onStreamEnd(ev.text, ev.source),
  tool_start: onToolStart,
  tool_end: onToolEnd,
  permission: onPermission,
  permission_done: onPermissionDone,
  info: (ev) => addInfo(ev.text, /hata|başarısız|cevap veremedi|durduruldu/i.test(ev.text)),
  busy: (ev) => { setBusy(ev.value); if (!ev.value) { closeEmptyAssistant(); loadSessions(); } },
  route: (ev) => { const m = ev.text.match(/^brain -> (.+)$/); if (m) applyStatus({ model: m[1] }); },
};

let connectedOnce = false;
function connect() {
  const es = new EventSource("/api/events?t=" + encodeURIComponent(TOKEN));
  es.onopen = () => {
    if (connectedOnce) loadHistory();
    connectedOnce = true;
  };
  es.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch { return; }
    const h = handlers[ev.type];
    if (h) h(ev);
  };
}

loadHistory().then(() => { connect(); loadSessions(); refreshStatus(); });
setInterval(() => { if (!busy) refreshStatus(); }, 10000);
input.focus();
