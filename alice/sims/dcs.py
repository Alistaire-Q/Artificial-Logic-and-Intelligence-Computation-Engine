"""DCS World bağlantısı.

  install()       -> Saved Games\\DCS*\\Scripts altına alice_export.lua koyar, Export.lua'ya bir satır ekler
  DCSListener     -> betiğin UDP ile gönderdiği veriyi alır, FlightState'e çevirir
  read_mission()  -> .miz görev dosyasından oyuncunun grubunu, frekansını ve waypoint'lerini çıkarır
"""

from __future__ import annotations

import json
import math
import os
import re
import socket
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from alice.sims.flight import FT, NM, FlightState, profile_for

PORT = 41230
SCRIPT = Path(__file__).with_name("alice_export.lua")
EXPORT_LINE = ("pcall(function() local aliceLfs = require('lfs'); "
               "dofile(aliceLfs.writedir() .. 'Scripts/alice_export.lua') end) -- ALICE")


def saved_games_dirs() -> list[Path]:
    """DCS'in Saved Games klasörleri (kararlı ve open beta)."""
    base = Path(os.environ.get("USERPROFILE", Path.home())) / "Saved Games"
    return [p for p in (base / "DCS", base / "DCS.openbeta") if p.is_dir()]


def install(dirs: list[Path] | None = None, port: int = PORT) -> list[Path]:
    """Betiği kur. Export.lua'da satır zaten varsa tekrar eklemez. Kurulan klasörleri döndürür."""
    done = []
    lua = SCRIPT.read_text(encoding="utf-8").replace('"127.0.0.1", 41230,', f'"127.0.0.1", {port},')
    for d in dirs if dirs is not None else saved_games_dirs():
        scripts = d / "Scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (scripts / "alice_export.lua").write_text(lua, encoding="utf-8")
        export = scripts / "Export.lua"
        text = export.read_text(encoding="utf-8", errors="replace") if export.exists() else ""
        if "alice_export.lua" not in text:
            export.write_text(text.rstrip("\n") + ("\n" if text else "") + EXPORT_LINE + "\n", encoding="utf-8")
        done.append(d)
    return done


def ensure_installed(dirs: list[Path] | None = None, port: int = PORT) -> list[Path]:
    """Kurulu değilse ya da betik değişmişse kur (çekirdek açılışta çağırır). Kurulan klasörleri döndürür."""
    lua = SCRIPT.read_text(encoding="utf-8").replace('"127.0.0.1", 41230,', f'"127.0.0.1", {port},')
    todo = []
    for d in dirs if dirs is not None else saved_games_dirs():
        script, export = d / "Scripts" / "alice_export.lua", d / "Scripts" / "Export.lua"
        ok = (script.exists() and script.read_text(encoding="utf-8", errors="replace") == lua and export.exists()
              and "alice_export.lua" in export.read_text(encoding="utf-8", errors="replace"))
        if not ok:
            todo.append(d)
    return install(todo, port) if todo else []


def uninstall(dirs: list[Path] | None = None) -> list[Path]:
    done = []
    for d in dirs if dirs is not None else saved_games_dirs():
        export = d / "Scripts" / "Export.lua"
        if export.exists():
            lines = export.read_text(encoding="utf-8", errors="replace").splitlines()
            kept = [ln for ln in lines if "alice_export.lua" not in ln]
            if len(kept) != len(lines):
                export.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
                done.append(d)
        script = d / "Scripts" / "alice_export.lua"
        if script.exists():
            script.unlink()
    return done


def to_state(d: dict[str, Any], internal_fuel_kg: float | None = None) -> FlightState:
    """Betiğin gönderdiği sözlüğü FlightState'e çevir."""
    aircraft = str(d.get("aircraft") or "")
    fuel = None
    fi, fe = d.get("fuel_int"), d.get("fuel_ext")
    if isinstance(fi, (int, float)):
        cap = internal_fuel_kg or profile_for(aircraft).internal_fuel_kg
        # Bazı modüller kilogram, bazıları dolu depoya oran (0-1) veriyor.
        fuel = fi * cap if fi <= 1.5 else fi
        if isinstance(fe, (int, float)) and fe > 0:
            fuel += fe * cap if fe <= 1.5 else fe
    num = lambda k: float(d[k]) if isinstance(d.get(k), (int, float)) else None  # noqa: E731
    return FlightState(t=float(d.get("t") or 0), source="dcs", aircraft=aircraft, ias=num("ias"), tas=num("tas"),
                       alt=num("alt"), agl=num("agl"), vv=num("vv"), aoa=num("aoa"), g=num("g"), fuel_kg=fuel,
                       burn_kgs=num("burn"), chaff=int(d["chaff"]) if isinstance(d.get("chaff"), (int, float)) else None,
                       flare=int(d["flare"]) if isinstance(d.get("flare"), (int, float)) else None,
                       gear=num("gear"), x=num("x"), z=num("z"),
                       heading=math.degrees(num("heading")) % 360 if num("heading") is not None else None,
                       extra=_extra(d, num))


