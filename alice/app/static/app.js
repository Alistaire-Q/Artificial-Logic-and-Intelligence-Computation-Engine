"use strict";

/* ===================================================================== temel */
const TOKEN = document.querySelector('meta[name="alice-token"]').content;
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const app = $("#app");
const scroller = $("#scroller");
const list = $("#messages");
const input = $("#input");

const S = {
  busy: false,
  status: {},
  cur: null,          // akmakta olan asistan cevabı
  working: null,      // "çalışıyor" göstergesi
  tools: new Map(),
  perms: new Map(),
  attachments: [],    // base64 PNG
  queue: [],          // meşgulken yazılan mesajlar
  commands: [],
  popup: null,        // {kind, items, sel, start}
  turnStart: 0,       // işin başladığı an (performance.now)
  sent: [],           // yukarı okla geri çağrılacak gönderilmiş mesajlar
  sentIdx: -1,
  online: true,
  todo: "",           // son plan (todo_write çıktısı)
  turn: null,         // bu turun sayaçları: araç, hata, değişen dosyalar
  changes: [],        // oturumdaki dosya değişiklikleri (/api/changes)
  started: Date.now(),
};

/* Kullanıcı tercihleri (tema, yazı boyutu, araç ayrıntıları, panel) tarayıcıda saklanır. */
const PREFS_KEY = "alice.prefs";
const PREFS = Object.assign({ theme: "system", size: "normal", verbose: false, panel: false, tab: "plan" },
  (() => { try { return JSON.parse(localStorage.getItem(PREFS_KEY) || "{}"); } catch { return {}; } })());
function savePrefs() { try { localStorage.setItem(PREFS_KEY, JSON.stringify(PREFS)); } catch { /* yok */ } }
function applyPrefs() {
  const root = document.documentElement;
  if (PREFS.theme === "system") delete root.dataset.theme; else root.dataset.theme = PREFS.theme;
  root.dataset.size = PREFS.size;
  app.classList.toggle("verbose", !!PREFS.verbose);
}
applyPrefs();

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
    return { ok: false, error: String(e) };
  }
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}
function svg(paths, cls) {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  if (cls) s.setAttribute("class", cls);
  s.innerHTML = paths;
  return s;
}
const IC = {
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h8"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  rewind: '<path d="M4 12a8 8 0 1 0 2.3-5.6"/><path d="M4 4v4.5h4.5"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/>',
  trash: '<path d="M5 7h14M10 7V5h4v2M7 7l1 12h8l1-12"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  chev: '<path d="M9 6l6 6-6 6"/>',
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
  pen: '<path d="M4 20h4L19 9l-4-4L4 16z"/>',
  term: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M13 15h4"/>',
  search: '<circle cx="11" cy="11" r="6"/><path d="M20 20l-4.3-4.3"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
  screen: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
  cursor: '<path d="M5 3l14 7-6 2-2 6z"/>',
  folder: '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2h7.5A2.5 2.5 0 0 1 21 9.5v7a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 16.5z"/>',
  list: '<path d="M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01"/>',
  bolt: '<path d="M13 3L5 13h6l-1 8 8-10h-6z"/>',
  plug: '<path d="M9 3v5M15 3v5M7 8h10v3a5 5 0 0 1-10 0zM12 16v5"/>',
  brain: '<path d="M9 4a3 3 0 0 0-3 3 3 3 0 0 0-2 5 3 3 0 0 0 2 5 3 3 0 0 0 6 1V5a2 2 0 0 0-3-1zM15 4a3 3 0 0 1 3 3 3 3 0 0 1 2 5 3 3 0 0 1-2 5 3 3 0 0 1-6 1"/>',
  spark: '<path d="M12 2v6M12 16v6M2 12h6M16 12h6M4.9 4.9l4.2 4.2M14.9 14.9l4.2 4.2M4.9 19.1l4.2-4.2M14.9 9.1l4.2-4.2"/>',
  shield: '<path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/>',
  map: '<path d="M9 4L3 6v14l6-2 6 2 6-2V4l-6 2z"/><path d="M9 4v14M15 6v14"/>',
  hand: '<path d="M8 13V5a1.5 1.5 0 0 1 3 0v6M11 11V4a1.5 1.5 0 0 1 3 0v7M14 11V5.5a1.5 1.5 0 0 1 3 0V14a7 7 0 0 1-7 7 6 6 0 0 1-5.5-3.5L3 14a1.5 1.5 0 0 1 2.6-1.4L8 15"/>',
  unlock: '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 7.5-2"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  panel: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M15 4v16"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z"/>',
  chat: '<path d="M4 5h16v11H8l-4 4z"/>',
  diff: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 14h6M12 11v6"/>',
  gauge: '<path d="M4 18a8 8 0 1 1 16 0"/><path d="M12 18l4-6"/>',
  keyboard: '<rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"/>',
  expand: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
  branch: '<circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="8" r="2"/><path d="M6 7v10M18 10c0 4-6 3-11.2 7.3"/>',
};

