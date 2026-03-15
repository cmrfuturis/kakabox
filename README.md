# Kakabox

A smart NFC-based audio speaker system for children, built on Raspberry Pi 5.

## Hardware

| Component | Interface | Purpose |
|-----------|-----------|---------|
| MAX98357A | I2S | Class D audio amplifier |
| PN532 | I2C (0x24) | NFC/RFID tag reader |
| PAJ7620U2 | I2C (0x73) | Gesture recognition |
| AS5600 | I2C (0x36) | Magnetic rotary encoder (volume) |
| LM393 | GPIO | Microphone sensor |

## Project Structure

```
kakabox/
├── device/                  # Raspberry Pi software (Python)
│   ├── main.py              # Main event loop
│   ├── hardware/
│   │   ├── nfc.py           # PN532 NFC reader
│   │   ├── gesture.py       # PAJ7620U2 gesture sensor
│   │   ├── encoder.py       # AS5600 magnetic encoder
│   │   ├── microphone.py    # LM393 microphone
│   │   └── audio_output.py  # MAX98357A ALSA/I2S interface
│   ├── audio/
│   │   ├── player.py        # Playback engine (mpv)
│   │   └── library.py       # Audio library scanner
│   ├── api/
│   │   └── routes.py        # FastAPI REST API
│   ├── config.json          # NFC tag mappings & device settings
│   └── requirements.txt
├── api-spec/
│   └── openapi.yaml         # OpenAPI spec for mobile app integration
└── README.md
```

## Getting Started

### Prerequisites

```bash
pip install -r device/requirements.txt
```

### Run

```bash
cd device
python main.py
```

## API

The device exposes a REST API for the companion parents app.
See `api-spec/openapi.yaml` for the full specification.

## Contributing

- **Device software**: See `device/`
- **Parents mobile app**: Connect via the REST API defined in `api-spec/`
