"""GPIO-Buttons der Kakabox.

Verdrahtung (KY-Module + GND, Pi 5):
  - GPIO16  = Grün  (Track restart / Track-zurück)
  - GPIO25  = Rot   (Track vor)
  - GPIO22  = Encoder-Push (Pause/Play-Toggle)

Alle Buttons sind gegen GND verdrahtet, interner Pull-up — gedrückt = LOW.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from gpiozero import Button as GpioButton, Device
from gpiozero.pins.lgpio import LGPIOFactory

# Pi 5 nutzt den RP1-Chip → lgpio ist die zuverlässige Schnittstelle.
Device.pin_factory = LGPIOFactory()

logger = logging.getLogger(__name__)

GREEN_PIN = 16
RED_PIN = 25
ENCODER_PUSH_PIN = 22

DEBOUNCE_S = 0.05  # 50 ms entprellt typische Taster sauber


class Buttons:
    def __init__(self) -> None:
        self.green = GpioButton(GREEN_PIN, pull_up=True, bounce_time=DEBOUNCE_S)
        self.red = GpioButton(RED_PIN, pull_up=True, bounce_time=DEBOUNCE_S)
        self.push = GpioButton(ENCODER_PUSH_PIN, pull_up=True, bounce_time=DEBOUNCE_S)
        logger.info(
            "Buttons ready: green=GPIO%d red=GPIO%d push=GPIO%d",
            GREEN_PIN, RED_PIN, ENCODER_PUSH_PIN,
        )

    def on_green(self, callback: Callable[[], None]) -> None:
        self.green.when_pressed = callback

    def on_red(self, callback: Callable[[], None]) -> None:
        self.red.when_pressed = callback

    def on_push(self, callback: Callable[[], None]) -> None:
        self.push.when_pressed = callback

    def close(self) -> None:
        for b in (self.green, self.red, self.push):
            try:
                b.close()
            except Exception:
                pass
