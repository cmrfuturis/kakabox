"""Demo der LED-Zonen — visueller Sanity-Check und Spielwiese.

Aufruf (manuell, mit sudo damit GPIO/PIO-Zugriff geht):
    sudo /home/riffi/Dokumente/kakabox/device/.venv/bin/python \\
         -m tools.led_demo

oder ein einzelner Effekt:
    sudo .venv/bin/python -m tools.led_demo zones
    sudo .venv/bin/python -m tools.led_demo ring
    sudo .venv/bin/python -m tools.led_demo rainbow
    sudo .venv/bin/python -m tools.led_demo volume     # 0→100% Sweep
    sudo .venv/bin/python -m tools.led_demo speed      # Speed-Mode: lila Pulse
    sudo .venv/bin/python -m tools.led_demo off

Helligkeit ist hart auf 25% gekappt (siehe hardware/leds.py).
"""
from __future__ import annotations

import sys
import time

from hardware.leds import (
    LED_COUNT, ZONE_RING, ZONE_NFC, ZONE_STRIP_LEFT, ZONE_STRIP_RIGHT,
    Leds, LedsUnavailable,
)


def demo_zones(leds: Leds) -> None:
    """Jede Zone bekommt 1.5 s ihre eigene Farbe — sieht man sofort, ob die
    Reihenfolge in der Daisy-Chain wie erwartet ist."""
    print("Zonen-Demo: Ring rot → NFC grün → Streifen-L blau → Streifen-R gelb")
    leds.off()
    time.sleep(0.5)
    leds.ring((255, 0, 0));         time.sleep(1.5)
    leds.nfc((0, 255, 0));          time.sleep(1.5)
    leds.strip_left((0, 0, 255));   time.sleep(1.5)
    leds.strip_right((255, 200, 0)); time.sleep(1.5)


def demo_ring_spinner(leds: Leds) -> None:
    """Lauflicht im Ring — schöner Loading-Indikator (z.B. für Voice-PTT
    oder NFC-Scan-Wait)."""
    print("Ring-Spinner: 3 Umdrehungen lila")
    leds.off()
    ring_start = ZONE_RING.start
    ring_size = ZONE_RING.stop - ZONE_RING.start
    for _ in range(3):
        for pos in range(ring_size):
            leds.off()
            leds[ring_start + pos] = (180, 0, 200)
            leds.show()
            time.sleep(0.08)


def demo_rainbow(leds: Leds) -> None:
    """Regenbogen über die Streifen — kindgerecht. Wandert einmal komplett durch."""
    print("Rainbow auf den Streifen: 5 s")
    leds.off()
    start = ZONE_STRIP_LEFT.start
    end = ZONE_STRIP_RIGHT.stop
    span = end - start
    t0 = time.time()
    while time.time() - t0 < 5.0:
        offset = int((time.time() - t0) * 60) % 256
        for i in range(start, end):
            hue = (offset + int(255 * (i - start) / span)) % 256
            leds[i] = _wheel(hue)
        leds.show()
        time.sleep(0.03)


def demo_volume_sweep(leds: Leds) -> None:
    """Lautstaerke 0 → 100% durchsweepen, jede Stufe 0.4 s halten.

    Demonstriert die Volume-Visualisierung (Farbe + LED-Count). Nach dem
    letzten Schritt 5 s warten, damit man das Auto-Off live sieht.
    """
    print("Volume-Sweep: 0 → 100% in 8 Stufen, dann 5 s warten → Auto-Off")
    for percent in (0, 12, 25, 37, 50, 62, 75, 87, 100):
        print(f"  show_volume({percent}%)")
        leds.show_volume(percent)
        time.sleep(0.4)
    print("  warte 5.5 s — Ring sollte nach ~5 s ausgehen …")
    time.sleep(5.5)
    print("  Auto-Off vorbei, Ring sollte aus sein.")


def demo_speed(leds: Leds) -> None:
    """Speed-Mode-Visualisierung: lila pulsierend, 50%→100%→200% sweep.

    Zeigt: Pulse läuft kontinuierlich, LED-Count ändert sich mit dem Speed,
    hide_speed schaltet sauber ab.
    """
    print("Speed-Sweep: 0.5 → 1.0 → 1.5 → 2.0, je 2 s, Pulse die ganze Zeit")
    leds.show_speed(0.5)
    time.sleep(2.0)
    print("  speed=1.0 (Normal)")
    leds.show_speed(1.0)
    time.sleep(2.0)
    print("  speed=1.5")
    leds.show_speed(1.5)
    time.sleep(2.0)
    print("  speed=2.0 (Max)")
    leds.show_speed(2.0)
    time.sleep(2.0)
    print("  hide_speed → Ring aus, Pulse stoppt")
    leds.hide_speed()
    time.sleep(0.3)


def _wheel(pos: int) -> tuple[int, int, int]:
    """Hue-Wheel 0..255 → RGB (klassischer NeoPixel-Trick)."""
    if pos < 85:
        return (pos * 3, 255 - pos * 3, 0)
    if pos < 170:
        pos -= 85
        return (255 - pos * 3, 0, pos * 3)
    pos -= 170
    return (0, pos * 3, 255 - pos * 3)


def main() -> int:
    effect = sys.argv[1] if len(sys.argv) > 1 else "all"
    try:
        leds = Leds()
    except LedsUnavailable as e:
        print(f"LEDs nicht verfügbar: {e}", file=sys.stderr)
        return 1

    try:
        if effect in ("off",):
            leds.off()
            print(f"OK: alle {LED_COUNT} LEDs aus.")
        elif effect == "zones":
            demo_zones(leds)
        elif effect == "ring":
            demo_ring_spinner(leds)
        elif effect == "rainbow":
            demo_rainbow(leds)
        elif effect == "volume":
            demo_volume_sweep(leds)
        elif effect == "speed":
            demo_speed(leds)
        elif effect in ("all", "demo"):
            demo_zones(leds)
            demo_ring_spinner(leds)
            demo_rainbow(leds)
            demo_volume_sweep(leds)
            demo_speed(leds)
            leds.off()
            print("Demo fertig, LEDs aus.")
        else:
            print(f"Unbekannter Effekt: {effect}. Gültig: zones|ring|rainbow|volume|speed|off|all",
                  file=sys.stderr)
            return 2
    finally:
        # Bei manuellem Abbruch (Ctrl-C) trotzdem aus
        if effect != "off":
            pass  # nicht zwangsweise abschalten — User soll Resultat sehen
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
