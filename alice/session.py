from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from alice.config import alice_home
from alice.providers.router import strip_images


def sessions_dir() -> Path:
    d = alice_home() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


_issued: set[str] = set()
_issued_lock = threading.Lock()


def new_session_id() -> str:
    """Saniyeye kadar zaman. Aynı saniyede açılan sohbetler (biri arkada çalışırken yenisi) aynı dosyaya yazmasın
    diye gerekirse sonuna -2, -3 eklenir."""
    base = datetime.now().strftime("%Y%m%d-%H%M%S")
    with _issued_lock:
        sid, n = base, 1
        while sid in _issued or (sessions_dir() / f"{sid}.json").exists():
            n += 1
            sid = f"{base}-{n}"
        _issued.add(sid)
    return sid


def save(session_id: str, messages: list[dict[str, Any]], cwd: Path, extra: dict[str, Any] | None = None) -> Path:
    """extra: mesajlar dışında yeniden açılışta geri kurulacak sohbet bilgisi (istatistik, kullanım dökümü, alt
    ajanlar, oturumun başlangıcı). Uygulama kapanıp açılınca bunlar sıfırlanıyordu."""
    path = sessions_dir() / f"{session_id}.json"
    # Ajan thread'i her adımda, arayüz istekleri (durdur, sohbet değiştir, özetle) kendi thread'lerinden aynı oturumu
    # kaydediyor. Ortak .json.tmp'ye aynı anda yazınca dosya karışıyor (bozuk oturum açılışta atlanır: sohbet
    # kaybolur) ya da ikinci replace FileNotFoundError/PermissionError veriyordu. Kayıtlar sıraya girer; anlık
    # görüntü kilit içinde alınır (sonra gelen eski hâli yazmasın), geçici dosya da yazana özeldir.
    with _save_lock:
        # Görüntüler diske yazılmaz, oturum dosyaları küçük kalsın.
        body = {"cwd": str(cwd), "saved": datetime.now().isoformat(timespec="seconds"),
                "messages": strip_images(messages[1:])}
        if extra:
            body["extra"] = extra
        # Önce geçici dosyaya yaz: her adımda kaydederken yarıda kalan bir yazma oturumu bozmasın.
        _atomic_write(path, json.dumps(body, ensure_ascii=False, indent=1))
    return path


_save_lock = threading.RLock()


def _atomic_write(path: Path, text: str) -> None:
    import os

    tmp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


ARCHIVE_TOOL_CHARS = 4000  # arşivde araç çıktısı bu kadar tutulur (yalnız ekranda göstermek için)


def archive_path(session_id: str) -> Path:
    # .jsonl: oturumları listeleyen "*.json" taramalarına girmez.
    return sessions_dir() / f"{session_id}.archive.jsonl"


def archive(session_id: str, messages: list[dict[str, Any]]) -> None:
    """Özetleme sırasında bağlamdan çıkan mesajları oturumun arşivine ekle. Model artık bunları görmez (özetini
    görür) ama sohbet ekranında durmaya devam ederler: önceden özetlenince bütün eski mesajlar ekrandan ve
    oturum dosyasından siliniyordu."""
    if not messages:
        return
    lines = []
    for m in strip_images(messages):
        m = {k: v for k, v in m.items() if k != "reasoning_content"}
        if m.get("role") == "tool" and isinstance(m.get("content"), str) and len(m["content"]) > ARCHIVE_TOOL_CHARS:
            m["content"] = m["content"][:ARCHIVE_TOOL_CHARS] + "\n…(kısaltıldı)"
        lines.append(json.dumps(m, ensure_ascii=False))
    path = archive_path(session_id)
    block = ("\n".join(lines) + "\n").encode("utf-8")
    # Aynı mesajlar ikinci kez geliyorsa (özetlemeden sonra oturum kaydedilemeden yeniden yüklendi) yazma:
    # arşivin sonu zaten bunlar. 11 Kasım'da 164 mesaj iki kez yazıldı, sohbet ekranda kendini tekrar ediyordu.
    # Yalnız dosyanın sonu okunur: arşiv MB'larca olabilir, her özetlemede tamamını okumak gereksiz.
    if _ends_with(path, block):
        return
    with open(path, "ab") as f:
        f.write(block)


def _ends_with(path: Path, block: bytes) -> bool:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size < len(block):
                return False
            start = size - len(block)
            f.seek(max(0, start - 1))
            tail = f.read()
    except OSError:
        return False
    if start == 0:
        return tail == block
    return tail[0:1] == b"\n" and tail[1:] == block  # satır sınırından başlamalı


