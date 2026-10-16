"""Test with real HA schemas and a mocked Bluetooth transport."""

import sys
import types
import importlib.util

import homeassistant  # Install HA's schema implementation before importing voluptuous.

if sys.platform == "win32" or importlib.util.find_spec("aiousbwatcher") is None:
    # Unit tests never start HA's optional USB monitoring service.
    sys.modules.setdefault("homeassistant.components.usb", types.ModuleType("homeassistant.components.usb"))
