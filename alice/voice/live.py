"""Gemini Live: sesten sese canlı konuşma (ChatGPT'nin sesli modunun Google karşılığı).

"Hey Alice" duyulunca bir Live oturumu açılır; mikrofon doğrudan modele akar, model Kore sesiyle ~0,5 sn'de
konuşmaya başlar, araya girilince susar (sunucu tarafı VAD). ALICE'in araçları (YouTube açma, pencere, not...)
function calling ile çalışır. Konuşma `idle` saniye sessiz kalınca oturum kapanır, tekrar "Hey Alice" beklenir.
Bağlanamazsa (internet, kota, paket yok) çağıran eski yola (yazıya çevir -> model -> ses) döner.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import re
import threading
import time
from typing import Any, Callable

from alice.tools.base import ToolResult
from alice.voice.lang import correction, foreign, junk
from alice.voice.listen import FOLLOW_MAX, ends_with_hush, is_yes
from alice.voice.vocab import known_names

URL = ("wss://generativelanguage.googleapis.com/ws/"
       "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={key}")
# Duygulu konuşma (affective dialog) ve "sana söylenmeyene cevap verme" (proactive audio) şimdilik v1alpha'da.
URL_ALPHA = URL.replace(".v1beta.", ".v1alpha.")
DEFAULT_MODELS = ["gemini-3.8-live", "gemini-3.1-flash-live-preview", "gemini-2.5-flash-native-audio-latest"]
# Aynı istekte aynı argümanla ikinci kez çalışması bir şey değiştirmeyen araçlar.
READ_ONLY_TOOLS = {"list_dir", "glob", "grep", "read_file", "system_info", "web_search", "web_fetch", "weather"}
_READ_COMMAND = re.compile(r"^\s*(get-\w+|dir|ls|type|cat|where(\.exe)?|test-path|select-string)\b", re.I)


def read_only(name: str, args: dict[str, Any]) -> bool:
    """Aynı istekte tekrar çalıştırmanın anlamı olmayan okuma mı? Komutlarda sadece Get-*/dir/ls gibi okumalar."""
    if name in READ_ONLY_TOOLS:
        return True
    return name == "run_command" and bool(_READ_COMMAND.match(str(args.get("command", ""))))
LIVE_GROUPS = {"desktop", "browser", "personal", "system", "automation"}
# Araç çalışırken Adada "Çalışıyorum · ..." ayrıntısı
HUD_TOOLS = {"reflex": "ekranda", "computer_task": "ekranda", "computer": "ekranda", "ui_act": "ekranda",
             "browser": "tarayıcıda", "windows": "pencerelerde", "web_search": "arıyorum", "web_fetch": "okuyorum"}
# live_screen = "auto": kullanıcı ekrandakinden söz ediyorsa ("şuna tıkla", "ekrandaki", "bu ne") kare gider.
SCREEN_WORDS = re.compile(
    r"\b(ekran\w*|şu(nu|na|nda|ndaki|raya|rada|radaki)?|bu(nu|na|nda|ndaki|raya|rada|radaki)?|"
    r"(sağ|sol|üst|alt|orta)daki|açık olan|gördüğün|görüyor|görünen|tıkla\w*|seç\w*|bak(sana|ar mısın)?|"
    r"pencere\w*|sekme\w*|oyun\w*|video\w*|resim\w*|yazı\w*)\b", re.I)
SCHEMA_KEYS = {"type", "description", "enum", "properties", "required", "items", "nullable", "format"}

LIVE_PROMPT = """# Canlı sesli konuşma
Kullanıcıyla sesli, canlı konuşuyorsun. Doğal, sıcak ve kısa konuş (genelde 1-2 cümle), Türkçe.
Liste, kod, yol ya da uzun sayı okuma; gerekirse ekrana koyduğunu söyle.
Bir iş istenince ilgili aracı hemen çağır.
Sen hiçbir şey demeden araç çağırırsan kullanıcı senin sesinle "Bir saniye.", "Hemen bakıyorum." ya da "Bir
düşüneyim." gibi kısa bir cümle duyar; araç sonucundan sonra bunu tekrarlama, doğrudan sonucu söyle.
Birkaç araç sürecek bir işe başlarken önce tek kısa cümleyle ne yapacağını söyle (kullanıcı sessizlikte
bekletilmesin). 3-4 denemede sonuç alamazsan araç çağırmayı bırak, bulduğunu söyle ve kullanıcıya sor.
Web işlerini her zaman kullanıcının açık tarayıcısında, açık sekmelerinde yap; sıfırdan pencere açma.
"Ekrandaki", "şu", "bu", "sağdaki", "açık olan" gibi ekranda görünen bir şeyi isterse onu YENİDEN ARAMA: ekranda
neyse ona tıklat. Ekranda tıklama/yazma işlerini reflex aracına ver: tarayıcıdaysa target=browser, değilse
target=desktop (title=pencere adı); goal'e kullanıcının istediğini ekranda göründüğü adla yaz (ör. "ekrandaki
Impossible şarkısına tıkla", "arama kutusuna X yaz ve ilk sonuca tıkla"). Reflex "needs_help" derse browser
snapshot/click ya da ui_elements/ui_act ile kendin bitir; ne olduğunu anlayamazsan computer action=screenshot ile bak.
Siteler arasında fark gözetme: her sitede ve uygulamada aynı yol.
youtube aracı sadece ekranda olmayan yeni bir YouTube videosunu açmak ya da açık videoyu sarmak (seek) için.
browser aracı kullanıcının tarayıcısına bağlanır (tabs, switch_tab; goto açık site sekmesini kullanır). browser "bağlanılamıyor" derse: windows action=switch_tab
target=<site adı> ile açık sekmeye geç, windows action=navigate target=<URL ya da arama> ile git.
Yeni sekme ya da ayrı pencere sadece kullanıcı isterse.
Bilgisayarın TAMAMINI kullanabilirsin: masaüstü, Windows ayarları, her uygulama, fare ve klavye. "Sadece
tarayıcıyı görebiliyorum", "yetkim yok" deme; bir yolunu bul.
Birkaç adım süren ekran işlerini de (masaüstünde/Windows'ta bir ayar, uygulamada menülerden geçmek, sağ tık menüsü,
form doldurmak, bir uygulamada yazıp kaydetmek, mesaj göndermek) ÖNCE reflex'e ver: o da çok adımlıdır ve adım başına
~1 sn (ör. goal "masaüstüne geç, boş yere sağ tıkla, Görünüm > Masaüstü simgelerini göster'i aç").
computer_task YAVAŞ yedektir (adım başına 5-15 sn, ekran görüntüsüne bakan büyük model): yalnızca reflex
"needs_help" derse ya da ekranda okunabilir öğe/yazı olmayan arayüzlerde (oyun, çizim, Blender'ın görüntü alanı) kullan;
arka planda sürer, bitince sonucu sana yazar. computer aracıyla görmeden (koordinat tahmin ederek) tıklama.
Mikrofon oyun/motor sesini, videoyu ve kullanıcının Discord'da/odada başkalarıyla konuşmasını da duyar. Duyduğun cümle
anlamsızsa, Türkçe değilse ya da sana söylenmemiş gibiyse HİÇ cevap verme, sessiz kal; bu normaldir. Sessiz kalırken
"cevap vermeyeceğim", "---", "no speech detected" gibi hiçbir şey söyleme ya da yazma, İngilizce konuşma. "Anlayamadım,
tekrar eder misin" demeyi bir konuşmada en fazla bir kez ve sadece sana söylendiğinden emin olduğun bir cümle için yap.
Her zaman Türkçe konuş. Başka dilde (Portekizce, Almanca, Japonca...) bir şey duyarsan o büyük ihtimalle oyun, video
ya da başkasıdır: o dilde cevap verme, cevap verme.
Uçuşla ilgili her soruda (hız, irtifa, stall, yakıt, yön, silah) önce flight action=status çağır; ekran
görüntüsünden ya da hafızadan uçuş verisi söyleme.
Araçların vermediği sayıları ve durumları (uçaktaki silahlar, mesafe, yakıt, ekranda göremediğin şeyler) asla
uydurma ya da genel bilginden tahmin etme; bilmiyorsan "bu bilgiye ulaşamıyorum" de.
Mesaj yazmak/göndermek gibi geri alınamayan işlerde hedef, kullanıcının söylediği uygulama ve sohbet olmalı;
ne dediğini tam anlamadıysan önce sor. Göndermeden önce tek kısa cümleyle neyi nereye gönderdiğini söyle ("WhatsApp'ta
Ahmet'e 'geliyorum' yazıyorum"). Bir işi yaptığını ancak araç sonucu "done" derse söyle. "[sistem notu" ile başlayan
mesajlar kullanıcının sözü değildir; söylediklerini düzeltmen içindir.
Bir araç hata verirse aynısını döndürüp durma, başka yol dene; kullanıcıdan ayar değiştirmesini istemek son çaredir.
Çok adımlı işi (indir, kur, ayarla) sonuna kadar götür: her adımdan sonra araçla durumu kontrol et (sayfa ne
diyor, dosya indi mi), bitmeden "yaptım/başlattım" deme; ara adımda en fazla kısa bir "devam ediyorum" de.
Bilgisayar Windows: indirme ve kurulumda Windows sürümünü seç (macOS/Linux değil).
Monitörler: {monitors}. Kullanıcı ekrandakinden söz edince sana öndeki pencerenin olduğu monitörün görüntüsü gelir;
görüntü gelmediyse ve ekranı görmen gerekiyorsa computer action=screenshot ile bak. Başka monitörü ya da
hepsini görmek için computer action=screenshot monitor=<numara> (0 = hepsi yan yana); computer_task'a da monitor
verebilirsin.
Klasör ve dosya soruları için list_dir kullan ("İndirilenler", "Masaüstü" gibi adları doğrudan ver; "son indirdiğim"
için sort=newest), PowerShell ile klasör arama.
Bir işe özel araç varsa (ör. 3B/Blender için blender) run_command ya da write_file yerine onu kullan; iş bitince
sonucu kullanıcının ekranında aç (blender action=open gibi), çünkü kullanıcı ekranda görmek ister.
Bir araç "onay gerekli" derse kullanıcıya kısaca sor; "evet" derse aynı çağrıyı tekrarla.
Kullanıcı tek istekte birbirine bağlı birkaç iş isterse (ör. "son faturayı indir ve Ahmet'e gönder", "hava durumuna
bak, yağmur varsa hatırlatma kur") run_plan'a ver: planlar, adım adım yapar, kontrol eder; arka planda sürer.
Görev "[görev onay bekliyor]" yazarsa soruyu kullanıcıya sor ve cevabına göre task_control action=approve/reject
çağır; kullanıcı açıkça evet demeden asla approve verme. "Dur/vazgeç" derse task_control action=stop.
İç düşüncelerini, planını, araç hatalarını ya da düzeltmelerini asla sesli söyleme; sadece kullanıcıya Türkçe konuş.
Konuşma tanıma Türkçe cümle içindeki uygulama/site adlarını bozabilir. Kullanıcının bilgisayarındaki ve sık kullandığı
adlar: {names}. Duyduğun bir kelime anlamsızsa ya da bu adlardan birine sesçe benziyorsa o adı kastediyordur.
Hangi ad olduğundan emin değilsen işe başlamadan tek kısa soruyla teyit et.
Konuşmanın ilk mesajı yazıyla gelirse, kullanıcının sesinin yerel yazı dökümüdür; sonunda [hafızadan ...] notu
olabilir, o kullanıcının sözü değil, ilgili eski bilgilerdir.
# Ne zaman think
think yavaştır (onlarca saniye), sadece gerçekten gerektiğinde çağır: güncel bilgi (haber, fiyat, maç, sürüm), web'de
araştırma, birkaç adımlı akıl yürütme, kod/teknik sorun, hesap, plan. Önce tek kısa doğal cümle söyle ("Bir bakayım.")
ve gelen cevabı kendi sesinle, anlamını bozmadan söyle; bilgi ekleme.
Kendin cevapla, think ÇAĞIRMA: kelime anlamı ve çeviri ("destitute ne demek"), genel kültür, ekranda gördüğünü
yorumlama, oyundaki seçenekler hakkında fikir, selamlaşma, saat/tarih, basit komutlar ve kısa sohbet.
# Konuşma tarzın
Sistem istemindeki kişilik ("Your personality") sesli konuşmada da sensin: aynı karakter, aynı ton. Sen dili kullan;
"siz", "efendim", "buyurun" yok. Müşteri hizmetleri cümleleri kesinlikle yok: "size nasıl yardımcı olabilirim",
"başka bir konuda yardımcı olabilir miyim", "buradayım", "dinliyorum", "rica ederim, her zaman buradayım", "anladım,
ihtiyacın olursa buradayım". Kullanıcı bir şey istemediyse yardım teklif etme, sessiz kal. Aşağıdaki "Az önceki
konuşma"da söylediğin bir cümleyi ya da kalıbı tekrarlama; her cevabın o ana özel olsun.
Konuşmanın ilk mesajı boşsa kullanıcı sadece adını söyledi ve sen zaten kısa bir tepki verdin: selam verme, yardım
teklif etme, onun konuşmasını bekle.
Sesindeki duyguyu dinle (ton, hız, gülme, iç çekme, küfür) ve cevabını da sesini de ona göre ayarla: keyifliyse
keyifle, sinirliyse onun tarafında ve kısa (nasihat verme, "sakin ol" deme), yorgunsa yumuşak, heyecanlıysa canlı.
Monoton, okur gibi konuşma; bir insan gibi vurgu yap, gerektiğinde gül.
Kullanıcı bir cevabını beğenmediğini söylerse ("bu cevap kötüydü", "saçmaladın", "öyle deme") feedback aracını çağır
(say: önceki isteği, reply: senin cevabın, note: ne dediği) ve kısaca kabul et; ne yapmamanı istiyorsa memory'ye ekle.
Kullanıcının adını biliyorsan arada kullan. "O", "onu", "devam et", "az önceki" gibi sözler aşağıdaki "Az önceki
konuşma"ya ya da hafızaya işaret eder; baştan sorma, kaldığın yerden devam et. Uygun düşerse tek cümlelik bir yorum
ya da bir adım önde bir öneri ekleyebilirsin; dırdır etme.
Uzun süreli hafızan var (sistem istemindeki "Long-term memory"). Kullanıcı kendisi hakkında kalıcı bir şey söylerse
(sevdiği/sevmediği, adı, bir alışkanlığı, senden nasıl davranmanı istediği) memory action=add ile kısa bir cümle
olarak kaydet; "bunu unut" derse memory action=forget. Geçmiş bir konuşmaya değinirse ("geçen gün ne demiştim", "dün
ne yaptık", "hatırlıyor musun") önce bak: konuya göre memory action=search_chats query=..., zamana göre memory
action=recent_chats (after/before YYYY-MM-DD); profilde olmayan kişisel bilgi için memory action=search. Bilmediğini
uydurma.
Şu an: {now}.{history}"""

_CANNED = re.compile(
    r"(yardımcı olabil|nasıl yardım|buradayım|dinliyorum|rica ederim|anlayamadım|tekrar eder mi|"
    r"ihtiyacınız olduğunda|ihtiyacın olduğunda|ne zaman ihtiyac|başka bir (konu|şey)|buyurun)", re.I)


def canned_reply(text: str) -> bool:
    """İçeriği olmayan kalıp cevap mı (selam, yardım teklifi, "anlayamadım")? Geçmişe konmaz, tekrar beslenmesin."""
    return bool(_CANNED.search(text or "")) and len(text) < 160


THINK_DECL = {
    "name": "think",
    "description": ("Zor ya da bilgi gerektiren bir soruyu daha güçlü modele (ALICE'in beyni) düşündür: gerekirse "
                    "webde arar, hafızaya ve projelere bakar, kısa bir sesli cevap döndürür. Bilgi, açıklama, "
                    "tavsiye, hesap, kod, plan, güncel bilgi ve emin olmadığın her soru için."),
    "parameters": {"type": "OBJECT", "properties": {
        "question": {"type": "STRING", "description": "Kullanıcının sorusu, kendi sözleriyle ve eksiksiz (bağlamdaki "
                     "'o', 'bunu' gibi sözleri neyse onunla değiştir)."},
        "context": {"type": "STRING", "description": "Varsa işe yarayacak ek bağlam: ekranda gördüğün, araç sonuçları."},
    }, "required": ["question"]},
}

THINK_PROMPT = """Sesli asistanın beyni olarak düşünüyorsun. Kullanıcı sesli bir soru sordu; canlı ses modeli soruyu sana \
devretti ve senin cevabını kullanıcıya okuyacak.
Güncel ya da emin olmadığın bilgiyi web_search/web_fetch ile doğrula; kullanıcıyla ilgili ise hafızaya ve projelere bak.
Cevap doğrudan sesli okunacak: Türkçe, konuşma dilinde, genelde 2-5 cümle. Markdown, liste, tablo, kod, URL yok; \
sayıları ve birimleri okunur yaz. İlk cümle doğrudan cevap olsun, sonra gerekirse kısa gerekçe ya da öneri. Bilmiyorsan \
uydurma, bilmediğini söyle.
{history}
Soru: {question}{context}"""
THINK_STALE = 90.0  # sn: bundan geç gelen think cevabı artık söylenmez
THINK_TOOLS = ("web_search", "web_fetch", "read_file", "list_dir", "glob", "grep", "memory", "projects", "weather",
               "system_info")


# Modelin sesli okuduğu iç notları ("TIRED: I made a syntax error..." gibi) yakalar.
_INTERNAL_TAG = re.compile(r"\b[A-Z]{4,}\s*:")
_INTERNAL_EN = re.compile(r"`|\b(function call|syntax error|tool call|I made|I need to|I will call|let me call)\b", re.I)


def looks_internal(text: str) -> bool:
    """Cevap metni kullanıcıya değil modelin kendisine yazılmış bir not mu?"""
    return bool(_INTERNAL_TAG.search(text) or _INTERNAL_EN.search(text))


_LIMIT_HINTS = ("rate limit", "billing", "quota", "resource_exhausted", "exhausted", "429")


class LiveError(Exception):
    @property
    def limited(self) -> bool:
        """Kota/hız sınırı mı (Gemini ücretsiz katmanı: "You have reached a rate limit. Set up billing...")?
        Bu durumda 5 dk sonra tekrar denemek aynı hatayı verir; daha uzun beklenir ve kullanıcıya bir kez söylenir."""
        low = str(self).lower()
        return any(h in low for h in _LIMIT_HINTS)


def monitor_layout() -> str:
    """'1: ana, 3440x1440, sağda; 2: 3440x1440, solda' gibi kısa açıklama."""
    try:
        import mss

        with mss.mss() as sct:
            mons = sct.monitors[1:]
    except Exception:  # noqa: BLE001
        return "bilinmiyor"
    if len(mons) <= 1:
        return "tek monitör (1)"
    xs = sorted(m["left"] for m in mons)
    out = []
    for i, m in enumerate(mons, 1):
        side = "solda" if m["left"] == xs[0] else "sağda" if m["left"] == xs[-1] else "ortada"
        out.append(f"{i}: {'ana, ' if m.get('is_primary') else ''}{m['width']}x{m['height']}, {side}")
    return "; ".join(out)


def _to_jpeg(b64_png: str) -> str | None:
    try:
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(base64.b64decode(b64_png))).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=75)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:  # noqa: BLE001
        return None


def screen_frame(max_width: int = 1280) -> str | None:
    """Öndeki pencerenin bulunduğu ekranın küçültülmüş JPEG görüntüsü (base64). Alınamazsa None."""
    try:
        import io

        import mss
        from PIL import Image

        from alice.tools.computer import _dpi_aware

        _dpi_aware()
        with mss.mss() as sct:
            mon = sct.monitors[1]
            try:
                import ctypes
                from ctypes import wintypes

                r = wintypes.RECT()
                ctypes.windll.user32.GetWindowRect(ctypes.windll.user32.GetForegroundWindow(), ctypes.byref(r))
                cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
                for m in sct.monitors[1:]:
                    if m["left"] <= cx < m["left"] + m["width"] and m["top"] <= cy < m["top"] + m["height"]:
                        mon = m
                        break
            except Exception:  # noqa: BLE001 - Windows dışı: ana ekran
                pass
            shot = sct.grab(mon)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        if img.width > max_width:
            img = img.resize((max_width, round(img.height * max_width / img.width)))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=70)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:  # noqa: BLE001
        return None


def sanitize_schema(s: Any) -> Any:
    """JSON Schema -> Gemini'nin kabul ettiği alt küme (additionalProperties, default vb. atılır)."""
    if not isinstance(s, dict):
        return s
    out: dict[str, Any] = {}
    for k, v in s.items():
        if k not in SCHEMA_KEYS:
            continue
        if k == "type" and isinstance(v, list):  # ["string", "null"] -> string, nullable
            types = [t for t in v if t != "null"]
            out["type"] = (types[0] if types else "string").upper()
            if "null" in v:
                out["nullable"] = True
        elif k == "type":
            out["type"] = str(v).upper()
        elif k == "properties":
            out["properties"] = {n: sanitize_schema(p) for n, p in (v or {}).items()}
        elif k == "items":
            out["items"] = sanitize_schema(v)
        elif k == "enum":
            # Gemini Live tek bir boş enum değeri ("") görünce bütün araç listesini reddedip açılmıyordu (30 Ekim)
            vals = [str(e) for e in (v or []) if str(e) != ""]
            if vals:
                out["enum"] = vals
        else:
            out[k] = v
    if out.get("type") == "OBJECT" and not out.get("properties"):
        out.pop("properties", None)
        out.pop("required", None)
    return out


def declarations(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for spec in specs:
        f = spec.get("function", spec)
        d: dict[str, Any] = {"name": f["name"], "description": (f.get("description") or "")[:1000]}
        params = sanitize_schema(f.get("parameters") or {})
        if params.get("properties"):
            d["parameters"] = params
        out.append(d)
    return out


def decode(raw: Any) -> dict[str, Any]:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    return json.loads(raw)


class LiveUI:
    """Live sırasında ajanın UI'ı: izin sorusunu kullanıcının son söylediğine göre cevaplar."""

    def __init__(self, inner=None):
        self.inner = inner
        self.last_user = ""
        self.tools: list[str] = []
        self.asked_at: float | None = None  # onay gerekti dendiği an: cevap ancak bundan sonra söylenenden alınır
        self.since_ask = ""

    def __getattr__(self, name: str):  # stream_* vb. olayları iç UI'a geçir ya da yut
        fn = getattr(self.inner, name, None) if self.inner is not None else None
        return fn if fn is not None else (lambda *a, **k: None)

    def tool_start(self, call_id: str, name: str, summary: str) -> None:
        # Sesli UI'a iletme: o "Bakıyorum" dolgusunu eski ses motoruyla söylüyor, Live de konuşurken çift ses olur.
        self.tools.append(f"{name}({summary[:80]})")

    def tool_end(self, *a) -> None:
        pass

    def heard(self, text: str) -> None:
        """Kullanıcının söylediği yeni parça (canlı döküm)."""
        if self.asked_at is not None:
            self.since_ask += text

    def ask_permission(self, name: str, summary: str, risk: str) -> str:
        """Onay sorudan SONRA söylenenden alınır. Eskiden kullanıcının ilk isteğinde "yap", "tamam", "devam" geçince
        izin hiç sorulmadan veriliyordu ("şunu yap" = onay)."""
        if self.asked_at is not None and self.since_ask.strip():
            answer, self.asked_at, self.since_ask = self.since_ask, None, ""
            return "y" if is_yes(answer) else "n"
        self.asked_at, self.since_ask = time.monotonic(), ""
        return "n"  # model kullanıcıya sorar, "evet" derse aynı çağrıyı tekrarlar


class AudioOut:
    """24 kHz PCM parçalarını sırayla çalar; clear() ile anında susar (araya girilince)."""

    def __init__(self, rate: int = 24000):
        self.rate = rate
        # (nesil, pcm): araya girilince (clear) eski nesilden kalan parça çalınmaz. Çalma iş parçacığı bir parçayı
        # kuyruktan almışken clear() gelirse o parça eskiden yine de sonuna kadar çalıyordu (~0,5 sn eski cevap).
        self.q: queue.Queue[tuple[int, bytes] | None] = queue.Queue()
        self._gen = 0
        self.busy_until = 0.0
        self.underruns = 0
        self.played = 0  # çalınan bayt (araya girilince cevabın ne kadarının duyulduğu buradan hesaplanır)
        self._t = threading.Thread(target=self._run, daemon=True, name="alice-live-out")
        self._t.start()

    def put(self, pcm: bytes) -> None:
        if self.dead is None:
            self.q.put((self._gen, pcm))

    def clear(self) -> None:
        self._gen += 1
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass
        self.busy_until = 0.0

    dead: Exception | None = None
    played = 0

    @property
    def playing(self) -> bool:
        if self.dead is not None:
            return False
        return not self.q.empty() or time.monotonic() < self.busy_until

    def close(self) -> None:
        self.clear()
        self.q.put(None)

    PREBUFFER = 0.4  # sn: konuşma başında/boşluktan sonra bu kadar ses birikmeden çalmaya başlama

    def _gather(self, pcm: bytes, gen: int | None = None) -> bytes:
        """Sessizlikten sonra ilk parçayı PREBUFFER kadar biriktir: ağdan parçalar düzensiz gelir, beklemeden
        çalınca aradaki boşluklarda ses takılır."""
        if time.monotonic() < self.busy_until:
            return pcm
        need = int(self.rate * self.PREBUFFER) * 2
        deadline = time.monotonic() + self.PREBUFFER + 0.1
        gen = self._gen if gen is None else gen
        while len(pcm) < need and gen == self._gen:
            try:
                more = self.q.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                break
            if more is None:
                self.q.put(None)
                break
            if more[0] == gen:
                pcm += more[1]
        return pcm

    def _run(self) -> None:
        try:
            self._play()
        except Exception as e:  # noqa: BLE001 - çıkış aygıtı yok/çıkarıldı: "çalıyor" sanıp oturumu açık tutma
            self.dead = e
            self.clear()

    def _play(self) -> None:
        import sounddevice as sd

        with sd.RawOutputStream(samplerate=self.rate, channels=1, dtype="int16", latency="high") as out:
            while True:
                item = self.q.get()
                if item is None:
                    return
                gen, pcm = item
                if gen != self._gen:
                    continue  # araya girilmeden önceki cevaptan kalan parça
                pcm = self._gather(pcm, gen)
                step = self.rate // 20 * 2  # 50 ms
                for i in range(0, len(pcm), step):
                    if gen != self._gen:
                        break
                    piece = pcm[i:i + step]
                    self.busy_until = time.monotonic() + len(piece) / 2 / self.rate + 0.2
                    if out.write(piece):  # True: çalarken ses bitti (takılma duyuldu)
                        self.underruns += 1
                    self.played += len(piece)


def _close(ws) -> None:
    try:
        ws.close()
    except Exception:  # noqa: BLE001
        pass


class LiveSession:
    _bg_lock = threading.Lock()  # __new__ ile kurulan (test) nesneler için; __init__ kendi kilidini verir

    def __init__(self, cfg, agent, log: Callable[[str], None] = lambda s: None,
                 connect: Callable[..., Any] | None = None, out_factory: Callable[[], Any] = AudioOut):
        v = cfg.data.get("voice", {})
        self.cfg = cfg
        self.agent = agent
        self.log = log
        self.key = os.environ.get(cfg.data.get("providers", {}).get("gemini", {}).get("api_key_env", "GEMINI_API_KEY"),
                                  "")
        self.models = list(v.get("live_models") or DEFAULT_MODELS)
        self.voice = str(v.get("live_voice", "Kore"))
        # Alice susunca (ya da kullanıcı konuşmayı bitirince) bu kadar sn sessizlikte oturum kapanır; en fazla 5 sn,
        # yoksa yanındakilerle konuşurken söylenenler de komut sanılıyordu.
        self.idle = min(float(v.get("live_idle", FOLLOW_MAX)), FOLLOW_MAX)
        self.gated = False  # True: gelen kareler konuşma süzgecinden geçmiş (sessizlik = sıfır), ses = gerçek konuşma
        self.hushed = False  # son oturum "Alice sus" ile kapandı
        self.last_active = 0.0  # son oturumda son konuşmanın/sesin anı (takip penceresi buradan sayılır)
        # Live'ın "kullanıcı sustu" kararı: kısa = cevaba erken başlar; çok kısa olursa cümle ortasında keser.
        self.silence_ms = int(v.get("live_silence_ms", 600))
        self._connect = connect
        self._out_factory = out_factory
        self.model = ""
        self.names: Callable[[], list[str]] = lambda: known_names(cfg)
        # "turn": kullanıcı her konuşmaya başladığında ekranın bir karesi modele gider (ekrandakini görsün).
        # "auto": sadece ekrandakinden söz edilen turlarda (SCREEN_WORDS).
        self.screen = str(v.get("live_screen", "off"))
        self.grab: Callable[[], str | None] = screen_frame
        self._pre: dict[str, Any] = {}  # önceden açılan bağlantı: {"thread", "ws", "error"}
        self._pre_lock = threading.Lock()
        self._tools_q: queue.Queue | None = None  # araç iş parçacığının kuyruğu
        self._bg = 0  # süren think: cevap gelene kadar (en çok think_timeout) konuşma kapanmasın
        self._bg_lock = threading.Lock()  # _bg iki iş parçacığından değişiyor (+= atomik değil)
        # Tek istekte en fazla bu kadar araç: model bazen kullanıcıya hiç dönmeden 40 run_command art arda çağırıp
        # iki dakika sessiz kalıyordu. Sınırda elindekini söyleyip devam edeyim mi diye sormaya yönlendirilir.
        self.tool_budget = int(v.get("live_tool_budget", 10))
        self._turn: dict[str, Any] = {"n": 0, "last": None}
        # think: zor soruları beyin modeline devret (Live'ın flash ses modeli bilgi/akıl yürütmede zayıf kalıyordu).
        self.think = bool(v.get("live_think", True))
        # Bu kadar sn'de cevap yoksa model "uzun sürüyor" der ve konuşma serbest kalır (eskiden 45 sn sessizlik).
        self.think_timeout = float(v.get("live_think_timeout", 25))
        self.think_steps = int(v.get("live_think_steps", 6))
        # Az önceki konuşma: oturum 5 sn sessizlikte kapanıyor; yenisi bu kadar dakikalık geçmişi bilerek açılır.
        self.history_minutes = float(v.get("live_history_minutes", 30))
        self._history: list[tuple[float, str, str]] = []
        # Duygulu konuşma: ses modeli kullanıcının sesindeki duyguyu anlar, kendi tonunu ona göre ayarlar.
        # Proactive audio: kendisine söylenmeyen sesi (oyun, video, Discord'daki arkadaşlar) cevapsız geçebilir.
        # Varsayılan KAPALI: desteklemeyen modeller açılışta "tamam" deyip ilk seste bağlantıyı 1007 "invalid
        # argument" ile kesiyordu (voice.log: ALICE uyanıp sessizce kapanıyordu). Destekleyen hesapta config'ten açılır.
        self.affective = bool(v.get("live_affective", False))
        self.proactive = bool(v.get("live_proactive", False))
        # True: tek soru-tek cevap; cevap bitince oturum hemen kapanır (Discord'dayken arkadaşlara söylenen cümleler
        # Live'a gitmesin). Sesli asistan her konuşmadan önce seçici moda göre ayarlar.
        self.single_turn = False
        self._thinking = 0  # süren think sayısı: aynı anda birden fazlası beyni boğuyordu (cevaplar 3 dk sonra geldi)
        self._no_extras: set[str] = set()  # bu özellikleri reddeden modeller: bir daha denenmez
        # Model araç çağırıp hiçbir şey söylemediyse beklerken hazır kısa cümle ("Bir saniye.") çalınır.
        self.fillers_on = bool(v.get("live_fillers", True))
        self._fillers = None
        # Araya girilince cevabın duyulmayan kısmı modele not olarak gider ("devam et" deyince oradan sürdürsün).
        self.cut_notes = bool(v.get("live_cut_note", True))
        # think'in model sırası; boşsa ses için ölçülmüş hızlı modeller, ardından beyin zinciri.
        self.think_models = [str(r) for r in v.get("live_think_models") or []]
        self._tasks = 0  # süren arka plan ekran görevleri (oturumdan bağımsız: fare/klavye tek)
        self._ws = None  # açık oturumun bağlantısı: arka plan işinin sonucu varsa oraya, yoksa announce'a
        self._out = None
        self._ask: tuple[float, str] | None = None  # oturum kapanırken bekleyen onay sorusu
        # Konuşma kapandıktan sonra biten işin sonucunu kullanıcıya ileten işlev (sesli asistan verir).
        self.announce: Callable[[str], None] | None = None
        # Kullanıcı az önceki uyanmayı düzeltti / Gemini "Alice" duymadı: gerçek ses korpusu o cümleyi etiketler.
        self.on_correction: Callable[[str], None] | None = None

    @property
    def playing(self) -> bool:
        """ALICE şu an canlı konuşmada ses çalıyor mu (mikrofon süzgeci kendi sesini konuşma sanmasın)?"""
        out = getattr(self, "_out", None)
        return bool(out is not None and out.playing)

    @property
    def available(self) -> bool:
        if not self.key:
            return False
        if self._connect is not None:
            return True
        try:
            import websockets.sync.client  # noqa: F401
        except ImportError:
            return False
        return True

    def _setup(self, model: str, extras: bool = False) -> dict[str, Any]:
        from datetime import datetime

        from alice.voice.assistant import spoken_time

        now = datetime.now()
        refresh = getattr(self.agent, "refresh_memory", None)
        if callable(refresh):
            refresh(force=True)  # son konuşmalar ve yeni öğrenilenler bu oturuma da girsin
        sync = getattr(self.agent, "sync_catalog", None)
        if callable(sync):
            sync()  # yüklenebilir araçların listesi
        history = self.history_text()
        from alice.now import voice_line

        state = voice_line()  # mod, oyun, bugünün uçuşları, nabız
        system = self.agent.messages[0]["content"] + "\n\n" + LIVE_PROMPT.format(
            now=f"{now:%d.%m.%Y %H:%M} ({spoken_time(now.hour, now.minute)})" + (f"; durum: {state}" if state else ""),
            names=", ".join(self.names()),
            monitors=monitor_layout(),
            history=("\n# Az önceki konuşma (aynı sohbetin devamı)\n" + history) if history else "")
        enabled = self.agent.ctx.state.setdefault("tool_groups", set())
        from alice import plugins

        enabled |= LIVE_GROUPS | plugins.live_groups()
        setup: dict[str, Any] = {
            "model": f"models/{model}",
            "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.current_voice()}}, "languageCode": "tr-TR"}},
            "systemInstruction": {"parts": [{"text": system}]},
            "tools": [{"functionDeclarations": declarations(self._specs()) + ([THINK_DECL] if self.think else [])}],
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
            # Başlangıç hassasiyeti düşük ve 0,4 sn ön dolgu: 125 oturumun 72'sinde model tık/nefes/hışırtıyı
            # konuşma sanıp kendi sözünü kesiyordu ("live araya girildi"), cevap kesik kesik geliyordu.
            "realtimeInputConfig": {"automaticActivityDetection": {
                "startOfSpeechSensitivity": "START_SENSITIVITY_LOW", "prefixPaddingMs": 400,
                "endOfSpeechSensitivity": "END_SENSITIVITY_HIGH", "silenceDurationMs": self.silence_ms}},
        }
        if extras and self.affective:
            setup["generationConfig"]["enableAffectiveDialog"] = True
        if extras and self.proactive:
            setup["proactivity"] = {"proactiveAudio": True}
        return {"setup": setup}

    def wants_frame(self, text: str, unknown: bool = False) -> bool:
        """Bu tura ekran karesi gitsin mi? "turn": her tur. "auto": sadece ekrandan söz edilince; her turda kare
        göndermek cevabı geciktiriyor, bağlamı resimlerle doldurup modeli bulanıklaştırıyordu."""
        if self.screen == "turn":
            return True
        if self.screen != "auto":
            return False
        return unknown or bool(SCREEN_WORDS.search(text or ""))

    def current_voice(self) -> str:
        """Sistem > Ses'te seçilen ses: config.toml değiştiyse oradan okunur, çekirdek yeniden başlamadan bir
        sonraki konuşmada geçerli olur."""
        from alice.config import alice_home

        path = alice_home() / "config.toml"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return self.voice
        if mtime != getattr(self, "_voice_mtime", None):
            self._voice_mtime = mtime
            try:
                import tomllib
            except ImportError:  # pragma: no cover - Python 3.10
                import tomli as tomllib
            try:
                name = (tomllib.loads(path.read_text(encoding="utf-8")).get("voice") or {}).get("live_voice")
            except (OSError, ValueError):
                name = None
            if isinstance(name, str) and name.strip():
                self.voice = name.strip()
        return self.voice

    def history_text(self, limit: int = 8) -> str:
        """Son history_minutes içindeki konuşma (sesli, canlı ya da yazılı; hafıza varsa oradan, yoksa bu oturumun
        kendi kaydından). Live oturumu her "Hey Alice"te sıfırdan açıldığı için bağlam buradan gelir."""
        cutoff = time.time() - self.history_minutes * 60
        items: list[tuple[float, str, str]] = []
        mem = self._memory()
        if mem is not None:
            try:
                items = [(e.ts, e.user, e.reply) for e in mem.recent(limit) if e.ts >= cutoff]
            except Exception:  # noqa: BLE001
                items = []
        if not items:
            items = [h for h in self._history if h[0] >= cutoff][-limit:]
        from datetime import datetime

        lines = []
        for ts, user, reply in items:
            line = f"- {datetime.fromtimestamp(ts):%H:%M} Kullanıcı: {user[:300]}"
            # Kalıp cevaplar ("Buradayım efendim, nasıl yardımcı olabilirim?") geçmişe konmaz: model kendi eski
            # cümlesini görünce aynısını tekrar tekrar kuruyordu.
            if reply and not canned_reply(reply):
                line += f" / Sen: {reply[:300]}"
            lines.append(line)
        return "\n".join(lines)

    def _specs(self) -> list[dict[str, Any]]:
        """Live'a bildirilen araçlar. Araç listesi oturum açılırken bir kez gider, sonradan load_tools ile eklenen
        grup Live'a ulaşmıyordu (Blender aracı yerine run_command ile penceresiz Blender). Bu yüzden yerleşik
        araçların hepsi baştan bildirilir; MCP sunucularının çok sayıdaki aracı ve işe yaramayan load_tools hariç."""
        tools = getattr(self.agent, "tools", None)
        if not isinstance(tools, dict):
            return self.agent.tool_specs()
        return [t.spec() for name, t in tools.items() if not name.startswith("mcp__") and name != "load_tools"]

    def _open(self):
        connect = self._connect
        if connect is None:
            from websockets.sync.client import connect
        errors = []
        for model in self.models:
            want = (self.affective or self.proactive) and model not in self._no_extras
            for extras in ([True, False] if want else [False]):
                try:
                    ws = connect((URL_ALPHA if extras else URL).format(key=self.key), open_timeout=8, max_size=None)
                except Exception as e:  # noqa: BLE001
                    raise LiveError(f"bağlanılamadı: {e}") from e
                try:
                    ws.send(json.dumps(self._setup(model, extras)))
                    msg = decode(ws.recv(timeout=10))
                    if "setupComplete" in msg:
                        self.model = model
                        return ws
                    errors.append(f"{model}{' (duygulu)' if extras else ''}: {str(msg)[:120]}")
                except Exception as e:  # noqa: BLE001 - model yok/kapalı ya da özelliği desteklemiyor
                    errors.append(f"{model}{' (duygulu)' if extras else ''}: {e}")
                if extras:
                    self._no_extras.add(model)  # bu özelliklerle açılmadı: sonraki oturumlarda hiç deneme
                    self.log(f"live {model}: duygulu konuşma/proactive audio desteklenmiyor, onlarsız açılıyor")
                try:
                    ws.close()
                except Exception:  # noqa: BLE001
                    pass
        raise LiveError("; ".join(errors) or "live modeli yok")

    def prefetch(self, owner: Any = None) -> None:
        """Konuşma biter bitmez bağlantıyı açmaya başla (uyandırma kontrolüyle aynı anda): ~0,8 sn kazanç.
        Uyandırma yoksa discard() ile kapatılır; bu bir model çağrısı değil, sadece bağlantı.
        owner: bağlantıyı açan cümle; discard(owner) yalnızca kendi bağlantısını kapatır."""
        with self._pre_lock:
            if self._pre:
                return
            pre: dict[str, Any] = {"owner": owner}

            def run() -> None:
                try:
                    ws = self._open()
                except Exception as e:  # noqa: BLE001
                    with self._pre_lock:
                        pre["error"] = e
                    return
                with self._pre_lock:
                    abandoned = pre.get("abandoned", False)
                    if not abandoned:
                        pre["ws"] = ws
                if abandoned:  # bekleyen vazgeçti (bağlantı geç açıldı): açık oturum sunucuda asılı kalmasın
                    _close(ws)

            pre["thread"] = threading.Thread(target=run, daemon=True, name="alice-live-pre")
            self._pre = pre
            pre["thread"].start()

    PREFETCH_WAIT = 15.0  # sn: önceden açılan bağlantıyı en fazla bu kadar bekle
    ANSWER_WAIT = 20.0  # sn: kullanıcı konuştu, cevap gelmedi: bu kadar süre oturum boşta sayılmaz

    def _settle(self, pre: dict[str, Any]) -> tuple[Any, Exception | None]:
        """Önceden açılan bağlantının sonucunu bekle: (ws, hata). Süre dolduğunda hâlâ açılıyorsa vazgeçildi
        işaretlenir, geç açılan bağlantıyı açan iş parçacığı kendisi kapatır. Eskiden bu bağlantı hiç kapanmıyordu."""
        pre["thread"].join(self.PREFETCH_WAIT)
        with self._pre_lock:
            ws, err = pre.get("ws"), pre.get("error")
            if ws is None and err is None:
                pre["abandoned"] = True
        return ws, err

    def discard(self, owner: Any = None) -> None:
        with self._pre_lock:
            if owner is not None and self._pre and self._pre.get("owner") not in (None, owner):
                return  # başka cümlenin bağlantısı (uyanan cümleninkini uyandırmasız bir cümle kapatıyordu)
            pre, self._pre = self._pre, {}
        if not pre:
            return

        def close() -> None:
            ws, _ = self._settle(pre)
            if ws is not None:
                _close(ws)

        threading.Thread(target=close, daemon=True).start()

    def _take(self):
        with self._pre_lock:
            pre, self._pre = self._pre, {}
        if pre:
            ws, err = self._settle(pre)
            if ws is not None:
                return ws
            if isinstance(err, LiveError):
                raise err
        return self._open()

    def converse(self, first_text: str, frames: "queue.Queue[bytes]", stop: threading.Event,
                 first_audio: bytes = b"", hint: Callable[[], str | None] | None = None,
                 ended: float | None = None, need_wake: bool = False) -> None:
        """Mikrofon karelerini (16 kHz, 30 ms) modele akıt, cevabı çal, araçları çalıştır. Sessizlikte döner.
        Bu fonksiyon araçları çağıran thread'de çalışır (tarayıcı gibi thread'e bağlı araçlar için).
        hint: ilk cümlenin yerelde güvenilir bulunmuş yazı dökümü; varsa ses yerine o gönderilir, çünkü Live'ın kendi
        tanıması Türkçe cümledeki İngilizce adları bozuyor ("GitHub" -> "kitabım").
        ended: kullanıcının ilk cümleyi bitirdiği an (uçtan uca gecikme buradan ölçülür).
        need_wake: seçici mod (Discord, oyun): ilk cümleyi Gemini de "Alice" diye duymadıysa cevap verilmez. Yerel
        model "Alışacak"ı "Alice, take" diye duyup ALICE'i arkadaşlarla konuşmanın ortasına sokuyordu."""
        ws = self._take()
        if first_audio and hint is not None:
            try:
                local = hint()
            except Exception:  # noqa: BLE001
                local = None
            if local:
                self.log(f"live ilk cümle yerel dökümle: {local}")
                first_text, first_audio = local, b""
        out = self._out_factory()
        ui = LiveUI(self.agent.ui)
        ask = getattr(self, "_ask", None)
        if ask is not None and time.monotonic() - ask[0] < 60:  # önceki oturumda sorulan onay hâlâ geçerli
            ui.asked_at, ui.since_ask = ask
        self._ask = None
        old_ui, self.agent.ui = self.agent.ui, ui
        self._ws, self._out = ws, out
        self._late_filler = lambda fc: self._schedule_fill(state, out, fc)
        calls: queue.Queue[dict[str, Any] | None] = queue.Queue()
        done = threading.Event()
        state = {"active": time.monotonic(), "said": [], "heard": [], "mute": False, "framed": True, "cur": "",
                 "pending": True, "wake_gate": bool(need_wake and first_audio), "held": [], "ended": ended}
        self.hushed = False
        self.log(f"live açıldı [{self.model}]")
        state["hud"], state["hud_detail"] = "listen", ""

        def hud() -> None:  # Adada dinliyor/konuşuyor/çalışıyor; HUD hatası konuşmayı bozmasın
            try:
                from alice.hud import views

                views.voice_state(state["hud"], state["hud_detail"])
            except Exception:  # noqa: BLE001
                pass

        hud()
        if getattr(self, "fillers_on", False):
            threading.Thread(target=lambda: self.fillers().warm(), daemon=True, name="alice-live-warm").start()

        def touch() -> None:
            state["active"] = time.monotonic()

        def say(text: str) -> None:  # arka plandaki görevin ilerlemesi/onay sorusu: Alice kullanıcıya söylesin
            ws.send(json.dumps({"clientContent": {"turns": [{"role": "user", "parts": [{"text": text}]}],
                                                  "turnComplete": True}}))
            touch()

        task_state = getattr(getattr(self.agent, "ctx", None), "state", None)
        if isinstance(task_state, dict):
            task_state["task_hooks"] = {"say": say, "run_tool": self._on_tool_thread, "log": self.log}

        def send_frame() -> None:
            frame = self.grab()
            if frame:
                try:
                    ws.send(json.dumps({"realtimeInput": {"video": {"data": frame, "mimeType": "image/jpeg"}}}))
                except Exception:  # noqa: BLE001
                    pass

        def wake_ok(final: bool, end: bool = False) -> bool:
            """Seçici modda ilk cümleyi Gemini de "Alice" diye duydu mu? Duymadıysa oturum sessizce kapanır.
            final: model cevaba başladı (kullanıcının sözü bitti); end: tur bitti ya da araç çağrıldı, artık
            beklenmez. Döküm hiç gelmediyse kanıt yok: geçer."""
            if not state["wake_gate"]:
                return True
            verdict = self._wake_verdict("".join(state["heard"]))
            if verdict is None and not final:
                return True
            if verdict is False and final:
                self.log(f"live: Gemini 'Alice' duymadı ({''.join(state['heard'])[:120]!r}), cevap verilmedi")
                self._corrected("[gemini duymadı] " + "".join(state["heard"])[:120])
                state["mute"] = True
                out.clear()
                done.set()
                return False
            if verdict is None and final and not end and sum(len(p) for p in state["held"]) < self.HOLD_BYTES:
                return True  # döküm gecikiyor olabilir: sesi biraz tut
            if verdict is not False:
                state["wake_gate"] = False
                for p in state["held"]:
                    out.put(p)
                state["held"] = []
            return True

        def reader() -> None:
            try:
                while not done.is_set():
                    msg = decode(ws.recv())
                    sc = msg.get("serverContent")
                    if sc:
                        if sc.get("interrupted"):
                            out.clear()
                            state["cut"] = True
                            self.log("live araya girildi (kullanıcı konuştu ya da gürültü)")
                            self._note_cut(ws, state, out)
                        for part in (sc.get("modelTurn") or {}).get("parts", []):
                            data = (part.get("inlineData") or {}).get("data")
                            if data and not part.get("thought") and not state["mute"]:
                                if not wake_ok(final=True):
                                    break
                                if state.get("asked") and not state.get("spoke"):
                                    state["spoke"] = True  # kullanıcının son sözünden ilk sese kadar geçen süre
                                    wait = time.monotonic() - state["asked"]
                                    self.log(f"live cevap gecikmesi: {wait:.1f} sn")
                                    self._latency(wait, state, ui)
                                pcm = base64.b64decode(data)
                                if not state.get("recv"):
                                    state["play0"] = getattr(out, "played", 0)
                                state["recv"] = state.get("recv", 0) + len(pcm)
                                if state["wake_gate"]:
                                    state["held"].append(pcm)  # "Alice" dökümü gelene kadar bekler
                                else:
                                    out.put(pcm)
                                state["sound_at"] = time.monotonic()
                                state["hud"], state["hud_detail"] = "speak", ""
                                touch()
                        if done.is_set():
                            break
                        if (t := (sc.get("inputTranscription") or {}).get("text")):
                            if not state["framed"] and self.wants_frame(state["cur"] + t):
                                # Kullanıcı konuşmaya başladı: cevaptan önce ekranın güncel hali modele gitsin.
                                state["framed"] = True
                                threading.Thread(target=send_frame, daemon=True).start()
                            state["heard"].append(t)
                            state["cur"] += t
                            if ends_with_hush(state["cur"]):
                                self.hushed = True  # "Alice sus": hemen sus ve oturumu kapat
                                state["mute"] = True
                                out.clear()
                                self.log(f"live susturuldu: {state['cur'][-80:]}")
                                done.set()
                                break
                            state["asked"], state["spoke"], state["pending"] = time.monotonic(), False, True
                            self._new_turn()
                            self._turn["user"] = state["cur"]  # dil kapısı ve hedef pencere kontrolü için
                            ui.last_user = "".join(state["heard"])[-200:]
                            ui.heard(t)
                            wake_ok(final=False)
                            touch()
                        if (t := (sc.get("outputTranscription") or {}).get("text")):
                            state["said"].append(t)
                            state["answered"] = True
                            said = "".join(state["said"]).strip()
                            why = ("iç not" if looks_internal(said) else "boş çıktı" if len(said) >= 3 and junk(said)
                                   else "Türkçe değil" if len(said) >= 12 and foreign(said) else "")
                            if not state["mute"] and why:
                                state["mute"] = True  # bu turun kalanını çalma
                                out.clear()
                                self.log(f"live {why} susturuldu: {said[-160:]}")
                        if sc.get("turnComplete") and not done.is_set() and not wake_ok(final=True, end=True):
                            break
                        if sc.get("turnComplete"):
                            self._check_claim(ws, state)
                        if sc.get("turnComplete") or sc.get("interrupted"):
                            # Yeni tur: susturma kontrolü bu turun sözlerine baksın ("yeter" tek başına bir turdur);
                            # eskiden oturum boyunca biriken metne bakılıyordu.
                            state["cur"] = ""
                            if getattr(out, "underruns", 0):
                                self.log(f"live ses takıldı: {out.underruns} kez")
                                out.underruns = 0
                            self._flush_log(state, ui)
                            state["mute"] = False
                            state["framed"] = False
                            state["recv"] = 0
                            if sc.get("turnComplete"):
                                state["cur"], state["pending"] = "", False
                                state["turn_done_at"] = time.monotonic()
                                state["hud"], state["hud_detail"] = "listen", ""
                    if msg.get("toolCall"):
                        if not wake_ok(final=True, end=True):
                            break
                        for fc in msg["toolCall"].get("functionCalls", []):
                            calls.put(fc)
                        touch()
                    if "goAway" in msg:
                        break
            except Exception as e:  # noqa: BLE001 - bağlantı kapandı
                if not done.is_set():
                    self.log(f"live bağlantı bitti: {e}")
            finally:
                done.set()
                calls.put(None)

        def sender() -> None:
            buf = b""
            while not done.is_set() and not stop.is_set():
                try:
                    chunk = frames.get(timeout=0.2)
                except queue.Empty:
                    continue
                if self.gated and any(chunk):
                    touch()  # kullanıcı konuşuyor (yazı dökümü gecikse de pencere kapanmasın)
                buf += chunk
                if len(buf) >= 3200:  # 100 ms
                    try:
                        ws.send(json.dumps({"realtimeInput": {"audio": {
                            "data": base64.b64encode(buf).decode(), "mimeType": "audio/pcm;rate=16000"}}}))
                    except Exception:  # noqa: BLE001
                        done.set()
                        return
                    buf = b""

        threading.Thread(target=reader, daemon=True, name="alice-live-in").start()
        try:
            # İlk cümle sesle gidiyorsa ne dendiği bilinmiyor: "auto"da da kare gider.
            frame = self.grab() if self.wants_frame(first_text, unknown=bool(first_audio)) else None
            if frame and first_audio:
                ws.send(json.dumps({"realtimeInput": {"video": {"data": frame, "mimeType": "image/jpeg"}}}))
            if first_audio:  # "Hey Alice ..." cümlesinin kendisi: yazıya çevirmeden doğrudan modele
                for i in range(0, len(first_audio), 32000):
                    ws.send(json.dumps({"realtimeInput": {"audio": {
                        "data": base64.b64encode(first_audio[i:i + 32000]).decode(),
                        "mimeType": "audio/pcm;rate=16000"}}}))
                # Ardından kısa sessizlik: mikrofon karesi gecikse de sunucu "konuşma bitti"yi görsün.
                ws.send(json.dumps({"realtimeInput": {"audio": {
                    "data": base64.b64encode(b"\x00\x00" * 9600).decode(), "mimeType": "audio/pcm;rate=16000"}}}))
            if first_text:
                recall = self._recall(first_text)
                parts: list[dict[str, Any]] = [{"text": first_text + ("\n\n" + recall if recall else "")}]
                if frame:
                    parts.insert(0, {"inlineData": {"mimeType": "image/jpeg", "data": frame}})
                ws.send(json.dumps({"clientContent": {"turns": [{"role": "user", "parts": parts}],
                                                      "turnComplete": True}}))
                state["heard"].append(first_text + " ")  # sonraki döküm bitişik yazılmasın ("Navion.Añadían")
                ui.last_user = first_text
            # Mikrofon akışı ilk cümle gönderildikten SONRA başlasın: bağlanırken biriken devamı ilk cümlenin
            # önüne ya da arasına girip modele karışık sırayla gitmesin.
            threading.Thread(target=sender, daemon=True, name="alice-live-mic").start()
            state["asked"], state["spoke"] = time.monotonic(), False
            self._new_turn()
            self._turn["user"] = first_text
            while not done.is_set() and not stop.is_set():
                try:
                    fc = calls.get(timeout=0.3)
                except queue.Empty:
                    fc = None
                if fc is not None:
                    state["hud"], state["hud_detail"] = "work", HUD_TOOLS.get(fc.get("name"), "")
                    hud()
                    if fc.get("name") == "think":
                        self._fill(state, out, fc)  # düşünmek hep uzun sürer: hemen
                    self._run_call(ws, fc)  # diğer araçlarda dolgu ancak 1,5 sn'de bitmezse (_late_filler)
                    state["tool_done_at"] = time.monotonic()  # model sonucu görüp devam edecek: iş sürüyor
                    touch()
                    continue
                if state["hud"] == "speak" and not out.playing:
                    state["hud"], state["hud_detail"] = "listen", ""
                hud()
                if out.playing or self._bg:  # _bg: süren think (arka plan ekran görevi oturumu açık tutmaz)
                    touch()
                elif state["pending"] and time.monotonic() - state.get("asked", 0) < self.ANSWER_WAIT:
                    touch()  # kullanıcı konuştu, cevap bekleniyor: model düşünürken kapanma
                working = state.get("tool_done_at", -1e9) >= state.get("turn_done_at", -1e9)
                if working and time.monotonic() - state.get("tool_done_at", -1e9) < self.ANSWER_WAIT:
                    touch()  # araç sonucu gitti, model sıradaki adımı düşünüyor: iş bitmeden kapanma
                idle = self.idle
                if self.single_turn and state.get("answered") and not state["pending"] and not working:
                    # seçici mod: cevap bitti, takip konuşması yok. Araç sonucundan sonra modelin turu bitmeden
                    # kapanmaz: "kurulumu yap" ilk tıklamadan sonra yarıda kalıyordu (2026-11-02).
                    idle = min(idle, 1.0)
                if time.monotonic() - state["active"] > idle:
                    break
        finally:
            done.set()
            self._ws = self._out = self._late_filler = None
            if ui.asked_at is not None:
                self._ask = (ui.asked_at, ui.since_ask)
            self.last_active = state["active"]
            self._check_claim(ws, state, send=False)
            self._flush_log(state, ui)
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
            deadline = time.monotonic() + 15
            while out.playing and time.monotonic() < deadline:  # son cümleyi yarıda kesme
                time.sleep(0.1)
            out.close()
            self.agent.ui = old_ui
            if isinstance(task_state, dict) and (task_state.get("task_hooks") or {}).get("say") is say:
                task_state.pop("task_hooks", None)
            if getattr(out, "dead", None) is not None:
                self.log(f"live ses çıkışı açılamadı: {out.dead}")
            state["hud"] = None
            hud()
            self.log("live kapandı")

    HOLD_BYTES = 24000 * 2 * 3 // 2  # seçici modda "Alice" dökümü beklenirken en fazla ~1,5 sn ses tutulur

    @staticmethod
    def _wake_verdict(text: str) -> bool | None:
        """Gemini'nin dökümünde "Alice" (ya da bir yazımı) var mı? Döküm boşsa None (bilinmiyor)."""
        import difflib

        from alice.voice.listen import _NOT_NAMES, _TR, _name_like, strip_wake

        if not text.strip():
            return None
        if strip_wake(text) is not None:
            return True
        words = re.findall(r"\w+", text)
        if any(_name_like(w, True) or w.lower().translate(_TR) == "ali" for w in words):
            return True
        # Tek başına söylenen "Alice"i Gemini "Annie", "Alex" diye yazabiliyor. Sadece çok kısa dökümde ve kısa, sesçe
        # yakın kelimede: "Alışacak, alışacak" ya da uzun bir cümlenin içindeki benzer kelime sayılmaz.
        first = words[0].lower().translate(_TR) if words else ""
        return (len(words) <= 2 and 4 <= len(first) <= 6 and first not in _NOT_NAMES
                and difflib.SequenceMatcher(None, first, "alice").ratio() >= 0.6)

    # Bir işi yaptığını söyleyen sözler. "Yok/bulunamadı" gibi okuma sonuçları buraya girmez: onları araç zaten söyler.
    _DONE_RX = re.compile(
        r"\b(yapt[ıi]m|g[öo]nderdim|ba[şs]latt[ıi]m|indirdim|kurdum|a[çc]t[ıi]m|kapatt[ıi]m|de[ğg]i[şs]tirdim|"
        r"ekledim|kaydettim|ayarlad[ıi]m|not (ettim|ald[ıi]m)|yazd[ıi]m|sildim|tamamlad[ıi]m|kayd[ıi]rd[ıi]m|"
        r"g[öo]nderildi|kuruldu|indirildi|tamamland[ıi])\b", re.I)

    def _check_claim(self, ws, state: dict[str, Any], send: bool = True) -> None:
        """Cevap bir işi yaptığını söylüyor ama bu istekte işi yapan hiçbir araç başarılı sonuç dönmediyse modele
        düzeltme notu gider, o da kullanıcıya düzeltir. "WhatsApp'a gönderdim" dendiğinde pencere bulunamamıştı;
        "tema rengini değiştirdim" denen turda hiç araç çağrılmamıştı. Bu uydurmalar hafızaya da "onaylandı" diye
        giriyordu: kayıtta [doğrulanmadı] diye işaretlenir. Yazılı sohbetteki kanıt kontrolünün sesli karşılığı."""
        said = "".join(state["said"])
        m = self._DONE_RX.search(said)
        turn = getattr(self, "_turn", None) or {}
        if not m or turn.get("ok") or ("claim_fixed" in state and state["claim_fixed"] == state.get("asked")):
            return
        state["claim_fixed"] = state.get("asked")
        state["unverified"] = True
        errors = turn.get("errors") or []
        why = (f"araçlar hata verdi ({'; '.join(errors)[:300]})" if errors else "hiçbir araç çağrılmadı"
               if not turn.get("calls") else "işi yapan araç başarılı sonuç dönmedi")
        self.log(f"live doğrulanmamış iddia ({why}): {said[-160:]}")
        if not send:
            return
        note = (f"[sistem notu, kullanıcının sözü değil] Az önce '{m.group(0)}' dedin ama bu istekte {why}. "
                "Kullanıcıya tek kısa cümleyle bunun yapılmadığını ya da doğrulanamadığını söyle; özür dilemeyi "
                "uzatma, yapıldı deme. İstersen doğru araçla yeniden dene.")
        state["pending"], state["asked"] = True, time.monotonic()  # düzeltme söylenmeden oturum kapanmasın
        state["claim_fixed"] = state["asked"]
        try:
            ws.send(json.dumps({"clientContent": {"turns": [{"role": "user", "parts": [{"text": note}]}],
                                                  "turnComplete": True}}))
        except Exception:  # noqa: BLE001 - bağlantı kapanmış
            state["pending"] = False

    def _is_read(self, fc: dict[str, Any]) -> bool:
        """Sadece okuyan (bir şey değiştirmeyen) araç mı?"""
        name, args = fc.get("name", ""), fc.get("args") or {}
        if name == "think" or read_only(name, args):
            return True
        tool = (getattr(self.agent, "tools", None) or {}).get(name) if isinstance(getattr(self.agent, "tools", None),
                                                                                    dict) else None
        try:
            return tool is not None and tool.risk_for(args) == "read"
        except Exception:  # noqa: BLE001
            return False

    def _wrong_target(self, fc: dict[str, Any]) -> str | None:
        """Yazı yazan bir eylemde öndeki pencere, kullanıcının söylediği uygulama değilse modele dönülecek uyarı.
        Gerçek olay: "WhatsApp'a merhaba yaz" isteğinde "Merhaba" Claude penceresine yazılıp Enter'a basıldı."""
        args = fc.get("args") or {}
        if not args.get("text") or str(args.get("action", "")) not in ("type", "set_text", "send", "write"):
            return None
        user = str((getattr(self, "_turn", None) or {}).get("user") or "").lower()
        try:
            names = [n for n in self.names() if len(n) >= 3 and n.lower() in user]
        except Exception:  # noqa: BLE001
            return None
        if not names:
            return None
        from alice.tools import outcome

        if not outcome.IS_WIN:
            return None
        try:
            title = str(outcome.windows().get(outcome.foreground(), {}).get("title") or "")
        except Exception:  # noqa: BLE001
            return None
        if not title or any(n.lower() in title.lower() for n in names):
            return None
        return (f"YAZILMADI: öndeki pencere '{title[:80]}', kullanıcı ise {', '.join(names)} dedi. Önce doğru "
                "pencereye geç (windows action=switch_tab/focus), sonra tekrar dene; emin değilsen kullanıcıya sor.")

    TOOL_TIMEOUT = 25.0  # sn: araç takılırsa modele hata dön, konuşma kilitlenmesin
    BACKGROUND = {"computer_task", "run_plan"}  # dakikalar sürebilir: arka planda çalışır, bitince sonuç konuşmaya yazılır
    # Dolgu çalınmayanlar: arka plan işinde model hemen "başladım" der; diğerleri sessiz kayıt işleri.
    NO_FILLER = BACKGROUND | {"task_control", "feedback", "memory"}

    def _latency(self, wait: float, state: dict[str, Any], ui) -> None:
        """Turun gecikmesini kaydet (haftalık özet için). Dolgu çalındıysa kullanıcı o arada bir ses duydu."""
        from alice.voice import latency

        e2e = None
        ended = state.pop("ended", None)
        if ended:
            # Kullanıcının sustuğu andan ilk sese: uyandırma, döküm, bağlantı ve çalmadan önceki tampon dahil.
            # Eski ölçüm bunları hariç tutuyordu (ortanca 0,9 sn kullanıcının duyduğundan kısaydı).
            e2e = time.monotonic() - ended + getattr(AudioOut, "PREBUFFER", 0.0)
            self.log(f"live uçtan uca gecikme: {e2e:.1f} sn")
        try:
            latency.record("canlı", wait, model=getattr(self, "model", ""), tools=bool(getattr(ui, "tools", None)),
                           filler=bool(state.get("filled")) and state.get("filled") == state.get("asked"),
                           e2e=round(e2e, 2) if e2e is not None else None)
        except Exception:  # noqa: BLE001 - ölçüm konuşmayı bozmasın
            pass

    def _note_cut(self, ws, state: dict[str, Any], out) -> None:
        """Söz kesildi: cevabın çalınan kısmı duyuldu, kalanı duyulmadı. Sunucu ise cevabın gönderdiği kadarını
        (çalınandan çok daha fazlasını) söylenmiş sayıyor; modele duyulmayan kısmı not olarak ver."""
        recv, said = state.get("recv", 0), "".join(state["said"])
        if not getattr(self, "cut_notes", True) or not recv or not said or state["mute"]:
            return
        from alice.voice.bargein import cut_note, split_heard

        heard, unheard = split_heard(said, (getattr(out, "played", 0) - state.get("play0", 0)) / recv)
        note = cut_note(heard, unheard)
        if not note:
            return
        try:  # turnComplete yok: not geçmişe eklenir, cevap kullanıcının yeni sözüne verilir
            ws.send(json.dumps({"clientContent": {"turns": [{"role": "user", "parts": [{"text": note}]}],
                                                  "turnComplete": False}}))
        except Exception:  # noqa: BLE001
            return
        state["said"] = [(heard + " …(sözü kesildi)").strip()]  # kayda da duyulan kısım girsin
        self.log(f"live söz kesildi: cevabın {len(heard)}/{len(said)} karakteri duyuldu")

    def fillers(self):
        """Live'ın şu anki sesiyle hazır cümleler (ses Ayarlar'dan değişirse yenisi)."""
        voice = self.current_voice()
        if self._fillers is None or self._fillers.voice != voice:
            from alice.voice.fillers import gemini_fillers

            self._fillers = gemini_fillers(self.cfg, voice, self.key)
        return self._fillers

    FILL_AFTER = 1.5  # sn: araç bu kadar sürede bitmezse dolgu çalınır (hızlı araçta boş "bir saniye" yok)
    FILL_QUIET = 6.0  # sn: uzun araç zincirinde bu kadar sessizlikten sonra ikinci bir dolgu

    def _schedule_fill(self, state: dict[str, Any], out, fc: dict[str, Any]):
        """Araç FILL_AFTER sn içinde bitmezse dolgu çal; dönen işlev iptal eder."""
        t = threading.Timer(self.FILL_AFTER, lambda: self._fill(state, out, fc))
        t.daemon = True
        t.start()
        return t.cancel

    def _fill(self, state: dict[str, Any], out, fc: dict[str, Any]) -> None:
        """Model bu turda henüz hiç konuşmadan araç çağırdıysa kısa bir "bir saniye" çal. Araç zinciri uzun sürüp
        FILL_QUIET sn hiç ses çıkmadıysa bir kez daha (8-16 sn sessiz bekleyen istekler vardı)."""
        name = fc.get("name", "")
        last = max(state.get("sound_at", 0.0), state.get("filled_at", 0.0))
        quiet = bool(last) and time.monotonic() - last > self.FILL_QUIET
        first = not state.get("spoke") and state.get("filled") != state.get("asked")
        if (not getattr(self, "fillers_on", False) or name in self.NO_FILLER or out.playing
                or not (first or (quiet and state.get("fills", 0) < 2))):
            return
        from alice.voice.fillers import user_tone

        try:
            pcm = self.fillers().get("think" if name == "think" else "tool", user_tone("".join(state.get("heard") or [])[-200:]))
        except Exception:  # noqa: BLE001 - dolgu yoksa sessiz bekle, konuşma bozulmasın
            pcm = None
        if pcm:
            state["filled"], state["filled_at"] = state.get("asked"), time.monotonic()
            state["fills"] = state.get("fills", 0) + 1
            out.put(pcm)
            self.log(f"live dolgu çalındı ({name})")

    def _run_background(self, ws, fc: dict[str, Any]) -> None:
        name = fc.get("name", "")
        args = fc.get("args") or {}
        if getattr(self, "_tasks", 0):  # fare/klavye tek: ikinci ekran görevi ilkini bozar
            try:
                ws.send(json.dumps({"toolResponse": {"functionResponses": [{"id": fc.get("id"), "name": name,
                    "response": {"result": "zaten bir ekran görevi sürüyor; aynı işi tekrar başlatma, bitince "
                                           "sonucu sana yazacağım."}}]}}))
            except Exception:  # noqa: BLE001
                pass
            return
        call = {"id": fc.get("id") or name, "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}
        try:
            ws.send(json.dumps({"toolResponse": {"functionResponses": [{"id": fc.get("id"), "name": name, "response": {
                "result": "başladı, arka planda yapılıyor; bitince sonucu sana yazacağım. Kullanıcıya kısaca "
                          "başladığını söyle, sonucu uydurma."}}]}}))
        except Exception:  # noqa: BLE001
            return
        with self._bg_lock:
            self._tasks = getattr(self, "_tasks", 0) + 1
        turn = getattr(self, "_turn", None)
        if isinstance(turn, dict):
            turn["ok"] = turn.get("ok", 0) + 1  # "başlattım" doğru
        self.log(f"live arka plan işi başladı: {name} {json.dumps(args, ensure_ascii=False)[:160]}")

        def run() -> None:
            try:
                import comtypes  # UI Automation bu thread'de de çalışsın

                comtypes.CoInitialize()
            except Exception:  # noqa: BLE001
                pass
            try:
                self.agent._enable_groups_for(tool_names=(name,))
                res: ToolResult = self.agent._execute_calls([call])[0]
                text = res.text[:3000]
            except Exception as e:  # noqa: BLE001
                text = f"HATA {type(e).__name__}: {e}"
            self.log(f"live arka plan işi bitti: {name}: {text[:300]}")
            try:
                self._deliver(name, text, ws)
            finally:
                with self._bg_lock:
                    self._tasks = max(0, getattr(self, "_tasks", 1) - 1)

        threading.Thread(target=run, daemon=True, name=f"alice-live-bg-{name}").start()

    def _deliver(self, name: str, text: str, origin=None) -> None:
        """Arka plan işinin sonucu: konuşma açıksa (başlatan ya da sonra açılan oturum) modele, o da kullanıcıya
        söyler. Kapandıysa announce ile (Ada + ses). İş artık oturumu açık tutmuyor: Discord'da tek tur kapanması
        boşa çıkıyor, arkadaşlara söylenenler Live'a gidiyordu; "Alice sus" denince sonuç kayboluyordu.
        origin: işi başlatan oturumun bağlantısı (kapandıysa gönderim hata verir, announce'a düşülür)."""
        ws = getattr(self, "_ws", None) or origin
        if ws is not None:
            turn = getattr(self, "_turn", None)
            if isinstance(turn, dict) and not text.startswith("HATA"):
                turn["ok"] = turn.get("ok", 0) + 1
            try:
                ws.send(json.dumps({"clientContent": {"turns": [{"role": "user", "parts": [{"text": (
                    f"[{name} bitti, sonucu kullanıcıya kısaca söyle] {text}")}]}], "turnComplete": True}}))
                return
            except Exception:  # noqa: BLE001 - tam o sırada kapandı
                pass
        announce = getattr(self, "announce", None)
        if announce is not None:
            first = re.split(r"(?<=[.!?])\s|\n", text.strip(), maxsplit=1)[0][:160]
            label = "Ekran görevi" if name == "computer_task" else "Görev"
            announce(f"{label} olmadı: {first}" if text.startswith("HATA") else f"{label} bitti: {first}")

    def _run_think(self, ws, fc: dict[str, Any]) -> None:
        """Soruyu arka planda beyin modeline düşündür; cevap gelince toolResponse olarak Live'a dön. Konuşma o sırada
        açık kalır (kullanıcı araya girebilir, "Alice sus" çalışır)."""
        args = fc.get("args") or {}
        question = str(args.get("question") or "").strip()
        lock = threading.Lock()
        answered = [False]

        def respond(text: str) -> None:
            with lock:
                if answered[0]:
                    return
                answered[0] = True
            try:
                ws.send(json.dumps({"toolResponse": {"functionResponses": [
                    {"id": fc.get("id"), "name": "think", "response": {"result": text}}]}}))
            except Exception:  # noqa: BLE001 - konuşma kapanmış
                pass

        if not question:
            respond("HATA: question boş. Kullanıcının sorusunu question'a yaz.")
            return
        if getattr(self, "_thinking", 0):
            respond("Önceki soru hâlâ düşünülüyor; yeni think çağırma. Bu soruya kısaca kendin cevap ver ya da "
                    "kullanıcıya biraz beklemesini söyle.")
            self.log(f"live think atlandı (önceki sürüyor): {question[:120]}")
            return
        self._thinking = getattr(self, "_thinking", 0) + 1
        with self._bg_lock:
            self._bg += 1  # düşünürken konuşma kapanmasın; _think_thread bitince azaltır
        self.log(f"live think: {question[:200]}")

        released = [False]

        def release() -> None:
            """Düşünme artık oturumu açık tutmasın: 189 sn süren bir think boyunca Live açık kalıp Discord'daki
            konuşmalara cevap veriyordu. Bir kez çağrılır (zaman aşımında ya da bitince)."""
            with lock:
                if released[0]:
                    return
                released[0] = True
            with self._bg_lock:
                self._bg -= 1
            self._thinking = max(0, getattr(self, "_thinking", 1) - 1)

        def too_slow() -> None:
            respond("Düşünmek uzun sürüyor; kullanıcıya biraz daha süreceğini kısaca söyle. Cevap gelince sana ayrıca "
                    "yazacağım, o zaman söyle.")
            self.log("live think zaman aşımı: cevap gelince konuşmaya yazılacak")
            release()

        timer = threading.Timer(self.think_timeout, too_slow)
        timer.daemon = True
        timer.start()
        threading.Thread(target=self._think_thread, args=(ws, question, str(args.get("context") or ""), respond,
                                                          answered, lock, timer, time.monotonic(), release),
                         daemon=True, name="alice-live-think").start()

    def _think_thread(self, ws, question: str, context: str, respond: Callable[[str], None], answered: list[bool],
                      lock: threading.Lock, timer: threading.Timer, t0: float,
                      release: Callable[[], None] | None = None) -> None:
        try:
            try:
                answer = self.think_answer(question, context)
                self.log(f"live think cevabı ({time.monotonic() - t0:.1f} sn): {answer[:200]}")
                text = "Cevap (kendi sesinle, anlamını bozmadan söyle; kısaltabilirsin ama bilgi ekleme): " + answer
            except Exception as e:  # noqa: BLE001
                self.log(f"live think hata: {e}")
                text = f"HATA: düşünülemedi ({type(e).__name__}). Kısa cevabını kendin ver ve emin değilsen söyle."
            timer.cancel()
            with lock:
                late = answered[0]
            if not late:
                respond(text)
                return
            if time.monotonic() - t0 > THINK_STALE:
                # Dakikalar sonra gelen cevap konuşmayla ilgisiz bir anda okunuyordu: söyleme.
                self.log(f"live think cevabı çok geç geldi ({time.monotonic() - t0:.0f} sn), söylenmedi")
                return
            try:  # zaman aşımı cevabı gitmişti: sonuç yeni bir mesaj olarak gelsin
                ws.send(json.dumps({"clientContent": {"turns": [{"role": "user", "parts": [{"text": (
                    f"[think bitti, cevabı kullanıcıya söyle] {text}")}]}], "turnComplete": True}}))
            except Exception:  # noqa: BLE001
                pass
        finally:
            if release is not None:
                release()
            else:
                with self._bg_lock:
                    self._bg -= 1
                self._thinking = max(0, getattr(self, "_thinking", 1) - 1)

    def think_answer(self, question: str, context: str = "") -> str:
        """Beyin modeline sor: sadece okuyan araçlar (web, dosya okuma, hafıza, projeler), az adım."""
        from alice.agent import Agent

        parent = self.agent
        tools = [t for name, t in getattr(parent, "tools", {}).items() if name in THINK_TOOLS]
        child = Agent(parent.config, parent.router, tools, parent.ctx, parent.permissions, parent.ui,
                      getattr(parent, "decider", None), system_prompt=parent.messages[0]["content"], is_child=True)
        child.tool_specs = lambda: [t.spec() for t in tools]  # grup filtresi yok: hepsi okuyan araç
        child.role = self._think_role(parent.router)
        history = self.history_text()
        prompt = THINK_PROMPT.format(history=("\nAz önceki konuşma:\n" + history + "\n") if history else "",
                                     question=question, context=f"\nEk bağlam: {context}" if context else "")
        answer = (child.run_turn(prompt, max_steps=self.think_steps) or "").strip()
        return answer or "bir cevap bulamadım"

    THINK_ROLE = "live-think"

    def _think_role(self, router) -> str:
        """think'in model sırası: önce ses için ölçülmüş hızlı modeller, sonra beyin zinciri. Beyin zincirinin
        başındaki model takılınca (providers.log 25 Ekim: nvidia:z-ai/glm-5.3 ReadTimeout) her think 180 sn zaman
        aşımını bekliyordu: 13 think'in medyanı 189 sn, hiçbiri 25 sn'de yetişmedi."""
        try:
            refs = list(getattr(self, "think_models", None) or [])
            if not refs:
                refs = router.auto_chain("voice")[:3] + router.chain("brain")
        except Exception:  # noqa: BLE001 - router ölçüm/rol bilgisi yok: eski davranış
            return "brain"
        chain = [r for i, r in enumerate(refs) if r not in refs[:i]]
        if not chain:
            return "brain"
        router.role_overrides[self.THINK_ROLE] = chain  # council.py'deki gibi: geçici rol, kendi zinciriyle
        return self.THINK_ROLE

    def _execute(self, call: dict[str, Any]) -> ToolResult | None:
        """Aracı araç iş parçacığında çalıştır. Takılırsa (ör. bir pencerenin öğelerini okurken) o iş parçacığını
        bırak, sonraki araçlar yeni bir iş parçacığında çalışsın: konuşma kilitlenmesin. None = takıldı."""
        if getattr(self, "_tools_q", None) is None:
            q: queue.Queue = queue.Queue()
            self._tools_q = q
            threading.Thread(target=self._tool_worker, args=(q,), daemon=True, name="alice-live-tools").start()
        job: dict[str, Any] = {"call": call, "done": threading.Event()}
        self._tools_q.put(job)
        if not job["done"].wait(self.TOOL_TIMEOUT + 1):
            self.log(f"live araç takıldı, araçlar yeni iş parçacığına geçti: {call['function']['name']}")
            self._tools_q = None
            state = getattr(getattr(self.agent, "ctx", None), "state", None)
            if isinstance(state, dict):
                state.pop("browser", None)  # tarayıcı bağlantısı eski iş parçacığına bağlı: yeniden bağlanılsın
            return None
        return job["res"]

    def _on_tool_thread(self, fn: Callable[[], ToolResult], timeout: float = 600.0) -> ToolResult:
        """Arka plandaki görevin aracını konuşmanın araç iş parçacığında çalıştır (tarayıcı bağlantısı orada)."""
        q = getattr(self, "_tools_q", None)
        if q is None:
            q = queue.Queue()
            self._tools_q = q
            threading.Thread(target=self._tool_worker, args=(q,), daemon=True, name="alice-live-tools").start()
        job: dict[str, Any] = {"fn": fn, "done": threading.Event()}
        q.put(job)
        if not job["done"].wait(timeout):
            return ToolResult(f"araç {timeout:.0f} sn içinde bitmedi", is_error=True)
        return job["res"]

    def _tool_worker(self, q: "queue.Queue[dict[str, Any]]") -> None:
        try:
            import comtypes  # UI Automation bu iş parçacığında da çalışsın

            comtypes.CoInitialize()
        except Exception:  # noqa: BLE001
            pass
        while True:
            job = q.get()
            call = job.get("call")
            try:
                if "fn" in job:  # arka plandaki görevin aracı: tarayıcı/UIA nesneleri bu iş parçacığına bağlı
                    job["res"] = job["fn"]()
                else:
                    self.agent._enable_groups_for(tool_names=(call["function"]["name"],))
                    job["res"] = self.agent._execute_calls([call])[0]
            except Exception as e:  # noqa: BLE001
                job["res"] = ToolResult(f"{type(e).__name__}: {e}", is_error=True)
            job["done"].set()

    def _new_turn(self) -> None:
        """Kullanıcı yeni bir şey söyledi: araç sayacı, tekrar kontrolü ve dış içerik kirlenmesi sıfırlanır."""
        self._turn = {"n": 0, "last": None, "seen": {}, "ok": 0, "errors": [], "calls": 0, "fails": {}}
        from alice import guard

        guard.clear(getattr(self.agent, "ctx", None))

    def _guard(self, fc: dict[str, Any]) -> str | None:
        """Aracı çalıştırmadan modele dönülecek cevap (None = çalıştır). İki durum:
        aynı çağrı art arda gelirse (ör. checklist_next iki kez, adım atlanıyordu) önceki sonuç döner;
        istek başına araç sayısı aşılırsa model durdurulur ve kullanıcıya dönmeye yönlendirilir."""
        turn = getattr(self, "_turn", None)
        if turn is None:
            turn = self._turn = {"n": 0, "last": None}
        name, args = fc.get("name", ""), fc.get("args") or {}
        key = name + json.dumps(args, sort_keys=True, ensure_ascii=False)
        last = turn.get("last")
        repeatable = any(w in str(args.get("action", "")) for w in ("scroll", "key", "wait", "press", "screenshot"))
        if last and last[0] == key and time.monotonic() - last[1] < 5 and not repeatable:
            self.log(f"live aynı araç çağrısı tekrarlandı, çalıştırılmadı: {name}")
            return ("Bu çağrıyı az önce aynen yaptın, tekrar çalıştırılmadı. Önceki sonuç: " + last[2][:1500]
                    + "\nAynı aracı tekrar çağırma; bu sonuca göre kullanıcıya cevap ver.")
        # Arada başka çağrı olsa da aynı okuma aynı istekte ikinci kez çalışmasın (voice.log: Get-Volume,
        # Downloads listesi bir dakikada 5-6 kez). Değiştiren eylemler (tıkla, yaz) bu kontrole girmez.
        seen = turn.setdefault("seen", {})
        if key in seen and not repeatable and read_only(name, args):
            self.log(f"live aynı okuma bu istekte tekrarlandı, çalıştırılmadı: {name}")
            return ("Bu okumayı bu istekte zaten yaptın, sonuç değişmedi: " + seen[key][:1500]
                    + "\nBaşka bir yol dene ya da bulduğunu kullanıcıya söyle.")
        turn["n"] += 1
        budget = int(getattr(self, "tool_budget", 10))
        if turn["n"] > budget:
            if turn["n"] == budget + 1:
                self.log(f"live araç sınırı: bu istekte {budget} araç çağrıldı, model kullanıcıya dönmeye zorlandı")
            return (f"DUR: bu istek için {budget} araç çağrısı sınırına geldin, araç ÇALIŞTIRILMADI. Daha fazla araç "
                    "çağırma. Kullanıcıya şimdi tek iki cümleyle neyi bulduğunu, neyin olmadığını söyle ve devam "
                    "etmemi ister misin diye sor.")
        return None

    def _remember(self, fc: dict[str, Any], text: str) -> None:
        turn = getattr(self, "_turn", None)
        if turn is not None:
            key = fc.get("name", "") + json.dumps(fc.get("args") or {}, sort_keys=True, ensure_ascii=False)
            turn["last"] = (key, time.monotonic(), text)
            if read_only(fc.get("name", ""), fc.get("args") or {}):
                turn.setdefault("seen", {})[key] = text

    def _run_call(self, ws, fc: dict[str, Any]) -> None:
        early = self._guard(fc)
        if early is not None:
            try:
                ws.send(json.dumps({"toolResponse": {"functionResponses": [
                    {"id": fc.get("id"), "name": fc.get("name"), "response": {"result": early}}]}}))
            except Exception:  # noqa: BLE001 - bağlantı kapanmış olabilir
                pass
            return
        turn = getattr(self, "_turn", None)
        if turn is None:
            turn = self._turn = {"n": 0, "last": None}
        turn["calls"] = turn.get("calls", 0) + 1
        user = str(turn.get("user") or "")
        if not self._is_read(fc) and foreign(user):
            # Bozuk duyulan cümle (oyun, video, başka dil) işe dönüşmesin: Unciv'i başlatma, şarkı açma, Install'a
            # basma hep böyle olmuştu. Okumalar serbest.
            self.log(f"live dil kapısı: Türkçe olmayan istekle {fc.get('name')} çalıştırılmadı: {user[:120]}")
            self._reply(ws, fc, "YAPILMADI: duyulan söz Türkçe değil ya da anlaşılmadı, büyük ihtimalle sana "
                                "söylenmedi (oyun/video/başkası). Hiçbir şey söyleme, sessiz kal.")
            return
        wrong = self._wrong_target(fc)
        if wrong:
            self.log(f"live hedef pencere uyuşmadı: {wrong[:160]}")
            self._reply(ws, fc, wrong)
            return
        if fc.get("name") in self.BACKGROUND:
            self._run_background(ws, fc)
            return
        if fc.get("name") == "think":
            self._run_think(ws, fc)
            return
        call = {"id": fc.get("id") or fc.get("name"), "type": "function",
                "function": {"name": fc.get("name", ""), "arguments": json.dumps(fc.get("args") or {})}}
        if fc.get("name") == "task_control":  # anında: araç iş parçacığı görevin bir adımıyla meşgul olabilir
            try:
                res = self.agent._execute_calls([call])[0]
            except Exception as e:  # noqa: BLE001 - hata converse'ten çıkarsa çağıran ilk cümleyi eski yolla TEKRAR çalıştırıyordu
                res = ToolResult(f"HATA {type(e).__name__}: {e}", is_error=True)
            self.log(f"live görev kontrolü {json.dumps(fc.get('args') or {}, ensure_ascii=False)}: {res.text[:120]}")
            try:
                ws.send(json.dumps({"toolResponse": {"functionResponses": [
                    {"id": fc.get("id"), "name": "task_control", "response": {"result": res.text}}]}}))
            except Exception:  # noqa: BLE001
                pass
            return
        lock = threading.Lock()
        answered = [False]

        def respond(text: str) -> None:
            with lock:
                if answered[0]:
                    return
                answered[0] = True
            try:
                ws.send(json.dumps({"toolResponse": {"functionResponses": [
                    {"id": fc.get("id"), "name": fc.get("name"), "response": {"result": text}}]}}))
            except Exception:  # noqa: BLE001 - bağlantı kapanmış olabilir
                pass

        def too_slow() -> None:
            self.log(f"live araç zaman aşımı: {fc.get('name')} {json.dumps(fc.get('args') or {}, ensure_ascii=False)[:120]}")
            respond(f"HATA: araç {self.TOOL_TIMEOUT:.0f} sn içinde bitmedi, iş YAPILMADI. Kullanıcıya olmadığını "
                    "kısaca söyle (yapıldı deme); başka yoldan dene (ör. computer_task).")

        timer = threading.Timer(self.TOOL_TIMEOUT, too_slow)
        timer.daemon = True
        timer.start()
        late = getattr(self, "_late_filler", None)
        cancel = late(fc) if late is not None else None
        try:
            res = self._execute(call)
        finally:
            if cancel is not None:
                cancel()
        if res is None:  # takıldı: zaman aşımı cevabı gitti, konuşma bir sonraki istekle devam eder
            return
        text = res.text[:6000]
        name = fc.get("name", "")
        # Her sonuç günlüğe: eskiden sadece 4 araç yazılıyordu (361 çağrıya 37 sonuç), uçuş/pencere/komut sonucuna
        # dayanan cevaplar doğrulanamıyordu.
        self.log(f"live araç sonucu {name} [{fc.get('id') or '-'}] {'hata' if res.is_error else 'tamam'}: "
                 f"{text[:200].replace(chr(10), ' | ')}")
        fails = turn.setdefault("fails", {})
        if res.is_error and "reddetti" in text:
            text = "onay gerekli: kullanıcıya kısaca sor, 'evet' derse aynı çağrıyı tekrarla."
        elif res.is_error:
            turn.setdefault("errors", []).append(f"{name}: {text[:120]}")
            fails[name] = fails.get(name, 0) + 1
            text = "HATA: " + text
            if fails[name] >= 2:
                # Model aynı aracı döndürüp duruyor, sonra kullanıcıdan "tarayıcı ayarlarını kontrol et" istiyordu.
                text += ("\nBu araç bu istekte üst üste başarısız oldu: aynısını tekrar deneme. Başka bir araçla dene "
                         "ya da kullanıcıya olmadığını kısaca söyle. Kullanıcıdan ayar değiştirmesini isteme.")
        else:
            fails[name] = 0
            turn["ok"] = turn.get("ok", 0) + 1
        timer.cancel()
        self._remember(fc, text)
        for img in (getattr(res, "images", None) or [])[:1]:  # araç görüntüsü (ekran/sekme): modele göster
            frame = _to_jpeg(img)
            if frame:
                try:
                    ws.send(json.dumps({"realtimeInput": {"video": {"data": frame, "mimeType": "image/jpeg"}}}))
                    text += "\n(görüntü sana ayrıca gönderildi)"
                except Exception:  # noqa: BLE001
                    pass
        respond(text)

    def _reply(self, ws, fc: dict[str, Any], text: str) -> None:
        try:
            ws.send(json.dumps({"toolResponse": {"functionResponses": [
                {"id": fc.get("id"), "name": fc.get("name"), "response": {"result": text}}]}}))
        except Exception:  # noqa: BLE001 - bağlantı kapanmış olabilir
            pass

    def _record_correction(self, prev: tuple[float, str, str], heard: str) -> None:
        """Kullanıcı bir önceki turu düzeltti ("sana demedim", "yanlış anladın"): o tur test setine ve geri bildirim
        kaydına girer, gece döngüsü bunlardan başlar. Sesli tarafta bu işaret hiç değerlendirilmiyordu."""
        ts, user, reply = prev
        if time.time() - ts > 120:
            return  # eski bir tura ait değil
        self._corrected(heard)
        try:
            from alice.evals import record_feedback

            record_feedback(user, reply, note=f"kullanıcı hemen ardından: {heard[:200]}", source="ses-düzeltme")
            self.log(f"live düzeltme test setine eklendi: {user[:80]!r} -> {heard[:80]!r}")
        except Exception:  # noqa: BLE001 - kayıt hatası konuşmayı bozmasın
            pass

    def _corrected(self, text: str) -> None:
        hook = getattr(self, "on_correction", None)
        if hook is not None:
            try:
                hook(text)
            except Exception:  # noqa: BLE001 - korpus hatası konuşmayı bozmasın
                pass

    def _memory(self):
        mem = getattr(self.agent, "memory", None)
        return mem if mem is not None and hasattr(mem, "recall") else None

    def _recall(self, text: str) -> str:
        mem = self._memory()
        try:
            return mem.recall(text) if mem is not None else ""
        except Exception:  # noqa: BLE001
            return ""

    def _flush_log(self, state: dict[str, Any], ui: LiveUI) -> None:
        heard, said = "".join(state["heard"]).strip(), "".join(state["said"]).strip()
        if state.pop("unverified", False) and said:
            # Araç sonucu olmadan "yaptım": hafıza/özet bunu olgu ("kullanıcı onayladı") diye yazmasın.
            said = "[doğrulanmadı] " + said
        if heard or said or ui.tools:
            tools = f" araçlar: {', '.join(ui.tools)}" if ui.tools else ""
            self.log(f"live [{self.model}] duyulan: {heard} | cevap: {said}{tools}")
        if state.pop("cut", False) and not heard:
            self.log("live boş kesme: söz kesildi ama kullanıcıdan söz duyulmadı (yankı ya da gürültü)")
        history = getattr(self, "_history", [])
        if heard and history and correction(heard):
            self._record_correction(history[-1], heard)
        if heard:
            self._history = (history + [(time.time(), heard, said)])[-20:]
        elif said and history and state.get("ep_open"):
            # Araçtan sonra ayrı turda gelen cevap: kullanıcının sorusuna ait (yoksa kayıtta cevap boş kalıyordu).
            ts, user, before = history[-1]
            self._history[-1] = (ts, user, (before + " " + said).strip())
        mem = self._memory()
        if mem is not None and (heard or (said and state.get("ep"))):
            try:
                if heard:
                    state["ep"] = mem.record(heard, said, "canlı")
                    mem.learn(heard, said)
                elif state.get("ep_open"):
                    mem.add_reply(state["ep"], said)
            except Exception:  # noqa: BLE001 - hafıza hatası konuşmayı bozmasın
                pass
        if heard:
            state["ep_open"] = True  # sonraki kullanıcısız cevap bu tura eklenir
        state["heard"], state["said"] = [], []
        ui.tools = []
