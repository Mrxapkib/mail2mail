# -*- coding: utf-8 -*-
"""IMAP mailbox migration engine — Yandex ve diger sunucular arasi hizli senkron."""
from __future__ import annotations

import base64
import email
import hashlib
import imaplib
import re
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Set, Tuple

LogFn = Callable[[str], None]
ProgressFn = Callable[[dict], None]

# Yandex ve yaygin IMAP sunuculari
IMAP_PRESETS = {
    "yandex": {"host": "imap.yandex.com", "port": 993, "label": "Yandex"},
    "gmail": {"host": "imap.gmail.com", "port": 993, "label": "Gmail"},
    "outlook": {"host": "outlook.office365.com", "port": 993, "label": "Outlook"},
    "icloud": {"host": "imap.mail.me.com", "port": 993, "label": "iCloud"},
    "yahoo": {"host": "imap.mail.yahoo.com", "port": 993, "label": "Yahoo"},
    "zoho": {"host": "imap.zoho.com", "port": 993, "label": "Zoho"},
    "mailru": {"host": "imap.mail.ru", "port": 993, "label": "Mail.ru"},
}


@dataclass
class MailboxConfig:
    host: str
    user: str
    password: str
    port: int = 993
    use_ssl: bool = True
    subfolder: str = ""


@dataclass
class SyncOptions:
    dry_run: bool = False
    check_only: bool = False
    folder_sizes_only: bool = False
    create_folders: bool = True
    skip_duplicates: bool = True
    delete_from_source: bool = False
    strict_dest: bool = False
    folder_filter: str = ""
    dest_subfolder: str = ""
    max_workers: int = 4
    max_retries: int = 3
    use_imapsync: bool = True


@dataclass
class SyncStats:
    folders_total: int = 0
    folders_done: int = 0
    messages_total: int = 0
    messages_copied: int = 0
    messages_skipped: int = 0
    messages_failed: int = 0
    bytes_transferred: int = 0
    errors: List[str] = field(default_factory=list)


def _log_safe(msg: str) -> str:
    """Sifreleri loglardan maskele."""
    return re.sub(
        r"(password|passwd|auth)[=:]\s*\S+",
        r"\1=***",
        msg,
        flags=re.IGNORECASE,
    )


class ImapConnection:
    def __init__(self, cfg: MailboxConfig, label: str = ""):
        self.cfg = cfg
        self.label = label
        self.conn: Optional[imaplib.IMAP4_SSL | imaplib.IMAP4] = None

    def connect(self) -> None:
        if self.cfg.use_ssl:
            ctx = ssl.create_default_context()
            self.conn = imaplib.IMAP4_SSL(self.cfg.host, self.cfg.port, ssl_context=ctx)
        else:
            self.conn = imaplib.IMAP4(self.cfg.host, self.cfg.port)
            if self.cfg.port == 993:
                ctx = ssl.create_default_context()
                self.conn.starttls(ssl_context=ctx)
        typ, data = self.conn.login(self.cfg.user, self.cfg.password)
        if typ != "OK":
            raise ConnectionError(f"{self.label} giris basarisiz: {data}")

    def close(self) -> None:
        if self.conn:
            try:
                self.conn.logout()
            except Exception:
                pass
            self.conn = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.close()


def _decode_modified_utf7(name: str) -> str:
    """IMAP klasor adlarini (modified UTF-7) Unicode'a cevir."""
    def _repl(m: re.Match) -> str:
        chunk = m.group(1)
        if chunk == "":
            return "&"
        b64 = chunk.replace(",", "/")
        pad = "=" * ((4 - len(b64) % 4) % 4)
        try:
            return base64.b64decode(b64 + pad).decode("utf-16-be")
        except Exception:
            return m.group(0)

    return re.sub(r"&([^-]*)-", _repl, name)


def _decode_folder_name(raw: bytes | str) -> str:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return _decode_modified_utf7(raw)


