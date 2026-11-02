// ALICE tarayıcı eklentisi: ALICE çekirdeğine yerel WebSocket'le bağlanır, gelen komutları senin sekmelerinde yapar.
// Uzaktan hata ayıklama (CDP) kullanılmaz: Opera her açılışta izin sormaz, indirmeler ve tema ellenmez.
// Protokol: ALICE -> {id, method, params}; eklenti -> {id, result} | {id, error}. Bağlanınca {hello}, 20 sn'de bir
// {ping} (servis çalışanı uykuya dalmasın).

const URL = "ws://127.0.0.1:9340/alice-ext";
let ws = null;
let state = "bağlı değil";

function setState(s) {
  state = s;
  chrome.action.setBadgeText({text: s === "bağlı" ? "" : "!"}).catch(() => {});
  chrome.action.setBadgeBackgroundColor({color: "#86868B"}).catch(() => {});
}

function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  try {
    ws = new WebSocket(URL);
  } catch (e) {
    setState("bağlı değil");
    return;
  }
  ws.onopen = () => {
    setState("bağlı");
    ws.send(JSON.stringify({hello: {version: chrome.runtime.getManifest().version, ua: navigator.userAgent}}));
  };
  ws.onclose = () => {
    setState("bağlı değil");
    ws = null;
    setTimeout(connect, 3000);
  };
  ws.onerror = () => {};
  ws.onmessage = async (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.id === undefined) return;
    let reply;
    try {
      const fn = METHODS[msg.method];
      if (!fn) throw new Error("bilinmeyen komut: " + msg.method);
      reply = {id: msg.id, result: await fn(msg.params || {})};
    } catch (e) {
      reply = {id: msg.id, error: String((e && e.message) || e)};
    }
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(reply));
  };
}

setInterval(() => {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ping: Date.now()}));
  else connect();
}, 20000);
chrome.alarms.create("alice-wake", {periodInMinutes: 1});
chrome.alarms.onAlarm.addListener(() => connect());
chrome.runtime.onStartup.addListener(() => connect());
chrome.runtime.onInstalled.addListener(() => connect());
chrome.runtime.onMessage.addListener((m, _s, send) => { if (m === "state") send(state); });
connect();

// ------------------------------------------------------------------ sekmeler
function tabInfo(t) {
  return {id: t.id, windowId: t.windowId, index: t.index, title: t.title || "", url: t.url || t.pendingUrl || "",
          active: t.active, status: t.status};
}

async function normalTabs() {
  const wins = await chrome.windows.getAll({populate: true, windowTypes: ["normal"]});
  const out = [];
  for (const w of wins) for (const t of w.tabs) out.push(tabInfo(t));
  return out;
}

function waitLoad(tabId, timeout) {
  return new Promise((resolve) => {
    let done = false;
    const finish = async () => {
      if (done) return;
      done = true;
      chrome.tabs.onUpdated.removeListener(on);
      try { resolve(tabInfo(await chrome.tabs.get(tabId))); } catch (e) { resolve(null); }
    };
    const on = (id, info) => { if (id === tabId && info.status === "complete") finish(); };
    chrome.tabs.onUpdated.addListener(on);
    chrome.tabs.get(tabId).then((t) => { if (t.status === "complete") finish(); }).catch(finish);
    setTimeout(finish, timeout || 15000);
  });
}

async function focusTab(tabId) {
  const t = await chrome.tabs.update(tabId, {active: true});
  await chrome.windows.update(t.windowId, {focused: true});
  return tabInfo(t);
}

