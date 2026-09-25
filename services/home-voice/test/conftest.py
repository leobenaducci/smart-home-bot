"""What every in-process test of the gateway needs before it can import it.

gateway.py imports openwakeword, numpy, aiohttp and paho at module scope. The
stub and the sys.path probe below used to be pasted at the top of each test
file, which meant the next heavy module-scope import had to be stubbed in every
one of them — and a file that missed it failed with an ImportError from inside
TestClient rather than anything legible.

openwakeword is stubbed rather than installed: it is not on the path any of
these tests exercise, and pulling onnxruntime in to check a JSON store would be
silly.
"""
import sys
import types
from pathlib import Path

import pytest

ow = types.ModuleType("openwakeword")
ow_model = types.ModuleType("openwakeword.model")
ow_model.Model = object
ow.model = ow_model
sys.modules.setdefault("openwakeword", ow)
sys.modules.setdefault("openwakeword.model", ow_model)

# /app in the container, ../voice-gateway in a checkout.
for candidate in ("/app", str(Path(__file__).resolve().parent.parent / "voice-gateway")):
    if (Path(candidate) / "gateway.py").is_file():
        sys.path.insert(0, candidate)
        break

# The gateway token every Alfred-facing route is gated on. Tests set the
# module's GATEWAY_TOKEN to match.
H = {"Authorization": "Bearer tok"}


@pytest.fixture()
def gateway():
    import gateway as module
    return module
