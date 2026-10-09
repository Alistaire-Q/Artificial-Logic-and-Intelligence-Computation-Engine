from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from alice.tools.base import WRITE, Tool, ToolContext, ToolError, ToolResult, make_diff, obj

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
             "dist", "build", ".idea", ".vs", "bin", "obj", "Library", "Temp", ".gradle", "AppData",
             "site-packages"}
# Dosya araması sınırları: ev klasörü gibi büyük bir yerde aranınca ajan dakikalarca takılmasın.
SEARCH_SECONDS = 20.0
SEARCH_MAX_FILES = 20000
SEARCH_MAX_BYTES = 2_000_000


def _skip_dir(name: str) -> bool:
    return name in SKIP_DIRS or name.startswith(".")


def _limit_note(reason: str) -> str:
    return (f"[ARAMA YARIDA KESİLDİ: {reason}. Sonuç eksik; 'eşleşme yok' aranan şeyin olmadığı anlamına GELMEZ. "
            "Daha dar bir klasör (path) ya da dosya filtresi (glob) vererek tekrar ara.]\n")


def _with_note(body: str, note: str) -> ToolResult:
    # Kesilen aramada notu başa koy ki zayıf modeller "eşleşme yok"u kesin sonuç sanmasın.
    return ToolResult(note + body, is_error=bool(note))


def _is_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return b"\0" in f.read(4096)
    except OSError:
        return True


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8", "utf-8-sig", "cp1254", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def snapshot(ctx: ToolContext, p: Path) -> None:
    """Geri sarma için: bu turda dosyaya ilk dokunulmadan önceki halini sakla."""
    cp = ctx.state.get("checkpoint")
    if cp is None:
        return
    key = str(p)
    if key not in cp:
        try:
            cp[key] = p.read_bytes() if p.exists() else None
        except OSError:
            pass


class ReadFile(Tool):
    name = "read_file"
    parallel_safe = True
    description = ("Dosya oku. Satır numaralarıyla döner. Büyük dosyalarda offset/limit kullan. "
                   "Görseller (png/jpg) görüntü olarak döner.")
    parameters = obj({
        "path": {"type": "string"},
        "offset": {"type": "integer", "description": "1'den başlayan ilk satır"},
        "limit": {"type": "integer", "description": "okunacak satır sayısı (varsayılan 2000)"},
    }, ["path"])

    def summary(self, args: dict[str, Any]) -> str:
        extra = f" (satır {args['offset']}+)" if args.get("offset") else ""
        return f"{args.get('path', '')}{extra}"

    def run(self, ctx: ToolContext, path: str, offset: int = 1, limit: int = 2000) -> ToolResult:
        p = ctx.resolve(path)
        if not p.exists():
            raise ToolError(f"dosya yok: {p}")
        if p.is_dir():
            raise ToolError(f"{p} bir klasör, list_dir kullan")
        if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"):
            from alice.tools.imaging import file_to_png_b64
            return ToolResult(f"görüntü: {p}", images=[file_to_png_b64(p, ctx.config.agent.get("screenshot_max_width", 1366))])
        if _is_binary(p):
            raise ToolError(f"{p} binary bir dosya ({p.stat().st_size} byte)")
        lines = _read_text(p).splitlines()
        start = max(offset, 1)
        chunk = lines[start - 1:start - 1 + limit]
        body = "\n".join(f"{i:>6}\t{line[:2000]}" for i, line in enumerate(chunk, start))
        more = len(lines) - (start - 1 + len(chunk))
        if more > 0:
            body += f"\n... ({more} satır daha, offset={start + len(chunk)} ile devam et)"
        return ToolResult(body or "(boş dosya)")


