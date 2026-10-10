"use strict";
/* Küçük sözdizimi renklendirici: kod blokları ve diff satırları için.
   Dışarıdan kütüphane yüklenmez (CSP sadece 'self'); yaygın dillerin anahtar
   kelimeleri, metinleri, yorumları ve sayıları renklenir. Çıktı her zaman
   kaçışlanmış HTML'dir, model çıktısı buradan sayfaya kod sokamaz. */
(function () {
  const words = (s) => new Set(s.split(" "));
  const C_LIKE = "if else for while do switch case default break continue return function var let const new delete typeof instanceof " +
    "class extends super this import export from as async await yield try catch finally throw in of void static public private " +
    "protected interface type enum implements package namespace using struct union goto sizeof template typename virtual override " +
    "final abstract readonly get set fn impl trait pub mod use mut match loop where crate self Self dyn ref move unsafe go chan " +
    "defer func map range select fallthrough var val fun when object companion lateinit";
  const LANGS = {
    py: { kw: words("and as assert async await break class continue def del elif else except finally for from global if import " +
      "in is lambda nonlocal not or pass raise return try while with yield match case"),
      lit: words("True False None self cls"), line: "#", str: ['"""', "'''", '"', "'"], deco: true },
    js: { kw: words(C_LIKE), lit: words("true false null undefined NaN Infinity"), line: "//", block: ["/*", "*/"], str: ['"', "'", "`"] },
    c: { kw: words(C_LIKE + " int char float double long short unsigned signed bool auto register extern volatile inline " +
      "#include #define #ifdef #ifndef #endif #pragma string void"), lit: words("true false null nullptr NULL nil"), line: "//",
      block: ["/*", "*/"], str: ['"', "'"] },
    sh: { kw: words("if then else elif fi for while until do done case esac in function return local export source alias " +
      "echo cd exit set unset readonly shift trap eval exec sudo"), lit: words("true false"), line: "#", str: ['"', "'"], vars: true },
    ps: { kw: words("if else elseif foreach for while do until switch function param begin process end return try catch " +
      "finally throw break continue exit Get-ChildItem Set-Location Write-Host Write-Output Remove-Item New-Item Copy-Item " +
      "Move-Item Get-Content Set-Content Select-Object Where-Object ForEach-Object Start-Process Get-Process"),
      lit: words("$true $false $null"), line: "#", block: ["<#", "#>"], str: ['"', "'"], vars: true, ci: true },
    css: { kw: words("@media @import @keyframes @font-face !important"), lit: words(""), block: ["/*", "*/"], str: ['"', "'"], css: true },
    json: { kw: words(""), lit: words("true false null"), str: ['"'] },
    lua: { kw: words("and break do else elseif end for function goto if in local not or repeat return then until while"),
      lit: words("true false nil"), line: "--", str: ['"', "'"] },
    sql: { kw: words("select from where and or not insert into values update set delete create table drop alter join left right " +
      "inner outer on group by order having limit as distinct union all null is in like primary key index"),
      lit: words("true false null"), line: "--", block: ["/*", "*/"], str: ["'", '"'], ci: true },
    toml: { kw: words(""), lit: words("true false"), line: "#", str: ['"""', '"', "'"], ini: true },
  };
  // Büyük/küçük harf duyarsız dillerde kümeleri bir kez küçült.
  for (const L of Object.values(LANGS)) {
    if (L.ci) { L.kw = new Set(Array.from(L.kw, (k) => k.toLowerCase())); L.lit = new Set(Array.from(L.lit, (k) => k.toLowerCase())); }
  }
  const ALIAS = {
    python: "py", py: "py", js: "js", javascript: "js", ts: "js", typescript: "js", jsx: "js", tsx: "js", mjs: "js",
    java: "c", c: "c", h: "c", cpp: "c", "c++": "c", hpp: "c", cs: "c", csharp: "c", go: "c", rust: "c", rs: "c", kotlin: "c",
    kt: "c", swift: "c", dart: "c", php: "c", sh: "sh", bash: "sh", zsh: "sh", shell: "sh", console: "sh",
    ps1: "ps", powershell: "ps", pwsh: "ps", psm1: "ps", css: "css", scss: "css", less: "css", json: "json", jsonc: "js",
    lua: "lua", sql: "sql", toml: "toml", ini: "toml", cfg: "toml", yaml: "toml", yml: "toml",
  };
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const span = (cls, s) => `<span class="tk-${cls}">${esc(s)}</span>`;

  function langOf(name) {
    if (!name) return null;
    const n = name.toLowerCase();
    if (ALIAS[n]) return ALIAS[n];
    const ext = n.includes(".") ? n.split(".").pop() : "";
    return ALIAS[ext] || null;
  }

  // Satır satır değil, tüm metin tek geçişte: çok satırlı yorum ve metinler doğru kapanır.
  function highlight(code, lang) {
    const L = LANGS[langOf(lang)];
    if (!L) return esc(code);
    let out = "", i = 0, plain = "";
    const flush = () => { if (plain) { out += esc(plain); plain = ""; } };
    const n = code.length;
    while (i < n) {
      const ch = code[i];
      const rest = code.startsWith.bind(code);
      if (L.block && rest(L.block[0], i)) {
        const e = code.indexOf(L.block[1], i + L.block[0].length);
        const j = e < 0 ? n : e + L.block[1].length;
        flush(); out += span("c", code.slice(i, j)); i = j; continue;
      }
      if (L.line && rest(L.line, i) && !(L.line === "#" && L.vars && code[i - 1] === "$")) {
        const e = code.indexOf("\n", i);
        const j = e < 0 ? n : e;
        flush(); out += span("c", code.slice(i, j)); i = j; continue;
      }
      const q = L.str.find((s) => rest(s, i));
      if (q) {
        let j = i + q.length;
        while (j < n && !rest(q, j)) {
          if (code[j] === "\\") j++;
          else if (q.length === 1 && q !== "`" && code[j] === "\n") break;
          j++;
        }
        j = Math.min(n, j + (rest(q, j) ? q.length : 0));
        flush();
        const s = code.slice(i, j);
        // JSON anahtarları ve TOML anahtarları metinden ayrı renklensin
        out += span(L === LANGS.json && /^\s*:/.test(code.slice(j, j + 3)) ? "p" : "s", s);
        i = j; continue;
      }
      if (/[0-9]/.test(ch) && !/[\w$]/.test(code[i - 1] || "")) {
        const m = code.slice(i).match(/^(0x[\da-fA-F_]+|\d[\d_]*(\.\d+)?([eE][+-]?\d+)?[a-zA-Z%]*)/);
        flush(); out += span("n", m[0]); i += m[0].length; continue;
      }
      if (L.deco && ch === "@" && /[A-Za-z_]/.test(code[i + 1] || "")) {
        const m = code.slice(i).match(/^@[\w.]+/);
        flush(); out += span("f", m[0]); i += m[0].length; continue;
      }
      if (L.vars && ch === "$" && /[\w{(]/.test(code[i + 1] || "")) {
        const m = code.slice(i).match(/^\$(\{[^}]*\}|\(|[\w:]+)/);
        const lit = L.lit.has(m[0].toLowerCase());
        flush(); out += span(lit ? "l" : "v", m[0]); i += m[0].length; continue;
      }
      if (/[A-Za-z_#@!-]/.test(ch) && !/[\w-]/.test(code[i - 1] || "")) {
        const m = code.slice(i).match(L.css ? /^[@!#.]?[\w-]+/ : /^[#@]?[A-Za-z_][\w-]*/);
        if (m) {
          let w = m[0];
          // Tireli kelimeler sadece PowerShell/CSS'te tek kelime; diğerlerinde a-b işlemdir.
          if (!L.css && !L.ci && w.includes("-")) w = w.split("-")[0] || w[0];
          const key = L.ci ? w.toLowerCase() : w;
          const has = (set) => set.has(key);
          let cls = "";
          if (has(L.kw)) cls = "k";
          else if (has(L.lit)) cls = "l";
          else if (L.css && /^\s*:\s/.test(code.slice(i + w.length, i + w.length + 4))) cls = "p";
          else if (L.ini && /^\s*=/.test(code.slice(i + w.length, i + w.length + 3)) && /(^|\n)\s*$/.test(code.slice(0, i))) cls = "p";
          else if (/^\s*\(/.test(code.slice(i + w.length, i + w.length + 2))) cls = "f";
          else if (/^[A-Z][a-z]/.test(w) && !L.css) cls = "t";
          if (cls) { flush(); out += span(cls, w); } else plain += w;
          i += w.length; continue;
        }
      }
      plain += ch; i++;
    }
    flush();
    return out;
  }

  window.AliceHL = { highlight, langOf, esc };
})();
