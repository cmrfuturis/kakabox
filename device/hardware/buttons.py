"""GPIO-Buttons der Kakabox.

Verdrahtung (KY-Module + GND, Pi 5):
  - GPIO16  = Grün         (Track-zurück — kurzer Druck;
                             STOP bei Halten ≥ 1s; Power-Off bei Halten ≥ 5s)
  - GPIO25  = Rot          (Track-vor   — kurzer Druck;
                             STOP bei Halten ≥ 1s; WLAN-Reset bei Halten ≥ 5s)
  - GPIO22  = Encoder-Push (nur noch Speed-Mode-Burst: 4× drücken in 3s)
  - GPIO5   = Blau         (Voice-Push-to-Talk — single-press)
  - GPIO24  = Gelb         (Pause/Play-Toggle — single-press)

Alle Buttons sind gegen GND verdrahtet, interner Pull-up — gedrückt = LOW.

Grün und Rot haben drei Stufen: kurz (< 1s) → press, ≥ 1s → stop, ≥ 5s → held.
``when_held`` (von gpiozero auf 1s gesetzt) feuert die Stop-Stage; ein internes
``threading.Timer`` wartet weitere 4s und feuert die Held-Stage, falls der
Button noch gedrückt ist. Beim Release entscheidet ein Flag, ob Press, Stop
oder Held bereits triggerte — und unterdrückt Press in den anderen Fällen.

Gelb, Blau und Encoder-Push sind single-press (kein Hold-Verhalten relevant).
Encoder-Push hat keinen eigenen Callback mehr — main.py wertet die Sequenz
selbst aus (4× in 3s während Wiedergabe → Speed-Mode).
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from gpiozero import Button as GpioButton, Device
from gpiozero.pins.lgpio import LGPIOFactory

Device.pin_factory = LGPIOFactory()

logger = logging.getLogger(__name__)

GREEN_PIN = 16
RED_PIN = 25
ENCODER_PUSH_PIN = 22
BLUE_PIN = 5        # Voice-Push-to-Talk (Aufnahme)
YELLOW_PIN = 24     # Pause/Play-Toggle

DEBOUNCE_S = 0.05
STOP_HOLD_SECONDS = 1.0   # grün/rot ≥ 1s = STOP (Playlist + Memory weg)
HOLD_SECONDS = 5.0        # grün ≥ 5s = Power-Off; rot ≥ 5s = WLAN-Reset


class Buttons:
    def __init__(self) -> None:
        # gpiozero's hold_time = unsere kürzeste Stop-Stufe; die längere Held-
        # Stufe (10s) machen wir selbst per Timer.
        self.green = GpioButton(
            GREEN_PIN, pull_up=True, bounce_time=DEBOUNCE_S, hold_time=STOP_HOLD_SECONDS
        )
        self.red = GpioButton(
            RED_PIN, pull_up=True, bounce_time=DEBOUNCE_S, hold_time=STOP_HOLD_SECONDS
        )
        self.push = GpioButton(
            ENCODER_PUSH_PIN, pull_up=True, bounce_time=DEBOUNCE_S,
        )
        self.yellow = GpioButton(
            YELLOW_PIN, pull_up=True, bounce_time=DEBOUNCE_S,
        )
        self.blue = GpioButton(
            BLUE_PIN, pull_up=True, bounce_time=DEBOUNCE_S,
        )

        # Drei-Stufen-Funktion grün (press / stop ≥1s / held ≥10s)
        self._green_press_cb: Optional[Callable[[], None]] = None
        self._green_stop_cb: Optional[Callable[[], None]] = None
        self._green_held_cb: Optional[Callable[[], None]] = None
        self._green_was_stop: bool = False
        self._green_was_held: bool = False
        self._green_long_timer: Optional[threading.Timer] = None
        self.green.when_held = self._on_green_stop_reached
        self.green.when_released = self._on_green_released

        # Drei-Stufen-Funktion rot
        self._red_press_cb: Optional[Callable[[], None]] = None
        self._red_stop_cb: Optional[Callable[[], None]] = None
        self._red_held_cb: Optional[Callable[[], None]] = None
        self._red_was_stop: bool = False
        self._red_was_held: bool = False
        self._red_long_timer: Optional[threading.Timer] = None
        self.red.when_held = self._on_red_stop_reached
        self.red.when_released = self._on_red_released

        # Encoder-Push: single-press → main.py macht den Burst-Counter selbst.
        self._push_press_cb: Optional[Callable[[], None]] = None
        self.push.when_pressed = self._on_push_internal_pressed

        # Gelb + Blau: single-press, kein Hold.
        self._yellow_press_cb: Optional[Callable[[], None]] = None
        self._blue_press_cb: Optional[Callable[[], None]] = None
        self.yellow.when_pressed = self._on_yellow_internal_pressed
        self.blue.when_pressed = self._on_blue_internal_pressed

        logger.info(
            "Buttons ready: green=GPIO%d red=GPIO%d push=GPIO%d yellow=GPIO%d blue=GPIO%d "
            "(stop ≥ %.0fs, long ≥ %.0fs)",
            GREEN_PIN, RED_PIN, ENCODER_PUSH_PIN, YELLOW_PIN, BLUE_PIN,
            STOP_HOLD_SECONDS, HOLD_SECONDS,
        )

    def on_green(self, callback: Callable[[], None]) -> None:
        """Kurzer Druck (< STOP_HOLD_SECONDS)."""
        self._green_press_cb = callback

    def on_green_stop(self, callback: Callable[[], None]) -> None:
        """Hold ≥ STOP_HOLD_SECONDS — feuert sofort beim Erreichen."""
        self._green_stop_cb = callback

    def on_green_held(self, callback: Callable[[], None]) -> None:
        """Hold ≥ HOLD_SECONDS — feuert sofort beim Erreichen (zusätzlich zu Stop)."""
        self._green_held_cb = callback

    def on_red(self, callback: Callable[[], None]) -> None:
        """Kurzer Druck (< STOP_HOLD_SECONDS)."""
        self._red_press_cb = callback

    def on_red_stop(self, callback: Callable[[], None]) -> None:
        """Hold ≥ STOP_HOLD_SECONDS — feuert sofort beim Erreichen."""
        self._red_stop_cb = callback

    def on_red_held(self, callback: Callable[[], None]) -> None:
        """Hold ≥ HOLD_SECONDS — feuert sofort beim Erreichen (zusätzlich zu Stop)."""
        self._red_held_cb = callback

    def on_push(self, callback: Callable[[], None]) -> None:
        """Encoder-Push — feuert bei jedem Druck. main.py bündelt Bursts selbst."""
        self._push_press_cb = callback

    def on_yellow(self, callback: Callable[[], None]) -> None:
        """Gelb — Pause/Play-Toggle."""
        self._yellow_press_cb = callback

    def on_blue(self, callback: Callable[[], None]) -> None:
        """Blau — Voice-Push-to-Talk (Aufnahme)."""
        self._blue_press_cb = callback

    # ---- Internals -------------------------------------------------------

    def _on_green_stop_reached(self) -> None:
        """Stage 1: ≥ STOP_HOLD_SECONDS erreicht → Stop-Callback + Timer für Stage 2."""
        self._green_was_stop = True
        if self._green_stop_cb:
            try:
                self._green_stop_cb()
            except Exception as e:
                logger.exception("on_green_stop callback failed: %s", e)
        extra = HOLD_SECONDS - STOP_HOLD_SECONDS
        self._green_long_timer = threading.Timer(extra, self._on_green_held_reached)
        self._green_long_timer.daemon = True
        self._green_long_timer.start()

    def _on_green_held_reached(self) -> None:
        """Stage 2: Timer abgelaufen; feuert Held-Callback nur, wenn noch gedrückt."""
        if not self.green.is_pressed:
            return
        self._green_was_held = True
        if self._green_held_cb:
            try:
                self._green_held_cb()
            except Exception as e:
                logger.exception("on_green_held callback failed: %s", e)

    def _on_green_released(self) -> None:
        timer = self._green_long_timer
        self._green_long_timer = None
        if timer is not None:
            timer.cancel()
        was_stop = self._green_was_stop
        was_held = self._green_was_held
        self._green_was_stop = False
        self._green_was_held = False
        if was_stop or was_held:
            return
        if self._green_press_cb:
            try:
                self._green_press_cb()
            except Exception as e:
                logger.exception("on_green callback failed: %s", e)

    def _on_red_stop_reached(self) -> None:
        """Stage 1: ≥ STOP_HOLD_SECONDS erreicht → Stop-Callback + Timer für Stage 2."""
        self._red_was_stop = True
        if self._red_stop_cb:
            try:
                self._red_stop_cb()
            except Exception as e:
                logger.exception("on_red_stop callback failed: %s", e)
        extra = HOLD_SECONDS - STOP_HOLD_SECONDS
        self._red_long_timer = threading.Timer(extra, self._on_red_held_reached)
        self._red_long_timer.daemon = True
        self._red_long_timer.start()

    def _on_red_held_reached(self) -> None:
        if not self.red.is_pressed:
            return
        self._red_was_held = True
        if self._red_held_cb:
            try:
                self._red_held_cb()
            except Exception as e:
                logger.exception("on_red_held callback failed: %s", e)

    def _on_red_released(self) -> None:
        timer = self._red_long_timer
        self._red_long_timer = None
        if timer is not None:
            timer.cancel()
        was_stop = self._red_was_stop
        was_held = self._red_was_held
        self._red_was_stop = False
        self._red_was_held = False
        if was_stop or was_held:
            return
        if self._red_press_cb:
            try:
                self._red_press_cb()
            except Exception as e:
                logger.exception("on_red callback failed: %s", e)

    def _on_push_internal_pressed(self) -> None:
        if self._push_press_cb:
            try:
                self._push_press_cb()
            except Exception as e:
                logger.exception("on_push callback failed: %s", e)

    def _on_yellow_internal_pressed(self) -> None:
        if self._yellow_press_cb:
            try:
                self._yellow_press_cb()
            except Exception as e:
                logger.exception("on_yellow callback failed: %s", e)

    def _on_blue_internal_pressed(self) -> None:
        if self._blue_press_cb:
            try:
                self._blue_press_cb()
            except Exception as e:
                logger.exception("on_blue callback failed: %s", e)

    def close(self) -> None:
        for b in (self.green, self.red, self.push, self.yellow, self.blue):
            try:
                b.close()
            except Exception:
                pass
