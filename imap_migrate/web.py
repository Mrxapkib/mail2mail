# -*- coding: utf-8 -*-
"""
IMAP Mailbox Tasima — Web Arayuzu
Yandex ve diger kaynaklardan kendi sunucunuza mail tasima.

Kullanim:
  python -m imap_migrate.web

Guvenlik:
  Varsayilan olarak sadece 127.0.0.1 uzerinde dinler.
  Uzak erisim icin IMAP_MIGRATE_TOKEN ortam degiskeni zorunludur.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict
from typing import Any, Dict, Optional

from flask import Flask, Response, jsonify, render_template, request, session

from .admin import admin_bp
from .config import ADMIN_PASSWORD, FREE_MODE, JOB_RETENTION_SEC, SERVICE_NAME
from .engine import IMAP_PRESETS, ImapMigrator, MailboxConfig, SyncOptions
from .license import free_sync_allowed, validate_license
from .license_db import init_db

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = os.environ.get("IMAP_MIGRATE_SECRET", secrets.token_hex(32))
app.config["PERMANENT_SESSION_LIFETIME"] = 86400 * 7
app.register_blueprint(admin_bp)

# Tek seferde bir is; loglar bellekte tutulur (sifreler asla loglanmaz)
_jobs: Dict[str, dict] = {}
_job_lock = threading.Lock()
_current_job_id: Optional[str] = None


def _auth_ok() -> bool:
    token = os.environ.get("IMAP_MIGRATE_TOKEN", "").strip()
    if not token:
        return True
    hdr = request.headers.get("X-Auth-Token") or request.form.get("token") or ""
    return secrets.compare_digest(hdr, token)


def _require_auth():
    if not _auth_ok():
        return jsonify({"error": "Yetkisiz erisim. IMAP_MIGRATE_TOKEN gerekli."}), 401
    return None


def _parse_config(prefix: str) -> MailboxConfig:
    data = request.get_json(silent=True) or request.form
    preset = (data.get(f"{prefix}_preset") or "").strip().lower()
    host = (data.get(f"{prefix}_host") or "").strip()
    port = int(data.get(f"{prefix}_port") or 993)
    if preset in IMAP_PRESETS and not host:
        host = IMAP_PRESETS[preset]["host"]
        port = IMAP_PRESETS[preset]["port"]
    return MailboxConfig(
        host=host,
        user=(data.get(f"{prefix}_user") or "").strip(),
        password=data.get(f"{prefix}_password") or "",
        port=port,
        use_ssl=str(data.get(f"{prefix}_ssl", "true")).lower() in ("1", "true", "yes"),
        subfolder=(data.get(f"{prefix}_subfolder") or "").strip(),
    )


def _parse_options() -> SyncOptions:
    data = request.get_json(silent=True) or request.form

    def _bool(key: str, default: bool = False) -> bool:
        v = data.get(key, default)
        if isinstance(v, bool):
            return v
        return str(v).lower() in ("1", "true", "yes", "on")

    return SyncOptions(
        dry_run=_bool("dry_run"),
        check_only=_bool("check_only"),
        folder_sizes_only=_bool("folder_sizes_only"),
        create_folders=_bool("create_folders", True),
        skip_duplicates=_bool("skip_duplicates", True),
        delete_from_source=_bool("delete_from_source"),
        strict_dest=_bool("strict_dest"),
        folder_filter=(data.get("folder_filter") or "").strip(),
        dest_subfolder=(data.get("dest_subfolder") or "").strip(),
        max_workers=min(6, max(1, int(data.get("max_workers") or 4))),
    )


def _run_job(job_id: str, source: MailboxConfig, dest: MailboxConfig, options: SyncOptions) -> None:
    job = _jobs[job_id]
    logs: deque = job["logs"]

    def on_log(msg: str) -> None:
        logs.append({"t": time.time(), "msg": msg})
        if len(logs) > 5000:
            logs.popleft()

    def on_progress(p: dict) -> None:
        job["progress"].update(p)

    migrator = ImapMigrator(source, dest, options, on_log=on_log, on_progress=on_progress)
    job["migrator"] = migrator
    job["status"] = "running"
    try:
        stats = migrator.run()
        job["stats"] = {
            "folders_total": stats.folders_total,
            "folders_done": stats.folders_done,
            "messages_total": stats.messages_total,
            "messages_copied": stats.messages_copied,
            "messages_skipped": stats.messages_skipped,
            "messages_failed": stats.messages_failed,
            "bytes_transferred": stats.bytes_transferred,
            "errors": stats.errors,
        }
        job["status"] = "cancelled" if migrator.cancel.is_set() else "done"
    except Exception as e:
        job["status"] = "error"
        on_log(f"[KRITIK HATA] {e}")
        job["stats"] = {"errors": [str(e)]}
    finally:
        # Sifreleri bellekten temizle
        source.password = ""
        dest.password = ""
        global _current_job_id
        with _job_lock:
            if _current_job_id == job_id:
                _current_job_id = None


@app.route("/")
def index():
    token_required = bool(os.environ.get("IMAP_MIGRATE_TOKEN", "").strip())
    return render_template(
        "index.html",
        presets=IMAP_PRESETS,
        token_required=token_required,
        service_name=SERVICE_NAME,
        free_mode=FREE_MODE,
    )


@app.route("/api/status")
def api_status():
    with _job_lock:
        running = _current_job_id is not None
        jid = _current_job_id
    if not jid or jid not in _jobs:
        return jsonify({"running": False})
    job = _jobs[jid]
    return jsonify({
        "running": running,
        "job_id": jid,
        "status": job["status"],
        "progress": job.get("progress", {}),
        "stats": job.get("stats"),
    })


@app.route("/api/start", methods=["POST"])
def api_start():
    global _current_job_id
    err = _require_auth()
    if err:
        return err

    with _job_lock:
        if _current_job_id is not None:
            return jsonify({"error": "Zaten calisan bir senkronizasyon var."}), 409

    source = _parse_config("source")
    dest = _parse_config("dest")
    options = _parse_options()
    data = request.get_json(silent=True) or request.form
    license_key = (data.get("license_key") or "").strip()

    if not source.host or not source.user or not source.password:
        return jsonify({"error": "Kaynak sunucu, kullanici ve sifre zorunlu."}), 400
    if not dest.host or not dest.user or not dest.password:
        return jsonify({"error": "Hedef sunucu, kullanici ve sifre zorunlu."}), 400

    # Ucretli senkron: lisans veya ucretsiz mod
    if not options.check_only and not options.folder_sizes_only and not options.dry_run:
        ok, lic_msg = validate_license(license_key, record_use=True)
        if not ok:
            allowed, msg = free_sync_allowed(0)
            if not allowed:
                return jsonify({"error": lic_msg or msg, "need_license": True}), 402

    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "id": job_id,
        "status": "queued",
        "logs": deque(maxlen=5000),
        "progress": {},
        "stats": None,
        "migrator": None,
        "started": time.time(),
    }
    with _job_lock:
        _current_job_id = job_id

    t = threading.Thread(
        target=_run_job,
        args=(job_id, source, dest, options),
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    err = _require_auth()
    if err:
        return err
    with _job_lock:
        jid = _current_job_id
    if not jid or jid not in _jobs:
        return jsonify({"error": "Calisan is yok."}), 404
    m = _jobs[jid].get("migrator")
    if m:
        m.stop()
    return jsonify({"ok": True})


@app.route("/api/logs/<job_id>")
def api_logs(job_id: str):
    since = float(request.args.get("since", 0))
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"logs": [], "status": "unknown"})
    entries = [e for e in job["logs"] if e["t"] > since]
    return jsonify({
        "logs": entries,
        "status": job["status"],
        "progress": job.get("progress", {}),
        "stats": job.get("stats"),
    })


@app.route("/api/check", methods=["POST"])
def api_check():
    err = _require_auth()
    if err:
        return err
    source = _parse_config("source")
    dest = _parse_config("dest")
    if not all([source.host, source.user, source.password, dest.host, dest.user, dest.password]):
        return jsonify({"error": "Tum alanlar doldurulmali."}), 400
    logs = []

    def on_log(m: str):
        logs.append(m)

    m = ImapMigrator(source, dest, SyncOptions(check_only=True), on_log=on_log)
    ok, msg = m.test_credentials()
    return jsonify({"ok": ok, "message": msg, "logs": logs})


@app.route("/api/validate-license", methods=["POST"])
def api_validate_license():
    data = request.get_json(silent=True) or request.form
    key = (data.get("license_key") or "").strip()
    ok, msg = validate_license(key, record_use=False)
    return jsonify({"ok": ok, "message": msg})


def main():
    init_db()
    host = os.environ.get("IMAP_MIGRATE_HOST", "127.0.0.1")
    port = int(os.environ.get("IMAP_MIGRATE_PORT", "8765"))
    token = os.environ.get("IMAP_MIGRATE_TOKEN", "").strip()

    print("=" * 60)
    print("  IMAP Mailbox Tasima Araci")
    print(f"  Musteri:  http://{host}:{port}/")
    print(f"  Admin:    http://{host}:{port}/admin")
    if ADMIN_PASSWORD:
        print("  Admin sifresi: AYARLI")
    else:
        print("  UYARI: IMAP_ADMIN_PASSWORD tanimlayin (lisans paneli)")
    if host not in ("127.0.0.1", "localhost"):
        if token:
            print("  Token korumasi: AKTIF")
        else:
            print("  UYARI: Uzak erisim icin IMAP_MIGRATE_TOKEN ayarlayin!")
    print("=" * 60)
    app.run(host=host, port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
