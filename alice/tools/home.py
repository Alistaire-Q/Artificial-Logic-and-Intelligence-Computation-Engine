"""Bilgisayarın dışındaki şeyler: PC'nin güç/ekran ayarları, Wi-Fi ve ev ağı, ADB ile Android TV ve telefon."""

from __future__ import annotations

from typing import Any

from alice.tools.base import COMPUTER, DANGER, READ, Tool, ToolContext, ToolError, ToolResult, obj


class PC(Tool):
    name = "pc"
    description = ("Bu bilgisayarın kendisi: kilitle, ekranı kapat, uyku, hazırda beklet, yeniden başlat, güç planı "
                   "(dengeli/yüksek performans/tasarruf), monitör parlaklığı (DDC/CI ya da dizüstü ekranı), "
                   "Wake-on-LAN bilgisi (bilgisayarı telefondan açmak için MAC ve gerekenler). Ses ve kapatma "
                   "zamanlayıcısı için media.")
    parameters = obj({
        "action": {"type": "string", "enum": ["lock", "monitor_off", "sleep", "hibernate", "restart", "plans",
                                              "set_plan", "brightness", "wol_info"],
                   "description": "lock | monitor_off | sleep | hibernate | restart (minutes sonra, varsayılan hemen) "
                   "| plans: güç planlarını listele | set_plan: plan'a geç | brightness: level verilirse ayarla, "
                   "verilmezse oku | wol_info: uzaktan açma için ağ kartı, MAC, durum."},
        "plan": {"type": "string", "description": "set_plan için ad ya da kısaltma: 'dengeli', 'performans', "
                 "'tasarruf'."},
        "level": {"type": "integer", "description": "brightness için 0-100."},
        "monitor": {"type": "integer", "description": "brightness için monitör sırası (1'den). Boş: hepsi."},
        "minutes": {"type": "number", "description": "restart için kaç dakika sonra. Varsayılan 0."},
    }, ["action"])
    risk = COMPUTER

    def risk_for(self, args: dict[str, Any]) -> str:
        a = args.get("action")
        if a in ("sleep", "hibernate", "restart"):
            return DANGER
        if a in ("plans", "wol_info") or (a == "brightness" and args.get("level") is None):
            return READ
        return COMPUTER

    def run(self, ctx: ToolContext, action: str, plan: str = "", level: int | None = None,
            monitor: int | None = None, minutes: float = 0) -> ToolResult:
        from alice.home import pc

        try:
            if action == "lock":
                return ToolResult(pc.lock())
            if action == "monitor_off":
                return ToolResult(pc.monitor_off())
            if action in ("sleep", "hibernate"):
                return ToolResult(pc.suspend(action == "hibernate"))
            if action == "restart":
                return ToolResult(pc.restart_in(minutes))
            if action == "plans":
                return ToolResult("\n".join(f"{'* ' if p['active'] else '  '}{p['name']}" for p in pc.plans())
                                  or "güç planı bulunamadı")
            if action == "set_plan":
                if not plan:
                    raise ToolError("plan gerekli")
                return ToolResult(pc.set_plan(plan))
            if action == "brightness":
                return ToolResult(pc.brightness(level, monitor))
            if action == "wol_info":
                return ToolResult(pc.wol_info())
        except (RuntimeError, OSError) as e:
            raise ToolError(str(e)) from e
        raise ToolError(f"bilinmeyen action: {action}")


