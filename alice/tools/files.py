from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from alice.tools.base import WRITE, Tool, ToolContext, ToolError, ToolResult, obj

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
             "dist", "build", ".idea", ".vs", "bin", "obj", "Library", "Temp", ".gradle"}


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


class ReadFile(Tool):
    name = "read_file"
    description = ("Dosya oku. Satır numaralarıyla döner. Büyük dosyalarda offset/limit kullan. "
                   "Görseller (png/jpg) görüntü olarak döner.")
    parameters = obj({
        "path": {"type": "string"},
        "offset": {"type": "integer", "description": "1'den başlayan ilk satır"},
        "limit": {"type": "integer", "description": "okunacak satır sayısı (varsayılan 2000)"},
    }, ["path"])

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
        p.parent.mkdir(parents=True, exist_ok=True)
        existed = p.exists()
        p.write_text(content, encoding="utf-8", newline="")
        return ToolResult(f"{'güncellendi' if existed else 'oluşturuldu'}: {p} ({len(content.splitlines())} satır)")


class EditFile(Tool):
    name = "edit_file"
    description = ("Dosyada birebir metin değiştir. old_string dosyada tam olarak (boşluklar dahil) ve "
                   "tek bir kez geçmeli; birden fazla geçiyorsa daha fazla bağlam ekle ya da replace_all=true ver.")
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
        text = _read_text(p)
        if old_string == new_string:
            raise ToolError("old_string ve new_string aynı")
        count = text.count(old_string)
        if count == 0:
            # CRLF dosyalarda modelin LF göndermesi çok yaygın.
            if "\r\n" in text and old_string.replace("\r\n", "\n").replace("\n", "\r\n") in text:
                old_string = old_string.replace("\r\n", "\n").replace("\n", "\r\n")
                new_string = new_string.replace("\r\n", "\n").replace("\n", "\r\n")
                count = text.count(old_string)
        if count == 0:
            raise ToolError("old_string dosyada bulunamadı. Dosyayı read_file ile tekrar oku ve birebir kopyala.")
        if count > 1 and not replace_all:
            raise ToolError(f"old_string {count} kez geçiyor; bağlam ekle ya da replace_all=true kullan")
        text = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
        p.write_text(text, encoding="utf-8", newline="")
        return ToolResult(f"düzenlendi: {p} ({count if replace_all else 1} değişiklik)")


class ListDir(Tool):
    name = "list_dir"
    description = "Klasör içeriğini listele (boyut ve tür ile)."
    parameters = obj({"path": {"type": "string", "description": "varsayılan: çalışma klasörü"}})

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
    description = "Desene uyan dosyaları bul, ör. '**/*.py' ya da 'src/**/*.cs'. Son değiştirilen önce."
    parameters = obj({"pattern": {"type": "string"}, "path": {"type": "string"}}, ["pattern"])

    def run(self, ctx: ToolContext, pattern: str, path: str = ".") -> ToolResult:
        base = ctx.resolve(path)
        hits = []
        for p in base.glob(pattern):
            if any(part in SKIP_DIRS for part in p.relative_to(base).parts):
                continue
            hits.append(p)
            if len(hits) >= 2000:
                break
        hits.sort(key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True)
        out = [str(h.relative_to(base)) for h in hits[:300]]
        return ToolResult("\n".join(out) if out else "eşleşme yok")


class Grep(Tool):
    name = "grep"
    description = "Dosya içeriklerinde regex ara. ripgrep varsa onu kullanır."
    parameters = obj({
        "pattern": {"type": "string"},
        "path": {"type": "string"},
        "glob": {"type": "string", "description": "dosya filtresi, ör. *.py"},
        "ignore_case": {"type": "boolean"},
        "max_results": {"type": "integer"},
    }, ["pattern"])

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
            cmd += ["-e", pattern, str(base)]
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
            lines = r.stdout.splitlines()[:max_results]
            return ToolResult("\n".join(lines) if lines else "eşleşme yok")
        try:
            rx = re.compile(pattern, re.I if ignore_case else 0)
        except re.error as e:
            raise ToolError(f"geçersiz regex: {e}") from e
        out: list[str] = []
        files = [base] if base.is_file() else self._walk(base)
        for f in files:
            if glob and not fnmatch.fnmatch(f.name, glob):
                continue
            if _is_binary(f):
                continue
            try:
                for i, line in enumerate(_read_text(f).splitlines(), 1):
                    if rx.search(line):
                        out.append(f"{f}:{i}:{line[:300]}")
                        if len(out) >= max_results:
                            return ToolResult("\n".join(out))
            except OSError:
                continue
        return ToolResult("\n".join(out) if out else "eşleşme yok")

    @staticmethod
    def _walk(base: Path):
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                yield Path(root) / name


TOOLS = [ReadFile(), WriteFile(), EditFile(), ListDir(), Glob(), Grep()]
