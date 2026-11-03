# ALICE: Sıradaki Adım

Bu belge bir yön haritası, sözleşme değil. İlerledikçe ne işe yarıyor, ne yaramıyor görüp değiştireceğiz.

---

## ALICE bugün

İlk plandaki her şey yapıldı ve her gün kullanılıyor. Ayrıntılar README'de.

* **Çekirdek:** Windows açılınca sistem tepsisinde başlar. Bilgisayarı, oyunları ve bağlı servisleri izler, kurallarla karar verir, ortama göre mod değiştirir (uçuşta sadece uçuşla ilgili konuşur, yerel ekran kartını oyuna bırakır). Kendi sağlık raporunu ve haftalık özetini çıkarır.
* **Ses:** "Hey Alice" ile uyanır, sesini tanır (başkasına cevap vermez), Gemini Live ile canlı konuşur, araya girince susar. Bulut ve yerel ses motorları, tona göre konuşma.
* **War Thunder:** yakıt, G, yere yaklaşma, düşman yönü ve mesafesi, burnu sana dönük düşman, SAM menzili, düşürme ve vurulma anonsları, uçuş raporu. Yüzlerce uçuşta kullanıldı.
* **DCS:** aynı uçuş arkadaşı, ayrıca RWR, kontrol listeleri, kneeboard, yaklaşma geri bildirimi. Az denendi.
* **Bilgisayar:** sıcaklık, bellek, disk, program izleme. Oyunda sadece kritik seviyede uyarır. Ekranda tıklama/yazma, tarayıcı kullanma.
* **Günlük:** sabah özeti, sesli notlar, hatırlatmalar, e-posta ve takvim takibi, servis akışları, telefondan erişim.

---

## Bir üst seviye: hayaldeki asistan

Buraya kadarki her şey "iyi bir asistan". Bundan sonrası ALICE'i sıradan bir ajandan ayıracak olan kısım. Fark daha zeki bir modelde değil: ALICE'in seni sürekli tanıması, kendini geliştirmesi ve dünyada gerçekten iş bitirmesinde.

### Yakın hedefler (elimizdeki parçalarla yapılabilir)

* **Tek bir "şu an" durumu.** Mod, açık program, oyun ve maç durumu, nabız, takvim, son konuşmalar ve ruh hali tek bir canlı durumda toplanır. Her cevap ve her karar ona bakar. "Nasılım?" sorusuna "3 maçtır arkadan vuruluyorsun, nabzın 130, mola ver" diyebilir.
* **Kendi kendini iyileştirme (gece döngüsü).** ALICE her gece kendi loglarını okur ve tekrarlayan hataları, gereksiz anonsları, yanlış uyanmaları bulur. Düzeltmeyi kendisi yazar, testleri koşar ve sabah "şunları düzelttim, onaylıyor musun?" diye sorar. Senin onayın olmadan hiçbir şey birleşmez.
* **Kanat adamı koçluğu.** Uçuş kayıtlarından ölüm desenleri çıkarılır: "Son 10 ölümünün 7'si alçakta dönerken arkadan geldi." Maç içinde de hatırlatır: "Bu haritada SAM seni geçen sefer şurada vurdu." Anons değil koçluk.
* **Sözünü tutan bilgisayar kullanımı.** Uygulama başına öğrenilmiş tarifler tutulur. Başarısız denemeler tarif olarak kaydedilir, tıklama/yazma işleri adım sınırına takılmadan biter.

### Ufuk (futuristik ama yolu görünen)

