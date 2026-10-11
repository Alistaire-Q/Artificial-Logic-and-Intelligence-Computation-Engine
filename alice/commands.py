"""Slash komutları: yerleşikler + kullanıcının yazdığı özel komutlar.

Özel komut = bir Markdown dosyası. ~/.alice/commands/ (her yerde geçerli) ya da
proje klasöründe .alice/commands/ (Claude Code uyumluluğu için .claude/commands/ da okunur).

  ~/.alice/commands/review.md:
      ---
      description: Değişiklikleri incele
      ---
      Bu projedeki son değişiklikleri incele, hataları listele. Odak: $ARGUMENTS

`/review performans` yazınca dosyanın gövdesi $ARGUMENTS yerine "performans" konmuş
halde ALICE'e normal mesaj olarak gider.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from alice.config import alice_home

BUILTINS: dict[str, str] = {
    "help": "Komutları göster",
    "clear": "Konuşmayı sıfırla (yeni sohbet)",
    "compact": "Context'i şimdi özetle",
    "mode": "İzin modu: plan | ask | auto | yolo",
    "plan": "Plan moduna geç (sadece okur, plan yazar)",
    "model": "Rol zincirleri / modeli sabitle",
    "think": "Düşünme seviyesi: off | low | medium | high | default (rol verilebilir: /think brain off)",
    "rewind": "Bir önceki mesaja geri sar (dosyalar dahil)",
    "usage": "Bugünkü istek ve token kullanımı",
    "cwd": "Çalışma klasörünü göster/değiştir",
    "tools": "Araç listesi",
    "mcp": "Bağlı MCP sunucuları ve araçları",
    "init": "Bu proje için ALICE.md talimat dosyası oluştur",
    "memory": "Kalıcı hafızayı göster",
    "kurul": "Bir konuyu inceleme kuruluna sor: eleştirmen, savunucu, kontrolcü ve uzmanlar tartışıp oylar",
}

INIT_PROMPT = (
    "Bu klasördeki projeyi incele (yapı, diller, build/test komutları, önemli dosyalar, kod stili) ve proje kökünde "
    "ALICE.md adında kısa ve yoğun bir talimat dosyası oluştur. Gelecekte bu projede çalışacak bir ajana lazım olacak "
    "her şeyi yaz: nasıl çalıştırılır, nasıl test edilir, klasör yapısı, dikkat edilecek kurallar. Varsa README'yi ve "
    "mevcut ALICE.md/CLAUDE.md/AGENTS.md dosyalarını temel al, tekrar etme."
)


def council_prompt(args: str) -> str:
    """/kurul <konu>: konuyu inceleme kuruluna sorduran mesaj."""
    topic = args.strip() or "Bu konuşmada en son konuştuğumuz konu ya da değişiklik"
    return (f"Şunu inceleme kuruluna sor (council aracı; konu kritikse critical=true): {topic}\n\nKurulun kararını, "
            "doğrulanmış bulguları ve kimin neyi neden savunduğunu bana kısaca özetle.")


@dataclass
class CustomCommand:
    name: str
    description: str
    body: str
    path: Path

    def expand(self, args: str) -> str:
        if "$ARGUMENTS" in self.body:
            return self.body.replace("$ARGUMENTS", args)
        return self.body + (f"\n\n{args}" if args else "")


def _parse(path: Path) -> CustomCommand:
    text = path.read_text(encoding="utf-8", errors="replace")
    desc = ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                k, _, v = line.partition(":")
                if k.strip() == "description":
                    desc = v.strip()
            text = text[end + 4:]
    body = text.strip()
    if not desc:
        desc = body.splitlines()[0][:80] if body else ""
    return CustomCommand(path.stem.lower(), desc, body, path)


def load_custom(cwd: Path) -> dict[str, CustomCommand]:
    out: dict[str, CustomCommand] = {}
    # Sonra gelen öncekini ezer: proje komutları kişisel komutlardan önceliklidir.
    for d in (alice_home() / "commands", cwd / ".claude" / "commands", cwd / ".alice" / "commands"):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            try:
                cmd = _parse(f)
            except OSError:
                continue
            if cmd.name not in BUILTINS:
                out[cmd.name] = cmd
    return out


def catalog(cwd: Path) -> list[dict[str, str]]:
    items = [{"name": n, "description": d, "kind": "builtin"} for n, d in BUILTINS.items()]
    items += [{"name": c.name, "description": c.description, "kind": "custom"} for c in load_custom(cwd).values()]
    return items


def expand_custom(text: str, cwd: Path) -> str | None:
    """'/isim argümanlar' özel bir komutsa genişletilmiş mesajı döndür, değilse None."""
    if not text.startswith("/"):
        return None
    name, _, args = text[1:].partition(" ")
    cmd = load_custom(cwd).get(name.lower())
    return cmd.expand(args.strip()) if cmd else None