def load_archive(session_id: str) -> list[dict[str, Any]]:
    try:
        text = archive_path(session_id).read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            m = json.loads(line)
        except json.JSONDecodeError:  # yarım yazılmış son satır
            continue
        if isinstance(m, dict):
            out.append(m)
    return out


_NOT_A_PROMPT = ("[Önceki konuşmanın özeti]", "[devam kontrolü]", "[eski ekran görüntüsü")


def first_prompt(messages: list[dict[str, Any]]) -> str:
    """Kullanıcının gerçekten yazdığı ilk mesaj (özet, hatırlatma ve ekran görüntüsü notları atlanır)."""
    for m in messages:
        if m.get("role") != "user" or m.get("_nudge") or m.get("_image"):
            continue
        c = m.get("_display") or m.get("content")
        if isinstance(c, list):
            c = next((p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text"), "")
        if isinstance(c, str) and c.strip() and not c.lstrip().startswith(_NOT_A_PROMPT):
            return c.strip()
    return ""


def _first_archived(session_id: str) -> str:
    """Arşivin ilk kullanıcı mesajı: bütün dosyayı okumadan (arşiv MB'larca olabilir) baştaki satırlara bakılır."""
    try:
        with open(archive_path(session_id), encoding="utf-8") as f:
            head = []
            for _ in range(40):
                line = f.readline()
                if not line:
                    break
                try:
                    head.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return ""
    return first_prompt([m for m in head if isinstance(m, dict)])


def session_prompt(session_id: str, messages: list[dict[str, Any]]) -> str:
    """Sohbetin ilk isteği. Özetlenmiş sohbette oturum dosyasının ilk mesajı özettir; asıl ilk mesaj arşivdedir.
    Önceden başlık özetin ilk 70 karakteri oluyordu ("[Önceki konuşmanın özeti] <tool calls>…")."""
    return _first_archived(session_id) or first_prompt(messages)


# list_sessions önbelleği: yol -> ((dosya ve arşivin mtime/boyutu), (cwd, ilk istek)). Kenar çubuğu her açılışta 60
# sohbet dosyasını baştan okuyup JSON çözüyordu (soğuk açılışta ~600 ms); değişmeyen dosya yeniden okunmaz.
_list_cache: dict[str, tuple[tuple[int, ...], tuple[str, str]]] = {}
_list_lock = threading.Lock()


def _file_sig(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
    except OSError:
        return (0, 0)
    return (st.st_mtime_ns, st.st_size)


def list_sessions(limit: int = 15) -> list[tuple[str, str, str]]:
    out = []
    for p in sorted(sessions_dir().glob("*.json"), reverse=True)[:limit]:
        sig = _file_sig(p) + _file_sig(archive_path(p.stem))
        with _list_lock:
            hit = _list_cache.get(str(p))
        if hit is not None and hit[0] == sig:
            cwd, first = hit[1]
        else:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict) or not isinstance(data.get("messages", []), list):
                continue
            cwd, first = str(data.get("cwd", "")), session_prompt(p.stem, data.get("messages", []))
            with _list_lock:
                _list_cache[str(p)] = (sig, (cwd, first))
        out.append((p.stem, cwd, first[:70].replace("\n", " ")))
    return out


def load(session_id: str | None = None) -> tuple[str, list[dict[str, Any]]] | None:
    files = sorted(sessions_dir().glob("*.json"), reverse=True)
    if session_id:
        files = [f for f in files if f.stem == session_id]
    # Bozuk ya da yarım kalmış bir dosya (list_sessions'ın da atladığı) devam etmeyi çökertmesin: sıradakine geç.
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        messages = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(messages, list):
            continue  # geçerli JSON ama oturum biçiminde değil (ör. liste ya da sayı): bozuk dosya gibi atla
        return f.stem, [m for m in messages if isinstance(m, dict)]
    return None


def load_extra(session_id: str) -> dict[str, Any]:
    try:
        data = json.loads((sessions_dir() / f"{session_id}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    extra = data.get("extra")
    return extra if isinstance(extra, dict) else {}


# Geri sarma ve Değişiklikler sekmesi için dosyaların ilk halleri. Oturum dosyasından ayrı tutulur: büyük olabilir ve
# her adımda değil yalnız değişince yazılır. Sonek .json değil: oturumları listeleyen "*.json" taramasına girmesin.
CHECKPOINT_FILE_MAX = 2_000_000  # bundan büyük dosyanın ilk hali saklanmaz (o dosya yeniden açılışta geri alınamaz)
CHECKPOINT_TOTAL_MAX = 40_000_000


def checkpoints_path(session_id: str) -> Path:
    return sessions_dir() / f"{session_id}.checkpoints"


def save_checkpoints(session_id: str, checkpoints: dict[int, dict[str, bytes | None]]) -> None:
    import base64

    out: dict[str, dict[str, str | None]] = {}
    total = 0
    for idx in sorted(list(checkpoints)):  # alt ajanlar aynı anda yazabilir: kopyalar üzerinde dolaş
        row = {}
        for path, data in list(checkpoints[idx].items()):
            if data is not None and (len(data) > CHECKPOINT_FILE_MAX or total + len(data) > CHECKPOINT_TOTAL_MAX):
                continue
            total += len(data or b"")
            row[path] = None if data is None else base64.b64encode(data).decode("ascii")
        if row:
            out[str(idx)] = row
    with _save_lock:
        _atomic_write(checkpoints_path(session_id), json.dumps(out))


def load_checkpoints(session_id: str) -> dict[int, dict[str, bytes | None]]:
    import base64

    try:
        raw = json.loads(checkpoints_path(session_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[int, dict[str, bytes | None]] = {}
    for k, row in raw.items():
        if not isinstance(row, dict):
            continue
        try:
            out[int(k)] = {p: (None if v is None else base64.b64decode(v)) for p, v in row.items()}
        except (ValueError, TypeError):
            continue
    return out


def saved_cwd(session_id: str) -> Path | None:
    """Oturumun kaydedildiği çalışma klasörü (hâlâ varsa). Arka planda diskten açılan sohbet (ör. PR takibinin
    düzeltme işi) uygulamanın o anki klasöründe değil kendi deposunda çalışsın."""
    try:
        data = json.loads((sessions_dir() / f"{session_id}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    cwd = Path(data.get("cwd") or "")
    return cwd if data.get("cwd") and cwd.is_dir() else None


# Kullanıcının verdiği ad ve sabitleme. Oturum dosyası her adımda baştan yazıldığı için ayrı tutulur:
# {"<id>": {"title": "...", "pinned": true}}
def _meta_path() -> Path:
    return alice_home() / "session_meta.json"


def load_meta() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_meta_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


_meta_lock = threading.Lock()


def set_meta(session_id: str, **fields: Any) -> dict[str, Any]:
    """Alanları güncelle; boş başlık ya da False sabitleme alanı siler."""
    with _meta_lock:  # başlık üreten arka plan işi ile arayüzden yeniden adlandırma birbirini ezmesin
        return _set_meta(session_id, **fields)


TITLE_PROMPT = (
    "Aşağıdaki mesaj bir sohbetin ilk isteği. Sohbet listesinde görünecek kısa bir başlık yaz: isteğin konusunu "
    "anlatan 3-6 kelime, mesajın dilinde, baş harfi büyük. Tırnak, nokta, emoji ve açıklama yok; yalnız başlık.")


def make_title(router: Any, prompt: str) -> str:
    """İlk istekten kısa bir başlık (hızlı model). Olmazsa boş: liste isteğin başını gösterir."""
    import re

    text = " ".join(prompt.split())[:1500]
    if len(text) < 4:
        return ""
    try:
        res = router.chat("fast", [{"role": "system", "content": TITLE_PROMPT}, {"role": "user", "content": text}],
                          max_tokens=200, temperature=0.2)
    except Exception:  # noqa: BLE001 - model yok: başlıksız kalır
        return ""
    lines = [ln.strip() for ln in (res.text or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    title = re.sub(r"^(başlık|title)\s*[:：-]\s*", "", lines[-1], flags=re.I).strip(" \"'`*#.«»“”")
    if not 2 <= len(title) <= 70 or title.startswith(("<", "{", "[")) or len(title.split()) > 10:
        return ""
    return title


def ensure_title(router: Any, session_id: str, messages: list[dict[str, Any]]) -> str:
    """Sohbetin otomatik başlığı yoksa ilk istekten üret ve sakla (session_meta: auto_title). Bir kez yapılır:
    başlık sonradan, konuşma özetlenince ya da uzayınca değişmez. Kullanıcının verdiği ad (title) ayrı durur."""
    row = load_meta().get(session_id) or {}
    if row.get("auto_title") or row.get("title"):
        return str(row.get("auto_title") or row.get("title"))
    prompt = session_prompt(session_id, messages)
    title = make_title(router, prompt) if prompt else ""
    if title:
        set_meta(session_id, auto_title=title)
    return title


def _set_meta(session_id: str, **fields: Any) -> dict[str, Any]:
    meta = load_meta()
    row = dict(meta.get(session_id) or {})
    for k, v in fields.items():
        if v in ("", None, False):
            row.pop(k, None)
        else:
            row[k] = v
    if row:
        meta[session_id] = row
    else:
        meta.pop(session_id, None)
    _meta_path().parent.mkdir(parents=True, exist_ok=True)
    tmp = _meta_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_meta_path())
    return row
