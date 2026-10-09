"""ALICE çekirdeği: arka planda sürekli açık duran servis.

  store     -> akan verilerin (metrik/olay) SQLite'ta saklanması ve sorgulanması
  monitors  -> veriyi toplayan gözlemciler (bilgisayar: CPU, GPU, RAM, disk, süreçler)
  modes     -> ortama göre mod (uçuş, geliştirme, sessiz, normal) ve mod politikası
  rules     -> "şu uzun süre şöyle olursa haber ver" kuralları; belirsizse Jev, gerekirse ALICE
  schedule  -> cron tabanlı zamanlanmış görevler, kaçırılanları telafi
  notify    -> bildirim kanalları (masaüstü bildirimi, kayıt, ileride ses)
  service   -> hepsini belirli aralıklarla çalıştıran döngü, sistem tepsisi, otomatik başlatma
"""
