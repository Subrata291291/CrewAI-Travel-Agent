from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[3]
AUTH_DB_PATH = Path(os.getenv("AUTH_DB_PATH", str(PROJECT_ROOT / "data" / "auth.sqlite3")))
SESSION_DAYS = 30
_EMAIL_PATTERN = re.compile(r"^[^@\s]{1,64}@[^@\s.]+(?:\.[^@\s.]+)+$")


def _connect() -> sqlite3.Connection:
    AUTH_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(AUTH_DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            full_name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS trip_owners (
            trip_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS sessions_user_id_idx ON sessions(user_id);
        CREATE INDEX IF NOT EXISTS trip_owners_user_id_idx ON trip_owners(user_id);
        """
    )
    return connection


@contextmanager
def _database() -> Iterator[sqlite3.Connection]:
    connection = _connect()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _safe_user(row: sqlite3.Row | None) -> dict[str, str] | None:
    if row is None:
        return None
    return {
        "user_id": row["user_id"],
        "full_name": row["full_name"],
        "email": row["email"],
        "created_at": row["created_at"],
    }


def validate_registration(full_name: str, email: str, password: str) -> tuple[str, str]:
    name = " ".join(full_name.split())
    normalized_email = email.strip().casefold()
    if not 1 <= len(name) <= 120:
        raise ValueError("Enter your full name (up to 120 characters).")
    if len(normalized_email) > 254 or not _EMAIL_PATTERN.fullmatch(normalized_email):
        raise ValueError("Enter a valid email address.")
    if len(password) < 10:
        raise ValueError("Password must be at least 10 characters.")
    if len(password) > 128:
        raise ValueError("Password must be no more than 128 characters.")
    return name, normalized_email


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt$16384$8$1${salt.hex()}${derived.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, n, r, p, salt_hex, digest_hex = stored.split("$")
        if algorithm != "scrypt":
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
        )
        return hmac.compare_digest(derived.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def create_user(full_name: str, email: str, password: str) -> dict[str, str]:
    name, normalized_email = validate_registration(full_name, email, password)
    created_at = datetime.now(timezone.utc).isoformat()
    user_id = str(uuid4())
    try:
        with _database() as connection:
            connection.execute(
                "INSERT INTO users(user_id, full_name, email, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, name, normalized_email, _hash_password(password), created_at),
            )
            row = connection.execute(
                "SELECT user_id, full_name, email, created_at FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
    except sqlite3.IntegrityError as exc:
        raise FileExistsError("An account with this email address already exists.") from exc
    safe = _safe_user(row)
    if safe is None:
        raise RuntimeError("The account could not be loaded after registration.")
    return safe


def authenticate_user(email: str, password: str) -> dict[str, str] | None:
    normalized_email = email.strip().casefold()
    with _database() as connection:
        row = connection.execute(
            "SELECT user_id, full_name, email, password_hash, created_at FROM users WHERE email = ? COLLATE NOCASE",
            (normalized_email,),
        ).fetchone()
    if row is None or not _verify_password(password, row["password_hash"]):
        return None
    return _safe_user(row)


def create_session(user_id: str) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    created_at = now.isoformat()
    expires_at = (now + timedelta(days=SESSION_DAYS)).isoformat()
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    with _database() as connection:
        connection.execute(
            "INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (token_hash, user_id, expires_at, created_at),
        )
    return token, expires_at


def get_session_user(token: str | None) -> dict[str, str] | None:
    if not token or len(token) > 256:
        return None
    token_hash = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    with _database() as connection:
        row = connection.execute(
            """
            SELECT users.user_id, users.full_name, users.email, users.created_at
            FROM sessions JOIN users USING(user_id)
            WHERE sessions.token_hash = ? AND sessions.expires_at > ?
            """,
            (token_hash, now),
        ).fetchone()
        connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
    return _safe_user(row)


def revoke_session(token: str | None) -> None:
    if not token:
        return
    token_hash = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
    with _database() as connection:
        connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def own_trip(user_id: str, trip_id: str) -> None:
    with _database() as connection:
        connection.execute(
            "INSERT INTO trip_owners(trip_id, user_id, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(trip_id) DO NOTHING",
            (trip_id, user_id, datetime.now(timezone.utc).isoformat()),
        )
        row = connection.execute(
            "SELECT user_id FROM trip_owners WHERE trip_id = ?", (trip_id,)
        ).fetchone()
    if row is None or row["user_id"] != user_id:
        raise PermissionError("Trip is not available to this account.")


def owns_trip(user_id: str, trip_id: str) -> bool:
    with _database() as connection:
        row = connection.execute(
            "SELECT 1 FROM trip_owners WHERE trip_id = ? AND user_id = ?",
            (trip_id, user_id),
        ).fetchone()
    return row is not None


def list_owned_trip_ids(user_id: str) -> set[str]:
    with _database() as connection:
        rows = connection.execute(
            "SELECT trip_id FROM trip_owners WHERE user_id = ?", (user_id,)
        ).fetchall()
    return {row["trip_id"] for row in rows}


def user_by_id(user_id: str) -> dict[str, str] | None:
    with _database() as connection:
        row = connection.execute(
            "SELECT user_id, full_name, email, created_at FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return _safe_user(row)


def trip_owner(trip_id: str) -> str | None:
    with _database() as connection:
        row = connection.execute(
            "SELECT user_id FROM trip_owners WHERE trip_id = ?", (trip_id,)
        ).fetchone()
    return row["user_id"] if row else None


def owned_trips_count(user_id: str) -> int:
    with _database() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS total FROM trip_owners WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return int(row["total"])
