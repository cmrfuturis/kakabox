"""
PAJ7620U2 gesture recognition sensor.
I2C address: 0x73
Detects: up, down, left, right, forward, backward, clockwise, counter-clockwise, wave.
"""
import time
import logging
import smbus2
from enum import IntFlag

logger = logging.getLogger(__name__)

_I2C_BUS = 1
_ADDRESS = 0x73

# Bank select register
_REG_BANK_SEL = 0xEF
_BANK0 = 0x00
_BANK1 = 0x01

# Gesture result registers (bank 0)
_REG_GESTURE_HIGH = 0x43
_REG_GESTURE_LOW = 0x44

# Initialization sequence (bank 1 registers → bank 0)
_INIT_SEQUENCE = [
    (0xEF, 0x00), (0x37, 0x07), (0x38, 0x17), (0x39, 0x06),
    (0x42, 0x01), (0x46, 0x2D), (0x47, 0x0F), (0x48, 0x3C),
    (0x49, 0x00), (0x4A, 0x1E), (0x4C, 0x22), (0x51, 0x10),
    (0x5E, 0x10), (0x60, 0x27), (0x80, 0x42), (0x81, 0x44),
    (0x82, 0x04), (0x8B, 0x01), (0x90, 0x06), (0x95, 0x0A),
    (0x96, 0x0C), (0x97, 0x05), (0x9A, 0x14), (0x9C, 0x3F),
    (0xA5, 0x19), (0xCC, 0x19), (0xCD, 0x0B), (0xCE, 0x13),
    (0xCF, 0x64), (0xD0, 0x21), (0xEF, 0x01), (0x02, 0x0F),
    (0x03, 0x10), (0x04, 0x02), (0x25, 0x01), (0x27, 0x39),
    (0x28, 0x7F), (0x29, 0x08), (0x3E, 0xFF), (0x5E, 0x3D),
    (0x65, 0x96), (0x67, 0x97), (0x69, 0xCD), (0x6A, 0x01),
    (0x6D, 0x2C), (0x6E, 0x01), (0x72, 0x01), (0x73, 0x35),
    (0x77, 0x01), (0xEF, 0x00), (0x41, 0xFF), (0x42, 0x01),
]


class Gesture(IntFlag):
    NONE             = 0x000
    UP               = 0x001
    DOWN             = 0x002
    LEFT             = 0x004
    RIGHT            = 0x008
    FORWARD          = 0x010
    BACKWARD         = 0x020
    CLOCKWISE        = 0x040
    COUNTER_CLOCKWISE = 0x080
    WAVE             = 0x100


class GestureSensor:
    def __init__(self, bus: int = _I2C_BUS):
        self._bus = smbus2.SMBus(bus)
        self._init()
        logger.info("PAJ7620U2 gesture sensor ready")

    def _write(self, reg: int, val: int) -> None:
        self._bus.write_byte_data(_ADDRESS, reg, val)

    def _read(self, reg: int) -> int:
        return self._bus.read_byte_data(_ADDRESS, reg)

    def _init(self) -> None:
        # Wake up sensor
        try:
            self._bus.read_byte(_ADDRESS)
        except Exception:
            pass
        time.sleep(0.005)

        for reg, val in _INIT_SEQUENCE:
            self._write(reg, val)
            time.sleep(0.001)

        # Select bank 0 for normal operation
        self._write(_REG_BANK_SEL, _BANK0)

    def read(self) -> Gesture:
        """Return detected gesture, or Gesture.NONE if nothing detected."""
        low = self._read(_REG_GESTURE_HIGH)   # register 0x43 = bits 0-7
        high = self._read(_REG_GESTURE_LOW)   # register 0x44 = bit 8 (wave)
        raw = ((high & 0x01) << 8) | low
        try:
            return Gesture(raw)
        except ValueError:
            return Gesture.NONE

    def close(self) -> None:
        self._bus.close()