class Wifi(Tool):
    name = "wifi"
    description = ("Wi-Fi ve ev ağı: bağlantı durumu ve sinyal, çevredeki ağlar, kayıtlı ağlar, bir ağa bağlan "
                   "(kayıtlı değilse şifreyle) ya da bağlantıyı kes, ağdaki cihazları listele (IP, MAC, ad), bir "
                   "cihazı Wake-on-LAN ile uyandır.")
    parameters = obj({
        "action": {"type": "string", "enum": ["status", "scan", "saved", "connect", "disconnect", "devices", "wake"],
                   "description": "status | scan: çevredeki ağlar | saved: kayıtlı ağlar | connect: ssid'e bağlan | "
                   "disconnect | devices: ağdaki cihazlar (birkaç saniye sürer) | wake: mac'teki cihaza WoL paketi."},
        "ssid": {"type": "string", "description": "connect için ağ adı, tam yazımıyla."},
        "password": {"type": "string", "description": "connect için şifre; ağ zaten kayıtlıysa gerekmez."},
        "mac": {"type": "string", "description": "wake için cihazın MAC adresi, ör. 'aa:bb:cc:dd:ee:ff'."},
    }, ["action"])
    risk = COMPUTER

    def risk_for(self, args: dict[str, Any]) -> str:
        return COMPUTER if args.get("action") in ("connect", "disconnect", "wake") else READ

    def run(self, ctx: ToolContext, action: str, ssid: str = "", password: str = "", mac: str = "") -> ToolResult:
        from alice.home import net, wifi

        try:
            if action == "status":
                return ToolResult(wifi.status())
            if action == "scan":
                nets = wifi.scan()
                return ToolResult("\n".join(f"{n['ssid'] or '(gizli)'}: %{n['signal']} {n['auth']}" for n in nets)
                                  or "ağ bulunamadı")
            if action == "saved":
                return ToolResult("\n".join(wifi.profiles()) or "kayıtlı ağ yok")
            if action == "connect":
                if not ssid:
                    raise ToolError("ssid gerekli")
                return ToolResult(wifi.connect(ssid, password))
            if action == "disconnect":
                return ToolResult(wifi.disconnect())
            if action == "devices":
                devs = net.devices()
                return ToolResult("\n".join(f"{d['ip']}  {d['mac']}  {d['name']}".rstrip() for d in devs)
                                  or "cihaz bulunamadı")
            if action == "wake":
                if not mac:
                    raise ToolError("mac gerekli")
                me = net.local_ip()
                net.wake_on_lan(mac, me.rsplit(".", 1)[0] + ".255")
                return ToolResult(f"uyandırma paketi gönderildi: {mac}")
        except ValueError as e:
            raise ToolError(str(e)) from e
        except (RuntimeError, OSError) as e:
            raise ToolError(str(e)) from e
        raise ToolError(f"bilinmeyen action: {action}")


