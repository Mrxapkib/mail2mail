# -*- coding: utf-8 -*-
"""Lisans veritabani (SQLite) — bitis tarihi ve durum yonetimi."""
from __future__ import annotations

import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from .config import LICENSE_DB_PATH


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _fmt_dt(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


@dataclass
class LicenseRecord:
    id: int
    license_key: str
    customer_name: str
    customer_email: str
    note: str
    status: str
    created_at: str
    expires_at: str
    last_used_at: Optional[str]
    use_count: int

    @property
    def is_valid(self) -> bool:
        if self.status != "active":
            return False
        exp = _parse_dt(self.expires_at)
        return exp is None or exp > _utcnow()

    @property
    def days_left(self) -> Optional[int]:
        exp = _parse_dt(self.expires_at)
        if not exp:
            return None
        delta = exp - _utcnow()
        return max(0, delta.days)


def init_db() -> None:
    LICENSE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS licenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                license_key TEXT NOT NULL UNIQUE,
                customer_name TEXT NOT NULL DEFAULT '',
                customer_email TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_used_at TEXT,
                use_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_licenses_key ON licenses(license_key)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_licenses_status ON licenses(status)"
        )
        conn.commit()


@contextmanager
def _connect():
    conn = sqlite3.connect(str(LICENSE_DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def generate_key(prefix: str = "MT") -> str:
    raw = secrets.token_hex(8).upper()
    return f"{prefix}-{raw[:4]}-{raw[4:8]}-{raw[8:12]}-{raw[12:16]}"


def create_license(
    *,
    customer_name: str = "",
    customer_email: str = "",
    note: str = "",
    valid_days: int = 30,
    expires_at: Optional[datetime] = None,
    license_key: Optional[str] = None,
) -> LicenseRecord:
    init_db()
    key = license_key or generate_key()
    now = _utcnow()
    if expires_at is None:
        expires_at = now + timedelta(days=max(1, valid_days))
    created = _fmt_dt(now)
    exp = _fmt_dt(expires_at)
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO licenses
            (license_key, customer_name, customer_email, note, status, created_at, expires_at)
            VALUES (?, ?, ?, ?, 'active', ?, ?)
            """,
            (key, customer_name.strip(), customer_email.strip(), note.strip(), created, exp),
        )
        conn.commit()
        row_id = cur.lastrowid
    rec = get_license(row_id)
    assert rec is not None
    return rec


def _row_to_record(row: sqlite3.Row) -> LicenseRecord:
    return LicenseRecord(
        id=row["id"],
        license_key=row["license_key"],
        customer_name=row["customer_name"] or "",
        customer_email=row["customer_email"] or "",
        note=row["note"] or "",
        status=row["status"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        last_used_at=row["last_used_at"],
        use_count=row["use_count"] or 0,
    )


def get_license(license_id: int) -> Optional[LicenseRecord]:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM licenses WHERE id = ?", (license_id,)).fetchone()
    return _row_to_record(row) if row else None


def get_by_key(key: str) -> Optional[LicenseRecord]:
    if not key:
        return None
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM licenses WHERE license_key = ?", (key.strip(),)
        ).fetchone()
    return _row_to_record(row) if row else None


def list_licenses(
    status: Optional[str] = None,
    search: str = "",
    limit: int = 200,
) -> List[LicenseRecord]:
    init_db()
    sql = "SELECT * FROM licenses WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if search.strip():
        q = f"%{search.strip()}%"
        sql += " AND (license_key LIKE ? OR customer_name LIKE ? OR customer_email LIKE ? OR note LIKE ?)"
        params.extend([q, q, q, q])
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_record(r) for r in rows]


def revoke_license(license_id: int) -> bool:
    init_db()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE licenses SET status = 'revoked' WHERE id = ? AND status = 'active'",
            (license_id,),
        )
        conn.commit()
        return cur.rowcount > 0


def extend_license(license_id: int, extra_days: int) -> Optional[LicenseRecord]:
    rec = get_license(license_id)
    if not rec:
        return None
    exp = _parse_dt(rec.expires_at) or _utcnow()
    base = exp if exp > _utcnow() else _utcnow()
    new_exp = _fmt_dt(base + timedelta(days=max(1, extra_days)))
    init_db()
    with _connect() as conn:
        conn.execute(
            "UPDATE licenses SET expires_at = ?, status = 'active' WHERE id = ?",
            (new_exp, license_id),
        )
        conn.commit()
    return get_license(license_id)


def record_usage(key: str) -> None:
    rec = get_by_key(key)
    if not rec:
        return
    now = _fmt_dt(_utcnow())
    init_db()
    with _connect() as conn:
        conn.execute(
            "UPDATE licenses SET use_count = use_count + 1, last_used_at = ? WHERE id = ?",
            (now, rec.id),
        )
        conn.commit()


def expire_stale() -> int:
    """Suresi dolmus aktif lisanslari 'expired' yap."""
    init_db()
    now = _fmt_dt(_utcnow())
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE licenses SET status = 'expired'
            WHERE status = 'active' AND expires_at < ?
            """,
            (now,),
        )
        conn.commit()
        return cur.rowcount


def stats_summary() -> dict:
    init_db()
    expire_stale()
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM licenses").fetchone()[0]
        active = conn.execute(
            "SELECT COUNT(*) FROM licenses WHERE status = 'active' AND expires_at >= ?",
            (_fmt_dt(_utcnow()),),
        ).fetchone()[0]
        expired = conn.execute(
            "SELECT COUNT(*) FROM licenses WHERE status = 'expired'"
        ).fetchone()[0]
        revoked = conn.execute(
            "SELECT COUNT(*) FROM licenses WHERE status = 'revoked'"
        ).fetchone()[0]
    return {
        "total": total,
        "active": active,
        "expired": expired,
        "revoked": revoked,
    }
