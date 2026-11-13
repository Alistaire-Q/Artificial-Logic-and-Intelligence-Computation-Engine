"""Hook'lar: belli anlarda kendi komutunu çalıştır (Claude Code'daki hooks gibi).

config.toml (ya da ~/.alice/hooks.toml):

  [[hooks.PostToolUse]]
  matcher = "edit_file|write_file|multi_edit"     # araç adı (regex); boş = hepsi
  command = "ruff format \"$ALICE_TOOL_PATH\""
  timeout = 30

Olaylar:
  SessionStart      ilk mesajdan önce; çıktısı (stdout) ilk mesaja bağlam olarak eklenir
  UserPromptSubmit  her kullanıcı mesajında; stdout mesaja eklenir; çıkış kodu 2 = mesajı engelle (stderr sebep)
  PreToolUse        araç çalışmadan önce; çıkış kodu 2 = aracı engelle, stderr modele gider
  PostToolUse       araç bittikten sonra; çıkış kodu 2 = stderr modele geri bildirim olarak gider (ör. lint hatası)
  Stop              ajan cevabını bitirince; çıkış kodu 2 = stderr modele gider ve ajan devam eder (ör. testler kırık)

PreToolUse hook'u çıkış 0 ile stdout'a JSON yazarsa (Claude Code'daki gibi) daha fazlasını yapabilir:
  {"updatedInput": {...}}                              aracın argümanlarını değiştir (ör. komuta bayrak ekle)
  {"permissionDecision": "allow" | "deny" | "ask", "reason": "..."}   izin kararı (allow sormadan çalıştırır)
  {"additionalContext": "..."}                         araç sonucuna bağlam ekle (PostToolUse'ta da geçerli)

Komut kabukta çalışır, olay bilgisi stdin'e JSON olarak gelir ({"event", "tool", "args", "result", "prompt",
"cwd"}); ortamda ALICE_EVENT, ALICE_TOOL, ALICE_TOOL_PATH (aracın yol/komut/url argümanı) ve ALICE_CWD var.
Güvenlik: hook'lar sadece senin config'inden okunur; klonladığın bir depodaki dosya komut çalıştıramaz.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")
BLOCK = 2  # Claude Code ile aynı: 2 = engelle / geri bildirim ver


@dataclass
class Hook:
    event: str
    command: str
    matcher: str = ""
    timeout: float = 30.0

    def matches(self, tool: str) -> bool:
        if not self.matcher or self.matcher == "*":
            return True
        try:
            return re.fullmatch(self.matcher, tool) is not None
        except re.error:
            return self.matcher == tool


@dataclass
class HookResult:
    hook: Hook
    code: int
    stdout: str
    stderr: str

    @property
    def blocked(self) -> bool:
        return self.code == BLOCK


def _read_hooks_file() -> dict[str, Any]:
    from alice.config import alice_home

    path = alice_home() / "hooks.toml"
    if not path.is_file():
        return {}
    try:
        try:
            import tomllib
        except ImportError:  # pragma: no cover
            import tomli as tomllib
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001 - bozuk dosya ALICE'i durdurmasın
        return {}
    return data.get("hooks", data) if isinstance(data, dict) else {}


def hooks_path() -> Path:
    from alice.config import alice_home

    return alice_home() / "hooks.toml"


def _stamp() -> tuple[float, int]:
    try:
        st = hooks_path().stat()
        return st.st_mtime, st.st_size
    except OSError:
        return 0.0, 0


def _parse(data: dict[str, Any]) -> list[Hook]:
    hooks = []
    for event in EVENTS:
        for h in data.get(event, []) or []:
            if isinstance(h, dict) and str(h.get("command", "")).strip():
                hooks.append(Hook(event, str(h["command"]), str(h.get("matcher", "") or ""),
                                  float(h.get("timeout", 30))))
    return hooks


def file_hooks() -> list[Hook]:
    """Yalnız ~/.alice/hooks.toml'daki hook'lar (arayüzden/sohbetten eklenip silinebilenler)."""
    return _parse(_read_hooks_file())


def _q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)  # TOML temel dizgisi JSON kaçışlarıyla uyumlu


def _save(hooks: list[Hook]) -> None:
    lines = ["# ALICE hook'ları (Otomasyon sayfasından ya da sohbetten yönetilir). Biçim: alice/hooks.py", ""]
    for h in hooks:
        lines += [f"[[hooks.{h.event}]]", f"command = {_q(h.command)}"]
        if h.matcher:
            lines.append(f"matcher = {_q(h.matcher)}")
        lines += [f"timeout = {h.timeout:g}", ""]
    p = hooks_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".toml.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(p)


def add_hook(event: str, command: str, matcher: str = "", timeout: float = 30.0) -> Hook:
    if event not in EVENTS:
        raise ValueError(f"bilinmeyen olay: {event} (olaylar: {', '.join(EVENTS)})")
    if not command.strip():
        raise ValueError("komut boş")
    if matcher:
        try:
            re.compile(matcher)
        except re.error as e:
            raise ValueError(f"matcher geçerli bir regex değil: {e}") from e
    h = Hook(event, command.strip(), matcher.strip(), max(1.0, min(float(timeout), 600.0)))
    _save(file_hooks() + [h])
    return h


def remove_hook(index: int) -> Hook:
    rows = file_hooks()
    if not 0 <= index < len(rows):
        raise ValueError(f"böyle bir hook yok: {index}")
    gone = rows.pop(index)
    _save(rows)
    return gone


class Hooks:
    def __init__(self, hooks: list[Hook], cwd: Path | None = None):
        self.hooks = hooks
        self.cwd = cwd
        self._cfg_hooks: list[Hook] | None = None  # from_config ile kurulduysa: hooks.toml değişince yenilenir
        self._stamp = (0.0, 0)

    @classmethod
    def from_config(cls, cfg, cwd: Path | None = None) -> "Hooks":
        self = cls([], cwd)
        self._cfg_hooks = _parse(dict(((getattr(cfg, "data", None) or {}).get("hooks")) or {}))
        self._reload()
        return self

    def _reload(self) -> None:
        if self._cfg_hooks is None:
            return
        stamp = _stamp()
        if stamp == self._stamp and self.hooks:
            return
        self._stamp = stamp
        self.hooks = self._cfg_hooks + file_hooks()

    def has(self, event: str) -> bool:
        self._reload()  # arayüzden ya da sohbetten eklenen hook açık sohbette de hemen geçerli olsun
        return any(h.event == event for h in self.hooks)

    def run(self, event: str, payload: dict[str, Any], tool: str = "") -> list[HookResult]:
        self._reload()
        out = []
        for h in self.hooks:
            if h.event != event or (tool and not h.matches(tool)):
                continue
            out.append(self._run_one(h, payload, tool))
            if out[-1].blocked and event in ("PreToolUse", "UserPromptSubmit"):
                break  # engellendi: sonraki hook'lara gerek yok
        return out

    def _run_one(self, h: Hook, payload: dict[str, Any], tool: str) -> HookResult:
        from alice.permissions import primary_arg

        args = payload.get("args") or {}
        env = {**os.environ, "ALICE_EVENT": h.event, "ALICE_TOOL": tool,
               "ALICE_TOOL_PATH": primary_arg(args) if isinstance(args, dict) else "",
               "ALICE_CWD": str(self.cwd or Path.cwd()),
               # Çıktı UTF-8 okunuyor; Windows'ta Python betikleri boruya cp1254 yazıyordu ("kullan�c�").
               "PYTHONIOENCODING": os.environ.get("PYTHONIOENCODING", "utf-8")}
        data = json.dumps({"event": h.event, "tool": tool, "cwd": str(self.cwd or ""), **payload},
                          ensure_ascii=False, default=str)
        try:
            p = subprocess.run(h.command, shell=True, input=data, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=h.timeout, cwd=str(self.cwd) if self.cwd else None, env=env,
                               creationflags=0x08000000 if sys.platform == "win32" else 0)
            return HookResult(h, p.returncode, (p.stdout or "").strip()[:4000], (p.stderr or "").strip()[:4000])
        except subprocess.TimeoutExpired:
            return HookResult(h, 1, "", f"hook {h.timeout:.0f} sn içinde bitmedi: {h.command}")
        except OSError as e:
            return HookResult(h, 1, "", f"hook çalıştırılamadı: {e}")


def decision(results: list[HookResult]) -> dict[str, Any]:
    """Başarılı (çıkış 0) hook'ların stdout'undaki JSON kararı birleştir (sonraki hook öncekini ezer):
      updatedInput        PreToolUse: aracın argümanlarını bununla değiştir (nesne)
      permissionDecision  PreToolUse: "allow" (sormadan çalıştır; yasak kuralları yine geçerli), "deny", "ask"
      reason              karar gerekçesi (deny'da modele gider)
      additionalContext   araç sonucuna eklenecek bağlam metni
    JSON olmayan stdout yok sayılır (eski hook'lar aynen çalışır)."""
    out: dict[str, Any] = {}
    notes: list[str] = []
    for r in results:
        if r.code != 0 or not r.stdout.lstrip().startswith("{"):
            continue
        try:
            data = json.loads(r.stdout)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        data = data.get("hookSpecificOutput", data) if isinstance(data.get("hookSpecificOutput"), dict) else data
        if isinstance(data.get("updatedInput"), dict):
            out["updatedInput"] = data["updatedInput"]
        if data.get("permissionDecision") in ("allow", "deny", "ask"):
            out["permissionDecision"] = data["permissionDecision"]
            out["reason"] = str(data.get("permissionDecisionReason") or data.get("reason") or "")[:1000]
        if data.get("additionalContext"):
            notes.append(str(data["additionalContext"])[:2000])
    if notes:
        out["additionalContext"] = "\n".join(notes)
    return out


def feedback(results: list[HookResult]) -> str:
    """Çıkış kodu 2 olan hook'ların stderr'i (modele gidecek metin)."""
    return "\n".join(r.stderr or r.stdout or f"hook engelledi: {r.hook.command}" for r in results if r.blocked)


def context(results: list[HookResult]) -> str:
    """Başarılı hook'ların stdout'u (mesaja eklenecek bağlam)."""
    return "\n".join(r.stdout for r in results if r.code == 0 and r.stdout)