marked.setOptions({ gfm: true, breaks: false });
function renderMd(text) {
  const html = DOMPurify.sanitize(marked.parse(text || ""), {
    FORBID_TAGS: ["style", "form", "input", "button", "iframe", "object", "embed", "img"],
    FORBID_ATTR: ["style"],
  });
  const tpl = document.createElement("template");
  tpl.innerHTML = html;
  // Kod bloklarını başlık + kopyala butonlu kutuya sar.
  for (const pre of tpl.content.querySelectorAll("pre")) {
    const code = pre.querySelector("code");
    const lang = ((code && code.className.match(/language-([\w+#-]+)/)) || [])[1] || "";
    const box = el("div", "codeblock");
    const head = el("div", "cb-head");
    const src = code ? code.textContent : pre.textContent;
    if (code && AliceHL.langOf(lang)) code.innerHTML = AliceHL.highlight(src, lang);
    const nLines = src.replace(/\n$/, "").split("\n").length;
    const title = el("span", "cb-lang", lang || "kod");
    if (nLines > 1) title.appendChild(el("small", "", `${nLines} satır`));
    head.appendChild(title);
    const cp = el("button", "act");
    cp.dataset.copy = "code";
    cp.appendChild(svg(IC.copy));
    cp.appendChild(document.createTextNode("Kopyala"));
    head.appendChild(cp);
    pre.replaceWith(box);
    box.append(head, pre);
  }
  return tpl.content;
}
function setMd(node, text) {
  node.replaceChildren(renderMd(text));
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.hidden = true; }, 2200);
}
async function copyText(text, btn) {
  try { await navigator.clipboard.writeText(text); }
  catch {
    const ta = el("textarea"); ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand("copy"); ta.remove();
  }
  if (btn) {
    const old = btn.innerHTML;
    btn.replaceChildren(svg(IC.check), document.createTextNode("Kopyalandı"));
    setTimeout(() => { btn.innerHTML = old; }, 1400);
  } else toast("Kopyalandı");
}

/* ================================================================== kaydırma */
function nearBottom() { return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 140; }
function toBottom(force) { if (force || nearBottom()) scroller.scrollTop = scroller.scrollHeight; updateJump(); }
function add(node, force) {
  const stick = force || nearBottom();
  setEmpty(false);
  list.appendChild(node);
  if (stick) scroller.scrollTop = scroller.scrollHeight;
  return node;
}
function setEmpty(v) { app.classList.toggle("empty", v); }
function clearChat() {
  hideWorking();
  list.replaceChildren();
  S.tools.clear(); S.perms.clear(); S.cur = null;
  setTodo("");
  setEmpty(true);
  greet();
}
function greet() {
  const h = new Date().getHours();
  const part = h < 5 ? "İyi geceler" : h < 12 ? "Günaydın" : h < 18 ? "İyi günler" : h < 23 ? "İyi akşamlar" : "İyi geceler";
  const name = S.status.user ? `, ${S.status.user}` : "";
  $("#greeting").textContent = part + name;
}

/* ================================================================== mesajlar */
function addUser(text, mi, images) {
  const wrap = el("div", "turn-user");
  const b = el("div", "bubble");
  if (images && images.length) {
    const th = el("div", "thumbs");
    for (const src of images) {
      const img = el("img");
      img.src = src.startsWith("data:") ? src : "data:image/png;base64," + src;
      img.addEventListener("click", () => showImage(img.src));
      th.appendChild(img);
    }
    b.appendChild(th);
  }
  b.appendChild(document.createTextNode(text));
  const acts = el("div", "msg-actions");
  const cp = el("button", "act"); cp.title = "Kopyala"; cp.appendChild(svg(IC.copy));
  cp.addEventListener("click", () => copyText(text));
  acts.appendChild(cp);
  if (mi !== undefined && mi !== null) {
    const ed = el("button", "act"); ed.title = "Mesajı düzenle ve buradan yeniden gönder (çift tıklama da olur)";
    ed.append(svg(IC.edit), document.createTextNode("Düzenle"));
    ed.addEventListener("click", () => editInline(wrap, text, mi));
    b.addEventListener("dblclick", () => editInline(wrap, text, mi));
    const rw = el("button", "act"); rw.title = "Bu mesajdan öncesine geri sar (dosyalar dahil)";
    rw.append(svg(IC.rewind), document.createTextNode("Geri sar"));
    rw.addEventListener("click", () => rewindTo(mi, false));
    acts.append(ed, rw);
  }
  wrap.append(b, acts);
  return add(wrap, true);
}

// Göstergede geçen süre de yazar: uzun bir adımda arayüz donmuş gibi görünmesin.
function showWorking(label) {
  if (S.working) { S.working.querySelector(".lbl").textContent = label; return; }
  const w = el("div", "working");
  w.appendChild(svg(IC.spark, "spark"));
  w.appendChild(el("span", "lbl shimmer", label));
  const secs = el("span", "secs");
  w.appendChild(secs);
  const t0 = S.turnStart || performance.now();
  w._timer = setInterval(() => {
    const s = Math.round((performance.now() - t0) / 1000);
    secs.textContent = s >= 3 ? fmtSecs(s) : "";
  }, 1000);
  S.working = add(w);
}
function hideWorking() { if (S.working) { clearInterval(S.working._timer); S.working.remove(); S.working = null; } }
function fmtSecs(s) { return s < 60 ? `${s} sn` : `${Math.floor(s / 60)} dk ${s % 60} sn`; }

function ensureAi() {
  if (S.cur) return S.cur;
  hideWorking();
  const root = el("div", "turn-ai");
  const prose = el("div", "prose");
  root.appendChild(prose);
  add(root);
  S.cur = { root, prose, text: "", thought: null, tStart: 0, raf: false };
  return S.cur;
}
function aiActions(root, text, source) {
  const acts = el("div", "msg-actions");
  const cp = el("button", "act"); cp.append(svg(IC.copy), document.createTextNode("Kopyala"));
  cp.addEventListener("click", () => copyText(text, cp));
  acts.appendChild(cp);
  if (source) acts.appendChild(el("span", "source", source));
  root.appendChild(acts);
}

function onThinking(t) {
  const c = ensureAi();
  if (!c.thought) {
    const d = el("details", "thought");
    const sum = el("summary");
    sum.append(svg(IC.chev), el("span", "shimmer", "Düşünüyor"));
    const body = el("div", "tbody");
    d.append(sum, body);
    c.root.insertBefore(d, c.prose);
    c.thought = { d, sum, body, text: "" };
    c.tStart = performance.now();
  }
  c.thought.text += t;
  c.thought.body.textContent = c.thought.text;
  toBottom();
}
function closeThought(c) {
  if (!c || !c.thought || c.thought.closed) return;
  c.thought.closed = true;
  const secs = Math.max(1, Math.round((performance.now() - c.tStart) / 1000));
  c.thought.sum.lastChild.className = "";
  c.thought.sum.lastChild.textContent = `${secs} sn düşündü`;
}

function onDelta(t) {
  const c = ensureAi();
  closeThought(c);
  c.text += t;
  if (!c.raf) {
    c.raf = true;
    requestAnimationFrame(() => {
      c.raf = false;
      if (S.cur !== c) return;
      const stick = nearBottom();
      setMd(c.prose, c.text);
      c.prose.classList.add("cursor");
      if (stick) scroller.scrollTop = scroller.scrollHeight;
      else updateJump();
    });
  }
}
function onStreamEnd(text, source) {
  const c = S.cur;
  if (!text) {
    if (c) { closeThought(c); if (!c.text && !c.thought) c.root.remove(); }
    S.cur = null;
    return;
  }
  const cc = c || ensureAi();
  closeThought(cc);
  cc.text = text;
  setMd(cc.prose, text);
  cc.prose.classList.remove("cursor");
  aiActions(cc.root, text, source);
  S.cur = null;
  toBottom();
}
function addAiStatic(text) {
  const root = el("div", "turn-ai");
  const prose = el("div", "prose");
  setMd(prose, text);
  root.appendChild(prose);
  aiActions(root, text, "");
  add(root);
}
// Aynı not art arda gelirse (model atlandı, limit...) satır çoğalmasın, sayaç artsın.
function addNote(text, kind) {
  const last = lastContent();
  if (last && last.classList.contains("note") && last._text === text) {
    last._n = (last._n || 1) + 1;
    last.textContent = `${text} (×${last._n})`;
    return last;
  }
  const n = el("div", "note" + (kind ? " " + kind : ""), text);
  n._text = text;
  return add(n);
}

/* =================================================================== araçlar */
const VERBS = {
  read_file: ["Okundu", IC.file], write_file: ["Yazıldı", IC.pen], edit_file: ["Düzenlendi", IC.pen],
  multi_edit: ["Düzenlendi", IC.pen],
  list_dir: ["Klasör", IC.folder], glob: ["Dosya arandı", IC.search], grep: ["Arandı", IC.search],
  run_command: ["Komut", IC.term], process_output: ["Süreç çıktısı", IC.term], process_stop: ["Süreç durduruldu", IC.term],
  web_fetch: ["Sayfa okundu", IC.globe], web_search: ["Web'de arandı", IC.globe], browser: ["Tarayıcı", IC.globe],
  computer: ["Ekran", IC.screen], windows: ["Pencere", IC.screen], ui_elements: ["Arayüz tarandı", IC.cursor],
  ui_act: ["Arayüz", IC.cursor], clipboard: ["Pano", IC.copy], system_info: ["Sistem bilgisi", IC.screen],
  process_list: ["Süreçler", IC.list], process_kill: ["Süreç sonlandırıldı", IC.x], todo_write: ["Plan", IC.list],
  memory: ["Hafıza", IC.brain], task: ["Alt ajan", IC.spark], reflex: ["Reflex", IC.bolt],
};
function verbFor(name) {
  if (name.startsWith("mcp__")) {
    const [, srv, tool] = name.split("__");
    return [`${srv} · ${tool}`, IC.plug];
  }
  return VERBS[name] || [name, IC.bolt];
}

// Art arda gelen salt okuma çağrıları tek satırda toplanır: "3 dosya okundu, 2 arama yapıldı".
const GROUP_KIND = {
  read_file: "file", list_dir: "dir", glob: "search", grep: "search", web_search: "web", web_fetch: "page",
  system_info: "sys", process_list: "sys", process_output: "sys",
};
const GROUP_TEXT = {
  file: (n) => `${n} dosya okundu`, dir: (n) => `${n} klasör listelendi`, search: (n) => `${n} arama yapıldı`,
  web: (n) => `${n} web araması`, page: (n) => `${n} sayfa okundu`, sys: (n) => `${n} sistem sorgusu`,
};
function lastContent() {
  let n = list.lastElementChild;
  if (n && n === S.working) n = n.previousElementSibling;
  return n;
}
function makeGroup() {
  const g = el("div", "tool-group");
  const row = el("div", "tool-row");
  row.appendChild(svg(IC.list, "ti"));
  const v = el("span", "verb");
  const meta = el("span", "meta");
  row.append(v, el("span", "arg"), meta, svg(IC.chev, "tchev"));
  const body = el("div", "group-body");
  g.append(row, body);
  row.addEventListener("click", () => g.classList.toggle("open"));
  if (PREFS.verbose) g.classList.add("open");
  g._v = v; g._meta = meta; g._body = body;
  return g;
}
function updateGroup(g) {
  if (!g) return;
  const members = Array.from(g._body.children);
  const counts = {};
  let running = 0, errors = 0;
  for (const m of members) {
    const k = GROUP_KIND[m._name] || "sys";
    counts[k] = (counts[k] || 0) + 1;
    if (m.classList.contains("running")) running++;
    if (m.classList.contains("err")) errors++;
  }
  const order = ["file", "dir", "search", "web", "page", "sys"];
  const text = order.filter((k) => counts[k]).map((k) => GROUP_TEXT[k](counts[k])).join(", ");
  g._v.textContent = text.charAt(0).toUpperCase() + text.slice(1);
  g._v.classList.toggle("shimmer", running > 0);
  g._meta.replaceChildren();
  if (errors) g._meta.appendChild(el("span", "d", `${errors} hata`));
}

function toolRow(id, name, summary, running) {
  hideWorking();
  if (S.cur) { closeThought(S.cur); if (!S.cur.text && !S.cur.thought) S.cur.root.remove(); S.cur = null; }
  const [verb, icon] = verbFor(name);
  const t = el("div", "tool" + (running ? " running" : ""));
  const row = el("div", "tool-row");
  // Durum yuvası: çalışırken dönen halka, bitince araç simgesi ve yeşil/kırmızı nokta.
  const st = el("span", "tstat");
  st.append(svg(icon, "ti"), el("i", "spin"));
  row.appendChild(st);
  const v = el("span", "verb" + (running ? " shimmer" : ""), verb);
  row.append(v, el("span", "arg", summary || ""));
  const meta = el("span", "meta");
  row.append(meta, svg(IC.chev, "tchev"));
  const peek = el("div", "tool-peek");
  const body = el("div", "tool-body");
  t.append(row, peek, body);
  row.addEventListener("click", () => t.classList.toggle("open"));
  t._v = v; t._meta = meta; t._body = body; t._peek = peek; t._name = name; t._summary = summary || "";
  t._t0 = running ? performance.now() : 0;
  if (running && S.turn) S.turn.tools++;
  if (running) {
    // Uzun süren araçta geçen süre canlı yazsın (Claude Code'daki gibi).
    t._timer = setInterval(() => {
      const s = (performance.now() - t._t0) / 1000;
      if (s >= 2) t._meta.textContent = fmtDur(s);
    }, 500);
  }
  if (id) S.tools.set(id, t);
  if (GROUP_KIND[name]) {
    const last = lastContent();
    let g = null;
    if (last && last.classList.contains("tool-group")) g = last;
    else if (last && last.classList.contains("tool") && GROUP_KIND[last._name]) {
      g = makeGroup();
      list.insertBefore(g, last);
      g._body.appendChild(last);
    }
    if (g) {
      g._body.appendChild(t);
      updateGroup(g);
      toBottom();
      return t;
    }
  }
  return add(t);
}
// 1.8k, 840k, 1M gibi kısa token yazımı.
function fmtTok(n) {
  if (n >= 1e6) {
    const m = n / 1048576;
    return String(Math.abs(m - Math.round(m)) < 0.01 ? Math.round(m) : (n / 1e6).toFixed(1)).replace(".", ",") + "M";
  }
  if (n >= 1e4) return `${Math.round(n / 1000)}k`;
  return `${(n / 1000).toFixed(1).replace(".", ",")}k`;
}
function fmtDur(s) { return s < 10 ? `${s.toFixed(1).replace(".", ",")} sn` : fmtSecs(Math.round(s)); }

// Satır numaralı, renklendirilmiş unified diff (Codex/Claude Code'daki gibi).
function renderDiff(diff, title) {
  const box = el("div", "diff");
  const head = el("div", "diff-head");
  const lines = diff.split("\n");
  const file = title || (lines.find((l) => l.startsWith("+++ b/")) || "").slice(6);
  let a = 0, d = 0, on = 0, nn = 0;
  const body = el("div", "diff-body");
  const hl = (code) => AliceHL.highlight(code, file);
  for (const line of lines) {
    if (line.startsWith("+++") || line.startsWith("---")) continue;
    const row = el("div", "dl");
    const lo = el("span", "ln"), ln = el("span", "ln"), sg = el("span", "dsg"), code = el("span", "code");
    if (line.startsWith("@@")) {
      const m = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)/);
      if (m) { on = +m[1]; nn = +m[2]; }
      row.className = "dl hunk";
      code.textContent = line;
    } else if (line.startsWith("+")) {
      row.className = "dl add"; a++;
      ln.textContent = nn++; sg.textContent = "+"; code.innerHTML = hl(line.slice(1));
    } else if (line.startsWith("-")) {
      row.className = "dl del"; d++;
      lo.textContent = on++; sg.textContent = "−"; code.innerHTML = hl(line.slice(1));
    } else if (line.startsWith("\\")) {
      row.className = "dl hunk"; code.textContent = line;
    } else {
      lo.textContent = on++; ln.textContent = nn++; code.innerHTML = hl(line.slice(1)) || " ";
    }
    row.append(lo, ln, sg, code);
    body.appendChild(row);
  }
  head.append(svg(IC.file), el("b", "", file), el("span", "a", `+${a}`), el("span", "d", `−${d}`));
  box.append(head, body);
  return { box, a, d, file };
}

function parseTodo(text) {
  const out = [];
  for (const line of (text || "").split("\n")) {
    const m = line.match(/^\[( |~|x)\]\s*(.*)$/);
    if (m) out.push({ state: m[1] === "x" ? "done" : m[1] === "~" ? "doing" : "", text: m[2] });
  }
  return out;
}
function renderTodo(text, big) {
  const items = parseTodo(text);
  const done = items.filter((i) => i.state === "done").length;
  const box = el("div", "todo" + (big ? " big" : ""));
  const head = el("div", "todo-title");
  head.append(svg(IC.list), el("span", "", "Görevler"), el("span", "todo-n", `${done}/${items.length}`));
  const bar = el("i", "todo-bar");
  const fill = el("i");
  fill.style.width = items.length ? (100 * done) / items.length + "%" : "0";
  bar.appendChild(fill);
  box.append(head, bar);
  const ul = el("ul");
  for (const it of items) {
    const li = el("li", it.state);
    const bx = el("span", "box");
    if (it.state === "done") bx.appendChild(svg(IC.check));
    li.append(bx, el("span", "", it.text));
    ul.appendChild(li);
  }
  box.appendChild(ul);
  box.addEventListener("click", () => openPanel("plan"));
  return box;
}

// Komut çıktısı terminal görünümünde: komut, çıkış kodu, çıktı.
function renderTerm(cmd, text, isErr) {
  const m = (text || "").match(/^çıkış kodu (-?\d+)\n?/);
  const code = m ? +m[1] : null;
  const out = m ? text.slice(m[0].length) : text || "";
  const box = el("div", "term");
  const head = el("div", "term-head");
  const c = el("span", "term-cmd");
  c.append(el("span", "pr", "$"), el("span", "", cmd));
  head.appendChild(c);
  if (code !== null) head.appendChild(el("span", "exit " + (code === 0 ? "ok" : "bad"), `çıkış ${code}`));
  else if (isErr) head.appendChild(el("span", "exit bad", "hata"));
  const pre = el("pre", "term-out", out.trim() || "(çıktı yok)");
  box.append(head, pre);
  return { box, code };
}

function peekText(t, ev) {
  let txt = ev.text || "";
  if (t._name === "run_command") txt = txt.replace(/^çıkış kodu -?\d+\n?/, "");
  const lines = txt.split("\n").map((l) => l.trimEnd()).filter((l) => l.trim());
  if (!lines.length) return "";
  const shown = lines.slice(0, 2).map((l) => (l.length > 140 ? l.slice(0, 137) + "…" : l));
  if (lines.length > 2) shown.push(`… +${lines.length - 2} satır`);
  return shown.join("\n");
}

function finishTool(t, ev, live) {
  clearInterval(t._timer);
  t.classList.remove("running");
  t._v.classList.remove("shimmer");
  t.classList.add(ev.is_error ? "err" : "ok");
  const group = t.parentElement && t.parentElement.closest(".tool-group");
  if (group) setTimeout(() => updateGroup(group), 0);
  const body = t._body;
  body.replaceChildren();
  t._meta.replaceChildren();
  if (live && S.turn && ev.is_error) S.turn.errs++;
  if (t._name === "todo_write" && !ev.is_error) {
    setTodo(ev.text || "");
    // Aynı turdaki eski plan kartı kalkar, güncel olanı en sonda durur (Codex'teki gibi tek plan).
    for (let n = t.previousElementSibling; n && !n.classList.contains("turn-user"); n = n.previousElementSibling) {
      if (n.classList.contains("todo")) { n.remove(); break; }
    }
    t.replaceWith(renderTodo(ev.text || ""));
    return;
  }
  if (ev.diff) {
    const { box, a, d, file } = renderDiff(ev.diff);
    body.appendChild(box);
    t._meta.append(el("span", "a", `+${a}`), document.createTextNode(" "), el("span", "d", `−${d}`));
    t.classList.add("open");
    if (live && S.turn) {
      const f = S.turn.files.get(file) || { a: 0, d: 0 };
      f.a += a; f.d += d;
      S.turn.files.set(file, f);
      refreshChanges();
    }
  } else if (t._name === "run_command" && !/^arka planda/.test(ev.text || "")) {
    const { box, code } = renderTerm(t._summary, ev.text, ev.is_error);
    body.appendChild(box);
    if (code !== null) t._meta.appendChild(el("span", "exit " + (code === 0 ? "ok" : "bad"), `çıkış ${code}`));
  } else if (ev.text) {
    body.appendChild(el("pre", "out", ev.text));
  }
  const took = t._t0 ? (performance.now() - t._t0) / 1000 : 0;
  if (took >= 0.05) t._meta.appendChild(el("span", "dur", fmtDur(took)));
  if (PREFS.verbose && body.childNodes.length) t.classList.add("open");
  if (!ev.diff) t._peek.textContent = peekText(t, ev);
  if (ev.images && ev.images.length) {
    const imgs = el("div", "imgs");
    for (const b64 of ev.images) {
      const img = el("img");
      img.src = "data:image/png;base64," + b64;
      img.addEventListener("click", () => showImage(img.src));
      imgs.appendChild(img);
    }
    body.appendChild(imgs);
    t.classList.add("open");
  }
}

/* ===================================================================== izin */
function onPermission(ev) {
  if (S.perms.has(ev.id)) return;
  hideWorking();
  const card = el("div", "perm" + (ev.risk === "danger" ? " danger" : ""));
  const q = el("div", "perm-q");
  const [verb, icon] = verbFor(ev.name);
  const ic = el("span", "perm-ic");
  ic.appendChild(svg(ev.risk === "danger" ? IC.shield : icon));
  const qt = el("div", "perm-qt");
  qt.append(el("b", "", ev.risk === "danger" ? "Bu işlem geri alınamayabilir" : "ALICE izin istiyor"),
    el("small", "", `${verb} · ${ev.name}`));
  q.append(ic, qt);
  const acts = el("div", "perm-actions");
  // 1/2/3 tuşları da cevaplar (Claude Code'daki gibi).
  const mk = (label, ans, cls, key) => {
    const b = el("button", "btn " + (cls || ""));
    b.append(document.createTextNode(label), el("kbd", "", key));
    b.addEventListener("click", () => api("/api/permission", { id: ev.id, answer: ans }));
    return b;
  };
  acts.append(mk("İzin ver", "y", "primary", "1"), mk("Bu oturumda hep izin ver", "a", "", "2"), mk("Reddet", "n", "danger", "3"));
  const pre = el("pre", ev.name === "run_command" ? "cmd" : "", ev.summary);
  card.append(q, pre, acts);
  card._id = ev.id;
  card._acts = acts;
  S.perms.set(ev.id, card);
  add(card, true);
}
function onPermissionDone(ev) {
  const card = S.perms.get(ev.id);
  if (!card) return;
  const txt = { y: "İzin verildi", a: "Bu oturumda bu araca hep izin verildi", n: "Reddedildi" }[ev.answer] || ev.answer;
  card._acts.replaceWith(el("div", "perm-done " + (ev.answer === "n" ? "no" : "yes"), txt));
  card.classList.add("answered");
}
function pendingPerm() {
  for (const c of S.perms.values()) if (!c.classList.contains("answered")) return c;
  return null;
}

/* ==================================================================== durum */
const MODES = {
  plan: ["Plan", "Sadece okur ve araştırır, değişiklik yapmadan plan yazar", IC.map],
  ask: ["Sor", "Okuma dışındaki her işlemde izin ister", IC.hand],
  auto: ["Otomatik", "Sadece tehlikeli işlemlerde izin ister", IC.shield],
  yolo: ["Tam erişim", "Hiçbir şey sormaz", IC.unlock],
};
function shortModel(ref) {
  if (!ref) return "model yok";
  const m = ref.split(":").slice(1).join(":");
  return (m.split("/").pop() || ref).replace(/:free$/, "");
}
// Kopyala satırı sadece bir turun son cevabında görünsün (Claude'daki gibi).
function markFinals() {
  const kids = Array.from(list.children);
  kids.forEach((n, i) => {
    if (!n.classList.contains("turn-ai")) return;
    let final = true;
    for (let j = i + 1; j < kids.length; j++) {
      const k = kids[j];
      if (k.classList.contains("turn-user")) break;
      if (k.classList.contains("turn-ai") || k.classList.contains("tool") || k.classList.contains("todo") ||
          k.classList.contains("tool-group") ||
          k.classList.contains("perm")) { final = false; break; }
    }
    n.classList.toggle("final", final && !(S.busy && i === kids.length - 1 - (S.working ? 1 : 0)));
  });
}

function setBusy(v) {
  if (v && !S.busy) {
    S.turnStart = performance.now();
    S.turn = { tools: 0, errs: 0, files: new Map() };
  }
  const ended = S.busy && !v;
  S.busy = !!v;
  app.classList.toggle("busy", S.busy);
  document.title = S.busy ? "● ALICE" : "ALICE";
  $("#sendBtn").title = S.busy ? "Durdur (Esc)" : "Gönder (Enter)";
  if (S.busy && !S.cur && !S.working) showWorking("Çalışıyor");
  if (!S.busy) { hideWorking(); markFinals(); flushQueue(); }
  if (ended) { turnSummary(); refreshChanges(); }
  setTodo(S.todo);
  updateSend();
}
// İş bitince tek satırlık özet: süre, araç sayısı, değişen dosyalar.
function turnSummary() {
  const t = S.turn;
  S.turn = null;
  if (!t) return;
  const secs = (performance.now() - S.turnStart) / 1000;
  if (!t.tools && secs < 4) return;
  const row = el("div", "turn-sum" + (t.errs ? " has-err" : ""));
  row.appendChild(svg(t.errs ? IC.x : IC.check));
  const parts = [fmtDur(secs)];
  if (t.tools) parts.push(`${t.tools} araç`);
  if (t.errs) parts.push(`${t.errs} hata`);
  row.appendChild(el("span", "", parts.join(" · ")));
  if (t.files.size) {
    let a = 0, d = 0;
    for (const f of t.files.values()) { a += f.a; d += f.d; }
    const ch = el("button", "ts-files");
    ch.append(svg(IC.diff), document.createTextNode(`${t.files.size} dosya `), el("span", "a", `+${a}`),
      document.createTextNode(" "), el("span", "d", `−${d}`));
    ch.title = Array.from(t.files.keys()).join("\n");
    ch.addEventListener("click", () => openPanel("changes"));
    row.appendChild(ch);
  }
  add(row);
}
function applyStatus(s) {
  Object.assign(S.status, s);
  const st = S.status;
  if ("team" in s) renderTeam(s.team);
  if (st.cwd !== undefined) { $("#cwd").textContent = st.cwd; $("#cwdBtn").title = st.cwd; }
  if (st.version) $("#ver").textContent = "ALICE " + st.version;
  if (st.user) { $("#userName").textContent = st.user; $("#avatar").textContent = st.user[0].toUpperCase(); }
  if (st.mode) {
    const [label, , icon] = MODES[st.mode] || [st.mode, "", IC.shield];
    $("#modeLabel").textContent = label;
    $("#modeIcon").innerHTML = icon;
    $("#modeBtn").className = "c-btn pill m-" + st.mode;
  }
  $("#modelLabel").textContent = shortModel(st.model);
  if (st.thinking && THINK[st.thinking]) {
    $("#thinkLabel").textContent = THINK[st.thinking][0];
    $("#thinkIcon").innerHTML = st.thinking === "off" ? IC.bolt : IC.brain;
    $("#thinkBtn").title = "Düşünme: " + THINK[st.thinking][1];
  }
  $("#modelBtn").title = st.model || "";
  $("#stModel").textContent = st.model || "model yok";
  if (st.ctx_limit) {
    const win = st.ctx_window || st.ctx_limit;
    const pct = Math.min(100, (100 * st.ctx_tokens) / win);
    const m = $("#stCtx .meter");
    m.firstElementChild.style.width = pct + "%";
    m.classList.toggle("hot", pct > 75);
    $("#stCtx span").textContent = `${fmtTok(st.ctx_tokens)} / ${fmtTok(win)}`;
    $("#stCtx").title = `Bağlam: ${st.ctx_tokens.toLocaleString("tr-TR")} / ${win.toLocaleString("tr-TR")} token. ` +
      `${fmtTok(st.ctx_limit)} token'da eski konuşma otomatik özetlenir.`;
  }
  if (st.today_requests !== undefined) $("#stReq").textContent = `bugün ${st.today_requests} istek`;
  if (st.decider) $("#stDec").textContent = `karar: ${st.decider}`;
  if (st.branch !== undefined) {
    $("#branch").textContent = st.branch;
    $("#branchChip").hidden = !st.branch;
    $("#stBranch").textContent = st.branch ? "⎇ " + st.branch : "";
    $$(".st-br").forEach((n) => { n.hidden = !st.branch; });
  }
  if (PREFS.panel && PREFS.tab === "context") renderPanel();
  // Sadece gelen durumda busy varsa uygula: kısmi güncelleme (ör. model değişimi) eski busy değeriyle işi bitmiş saymasın.
  if (s.busy !== undefined && s.busy !== S.busy) setBusy(s.busy);
  for (const p of st.pending || []) onPermission(p);
  $$(".sess").forEach((b) => b.classList.toggle("active", b.dataset.id === st.session));
  if (app.classList.contains("empty")) greet();
}
async function refreshStatus() { const s = await api("/api/state"); if (s && s.version) applyStatus(s); }

/* ============================================================ geçmiş/oturum */
function renderHistory(items) {
  clearChat();
  for (const it of items || []) {
    if (it.type === "user") { addUser(it.text, it.mi); remember(it.text); }
    else if (it.type === "assistant") addAiStatic(it.text);
    else if (it.type === "info") addNote(it.text);
    else if (it.type === "tool") finishTool(toolRow(null, it.name, it.summary, false), { is_error: it.is_error, text: it.text });
  }
  if (!items || !items.length) setEmpty(true);
  markFinals();
  toBottom(true);
}
async function loadHistory() { renderHistory(await api("/api/history")); }

// Kenar çubuğu: aramayla süzülen, tarihe göre gruplanmış sohbetler.
async function loadSessions() {
  const rows = await api("/api/sessions");
  S.sessions = Array.isArray(rows) ? rows : [];
  drawSessions();
}
function relTime(ts) {
  if (!ts) return "";
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "şimdi";
  if (s < 3600) return `${Math.floor(s / 60)} dk`;
  if (s < 86400) return `${Math.floor(s / 3600)} sa`;
  if (s < 7 * 86400) return `${Math.floor(s / 86400)} g`;
  return new Date(ts * 1000).toLocaleDateString("tr-TR", { day: "numeric", month: "short" });
}
function dayGroup(ts) {
  if (!ts) return "Daha eski";
  const d = new Date(ts * 1000), now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
  if (ts >= start) return "Bugün";
  if (ts >= start - 86400) return "Dün";
  if (ts >= start - 6 * 86400) return "Bu hafta";
  if (d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth()) return "Bu ay";
  return "Daha eski";
}
function drawSessions() {
  const nav = $("#sessions");
  nav.replaceChildren();
  const q = ($("#sessSearch").value || "").trim().toLocaleLowerCase("tr");
  const rows = (S.sessions || []).filter((s) => !q || (s.title + " " + s.cwd).toLocaleLowerCase("tr").includes(q));
  if (!rows.length) {
    nav.appendChild(el("div", "side-empty", q ? "Eşleşen sohbet yok" : "Henüz sohbet yok"));
    return;
  }
  let group = null;
  for (const s of rows) {
    const g = dayGroup(s.updated);
    if (g !== group) { group = g; nav.appendChild(el("div", "side-label", g)); }
    const b = el("button", "sess");
    b.dataset.id = s.id;
    b.title = `${s.title}\n${s.cwd}`;
    b.appendChild(el("span", "st", s.title || "(boş)"));
    b.appendChild(el("small", "when", relTime(s.updated)));
    const del = el("span", "del");
    del.title = "Sil";
    del.appendChild(svg(IC.trash));
    del.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!(await confirmDialog("Sohbet silinsin mi?", "Bu sohbet geçmişten kalıcı olarak silinecek.", "Sil"))) return;
      await api("/api/session/delete", { id: s.id });
      if (s.id === S.status.session) newChat();
      loadSessions();
    });
    b.appendChild(del);
    b.addEventListener("click", () => resume(s.id));
    nav.appendChild(b);
  }
  $$(".sess").forEach((b) => b.classList.toggle("active", b.dataset.id === S.status.session));
}
$("#sessSearch").addEventListener("input", drawSessions);
$("#sessSearch").addEventListener("keydown", (e) => { if (e.key === "Escape") { e.target.value = ""; drawSessions(); e.target.blur(); } });
async function resume(id) {
  if (S.busy) return toast("Önce mevcut işi durdur");
  const r = await api("/api/resume", { id });
  if (!r.ok) return toast(r.error || "Oturum yüklenemedi");
  renderHistory(r.history);
  applyStatus(r);
  refreshChanges();
  closeSideOnMobile();
}
async function newChat() {
  if (S.busy) return toast("Önce mevcut işi durdur");
  const r = await api("/api/new", {});
  if (r.ok) { clearChat(); applyStatus(r); loadSessions(); refreshChanges(); input.focus(); }
}
async function rewindTo(mi, edit) {
  if (S.busy) return toast("Önce mevcut işi durdur");
  const ok = await confirmDialog(edit ? "Mesajı düzenle" : "Buraya geri sar",
    "Konuşma bu mesajdan öncesine döner. ALICE'in bu mesajdan sonra dosya araçlarıyla yaptığı değişiklikler geri alınır " +
    "(komutlarla yapılan değişiklikler geri alınamaz).", edit ? "Düzenle" : "Geri sar");
  if (!ok) return;
  const r = await api("/api/rewind", { mi });
  if (!r.ok) return toast(r.error || "Geri sarılamadı");
  renderHistory(r.history);
  applyStatus(r);
  refreshChanges();
  toast(r.restored && r.restored.length ? `Geri sarıldı, ${r.restored.length} dosya eski haline getirildi` : "Geri sarıldı");
  if (edit) { input.value = r.text; autosize(); input.focus(); }
}

