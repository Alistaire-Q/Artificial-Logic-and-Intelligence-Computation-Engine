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
    description = "Spotify'da şarkı çalar."     # model bunu okuyup ne zaman çağıracağına karar verir
    parameters = obj({"query": {"type": "string"}}, ["query"])
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
| `name` | Küçük harf, rakam, `-`, `_`. Grup adıdır. |
| `tools` | `Tool` nesneleri. |
| `description`, `keywords` | Araçlar varsayılan olarak bir grupta durur ve modele sadece gerekince gider; bu, isteği küçük ve modeli odaklı tutar. |
| `always` | `True`: araçlar her istekte gider (grup yok). Az ve sık kullanılan araçlar için. |
| `live` | Sesli konuşmada grubu baştan aç. |
| `cleanup` | Açık bağlantı/süreç kapatma. |
| `version` | Bilgi amaçlı. |

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