* **Ekrandan füze görmek.** War Thunder füze ve RWR bilgisini vermiyor ama ekranda gösteriyor. ALICE oyun görüntüsünün RWR/füze uyarısı bölgesini işlemcide küçük bir görüntü tanımayla izler ve gerçek "Füze! Saat 4" der. Uçuşta ekran kartına dokunulmaz.
* **Ekran üstü HUD, neredeyse her yerde.** Ekranın üstünde, tıklamaları geçiren saydam bir katman. ALICE sadece konuşmaz, gösterir. Tek bir HUD motoru olur (ok, çerçeve, etiket, küçük kart, ilerleme çubuğu), her yer onu kullanır:
  * **Oyunda:** tehdidin yönüne ok ve mesafe, SAM menzil uyarısı, yakıt dakikası, "arkanda" kenar parlaması, düşürdüğünde küçük bir onay. Uçuşta sade kalır ve göz yormaz.
  * **Masaüstünde:** ALICE bir şey anlatırken düğmenin etrafına çerçeve çizer ("şuna tıkla"). Ekranda iş yaparken nereye tıklayacağını önceden gösterir. Ne yaptığını görürsün, istersen durdurursun.
  * **Tarayıcıda:** sayfadaki önemli yeri işaretler, uzun sayfanın özetini kenarda kart olarak gösterir.
  * **Konuşurken:** söylediğinin kısa yazılı hali, sayı ya da listesi köşede belirip kaybolur. Sesli cevap uzun listeyi okumaz, gösterir.
  * **Bildirimlerde:** Windows balonu yerine ALICE'in kendi zarif kartı. Önemine göre renk alır, oyundayken oyunu bölmeyecek bir köşede durur.
  * **Sistemde:** kritik sıcaklık ya da RAM gibi durumlar küçük bir gösterge olarak görünür, konuşma gerekmez.

  Kurallar: tıklamaları ve klavyeyi asla yakalamaz, odak çalmaz (oyun arkaya düşmez), tam ekran oyunda çalışmazsa kenarlıksız pencere modunda çalışır, tek tuşla tamamen gizlenir.
* **Önsezi motoru.** ALICE alışkanlıklarından ne yapacağını önceden tahmin eder ve hazırlar:
  * akşam 9'da War Thunder açacaksın: güncellemesi önceden inmiş olur
  * maç öncesi Discord'u sessize alır
  * sabah ilk soracağın şeyin cevabı hazır bekler
* **Rüya modu.** Gece günü baştan oynatır. Hafızayı toparlar, yarının işlerini prova eder ve senin göremediğin bağlantıları çıkarır: "Geç yattığın gecelerin ertesinde maçlarda iki kat fazla ölüyorsun."
* **Kendi aracını yazan ALICE.** Bir işi yapacak aracı yoksa "bunu yapamıyorum" demek yerine aracı kendisi yazar. Akış:
  1. Neyin eksik olduğunu söyler ("Spotify'da çalma listesine ekleyecek aracım yok, yazayım mı?").
  2. Aracı ve testini yazar, kum havuzunda (ağ ve dosya erişimi kısıtlı) dener.
  3. Ne yaptığını, neye erişeceğini bir cümleyle anlatıp onayını ister.
  4. Onaylarsan araç kutusuna eklenir ve hemen kullanılır. Bir sonraki sefere hazırdır.

  Kullanılmayan ya da bozulan araçları gece döngüsü fark eder, düzeltir ya da kaldırmayı önerir. Yetenek listesi zamanla senin ihtiyaçlarına göre kendiliğinden büyür.
* **Her yerde aynı ALICE.** Bilgisayar, telefon, saat ve ev aynı sohbetin parçası olur. Odadan çıkınca konuşma telefondan, saatten devam eder. Eve gelince ev hazırdır.
* **Duygusal zekâ.** Nabız, ses tonu ve oyun olayları birlikte okunur. Sinirlendiğini (tilt) fark eder, sesini ve konuşma sıklığını buna göre ayarlar. Gerekirse "bir maç ara ver" der.
* **Zaman makinesi hafıza.** Ekranda gördüğün her şey yerelde, şifreli ve aranabilir bir zaman çizgisine yazılır: "Geçen hafta Discord'da paylaşılan o linki bul." Sadece senin bilgisayarında kalır, istediğin an silinir.
* **DCS'te sesle yönetilen yapay zekâ kanat adamları.** Tek oyunculu görevlerde ALICE görev betiği üzerinden yapay zekâ uçaklarına doğal dille emir verir: "İkinci, sağdaki SAM'i bastır." Araştırma gerektirir (DCS betik ortamı).

### Sıralama

1. **Ekran üstü HUD motoru:** önce oyunda (tehdit oku, uyarılar), sonra masaüstü, konuşma ve bildirimler. Diğer fikirlerin çoğu sonucunu bu katmanda gösterecek.
2. **Ekrandan füze görme:** HUD'daki tehdit oku gerçek füze uyarısıyla dolsun.
3. **Kendi aracını yazan ALICE**
4. **Gece döngüsü (kendi kendini iyileştirme) ve rüya modu:** aynı gece işinin iki yüzü.
5. **Tek "şu an" durumu ve önsezi motoru**
6. **Kanat adamı koçluğu, duygusal zekâ**
7. **Her yerde aynı ALICE, zaman makinesi hafıza, DCS kanat adamları**