// Mesajı yerinde düzenle: balon yazı kutusuna döner; Gönder konuşmayı o mesajın öncesine sarar ve yeni metni yollar.
function editInline(wrap, text, mi) {
  if (wrap.querySelector(".edit-box")) return;
  const box = el("div", "edit-box");
  const ta = el("textarea");
  ta.value = text;
  ta.rows = 1;
  const fit = () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 320) + "px"; };
  const row = el("div", "edit-row");
  row.appendChild(el("small", "", "Sonrası silinir, ALICE'in dosya değişiklikleri geri alınır"));
  const cancel = el("button", "btn", "Vazgeç");
  const send = el("button", "btn primary", "Gönder");
  row.append(cancel, send);
  box.append(ta, row);
  wrap.classList.add("editing");
  wrap.appendChild(box);
  const close = () => { box.remove(); wrap.classList.remove("editing"); };
  const go = async () => {
    const t = ta.value.trim();
    if (!t) return;
    send.disabled = cancel.disabled = true;
    if (!(await resendFrom(mi, t))) { send.disabled = cancel.disabled = false; return; }
  };
  cancel.addEventListener("click", close);
  send.addEventListener("click", go);
  ta.addEventListener("input", fit);
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); close(); }
    else if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); go(); }
  });
  fit();
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
}
// ALICE çalışıyorsa önce durdurur (bitmesini en fazla ~10 sn bekler), sonra geri sarıp yeni metni gönderir.
async function resendFrom(mi, text) {
  if (S.busy) {
    stop();
    for (let i = 0; i < 50 && S.busy; i++) { await new Promise((r) => setTimeout(r, 200)); await refreshStatus(); }
    if (S.busy) { toast("ALICE henüz durmadı, biraz sonra tekrar dene"); return false; }
  }
  const r = await api("/api/rewind", { mi });
  if (!r.ok) { toast(r.error || "Düzenlenemedi"); return false; }
  renderHistory(r.history);
  applyStatus(r);
  refreshChanges();
  if (r.restored && r.restored.length) toast(`${r.restored.length} dosya eski haline getirildi`);
  await submit(text);
  return true;
}

/* ===================================================================== takım */
// Sohbetteki mini kart: ALICE'in başlattığı projenin durumu. Ayrıntı ve yönetim Projeler sayfasında.
const TEAM_ST = { queued: "sırada", running: "çalışıyor", waiting: "bekliyor", asking: "soru soruyor",
  review: "inceleme bekliyor", done: "bitti", failed: "hata", stopped: "durdu", idle: "beklemede" };
const PROJ_ST = { planning: "planlanıyor", running: "çalışıyor", review: "incelemede", needs_you: "sana soruyor",
  done: "bitti", stopped: "durdu" };
function renderTeam(t) {
  const box = $("#team");
  S.team = t;
  if (!t || box.dataset.closed === t.id) { box.hidden = true; return; }
  box.hidden = false;
  box.replaceChildren();
  const [done, total] = t.progress || [0, 0];
  const head = el("div", "team-head");
  const open = el("button", "team-open");
  open.title = "Projeyi aç";
  open.append(el("i", "dot st-" + t.status), el("b", "", t.title), el("span", "pill " + t.status, PROJ_ST[t.status] || t.status),
    progressBar(done, total), el("span", "team-sum", total ? `${done}/${total}` : ""));
  open.addEventListener("click", () => openProject(t.id));
  const x = el("button", "act"); x.title = "Gizle"; x.appendChild(svg(IC.x));
  x.addEventListener("click", () => { box.dataset.closed = t.id; box.hidden = true; });
  head.append(open, x);
  box.appendChild(head);
  const q = (t.questions || []).find((q) => !q.answer);
  const line = q ? "Alice soruyor: " + q.text : t.status === "done" ? (t.summary || "").split("\n")[0] : activeLine(t);
  if (line) box.appendChild(el("div", "team-line" + (q ? " ask" : ""), line));
}
function activeLine(t) {
  const busy = t.members.filter((m) => ["running", "waiting", "asking"].includes(m.status));
  const s = busy.slice(0, 2).map((m) => `${m.name}: ${m.last ? friendlyLast(m.last) : TEAM_ST[m.status]}`).join(" · ");
  return s || (t.lead && t.lead.state === "running" ? "Alice planlıyor / inceliyor…" : "");
}
function progressBar(done, total) {
  const b = el("span", "prog"), i = el("i");
  i.style.width = total ? `${(100 * done) / total}%` : "0";
  b.appendChild(i);
  return b;
}
// Sohbetteki kart sadece ALICE'in o anki projesini gösterir; Projeler sayfası ve listesi her projeyi izler.
function onTeamEvent(ev) {
  if (!S.team || S.team.id === ev.id || ev.origin === "alice") renderTeam(ev);
  if (S.proj && S.proj.id === ev.id) refreshProjectSoon();
  clearTimeout(onTeamEvent._t);
  onTeamEvent._t = setTimeout(loadProjects, 800);
}
// Ajan ya da yönetici cevabını yazarken metin ve düşünme canlı gelir; açık konuşmanın altında akar.
function onTeamLive(ev) {
  S.live = S.live || {};
  const key = ev.id + "/" + ev.name;
  if (!ev.text && !ev.think) delete S.live[key]; else S.live[key] = ev;
  const pr = S.proj;
  if (pr && pr.id === ev.id && pr.sel === ev.name && (pr.tab || "chat") === "chat") drawLive();
}
function drawLive() {
  const pr = S.proj;
  const feed = document.querySelector("#projectView .pd-feed");
  if (!pr || !feed) return;
  const ev = (S.live || {})[pr.id + "/" + pr.sel];
  let box = feed.querySelector(".pf-stream");
  if (!ev) { if (box) box.remove(); return; }
  const stick = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 60;
  if (!box) {
    box = el("div", "pf-ai pf-stream");
    const th = el("details", "pf-think");
    th.appendChild(el("summary", "", "düşünüyor…"));
    th.appendChild(el("div", "pf-think-t"));
    box.append(th, el("div", "prose"));
    const live = feed.querySelector(".pf-live");
    if (live) live.remove();
    feed.appendChild(box);
  }
  const th = box.querySelector(".pf-think");
  th.hidden = !ev.think;
  th.querySelector(".pf-think-t").textContent = ev.think || "";
  th.querySelector("summary").textContent = ev.text ? "düşündü" : "düşünüyor…";
  setMd(box.querySelector(".prose"), ev.text || "");
  if (stick) feed.scrollTop = feed.scrollHeight;
}
// Projeden sohbete bildirim (soru, bitiş): tıklanınca projeyi açan kart.
function onProjectNote(ev) {
  const b = el("button", "pnote " + (ev.kind || "info"));
  b.append(el("i", "dot st-" + (ev.kind === "ask" ? "needs_you" : ev.kind === "done" ? "done" : "running")),
    el("span", "", ev.text), el("b", "", "Projeyi aç →"));
  b.addEventListener("click", () => openProject(ev.id));
  add(b);
  if (ev.kind === "ask" || ev.kind === "done") toast(ev.kind === "ask" ? "Proje sana soruyor" : "Proje bitti");
  loadProjects();
}
function fmtSecs(n) { return n < 60 ? `${n} sn` : n < 3600 ? `${Math.floor(n / 60)} dk ${n % 60} sn` : `${Math.floor(n / 3600)} sa ${Math.floor((n % 3600) / 60)} dk`; }
async function teamAction(action, name, text) {
  if (!S.team) return;
  const r = await api("/api/project/action", { id: S.team.id, action, name, text: text || "" });
  if (!r.ok) return toast(r.error || "Olmadı");
  if (r.team) renderTeam(r.team);
}

/* ================================================================== projeler */
// Claude Code'daki Projeler gibi: her proje kendi yöneticisi (Alice) olan bir ajan takımı; her ajan ayrı bir konuşma.
// Konular duruma göre gruplanır: soru soranlar ve inceleme bekleyenler üstte, bitenler altta.
async function loadProjects() {
  const rows = await api("/api/projects");
  S.projects = Array.isArray(rows) ? rows : [];
  drawProjects();
}
function drawProjects() {
  const nav = $("#projects");
  nav.replaceChildren();
  const rows = S.projects || [];
  if (!rows.length) { nav.appendChild(el("div", "side-empty small", "Henüz proje yok")); return; }
  for (const p of rows.slice(0, 30)) {
    const b = el("button", "proj-row" + (S.proj && S.proj.id === p.id ? " active" : ""));
    b.title = `${p.goal || p.title}\n${PROJ_ST[p.status] || p.status} · ${(p.members || []).length} ajan`;
    b.append(el("i", "dot st-" + p.status), el("span", "pt", p.title || "(adsız)"));
    const rev = (p.members || []).filter((m) => m.status === "review").length;
    if (p.questions) b.appendChild(el("span", "badge ask", "?"));
    else if (rev) b.appendChild(el("span", "badge rev", String(rev)));
    b.appendChild(el("small", "", relTime(p.updated || p.created)));
    b.addEventListener("click", () => openProject(p.id));
    nav.appendChild(b);
  }
}
async function openProject(id, sel) {
  const same = S.proj && S.proj.id === id;
  S.proj = { id, sel: sel || (same ? S.proj.sel : null), tab: same ? S.proj.tab : "chat", data: null };
  $("#app").classList.add("in-project");
  $("#projectView").hidden = false;
  drawProjects();
  closeSideOnMobile();
  await refreshProject();
}
function closeProject() {
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);
  S.proj = null;
  $("#app").classList.remove("in-project");
  $("#projectView").hidden = true;
  drawProjects();
}
function refreshProjectSoon() {
  if (refreshProjectSoon._t) return;
  refreshProjectSoon._t = setTimeout(() => { refreshProjectSoon._t = null; refreshProject(); }, 700);
}
// İlk açılışta dikkat isteyen konu seçilir: soru soran, inceleme bekleyen, çalışan; yoksa yönetici.
function pickThread(d) {
  for (const st of ["asking", "review", "running", "waiting"]) {
    const m = d.members.find((x) => x.status === st);
    if (m) return m.name;
  }
  return "alice";
}
async function refreshProject() {
  if (!S.proj) return;
  const pr = S.proj;
  if (!pr.sel) {
    const d0 = await api(`/api/project?id=${encodeURIComponent(pr.id)}`);
    if (!d0 || d0.ok === false || !d0.id) { toast((d0 && d0.error) || "Proje açılamadı"); closeProject(); loadProjects(); return; }
    pr.sel = pickThread(d0);
  }
  const sel = pr.sel !== "__log" ? pr.sel : "";  // "__council": proje kurulları
  const q = `/api/project?id=${encodeURIComponent(pr.id)}` + (sel ? `&member=${encodeURIComponent(sel)}` : "");
  const [d, df] = await Promise.all([api(q),
    pr.tab === "diff" && sel && !["alice", "__council", "__timeline"].includes(sel) ? api(`/api/project/diff?id=${encodeURIComponent(pr.id)}&member=${encodeURIComponent(sel)}`) : null]);
  if (S.proj !== pr) return;
  if (!d || d.ok === false || !d.id) { toast((d && d.error) || "Proje açılamadı"); closeProject(); loadProjects(); return; }
  pr.data = d;
  pr.diff = df ? df.diff || "" : null;
  renderProject();
}
function renderProject() {
  const pr = S.proj, d = pr.data, v = $("#projectView");
  // Yazılmakta olan mesaj ve kaydırma yeri yenilemede kaybolmasın.
  const keep = {};
  for (const t of v.querySelectorAll("textarea[data-k], input[data-k]")) keep[t.dataset.k] = { v: t.value, f: document.activeElement === t };
  const feed = v.querySelector(".pd-feed");
  const atBottom = !feed || feed.scrollHeight - feed.scrollTop - feed.clientHeight < 40;
  const oldTop = feed ? feed.scrollTop : 0;
  v.replaceChildren();

  // ---- başlık
  const head = el("div", "pv-head");
  const back = el("button", "icon-btn"); back.title = "Sohbete dön";
  back.innerHTML = '<svg viewBox="0 0 24 24"><path d="M15 6l-6 6 6 6"/></svg>';
  back.addEventListener("click", closeProject);
  const mid = el("div", "grow");
  const t1 = el("div", "pv-trow");
  t1.append(el("h2", "pv-title", d.title), el("span", "pill " + d.status, PROJ_ST[d.status] || d.status));
  const [done, total] = d.progress || [0, 0];
  const t2 = el("div", "pv-sub");
  t2.append(progressBar(done, total), el("span", "", total ? `${done}/${total} onaylandı` : "ajan yok"),
    el("span", "", `yönetici ${d.lead.model}${d.lead.thinking !== "default" ? " · " + d.lead.thinking : ""}`),
    el("span", "", d.council ? "kurul açık" : d.review ? "inceleme açık" : "inceleme kapalı"), el("span", "mono", d.cwd), el("span", "", relTime(d.created)));
  if (d.check) t2.appendChild(el("span", "mono", "kontrol: " + d.check));
  const goal = el("div", "pv-goal", d.goal);
  goal.title = "Tamamını göster";
  goal.addEventListener("click", () => goal.classList.toggle("open"));
  mid.append(t1, t2, goal);
  const acts = el("div", "pv-actions");
  const add = el("button", "btn", "Ajan ekle");
  add.addEventListener("click", () => projectDialog(d));
  acts.appendChild(add);
  if (d.council && d.members.length) {
    const ask = el("button", "btn", d.council_busy ? "Kurul toplanıyor…" : "Kurula sor");
    ask.title = "Projenin şu anki halini inceleme kuruluna sor";
    ask.disabled = !!d.council_busy;
    ask.addEventListener("click", () => projectAction("council", ""));
    acts.appendChild(ask);
  }
  if (d.members.length) {
    const tp = el("button", "btn", "Şablon");
    tp.title = "Bu projeyi şablon olarak kaydet: hedef, ayarlar ve ajanlar yeni projede tek tıkla gelir";
    tp.addEventListener("click", () => saveTemplateDialog(d));
    acts.appendChild(tp);
  }
  if (d.active) {
    const st = el("button", "btn danger", "Durdur");
    st.title = "Bütün ajanları ve yöneticiyi durdur";
    st.addEventListener("click", () => projectAction("stop", ""));
    acts.appendChild(st);
  } else {
    if (d.status === "stopped") {
      const rs = el("button", "btn accent", "Sürdür");
      rs.title = "Durmuş ajanlar kaldığı yerden devam eder";
      rs.addEventListener("click", () => projectAction("resume", ""));
      acts.appendChild(rs);
    }
    const del = el("button", "btn danger", "Sil");
    del.addEventListener("click", async () => {
      if (!(await confirmDialog("Proje silinsin mi?", "Projenin kaydı ve ajanların konuşmaları silinir. Dosyalarda yapılan değişiklikler kalır.", "Sil"))) return;
      if (await projectAction("delete", "")) { closeProject(); loadProjects(); }
    });
    acts.appendChild(del);
  }
  head.append(back, mid, acts);
  v.appendChild(head);

  // ---- sorular ve özet
  for (const q of (d.questions || []).filter((q) => !q.answer)) {
    const c = el("div", "pv-ask");
    c.append(el("b", "", "Alice soruyor"), el("div", "pv-ask-q", q.text));
    const row = el("div", "pv-ask-row");
    const inp = el("input"); inp.type = "text"; inp.placeholder = "Cevabın…"; inp.dataset.k = "q" + q.id;
    const go = el("button", "btn accent", "Cevapla");
    const send = async () => { const t = inp.value.trim(); if (t && await projectAction("answer", q.id, t)) inp.value = ""; };
    go.addEventListener("click", send);
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); send(); } });
    row.append(inp, go);
    c.appendChild(row);
    v.appendChild(c);
  }
  if (d.summary) {
    const c = el("details", "pv-summary");
    c.open = !pr.summaryClosed;
    c.addEventListener("toggle", () => { pr.summaryClosed = !c.open; });
    const sm = el("summary", "", "Proje özeti");
    const body = el("div", "prose");
    setMd(body, d.summary);
    c.append(sm, body);
    v.appendChild(c);
  }

  // ---- konular
  const body = el("div", "pv-body");
  const list = el("div", "pv-threads");
  const lead = { name: "alice", status: d.lead.state, task: d.status === "planning" ? "İşi planlıyor ve ajanları kuruyor" : "Planlar, soruları cevaplar, biten işleri inceler",
    model: d.lead.model, last: d.lead.last };
  const section = (title, items, cls) => {
    if (!items.length) return;
    list.appendChild(el("div", "th-sec" + (cls ? " " + cls : ""), `${title} · ${items.length}`));
    for (const it of items) list.appendChild(it);
  };
  section("Yönetici", [threadItem(lead, true), logItem(d), timelineItem(d),
    ...((d.councils || []).length || d.council_busy ? [councilItem(d)] : [])]);
  const by = (sts) => d.members.filter((m) => sts.includes(m.status)).map((m) => threadItem(m));
  section("Soru soruyor", by(["asking"]), "warn");
  section("İnceleme bekliyor", by(["review"]), "rev");
  section("Çalışıyor", by(["running", "waiting"]));
  section("Sırada", by(["queued"]));
  section("Bitti", by(["done"]), "ok");
  section("Durdu / hata", by(["stopped", "failed"]), "err");
  const det = el("div", "pv-detail");
  if (pr.sel === "__log") renderTeamLog(det, d);
  else if (pr.sel === "__council") renderProjectCouncils(det, d);
  else if (pr.sel === "__timeline") renderTimeline(det, d);
  else if (d.member) renderThread(det, d.member, d);
  else det.appendChild(el("div", "pd-empty", "Bir konu seç"));
  body.append(list, det);
  v.appendChild(body);

  drawLive();
  const nf = v.querySelector(".pd-feed");
  if (nf) nf.scrollTop = atBottom || pr.lastSel !== pr.sel ? nf.scrollHeight : oldTop;
  pr.lastSel = pr.sel;
  for (const t of v.querySelectorAll("textarea[data-k], input[data-k]")) {
    const k = keep[t.dataset.k];
    if (k) { t.value = k.v; if (k.f) t.focus(); }
  }
}
function threadItem(m, isLead) {
  const pr = S.proj;
  const b = el("button", "th" + (pr.sel === m.name ? " sel" : "") + (isLead ? " lead" : ""));
  const top = el("div", "th-top");
  const st = isLead ? (m.status === "running" ? "running" : m.status === "done" ? "done" : "idle") : m.status;
  top.append(el("i", "dot st-" + st), el("b", "", isLead ? "alice · yönetici" : m.name), el("small", "", TEAM_ST[st] || st));
  b.append(top, el("div", "th-task", m.task));
  let last;
  if (isLead) last = m.last;
  else if (m.status === "queued" && m.after && m.after.length) last = `${m.after.join(", ")} bitince başlar`;
  else if (m.status === "asking" && m.question) last = "soru: " + m.question;
  else if (m.council_running) last = m.council && m.council.rounds ? `kurul toplanıyor · tur ${m.council.round || 1}/${m.council.rounds}` : "kurul toplanıyor…";
  else if (m.status === "review" && m.council && m.council.decision) last = "kurul: " + (DEC_TR[m.council.decision] || m.council.decision);
  else if (["running", "waiting"].includes(m.status) && m.last) last = friendlyLast(m.last);
  else last = [m.model, m.thinking !== "default" ? m.thinking : "", `${m.steps} adım`, m.secs ? fmtSecs(m.secs) : "",
    m.files && m.files.length ? `${m.files.length} dosya` : "", m.reviews ? `${m.reviews} düzeltme` : ""].filter(Boolean).join(" · ");
  if (last) b.appendChild(el("div", "th-last", last));
  b.addEventListener("click", () => { pr.sel = m.name; pr.tab = "chat"; refreshProject(); });
  return b;
}
function logItem(d) {
  const pr = S.proj;
  const b = el("button", "th" + (pr.sel === "__log" ? " sel" : ""));
  const top = el("div", "th-top");
  top.append(el("i", "dot st-chat"), el("b", "", "Takım sohbeti"), el("small", "", `${d.log.length} mesaj`));
  const last = d.log[d.log.length - 1];
  b.append(top, el("div", "th-last", last ? `${last.from} → ${last.to}: ${last.text}` : "Henüz mesaj yok"));
  b.addEventListener("click", () => { pr.sel = "__log"; refreshProject(); });
  return b;
}
function renderThread(det, m, d) {
  const pr = S.proj, isLead = m.name === "alice";
  const h = el("div", "pd-head");
  const r1 = el("div", "pd-row");
  const st = isLead ? (m.status === "running" ? "running" : m.status === "done" ? "done" : "idle") : m.status;
  r1.append(el("i", "dot st-" + st), el("b", "", isLead ? "alice · yönetici" : m.name), el("span", "chip", TEAM_ST[st] || st),
    el("span", "pd-meta", [m.model, m.thinking !== "default" ? "düşünme " + m.thinking : "", isLead ? "" : m.isolation,
      m.steps ? `${m.steps} adım` : "", m.secs ? fmtSecs(m.secs) : ""].filter(Boolean).join(" · ")));
  const grow = el("span"); grow.style.flex = "1"; r1.appendChild(grow);
  if (!isLead) {
    if (["running", "waiting", "asking"].includes(m.status)) {
      const b = el("button", "btn danger", "Durdur");
      b.addEventListener("click", () => projectAction("stop", m.name));
      r1.appendChild(b);
    } else {
      if (m.files && m.files.length) {
        const b = el("button", "btn", "Değişiklikleri geri al");
        b.addEventListener("click", async () => {
          if (await confirmDialog("Değişiklikler geri alınsın mı?", `${m.name} ajanının dosya araçlarıyla yaptığı değişiklikler eski haline döner (${m.files.length} dosya).`, "Geri al"))
            projectAction("revert", m.name);
        });
        r1.appendChild(b);
      }
      if (d.council && !m.council_running) {
        const k = el("button", "btn", "Kurula sor");
        k.title = "Bu işi inceleme kuruluna sor (eleştirmen, savunucu, kontrolcü, uzmanlar)";
        k.addEventListener("click", () => { pr.tab = "council"; projectAction("council", m.name); });
        r1.appendChild(k);
      }
      const b = el("button", "btn", "Baştan başlat");
      b.title = "Temiz bir konuşmayla görevi yeniden yapsın";
      b.addEventListener("click", () => projectAction("restart", m.name));
      r1.appendChild(b);
    }
  }
  const rawBtn = el("button", "btn ghost" + (rawView() ? " on" : ""), rawView() ? "Ayrıntıları gizle" : "Ayrıntılar");
  rawBtn.title = "Araç çağrılarını ve iç olayları ham haliyle göster";
  rawBtn.addEventListener("click", () => { try { localStorage.setItem("alice.rawThreads", rawView() ? "" : "1"); } catch {} renderProject(); });
  r1.appendChild(rawBtn);
  h.append(r1, el("div", "pd-task", m.task));
  const r2 = el("div", "pd-row");
  if (m.constraints) r2.appendChild(el("span", "chip", "sınır: " + m.constraints));
  if (m.scope && m.scope.length) r2.appendChild(el("span", "chip", "yazar: " + m.scope.join(", ")));
  if (m.after && m.after.length) r2.appendChild(el("span", "chip", "sonra: " + m.after.join(", ")));
  if (m.branch) r2.appendChild(el("span", "chip file", "dal " + m.branch));
  if (m.limit_hit) r2.appendChild(el("span", "chip warn", "adım limiti doldu"));
  if (r2.childNodes.length) h.appendChild(r2);
  det.appendChild(h);

  if (m.status === "review") {
    const bar = el("div", "pd-review");
    const rvText = m.council_running ? "Kurul toplandı, tartışıyor. İstersen beklemeden kararı sen ver."
      : m.council && m.council.decision === "contested" ? "Kurul ikiye bölündü; Alice karar verecek. Kurul sekmesinden oy verebilir ya da kararı sen verebilirsin."
      : d.review ? "İnceleme bekliyor. Alice inceliyor; istersen kararı sen ver." : "Bitti, onay bekliyor.";
    bar.appendChild(el("div", "pd-rv-t", rvText));
    const row = el("div", "pd-ask-row");
    const fb = el("input"); fb.type = "text"; fb.placeholder = "Düzeltme için geri bildirim (ne eksik, ne bekliyorsun)…"; fb.dataset.k = "fb" + m.name;
    const no = el("button", "btn", "Düzeltme iste");
    no.addEventListener("click", async () => { const t = fb.value.trim(); if (!t) { fb.focus(); return toast("Önce geri bildirimi yaz"); } if (await projectAction("revise", m.name, t)) fb.value = ""; });
    const ok = el("button", "btn accent", "Onayla");
    ok.addEventListener("click", () => projectAction("approve", m.name));
    row.append(fb, no, ok);
    bar.appendChild(row);
    det.appendChild(bar);
  } else if (m.status === "asking" && m.question) {
    const bar = el("div", "pd-review ask");
    bar.appendChild(el("div", "pd-rv-t", "Alice'e soruyor: " + m.question));
    det.appendChild(bar);
  }

  if (!isLead) {
    const tabs = el("div", "pd-tabs");
    const tabsList = [["chat", "Konuşma"], ["diff", `Değişiklikler${m.files && m.files.length ? " · " + m.files.length : ""}`]];
    if ((m.councils || []).length) tabsList.push(["council", `Kurul · ${m.councils.length}${m.council_running ? " …" : ""}`]);
    for (const [k, label] of tabsList) {
      const b = el("button", "pd-tab" + ((pr.tab || "chat") === k ? " on" : ""), label);
      b.addEventListener("click", () => { pr.tab = k; refreshProject(); });
      tabs.appendChild(b);
    }
    det.appendChild(tabs);
  }
  const feed = el("div", "pd-feed");
  if (!isLead && pr.tab === "council") {
    const list = (m.councils || []).slice().reverse();
    list.forEach((c, i) => feed.appendChild(councilCard(c, m.name, i === 0)));
    if (!list.length) feed.appendChild(el("div", "pd-empty", "Henüz kurul toplanmadı"));
  } else if (!isLead && pr.tab === "diff") {
    const text = pr.diff || "";
    if (!text.trim()) feed.appendChild(el("div", "pd-empty", "Bu ajan dosya değiştirmedi"));
    for (const part of text.split(/\n(?=--- )/)) {
      if (!part.trim()) continue;
      if (!part.includes("\n@@")) { feed.appendChild(el("div", "pf-tool", part)); continue; }
      feed.appendChild(renderDiff(part).box);
    }
  } else {
    for (const node of buildFeed(m.history || [], isLead)) feed.appendChild(node);
    if (st === "running") {
      const w = el("div", "pf-live");
      w.append(el("i", "dot st-running"), el("span", "", m.last ? friendlyLast(m.last) : (isLead ? "Alice çalışıyor…" : "çalışıyor…")));
      feed.appendChild(w);
    }
    if (!feed.childNodes.length) feed.appendChild(el("div", "pd-empty", "Henüz bir şey yok"));
  }
  det.appendChild(feed);
  const say = el("div", "pd-say");
  const ta = el("textarea"); ta.rows = 1; ta.dataset.k = "say" + m.name;
  ta.placeholder = isLead ? "Alice'e yaz: yeni iş, yön değişikliği, soru…" :
    ["running", "waiting", "asking"].includes(m.status) ? `${m.name}'e yaz (bir sonraki adımında görür)…` : `${m.name}'e yaz: kaldığı yerden devam eder…`;
  const go = el("button", "btn primary", "Gönder");
  const send = async () => { const v = ta.value.trim(); if (!v) return; ta.value = ""; await projectAction("send", m.name, v); };
  go.addEventListener("click", send);
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
  ta.addEventListener("input", () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 160) + "px"; });
  say.append(ta, go);
  det.appendChild(say);
}
// ---- inceleme kurulu
const ROLE_TR = { critic: "eleştirmen", advocate: "savunucu", verifier: "kontrolcü", security: "güvenlik",
  performance: "performans", ux: "kullanıcı gözü" };
