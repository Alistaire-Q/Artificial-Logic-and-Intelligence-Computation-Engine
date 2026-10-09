"""Bildirim kanalları.

Notifier her bildirimi önce mod politikasından geçirir: geçemeyen "susturuldu" olarak
kaydedilir (sonradan "ben oyundayken ne oldu?" diye sorulabilir), geçen her kanala gider.
Kanallar: masaüstü bildirimi (Windows toast / macOS / notify-send) ve eklenen her
callable (sistem tepsisi, ileride ALICE'in sesi).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable

from alice.core.modes import Policy
from alice.core.store import Store


@dataclass
class Notification:
    title: str
    body: str = ""
    severity: str = "info"  # info | warn | critical
    topic: str = ""         # ör. "flight", "pc", "task", "health"
    kind: str = "alert"     # alert | task | mode | info | flight
    emotion: str = ""       # boşsa ses, önem ve konudan tonu kendisi seçer


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def desktop_notify(n: Notification) -> bool:
    """İşletim sisteminin bildirim balonunu göster. Başarısızsa False."""
    try:
        if sys.platform == "win32":
            script = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime]"
                " | Out-Null;"
                "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
                "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
                f"$x=$t.GetElementsByTagName('text');$x.Item(0).InnerText={_ps_quote(n.title)};"
                f"$x.Item(1).InnerText={_ps_quote(n.body)};"
                "$app='{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show("
                "[Windows.UI.Notifications.ToastNotification]::new($t))"
            )
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            r = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, timeout=15,
                               creationflags=flags)
            return r.returncode == 0
        if sys.platform == "darwin":
            r = subprocess.run(["osascript", "-e", f"display notification {n.body!r} with title {n.title!r}"],
                               capture_output=True, timeout=10)
            return r.returncode == 0
        if shutil.which("notify-send"):
            urgency = {"critical": "critical", "warn": "normal"}.get(n.severity, "low")
            r = subprocess.run(["notify-send", "-u", urgency, "-a", "ALICE", n.title, n.body], capture_output=True,
                               timeout=10)
            return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    return False


class Notifier:
    def __init__(self, store: Store, policy: Callable[[], Policy], desktop: bool = True):
        self.store = store
        self.policy = policy
        self.channels: list[Callable[[Notification, Policy], None]] = []
        if desktop:
            self.channels.append(lambda n, p: desktop_notify(n))

    def send(self, n: Notification) -> bool:
        """Bildirimi gönder. Mod politikası susturduysa False."""
        pol = self.policy()
        ok = pol.allows(n.severity, n.topic)
        self.store.add_event(n.kind, n.title, n.body, n.severity, n.topic, delivered=ok)
        if not ok:
            return False
        for ch in self.channels:
            try:
                ch(n, pol)
            except Exception:  # bir kanal çökse de diğerleri çalışsın
                pass
        return True
