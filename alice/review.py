"""Bağımsız kod incelemesi: değişikliği yazan konuşmadan ayrı, temiz bağlamlı bir inceleyici ajan.

Kodu yazan model kendi hatasını görmekte zorlanır; konuşmanın varsayımlarını taşır. Burada:

1. Fark toplanır: yerel değişiklikler (ana daldan ayrıldığı noktadan bu yana, commit'lenmemişler dahil) ya da bir
   GitHub PR'ı.
2. İnceleyici ajan farkı ve gerektiğinde depodaki dosyaları (yalnız okuma araçlarıyla) inceler, bulguları JSON
   olarak verir: dosya, satır, önem, ne yanlış, hangi durumda bozulur.
3. Ayrı bir doğrulayıcı ajan her bulguyu kodda kontrol eder; çürütülenler atılır (yanlış alarm raporlanmasın).
4. İstenirse sonuç PR'a satır yorumlarıyla bir inceleme olarak yazılır.

İnceleme için model seçici "inceleme" alanındaki en güçlü modelleri kullanır (alice/providers/picker.py).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

READ_TOOLS = ("read_file", "grep", "glob", "list_dir")
MAX_DIFF = 60_000  # karakter: daha büyük fark kısaltılır (dosya listesi her zaman verilir)
SEVERITIES = ("critical", "high", "medium", "low")

FOCUS = {
    "bugs": "Doğruluk hataları: yanlış mantık, kenar durumlar, yarış durumları, hata yönetimi, kaynak sızıntısı, "
            "geriye dönük uyumsuzluk, testlerin gerçekte sınamadığı davranış.",
    "security": "Güvenlik: enjeksiyon (komut, SQL, yol), kimlik/yetki atlatma, gizli bilgi sızıntısı (log, hata "
                "mesajı, URL), güvensiz seri hale getirme, dış girdinin doğrulanmaması, SSRF.",
    "all": "Doğruluk hataları ve güvenlik açıkları; ayrıca gereksiz karmaşıklık ve açık performans sorunları.",
    "performance": "Performans: döngü içinde tekrarlanan pahalı iş (dosya/ağ/veritabanı çağrısı, N+1 sorgu), gereksiz "
                   "kopyalama ve büyük bellek tutma, kapanmayan kaynak (dosya, bağlantı, thread), ana/UI thread'ini "
                   "bloklayan çağrı, sınırsız büyüyen önbellek/kuyruk. Yalnız ölçülebilir etkisi olanı yaz.",
    "tests": "Test kapsamı: değişen davranışın test edilmeyen yolları (hata dalları, kenar durumlar, eşzamanlılık), "
             "gerçekte hiçbir şeyi sınamayan testler (her durumda geçen assert, sahteyle kendini test eden), kırılgan "
             "testler (zamanlamaya, sıraya, ortama bağlı). Bulguda hangi senaryonun test edilmediğini somut yaz; "
             "'line' eksik testin ilgili olduğu kod satırı olsun.",
    "docs": "Doküman doğruluğu: değişiklikten sonra artık yanlış olan yorumlar, docstring'ler, README/ayar açıklamaları "
            "(var olmayan parametre, değişmiş varsayılan, eski davranış tarifi). Yalnız koda göre YANLIŞ olanı yaz, "
            "eksik doküman ya da üslup değil.",
}
# deep: her biri ayrı odaklı inceleyiciler aynı anda çalışır, bulgular birleşip tek doğrulayıcıdan geçer.
DEEP = ("bugs", "security", "performance", "tests")

REVIEWER = """Sen bağımsız bir kod inceleyicisisin. Bu değişikliği sen yazmadın ve yazarın konuşmasını görmüyorsun.
Görevin: değişiklikteki GERÇEK sorunları bulmak. Biçim, isimlendirme, zevk meselesi yazma.

Odak: {focus}

