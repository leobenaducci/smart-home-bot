"""Device-key registry for home-chat.

Restricts remote login (chat.home) to devices whose public key has
been registered - even the correct password is not enough from an
unregistered device. Registration is a two-step, admin-gated process:

1. Admin generates a one-time 6-digit code for a specific account via
   manage_devices.py *over SSH* - there is no HTTP way to generate a code,
   so only someone with SSH access to the VPS can ever produce one.
2. The family member enters that code once in the app; the app generates a
   keypair on-device (Android Keystore; the private key never leaves it) and
   redeems the code + its public key via POST /enroll/complete. This one HTTP
   endpoint has to exist (the device has no other way to hand over its public
   key), so it's rate-limited per source IP and the code is single-use,
   6-digit-random, and expires in 10 minutes.

After registration, the server only ever sees per-login signatures over a
single-use challenge - never the private key itself.
"""
import os
import sqlite3
import secrets
import time
import base64

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

DB_PATH = os.environ.get('DEVICES_DB_PATH', 'data/devices.db')

CHALLENGE_TTL_SECONDS = 60
ENROLL_CODE_TTL_SECONDS = 10 * 60
ENROLL_MAX_ATTEMPTS = 8
ENROLL_ATTEMPT_WINDOW_SECONDS = 10 * 60

# Single-process app (see Dockerfile) - plain in-memory dicts are fine.
_challenges: dict = {}
_enroll_attempts: dict = {}


def _connect():
    dirname = os.path.dirname(DB_PATH)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = _connect()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS device_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            public_key TEXT NOT NULL UNIQUE,
            label TEXT,
            created_at INTEGER NOT NULL,
            last_used INTEGER
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS enroll_codes (
            code TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            used INTEGER NOT NULL DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()


def generate_enroll_code(username: str) -> str:
    """Admin-only (called from manage_devices.py over SSH - no HTTP route
    calls this)."""
    code = f'{secrets.randbelow(1_000_000):06d}'
    conn = _connect()
    conn.execute(
        'INSERT INTO enroll_codes (code, username, created_at, used) VALUES (?, ?, ?, 0)',
        (code, username, int(time.time())),
    )
    conn.commit()
    conn.close()
    return code


def _check_enroll_rate_limit(ip: str) -> bool:
    now = time.time()
    attempts = [t for t in _enroll_attempts.get(ip, []) if now - t < ENROLL_ATTEMPT_WINDOW_SECONDS]
    _enroll_attempts[ip] = attempts
    return len(attempts) < ENROLL_MAX_ATTEMPTS


def consume_enroll_code(code: str, public_key_b64: str, label: str, ip: str):
    """Validate + consume a one-time code, registering the device's public
    key. Returns the username it was issued for, or None if invalid/expired/
    used/rate-limited. Every call (success or failure) counts against the
    per-IP rate limit, since a 6-digit code only resists brute force if
    guessing it is expensive."""
    _enroll_attempts.setdefault(ip, []).append(time.time())
    if not _check_enroll_rate_limit(ip):
        return None
    if not code or not public_key_b64 or not _is_valid_public_key(public_key_b64):
        return None
    now = int(time.time())
    conn = _connect()
    try:
        row = conn.execute(
            'SELECT username, created_at, used FROM enroll_codes WHERE code = ?',
            (code,),
        ).fetchone()
        if not row:
            return None
        username, created_at, used = row
        if used or now - created_at > ENROLL_CODE_TTL_SECONDS:
            return None
        conn.execute('UPDATE enroll_codes SET used = 1 WHERE code = ?', (code,))
        conn.execute(
            'INSERT OR IGNORE INTO device_keys (username, public_key, label, created_at) VALUES (?, ?, ?, ?)',
            (username, public_key_b64, label, now),
        )
        conn.commit()
        return username
    finally:
        conn.close()


def _is_valid_public_key(public_key_b64: str) -> bool:
    try:
        serialization.load_der_public_key(base64.b64decode(public_key_b64))
        return True
    except Exception:
        return False


def register_device_key(username: str, public_key_b64: str, label: str = "") -> bool:
    """Admin-only action (run via manage_devices.py over SSH, not exposed over
    HTTP). Returns False if the public key isn't valid DER or is already
    registered."""
    if not username or not public_key_b64 or not _is_valid_public_key(public_key_b64):
        return False
    conn = _connect()
    try:
        conn.execute(
            'INSERT OR IGNORE INTO device_keys (username, public_key, label, created_at) VALUES (?, ?, ?, ?)',
            (username, public_key_b64, label, int(time.time())),
        )
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def remove_device(device_id: int) -> bool:
    conn = _connect()
    try:
        conn.execute('DELETE FROM device_keys WHERE id = ?', (device_id,))
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def list_all_devices():
    conn = _connect()
    try:
        rows = conn.execute(
            'SELECT id, username, label, created_at, last_used FROM device_keys ORDER BY username, created_at',
        ).fetchall()
        return [
            {'id': r[0], 'username': r[1], 'label': r[2], 'created_at': r[3], 'last_used': r[4]}
            for r in rows
        ]
    finally:
        conn.close()


def list_device_keys(username: str):
    conn = _connect()
    try:
        rows = conn.execute(
            'SELECT public_key FROM device_keys WHERE username = ?', (username,),
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def _touch_device(username: str, public_key_b64: str):
    conn = _connect()
    conn.execute(
        'UPDATE device_keys SET last_used = ? WHERE username = ? AND public_key = ?',
        (int(time.time()), username, public_key_b64),
    )
    conn.commit()
    conn.close()


def issue_challenge() -> str:
    now = time.time()
    for c, t in list(_challenges.items()):  # opportunistic cleanup
        if now - t > CHALLENGE_TTL_SECONDS:
            _challenges.pop(c, None)
    challenge = secrets.token_urlsafe(24)
    _challenges[challenge] = now
    return challenge


def _consume_challenge(challenge: str) -> bool:
    issued_at = _challenges.pop(challenge, None)
    if issued_at is None:
        return False
    return time.time() - issued_at <= CHALLENGE_TTL_SECONDS


def _verify_signature(public_key_b64: str, message: str, signature_b64: str) -> bool:
    try:
        public_key = serialization.load_der_public_key(base64.b64decode(public_key_b64))
        public_key.verify(
            base64.b64decode(signature_b64),
            message.encode('utf-8'),
            ec.ECDSA(hashes.SHA256()),
        )
        return True
    except Exception:
        return False


def verify_login_device(username: str, challenge: str, signature_b64: str) -> bool:
    """True if `challenge` is fresh/unused AND some device registered for
    `username` produced `signature_b64` over it. The challenge is consumed
    (single use) either way, so a failed attempt can't be replayed."""
    if not challenge or not signature_b64:
        return False
    if not _consume_challenge(challenge):
        return False
    for public_key_b64 in list_device_keys(username):
        if _verify_signature(public_key_b64, challenge, signature_b64):
            _touch_device(username, public_key_b64)
            return True
    return False
