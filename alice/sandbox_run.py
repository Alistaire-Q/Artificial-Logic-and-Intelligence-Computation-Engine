"""Windows sandbox yardımcısı: `python -m alice.sandbox_run -- <komut...>`.

Kendi bütünlük seviyesini Low'a düşürür, sonra komutu çalıştırır (çocuk süreç Low seviyeyi devralır) ve onun çıkış
kodunu döndürür. Seviye düşürülemezse komutu ÇALIŞTIRMAZ (126): sandbox istenmişse sandbox'sız çalışmak yanlış olur.
"""

from __future__ import annotations

import subprocess
import sys


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        print("sandbox: komut yok", file=sys.stderr)
        return 2
    try:
        from alice.sandbox import lower_integrity

        lower_integrity()
    except Exception as e:  # noqa: BLE001
        print(f"sandbox kurulamadı, komut çalıştırılmadı: {e}", file=sys.stderr)
        return 126
    # Çıktı açıkça aktarılır: subprocess.call ile konsolsuz (CREATE_NO_WINDOW) çocuk bizim boru uçlarımızı
    # devralmıyordu; kum havuzundaki her komutun (ALICE'in yazdığı araçların testleri dahil) çıktısı kayboluyor,
    # model yalnızca "çıkış kodu 1 (çıktı yok)" görüp hatanın sebebini bilemiyordu.
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         creationflags=0x08000000 if sys.platform == "win32" else 0)

    def pump(src, dst) -> None:
        for chunk in iter(lambda: src.read1(65536) if hasattr(src, "read1") else src.read(65536), b""):
            dst.write(chunk)
            dst.flush()

    import threading

    threads = [threading.Thread(target=pump, args=(p.stdout, sys.stdout.buffer), daemon=True),
               threading.Thread(target=pump, args=(p.stderr, sys.stderr.buffer), daemon=True)]
    for t in threads:
        t.start()
    code = p.wait()
    for t in threads:
        t.join(5)
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
