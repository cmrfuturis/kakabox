"""
PN532 NFC reader via I2C.
I2C address: 0x24
Reads ISO14443A tag UIDs (Mifare, NTAG, etc.)
"""
import time
import logging
from smbus2 import SMBus, i2c_msg

logger = logging.getLogger(__name__)

_I2C_BUS = 1
_ADDRESS = 0x24

# Frame constants
_PREAMBLE    = 0x00
_STARTCODE   = [0x00, 0xFF]
_POSTAMBLE   = 0x00
_HOST_TO_PN532 = 0xD4
_PN532_TO_HOST = 0xD5

# Commands
_CMD_GETFIRMWAREVERSION   = 0x02
_CMD_SAMCONFIGURATION     = 0x14
_CMD_RFCONFIGURATION      = 0x32
_CMD_INLISTPASSIVETARGET  = 0x4A

_ACK = [0x00, 0x00, 0xFF, 0x00, 0xFF, 0x00]


def _lcs(length: int) -> int:
    return (~length + 1) & 0xFF


def _dcs(data: list[int]) -> int:
    return (~sum(data) + 1) & 0xFF


class PN532:
    def __init__(self, bus: int = _I2C_BUS):
        self._bus = SMBus(bus)
        self._init()
        logger.info("PN532 NFC reader ready")

    # ------------------------------------------------------------------
    # Low-level I2C frame I/O
    # ------------------------------------------------------------------

    def _write(self, data: list[int]) -> None:
        msg = i2c_msg.write(_ADDRESS, data)
        self._bus.i2c_rdwr(msg)

    def _read(self, length: int) -> list[int]:
        msg = i2c_msg.read(_ADDRESS, length)
        self._bus.i2c_rdwr(msg)
        return list(msg)

    def _send_frame(self, command: int, params: list[int] = []) -> None:
        body = [_HOST_TO_PN532, command] + params
        length = len(body)
        frame = (
            [_PREAMBLE] + _STARTCODE
            + [length, _lcs(length)]
            + body
            + [_dcs(body), _POSTAMBLE]
        )
        self._write(frame)

    def _wait_ready(self, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self._read(1)
            if status[0] & 0x01:
                return True
            time.sleep(0.01)
        return False

    def _read_ack(self) -> bool:
        data = self._read(7)   # 1 status byte + 6 ACK bytes
        return data[1:7] == _ACK

    def _send_command(self, command: int, params: list[int] = [],
                      timeout: float = 1.0) -> list[int] | None:
        self._send_frame(command, params)
        time.sleep(0.01)

        if not self._wait_ready(timeout):
            logger.warning("PN532 timeout waiting for ACK")
            return None
        if not self._read_ack():
            logger.warning("PN532 bad ACK")
            return None
        if not self._wait_ready(timeout):
            logger.warning("PN532 timeout waiting for response")
            return None

        # Response: status + preamble(3) + len + lcs + TFI + cmd+1 + data + dcs + postamble
        raw = self._read(64)
        # raw[0] = status, raw[1..3] = preamble/startcode, raw[4] = len
        length = raw[4]
        return raw[6:6 + length]   # TFI + response data

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def _init(self) -> None:
        # SAMConfiguration: normal mode, no IRQ
        self._send_command(_CMD_SAMCONFIGURATION, [0x01, 0x14, 0x00])
        # RFConfiguration MaxRetries: limit passive activation retries so
        # InListPassiveTarget returns quickly when no tag is present.
        # Params: MxRtyATR=0xFF, MxRtyPSL=0x01, MxRtyPassiveActivation=0x02
        self._send_command(_CMD_RFCONFIGURATION, [0x05, 0xFF, 0x01, 0x02])

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def read_tag(self, timeout: float = 0.3) -> str | None:
        """
        Poll for one ISO14443A tag. Returns UID as hex string (e.g. '04:AB:12:CD')
        or None if no tag is present within timeout.
        """
        response = self._send_command(
            _CMD_INLISTPASSIVETARGET,
            [0x01, 0x00],   # maxTg=1, BrTy=ISO14443A
            timeout=timeout,
        )
        if not response or len(response) < 3:
            return None

        # response[0] = TFI (0xD5), response[1] = 0x4B
        # response[2] = number of targets
        num_targets = response[2]
        if num_targets == 0:
            return None

        # response[7] = UID length, response[8:] = UID bytes
        if len(response) < 9:
            return None
        uid_length = response[7]
        uid_bytes = response[8: 8 + uid_length]
        uid = ":".join(f"{b:02X}" for b in uid_bytes)
        logger.debug("Tag detected: %s", uid)
        return uid

    def close(self) -> None:
        self._bus.close()