Yöntem:
- Fark yetmiyorsa çevredeki kodu read_file/grep ile oku: çağıranlar, kullanılan fonksiyonlar, testler.
- Her bulgu için somut bir bozulma senaryosu yaz: hangi girdi/durumda ne yanlış olur. Senaryo yazamıyorsan yazma.
- Yalnız değişen satırlar ve onların doğrudan etkisi. Eskiden beri var olan, değişiklikle ilgisiz sorunları yazma.

Bitince YALNIZCA şu JSON'u yaz (başka metin yok):
{{"findings": [{{"file": "yol/dosya.py", "line": 42, "severity": "high", "title": "kısa başlık",
"detail": "ne yanlış", "scenario": "hangi durumda bozulur", "fix": "nasıl düzelir (kısa)"}}]}}
Sorun yoksa {{"findings": []}}. Önem: critical | high | medium | low."""

VERIFIER = """Sen bir doğrulayıcısın. Başka bir inceleyicinin bulgularını kodda tek tek kontrol et: iddia gerçekten
doğru mu, senaryo gerçekten olur mu? Kodu read_file/grep ile oku; varsayımla karar verme.

Bitince YALNIZCA şu JSON'u yaz:
{"verdicts": [{"id": 0, "verdict": "CONFIRMED", "reason": "kısa gerekçe"}]}
verdict: CONFIRMED (kodda doğrulandı) | PLAUSIBLE (büyük ihtimalle doğru ama tam doğrulanamadı) |
REJECTED (yanlış, zaten ele alınmış ya da senaryo olamaz)."""


@dataclass
class Finding:
    file: str
    line: int
    severity: str
    title: str
    detail: str
    scenario: str = ""
    fix: str = ""
    verdict: str = ""
    reason: str = ""

    def text(self) -> str:
        head = f"[{self.severity}] {self.file}:{self.line} {self.title}" + (f" ({self.verdict})" if self.verdict else "")
        body = [self.detail]
        if self.scenario:
            body.append(f"Ne zaman bozulur: {self.scenario}")
        if self.fix:
            body.append(f"Öneri: {self.fix}")
        return head + "\n  " + "\n  ".join(body)


# ------------------------------------------------------------------------------------------------- fark
def local_diff(cwd: Path, base: str = "") -> tuple[str, str]:
    """(başlık, fark): ana daldan ayrılınan noktadan bu yana bütün değişiklikler (commit'lenmemişler dahil)."""
    from alice.tools.github import ToolError, current_branch, repo_root, run_git

    root = repo_root(cwd)
    if not base:
        code, out = run_git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], root, check=False)
        base = out.strip() if code == 0 and out.strip() else "origin/main"
    code, mb = run_git(["merge-base", "HEAD", base], root, check=False)
    start = mb.strip() if code == 0 and mb.strip() else "HEAD"
    _, diff = run_git(["diff", "--no-color", "-U5", start], root)
    _, untracked = run_git(["ls-files", "--others", "--exclude-standard"], root, check=False)
    extra = []
    for name in untracked.splitlines()[:20]:  # yeni dosyalar farkta görünmüyor: içerikleriyle eklenir
        p = root / name.strip()
        try:
            if p.is_file() and p.stat().st_size < 60_000:
                extra.append(f"--- /dev/null\n+++ b/{name}\n" + "".join(f"+{ln}\n" for ln in
                                                                      p.read_text(encoding="utf-8").splitlines()))
        except (OSError, UnicodeDecodeError):
            continue
    full = (diff + ("\n" + "\n".join(extra) if extra else "")).strip()
    if not full:
        raise ToolError(f"incelenecek değişiklik yok ({start[:10]} ile aynı)")
    return f"{current_branch(root) or 'HEAD'} dalı, {base} ile fark", full


