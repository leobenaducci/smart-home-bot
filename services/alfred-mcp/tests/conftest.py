"""Environment for the suite, set before `alfred_mcp.server` is imported.

`config.load()` runs at import of server.py, so anything set after that import
is too late -- the same shape as the packaging suites that pointed their path
constants at scratch *after* `import app` and edited the live household state
because of it. Set here, in a conftest, so there is no import order for anyone
to get wrong.

Every upstream address points at port 9 (discard). Nothing in this suite should
reach the network, and a test that starts doing so should fail by hanging
briefly and refusing, not by quietly talking to a real broker.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("ALFRED_MCP_MEMBER", "user1")
os.environ.setdefault("ALFRED_MCP_LOGIN", "999000111")
os.environ.setdefault("ALFRED_MCP_FOLDER", "testfolder")
os.environ.setdefault("ALFRED_MCP_TOKEN", "the-expected-token")
os.environ.setdefault("ALFRED_MCP_PORT", "21072")
os.environ.setdefault("CODE_BROKER_URL", "http://127.0.0.1:9")
os.environ.setdefault("CODE_BROKER_TOKEN", "broker-token")
os.environ.setdefault("HOMECORE_URL", "https://127.0.0.1:9")
os.environ.setdefault("HOMECORE_PROXY_TOKEN", "proxy-token")
