# ALICE

**Artificial Logic and Intelligence Computation Engine.** Bilgisayarında çalışan, Claude Code / Codex tarzı bir ajan harness. Fark şu: tek bir ücretli modele bağlı değil, ücretsiz API katmanlarını sırayla kullanıyor ve hızlı arayüz kararları için Jev gibi System One modelleri kullanabiliyor.

Neler yapabiliyor:

* Dosya okuma/yazma/düzenleme, kod tabanında arama (`grep`, `glob`)
* PowerShell/bash komutları, arka planda sunucu çalıştırıp çıktısını izleme
* Ekran görüntüsü alıp fare ve klavyeyi kullanma (Türkçe karakter yazabiliyor)
* Windows UI Automation ile pencerelerin butonlarını ID'leriyle görüp tıklama (pixel tahmini yok)
* Gerçek tarayıcıyı kontrol etme (kalıcı profil, bir kere giriş yaptığın siteler açık kalıyor)
* Uygulama açma, pencere yönetimi, süreç listeleme/öldürme, sistem bilgisi, pano
* Web arama (anahtarsız) ve sayfa okuma
* **Reflex modu:** net bir arayüz hedefini System One kararlarıyla adım başına ~100-500 ms'de yürütme
* Alt ajan, todo planı, oturumlar arası hafıza, oturum kaydetme/devam ettirme, otomatik context özetleme

## Mimari

```
                 ┌──────────────────────── ALICE ────────────────────────┐
  sen ──► CLI ──►│ Agent (System 2)                                       │
                 │   brain modeli ⇄ araçlar (dosya, shell, ekran, UIA,    │
                 │                  tarayıcı, web, sistem, alt ajan)      │
                 │        │                                               │
                 │        └─► reflex aracı ──► ReflexLoop (System 1)      │
                 │              observe → aday listesi → Decider → act    │
                 │                                   │                    │
                 │                    Jev ──(yoksa)──► hızlı LLM (JSON)   │
                 │                                                        │
                 │ Router: her rol için sağlayıcı zinciri + otomatik geçiş│
                 └────────────────────────────────────────────────────────┘
```

**İki sistemli yapı.** Büyük model (brain) planlar, kod yazar, karar verir. Ama "Ayarlar'a gir, Gizlilik'e tıkla, şu kutuyu işaretle" gibi işlerde her tıklamayı büyük modele sormak hem yavaş hem free tier kotasını yakıyor. O yüzden brain bu tür alt hedefleri `reflex` aracına devrediyor:

1. Ekran ID'li bir aday listesine çevriliyor (tarayıcıda DOM, Windows'ta UI Automation)
2. Hedefle en alakalı 40 aday seçiliyor
3. Tek bir System One çağrısı: "sıradaki aksiyon hangisi?" + "hedef tamam mı?"
4. Seçilen aksiyon uygulanıyor, metin yazmak gerekiyorsa hızlı LLM üretiyor
5. Düşük güven, aynı aksiyonun tekrarı ya da "stuck" olursa kontrol brain'e geri dönüyor

Bu, Cua'nın `jev-use` tarifiyle aynı mantık: Jev bir ajan değil, bir karar bileşeni.

## Model seçimi: neden bu sıra

Ekim 2026 itibarıyla ücretsiz katmanları taradım. Rakamlar sık değişiyor, kaynaklar birbiriyle çelişebiliyor; aşağıdaki tablo kararın gerekçesi, kesin limit için her sağlayıcının kendi panelini kontrol et.

