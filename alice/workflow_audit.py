"""GitHub Actions iş akışı denetimi: bilinen tehlikeli desenleri kural tabanlı bulur (model gerekmez).

claude-code-action deposundaki iş akışı sertleştirme kontrolünden esinlenildi; kurallar oradaki kuruma özel olanlar
değil, her depoda geçerli bilinen açıklar:

1. Ayrıcalıklı tetikleyicide (pull_request_target / workflow_run) PR'ın kodunu checkout etmek ("pwn request"):
   iş akışı depo sırlarıyla ve yazma yetkisiyle çalışır, saldırganın kodu da onunla çalışır.
2. run:/script: içinde dışarıdan gelen metni ${{ }} ile doğrudan yazmak (komut enjeksiyonu): issue başlığı,
   PR gövdesi, yorum, dal adı, commit mesajı saldırganın yazdığı metindir.
3. permissions: write-all — her işe her yetki.
4. Üçüncü taraf aksiyonu tam commit SHA'sına sabitlememek: etiket sonradan başka koda taşınabilir.

Satır bazlı çalışır (PyYAML gerekmez); amaç inceleme sırasında kesin, açıklanabilir bulgu üretmek. Satır numaraları
1'den başlar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_WORKFLOW_RX = re.compile(r"(^|/)\.github/workflows/[^/]+\.ya?ml$", re.I)
_PRIVILEGED_RX = re.compile(r"^\s*-?\s*['\"]?(pull_request_target|workflow_run)['\"]?\s*(:|$|,|\])|"
                            r"\[\s*[^\]]*\b(pull_request_target|workflow_run)\b")
# Saldırganın yazabildiği bağlam alanları (GitHub'ın "untrusted input" listesi).
_UNTRUSTED = (r"github\.event\.(?:issue\.(?:title|body)|pull_request\.(?:title|body|head\.ref|head\.label|"
              r"head\.repo\.default_branch)|comment\.body|review\.body|review_comment\.body|"
              r"discussion\.(?:title|body)|head_commit\.(?:message|author\.(?:name|email))|"
              r"commits(?:\[[^\]]*\]|\.\*)\.(?:message|author\.(?:name|email))|pages(?:\[[^\]]*\]|\.\*)\.page_name|"
              r"workflow_run\.(?:head_branch|head_commit\.message|display_title))|github\.head_ref")
_INJECT_RX = re.compile(r"\$\{\{[^}]*\b(" + _UNTRUSTED + r")\b[^}]*\}\}")
_PR_HEAD_RX = re.compile(r"\$\{\{\s*(github\.event\.pull_request\.head\.(?:sha|ref|repo\.full_name)|github\.head_ref|"
                         r"github\.event\.workflow_run\.head_(?:sha|branch)|"
                         r"format\([^)]*pull/[^)]*\))")
_USES_RX = re.compile(r"^\s*-?\s*uses:\s*['\"]?([^@\s'\"]+)@([^\s'\"#]+)")
_SHA_RX = re.compile(r"^[0-9a-f]{40}$")
_BLOCK_KEY_RX = re.compile(r"^(\s*)(?:-\s*)?(run|script):\s*(.*)$")
_TRUSTED_OWNERS = {"actions", "github"}


@dataclass
class Issue:
    line: int
    severity: str  # critical | high | medium | low (review.SEVERITIES ile aynı)
    title: str
    detail: str
    fix: str


def is_workflow(path: str) -> bool:
    return bool(_WORKFLOW_RX.search(path.replace("\\", "/")))


def _code(line: str) -> str:
    """Yorum satırı mı? Yorumları denetleme (örnek kod yorumda durabilir)."""
    return "" if line.lstrip().startswith("#") else line


def _privileged(lines: list[str]) -> bool:
    """Tetikleyiciler arasında ayrıcalıklı olan var mı (jobs: bölümünden önce)."""
    for raw in lines:
        line = _code(raw)
        if re.match(r"^jobs\s*:", line):
            return False
        if _PRIVILEGED_RX.search(line):
            return True
    return False


def _script_lines(lines: list[str]) -> set[int]:
    """run: ve script: (actions/github-script) içeriğinin satırları (0'dan başlayan sıra)."""
    out: set[int] = set()
    i = 0
    while i < len(lines):
        m = _BLOCK_KEY_RX.match(_code(lines[i]))
        if not m:
            i += 1
            continue
        indent, rest = len(m.group(1)), m.group(3).strip()
        if rest and not rest.startswith(("|", ">")):
            out.add(i)  # tek satırlık: run: echo ...
            i += 1
            continue
        out.add(i)
        j = i + 1
        while j < len(lines):
            body = lines[j]
            if body.strip() and len(body) - len(body.lstrip()) <= indent:
                break
            out.add(j)
            j += 1
        i = j
    return out


def audit(text: str) -> list[Issue]:
    lines = text.splitlines()
    issues: list[Issue] = []
    privileged = _privileged(lines)
    scripts = _script_lines(lines)
    for i, raw in enumerate(lines):
        line = _code(raw)
        if not line:
            continue
        n = i + 1
        if i in scripts:
            m = _INJECT_RX.search(line)
            if m:
                issues.append(Issue(
                    n, "high", f"Komut enjeksiyonu: {m.group(1)} doğrudan betikte",
                    f"`{m.group(1)}` dışarıdan biri tarafından yazılabilen bir metin (başlık, gövde, yorum, dal adı, "
                    "commit mesajı). ${{ }} betik çalışmadan önce metne gömülür: içinde `\"; curl ... | sh` gibi bir "
                    "şey varsa runner'da komut olarak çalışır ve iş akışının sırlarına erişir.",
                    "Değeri bir ortam değişkenine al (env: TITLE: ${{ ... }}) ve betikte \"$TITLE\" diye tırnaklı kullan."))
        if privileged and _PR_HEAD_RX.search(line) and re.search(r"\b(ref|repository)\s*:", line):
            issues.append(Issue(
                n, "critical", "Ayrıcalıklı tetikleyicide PR kodu checkout ediliyor (pwn request)",
                "pull_request_target/workflow_run iş akışı ana dalın yetkileri ve sırlarıyla çalışır. PR'ın kodunu "
                "checkout edip derlemek/çalıştırmak (npm install, make, test betikleri) saldırganın koduna o sırları ve "
                "GITHUB_TOKEN'ın yazma yetkisini verir.",
                "PR kodunu çalıştıran işleri `pull_request` tetikleyicisine taşı; sır gerekiyorsa sonucu artifact olarak "
                "ayrı bir workflow_run işine aktar ve orada PR kodunu çalıştırma."))
        if re.match(r"^\s*permissions\s*:\s*['\"]?write-all['\"]?\s*$", line):
            issues.append(Issue(
                n, "medium", "permissions: write-all",
                "Token'a depodaki her şeye yazma yetkisi verir; işte bir enjeksiyon olursa zarar en büyük olur.",
                "Yalnız gereken izinleri yaz (ör. contents: read, pull-requests: write)."))
        m = _USES_RX.match(line)
        if m:
            name, ref = m.group(1), m.group(2)
            owner = name.split("/", 1)[0].lower()
            if (not name.startswith(("./", "docker://")) and "/" in name and owner not in _TRUSTED_OWNERS
                    and not _SHA_RX.match(ref)):
                issues.append(Issue(
                    n, "low", f"Aksiyon commit'e sabitlenmemiş: {name}@{ref}",
                    "Etiket ya da dal sonradan başka bir commit'e taşınabilir; aksiyonun sahibi (ya da hesabını ele "
                    "geçiren) iş akışında istediği kodu çalıştırır.",
                    f"Tam commit SHA'sı kullan: uses: {name}@<40 karakterlik sha>  # {ref}"))
    return issues
