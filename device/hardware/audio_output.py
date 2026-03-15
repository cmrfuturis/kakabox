"""
MAX98357A volume control via ALSA PCM mixer.
The MAX98357A has no I2C control — volume is handled entirely in software.
"""
import subprocess
import logging

logger = logging.getLogger(__name__)

# MAX98357A has no hardware mixer — volume is controlled via the software
# PCM control on card 0 (ALSA default).
_CARD = "0"
_CONTROL = "PCM"


def set_volume(percent: int) -> None:
    """Set system ALSA volume (0–100)."""
    percent = max(0, min(100, percent))
    subprocess.run(
        ["amixer", "-c", _CARD, "sset", _CONTROL, f"{percent}%"],
        check=True, capture_output=True,
    )
    logger.debug("Volume set to %d%%", percent)


def get_volume() -> int:
    """Return current ALSA volume (0–100)."""
    result = subprocess.run(
        ["amixer", "-c", _CARD, "sget", _CONTROL],
        check=True, capture_output=True, text=True,
    )
    for line in result.stdout.splitlines():
        if "%" in line:
            start = line.index("[") + 1
            end = line.index("%")
            return int(line[start:end])
    return 0


def mute() -> None:
    subprocess.run(
        ["amixer", "-c", _CARD, "sset", _CONTROL, "mute"],
        check=True, capture_output=True,
    )


def unmute() -> None:
    subprocess.run(
        ["amixer", "-c", _CARD, "sset", _CONTROL, "unmute"],
        check=True, capture_output=True,
    )