const METHODS = {
  async tabs() {
    const focused = await chrome.windows.getLastFocused({windowTypes: ["normal"]}).catch(() => null);
    return {tabs: await normalTabs(), focusedWindow: focused ? focused.id : null};
  },
  async tab({tabId}) { return tabInfo(await chrome.tabs.get(tabId)); },
  async focus({tabId}) { return focusTab(tabId); },
  async navigate({tabId, url, timeout}) {
    await chrome.tabs.update(tabId, {url});
    await new Promise((r) => setTimeout(r, 150));  // "complete" eski sayfanınki olmasın
    return waitLoad(tabId, timeout);
  },
  async create({url, timeout}) {
    const w = await chrome.windows.getLastFocused({windowTypes: ["normal"]}).catch(() => null);
    const t = await chrome.tabs.create({url: url || "about:blank", windowId: w ? w.id : undefined, active: true});
    return url ? waitLoad(t.id, timeout) : tabInfo(t);
  },
  async wait({tabId, timeout}) { return waitLoad(tabId, timeout); },
  async back({tabId, timeout}) {
    await chrome.tabs.goBack(tabId);
    await new Promise((r) => setTimeout(r, 150));
    return waitLoad(tabId, timeout);
  },
  async capture({tabId}) {
    const t = await focusTab(tabId);
    return chrome.tabs.captureVisibleTab(t.windowId, {format: "png"});
  },
  async downloads({limit}) {
    const items = await chrome.downloads.search({orderBy: ["-startTime"], limit: limit || 5});
    return items.map((d) => ({file: d.filename, url: d.finalUrl || d.url, state: d.state, error: d.error || "",
                              bytes: d.bytesReceived, total: d.totalBytes, started: d.startTime}));
  },
  async run({tabId, fn, args}) {
    const func = PAGE[fn];
    if (!func) throw new Error("bilinmeyen sayfa işlevi: " + fn);
    const [r] = await chrome.scripting.executeScript({target: {tabId}, func, args: args || []});
    if (r && r.result && r.result.__error) throw new Error(r.result.__error);
    return r ? r.result : null;
  },
  async eval({tabId, code}) {
    const [r] = await chrome.scripting.executeScript({
      target: {tabId}, world: "MAIN",
      func: (c) => { try { return {v: JSON.parse(JSON.stringify((0, eval)(c) ?? null))}; } catch (e) { return {e: String(e)}; } },
      args: [code]});
    if (!r || !r.result) return null;
    if (r.result.e) throw new Error("sayfa eval'e izin vermedi ya da hata verdi: " + r.result.e);
    return r.result.v;
  },
};

