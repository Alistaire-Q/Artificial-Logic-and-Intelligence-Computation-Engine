"""Akıllı saat verisi (Huawei Watch GT 2 Pro, telefondaki Gadgetbridge üzerinden).

İki yol var, ikisi de aynı anahtarlara yazar (watch.hr, watch.steps, watch.stress, watch.spo2):
  1. Gadgetbridge'in otomatik dışa aktardığı veritabanı (Syncthing vb. ile bilgisayara gelir):
     çekirdek dosya değiştikçe yeni örnekleri içe alır. Birkaç dakikada bir / saatte bir veri.
  2. Canlı: telefondaki bir otomasyon (Tasker, Automate, HTTP Shortcuts) nabzı `alice phone`
     sunucusuna POST eder. Uçuş sırasında anlık tepki için gerekir.
`alice watch status` verinin ne sıklıkla geldiğini ölçer: planın "Gadgetbridge testi" budur.
"""
