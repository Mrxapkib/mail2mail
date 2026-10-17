# -*- coding: utf-8 -*-
"""Ticari hizmet ayarlari — ortam degiskenleri veya .env ile yonetin."""
import os
from pathlib import Path

# Ucretsiz: sadece baglanti testi ve klasor boyutu (tam senkron icin lisans gerekir)
FREE_MODE = os.environ.get("IMAP_FREE_MODE", "test_only").lower()
# test_only | limited | off
# limited = IMAP_FREE_MAX_MB altina kadar ucretsiz senkron

FREE_MAX_MB = int(os.environ.get("IMAP_FREE_MAX_MB", "500"))

# Lisans veritabani (SQLite)
LICENSE_DB_PATH = Path(
    os.environ.get("IMAP_LICENSE_DB", Path(__file__).parent / "data" / "licenses.db")
)

# Admin panel sifresi (zorunlu — uzak sunucuda mutlaka ayarlayin)
ADMIN_PASSWORD = os.environ.get("IMAP_ADMIN_PASSWORD", "").strip()

# Eski dosya tabanli lisans (artik kullanilmiyor; sadece geri uyumluluk)
LICENSE_FILE = Path(os.environ.get("IMAP_LICENSE_FILE", Path(__file__).parent / "licenses.txt"))

# Is bitince loglari kac saniye tutulsun (sifreler zaten hic kaydedilmez)
JOB_RETENTION_SEC = int(os.environ.get("IMAP_JOB_RETENTION_SEC", "3600"))

# Maksimum eszamanli is (sunucu kapasitesine gore)
MAX_CONCURRENT_JOBS = int(os.environ.get("IMAP_MAX_CONCURRENT_JOBS", "1"))

# imapsync binary yolu (Linux sunucuda cok daha hizli — varsa otomatik kullanilir)
IMAPSYNC_BIN = os.environ.get("IMAPSYNC_BIN", "imapsync")

# Hizmet markasi
SERVICE_NAME = os.environ.get("IMAP_SERVICE_NAME", "MailTaşı Pro")
SERVICE_URL = os.environ.get("IMAP_SERVICE_URL", "")
