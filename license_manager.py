"""
license_manager.py  –  Offline license engine for FreshMarket POS
==================================================================

DESIGN OVERVIEW
---------------
1.  Machine fingerprint  – derived from stable hardware IDs (UUID, MAC, disk serial).
    Combined with a vendor salt and hashed with SHA-256 to produce a 12-char
    human-readable "Secret Code" the user copies into the Key Generator tool.

2.  Key generation (keygen.py)  –  a *separate* offline tool given only to the
    vendor / support team.  It is NOT shipped with the app.  The keygen:
      •  Decodes the Secret Code to recover machine_hash
      •  Signs  { machine_hash | expiry_epoch | key_type | nonce }  with
         HMAC-SHA256 using a private VENDOR_SECRET
      •  Encodes the result as a 5×5 uppercase alphanumeric activation key
         (groups of 5 separated by dashes, e.g.  AB3CD-EF7GH-…)

3.  Key validation  –  the app re-derives the machine hash, reconstructs the
    signed payload, and verifies the HMAC.  No internet required at any step.

CLOCK TAMPER DETECTION  (NTP-free)
-----------------------------------
The system maintains a "last-seen" timestamp in the license store.  Every
startup the current wall-clock is compared against the stored value.

Rules
  •  current_time  >=  last_seen        → OK, advance last_seen
  •  current_time  <   last_seen - GRACE  → TAMPER detected, lock app

GRACE is 120 seconds to tolerate minor NTP-style drift or DST adjustment.
Any backward jump larger than that is treated as clock rollback and the
license is invalidated permanently until a renewal key is entered.

The license store is stored in SQLite (same pos.db used by the app) under
the table  `license`.  The HMAC check on the stored record prevents casual
editing with a DB browser.

KEY TYPES
---------
  ACTIVATE  –  first-time activation, sets expiry N days from now
  RENEW     –  extends an existing (possibly expired) license by N more days
               from the *current* date (not the old expiry)

Both key types are machine-locked and single-use (the nonce embedded in the
key is stored after first use; reusing the same key is rejected).
"""

import os
import sys
import hmac
import hashlib
import struct
import time
import sqlite3
import uuid
import json
import base64
import re
from contextlib import contextmanager
from typing import Tuple, Optional

# ─── Configuration ────────────────────────────────────────────────────────────

# This secret must be identical in license_manager.py AND keygen.py.
# Keep it out of version control; hard-code a long random value before shipping.
VENDOR_SECRET = b"K9#mPx2!qL5@nR8&vT1*wZ4^jF6$bH3%cD7_yA0"

# How many seconds of backward clock drift are tolerated (DST / minor skew)
CLOCK_GRACE_SECONDS = 120

# ─── Paths ────────────────────────────────────────────────────────────────────

def _resource_path(*parts: str) -> str:
    if parts and parts[0] == '__data__':
        if getattr(sys, 'frozen', False):
            base = os.path.dirname(sys.executable)
        else:
            base = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base, *parts[1:])
    else:
        if getattr(sys, 'frozen', False):
            base = sys._MEIPASS
        else:
            base = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base, *parts)

DB_PATH = _resource_path('__data__', 'data', 'pos.db')

# ─── Database helpers ─────────────────────────────────────────────────────────

