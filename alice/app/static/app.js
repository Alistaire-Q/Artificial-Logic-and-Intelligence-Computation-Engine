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
};

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
    head.appendChild(el("span", "", lang || "kod"));
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
    const ed = el("button", "act"); ed.title = "Düzenle ve buradan yeniden başlat";
    ed.append(svg(IC.edit), document.createTextNode("Düzenle"));
    ed.addEventListener("click", () => rewindTo(mi, true));
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
  row.appendChild(svg(icon, "ti"));
  const v = el("span", "verb" + (running ? " shimmer" : ""), verb);
  row.append(v, el("span", "arg", summary || ""));
  const meta = el("span", "meta");
  row.append(meta, svg(IC.chev, "tchev"));
  const body = el("div", "tool-body");
  t.append(row, body);
  row.addEventListener("click", () => t.classList.toggle("open"));
  t._v = v; t._meta = meta; t._body = body; t._name = name;
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

function renderDiff(diff) {
  const box = el("div", "diff");
  const head = el("div", "diff-head");
  const lines = diff.split("\n");
  const file = (lines.find((l) => l.startsWith("+++ b/")) || "").slice(6);
  let a = 0, d = 0;
  const body = el("div", "diff-body");
  for (const line of lines) {
    if (line.startsWith("+++") || line.startsWith("---")) continue;
    let cls = "";
    if (line.startsWith("+")) { cls = "add"; a++; }
    else if (line.startsWith("-")) { cls = "del"; d++; }
    else if (line.startsWith("@@")) cls = "hunk";
    body.appendChild(el("div", cls, line || " "));
  }
  head.append(el("b", "", file), el("span", "", `+${a} −${d}`));
  box.append(head, body);
  return { box, a, d };
}

function renderTodo(text) {
  const box = el("div", "todo");
  box.appendChild(el("div", "todo-title", "Görevler"));
  const ul = el("ul");
  for (const line of text.split("\n")) {
    const m = line.match(/^\[( |~|x)\]\s*(.*)$/);
    if (!m) continue;
    const li = el("li", m[1] === "x" ? "done" : m[1] === "~" ? "doing" : "");
    const bx = el("span", "box");
    if (m[1] === "x") bx.appendChild(svg(IC.check));
    li.append(bx, el("span", "", m[2]));
    ul.appendChild(li);
  }
  box.appendChild(ul);
  return box;
}

