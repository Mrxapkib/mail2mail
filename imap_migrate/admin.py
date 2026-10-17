# -*- coding: utf-8 -*-
"""Admin panel — lisans uretimi ve bitis tarihi yonetimi."""
from __future__ import annotations

import functools
import os
from datetime import datetime, timedelta, timezone

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from .config import ADMIN_PASSWORD, SERVICE_NAME
from .license_db import (
    create_license,
    extend_license,
    get_license,
    init_db,
    list_licenses,
    revoke_license,
    stats_summary,
)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin", template_folder="templates")


def _admin_configured() -> bool:
    return bool(ADMIN_PASSWORD)


@admin_bp.before_request
def _require_admin_password_config():
    if request.endpoint and request.endpoint.endswith("login"):
        return None
    if not _admin_configured():
        return (
            render_template(
                "admin_setup.html",
                service_name=SERVICE_NAME,
            ),
            503,
        )
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login", next=request.path))
    return None


def admin_login_required(f):
    @functools.wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin.login"))
        return f(*args, **kwargs)

    return wrapped


@admin_bp.route("/login", methods=["GET", "POST"])
def login():
    if not _admin_configured():
        return render_template("admin_setup.html", service_name=SERVICE_NAME), 503

    if request.method == "POST":
        pwd = request.form.get("password") or ""
        if pwd == ADMIN_PASSWORD:
            session["admin_logged_in"] = True
            session.permanent = True
            nxt = request.args.get("next") or url_for("admin.dashboard")
            return redirect(nxt)
        flash("Hatali sifre.", "error")

    return render_template("admin_login.html", service_name=SERVICE_NAME)


@admin_bp.route("/logout")
def logout():
    session.pop("admin_logged_in", None)
    return redirect(url_for("admin.login"))


@admin_bp.route("/")
@admin_login_required
def dashboard():
    init_db()
    status = request.args.get("status", "").strip() or None
    search = request.args.get("q", "").strip()
    licenses = list_licenses(status=status, search=search)
    summary = stats_summary()
    return render_template(
        "admin_dashboard.html",
        service_name=SERVICE_NAME,
        licenses=licenses,
        summary=summary,
        status_filter=status or "",
        search=search,
    )


@admin_bp.route("/create", methods=["GET", "POST"])
@admin_login_required
def create():
    init_db()
    created = None
    if request.method == "POST":
        name = (request.form.get("customer_name") or "").strip()
        email = (request.form.get("customer_email") or "").strip()
        note = (request.form.get("note") or "").strip()
        days = int(request.form.get("valid_days") or 30)
        custom_exp = (request.form.get("expires_at") or "").strip()

        expires_at = None
        if custom_exp:
            try:
                expires_at = datetime.fromisoformat(custom_exp).replace(tzinfo=timezone.utc)
            except ValueError:
                flash("Gecersiz bitis tarihi formati.", "error")
                return render_template("admin_create.html", service_name=SERVICE_NAME)

        try:
            created = create_license(
                customer_name=name,
                customer_email=email,
                note=note,
                valid_days=days,
                expires_at=expires_at,
            )
            flash(f"Lisans olusturuldu: {created.license_key}", "ok")
        except Exception as e:
            flash(f"Hata: {e}", "error")

    return render_template(
        "admin_create.html",
        service_name=SERVICE_NAME,
        created=created,
        default_days=30,
    )


@admin_bp.route("/license/<int:license_id>/revoke", methods=["POST"])
@admin_login_required
def license_revoke(license_id: int):
    if revoke_license(license_id):
        flash("Lisans iptal edildi.", "ok")
    else:
        flash("Lisans iptal edilemedi.", "error")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/license/<int:license_id>/extend", methods=["POST"])
@admin_login_required
def license_extend(license_id: int):
    days = int(request.form.get("extra_days") or 30)
    rec = extend_license(license_id, days)
    if rec:
        flash(f"Lisans {days} gun uzatildi. Yeni bitis: {rec.expires_at}", "ok")
    else:
        flash("Uzatma basarisiz.", "error")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/license/<int:license_id>")
@admin_login_required
def license_detail(license_id: int):
    rec = get_license(license_id)
    if not rec:
        flash("Lisans bulunamadi.", "error")
        return redirect(url_for("admin.dashboard"))
    return render_template(
        "admin_detail.html",
        service_name=SERVICE_NAME,
        lic=rec,
    )