class WriteFile(Tool):
    name = "write_file"
    description = "Dosyayı verilen içerikle oluştur ya da tamamen üzerine yaz. Klasörler otomatik açılır."
    parameters = obj({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"])
    risk = WRITE

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('path')} ({len(args.get('content', ''))} karakter)"

    def run(self, ctx: ToolContext, path: str, content: str) -> ToolResult:
        p = ctx.resolve(path)
        snapshot(ctx, p)
        p.parent.mkdir(parents=True, exist_ok=True)
        existed = p.exists()
        before = _read_text(p) if existed and not _is_binary(p) else ""
        p.write_text(content, encoding="utf-8", newline="")
        return ToolResult(f"{'güncellendi' if existed else 'oluşturuldu'}: {p} ({len(content.splitlines())} satır)",
                          diff=make_diff(path, before, content))


def _apply_edit(text: str, old: str, new: str, replace_all: bool, where: str = "") -> tuple[str, int]:
    """Bir değişikliği bellekteki metne uygula. Bulunamazsa ya da belirsizse ToolError."""
    if old == new:
        raise ToolError(f"{where}old_string ve new_string aynı")
    if not old:
        raise ToolError(f"{where}old_string boş olamaz")
    count = text.count(old)
    if count == 0 and "\r\n" in text:
        # CRLF dosyalarda modelin LF göndermesi çok yaygın.
        old_crlf = old.replace("\r\n", "\n").replace("\n", "\r\n")
        if old_crlf in text:
            old, new = old_crlf, new.replace("\r\n", "\n").replace("\n", "\r\n")
            count = text.count(old)
    if count == 0:
        raise ToolError(f"{where}old_string dosyada bulunamadı. Dosyayı read_file ile tekrar oku ve birebir kopyala.")
    if count > 1 and not replace_all:
        raise ToolError(f"{where}old_string {count} kez geçiyor; bağlam ekle ya da replace_all=true kullan")
    if replace_all:
        return text.replace(old, new), count
    return text.replace(old, new, 1), 1


class EditFile(Tool):
    name = "edit_file"
    description = ("Dosyada birebir metin değiştir. old_string dosyada tam olarak (boşluklar dahil) ve "
                   "tek bir kez geçmeli; birden fazla geçiyorsa daha fazla bağlam ekle ya da replace_all=true ver. "
                   "Aynı dosyada birden fazla değişiklik için multi_edit kullan.")
    parameters = obj({
        "path": {"type": "string"},
        "old_string": {"type": "string"},
        "new_string": {"type": "string"},
        "replace_all": {"type": "boolean"},
    }, ["path", "old_string", "new_string"])
    risk = WRITE

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("path"))

    def run(self, ctx: ToolContext, path: str, old_string: str, new_string: str, replace_all: bool = False) -> ToolResult:
        p = ctx.resolve(path)
        if not p.is_file():
            raise ToolError(f"dosya yok: {p}")
        before = _read_text(p)
        text, n = _apply_edit(before, old_string, new_string, replace_all)
        snapshot(ctx, p)
        p.write_text(text, encoding="utf-8", newline="")
        return ToolResult(f"düzenlendi: {p} ({n} değişiklik)",
                          diff=make_diff(path, before.replace("\r\n", "\n"), text.replace("\r\n", "\n")))


class MultiEdit(Tool):
    name = "multi_edit"
    description = ("Tek dosyada birden fazla değişikliği tek seferde yap. edits sırayla uygulanır (her biri bir öncekinin "
                   "sonucuna); biri bile başarısız olursa dosyaya HİÇBİR şey yazılmaz. Her edit edit_file kurallarına "
                   "uyar: old_string birebir ve tek olmalı ya da replace_all=true. Yeni dosya için write_file kullan.")
    parameters = obj({
        "path": {"type": "string"},
        "edits": {"type": "array", "minItems": 1, "items": obj({
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean"},
        }, ["old_string", "new_string"])},
    }, ["path", "edits"])
    risk = WRITE

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('path')} ({len(args.get('edits') or [])} değişiklik)"

    def run(self, ctx: ToolContext, path: str, edits: list[dict[str, Any]]) -> ToolResult:
        p = ctx.resolve(path)
        if not p.is_file():
            raise ToolError(f"dosya yok: {p}")
        if not edits:
            raise ToolError("edits boş")
        before = _read_text(p)
        text = before
        total = 0
        for i, e in enumerate(edits, 1):
            if not isinstance(e, dict) or "old_string" not in e or "new_string" not in e:
                raise ToolError(f"edit {i}: old_string ve new_string gerekli")
            text, n = _apply_edit(text, str(e["old_string"]), str(e["new_string"]), bool(e.get("replace_all")),
                                  where=f"edit {i}/{len(edits)}: ")
            total += n
        snapshot(ctx, p)
        p.write_text(text, encoding="utf-8", newline="")
        return ToolResult(f"düzenlendi: {p} ({len(edits)} edit, {total} değişiklik)",
                          diff=make_diff(path, before.replace("\r\n", "\n"), text.replace("\r\n", "\n")))


class ListDir(Tool):
    name = "list_dir"
    parallel_safe = True
    description = "Klasör içeriğini listele (boyut ve tür ile)."
    parameters = obj({"path": {"type": "string", "description": "varsayılan: çalışma klasörü"}})

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("path") or ".")

    def run(self, ctx: ToolContext, path: str = ".") -> ToolResult:
        p = ctx.resolve(path)
        if not p.is_dir():
            raise ToolError(f"klasör yok: {p}")
        rows = []
        for e in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:500]:
            try:
                rows.append(f"{e.name}/" if e.is_dir() else f"{e.name}  ({e.stat().st_size} B)")
            except OSError:
                rows.append(f"{e.name}  (erişilemedi)")
        return ToolResult(f"{p}\n" + "\n".join(rows) if rows else f"{p} boş")