const VOTE_TR2 = { approve: "onay", conditional: "şartlı onay", reject: "ret" };
const DEC_TR = { approve: "onay", approve_conditions: "şartlı onay", revise: "düzeltme gerekli", contested: "çekişmeli" };
const KIND_TR = { plan: "Plan kurulu", work: "İş kurulu", recheck: "Kontrol turu", final: "Final kurulu", adhoc: "Kurula sor" };
const SEV_TR = { critical: "kritik", major: "önemli", minor: "küçük" };
const FST_TR = { confirmed: "doğrulandı", refuted: "çürütüldü", unclear: "belirsiz", pending: "bekliyor" };
function councilItem(d) {
  const pr = S.proj;
  const b = el("button", "th" + (pr.sel === "__council" ? " sel" : ""));
  const top = el("div", "th-top");
  const last = (d.councils || [])[d.councils.length - 1];
  top.append(el("i", "dot st-" + (d.council_busy ? "running" : "review")), el("b", "", "Kurul"),
    el("small", "", d.council_busy ? "toplanıyor" : `${(d.councils || []).length} toplantı`));
  b.append(top, el("div", "th-last", last ? `${KIND_TR[last.kind] || last.kind}: ${DEC_TR[last.decision] || "sürüyor"}` : "plan ve final kurulları"));
  b.addEventListener("click", () => { pr.sel = "__council"; refreshProject(); });
  return b;
}
function renderProjectCouncils(det, d) {
  const h = el("div", "pd-head");
  const r = el("div", "pd-row"); r.appendChild(el("b", "", "Kurul"));
  h.appendChild(r);
  const feed = el("div", "pd-feed");
  if ((d.lessons || []).length) {
    const box = el("div", "cc lessons");
    box.appendChild(el("b", "", "Bu projenin dersleri"));
    box.appendChild(el("div", "cc-title", "Kurulun doğruladığı ciddi sorunlar; sonraki ajanlara ve kurullara hatırlatılır."));
    const ul = el("ul", "cc-conds");
    for (const x of d.lessons) ul.appendChild(el("li", "", x));
    box.appendChild(ul);
    feed.appendChild(box);
  }
  const list = (d.council_list || []).slice().reverse();
  list.forEach((c, i) => feed.appendChild(councilCard(c, "", i === 0)));
  if (!list.length) feed.appendChild(el("div", "pd-empty", d.council_busy ? "Kurul toplanıyor…" : "Henüz kurul toplanmadı"));
  det.append(h, feed);
}
// ---- zaman çizelgesi
const TL_ICON = { start: "▶", run: "▷", review: "◎", done: "✓", approve: "✓", revise: "↺", failed: "✗", stopped: "■",
  stop: "■", resume: "▶", council: "⚖", ask: "?", answer: "↩", finish: "★", check: "✔", conflict: "⚠", stall: "⏳", plan: "✎", msg: "✉" };
