"""KY-040 Rotary Encoder via GPIO.

Verdrahtung:
  - GPIO17 = CLK
  - GPIO27 = DT
  (Push-Funktion wird in buttons.py separat behandelt — GPIO22.)

Quadratur-Decoding via gpiozero.RotaryEncoder. Eine Detent (= 1 Klick) löst
einmal den jeweiligen Callback aus.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from gpiozero import RotaryEncoder as GpioRotary, Device
from gpiozero.pins.lgpio import LGPIOFactory

Device.pin_factory = LGPIOFactory()

logger = logging.getLogger(__name__)

CLK_PIN = 17
DT_PIN = 27


class Encoder:
    def __init__(self) -> None:
        # max_steps=0 → keine Wertbegrenzung, wir tracken Richtung selbst.
        # bounce_time klein, damit schnelle Drehs nicht verloren gehen.
        self._enc = GpioRotary(CLK_PIN, DT_PIN, max_steps=0, bounce_time=0.001)
        self._on_cw: Optional[Callable[[], None]] = None
        self._on_ccw: Optional[Callable[[], None]] = None
        self._enc.when_rotated_clockwise = self._cw
        self._enc.when_rotated_counter_clockwise = self._ccw
        logger.info("Rotary encoder ready: CLK=GPIO%d DT=GPIO%d", CLK_PIN, DT_PIN)

    def on_clockwise(self, callback: Callable[[], None]) -> None:
        self._on_cw = callback

    def on_counterclockwise(self, callback: Callable[[], None]) -> None:
        self._on_ccw = callback

    def _cw(self) -> None:
        if self._on_cw:
            self._on_cw()

    def _ccw(self) -> None:
        if self._on_ccw:
            self._on_ccw()

    def close(self) -> None:
        try:
            self._enc.close()
        except Exception:
            pass