def pr_diff(api: Any, repo: str, number: int) -> tuple[str, str, str]:
    """(başlık, fark, uç commit) — GitHub'dan."""
    from alice.tools.github import ToolError

    pr = api.get(f"/repos/{repo}/pulls/{number}")
    try:
        r = api.client.get(f"/repos/{repo}/pulls/{number}", headers={"Accept": "application/vnd.github.diff"})
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"PR farkı alınamadı: {e}") from e
    if r.status_code >= 400:
        raise ToolError(f"PR farkı alınamadı (HTTP {r.status_code})")
    return f"PR #{number}: {pr.get('title', '')}", r.text, pr["head"]["sha"]


def shorten(diff: str, limit: int = MAX_DIFF) -> str:
    if len(diff) <= limit:
        return diff
    files = re.findall(r"(?m)^\+\+\+ b/(.+)$", diff)
    return diff[:limit] + f"\n… (fark kısaltıldı; değişen dosyalar: {', '.join(files)}. Gerekirse read_file ile oku.)"


# ----------------------------------------------------------------------------------------------- ajanlar
def _agent(cfg: Any, router: Any, cwd: Path, system: str) -> Any:
    from alice.agent import Agent, NullUI
    from alice.permissions import Permissions
    from alice.tools import ToolContext, all_tools

    tools = [t for t in all_tools(cfg) if t.name in READ_TOOLS]
    ctx = ToolContext(cwd=cwd, config=cfg, router=router)
    agent = Agent(cfg, router, tools, ctx, Permissions("auto"), NullUI(approve="n"), system_prompt=system, is_child=True)
    agent.tool_specs = lambda: [t.spec() for t in tools]  # grup filtresinden bağımsız: yalnız okuma araçları
    try:
        from alice.providers import picker

        chain = picker.chain_for(router, "review")
        if chain:
            router.role_overrides["review:code"] = chain
            agent.role = "review:code"
    except Exception:  # noqa: BLE001 - seçici çalışmazsa ana model
        pass
    return agent


def parse_json(text: str, key: str) -> list[dict[str, Any]]:
    """Cevaptaki JSON (kod bloğunda ya da düz). Bulunamazsa boş liste."""
    for m in re.finditer(r"\{.*\}", text or "", re.S):
        try:
            data = json.loads(m.group(0))
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get(key), list):
            return [x for x in data[key] if isinstance(x, dict)]
    return []


def _finding(d: dict[str, Any]) -> Finding | None:
    try:
        line = int(d.get("line") or 0)
    except (TypeError, ValueError):
        line = 0
    title, file = str(d.get("title") or "").strip(), str(d.get("file") or "").strip()
    if not title or not file:
        return None
    sev = str(d.get("severity") or "medium").lower()
    return Finding(file=file, line=line, severity=sev if sev in SEVERITIES else "medium", title=title[:200],
                   detail=str(d.get("detail") or "")[:1500], scenario=str(d.get("scenario") or "")[:800],
                   fix=str(d.get("fix") or "")[:600])


def _review_one(cfg: Any, router: Any, cwd: Path, title: str, diff: str, focus: str) -> list[Finding]:
    reviewer = _agent(cfg, router, cwd, REVIEWER.format(focus=FOCUS.get(focus, FOCUS["bugs"])))
    out = reviewer.run_turn(f"İncelenecek değişiklik: {title}\n\n```diff\n{shorten(diff)}\n```", max_steps=30)
    return [f for f in (_finding(d) for d in parse_json(out, "findings")) if f]


def _dedupe(findings: list[Finding]) -> list[Finding]:
    """Farklı odaklı inceleyiciler aynı sorunu bulabilir: aynı dosyada 2 satır içinde, başlığı aynı olanlardan biri
    kalır (önemi yüksek olan)."""
    kept: list[Finding] = []
    for f in sorted(findings, key=lambda f: SEVERITIES.index(f.severity)):
        key = re.sub(r"\W+", " ", f.title.lower()).strip()
        if any(k.file == f.file and abs(k.line - f.line) <= 2 and re.sub(r"\W+", " ", k.title.lower()).strip() == key
               for k in kept):
            continue
        kept.append(f)
    return kept


