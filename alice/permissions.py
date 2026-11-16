"""İzin modları.

plan : sadece okuma/araştırma; dosya değiştiren, komut çalıştıran, ekrana dokunan her şey reddedilir
ask  : okuma dışındaki her şey onay ister
auto : tehlikeli komutlar (rm -r, format, registry, shutdown...), sistem klasörlerine yazma ve
       başkasına giden mesajlar (send: Discord/WhatsApp/e-posta) onay ister; geri kalan her şey
       (dosya düzenleme, komut, fare/klavye) serbest
yolo : hiçbir şey sormaz

Kalıcı kurallar (Claude Code'daki allow/deny gibi), config.toml ve projede .alice/permissions.toml:
  [permissions]
  allow = ["run_command(git status*)", "run_command(npm test*)", "edit_file(src/*)"]
  ask   = ["run_command(git push*)"]
  deny  = ["run_command(*rm -rf*)", "write_file(*.env)", "mcp__github__delete_*"]
Biçim: "araç" ya da "araç(desen)". Araç adında * olabilir. Desen (büyük/küçük harf duyarsız, fnmatch) aracın asıl
argümanına bakar: command, path, url, target, app, action ya da query (ilk bulunan). deny her modda (yolo dahil)
geçerlidir; ask yolo dışında her zaman sorar; allow sormadan izin verir (plan modunu delmez; tehlikeli bir işe
ancak desenli bir kural izin verir, "run_command" tek başına rm -r'yi serbest bırakmaz; dış içerik okunmuş bir turda
desensiz kural komut çalıştırmayı, proje dışına yazmayı ve mesaj göndermeyi de sormadan geçirmez).
"""

from __future__ import annotations

import fnmatch
import sys
import threading
from pathlib import Path
from typing import Any

from alice.tools.base import DANGER, NETWORK, READ, SEND, WRITE, Tool, ToolContext

MODES = ("plan", "ask", "auto", "yolo")

SYSTEM_DIRS_WIN = [r"c:\windows", r"c:\program files", r"c:\program files (x86)", r"c:\programdata\microsoft"]
SYSTEM_DIRS_POSIX = ["/etc", "/usr", "/bin", "/sbin", "/boot", "/lib", "/lib64", "/var/lib", "/System", "/Library"]


def is_system_path(p: Path) -> bool:
    s = str(p)
    if sys.platform == "win32":
        low = s.lower()
        return any(low == d or low.startswith(d + "\\") for d in SYSTEM_DIRS_WIN)
    return any(s == d or s.startswith(d + "/") for d in SYSTEM_DIRS_POSIX)


def _confirm_outward(ctx: ToolContext) -> bool:
    cfg = getattr(ctx, "config", None)
    agent = getattr(cfg, "agent", None) or {}
    return bool(agent.get("confirm_outward", True)) if isinstance(agent, dict) else True


PRIMARY_ARGS = ("command", "path", "url", "target", "app", "action", "query")
_project_cache: dict[str, tuple[float, dict[str, list[str]]]] = {}


def primary_arg(args: dict[str, Any]) -> str:
    for k in PRIMARY_ARGS:
        v = args.get(k)
        if v not in (None, ""):
            return str(v)
    return ""


def parse_rule(rule: str) -> tuple[str, str | None]:
    rule = rule.strip()
    if rule.endswith(")") and "(" in rule:
        name, spec = rule[:-1].split("(", 1)
        return name.strip(), spec.strip()
    return rule, None


def _path_forms(raw: str, cwd: Path | None) -> list[str]:
    """Yol argümanının kuralla karşılaştırılacak biçimleri: çözülmüş tam yol ve (proje içindeyse) projeye göre yolu.
    Ham metin yalnız ".." içermiyorsa eklenir. Önceden yalnız ham metne bakılıyordu: "edit_file(gizli/*)" yasağı
    "./gizli/a" ya da tam yolla deliniyor, "edit_file(src/*)" izni "src/../../dışarı" yazmaya izin veriyordu; "bu
    projede hep izin ver" ile yazılan göreli kural da tam yolla gelen çağrıda hiç eşleşmiyordu."""
    out = []
    if ".." not in Path(raw).parts:
        out.append(raw)
    if cwd is not None:
        try:
            base = Path(cwd).resolve()
            p = Path(raw).expanduser()
            full = (p if p.is_absolute() else base / p).resolve()
            out.append(str(full))
            if full != base and _inside(full, base):
                out.append(str(full.relative_to(base)))
        except (OSError, ValueError, RuntimeError):
            pass
    return out