// ------------------------------------------------------------------ sayfanın içinde çalışanlar
// executeScript bunları sayfaya metin olarak kopyalar: dışarıdaki hiçbir şeye dayanamazlar ve "function (...)" biçiminde
// olmalılar (kısa yöntem söz dizimi "ad(...) {}" sayfada çalışmıyor, sessizce boş dönüyordu).
const PAGE = {
  // alice/tools/browser.py SNAPSHOT_JS ile aynı seçici ve alanlar (test eşitliği denetler)
  snapshot: function (limit) {
    const sel = 'a[href],button,input:not([type=hidden]),textarea,select,summary,[role=button],[role=link],' +
                '[role=tab],[role=menuitem],[role=checkbox],[role=option],[role=combobox],[role=textbox],' +
                '[role=searchbox],[contenteditable=true],[onclick],[tabindex]:not([tabindex="-1"]),[draggable=true],[ondrop]';
    document.querySelectorAll('[data-alice-id]').forEach(e => e.removeAttribute('data-alice-id'));
    const out = [];
    const vh = window.innerHeight, vw = window.innerWidth;
    for (const el of document.querySelectorAll(sel)) {
      const r = el.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) continue;
      const st = getComputedStyle(el);
      if (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') continue;
      if (r.bottom < 0 || r.top > vh * 2.5 || r.right < 0 || r.left > vw) continue;
      const tag = el.tagName.toLowerCase();
      let text = (el.getAttribute('aria-label') || el.innerText || el.value || el.placeholder ||
                  el.title || el.getAttribute('alt') || '').trim().replace(/\s+/g, ' ');
      if (!text && tag === 'a') text = el.getAttribute('href') || '';
      if (!text && tag !== 'input' && tag !== 'textarea' && tag !== 'select') continue;
      const id = out.length + 1;
      el.setAttribute('data-alice-id', String(id));
      out.push({
        id, tag, role: el.getAttribute('role') || '', type: el.type || '',
        text: text.slice(0, 90),
        placeholder: (el.placeholder || '').slice(0, 40),
        value: (tag === 'input' || tag === 'textarea') && el.type !== 'password' ? (el.value || '').slice(0, 60) : '',
        href: tag === 'a' ? (el.getAttribute('href') || '').slice(0, 80) : '',
        inView: r.top >= 0 && r.bottom <= vh,
      });
      if (out.length >= limit) break;
    }
    return out;
  },
  text: function () { return document.body ? document.body.innerText : ''; },
  title: function () { return document.title; },
  viewport: function () { return {width: innerWidth, height: innerHeight}; },
  // Öğenin (ya da id yoksa sayfanın) kutusu ve pencerenin ekrandaki yeri: ekranda gerçek tıklama ve HUD için.
  place: function (id) {
    let r = {left: 0, top: 0, width: 0, height: 0};
    if (id !== null && id !== undefined) {
      const el = document.querySelector('[data-alice-id="' + id + '"]');
      if (!el) return {__error: "öğe [" + id + "] yok; sayfa değişmiş olabilir, snapshot ile yenile"};
      r = el.getBoundingClientRect();
    }
    return {left: r.left, top: r.top, width: r.width, height: r.height, sx: window.screenX, sy: window.screenY,
            ow: window.outerWidth, oh: window.outerHeight, iw: window.innerWidth, ih: window.innerHeight,
            dpr: window.devicePixelRatio, visible: document.visibilityState === 'visible' && document.hasFocus()};
  },
  reveal: function (id) {
    const el = document.querySelector('[data-alice-id="' + id + '"]');
    if (!el) return {__error: "öğe [" + id + "] yok; sayfa değişmiş olabilir, snapshot ile yenile"};
    el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
    return true;
  },
  outline: function (id) {
    const el = document.querySelector('[data-alice-id="' + id + '"]');
    if (!el) return {__error: "öğe [" + id + "] yok"};
    const o = el.style.outline, f = el.style.outlineOffset;
    el.style.outline = '3px solid #0066CC'; el.style.outlineOffset = '2px';
    setTimeout(() => { el.style.outline = o; el.style.outlineOffset = f; }, 3000);
    return true;
  },
  // Pencere arkadayken ya da küçültülmüşken: gerçek fare yerine sayfa olaylarıyla tıkla
  click: function (id, button, count) {
    const el = document.querySelector('[data-alice-id="' + id + '"]');
    if (!el) return {__error: "öğe [" + id + "] yok; sayfa değişmiş olabilir, snapshot ile yenile"};
    el.scrollIntoView({block: 'center', behavior: 'instant'});
    const r = el.getBoundingClientRect();
    const o = {bubbles: true, cancelable: true, composed: true, view: window, clientX: r.left + r.width / 2,
               clientY: r.top + r.height / 2, button: button === 'right' ? 2 : 0};
    for (let i = 0; i < (count || 1); i++) {
      el.dispatchEvent(new PointerEvent('pointerdown', o)); el.dispatchEvent(new MouseEvent('mousedown', o));
      if (el.focus) el.focus();
      el.dispatchEvent(new PointerEvent('pointerup', o)); el.dispatchEvent(new MouseEvent('mouseup', o));
      if (button === 'right') el.dispatchEvent(new MouseEvent('contextmenu', o));
      else el.click();
    }
    if (count === 2) el.dispatchEvent(new MouseEvent('dblclick', o));
    return true;
  },
  // Playwright'ın fill'i gibi: alanı temizleyip yazar; React/Vue gibi çatılar değişikliği görür
  fill: function (id, text) {
    const el = document.querySelector('[data-alice-id="' + id + '"]');
    if (!el) return {__error: "öğe [" + id + "] yok; sayfa değişmiş olabilir, snapshot ile yenile"};
    el.scrollIntoView({block: 'center', behavior: 'instant'});
    el.focus();
    if (el.isContentEditable) {
      document.execCommand('selectAll', false, null);
      document.execCommand('insertText', false, text);
    } else {
      el.select && el.select();
      if (!document.execCommand('insertText', false, text) || el.value !== text) {
        const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, text);
        el.dispatchEvent(new Event('input', {bubbles: true}));
      }
      el.dispatchEvent(new Event('change', {bubbles: true}));
    }
    return true;
  },
  focus: function (id) {
    const el = document.querySelector('[data-alice-id="' + id + '"]');
    if (!el) return {__error: "öğe [" + id + "] yok; sayfa değişmiş olabilir, snapshot ile yenile"};
    el.focus();
    return true;
  },
  select: function (id, label) {
    const el = document.querySelector('[data-alice-id="' + id + '"]');
    if (!el) return {__error: "öğe [" + id + "] yok; sayfa değişmiş olabilir, snapshot ile yenile"};
    const want = String(label).trim().toLowerCase();
    const opt = [...(el.options || [])].find(o => o.text.trim().toLowerCase() === want) ||
                [...(el.options || [])].find(o => o.text.trim().toLowerCase().includes(want));
    if (!opt) return {__error: "seçenek yok: " + label};
    el.value = opt.value;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    return opt.text;
  },
  // Pencere arkadayken tuş: odaktaki öğeye klavye olayları; Enter formdaysa formu gönderir
  key: function (key) {
    const el = document.activeElement || document.body;
    const o = {key, code: key, bubbles: true, cancelable: true, composed: true};
    const go = el.dispatchEvent(new KeyboardEvent('keydown', o));
    el.dispatchEvent(new KeyboardEvent('keypress', o));
    el.dispatchEvent(new KeyboardEvent('keyup', o));
    if (go && key === 'Enter' && el.form) {
      if (el.form.requestSubmit) el.form.requestSubmit(); else el.form.submit();
    }
    return true;
  },
  scroll: function (dy) { window.scrollBy({top: dy, behavior: 'instant'}); return true; },
};
