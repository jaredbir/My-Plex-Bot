"""
Pytest bootstrap for the test suite.

`main.py` performs real, side-effecting work at *module import time*:
it reads several environment variables and constructs `SonarrAPI` /
`RadarrAPI` clients whose constructors make a live HTTP call to validate
the connection to a Sonarr/Radarr server.

To keep the test suite runnable in a clean CI runner with no network
access and no real credentials, we:

1. Set dummy values for every env var `main.py` reads via `os.getenv`,
   *before* `main` is imported anywhere.
2. Replace `arrapi.SonarrAPI` / `arrapi.RadarrAPI` with dummy classes
   that don't touch the network, *before* `main` is imported (main.py
   does `from arrapi import SonarrAPI, RadarrAPI`, which does a live
   attribute lookup on the `arrapi` module at import time, so patching
   the attributes here is enough for that import to pick up the dummies).
3. Import `main` exactly once, here, so every test module gets the same
   already-imported, side-effect-free module object from `sys.modules`.

`boto3.resource(...)` and `dynamodb.Table(...)` are not mocked because
boto3 resource/table construction is lazy and does not make a network
call by itself -- only actual operations (get_item, put_item, etc.) do,
and none of the pure-logic tests in this suite exercise those.
"""

import os
from unittest.mock import MagicMock

# --- 1. Dummy environment variables (must be set before `import main`) ---

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("SONARR_API_KEY", "dummy-sonarr-api-key")
os.environ.setdefault("SONARR_BASE_URL", "http://sonarr.invalid")
os.environ.setdefault("RADARR_API_KEY", "dummy-radarr-api-key")
os.environ.setdefault("RADARR_BASE_URL", "http://radarr.invalid")
os.environ.setdefault("CF_ACCESS_CLIENT_ID", "dummy-cf-client-id")
os.environ.setdefault("CF_ACCESS_CLIENT_SECRET", "dummy-cf-client-secret")

# --- 2. Patch arrapi's constructors so they never hit the network ---

import arrapi  # noqa: E402


class _DummyArrAPI(MagicMock):
    """Stand-in for SonarrAPI/RadarrAPI: no network call on construction."""

    def __init__(self, *args, **kwargs):
        super().__init__()


arrapi.SonarrAPI = _DummyArrAPI
arrapi.RadarrAPI = _DummyArrAPI

# --- 3. Import main exactly once, with the patches above already in place ---

import main  # noqa: E402,F401
