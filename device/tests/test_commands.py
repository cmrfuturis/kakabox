"""Tests für den Webapp-Command-Poll/Dispatch (#18) und _set_volume.

Kakabox.__init__ braucht Hardware — wir umgehen es mit object.__new__ und
setzen nur die Attribute, die der jeweilige Pfad anfasst.
"""
import threading

import main


class FakeCmdBackend:
    def __init__(self, commands):
        self._commands = commands
        self.is_connected = True
        self.acked: list[int] = []

    def fetch_commands(self):
        return self._commands

    def acknowledge_command(self, cmd_id):
        self.acked.append(cmd_id)
        return True


def _bare_box() -> "main.Kakabox":
    box = object.__new__(main.Kakabox)
    box._standby = False  # _poll_commands/_send_heartbeat prüfen das Flag
    box._spotify = None   # _adjust_volume prüft den Spotify-Chip-Zustand
    box._spotify_active = False
    return box


def test_poll_commands_dispatches_and_acks(monkeypatch):
    cmds = [
        {"id": 1, "command": "set_volume", "payload": {"volume": 42}},
        {"id": 2, "command": "set_mode", "payload": {"mode": "nacht"}},
        {"id": 3, "command": "sync_audio", "payload": {}},
        {"id": 4, "command": "refresh_settings",
         "payload": {"max_volume": 60, "enable_zauberwort": True, "mode": "abend"}},
        {"id": 5, "command": "finder", "payload": {}},
        {"id": 6, "command": "bogus_unknown", "payload": {}},
    ]
    backend = FakeCmdBackend(cmds)
    box = _bare_box()
    box.backend = backend

    calls: list[tuple] = []
    finder_ran = threading.Event()
    monkeypatch.setattr(box, "_set_volume", lambda v: calls.append(("vol", v)))
    monkeypatch.setattr(box, "_set_mode", lambda m: calls.append(("mode", m)))
    monkeypatch.setattr(box, "_trigger_background_sync", lambda r: calls.append(("sync", r)))
    monkeypatch.setattr(box, "_apply_rule_from_manifest", lambda rule: calls.append(("rule", rule)))
    monkeypatch.setattr(box, "_play_finder", lambda: finder_ran.set())

    box._poll_commands()

    # Alle Commands ge-ack't — auch der unbekannte (kein Poison-Pill in der Queue).
    assert backend.acked == [1, 2, 3, 4, 5, 6]
    assert ("vol", 42) in calls
    assert ("mode", "nacht") in calls            # set_mode
    assert ("sync", "command") in calls
    assert ("rule", {"max_volume": 60, "enable_zauberwort": True}) in calls
    assert ("mode", "abend") in calls            # refresh_settings.mode
    assert finder_ran.wait(timeout=1.0)          # finder läuft im Thread


def test_refresh_settings_forwards_quiet_hours_and_blocked_categories(monkeypatch):
    """QS-Finding A2: quiet_hours + blocked_category_ids aus dem Push-Payload
    müssen SOFORT über den Rule-Pfad angewandt werden (nicht erst beim nächsten
    5-Min-Sync). Der Server schickt sie im refresh_settings-Command mit."""
    qh = [{"start_time": "20:00", "end_time": "07:00", "days": ["mo"]}]
    cmds = [{
        "id": 1, "command": "refresh_settings",
        "payload": {
            "max_volume": 55, "enable_zauberwort": False,
            "quiet_hours": qh, "blocked_category_ids": [4, 9],
        },
    }]
    box = _bare_box()
    box.backend = FakeCmdBackend(cmds)

    applied: list[dict] = []
    monkeypatch.setattr(box, "_apply_rule_from_manifest", lambda rule: applied.append(rule))
    monkeypatch.setattr(box, "_apply_tts_voice", lambda v: None)

    box._poll_commands()

    assert len(applied) == 1
    rule = applied[0]
    assert rule["quiet_hours"] == qh
    assert rule["blocked_category_ids"] == [4, 9]
    assert rule["max_volume"] == 55


def test_refresh_settings_omits_absent_rule_fields(monkeypatch):
    """Fehlen quiet_hours/blocked_category_ids im Payload (älterer Server),
    dürfen sie NICHT als leere Liste durchgereicht werden — sonst würde
    _apply_rule_from_manifest den lokalen Stand fälschlich auf "leer" setzen."""
    cmds = [{
        "id": 1, "command": "refresh_settings",
        "payload": {"max_volume": 55, "enable_zauberwort": False},
    }]
    box = _bare_box()
    box.backend = FakeCmdBackend(cmds)

    applied: list[dict] = []
    monkeypatch.setattr(box, "_apply_rule_from_manifest", lambda rule: applied.append(rule))
    monkeypatch.setattr(box, "_apply_tts_voice", lambda v: None)

    box._poll_commands()

    assert "quiet_hours" not in applied[0]
    assert "blocked_category_ids" not in applied[0]


def test_poll_commands_skips_when_disconnected():
    backend = FakeCmdBackend([{"id": 1, "command": "finder", "payload": {}}])
    backend.is_connected = False
    box = _bare_box()
    box.backend = backend
    box._poll_commands()
    assert backend.acked == []


def test_set_volume_absolute_respects_cap():
    box = _bare_box()
    box._volume = 50
    box._max_volume = 70
    box.leds = None
    box.config = {}

    played: list[int] = []

    class _Player:
        def set_volume(self, v):
            played.append(v)

    box.player = _Player()

    box._set_volume(90)          # über Cap → auf 70 geklemmt
    assert box._volume == 70
    assert played[-1] == 70

    box._set_volume(30)
    assert box._volume == 30
    assert played[-1] == 30
