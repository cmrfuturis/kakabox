"""CLI für Trockentests — ohne Mikrofon nutzbar.

Beispiele:
    # Reine Intent-Erkennung gegen die lokale Library
    .venv/bin/python -m voice "spiele bitte das Album Bambi"

    # Mit ASR über eine vorbereitete WAV-Datei (16 kHz mono)
    .venv/bin/python -m voice --wav samples/spiel-bambi.wav

    # Eigener Catalog aus Datei (ein Eintrag pro Zeile, "id|name")
    .venv/bin/python -m voice "spiele bibi" --catalog-file kakas.txt
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Die importierende Stelle muss device/ im sys.path haben — Aufruf via
# ``python -m voice`` aus device/ heraus funktioniert.
from audio.library import scan
from voice.catalog import build_catalog_from_file
from voice.intent import Candidate, parse_play_command

# Vom Hauptloop nach jedem Audio-Sync geschrieben (siehe main._write_voice_catalog).
# Enthält Titel + Aliase aller Backend-Songs. Wenn nicht vorhanden (z.B. noch
# nie online gesynct), fällt das CLI auf die lokale Library zurück.
_BACKEND_CATALOG = Path(__file__).resolve().parent.parent / "voice_catalog.json"


def _build_album_catalog() -> list[Candidate]:
    lib = scan()
    return [
        Candidate(id=a.id, name=a.name, kind="album")
        for a in lib.albums
    ]


def _build_default_catalog() -> list[Candidate]:
    """Lokale Library + Backend-Songs (mit Aliasen + Artist-Candidates) zusammen.

    Beides nebeneinander, damit auch ein offline-gebootetes Setup mit lokal
    abgelegten MP3s funktioniert, und ein normal verbundenes Setup zusätzlich
    Alias-Aufrufe + Künstler-Match der Webapp-Songs versteht.
    """
    return _build_album_catalog() + build_catalog_from_file(_BACKEND_CATALOG)


def _load_catalog_file(path: Path) -> list[Candidate]:
    """Format pro Zeile: ``id|name`` (kind = 'album' default).

    Beispiel:
        bibi-blocksberg|Bibi Blocksberg
        bambi|Bambi
        dschungelbuch|Das Dschungelbuch
    """
    out: list[Candidate] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|", 1)
        if len(parts) != 2:
            continue
        out.append(Candidate(id=parts[0], name=parts[1], kind="album"))
    return out


def main() -> int:
    p = argparse.ArgumentParser(prog="voice")
    p.add_argument("text", nargs="?", help="Direkt-Text statt WAV (Trockentest)")
    p.add_argument("--wav", type=Path, help="WAV-Datei (16 kHz mono) für ASR-Test")
    p.add_argument(
        "--catalog-file",
        type=Path,
        help="Eigener Catalog ('id|name' pro Zeile) statt der lokalen Library",
    )
    p.add_argument(
        "--threshold",
        type=float,
        default=0.55,
        help="Match-Score-Schwelle (0..1; niedriger = toleranter)",
    )
    p.add_argument(
        "--grammar",
        action="store_true",
        help=(
            "Vosk auf das Catalog-Vokabular beschränken. "
            "Nur sinnvoll, wenn alle Titel-Wörter im DE-Sprachmodell sind — "
            "bei Eigennamen wie 'DIKKA' werden Wörter ignoriert."
        ),
    )
    args = p.parse_args()

    catalog = (
        _load_catalog_file(args.catalog_file)
        if args.catalog_file
        else _build_default_catalog()
    )
    if not catalog:
        print(
            "⚠ Catalog ist leer. Entweder lokale Library hat 0 Alben "
            "oder --catalog-file zeigt auf eine leere Datei.",
            file=sys.stderr,
        )

    if args.wav:
        from voice.asr import Recognizer, VoiceUnavailable
        rec = Recognizer()
        grammar = [c.name for c in catalog] if (args.grammar and catalog) else None
        try:
            text = rec.transcribe_wav(args.wav, grammar=grammar)
        except VoiceUnavailable as e:
            print(f"ASR nicht verfügbar: {e}", file=sys.stderr)
            return 2
        print(f"Transkribiert: «{text}»")
    elif args.text:
        text = args.text
    else:
        p.error("entweder text-Argument oder --wav nötig")

    cmd = parse_play_command(text, catalog, threshold=args.threshold)
    if cmd is None:
        print("Kein Match. (kein Play-Verb erkannt oder Score zu niedrig)")
        return 1

    print(f"Spiele: {cmd.target.name}  ({cmd.target.kind} id={cmd.target.id})")
    print(f"  Match-Score : {cmd.score:.2f}")
    print(f"  Query nach Stripping: «{cmd.query}»")
    return 0


if __name__ == "__main__":
    sys.exit(main())
