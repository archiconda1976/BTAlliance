# Network feature regression tests

These tests use Home Assistant's real flows and schemas with mocked Bluetooth
transport. They do not connect to devices or issue live mesh commands.

Use Python 3.14 and an isolated environment:

```sh
python -m pip install "homeassistant==2026.10.1" pytest pytest-asyncio bleak-retry-connector
python -m pytest -q tests
```

USB monitoring is stubbed where its optional platform dependency is unavailable.
Radio behavior, complete factory-reset effects, and reconnection after network
changes require a separate physical test with one confirmed owned device.