function finishTool(t, ev) {
  t.classList.remove("running");
  t._v.classList.remove("shimmer");
  if (ev.is_error) t.classList.add("err");
  const group = t.parentElement && t.parentElement.closest(".tool-group");
  if (group) setTimeout(() => updateGroup(group), 0);
  const body = t._body;
  body.replaceChildren();
  if (t._name === "todo_write" && !ev.is_error) {
    t.replaceWith(renderTodo(ev.text || ""));
    return;
  }
  if (ev.diff) {
    const { box, a, d } = renderDiff(ev.diff);
    body.appendChild(box);
    t._meta.replaceChildren(el("span", "a", `+${a}`), document.createTextNode(" "), el("span", "d", `−${d}`));
    t.classList.add("open");
  }
  if (ev.text && (!ev.diff || ev.is_error)) body.appendChild(el("pre", "out", ev.text));
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
  const [verb] = verbFor(ev.name);
  q.append(document.createTextNode(ev.risk === "danger" ? "Bu işlem geri alınamayabilir. " : "ALICE izin istiyor: "),
    el("b", "", verb));
  const acts = el("div", "perm-actions");
  const mk = (label, ans, cls) => {
    const b = el("button", "btn " + (cls || ""), label);
    b.addEventListener("click", () => api("/api/permission", { id: ev.id, answer: ans }));
    return b;
  };
  acts.append(mk("İzin ver", "y", "primary"), mk("Bu oturumda hep izin ver", "a"), mk("Reddet", "n", "danger"));
  card.append(q, el("pre", "", ev.summary), acts);
  card._acts = acts;
  S.perms.set(ev.id, card);
  add(card, true);
}
function onPermissionDone(ev) {
  const card = S.perms.get(ev.id);
  if (!card) return;
  const txt = { y: "İzin verildi", a: "Bu oturumda bu araca hep izin verildi", n: "Reddedildi" }[ev.answer] || ev.answer;
  card._acts.replaceWith(el("div", "perm-done", txt));
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
  if (v && !S.busy) S.turnStart = performance.now();
  S.busy = !!v;
  app.classList.toggle("busy", S.busy);
  document.title = S.busy ? "● ALICE" : "ALICE";
  $("#sendBtn").title = S.busy ? "Durdur (Esc)" : "Gönder (Enter)";
  if (S.busy && !S.cur && !S.working) showWorking("Çalışıyor");
  if (!S.busy) { hideWorking(); markFinals(); flushQueue(); }
  updateSend();
}
function applyStatus(s) {
  Object.assign(S.status, s);
  const st = S.status;
  if (st.cwd !== undefined) { $("#cwd").textContent = st.cwd; $("#cwdBtn").title = st.cwd; }
  if (st.version) $("#ver").textContent = "ALICE " + st.version;
  if (st.user) { $("#userName").textContent = st.user; $("#avatar").textContent = st.user[0].toUpperCase(); }
  if (st.mode) {
    const [label, , icon] = MODES[st.mode] || [st.mode, "", IC.shield];
    $("#modeLabel").textContent = label;
    $("#modeIcon").innerHTML = icon;
    $("#modeBtn").className = "c-btn pill m-" + st.mode;
  }
  $("#modelLabel").textContent = shortModel(st.model) + (st.thinking && THINK[st.thinking] ? " · " + THINK[st.thinking][0] : "");
  $("#modelBtn").title = st.model || "";
  $("#stModel").textContent = st.model || "model yok";
  if (st.ctx_limit) {
    const pct = Math.min(100, (100 * st.ctx_tokens) / st.ctx_limit);
    const m = $("#stCtx .meter");
    m.firstElementChild.style.width = pct + "%";
    m.classList.toggle("hot", pct > 75);
    $("#stCtx span").textContent = `${(st.ctx_tokens / 1000).toFixed(1)}k / ${Math.round(st.ctx_limit / 1000)}k`;
  }
  if (st.today_requests !== undefined) $("#stReq").textContent = `bugün ${st.today_requests} istek`;
  if (st.decider) $("#stDec").textContent = `karar: ${st.decider}`;
  if (st.busy !== undefined && st.busy !== S.busy) setBusy(st.busy);
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

async function loadSessions() {
  const rows = await api("/api/sessions");
  const nav = $("#sessions");
  nav.replaceChildren();
  for (const s of Array.isArray(rows) ? rows : []) {
    const b = el("button", "sess");
    b.dataset.id = s.id;
    b.title = `${s.title}\n${s.cwd}`;
    b.appendChild(el("span", "", s.title || "(boş)"));
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
async function resume(id) {
  if (S.busy) return toast("Önce mevcut işi durdur");
  const r = await api("/api/resume", { id });
  if (!r.ok) return toast(r.error || "Oturum yüklenemedi");
  renderHistory(r.history);
  applyStatus(r);
  closeSideOnMobile();
}
async function newChat() {
  if (S.busy) return toast("Önce mevcut işi durdur");
  const r = await api("/api/new", {});
  if (r.ok) { clearChat(); applyStatus(r); loadSessions(); input.focus(); }
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
  toast(r.restored && r.restored.length ? `Geri sarıldı, ${r.restored.length} dosya eski haline getirildi` : "Geri sarıldı");
  if (edit) { input.value = r.text; autosize(); input.focus(); }
}

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

function modelMenu() {
  openMenu($("#modelBtn"), (m) => {
    m.appendChild(el("div", "menu-title", "Düşünme"));
    for (const [k, [label, sub]] of Object.entries(THINK)) {
      menuItem(m, k === "off" ? IC.bolt : IC.brain, label[0].toUpperCase() + label.slice(1), sub,
        () => setThinking(k), S.status.thinking === k);
    }
    m.appendChild(el("div", "menu-sep"));
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

function settingsMenu() {
  openMenu($("#settingsBtn"), (m) => {
    menuItem(m, IC.list, "Modeller", "Rol zincirleri ve durumları", showModels);
    menuItem(m, IC.bolt, "Araçlar", "ALICE'in kullanabildiği araçlar (MCP dahil)", showTools);
    menuItem(m, IC.brain, "Hafıza", "Oturumlar arası kaydedilen notlar", showMemory);
    menuItem(m, IC.list, "Kullanım", "Bugünkü istek ve token sayıları", showUsage);
    menuItem(m, IC.term, "Komutlar", "Slash komutları ve kısayollar", showHelp);
  });
}

/* =================================================================== dialoglar */
let onDialogClose = null;
function showDialog(build) {
  if (onDialogClose) closeDialog();
  const d = $("#dialog");
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
    d.append(inp, err, row);
    setTimeout(() => { inp.focus(); inp.select(); }, 0);
  });
}
async function showModels() {
  const data = await api("/api/models");
  showDialog((d) => {
    d.append(el("h2", "", "Modeller"),
      el("p", "", "Her rol soldan sağa denenir. Limite takılan ya da hata veren model atlanır, kısa süre sonra tekrar denenir."));
    const names = { brain: "Ana model", fast: "Hızlı işler", vision: "Görsel" };
    for (const [role, rows] of Object.entries(data || {})) {
      d.appendChild(el("div", "sec", names[role] || role));
      for (const r of rows) {
        const row = el("div", "list-row");
        row.append(el("span", "l", r.ref), el("span", "r" + (r.status === "hazır" ? " ok" : ""), r.status));
        d.appendChild(row);
      }
    }
    dialogClose(d);
  });
}
async function showTools() {
  const tools = await api("/api/tools");
  showDialog((d) => {
    d.append(el("h2", "", "Araçlar"), el("p", "", `${tools.length} araç. MCP sunucularını config.toml'daki [mcp.servers] altına ekleyebilirsin.`));
    for (const t of tools) {
      const row = el("div", "list-row");
      row.append(el("span", "l", t.name), el("span", "r", t.risk));
      row.title = t.description;
      d.appendChild(row);
    }
    dialogClose(d);
  });
}
async function showMemory() {
  const r = await api("/api/memory");
  showDialog((d) => {
    d.append(el("h2", "", "Hafıza"), el("p", "", "ALICE'in oturumlar arası kaydettiği notlar (~/.alice/memory.md)."),
      el("pre", "mem", r.text || "(henüz bir şey kaydedilmedi)"));
    dialogClose(d);
  });
}
async function showUsage() {
  const u = await api("/api/usage");
  showDialog((d) => {
    d.append(el("h2", "", "Bugünkü kullanım"));
    const entries = Object.entries(u || {});
    if (!entries.length) d.appendChild(el("p", "", "Bugün henüz istek yok."));
    for (const [ref, r] of entries) {
      const row = el("div", "list-row");
      row.append(el("span", "l", ref),
        el("span", "r", `${r.requests} istek · ${r.errors} hata · ${(r.prompt_tokens / 1000).toFixed(1)}k girdi · ${(r.completion_tokens / 1000).toFixed(1)}k çıktı`));
      d.appendChild(row);
    }
    dialogClose(d);
  });
}
function showHelp() {
  showDialog((d) => {
    d.append(el("h2", "", "Komutlar"), el("p", "", "Mesaj kutusuna / yazınca liste açılır. @ yazınca dosya önerir."));
    for (const c of S.commands) {
      const row = el("div", "list-row");
      row.append(el("span", "l", "/" + c.name), el("span", "r", c.description + (c.kind === "custom" ? " (özel)" : "")));
      d.appendChild(row);
    }
    d.appendChild(el("div", "sec", "Kısayollar"));
    for (const [k, v] of [["Enter", "Gönder (çalışırken sıraya ekler)"], ["Shift+Enter", "Yeni satır"], ["↑ / ↓", "Önceki mesajları geri getir (kutu boşken)"], ["Esc", "Durdur / menüyü kapat"],
      ["Shift+Tab", "İzin modunu değiştir"], ["Ctrl+B", "Kenar çubuğu"], ["Ctrl+Shift+O", "Yeni sohbet"], ["Ctrl+V", "Görsel yapıştır"]]) {
      const row = el("div", "list-row");
      row.append(el("span", "l", k), el("span", "r", v));
      d.appendChild(row);
    }
    dialogClose(d);
  });
}

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
      if (THINK[arg]) setThinking(arg); else modelMenu();
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
$("#settingsBtn").addEventListener("click", settingsMenu);
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
  if (e.key === "Escape") {
    if (!$("#menu").hidden) closeMenu();
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
  user: (ev) => addUser(ev.text, ev.mi, ev.images),
  busy: (ev) => { setBusy(ev.value); if (!ev.value) loadSessions(); },
  stream_start: () => { if (!S.cur) showWorking("Düşünüyor"); },
  thinking: (ev) => onThinking(ev.text),
  delta: (ev) => onDelta(ev.text),
  stream_reset: () => { if (S.cur) { S.cur.text = ""; S.cur.prose.replaceChildren(); } },
  stream_end: (ev) => { onStreamEnd(ev.text, ev.source); if (S.busy) showWorking("Çalışıyor"); },
  tool_start: (ev) => toolRow(ev.id, ev.name, ev.summary, true),
  tool_end: (ev) => {
    const t = S.tools.get(ev.id) || toolRow(ev.id, ev.name, "", true);
    finishTool(t, ev);
    if (S.busy) showWorking("Çalışıyor");
    toBottom();
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
  const cmds = await api("/api/commands");
  S.commands = Array.isArray(cmds) ? cmds : [];
  autosize();
  input.focus();
  setInterval(() => { if (!S.busy) refreshStatus(); }, 10000);
})();
