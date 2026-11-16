"""Veri toplayan gözlemciler.

Her gözlemci `collect()` ile (anahtar, sayı, metin) satırları döndürür; servis bunları
depoya yazar. Şimdilik bilgisayar gözlemcisi var; DCS, War Thunder ve saat aynı arayüzle
eklenecek.

Sıcaklıklar: Linux'ta psutil, NVIDIA kartlarda nvidia-smi. Windows'ta CPU sıcaklığı için
LibreHardwareMonitor açıksa onun WMI kaydı okunur (psutil Windows'ta sıcaklık vermiyor).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
from typing import Protocol

import psutil

Row = tuple[str, float | None, str | None]


class Monitor(Protocol):
    name: str

    def collect(self) -> list[Row]: ...


def _key_part(s: str) -> str:
    """Anahtar içinde güvenli parça: 'C:\\' -> 'C', 'Unity Hub.exe' -> 'unity_hub.exe'."""
    return re.sub(r"[^\w.-]+", "_", s.strip().rstrip(":\\/")).strip("_").lower() or "root"


def _run(cmd: list[str], timeout: float = 5) -> str | None:
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # Türkçe Windows'ta PowerShell hata metni cp857/cp1254: yerel kod sayfasıyla çözmek UnicodeDecodeError atıp
        # gözlemciyi her turda düşürüyordu.
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=flags)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def foreground_process() -> str | None:
    """Öndeki pencerenin program adı (sadece Windows)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        hwnd = user32.GetForegroundWindow()
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return psutil.Process(pid.value).name() if pid.value else None
    except (OSError, AttributeError, psutil.Error):
        return None


def _win_proc_stats() -> list[tuple[int, str, int, float]] | None:
    """Windows: (pid, ad, rss bayt, kullanıcı+çekirdek işlemci sn); erişilemeyen süreçler atlanır. Başka
    platformda ya da psutil'in iç yapısı değişmişse None (yavaş genel yol kullanılır)."""
    if sys.platform != "win32":
        return None
    try:
        from psutil import _psplatform

        mem_info, times = _psplatform.cext.proc_memory_info, _psplatform.cext.proc_times
    except (ImportError, AttributeError):
        return None
    out = []
    for p in psutil.process_iter(["name"]):  # adlar tek anlık görüntüden gelir: hızlı
        name = p.info.get("name") or ""
        if not name:
            continue
        try:
            rss = mem_info(p.pid)[2]  # psutil'de de rss = WorkingSetSize = 3. alan
            user, system, _created = times(p.pid)
        except Exception:  # noqa: BLE001 - korumalı süreç (erişim yok) ya da süreç kapandı: atla
            continue
        out.append((p.pid, name, int(rss), float(user) + float(system)))
    return out


