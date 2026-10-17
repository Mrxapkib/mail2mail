# -*- coding: utf-8 -*-
"""
PostaAktar — müşteri sitesi + online araç + admin lisans paneli.

Kullanim:
  set IMAP_ADMIN_PASSWORD=gizli-sifre
  python app.py

Admin (lisans): http://127.0.0.1:5055/admin
"""
from __future__ import annotations

import os
import secrets
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, Optional

ROOT = Path(__file__).resolve().parent
PARENT = ROOT.parent
if (ROOT / "imap_migrate").is_dir():
    sys.path.insert(0, str(ROOT))
elif str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

# Marka + admin sifresi (imap_migrate config okumadan once)
os.environ.setdefault("IMAP_SERVICE_NAME", "mail2mail.org")
if os.environ.get("POSTAAKTAR_ADMIN_PASSWORD") and not os.environ.get("IMAP_ADMIN_PASSWORD"):
    os.environ["IMAP_ADMIN_PASSWORD"] = os.environ["POSTAAKTAR_ADMIN_PASSWORD"]

from flask import (  # noqa: E402
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from imap_migrate.admin import admin_bp  # noqa: E402
from imap_migrate.config import SERVICE_NAME  # noqa: E402
from imap_migrate.engine import IMAP_PRESETS, ImapMigrator, MailboxConfig, SyncOptions  # noqa: E402
from imap_migrate.license import free_sync_allowed, validate_license  # noqa: E402
from imap_migrate.license_db import init_db  # noqa: E402

from i18n import LOCALES, DEFAULT, normalize_lang, translate  # noqa: E402

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SECRET_KEY"] = os.environ.get("POSTAAKTAR_SECRET", secrets.token_hex(32))
app.config["PERMANENT_SESSION_LIFETIME"] = 86400 * 7
app.register_blueprint(admin_bp)

PRICE_PRO = int(os.environ.get("POSTAAKTAR_PRICE_PRO", "2490"))
PRICE_SUPPORT = int(os.environ.get("POSTAAKTAR_PRICE_SUPPORT", "4490"))
SUPPORT_EMAIL = os.environ.get("POSTAAKTAR_SUPPORT_EMAIL", "hello@mail2mail.org")
ORDERS_FILE = Path(os.environ.get("POSTAAKTAR_ORDERS", ROOT / "data" / "orders.txt"))

_jobs: Dict[str, dict] = {}
_job_lock = threading.Lock()
_current_job_id: Optional[str] = None


@app.before_request
def _set_lang():
    lang = request.args.get("lang") or session.get("lang") or request.accept_languages.best_match(LOCALES) or DEFAULT
    session["lang"] = normalize_lang(lang)


@app.context_processor
def _inject_i18n():
    lang = session.get("lang", DEFAULT)

    def _(key: str, **kwargs) -> str:
        return translate(lang, key, **kwargs)

    return {
        "_": _,
        "lang": lang,
        "locales": LOCALES,
        "support_email": SUPPORT_EMAIL,
        "service_name": SERVICE_NAME or "mail2mail.org",
    }


@app.get("/lang/<code>")
def set_lang(code: str):
    session["lang"] = normalize_lang(code)
    nxt = request.args.get("next") or request.referrer or url_for("landing")
    return redirect(nxt)


def _auth_ok() -> bool:
    token = os.environ.get("POSTAAKTAR_TOKEN", "").strip()
    if not token:
        if request.remote_addr not in ("127.0.0.1", "::1"):
            return False
        return True
    hdr = request.headers.get("X-Auth-Token") or ""
    body = (request.get_json(silent=True) or {}).get("token") or ""
    return secrets.compare_digest(hdr or body, token)


def _require_auth():
    if not _auth_ok():
        return jsonify({"error": "Unauthorized"}), 401
    return None


def _cfg(prefix: str, data: dict) -> MailboxConfig:
    preset = (data.get(f"{prefix}_preset") or "").strip().lower()
    host = (data.get(f"{prefix}_host") or "").strip()
    port = int(data.get(f"{prefix}_port") or 993)
    if preset in IMAP_PRESETS and not host:
        host = IMAP_PRESETS[preset]["host"]
        port = IMAP_PRESETS[preset]["port"]
    ssl = data.get(f"{prefix}_ssl", data.get("src_ssl", True))
    if isinstance(ssl, str):
        ssl = ssl.lower() in ("1", "true", "yes", "on")
    return MailboxConfig(
        host=host,
        user=(data.get(f"{prefix}_user") or "").strip(),
        password=data.get(f"{prefix}_password") or "",
        port=port,
        use_ssl=bool(ssl),
    )


@app.get("/")
def landing():
    return render_template(
        "landing.html",
        price_pro=PRICE_PRO,
        price_support=PRICE_SUPPORT,
    )


@app.get("/app")
def tool():
    return render_template("app.html")


@app.get("/docs")
def docs():
    return render_template("docs.html")


@app.get("/buy")
def buy_get():
    plan = (request.args.get("plan") or "pro").lower()
    lang = session.get("lang", DEFAULT)
    if plan == "support":
        title, amount = translate(lang, "plan_support_title"), PRICE_SUPPORT
    else:
        plan = "pro"
        title, amount = translate(lang, "plan_pro_title"), PRICE_PRO
    return render_template("buy.html", plan=plan, plan_title=title, amount=amount)


@app.post("/buy")
def buy_post():
    plan = (request.form.get("plan") or "pro").strip()
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip()
    note = (request.form.get("note") or "").strip()
    ORDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with ORDERS_FILE.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{plan}\t{name}\t{email}\t{note}\n")
    lang = session.get("lang", DEFAULT)
    return render_template(
        "buy_thanks.html",
        name=name,
        email=email,
        thanks_title=translate(lang, "buy_thanks_title"),
        thanks_body=translate(lang, "buy_thanks_body", name=name, email=email),
    )


@app.post("/api/license")
def api_license():
    err = _require_auth()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    key = (data.get("license") or "").strip()
    ok, msg = validate_license(key)
    return jsonify({"ok": ok, "message": msg})


@app.post("/api/test")
def api_test():
    err = _require_auth()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    src = _cfg("src", data)
    dst = _cfg("dst", data)
    logs: list[str] = []

    def on_log(m: str):
        logs.append(m)

    try:
        migrator = ImapMigrator(src, dst, SyncOptions(check_only=True), on_log=on_log)
        ok, msg = migrator.test_credentials()
        if not ok:
            return jsonify({"error": msg, "logs": logs}), 400
        return jsonify({"message": msg, "logs": logs})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.post("/api/start")
def api_start():
    err = _require_auth()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    license_key = (data.get("license") or "").strip()
    ok_lic, lic_msg = validate_license(license_key)
    free_ok, free_msg = free_sync_allowed()
    if not ok_lic and not free_ok:
        return jsonify({"error": lic_msg or free_msg or "License required."}), 402

    src = _cfg("src", data)
    dst = _cfg("dst", data)
    opts = SyncOptions(
        dry_run=bool(data.get("dry_run")),
        delete_from_source=bool(data.get("delete")),
    )

    global _current_job_id
    with _job_lock:
        if _current_job_id and _jobs.get(_current_job_id, {}).get("status") == "running":
            return jsonify({"error": "Another job is running."}), 409
        job_id = uuid.uuid4().hex[:12]
        logs: Deque[str] = deque(maxlen=2000)
        job = {
            "id": job_id,
            "status": "running",
            "progress": 1,
            "logs": logs,
            "message": "",
            "seen": 0,
            "migrator": None,
        }
        _jobs[job_id] = job
        _current_job_id = job_id

    def run():
        def on_log(line: str):
            logs.append(line)

        def on_progress(payload: Any):
            if isinstance(payload, dict):
                ft = float(payload.get("folders_total") or 0)
                fd = float(payload.get("folders_done") or 0)
                if ft > 0:
                    job["progress"] = max(1, min(99, int(100 * fd / ft)))
            elif isinstance(payload, (int, float)):
                job["progress"] = float(payload)

        try:
            migrator = ImapMigrator(src, dst, opts, on_log=on_log, on_progress=on_progress)
            job["migrator"] = migrator
            migrator.run()
            cancelled = migrator.cancel.is_set()
            job["status"] = "stopped" if cancelled else "done"
            job["progress"] = 100 if job["status"] == "done" else job["progress"]
            job["message"] = "done" if job["status"] == "done" else "stopped"
        except Exception as e:
            job["status"] = "error"
            job["message"] = str(e)
            logs.append("ERROR: " + str(e))
        finally:
            src.password = ""
            dst.password = ""
            with _job_lock:
                if _current_job_id == job_id:
                    _current_job_id = None

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.get("/api/job/<job_id>")
def api_job(job_id: str):
    err = _require_auth()
    if err:
        return err
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    seen = job.get("seen", 0)
    logs = list(job["logs"])
    new_logs = logs[seen:]
    job["seen"] = len(logs)
    return jsonify(
        {
            "status": job["status"],
            "progress": job.get("progress", 0),
            "message": job.get("message", ""),
            "logs": new_logs,
        }
    )


@app.post("/api/stop")
def api_stop():
    err = _require_auth()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    job_id = data.get("job_id")
    job = _jobs.get(job_id or "")
    if not job:
        return jsonify({"error": "Not found"}), 404
    migrator = job.get("migrator")
    if migrator is not None:
        migrator.stop()
    return jsonify({"ok": True})


def main():
    init_db()
    ORDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    host = os.environ.get("POSTAAKTAR_HOST", "127.0.0.1")
    port = int(os.environ.get("POSTAAKTAR_PORT", "5055"))
    admin_set = bool(os.environ.get("IMAP_ADMIN_PASSWORD", "").strip())
    print(f"mail2mail.org -> http://{host}:{port}")
    print(f"Admin lisans -> http://{host}:{port}/admin" + ("" if admin_set else "  (IMAP_ADMIN_PASSWORD ayarlayin)"))
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
