"""Authentication helpers: pure-Python TOTP (authenticator-app 2FA), password
hashing, and email allow-list matching with wildcards. No external deps."""
import base64
import fnmatch
import hashlib
import hmac
import os
import struct
import time
import urllib.parse

from werkzeug.security import check_password_hash, generate_password_hash

# ---------------- passwords ----------------

import re

# Minimum strong-password policy, enforced everywhere a password is set.
PASSWORD_MIN = 10


def password_strength_error(pw):
    """Return None if the password is strong enough, else a message explaining
    what's missing. Policy: >= 10 chars with lower, upper, number and symbol."""
    pw = pw or ""
    if len(pw) < PASSWORD_MIN:
        return f"Password must be at least {PASSWORD_MIN} characters long."
    checks = [(r"[a-z]", "a lowercase letter"),
              (r"[A-Z]", "an uppercase letter"),
              (r"[0-9]", "a number"),
              (r"[^A-Za-z0-9]", "a symbol")]
    missing = [name for rx, name in checks if not re.search(rx, pw)]
    if missing:
        return "Password must include " + ", ".join(missing) + "."
    return None


def hash_password(pw):
    return generate_password_hash(pw)


def verify_password(hash_, pw):
    try:
        return check_password_hash(hash_, pw)
    except Exception:
        return False


# ---------------- TOTP (RFC 6238) ----------------

def new_totp_secret():
    """A fresh base32 secret for an authenticator app."""
    return base64.b32encode(os.urandom(20)).decode("ascii").rstrip("=")


def _totp_at(secret, for_time, step=30, digits=6):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    counter = struct.pack(">Q", int(for_time // step))
    h = hmac.new(key, counter, hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def verify_totp(secret, code, window=1):
    """Check a 6-digit code, allowing ±window steps for clock drift."""
    if not secret or not code:
        return False
    code = str(code).strip().replace(" ", "")
    if not code.isdigit():
        return False
    now = time.time()
    for w in range(-window, window + 1):
        if hmac.compare_digest(_totp_at(secret, now + w * 30), code):
            return True
    return False


def otpauth_uri(secret, account, issuer="Ascom Network Monitor"):
    label = urllib.parse.quote(f"{issuer}:{account}")
    params = urllib.parse.urlencode({"secret": secret, "issuer": issuer,
                                     "algorithm": "SHA1", "digits": 6, "period": 30})
    return f"otpauth://totp/{label}?{params}"


# ---------------- email allow-list ----------------

def email_allowed(email, patterns):
    """True if email matches any wildcard pattern. Empty list = allow all.
    A blank email is always allowed (protects the built-in local admin, which
    has no email, from being locked out by the list)."""
    if not patterns:
        return True
    email = (email or "").strip().lower()
    if not email:
        return True
    for p in patterns:
        p = p.strip().lower()
        if p and fnmatch.fnmatch(email, p):
            return True
    return False


def parse_email_patterns(text):
    """Split a comma/newline separated allow-list into clean patterns."""
    if not text:
        return []
    out = []
    for tok in str(text).replace("\n", ",").replace(";", ",").split(","):
        tok = tok.strip().lower()
        if tok and tok not in out:
            out.append(tok)
    return out
