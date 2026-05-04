"""GPIO-Buttons der Kakabox.

Verdrahtung (KY-Module + GND, Pi 5):
  - GPIO16  = Grün  (Track restart / Track-zurück)
  - GPIO25  = Rot   (Track vor — kurzer Druck; WLAN-Reset bei Halten ≥ 10s)
  - GPIO22  = Encoder-Push (Pause/Play-Toggle)

Alle Buttons sind gegen GND verdrahtet, interner Pull-up — gedrückt = LOW.

Roter Knopf hat eine Doppelfunktion:
  - kurz drücken (loslassen vor 10s) → Track-Vor-Aktion (on_red)
  - 10s halten → on_red_held (z. B. WLAN-Reset)
gpiozero feuert when_held nach dem Hold-Timeout. Wir merken uns intern, ob
held aktiv war, und triggern die normale Press-Aktion erst in when_released
NUR wenn nicht gehalten wurde.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from gpiozero import Button as GpioButton, Device
from gpiozero.pins.lgpio import LGPIOFactory

Device.pin_factory = LGPIOFactory()

logger = logging.getLogger(__name__)

GREEN_PIN = 16
RED_PIN = 25
ENCODER_PUSH_PIN = 22

DEBOUNCE_S = 0.05
RED_HOLD_SECONDS = 10.0


class Buttons:
    def __init__(self) -> None:
        self.green = GpioButton(GREEN_PIN, pull_up=True, bounce_time=DEBOUNCE_S)
        self.red = GpioButton(
            RED_PIN, pull_up=True, bounce_time=DEBOUNCE_S, hold_time=RED_HOLD_SECONDS
        )
        self.push = GpioButton(ENCODER_PUSH_PIN, pull_up=True, bounce_time=DEBOUNCE_S)

        # Doppelfunktion roter Knopf
        self._red_press_cb: Optional[Callable[[], None]] = None
        self._red_held_cb: Optional[Callable[[], None]] = None
        self._red_was_held: bool = False
        self.red.when_held = self._on_red_internal_held
        self.red.when_released = self._on_red_internal_released

        logger.info(
            "Buttons ready: green=GPIO%d red=GPIO%d push=GPIO%d (red hold ≥ %ds = special)",
            GREEN_PIN, RED_PIN, ENCODER_PUSH_PIN, int(RED_HOLD_SECONDS),
        )

    def on_green(self, callback: Callable[[], None]) -> None:
        self.green.when_pressed = callback

    def on_red(self, callback: Callable[[], None]) -> None:
        """Kurzer Druck (vor dem Hold-Timeout losgelassen)."""
        self._red_press_cb = callback

    def on_red_held(self, callback: Callable[[], None]) -> None:
        """Hold ≥ RED_HOLD_SECONDS — feuert SOFORT beim Erreichen des Timeouts."""
        self._red_held_cb = callback

    def on_push(self, callback: Callable[[], None]) -> None:
        self.push.when_pressed = callback

    # ---- Internals -------------------------------------------------------

    def _on_red_internal_held(self) -> None:
        self._red_was_held = True
        if self._red_held_cb:
            try:
                self._red_held_cb()
            except Exception as e:
                logger.exception("on_red_held callback failed: %s", e)

    def _on_red_internal_released(self) -> None:
        # Nur normale Press-Aktion auslösen, wenn nicht gehalten wurde
        was_held = self._red_was_held
        self._red_was_held = False
        if was_held:
            return
        if self._red_press_cb:
            try:
                self._red_press_cb()
            except Exception as e:
                logger.exception("on_red callback failed: %s", e)

    def close(self) -> None:
        for b in (self.green, self.red, self.push):
            try:
                b.close()
            except Exception:
                pass
