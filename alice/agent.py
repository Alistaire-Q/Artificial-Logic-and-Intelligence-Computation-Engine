"""Ana ajan döngüsü (Claude Code / Codex tarzı).

user mesajı -> model (araçlarla) -> tool_calls varsa izin kontrolü + çalıştır ->
sonuçları geri ver -> model artık araç çağırmayınca tur biter.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from alice.config import Config
from alice.decider import Decider
from alice.permissions import Permissions
from alice.prompts import COMPACT_PROMPT, build_system_prompt
from alice.providers.openai_compat import ChatResult, StreamCancelled
from alice.providers.router import (AllProvidersFailed, Router, cached_tokens, estimate_tokens,  # noqa: F401
                                   strip_images)
from alice.tools.base import COMPUTER, DANGER, SEND, Tool, ToolContext, ToolError, ToolResult, check_args, truncate, unknown_params

# Yutulan hatalar için iz: varsayılan olarak sessiz, `logging.basicConfig(level=logging.DEBUG)` ile görünür.
log = logging.getLogger(__name__)


class UI(Protocol):
    """Arayüzün (terminal ya da masaüstü uygulaması) ajan olaylarını gösterme sözleşmesi."""

    def stream_start(self) -> None: ...  # model cevabı başlıyor (ilk parça gelene kadar "düşünüyor")
    def stream_delta(self, text: str) -> None: ...
    # Opsiyonel: def thinking_delta(self, text: str) -> None  (modelin akıl yürütme metni)
    def stream_reset(self) -> None: ...  # model akış ortasında düştü, yedek model baştan yazacak
    def stream_end(self, text: str, source: str) -> None: ...
    def tool_start(self, call_id: str, name: str, summary: str) -> None: ...
    def tool_end(self, call_id: str, name: str, result: ToolResult) -> None: ...
    def ask_permission(self, name: str, summary: str, risk: str) -> str: ...  # y | n | a
    def info(self, msg: str) -> None: ...


class NullUI:
    def __init__(self, approve: str = "y"):
        self.approve = approve
        self.events: list[tuple[str, Any]] = []

    def stream_start(self):
        self.events.append(("start", None))

    def stream_delta(self, text):
        self.events.append(("delta", text))

    def stream_reset(self):
        self.events.append(("reset", None))

    def stream_end(self, text, source):
        if text:
            self.events.append(("text", text))

    def tool_start(self, call_id, name, summary):
        self.events.append(("tool", name))

    def tool_end(self, call_id, name, result):
        self.events.append(("result", result.text))

    def ask_permission(self, name, summary, risk):
        self.events.append(("ask", name))
        return self.approve

    def info(self, msg):
        self.events.append(("info", msg))


class Cancelled(Exception):
    pass


class _Sink:
    def __init__(self, ui: UI, cancel: threading.Event):
        self.ui = ui
        self.cancel = cancel
        self.dead = False  # tur durdurulduysa arkada süren model çağrısı artık arayüze yazmasın
        self.parts: list[str] = []

    first_at = 0.0  # bu model çağrısında ilk metnin geldiği an (oturum istatistiği: ilk kelime süresi)

    def on_delta(self, text: str) -> None:
        if self.dead:
            return
        if not self.first_at:
            self.first_at = time.monotonic()
        self.parts.append(text)
        self.ui.stream_delta(text)

    early: Any = None  # tamamlanan araç çağrısını cevap bitmeden başlatan ajan fonksiyonu
    early_reset: Any = None

    def on_reset(self) -> None:
        if self.early_reset is not None:
            self.early_reset()  # model akışın ortasında değişti: erken başlatılanların sonucu kullanılmaz
        if self.dead:
            return
        self.parts.clear()
        self.ui.stream_reset()

    def on_call(self, call: dict[str, Any]) -> None:
        if self.early is not None and not self.dead and not self.cancel.is_set():
            self.early(call)

    def on_reasoning(self, text: str) -> None:
        if not self.first_at:
            self.first_at = time.monotonic()
        fn = getattr(self.ui, "thinking_delta", None)
        if fn is not None and not self.dead:
            fn(text)

    def on_tool(self, name: str, size: int) -> None:
        if not self.first_at:
            self.first_at = time.monotonic()
        fn = getattr(self.ui, "tool_delta", None)
        if fn is not None and not self.dead:
            fn(name, size)

    def cancelled(self) -> bool:
        return self.dead or self.cancel.is_set()


def _call_key(call: dict[str, Any]) -> str:
    return call["function"]["name"] + ":" + (call["function"].get("arguments") or "").strip()


REPEAT_FAIL_NOTE = ("\n\n[ALICE notu] Bu çağrı aynı argümanlarla {n}. kez başarısız oldu. Aynısını tekrarlama: hatayı oku, "
                    "kök nedeni düşün, farklı bir araç ya da yaklaşım dene; gerçekten yapılamıyorsa kullanıcıya sor.")
ERROR_STREAK_NOTE = ("\n\n[ALICE notu] Son {n} araç çağrısı hata verdi. Devam etmeden dur ve düşün: hata mesajları ne "
                     "söylüyor, varsayımların (ortam, kabuk sözdizimi, dosya yolu, programın kurulu olup olmadığı) doğru mu? "
                     "Yeni bir yol seç ve neden işe yarayacağını kısaca yaz.")
REPEAT_OK_NOTE = ("\n\n[ALICE notu] Aynı çağrıyı {n} kez yaptın ve sonuç ilerlemiyor gibi. Döngüye girme: farklı bir şey "
                  "dene ya da elindeki bilgiyle işi bitir.")


_TOO_BIG = re.compile(r"istek ~(\d+) token, model sınırı (\d+)")


def lean_specs(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Araç tanımlarının kısa hali: açıklamanın ilk cümlesi, parametre açıklamaları kırpılmış (dar bağlamlı modeller)."""
    out = []
    for sp in specs:
        fn = dict(sp.get("function") or {})
        desc = (fn.get("description") or "").strip().split("\n")[0]
        fn["description"] = desc[:160]
        params = json.loads(json.dumps(fn.get("parameters") or {}))
        for prop in (params.get("properties") or {}).values():
            if isinstance(prop, dict) and prop.get("description"):
                prop["description"] = str(prop["description"])[:60]
        fn["parameters"] = params
        out.append({**sp, "function": fn})
    return out


def fits_after_compact(errors: list[str]) -> int:
    """Modeller sadece konuşma bağlamlarına sığmadığı için düştüyse, sığacak en büyük bağlam (token); yoksa 0."""
    limits = [int(m.group(2)) for e in errors if (m := _TOO_BIG.search(e))]
    big = [int(m.group(1)) for e in errors if (m := _TOO_BIG.search(e))]
    # Büyük bir model sadece kısa süreliğine meşgulse (birkaç dakikalık bekleme, hız sınırı) konuşmayı kalıcı olarak
    # küçültme: beklemek daha iyi. Saatlik kota beklemesi ise kısa değil.
    for e in errors:
        w = re.search(r"soğumada \((\d+) sn\)", e)
        if (w and int(w.group(1)) < 300) or "rate_limit" in e or "429" in e:
            return 0
    if not limits or max(limits) < 4000 or (big and max(limits) >= max(big)):
        return 0
    return max(limits)


def parse_args(raw: str | dict | None) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if raw is not None and not isinstance(raw, str):
        # Bazı sağlayıcılar argümanı liste/sayı olarak gönderiyor: çökmesin, modele hata olarak dönsün.
        raise json.JSONDecodeError("argümanlar obje değil", str(raw), 0)
    if not raw or not raw.strip():
        return {}
    try:
        val = json.loads(raw)
    except json.JSONDecodeError:
        # Bazı modeller sona fazladan metin ya da code fence ekliyor.
        cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip()).strip()
        m = re.search(r"\{.*\}", cleaned, re.S)
        if not m:
            raise
        val = json.loads(m.group(0))
    if not isinstance(val, dict):
        raise json.JSONDecodeError("argümanlar obje değil", str(raw), 0)
    return val


def _sanitize_calls(msg: dict[str, Any]) -> None:
    """Modelin tool_call'larında ad ya da argüman metin değilse (null, liste, sayı) metne çevir: yoksa döngü
    çöküyor ve geçmişe giden bozuk çağrı sonraki her isteği 400 ile düşürüyordu."""
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function")
        if not isinstance(fn, dict):
            fn = tc["function"] = {}
        if not isinstance(fn.get("name"), str):
            fn["name"] = "" if fn.get("name") is None else str(fn["name"])
        args = fn.get("arguments")
        if not isinstance(args, str):
            fn["arguments"] = "{}" if args is None else json.dumps(args, ensure_ascii=False)


# Eski sabit özetleme sınırı. Eski config.toml'larda yazılı kaldığı için "auto" sayılır (modelin penceresine göre).
LEGACY_COMPACT_AT = 48000
PLAN_NOTE = ("[PLAN MODU: Şu an sadece okuyup araştırabilirsin; dosya değiştiren, komut çalıştıran ya da ekrana "
             "dokunan araçlar reddedilecek. İşi incele, sonra adım adım net bir uygulama planı yaz ve kullanıcının "
             "onayını bekle.]\n\n")


_CATALOG_RX = re.compile(r"\n\n# Yüklenebilir araçlar\n.*?(?=\n\n# |\n\n<|\Z)", re.S)
# Ret sayacı: izin verilmeyen çağrılar art arda bu kadar ya da turda toplam bu kadar olunca tur durur. Gözetimsiz
# ajan (KAIROS, PR takibi, görevler) ya da plan modu reddedildikçe aynı şeyi başka biçimde deneyip duruyordu.
DENIAL_LIMITS = (3, 20)
DENIAL_HALT = ("İzin verilmeyen işlemleri art arda denediğim için durdum: bu iş şu anki izin moduyla yapılamıyor. "
               "İzin modunu değiştirip ya da ne yapmamı istediğini söyleyip \"devam et\" yazarsan sürdürürüm.")
BUDGET_HALT = ("Bu tur için ayrılan token bütçesini ({used:,} / {budget:,}) doldurduğum için durdum. İş yarıda; "
               "\"devam et\" yazarsan kaldığım yerden sürdürürüm. (Sınır: [agent] turn_token_budget)")
MAX_CUTOFFS = 3  # çıktı sınırında kesilen cevap en fazla bu kadar kez sürdürülür
CUTOFF_NOTE = ("Cevabın çıktı sınırına takılıp yarıda kesildi. Tam kaldığın yerden, önceki kısmı tekrar etmeden "
               "ve özür dilemeden devam et.")
KEEP_OUTPUTS = 6  # son bu kadar araç çıktısı tam kalır
OLD_OUTPUT_CHARS = 1200  # daha eskilerin uzunları bu kadara kısaltılır
# Geçmişteki bir mesajı değiştirmek sağlayıcının prompt önbelleğini o noktadan sonrası için boşa çıkarır. Kısaltma
# her adımda değil, toplu ve ancak gerektiğinde yapılır: önbellek zaten soğuduysa (son istekten bu kadar sn sonra)
# ya da bağlam özetleme eşiğinin yarısını geçtiyse ve kısaltılacak yeterince eski çıktı biriktiyse.
CACHE_COLD_S = 300
TRIM_BATCH = 8
IMAGE_SLACK = 3  # ekran görüntüleri keep_images + bu kadar olunca topluca budanır
NUDGE_PREFIX = "[devam kontrolü] "
# Cevabın sonu "yapacağım" diyorsa iş yarım kalmış demektir.
_PROMISE_RX = re.compile(
    r"(yapaca[ğg][ıi]m|edece[ğg]im|ba[şs]l[ıi]yorum|bakaca[ğg][ıi]m|deneyece[ğg]im|ekleyece[ğg]im|d[üu]zeltece[ğg]im|"
    r"[çc]al[ıi][şs]t[ıi]raca[ğg][ıi]m|kontrol edece[ğg]im|yazaca[ğg][ıi]m|kuraca[ğg][ıi]m|indirece[ğg]im|"
    r"\blet me\b|\bi'll\b|\bi will\b|\bnext,? i\b|\bnow i\b)[^.!?]{0,60}[.:…]*\s*$", re.I)
