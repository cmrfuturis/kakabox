"""GPIO-Buttons der Kakabox.

Verdrahtung (KY-Module + GND, Pi 5):
  - GPIO16  = Grün         (Track-zurück — kurzer Druck;
                             STOP bei Halten ≥ 1s; Power-Off bei Halten ≥ 5s)
  - GPIO25  = Rot          (Track-vor   — kurzer Druck;
                             STOP bei Halten ≥ 1s; WLAN-Reset bei Halten ≥ 5s)
  - GPIO22  = Encoder-Push (kurz: Speed-Mode-Burst 4× in 3s; Hold ≥ 1s: Random-Modus)
  - GPIO5   = Blau         (Voice-Push-to-Talk — single-press;
                             Hold ≥ 2s: KI-Konversations-Modus)
  - GPIO24  = Gelb         (kurz: Pause/Play-Toggle; Hold ≥ 3s: LED-Streifen
                             toggeln, Musik pausiert während Hold)

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
PUSH_HOLD_SECONDS = 1.0   # Encoder-Push ≥ 1s = Random-Modus an/neu starten
YELLOW_HOLD_SECONDS = 3.0 # Gelb ≥ 3s = LED-Streifen toggeln + Musik pause während Hold
BLUE_HOLD_SECONDS = 2.0   # Blau ≥ 2s = KI-Konversations-Modus (statt single-press Voice)


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
            hold_time=PUSH_HOLD_SECONDS,
        )
        self.yellow = GpioButton(
            YELLOW_PIN, pull_up=True, bounce_time=DEBOUNCE_S,
            hold_time=YELLOW_HOLD_SECONDS,
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

        # Encoder-Push: zwei Stufen — kurz (Burst-Counter macht main.py) vs.
        # Hold ≥ PUSH_HOLD_SECONDS (Random-Modus an / neu starten). Beim
        # Release entscheidet ein Flag, ob "press" oder "held" gefeuert wird,
        # damit Hold nicht zusätzlich Press triggert (gleiches Schema wie
        # grün/rot, nur ohne zweite "Held"-Stufe).
        self._push_press_cb: Optional[Callable[[], None]] = None
        self._push_held_cb: Optional[Callable[[], None]] = None
        self._push_was_held: bool = False
        self.push.when_held = self._on_push_internal_held
        self.push.when_released = self._on_push_internal_released

        # Gelb: dreiphasig (press/held/released). Beim Press feuert ein Hook,
        # damit main.py sofort pausieren kann (User-Wunsch: pause-während-Hold).
        # Bei Release entscheidet ``_yellow_was_held`` welcher Callback feuert.
        self._yellow_press_cb: Optional[Callable[[], None]] = None
        self._yellow_held_cb: Optional[Callable[[], None]] = None
        self._yellow_down_cb: Optional[Callable[[], None]] = None
        self._yellow_was_held: bool = False
        self.yellow.when_pressed = self._on_yellow_internal_down
        self.yellow.when_held = self._on_yellow_internal_held
        self.yellow.when_released = self._on_yellow_internal_released

        # Blau: single-press → Voice-Mode, Hold ≥ 2s → KI-Modus.
        self._blue_press_cb: Optional[Callable[[], None]] = None
        self._blue_held_cb: Optional[Callable[[], None]] = None
        self._blue_was_held: bool = False
        self.blue.when_pressed = self._on_blue_internal_down
        self.blue.when_held = self._on_blue_internal_held
        self.blue.when_released = self._on_blue_internal_released
        # gpiozero's when_held feuert nach hold_time; wir setzen das auf BLUE_HOLD_SECONDS.
        self.blue.hold_time = BLUE_HOLD_SECONDS

        logger.info(
            "Buttons ready: green=GPIO%d red=GPIO%d push=GPIO%d yellow=GPIO%d blue=GPIO%d "
            "(stop ≥ %.0fs, long ≥ %.0fs, push-hold ≥ %.0fs, yellow-hold ≥ %.0fs, blue-hold ≥ %.0fs)",
            GREEN_PIN, RED_PIN, ENCODER_PUSH_PIN, YELLOW_PIN, BLUE_PIN,
            STOP_HOLD_SECONDS, HOLD_SECONDS, PUSH_HOLD_SECONDS, YELLOW_HOLD_SECONDS, BLUE_HOLD_SECONDS,
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
        """Kurzer Encoder-Push (< PUSH_HOLD_SECONDS). main.py bündelt Bursts."""
        self._push_press_cb = callback

    def on_push_held(self, callback: Callable[[], None]) -> None:
        """Encoder-Push ≥ PUSH_HOLD_SECONDS — z.B. Random-Modus starten."""
        self._push_held_cb = callback

    def on_yellow(self, callback: Callable[[], None]) -> None:
        """Gelb — kurzer Druck (< YELLOW_HOLD_SECONDS), bei Release gefeuert."""
        self._yellow_press_cb = callback

    def on_yellow_held(self, callback: Callable[[], None]) -> None:
        """Gelb — Hold ≥ YELLOW_HOLD_SECONDS, bei Release nach Hold gefeuert."""
        self._yellow_held_cb = callback

    def on_yellow_down(self, callback: Callable[[], None]) -> None:
        """Gelb — feuert SOFORT beim Drücken (vor jedem Hold/Release).
        Nützlich für "pause-while-pressed"-Logik in main.py."""
        self._yellow_down_cb = callback

    def on_blue(self, callback: Callable[[], None]) -> None:
        """Blau — Voice-Push-to-Talk (Aufnahme, single-press)."""
        self._blue_press_cb = callback

    def on_blue_held(self, callback: Callable[[], None]) -> None:
        """Blau — Hold ≥ BLUE_HOLD_SECONDS (2s), KI-Konversations-Modus."""
        self._blue_held_cb = callback

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

    def _on_push_internal_held(self) -> None:
        """Hold-Schwelle (PUSH_HOLD_SECONDS) erreicht. Feuert sofort beim
        Überschreiten und merkt sich, dass dieser Druck kein "press" mehr
        ist — beim Loslassen wird der press-Callback dann übersprungen."""
        self._push_was_held = True
        if self._push_held_cb:
            try:
                self._push_held_cb()
            except Exception as e:
                logger.exception("on_push_held callback failed: %s", e)

    def _on_push_internal_released(self) -> None:
        was_held = self._push_was_held
        self._push_was_held = False
        if was_held:
            return
        if self._push_press_cb:
            try:
                self._push_press_cb()
            except Exception as e:
                logger.exception("on_push callback failed: %s", e)

    def _on_yellow_internal_down(self) -> None:
        """Sofort bei Press — vor any Hold-Decision. Snapshot-Hook für main.py."""
        if self._yellow_down_cb:
            try:
                self._yellow_down_cb()
            except Exception as e:
                logger.exception("on_yellow_down callback failed: %s", e)

    def _on_yellow_internal_held(self) -> None:
        """Hold-Schwelle (YELLOW_HOLD_SECONDS) erreicht — markieren."""
        self._yellow_was_held = True

    def _on_yellow_internal_released(self) -> None:
        was_held = self._yellow_was_held
        self._yellow_was_held = False
        cb = self._yellow_held_cb if was_held else self._yellow_press_cb
        if cb:
            try:
                cb()
            except Exception as e:
                logger.exception(
                    "on_yellow %s callback failed: %s",
                    "held" if was_held else "press", e,
                )

    def _on_blue_internal_down(self) -> None:
        """Sofort bei Press — wird ignoriert wenn Hold kommt."""
        pass  # Entscheidung erfolgt im _on_blue_internal_released

    def _on_blue_internal_held(self) -> None:
        """Hold-Schwelle (BLUE_HOLD_SECONDS=2s) erreicht — KI-Modus starten."""
        self._blue_was_held = True

    def _on_blue_internal_released(self) -> None:
        was_held = self._blue_was_held
        self._blue_was_held = False
        cb = self._blue_held_cb if was_held else self._blue_press_cb
        if cb:
            try:
                cb()
            except Exception as e:
                logger.exception(
                    "on_blue %s callback failed: %s",
                    "held" if was_held else "press", e,
                )

    def close(self) -> None:
        for b in (self.green, self.red, self.push, self.yellow, self.blue):
            try:
                b.close()
            except Exception:
                pass
