# ALICE eklentileri

Eklenti, ALICE'e çekirdek koda dokunmadan yeni araç (yetenek) ekler. Bir Python dosyası yeterli.

## Hızlı başlangıç

```
alice plugins new spotify     # ~/.alice/plugins/spotify.py şablonunu oluşturur
alice plugins                 # yüklü eklentileri ve hataları listeler
```

Dosyayı düzenle, ALICE'i (çekirdek, uygulama ya da ses) yeniden başlat.

## Eklenti dosyası

```python
from alice.plugins import Plugin
from alice.tools.base import NETWORK, Tool, ToolContext, ToolError, ToolResult, obj


class SpotifyPlay(Tool):
    name = "spotify_play"                       # tüm araçlar arasında benzersiz
    description = ("Spotify masaüstü uygulamasında şarkı, albüm ya da çalma listesi arayıp çalar. "
                   "Durdur/devam ve ses için media aracını kullan.")  # model bunu okuyup ne zaman çağıracağına karar verir
    parameters = obj({"query": {"type": "string", "description": "Şarkı ya da sanatçı, ör. 'Coldplay Yellow'."}},
                     ["query"])
    risk = NETWORK                              # izin sistemi: READ, WRITE, EXEC, COMPUTER, NETWORK, DANGER

    def run(self, ctx: ToolContext, query: str) -> ToolResult:
        if not query:
            raise ToolError("şarkı adı boş")    # modele anlamlı hata döner
        ...
        return ToolResult(f"çalıyor: {query}")


PLUGIN = Plugin(
    name="spotify",                             # aynı zamanda grup adı: load_tools(["spotify"])
    tools=[SpotifyPlay()],
    description="Spotify çalma, durdurma",      # load_tools listesinde görünür
    keywords=r"spotify|şarkı çal",              # mesaj buna uyarsa grup kendiliğinden açılır
    live=True,                                  # sesli (Gemini Live) konuşmada baştan açık
    cleanup=None,                               # oturum kapanırken çağrılır: cleanup(ctx)
)
```

`Plugin` alanları:

| alan | anlamı |
|---|---|
| `name` | Küçük harf, rakam, `-`, `_`; harfle başlar, en çok 40 karakter. Grup adıdır. |
| `tools` | `Tool` nesneleri. |
| `description`, `keywords` | Araçlar varsayılan olarak bir grupta durur ve modele sadece gerekince gider; bu, isteği küçük ve modeli odaklı tutar. |
| `always` | `True`: araçlar her istekte gider (grup yok). Az ve sık kullanılan araçlar için. |
| `live` | Sesli konuşmada grubu baştan aç. |
| `cleanup` | Açık bağlantı/süreç kapatma. |
| `version` | Bilgi amaçlı. |

## İyi araç tanımı

Model bir aracı sadece adına, açıklamasına ve parametre açıklamalarına bakarak seçer. Claude Code ve Codex'in
yerleşik araçlarındaki düzen burada da geçerli (yerleşik araçlar `tests/test_tool_specs.py` ile denetlenir):

- İlk cümle aracın ne yaptığını söyler; model çoğu zaman sadece onu okur.
- Ne zaman kullanılır ve, en önemlisi, ne zaman KULLANILMAZ: benzer bir araç varsa adını ver ("durdur/devam için
  media kullan"). Bu, yanlış aracın seçilmesini en çok önleyen kısımdır.
- Her parametrenin `description`'ı olur: birimi, varsayılanı ve kısa bir örneği (`"Saniye. Varsayılan 120."`,
  `"ör. 'Coldplay Yellow'"`). `action` gibi enum parametrelerde her değerin ne yaptığı orada yazar.
- Açıklama 1000 karakteri geçmez: Gemini Live sonrasını keser.
- Hata mesajı modelin kendini düzeltmesine yeter: neyin yanlış olduğunu ve ne yapması gerektiğini söyler
  (`"öğe bulunamadı; browser action=snapshot ile yenile"`). Eksik zorunlu parametre ve geçersiz enum değeri, araç
  çalışmadan ALICE tarafından açıklamasıyla birlikte modele geri döner.

Araçlar arası durum için `ctx.state` (sözlük), ayarlar için `ctx.config.data`, kullanıcıya kısa bilgi için
`ctx.emit("...")` kullanılır. Uzun döngülerde `ctx.cancelled()` kontrol edilmeli.

Bir modül tek eklenti için `PLUGIN`, birden çok için `PLUGINS = [...]` ya da bunları döndüren `register()` tanımlar.

## Nereden yüklenir

1. `~/.alice/plugins/*.py` ve `~/.alice/plugins/<klasör>/__init__.py` (`_` ile başlayanlar atlanır),
2. config'te `[plugins] paths = ["C:/yol/eklenti.py"]`,
3. pip paketleri: `pyproject.toml` içinde `[project.entry-points."alice.plugins"] spotify = "paket.modul"`.

## Kapatma ve hatalar

```toml
[plugins]
enabled = true        # false: hiçbir eklenti yüklenmez
disabled = ["spotify"]
```

Bozuk eklenti ALICE'i durdurmaz: atlanır, hatası `alice plugins` çıktısında ve başlangıç mesajlarında görünür.
Yerleşik bir araç ya da grup adıyla çakışan eklenti de atlanır.

## ALICE'in kendi yazdığı araçlar

Bir işi yapacak aracı yoksa ALICE aracı kendisi yazabilir (`make_tool`, ayrıntı: `alice/selftools.py`):

1. Neyin eksik olduğunu söyler ve eklentiyi, testini ve **bildirgesini** yazar. Bildirge, aracın neye erişeceğini söyler: internet adresleri, klasörler, programlar. Bildirgede olmayan bir adres, klasör ya da program kullanan kod reddedilir. `eval`, `exec` ve `ctypes` yasak.
2. Test kum havuzunda çalışır. Windows'ta bu, düşük bütünlük seviyesi demek: test yalnız geçici klasöre yazabilir ve süre sınırı vardır.
3. ALICE ne yaptığını ve neye erişeceğini bir cümleyle söyleyip **her zaman** onay ister. Onay yoksa bu klasöre hiçbir dosya girmez.
4. Onaydan sonra araç `~/.alice/plugins/<ad>.py` olarak eklenir ve aynı konuşmada kullanılır. Testi `_tests/` altında durur.

`~/.alice/selftools.json` her aracı tutar: ne zaman ve neden yazıldığı, kaç kez kullanıldığı, son hatası. Üç kez hata veren araç kendiliğinden kapanır. Bildirgede `asks_each_time` olan araçlar (kod değiştiren, para harcayan, mesaj gönderen, silen) her kullanımda ayrıca onay ister. "Hangi araçları kendin yazdın?" sorusunun cevabı `make_tool action=list`. Kaldırılan araç silinmez, `_kaldirilan/` klasörüne taşınır.

Sınır: bildirge denetimi koda bakarak yapılır. Çalışırken ağ ve dosya erişimi işletim sistemi seviyesinde kısıtlanmaz.