| Sağlayıcı | Ücretsiz olarak ne veriyor | ALICE'teki rolü |
|---|---|---|
| **Google Gemini (AI Studio)** | Flash ve Flash-Lite ücretsiz, 1M context, görsel destekli. Pro modeller Nisan 2026'da free tier'dan çıktı. Free tier verileri ürün geliştirmede kullanılabiliyor. | **brain #1, vision #1.** Büyük context + görsel tek modelde, ajan için en uygun ücretsiz seçenek |
| **OpenRouter** | `:free` modeller 20 RPM / günde 50 istek; tek seferlik 10$ yüklemeyle günde 1000. Katalog birkaç ayda bir değişiyor (gpt-oss-120b, Poolside Laguna, North Mini Code vb.) | brain yedeği, `openrouter/free` son çare router |
| **NVIDIA NIM** | Developer Program ile ~40 RPM (hesap bazlı), qwen3-coder-480b, deepseek gibi büyük modeller | brain yedeği, güçlü kodlama modeli |
| **Groq** | Günde 1000 istek ama dakikada 8K, günde 200K token. Llama ve Kimi kaldırıldı, gpt-oss duruyor. Çok hızlı | **fast #1.** Kısa işler ve Jev emülasyonu için ideal, büyük context'e uygun değil |
| **Cerebras** | Son raporlara göre süreli kredi, kalıcı free tier belirsiz | yedek |
| **Ollama (yerel)** | Limitsiz, offline, GPU'na bağlı | her zincirin sonunda son çare |

**Tek sağlayıcıya güvenmiyorum.** Router her rol için bir zincir tutuyor. 429 gelince o model soğumaya alınıyor ve sıradakine geçiliyor, günlük kota dolunca 1 saat, anahtar yanlışsa sağlayıcı oturum boyunca, model katalogdan kalkmışsa o model kapatılıyor. İstek bir sağlayıcının token sınırını aşıyorsa (Groq'un 8K TPM'i gibi) o sağlayıcı o istek için atlanıyor. Yani bir sağlayıcı düşse de ALICE çalışmaya devam ediyor.

Katalog değiştiğinde `alice models --write` canlı `/models` uçlarını tarayıp zincirleri güncelliyor.

### Jev hakkında net bilgi

Jev (TypeSafe AI, Eylül 2026) metin üretmeyen, tipli karar veren bir System One modeli. **Ücretsiz katmanı yok**: girdi tokenı başına 0.042$/milyon, çıktı ücretsiz, şu an erken erişim listesiyle. Pratikte bu çok ucuz (bir reflex adımı ~1-2K token, yani milyonlarca karar birkaç dolar), ama sen ücretsiz istediğin için şöyle kurdum:

* `TYPESAFE_API_KEY` varsa karar motoru Jev (REST `POST /v1/systemone`, Choice/Noul/Score soruları)
* TypeSafe yeni kayıtları durdurduğu için Jev'e **OpenRouter üzerinden** de gidiliyor (`~typesafe/jev-latest`, aynı `OPENROUTER_API_KEY`). Burada da `:free` sürümü yok, hesapta kredi olmalı; kredi yoksa OpenRouter 402 döner, ALICE bunu görünce Jev'i 1 saat atlayıp yedeğe geçer
* Yoksa ya da Jev hata verirse aynı soru formatı, `fast` rolündeki ücretsiz hızlı modele (Groq gpt-oss-20b, `reasoning_effort=low`) JSON olarak soruluyor

Yani reflex modu anahtarsız da çalışıyor, sadece daha yavaş: Jev için bildirilen süre ~100-350 ms, Groq yedeği ise benim tahminimle adım başına birkaç yüz ms ile 1-2 sn arası (ölçmek için `alice doctor` karar süresini yazıyor). Jev erişimi alırsan tek satır anahtar eklemen yetiyor. Jev'e Score soruları güvenli olsun diye Choice olarak gidiyor, çünkü resmi dokümanda en net belgelenen format o.

## Kurulum (Windows)

Python 3.10+ lazım (python.org, "Add to PATH" işaretli).

```powershell
git clone https://github.com/hayatexe/Artificial-Logic-and-Intelligence-Computation-Engine
cd Artificial-Logic-and-Intelligence-Computation-Engine
powershell -ExecutionPolicy Bypass -File install.ps1
```