# "Bitince yazacağım": iş arka plana (proje, uzun komut) verilmiş, söz onun bitişine bağlı. Bu yarım kalmış iş
# değil; 11 Kasım'da bu cevaba "yapmadan durdun" denince ALICE arka plan projesini 30 dk bekleyip kotayı yaktı.
_DEFERRED_RX = re.compile(
    r"\b(bitince|bitti[ğg]inde|tamamlan[ıi]nca|tamamland[ıi][ğg][ıi]nda|haz[ıi]r olunca|sonu[çc]lan[ıi]nca|"
    r"arka planda|once (it|they)('s| is| are)? (done|finished)|when (it|they) (finish|complete)\w*)\b", re.I)


def _valid_summary(text: str) -> bool:
    """Özet gerçekten özet mi? Boş/çok kısa, araç çağrısı biçiminde ya da dökümün kopyası olan metin değildir."""
    t = text.strip()
    if len(t) < 8:
        return False
    low = t[:400].lower()
    if re.match(r"\s*(<\s*(tool|function|invoke)|\{\s*\"(name|tool|function)\"|->\s*\w+\(|\[(assistant|tool|user)\])", low):
        return False
    echoed = len(re.findall(r"(?m)^\[(assistant|tool|user)\]|<parameter name=|<tool call|->\s*\w+\(\{", t))
    return echoed < 4


def _fallback_summary(old: list[dict[str, Any]]) -> str:
    """Modelsiz özet: kullanıcının yazdıkları sırayla (kısaltılmış), dokunulan dosyalar ve son asistan cevabı."""
    asks, files, last = [], [], ""
    for m in old:
        c = m.get("content")
        if m.get("role") == "user" and not m.get("_nudge") and isinstance(c, str) and c.strip():
            if c.startswith("[Önceki konuşmanın özeti]"):
                asks.append("(daha eski özet) " + _clip(c[len("[Önceki konuşmanın özeti]"):].strip(), 1500))
            else:
                asks.append(_clip(m.get("_display") or c, 600))
        elif m.get("role") == "assistant":
            if isinstance(c, str) and c.strip():
                last = c
            for tc in m.get("tool_calls") or []:
                if tc["function"]["name"] in ("edit_file", "write_file", "multi_edit", "apply_patch"):
                    hit = re.search(r'"path"\s*:\s*"((?:[^"\\]|\\.)*)"', tc["function"].get("arguments") or "")
                    if hit and hit.group(1) not in files:
                        files.append(hit.group(1))
    parts = ["## Kullanıcının yazdıkları (sırayla)"] + [f"{i}. {a}" for i, a in enumerate(asks[-25:], 1)]
    if files:
        parts += ["", "## Değiştirilen dosyalar"] + [f"- {f}" for f in files[-40:]]
    if last:
        parts += ["", "## Son cevabım", _clip(last, 2500)]
    parts += ["", "(Bu özet otomatik çıkarıldı; ayrıntı gerekiyorsa dosyaları ve git durumunu araçlarla yeniden oku.)"]
    return "\n".join(parts)


# Sonuç bildiren cevap: kanıt kontrolü yalnız bunlarda çalışır (her cevapta ek model çağrısı olmasın)
_CLAIM_RX = re.compile(
    r"(yapt[ıi]m|kurdum|kuruldu|indirdim|indirildi|ba[şs]latt[ıi]m|a[çc]t[ıi]m|a[çc][ıi]ld[ıi]|g[öo]nderdim|"
    r"g[öo]nderildi|tamamlad[ıi]m|tamamland[ıi]|bitti|ayarlad[ıi]m|kaydettim|kaydedildi|sildim|silindi|"
    r"y[üu]klendi|[çc]al[ıi][şs][ıi]yor|haz[ıi]r|\byok\b|bulunamad[ıi]|bulamad[ıi]m|mevcut de[ğg]il|"
    r"\bdone\b|\binstalled\b|\bdownloaded\b|\bnot found\b)", re.I)


def _clip(text: str, limit: int) -> str:
    """Uzun araç çıktısının başını ve sonunu tut (sonuç çoğu zaman sonda yazar), ortasını '…' ile kısalt."""
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    return text[:head] + "\n…\n" + text[-(limit - head):]