def review(cfg: Any, router: Any, cwd: Path, title: str, diff: str, focus: str = "bugs",
           verify: bool = True) -> list[Finding]:
    if focus == "deep":
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=len(DEEP)) as pool:
            parts = list(pool.map(lambda fo: _safe_review(cfg, router, cwd, title, diff, fo), DEEP))
        findings = _dedupe([f for part in parts for f in part])
    else:
        findings = _review_one(cfg, router, cwd, title, diff, focus)
    # Kural tabanlı iş akışı bulguları kesin: doğrulayıcıya gitmez (bir model onları "yanlış alarm" diye atamasın).
    static = workflow_findings(cwd, diff)
    if not findings or not verify:
        return _sorted(static + findings)
    listing = "\n\n".join(f"id={i}\n{f.text()}" for i, f in enumerate(findings))
    verifier = _agent(cfg, router, cwd, VERIFIER)
    res = verifier.run_turn(f"Değişiklik: {title}\n\n```diff\n{shorten(diff, MAX_DIFF // 2)}\n```\n\nBulgular:\n{listing}",
                            max_steps=30)
    verdicts = {int(v["id"]): v for v in parse_json(res, "verdicts") if str(v.get("id", "")).isdigit()}
    kept = []
    for i, f in enumerate(findings):
        v = verdicts.get(i)
        if v is None:  # doğrulayıcı bakmadı: bulgu durur, işaretsiz
            kept.append(f)
            continue
        f.verdict, f.reason = str(v.get("verdict") or "").upper(), str(v.get("reason") or "")[:300]
        if f.verdict != "REJECTED":
            kept.append(f)
    return _sorted(static + kept)


def _safe_review(cfg: Any, router: Any, cwd: Path, title: str, diff: str, focus: str) -> list[Finding]:
    """deep modunda bir inceleyicinin hatası (model yok, zaman aşımı) ötekilerin bulgularını yok etmesin."""
    import logging

    try:
        return _review_one(cfg, router, cwd, title, diff, focus)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("inceleyici (%s) hata verdi", focus, exc_info=True)
        return []


def workflow_findings(cwd: Path, diff: str) -> list[Finding]:
    """Farkta değişen GitHub Actions iş akışlarını kural tabanlı denetle (alice/workflow_audit.py). Yalnız değişen
    satırlara düşen bulgular döner; dosya yerelde yoksa (başka depodaki PR) denetlenmez."""
    from alice import workflow_audit

    out: list[Finding] = []
    for path, added in _added_lines(diff).items():
        if not workflow_audit.is_workflow(path):
            continue
        try:
            text = (cwd / path).read_text(encoding="utf-8-sig")
        except (OSError, ValueError):
            continue
        rows = text.splitlines()
        for issue in workflow_audit.audit(text):
            # Yalnız bu değişiklikte EKLENEN satırlar; yereldeki dosya PR'ınkinden farklıysa (başka dal açık)
            # satır metni tutmaz ve bulgu yanlış yere düşmesin diye atlanır.
            n = issue.line
            if n in added and 0 < n <= len(rows) and rows[n - 1].rstrip() == added[n].rstrip():
                out.append(Finding(file=path, line=issue.line, severity=issue.severity, title=issue.title,
                                   detail=issue.detail, fix=issue.fix, verdict="KURAL"))
    return out


def _sorted(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (SEVERITIES.index(f.severity), f.verdict != "CONFIRMED", f.file, f.line))


def report(findings: list[Finding]) -> str:
    if not findings:
        return "İnceleme bitti: doğrulanmış sorun bulunmadı."
    return f"{len(findings)} bulgu (önem sırasıyla):\n\n" + "\n\n".join(f.text() for f in findings)


