"""Tests für voice.tts.TitleSpeaker — die engine-unabhängige Logik.

Piper/espeak selbst werden nicht aufgerufen: getestet werden Cache-Schlüssel,
Cache-Vorrang (Hit ohne Engine) und der Leertext-Kurzschluss. Die eigentliche
Synthese ist auf der Box per Smoke-Test verifiziert.
"""
from voice.tts import TitleSpeaker, _cache_key


def test_cache_key_stable_and_normalized():
    # Case- und Whitespace-insensitiv → derselbe Schlüssel.
    assert _cache_key("Bambi") == _cache_key("bambi")
    assert _cache_key("Die  Katze") == _cache_key("die katze")
    assert _cache_key(" Bambi ") == _cache_key("Bambi")
    # Unterschiedliche Titel → unterschiedliche Schlüssel.
    assert _cache_key("Bambi") != _cache_key("Dumbo")


def test_empty_text_returns_none(tmp_path):
    sp = TitleSpeaker(model_path=tmp_path / "fehlt.onnx", cache_dir=tmp_path)
    assert sp.synth_to_wav("") is None
    assert sp.synth_to_wav("   ") is None


def test_cache_hit_takes_precedence_over_engine(tmp_path):
    # Modell existiert NICHT — ein Cache-Hit muss trotzdem sofort greifen,
    # ohne Piper/espeak zu bemühen.
    sp = TitleSpeaker(model_path=tmp_path / "fehlt.onnx", cache_dir=tmp_path)
    key = _cache_key("Bambi")
    cached = tmp_path / f"{key}.wav"
    cached.write_bytes(b"RIFF-fake-wav")
    assert sp.synth_to_wav("Bambi") == cached
    # Normalisierung: andere Schreibweise trifft denselben Cache-Eintrag.
    assert sp.synth_to_wav("  bambi ") == cached


def test_missing_engine_and_no_cache_returns_none(tmp_path, monkeypatch):
    # Kein Piper-Modell und espeak künstlich "weg" → None statt Crash.
    sp = TitleSpeaker(model_path=tmp_path / "fehlt.onnx", cache_dir=tmp_path)

    def _no_espeak(text):
        return None

    monkeypatch.setattr(sp, "_espeak_to_tmp", _no_espeak)
    assert sp.synth_to_wav("Etwas Unbekanntes") is None