class Android(Tool):
    name = "android"
    description = ("ADB ile Android TV ve Android telefon. Her ikisinde: aç/kapa, tuş (ses, oynat, yön, ana ekran), "
                   "uygulama aç, YouTube'da video aç, yazı yaz, ekran görüntüsü, uygulama listesi. Telefonda ayrıca "
                   "pil, bildirimler, bilgisayardan dosya gönderme. İlk kullanımda find (TV'yi ağda arar) ya da "
                   "host ile connect; telefon için önce pair. Adres bir kere kaydedilir.")
    parameters = obj({
        "device": {"type": "string", "enum": ["tv", "phone"], "description": "Hangi cihaz."},
        "action": {"type": "string", "enum": ["connect", "pair", "find", "power", "key", "open_app", "youtube",
                                              "open_url", "type", "screenshot", "apps", "battery", "notifications",
                                              "send_file"],
                   "description": "connect: host'a bağlan ve kaydet | pair: telefonu eşle (host=IP:port, code) | "
                   "find: ağda ADB'si açık TV ara | power: state'e göre | key: key tuşuna times kez bas | open_app: "
                   "app | youtube: query ya da url'deki videoyu aç | open_url | type: text yaz | screenshot | apps | "
                   "battery | notifications | send_file: path'teki dosyayı telefonun İndirilenler'ine."},
        "host": {"type": "string", "description": "connect/pair için IP ya da IP:port."},
        "code": {"type": "string", "description": "pair için telefonda görünen 6 haneli kod."},
        "state": {"type": "string", "enum": ["on", "off", "toggle", "status"], "description": "power için."},
        "key": {"type": "string", "enum": ["home", "back", "up", "down", "left", "right", "ok", "play_pause", "next",
                                           "previous", "volume_up", "volume_down", "mute", "input", "menu"],
                "description": "key için tuş."},
        "times": {"type": "integer", "description": "key için basış sayısı. Varsayılan 1."},
        "app": {"type": "string", "description": "open_app için ad (youtube, netflix, spotify...) ya da paket adı."},
        "query": {"type": "string", "description": "youtube için arama, ör. 'lofi hip hop'."},
        "url": {"type": "string", "description": "youtube ya da open_url için adres."},
        "text": {"type": "string", "description": "type için yazılacak metin."},
        "path": {"type": "string", "description": "send_file için bilgisayardaki dosya yolu."},
    }, ["device", "action"])
    risk = COMPUTER

    def risk_for(self, args: dict[str, Any]) -> str:
        a = args.get("action")
        if a in ("find", "apps", "battery", "screenshot") or (a == "power" and args.get("state") == "status"):
            return READ
        return COMPUTER

    def run(self, ctx: ToolContext, device: str, action: str, host: str = "", code: str = "", state: str = "status",
            key: str = "", times: int = 1, app: str = "", query: str = "", url: str = "", text: str = "",
            path: str = "") -> ToolResult:
        from alice.home import android as adb

        conf = ctx.config.data.get("android", {}) or {}
        exe = conf.get("adb", "")
        try:
            if action == "pair":
                if not host or not code:
                    raise ToolError("pair için host (telefonun eşleme ekranındaki IP:port) ve code gerekli")
                return ToolResult(adb.pair(host, code, exe))
            if action == "find":
                from alice.home.net import scan_port

                hits = scan_port(5555)
                return ToolResult(("ADB'si açık cihazlar: " + ", ".join(hits) + ". Bağlanmak için connect host=...")
                                  if hits else "Ağda ADB'si açık cihaz yok: TV'de Geliştirici seçenekleri > Ağ "
                                  "üzerinden hata ayıklama açık mı?")
            if action == "connect":
                target = host or conf.get(device, "")
                d = adb.Device(target, device, exe)
                msg = d.connect()
                if host:
                    from alice.config import set_config_value

                    set_config_value("android", device, d.serial)
                    conf[device] = d.serial
                    ctx.config.data["android"] = conf
                return ToolResult(msg)
            d = self._device(ctx, conf, device, exe)
            if action == "power":
                return ToolResult(d.power(state))
            if action == "key":
                if not key:
                    raise ToolError("key gerekli")
                return ToolResult(d.keys(key, times))
            if action == "open_app":
                if not app:
                    raise ToolError("app gerekli")
                return ToolResult(d.open_app(app))
            if action == "youtube":
                vid = adb.youtube_id(url) if url else None
                title = url
                if not vid:
                    if not query:
                        raise ToolError("youtube için query ya da url gerekli")
                    from alice.tools.extras import youtube_search

                    hits = youtube_search(query, 1)
                    if not hits:
                        return ToolResult("video bulunamadı")
                    title, vid = hits[0]
                return ToolResult(f"{d.youtube(vid)} ({title})")
            if action == "open_url":
                if not url:
                    raise ToolError("url gerekli")
                return ToolResult(d.open_url(url))
            if action == "type":
                return ToolResult(d.type_text(text))
            if action == "screenshot":
                return ToolResult(f"{device} ekranı", images=[d.screenshot()])
            if action == "apps":
                return ToolResult("\n".join(d.packages()) or "uygulama yok")
            if action == "battery":
                return ToolResult(d.battery())
            if action == "notifications":
                items = d.notifications()
                return ToolResult("\n".join(f"[{n['app']}] {n['title']}: {n['text']}".rstrip(": ") for n in items)
                                  or "bildirim yok")
            if action == "send_file":
                if not path:
                    raise ToolError("path gerekli")
                p = ctx.resolve(path)
                if not p.is_file():
                    raise ToolError(f"dosya yok: {p}")
                return ToolResult(d.push(str(p)))
        except adb.AdbError as e:
            raise ToolError(str(e)) from e
        except (OSError, TimeoutError) as e:
            raise ToolError(f"{device} komutu çalışmadı: {e}") from e
        raise ToolError(f"bilinmeyen action: {action}")

    @staticmethod
    def _device(ctx: ToolContext, conf: dict, device: str, exe: str):
        from alice.home import android as adb

        d = adb.Device(conf.get(device, ""), device, exe)
        try:
            d.ensure()
        except adb.AdbError:
            # Telefonun kablosuz hata ayıklama portu her seferinde değişir: aynı IP'nin yeni portunu mDNS'ten bul.
            new = adb.mdns_connect_address(d.serial.split(":")[0], exe) if device == "phone" else None
            if not new or new == d.serial:
                raise
            from alice.config import set_config_value

            d = adb.Device(new, device, exe)
            d.connect()
            set_config_value("android", device, d.serial)
            conf[device] = d.serial
        return d


TOOLS = [PC(), Wifi(), Android()]
