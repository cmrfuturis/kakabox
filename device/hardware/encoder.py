"""
AS5600 magnetic rotary encoder — volume knob.
I2C address: 0x36
Reads 12-bit angle (0–4095 = 0–360°) and maps rotation delta to volume changes.
"""
import time
import logging
import smbus2

logger = logging.getLogger(__name__)

_I2C_BUS = 1
_ADDRESS = 0x36
_REG_ANGLE_H = 0x0E   # processed angle high byte (with hysteresis + zero position)
_REG_ANGLE_L = 0x0F   # processed angle low byte

_MAX_ANGLE = 4096      # 12-bit resolution
_HALF = _MAX_ANGLE // 2


class Encoder:
    def __init__(self, bus: int = _I2C_BUS):
        self._bus = smbus2.SMBus(bus)
        self._last_angle = self._read_angle()
        logger.info("AS5600 encoder ready, initial angle: %d", self._last_angle)

    def _read_angle(self) -> int:
        high = self._bus.read_byte_data(_ADDRESS, _REG_ANGLE_H)
        low = self._bus.read_byte_data(_ADDRESS, _REG_ANGLE_L)
        return ((high & 0x0F) << 8) | low

    def read_delta(self) -> int:
        """
        Return the angular delta since last call, accounting for wrap-around.
        Positive = clockwise, negative = counter-clockwise.
        Range: roughly -2048 to +2048 per call.
        """
        current = self._read_angle()
        delta = current - self._last_angle

        # Handle wrap-around at 0/4095 boundary
        if delta > _HALF:
            delta -= _MAX_ANGLE
        elif delta < -_HALF:
            delta += _MAX_ANGLE

        self._last_angle = current
        return delta

    def volume_delta(self, sensitivity: float = 0.05) -> int:
        """
        Convert rotation into a volume change (-100 to +100 scale).
        sensitivity: how many degrees per 1% volume change (lower = more sensitive).
        Returns an integer volume delta (e.g. +3, -2).
        """
        delta = self.read_delta()
        return int(delta * sensitivity)

    def close(self) -> None:
        self._bus.close()