Script `.venv` açıyor, her şeyi kuruyor, Chrome/Edge yoksa Playwright Chromium indiriyor ve `alice init` çalıştırıyor. Elle yapmak istersen:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[all]"
alice init
```

Linux/macOS'ta aynısı, `pip install -e ".[all]"` sonrası `alice init`. Linux'ta pencere listesi için `wmctrl` lazım; UI Automation şimdilik sadece Windows'ta.

### API anahtarları

`alice init` `%USERPROFILE%\.alice\.env` dosyasını oluşturuyor. En az birini doldur, hepsini doldurursan zincir o kadar dayanıklı olur:

* `GEMINI_API_KEY`: https://aistudio.google.com/apikey (en önemlisi bu)
* `GROQ_API_KEY`: https://console.groq.com/keys (reflex ve hızlı işler için)
* `OPENROUTER_API_KEY`: https://openrouter.ai/keys
* `NVIDIA_API_KEY`: https://build.nvidia.com
* `CEREBRAS_API_KEY`: https://cloud.cerebras.ai
* `TYPESAFE_API_KEY`: Jev erişimi alırsan

Sonra:

```powershell
alice doctor          # her sağlayıcıya test isteği atar, karar motorunu dener, eksik paketleri gösterir
alice models --write  # canlı ücretsiz katalogdan rol zincirlerini günceller (opsiyonel)
```

## Kullanım

```powershell
alice                                   # etkileşimli oturum
alice "bu klasördeki Unity projesinde PlayerController'daki zıplama bug'ını bul ve düzelt"
alice -p "RAM'i en çok yiyen 5 süreci listele"   # tek seferlik, sonucu yazıp çıkar
alice --yolo                            # hiçbir şey sormadan tam erişim
alice --resume                          # son oturuma devam
alice -v                                # hangi modelin cevap verdiğini göster
```

Reflex'i tek başına denemek için:

```powershell
alice reflex "Ayarlar sayfasını açıp Gizlilik sekmesine geç" --target browser --url https://github.com
alice reflex "Kaydet butonuna bas" --target desktop --title "Not Defteri"
alice reflex "hava durumunu ara" --target browser --url https://duckduckgo.com --input "ara=ankara hava durumu"
```

REPL komutları: `/help`, `/mode ask|auto|yolo`, `/model` (zincir durumları), `/model brain groq:openai/gpt-oss-120b` (sabitle), `/usage` (bugünkü istek/token), `/compact`, `/clear`, `/sessions`, `/resume`, `/cwd`, `/tools`, `/exit`.

## İzin modları ve güvenlik

* **ask:** okuma dışındaki her şey onay ister
* **auto** (varsayılan): dosya düzenleme, komut çalıştırma, fare/klavye serbest; `rm -r`, `Remove-Item -Recurse`, format, registry, shutdown, `git push --force`, `curl | sh` gibi tehlikeli kalıplar ve `C:\Windows`, `Program Files` gibi sistem klasörlerine yazma onay ister
* **yolo:** hiçbir şey sormaz

Onay sorusunda `a` dersen o araca oturum boyunca izin veriyorsun (tehlikeli kalıplar hariç).

Varsayılanı `auto` yaptım çünkü tam erişim istedin ama bir free modelin yanlış anlayıp yanlış klasörü silmesi geri alınamıyor; `auto` normal işte neredeyse hiç soru sormuyor. Tamamen sorusuz istiyorsan `--yolo` ya da config'te `permission_mode = "yolo"`.

**Acil durdurma:** çalışırken `Ctrl+C` mevcut işi keser. Fareyi ekranın **sol üst köşesine** götürmek fare/klavye kontrolünü anında durdurur (pyautogui failsafe).

## Config

`~/.alice/config.toml` (Windows'ta `%USERPROFILE%\.alice\config.toml`). Önemli kısımlar:

```toml
[agent]
permission_mode = "auto"
compact_at_tokens = 48000   # bu boyutta context özetlenir (free TPM limitleri dar)
keep_images = 2             # geçmişte tutulan ekran görüntüsü sayısı
browser_headless = false