class PCMonitor:
    name = "pc"

    def __init__(self, top_n: int = 5):
        self.top_n = top_n
        self.processes: set[str] = set()  # son toplamadaki süreç adları (mod tespiti için)
        self._nvidia = shutil.which("nvidia-smi")
        self._lhm = sys.platform == "win32"  # ilk başarısızlıkta kapanır
        # -inf: ilk ölçüm hemen yapılır (monotonic() açılıştan sonraki ilk 60 sn'de < 60 olabilir)
        self._disk_at = float("-inf")
        self._temps_at = float("-inf")
        self._temps_last: list[Row] = []
        psutil.cpu_percent(interval=None)  # ilk çağrı hep 0 döner, ısındır

    def collect(self) -> list[Row]:
        rows: list[Row] = [("pc.cpu.percent", psutil.cpu_percent(interval=None), None)]
        vm = psutil.virtual_memory()
        rows.append(("pc.mem.percent", vm.percent, None))
        rows.append(("pc.mem.used_gb", vm.used / 2**30, None))
        rows += self._disks()
        rows += self._temps()
        rows += self._gpu()
        rows += self._procs()
        fg = foreground_process()
        if fg:
            rows.append(("pc.foreground", None, fg))
        return rows

    def _disks(self) -> list[Row]:
        # Disk doluluğu yavaş değişir, dakikada bir yeter.
        if time.monotonic() - self._disk_at < 60:
            return []
        self._disk_at = time.monotonic()
        out: list[Row] = []
        for part in psutil.disk_partitions(all=False):
            opts = part.opts.split(",")
            # CD-ROM ve salt okunur bağlamalar (squashfs vb.) hep "%100 dolu" görünür, atla.
            if "cdrom" in opts or "ro" in opts or not part.fstype:
                continue
            try:
                u = psutil.disk_usage(part.mountpoint)
            except (PermissionError, OSError):
                continue
            out.append((f"pc.disk.{_key_part(part.mountpoint)}.percent", u.percent, None))
            # Yüzde büyük veri diskinde (F: hep %98) gürültü, küçük sistem diskinde geç kalır: boş GB de yazılsın.
            out.append((f"pc.disk.{_key_part(part.mountpoint)}.free_gb", round(u.free / 1024 ** 3, 2), None))
        return out

    def _temps(self) -> list[Row]:
        out: list[Row] = []
        fn = getattr(psutil, "sensors_temperatures", None)
        if fn:
            try:
                temps = fn() or {}
            except (OSError, RuntimeError):
                temps = {}
            cpu = [t.current for name in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz")
                   for t in temps.get(name, []) if t.current]
            if cpu:
                out.append(("pc.cpu.temp", max(cpu), None))
        if self._lhm and not out:
            # Her sorgu bir PowerShell açıyor (~0,5 sn işlemci): 15 sn'de bir değil dakikada bir yeter.
            if time.monotonic() - self._temps_at >= 60:
                self._temps_at = time.monotonic()
                self._temps_last = self._lhm_temps()
            out += self._temps_last
        return out

    def _lhm_temps(self) -> list[Row]:
        ps = ("Get-CimInstance -Namespace root/LibreHardwareMonitor -ClassName Sensor -ErrorAction Stop | "
              "Where-Object {$_.SensorType -eq 'Temperature'} | ForEach-Object {\"$($_.Identifier)|$($_.Value)\"}")
        text = _run(["powershell", "-NoProfile", "-Command", ps], timeout=8)
        if not text:
            self._lhm = False
            return []
        cpu = []
        for ident, _, v in (ln.partition("|") for ln in text.splitlines()):
            if "cpu" not in ident.lower():
                continue
            try:
                cpu.append(float(v.strip().replace(",", ".")))  # Türkçe yerel ayarda ondalık virgül
            except ValueError:
                continue
        return [("pc.cpu.temp", max(cpu), None)] if cpu else []

    def _gpu(self) -> list[Row]:
        if not self._nvidia:
            return []
        text = _run([self._nvidia, "--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total",
                     "--format=csv,noheader,nounits"])
        if not text:
            return []
        try:
            temp, util, used, total = (float(x) for x in text.splitlines()[0].split(","))
        except ValueError:
            return []
        return [("pc.gpu.temp", temp, None), ("pc.gpu.util", util, None),
                ("pc.gpu.mem_percent", 100 * used / total if total else 0.0, None)]

    def _procs(self) -> list[Row]:
        mem: dict[str, float] = {}
        cpu: dict[str, float] = {}
        names: set[str] = set()
        fast = _win_proc_stats()
        if fast is not None:
            # Windows: psutil erişilemeyen (korumalı) süreçte bütün sistemin anlık görüntüsünü alıp yeniden deniyor
            # ve bunu GIL'i bırakmadan yapıyor. 465 süreçte tarama 17 sn sürüyordu (15 sn'de bir): sesli asistanın
            # dinleme döngüsü GIL bekleyip mikrofon sesini kaybediyor, "Alice naber" hiç duyulmuyordu (30 Ekim,
            # py-spy ile görüldü). Korumalı süreçleri atlayan yol 0,02 sn.
            now = time.monotonic()
            prev, self._cpu_prev = getattr(self, "_cpu_prev", {}), {}
            for pid, name, rss, busy in fast:
                names.add(name)
                mem[name] = mem.get(name, 0.0) + rss / 2**20
                self._cpu_prev[pid] = (busy, now)
                if pid in prev and now > prev[pid][1]:
                    pct = 100 * (busy - prev[pid][0]) / (now - prev[pid][1])
                    cpu[name] = cpu.get(name, 0.0) + max(0.0, pct)
        else:
            for p in psutil.process_iter(["name", "memory_info", "cpu_percent"]):
                name = p.info.get("name") or ""
                if not name:
                    continue
                names.add(name)
                mi = p.info.get("memory_info")
                mem[name] = mem.get(name, 0.0) + (mi.rss / 2**20 if mi else 0.0)
                cpu[name] = cpu.get(name, 0.0) + (p.info.get("cpu_percent") or 0.0)
        self.processes = names
        out: list[Row] = []
        for name in sorted(mem, key=mem.get, reverse=True)[: self.top_n]:
            out.append((f"pc.proc.{_key_part(name)}.mem_mb", mem[name], None))
        for name in sorted(cpu, key=cpu.get, reverse=True)[: self.top_n]:
            if cpu[name] > 0:
                out.append((f"pc.proc.{_key_part(name)}.cpu", cpu[name], None))
        return out