function timelineItem(d) {
  const pr = S.proj;
  const b = el("button", "th" + (pr.sel === "__timeline" ? " sel" : ""));
  const top = el("div", "th-top");
  top.append(el("i", "dot st-chat"), el("b", "", "Akış"), el("small", "", `${(d.timeline_n || 0) + d.log.length} olay`));
  b.append(top, el("div", "th-last", "Kim ne zaman ne yaptı"));
  b.addEventListener("click", () => { pr.sel = "__timeline"; refreshProject(); });
  return b;
}
function renderTimeline(det, d) {
  const h = el("div", "pd-head");
  const r = el("div", "pd-row"); r.appendChild(el("b", "", "Akış"));
  h.append(r, el("div", "pd-task", "Projede olan her şey sırayla: başlayan ve biten işler, kurul kararları, onaylar, sorular, mesajlar."));
  const feed = el("div", "pd-feed tl");
  let day = "";
  for (const x of d.timeline || []) {
    const dt = new Date(x.at * 1000);
    const dd = dt.toLocaleDateString("tr-TR", { day: "numeric", month: "long" });
    if (dd !== day) { day = dd; feed.appendChild(el("div", "tl-day", dd)); }
    const row = el("div", "tl-row k-" + x.kind);
    row.append(el("small", "", dt.toLocaleTimeString("tr-TR", { hour: "2-digit", minute: "2-digit", second: "2-digit" })),
      el("i", "", TL_ICON[x.kind] || "•"), el("b", "", x.who), el("span", "", x.text));
    feed.appendChild(row);
  }
  if (!(d.timeline || []).length) feed.appendChild(el("div", "pd-empty", "Henüz olay yok"));
  det.append(h, feed);
}
function saveTemplateDialog(d) {
  showDialog((dl) => {
    dl.append(el("h2", "", "Şablon olarak kaydet"),
      el("p", "", "Hedef, ayarlar ve ajan tanımları kaydedilir (konuşmalar değil). Yeni proje penceresinde 'Şablonlarım' altında çıkar."));
    const inp = txt(d.title, "şablon adı");
    const row = el("div", "row");
    const no = el("button", "btn", "Vazgeç"); no.addEventListener("click", closeDialog);
    const ok = el("button", "btn accent", "Kaydet");
    ok.addEventListener("click", async () => { if (await projectAction("save_template", "", inp.value.trim())) closeDialog(); });
    row.append(no, ok);
    dl.append(inp, row);
    setTimeout(() => inp.focus(), 0);
  });
}
// Eski kayıtlarda döküm ham JSON içerebilir; okunur kısmı al.
function councilProse(t) {
  t = (t || "").replace(/```(?:json)?\s*\{[\s\S]*?\}\s*```/g, "");
  const m = t.search(/\{\s*"(?:vote|checks|stances|findings|reason|conditions)"/);
  if (m >= 0) t = t.slice(0, m);
  t = t.replace(/```(?:json)?\s*$/, "").trim();
  if (/"(?:stance|status|id)"\s*:/.test(t)) return "";  // baştan kırpılmış JSON parçası
  return t;
}
function findingRow(f) {
  const fc = el("div", "cf st-" + f.status);
  const t = el("div", "cf-top");
  const agree = (f.notes || []).filter((n) => n.stance === "agree").length;
  const against = (f.notes || []).length - agree;
  t.append(el("b", "", f.id), el("span", "chip sev-" + f.severity, SEV_TR[f.severity] || f.severity),
    el("span", "cf-title", f.title));
  const meta = el("small", "", [ROLE_TR[f.by] || f.by, FST_TR[f.status] || f.status,
    agree ? `${agree} katılıyor` : "", against ? `${against} itiraz` : ""].filter(Boolean).join(" · "));
  t.appendChild(meta);
  fc.appendChild(t);
  const more = el("div", "cf-more");
  if (f.evidence) more.appendChild(el("div", "cf-ev", f.evidence));
  if (f.fix) more.appendChild(el("div", "cf-fix", "Öneri: " + f.fix));
  if (f.check) more.appendChild(el("div", "cf-chk", "Kontrolcü: " + f.check));
  for (const n of f.notes || []) more.appendChild(el("div", "cf-note", `${ROLE_TR[n.by] || n.by} ${n.stance === "agree" ? "katılıyor" : "itiraz ediyor"}: ${n.note}`));
  if (more.childNodes.length) {
    fc.appendChild(more);
    t.addEventListener("click", () => fc.classList.toggle("open"));
    fc.classList.add("has-more");
  }
  return fc;
}
function councilCard(c, target, latest) {
  const res = c.result || {};
  const running = c.state === "running";
  // Eski toplantılar tek satır başlık olarak kapalı durur; en son ve süren açık.
  const card = el(latest || running ? "div" : "details", "cc" + (running ? " live" : ""));
  const head = el(latest || running ? "div" : "summary", "cc-head");
  head.append(el("b", "", KIND_TR[c.kind] || "Kurul"));
  if (running) head.appendChild(el("span", "pill running", `toplanıyor · tur ${c.round || 1}/${c.rounds || "?"}`));
  else head.appendChild(el("span", "pill dec-" + (res.decision || "contested"), DEC_TR[res.decision] || "tamamlanamadı"));
  if (c.started) head.appendChild(el("small", "cc-when", new Date(c.started * 1000).toLocaleTimeString("tr-TR", { hour: "2-digit", minute: "2-digit" })));
  card.appendChild(head);
  if (res.error) card.appendChild(el("div", "cc-err", "Kurul tamamlanamadı."));

  // İlk bakışta sadece özet: oyların tek cümlesi ve düzeltilecek birkaç madde. Gerisi "Ayrıntılar"da.
  const vs = (c.votes || []).filter((v) => v.last);
  if (running) card.appendChild(el("div", "cc-sum", "Üyeler inceliyor, sonuç birazdan burada."));
  else if (vs.length) {
    const cnt = {};
    for (const v of vs) cnt[v.last] = (cnt[v.last] || 0) + 1;
    const parts = ["reject", "conditional", "approve"].filter((k) => cnt[k]).map((k) => `${cnt[k]} ${VOTE_TR2[k]}`);
    card.appendChild(el("div", "cc-sum", `${vs.length} üye oy verdi: ${parts.join(", ")}.`));
  }
  if (!running) {
    const sevRank = { critical: 0, major: 1, minor: 2 };
    const todo = [...new Set([...(res.conditions || []),
      ...(c.findings || []).filter((f) => f.status === "confirmed" && f.severity !== "minor")
        .sort((a, b) => sevRank[a.severity] - sevRank[b.severity]).map((f) => f.title)])];
    if (todo.length && res.decision !== "approve") {
      const ul = el("ul", "cc-todo");
      for (const x of todo.slice(0, 4)) ul.appendChild(el("li", "", x));
      if (todo.length > 4) ul.appendChild(el("li", "more", `ve ${todo.length - 4} madde daha`));
      card.appendChild(ul);
    } else if (res.decision === "approve") card.appendChild(el("div", "cc-sum ok", "Engelleyen bir sorun yok."));
  }
  if (latest && !running) {
    const row = el("div", "cc-uv");
    row.appendChild(el("span", "", c.user_vote ? `Oyun: ${VOTE_TR2[c.user_vote.vote]}. Değiştir:` : "Senin oyun:"));
    for (const [v, l] of [["approve", "Onay"], ["conditional", "Şartlı"], ["reject", "Ret"]]) {
      const b = el("button", "btn ghost" + (c.user_vote && c.user_vote.vote === v ? " on" : ""), l);
      b.addEventListener("click", () => projectAction("vote", target, v, {}));
      row.appendChild(b);
    }
    card.appendChild(row);
  }
  const more = el("details", "cc-more");
  more.appendChild(el("summary", "", "Ayrıntılar"));
  card.appendChild(more);
  const card0 = card;
  {
  const card = more;
  if (c.title) card.appendChild(el("div", "cc-title", c.title));
  if (res.score !== undefined && !running) card.appendChild(el("div", "cc-title", `Ağırlıklı onay oranı %${Math.round((res.score || 0) * 100)}` +
    ((res.veto || []).length ? ` · veto eden bulgular: ${res.veto.join(", ")}` : "") + (c.critical ? " · kritik iş, en güçlü modeller" : "")));
  if (res.error) card.appendChild(el("div", "cc-err", res.error));

  // oylar: rol başına küçük kutu; gerekçe iki satır, tıklayınca tamamı
  const votes = el("div", "cc-votes");
  const voteBox = (name, model, v, weight, reason, off) => {
    const box = el("div", "cv v-" + (v || "none") + (off ? " off" : ""));
    const top = el("div", "cv-top");
    top.append(el("b", "", name), el("span", "vote v-" + (v || "none"), v ? VOTE_TR2[v] : "oy yok"));
    if (weight > 1) top.appendChild(el("span", "cv-w", `×${weight}`));
    box.appendChild(top);
    if (model) box.title = model;
    if (off) box.appendChild(el("div", "cv-off", "sayılmadı: " + off));
    if (reason) {
      const r = el("div", "cv-r", reason);
      r.title = "Tamamını göster";
      r.addEventListener("click", () => r.classList.toggle("open"));
      box.appendChild(r);
    }
    return box;
  };
  for (const v of c.votes || []) {
    const changed = v.first && v.last && v.first !== v.last;
    const box = voteBox(ROLE_TR[v.role] || v.role, v.model || "", v.last, v.weight, v.reason, v.counted === false ? v.note : "");
    if (changed) box.querySelector(".vote").textContent = `${VOTE_TR2[v.first]} → ${VOTE_TR2[v.last]}`;
    votes.appendChild(box);
  }
  if (c.user_vote) votes.appendChild(voteBox("sen", "", c.user_vote.vote, 3, c.user_vote.note, ""));
  if (votes.childNodes.length) card.appendChild(votes);

  // bulgular: doğrulanan ve belirsizler görünür, çürütülenler kapalı; ayrıntı tıklayınca
  const order = { confirmed: 0, unclear: 1, pending: 2, refuted: 3 };
  const sev = { critical: 0, major: 1, minor: 2 };
  const fs = (c.findings || []).slice().sort((a, b) => (order[a.status] - order[b.status]) || (sev[a.severity] - sev[b.severity]) || a.id.localeCompare(b.id, undefined, { numeric: true }));
  const live = fs.filter((f) => f.status !== "refuted"), dead = fs.filter((f) => f.status === "refuted");
  if (fs.length) {
    const n = (st) => fs.filter((f) => f.status === st).length;
    card.appendChild(el("div", "cc-sub", ["Bulgular", n("confirmed") ? `${n("confirmed")} doğrulandı` : "", n("unclear") ? `${n("unclear")} belirsiz` : "",
      dead.length ? `${dead.length} çürütüldü` : ""].filter(Boolean).join(" · ")));
    const box = el("div", "cc-findings");
    for (const f of live) box.appendChild(findingRow(f));
    if (dead.length) {
      const d = el("details", "cc-dead");
      d.appendChild(el("summary", "", `Çürütülen bulgular (${dead.length})`));
      for (const f of dead) d.appendChild(findingRow(f));
      box.appendChild(d);
    }
    card.appendChild(box);
  } else if (!running) card.appendChild(el("div", "cc-title", "Bulgu yok."));
  // tartışma dökümü: tur tur, her rol kapalı; JSON kararlar zaten yukarıda
  const tr = (c.transcript || []).map((t) => ({ ...t, text: councilProse(t.text) })).filter((t) => t.text);
  if (tr.length) {
    const dt = el("details", "cc-tr");
    dt.appendChild(el("summary", "", `Tartışma dökümü (${tr.length} konuşma)`));
    let round = null;
    for (const t of tr) {
      if (t.round !== round) { round = t.round; dt.appendChild(el("div", "cc-tr-round", round === 1 ? "Tur 1 · bağımsız inceleme" : `Tur ${round}`)); }
      const it = el("details", "cc-tr-it");
      const sm = el("summary");
      sm.append(el("b", "", ROLE_TR[t.role] || t.role), el("span", "", t.text.replace(/[#*_`>]/g, "").split("\n").find((x) => x.trim()) || ""));
      const pz = el("div", "prose");
      setMd(pz, t.text);
      it.append(sm, pz);
      dt.appendChild(it);
    }
    card.appendChild(dt);
  }
  }
  return card0;
}
// ---- konuşmayı insanca göster: iç olaylar küçük notlara, araç çağrıları tek açılır satıra dönüşür.
function rawView() { try { return localStorage.getItem("alice.rawThreads") === "1"; } catch { return false; } }
const HIDDEN_TOOLS = new Set(["ask_user", "finish", "project_status"]);  // içerikleri soru kartında ve özette zaten var
const TOOL_TR = {
  read_file: ["okudu", "dosya okudu"], list_dir: ["klasöre baktı", "klasöre baktı"], glob: ["dosya aradı", "dosya aradı"],
  grep: ["kodda aradı", "kodda aradı"], write_file: ["dosya yazdı", "dosya yazdı"], edit_file: ["düzenledi", "düzenleme yaptı"],
  multi_edit: ["düzenledi", "düzenleme yaptı"], apply_patch: ["yama uyguladı", "yama uyguladı"], file_ops: ["dosya işlemi", "dosya işlemi yaptı"],
  run_command: ["komut çalıştırdı", "komut çalıştırdı"], process_output: ["çıktıya baktı", "çıktıya baktı"],
  web_search: ["web'de aradı", "web'de aradı"], web_fetch: ["sayfa okudu", "sayfa okudu"], http_request: ["istek gönderdi", "istek gönderdi"],
  git: ["git", "git kullandı"], todo_write: ["planı güncelledi", "planı güncelledi"], team_send: ["mesaj attı", "mesaj attı"],
  team_wait: ["cevap bekledi", "cevap bekledi"], message: ["mesaj attı", "mesaj attı"], assign: ["ajan kurdu", "ajan kurdu"],
  plan: ["planı sundu", "planı sundu"], review: ["inceledi", "karar verdi"], diff: ["farka baktı", "farka baktı"],
  council: ["kurula sordu", "kurula sordu"],
};
function toolLine(it) {
  const [verb] = TOOL_TR[it.name] || [it.name];
  const sum = it.summary || "";
  if (it.name === "assign") return `ajan kurdu: ${sum}`;
  if (it.name === "plan") return `planı kurdu (${sum.replace(/^plan:\s*/, "")})`;
  if (it.name === "review") return sum.includes("approve") ? `${sum.split(":")[0]} işini onayladı` : `${sum.split(":")[0]} işini düzeltmeye gönderdi`;
  if (it.name === "message" || it.name === "team_send") { const i = sum.indexOf(":"); return `${sum.slice(0, i)}'e yazdı: ${sum.slice(i + 1).trim()}`; }
  if (it.name === "team_wait") return "cevap bekledi";
  if (it.name === "run_command") return `komut çalıştırdı`;
  return sum ? `${verb}: ${sum}` : verb;
}
function friendlyLast(last) {
  const name = last.split(" ")[0];
  if (HIDDEN_TOOLS.has(name)) return "çalışıyor…";
  return toolLine({ name, summary: last.slice(name.length + 1) });
}
// Yöneticiye giden olay metinleri (talimat içerir) kısa notlara çevrilir.
const EVENT_RULES = [
  [/^Proje başladı ve henüz ajan yok/, () => "Proje başladı; Alice işi planlıyor."],
  [/^Proje başladı; ajanlar çalışıyor/, () => "Proje başladı; ajanlar çalışıyor."],
  [/^(\S+) bitirdi, inceleme bekliyor/, (m) => `${m[1]} işini bitirdi, inceleniyor.`],
  [/^(\S+) bitirdi \(inceleme yok\)/, (m) => `${m[1]} işini bitirdi.`],
  [/^(\S+) hata verdi/, (m) => `${m[1]} hata verdi.`],
  [/^(\S+) (\d+) dakikadır ilerlemiyor/, (m) => `${m[1]} ${m[2]} dakikadır ilerlemiyor.`],
  [/^Kullanıcı (\S+) ajanının işini onayladı/, (m) => `${m[1]} işini sen onayladın.`],
  [/^Kullanıcı projeyi sürdürdü/, () => "Projeyi sürdürdün."],
  [/^Bütün ajanlar bitti/, () => "Bütün ajanlar bitti; Alice sonucu doğruluyor."],
  [/^Final kurulu|^Final kurulu projeyi onayladı/, () => "Final kurulu toplandı."],
  [/Final kurulu: (\S+)/, (m) => `Final kurulu: ${m[1].toLowerCase()}.`],
  [/^Kurul (\S+) işinde ikiye bölündü/, (m) => `Kurul ${m[1]} işinde ikiye bölündü; Alice karar veriyor.`],
  [/^Kurul (\S+) işini iki düzeltme/, (m) => `Kurul ${m[1]} işini hâlâ kabul etmiyor; Alice karar veriyor.`],
  [/^İnceleme bekleyen ajanlar var/, () => "Alice'e inceleme hatırlatıldı."],
  [/^Henüz ajan kurmadın/, () => "Alice'e planı sunması hatırlatıldı."],
  [/proje kapanmadı/, () => "Alice'e projeyi bitirmesi hatırlatıldı."],
  [/^Kullanıcı kurula sordu/, () => "Kurula sordun; sonuç Alice'e iletildi."],
  [/^Kullanıcı kurulda oy verdi/, () => "Kurulda oy verdin."],
  [/^Kurul çalışamadı/, () => "Kurul toplanamadı; Alice kendisi inceliyor."],
];
function eventNotes(body) {
  // "[olaylar]" gövdesi: sistem olayları ve "[kimden -> kime] mesaj" satırları, boş satırla ayrılmış
  const out = [];
  for (const part of body.split(/\n\n(?=\S)/)) {
    const msg = part.match(/^\[([^\]]+?) -> ([^\]]+?)\] ([\s\S]*)$/);
    if (msg) { out.push({ kind: "msg", from: msg[1], to: msg[2], text: msg[3] }); continue; }
    const rule = EVENT_RULES.find(([rx]) => rx.test(part));
    if (rule) out.push({ kind: "note", text: rule[1](part.match(rule[0])) });
    else if (/^(Raporu|Değişen dosyalar|Dal:|İncele:|Kurula sor|İş kurulu|Kontrol turu|-|\(|Bulgular|Şartlar)/.test(part)) continue;
    else out.push({ kind: "note", text: part.split("\n")[0].slice(0, 160) });
  }
  return out;
}
function msgCard(from, to, text) {
  const c = el("div", "pf-msg" + (from === "kullanıcı" ? " me" : ""));
  c.append(el("b", "", from === "kullanıcı" ? "Sen" : from === "alice" ? "Alice" : from), el("span", "", to && to !== "alice" ? ` → ${to}` : ""));
  // Soruya verilen cevaplar yöneticiye "Sorduğun soruya cevap (soru): cevap" diye gider; kullanıcıya sadece cevap.
  if (from === "kullanıcı") text = text.replace(/^Sorduğun soruya cevap \([\s\S]*?\): /, "");
  const t = el("div", "pf-msg-t"); setMd(t, text); c.appendChild(t);
  return c;
}
function buildFeed(history, isLead) {
  if (rawView()) return history.map(feedItem);
  const out = [];
  let group = [];
  const flush = () => {
    const visible = group.filter((it) => !HIDDEN_TOOLS.has(it.name));
    group = [];
    if (!visible.length) return;
    const box = el("details", "pf-steps");
    const counts = {};
    for (const it of visible) { const k = (TOOL_TR[it.name] || [it.name, it.name])[1]; counts[k] = (counts[k] || 0) + 1; }
    const errs = visible.filter((it) => it.is_error).length;
    const head = Object.entries(counts).map(([k, n]) => (n > 1 ? `${k} ×${n}` : k)).join(" · ");
    box.appendChild(el("summary", "", head + (errs ? ` · ${errs} hata` : "")));
    for (const it of visible) {
      const row = el("div", "pf-step" + (it.is_error ? " err" : ""));
      row.appendChild(el("span", "", toolLine(it)));
      const pre = el("pre", "", (it.text || "").slice(0, 3000));
      row.appendChild(pre);
      row.addEventListener("click", () => row.classList.toggle("open"));
      box.appendChild(row);
    }
    out.push(box);
  };
  for (const it of history) {
    if (it.type === "tool") { group.push(it); continue; }
    flush();
    if (it.type === "assistant") { if ((it.text || "").trim()) out.push(feedItem(it)); continue; }
    const txt = it.text || "";
    if (txt === "Görevine başla.") { out.push(el("div", "pf-note", "Görev başladı")); continue; }
    if (txt.startsWith("[olaylar]")) {
      const body = txt.replace(/^\[olaylar\]\n/, "").replace(/\n\n\[durum\]\n[\s\S]*$/, "");
      for (const n of eventNotes(body)) out.push(n.kind === "msg" ? msgCard(n.from, n.to, n.text) : el("div", "pf-note", n.text));
      continue;
    }
    if (txt.startsWith("[takım mesaj")) {
      const body = txt.replace(/^\[takım mesajı\]\n|^\[takım mesajları\]\n/, "").replace(/\n\nBunlara göre işine devam et[\s\S]*$/, "");
      for (const line of body.split(/\n(?=\[)/)) {
        const m = line.match(/^\[([^\]]+?) -> ([^\]]+?)\] ([\s\S]*)$/);
        out.push(m ? msgCard(m[1], m[2], m[3]) : el("div", "pf-note", line.slice(0, 200)));
      }
      continue;
    }
    if (txt === "Devam et.\n\nBunlara göre işine devam et; bitince yine kısa rapor yaz.") { out.push(el("div", "pf-note", "Devam ediyor")); continue; }
    out.push(feedItem(it));
  }
  flush();
  return out;
}
function feedItem(it) {
  if (it.type === "user") {
    const u = el("div", "pf-user");
    const txt = it.text || "";
    const label = txt.startsWith("[olaylar]") ? "olaylar" : txt.startsWith("[takım mesaj") ? "takım mesajı" : txt === "Görevine başla." ? "başlangıç" : "";
    if (label) u.appendChild(el("small", "", label));
    // Yöneticiye giden olaylardaki "[durum]" tablosu zaten solda görünüyor; konuşmada gizle.
    u.appendChild(document.createTextNode(txt.replace(/^\[(olaylar|takım mesajı|takım mesajları)\]\n/, "").replace(/\n\n\[durum\]\n[\s\S]*$/, "")));
    return u;
  }
  if (it.type === "assistant") {
    const a = el("div", "pf-ai");
    const prose = el("div", "prose");
    setMd(prose, it.text);
    a.appendChild(prose);
    return a;
  }
  if (it.type === "tool") {
    const t = el("div", "pf-tool" + (it.is_error ? " err" : ""));
    t.append(el("b", "", it.name), document.createTextNode(" " + (it.summary || "")));
    t.appendChild(el("pre", "", (it.text || "").slice(0, 4000)));
    t.addEventListener("click", () => t.classList.toggle("open"));
    return t;
  }
  return el("div", "pf-tool", it.text || "");
}
function renderTeamLog(det, d) {
  const h = el("div", "pd-head");
  const r = el("div", "pd-row"); r.appendChild(el("b", "", "Takım sohbeti"));
  h.append(r, el("div", "pd-task", "Ajanların birbirine, Alice'e ve sana yazdıkları. Buradan herkese ya da tek bir ajana yazabilirsin."));
  const feed = el("div", "pd-feed");
  for (const x of d.log) {
    const row = el("div", "pf-log" + (x.from === "kullanıcı" ? " me" : x.from === "sistem" ? " sys" : ""));
    row.append(el("b", "", `${x.from} → ${x.to}`), el("span", "", x.text),
      el("small", "", new Date(x.at * 1000).toLocaleTimeString("tr-TR", { hour: "2-digit", minute: "2-digit" })));
    feed.appendChild(row);
  }
  if (!d.log.length) feed.appendChild(el("div", "pd-empty", "Henüz mesaj yok"));
  const say = el("div", "pd-say");
  const sel = el("select"); sel.dataset.k = "logto";
  for (const [v, l] of [["alice", "alice (yönetici)"], ["all", "herkes"], ...d.members.map((m) => [m.name, m.name])]) {
    const o = el("option", "", l); o.value = v; sel.appendChild(o);
  }
  const ta = el("textarea"); ta.rows = 1; ta.placeholder = "Takıma yaz…"; ta.dataset.k = "logsay";
  const go = el("button", "btn primary", "Gönder");
  const send = async () => { const v = ta.value.trim(); if (!v) return; ta.value = ""; await projectAction("send", sel.value, v); };
  go.addEventListener("click", send);
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
  say.append(sel, ta, go);
  det.append(h, feed, say);
}
async function projectAction(action, name, text, agent) {
  if (!S.proj) return false;
  const r = await api("/api/project/action", { id: S.proj.id, action, name, text: text || "", agent });
  if (!r.ok) { toast(r.error || "Olmadı"); return false; }
  if (r.note && !["send", "answer"].includes(action)) toast(r.note);
  if (action !== "delete") refreshProject();
  return true;
}

