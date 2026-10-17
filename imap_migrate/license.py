# -*- coding: utf-8 -*-
"""Lisans dogrulama — SQLite veritabani + bitis tarihi kontrolu."""
from __future__ import annotations

from typing import Tuple

from .config import FREE_MODE, FREE_MAX_MB
from .license_db import _parse_dt, _utcnow, expire_stale, get_by_key, record_usage


def validate_license(key: str, record_use: bool = False) -> Tuple[bool, str]:
    """Lisansi dogrula. Donus: (gecerli_mi, mesaj)"""
    if not key or not key.strip():
        return False, "Lisans anahtari girilmedi."

    expire_stale()
    rec = get_by_key(key.strip())
    if not rec:
        return False, "Gecersiz lisans anahtari."

    if rec.status == "revoked":
        return False, "Bu lisans iptal edilmis."

    if rec.status == "expired":
        return False, f"Lisans suresi dolmus ({rec.expires_at})."

    if rec.status != "active":
        return False, "Lisans aktif degil."

    exp = _parse_dt(rec.expires_at)
    if exp and exp <= _utcnow():
        return False, f"Lisans suresi dolmus ({rec.expires_at})."

    if record_use:
        record_usage(key.strip())

    days = rec.days_left
    extra = f" (kalan: {days} gun)" if days is not None else ""
    return True, f"Lisans gecerli{extra}. Bitis: {rec.expires_at}"


def free_sync_allowed(estimated_mb: float = 0) -> tuple[bool, str]:
    if FREE_MODE == "off":
        return True, ""
    if FREE_MODE == "test_only":
        return False, "Tam senkron icin lisans anahtari gerekir. EFT sonrasi size iletilen kodu girin."
    if FREE_MODE == "limited" and estimated_mb <= FREE_MAX_MB:
        return True, ""
    return False, f"Ucretsiz limit {FREE_MAX_MB} MB. Lisans anahtari gerekir."
