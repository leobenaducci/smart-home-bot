"""Auth helpers for the home-chat proxy.

Two modes, selected by AUTH_MODE:

  `upstream` (default) -- the proxy forwards the credentials to the home machine
      over the reverse tunnel and believes its answer. Nothing about the
      household is stored out here: no usernames, no bcrypt hashes, no user
      store at all. This is the mode that matches what the VPS is for, which is
      terminating a public connection and forwarding it.

  `local` -- verify against a synced copy of the portal's users.json. Kept for
      an install that deliberately wants the proxy to keep answering while the
      home machine is unreachable, at the cost of putting the household's
      password hashes on a box they do not physically control.

Either way, on success we mint a signed JWT stored in an HttpOnly cookie on the
family's phones (long-lived).
"""
import os
import jwt
import bcrypt
from datetime import datetime, timedelta, timezone

SECRET_KEY = os.environ.get("SESSION_SIGNING_KEY", "")
if not SECRET_KEY:
    raise RuntimeError(
        "SESSION_SIGNING_KEY environment variable must be set (an empty key would let "
        "anyone forge a valid session JWT). Generate one with: "
        'python -c "import secrets; print(secrets.token_hex(32))"'
    )
ALGORITHM = "HS256"
# Long-lived by design: the family stays signed in until they explicitly log out
# (the "Salir" button, which clears the cookie) or the token expires. Rotate
# SESSION_SIGNING_KEY at any time to force everyone to re-authenticate immediately.
ACCESS_TOKEN_EXPIRE_DAYS = int(os.environ.get("TOKEN_EXPIRE_DAYS", "180"))

COOKIE_NAME = "hc_session"
COOKIE_MAX_AGE = ACCESS_TOKEN_EXPIRE_DAYS * 24 * 3600


AUTH_MODE = os.environ.get("AUTH_MODE", "upstream").strip().lower()


def verify_password(password: str, password_hash: str) -> bool:
    """Local-mode check. Never reached when AUTH_MODE is `upstream`."""
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except Exception:
        return False


async def verify_upstream(client, upstream_url: str, shared_secret: str,
                          username: str, password: str) -> bool:
    """Ask the home machine whether these credentials are good.

    A network failure is a failed login, not a successful one: an upstream that
    cannot be reached must never fall back to letting somebody in, and the proxy
    has nothing of its own to check against in this mode anyway.
    """
    try:
        response = await client.post(
            upstream_url.rstrip("/") + "/api/auth/verify",
            json={"username": username, "password": password},
            headers={"X-Proxy-Secret": shared_secret},
            timeout=15.0,
        )
    except Exception:
        return False
    if response.status_code != 200:
        return False
    try:
        return bool(response.json().get("ok"))
    except Exception:
        return False


def create_token(username: str) -> str:
    payload = {
        "sub": username,
        "exp": datetime.now(timezone.utc) + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None
