"""Tests für Button Hold-Detection (Blau 2s für KI-Modus)."""
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from hardware.buttons import Buttons, BLUE_HOLD_SECONDS


class TestButtonHoldDetection:
    """Blue button hold-time detection für KI-Mode Activation."""

    def test_blue_button_has_hold_time_set(self):
        """Blau-Button sollte hold_time = BLUE_HOLD_SECONDS im Constructor haben."""
        with patch("hardware.buttons.GpioButton") as MockButton:
            buttons = Buttons()
            # Verifiziere dass GpioButton mit hold_time=BLUE_HOLD_SECONDS aufgerufen wurde
            mock_call = [c for c in MockButton.call_args_list if c[0][0] == 5][0]
            assert mock_call[1].get("hold_time") == BLUE_HOLD_SECONDS
            assert BLUE_HOLD_SECONDS == 2.0

    def test_blue_button_registers_held_callback(self):
        """on_blue_held() registriert einen Callback."""
        with patch("hardware.buttons.GpioButton"):
            buttons = Buttons()
            callback = MagicMock()
            buttons.on_blue_held(callback)
            assert buttons._blue_held_cb is callback

    def test_blue_held_callback_fires_on_hold(self):
        """Wenn hold_time überschritten wird, feuert on_blue_held."""
        with patch("hardware.buttons.GpioButton") as MockButton:
            buttons = Buttons()
            callback = MagicMock()
            buttons.on_blue_held(callback)

            # Simuliere Hold → wenn gpiozero's when_held feuert, setzen wir _blue_was_held
            buttons._on_blue_internal_held()
            # Release mit held-Flag → feuert blue_held_cb
            buttons._on_blue_internal_released()

            callback.assert_called_once()

    def test_blue_press_callback_fires_on_release_without_hold(self):
        """Short press (< hold_time) feuert on_blue (press), nicht on_blue_held."""
        with patch("hardware.buttons.GpioButton"):
            buttons = Buttons()
            press_cb = MagicMock()
            held_cb = MagicMock()
            buttons.on_blue(press_cb)
            buttons.on_blue_held(held_cb)

            # Nur _on_blue_internal_released ohne _on_blue_internal_held
            buttons._on_blue_internal_released()

            press_cb.assert_called_once()
            held_cb.assert_not_called()

    def test_blue_held_excludes_press_callback(self):
        """Hold feuert nur on_blue_held, NICHT on_blue (press)."""
        with patch("hardware.buttons.GpioButton"):
            buttons = Buttons()
            press_cb = MagicMock()
            held_cb = MagicMock()
            buttons.on_blue(press_cb)
            buttons.on_blue_held(held_cb)

            # Simuliere hold
            buttons._on_blue_internal_held()
            buttons._on_blue_internal_released()

            press_cb.assert_not_called()
            held_cb.assert_called_once()

    def test_blue_callback_exception_doesnt_crash(self):
        """Exception im Callback sollte geloggt, nicht gethrowt werden."""
        with patch("hardware.buttons.GpioButton"):
            buttons = Buttons()
            bad_callback = MagicMock(side_effect=ValueError("oops"))
            buttons.on_blue_held(bad_callback)

            # Sollte nicht crashen
            buttons._on_blue_internal_held()
            buttons._on_blue_internal_released()

            bad_callback.assert_called_once()


class TestButtonHoldStates:
    """Stelle sicher, dass Flags korrekt gesetzt/zurückgesetzt werden."""

    def test_blue_was_held_flag_reset_after_release(self):
        """_blue_was_held sollte nach Release zurückgesetzt sein."""
        with patch("hardware.buttons.GpioButton"):
            buttons = Buttons()
            assert buttons._blue_was_held is False

            buttons._on_blue_internal_held()
            assert buttons._blue_was_held is True

            buttons._on_blue_internal_released()
            assert buttons._blue_was_held is False

    def test_multiple_rapid_holds(self):
        """Mehrere Holds hintereinander sollten alle funktionieren."""
        with patch("hardware.buttons.GpioButton"):
            buttons = Buttons()
            callback = MagicMock()
            buttons.on_blue_held(callback)

            for _ in range(3):
                buttons._on_blue_internal_held()
                buttons._on_blue_internal_released()

            assert callback.call_count == 3