def _quote_mailbox(folder: str) -> str:
    escaped = folder.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def list_folders(conn: imaplib.IMAP4) -> List[str]:
    typ, data = conn.list()
    if typ != "OK" or not data:
        return ["INBOX"]
    folders = []
    seen = set()
    for item in data:
        if not item:
            continue
        line = item.decode("utf-8", errors="replace") if isinstance(item, bytes) else str(item)
        m = re.search(r'\)\s+".*?"\s+(.*)$', line)
        if not m:
            parts = line.rsplit(" ", 1)
            name_raw = parts[1].strip() if len(parts) == 2 else ""
        else:
            name_raw = m.group(1).strip()
        name = _decode_folder_name(name_raw)
        if name and name not in seen and "\\Noselect" not in line:
            seen.add(name)
            folders.append(name)
    return folders or ["INBOX"]


def folder_message_count(conn: imaplib.IMAP4, folder: str) -> int:
    typ, _ = conn.select(f'"{folder}"' if " " in folder else folder, readonly=True)
    if typ != "OK":
        return 0
    typ, data = conn.search(None, "ALL")
    if typ != "OK" or not data or not data[0]:
        return 0
    return len(data[0].split())


def _select_folder(conn: imaplib.IMAP4, folder: str, readonly: bool = False) -> bool:
    typ, _ = conn.select(_quote_mailbox(folder), readonly=readonly)
    return typ == "OK"


def _ensure_folder(conn: imaplib.IMAP4, folder: str) -> bool:
    typ, _ = conn.create(_quote_mailbox(folder))
    return typ == "OK"


def _message_fingerprint(raw: bytes) -> str:
    """Message-ID yoksa konu+gönderen+tarih+boyut ile parmak izi."""
    try:
        msg = email.message_from_bytes(raw)
        parts = [
            (msg.get("From") or "").strip().lower(),
            (msg.get("Subject") or "").strip().lower(),
            (msg.get("Date") or "").strip(),
            str(len(raw)),
        ]
        return hashlib.sha256("|".join(parts).encode("utf-8", errors="replace")).hexdigest()
    except Exception:
        return hashlib.sha256(raw[:4096]).hexdigest()


def _extract_message_id(raw: bytes) -> Optional[str]:
    try:
        msg = email.message_from_bytes(raw)
        mid = msg.get("Message-ID") or msg.get("Message-Id")
        if mid:
            return mid.strip()
    except Exception:
        pass
    return None


def _build_dest_index(conn: imaplib.IMAP4, folder: str) -> Tuple[Set[str], Set[str]]:
    """Hedefteki Message-ID ve parmak izi setleri (cift katmanli dedup)."""
    ids: Set[str] = set()
    fps: Set[str] = set()
    if not _select_folder(conn, folder, readonly=True):
        return ids, fps
    typ, data = conn.search(None, "ALL")
    if typ != "OK" or not data or not data[0]:
        return ids, fps
    uids = data[0].split()
    batch = 200
    for i in range(0, len(uids), batch):
        chunk = b",".join(uids[i : i + batch])
        typ, fetched = conn.fetch(
            chunk,
            "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT DATE)])",
        )
        if typ != "OK" or not fetched:
            continue
        for part in fetched:
            if isinstance(part, tuple) and len(part) >= 2:
                header = part[1]
                if isinstance(header, bytes):
                    blob = header + b"\r\n\r\n"
                    mid = _extract_message_id(blob)
                    if mid:
                        ids.add(mid)
                    fps.add(_message_fingerprint(blob))
    return ids, fps


def _clean_flags(raw_flags: str) -> str:
    parts = [p for p in raw_flags.split() if p and p not in ("\\Recent",)]
    return "(" + " ".join(parts) + ")"


def _parse_internal_date(fetch_data: bytes) -> Optional[str]:
    m = re.search(rb'INTERNALDATE "([^"]+)"', fetch_data)
    if m:
        return m.group(1).decode("ascii", errors="replace")
    return None