def _extra(d: dict[str, Any], num: Callable[[str], float | None]) -> dict[str, Any]:
    deg = lambda k: None if num(k) is None else math.degrees(num(k))  # noqa: E731
    e: dict[str, Any] = {"mach": num("mach"), "pitch": deg("pitch"), "bank": deg("bank"),
                         "mag_heading": None if num("mag_heading") is None else deg("mag_heading") % 360,
                         "radalt": None if num("radalt") is None else num("radalt") / FT,
                         "flaps": num("flaps"), "speedbrake": num("speedbrake"), "hook": num("hook"),
                         "wp_x": num("wp_x"), "wp_z": num("wp_z"), "wp_num": num("wp_num"),
                         "ils_glide": num("ils_glide"), "ils_side": num("ils_side"), "gun": num("gun")}
    rpms = [v for v in (num("rpm_l"), num("rpm_r")) if v]
    if rpms:
        e["rpm"] = max(rpms)
    egts = [v for v in (num("egt_l"), num("egt_r")) if v]
    if egts:
        e["egt"] = max(egts)
    wx, wz = num("wind_x"), num("wind_z")
    if wx is not None and wz is not None:
        e["wind_kt"] = math.hypot(wx, wz) / 0.514444
        e["wind_from"] = (math.degrees(math.atan2(wz, wx)) + 180) % 360  # rüzgarın geldiği yön
    for k in ("weapons", "warnings", "mode"):
        if isinstance(d.get(k), str) and d[k].strip("/ "):
            e[k] = d[k]
    return {k: v for k, v in e.items() if v is not None}


class DCSListener:
    """UDP'den gelen veriyi companion'a ve (saniyede bir) depoya iletir."""

    def __init__(self, on_state: Callable[[FlightState], None], on_restricted: Callable[[], None] = lambda: None,
                 on_end: Callable[[], None] = lambda: None, on_metrics: Callable[[list], None] = lambda rows: None,
                 port: int = PORT, timeout: float = 10.0):
        self.on_state = on_state
        self.on_restricted = on_restricted
        self.on_end = on_end
        self.on_metrics = on_metrics
        self.port = port
        self.timeout = timeout
        self.last: FlightState | None = None
        self.active = False
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._metrics_at = 0.0

    def start(self) -> "DCSListener":
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.settimeout(1.0)
        threading.Thread(target=self._loop, daemon=True, name="alice-dcs").start()
        return self

    @property
    def bound_port(self) -> int:
        return self._sock.getsockname()[1] if self._sock else self.port

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            self._sock.close()

    def handle(self, data: dict[str, Any]) -> None:
        if data.get("event") == "stop":
            self._end()
            return
        if data.get("event") == "start":
            return
        self.active = True
        if data.get("restricted"):
            self.on_restricted()
            return
        s = to_state(data)
        self.last = s
        self.on_state(s)
        if time.monotonic() - self._metrics_at >= 1.0:
            self._metrics_at = time.monotonic()
            rows = [(f"dcs.{k}", v, None) for k, v in (
                ("ias_kt", s.ias and s.ias / 0.514444), ("alt_ft", s.alt and s.alt / FT), ("g", s.g), ("aoa", s.aoa),
                ("fuel.kg", s.fuel_kg), ("chaff", s.chaff), ("flare", s.flare)) if v is not None]
            self.on_metrics(rows)

    def _end(self) -> None:
        if self.active:
            self.active = False
            self.on_end()

    def _loop(self) -> None:
        last_rx = time.monotonic()
        while not self._stop.is_set():
            try:
                raw, _ = self._sock.recvfrom(65535)
            except socket.timeout:
                if self.active and time.monotonic() - last_rx > self.timeout:
                    self._end()
                continue
            except OSError:
                return
            last_rx = time.monotonic()
            try:
                data = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(data, dict):
                try:
                    self.handle(data)
                except Exception:  # bir paket bozuksa dinleyici ölmesin
                    pass


