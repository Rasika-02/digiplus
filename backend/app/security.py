import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone

import jwt

from .config import settings

PERMISSIONS = {
    "admin": {"*"},
    "manager": {"task:create", "task:assign", "task:view_all", "handover:create", "handover:review"},
    "coordinator": {"task:create", "task:assign", "task:view_all", "handover:create"},
    "billing": {"task:create", "handover:create"},
    "claims": {"task:create", "handover:create"},
    "doctor": {"task:create", "handover:create"},
    "analyst": {"task:view_all"},
}


def has_perm(role: str, perm: str) -> bool:
    perms = PERMISSIONS.get(role, set())
    return "*" in perms or perm in perms


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 200_000)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except ValueError:
        return False


def create_token(user_id: int, role: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_minutes)
    return jwt.encode({"sub": str(user_id), "role": role, "exp": exp}, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