// ---- yeni proje / ajan ekle penceresi
const THINK_OPTS = [["default", "Varsayılan"], ["off", "Düşünmesin"], ["low", "Az"], ["medium", "Orta"], ["high", "Çok"]];
const PLACE_OPTS = [["shared", "Aynı klasör"], ["worktree", "Ayrı git dalı"], ["readonly", "Salt okuma"]];
// Şablon hedefin altına "bitti sayılma" ölçütünü ekler; ilk satır (proje başlığı) kullanıcının kendi cümlesi kalır.
const TEMPLATES = [
  ["Özellik ekle", "Eklenecek özellik: ", "Bitti sayılma: özellik çalışıyor, testleri yazılmış ve geçiyor."],
  ["Hata düzelt", "Düzeltilecek hata: ", "Bitti sayılma: kök neden bulunmuş, düzeltilmiş, tekrarını önleyen test eklenmiş."],
  ["Kod incelemesi", "Projeyi incele", "Hataları, güvenlik açıklarını ve riskli yerleri öncelik sırasıyla raporla. Kod değiştirme."],
  ["Yeniden düzenle", "Yeniden düzenlenecek kısım: ", "Bitti sayılma: davranış aynı, bütün testler geçiyor."],
  ["Test yaz", "Testi eksik kısımlara test yaz", "Bitti sayılma: önemli yollar test edilmiş, bulunan hatalar raporlanmış."],
];
function field(label, input, hint) {
  const f = el("label", "fld");
  f.appendChild(el("span", "fl", label));
  f.appendChild(input);
  if (hint) f.appendChild(el("span", "fh", hint));
  return f;
}
function sel(opts, val) {
  const s = el("select");
  for (const [v, l] of opts) { const o = el("option", "", l); o.value = v; s.appendChild(o); }
  s.value = val;
  return s;
}
function txt(val, ph, list) {
  const i = el("input"); i.type = "text"; i.value = val || ""; if (ph) i.placeholder = ph; if (list) i.setAttribute("list", list);
  return i;
}
function agentCard(spec, n) {
  spec = spec || {};
  const box = el("div", "np-agent");
  const top = el("div", "np-row");
  const nm = txt(spec.name || "", "ad"); nm.className = "nm";
  const model = txt(spec.model || "brain", "", "npModels");
  const th = sel(THINK_OPTS, spec.thinking || "default");
  const iso = sel(PLACE_OPTS, spec.isolation || "shared");
  top.append(el("span", "np-n", String(n || "")), field("Ad", nm), field("Model", model), field("Düşünme", th), field("Çalışma yeri", iso));
  const x = el("button", "act"); x.title = "Kaldır"; x.appendChild(svg(IC.x));
  x.addEventListener("click", () => box.remove());
  top.appendChild(x);
  const task = el("textarea"); task.placeholder = "Ne yapacak, hangi dosyalarda, nasıl doğrulayacak?"; task.value = spec.task || "";
  const adv = el("details", "np-adv");
  adv.appendChild(el("summary", "", "Kapsam, sınırlar, sıra"));
  const sc = txt((spec.scope || []).join(", "), "ör. src/api, tests (boş: her yer)");
  const cn = txt(spec.constraints || "", "ör. veritabanı şemasına dokunma");
  const af = txt((spec.after || []).join(", "), "ör. backend");
  const g = el("div", "np-grid");
  g.append(field("Yazabileceği yerler", sc), field("Sınırlar", cn), field("Şunlar bitince başlasın", af));
  adv.appendChild(g);
  box.append(top, field("Görev", task), adv);
  box.read = () => ({ name: nm.value.trim().toLowerCase(), model: model.value.trim() || "brain", thinking: th.value, isolation: iso.value,
    task: task.value.trim(), constraints: cn.value.trim(),
    scope: sc.value.split(",").map((s) => s.trim()).filter(Boolean), after: af.value.split(",").map((s) => s.trim().toLowerCase()).filter(Boolean) });
  return box;
}
function projectDialog(project) {
  showDialog((d) => {
    d.classList.add("wide", "np");
    const adding = !!project;
    const dl = el("datalist"); dl.id = "npModels";
    for (const v of ["brain", "fast", ...((S.status.brain_chain || []).slice(0, 8))]) { const o = el("option"); o.value = v; dl.appendChild(o); }
    d.appendChild(dl);
    const err = el("div", "note err");
    if (adding) {
      d.append(el("h2", "", "Ajan ekle"), el("p", "", `"${project.title}" projesine katılır ve hemen başlar; diğerlerine haber verilir.`));
      const card = agentCard({}, 0);
      d.append(card, err);
      const row = el("div", "row");
      const no = el("button", "btn", "Vazgeç"); no.addEventListener("click", closeDialog);
      const ok = el("button", "btn accent", "Ekle ve başlat");
      ok.addEventListener("click", async () => { if (await projectAction("add", "", "", card.read())) closeDialog(); });
      row.append(no, ok);
      d.appendChild(row);
      setTimeout(() => card.querySelector("input").focus(), 0);
      return;
    }
    d.append(el("h2", "", "Yeni proje"),
      el("p", "", "Alice işi ajanlara böler, sorularını cevaplar, biten her işi inceler ve sonunda sana özet yazar. Sen Projeler sayfasından izler, istediğin an müdahale edersin."));
    const goal = el("textarea", "np-goal");
    goal.placeholder = "Ne yapılacak? Bitti sayılma ölçütünü ve bilinmesi gerekenleri de yaz.";
    const chips = el("div", "np-chips");
    for (const [label, first, crit] of TEMPLATES) {
      const c = el("button", "chip-btn", label);
      c.addEventListener("click", () => {
        const head = goal.value.split("\n\n")[0].trim() || first;
        goal.value = head + "\n\n" + crit;
        goal.focus();
        goal.setSelectionRange(head.length, head.length);
      });
      chips.appendChild(c);
    }
    d.append(field("Hedef", goal), chips);
    const mine = el("div", "np-chips mine");
    d.appendChild(mine);

    const seg = el("div", "seg");
    const m1 = el("button", "on", "Alice planlasın"), m2 = el("button", "", "Ajanları ben belirleyeyim");
    m1.title = "Alice kodu inceleyip ajanları, modellerini ve sırasını kendisi seçer (önerilen)";
    seg.append(m1, m2);
    d.appendChild(seg);
    const manual = el("div", "np-manual"); manual.hidden = true;
    const list = el("div", "np-agents");
    const more = el("button", "btn", "+ Ajan ekle");
    const renum = () => [...list.children].forEach((c, i) => { c.querySelector(".np-n").textContent = i + 1; });
    more.addEventListener("click", () => { if (list.children.length < 8) { list.appendChild(agentCard({}, list.children.length + 1)); renum(); } });
    manual.append(list, more);
    d.appendChild(manual);
    const setMode = (man) => {
      m1.classList.toggle("on", !man); m2.classList.toggle("on", man); manual.hidden = !man;
      if (man && !list.children.length) list.appendChild(agentCard({ name: "backend" }, 1));
    };
    m1.addEventListener("click", () => setMode(false));
    m2.addEventListener("click", () => setMode(true));

    const opts = el("details", "np-opts");
    opts.appendChild(el("summary", "", "Ayarlar"));
    const g = el("div", "np-grid");
    const cwd = txt(S.status.cwd || "", "çalışma klasörü");
    const lm = txt("brain", "", "npModels");
    const lt = sel(THINK_OPTS, "default");
    const ck = txt("", "ör. python -m pytest -q");
    const guess = async () => {
      const r = await api(`/api/project/check_guess?cwd=${encodeURIComponent(cwd.value.trim())}`);
      ck.placeholder = r && r.check ? `öneri: ${r.check} (boş bırakırsan çalışmaz)` : "ör. python -m pytest -q";
      ck.dataset.guess = (r && r.check) || "";
    };
    cwd.addEventListener("change", guess);
    guess();
    ck.addEventListener("focus", () => { if (!ck.value && ck.dataset.guess) ck.value = ck.dataset.guess; });
    const rv = el("input"); rv.type = "checkbox"; rv.checked = true;
    const rvl = el("label", "chk"); rvl.append(rv, document.createTextNode(" Biten işleri Alice incelesin (onaylasın ya da düzeltmeye göndersin)"));
    const kc = el("input"); kc.type = "checkbox"; kc.checked = true;
    const kcl = el("label", "chk"); kcl.append(kc, document.createTextNode(" İnceleme kurulu: plan, her iş ve final; eleştirmen, savunucu, kontrolcü ve uzmanlar tartışıp oylar"));
    rv.addEventListener("change", () => { kc.disabled = !rv.checked; if (!rv.checked) kc.checked = false; });
    g.append(field("Klasör", cwd), field("Yönetici modeli", lm), field("Yönetici düşünmesi", lt));
    const ckf = field("Kontrol komutu", ck, "Testler/lint: her kurul öncesi otomatik çalışır, çıktısı kanıt olur.");
    ckf.classList.add("wide");
    g.appendChild(ckf);
    opts.append(g, rvl, kcl);
    d.append(opts, err);
    const row = el("div", "row");
    const no = el("button", "btn", "Vazgeç"); no.addEventListener("click", closeDialog);
    const ok = el("button", "btn accent", "Başlat");
    ok.addEventListener("click", async () => {
      const g2 = goal.value.trim();
      if (!g2) { err.textContent = "Hedefi yaz"; goal.focus(); return; }
      const agents = manual.hidden ? [] : [...list.children].map((b) => b.read()).filter((a) => a.name || a.task);
      if (!manual.hidden && !agents.length) { err.textContent = "En az bir ajan ekle ya da planı Alice'e bırak"; return; }
      ok.disabled = true;
      const r = await api("/api/project/create", { goal: g2, agents, cwd: cwd.value.trim(), lead_model: lm.value.trim() || "brain",
        lead_thinking: lt.value, review: rv.checked, council: kc.checked, check: ck.value.trim() });
      ok.disabled = false;
      if (!r.ok) { err.textContent = r.error || "Başlatılamadı"; return; }
      closeDialog();
      await loadProjects();
      openProject(r.id);
    });
    row.append(no, ok);
    d.appendChild(row);
    setTimeout(() => goal.focus(), 0);
    // Şablonlarım: kayıtlı projeden hedef, ayarlar ve ajanlar tek tıkla gelir
    const fill = (t) => {
      goal.value = t.goal || "";
      lm.value = t.lead_model || "brain"; lt.value = t.lead_thinking || "default";
      rv.checked = t.review !== false; kc.checked = t.council !== false; ck.value = t.check || "";
      list.replaceChildren();
      (t.agents || []).forEach((a, i) => list.appendChild(agentCard(a, i + 1)));
      setMode((t.agents || []).length > 0);
      opts.open = true;
    };
    api("/api/project/templates").then((rows) => {
      if (!Array.isArray(rows) || !rows.length) return;
      mine.appendChild(el("span", "np-mine-l", "Şablonlarım:"));
      for (const t of rows) {
        const c = el("span", "chip-btn tpl");
        const b = el("button", "", t.name);
        b.title = `${(t.agents || []).length} ajan · ${(t.goal || "").slice(0, 120)}`;
        b.addEventListener("click", () => fill(t));
        const x = el("button", "x", "×"); x.title = "Şablonu sil";
        x.addEventListener("click", async () => { await api("/api/project/template/delete", { name: t.name }); c.remove(); });
        c.append(b, x);
        mine.appendChild(c);
      }
    });
  });
}
$("#newProject").addEventListener("click", () => projectDialog(null));

/* ==================================================================== menüler */
function openMenu(anchor, build, align) {
  const m = $("#menu");
  m.replaceChildren();
  build(m);
  m.hidden = false;
  const r = anchor.getBoundingClientRect();
  const mh = m.offsetHeight, mw = m.offsetWidth;
  let top = r.top - mh - 6;
  if (top < 8) top = r.bottom + 6;
  let left = align === "right" ? r.right - mw : r.left;
  left = Math.max(8, Math.min(left, window.innerWidth - mw - 8));
  m.style.top = top + "px";
  m.style.left = left + "px";
}
function closeMenu() { $("#menu").hidden = true; }
function menuItem(m, icon, title, sub, onClick, checked) {
  const b = el("button", "mi");
  if (icon) b.appendChild(svg(icon));
  const t = el("span", "mt");
  t.appendChild(el("b", "", title));
  if (sub) t.appendChild(el("small", "", sub));
  b.appendChild(t);
  if (checked) b.appendChild(svg(IC.check, "check"));
  b.addEventListener("click", () => { closeMenu(); onClick(); });
  m.appendChild(b);
}
document.addEventListener("mousedown", (e) => {
  if (!$("#menu").hidden && !e.target.closest("#menu") && !e.target.closest(".c-btn") && !e.target.closest(".user-chip")) closeMenu();
});

function modeMenu() {
  openMenu($("#modeBtn"), (m) => {
    m.appendChild(el("div", "menu-title", "İzin modu"));
    for (const [k, [label, sub, icon]] of Object.entries(MODES)) {
      menuItem(m, icon, label, sub, () => setMode(k), S.status.mode === k);
    }
  });
}
async function setMode(mode) { applyStatus(await api("/api/mode", { mode })); }
function cycleMode() {
  const keys = Object.keys(MODES);
  setMode(keys[(keys.indexOf(S.status.mode) + 1) % keys.length]);
}

const THINK = {
  off: ["düşünmez", "En hızlı: hemen cevap verir. Basit işler için"],
  low: ["az düşünür", "Kısa düşünür, sonra yapar (önerilen)"],
  medium: ["orta düşünür", "Biraz daha dikkatli, biraz daha yavaş"],
  high: ["çok düşünür", "Zor işler için; basit işte gereksiz uzun sürer"],
  default: ["model karar verir", "Modelin kendi ayarı"],
};
async function setThinking(level) {
  applyStatus(await api("/api/thinking", { level }));
  toast("Düşünme: " + THINK[level][0]);
}

function thinkMenu() {
  openMenu($("#thinkBtn"), (m) => {
    m.appendChild(el("div", "menu-title", "Düşünme"));
    for (const [k, [label, sub]] of Object.entries(THINK)) {
      menuItem(m, k === "off" ? IC.bolt : IC.brain, label[0].toUpperCase() + label.slice(1), sub,
        () => setThinking(k), S.status.thinking === k);
    }
  }, "right");
}
function modelMenu() {
  openMenu($("#modelBtn"), (m) => {
    m.appendChild(el("div", "menu-title", "Ana model"));
    menuItem(m, IC.spark, "Otomatik", "Zincirdeki ilk uygun model, limit dolunca sıradakine geçer",
      () => pinModel("reset"), !S.status.brain_pinned);
    for (const ref of S.status.brain_chain || []) {
      if (S.status.brain_pinned && S.status.brain_chain.length === 1) break;
      menuItem(m, null, shortModel(ref), ref, () => pinModel(ref), false);
    }
    if (S.status.brain_pinned) menuItem(m, null, shortModel(S.status.brain_chain[0]), S.status.brain_chain[0], () => {}, true);
    m.appendChild(el("div", "menu-sep"));
    menuItem(m, IC.list, "Tüm modeller ve durumları", "", showModels);
  }, "right");
}
async function pinModel(ref) {
  await api("/api/model", { role: "brain", ref });
  await refreshStatus();
  toast(ref === "reset" ? "Otomatik model seçimi" : "Model sabitlendi: " + shortModel(ref));
}

/* =================================================================== dialoglar */
let onDialogClose = null;
function showDialog(build) {
  if (onDialogClose) closeDialog();
  const d = $("#dialog");
  d.className = "dialog";
  d.replaceChildren();
  build(d);
  $("#overlay").hidden = false;
}
// Esc ya da dışarı tıklamak da "Vazgeç" sayılır; onay bekleyen kod askıda kalmasın.
function closeDialog() {
  $("#overlay").hidden = true;
  const f = onDialogClose;
  onDialogClose = null;
  if (f) f();
}
$("#overlay").addEventListener("mousedown", (e) => { if (e.target.id === "overlay") closeDialog(); });
function dialogClose(d, label) {
  const row = el("div", "row");
  const b = el("button", "btn primary", label || "Kapat");
  b.addEventListener("click", closeDialog);
  row.appendChild(b);
  d.appendChild(row);
}
function confirmDialog(title, text, okLabel) {
  return new Promise((resolve) => {
    showDialog((d) => {
      onDialogClose = () => resolve(false);
      d.append(el("h2", "", title), el("p", "", text));
      const row = el("div", "row");
      const no = el("button", "btn", "Vazgeç");
      const yes = el("button", "btn accent", okLabel || "Tamam");
      no.addEventListener("click", closeDialog);
      yes.addEventListener("click", () => { onDialogClose = null; closeDialog(); resolve(true); });
      row.append(no, yes);
      d.appendChild(row);
      setTimeout(() => yes.focus(), 0);
    });
  });
}
function showImage(src) {
  showDialog((d) => { const i = el("img", "full"); i.src = src; d.appendChild(i); dialogClose(d); });
}
function askCwd() {
  showDialog((d) => {
    d.append(el("h2", "", "Çalışma klasörü"), el("p", "", "Komutlar ve göreli dosya yolları bu klasöre göre çalışır. Projenin klasörünü seç."));
    const inp = el("input"); inp.type = "text"; inp.value = S.status.cwd || "";
    const err = el("div", "note err");
    const row = el("div", "row");
    const no = el("button", "btn", "Vazgeç");
    const ok = el("button", "btn accent", "Değiştir");
    const submit = async () => {
      const r = await api("/api/cwd", { path: inp.value });
      if (!r.ok) { err.textContent = r.error; return; }
      applyStatus(r); closeDialog(); toast("Klasör değişti");
    };
    no.addEventListener("click", closeDialog);
    ok.addEventListener("click", submit);
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
    row.append(no, ok);
    d.append(inp, err, row, githubPicker(err));
    setTimeout(() => { inp.focus(); inp.select(); }, 0);
  });
}
// GitHub'daki depolar: tıklanınca ~/GitHub altına klonlanır (varsa güncellenir) ve çalışma klasörü olur.
function githubPicker(err) {
  const box = el("div", "gh-pick");
  const q = el("input"); q.type = "text"; q.placeholder = "GitHub deposu ara…";
  const list = el("div", "gh-list");
  const note = el("div", "note", "Depolar yükleniyor…");
  box.append(el("h3", "", "GitHub'dan aç"), q, note, list);
  let repos = [];
  const draw = () => {
    const f = q.value.trim().toLocaleLowerCase("tr");
    const rows = repos.filter((r) => !f || (r.full_name + " " + r.description).toLocaleLowerCase("tr").includes(f));
    list.replaceChildren(...rows.slice(0, 50).map((r) => {
      const b = el("button", "list-row gh-row");
      const l = el("span", "l", r.full_name);
      if (r.description) l.title = r.description;
      b.append(l, el("span", "r" + (r.local ? " ok" : ""), r.local ? "bilgisayarda" : r.private ? "gizli" : ""));
      b.addEventListener("click", async () => {
        list.querySelectorAll("button").forEach((x) => (x.disabled = true));
        note.textContent = r.local ? `${r.full_name} güncelleniyor…` : `${r.full_name} klonlanıyor…`;
        const res = await api("/api/github/open", { repo: r.full_name });
        if (!res.ok) {
          err.textContent = res.error; note.textContent = "";
          list.querySelectorAll("button").forEach((x) => (x.disabled = false));
          return;
        }
        applyStatus(res); closeDialog(); toast(`${r.full_name} ${res.note}`);
      });
      return b;
    }));
    note.textContent = rows.length ? "" : (repos.length ? "Eşleşen depo yok" : note.textContent);
  };
  q.addEventListener("input", draw);
  api("/api/github/repos").then((r) => {
    if (!r.ok) { note.textContent = r.error; note.classList.add("warn"); return; }
    repos = r.repos || [];
    note.textContent = repos.length ? "" : "Depo yok";
    draw();
  });
  return box;
}
const showModels = () => showSettings("models");
const showTools = () => showSettings("tools");
const showMemory = () => showSettings("memory");
const showUsage = () => showSettings("usage");
const showHelp = () => showSettings("keys");

/* ================================================================ plan çubuğu */
function setTodo(text) {
  S.todo = text || "";
  const items = parseTodo(S.todo);
  const done = items.filter((i) => i.state === "done").length;
  const bar = $("#planbar");
  // Plan çubuğu sadece iş sürerken görünür; bitince plan yan panelde kalır.
  const open = S.busy && items.length && done < items.length;
  bar.hidden = !open;
  if (open) {
    const cur = items.find((i) => i.state === "doing") || items.find((i) => i.state !== "done");
    const next = items.find((i) => i !== cur && i.state === "");
    $("#pbCount").textContent = `${done}/${items.length}`;
    $("#pbText").textContent = cur ? cur.text : "";
    $("#pbNext").textContent = next ? "Sonra: " + next.text : "";
    const arc = $("#pbArc");
    const len = 2 * Math.PI * 15;
    arc.style.strokeDasharray = `${(len * done) / items.length} ${len}`;
  }
  if (PREFS.panel && PREFS.tab === "plan") renderPanel();
}
$("#planbar").addEventListener("click", () => openPanel("plan"));

/* ============================================================ değişiklikler */
let changesSeq = 0;
async function refreshChanges() {
  const seq = ++changesSeq;
  const rows = await api("/api/changes");
  if (seq !== changesSeq || !Array.isArray(rows)) return;
  S.changes = rows;
  drawChangesChip();
  if (PREFS.panel && PREFS.tab === "changes") renderPanel();
}
function changeTotals() {
  let a = 0, d = 0;
  for (const c of S.changes) { a += c.added; d += c.removed; }
  return { a, d, n: S.changes.length };
}
function drawChangesChip() {
  const { a, d, n } = changeTotals();
  $("#changesBtn").hidden = !n;
  $("#chA").textContent = `+${a}`;
  $("#chD").textContent = `−${d}`;
  $("#changesBtn").title = `Bu oturumda ${n} dosya değişti`;
  $("#chCount").textContent = n ? String(n) : "";
}
async function revertFile(c) {
  const ok = await confirmDialog("Değişiklik geri alınsın mı?",
    `${c.path} bu oturumdaki ilk haline döner` + (c.status === "eklendi" ? " (ALICE oluşturduğu için silinir)." : "."), "Geri al");
  if (!ok) return;
  const r = await api("/api/changes/revert", { path: c.abs });
  if (!r.ok) return toast(r.error || "Geri alınamadı");
  S.changes = r.changes || [];
  drawChangesChip();
  renderPanel();
  toast(`${c.path} geri alındı`);
}