# ------------------------------------------------------------ görev dosyası
class LuaError(ValueError):
    pass


def parse_lua_table(text: str) -> Any:
    """DCS görev dosyasındaki `mission = { ... }` gibi Lua tablolarını Python'a çevirir.
    Sadece veri sözdizimi: tablo, string, sayı, true/false/nil, yorumlar."""
    i = 0
    n = len(text)

    def skip():
        nonlocal i
        while i < n:
            if text[i].isspace():
                i += 1
            elif text.startswith("--", i):
                if text.startswith("--[[", i):
                    end = text.find("]]", i)
                    i = n if end < 0 else end + 2
                else:
                    end = text.find("\n", i)
                    i = n if end < 0 else end + 1
            else:
                return

    def string():
        nonlocal i
        q = text[i]
        i += 1
        out = []
        while i < n and text[i] != q:
            c = text[i]
            if c == "\\" and i + 1 < n:
                nxt = text[i + 1]
                out.append({"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'", "\n": "\n"}.get(nxt, nxt))
                i += 2
                continue
            out.append(c)
            i += 1
        i += 1
        return "".join(out)

    def long_string():
        nonlocal i
        m = re.match(r"\[(=*)\[", text[i:])
        close = "]" + m.group(1) + "]"
        start = i + m.end()
        end = text.find(close, start)
        i = end + len(close)
        return text[start:end]

    def value():
        nonlocal i
        skip()
        c = text[i]
        if c == "{":
            return table()
        if c in "\"'":
            return string()
        if c == "[" and re.match(r"\[=*\[", text[i:]):
            return long_string()
        m = re.match(r"-?(0x[0-9a-fA-F]+|\d+\.?\d*(e[+-]?\d+)?|\.\d+(e[+-]?\d+)?)", text[i:], re.I)
        if m:
            i += m.end()
            tok = m.group(0)
            if "x" in tok.lower():
                return int(tok, 16)
            return float(tok) if any(ch in tok for ch in ".eE") else int(tok)
        for word, val in (("true", True), ("false", False), ("nil", None)):
            if text.startswith(word, i):
                i += len(word)
                return val
        raise LuaError(f"beklenmeyen karakter {c!r} (konum {i})")

    def table():
        nonlocal i
        i += 1
        out: dict[Any, Any] = {}
        auto = 1
        while True:
            skip()
            if text[i] == "}":
                i += 1
                break
            if text[i] == "[" and not re.match(r"\[=*\[", text[i:]):
                i += 1
                key = value()
                skip()
                i += 1  # ]
                skip()
                i += 1  # =
                out[key] = value()
            else:
                m = re.match(r"([A-Za-z_]\w*)\s*=(?!=)", text[i:])
                if m:
                    i += m.end()
                    out[m.group(1)] = value()
                else:
                    out[auto] = value()
                    auto += 1
            skip()
            if text[i] in ",;":
                i += 1
        return out

    skip()
    m = re.match(r"([A-Za-z_]\w*)\s*=", text[i:])
    if m:
        i += m.end()
    return value()


def lua_list(t: Any) -> list:
    if isinstance(t, dict):
        return [t[k] for k in sorted(k for k in t if isinstance(k, int))]
    return list(t or [])


@dataclass
class Waypoint:
    name: str
    x: float
    y: float
    alt_ft: float
    speed_kt: float | None = None
    kind: str = ""


@dataclass
class MissionInfo:
    name: str
    aircraft: str
    group: str
    frequency_mhz: float | None
    waypoints: list[Waypoint] = field(default_factory=list)
    description: str = ""
    theatre: str = ""

    def legs(self) -> list[tuple[Waypoint, float, float]]:
        """(waypoint, önceki noktadan mesafe nm, gerçek kerteriz derece)."""
        out = []
        prev = None
        for wp in self.waypoints:
            if prev is None:
                out.append((wp, 0.0, 0.0))
            else:
                dx, dy = wp.x - prev.x, wp.y - prev.y  # x kuzey, y doğu
                out.append((wp, math.hypot(dx, dy) / NM, (math.degrees(math.atan2(dy, dx)) + 360) % 360))
            prev = wp
        return out


