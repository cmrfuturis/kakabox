"""Eval-Harness für die Voice-Pipeline (ASR-Plan 2026-07-07, Stufe 0).

Fährt gelabelte Voice-Samples durch exakt dieselbe Kette wie die Box
(``transcribe_wav`` → ``route_transcript``) und misst die
**Kommando-Erfolgsrate** (korrekt gematchter Intent + Song, Ende-zu-Ende —
bewusst nicht WER), die False-Positive-Rate (falsches Lied gestartet) und
die ASR-Latenz p50/p95.

Samples entstehen im Betrieb mit ``voice.keep_samples: true`` in config.json
(landen unter device/voice_samples/ mit Sidecar-JSON). Zum Labeln in der
JSON ergänzen:

    "ground_truth_text": "spiele superkind",     # was wirklich gesagt wurde
    "expected_action":   "play",                 # play|random|title_question|no_match
    "expected_song_id":  "123",                  # Candidate.id (nur bei play)
    "alter": "4-6",                              # optional: 4-6 | 7-8
    "distanz": "1-2m"                            # optional: 0.5-1m | 1-2m | >2m

Aufruf (Benchmarks nur mit Governor performance/ondemand, s. ASR-Plan):

    .venv/bin/python -m tools.eval_voice --samples voice_samples \\
        --catalog voice_catalog.json
    # Modell-/Parameter-Varianten vergleichen:
    .venv/bin/python -m tools.eval_voice --samples voice_samples \\
        --catalog eval/catalog-frozen.json \\
        --model /usr/share/kakabox/voice/ggml-base.bin --beam-size 5

Der Katalog sollte für Vergleichsläufe über Wochen EINGEFROREN werden
(Kopie neben das Testset legen) — der Sync ändert voice_catalog.json laufend.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voice.asr import Recognizer, VoiceUnavailable  # noqa: E402
from voice.catalog import build_catalog_from_file  # noqa: E402
from voice.router import route_transcript  # noqa: E402


_INSTALLED_TINY = Path("/usr/share/kakabox/voice/ggml-tiny.bin")


def _default_whisper_model() -> Path | None:
    """Modellpfad, den die BOX real fährt — nicht der asr.py-Default
    (ggml-base.bin, auf dem Pi nicht installiert). Erst config.json, dann das
    installierte tiny. So misst der Harness ohne --model exakt das Prod-Modell.
    """
    cfg_path = Path(__file__).resolve().parent.parent / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        model = (cfg.get("voice", {}).get("whisper", {}) or {}).get("model_path")
        if model and Path(model).is_file():
            return Path(model)
    except (OSError, ValueError):
        pass
    return _INSTALLED_TINY if _INSTALLED_TINY.is_file() else None


def _load_labeled_samples(samples_dir: Path) -> list[dict]:
    out = []
    for meta_path in sorted(samples_dir.glob("*.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"  ! {meta_path.name}: unlesbar ({e}) — übersprungen")
            continue
        wav = meta_path.with_suffix(".wav")
        if not wav.is_file():
            continue
        if "expected_action" not in meta:
            print(f"  ! {meta_path.name}: kein Label (expected_action) — übersprungen")
            continue
        meta["_wav"] = wav
        out.append(meta)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--samples", required=True, type=Path,
                    help="Verzeichnis mit sample-*.wav + gelabelter sample-*.json")
    ap.add_argument("--catalog", required=True, type=Path,
                    help="voice_catalog.json (für Vergleichsläufe: eingefrorene Kopie!)")
    ap.add_argument("--model", type=Path, default=None,
                    help="Whisper-ggml-Modellpfad. Default: der Pfad aus "
                         "config.json (voice.whisper.model_path), sonst das "
                         "installierte tiny-Modell — NICHT der asr.py-Default "
                         "(ggml-base.bin, auf der Box nicht installiert).")
    ap.add_argument("--backend", default="whisper", choices=["whisper", "vosk"])
    ap.add_argument("--beam-size", type=int, default=5, help="0 = greedy")
    ap.add_argument("--no-dynamic-audio-ctx", action="store_true",
                    help="volles 30-s-Fenster statt audio_ctx (A/B-Vergleich)")
    ap.add_argument("--min-segment-probability", type=float, default=0.4)
    ap.add_argument("--zauberwort", action="store_true",
                    help="Routing mit aktivem Zauberwort-Modus bewerten")
    args = ap.parse_args()

    catalog = build_catalog_from_file(args.catalog)
    if not catalog:
        print(f"FEHLER: Katalog leer/unlesbar: {args.catalog}")
        return 2
    samples = _load_labeled_samples(args.samples)
    if not samples:
        print(f"FEHLER: keine gelabelten Samples unter {args.samples} "
              "(JSONs brauchen 'expected_action')")
        return 2

    backend_kwargs: dict = {}
    if args.backend == "whisper":
        backend_kwargs = {
            "beam_size": args.beam_size,
            "dynamic_audio_ctx": not args.no_dynamic_audio_ctx,
            "min_segment_probability": args.min_segment_probability,
        }
        model = args.model or _default_whisper_model()
        if model is None:
            print("FEHLER: kein Whisper-Modell gefunden (weder --model, noch "
                  "config.json, noch das installierte tiny). Mit --model angeben.")
            return 2
        backend_kwargs["model_path"] = model
        print(f"Modell: {model}")
    recognizer = Recognizer(backend=args.backend, **backend_kwargs)

    print(f"{len(samples)} gelabelte Samples · Katalog: {len(catalog)} Candidates "
          f"· Backend: {args.backend} {args.model or '(prod-Modell)'}")
    print()

    latencies: list[float] = []
    ok, wrong_song, missed, false_positive = 0, 0, 0, 0
    by_group: dict[str, list[bool]] = defaultdict(list)

    for meta in samples:
        expected_action = meta["expected_action"]
        expected_song = str(meta.get("expected_song_id", "")) or None

        t0 = time.monotonic()
        try:
            text = recognizer.transcribe_wav(meta["_wav"])
        except VoiceUnavailable as e:
            print(f"FEHLER: ASR nicht verfügbar: {e}")
            return 2
        latency = time.monotonic() - t0
        latencies.append(latency)

        route = route_transcript(text, catalog, zauberwort_enabled=args.zauberwort)
        got_action = route.action if route.action != "hallucination" else "no_match"
        got_song = route.command.target.id if route.command else None

        success = got_action == expected_action and (
            expected_action != "play" or expected_song in (None, got_song)
        )
        if success:
            ok += 1
            marker = "✓"
        elif expected_action == "play" and got_action == "play":
            wrong_song += 1
            marker = "✗ falsches Lied"
        elif expected_action == "no_match" and got_action == "play":
            false_positive += 1
            marker = "✗ FALSE POSITIVE"
        else:
            missed += 1
            marker = f"✗ {got_action} statt {expected_action}"

        for key in ("alter", "distanz"):
            if meta.get(key):
                by_group[f"{key}={meta[key]}"].append(success)

        gt = meta.get("ground_truth_text", "?")
        print(f"  {marker:>18s} | {latency:5.2f}s | «{text}» (gesagt: «{gt}»)"
              + (f" → {route.command.target.name} (score {route.command.score:.2f},"
                 f" margin {route.command.margin:.2f})" if route.command else ""))

    n = len(samples)
    print()
    print(f"Kommando-Erfolgsrate: {ok}/{n} = {ok / n * 100:.0f}%")
    print(f"  falsches Lied: {wrong_song} · verpasst/falsche Aktion: {missed}"
          f" · False Positives: {false_positive}")
    if latencies:
        p50 = statistics.median(latencies)
        p95 = sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)]
        print(f"ASR-Latenz: p50 {p50:.2f}s · p95 {p95:.2f}s")
    for group, results in sorted(by_group.items()):
        rate = sum(results) / len(results) * 100
        print(f"  {group}: {sum(results)}/{len(results)} = {rate:.0f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