/* ================================================================= yan panel */
function openPanel(tab) {
  if (tab) PREFS.tab = tab;
  PREFS.panel = true;
  savePrefs();
  app.classList.add("panel-open");
  renderPanel();
  if (PREFS.tab === "changes") refreshChanges();
}
function closePanel() { PREFS.panel = false; savePrefs(); app.classList.remove("panel-open"); }
function togglePanel() { if (PREFS.panel) closePanel(); else openPanel(); }
$("#panelBtn").addEventListener("click", togglePanel);
$("#panelClose").addEventListener("click", closePanel);
$("#changesBtn").addEventListener("click", () => openPanel("changes"));
$$("#panel .tab").forEach((b) => b.addEventListener("click", () => openPanel(b.dataset.tab)));

function emptyState(icon, title, text) {
  const e = el("div", "p-empty");
  e.append(svg(icon), el("b", "", title), el("p", "", text));
  return e;
}
function renderPanel() {
  if (!PREFS.panel) return;
  $$("#panel .tab").forEach((b) => b.classList.toggle("on", b.dataset.tab === PREFS.tab));
  const body = $("#panelBody");
  const keep = body.scrollTop;
  body.replaceChildren();
  if (PREFS.tab === "plan") {
    if (!parseTodo(S.todo).length) {
      body.appendChild(emptyState(IC.list, "Henüz plan yok",
        "ALICE çok adımlı bir işe başladığında yapılacakları burada listeler ve ilerledikçe işaretler."));
    } else body.appendChild(renderTodo(S.todo, true));
  } else if (PREFS.tab === "changes") {
    if (!S.changes.length) {
      body.appendChild(emptyState(IC.diff, "Değişiklik yok",
        "ALICE bu oturumda dosya araçlarıyla bir dosyayı değiştirdiğinde fark burada görünür. Dosya bazında geri alabilirsin."));
    } else {
      const { a, d, n } = changeTotals();
      const head = el("div", "ch-sum");
      head.append(el("b", "", `${n} dosya değişti`), el("span", "a", `+${a}`), el("span", "d", `−${d}`));
      const fold = el("button", "act", "Tümünü daralt");
      fold.addEventListener("click", () => $$(".ch-file", body).forEach((f) => f.classList.remove("open")));
      head.append(el("span", "grow"), fold);
      body.appendChild(head);
      S.changes.forEach((c, i) => {
        const f = el("div", "ch-file" + (i < 3 ? " open" : ""));
        const row = el("div", "ch-row");
        row.append(svg(IC.chev, "tchev"), el("span", "ch-st " + c.status, c.status[0].toUpperCase()));
        const name = el("span", "ch-name");
        const slash = c.path.lastIndexOf("/");
        if (slash >= 0) name.appendChild(el("small", "", c.path.slice(0, slash + 1)));
        name.appendChild(document.createTextNode(c.path.slice(slash + 1)));
        row.append(name, el("span", "a", `+${c.added}`), el("span", "d", `−${c.removed}`));
        const rv = el("button", "act ch-revert");
        rv.title = "Bu dosyayı geri al";
        rv.append(svg(IC.rewind), document.createTextNode("Geri al"));
        rv.addEventListener("click", (e) => { e.stopPropagation(); revertFile(c); });
        row.appendChild(rv);
        row.addEventListener("click", () => f.classList.toggle("open"));
        const inner = el("div", "ch-body");
        if (c.binary) inner.appendChild(el("div", "note", "İkili dosya, fark gösterilemiyor"));
        else if (c.diff) inner.appendChild(renderDiff(c.diff, c.path).box);
        f.append(row, inner);
        body.appendChild(f);
      });
    }
  } else {
    const st = S.status;
    const win = st.ctx_window || st.ctx_limit || 0;
    const pct = win ? Math.min(100, (100 * st.ctx_tokens) / win) : 0;
    const ctx = el("div", "ctx-card");
    const big = el("div", "ctx-big");
    big.append(el("b", "", `${Math.round(pct)}%`), el("span", "", `${fmtTok(st.ctx_tokens || 0)} / ${fmtTok(win)} token`));
    const meter = el("i", "ctx-meter" + (pct > 75 ? " hot" : ""));
    const fill = el("i");
    fill.style.width = pct + "%";
    meter.appendChild(fill);
    const cp = el("button", "btn", "Şimdi özetle");
    cp.addEventListener("click", () => runCommand("/compact"));
    ctx.append(el("div", "sec", "Bağlam doluluğu"), big, meter,
      el("p", "", `Modelin penceresi ${fmtTok(win)} token. ${fmtTok(st.ctx_limit || 0)} token'a gelince eski konuşma otomatik özetlenir. ` +
        "Sayı, sağlayıcının son istekte saydığı token'dır (araç tanımları dahil)."), cp);
    body.appendChild(ctx);
    const kv = el("dl", "kv");
    const mins = Math.floor((Date.now() - S.started) / 60000);
    const { a, d, n } = changeTotals();
    for (const [k, v] of [["Model", st.model || "yok"], ["Düşünme", (THINK[st.thinking] || [st.thinking || "?"])[0]],
      ["İzin modu", (MODES[st.mode] || [st.mode])[0]], ["Karar", st.decider || "-"], ["Klasör", st.cwd || ""],
      ["Git dalı", st.branch || "-"], ["Oturum", st.session || ""], ["Açık kalma", mins < 1 ? "1 dk'dan az" : `${mins} dk`],
      ["Bugün", `${st.today_requests || 0} istek`], ["Değişiklik", n ? `${n} dosya +${a} −${d}` : "yok"]]) {
      kv.append(el("dt", "", k), el("dd", "", v));
    }
    const sec = el("div", "sec", "Oturum");
    body.append(sec, kv);
  }
  body.scrollTop = keep;
}

/* ============================================================ komut paleti */
function paletteItems() {
  const items = [];
  const act = (group, icon, label, hint, run, key) => items.push({ group, icon, label, hint, run, key });
  act("Eylemler", IC.chat, "Yeni sohbet", "", newChat, "Ctrl+Shift+O");
  act("Eylemler", IC.panel, PREFS.panel ? "Yan paneli kapat" : "Yan paneli aç", "", togglePanel, "Ctrl+J");
  act("Eylemler", IC.list, "Planı göster", "Yapılacaklar listesi", () => openPanel("plan"));
  act("Eylemler", IC.diff, "Değişiklikleri göster", "Bu oturumda değişen dosyalar", () => openPanel("changes"));
  act("Eylemler", IC.gauge, "Bağlam ve oturum bilgisi", "", () => openPanel("context"));
  act("Eylemler", IC.expand, PREFS.verbose ? "Araç ayrıntılarını gizle" : "Araç ayrıntılarını göster", "Tüm araç çıktılarını aç/kapat", toggleVerbose, "Ctrl+O");
  act("Eylemler", IC.folder, "Çalışma klasörünü değiştir", S.status.cwd || "", askCwd);
  act("Eylemler", IC.spark, "Konuşmayı özetle", "/compact", () => runCommand("/compact"));
  act("Eylemler", IC.rewind, "Son mesaja geri sar", "/rewind", () => runCommand("/rewind"));
  act("Eylemler", IC.gear, "Ayarlar", "", () => showSettings("general"), "Ctrl+,");
  act("Eylemler", IC.keyboard, "Klavye kısayolları", "", () => showSettings("keys"));
  for (const [k, label] of [["system", "Sistem"], ["light", "Açık"], ["dark", "Koyu"]]) {
    act("Tema", k === "dark" ? IC.moon : IC.sun, `Tema: ${label}`, "", () => setPref("theme", k));
  }
  for (const [k, [label, sub, icon]] of Object.entries(MODES)) act("İzin modu", icon, `Mod: ${label}`, sub, () => setMode(k));
  for (const [k, [label, sub]] of Object.entries(THINK)) act("Düşünme", IC.brain, `Düşünme: ${label}`, sub, () => setThinking(k));
  act("Model", IC.spark, "Model: Otomatik", "Zincirdeki ilk uygun model", () => pinModel("reset"));
  for (const ref of S.status.brain_chain || []) act("Model", IC.spark, `Model: ${shortModel(ref)}`, ref, () => pinModel(ref));
  for (const c of S.commands) {
    act("Komutlar", IC.term, "/" + c.name, c.description, () => {
      input.value = "/" + c.name + " "; autosize(); input.focus();
    });
  }
  for (const s of S.sessions || []) act("Sohbetler", IC.chat, s.title || "(boş)", relTime(s.updated), () => resume(s.id));
  return items;
}
// Sırayla geçen harf eşleşmesi; ardışık harfler ve kelime başları daha yüksek puan alır.
function fuzzy(q, text) {
  if (!q) return 1;
  const t = text.toLocaleLowerCase("tr");
  const idx = t.indexOf(q);
  if (idx >= 0) return 100 - idx + (idx === 0 || /\s|\/|:/.test(t[idx - 1]) ? 50 : 0);
  let score = 0, j = 0, run = 0;
  for (let i = 0; i < t.length && j < q.length; i++) {
    if (t[i] === q[j]) { j++; run++; score += run * 2 + (i === 0 || /\s|\/|:/.test(t[i - 1]) ? 5 : 0); }
    else run = 0;
  }
  return j === q.length ? score : 0;
}
const PAL = { items: [], shown: [], sel: 0 };
function openPalette(prefill) {
  closeMenu();
  PAL.items = paletteItems();
  $("#palette").hidden = false;
  const inp = $("#palInput");
  inp.value = prefill || "";
  drawPalette();
  setTimeout(() => inp.focus(), 0);
}
function closePalette() { $("#palette").hidden = true; }
function drawPalette() {
  const q = $("#palInput").value.trim().toLocaleLowerCase("tr");
  const scored = PAL.items.map((it) => ({ it, sc: Math.max(fuzzy(q, it.label), fuzzy(q, it.group + " " + it.label) * 0.6, fuzzy(q, it.hint || "") * 0.4) }))
    .filter((x) => x.sc > 0);
  if (q) scored.sort((x, y) => y.sc - x.sc);
  PAL.shown = scored.slice(0, 60).map((x) => x.it);
  PAL.sel = Math.min(PAL.sel, Math.max(0, PAL.shown.length - 1));
  const box = $("#palList");
  box.replaceChildren();
  if (!PAL.shown.length) { box.appendChild(el("div", "pal-empty", "Sonuç yok")); return; }
  let group = null;
  PAL.shown.forEach((it, i) => {
    if (!q && it.group !== group) { group = it.group; box.appendChild(el("div", "pal-group", group)); }
    const row = el("div", "pal-item" + (i === PAL.sel ? " sel" : ""));
    row.appendChild(svg(it.icon));
    const t = el("span", "pal-t");
    t.appendChild(el("b", "", it.label));
    if (it.hint) t.appendChild(el("small", "", it.hint));
    row.appendChild(t);
    if (q) row.appendChild(el("span", "pal-g", it.group));
    if (it.key) row.appendChild(el("kbd", "", it.key));
    row.addEventListener("mousemove", () => { if (PAL.sel !== i) { PAL.sel = i; markPalSel(); } });
    row.addEventListener("click", () => runPalette(i));
    box.appendChild(row);
  });
  markPalSel();
}
function markPalSel() {
  $$("#palList .pal-item").forEach((r, i) => r.classList.toggle("sel", i === PAL.sel));
  $("#palList .pal-item.sel")?.scrollIntoView({ block: "nearest" });
}
function runPalette(i) {
  const it = PAL.shown[i];
  closePalette();
  if (it) it.run();
}
$("#palInput").addEventListener("input", () => { PAL.sel = 0; drawPalette(); });
$("#palInput").addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") { e.preventDefault(); PAL.sel = Math.min(PAL.shown.length - 1, PAL.sel + 1); markPalSel(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); PAL.sel = Math.max(0, PAL.sel - 1); markPalSel(); }
  else if (e.key === "Enter") { e.preventDefault(); runPalette(PAL.sel); }
  else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closePalette(); }
});
$("#palette").addEventListener("mousedown", (e) => { if (e.target.id === "palette") closePalette(); });
$("#paletteBtn").addEventListener("click", () => openPalette());

/* ================================================================== ayarlar */
function setPref(k, v) {
  PREFS[k] = v;
  savePrefs();
  applyPrefs();
}
function toggleVerbose() {
  setPref("verbose", !PREFS.verbose);
  $$(".tool").forEach((t) => t.classList.toggle("open", PREFS.verbose || !!t.querySelector(".diff")));
  $$(".tool-group").forEach((g) => g.classList.toggle("open", PREFS.verbose));
  toast(PREFS.verbose ? "Araç ayrıntıları açık" : "Araç ayrıntıları kapalı");
}
function segmented(options, value, onPick) {
  const seg = el("div", "seg");
  for (const [k, label] of options) {
    const b = el("button", k === value ? "on" : "", label);
    b.addEventListener("click", () => { onPick(k); $$("button", seg).forEach((x) => x.classList.toggle("on", x === b)); });
    seg.appendChild(b);
  }
  return seg;
}
function setRow(title, sub, control) {
  const r = el("div", "set-row");
  const t = el("div", "set-t");
  t.appendChild(el("b", "", title));
  if (sub) t.appendChild(el("small", "", sub));
  r.append(t, control);
  return r;
}
const SETTINGS_TABS = [
  ["general", "Genel", IC.gear], ["models", "Modeller", IC.spark], ["tools", "Araçlar", IC.bolt],
  ["memory", "Hafıza", IC.brain], ["usage", "Kullanım", IC.gauge], ["keys", "Kısayollar", IC.keyboard],
];
async function showSettings(tab) {
  tab = tab || "general";
  const data = {
    models: tab === "models" ? await api("/api/models") : null,
    tools: tab === "tools" ? await api("/api/tools") : null,
    memory: tab === "memory" ? await api("/api/memory") : null,
    usage: tab === "usage" ? await api("/api/usage") : null,
  };
  showDialog((d) => {
    d.classList.add("settings");
    const nav = el("nav", "set-nav");
    nav.appendChild(el("div", "set-h", "Ayarlar"));
    for (const [k, label, icon] of SETTINGS_TABS) {
      const b = el("button", k === tab ? "on" : "");
      b.append(svg(icon), document.createTextNode(label));
      b.addEventListener("click", () => showSettings(k));
      nav.appendChild(b);
    }
    const pane = el("div", "set-pane");
    const x = el("button", "icon-btn set-x");
    x.title = "Kapat (Esc)";
    x.appendChild(svg(IC.x));
    x.addEventListener("click", closeDialog);
    pane.appendChild(x);
    if (tab === "general") {
      pane.append(el("h2", "", "Genel"));
      pane.append(
        setRow("Tema", "Sistem seçilirse Windows'un açık/koyu ayarını izler",
          segmented([["system", "Sistem"], ["light", "Açık"], ["dark", "Koyu"]], PREFS.theme, (v) => setPref("theme", v))),
        setRow("Yazı boyutu", "Sohbet metninin boyutu",
          segmented([["small", "Küçük"], ["normal", "Normal"], ["large", "Büyük"]], PREFS.size, (v) => setPref("size", v))),
        setRow("Araç ayrıntıları", "Komut çıktıları ve okunan dosyalar açık gelsin (Ctrl+O)",
          segmented([["0", "Kapalı"], ["1", "Açık"]], PREFS.verbose ? "1" : "0", (v) => { if ((v === "1") !== PREFS.verbose) toggleVerbose(); })),
        setRow("Yan panel", "Plan, değişiklikler ve bağlam (Ctrl+J)",
          segmented([["0", "Kapalı"], ["1", "Açık"]], PREFS.panel ? "1" : "0", (v) => (v === "1" ? openPanel() : closePanel()))),
        setRow("İzin modu", (MODES[S.status.mode] || ["", ""])[1],
          segmented(Object.entries(MODES).map(([k, [l]]) => [k, l]), S.status.mode, (v) => setMode(v))),
        setRow("Çalışma klasörü", S.status.cwd || "", (() => { const b = el("button", "btn", "Değiştir"); b.addEventListener("click", askCwd); return b; })()),
      );
    } else if (tab === "models") {
      pane.append(el("h2", "", "Modeller"),
        el("p", "", "Her rol soldan sağa denenir. Limite takılan ya da hata veren model atlanır, kısa süre sonra tekrar denenir."));
      pane.appendChild(setRow("Düşünme", (THINK[S.status.thinking] || ["", ""])[1],
        segmented(Object.entries(THINK).map(([k, [l]]) => [k, l]), S.status.thinking, (v) => setThinking(v))));
      const names = { brain: "Ana model", fast: "Hızlı işler", vision: "Görsel" };
      for (const [role, rows] of Object.entries(data.models || {})) {
        pane.appendChild(el("div", "sec", names[role] || role));
        for (const r of rows) {
          const row = el("div", "list-row");
          row.append(el("span", "l", r.ref), el("span", "r" + (r.status === "hazır" ? " ok" : ""), r.status));
          if (role === "brain") {
            const pin = el("button", "act", "Sabitle");
            pin.addEventListener("click", () => pinModel(r.ref));
            row.appendChild(pin);
          }
          pane.appendChild(row);
        }
      }
    } else if (tab === "tools") {
      const tools = Array.isArray(data.tools) ? data.tools : [];
      pane.append(el("h2", "", "Araçlar"), el("p", "", `${tools.length} araç. MCP sunucularını config.toml'daki [mcp.servers] altına ekleyebilirsin.`));
      const f = el("input"); f.type = "text"; f.placeholder = "Araç ara"; f.className = "set-filter";
      const listBox = el("div");
      const draw = () => {
        listBox.replaceChildren();
        const q = f.value.toLowerCase();
        for (const t of tools.filter((t) => !q || (t.name + t.description).toLowerCase().includes(q))) {
          const row = el("div", "list-row");
          const [, icon] = verbFor(t.name);
          const l = el("span", "l");
          l.append(el("b", "", t.name), el("small", "", t.description));
          row.append(svg(icon), l, el("span", "risk " + t.risk, t.risk));
          listBox.appendChild(row);
        }
      };
      f.addEventListener("input", draw);
      draw();
      pane.append(f, listBox);
      setTimeout(() => f.focus(), 0);
    } else if (tab === "memory") {
      pane.append(el("h2", "", "Hafıza"), el("p", "", "ALICE'in oturumlar arası kaydettiği notlar (~/.alice/memory.md)."));
      const box = el("div", "prose mem");
      setMd(box, (data.memory && data.memory.text) || "_(henüz bir şey kaydedilmedi)_");
      pane.appendChild(box);
    } else if (tab === "usage") {
      pane.append(el("h2", "", "Bugünkü kullanım"));
      const entries = Object.entries(data.usage || {});
      if (!entries.length) pane.appendChild(el("p", "", "Bugün henüz istek yok."));
      const max = Math.max(1, ...entries.map(([, r]) => r.requests));
      for (const [ref, r] of entries) {
        const row = el("div", "use-row");
        const bar = el("i", "use-bar");
        const fill = el("i");
        fill.style.width = (100 * r.requests) / max + "%";
        bar.appendChild(fill);
        row.append(el("span", "l", ref), bar,
          el("span", "r", `${r.requests} istek · ${r.errors} hata · ${(r.prompt_tokens / 1000).toFixed(1)}k girdi · ${(r.completion_tokens / 1000).toFixed(1)}k çıktı`));
        pane.appendChild(row);
      }
    } else {
      pane.append(el("h2", "", "Kısayollar ve komutlar"));
      for (const [k, v] of SHORTCUTS) {
        const row = el("div", "list-row");
        const keys = el("span", "keys");
        for (const part of k.split(" / ")) keys.appendChild(el("kbd", "", part));
        row.append(el("span", "l plain", v), keys);
        pane.appendChild(row);
      }
      pane.appendChild(el("div", "sec", "Slash komutları"));
      for (const c of S.commands) {
        const row = el("div", "list-row");
        row.append(el("span", "l", "/" + c.name), el("span", "r", c.description + (c.kind === "custom" ? " (özel)" : "")));
        pane.appendChild(row);
      }
    }
    d.append(nav, pane);
  });
}
const SHORTCUTS = [
  ["Ctrl+K", "Komut paleti"], ["Ctrl+J", "Yan panel (plan, değişiklikler, bağlam)"], ["Ctrl+O", "Araç ayrıntılarını aç/kapat"],
  ["Ctrl+,", "Ayarlar"], ["Enter", "Gönder (çalışırken sıraya ekler)"], ["Shift+Enter", "Yeni satır"],
  ["↑ / ↓", "Önceki mesajları geri getir (kutu boşken)"], ["Esc", "Durdur / menüyü kapat"],
  ["1 / 2 / 3", "İzin kartını cevapla: izin ver / hep izin ver / reddet"], ["Shift+Tab", "İzin modunu değiştir"],
  ["Ctrl+B", "Kenar çubuğu"], ["Ctrl+Shift+O", "Yeni sohbet"], ["Ctrl+V", "Görsel yapıştır"],
];

