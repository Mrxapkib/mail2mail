# -*- coding: utf-8 -*-
"""imapsync binary arayuzu — varsa Python'dan 3-5x daha hizli."""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
from typing import Callable, Optional

from .config import IMAPSYNC_BIN
from .engine import MailboxConfig, SyncOptions, SyncStats

LogFn = Callable[[str], None]


def imapsync_available() -> bool:
    return bool(shutil.which(IMAPSYNC_BIN))


def run_imapsync(
    source: MailboxConfig,
    dest: MailboxConfig,
    options: SyncOptions,
    cancel: threading.Event,
    log: LogFn,
    on_progress: Optional[Callable[[dict], None]] = None,
) -> SyncStats:
    stats = SyncStats()
    if not imapsync_available():
        raise RuntimeError("imapsync bulunamadi")

    args = [
        IMAPSYNC_BIN,
        "--host1", source.host,
        "--port1", str(source.port),
        "--user1", source.user,
        "--password1", source.password,
        "--host2", dest.host,
        "--port2", str(dest.port),
        "--user2", dest.user,
        "--password2", dest.password,
        "--ssl1", "SSL" if source.use_ssl else "NO",
        "--ssl2", "SSL" if dest.use_ssl else "NO",
        "--automap",
        "--useheader", "Message-Id",
        "--usecache",
        "--nofoldersizes",
        "--nofoldersizesatend",
        "--errorsmax", "100",
    ]
    if options.skip_duplicates:
        args.append("--skipduplicates")
    if options.delete_from_source:
        args.append("--delete1")
    if options.dry_run:
        args.append("--dry")
    if options.folder_filter:
        args.extend(["--folder", options.folder_filter])
    if dest.subfolder or options.dest_subfolder:
        sub = (dest.subfolder or options.dest_subfolder).strip()
        if sub:
            args.extend(["--subfolder2", sub])

    log("[imapsync] Profesyonel motor baslatildi (en hizli mod)")
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        errors="replace",
    )

    copied = skipped = failed = 0
    for line in proc.stdout or []:
        if cancel.is_set():
            proc.terminate()
            log("[imapsync] Iptal edildi")
            break
        line = line.rstrip()
        if not line:
            continue
        # Sifre satirlarini gosterme
        if "password" in line.lower():
            continue
        log(line)
        m = re.search(r"Messages transferred\s*:\s*(\d+)", line, re.I)
        if m:
            copied = int(m.group(1))
        m = re.search(r"Messages skipped\s*:\s*(\d+)", line, re.I)
        if m:
            skipped = int(m.group(1))
        m = re.search(r"Messages found duplicate\s*:\s*(\d+)", line, re.I)
        if m:
            skipped += int(m.group(1))
        if on_progress:
            on_progress({
                "messages_copied": copied,
                "messages_skipped": skipped,
                "messages_failed": failed,
                "engine": "imapsync",
            })

    rc = proc.wait()
    stats.messages_copied = copied
    stats.messages_skipped = skipped
    if rc != 0 and not cancel.is_set():
        stats.messages_failed = 1
        stats.errors.append(f"imapsync cikis kodu: {rc}")
    return stats