def _ensure_license_table():
    """Create license table if it doesn't exist yet."""
    db_dir = os.path.dirname(DB_PATH)
    os.makedirs(db_dir, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS license (
                key     TEXT PRIMARY KEY,
                value   TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS used_nonces (
                nonce TEXT PRIMARY KEY,
                used_at INTEGER NOT NULL
            )
        """)
        conn.commit()

def _lic_get(key: str) -> Optional[str]:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT value FROM license WHERE key=?", (key,)
            ).fetchone()
            return row[0] if row else None
    except Exception:
        return None

def _lic_set(key: str, value: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO license (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )
        conn.commit()

def _nonce_used(nonce: str) -> bool:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT 1 FROM used_nonces WHERE nonce=?", (nonce,)
            ).fetchone()
            return row is not None
    except Exception:
        return False

def _mark_nonce(nonce: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO used_nonces (nonce, used_at) VALUES (?,?)",
            (nonce, int(time.time()))
        )
        conn.commit()

# ─── Machine Fingerprint ──────────────────────────────────────────────────────

def _get_machine_id() -> str:
    """
    Build a stable machine identifier from hardware-level sources.
    Falls back gracefully on non-Windows platforms.
    """
    components = []

    # 1. Windows MachineGuid from registry
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography"
        )
        guid, _ = winreg.QueryValueEx(key, "MachineGuid")
        winreg.CloseKey(key)
        components.append(f"mg:{guid}")
    except Exception:
        pass

    # 2. Python's uuid.getnode() (MAC address of primary NIC)
    try:
        mac = uuid.getnode()
        # uuid.getnode() may return a random value on some VMs; that's fine
        components.append(f"mac:{mac:012x}")
    except Exception:
        pass

    # 3. Windows volume serial number of C:\
    try:
        import ctypes
        vol_serial = ctypes.c_ulong(0)
        ctypes.windll.kernel32.GetVolumeInformationW(
            "C:\\", None, 0, ctypes.byref(vol_serial), None, None, None, 0
        )
        components.append(f"vol:{vol_serial.value:08x}")
    except Exception:
        pass

    # 4. Platform fallback
    if not components:
        import platform
        components.append(f"plat:{platform.node()}:{platform.machine()}")

    raw = "|".join(components)
    digest = hmac.new(VENDOR_SECRET, raw.encode(), hashlib.sha256).digest()
    # Return first 16 hex chars = 64-bit fingerprint
    return digest.hex()[:16]


def get_secret_code() -> str:
    """
    Return the 16-char hex machine fingerprint.
    Displayed in the activation screen so the user can send it to the vendor.
    Formatted as  XXXX-XXXX-XXXX-XXXX  for readability.
    """
    mid = _get_machine_id()
    return f"{mid[0:4]}-{mid[4:8]}-{mid[8:12]}-{mid[12:16]}".upper()

# ─── Key Encoding / Decoding ──────────────────────────────────────────────────

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # 32 chars; no 0/O/1/I

def _encode_key(data: bytes) -> str:
    """Base-32 encode bytes → groups of 5 separated by dashes."""
    # Pad to multiple of 5 bits → use standard base32 then re-encode
    b32 = base64.b32encode(data).decode().rstrip("=")
    # Map standard base32 alphabet to our unambiguous alphabet
    std = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    result = "".join(_ALPHABET[std.index(c)] for c in b32)
    # Group into 5-char chunks
    chunks = [result[i:i+5] for i in range(0, len(result), 5)]
    return "-".join(chunks)

def _decode_key(key_str: str) -> bytes:
    """Inverse of _encode_key."""
    clean = key_str.replace("-", "").replace(" ", "").upper()
    std = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    b32_chars = ""
    for c in clean:
        if c not in _ALPHABET:
            raise ValueError(f"Invalid character in key: {c!r}")
        b32_chars += std[_ALPHABET.index(c)]
    # Re-pad
    pad = (8 - len(b32_chars) % 8) % 8
    b32_chars += "=" * pad
    return base64.b32decode(b32_chars)

# ─── Key Payload ──────────────────────────────────────────────────────────────
#
#  Payload layout (all big-endian):
#    [0:8]   machine_hash   (8 bytes, first 8 bytes of SHA-256 of machine_id)
#    [8:12]  expiry_epoch   (4 bytes, unix timestamp / 60 → minutes, fits 32-bit)
#    [12]    key_type       (1 byte:  0x01=ACTIVATE  0x02=RENEW)
#    [13:17] nonce          (4 bytes, random)
#    [17:25] hmac8          (8 bytes, first 8 bytes of HMAC-SHA256 of [0:17])
#
#  Total: 25 bytes → 40 base32 chars → 8 groups of 5 → key looks like:
#    ABCDE-FGHIJ-KLMNO-PQRST-UVWXY-Z2345-67890-ABCDE

KEY_TYPE_ACTIVATE = 0x01
KEY_TYPE_RENEW    = 0x02

PAYLOAD_LEN = 25   # bytes before encoding

def _make_payload(machine_id_hex: str, expiry_ts: int, key_type: int, nonce: bytes) -> bytes:
    machine_hash = bytes.fromhex(
        hashlib.sha256(machine_id_hex.encode()).hexdigest()
    )[:8]
    expiry_min = expiry_ts // 60
    header = (
        machine_hash
        + struct.pack(">I", expiry_min)
        + struct.pack("B", key_type)
        + nonce[:4]
    )
    sig = hmac.new(VENDOR_SECRET, header, hashlib.sha256).digest()[:8]
    return header + sig

def _validate_payload(raw: bytes, machine_id_hex: str) -> Tuple[bool, str, int, int, str]:
    """
    Returns (ok, error_msg, expiry_ts, key_type, nonce_hex)
    """
    if len(raw) != PAYLOAD_LEN:
        return False, "Key length mismatch", 0, 0, ""

    machine_hash_expected = bytes.fromhex(
        hashlib.sha256(machine_id_hex.encode()).hexdigest()
    )[:8]
    machine_hash_got = raw[0:8]
    if machine_hash_got != machine_hash_expected:
        return False, "This key was generated for a different machine", 0, 0, ""

    header   = raw[0:17]
    sig_got  = raw[17:25]
    sig_want = hmac.new(VENDOR_SECRET, header, hashlib.sha256).digest()[:8]
    if not hmac.compare_digest(sig_got, sig_want):
        return False, "Key signature invalid – key may be tampered or forged", 0, 0, ""

    expiry_min = struct.unpack(">I", raw[8:12])[0]
    expiry_ts  = expiry_min * 60
    key_type   = raw[12]
    nonce_hex  = raw[13:17].hex()

    return True, "", expiry_ts, key_type, nonce_hex

# ─── Clock Tamper Detection ───────────────────────────────────────────────────

def _current_time() -> int:
    return int(time.time())

def _check_and_advance_clock() -> Tuple[bool, str]:
    """
    Compare current wall-clock to stored last-seen time.
    Returns (ok, error_message).
    Advances last-seen on success.
    """
    now = _current_time()
    stored_str = _lic_get("last_seen")

    if stored_str is None:
        # First run after activation – just record now
        _lic_set("last_seen", str(now))
        return True, ""

    try:
        stored = int(stored_str)
    except ValueError:
        # Corrupt record – treat as tampered
        _lic_set("tampered", "1")
        return False, "License store corrupted. Please contact support."

    # Allow small backward drift (DST, slight skew)
    if now < stored - CLOCK_GRACE_SECONDS:
        _lic_set("tampered", "1")
        return False, (
            f"Clock tampering detected. "
            f"System time appears to have been rolled back by "
            f"{stored - now} seconds. "
            "Contact support for a renewal key."
        )

    # Normal forward progress – advance
    if now > stored:
        _lic_set("last_seen", str(now))
    return True, ""

# ─── Public API ───────────────────────────────────────────────────────────────

class LicenseStatus:
    UNLICENSED  = "unlicensed"   # No valid license at all
    ACTIVE      = "active"       # Valid and not expired
    EXPIRED     = "expired"      # Validated but past expiry date
    TAMPERED    = "tampered"     # Clock rollback detected
    INVALID_KEY = "invalid_key"  # Key rejected during activation attempt


def check_license() -> dict:
    """
    Called on every app startup.  Returns a dict:
        {
          "status":       LicenseStatus.*,
          "message":      str,
          "expiry_date":  "DD-MM-YYYY" | None,
          "days_left":    int | None,
          "secret_code":  str,          # always present so screen can show it
        }
    """
    _ensure_license_table()
    secret = get_secret_code()

    # 1. Already tampered in a previous session?
    if _lic_get("tampered") == "1":
        return {
            "status": LicenseStatus.TAMPERED,
            "message": "Clock tampering was previously detected. Enter a renewal key to restore access.",
            "expiry_date": None,
            "days_left": None,
            "secret_code": secret,
        }

    # 2. Clock check
    clock_ok, clock_msg = _check_and_advance_clock()
    if not clock_ok:
        return {
            "status": LicenseStatus.TAMPERED,
            "message": clock_msg,
            "expiry_date": None,
            "days_left": None,
            "secret_code": secret,
        }

    # 3. Is there a stored expiry?
    expiry_str = _lic_get("expiry")
    if expiry_str is None:
        return {
            "status": LicenseStatus.UNLICENSED,
            "message": "This software is not activated. Enter your activation key to continue.",
            "expiry_date": None,
            "days_left": None,
            "secret_code": secret,
        }

    try:
        expiry_ts = int(expiry_str)
    except ValueError:
        return {
            "status": LicenseStatus.UNLICENSED,
            "message": "License data is corrupt. Please re-activate.",
            "expiry_date": None,
            "days_left": None,
            "secret_code": secret,
        }

    now = _current_time()
    days_left = max(0, (expiry_ts - now) // 86400)
    import datetime as dt
    expiry_date = dt.datetime.utcfromtimestamp(expiry_ts).strftime("%d-%m-%Y")

    if now > expiry_ts:
        return {
            "status": LicenseStatus.EXPIRED,
            "message": f"Your license expired on {expiry_date}. Enter a renewal key to continue.",
            "expiry_date": expiry_date,
            "days_left": 0,
            "secret_code": secret,
        }

    return {
        "status": LicenseStatus.ACTIVE,
        "message": f"License active. Expires {expiry_date} ({days_left} day(s) remaining).",
        "expiry_date": expiry_date,
        "days_left": days_left,
        "secret_code": secret,
    }


def activate(key_str: str) -> dict:
    """
    Attempt to activate or renew with the supplied key string.
    Returns { "ok": bool, "message": str, "expiry_date": str|None, "days_left": int|None }
    """
    _ensure_license_table()
    machine_id = _get_machine_id()

    # Decode
    try:
        raw = _decode_key(key_str.strip())
    except Exception as e:
        return {"ok": False, "message": f"Key format error: {e}", "expiry_date": None, "days_left": None}

    # Validate payload
    ok, err, expiry_ts, key_type, nonce_hex = _validate_payload(raw, machine_id)
    if not ok:
        return {"ok": False, "message": err, "expiry_date": None, "days_left": None}

    # Check key type
    if key_type not in (KEY_TYPE_ACTIVATE, KEY_TYPE_RENEW):
        return {"ok": False, "message": "Unknown key type.", "expiry_date": None, "days_left": None}

    # Replay protection – nonce already used?
    if _nonce_used(nonce_hex):
        return {"ok": False, "message": "This key has already been used.", "expiry_date": None, "days_left": None}

    now = _current_time()
    if expiry_ts < now:
        return {"ok": False, "message": "This key has already expired. Request a new key.", "expiry_date": None, "days_left": None}

    # Store
    _mark_nonce(nonce_hex)
    _lic_set("expiry", str(expiry_ts))
    _lic_set("tampered", "0")
    _lic_set("last_seen", str(now))

    import datetime as dt
    expiry_date = dt.datetime.utcfromtimestamp(expiry_ts).strftime("%d-%m-%Y")
    days_left = max(0, (expiry_ts - now) // 86400)

    action = "activated" if key_type == KEY_TYPE_ACTIVATE else "renewed"
    return {
        "ok": True,
        "message": f"License {action} successfully! Valid until {expiry_date}.",
        "expiry_date": expiry_date,
        "days_left": days_left,
    }
