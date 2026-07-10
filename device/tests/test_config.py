"""Tests für load_config()/save_config() — atomarer Write + Korruptions-Fallback.

Deckt den P0-Fix aus dem QS-Audit vom 2026-07-07 ab (main.py:278-291 vorher:
nicht-atomarer write_text + ungefangenes json.loads → Boot-Crash-Loop bei
Brownout während des Schreibens).
"""
import json
import os
import stat
import threading

import main


def test_save_config_leaves_no_tmp_file_behind(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)

    main.save_config({"volume": 42})

    assert config_path.is_file()
    # Kein zurückgelassenes tmp-Fragment (mkstemp-Namen sind zufällig).
    assert list(tmp_path.glob("config.json.*.tmp")) == []
    assert json.loads(config_path.read_text()) == {"volume": 42}


def test_save_config_sets_owner_only_permissions(tmp_path, monkeypatch):
    """config.json trägt den api_token im Klartext → 0600, nicht world-readable
    (QS-Finding F3)."""
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)

    main.save_config({"api_token": "geheim", "volume": 1})

    mode = stat.S_IMODE(os.stat(config_path).st_mode)
    assert mode == 0o600, oct(mode)


def test_save_config_concurrent_writes_do_not_crash(tmp_path, monkeypatch):
    """Regression F10: zwei Threads schrieben denselben ".tmp"-Pfad → der
    zweite replace() warf FileNotFoundError und killte den Sync-Thread. Mit
    Lock + eindeutigem tmp muss paralleles Schreiben sauber durchlaufen."""
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)

    errors = []

    def writer(vol):
        try:
            for _ in range(20):
                main.save_config({"volume": vol})
        except Exception as e:  # pragma: no cover - nur bei Regression
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(v,)) for v in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    # Datei ist valides JSON (kein halber Write), Ergebnis eines der Writer.
    assert json.loads(config_path.read_text())["volume"] in range(6)
    assert list(tmp_path.glob("config.json.*.tmp")) == []


def test_load_config_round_trips_saved_config(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)

    main.save_config({"volume": 55, "tags": {"abc123": 7}})

    assert main.load_config() == {"volume": 55, "tags": {"abc123": 7}}


def test_load_config_without_file_returns_defaults(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)

    config = main.load_config()

    assert config["volume"] == 70
    assert config["tags"] == {}


def test_load_config_recovers_from_corrupted_file(tmp_path, monkeypatch):
    """Simuliert einen Brownout mitten in save_config(): die Datei existiert,
    ist aber kein valides JSON. load_config() darf nicht crashen (Boot-Crash-
    Loop), sondern muss die kaputte Datei beiseiteschieben und mit Defaults
    weiterlaufen."""
    config_path = tmp_path / "config.json"
    config_path.write_text('{"volume": 42, "tags": {')  # abgeschnitten
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)

    config = main.load_config()

    assert config["volume"] == 70  # Default, nicht der kaputte Wert
    assert not config_path.exists()  # kaputte Datei wurde umbenannt
    broken = list(tmp_path.glob("config.json.broken-*"))
    assert len(broken) == 1
    assert broken[0].read_text() == '{"volume": 42, "tags": {'