Aşağıda ilk üç madde için nasıl yapılacağı yazılı. Diğerlerinin ayrıntısı sırası gelince eklenir.

### Durum (3 Kasım 2026)

#157-#170 main'de. 2-3 Kasım işleri (HUD, telefon, önsezi, tarayıcı eklentisi, gözlem ve kanıt kontrolü, füze görme,
gece döngüsü) PR #172'de, birleşmeyi bekliyor.

| Madde | Durum | Senden beklenen |
|---|---|---|
| HUD motoru (Ada) | Apple'ın Dynamic Island kurallarıyla tek "Ada" (açık buzlu cam, marka mavisi, sürekli köşe, yay animasyonu); kenar ışığı. ALICE'in imleci hedefe kayar, tıklamada halka yayılır; dinlerken dalga ve mavi parıltı; oyunda yalnız tehdit ve uyarı (`[hud] mode`, Ctrl+Alt+K); Ctrl+Alt+J son bildirimler; yönsüz tehdide radar simgesi; çalan şarkı. War Thunder'da kenarlıksız pencerede denendi, FPS'e etkisi yok | — |
| Ekrandan füze görme (#159) | Medal'deki 344 War Thunder klibinden: RWR turkuaz ve uyarı yazıyla (TRACK kilit, LAUNCH füze, dairedeki MSL yön). Turkuaz maske ve kelime düzeyinde OCR; 40 klipte 28'inde füze yakalandı, örneklenen karelerin hepsi gerçek LAUNCH, kare ~8-15 ms. Bölge ekran yüksekliğine göre kendiliğinden, varsayılan açık | Bir maçta "Füze!" ve "Kilitlendin!" anonslarını dinle; geç ya da yanlış gelirse söyle |
| Kendi aracını yazan ALICE (#160) | Altyapıdaki dört hata düzeldi (kum havuzu çıktısı kayboluyordu, `from __future__` sırası, çift ters eğik çizgili klasör, iki adla görünen kullanıcı klasöründe tmp_path). Steam aracı testi ilk denemede geçiyor; ama son oynanmayı değil son güncellenmeyi sıralıyor | Steam aracını "son oynanma" (`localconfig.vdf`) ile düzeltip kurayım mı |
| Gece döngüsü ve rüya modu (#161) | Gece raporu, sabah sorusu, bağlantılar. Artık tek seferlik iş parçacığı ölümleri ve sohbetlerdeki takılmalar da (aynı adımı tekrarlama, ekran işinde aynı tuşa basıp durma, kesilen iş, "yine saçmaladı") isteğiyle birlikte rapora girer; kendiliğinden kod değiştirmez | Sabah sorusuna evet/hayır |
| Şu an durumu ve önsezi (#162, tamamlandı) | Oyundan önce ses hazırlığı ve Steam güncellemesi, maçta Windows "Rahatsız etmeyin", sabah ilk sorulanın hazır cevabı. "Ne çalıyor": Windows medya oturumu (Spotify, Sonora, tarayıcı), durdur/geç tam çalan uygulamaya | Steam'de War Thunder'ı "her zaman güncel tut"a al |
| Koçluk ve duygusal zekâ (#163) | Ölüm desenleri, maç içi öğüt, tilt'te daha az konuşma, mola önerisi | — |
| Zaman makinesi hafıza | Şifreli (DPAPI) yerel zaman çizgisi, arama, silme; senin ayarında açık | — |
| Her yerde aynı ALICE | Telefon uygulaması (`/m`) denendi: sohbet, bilgisayar durumu ve kontrolleri, medya kartında çalan şarkı, canlı ekran, akış | Telefonda `alice phone` adresini aç, ana ekrana ekle. Saat şimdilik beklemede |
| Sözünü tutan bilgisayar kullanımı | Ekran işleri önce reflex'e (~1 sn/adım), ekran görevi yedek; ekran görevi JPEG ve "ekran durulunca devam" ile hızlı. Ekranı etkileyen her eylemden sonra yeni pencere/hata kutusu/UAC sonuca eklenir; "yaptım/indirildi/yok" diyen cevap araç sonuçlarıyla desteklenmiyorsa bir kez doğrulamaya gönderilir. Olmayan dosya açılmaz, indirmeler Farklı Kaydet'e girmeden doğrudan İndirilenler'e | Sonora gibi bir indirme-kurulum işini baştan sona dene |
| Tarayıcı eklentisi | Opera'ya ALICE eklentisi: uzaktan hata ayıklama ve izin penceresi yok, indirme/tema ellenmez; sekme, okuma, yazma, tıklama, ekran görüntüsü, indirme listesi. Opera'da kuruldu ve okuma denendi | Opera'da gerçek bir tıklama/yazma işini dene |
| Tek Python ortamı | ALICE'in Python'u kaydedilir, başka Python'dan başlayan çekirdek kayıtlıyla yeniden başlar; açılışta eksik paketleri kendisi kurar | — |
| DCS kanat adamları (#170) | Sesle AI gruplarına saldır, yanıma gel, bekle, eve dön. `MissionScripting.lua` değişmedi; sabit komut kabul eden bir DCS hook dosyası kullanılıyor, çekirdek başlarken kurulur. DCS'te denenmedi | Tek oyunculu bir görevde ilk komutu dene; çalışmazsa `dcs.log`'daki `ALICE` satırları |

### 1. Ekran üstü HUD motoru

**Nasıl yapılır.**
* **Ayrı süreç:** HUD ayrı ve küçük bir süreç olur (`alice hud`). Çekirdek onu başlatır, çökerse yeniden başlatır. HUD çökse de ALICE konuşmaya devam eder.
* **Pencere:** tkinter ile çizilen, saydam, çerçevesiz, her zaman en üstte bir pencere. Win32 ayarlarıyla tıklamaları altındaki pencereye geçirir (`WS_EX_TRANSPARENT`, `WS_EX_LAYERED`), odak almaz ve görev çubuğunda görünmez (`WS_EX_NOACTIVATE`, `WS_EX_TOOLWINDOW`). Yeni paket gerekmez.
* **Ekran görüntüsü dışı:** `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` ile ekran görüntülerinden hariç tutulur. Yoksa ALICE ekrana baktığında kendi çizdiği oku görüp onu tehdit sanar.
* **Haberleşme:** çekirdek ve sesli mod HUD'a yerel bir soket üzerinden kısa komutlar yollar: `{"kind": "arrow", "clock": 4, "text": "SAM 12 km", "ttl": 3}`. Kodun geri kalanı yalnızca "göster" der; nasıl çizileceğini HUD bilir.
* **Öğeler:** ok (saat yönü), kenar parlaması, etiket, kart, çerçeve (ekran koordinatıyla), ilerleme çubuğu. Her öğenin bir ömrü (ttl) vardır, süresi dolunca kaybolur. Aynı anda en fazla birkaç öğe görünür.
* **Ayar ve kısayol:** tek kısayolla tamamen gizlenir. `[hud]` ayarlarıyla açılıp kapanır, hangi ekranda ve ne kadar saydam çizileceği seçilir.

**İlk adım.** Masaüstünde sabit bir ok ve kart çizen deneme. Ardından War Thunder kenarlıksız penceredeyken aynı deneme yapılır ve odak ile FPS ölçülür.

**Bitti sayılır.**
* War Thunder'da, kenarlıksız pencerede, tehdit oku veri geldikten sonra en geç 0,3 saniyede görünür.
* Oyunun FPS'i yüzde 2'den fazla düşmez, odak bir saat boyunca hiç kaymaz.
* ALICE'in kendi ekran görüntülerinde HUD görünmez.
* HUD çökünce çekirdek 5 saniye içinde yeniden başlatır.

**Riskler.**
* Özel tam ekranda (exclusive fullscreen) hiçbir pencere oyunun üstüne çizilemez. Çözüm oyunu kenarlıksız pencerede açmak. ALICE bunu fark edip bir kez söyler. Olmazsa oyunda yalnızca sesli kalınır.
* Hile koruması: ALICE oyuna kod enjekte etmez ve belleğini okumaz. Sadece kendi penceresini çizer, Discord ya da OBS katmanı gibi. Bu sınır kesindir.
* Birden fazla monitör ve DPI ölçeği: koordinatlar Windows'un DPI farkındalığıyla hesaplanmalı (bilgisayar kullanımı kodunda bu zaten çözülmüştü).

### 2. Ekrandan füze görme

**Nasıl yapılır.**
* **Yakalama:** sadece uçuş modunda, ekranın yalnızca ilgili küçük bölgesi (RWR göstergesi ve füze uyarısı) işlemcide saniyede 10 kez yakalanır (`mss`). Tüm ekran yakalanmaz.
* **Tanıma:** önce kurallarla yapılır: renk eşiği ve şablon eşleme (`numpy`, gerekirse OpenCV; ikisi de kurulu). RWR dairesi üzerindeki işaretin açısından saat yönü çıkar. Yanıp sönen uyarı birkaç karede doğrulanır, tek karelik yanlış alarm olmaz.
* **Sonuç:** mevcut temas listesine `missile: True` olarak eklenir. Böylece bugünkü "Füze! Saat 4, chaff at" anonsu ve HUD'daki ok hiçbir değişiklik gerekmeden çalışır.
* **Ayar:** bölge koordinatları ve renkler uçağa ya da HUD düzenine göre değişebilir. Bir kerelik bir "kalibrasyon" komutu olur: ALICE bölgeyi ekran görüntüsünde gösterir, sen onaylarsın.

**İlk adım (yapıldı, 3 Kasım).** Medal kliplerinden ölçüldü: War Thunder'ın RWR'ı kırmızı değil turkuaz ve uyarıyı yazıyla veriyor (TRACK, LAUNCH, dairede MSL). Renk eşiği yerine turkuaz maske ve kelime düzeyinde Windows OCR kullanılıyor; LAUNCH saniyede ~2 kez yanıp söndüğü için yakalama 6 kare/sn.

**Bitti sayılır.**
* Kaydedilmiş oyun görüntülerinde füze uyarılarının en az yüzde 90'ı yakalanır.
* Bir saatlik normal uçuşta en fazla bir yanlış "Füze!" olur.
* Uyarı ekranda belirdikten sonra en geç 0,3 saniyede söylenir.
* Bir işlemci çekirdeğinin yüzde 3'ünden azını kullanır, ekran kartına hiç dokunmaz.

**Riskler.**
* Özel tam ekranda yakalama siyah görüntü dönebilir. Çözüm HUD'daki gibi kenarlıksız pencere.
* Oyun güncellemesi HUD görünümünü değiştirirse tanıma bozulur. Gece döngüsü "bu maçta hiç füze görmedim ama ölüm kaydında füze var" gibi tutarsızlıkları yakalayıp kalibrasyonu yenilemeyi önerir.
* Kokpit görünümü ile dış görünüm farklı göstergeler kullanır. İlk sürüm tek bir görünümle başlar.

### 3. Kendi aracını yazan ALICE

**Nasıl yapılır.**
* **Araç:** ALICE'in zaten bir eklenti sistemi var (`~/.alice/plugins/`, bkz. `docs/PLUGINS.md`). Kendi yazdığı araç bir eklenti olarak oraya düşer, çekirdek koda dokunulmaz. Yeni araç `make_tool` olur: ihtiyacı alır, eklentiyi ve testini yazar.
* **Deneme:** yazılan test mevcut kum havuzunda (Windows'ta düşük bütünlük seviyesi) çalışır: sadece geçici klasöre yazabilir, süre sınırı vardır.
* **Bildirge:** her eklentinin başında neye erişeceği yazar (internet adresleri, klasörler, hangi uygulama). ALICE bunu ve kodun kısa özetini gösterip onay ister. Bildirgede olmayan bir şeye erişen kod reddedilir.
* **Onaydan sonra:** eklenti yüklenir. İlk kullanımlarda sonucu kontrol edilir. Üç kez hata veren araç kendiliğinden kapanır ve gece döngüsüne düşer.
* **Kayıt:** bütün yazılan araçlar bir listede durur: ne zaman yazıldı, kaç kez kullanıldı, son hata ne. "Hangi araçları kendin yazdın?" diye sorulabilir.

**İlk adım.** Küçük ve zararsız bir hedefle uçtan uca deneme. Örneğin "Steam'deki son oynanan oyunları listeleyen araç": ihtiyaç, yazma, test, onay, yükleme ve kullanma.

**Bitti sayılır.**
* ALICE yapamadığı bir işi fark edip araç önerir, yazar, testini kum havuzunda geçirir.
* Onaydan sonra aynı konuşmada aracı kullanıp işi bitirir.
* Onay verilmezse hiçbir dosya eklenti klasörüne girmez.
* Bozuk bir araç ALICE'i çökertmez.

**Riskler.**
* En hassas madde bu: onaylanan eklenti ALICE'in içinde ALICE'in yetkileriyle çalışır. Bu yüzden onay ekranı kısa ve anlaşılır olmalı. Kod değiştiren, para harcayan, mesaj gönderen ya da bir şey silen araçlar her kullanımda ayrıca onay ister.
* Windows kum havuzunda ağ açık. Test sırasında dışarı veri gönderilmemesi için testler mümkün olduğunca sahte servislerle yazılır. Ağ isteyen test ayrıca belirtilir.
* Model yanlış ya da gereksiz araç yazabilir. Var olan bir araç ya da bağlantı (connect) işi görüyorsa yenisi yazılmaz.

### Sınırlar

* War Thunder'ın verdiği veriler sınırlı. Ekrandan okuma yapılmadıkça "füze geliyor" kesin olmaz.
* Kod değiştiren, para harcayan, mesaj gönderen ya da bir şey silen her adım senin onayınla olur. ALICE önerir, sen karar verirsin.
* Ekran kaydı ve hafıza gibi özellikler sadece yerelde ve senin kontrolünde çalışır.

---

## Şu an seni bekleyenler

* **PR #172'yi birleştir.** 2-3 Kasım işlerinin hepsi orada.
* **Google bağlantıları koptu.** Google Takvim ve 4 Gmail hesabının izni dolmuş, sağlık raporu her gün bunu söylüyor. `alice connect google_calendar` ve Ayarlar → Kurulum → Gmail ekle (aynı adla).
* **Ekrandan füze görmeyi bir maçta dinle:** "Füze! Saat 4", "Füze atıldı!", "Kilitlendin!". Geç, eksik ya da yanlış gelirse söyle.
* **Steam aracı:** ALICE'in yazdığı araç testi geçiyor ama son oynanmayı değil son güncellenmeyi sıralıyor; düzeltip kurmak için onay.
* **Model listesinde çalışmayanlar:** Vercel AI Gateway kredi kartı istiyor, Ollama kapalı. Kullanmıyorsan listeden çıkaralım, boşuna denenmesinler.
* **Örnek Discord akışı:** `~/.alice/feeds.toml` içindeki `ornek` akışı hiç kurulmadı. Bir kanal izlemek istiyorsan dolduralım, istemiyorsan silelim.
* **Akıllı saat:** Gadgetbridge dışa aktarması hâlâ bağlı değil (`[watch] export_path` boş). Nabız uçuş raporlarına ve duygusal zekâ fikrine bundan gelecek.

---

<details>
<summary>Araştırmada kullanılan kaynaklar</summary>

* [Gemini TTS](https://ai.google.dev/gemini-api/docs/speech-generation) ve [stil yönlendirme rehberi](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/text-to-speech/prompting-guide)
* [Yerel TTS gecikme ölçümleri](https://gigagpu.com/tts-latency-benchmarks/), [XTTS v2 duygu kontrolü](https://huggingface.co/coqui/XTTS-v2/discussions/19), [Chatterbox](https://github.com/devnen/Chatterbox-TTS-Server)
* [Açık kaynak Türkçe TTS rehberi](https://www.ablt.dev/blog/turkish-tts/)
* [Groq Whisper ücretsiz limitleri](https://spokenly.app/blog/free-speech-to-text-apis)
* [openWakeWord özel kelime eğitimi](https://github.com/briankelley/atlas-voice-training)
* [Gadgetbridge Huawei desteği](https://gadgetbridge.org/basics/topics/huawei-honor/) ve [Intent API](https://gadgetbridge.org/internals/automations/intents/)
* [DCS forum: multiplayer export](https://forum.dcs.world/topic/232566-multiplayer-export/)

</details>
