"""ALICE'in sesi ve kulağı.

  audio     -> PCM yardımcıları, WAV okuma/yazma, çalma (durdurulabilir) ve mikrofon
  tone      -> söylenecek şeye göre duygu seçimi (sakin, neşeli, ciddi, acil, şakacı)
  tts       -> ses motorları: Gemini (bulut), XTTS ve Chatterbox (yerel, duygu kütüphanesinden klonlar)
  stt       -> konuşmayı yazıya çevirme: Groq Whisper (bulut), faster-whisper (yerel)
  speaker   -> cümle cümle sentezleyip sırayla çalan, araya girilince susan konuşmacı
  listen    -> konuşma algılama (VAD), bas-konuş ve "Hey Alice"
  assistant -> dinle -> anla -> ALICE -> konuş döngüsü
  audition  -> ses seçimi için dinleme testi
"""
