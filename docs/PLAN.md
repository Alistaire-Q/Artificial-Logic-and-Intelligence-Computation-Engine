# ALICE: Sıradaki Adım

Şu anki ALICE, sen bir şey yazınca çalışan bir ajan. Bir sonraki adımda arka planda hep açık duran, seni dinleyen, kendi sesiyle konuşan ve bilgisayarda, oyunda ya da sağlığında olup bitenlere göre kendiliğinden devreye giren bir asistana dönüşecek. Hedef JARVIS hissi.

Bu belge bir yön haritası, sözleşme değil. İlerledikçe ne işe yarıyor, ne yaramıyor görüp değiştireceğiz.

---

## Nasıl çalışacak

ALICE'in altına, Windows açılınca sessizce başlayan ve sistem tepsisinde duran küçük bir çekirdek gelecek. Bu çekirdek üç şey yapıyor:

**Dinliyor.** Bilgisayarın sıcaklığı, DCS'teki uçağın yakıtı, saatten gelen nabız, açık olan program... Hepsi sürekli akan veriler olarak gelir ve bir süre saklanır. Böylece "son bir saatte neden kasıldı?" gibi sorular cevaplanabilir.

**Karar veriyor.** Her veri için yapay zekâya gitmek hem yavaş hem kotayı bitirir. O yüzden önce basit kurallar bakar: "GPU uzun süre çok ısındıysa söyle", "yakıt azaldıysa haber ver". Durum belirsizse hızlı karar modeli (Jev) devreye girer. Gerçekten araştırma gerekiyorsa ALICE'in kendisi uyanır. Kuralları sen yazmazsın; "nabzım uzun süre yüksek kalırsa beni uyar" demen yeter.

**Ortama uyum sağlıyor.** DCS açıkken uçuş moduna geçer: sadece uçuşla ilgili konuşur, gerisini susturur. Unity açıkken geliştirme moduna, gece sessiz moda geçer. Ekran kartı da buna göre paylaşılır: DCS açıkken ALICE GPU'ya dokunmaz, ağır işleri buluta bırakır.

---

## ALICE'in sesi

En çok önem verdiğin kısım bu.

**Ses.** Sakin, sıcak bir kadın sesi. Gerektiğinde şakacı, ama her şeyi aynı tonda okuyan bir robot değil. Söylediğine göre tonu değişecek: normalde sakin, iyi bir haberde neşeli, bir hatada ciddi, uçuşta bir uyarıda net ve acil. Acil anlarda espri yapmayacak.

