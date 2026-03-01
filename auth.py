"""
auth.py – JWT + bcrypt authentication helpers for TestArena
"""

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import bcrypt as _bcrypt
from jose import JWTError, jwt

# ── Config ────────────────────────────────────────────────────────────────────


def _load_or_create_secret() -> str:
    """Return a stable JWT secret that survives server restarts.

    Priority:
      1. SECRET_KEY environment variable (for production / Docker).
      2. .jwt_secret file in the project directory (created on first run).

    The random-token-per-process approach that was here previously invalidated
    all sessions on every server restart, causing the login-loop bug.
    """
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    secret_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".jwt_secret")
    if os.path.exists(secret_file):
        with open(secret_file, encoding="utf-8") as fh:
            stored = fh.read().strip()
        if stored:
            return stored
    new_secret = "ta-" + secrets.token_hex(32)
    with open(secret_file, "w", encoding="utf-8") as fh:
        fh.write(new_secret)
    return new_secret


SECRET_KEY        = _load_or_create_secret()
ALGORITHM         = "HS256"
TOKEN_EXPIRE_DAYS = 7

# ── Password hashing ──────────────────────────────────────────────────────────


def hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return _bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


# ── JWT helpers ───────────────────────────────────────────────────────────────

def create_access_token(user_id: int, email: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=TOKEN_EXPIRE_DAYS)
    payload = {
        "sub":   str(user_id),
        "email": email,
        "exp":   expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


# ── FastAPI security scheme ───────────────────────────────────────────────────

_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> dict:
    """Dependency: raises HTTP 401 if token is missing or invalid.
    Returns ``{"user_id": int, "email": str}``."""
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_token(credentials.credentials)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {"user_id": int(payload["sub"]), "email": payload["email"]}


def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> Optional[dict]:
    """Dependency: returns user dict if token valid, else None (no error raised)."""
    if not credentials:
        return None
    payload = decode_token(credentials.credentials)
    if not payload:
        return None
    return {"user_id": int(payload["sub"]), "email": payload["email"]}