def _sync_one_folder(
    source_cfg: MailboxConfig,
    dest_cfg: MailboxConfig,
    src_folder: str,
    dest_folder: str,
    options: SyncOptions,
    cancel: threading.Event,
    log: LogFn,
) -> Tuple[int, int, int, int]:
    """Tek klasoru senkronize et. Donus: copied, skipped, failed, bytes."""
    copied = skipped = failed = nbytes = 0
    src = ImapConnection(source_cfg, "Kaynak")
    dst = ImapConnection(dest_cfg, "Hedef")
    try:
        src.connect()
        dst.connect()
        if options.create_folders and not options.dry_run:
            _ensure_folder(dst.conn, dest_folder)

        if not _select_folder(src.conn, src_folder, readonly=not options.delete_from_source):
            log(f"  [!] Kaynak klasor acilamadi: {src_folder}")
            return 0, 0, 1, 0

        typ, data = src.conn.uid("SEARCH", None, "ALL")
        if typ != "OK" or not data or not data[0]:
            log(f"  [i] Bos klasor: {src_folder}")
            return 0, 0, 0, 0

        uids = data[0].split()
        log(f"  -> {src_folder} ({len(uids)} mesaj) => {dest_folder}")

        dest_ids: Set[str] = set()
        dest_fps: Set[str] = set()
        source_ids: Set[str] = set()
        if options.skip_duplicates and not options.dry_run:
            dest_ids, dest_fps = _build_dest_index(dst.conn, dest_folder)
            if dest_ids or dest_fps:
                log(f"     Hedefte {len(dest_ids)} ID + {len(dest_fps)} parmak izi indekslendi")

        if not _select_folder(dst.conn, dest_folder, readonly=False):
            if options.create_folders:
                _ensure_folder(dst.conn, dest_folder)
                _select_folder(dst.conn, dest_folder, readonly=False)
            else:
                log(f"  [!] Hedef klasor acilamadi: {dest_folder}")
                return 0, 0, len(uids), 0

        if not _select_folder(src.conn, src_folder, readonly=not options.delete_from_source):
            return 0, 0, len(uids), 0

        quoted_dest = _quote_mailbox(dest_folder)

        for idx, uid in enumerate(uids, 1):
            if cancel.is_set():
                log("  [x] Iptal edildi")
                break

            if options.dry_run:
                copied += 1
                continue

            typ, fetched = src.conn.uid("FETCH", uid, "(FLAGS INTERNALDATE BODY.PEEK[])")
            if typ != "OK" or not fetched:
                failed += 1
                continue

            raw_msg = None
            flags = "(\\Seen)"
            internal_date = None
            for part in fetched:
                if isinstance(part, tuple) and len(part) >= 2:
                    meta = part[0] if isinstance(part[0], bytes) else b""
                    raw_msg = part[1]
                    internal_date = _parse_internal_date(meta)
                    fm = re.search(rb"FLAGS \(([^)]*)\)", meta)
                    if fm:
                        flags = _clean_flags(fm.group(1).decode("ascii", errors="replace"))

            if not raw_msg or not isinstance(raw_msg, bytes):
                failed += 1
                continue

            mid = _extract_message_id(raw_msg)
            if mid:
                source_ids.add(mid)
            if options.skip_duplicates:
                fp = _message_fingerprint(raw_msg)
                if mid and mid in dest_ids:
                    skipped += 1
                    continue
                if fp in dest_fps:
                    skipped += 1
                    continue
                if mid:
                    dest_ids.add(mid)
                dest_fps.add(fp)

            appended = False
            last_err = ""
            for attempt in range(1, options.max_retries + 1):
                try:
                    if internal_date:
                        typ, _ = dst.conn.append(quoted_dest, flags, internal_date, raw_msg)
                    else:
                        typ, _ = dst.conn.append(quoted_dest, flags, None, raw_msg)
                    if typ == "OK":
                        appended = True
                        break
                    last_err = f"IMAP APPEND: {typ}"
                except Exception as e:
                    last_err = str(e)
                    if attempt < options.max_retries:
                        time.sleep(0.5 * attempt)
            if not appended:
                failed += 1
                log(f"     Hata UID {uid.decode()}: {last_err}")
                continue

            copied += 1
            nbytes += len(raw_msg)
            if options.delete_from_source:
                src.conn.uid("STORE", uid, "+FLAGS", "\\Deleted")

            if idx % 50 == 0:
                log(f"     ... {idx}/{len(uids)} islendi")

        if options.strict_dest and not options.dry_run and not cancel.is_set() and source_ids:
            if _select_folder(dst.conn, dest_folder, readonly=False):
                typ, ddata = dst.conn.uid("SEARCH", None, "ALL")
                if typ == "OK" and ddata and ddata[0]:
                    removed = 0
                    for duid in ddata[0].split():
                        typ, fetched = dst.conn.uid(
                            "FETCH",
                            duid,
                            "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])",
                        )
                        if typ != "OK" or not fetched:
                            continue
                        blob = b""
                        for part in fetched:
                            if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], bytes):
                                blob = part[1]
                        dmid = _extract_message_id(blob + b"\r\n\r\n") if blob else None
                        if dmid and dmid not in source_ids:
                            dst.conn.uid("STORE", duid, "+FLAGS", "\\Deleted")
                            removed += 1
                    if removed:
                        dst.conn.expunge()
                        log(f"     Hedefte {removed} fazla mesaj silindi")

        if options.delete_from_source and not options.dry_run and not cancel.is_set():
            src.conn.expunge()

    except Exception as e:
        log(f"  [!] Klasor hatasi ({src_folder}): {e}")
        failed += 1
    finally:
        src.close()
        dst.close()

    return copied, skipped, failed, nbytes