/* =========================================================== slash komutları */
async function runCommand(text) {
  const [cmd, ...rest] = text.slice(1).split(" ");
  const arg = rest.join(" ").trim();
  switch (cmd.toLowerCase()) {
    case "help": showHelp(); return true;
    case "clear": case "new": newChat(); return true;
    case "compact": {
      if (S.busy) { toast("Önce mevcut işi durdur"); return true; }
      toast("Özetleniyor…");
      const r = await api("/api/compact", {});
      applyStatus(r); toast(r.ok ? "Context özetlendi" : "Özetlenecek bir şey yok");
      loadHistory();
      return true;
    }
    case "mode":
      if (MODES[arg]) setMode(arg); else modeMenu();
      return true;
    case "plan": setMode("plan"); toast("Plan modu: sadece okur, plan yazar"); return true;
    case "model":
      if (arg) pinModel(arg.includes(":") ? arg : "reset"); else modelMenu();
      return true;
    case "think":
      if (THINK[arg]) setThinking(arg); else thinkMenu();
      return true;
    case "rewind": {
      const users = $$(".turn-user");
      const last = users[users.length - 1];
      if (!last) { toast("Geri sarılacak mesaj yok"); return true; }
      last.querySelector(".act[title^='Bu mesajdan']")?.click();
      return true;
    }
    case "usage": showUsage(); return true;
    case "cwd":
      if (arg) { const r = await api("/api/cwd", { path: arg }); r.ok ? applyStatus(r) : toast(r.error); }
      else askCwd();
      return true;
    case "tools": case "mcp": showTools(); return true;
    case "memory": showMemory(); return true;
    default: return false; // /init ve özel komutlar sunucuya gider
  }
}

/* ===================================================================== popup */
function caretToken() {
  const pos = input.selectionStart;
  const before = input.value.slice(0, pos);
  if (/^\/[\w-]*$/.test(before)) return { kind: "cmd", q: before.slice(1), start: 0 };
  const m = before.match(/(^|\s)@([^\s@]*)$/);
  if (m) return { kind: "file", q: m[2], start: pos - m[2].length - 1 };
  return null;
}
let popupSeq = 0;
async function updatePopup() {
  const tok = caretToken();
  if (!tok) return hidePopup();
  const seq = ++popupSeq;
  let items;
  if (tok.kind === "cmd") {
    items = S.commands.filter((c) => c.name.startsWith(tok.q.toLowerCase()))
      .map((c) => ({ label: "/" + c.name, desc: c.description, tag: c.kind === "custom" ? "özel" : "", value: "/" + c.name + " " }));
  } else {
    const files = await api("/api/files?q=" + encodeURIComponent(tok.q));
    if (seq !== popupSeq) return;
    items = (Array.isArray(files) ? files : []).map((f) => ({ label: f, desc: "", tag: f.endsWith("/") ? "klasör" : "", value: "@" + f + " " }));
  }
  if (!items.length) return hidePopup();
  S.popup = { ...tok, items, sel: 0 };
  drawPopup();
}
function drawPopup() {
  const p = $("#popup");
  p.replaceChildren();
  S.popup.items.forEach((it, i) => {
    const row = el("div", "pi" + (i === S.popup.sel ? " sel" : ""));
    row.append(el("b", "", it.label));
    if (it.desc) row.appendChild(el("span", "", it.desc));
    if (it.tag) row.appendChild(el("span", "tag", it.tag));
    row.addEventListener("mousedown", (e) => { e.preventDefault(); choosePopup(i); });
    p.appendChild(row);
  });
  p.hidden = false;
  p.querySelector(".sel")?.scrollIntoView({ block: "nearest" });
}
function hidePopup() { S.popup = null; $("#popup").hidden = true; }
function choosePopup(i) {
  const it = S.popup.items[i];
  const pos = input.selectionStart;
  const v = input.value;
  input.value = v.slice(0, S.popup.start) + it.value + v.slice(pos);
  const caret = S.popup.start + it.value.length;
  input.setSelectionRange(caret, caret);
  hidePopup();
  autosize();
  input.focus();
  if (it.value.endsWith("/ ")) { // klasör: içine girmeye devam et
    input.value = input.value.slice(0, caret - 1) + input.value.slice(caret);
    input.setSelectionRange(caret - 1, caret - 1);
    updatePopup();
  }
}

/* ================================================================== composer */
function autosize() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 300) + "px";
  updateSend();
}
function updateSend() {
  $("#sendBtn").disabled = !S.busy && !input.value.trim() && !S.attachments.length;
}
function drawAttachments() {
  const box = $("#attachments");
  box.replaceChildren();
  S.attachments.forEach((b64, i) => {
    const a = el("div", "att");
    const img = el("img"); img.src = "data:image/png;base64," + b64;
    const x = el("button"); x.title = "Kaldır"; x.appendChild(svg(IC.x));
    x.addEventListener("click", () => { S.attachments.splice(i, 1); drawAttachments(); });
    a.append(img, x);
    box.appendChild(a);
  });
  box.classList.toggle("has", S.attachments.length > 0);
  updateSend();
}
function addImageFile(file) {
  if (!file || !file.type.startsWith("image/")) return;
  const reader = new FileReader();
  reader.onload = () => {
    const img = new Image();
    img.onload = () => {
      // PNG'ye çevir ve 1600 px'e küçült: her sağlayıcı aynı formatı alsın, token israf olmasın.
      const scale = Math.min(1, 1600 / Math.max(img.width, img.height));
      const c = document.createElement("canvas");
      c.width = Math.round(img.width * scale); c.height = Math.round(img.height * scale);
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      S.attachments.push(c.toDataURL("image/png").split(",")[1]);
      drawAttachments();
    };
    img.src = reader.result;
  };
  reader.readAsDataURL(file);
}

async function submit(textArg) {
  const text = (textArg ?? input.value).trim();
  const images = textArg === undefined ? S.attachments.slice() : [];
  if (!text && !images.length) return;
  hidePopup();
  if (text.startsWith("/") && !images.length && (await runCommand(text))) {
    if (textArg === undefined) { input.value = ""; autosize(); }
    return;
  }
  if (textArg === undefined) remember(text);
  if (S.busy) {
    S.queue.push({ text, images });
    drawQueue();
  } else {
    const r = await api("/api/send", { text, images });
    if (!r.ok) {
      // Sesli asistan aynı anda iş başlatmış olabilir: mesaj kaybolmasın, sıraya girsin.
      if (/çalışıyor/.test(r.error || "")) {
        S.queue.push({ text, images }); drawQueue();
        await refreshStatus();
        if (!S.busy) flushQueue();
      }
      else { toast(r.error || "Gönderilemedi"); return; }
    }
  }
  if (textArg === undefined) {
    input.value = ""; S.attachments = []; drawAttachments(); autosize();
  }
}
// Kutu boşken (ya da geri çağrılmış bir mesaj değişmeden duruyorken) oklar önceki mesajlarda gezer.
function recallSent(dir) {
  const cur = S.sentIdx >= 0 ? S.sent[S.sent.length - 1 - S.sentIdx] : "";
  if (input.value !== cur || !S.sent.length) return false;
  const i = S.sentIdx + dir;
  if (i < -1 || i >= S.sent.length) return true;
  S.sentIdx = i;
  input.value = i < 0 ? "" : S.sent[S.sent.length - 1 - i];
  autosize();
  input.setSelectionRange(input.value.length, input.value.length);
  return true;
}
function remember(text) {
  if (!text || text.startsWith("/")) return;
  S.sent = S.sent.filter((t) => t !== text).concat(text).slice(-50);
  S.sentIdx = -1;
}
function drawQueue() {
  const q = $("#queue");
  q.replaceChildren();
  S.queue.forEach((item, i) => {
    const row = el("div", "queued");
    row.append(el("small", "", "Sırada"), el("span", "", item.text));
    const x = el("button", "act"); x.title = "Sıradan çıkar"; x.appendChild(svg(IC.x));
    x.addEventListener("click", () => { S.queue.splice(i, 1); drawQueue(); });
    row.appendChild(x);
    q.appendChild(row);
  });
}
async function flushQueue() {
  if (S.busy || !S.queue.length) return;
  const item = S.queue.shift();
  drawQueue();
  const r = await api("/api/send", item);
  if (!r.ok) toast(r.error || "Sıradaki mesaj gönderilemedi");
}
function stop() { api("/api/stop", {}); S.queue = []; drawQueue(); }

input.addEventListener("input", () => { autosize(); updatePopup(); });
input.addEventListener("click", updatePopup);
input.addEventListener("keydown", (e) => {
  if (S.popup) {
    if (e.key === "ArrowDown") { e.preventDefault(); S.popup.sel = (S.popup.sel + 1) % S.popup.items.length; drawPopup(); return; }
    if (e.key === "ArrowUp") { e.preventDefault(); S.popup.sel = (S.popup.sel - 1 + S.popup.items.length) % S.popup.items.length; drawPopup(); return; }
    if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) { e.preventDefault(); choosePopup(S.popup.sel); return; }
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); hidePopup(); return; }
  }
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
  if ((e.key === "ArrowUp" || e.key === "ArrowDown") && !e.shiftKey && recallSent(e.key === "ArrowUp" ? 1 : -1)) e.preventDefault();
  if (e.key === "Tab" && e.shiftKey) { e.preventDefault(); cycleMode(); }
});
input.addEventListener("paste", (e) => {
  const files = Array.from(e.clipboardData?.files || []).filter((f) => f.type.startsWith("image/"));
  if (files.length) { e.preventDefault(); files.forEach(addImageFile); }
});
$("#attachBtn").addEventListener("click", () => $("#fileInput").click());
$("#fileInput").addEventListener("change", (e) => { Array.from(e.target.files).forEach(addImageFile); e.target.value = ""; });
let dragDepth = 0;
window.addEventListener("dragenter", (e) => { if (e.dataTransfer?.types.includes("Files")) { dragDepth++; $("#dropzone").hidden = false; } });
window.addEventListener("dragleave", () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) $("#dropzone").hidden = true; });
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => {
  e.preventDefault(); dragDepth = 0; $("#dropzone").hidden = true;
  Array.from(e.dataTransfer?.files || []).forEach(addImageFile);
});

$("#sendBtn").addEventListener("click", () => (S.busy && !input.value.trim() ? stop() : submit()));
$("#modeBtn").addEventListener("click", modeMenu);
$("#modelBtn").addEventListener("click", modelMenu);
$("#thinkBtn").addEventListener("click", thinkMenu);
$("#settingsBtn").addEventListener("click", () => showSettings("general"));
$("#cwdBtn").addEventListener("click", askCwd);
$("#newChat").addEventListener("click", newChat);
$$(".sg").forEach((b) => b.addEventListener("click", () => {
  input.value = b.dataset.text; autosize(); input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
  if (!b.dataset.text.endsWith(": ")) submit();
}));
function toggleSide() {
  if (window.innerWidth <= 760) app.classList.toggle("side-open");
  else app.classList.toggle("side-hidden");
  try { localStorage.setItem("alice.side", app.classList.contains("side-hidden") ? "0" : "1"); } catch { /* yok */ }
}
function closeSideOnMobile() { if (window.innerWidth <= 760) app.classList.remove("side-open"); }
$("#collapseSide").addEventListener("click", toggleSide);
$("#openSide").addEventListener("click", toggleSide);
try { if (localStorage.getItem("alice.side") === "0") app.classList.add("side-hidden"); } catch { /* yok */ }

document.addEventListener("keydown", (e) => {
  const mod = e.ctrlKey || e.metaKey;
  if (mod && !e.shiftKey && e.key.toLowerCase() === "k") { e.preventDefault(); $("#palette").hidden ? openPalette() : closePalette(); return; }
  if (mod && e.shiftKey && e.key.toLowerCase() === "p") { e.preventDefault(); openPalette(); return; }
  if (mod && e.key.toLowerCase() === "j") { e.preventDefault(); togglePanel(); return; }
  if (mod && e.key.toLowerCase() === "o" && !e.shiftKey) { e.preventDefault(); toggleVerbose(); return; }
  if (mod && e.key === ",") { e.preventDefault(); showSettings("general"); return; }
  // İzin kartı 1/2/3 ile cevaplanır; mesaj yazılırken rakamlar metne gider.
  const typing = (document.activeElement === input && input.value) ||
    (document.activeElement && document.activeElement !== input && /INPUT|TEXTAREA/.test(document.activeElement.tagName));
  if (!mod && !e.altKey && !typing && /^[123]$/.test(e.key) && $("#overlay").hidden && $("#palette").hidden) {
    const card = pendingPerm();
    if (card) {
      e.preventDefault();
      api("/api/permission", { id: card._id, answer: { 1: "y", 2: "a", 3: "n" }[e.key] });
      return;
    }
  }
  if (e.key === "Escape") {
    if (!$("#palette").hidden) closePalette();
    else if (!$("#menu").hidden) closeMenu();
    else if (!$("#overlay").hidden) closeDialog();
    else if (S.busy) stop();
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "b") { e.preventDefault(); toggleSide(); }
  if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === "o") { e.preventDefault(); newChat(); }
});

// Kopyala butonları ve linkler (model çıktısı uygulamanın içinde gezinmesin).
list.addEventListener("click", (e) => {
  const cp = e.target.closest("[data-copy='code']");
  if (cp) {
    const code = cp.closest(".codeblock").querySelector("code, pre");
    copyText(code.textContent, cp);
    return;
  }
  const a = e.target.closest("a[href]");
  if (a) { e.preventDefault(); api("/api/open", { url: a.href }); }
});

/* ==================================================================== olaylar */
const handlers = {
  status: applyStatus,
  team: onTeamEvent,
  team_live: onTeamLive,
  project_note: onProjectNote,
  user: (ev) => addUser(ev.text, ev.mi, ev.images),
  busy: (ev) => { S.status.busy = ev.value; setBusy(ev.value); if (!ev.value) loadSessions(); },
  stream_start: () => { if (!S.cur) showWorking("Düşünüyor"); },
  thinking: (ev) => onThinking(ev.text),
  delta: (ev) => onDelta(ev.text),
  stream_reset: () => { if (S.cur) { S.cur.text = ""; S.cur.prose.replaceChildren(); } },
  stream_end: (ev) => { onStreamEnd(ev.text, ev.source); if (S.busy) showWorking("Çalışıyor"); },
  tool_start: (ev) => toolRow(ev.id, ev.name, ev.summary, true),
  tool_end: (ev) => {
    const t = S.tools.get(ev.id) || toolRow(ev.id, ev.name, "", true);
    // Sonuç (diff, plan kartı) eklenmeden önce alttaysak, eklendikten sonra da altta kal.
    const stick = nearBottom();
    finishTool(t, ev, true);
    if (S.busy) showWorking("Çalışıyor");
    toBottom(stick);
  },
  permission: onPermission,
  permission_done: onPermissionDone,
  info: (ev) => addNote(ev.text, /hata|başarısız|cevap veremedi|başlatılamadı/i.test(ev.text) ? "err" :
    /durduruldu|limit/i.test(ev.text) ? "warn" : ""),
  route: (ev) => {
    const m = ev.text.match(/^brain -> (.+)$/);
    if (m) { applyStatus({ model: m[1] }); return; }
    // Model geçişleri ve sağlayıcı hataları: hangi modelin neden atlandığı görünsün.
    if (!/^(fast|vision) -> /.test(ev.text)) addNote(ev.text, /hata|geçildi|meşgul|atlandı/i.test(ev.text) ? "warn" : "");
  },
};
// Çalışıyor göstergesini her zaman en altta tut.
const keepWorkingLast = () => { if (S.working && S.working !== list.lastElementChild) list.appendChild(S.working); };
new MutationObserver(() => { keepWorkingLast(); updateJump(); }).observe(list, { childList: true });

// Yukarı kaydırılmışken yeni içerik gelirse "en alta in" düğmesi çıkar.
function updateJump() { $("#jump").hidden = app.classList.contains("empty") || nearBottom(); }
scroller.addEventListener("scroll", updateJump, { passive: true });
$("#jump").addEventListener("click", () => { scroller.scrollTo({ top: scroller.scrollHeight, behavior: "smooth" }); });

// Sunucu kapanınca (çekirdek yeniden başlarken) sessizce donmak yerine bunu söyle.
function setOnline(v) {
  if (S.online === v) return;
  S.online = v;
  clearTimeout(setOnline._t);
  if (v) { $("#conn").hidden = true; return; }
  // Kısa kopmalarda (uyku, ağ) yanıp sönmesin.
  setOnline._t = setTimeout(() => { if (!S.online) $("#conn").hidden = false; }, 1500);
}

let connectedOnce = false;
function connect() {
  const es = new EventSource("/api/events?t=" + encodeURIComponent(TOKEN));
  es.onopen = () => {
    setOnline(true);
    if (connectedOnce) {
      loadHistory().then(() => { if (S.busy) showWorking("Çalışıyor"); });
      loadSessions();
      loadProjects();
      if (S.proj) refreshProject();
    }
    connectedOnce = true;
  };
  es.onerror = () => {
    setOnline(false);
    // Tarayıcı kendisi yeniden dener; bağlantı tamamen kapandıysa biz deneriz.
    if (es.readyState === EventSource.CLOSED) { es.close(); setTimeout(connect, 3000); }
  };
  es.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch { return; }
    const h = handlers[ev.type];
    if (h) h(ev);
  };
}

(async function boot() {
  greet();
  await refreshStatus();
  await loadHistory();
  connect();
  loadSessions();
  await loadProjects();
  // Telefondan/bağlantıdan gelen adres: #projects (listeyi aç) ya da #p=<id> (projeyi aç)
  const h = location.hash;
  if (h.startsWith("#p=")) openProject(decodeURIComponent(h.slice(3)));
  else if (h === "#projects") {
    if (window.innerWidth <= 760) $("#app").classList.add("side-open");
    const first = (S.projects || []).find((p) => p.status !== "done") || (S.projects || [])[0];
    if (first) openProject(first.id);
  }
  const cmds = await api("/api/commands");
  S.commands = Array.isArray(cmds) ? cmds : [];
  autosize();
  input.focus();
  refreshChanges();
  if (PREFS.panel) openPanel();
  setInterval(() => { if (!S.busy) refreshStatus(); }, 10000);
  const clock = () => {
    const m = Math.floor((Date.now() - S.started) / 60000);
    $("#stClock").textContent = m < 60 ? `${m} dk` : `${Math.floor(m / 60)} sa ${m % 60} dk`;
  };
  clock();
  setInterval(clock, 30000);
})();
