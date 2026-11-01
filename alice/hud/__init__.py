"""Ekran üstü HUD: oyunun, masaüstünün ve tarayıcının üstüne ok, kart, çerçeve çizen ayrı küçük süreç.

  alice hud            HUD'u başlat (çekirdek [hud] enabled = true ise kendisi başlatır)
  alice hud --demo     masaüstünde ok, kenar parlaması, kart ve ilerleme çubuğu çiz (30 sn)

Kodun geri kalanı yalnızca "göster" der (alice.hud.client.show); nasıl çizileceğini HUD bilir. HUD oyuna kod
enjekte etmez, belleğini okumaz: sadece kendi saydam penceresini çizer (Discord ya da OBS katmanı gibi). Pencere
tıklamaları altına geçirir, odak almaz, görev çubuğunda ve ALICE'in ekran görüntülerinde görünmez.

Komutlar 127.0.0.1'e UDP ile JSON gelir: {"kind": "arrow", "clock": 4, "text": "SAM 12 km", "ttl": 3}.
"""

PORT = 41240