**Sesi seçmek.** Bunu teknik tablolarla değil kulakla seçeceğiz. Önce birkaç adayla (Google'ın Gemini sesleri, bilgisayarında çalışabilen XTTS ve Chatterbox) aynı Türkçe cümleleri farklı duygularla seslendireceğim, sen dinleyip seçeceksin. Microsoft sesleri listede yok.

**Her yerde aynı ses.** Seçtiğin sesi hem bulutta hem bilgisayarında aynı şekilde kullanabilmek için o sesin farklı duygulardaki örneklerini bir "duygu kütüphanesi" olarak saklayacağız. Böylece DCS açıkken buluttan, kapalıyken bilgisayarından konuşsa da hep aynı ALICE olacak.

**Konuşmak.** İki yolu da olacak:
* **Bas-konuş tuşu:** her zaman güvenilir.
* **"Hey Alice":** el gerektirmez.

ALICE konuşurken araya girersen susup seni dinleyecek. Cevaba hızlı başlamak için cümlenin tamamını beklemeden ilk cümleyi söylemeye başlayacak. Uzun sürecek bir işte önce "hemen bakıyorum" deyip sonra sonucu söyleyecek. Hedef, sen sustuktan sonra birkaç saniye içinde cevabın başlaması.

**Kişilik.** Sana nasıl hitap edeceği, ne kadar uzun konuşacağı, ne sıklıkla şaka yapacağı ayrı bir dosyada duracak, istediğin zaman değiştirebileceksin.

---

## Hızlı bar

Hangi programdayken olursan ol (editör, tarayıcı, oyun) bir kısayolla ekranın ortasında küçük bir kutu açılacak. Yazabilir ya da konuşabilirsin, cevap küçük bir pencerede gelir. Bir de "ekrandan alan seç ve sor" kısayolu olacak: bir hata mesajını ya da Unity console'u seçip "bu ne?" diyebileceksin.

---

## Bilgisayar

* **İzleme:** ALICE işlemci ve ekran kartı sıcaklığını, belleği, diski ve hangi programın ne kadar kaynak kullandığını sürekli izleyecek.
* **Uyarılar:** bir şey uzun süre fazla ısınırsa, disk dolmak üzereyse ya da bir program belleği şişiriyorsa söyleyecek.
* **Sorular:** "dün DCS oynarken GPU kaç dereceydi?" ya da "neden kasıldı?" diye sorabileceksin.

---

## Zamanlanmış görevler

"Her pazar gece Unity projelerimi yedekle", "her sabah bana günün özetini söyle" gibi işleri doğal dille tanımlayabileceksin. ALICE bunu bir zamanlamaya çevirip sana gösterecek, onaylarsan kuracak. Bilgisayar kapalıyken kaçırılan görevleri açılışta telafi edebilecek. Sonuçlar sesli ya da bildirim olarak gelecek.

---

## DCS ve War Thunder

ALICE oyuna hiçbir şey göndermeyecek, sadece okuyup konuşacak.

F-16'nın kendi sesli uyarıları zaten var, ALICE onları tekrarlamayacak. Onun yerine bir uçuş arkadaşı gibi davranacak:

* **Yakıt:** sadece "bingo" demekle kalmaz; mevcut tüketimle ne kadar süren kaldığını, eve dönmek için ne zaman ayrılman gerektiğini söyler.
* **İniş yaklaşması:** yaklaşmada hızın ve açın doğru mu, kısa cümlelerle geri bildirim verir.
* **Diğer hatırlatmalar:** uzun süre yüksek G'de kalırsan ya da chaff/flare azalırsa hatırlatır.
* **Kontrol listeleri:** "Alice, iniş kontrol listesi" dersin, adım adım okur.
* **Kneeboard:** görevin frekanslarını, waypoint'lerini ve notlarını oyun içi kneeboard'a sayfa olarak koyar. Sesle not da ekleyebilirsin.
* **Uçuş sonrası:** inince kısa bir değerlendirme yapar; en yüksek G, yakıt yönetimi, yaklaşma nasıldı. Saat verisi gelirse nabzın da aynı grafikte görünür.

Hem tek hem çok oyunculuda çalışacak. Bazı sunucular veri paylaşımını kısıtlayabiliyor; öyle bir sunucuda ALICE bunu söyleyip veri gerektirmeyen özelliklerle (kontrol listeleri, kneeboard, sesli komutlar) devam edecek.

War Thunder için de aynı mantık: oyunun kendi yayınladığı göstergeleri okuyup uyarı verecek, hepsi bu.

---

## Akıllı saat

Huawei Watch GT 2 Pro'nun kendi nabız yayını yok, o yüzden telefondaki Gadgetbridge üzerinden gidiyoruz. Bağlantı kuruldu, şimdi verinin gelip gelmediğini ve ne sıklıkla geldiğini görmemiz lazım. Sonuca göre:

* **Birkaç dakikada bir veri geliyorsa:** gün içi nabız, stres ve uyku takibi, "bugün nabzım neden yüksekti" soruları ve uzun süre yüksek seyreden nabız uyarısı mümkün. Uçuş değerlendirmesinde nabız grafiği de olur.
* **Canlı veri geliyorsa:** uçuş sırasında nabzın yükseldiğinde ALICE'in sesinin sakinleşmesi gibi anlık tepkiler de eklenir.
* **Veri gelmiyorsa:** saat kısmını erteleriz.

Telefon zaten köprü olacağı için istersen ALICE'e telefonun tarayıcısından da ulaşabileceksin.

---

## Küçük ama hoş eklemeler

* **Sesli notlar:** "Alice, not al…" ve sonradan "geçen hafta ne not almıştım?"
* **Sabah özeti:** hava durumu, o gün staj mı okul mu, dünden kalanlar, gece çalışan görevlerin sonuçları
* **Haftalık bilgisayar raporu**
* **Pano:** kopyaladığın bir hatayı tek tuşla açıklatmak
* **Sesle müzik, ses seviyesi ve kapatma zamanlayıcısı**

---

## Sıralama

1. **Çekirdek:** arka planda çalışan servis, veri akışı, kurallar, bildirimler, modlar. Gözle görülen kısmı az ama her şey bunun üstüne kurulacak.
2. **Ses ve hızlı bar:** önce ses seçimi için dinleme testi, sonra ALICE'in konuşması ve dinlemesi.
3. **Bilgisayar izleme ve zamanlanmış görevler:** çekirdeği gerçek veriyle denemenin en kolay yolu.
4. **DCS ve War Thunder**
5. **Akıllı saat ve telefon:** Gadgetbridge testinin sonucuna göre.
6. **Eklemeler**

Sıra ihtiyaca göre değişebilir. Bir şey beklediğimiz gibi çıkmazsa ya da daha iyi bir fikir gelirse planı güncelleriz.

---

## Şu an seni bekleyenler

* Gadgetbridge testinin sonucu: nabız ve adım geliyor mu, ne sıklıkla?
* ALICE'i birkaç gün gerçek işlerde kullanıp takıldığın yerleri not etmen. Sıradaki adımları gerçek ihtiyaca göre şekillendirmenin en iyi yolu bu.

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
