"""Tests für quiet_hours_active() — H5-Fix (QS-Audit 2026-07-07).

Reine Funktion, keine Hardware/Kakabox-Instanz nötig.
"""
from datetime import datetime

from main import quiet_hours_active

ALL_DAYS = ["mo", "di", "mi", "do", "fr", "sa", "so"]


def _window(start, end, days=ALL_DAYS):
    return {"start_time": start, "end_time": end, "days": days}


def test_no_quiet_hours_configured_is_never_active():
    assert quiet_hours_active([], datetime(2026, 7, 7, 23, 0)) is False


def test_overnight_window_active_in_evening():
    # Montag 21:00, Fenster 20:00-07:00 jede Nacht.
    now = datetime(2026, 7, 6, 21, 0)  # 2026-07-06 ist ein Montag
    assert quiet_hours_active([_window("20:00", "07:00")], now) is True


def test_overnight_window_active_early_morning_tail_of_previous_day():
    # Dienstag 06:00 — gehoert noch zum Montagabend-Fenster (20:00-07:00).
    now = datetime(2026, 7, 7, 6, 0)  # Dienstag
    assert quiet_hours_active([_window("20:00", "07:00")], now) is True


def test_overnight_window_inactive_at_midday():
    now = datetime(2026, 7, 7, 12, 0)  # Dienstag Mittag
    assert quiet_hours_active([_window("20:00", "07:00")], now) is False


def test_overnight_window_respects_days_for_evening_side():
    # Fenster nur an "sa" konfiguriert — Montagabend darf nicht matchen.
    now = datetime(2026, 7, 6, 21, 0)  # Montag
    assert quiet_hours_active([_window("20:00", "07:00", days=["sa"])], now) is False


def test_overnight_window_respects_days_for_morning_tail():
    # Fenster nur an "mo" (Montag) konfiguriert — der Dienstagmorgen-Tail
    # gehoert zum Montagabend-Start, muss also trotzdem matchen.
    now = datetime(2026, 7, 7, 6, 0)  # Dienstag frueh
    assert quiet_hours_active([_window("20:00", "07:00", days=["mo"])], now) is True
    # Aber am Mittwochmorgen (Tail von Dienstagabend, nicht konfiguriert) nicht.
    now2 = datetime(2026, 7, 8, 6, 0)  # Mittwoch frueh
    assert quiet_hours_active([_window("20:00", "07:00", days=["mo"])], now2) is False


def test_same_day_window_active_inside_range():
    now = datetime(2026, 7, 6, 14, 0)  # Montag 14:00
    assert quiet_hours_active([_window("13:00", "15:00")], now) is True


def test_same_day_window_inactive_outside_range():
    now = datetime(2026, 7, 6, 16, 0)  # Montag 16:00
    assert quiet_hours_active([_window("13:00", "15:00")], now) is False


def test_multiple_windows_any_match_wins():
    windows = [_window("13:00", "15:00"), _window("20:00", "07:00")]
    assert quiet_hours_active(windows, datetime(2026, 7, 6, 21, 0)) is True
    assert quiet_hours_active(windows, datetime(2026, 7, 6, 14, 0)) is True
    assert quiet_hours_active(windows, datetime(2026, 7, 6, 17, 0)) is False


def test_malformed_window_is_ignored_not_crashing():
    bad = {"start_time": None, "end_time": "07:00", "days": ALL_DAYS}
    assert quiet_hours_active([bad], datetime(2026, 7, 6, 21, 0)) is False
