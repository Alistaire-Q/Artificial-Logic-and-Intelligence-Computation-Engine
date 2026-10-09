# ALICE Yol Haritası: Kod Ajanından Kişisel Asistana

Bu belge ALICE'in bir sonraki büyük adımının planıdır. Şu anki ALICE sen yazınca çalışan bir ajan. Hedef, arka planda sürekli çalışan, seni dinleyen, kendi sesiyle konuşan, bilgisayarı, oyunları ve sağlık verisini izleyip gerektiğinde kendiliğinden devreye giren bir asistan. Kısacası JARVIS.

Durum: **plan aşaması.** Kod yazımı onay sonrası 1. aşamadan başlayacak.

---

## İçindekiler

1. [Verilen kararlar](#1-verilen-kararlar)
2. [Genel mimari](#2-genel-mimari)
3. [Donanım bütçesi (RTX 4060, 8 GB)](#3-donanım-bütçesi-rtx-4060-8-gb)
4. [Aşama 0: Hazırlık](#aşama-0-hazırlık)
5. [Aşama 1: Çekirdek](#aşama-1-çekirdek)
6. [Aşama 2: Ses ve hızlı bar](#aşama-2-ses-ve-hızlı-bar)
7. [Aşama 3: Donanım izleme ve zamanlanmış görevler](#aşama-3-donanım-izleme-ve-zamanlanmış-görevler)
8. [Aşama 4: DCS F-16 ve War Thunder](#aşama-4-dcs-f-16-ve-war-thunder)
9. [Aşama 5: Telefon köprüsü ve akıllı saat](#aşama-5-telefon-köprüsü-ve-akıllı-saat)
10. [Aşama 6: Ekler](#aşama-6-ekler)
11. [Veri formatları](#veri-formatları)
12. [Riskler ve açık sorular](#riskler-ve-açık-sorular)
13. [Kaynaklar](#kaynaklar)

---

## 1. Verilen kararlar

| Konu | Karar |
|---|---|
| Ses girişi | Bas-konuş tuşu **ve** "Hey Alice" uyandırma kelimesi, ikisi birden |
| ALICE'in sesi | Kadın sesi. Varsayılan sakin, duruma göre şakacı. Tonu duyguya göre değişecek, düz robot okuması olmayacak |
| Microsoft sesleri (edge-tts) | Kalitesi yetersiz bulundu, kullanılmayacak |
| Ekran kartı | RTX 4060, 8 GB VRAM. DCS açıkken GPU'da yer yok, plan buna göre kuruldu |
| DCS | F-16C. Tek oyunculu ve çok oyunculu. **Sadece okuma ve sesli uyarı, oyuna hiçbir girdi gönderilmeyecek** |
| War Thunder | Sadece okuma ve uyarı. Girdi otomasyonu yok (anti-cheat riski) |
| Telefon | Android |
| Telegram | Kullanılmayacak |
| Staj defteri | Gerek yok, plandan çıkarıldı |
| Akıllı saat | Huawei Watch GT 2 Pro. Saatin kendi nabız yayını yok. Gadgetbridge üzerinden telefon köprüsü deneniyor (test sürüyor) |
| Tetikleme mantığı | Veri sürekli akacak ama ajan her veride değil, kurallar tetikleyince devreye girecek |

---

## 2. Genel mimari

```
┌────────────────────────────── ALICE Çekirdek (arka plan servisi, tepsi ikonu) ──────────────────────────────┐
│                                                                                                               │
│  SENSÖRLER ─────────────►  OLAY HATTI  ─────────►  KAYIT (SQLite, son N saat/gün)                            │
│  * donanım (LHM, nvidia-smi)      │                                                                           │
│  * DCS / War Thunder              │                                                                           │
│  * telefon köprüsü (nabız)        ▼                                                                           │
│  * zamanlayıcı              KURAL MOTORU                                                                      │
│  * aktif pencere            ├─ 1. Refleks kuralı  (eşik + süre, ms)          ──┐                              │
│  * dosya izleyici           ├─ 2. Karar / Jev     (belirsiz durum, ~100-500 ms) ├──► AKSİYON                   │
│                             └─ 3. Ajan / LLM      (araştır, araç kullan, sn)  ──┘    * konuş (TTS)            │
│                                                                                       * bildirim              │
│  BAĞLAM MODU: normal / uçuş (DCS) / oyun (WT) / geliştirme (Unity) / gece             * uygulamaya yaz        │
│                                                                                       * ajanı çalıştır        │
│                                                                                                               │
│  SES HATTI:  mikrofon → (bas-konuş | Hey Alice) → VAD → STT → ajan/hızlı model → duygu etiketleri → TTS → hoparlör │
│  HIZLI BAR:  Ctrl+Alt+Space (yaz/konuş), Ctrl+Alt+S (ekrandan alan seç ve sor)                               │
└───────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
                     ▲                                       ▲
       ALICE uygulaması (mevcut)              Telefon (Gadgetbridge köprüsü, tarayıcıdan ALICE)
```

**Temel ilke:** LLM pahalı ve yavaş (ücretsiz kotalar sınırlı), kurallar bedava ve anlık. Sürekli akan veri önce kural motoruna gider. Sadece gerektiğinde bir üst seviyeye çıkar.

---

## 3. Donanım bütçesi (RTX 4060, 8 GB)

DCS tek başına 8 GB'ın neredeyse tamamını kullanabiliyor. Bu yüzden ALICE, hangi programın açık olduğuna göre modelleri yer değiştirecek.

| Bileşen | Yerel VRAM (tahmini) | DCS kapalı | DCS açık |
|---|---|---|---|
| Konuşma tanıma (STT) | faster-whisper ~1,5-3 GB | Yerel (GPU) | Bulut (Groq Whisper) |
| ALICE'in sesi (TTS, dinamik cümleler) | XTTS v2 ~4-5 GB | Yerel (GPU) | Bulut (Gemini TTS) |
| Uçuş uyarıları | 0 | Önceden üretilmiş ses dosyası | Önceden üretilmiş ses dosyası |
| Uyandırma kelimesi | CPU, çok küçük | CPU | CPU |
| Acil yedek ses | Piper, CPU | Gerekirse | Gerekirse |
| LLM (beyin) | 0 (bulut) | Bulut | Bulut |

**Kural:** DCS açıkken ALICE GPU'ya hiçbir model yüklemez. Geçiş bağlam modu tarafından otomatik yapılır; DCS kapanınca yerel modeller geri yüklenir.

---

## Aşama 0: Hazırlık

Kod gerektirmeyen, senin tarafındaki adımlar. Diğer aşamaların sağlıklı test edilebilmesi için önemli.

* [ ] ALICE'i kendi bilgisayarında gerçek API anahtarlarıyla çalıştır (`install.ps1`, `.env`, `alice doctor`)
* [ ] Birkaç gün normal işlerde kullan, takıldığın yerleri not et
* [ ] Gadgetbridge testi: saati 20-30 dk takıp senkronize et, nabız ve adım geliyor mu bak (bkz. [Aşama 5](#aşama-5-telefon-köprüsü-ve-akıllı-saat))
* [ ] Gemini API anahtarının AI Studio'da TTS modellerine erişimi olduğunu kontrol et
* [ ] Groq anahtarı (Whisper için)

---

## Aşama 1: Çekirdek

Diğer her şey bunun üzerine oturuyor. Kullanıcıya görünen yeni özellik az, ama sonraki her aşamanın temeli.

### 1.1 Arka plan servisi ve tepsi ikonu

* [ ] `alice daemon` komutu: ALICE'i pencere olmadan arka planda çalıştırır
* [ ] Windows açılışında otomatik başlama (kurulumda seçenek, `alice autostart on/off`)
* [ ] Sistem tepsisi ikonu (pystray). Menü:
  * Uygulamayı aç
  * Sessiz mod (1 saat / akşama kadar / kapat)
  * Mod göster (normal / uçuş / oyun / geliştirme / gece)
  * Çıkış
* [ ] Uygulama penceresi kapansa da servis çalışmaya devam eder; uygulama açılınca servise bağlanır (mevcut yerel sunucu servisin içine taşınır)
* [ ] Tek örnek koruması: ikinci kez başlatılırsa mevcut servisi öne getirir

### 1.2 Olay hattı (event bus)

* [ ] Süreç içi yayınla/abone ol yapısı (konu bazlı: `hw.gpu.temp`, `dcs.fuel`, `health.hr` ...)
* [ ] Her olay: zaman damgası, kaynak, konu, değer, birim (bkz. [Veri formatları](#veri-formatları))
* [ ] Yavaş tüketici hızlı üreticiyi bloklamaz (kuyruk + düşürme politikası)

### 1.3 Kayıt (zaman serisi)

* [ ] SQLite veritabanı: `~/.alice/telemetry.db`
* [ ] Saklama süresi konu bazında: ham veri 48 saat, dakikalık özet 30 gün, günlük özet 1 yıl
* [ ] Ajan için yeni araç: `telemetry_query` ("son 1 saatte GPU sıcaklığı", "dün 20:00-23:00 arası DCS'te en yüksek G")
* [ ] Uygulamada küçük grafik bileşeni (ileride donanım ve uçuş değerlendirmesi için)

### 1.4 Kural motoru

* [ ] Kural tanımı: koşul, süre, gecikme payı (hysteresis), bekleme süresi (cooldown), geçerli olduğu modlar, aksiyon
* [ ] Üç tetikleme seviyesi:
  * **Refleks:** eşik ve süre koşulu, doğrudan aksiyon (konuş, bildir)
  * **Karar:** Jev / System One ile belirsiz durumu sınıflandır ("bu önemli mi, rahatsız etmeye değer mi?")
  * **Ajan:** LLM'e bağlamla birlikte görev ver ("GPU 90°C'yi geçti, nedenini araştır ve öner")
* [ ] Kurallar `~/.alice/rules/*.toml` dosyalarında
* [ ] Ajan için yeni araçlar: `rule_create`, `rule_list`, `rule_delete`. "Nabzım 2 dakika 150 üstünde kalırsa beni uyar" denince kuralı ALICE kendisi yazar, gösterip onaylatır
* [ ] Spam koruması: aynı kural tekrar tetiklenmeden önce bekleme süresi, saatte en fazla N bildirim

### 1.5 Bildirim yönlendirici

* [ ] Kanallar: ses (TTS), Windows bildirimi (toast), uygulama içi not
* [ ] Öncelik seviyeleri: bilgi / uyarı / acil
* [ ] Yönlendirme kuralı: moda ve önceliğe göre kanal seçimi
  * Uçuş modunda sadece uçuş uyarıları ve acil olanlar, sadece ses, kısa cümle
  * Gece modunda acil dışında sessiz
  * Bilgisayar başında değilsen (fare/klavye 10 dk hareketsiz) bildirimler biriktirilir, dönünce özetlenir

### 1.6 Bağlam modları

* [ ] Aktif pencere ve çalışan süreç izleme (DCS.exe, aces.exe, Unity.exe ...)
* [ ] Modlar: normal, uçuş (DCS), oyun (War Thunder ve diğerleri), geliştirme (Unity / VS Code), gece (saat aralığı)
* [ ] Mod değişince: kurallar açılır/kapanır, bildirim yönlendirmesi değişir, GPU modelleri yer değiştirir ([bkz. donanım bütçesi](#3-donanım-bütçesi-rtx-4060-8-gb))
* [ ] Elle mod sabitleme (tepsi menüsünden)

**Kabul kriterleri**
* Servis 24 saat boyunca hatasız çalışır, bellek kullanımı sabit kalır
* Sahte bir sensörle tanımlanan kural doğru tetiklenir, bekleme süresine uyar, yanlış modda tetiklenmez
* Ajan "son 10 dakikanın verisi" sorusuna kayıttan doğru cevap verir

---

## Aşama 2: Ses ve hızlı bar

Ana odak bu. Önce sesi seçeriz, sonra hattı kurarız.

### 2.1 Ses dinleme testi (ilk iş, kod yazmadan önce karar)

* [ ] 20-25 Türkçe test cümlesi: ı, ğ, ş, ö, ü yoğun; ALICE'in gerçekte söyleyeceği türden (selam, hata bildirimi, şaka, acil uyarı, uzun açıklama)
* [ ] Her cümle farklı duygularda: sakin, şakacı, ciddi, acil, yumuşak
* [ ] Adaylar:
  * Gemini TTS (birkaç farklı kadın sesi, stil talimatıyla)
  * XTTS v2 (Gemini sesinden kopyalanmış)
  * Chatterbox (Türkçe kalitesi doğrulanacak)
  * Referans olarak Piper
* [ ] Her biri için ölçüm: ilk sesin gelme süresi, VRAM kullanımı
* [ ] Sonuç: ses dosyaları + basit bir dinleme sayfası. Kulakla seçeceksin

### 2.2 ALICE'in ses kimliği ve kişiliği

* [ ] Ses tasarımı: seçilen ana ses + tarif ("sakin, sıcak, hafif şakacı genç bir kadın")
* [ ] Kişilik dosyası `~/.alice/persona.md`:
  * Sana hitap şekli
  * Varsayılan ton: sakin
  * Şaka sıklığı ve ne zaman şaka yapılmayacağı (acil ve ciddi durumlarda **asla**)
  * Sesli cevap uzunluğu: kısa, Markdown yok, ayrıntı uygulamada
* [ ] Sesli ve yazılı cevap için ayrı üslup: sesli olanı kısa, yazılı olan ayrıntılı

### 2.3 Duygu katmanı

* [ ] Ton etiketleri: `sakin` (varsayılan), `şakacı`, `neşeli`, `ciddi`, `acil`, `yumuşak`
* [ ] Anlık efektler: gülme, iç çekme, kısa duraklama
* [ ] Sesli cevap üretirken model her cümleyi etiketler (bkz. [Veri formatları](#duygu-etiketli-sesli-cevap))
* [ ] Etiket kuralları:
  * Acil ve ciddi durumda şaka yasak (motor seviyesinde zorlanır, sadece modele bırakılmaz)
  * Uyarılar her zaman `acil` ya da `ciddi`
  * Nabız verisi varsa ve yüksekse ALICE daha yavaş ve daha sakin konuşur
* [ ] Motorlara aktarım:
  * **Gemini TTS:** stil talimatı ("sıcak ve hafif eğlenmiş") + satır içi efekt etiketleri
  * **XTTS v2:** duygu kütüphanesinden o duygunun referans kaydı
  * **Chatterbox (seçilirse):** duygu yoğunluğu ayarı + referans kayıt
* [ ] Duygu kütüphanesi: aynı sesle her duygu için 3-5 referans kayıt (Gemini ile üretilir), `~/.alice/voice/<duygu>/*.wav`

### 2.4 Konuşma girişi

* [ ] Mikrofon yakalama (sounddevice), cihaz seçimi ayarlardan
* [ ] **Bas-konuş:** tuşa basılı tut, bırakınca gönder. Varsayılan tuş DCS'teki F-16 telsiz ve HOTAS tuşlarıyla çakışmayacak şekilde seçilecek, ayarlanabilir
* [ ] **"Hey Alice":**
  * openWakeWord ile özel model eğitimi. "Alice" İngilizce bir isim olduğu için İngilizce eğitim hattı kullanılabilir
  * Yanlış tetiklenme / kaçırma oranı testi
  * Uçuş modunda varsayılan kapalı (telsiz konuşmalarında yanlış tetiklenmesin), ayarla açılabilir
* [ ] Konuşmanın bitişini algılama: Silero VAD, ~300 ms sessizlik
* [ ] Konuşma tanıma:
  * Bulut: Groq `whisper-large-v3-turbo` (ücretsiz: saatte 7.200 ses saniyesi, günde 2.000 istek, istek başı en az 10 sn sayılır)
  * Yerel: faster-whisper (CUDA), DCS kapalıyken varsayılan
  * Türkçe dil ipucu ve kişisel kelime listesi (ALICE, F-16, Unity, bingo, joker ...) tanıma doğruluğu için

### 2.5 Konuşma çıkışı

* [ ] Cümle cümle akış: modelin ilk cümlesi hazır olunca konuşma başlar, gerisi arkadan gelir
* [ ] Ses çıkış kuyruğu: öncelikli mesajlar (acil uyarı) sıradakini keser
* [ ] **Sözünü kesebilme (barge-in):** ALICE konuşurken sen konuşmaya başlarsan ALICE susar ve dinler
* [ ] Ses çıkış cihazı seçimi (kulaklık / hoparlör)
* [ ] Ses seviyesi otomatik: uçuşta oyun sesinin üstünde duyulacak seviyede

### 2.6 Sesli konuşma akışı

* [ ] **İki aşamalı cevap:**
  * Sohbet ve basit soru → hızlı model, doğrudan cevap
  * Araç gerektiren iş → önce kısa onay ("Hemen bakıyorum"), iş arka planda, bitince sonuç sesli
* [ ] Takip konuşması: cevaptan sonra birkaç saniye "Hey Alice" demeden devam edebilme
* [ ] Sesli onay: tehlikeli işlemlerde ALICE sesli sorar, "evet / hayır" ile cevaplanır
* [ ] Her sesli konuşma uygulamada da yazılı görünür

### 2.7 Gecikme hedefi

| Adım | Hedef |
|---|---|
| Konuşmanın bittiğini algılama | ~300 ms |
| Yazıya çevirme | 400-700 ms |
| Modelin ilk cümlesi | 400-800 ms |
| İlk cümlenin sese dönmesi | ~200-400 ms |
| **Toplam (sen sustuktan sonra ilk ses)** | **~1,5-2,5 sn** |

* [ ] Her adımın süresi ölçülüp kaydedilir, uygulamada "son konuşmanın gecikme dökümü" gösterilir

### 2.8 Hızlı bar

* [ ] **Ctrl+Alt+Space:** ekranın ortasında küçük, her şeyin üstünde duran kutu. Yazma ya da aynı kutudan bas-konuş (ses dalgası görünür)
* [ ] Cevap küçük bir pencerede, istenirse sesli
* [ ] "Uygulamada devam et" butonu: konuşma ana pencereye taşınır
* [ ] **Ctrl+Alt+S:** ekranda alan seç, seçilen görüntü + isteğe bağlı soru ALICE'e gider (hata mesajı, Unity console, menü)
* [ ] Tam ekran oyun üstünde açılabilmesi test edilecek; açılamıyorsa oyunda sadece ses kullanılır
* [ ] Kısayollar ayarlardan değiştirilebilir

**Kabul kriterleri**
* Bas-konuş ile "saat kaç" sorusuna ortalama 2,5 sn altında sesli cevap
* Duygu geçişleri dinleme testinde ayırt edilebilir
* DCS açıkken ALICE GPU'ya model yüklemez (VRAM ölçümüyle doğrulanır)
* ALICE konuşurken araya girince 300 ms içinde susar

---

## Aşama 3: Donanım izleme ve zamanlanmış görevler

### 3.1 Donanım sensörleri

* [ ] **LibreHardwareMonitor** entegrasyonu (ücretsiz, açık kaynak): kendi yerel web sunucusundan CPU/GPU sıcaklıkları, fan, voltaj, saat hızları
* [ ] `nvidia-smi`: GPU kullanımı, VRAM, sıcaklık, güç
* [ ] psutil: CPU/RAM/disk kullanımı, en çok kaynak kullanan süreçler
* [ ] Disk doluluk ve sağlık (SMART, LibreHardwareMonitor üzerinden)
* [ ] Örnekleme: normalde 5 sn, oyun modunda 2 sn

### 3.2 Donanım kuralları (varsayılan, düzenlenebilir)

* [ ] GPU 30 sn boyunca 85°C üstü → uyarı
* [ ] CPU 30 sn boyunca 90°C üstü → uyarı
* [ ] Disk %90 dolu → günde bir bildirim
* [ ] RAM 2 dk boyunca %90 üstü → hangi programın şişirdiğini söyleyen uyarı
* [ ] Ani FPS düşüşü / takılma tespiti (oyun modunda): o anki sıcaklık ve süreçlerle birlikte kayda işaret koyar

### 3.3 Sorular ve raporlar

* [ ] "Son 1 saatte neden kasıldı?" → kayıttaki işaretli anlar, sıcaklık, süreçler
* [ ] "Dün DCS'te GPU kaç dereceydi?"
* [ ] Haftalık bilgisayar raporu (Aşama 6'da zamanlanmış görev olarak)

### 3.4 Zamanlanmış görevler

* [ ] Zamanlayıcı servisi (çekirdeğin içinde)
* [ ] Tanımlama doğal dille: "her pazar 23:00'te Unity projelerimi yedekle". ALICE zamanlama ifadesine çevirir, gösterip onaylatır
* [ ] Görev tipleri:
  * **Ajan görevi:** ALICE'e verilen istek (esnek, kota harcar)
  * **Doğrudan komut:** shell komutu ya da hazır aksiyon (hızlı, kota harcamaz)
* [ ] Kaçırılan görev politikası (görev başına): ilk açılışta çalıştır / atla
* [ ] Sonuç bildirimi: ses, Windows bildirimi, uygulama; başarısızlıkta hata özeti
* [ ] Çalışma geçmişi
* [ ] Ajan araçları: `schedule_create`, `schedule_list`, `schedule_delete`, `schedule_run_now`
* [ ] Uygulamada "Görevler" paneli: liste, sonraki çalışma zamanı, son sonuç, aç/kapat

**Kabul kriterleri**
* Sıcaklık eşiği aşılınca doğru modda, doğru kanaldan, bekleme süresine uyarak uyarı gelir
* Zamanlanmış görev bilgisayar kapalıyken kaçırılırsa seçilen politikaya göre davranır

---

## Aşama 4: DCS F-16 ve War Thunder

**Temel kural:** ALICE oyuna hiçbir tuş, komut ya da girdi göndermez. Sadece okur, hesaplar ve konuşur.

### 4.1 DCS veri köprüsü

* [ ] `Saved Games\DCS\Scripts\ALICE\alice_export.lua`: veriyi saniyede 10 kez yerel UDP ile gönderir
* [ ] `Export.lua`'ya tek satırlık yükleme ekler; Tacview, SRS, DCS-BIOS gibi araçların satırlarına dokunmaz, onları ezmez
* [ ] Kurulum/kaldırma komutu: `alice dcs install` / `alice dcs uninstall` (yedek alarak)
* [ ] Okunacak veriler (DCS export fonksiyonları): irtifa (deniz seviyesi ve yer), gösterge hızı, Mach, dikey hız, AoA, G, iç ve dış yakıt, iniş takımı ve flap durumu, chaff/flare sayısı, uçak tipi, konum
* [ ] Veri gelmezse (çok oyunculu sunucu kısıtlaması): ALICE bir kere "Bu sunucuda uçuş verisi kapalı" der ve veri gerektirmeyen özelliklere geçer

### 4.2 F-16C profili

Betty'nin (uçağın kendi sesli uyarı sistemi) zaten söylediklerini ("Altitude", "Pull up", "Warning", "Bingo") tekrar etmez. Onun yapmadıklarına odaklanır.

* [ ] Profil dosyası `~/.alice/dcs/profiles/F-16C_50.toml` (eşikler düzenlenebilir)
* [ ] **Yakıt yönetimi:**
  * Son birkaç dakikanın ortalamasından yakıt tüketim hızı
  * Kalan süre ("mevcut tüketimle 14 dakika")
  * Üsse dönüş için gereken yakıt (üs konumu görevden ya da elle) ve "dönüş için X dakika içinde ayrılmalısın" uyarısı
  * Joker / bingo değerleri profilden, istenirse uçuş öncesi sesle ayarlanır ("Alice, bingo 2500")
* [ ] **Yaklaşma ve iniş:** iniş takımı açık ve alçakken AoA takibi; 13° (doğru yaklaşma açısı) etrafında kısa geri bildirim ("biraz hızlısın", "on speed")
* [ ] **Uzun süreli yüksek G:** belli bir G'nin üstünde belli süre kalınca uyarı (eşikler profilde)
* [ ] **Karşı tedbir:** chaff/flare belli sayının altına inince bir kere bildirim
* [ ] Tüm uçuş uyarıları `acil` / `ciddi` tonunda, önceden üretilmiş ses dosyaları

### 4.3 Sesli kontrol listeleri

* [ ] F-16C için: soğuk başlatma, kalkış öncesi, iniş öncesi, iniş sonrası (metinler düzenlenebilir dosyada)
* [ ] "Alice, iniş kontrol listesi" → adım adım okuma, "tamam" / "tekrar" / "atla" ile ilerleme
* [ ] Veri varsa kontrol edilebilen adımları kendisi doğrular (ör. "iniş takımı: aşağıda, üç yeşil görüyorum")

### 4.4 Kneeboard

* [ ] Görev dosyasından (.miz, içinde Lua tablosu olan bir zip) frekanslar, waypoint'ler, hedef bilgileri, brifing metni okunur
* [ ] Sayfa olarak resim üretilip `Saved Games\DCS\Kneeboard\F-16C_50\` klasörüne konur, DCS oyun içi kneeboard'da gösterir
* [ ] Sesle not: "Alice, kneeboard'a not al: tanker 251.0, FL220" → yeni sayfa ya da mevcut not sayfasına ekleme

### 4.5 Uçuş sonrası değerlendirme

* [ ] Her uçuş kayda geçer (kalkıştan inişe ya da görev bitişine kadar)
* [ ] İnince kısa sesli özet + uygulamada rapor:
  * Süre, en yüksek G, en uzun yüksek G süresi, en düşük yakıt, en yüksek hız/irtifa
  * İniş yaklaşmasında AoA grafiği
  * Nabız verisi varsa G ve olaylarla aynı zaman ekseninde
* [ ] "Geçen uçuşa göre nasıldım?" karşılaştırması

### 4.6 War Thunder

* [ ] Oyunun yerel adresindeki gösterge verisinden (`localhost:8111`) okuma
* [ ] Aynı kural motoru ve sesli uyarılar (yakıt, aşırı hız, motor sıcaklığı gibi), araç tipine göre profil
* [ ] Sadece okuma, girdi yok

**Kabul kriterleri**
* Tek oyunculu bir görevde F-16 verisi saniyede ~10 kez akar, uyarılar doğru anda gelir
* Tacview / SRS kuruluyken hepsi birlikte sorunsuz çalışır
* Çok oyunculu sunucuda veri kapalıysa ALICE bunu söyler ve çökmeden devam eder

---

## Aşama 5: Telefon köprüsü ve akıllı saat

**Durum:** GT 2 Pro'nun kendi Bluetooth nabız yayını yok. Gadgetbridge ile bağlantı kuruldu (pil ve model okunuyor). Gadgetbridge'in resmi Huawei sayfası "gerçek zamanlı veri"yi henüz desteklenmeyenler arasında sayıyor. Veri gelip gelmediği test ediliyor.

### 5.1 Test (senin tarafında, sürüyor)

* [ ] Gadgetbridge'de nabız ölçüm ayarını aç
* [ ] 20-30 dk saati tak, biraz yürü, senkronize et
* [ ] Sonuçlar:
  * Adım geliyor mu?
  * Nabız grafiğinde nokta var mı, aralık kaç dakika?
  * "Live activity" seçeneği var mı?

### 5.2 Sonuca göre yol

**A) Aralıklı veri geliyorsa (en olası):**
* [ ] Telefonda periyodik senkronizasyon (Gadgetbridge'in Intent API'si ile tetikleme)
* [ ] Veri ev Wi-Fi'ı üzerinden ALICE'e akar. Ya Gadgetbridge'in otomatik dışa aktarımı, ya da ALICE'in küçük Android yardımcısı (ileride)
* [ ] Gecikme: dakikalar
* [ ] **Yapılabilenler:** gün içi nabız ve stres eğilimi, "bugün nabzım neden yüksekti", uyku, uzun süre yüksek seyreden nabız uyarısı, uçuş değerlendirmesinde nabız grafiği (sonradan senkronize edilmiş veriyle)
* [ ] **Yapılamayan:** uçuş sırasında canlı nabıza anlık tepki

**B) Canlı veri gelirse:** tam plan. Canlı nabız, anlık uyarılar, uçuşta nabza göre sakinleşen ses.

**C) Hiç veri gelmezse:** saat kısmı ertelenir. Canlı nabız istenirse standart Bluetooth yayını yapan bir nabız bandı ALICE'e doğrudan bağlanır. Bu bir öneri, zorunluluk değil.

### 5.3 Nabız kuralları (veri gelirse)

* [ ] Dinlenirken (adım yok, bilgisayar başında) 2 dk boyunca belirlenen eşiğin üstü → yumuşak tonda bildirim
* [ ] Uçuş modunda yüksek nabız → ALICE'in konuşma tonu daha sakin ve yavaş
* [ ] Uyarılar bilgi amaçlıdır, tıbbi cihaz yerine geçmez

### 5.4 Telefondan ALICE (opsiyonel)

* [ ] ALICE uygulaması zaten web tabanlı; ev Wi-Fi'ında telefonun tarayıcısından açılabilir
* [ ] Güvenlik: şu anki tek kullanımlık token yerine eşleştirme kodu ile kalıcı cihaz yetkisi
* [ ] Dışarıdan erişim istenirse: ücretsiz Tailscale (port açmadan)

---

## Aşama 6: Ekler

* [ ] **Kişilik dosyası arayüzü:** persona ayarlarını uygulamadan düzenleme
* [ ] **Sesli notlar:** "Alice, not al: …" → `~/.alice/notes/` altında tarihli not. "Geçen hafta Unity için ne not almıştım?" ile arama
* [ ] **Sabah özeti (sesli, zamanlanmış görev):** Ankara hava durumu, o gün staj mı okul mu, dünden kalan işler, gece çalışan görevlerin sonuçları, nabız/uyku özeti (varsa)
* [ ] **Haftalık bilgisayar raporu:** en çok ısınan zamanlar, disk doluluğu, en çok kullanılan programlar, uyarı geçmişi
* [ ] **Pano hızlı aksiyonları:** kopyalanan hata mesajını hızlı bardan tek tuşla açıklatma
* [ ] **Medya ve sistem kontrolü sesle:** ses seviyesi, müzik, "30 dakika sonra bilgisayarı kapat"

---

## Veri formatları

### Olay

```json
{
  "ts": 1760000000.123,
  "source": "dcs",
  "topic": "dcs.fuel.internal",
  "value": 3120.5,
  "unit": "lbs",
  "meta": {"aircraft": "F-16C_50"}
}
```

### Kural

```toml
[[rule]]
id = "gpu_hot"
when = "hw.gpu.temp > 85"
for = "30s"             # koşul bu kadar süre boyunca sağlanmalı
clear = "hw.gpu.temp < 80"   # gecikme payı: tekrar silahlanma koşulu
cooldown = "10m"
modes = ["normal", "oyun", "gelistirme"]
priority = "uyari"
action = "speak"
tone = "ciddi"
say = "GPU {value:.0f} dereceye çıktı, otuz saniyedir sıcak."
escalate = "agent"      # isteğe bağlı: ajan nedenini araştırır
```

```toml
[[rule]]
id = "f16_bingo_time"
when = "dcs.fuel.minutes_to_bingo < 3"
modes = ["ucus"]
priority = "acil"
action = "speak_clip"   # önceden üretilmiş ses dosyası
clip = "f16/bingo_yakin"
cooldown = "5m"
```

### Zamanlanmış görev

```toml
[[schedule]]
id = "unity_yedek"
cron = "0 23 * * 0"     # her pazar 23:00
type = "agent"
prompt = "D:/Projeler altındaki Unity projelerini D:/Yedek'e tarihli zip olarak yedekle, 5'ten eski yedekleri sil."
missed = "run_on_start"
notify = ["toast", "speak"]
```

### Duygu etiketli sesli cevap

Model sesli cevabı cümle başına ton etiketiyle üretir:

```
[sakin] Projeyi derledim, iki uyarı var ama hata yok.
[şakacı] Bu sefer Unity seni affetti gibi. {gülme}
[ciddi] Yalnız PlayerController'da null kontrolü eksik, oyunda çökebilir.
```

`{gülme}`, `{iç çekme}`, `{duraklama}` gibi efektler motor destekliyorsa sese, desteklemiyorsa sessizliğe çevrilir.

### DCS UDP paketi

```json
{
  "t": 1234.5,
  "ac": "F-16C_50",
  "alt_msl_ft": 18500, "alt_agl_ft": 17900,
  "ias_kt": 420, "mach": 0.82, "vvi_fpm": -500,
  "aoa_deg": 4.1, "g": 1.2,
  "fuel_int_lbs": 4210, "fuel_ext_lbs": 0,
  "gear": 0.0, "flaps": 0.0,
  "chaff": 60, "flare": 30,
  "lat": 41.93, "lon": 41.86
}
```

---

## Riskler ve açık sorular

| Konu | Risk | Plan |
|---|---|---|
| Gemini TTS | Önizleme sürümü; ücretsiz limit yayınlanmıyor, şartlar değişebilir | Yerel XTTS + duygu kütüphanesi her zaman yedek |
| XTTS v2 lisansı | Kaynaklar çelişiyor, ticari kullanım muhtemelen yasak | Kişisel kullanım için sorun değil; ileride ticari olursa Chatterbox'a geçiş |
| Chatterbox Türkçe | Türkçe kalitesi doğrulanmadı | Dinleme testinde karar |
| "Hey Alice" eğitimi | openWakeWord eğitim ortamı eski kütüphane sürümleri istiyor, Türkçe resmi destek yok | "Alice" İngilizce kelime olarak eğitilir; zorlanırsa bas-konuş ile devam |
| GT 2 Pro + Gadgetbridge | Canlı veri desteklenmiyor olabilir, resmi destek listesinde yok | Test sonucuna göre yol A / B / C |
| DCS çok oyunculu | Bazı sunucular export kısıtlayabiliyor, güncel resmi kural bulunamadı | Veri yoksa veri gerektirmeyen özelliklere düş |
| VRAM | DCS 8 GB'ın çoğunu kullanıyor | DCS açıkken bulut, kapalıyken yerel; otomatik geçiş |
| Tam ekran oyun üstü hızlı bar | Bazı oyunlarda pencere üste çıkamayabilir | Oyunda ses öncelikli |
| Gecikme | Ücretsiz bulut servislerinin hızı değişken | Her adım ölçülür, yavaşlayan servis zincirde geriye alınır |

---

## Kaynaklar

* [Groq ücretsiz Whisper limitleri (Spokenly karşılaştırması)](https://spokenly.app/blog/free-speech-to-text-apis)
* [Groq pricing 2026 (eesel.ai)](https://eesel.ai/blog/groq-pricing)
* [Gemini TTS dokümanı](https://ai.google.dev/gemini-api/docs/speech-generation)
* [Gemini-TTS (Google Cloud)](https://docs.cloud.google.com/text-to-speech/docs/gemini-tts)
* [Gemini TTS yönlendirme rehberi](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/text-to-speech/prompting-guide)
* [Gemini TTS ücretsiz katman](https://projedefteri.com/en/blog/is-gemini-3-8-tts-free/)
* [TTS gecikme ölçümleri (RTX 4060 dahil)](https://gigagpu.com/tts-latency-benchmarks/)
* [Yerel TTS karşılaştırması: XTTS v2](https://www.promptquorum.com/power-local-llm/best-local-tts-nvidia-gpu)
* [XTTS-v2 duygu kontrolü tartışması](https://huggingface.co/coqui/XTTS-v2/discussions/19)
* [Chatterbox TTS Server](https://github.com/devnen/Chatterbox-TTS-Server)
* [Chatterbox kurulum rehberi](https://localaimaster.com/blog/chatterbox-tts-setup-guide)
* [Açık kaynak Türkçe TTS rehberi](https://www.ablt.dev/blog/turkish-tts/)
* [Piper Türkçe ses (Cem)](https://huggingface.co/dcx514ai/piper_tts_turkish_high)
* [openWakeWord özel model eğitimi (2026)](https://github.com/briankelley/atlas-voice-training)
* [Home Assistant: uyandırma kelimeleri ve dil desteği](https://home-assistant.io/blog/2023/10/12/year-of-the-voice-chapter-4-wakewords)
* [Gadgetbridge: Huawei / Honor cihazları](https://gadgetbridge.org/basics/topics/huawei-honor/)
* [Gadgetbridge Intent API](https://gadgetbridge.org/internals/automations/intents/)
* [Gadgetbridge veri dışa aktarma](https://codeberg.org/Freeyourgadget/Gadgetbridge/wiki/Data-Export-Import-Merging-Processing)
* [Huawei: nabız yayını destek sayfası](https://consumer.huawei.com/ca/support/content/en-us15827504/)
* [DCS forum: multiplayer export](https://forum.dcs.world/topic/232566-multiplayer-export/)
* [DCS forum: Export.lua ile kendi konumunu alma](https://forum.dcs.world/topic/135111-exporting-self-coordinates-exportlua/)