def rule_matches(rule: str, tool_name: str, args: dict[str, Any], cwd: Path | None = None) -> bool:
    name, spec = parse_rule(rule)
    if not fnmatch.fnmatchcase(tool_name, name):
        return False
    if spec is None:
        return True
    raw = primary_arg(args)
    is_path = args.get("command") in (None, "") and args.get("path") not in (None, "")  # asıl argüman yol mu
    values = _path_forms(raw, cwd) if is_path else [raw]
    pattern = spec.replace("\\", "/").lower()
    return any(fnmatch.fnmatchcase(v.replace("\\", "/").lower(), pattern) for v in values)


def _trust_path() -> Path:
    from alice.config import alice_home

    return alice_home() / "trusted-permissions.json"


def _digest(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _trusted(path: Path, text: str) -> bool:
    import json

    try:
        data = json.loads(_trust_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get(str(path.resolve())) == _digest(text)


def _trust(path: Path, text: str) -> None:
    """Bu içerik ALICE'in kendisi tarafından (kullanıcının onayıyla) yazıldı: allow kuralları geçerli."""
    import json

    tp = _trust_path()
    try:
        data = json.loads(tp.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[str(path.resolve())] = _digest(text)
    try:
        tp.parent.mkdir(parents=True, exist_ok=True)
        tp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except OSError:
        pass


def project_rules(cwd: Path | None, include_untrusted: bool = False) -> dict[str, list[str]]:
    """<proje>/.alice/permissions.toml ([permissions] başlıklı ya da başlıksız allow/ask/deny).

    Dosya projenin içinde: klonlanan bir repo ya da sandbox'taki bir komut `allow = ["*(*)"]` yazıp bütün onayları
    atlatabiliyordu. Bu yüzden allow kuralları yalnız dosya ALICE'in yazdığı (kullanıcının "bu projede hep izin ver"
    dediği) haliyle duruyorsa geçerli; içeriğin özeti ~/.alice/trusted-permissions.json'da. Elle ya da dışarıdan
    değişen dosyanın allow'ları yok sayılır (include_untrusted ile listelenir); ask ve deny yalnız sıkılaştırdığı
    için her zaman geçerli."""
    if cwd is None:
        return {}
    path = Path(cwd) / ".alice" / "permissions.toml"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    hit = _project_cache.get(str(path))
    if hit and hit[0] == mtime:
        rules, trusted = hit[1], hit[2]
    else:
        try:
            try:
                import tomllib
            except ImportError:  # pragma: no cover
                import tomli as tomllib
            text = path.read_text(encoding="utf-8-sig")
            data = tomllib.loads(text)
        except Exception:  # noqa: BLE001 - bozuk dosya izinleri gevşetmesin: yok say
            text, data = "", {}
        data = data.get("permissions", data) if isinstance(data, dict) else {}
        rules = {k: [str(r) for r in data.get(k, []) or []] for k in ("allow", "ask", "deny")}
        trusted = bool(text) and _trusted(path, text)
        _project_cache[str(path)] = (mtime, rules, trusted)
    if trusted or include_untrusted:
        return rules
    return {**rules, "allow": []}


def project_rules_trusted(cwd: Path | None) -> bool:
    """Projedeki allow kuralları geçerli mi (dosya yoksa ya da ALICE yazdıysa True)."""
    rules = project_rules(cwd, include_untrusted=True)
    return not rules.get("allow") or project_rules(cwd).get("allow") == rules.get("allow")


def _write_project_rules(cwd: Path, rules: dict[str, list[str]]) -> Path:
    import json

    def arr(items: list[str]) -> str:
        return "[" + ", ".join(json.dumps(i, ensure_ascii=False) for i in items) + "]"

    path = Path(cwd) / ".alice" / "permissions.toml"
    text = ("# ALICE izin kuralları (bu proje). Biçim: alice/permissions.py\n"
            "# Elle değiştirilirse allow kuralları ALICE'te tekrar onaylanana kadar yok sayılır.\n[permissions]\n"
            f"allow = {arr(rules.get('allow', []))}\nask = {arr(rules.get('ask', []))}\n"
            f"deny = {arr(rules.get('deny', []))}\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _trust(path, path.read_text(encoding="utf-8-sig"))
    _project_cache.pop(str(path), None)
    return path


def trust_project_rules(cwd: Path) -> bool:
    """Kullanıcı projedeki (elle ya da dışarıdan yazılmış) kuralları onayladı: olduğu gibi geçerli say."""
    rules = project_rules(cwd, include_untrusted=True)
    if not rules:
        return False
    _write_project_rules(cwd, rules)
    return True


def suggest_rule(tool: Tool, args: dict[str, Any], cwd: Path | None = None) -> str:
    """"Bu projede hep izin ver" denince yazılacak kural: dar ama işe yarar. Komutta ilk iki kelime ("git status*"),
    dosya araçlarında dosyanın klasörü, diğerlerinde aracın adı."""
    name = tool.name
    if "command" in args and args.get("command"):
        words = str(args["command"]).split()
        head = " ".join(words[:2]) if len(words) > 1 and not words[1].startswith(("-", "/", "\\", ".")) else words[0]
        return f"{name}({head}*)"
    if args.get("path"):
        p = Path(str(args["path"]))
        try:
            base = cwd.resolve() if cwd else None
            full = (base / p).resolve() if base and not p.is_absolute() else p.resolve()
            rel = full.parent.relative_to(base) if base else full.parent
            folder = str(rel).replace("\\", "/")
        except (ValueError, OSError):
            folder = str(p.parent).replace("\\", "/")
        return f"{name}({'' if folder in ('', '.') else folder.rstrip('/') + '/'}*)"
    return name


def persist_allow(cwd: Path, rule: str) -> Path:
    """Kuralı <proje>/.alice/permissions.toml'un allow listesine ekle (varsa tekrar eklemez)."""
    # Onaylanmamış (dışarıdan gelen) allow kuralları burada da düşer: kullanıcının tek bir onayı onları aklamasın.
    rules = dict(project_rules(cwd)) or {"allow": [], "ask": [], "deny": []}
    for k in ("allow", "ask", "deny"):
        rules.setdefault(k, [])
    if rule not in rules["allow"]:
        rules["allow"] = rules["allow"] + [rule]
    return _write_project_rules(cwd, rules)


HISTORY_MAX = 2000  # satır: izin cevapları kaydı bu kadarla sınırlı
_HISTORY_LOCK = threading.Lock()


def history_path() -> Path:
    from alice.config import alice_home

    return alice_home() / "permission-history.jsonl"


def record_answer(tool: Tool, args: dict[str, Any], cwd: Path | None, risk: str, answer: str) -> None:
    """Sorulan izni ve cevabı kaydet: hep onaylanan işler için "artık sorma" önerisi bundan çıkar."""
    import json
    import time

    row = {"ts": round(time.time()), "tool": tool.name, "rule": suggest_rule(tool, args, cwd), "risk": risk,
           "answer": answer, "cwd": str(cwd or "")}
    p = history_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Aynı anda iki sohbet cevap yazınca oku-değiştir-yaz birinin satırını siliyordu: kilit + geçici dosyadan
    # os.replace (yarım yazılmış dosya okunmaz).
    with _HISTORY_LOCK:
        try:
            lines = p.read_text(encoding="utf-8").splitlines()[-(HISTORY_MAX - 1):]
        except OSError:
            lines = []
        _atomic_write(p, "\n".join(lines + [json.dumps(row, ensure_ascii=False)]) + "\n")


def _atomic_write(path: Path, text: str) -> None:
    import os
    import tempfile
    import time

    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:  # Windows: hedef o an başka bir süreçte açık
                if attempt == 4:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def suggestions(cwd: Path | None = None, min_yes: int = 3) -> list[dict[str, Any]]:
    """Kayıttan öneriler: en az min_yes kez onaylanmış, hiç reddedilmemiş, tehlikeli ya da başkasına giden olmayan,
    henüz izinli olmayan kurallar. cwd verilirse yalnız o projede sorulanlar."""
    import json

    try:
        text = history_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    stats: dict[str, dict[str, Any]] = {}
    for ln in text.splitlines():
        # Bozuk bir satır (yarım yazma, elle düzenleme) ya da "rule"suz kayıt önerilerin tamamını yok etmesin: atla.
        try:
            r = json.loads(ln) if ln.strip() else None
            if not isinstance(r, dict) or not isinstance(r.get("rule"), str) or not r["rule"]:
                continue
            if cwd is not None and r.get("cwd") and Path(str(r["cwd"])) != Path(cwd):
                continue
            ts = int(r.get("ts") or 0)
        except (ValueError, TypeError):
            continue
        s = stats.setdefault(r["rule"], {"rule": r["rule"], "tool": str(r.get("tool") or ""), "yes": 0, "no": 0,
                                         "risky": False, "last": 0})
        s["yes" if r.get("answer") in ("y", "a", "p") else "no"] += 1
        s["risky"] = s["risky"] or r.get("risk") in (DANGER, SEND)
        s["last"] = max(s["last"], ts)
    have = set(project_rules(cwd).get("allow", [])) if cwd is not None else set()
    out = [s for s in stats.values() if s["yes"] >= min_yes and not s["no"] and not s["risky"] and s["rule"] not in have]
    return sorted(out, key=lambda s: (-s["yes"], -s["last"]))


def remove_project_rule(cwd: Path, kind: str, rule: str) -> bool:
    """Projedeki .alice/permissions.toml'dan bir kuralı sil (uygulamadaki Asistan sekmesi)."""
    trusted = project_rules_trusted(cwd)
    rules = dict(project_rules(cwd, include_untrusted=True))
    if kind not in ("allow", "ask", "deny") or rule not in (rules.get(kind) or []):
        return False
    rules[kind] = [r for r in rules[kind] if r != rule]
    if not trusted:
        rules["allow"] = []  # silme işlemi onaylanmamış allow'ları geçerli hale getirmesin
    _write_project_rules(cwd, rules)
    return True


class Permissions:
    def __init__(self, mode: str = "auto", rules: dict[str, list[str]] | None = None):
        if mode not in MODES:
            raise ValueError(f"geçersiz izin modu: {mode}")
        self.before_plan: str | None = None  # plan modundan önceki mod: exit_plan_mode onaydan sonra ona döner
        self._mode = mode
        self.always_allow: set[str] = set()
        self.rules = rules  # None: config.toml [permissions] + projedeki .alice/permissions.toml

    @property
    def mode(self) -> str:
        return self._mode

    @mode.setter
    def mode(self, value: str) -> None:
        if value == "plan" and self._mode != "plan":
            self.before_plan = self._mode
        self._mode = value

    def rules_for(self, ctx: ToolContext) -> dict[str, list[str]]:
        if self.rules is not None:
            return self.rules
        cfg = getattr(ctx, "config", None)
        data = getattr(cfg, "data", None)
        base = (data.get("permissions") if isinstance(data, dict) else None) or {}
        proj = project_rules(getattr(ctx, "cwd", None))
        return {k: [str(r) for r in base.get(k, []) or []] + proj.get(k, []) for k in ("allow", "ask", "deny")}

    @staticmethod
    def _risky_after_untrusted(tool: Tool, args: dict[str, Any], risk: str, ctx: ToolContext) -> bool:
        """Dış içerik okunmuş bir turda onaya bağlanan işler: mesaj göndermek, komut çalıştırmak, çalışma klasörü
        dışına yazmak."""
        if tool.name == "run_command":
            from alice import sandbox

            return not sandbox.usable(ctx, args)  # sandbox'ta çalışacaksa sormaya gerek yok: proje dışına yazamaz
        if risk == SEND or tool.name == "process_kill" or "command" in args:
            return True
        if risk == WRITE:
            # "path" ve yama metnindeki dosyalar (apply_patch'in "path"i yok: önceden hiç bakılmıyordu). Önek
            # karşılaştırması C:\proje2'yi C:\proje'nin içinde sayıyordu; klasör sınırına göre bakılır.
            try:
                root = Path(ctx.cwd).resolve()
                return any(not _inside(ctx.resolve(p), root) for p in _write_targets(args))
            except (OSError, ValueError, AttributeError, TypeError):
                return True
        return False

    def matching_rule(self, tool: Tool, args: dict[str, Any], ctx: ToolContext, kind: str) -> str | None:
        for rule in self.rules_for(ctx).get(kind, []):
            try:
                if rule_matches(rule, tool.name, args, getattr(ctx, "cwd", None)):
                    return rule
            except Exception:  # noqa: BLE001 - garip argüman kuralı bozmasın
                continue
        return None

    def decide(self, tool: Tool, args: dict[str, Any], ctx: ToolContext) -> str:
        """'allow', 'ask' ya da 'deny' döndürür."""
        if self.matching_rule(tool, args, ctx, "deny"):
            return "deny"
        if self.mode == "yolo":
            return "allow"
        if self.matching_rule(tool, args, ctx, "ask"):
            return "ask"
        risk = tool.risk_for(args)
        if risk == READ:
            return "allow"
        if self.mode == "plan":
            return "allow" if risk == NETWORK else "deny"
        from alice import guard

        allowed = self.matching_rule(tool, args, ctx, "allow")
        if allowed and (risk != DANGER or parse_rule(allowed)[1] is not None):
            # Desensiz kural ("run_command" tek başına) kirli turda komut çalıştırmayı/proje dışına yazmayı/mesaj
            # göndermeyi sormadan geçirmez: dış içerikten gelen talimat o aracın her kullanımını açmış olurdu.
            # Desenli kurallar ("run_command(git status*)") dar olduğu için geçerli kalır.
            if parse_rule(allowed)[1] is not None or not (
                    guard.tainted(ctx) and self._risky_after_untrusted(tool, args, risk, ctx)):
                return "allow"
        # Bu turda dış içerik okundu: talimat oradan gelmiş olabilir. Oturumluk "hep izin ver" (always_allow) bunu
        # delmez: kullanıcı o onayı enjeksiyonu düşünerek vermedi (desenli allow kuralları yukarıda geçerli).
        if guard.tainted(ctx) and self._risky_after_untrusted(tool, args, risk, ctx):
            return "ask"
        if tool.name in self.always_allow and risk != DANGER:
            return "allow"
        if self.mode == "ask":
            return "ask"
        # auto
        if risk == DANGER:
            return "ask"
        if risk == SEND and _confirm_outward(ctx):
            return "ask"
        if risk == WRITE:
            try:
                if any(is_system_path(ctx.resolve(p)) for p in _write_targets(args)):
                    return "ask"
            except (OSError, ValueError):
                return "ask"
        return "allow"


def _inside(target: Path, root: Path) -> bool:
    """target, root klasörünün içinde mi (Windows'ta büyük/küçük harf duyarsız)."""
    t, r = str(target), str(root)
    if sys.platform == "win32":
        t, r = t.lower(), r.lower()
    sep = "\\" if sys.platform == "win32" else "/"
    return t == r or t.startswith(r.rstrip("\\/") + sep)


def _write_targets(args: dict[str, Any]) -> list[str]:
    """Yazma aracının dokunacağı yollar: "path" ve yama metnindeki dosyalar (apply_patch sistem klasörüne
    yazarken önceden hiç sorulmuyordu)."""
    out = [str(args["path"])] if "path" in args else []
    patch = args.get("patch")
    if isinstance(patch, str) and patch:
        from alice.tools.base import ToolError
        from alice.tools.patch import parse_patch

        try:
            ops = parse_patch(patch)
        except ToolError:
            return out  # bozuk yama: araç zaten hata verip hiçbir şey yazmaz
        for op in ops:
            out += [p for p in (op.path, op.move_to) if p]
    return out
