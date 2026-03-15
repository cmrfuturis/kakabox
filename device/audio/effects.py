"""
Encoder-driven audio effects via mpv.

Three modes cycled by WAVE gesture:
  SPEED   — changes playback tempo, pitch stays constant (rubberband)
  PITCH   — shifts pitch up/down, tempo stays constant (rubberband)
  VINYL   — changes both speed and pitch together (turntable/record effect)

Encoder center position = no effect. Values reset to normal on mode switch.
"""
import logging
from enum import Enum, auto
import mpv

logger = logging.getLogger(__name__)

# Effect parameter ranges
_SPEED_MIN, _SPEED_MAX, _SPEED_NORMAL = 0.4, 2.5, 1.0
_PITCH_MIN, _PITCH_MAX, _PITCH_NORMAL = 0.5, 2.0, 1.0   # semitone scale factor
_VINYL_MIN, _VINYL_MAX, _VINYL_NORMAL = 0.4, 2.5, 1.0

# How much each encoder step changes the parameter
_SPEED_STEP = 0.02
_PITCH_STEP = 0.02
_VINYL_STEP = 0.03


class EffectMode(Enum):
    SPEED = auto()
    PITCH = auto()
    VINYL = auto()


_MODE_NAMES = {
    EffectMode.SPEED: "SPEED  (tempo, pitch fixed)",
    EffectMode.PITCH: "PITCH  (tone, tempo fixed)",
    EffectMode.VINYL: "VINYL  (turntable — speed + pitch)",
}

_MODE_CYCLE = [EffectMode.SPEED, EffectMode.PITCH, EffectMode.VINYL]


class AudioEffects:
    def __init__(self, player: mpv.MPV):
        self._mpv = player
        self._mode = EffectMode.SPEED
        self._speed = _SPEED_NORMAL
        self._pitch = _PITCH_NORMAL
        self._vinyl = _VINYL_NORMAL
        self._apply()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def mode(self) -> EffectMode:
        return self._mode

    def next_mode(self) -> EffectMode:
        """Cycle to next effect mode and reset effect to normal."""
        idx = (_MODE_CYCLE.index(self._mode) + 1) % len(_MODE_CYCLE)
        self._mode = _MODE_CYCLE[idx]
        self.reset()
        logger.info("Effect mode: %s", _MODE_NAMES[self._mode])
        return self._mode

    def apply_delta(self, delta: int) -> None:
        """
        Apply encoder rotation delta to the current effect.
        delta > 0 = clockwise, delta < 0 = counter-clockwise.
        """
        if abs(delta) < 2:   # deadband — filter jitter
            return

        if self._mode == EffectMode.SPEED:
            self._speed = _clamp(
                self._speed + delta * _SPEED_STEP,
                _SPEED_MIN, _SPEED_MAX,
            )
        elif self._mode == EffectMode.PITCH:
            self._pitch = _clamp(
                self._pitch + delta * _PITCH_STEP,
                _PITCH_MIN, _PITCH_MAX,
            )
        elif self._mode == EffectMode.VINYL:
            self._vinyl = _clamp(
                self._vinyl + delta * _VINYL_STEP,
                _VINYL_MIN, _VINYL_MAX,
            )

        self._apply()

    def reset(self) -> None:
        """Reset all effects to normal."""
        self._speed = _SPEED_NORMAL
        self._pitch = _PITCH_NORMAL
        self._vinyl = _VINYL_NORMAL
        self._apply()

    def status(self) -> dict:
        return {
            "mode": self._mode.name,
            "speed": round(self._speed, 2),
            "pitch": round(self._pitch, 2),
            "vinyl": round(self._vinyl, 2),
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _apply(self) -> None:
        if self._mode == EffectMode.SPEED:
            # mpv built-in pitch correction — faster/slower, pitch stays constant
            self._mpv.command("af", "set", "")
            self._mpv["audio-pitch-correction"] = True
            self._mpv.speed = self._speed

        elif self._mode == EffectMode.PITCH:
            # lavfi rubberband: shift pitch only, speed stays at 1.0
            self._mpv["audio-pitch-correction"] = False
            self._mpv.speed = 1.0
            self._mpv.command("af", "set", f"lavfi=[rubberband=pitch={self._pitch:.3f}]")

        elif self._mode == EffectMode.VINYL:
            # No filter — speed change with pitch correction OFF → turntable effect
            self._mpv.command("af", "set", "")
            self._mpv["audio-pitch-correction"] = False
            self._mpv.speed = self._vinyl

        logger.debug("Effect applied: %s", self.status())


def _clamp(value: float, min_val: float, max_val: float) -> float:
    return max(min_val, min(max_val, value))