def diff_lines(diff: str) -> dict[str, set[int]]:
    """Farkta yorum yazılabilen satırlar (yeni dosyadaki satır numaraları: eklenen ve bağlam satırları). GitHub satır
    yorumunu yalnız bunlara kabul eder; birinin dışında olması bütün incelemeyi reddettirir."""
    out: dict[str, set[int]] = {}
    cur: set[int] | None = None
    line = 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            cur = None if path == "/dev/null" else out.setdefault(path[2:] if path.startswith("b/") else path, set())
            continue
        if raw.startswith("--- ") or raw.startswith("diff --git"):
            continue
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if m:
            line = int(m.group(1))
            continue
        if cur is None or raw.startswith("\\"):
            continue
        if raw.startswith("-"):
            continue
        if raw.startswith("+") or raw.startswith(" ") or raw == "":
            cur.add(line)
            line += 1
    return out


def _added_lines(diff: str) -> dict[str, dict[int, str]]:
    """Farkta eklenen satırlar: dosya -> {yeni dosyadaki satır no: metin}."""
    out: dict[str, dict[int, str]] = {}
    cur: dict[int, str] | None = None
    line = 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            cur = None if path == "/dev/null" else out.setdefault(path[2:] if path.startswith("b/") else path, {})
            continue
        if raw.startswith("--- ") or raw.startswith("diff --git"):
            continue
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if m:
            line = int(m.group(1))
            continue
        if cur is None or raw.startswith("\\") or raw.startswith("-"):
            continue
        if raw.startswith("+"):
            cur[line] = raw[1:]
        line += 1
    return out


def _anchor(f: Finding, lines: dict[str, set[int]]) -> int:
    """Bulgunun yazılacağı satır: tam satır farktaysa o, değilse aynı dosyada 3 satır içindeki en yakın fark satırı;
    hiçbiri yoksa 0 (bulgu inceleme metnine gider)."""
    ok = lines.get(f.file) or set()
    if f.line in ok:
        return f.line
    near = [n for n in ok if abs(n - f.line) <= 3]
    return min(near, key=lambda n: abs(n - f.line)) if near and f.line > 0 else 0


def post(api: Any, repo: str, number: int, sha: str, findings: list[Finding], diff: str = "") -> str:
    """Bulguları PR'a satır yorumlarıyla tek bir inceleme olarak yaz. Farktaki satıra düşen bulgu o satıra yorum olur;
    farkın dışında kalanlar (değişmemiş bir yerdeki sorun) inceleme metnine konur. GitHub yine reddederse hepsi
    metne gider."""
    from alice.tools.github import ALICE_MARK, ToolError

    lines = diff_lines(diff) if diff else {}
    inline, outside = [], []
    for f in findings:
        at = _anchor(f, lines) if diff else f.line
        (inline if at > 0 else outside).append((f, at))
    comments = [{"path": f.file, "line": at, "side": "RIGHT", "body": f.text() + f"\n\n{ALICE_MARK}"}
                for f, at in inline]
    summary = (f"Bağımsız inceleme: {len(findings)} bulgu" + (f" ({len(comments)} satır yorumu)" if comments else "")
               + "." if findings else "Bağımsız inceleme: doğrulanmış sorun bulunmadı.")
    if outside:
        summary += "\n\nFarkın dışında kalan bulgular:\n\n" + "\n\n".join(f.text() for f, _ in outside)
    summary += f"\n\n{ALICE_MARK}"
    payload = {"commit_id": sha, "event": "COMMENT", "body": summary, "comments": comments}
    try:
        r = api.post(f"/repos/{repo}/pulls/{number}/reviews", payload)
    except ToolError:
        payload = {"commit_id": sha, "event": "COMMENT",
                   "body": summary.removesuffix(f"\n\n{ALICE_MARK}") + "\n\n"
                   + "\n\n".join(f.text() for f in findings) + f"\n\n{ALICE_MARK}"}
        r = api.post(f"/repos/{repo}/pulls/{number}/reviews", payload)
    from alice import undercover

    undercover.remember_own(r.get("id"))
    return str(r.get("html_url") or "")
