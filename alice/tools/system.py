from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from typing import Any

import psutil

from alice.tools.base import DANGER, EXEC, Tool, ToolContext, ToolError, ToolResult, obj

PROTECTED = {"system", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe",
             "explorer.exe", "init", "systemd", "launchd", "kernel_task", "dwm.exe"}


def _gpu() -> str:
    if shutil.which("nvidia-smi"):
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                            "--format=csv,noheader"], capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout.strip()
    if sys.platform == "win32":
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "(Get-CimInstance Win32_VideoController).Name -join ', '"], capture_output=True, text=True)
        return r.stdout.strip()
    return "bilinmiyor"


class SystemInfo(Tool):
    name = "system_info"
    description = "İşletim sistemi, CPU, RAM, diskler, GPU, ağ ve pil durumu."
    parameters = obj({})

    def run(self, ctx: ToolContext) -> ToolResult:
        vm = psutil.virtual_memory()
        rows = [
            f"OS: {platform.platform()} ({platform.machine()})",
            f"CPU: {platform.processor() or 'bilinmiyor'} | {psutil.cpu_count(logical=False)}C/{psutil.cpu_count()}T | "
            f"kullanım %{psutil.cpu_percent(interval=0.5)}",
            f"RAM: {vm.used / 2**30:.1f}/{vm.total / 2**30:.1f} GB (%{vm.percent})",
            f"GPU: {_gpu()}",
        ]
        for part in psutil.disk_partitions(all=False):
            try:
                u = psutil.disk_usage(part.mountpoint)
            except (PermissionError, OSError):
                continue
            rows.append(f"Disk {part.mountpoint}: {u.used / 2**30:.0f}/{u.total / 2**30:.0f} GB (%{u.percent}) {part.fstype}")
        bat = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
        if bat:
            rows.append(f"Pil: %{bat.percent:.0f} {'şarjda' if bat.power_plugged else 'pilde'}")
        net = psutil.net_io_counters()
        rows.append(f"Ağ: gönderilen {net.bytes_sent / 2**20:.0f} MB, alınan {net.bytes_recv / 2**20:.0f} MB")
        return ToolResult("\n".join(rows))


class ProcessList(Tool):
    name = "process_list"
    description = "Çalışan süreçleri listele (isim filtresi opsiyonel), CPU/RAM'e göre sıralı."
    parameters = obj({"name": {"type": "string"}, "sort": {"type": "string", "enum": ["cpu", "memory"]},
                      "limit": {"type": "integer"}})

    def run(self, ctx: ToolContext, name: str | None = None, sort: str = "memory", limit: int = 40) -> ToolResult:
        procs: list[dict[str, Any]] = []
        for p in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent"]):
            info = p.info
            if name and name.lower() not in (info["name"] or "").lower():
                continue
            procs.append(info)
        key = (lambda i: i["cpu_percent"] or 0) if sort == "cpu" else (lambda i: i["memory_info"].rss if i["memory_info"] else 0)
        procs.sort(key=key, reverse=True)
        rows = [f"{i['pid']:>7}  {(i['memory_info'].rss if i['memory_info'] else 0) / 2**20:>8.0f} MB  "
                f"%{i['cpu_percent'] or 0:>5.1f}  {i['name']}" for i in procs[:limit]]
        return ToolResult("    PID       RAM     CPU  İSİM\n" + "\n".join(rows) if rows else "süreç bulunamadı")


class ProcessKill(Tool):
    name = "process_kill"
    description = "Süreci PID ya da isimle sonlandır (isimle verilirse eşleşen hepsi)."
    parameters = obj({"pid": {"type": "integer"}, "name": {"type": "string"}})
    risk = EXEC

    def risk_for(self, args: dict[str, Any]) -> str:
        n = (args.get("name") or "").lower()
        return DANGER if n in PROTECTED or not (args.get("pid") or n) else EXEC

    def run(self, ctx: ToolContext, pid: int | None = None, name: str | None = None) -> ToolResult:
        targets = []
        if pid:
            targets = [psutil.Process(pid)]
        elif name:
            targets = [p for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == name.lower()]
        if not targets:
            raise ToolError("eşleşen süreç yok")
        done = []
        for p in targets:
            try:
                p.terminate()
                done.append(f"{p.pid}")
            except psutil.Error as e:
                done.append(f"{p.pid} (hata: {e})")
        psutil.wait_procs(targets, timeout=3)
        return ToolResult("sonlandırıldı: " + ", ".join(done))


TOOLS = [SystemInfo(), ProcessList(), ProcessKill()]
