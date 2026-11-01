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

### Sınırlar

* War Thunder'ın verdiği veriler sınırlı. Ekrandan okuma yapılmadıkça "füze geliyor" kesin olmaz.
* Kod değiştiren, para harcayan, mesaj gönderen ya da bir şey silen her adım senin onayınla olur. ALICE önerir, sen karar verirsin.
* Ekran kaydı ve hafıza gibi özellikler sadece yerelde ve senin kontrolünde çalışır.

---

## Şu an seni bekleyenler

* **Google bağlantıları koptu.** Google Takvim ve 4 Gmail hesabının izni dolmuş, sağlık raporu her gün bunu söylüyor. `alice connect google_calendar` ve Ayarlar → Kurulum → Gmail ekle (aynı adla).
* **Yeni War Thunder anonslarını bir maçta dinle:** burnu sana dönük düşman, SAM/uçaksavar menzili, düşürdün/yanıyorsun. Oyuncu adın ilk ölümde öğrenilir; olmazsa `[sims] warthunder_nick` ile ver. Fazla ya da eksik gelenleri söyle.
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