class Glob(Tool):
    name = "glob"
    parallel_safe = True
    description = "Desene uyan dosyaları bul, ör. '**/*.py' ya da 'src/**/*.cs'. Son değiştirilen önce."
    parameters = obj({"pattern": {"type": "string"}, "path": {"type": "string"}}, ["pattern"])

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("pattern", ""))

    def run(self, ctx: ToolContext, pattern: str, path: str = ".") -> ToolResult:
        import time

        base = ctx.resolve(path)
        hits = []
        note = ""
        deadline = time.monotonic() + SEARCH_SECONDS
        for n, p in enumerate(base.glob(pattern)):
            if n % 200 == 0:
                if ctx.cancelled():
                    note = _limit_note("kullanıcı durdurdu")
                    break
                if time.monotonic() > deadline:
                    note = _limit_note(f"{SEARCH_SECONDS:.0f} sn doldu")
                    break
            if any(_skip_dir(part) for part in p.relative_to(base).parts[:-1]):
                continue
            hits.append(p)
            if len(hits) >= 2000:
                break
        hits.sort(key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True)
        out = [str(h.relative_to(base)) for h in hits[:300]]
        return _with_note("\n".join(out) if out else "eşleşme yok", note)


class Grep(Tool):
    name = "grep"
    parallel_safe = True
    description = "Dosya içeriklerinde regex ara. ripgrep varsa onu kullanır."
    parameters = obj({
        "pattern": {"type": "string"},
        "path": {"type": "string"},
        "glob": {"type": "string", "description": "dosya filtresi, ör. *.py"},
        "ignore_case": {"type": "boolean"},
        "max_results": {"type": "integer"},
    }, ["pattern"])

    def summary(self, args: dict[str, Any]) -> str:
        where = f" ({args['glob']})" if args.get("glob") else ""
        return f"{args.get('pattern', '')}{where}"

    def run(self, ctx: ToolContext, pattern: str, path: str = ".", glob: str | None = None,
            ignore_case: bool = False, max_results: int = 200) -> ToolResult:
        base = ctx.resolve(path)
        rg = shutil.which("rg")
        if rg:
            cmd = [rg, "-n", "--no-heading", "--max-columns", "300", "-m", "50"]
            if ignore_case:
                cmd.append("-i")
            if glob:
                cmd += ["-g", glob]
            # Göreli yollar: hem okunaklı hem modele daha az token.
            cmd += ["-e", pattern, "." if base.is_dir() else str(base)]
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                               cwd=base if base.is_dir() else None)
            lines = r.stdout.splitlines()[:max_results]
            return ToolResult("\n".join(lines) if lines else "eşleşme yok")
        try:
            rx = re.compile(pattern, re.I if ignore_case else 0)
        except re.error as e:
            raise ToolError(f"geçersiz regex: {e}") from e
        import time

        out: list[str] = []
        note = ""
        deadline = time.monotonic() + SEARCH_SECONDS
        files = [base] if base.is_file() else self._walk(base)
        for n, f in enumerate(files):
            if n % 200 == 0:
                if ctx.cancelled():
                    note = _limit_note("kullanıcı durdurdu")
                    break
                if time.monotonic() > deadline:
                    note = _limit_note(f"{SEARCH_SECONDS:.0f} sn doldu")
                    break
            if n >= SEARCH_MAX_FILES:
                note = _limit_note(f"{SEARCH_MAX_FILES} dosyaya bakıldı")
                break
            if glob and not fnmatch.fnmatch(f.name, glob):
                continue
            try:
                if f.stat().st_size > SEARCH_MAX_BYTES:
                    continue
            except OSError:
                continue
            if _is_binary(f):
                continue
            try:
                for i, line in enumerate(_read_text(f).splitlines(), 1):
                    if rx.search(line):
                        shown = f.relative_to(base) if base.is_dir() else f
                        out.append(f"{shown}:{i}:{line[:300]}")
                        if len(out) >= max_results:
                            return ToolResult("\n".join(out))
            except OSError:
                continue
        return _with_note("\n".join(out) if out else "eşleşme yok", note)

    @staticmethod
    def _walk(base: Path):
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not _skip_dir(d)]
            for name in files:
                yield Path(root) / name


TOOLS = [ReadFile(), WriteFile(), EditFile(), MultiEdit(), ListDir(), Glob(), Grep()]