class ImapMigrator:
    def __init__(
        self,
        source: MailboxConfig,
        dest: MailboxConfig,
        options: Optional[SyncOptions] = None,
        on_log: Optional[LogFn] = None,
        on_progress: Optional[ProgressFn] = None,
    ):
        self.source = source
        self.dest = dest
        self.options = options or SyncOptions()
        self.on_log = on_log or (lambda m: None)
        self.on_progress = on_progress or (lambda p: None)
        self.cancel = threading.Event()
        self.stats = SyncStats()

    def log(self, msg: str) -> None:
        self.on_log(_log_safe(msg))

    def progress(self, **kwargs) -> None:
        payload = {
            "folders_total": self.stats.folders_total,
            "folders_done": self.stats.folders_done,
            "messages_copied": self.stats.messages_copied,
            "messages_skipped": self.stats.messages_skipped,
            "messages_failed": self.stats.messages_failed,
            "bytes_transferred": self.stats.bytes_transferred,
            **kwargs,
        }
        self.on_progress(payload)

    def stop(self) -> None:
        self.cancel.set()

    def test_credentials(self) -> Tuple[bool, str]:
        for label, cfg in [("Kaynak", self.source), ("Hedef", self.dest)]:
            try:
                with ImapConnection(cfg, label):
                    self.log(f"[OK] {label} baglantisi basarili: {cfg.user} @ {cfg.host}")
            except Exception as e:
                return False, f"{label} baglanti hatasi: {e}"
        return True, "Her iki sunucuya da basariyla baglanildi."

    def folder_sizes(self) -> List[dict]:
        result = []
        with ImapConnection(self.source, "Kaynak") as src:
            folders = list_folders(src.conn)
            for f in folders:
                if self.options.folder_filter and self.options.folder_filter not in f:
                    continue
                cnt = folder_message_count(src.conn, f)
                result.append({"folder": f, "messages": cnt})
                self.log(f"  {f}: {cnt} mesaj")
        return result

    def _dest_folder_name(self, src_folder: str) -> str:
        prefix = (self.dest.subfolder or self.options.dest_subfolder or "").strip().rstrip("/")
        if not prefix:
            return src_folder
        if src_folder.upper() == "INBOX":
            return prefix
        return f"{prefix}/{src_folder}"

    def run(self) -> SyncStats:
        t0 = time.time()
        self.log("=== IMAP Mailbox Tasima Basladi ===")
        self.log(f"Kaynak: {self.source.user} @ {self.source.host}")
        self.log(f"Hedef:  {self.dest.user} @ {self.dest.host}")
        self.log("Guvenlik: Sifreler kaydedilmez, islem bitince bellekten silinir.")

        if self.options.check_only:
            ok, msg = self.test_credentials()
            self.log(msg)
            return self.stats

        if self.options.folder_sizes_only:
            self.log("--- Klasor boyutlari ---")
            self.folder_sizes()
            return self.stats

        ok, msg = self.test_credentials()
        if not ok:
            self.log(f"[HATA] {msg}")
            self.stats.errors.append(msg)
            return self.stats

        # Linux sunucuda imapsync varsa otomatik (en hizli)
        if self.options.use_imapsync and not self.options.dry_run:
            try:
                from .imapsync_backend import imapsync_available, run_imapsync

                if imapsync_available():
                    stats = run_imapsync(
                        self.source,
                        self.dest,
                        self.options,
                        self.cancel,
                        self.log,
                        self.on_progress,
                    )
                    self.stats = stats
                    elapsed = time.time() - t0
                    self.log("=== Tamamlandi (imapsync) ===")
                    self.log(
                        f"Kopyalanan: {stats.messages_copied} | "
                        f"Atlanan: {stats.messages_skipped} | Hatali: {stats.messages_failed}"
                    )
                    self.log(f"Sure: {elapsed:.1f} sn")
                    self.progress(status="done", engine="imapsync")
                    return self.stats
            except Exception as e:
                self.log(f"[i] imapsync kullanilamadi, Python motoru: {e}")

        with ImapConnection(self.source, "Kaynak") as src:
            folders = list_folders(src.conn)

        prefix = (self.source.subfolder or "").strip().strip("/")
        if prefix:
            folders = [f for f in folders if f == prefix or f.startswith(prefix + "/")]
        if self.options.folder_filter:
            folders = [f for f in folders if self.options.folder_filter in f]

        # INBOX once, sonra digerleri
        folders.sort(key=lambda f: (0 if f.upper() == "INBOX" else 1, f))
        self.stats.folders_total = len(folders)

        total_msgs = 0
        with ImapConnection(self.source, "Kaynak") as src:
            for f in folders:
                total_msgs += folder_message_count(src.conn, f)
        self.stats.messages_total = total_msgs
        self.log(f"Toplam {len(folders)} klasor, ~{total_msgs} mesaj")
        self.progress(messages_total=total_msgs)

        workers = min(self.options.max_workers, len(folders), 6)
        if workers < 1:
            workers = 1

        def _task(folder: str):
            dest_folder = self._dest_folder_name(folder)
            return folder, _sync_one_folder(
                self.source,
                self.dest,
                folder,
                dest_folder,
                self.options,
                self.cancel,
                self.log,
            )

        if workers == 1 or self.options.dry_run:
            for folder in folders:
                if self.cancel.is_set():
                    break
                self.progress(current_folder=folder)
                _, (c, s, f, b) = _task(folder)
                self.stats.messages_copied += c
                self.stats.messages_skipped += s
                self.stats.messages_failed += f
                self.stats.bytes_transferred += b
                self.stats.folders_done += 1
                self.progress(current_folder=folder)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_task, f): f for f in folders}
                for fut in as_completed(futures):
                    if self.cancel.is_set():
                        break
                    folder = futures[fut]
                    try:
                        _, (c, s, f, b) = fut.result()
                        self.stats.messages_copied += c
                        self.stats.messages_skipped += s
                        self.stats.messages_failed += f
                        self.stats.bytes_transferred += b
                    except Exception as e:
                        self.log(f"[!] {folder}: {e}")
                        self.stats.errors.append(str(e))
                    self.stats.folders_done += 1
                    self.progress(current_folder=folder)

        elapsed = time.time() - t0
        mb = self.stats.bytes_transferred / (1024 * 1024)
        speed = mb / elapsed if elapsed > 0 else 0
        self.log("=== Tamamlandi ===")
        self.log(
            f"Kopyalanan: {self.stats.messages_copied} | "
            f"Atlanan (dup): {self.stats.messages_skipped} | "
            f"Hatali: {self.stats.messages_failed}"
        )
        self.log(f"Aktarilan: {mb:.2f} MB | Sure: {elapsed:.1f} sn | Hiz: {speed:.2f} MB/s")
        self.progress(status="done", elapsed_sec=round(elapsed, 1), speed_mbps=round(speed, 2))
        return self.stats
