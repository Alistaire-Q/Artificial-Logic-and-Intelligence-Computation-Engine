"""apply_patch: Codex'in çok dosyalı yama biçimi. Birden fazla dosyada ekleme/silme/düzenleme/taşımayı tek çağrıda,
hepsi-ya-hiçbiri yapar. OpenAI modelleri bu biçimle eğitildiği için onlarla edit_file'dan daha az hata çıkıyor."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alice.tools.base import WRITE, Tool, ToolContext, ToolError, ToolResult, make_diff, obj
from alice.tools.files import FileOps, _read_text, mark_read, require_read, snapshot

BEGIN, END = "*** Begin Patch", "*** End Patch"
ADD, DELETE, UPDATE, MOVE, EOF_ = "*** Add File: ", "*** Delete File: ", "*** Update File: ", "*** Move to: ", \
    "*** End of File"


@dataclass
class Chunk:
    anchor: str = ""  # "@@ def foo():" -> "def foo():"
    old: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    at_eof: bool = False


@dataclass
class FileOp:
    kind: str  # add | delete | update
    path: str
    move_to: str = ""
    content: list[str] = field(default_factory=list)  # add
    chunks: list[Chunk] = field(default_factory=list)  # update


def parse_patch(text: str) -> list[FileOp]:
    lines = text.replace("\r\n", "\n").strip("\n").split("\n")
    # Modeller bazen yamayı ``` ya da heredoc içine koyuyor; çevreyi at.
    while lines and not lines[0].strip().startswith(BEGIN):
        lines.pop(0)
    while lines and not lines[-1].strip().startswith(END):
        lines.pop()
    if len(lines) < 2:
        raise ToolError(f"yama '{BEGIN}' ile başlayıp '{END}' ile bitmeli")
    ops: list[FileOp] = []
    i = 1
    while i < len(lines) - 1:
        line = lines[i]
        if line.startswith(ADD):
            op = FileOp("add", line[len(ADD):].strip())
            i += 1
            while i < len(lines) - 1 and not lines[i].startswith("*** "):
                if not lines[i].startswith("+"):
                    raise ToolError(f"satır {i + 1}: Add File içindeki her satır '+' ile başlamalı: {lines[i][:80]!r}")
                op.content.append(lines[i][1:])
                i += 1
            ops.append(op)
        elif line.startswith(DELETE):
            ops.append(FileOp("delete", line[len(DELETE):].strip()))
            i += 1
        elif line.startswith(UPDATE):
            op = FileOp("update", line[len(UPDATE):].strip())
            i += 1
            if i < len(lines) - 1 and lines[i].startswith(MOVE):
                op.move_to = lines[i][len(MOVE):].strip()
                i += 1
            chunk: Chunk | None = None
            while i < len(lines) - 1 and (not lines[i].startswith("*** ") or lines[i].startswith(EOF_)):
                cur = lines[i]
                if cur.startswith(EOF_):
                    if chunk is not None:
                        chunk.at_eof = True
                elif cur.startswith("@@"):
                    chunk = Chunk(anchor=cur[2:].strip())
                    op.chunks.append(chunk)
                else:
                    if chunk is None:
                        chunk = Chunk()
                        op.chunks.append(chunk)
                    tag, body = (cur[:1], cur[1:]) if cur else (" ", "")
                    if tag == " ":
                        chunk.old.append(body)
                        chunk.new.append(body)
                    elif tag == "-":
                        chunk.old.append(body)
                    elif tag == "+":
                        chunk.new.append(body)
                    else:
                        raise ToolError(f"satır {i + 1}: Update File içindeki satırlar ' ', '-' ya da '+' ile "
                                        f"başlamalı: {cur[:80]!r}")
                i += 1
            if not op.chunks and not op.move_to:
                raise ToolError(f"{op.path}: Update File boş")
            ops.append(op)
        elif not line.strip():
            i += 1
        else:
            raise ToolError(f"satır {i + 1}: beklenmeyen satır {line[:80]!r}; '{ADD}', '{UPDATE}' ya da '{DELETE}' "
                            "bekleniyordu")
    if not ops:
        raise ToolError("yamada hiç dosya işlemi yok")
    return ops


def _find(lines: list[str], seq: list[str], start: int, at_eof: bool) -> int:
    """seq'i lines içinde start'tan itibaren bul; önce birebir, sonra sondaki boşluklar, sonra baştaki/sondaki
    boşluklar yok sayılarak (Codex'in seek_sequence'ı gibi). Bulunamazsa -1."""
    if not seq:
        return len(lines) if at_eof else start
    n = len(seq)
    for norm in (lambda s: s, str.rstrip, str.strip):
        target = [norm(s) for s in seq]
        rng = [len(lines) - n] if at_eof else range(start, len(lines) - n + 1)
        for k in rng:
            if k >= 0 and [norm(s) for s in lines[k:k + n]] == target:
                return k
    return -1


def apply_chunks(text: str, chunks: list[Chunk], path: str) -> str:
    crlf = "\r\n" in text
    trailing_nl = text.endswith("\n") or not text
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    cursor = 0
    for n, ch in enumerate(chunks, 1):
        if ch.anchor:
            k = _find(lines, [ch.anchor], cursor, False)
            if k < 0:
                raise ToolError(f"{path}: parça {n}: '@@ {ch.anchor}' satırı bulunamadı")
            cursor = k + 1
        k = _find(lines, ch.old, cursor, ch.at_eof)
        if k < 0 and ch.old and not ch.old[-1].strip():  # sondaki boş bağlam satırı sık sık fazladan gelir
            k = _find(lines, ch.old[:-1], cursor, ch.at_eof)
            if k >= 0:
                ch = Chunk(ch.anchor, ch.old[:-1], ch.new[:-1] if ch.new and not ch.new[-1].strip() else ch.new)
        if k < 0:
            preview = "\n".join(ch.old[:6])
            raise ToolError(f"{path}: parça {n}: değiştirilecek satırlar dosyada bulunamadı. Dosyayı read_file ile "
                            f"tekrar oku; bağlam satırları birebir olmalı. Aranan:\n{preview}")
        lines[k:k + len(ch.old)] = ch.new
        cursor = k + len(ch.new)
    out = "\n".join(lines) + ("\n" if trailing_nl and lines else "")
    return out.replace("\n", "\r\n") if crlf else out


class ApplyPatch(Tool):
    name = "apply_patch"
    description = (
        "Bir ya da birden fazla dosyayı tek yamayla değiştirir: dosya ekleme, silme, düzenleme, taşıma. Hepsi-ya-hiçbiri: "
        "bir parça uymazsa hiçbir dosyaya yazılmaz. Birden çok dosyaya dokunan değişiklikler için edit_file'ı tek tek "
        "çağırmak yerine bunu kullan. Biçim:\n"
        "*** Begin Patch\n*** Update File: src/a.py\n@@ def foo():\n bağlam satırı\n-eski satır\n+yeni satır\n"
        "*** Add File: src/b.py\n+yeni dosyanın satırı\n*** Delete File: eski.txt\n*** End Patch\n"
        "Update içinde değişikliğin önüne ve arkasına 3 satır bağlam koy (' ' ile); '@@ <satır>' yeri daraltır. "
        "Taşımak için Update File'ın hemen altına '*** Move to: yeni/yol'. Update edilecek dosyayı önce read_file ile "
        "oku (edit_file gibi); Delete File dosyayı ALICE çöpüne taşır.")
    parameters = obj({"patch": {"type": "string", "description": "'*** Begin Patch' ile başlayıp '*** End Patch' ile "
                                "biten yama metninin tamamı. Yollar çalışma klasörüne göre."}}, ["patch"])
    risk = WRITE

    def summary(self, args: dict[str, Any]) -> str:
        try:
            ops = parse_patch(str(args.get("patch") or ""))
        except ToolError:
            return "(geçersiz yama)"
        return ", ".join(f"{o.kind} {o.path}" + (f" -> {o.move_to}" if o.move_to else "") for o in ops)[:160]

    def run(self, ctx: ToolContext, patch: str) -> ToolResult:
        ops = parse_patch(patch)
        # Önce her şeyi bellekte hazırla; hata olursa diske hiçbir şey yazılmadan dön.
        # (yol, yeni içerik ya da None=kaldır, gösterilen ad, çöpe mi). Delete File çöpe gider (file_ops delete
        # gibi, geri alınabilir); Move to'nun kaynağı ise içeriği hedefe yazıldığı için doğrudan kaldırılır.
        writes: list[tuple[Path, str | None, str, bool]] = []
        diffs: list[str] = []
        report: list[str] = []
        for op in ops:
            p = ctx.resolve(op.path)
            if op.kind == "add":
                if p.exists():
                    raise ToolError(f"{op.path} zaten var; değiştirmek için '*** Update File:' kullan")
                body = "\n".join(op.content) + ("\n" if op.content else "")
                writes.append((p, body, op.path, False))
                diffs.append(make_diff(op.path, "", body))
                report.append(f"A {op.path}")
            elif op.kind == "delete":
                if not p.is_file():
                    raise ToolError(f"silinecek dosya yok: {op.path}")
                writes.append((p, None, op.path, True))
                diffs.append(make_diff(op.path, _read_text(p), ""))
                report.append(f"D {op.path}")
            else:
                if not p.is_file():
                    raise ToolError(f"düzenlenecek dosya yok: {op.path}")
                require_read(ctx, p)  # edit_file ile aynı kural: okunmamış/okunduktan sonra değişmiş dosya düzenlenmez
                before = _read_text(p)
                after = apply_chunks(before, op.chunks, op.path) if op.chunks else before
                if op.move_to:
                    dest = ctx.resolve(op.move_to)
                    if dest.exists():
                        raise ToolError(f"taşıma hedefi zaten var: {op.move_to}")
                    writes += [(dest, after, op.move_to, False), (p, None, op.path, False)]
                    report.append(f"R {op.path} -> {op.move_to}")
                else:
                    writes.append((p, after, op.path, False))
                    report.append(f"M {op.path}")
                diffs.append(make_diff(op.move_to or op.path, before.replace("\r\n", "\n"),
                                       after.replace("\r\n", "\n")))
        for p, _body, _, _ in writes:
            snapshot(ctx, p)
        trashed: list[str] = []
        for p, body, name, to_trash in writes:
            if body is None:
                if to_trash:
                    trashed.append(f"{name} -> {FileOps._trash(ctx, p)}")
                else:
                    p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(body, encoding="utf-8", newline="")
                mark_read(ctx, p)  # kendi yazdığı hâli biliyor: ardından tekrar düzenleyebilsin
        text = "yama uygulandı:\n" + "\n".join(report)
        if trashed:
            text += "\nsilinenler ALICE çöpünde:\n" + "\n".join(trashed)
        return ToolResult(text, diff="\n".join(d for d in diffs if d))


TOOLS = [ApplyPatch()]
