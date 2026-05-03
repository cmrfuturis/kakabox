#!/usr/bin/env python3
"""Reset-Watcher für die Kakabox.

Beobachtet den roten Knopf (GPIO25). Wird er ≥ 10 Sekunden ohne
Unterbrechung gedrückt, werden alle gespeicherten WLAN-Verbindungen
gelöscht und das Gerät neu gestartet — danach kommt comitup im
Hotspot-Modus hoch und der Nutzer kann ein neues WLAN einrichten.

Läuft als systemd-Service (kakabox-reset.service) parallel zur Box.
Bewusst getrennt vom main.py, damit Reset auch dann möglich ist, wenn
main.py crasht oder noch nicht gestartet hat.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time

from gpiozero import Button, Device
from gpiozero.pins.lgpio import LGPIOFactory

Device.pin_factory = LGPIOFactory()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s reset-watcher: %(message)s",
)
log = logging.getLogger(__name__)

RED_PIN = 25
HOLD_SECONDS = 10.0


def nuke_wifi() -> None:
    """Alle WLAN-Profile in NetworkManager löschen + Reboot."""
    log.warning("RESET-Trigger: lösche WLAN-Profile, reboote.")
    try:
        # Liste der Connection-UUIDs vom Type 802-11-wireless
        out = subprocess.run(
            ["nmcli", "-t", "-f", "TYPE,UUID", "connection", "show"],
            capture_output=True, text=True, check=True, timeout=5,
        )
        for line in out.stdout.strip().splitlines():
            if not line.startswith("802-11-wireless:"):
                continue
            uuid = line.split(":", 1)[1]
            log.info("nmcli connection delete %s", uuid)
            subprocess.run(["nmcli", "connection", "delete", uuid], check=False, timeout=5)
    except subprocess.SubprocessError as e:
        log.error("nmcli-Aufruf fehlgeschlagen: %s", e)
    finally:
        # Reboot — comitup startet danach im Hotspot-Modus
        subprocess.run(["systemctl", "reboot"], check=False)


def main() -> int:
    log.info("Watcher gestartet — Roter Knopf (GPIO%d) %ds halten löst Reset aus.",
             RED_PIN, int(HOLD_SECONDS))
    btn = Button(RED_PIN, pull_up=True, hold_time=HOLD_SECONDS)
    btn.when_held = nuke_wifi

    while True:
        time.sleep(60)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log.info("Beendet.")
        sys.exit(0)
