"""Ekran durumu: öndeki oyun özel tam ekranda mı? O zaman hiçbir pencere (HUD dahil) oyunun üstüne çizilemez."""

from __future__ import annotations

import sys

QUNS_RUNNING_D3D_FULL_SCREEN = 3  # kenarlıksız pencere QUNS_BUSY (2) döner, o HUD için sorun değil


def exclusive_fullscreen() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        state = ctypes.c_int()
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return False
        return state.value == QUNS_RUNNING_D3D_FULL_SCREEN
    except Exception:  # noqa: BLE001 - bilinmiyorsa uyarma
        return False