def read_mission(path: Path) -> MissionInfo:
    with zipfile.ZipFile(path) as z:
        mission = parse_lua_table(z.read("mission").decode("utf-8", errors="replace"))
        dictionary: dict = {}
        for name in z.namelist():
            if name.endswith("l10n/DEFAULT/dictionary"):
                dictionary = parse_lua_table(z.read(name).decode("utf-8", errors="replace"))
    resolve = lambda v: dictionary.get(v, v) if isinstance(v, str) else (v or "")  # noqa: E731
    found = None
    for side in ("blue", "red", "neutrals"):
        for country in lua_list(mission.get("coalition", {}).get(side, {}).get("country")):
            for cat in ("plane", "helicopter"):
                for group in lua_list(country.get(cat, {}).get("group")):
                    for unit in lua_list(group.get("units")):
                        if unit.get("skill") in ("Client", "Player"):
                            found = found or (group, unit, unit.get("skill") == "Player")
                            if unit.get("skill") == "Player":
                                found = (group, unit, True)
    if found is None:
        raise ValueError("görevde oyuncu ya da client uçağı bulunamadı")
    group, unit, _ = found
    wps = []
    for i, p in enumerate(lua_list(group.get("route", {}).get("points"))):
        name = resolve(p.get("name")) or ("Kalkış" if i == 0 else f"WP{i}")
        wps.append(Waypoint(str(name), float(p.get("x", 0)), float(p.get("y", 0)), float(p.get("alt", 0)) / FT,
                            float(p["speed"]) / 0.514444 if p.get("speed") else None, str(p.get("type", ""))))
    return MissionInfo(name=Path(path).stem, aircraft=str(unit.get("type", "")), group=str(resolve(group.get("name"))),
                       frequency_mhz=float(group["frequency"]) if group.get("frequency") else None, waypoints=wps,
                       description=str(resolve(mission.get("descriptionText", "")))[:1500],
                       theatre=str(mission.get("theatre", "")))


def latest_mission(dirs: list[Path] | None = None) -> Path | None:
    """En son değiştirilen .miz (görevler ve çok oyunculu indirilenler)."""
    cands = []
    for d in dirs if dirs is not None else saved_games_dirs():
        for sub in ("Missions", "MissionEditor", "Tracks", "Temp"):
            p = d / sub
            if p.is_dir():
                cands += list(p.rglob("*.miz"))
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


# ---------------------------------------------------------------- komutlar
def dcs_cmd(argv: list[str]) -> int:
    """alice dcs install | uninstall | status | kneeboard [görev.miz]"""
    from rich.console import Console

    console = Console(highlight=False)
    sub = argv[0] if argv else "status"
    dirs = saved_games_dirs()
    if sub in ("install", "uninstall") and not dirs:
        console.print("[red]DCS Saved Games klasörü bulunamadı[/] (DCS bir kez açılmış olmalı)")
        return 1
    if sub == "install":
        for d in install(dirs):
            console.print(f"[green]kuruldu:[/] {d / 'Scripts' / 'alice_export.lua'}")
        console.print("DCS'i yeniden başlat. Veri, çekirdek açıkken (alice core run) işlenir.")
        return 0
    if sub == "uninstall":
        for d in uninstall(dirs):
            console.print(f"[green]kaldırıldı:[/] {d}")
        return 0
    if sub == "kneeboard":
        from alice.sims import kneeboard as kb

        path = Path(argv[1]) if len(argv) > 1 else latest_mission(dirs)
        info = read_mission(path) if path else None
        written = kb.write(kb.render(info, kb.load_notes()), kb.kneeboard_dirs(dirs))
        console.print(f"[green]{len(written)} sayfa yazıldı[/]" + (f" ({info.name})" if info else ""))
        return 0
    for d in dirs:
        installed = (d / "Scripts" / "alice_export.lua").exists()
        console.print(f"{d}: {'[green]kurulu[/]' if installed else '[yellow]kurulu değil (alice dcs install)[/]'}")
    if not dirs:
        console.print("[yellow]DCS Saved Games klasörü bulunamadı[/]")
    m = latest_mission(dirs)
    if m:
        console.print(f"son görev: {m}")
    return 0