[roles]
brain = ["gemini:gemini-flash-latest", "openrouter:openai/gpt-oss-120b:free", "..."]
fast  = ["groq:openai/gpt-oss-20b", "..."]
vision = ["gemini:gemini-flash-latest", "..."]

[decider]
chain = ["jev", "llm:fast"]
min_confidence = 0.55       # altındaki reflex kararları brain'e devredilir

[models."groq:openai/gpt-oss-20b"]
extra = { reasoning_effort = "low" }   # isteğe eklenen ek parametreler
```

Yeni bir OpenAI uyumlu sağlayıcı eklemek için `[providers.isim]` altına `base_url`, `api_key_env`, `vision_models`, `max_input_tokens` yazman yeterli.

Proje klasöründe `ALICE.md` (ya da `CLAUDE.md` / `AGENTS.md`) varsa o projeye özel talimat olarak okunur. `~/.alice/ALICE.md` her oturumda okunur. `memory` aracının kaydettiği notlar `~/.alice/memory.md`'de durur.

## Görsel desteği nasıl çalışıyor

Ekran görüntüsü alındığında, o an kullanılacak brain modeli görsel destekliyorsa görüntü direkt ona gidiyor. Desteklemiyorsa görüntü `vision` rolündeki modele "bu ekranda ne var, butonlar nerede" diye açıklatılıyor ve brain metni görüyor. Böylece görsel desteği olmayan güçlü kodlama modelleri de ekranla çalışabiliyor. Eski ekran görüntüleri context'ten otomatik atılıyor.

## Bilinen sınırlar

* Oyunlar (DCS, War Thunder) UI Automation'a görünmez; orada `computer` aracıyla ekran görüntüsü ve tuş basma kullanılıyor, bu da reflex kadar hızlı değil.
* Ücretsiz modeller tool calling'de Claude/GPT seviyesinde değil. Router hatalı JSON'u modele geri bildiriyor, çoğunlukla ikinci denemede düzeliyor, ama uzun ve karmaşık işlerde Gemini Flash'ın önde olması bu yüzden.
* Jev'in API'si erken erişimde; alan adları değişirse `alice/decider.py` içindeki `JevEngine._parse` toleranslı ama güncelleme gerekebilir. Jev hata verirse otomatik olarak LLM yedeğine geçiliyor.
* UI Automation Linux/macOS'ta yok; orada tarayıcı ve `computer` aracı kullanılıyor.

## Geliştirme

```bash
pip install -e ".[dev]"
pytest
```

Testler sahte sağlayıcılarla router geçişlerini, ajan döngüsünü (araç çağırma, izin reddi, görsel yönlendirme, context özetleme, kesinti onarımı, alt ajan), Jev istek/cevap formatını ve reflex döngüsünü kapsıyor.

## Kaynaklar

* [OpenRouter: Free LLM API in 2026](https://openrouter.ai/blog/tutorials/free-llm-apis-compared/)
* [Free LLM API Tiers (2026): Limits Verified Daily](https://gravity.fast/data/free-llm-api-tiers/)
* [Groq Free Tier 2026: 1,000 Requests a Day, Llama Is Gone](https://klymentiev.com/blog/groq-pricing)
* [Gemini API Free Tier 2026](https://pecollective.com/tools/gemini-free-tier-guide/)
* [NVIDIA NIM free tier limits](https://news.creeta.com/en/nvidia-nim-free-tier-limits-2026/)
* [Best Free OpenRouter Models for Programming (2026)](https://aireiter.com/blog/best-free-openrouter-models-for-programming-2026)
* [TypeSafe Jev quickstart](https://docs.typesafe.ai/introduction/quickstart)
* [How to build with System One](https://docs.typesafe.ai/concepts/how-to-build-with-system-one)
* [Jev pricing](https://www.mindstudio.ai/blog/jev-pricing-cost-per-token)
* [Cua: Jev, System One models, and the future of computer use](https://x.com/trycua/status/2101437979180904640)
* [Jev Ultrafast web agent architecture](https://dev.to/terminalchai/jev-ultrafast-the-sub-10-second-web-agent-architecture-1728)
