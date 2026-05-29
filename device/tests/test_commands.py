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