class Agent:
    def __init__(
        self,
        config: Config,
        router: Router,
        tools: list[Tool],
        ctx: ToolContext,
        permissions: Permissions,
        ui: UI | None = None,
        decider: Decider | None = None,
        system_prompt: str | None = None,
        is_child: bool = False,
    ):
        self.config = config
        self.router = router
        self.tools = {t.name: t for t in tools if not (is_child and t.name in ("task", "subagents"))}
        self.ctx = ctx
        self.permissions = permissions
        self.ui = ui or NullUI()
        self.decider = decider
        self.is_child = is_child
        self.role = "brain"  # sesli asistan "voice" kullanır: ilk kelimesi en çabuk gelen modeller
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt or build_system_prompt(ctx.cwd)}]
        a = config.agent
        self.max_steps = int(a.get("max_steps", 80))
        self._compact_cfg = a.get("compact_at_tokens", "auto")
        # Üst sınır (0 = yok). Proje ajanları ve kurul için konur: 1M bağlamlı modelde her adım yüz binlerce token
        # gönderiyordu (bir proje ~100M token).
        self.compact_cap = 0
        self._last_prompt: tuple[int, int, Any] | None = None  # (sağlayıcının saydığı token, mesaj sayısı, son mesaj)
        self.output_limit = int(a.get("tool_output_chars", 12000))
        self.keep_images = int(a.get("keep_images", 2))
        self._last_call_at = 0.0  # son model çağrısının bittiği an (monotonic): önbellek soğudu mu
        self.stream = bool(a.get("stream", True))
        # Araçlarla çalışırken iş bitmeden durursa ("şimdi testleri çalıştıracağım" deyip susmak, todo listesinde
        # bitmemiş adım bırakmak) en fazla bu kadar kez devam etmesi hatırlatılır. 0 = kapalı.
        self.finish_nudges = int(a.get("finish_nudges", 2))
        try:
            from alice.hooks import Hooks

            self.hooks = Hooks.from_config(config, ctx.cwd)  # kullanıcının hook'ları (config.toml / hooks.toml)
        except Exception:  # noqa: BLE001 - bozuk hook ayarı ajanı açılmaz yapmasın
            from alice.hooks import Hooks

            self.hooks = Hooks([], ctx.cwd)
        self.cancel = threading.Event()
        self.busy = False
        # Çalışırken gelen kullanıcı mesajları (steer): bir sonraki model adımından önce konuşmaya girer.
        self._inbox: list[tuple[str, list[str]]] = []
        self._inbox_lock = threading.Lock()
        self._steering = False  # tur, inbox'ı alabilecek noktada mı (finally'de kapanır)
        self._call_counts: dict[str, int] = {}
        self._fail_counts: dict[str, int] = {}
        self._error_streak = 0
        # Oturum istatistiği (durum çubuğundaki paneller): tur, adım, model ve araç süresi, ilk kelime süresi, token.
        self.stats: dict[str, float] = {"turns": 0, "steps": 0, "llm_s": 0.0, "tool_s": 0.0, "ttft_s": 0.0,
                                        "ttft_n": 0, "gen_s": 0.0, "gen_tokens": 0, "input": 0, "cached": 0,
                                        "output": 0}
        # Kullanım dökümü: tokenlar hangi modele, bağlam hangi araçların çıktısına gitti
        self.usage_models: dict[str, dict[str, float]] = {}
        self.usage_tools: dict[str, dict[str, float]] = {}
        # Her araç adımından sonra çağrılır (oturumu kaydetmek için): tur takılır ya da çökerse iz kaybolmasın.
        self.on_step: Any = None
        self.on_compact: Any = None  # özetlemede bağlamdan çıkan mesajlar buraya verilir (uygulama arşive yazar)
        self.on_usage: Any = None  # her model cevabının kullanım bilgisi (token, varsa ücret) buraya da bildirilir
        # Uzun süreli hafıza (alice.memory.LongMemory): oturum kuranlar bağlar; testler ve gözetimsiz görevlerde None.
        self.memory: Any = None
        self._memory_surfaced: set[str] = set()  # bu sohbette mesaja eklenmiş notlar (tekrar eklenmesin)
        self.memory_channel = ""
        self._memory_version: Any = -1
        # Geri sarma için: kullanıcı mesajının indeksi -> o turda ilk değişen dosyaların eski hali.
        self.checkpoints: dict[int, dict[str, bytes | None]] = {}
        if not is_child:
            ctx.state["agent"] = self

    # ------------------------------------------------------------------ public
    def tool_specs(self) -> list[dict[str, Any]]:
        """Modele gönderilen araçlar: temel araçlar + bu oturumda açılmış gruplar (bkz. tools/groups.py)."""
        if str(self.config.agent.get("tool_groups", "auto")) == "all":
            return [t.spec() for t in self.tools.values()]
        from alice.tools.groups import visible_tools

        tools = visible_tools(self.tools, self.ctx.state.setdefault("tool_groups", set()))
        # Sıra sabit: önce gönderilenler aynı yerde kalır, yeni açılan grubun araçları sona eklenir. Araç listesi
        # isteğin başında; ortasına araç girince prompt önbelleği listenin oradan sonrası için boşa çıkıyordu.
        order: list[str] = self.__dict__.setdefault("_spec_order", [])
        by = {t.name: t for t in tools}
        order.extend(n for n in by if n not in order)
        return [by[n].spec() for n in order if n in by]

    def _cache_warm(self) -> bool:
        """Araç listesi değişirse önbellekteki konuşma boşa mı gider? Sohbetin ilk isteğinden sonra evet: araç listesi
        isteğin en başında (Claude'da araçlar, sistem isteminden de önce gelir), Gemini ve OpenAI da önbelleği istemin
        başından eşleştiriyor. Araç eklenince sistem istemi ve konuşmanın tamamı önbellekten düşüyordu (ölçüm: her
        yeni grup ~11-35K token yeniden yazma). Ayar: agent.cache_stable_tools = false eski davranış."""
        if self.is_child or str(self.config.agent.get("tool_groups", "auto")) == "all":
            return False
        if not self.config.agent.get("cache_stable_tools", True):
            return False
        return any(m.get("role") == "assistant" for m in self.messages[1:-1])

    def _enable_groups_for(self, text: str = "", tool_names: tuple[str, ...] = ()) -> None:
        from alice.tools.groups import group_of, groups_for_text

        enabled: set[str] = self.ctx.state.setdefault("tool_groups", set())
        self.ctx.state["tool_names"] = tuple(self.tools)
        guessed = groups_for_text(text, self.tools) - enabled if text else set()
        if guessed and self._cache_warm():
            # Kelimeden tahmin edilen grup gerçekten gerekmeyebilir ("aç", "token", "ekran" gibi geniş kalıplar).
            # Uzun sohbette araç listesine eklemek bütün konuşmayı önbelleğe yeniden yazdırıyordu; bunun yerine
            # mesaja not düşülür, model gerçekten gerekirse load_tools ile kendisi açar.
            self._hint_groups(guessed)
        else:
            enabled |= guessed
        enabled |= {g for g in map(group_of, tool_names) if g}
        if getattr(getattr(self, "permissions", None), "mode", "") == "plan" and not getattr(self, "is_child", False):
            enabled.add("plan")  # plan modunda exit_plan_mode hep hazır olsun

    def _hint_groups(self, names: set[str]) -> None:
        """Yüklenmeyen ama gerekebilecek araç gruplarını son kullanıcı mesajına not olarak ekle."""
        from alice.tools.groups import all_groups

        if not self.messages or self.messages[-1].get("role") != "user":
            return
        groups = all_groups()
        parts = []
        for n in sorted(names):
            desc = groups[n][1] if n in groups else ("MCP sunucusu" if n.startswith("mcp:") else "araç")
            parts.append(f"{n} ({desc[:90]})")
        note = ("[Araç notu: şu araçlar yüklü değil: " + "; ".join(parts) + ". Bu iş için gerekirse önce load_tools "
                "ile yükle; gerekmiyorsa yükleme.]")
        msg = self.messages[-1]
        content = msg.get("content")
        if isinstance(content, str):
            msg.setdefault("_display", content)
            msg["content"] = content + "\n\n" + note
        elif isinstance(content, list):
            msg.setdefault("_display", next((p.get("text", "") for p in content if isinstance(p, dict)
                                              and p.get("type") == "text"), ""))
            msg["content"] = content + [{"type": "text", "text": note}]

    def spawn_child(self) -> "Agent":
        return Agent(self.config, self.router, list(self.tools.values()), self.ctx, self.permissions, self.ui,
                     self.decider, system_prompt=self.messages[0]["content"], is_child=True)

    def refresh_memory(self, force: bool = False) -> None:
        """Hafızada yeni/değişen bilgi varsa sistem istemindeki profil bölümünü güncelle."""
        mem = self.memory
        if mem is None or self.is_child:
            return
        try:
            from alice import projects

            # Sistem istemi isteğin en başında: değişirse prompt önbelleği baştan kurulur. Eskiden her tur kaydedilince
            # (mem.version) profil yeniden yazılıyor, her yeni mesajda önbellek sıfırlanıyordu. Sohbet başlamadan
            # notlar ve projeler değişince yenilenir. Sohbet başladıktan sonra yalnız kullanıcı hafızayı elle
            # değiştirince (not silme/düzenleme), klasör ya da gün değişince: tur sonu otomatik not çıkarımı ve proje
            # taraması sistem istemini sohbetin ortasında değiştirip konuşmanın tamamını önbelleğe yeniden
            # yazdırıyordu. Bu sohbette yeni çıkan notlar zaten konuşmanın içinde, eksik kalan ilgili not da hatırlama
            # (recall) ile mesaja eklenir.
            key = (str(self.ctx.cwd), getattr(mem, "user_edits", 0), time.strftime("%Y-%m-%d"))
            if len(self.messages) > 1 and isinstance(self._memory_version, tuple) and not force:
                if self._memory_version[0] == key:
                    return
                version: Any = (key, None, None)
            else:
                scanned = projects.load()[0] if projects.enabled(self.config) else 0.0
                version = (key, scanned, mem.notes_stamp(self.ctx.cwd))
            if force or version != self._memory_version:
                from alice.prompts import with_memory

                profile = "\n\n".join(b for b in (mem.profile(cwd=self.ctx.cwd), projects.brief(self.config))
                                       if b)
                self.messages[0]["content"] = with_memory(self.messages[0]["content"], profile)
                self._memory_version = version
        except Exception:  # noqa: BLE001 - hafıza hatası konuşmayı durdurmasın
            log.debug("hafıza profili yenilenemedi", exc_info=True)

    def _recall(self, user_text: str) -> str:
        if self.memory is None or self.is_child:
            return ""
        try:
            voice = str(getattr(self, "memory_channel", "") or "").startswith(("ses", "voice", "live"))
            return self.memory.recall(user_text, cwd=self.ctx.cwd, surfaced=self._memory_surfaced,
                                      use_model=False if voice or self.role == "voice" else None)
        except Exception:  # noqa: BLE001
            log.debug("hafızadan hatırlama başarısız", exc_info=True)
            return ""

    def _remember_turn(self, user_text: str, reply: str) -> None:
        if self.memory is None or self.is_child:
            return
        try:
            wrote = bool(self.ctx.state.pop("memory_wrote", False))
            self.memory.record(user_text, reply, self.memory_channel,
                               sid=str(self.ctx.state.get("session_id") or ""))
            self.memory.learn(user_text, reply, cwd=self.ctx.cwd, wrote=wrote)
        except Exception:  # noqa: BLE001
            log.debug("tur hafızaya yazılamadı", exc_info=True)
        self._feed_buddy(user_text, reply)
        self._update_magic_docs()

    def _update_magic_docs(self) -> None:
        """Bu sohbette okunan sihirli belgeleri (# MAGIC DOC:) arka planda konuşmaya göre güncelle."""
        if self.is_child or not self.ctx.state.get("magic_docs"):
            return
        import threading

        from alice import magicdocs

        msgs = list(self.messages)
        threading.Thread(target=lambda: magicdocs.update(self.ctx.state, self.router, msgs), daemon=True,
                         name="alice-magicdocs").start()

    def _feed_buddy(self, user_text: str, reply: str) -> None:
        """BUDDY konuşmayla doyar; ara sıra kısa bir yorum yapar (arka planda, turu bekletmez)."""
        import threading

        from alice import buddy

        quiet = self.is_child or self.role == "voice" or self.memory_channel in ("ses", "telefon", "voice", "call")
        if not buddy.settings(self.config).get("enabled", True) or quiet:
            return  # sesli konuşmada ve alt ajanlarda arkadaş yorum yapmaz (gereksiz model çağrısı)

        def work() -> None:
            try:
                buddy.on_turn(self.config, self.router, user_text, reply)
            except Exception:  # noqa: BLE001
                log.debug("buddy güncellenemedi", exc_info=True)

        threading.Thread(target=work, daemon=True, name="alice-buddy").start()

    def sync_catalog(self) -> None:
        """Sistem talimatının sonundaki 'Yüklenebilir araçlar' listesini güncel araçlarla yenile (MCP ve eklenti
        araçları ajan kurulduktan sonra eklenebiliyor)."""
        if str(self.config.agent.get("tool_groups", "auto")) == "all":
            return
        from alice.tools.groups import catalog

        text = catalog(self.tools)
        system = self.messages[0]["content"]
        body = _CATALOG_RX.sub("", system)
        new = body + ("\n\n" + text if text else "")
        if new != system:
            self.messages[0]["content"] = new

    def run_turn(self, user_text: str, max_steps: int | None = None, images: list[str] | None = None) -> str:
        self.refresh_memory()
        self.sync_catalog()
        recall = self._recall(user_text)
        old_role = self.role
        routed = self._route_role(user_text)
        if routed:
            self.role = routed
        try:
            final = self._run_turn(user_text, max_steps, images, recall)
        finally:
            self.role = old_role
        self._remember_turn(user_text, final)
        return final

    ROUTE_ROLES = {"strong": "route-strong", "light": "route-light"}

    def _route_role(self, user_text: str) -> str | None:
        """Jev ile bu tur için model katmanı (alice/providers/jev_route.py). Sadece ana beyin turunda; menüden
        elle seçilen model (sabitleme) ya da kapalı [routing] varken dokunulmaz. None = bugünkü zincir."""
        rc = self.config.data.get("routing", {}) or {}
        router = self.router
        if (not rc.get("enabled") or self.is_child or self.role != "brain"
                or "brain" in getattr(router, "role_overrides", {"brain": 1})):
            return None
        from alice.providers import jev_route

        prev = ""
        for m in reversed(self.messages[1:]):
            if m.get("role") == "assistant" and isinstance(m.get("content"), str) and m["content"].strip():
                prev = m["content"]
                break
        log = getattr(router, "on_event", None) or (lambda s: None)
        r = jev_route.route(self.config, user_text, prev, log=log)
        try:
            log(f"yönlendirme: {r.tier} ({r.reason}) {r.signals} {r.latency:.2f} sn")
        except Exception:  # noqa: BLE001
            pass
        role = self.ROUTE_ROLES.get(r.tier)
        if role is None:
            return None
        try:
            chain = jev_route.tier_chain(router, self.config, r.tier)
        except Exception:  # noqa: BLE001 - zincir kurulamazsa bugünkü davranış
            return None
        if not chain:
            return None
        router.role_overrides[role] = chain
        return role

    def _run_turn(self, user_text: str, max_steps: int | None, images: list[str] | None, recall: str) -> str:
        from alice.mentions import expand_mentions

        content_text = expand_mentions(user_text, self.ctx.cwd)
        if recall:
            content_text += "\n\n" + recall
        if self.hooks.hooks and not self.is_child:
            from alice.hooks import context, feedback

            extra = []
            if len(self.messages) == 1 and self.hooks.has("SessionStart"):
                extra.append(context(self.hooks.run("SessionStart", {})))
            if self.hooks.has("UserPromptSubmit"):
                res = self.hooks.run("UserPromptSubmit", {"prompt": user_text})
                blocked = feedback(res)
                if blocked:
                    self.ui.info(f"hook mesajı engelledi: {blocked}")
                    return ""
                extra.append(context(res))
            extra = [e for e in extra if e]
            if extra:
                content_text += "\n\n[hook bağlamı]\n" + "\n".join(extra)
        if not self.is_child:
            from alice import guard

            guard.clear(self.ctx)  # kullanıcının yeni mesajı: önceki turda okunan dış içerik bu turu kısıtlamaz
        if self.permissions.mode == "plan" and not self.is_child:
            content_text = PLAN_NOTE + content_text
        content: Any = content_text
        if images:
            content = [{"type": "text", "text": content_text}] + [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}} for b in images]
        if not self.is_child:
            self.checkpoints[len(self.messages)] = {}
            self.ctx.state["checkpoint"] = self.checkpoints[len(self.messages)]
        msg: dict[str, Any] = {"role": "user", "content": content}
        if content_text != user_text:
            msg["_display"] = user_text
        if images:
            msg["_user_images"] = len(images)
        self.messages.append(msg)
        self._enable_groups_for(content_text)
        final = ""
        steps = max_steps or self.max_steps
        self.cancel.clear()
        self.busy = True
        self._call_counts.clear()
        self._fail_counts.clear()
        self._error_streak = 0
        sink = _Sink(self.ui, self.cancel)
        sink.early, sink.early_reset = self._early_start, self._early_clear
        self._turn_tools: set[str] = set()
        nudges = 0
        verified = False
        answered = ""  # kanıt kontrolü hatırlatmasından önce verilen cevap
        self._turn_start = len(self.messages) - 1
        self.stats["turns"] += 1
        stop_hooks = 3  # Stop hook'u ajanı en fazla bu kadar kez geri çevirebilir (sonsuz döngü olmasın)
        cutoffs, cut_text = 0, ""  # çıktı sınırında kesilen cevabın önceki parçaları
        self._denials = [0, 0]  # (art arda, toplam) reddedilen çağrı
        self._turn_tokens = 0  # bu turda harcanan (önbellekten gelmeyen girdi + çıktı) token: tur bütçesi için
        self._steering = not self.is_child
        try:
            for _ in range(steps):
                if self.cancel.is_set():
                    raise Cancelled
                self._drain_inbox()
                self._early_clear()
                self._maybe_compact()
                self._prune_images()
                self._shrink_old_outputs()
                sink.parts.clear()
                self.ui.stream_start()
                sent = len(self.messages)
                sink.first_at = 0.0
                t_call = time.monotonic()
                try:
                    res = self._interruptible(self.router.chat, self.role, list(self.messages), tools=self._specs(),
                                              stream=sink if self.stream else None)
                except AllProvidersFailed as e:
                    # Kalan modellerin bağlamı konuşmaya yetmiyorsa: o modele sığacak kadar özetle, bir kez daha dene.
                    room = fits_after_compact(e.errors)
                    if room and estimate_tokens(self.messages[:1], self.tool_specs()) > room * 0.45:
                        self._lean = True  # araç tanımları bile dar modeli dolduruyor: kısa tanımlarla gönder
                    if not room or not self._maybe_compact(force=True, limit=room):
                        raise
                    self.ui.stream_start()
                    sent = len(self.messages)
                    res = self._interruptible(self.router.chat, self.role, list(self.messages), tools=self._specs(),
                                              stream=sink if self.stream else None)
                self._count_call(res, t_call, sink.first_at)
                msg = res.message
                _sanitize_calls(msg)
                if getattr(self, "_lean", False) and self._roomy(res):
                    self._lean = False  # geniş bağlamlı model geri geldi: tam araç tanımlarına dön
                if self.on_usage is not None:
                    try:
                        self.on_usage(res.usage or {})
                    except Exception:  # noqa: BLE001 - sayaç hatası turu bozmasın
                        log.debug("on_usage hata verdi", exc_info=True)
                pt = int((res.usage or {}).get("prompt_tokens") or 0)
                self._last_prompt = (pt, sent, self.messages[sent - 1]) if pt else None
                if not self.stream and msg.get("content"):
                    self.ui.stream_delta(msg["content"])
                self.messages.append(msg)
                sink.parts.clear()  # metin artık geçmişte: kesilirse bir daha "[durduruldu]" diye eklenmesin
                self.ui.stream_end(msg.get("content") or "", f"{res.provider}:{res.model}")
                if msg.get("content"):
                    final = cut_text + msg["content"]
                if not msg.get("tool_calls") and getattr(res, "finish", "") == "length" and cutoffs < MAX_CUTOFFS:
                    # Cevap yazının ortasında kesildi: yarım metni son cevap sayma, kaldığı yerden sürdürt.
                    cutoffs += 1
                    cut_text = final
                    self.messages.append({"role": "user", "content": NUDGE_PREFIX + CUTOFF_NOTE, "_nudge": True})
                    continue
                if not msg.get("tool_calls"):
                    if self._drain_inbox():
                        continue  # cevap yazarken kullanıcı yeni bir şey söyledi: tur bitmeden onu da ele al
                    if stop_hooks and self.hooks.has("Stop") and not self.is_child:
                        from alice.hooks import feedback

                        back = feedback(self.hooks.run("Stop", {"reply": final}))
                        if back:
                            stop_hooks -= 1
                            self.messages.append({"role": "user", "content": NUDGE_PREFIX + "Stop hook'u işi bitmiş "
                                                  "saymadı, devam et:\n" + back, "_nudge": True})
                            continue
                    nudge = self._unfinished(msg.get("content") or "") if nudges < self.finish_nudges else ""
                    if not nudge and not verified:
                        verified = True  # turda bir kez: doğrulamanın kendisi döngüye girmesin
                        nudge = self._unverified(msg.get("content") or "")
                        if nudge:
                            answered = final  # asıl cevap ekranda duruyor: düzeltme onun yerine geçmez, eklenir
                    if not nudge:
                        # Kanıt kontrolünden sonra yazılan düzeltme tek başına kayda geçince asıl cevap kayboluyor,
                        # hafızada "Merhaba" -> "Haklısın, doğrulama başarısız oldu" gibi kopuk turlar kalıyordu.
                        if answered and final != answered:
                            final = answered + "\n\n" + final
                        return final
                    nudges += 1
                    self.messages.append({"role": "user", "content": nudge, "_nudge": True})
                    continue
                self._turn_tools.update(c["function"]["name"] for c in msg["tool_calls"])
                t_tools = time.monotonic()
                self._run_tools(msg["tool_calls"])
                self.stats["tool_s"] += time.monotonic() - t_tools
                if self._denials[0] >= DENIAL_LIMITS[0] or self._denials[1] >= DENIAL_LIMITS[1]:
                    return self._halt(DENIAL_HALT) or final
                budget = self._turn_budget()
                if budget and self._turn_tokens >= budget:
                    # Gözetimsiz uzun araç zincirleri (yüzlerce çağrı) faturayı habersiz büyütmesin.
                    return self._halt(BUDGET_HALT.format(used=self._turn_tokens, budget=budget)) or final
                if self.on_step is not None:
                    try:
                        self.on_step()
                    except Exception:  # noqa: BLE001 - kayıt hatası işi durdurmasın
                        log.debug("on_step hata verdi", exc_info=True)
            return self._halt(f"Adım sınırına ({steps}) ulaştım, iş yarıda kaldı. Kaldığım yerden sürdürmem için "
                              "\"devam et\" yaz.") or final
        except (KeyboardInterrupt, Cancelled, StreamCancelled):
            sink.dead = True
            partial = "".join(sink.parts)
            if partial and not (self.messages[-1]["role"] == "assistant" and self.messages[-1].get("content") == partial):
                self.messages.append({"role": "assistant", "content": partial + " [durduruldu]"})
                self.ui.stream_end(partial, "")
            self._repair_after_interrupt()
            self.ui.info("durduruldu")
            return final
        except AllProvidersFailed as e:
            self.ui.stream_end("", "")
            self._repair_after_interrupt()
            self.ui.info(str(e))
            # Önceden tur burada sessizce bitiyordu: ekranda küçük bir bilgi satırı, sohbet kaydında hiçbir şey.
            # Kullanıcı işin durduğunu anlamayıp bekliyor, sonra "devam et" yazmak zorunda kalıyordu.
            why = "; ".join(" ".join(x.split())[:140] for x in e.errors[:4])
            head = ("Seçtiğin model cevap veremedi, iş yarıda kaldı (elle seçildiği için başka modele geçmedim)."
                    if getattr(e, "pinned", False) else "Sıradaki hiçbir model cevap veremedi, iş yarıda kaldı.")
            return self._halt(f"{head}\n\nNeden: {why}\n\nModel menüsünden başka bir model seçip ya da biraz bekleyip "
                              "\"devam et\" yazarsan kaldığım yerden sürdürürüm.") or final
        except Exception:
            # Beklenmeyen hata: cevapsız tool_calls geçmişte kalırsa sonraki her tur sağlayıcıdan 400 alır.
            sink.dead = True
            self._repair_after_interrupt()
            raise
        finally:
            with self._inbox_lock:
                self._steering = False  # bundan sonra gelen mesaj yeni tur olur (take_inbox ile kalanlar alınır)
            self.busy = False
            self._release_desktop()

    def _turn_budget(self) -> int:
        """Turda harcanabilecek token (0 = sınırsız). Alt ajanlar kendi sınırlarını [agent] sub_turn_token_budget'tan
        alır; yoksa ana sınır geçerli. Önbellekten okunan girdi sayılmaz: ucuz ve her adımda tekrar ediyor."""
        try:
            if self.is_child and self.config.agent.get("sub_turn_token_budget") is not None:
                return max(0, int(self.config.agent.get("sub_turn_token_budget") or 0))
            return max(0, int(self.config.agent.get("turn_token_budget", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _count_call(self, res: ChatResult, started: float, first: float) -> None:
        """Bir model çağrısını oturum istatistiğine ekle."""
        st = self.stats
        now = time.monotonic()
        self._last_call_at = now
        st["steps"] += 1
        st["llm_s"] += now - started
        if first:
            st["ttft_s"] += first - started
            st["ttft_n"] += 1
        u = res.usage or {}
        out = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
        fresh = max(0, int(u.get("prompt_tokens") or u.get("input_tokens") or 0) - cached_tokens(u))
        self.__dict__["_turn_tokens"] = self.__dict__.get("_turn_tokens", 0) + fresh + out
        st["input"] += int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
        st["cached"] += cached_tokens(u)
        st["output"] += out
        if first and out and now - first > 0.2:  # akış hızı: yalnız gerçekten akan cevaplarda
            st["gen_s"] += now - first
            st["gen_tokens"] += out
        m = self.usage_models.setdefault(f"{res.provider}:{res.model}", {"calls": 0, "input": 0, "cached": 0,
                                                                          "output": 0, "seconds": 0.0})
        m["calls"] += 1
        m["input"] += int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
        m["cached"] += cached_tokens(u)
        m["output"] += out
        m["seconds"] += now - started

    def stats_view(self) -> dict[str, Any]:
        """Durum çubuğu panelleri: oturum istatistiği, token kullanımı ve bağlamın dağılımı."""
        st = self.stats
        system = estimate_tokens(self.messages[:1])
        tools = estimate_tokens([], self._specs())
        total = self.context_tokens()
        return {
            "turns": int(st["turns"]), "steps": int(st["steps"]), "llm_s": round(st["llm_s"], 1),
            "tool_s": round(st["tool_s"], 1),
            "ttft": round(st["ttft_s"] / st["ttft_n"], 2) if st["ttft_n"] else 0.0,
            "tps": round(st["gen_tokens"] / st["gen_s"]) if st["gen_s"] else 0,
            "input": int(st["input"]), "cached": int(st["cached"]), "output": int(st["output"]),
            "ctx": {"system": system, "tools": tools, "messages": max(0, total - system - tools), "total": total},
        }

    def export_stats(self) -> dict[str, Any]:
        """Oturum dosyasına yazılan istatistik ve kullanım dökümü (yeniden açılışta sıfırlanmasın)."""
        return {"stats": dict(self.stats), "usage_models": self.usage_models, "usage_tools": self.usage_tools}

    def import_stats(self, data: dict[str, Any]) -> None:
        for k, v in (data.get("stats") or {}).items():
            if k in self.stats and isinstance(v, (int, float)):
                self.stats[k] = v
        self.usage_models = {str(k): dict(v) for k, v in (data.get("usage_models") or {}).items() if isinstance(v, dict)}
        self.usage_tools = {str(k): dict(v) for k, v in (data.get("usage_tools") or {}).items() if isinstance(v, dict)}

    def usage_breakdown(self) -> dict[str, Any]:
        """"Tokenlarım nereye gitti": model başına çağrı/girdi/önbellek/çıktı, araç başına bağlama eklenen çıktı ve
        bağlamın şu anki dağılımı. Kullanım dökümü penceresi ve usage aracı kullanır."""
        models = sorted(({"model": k, **{x: (round(y, 1) if isinstance(y, float) else y) for x, y in v.items()}}
                         for k, v in self.usage_models.items()), key=lambda r: -(r["input"] + r["output"]))
        tools = sorted(({"tool": k, **v} for k, v in self.usage_tools.items()), key=lambda r: -r["tokens"])
        return {**self.stats_view(), "models": models, "tools": tools}

    def steer(self, text: str, images: list[str] | None = None) -> bool:
        """Çalışan tura mesaj ekle. Tur alabilecek durumdaysa True (mesaj bir sonraki adımda modele gider); değilse
        False ve çağıran mesajı yeni tur olarak çalıştırmalı."""
        with self._inbox_lock:
            if not (self.busy and self._steering) or self.cancel.is_set():
                return False
            self._inbox.append((text, list(images or [])))
            return True

    def take_inbox(self) -> list[tuple[str, list[str]]]:
        """Turun alamadığı mesajlar (tur bitti/kesildi): çağıran bunları yeni tur olarak çalıştırır."""
        with self._inbox_lock:
            out, self._inbox = self._inbox, []
            return out

    def _drain_inbox(self) -> bool:
        """Bekleyen kullanıcı mesajlarını konuşmaya ekle. Yalnız araç sonuçlarından sonra çağrılır (tool_calls ile
        sonuçları arasına mesaj girmez). Eklendiyse True."""
        with self._inbox_lock:
            items, self._inbox = self._inbox, []
        for text, images in items:
            body = f"[Sen çalışırken kullanıcı yazdı; işe bunu da katarak devam et]\n{text}"
            content: Any = body
            if images:
                content = [{"type": "text", "text": body}] + [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}} for b in images]
            msg: dict[str, Any] = {"role": "user", "content": content, "_display": text, "_steer": True}
            if images:
                msg["_user_images"] = len(images)
            self.messages.append(msg)
            self._enable_groups_for(text)
        return bool(items)

    def _halt(self, text: str) -> str:
        """Tur cevap verilemeden bittiğinde (model yok, adım sınırı) bunu sohbete asistan mesajı olarak yaz: ekranda ve
        kayıtta görünür, sonraki turda model de işin yarıda kaldığını bilir. Alt ajanlarda ve seste yalnız bilgi."""
        if self.is_child or self.role == "voice":
            self.ui.info(text)
            return ""
        try:
            self.messages.append({"role": "assistant", "content": text, "_notice": True})
            self.ui.stream_start()
            self.ui.stream_delta(text)
            self.ui.stream_end(text, "")
            if self.on_step is not None:
                self.on_step()
        except Exception:  # noqa: BLE001 - bildirim yazılamadı diye tur hata vermesin
            log.debug("durma bildirimi yazılamadı", exc_info=True)
        return text

    def _unfinished(self, text: str) -> str:
        """Tur araçlarla iş yaparken cevap verip durduysa ve iş bitmemiş görünüyorsa modele gidecek hatırlatma (boş:
        bitti). Sesli rolde yok: ikinci cevap da sesli okunurdu."""
        used = getattr(self, "_turn_tools", set())
        if not used or self.role == "voice" or self.permissions.mode == "plan":
            return ""  # plan modunda "şunu yapacağım" zaten istenen cevap
        tail = text.strip()[-240:]
        if tail.endswith("?"):
            return ""  # kullanıcıya soru soruyor: beklemesi doğru
        todos = self.ctx.state.get("todos") or []
        left = [t.get("content", "") for t in todos if isinstance(t, dict) and t.get("status") != "done"]
        if "todo_write" in used and left:
            return (NUDGE_PREFIX + "Todo listesinde bitmemiş adımlar var: " + "; ".join(left[:6]) + ". Kullanıcıdan bir "
                    "şey gerekmiyorsa araçlarla kaldığın yerden devam et. Gerçekten bittiyse listeyi güncelle ve "
                    "sonucu söyle; takıldıysan neden takıldığını açıkça söyle.")
        if _PROMISE_RX.search(tail) and not _DEFERRED_RX.search(tail):
            return (NUDGE_PREFIX + "Yapacağını söyledin ama yapmadan durdun. Kullanıcıdan bir şey gerekmiyorsa şimdi "
                    "araçlarla yap ve bitince sonucu bildir; gerekiyorsa açıkça sor.")
        return ""

    def _unverified(self, text: str) -> str:
        """Sonuç söyleyen cevap ("yaptım", "kuruldu", "indirildi", "yok") bu turdaki araç sonuçlarıyla destekleniyor mu?
        Desteklenmeyen iddia varsa modele gidecek hatırlatma (boş: sorun yok). Hızlı bir model karşılaştırır.

        Sonora kurulumunda ALICE dosya inmeden "indirmeyi başlattım", sürüm sayfasında Setup.exe varken "Windows
        sürümü yok" dedi: ekranı/araç sonuçlarını varsayımıyla doldurdu. Bu araca özel değil, her işte olabilir."""
        used = getattr(self, "_turn_tools", set())
        if (not used or self.role == "voice" or self.permissions.mode == "plan" or self.is_child
                or not self.config.agent.get("verify_claims", True) or not _CLAIM_RX.search(text)):
            return ""
        evidence: list[str] = []
        for m in self.messages[getattr(self, "_turn_start", 0):]:
            if m.get("role") == "tool":
                evidence.append(f"[{m.get('name')}] {_clip(str(m.get('content') or ''), 3000)}")
            elif m.get("role") == "assistant" and m.get("tool_calls"):
                evidence.append("çağrılar: " + "; ".join(
                    f"{c['function']['name']}({str(c['function'].get('arguments') or '')[:200]})"
                    for c in m["tool_calls"]))
        ask = next((str(m.get("content")) for m in reversed(self.messages[:getattr(self, "_turn_start", 0) + 1])
                    if m.get("role") == "user"), "")
        prompt = (
            "Bir asistanın cevabını, bu turda kullandığı araçların SONUÇLARIYLA karşılaştır. Yalnızca şu iki tür "
            "iddiayı listele:\n"
            "1. Asistanın BU TURDA yaptığını söylediği ama karşılığında hiçbir araç çağrısı olmayan iş (kurdum, "
            "indirdim, gönderdim, sildim, kaydettim, açtım gibi).\n"
            "2. Araç sonuçlarıyla AÇIKÇA ÇELİŞEN iddia (sonuç hata derken 'oldu' demek, sonuçta varken 'yok' demek, "
            "farklı bir sayı ya da durum söylemek).\n"
            "Şunları LİSTELEME: önceki turlardan ya da konuşmadan bilinen şeyler, genel bilgi, açıklama, yorum, tahmin "
            "ya da 'doğrulamadım' diye sunulanlar, niyet, öneri, soru. Araç çıktıları burada kısaltılmış olabilir "
            "('…' ile): kısaltılan yerde bulunabilecek ayrıntıyı dayanaksız sayma. Emin değilsen listeye koyma.\n"
            'Sadece JSON yaz: {"unsupported": ["kısa iddia", ...]}\n\n'
            f"KULLANICI İSTEĞİ:\n{ask[:1500]}\n\nARAÇ SONUÇLARI:\n" + "\n".join(evidence)[-24000:]
            + f"\n\nCEVAP:\n{text[:4000]}")
        # Cevap ekranda dururken bu kontrol bitene kadar tur açık kalıyor, arayüz "Çalışıyor" gösteriyordu. Hızlı
        # sıradaki takılan bir model (step-5-preview, 180 sn zaman aşımı) işi bitmiş ALICE'i dakikalarca "çalışıyor"
        # bırakıyordu: kontrol süre sınırlı, yetişmezse cevap olduğu gibi kalır (sonucu arkada atılır).
        box: dict[str, Any] = {}
        give_up = threading.Event()

        class _Quiet:
            """Akışı kimseye göstermeyen ama iptal edilebilen alıcı: süre dolunca sağlayıcı isteği de kesilir,
            arkada kota ve bağlantı harcayarak sürmez (router akış sırasında ve modeller arasında buna bakar)."""

            def on_delta(self, text: str) -> None:
                pass

            def on_reset(self) -> None:
                pass

            def cancelled(self) -> bool:
                return give_up.is_set()

        def check() -> None:
            try:
                # 400 token düşünen modellerde (Cerebras qwen, step) düşünmeye gidip cevap boş dönüyordu.
                box["res"] = self.router.chat(str(self.config.agent.get("verify_role", "fast")),
                                              [{"role": "user", "content": prompt}], temperature=0, max_tokens=1200,
                                              stream=_Quiet())
            except Exception as e:  # noqa: BLE001 - iptal (StreamCancelled) dahil
                box["err"] = e

        worker = threading.Thread(target=check, daemon=True, name="alice-verify")
        worker.start()
        deadline = time.monotonic() + float(self.config.agent.get("verify_seconds", 25))
        while worker.is_alive() and time.monotonic() < deadline and not self.cancel.is_set():
            worker.join(0.2)
        if "res" not in box:
            give_up.set()
            if worker.is_alive():
                log.debug("iddia kontrolü süre sınırını aştı; istek iptal edildi, cevap olduğu gibi bırakıldı")
            return ""
        try:
            res = box["res"]
            m = re.search(r"\{.*\}", res.text or "", re.S)
            bad = [str(x)[:160] for x in (json.loads(m.group(0)).get("unsupported") or [])] if m else []
        except Exception:  # noqa: BLE001 - doğrulama yapılamadıysa cevabı engelleme
            return ""
        if not bad:
            return ""
        return (NUDGE_PREFIX + "Cevabındaki şu iddiaların bu turdaki araç sonuçlarında dayanağı yok: "
                + "; ".join(bad[:5]) + ". Ya bir araçla gerçekten doğrula, ya da kullanıcıya bunu doğrulamadığını "
                "açıkça söyle. Kontrol etmeden 'yaptım/indirildi/yok' deme.\n"
                "Bu notu kullanıcı GÖRMÜYOR ve o yazmadı; önceki cevabın ekranda duruyor. Bu yüzden: nota cevap verir "
                "gibi yazma ('Haklısın', 'kontrol ettim', 'bu turda' diye başlama, bu kontrolden söz etme), önceki "
                "cevabı baştan tekrar etme. Yalnızca kısa bir ek yaz: doğruladıysan sonucu bir iki cümleyle, "
                "doğrulayamadıysan 'Düzeltme:' diye başlayıp hangi kısmın kesin olmadığını söyle.")

    def stop(self) -> None:
        """Başka bir thread'den (ör. uygulamadaki Durdur butonu) mevcut turu durdur. Model ya da araç cevabı
        beklenmez; çalışan komutlar da sonlandırılır."""
        self.cancel.set()
        from alice.tools.browser import abort_browser
        from alice.tools.shell import kill_foreground

        kill_foreground(self.ctx)
        abort_browser(self.ctx, "stop")  # takılan tarayıcı çağrısı (sadece çalışan varsa)

    def _interruptible(self, fn, *args: Any, **kwargs: Any) -> Any:
        """fn'i ayrı bir thread'de çalıştır ve bitmesini beklerken durdurma isteğine bak. Durdurulursa beklemeden
        Cancelled fırlatır; arkadaki çağrı kendi başına biter ve sonucu atılır."""
        box: dict[str, Any] = {}
        done = threading.Event()

        def work() -> None:
            try:
                box["res"] = fn(*args, **kwargs)
            except BaseException as e:  # noqa: BLE001 - çağıran thread'de yeniden fırlatılır
                box["err"] = e
            finally:
                done.set()

        threading.Thread(target=work, daemon=True, name="alice-call").start()
        while not done.wait(0.1):
            if self.cancel.is_set():
                raise Cancelled
        if "err" in box:
            raise box["err"]
        return box["res"]

    def rewind(self, msg_index: int) -> tuple[str, list[str]]:
        """Konuşmayı msg_index'teki kullanıcı mesajından öncesine geri sar ve o andan beri
        ALICE'in dosya araçlarıyla değiştirdiği dosyaları eski haline getir.
        (Shell komutlarının yaptığı değişiklikler izlenemez.) Mesajın metnini ve geri yüklenen dosyaları döndürür."""
        if self.busy:
            raise RuntimeError("çalışırken geri sarılamaz")
        if not 0 < msg_index < len(self.messages) or self.messages[msg_index].get("role") != "user":
            raise ValueError("geçersiz mesaj")
        original: dict[str, bytes | None] = {}
        for idx in sorted(k for k in self.checkpoints if k >= msg_index):
            for path, data in self.checkpoints[idx].items():
                original.setdefault(path, data)
        restored = []
        for path, data in original.items():
            p = Path(path)
            try:
                if data is None:
                    if p.exists():
                        p.unlink()
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(data)
                restored.append(path)
            except OSError as e:
                self.ui.info(f"geri yüklenemedi: {path} ({e})")
        m = self.messages[msg_index]
        text = m.get("_display") or (m["content"] if isinstance(m["content"], str) else
                                     next((p.get("text", "") for p in m["content"] if p.get("type") == "text"), ""))
        if text.startswith(PLAN_NOTE):
            text = text[len(PLAN_NOTE):]
        self.messages = self.messages[:msg_index]
        for k in [k for k in self.checkpoints if k >= msg_index]:
            del self.checkpoints[k]
        return text, restored

    # ------------------------------------------------------------------- tools
    def _run_tools(self, calls: list[dict[str, Any]]) -> None:
        # Model gönderilmemiş bir grubun aracını çağırdıysa (sistem isteminden biliyor) grubu açık tut.
        self._enable_groups_for(tool_names=tuple(c["function"]["name"] for c in calls))
        results = self._execute_calls(calls)
        pending_images: list[tuple[str, list[str]]] = []
        for call, result in zip(calls, results, strict=True):
            name = call["function"]["name"]
            from alice import tool_results

            text = tool_results.shape(result.text, self.output_limit, str(call.get("id") or ""), name)
            if result.is_error:
                text = f"HATA: {text}"
            text += self._loop_note(call, result)
            if result.images:
                if self._brain_sees_images():
                    pending_images.append((name, result.images))
                else:
                    text += "\n\n[görüntü açıklaması - vision modeli]\n" + self._describe_images(result.images)
            tool_msg: dict[str, Any] = {"role": "tool", "tool_call_id": call["id"], "name": name, "content": text}
            tu = self.usage_tools.setdefault(name, {"calls": 0, "tokens": 0, "errors": 0})
            tu["calls"] += 1
            tu["tokens"] += max(1, len(text) // 4)  # sonucun bağlama eklediği yaklaşık token
            tu["errors"] += 1 if result.is_error else 0
            if result.files:  # arayüz için (modele gitmez): sohbet yeniden açılınca dosya kartı yine görünsün
                tool_msg["_files"] = result.files[:10]
            if result.card:
                tool_msg["_card"] = result.card
            self.messages.append(tool_msg)
        for name, images in pending_images:
            parts: list[dict[str, Any]] = [{"type": "text", "text": f"[{name} aracının döndürdüğü görüntü]"}]
            parts += [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}} for b in images]
            self.messages.append({"role": "user", "content": parts, "_image": True})

    def _loop_note(self, call: dict[str, Any], result: ToolResult) -> str:
        """Zayıf modeller aynı hatalı çağrıyı tekrar tekrar yapıp takılıyor; bunu fark edip modele söyle."""
        if self.cancel.is_set():
            return ""
        key = _call_key(call)
        self._call_counts[key] = self._call_counts.get(key, 0) + 1
        if result.is_error:
            self._error_streak += 1
            self._fail_counts[key] = self._fail_counts.get(key, 0) + 1
            if self._fail_counts[key] >= 2:
                return REPEAT_FAIL_NOTE.format(n=self._fail_counts[key])
            if self._error_streak >= 3 and self._error_streak % 3 == 0:
                return ERROR_STREAK_NOTE.format(n=self._error_streak)
            return ""
        self._error_streak = 0
        n = self._call_counts[key]
        if n >= 3 and call["function"]["name"] not in ("todo_write",):
            return REPEAT_OK_NOTE.format(n=n)
        return ""

    def _execute_calls(self, calls: list[dict[str, Any]]) -> list[ToolResult]:
        """Çağrıları sırayla işle; art arda gelen, izin gerektirmeyen ve paralel güvenli (salt okuma)
        çağrıları aynı anda çalıştır. Arayüz olayları hep bu thread'den ve çağrı sırasıyla gider."""
        results: list[ToolResult | None] = [None] * len(calls)
        i = 0
        while i < len(calls):
            if self.cancel.is_set():
                results[i] = ToolResult("durduruldu (kullanıcı kesti)", is_error=True)
                i += 1
                continue
            early = self._take_early(calls[i])
            if early is not None:  # cevap akarken başlatılmıştı: sonucu hazır (ya da az kaldı)
                tool, args, summary, fut = early
                self.ui.tool_start(calls[i]["id"], tool.name, summary)
                res = self._await(fut)
                self._note_read(tool, args, res)
                self.ui.tool_end(calls[i]["id"], tool.name, res)
                results[i] = self._fence(tool, res)
                i += 1
                continue
            prep = self._prepare(calls[i])
            if isinstance(prep, ToolResult):
                results[i] = prep
                i += 1
                continue
            batch = [(i, prep)]
            j = i + 1
            if self._parallel_ok(prep):
                busy = {prep[0].exclusive_key(prep[1])} - {""}
                while j < len(calls) and not self.cancel.is_set():
                    nxt = self._prepare(calls[j], quiet=True)
                    if isinstance(nxt, ToolResult) or not self._parallel_ok(nxt):
                        break
                    key = nxt[0].exclusive_key(nxt[1])
                    if key and key in busy:
                        break  # aynı depoya/paket yöneticisine dokunan ikinci komut: öncekiler bitince çalışır
                    busy.add(key)
                    batch.append((j, nxt))
                    j += 1
            if len(batch) == 1:
                results[i] = self._execute(calls[i], *prep)
            else:
                for idx, (tool, _args, summary) in batch:
                    self.ui.tool_start(calls[idx]["id"], tool.name, summary)
                pool = ThreadPoolExecutor(max_workers=min(8, len(batch)))
                try:
                    futures = {idx: pool.submit(self._run_tool, tool, args) for idx, (tool, args, _) in batch}
                    for idx, (tool, args, _) in batch:
                        results[idx] = self._await(futures[idx])
                        self._note_read(tool, args, results[idx])
                        self.ui.tool_end(calls[idx]["id"], tool.name, results[idx])
                        results[idx] = self._fence(tool, results[idx])
                finally:
                    # Durdurulduysa arkada süren salt okuma araçları beklenmez (with bloğu shutdown(wait=True) ile
                    # hepsinin bitmesini bekliyordu).
                    pool.shutdown(wait=False)
            i = j
        return [r if r is not None else ToolResult("çalıştırılmadı", is_error=True) for r in results]

    def _await(self, fut: Any) -> ToolResult:
        """Paralel ya da erken başlatılan aracın sonucunu bekle; Durdur'a basılırsa beklemeden dön. Önceden
        fut.result() süresiz bekliyordu: uzun bir grep ya da yavaş web_fetch durdurulan turu açık tutuyordu."""
        from concurrent.futures import TimeoutError as FutTimeout

        while True:
            try:
                return fut.result(timeout=0.1)
            except FutTimeout:
                if self.cancel.is_set():
                    return ToolResult("durduruldu (kullanıcı kesti)", is_error=True)
            except Exception as e:  # noqa: BLE001
                return ToolResult(f"{type(e).__name__}: {e}", is_error=True)

    def _early_start(self, call: dict[str, Any]) -> None:
        """Akış sürerken tamamlanan araç çağrısı: paralel güvenliyse (salt okuma, izin gerekmez, hook yok, ekranı
        kullanmaz) hemen arka planda başlat. Yüzlerce araç çağrısı olan işlerde modelin yazmayı bitirmesini beklemek
        dakikalar kaybettiriyordu. Sonuç, cevap bitince aynı çağrı gelirse kullanılır."""
        try:
            prep = self._prepare(call, quiet=True)
            if isinstance(prep, ToolResult) or not self._parallel_ok(prep):
                return
            tool, args, summary = prep
            if getattr(tool, "risk", "") == COMPUTER:
                return
            pool = self.__dict__.get("_early_pool")
            if pool is None:
                pool = self._early_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="alice-early")
            fut = pool.submit(self._run_tool, tool, args)
            self.__dict__.setdefault("_early", {})[call["id"]] = (args, tool, summary, fut)
        except Exception:  # noqa: BLE001 - erken başlatma yalnız hızlandırır; olmazsa normal yol çalışır
            log.debug("araç erken başlatılamadı", exc_info=True)

    def _early_clear(self) -> None:
        self.__dict__["_early"] = {}

    def _take_early(self, call: dict[str, Any]) -> tuple[Tool, dict[str, Any], str, Any] | None:
        e = self.__dict__.get("_early", {}).pop(call.get("id"), None)
        if e is None:
            return None
        args, tool, summary, fut = e
        try:
            final = parse_args(call["function"].get("arguments"))
        except (json.JSONDecodeError, KeyError, TypeError):
            return None
        if final != args or call["function"].get("name") != tool.name:
            return None  # son hâli farklı: normal yoldan yeniden çalışır
        return tool, args, summary, fut

    def _parallel_ok(self, prep: tuple[Tool, dict[str, Any], str]) -> bool:
        tool, args, _ = prep
        if self.hooks.has("PreToolUse") or self.hooks.has("PostToolUse"):
            return False  # hook'lar her çağrıda sırayla çalışsın (paralel yol _execute'u atlıyor)
        try:
            return bool(tool.parallel_for(self.ctx, args)) and self.permissions.decide(tool, args, self.ctx) == "allow"
        except Exception:  # noqa: BLE001 - bozuk argüman: sıralı yolda hata olarak modele dönecek
            return False

    def _prepare(self, call: dict[str, Any], quiet: bool = False) -> ToolResult | tuple[Tool, dict[str, Any], str]:
        """Aracı bul, argümanları çöz, izni al. Hata ya da ret varsa ToolResult döner.
        quiet=True iken izin sorulmaz (paralel gruba alınabilir mi diye bakarken)."""
        name = call["function"]["name"]
        tool = self.tools.get(name)
        if tool is None:
            return ToolResult(f"bilinmeyen araç '{name}'. Mevcut araçlar: {', '.join(self.tools)}", is_error=True)
        try:
            args = parse_args(call["function"].get("arguments"))
        except json.JSONDecodeError as e:
            return ToolResult(f"argümanlar geçerli JSON değil ({e}). Tekrar dene.", is_error=True)
        if not isinstance(args, dict):
            return ToolResult("argümanlar bir JSON nesnesi olmalı ({\"ad\": değer}). Tekrar dene.", is_error=True)
        bad = check_args(tool, args)
        if bad:
            return ToolResult(bad + " Düzeltip tekrar çağır.", is_error=True)
        hook_verdict = ""
        if not quiet and self.hooks.has("PreToolUse"):
            # İzinden önce: hook argümanları değiştirebilir ya da izin kararı verebilir (bkz. alice.hooks.decision).
            from alice.hooks import decision, feedback

            results = self.hooks.run("PreToolUse", {"args": args}, tool.name)
            blocked = feedback(results)
            if blocked:
                res = ToolResult(f"Kullanıcının hook'u bu çağrıyı engelledi: {blocked}", is_error=True)
                self.ui.tool_start(call["id"], name, name)
                self.ui.tool_end(call["id"], name, res)
                return res
            d = decision(results)
            if "updatedInput" in d:
                args = d["updatedInput"]
                bad = check_args(tool, args)
                if bad:
                    return ToolResult(f"Hook'un değiştirdiği argümanlar geçersiz: {bad}", is_error=True)
            hook_verdict = str(d.get("permissionDecision") or "")
            if hook_verdict == "deny":
                why = d.get("reason") or "gerekçe verilmedi"
                res = ToolResult(f"Kullanıcının hook'u bu çağrıya izin vermedi: {why}", is_error=True)
                self.ui.tool_start(call["id"], name, name)
                self.ui.tool_end(call["id"], name, res)
                self._note_denial()
                return res
            if d.get("additionalContext"):
                self.__dict__.setdefault("_hook_notes", {})[call["id"]] = d["additionalContext"]
        try:
            summary = tool.summary(args)
            if quiet:
                return tool, args, summary
            verdict = self.permissions.decide(tool, args, self.ctx)
            if hook_verdict == "allow" and verdict == "ask":
                verdict = "allow"  # hook onayladı; yasak kuralları ve plan modu yine geçerli
            elif hook_verdict == "ask" and verdict == "allow":
                verdict = "ask"
            risk = tool.risk_for(args) if verdict == "ask" else ""
        except Exception as e:  # noqa: BLE001 - zayıf modelin yanlış tipli argümanı (liste, null) turu ve geçmişi bozmasın
            return ToolResult(f"geçersiz argüman: {type(e).__name__}: {e}. Parametre tiplerini düzeltip tekrar dene.",
                              is_error=True)
        if verdict == "deny":
            rule = self.permissions.matching_rule(tool, args, self.ctx, "deny")
            self._log_outward(tool, args, summary, "blocked", f"izin kuralı {rule}" if rule else
                              "plan modu ya da gözetimsiz çalışma")
            if rule:
                res = ToolResult(f"Kullanıcının izin kuralı bunu yasaklıyor ({rule}). Aynı şeyi başka biçimde de deneme; "
                                 "gerekiyorsa kullanıcıya neden gerektiğini söyle.", is_error=True)
            else:
                res = ToolResult("Plan modundasın: bu araç değişiklik yapıyor ve şu an kapalı. Önce planını yaz; "
                                 "kullanıcı onaylayıp modu değiştirince uygularsın.", is_error=True)
            self.ui.tool_start(call["id"], name, summary)
            self.ui.tool_end(call["id"], name, res)
            self._note_denial()
            return res
        if verdict == "ask":
            from alice import guard

            shown = f"[dış içerik okunduktan sonra: {guard.tainted(self.ctx)}] {summary}" if guard.tainted(self.ctx) \
                else summary
            ans = self.ui.ask_permission(name, shown, risk)
            try:  # hep onaylanan işler için "artık sorma" önerisi çıkarılabilsin (permissions action=suggest)
                from alice.permissions import record_answer

                record_answer(tool, args, self.ctx.cwd, risk, ans)
            except Exception:  # noqa: BLE001 - kayıt yazılamadı diye iş durmasın
                log.debug("izin cevabı kaydedilemedi", exc_info=True)
            if ans == "a":
                self.permissions.always_allow.add(name)
            elif ans == "p" and risk in (DANGER, SEND):
                # Geri alınamayan ya da başkasına giden işe kalıcı izin yazılmaz: bu seferlik evet sayılır.
                self.ui.info("tehlikeli ya da dışarı giden işlerde kalıcı izin verilmez; sadece bu sefer izin verildi")
            elif ans == "p":  # bu projede hep: kalıcı kural olarak yazılır, sonraki oturumlarda da sorulmaz
                from alice.permissions import persist_allow, suggest_rule

                rule = suggest_rule(tool, args, self.ctx.cwd)
                try:
                    where = persist_allow(self.ctx.cwd, rule)
                    self.ui.info(f"kalıcı izin eklendi: {rule} ({where})")
                except OSError as e:
                    self.ui.info(f"kalıcı izin yazılamadı ({e}); bu oturum için izin verildi")
                    self.permissions.always_allow.add(name)
            elif ans not in ("y", "p"):
                self._log_outward(tool, args, summary, "rejected")
                self._note_denial()
                return ToolResult("Kullanıcı bu çağrıyı reddetti. Aynı şeyi tekrar deneme; başka yol bul ya da kullanıcıya sor.",
                                  is_error=True)
        if hasattr(self, "_denials"):
            self._denials[0] = 0  # izin verilen çağrı art arda sayımı sıfırlar
        return tool, args, summary

    def _note_denial(self) -> None:
        d = self.__dict__.setdefault("_denials", [0, 0])
        d[0] += 1
        d[1] += 1

    def _execute(self, call: dict[str, Any], tool: Tool, args: dict[str, Any], summary: str) -> ToolResult:
        self.ui.tool_start(call["id"], tool.name, summary)
        watch = self._watch(tool, args)
        result = self._run_tool(tool, args)
        self._note_read(tool, args, result)
        note =self.__dict__.get("_hook_notes", {}).pop(call["id"], "")
        if note:
            from dataclasses import replace

            result = replace(result, text=result.text + "\n\n[hook bağlamı]\n" + note)
        if watch is not None and not self.cancel.is_set():
            from dataclasses import replace

            seen = watch.note()
            if seen:  # ekranda gerçekte olan: yeni pencere, hata kutusu, UAC, açılmayan program
                result = replace(result, text=result.text + seen)
        if self.hooks.has("PostToolUse"):
            from dataclasses import replace

            from alice.hooks import decision, feedback

            post = self.hooks.run("PostToolUse", {"args": args, "result": result.text[:200_000],
                                                  "is_error": result.is_error}, tool.name)
            verdict = decision(post)
            if "updatedToolOutput" in verdict:  # hook çıktıyı değiştirdi (ör. sırları gizledi)
                result = replace(result, text=verdict["updatedToolOutput"][:200_000])
            back = "\n".join(x for x in (feedback(post), verdict.get("additionalContext", "")) if x)
            if back:
                result = replace(result, text=result.text + "\n\n[hook geri bildirimi]\n" + back)
        self._log_outward(tool, args, summary, "failed" if result.is_error else "sent", result.text)
        self.ui.tool_end(call["id"], tool.name, result)
        return self._fence(tool, result)

    def _note_read(self, tool: Tool, args: dict[str, Any], result: ToolResult) -> None:
        """Okunan sihirli belgeyi (# MAGIC DOC:) kaydet. Sıralı, paralel ve erken başlatılan yolların hepsinde
        çağrılır: read_file paralel güvenli olduğu için çoğu zaman o yollardan geçiyor, belge hiç kaydedilmiyordu."""
        if tool.name != "read_file" or result.is_error or not args.get("path"):
            return
        try:
            from alice import magicdocs

            magicdocs.note_read(self.ctx.state, self.ctx.resolve(str(args["path"])))
        except Exception:  # noqa: BLE001 - sihirli belge kaydı aracı bozmasın
            pass

    def _watch(self, tool: Tool, args: dict[str, Any]):
        """Ekranı etkileyen eylem (tıklama, tuş, program açma, komut) öncesi pencereleri not al; sonra ne değiştiği
        araç sonucuna eklenir (bkz. alice.tools.outcome). Salt okuma eylemlerinde ve ayar kapalıyken None."""
        if not self.config.agent.get("observe_outcomes", True):
            return None
        try:
            if tool.name != "run_command" and tool.risk_for(args) != COMPUTER:
                return None
        except Exception:  # noqa: BLE001
            return None
        if str(args.get("action") or "") in ("screenshot", "list", "snapshot", "text", "tabs", "downloads",
                                              "cursor_position", "wait"):
            return None
        from alice.tools import outcome

        if not outcome.IS_WIN:
            return None
        return outcome.Watch(opening=tool.name in ("windows", "app") and args.get("action") in ("open", "launch"))

    def _fence(self, tool: Tool, result: ToolResult) -> ToolResult:
        """Dış içerik getiren aracın sonucunu "güvenilmeyen veri" diye işaretle ve turu kirlenmiş say (bkz. guard)."""
        if not getattr(tool, "untrusted", False) or result.is_error:
            return result
        from dataclasses import replace

        from alice import guard

        if not guard.enabled(self.ctx):
            return result
        guard.mark(self.ctx, tool.name)
        return replace(result, text=guard.wrap(tool.name, result.text))

    @staticmethod
    def _log_outward(tool: Tool, args: dict[str, Any], summary: str, outcome: str, detail: str = "") -> None:
        """Başkasına giden eylemi (send riski) eylem kaydına yaz: gönderilen, reddedilen, engellenen hepsi."""
        try:
            if tool.risk_for(args) != SEND:
                return
        except Exception:  # noqa: BLE001 - risk hesaplanamadıysa kayıt yüzünden tur bozulmasın
            return
        from alice import actionlog

        actionlog.log(tool.name, summary, outcome, args, detail)

    def _run_tool(self, tool: Tool, args: dict[str, Any]) -> ToolResult:
        # Araçlar bu thread'de çalışır (Playwright ve UI Automation nesneleri thread'e bağlı). Durdurulunca
        # stop() araçların başlattığı komutları öldürür, böylece bekleyen araç da hemen döner.
        if not self._take_desktop(tool):
            return ToolResult("durduruldu (kullanıcı kesti)", is_error=True)
        try:
            return tool.run(self.ctx, **args)
        except ToolError as e:
            return ToolResult(str(e), is_error=True)
        except TypeError as e:
            return ToolResult(unknown_params(tool, args) or f"geçersiz parametre: {e}", is_error=True)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001 - araç hatası ajanı düşürmesin, modele dönsün
            return ToolResult(f"{type(e).__name__}: {e}", is_error=True)

    def _take_desktop(self, tool: Tool) -> bool:
        """Uygulamada aynı anda birden çok sohbet çalışabilir; ekranı/tarayıcıyı süren araç sırayla kullanılır
        (bkz. DesktopLock). Kilit yoksa (CLI, ses, çekirdek) bir şey yapmaz."""
        lock = self.ctx.state.get("desktop_lock")
        owner = self.ctx.state.get("lock_owner") or self.ctx  # alt ajan ana sohbetin adına tutar
        if lock is None or getattr(tool, "risk", "") != COMPUTER or lock.held_by(owner):
            return True
        return lock.acquire(owner, self.cancel.is_set, on_wait=lambda: self.ui.info(
            "başka bir sohbet ekranı/tarayıcıyı kullanıyor; o turunu bitirince devam edeceğim"))

    def _release_desktop(self) -> None:
        lock = self.ctx.state.get("desktop_lock")
        if lock is not None and not self.is_child:  # alt ajan bitince ana sohbet hâlâ ekranda olabilir
            lock.release(self.ctx)

    # ----------------------------------------------------------------- vision
    def _brain_sees_images(self) -> bool:
        ref = self.router.first_available(self.role)
        return bool(ref and self.router.supports_vision(ref))

    def _describe_images(self, images: list[str]) -> str:
        task = next((m["content"] for m in reversed(self.messages)
                     if m["role"] == "user" and isinstance(m.get("content"), str)), "")
        parts: list[dict[str, Any]] = [{"type": "text", "text": (
            "You are the eyes of a computer-use agent. Describe this screenshot precisely for the task below: "
            "visible windows and their titles, focused element, all buttons/links/fields/menus with their exact text "
            "and approximate pixel coordinates (x,y of center in this image), dialogs, errors, and anything relevant. "
            f"Task: {task[:800]}")}]
        parts += [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}} for b in images]
        try:
            res = self.router.chat("vision", [{"role": "user", "content": parts}], max_tokens=1500)
            return res.text
        except (AllProvidersFailed, KeyError) as e:
            return f"(görüntü açıklanamadı: {str(e)[:200]})"

    def _cache_cold(self) -> bool:
        return bool(self._last_call_at) and time.monotonic() - self._last_call_at > CACHE_COLD_S

    def _prune_images(self) -> None:
        idx = [i for i, m in enumerate(self.messages) if m.get("_image")]
        if len(idx) <= self.keep_images + IMAGE_SLACK and not (self._cache_cold() and len(idx) > self.keep_images):
            return  # her yeni ekran görüntüsünde budamak önbelleği her adımda bozuyordu: topluca buda
        for i in idx[:-self.keep_images] if self.keep_images else idx:
            self.messages[i] = {"role": "user", "content": "[eski ekran görüntüsü yer kazanmak için kaldırıldı]", "_image": False}

    def _shrink_old_outputs(self) -> None:
        """Eski araç çıktılarını kısalt: son KEEP_OUTPUTS sonuç olduğu gibi kalır, öncekilerin uzunları başı ve sonuyla
        kısaltılır. Uzun bir işte okunan dosyalar, listeler, sayfalar her adımda baştan gönderiliyor ve bağlamı
        dolduruyordu; model onların özünü zaten kullandı, gerekirse tekrar okur."""
        idx = [i for i, m in enumerate(self.messages) if m["role"] == "tool" and isinstance(m.get("content"), str)]
        todo = [i for i in idx[:-KEEP_OUTPUTS] if len(self.messages[i]["content"]) > OLD_OUTPUT_CHARS * 1.5]
        if not todo:
            return
        if not self._cache_cold() and (len(todo) < TRIM_BATCH or self.context_tokens() < self.compact_at * 0.5):
            return  # önbellek sıcak ve bağlam rahat: geçmişi değiştirme
        for i in todo:
            c = self.messages[i]["content"]
            if len(c) > OLD_OUTPUT_CHARS * 1.5:
                self.messages[i] = {**self.messages[i], "content": "[eski çıktı, yer kazanmak için kısaltıldı; "
                                    "gerekirse aracı tekrar çağır]\n" + truncate(c, OLD_OUTPUT_CHARS)}

    # -------------------------------------------------------------- context
    @property
    def compact_at(self) -> int:
        """Özetleme sınırı. "auto": kullanılan modelin bağlam penceresinin %80'i (GLM 5.3 ve Gemini'de ~840K,
        Groq gibi dar ücretsiz katmanlarda sağlayıcı sınırı). Sayı yazılırsa o kullanılır."""
        v = self._compact_cfg
        cap = self.compact_cap or 10**9
        if isinstance(v, (int, float)) and not isinstance(v, bool) and int(v) > 0 and int(v) != LEGACY_COMPACT_AT:
            return min(int(v), cap)
        try:
            ref = self.router.last_ref.get(self.role) or self.router.first_available(self.role)
        except KeyError:
            ref = None
        if not ref:
            return min(LEGACY_COMPACT_AT, cap)
        return min(max(16000, int(self.router.context_limit(ref) * 0.8)), cap)

    @compact_at.setter
    def compact_at(self, value: int) -> None:
        self._compact_cfg = int(value)

    def context_tokens(self) -> int:
        """Bağlamdaki token sayısı. Son istekte sağlayıcının kendi saydığı değer (araç tanımları ve sistem istemi
        dahil) esas alınır, ondan sonra eklenen mesajlar tahminle eklenir; yoksa tamamen tahmin."""
        est = estimate_tokens(self.messages, self.tool_specs())
        lp = self._last_prompt
        if lp and lp[0] > 0 and 0 < lp[1] <= len(self.messages) and self.messages[lp[1] - 1] is lp[2]:
            real = lp[0] + (estimate_tokens(self.messages[lp[1]:]) if lp[1] < len(self.messages) else 0)
            # Tahminden 4 kat küçükse sağlayıcının sayısı güvenilmez (ör. önbellekli kısmı saymayan uç): tahmin kalsın.
            if real * 4 >= est:
                return real
        return est

    def _maybe_compact(self, force: bool = False, limit: int = 0) -> bool:
        """Konuşmayı özetle. limit: konuşmanın sığması gereken model bağlamı (ör. sadece küçük modeller açıkken)."""
        if not force and self.context_tokens() < self.compact_at:
            return False
        window = min(limit, self.compact_at) if limit else self.compact_at
        budget = int(window * 0.35)
        summary_tokens = 2000
        if limit:
            # Dar model: sistem istemi ve araç tanımları sabit yer kaplar; özet ve kalan konuşma geri kalana sığmalı.
            fixed = estimate_tokens(self.messages[:1], self._specs())
            summary_tokens = max(300, min(2000, limit // 8))
            budget = int(limit * 0.85) - fixed - summary_tokens
            if budget < 500:
                return False
        # Kesme noktaları: kullanıcı mesajları ve asistan adımlarının başı (araç çağrısı ile sonucu ayrılmaz).
        # Ajanlar tek görev mesajıyla uzun araç zincirleri yürüttüğü için asistan adımlarından da kesilebilmeli.
        users = [i for i, m in enumerate(self.messages) if i > 1 and m["role"] in ("user", "assistant")
                 and not m.get("_image")]
        if not users:
            return False
        cut = users[-1]
        for i in reversed(users):
            if estimate_tokens(self.messages[i:]) <= budget:
                cut = i
            else:
                break
        old = self.messages[1:cut]
        if not old:
            # Tek turda bile çok büyümüş: büyük araç çıktılarını kısalt.
            changed = False
            for m in self.messages[cut:]:
                if m["role"] == "tool" and isinstance(m.get("content"), str) and len(m["content"]) > 3000:
                    m["content"] = truncate(m["content"], 3000)
                    changed = True
            return changed and bool(limit)
        transcript = []
        for m in strip_images(old):
            c = m.get("content") or ""
            if m.get("tool_calls"):
                c += "\n" + "\n".join(f"-> {tc['function']['name']}({tc['function']['arguments'][:400]})" for tc in m["tool_calls"])
            transcript.append(f"[{m['role']}] {truncate(c, 2500)}")
        self.ui.info("context büyüdü, özetleniyor...")
        try:
            res: ChatResult = self.router.chat("brain", [
                {"role": "system", "content": COMPACT_PROMPT},
                # Özet isteği de modele sığmalı: dar bağlamda dökümün sonu (karakter ≈ 3 token) gönderilir.
                {"role": "user", "content": "\n\n".join(transcript)[-(int(limit * 2.4) if limit else
                                                                       max(120000, self.compact_at * 2)):]},
            ], max_tokens=summary_tokens)
        except AllProvidersFailed as e:
            self.ui.info(f"özetleme başarısız: {e}")
            return False
        summary = (res.text or "").strip()
        if not _valid_summary(summary):
            # Model özet yerine araç çağrısı metni ya da dökümün kopyasını döndürdü (11 Kasım: "<tool calls>…" ve
            # "-> git({…}) [tool] …" özet diye kabul edildi, ALICE işin ne olduğunu unuttu). Böyle bir "özet"
            # konuşmanın yerine geçmesin: kullanıcının istekleri ve son cevap birebir korunur.
            self.ui.info("model geçerli bir özet üretemedi; kullanıcı istekleri ve son durum birebir saklandı")
            summary = _clip(_fallback_summary(old), summary_tokens * 3)  # dar modelin özet payına sığsın
        if self.on_compact is not None:
            try:
                self.on_compact(old)  # arayüz eski mesajları göstermeye devam etsin (oturum arşivi)
            except Exception:  # noqa: BLE001 - arşiv yazılamadı diye özetleme durmasın
                log.debug("on_compact hata verdi", exc_info=True)
        head = [self.messages[0], {"role": "user", "content": f"[Önceki konuşmanın özeti]\n{summary}"}]
        if self.messages[cut]["role"] == "user":  # roller sırayla gitsin
            head.append({"role": "assistant", "content": "Özeti aldım, kaldığım yerden devam ediyorum."})
        self.messages = head + self.messages[cut:]
        self.checkpoints.clear()  # mesaj indeksleri değişti, eski geri sarma noktaları geçersiz
        if self.on_step is not None:
            # Hemen kaydet: arşive yazılan mesajlar oturum dosyasında da kalırsa (tur burada hata verir ya da ALICE
            # yeniden başlarsa) açılışta yeniden yüklenip bir kez daha özetleniyor, arşive ikinci kez yazılıyordu.
            try:
                self.on_step()
            except Exception:  # noqa: BLE001 - kayıt hatası özetlemeyi geri almasın
                log.debug("on_step hata verdi", exc_info=True)
        return True

    def _roomy(self, res: ChatResult) -> bool:
        try:
            limit = self.router.context_limit(f"{res.provider}:{res.model}")
        except Exception:  # noqa: BLE001
            return False
        return limit > estimate_tokens(self.messages[:1], self.tool_specs()) * 3

    def _specs(self) -> list[dict[str, Any]]:
        specs = self.tool_specs()
        return lean_specs(specs) if getattr(self, "_lean", False) else specs

    def compact(self) -> bool:
        return self._maybe_compact(force=True)

    def _repair_after_interrupt(self) -> None:
        """Yarım kalan tool_calls için sahte sonuç ekle, yoksa sonraki istek 400 alır."""
        for i in range(len(self.messages) - 1, 0, -1):
            m = self.messages[i]
            if m["role"] == "assistant" and m.get("tool_calls"):
                answered = {x.get("tool_call_id") for x in self.messages[i + 1:] if x["role"] == "tool"}
                # Sonuçlar çağrının hemen ardındaki tool bloğuna girmeli: araya yarım metin ya da görüntü mesajı
                # girdiyse sona eklenen sonuç sahipsiz kalıyor ve sağlayıcı bütün konuşmayı reddediyordu.
                at = i + 1
                while at < len(self.messages) and self.messages[at]["role"] == "tool":
                    at += 1
                missing = [{"role": "tool", "tool_call_id": tc["id"], "name": tc["function"]["name"],
                            "content": "durduruldu (kullanıcı kesti)"}
                           for tc in m["tool_calls"] if tc["id"] not in answered]
                self.messages[at:at] = missing
                break

    def reset(self) -> None:
        self.messages = self.messages[:1]
