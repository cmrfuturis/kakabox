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
    cached = sp._cache_path("Bambi")
    cached.write_bytes(b"RIFF-fake-wav")
    assert sp.synth_to_wav("Bambi") == cached
    # Normalisierung: andere Schreibweise trifft denselben Cache-Eintrag.
    assert sp.synth_to_wav("  bambi ") == cached


def test_cache_is_per_voice(tmp_path):
    # Verschiedene Stimmen (Modelle) → verschiedene Cache-Dateien, sodass ein
    # Stimmwechsel nie eine Ansage in der alten Stimme liefert.
    male = TitleSpeaker(model_path=tmp_path / "de_DE-thorsten-medium.onnx", cache_dir=tmp_path)
    female = TitleSpeaker(model_path=tmp_path / "de_DE-kerstin-low.onnx", cache_dir=tmp_path)
    assert male._cache_path("Bambi") != female._cache_path("Bambi")
    assert "thorsten" in male._cache_path("Bambi").name
    assert "kerstin" in female._cache_path("Bambi").name


def test_set_model_switches_voice_and_resets(tmp_path):
    sp = TitleSpeaker(model_path=tmp_path / "de_DE-thorsten-medium.onnx", cache_dir=tmp_path)
    before = sp._cache_path("Bambi")
    sp.set_model(tmp_path / "de_DE-kerstin-low.onnx")
    after = sp._cache_path("Bambi")
    assert before != after
    assert sp._piper_failed is False
    assert sp._voice is None


def test_write_path_follows_loaded_model_not_current(tmp_path, monkeypatch):
    # Cache-Poisoning-Schutz: synthetisiert eine Stimme, die NICHT der aktuellen
    # _model_path entspricht (simuliert parallelen set_model), und prüft, dass die
    # WAV unter dem Dateinamen des TATSÄCHLICH geladenen Modells landet.
    sp = TitleSpeaker(model_path=tmp_path / "de_DE-thorsten-medium.onnx", cache_dir=tmp_path)

    class FakeVoice:
        def synthesize_wav(self, text, w):
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(22050)
            w.writeframes(b"\x00\x00" * 100)

    loaded_path = tmp_path / "de_DE-kerstin-low.onnx"
    monkeypatch.setattr(sp, "_load_piper", lambda: (FakeVoice(), loaded_path))

    out = sp.synth_to_wav("Bambi")
    assert out is not None
    # Dateiname folgt dem GELADENEN Modell (kerstin), nicht self._model_path (thorsten).
    assert "kerstin" in out.name
    assert "thorsten" not in out.name


def test_missing_engine_and_no_cache_returns_none(tmp_path, monkeypatch):
    # Kein Piper-Modell und espeak künstlich "weg" → None statt Crash.
    sp = TitleSpeaker(model_path=tmp_path / "fehlt.onnx", cache_dir=tmp_path)

    def _no_espeak(text):
        return None

    monkeypatch.setattr(sp, "_espeak_to_tmp", _no_espeak)
    assert sp.synth_to_wav("Etwas Unbekanntes") is None
