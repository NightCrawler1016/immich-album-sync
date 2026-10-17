import asyncio
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus, urlparse

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
import bcrypt as _bcrypt
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile
from starlette.middleware.sessions import SessionMiddleware

from .crypto import (
    decrypt_secret as _decrypt_raw,
    encrypt_secret as _encrypt_raw,
    is_encrypted,
)
from .database import SessionLocal, get_db, init_db
from .immich_client import ImmichClient
from .models import Settings, SyncJob, SyncRun
from .scheduler import (
    cron_trigger,
    execute_run,
    get_next_run_time,
    init_scheduler,
    prune_run_history,
    remove_job,
    schedule_job,
)
from .sync import CACHE_BASE, LOG_PATH, release_job, try_acquire_job
from . import backup, notify

# --------------------------------------------------------------------------- #
# App setup
# --------------------------------------------------------------------------- #

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
logger = logging.getLogger(__name__)

SECRET_KEY = os.getenv("SECRET_KEY", "change-me-to-a-unique-random-32-64-char-string")
# App version. Overridable at build/run time (e.g. baked from a git tag) via
# the APP_VERSION env var; falls back to this default otherwise.
APP_VERSION = os.getenv("APP_VERSION", "1.0.0")

# --------------------------------------------------------------------------- #
# Global auth gate
#
# A single app-wide dependency enforces, for every route except the public set:
#   1. an authenticated session, and
#   2. that the forced first-login password change has been completed.
#
# Enforcing this in ONE place (rather than per-route) is deliberate: the
# forced-change gate used to live only on the GET page handlers, so every POST
# and /api/* route silently skipped it — a fresh admin/admin session could
# create jobs or hit /api/test-connection without ever setting a password.
# Applying the check app-wide means no new route can forget it.
# --------------------------------------------------------------------------- #
_PUBLIC_PATHS = {"/health", "/login", "/logout", "/change-password"}


def _path_wants_json(path: str) -> bool:
    """AJAX/JSON endpoints get a status code; page routes get a redirect."""
    return (
        path.startswith("/api/")
        or path == "/logs/stream"
        or path.endswith("/toggle")
        or path.endswith("/run")
    )


def _get_setting(db: Session, key: str) -> Optional[str]:
    row = db.query(Settings).filter(Settings.key == key).first()
    return row.value if row else None


def _logged_in(request: Request, db: Session) -> bool:
    """True if the session has a user AND was issued in the current session epoch.

    Session cookies are stateless and signed, so a copied cookie can't be
    revoked individually. Instead every cookie carries the epoch it was issued
    in; changing the password or username bumps the epoch, which signs out
    every other session at once.
    """
    session = request.session
    if not session.get("user"):
        return False
    if session.get("epoch") != (_get_setting(db, "session_epoch") or "0"):
        session.clear()
        return False
    return True


def _password_change_pending(db: Session) -> bool:
    """True until the default admin password is replaced (fresh installs seed "false").

    Read from the DB on every request rather than cached in the session, so no
    session can carry a stale flag past the forced change.
    """
    return _get_setting(db, "password_changed") == "false"


def _bump_session_epoch(db: Session, request: Request) -> None:
    """Sign out every other session; keep this one signed in."""
    epoch = str(int(_get_setting(db, "session_epoch") or "0") + 1)
    _upsert_setting(db, "session_epoch", epoch)
    request.session["epoch"] = epoch


def _auth_gate(request: Request):
    # Route handlers rely on this gate and do not re-check the session.
    # Use the routed path, NOT request.url.path: Starlette rebuilds request.url
    # from the unvalidated Host header, so "Host: x/static?" would make any
    # path look public (CVE-2026-48710).
    path = request.scope["path"]
    if path in _PUBLIC_PATHS or path.startswith("/static"):
        return
    # A short-lived session of its own: a get_db session stays checked out until
    # the response finishes, so every open /logs/stream would pin a pool connection.
    db = SessionLocal()
    try:
        logged_in = _logged_in(request, db)
        change_pending = logged_in and _password_change_pending(db)
    finally:
        db.close()
    if not logged_in:
        if _path_wants_json(path):
            raise HTTPException(status_code=401, detail="Unauthorized")
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    # Fresh install: nothing but the forced password change until it's done.
    if change_pending:
        if _path_wants_json(path):
            raise HTTPException(status_code=403, detail="Password change required")
        raise HTTPException(status_code=302, headers={"Location": "/change-password"})


app = FastAPI(
    title="Immich Album Sync",
    docs_url=None,
    redoc_url=None,
    dependencies=[Depends(_auth_gate)],
)
# Optional HTTPS (off by default — plain-HTTP LAN use must keep working). With
# HTTPS_CERT_FILE + HTTPS_KEY_FILE the container serves TLS itself (wired in
# entrypoint.sh); SESSION_COOKIE_SECURE=true is for TLS terminated by a reverse
# proxy. Either way the session cookie is marked Secure, which browsers never
# send over plain HTTP — so this must stay off for http:// access.
NATIVE_HTTPS = bool(os.getenv("HTTPS_CERT_FILE") and os.getenv("HTTPS_KEY_FILE"))
SESSION_COOKIE_SECURE = NATIVE_HTTPS or os.getenv("SESSION_COOKIE_SECURE", "").strip().lower() == "true"
app.add_middleware(
    SessionMiddleware, secret_key=SECRET_KEY, max_age=86400,  # 24 h
    https_only=SESSION_COOKIE_SECURE,
)

# --------------------------------------------------------------------------- #
# CSRF protection
#
# State-changing requests must originate from this app's own pages. We compare
# the browser-set Origin (falling back to Referer) host against the Host header.
# Modern browsers always send Origin on POST/fetch, so legitimate same-origin
# requests carry a matching value; we therefore fail CLOSED — a request with no
# usable Origin/Referer on an unsafe method is rejected, not waved through.
# This backs up the SameSite=Lax session cookie without needing a per-form token.
# --------------------------------------------------------------------------- #
_CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


@app.middleware("http")
async def csrf_protect(request: Request, call_next):
    if request.method not in _CSRF_SAFE_METHODS:
        host = request.headers.get("host", "")
        source = request.headers.get("origin") or request.headers.get("referer")
        source_netloc = urlparse(source).netloc if source else ""
        # Fail closed: require a same-host Origin/Referer. Missing header,
        # missing Host, or a cross-origin mismatch all reject.
        if not host or source_netloc != host:
            return JSONResponse(
                {"error": "CSRF validation failed — request origin missing or mismatched."},
                status_code=403,
            )
    response = await call_next(request)
    # Anti-clickjacking: the UI is never meant to be framed. frame-ancestors only —
    # a fuller CSP would block the Tailwind CDN and the inline template scripts.
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    return response


_MAX_BODY_BYTES = 64 * 1024  # every legitimate request body here is a few KB
_RESTORE_PATH = "/settings/restore"
_RESTORE_MAX_BYTES = 2 * 1024 * 1024  # an uploaded backup file


class _BodySizeLimit:
    """Cap request bodies before anything parses them.

    Starlette 0.49 (the newest FastAPI 0.120 allows) has unauthenticated
    form-parsing DoS issues reachable via POST /login. Bytes are counted as
    they stream in, so a chunked body can't sidestep a Content-Length check.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        restore = scope["path"] == _RESTORE_PATH
        limit = _RESTORE_MAX_BYTES if restore else _MAX_BODY_BYTES
        # Reject a declared oversize body up front (browsers always declare it):
        # the error raised mid-stream below can't be caught by a route and ends
        # as a 500, so it is only the backstop for chunked bodies.
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > limit:
            response = (
                _settings_redirect("error", "That file is too large to be a backup.") if restore
                else PlainTextResponse("Request body too large", status_code=413)
            )
            return await response(scope, receive, send)
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(status_code=413, detail="Request body too large")
            return message

        await self.app(scope, limited_receive, send)


# Added last, so it is the outermost layer and wraps every other middleware.
app.add_middleware(_BodySizeLimit)

# Templates directory is relative to the Python package root
_template_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_template_dir))

# Static assets (app icon / favicon / logo). Served publicly with no auth so
# the icon renders on the login and change-password pages too.
_static_dir = Path(__file__).parent / "static"
_static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# Jinja2 global helpers
templates.env.globals["now"] = datetime.utcnow
templates.env.globals["app_version"] = APP_VERSION
# Every timestamp shown in the UI is UTC (naive utcnow() / APScheduler UTC) —
# label it, since sync.log uses the container's local TZ.
templates.env.filters["datetimefmt"] = lambda dt, fmt="%Y-%m-%d %H:%M UTC": (
    dt.strftime(fmt) if dt else "—"
)

def _hash_password(plain: str) -> str:
    return _bcrypt.hashpw(plain.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")

def _verify_password(plain: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# API key encryption helpers (wraps crypto.py with the app SECRET_KEY)
# --------------------------------------------------------------------------- #

def _encrypt_key(plain: str) -> str:
    """Encrypt an API key for database storage."""
    return _encrypt_raw(plain, SECRET_KEY)


def _decrypt_key(stored: str) -> str:
    """Decrypt a stored API key. Falls back gracefully for legacy plaintext."""
    return _decrypt_raw(stored, SECRET_KEY)


# --------------------------------------------------------------------------- #
# Startup / shutdown
# --------------------------------------------------------------------------- #

@app.on_event("startup")
async def startup():
    # Validate SECRET_KEY before anything else. It both signs session cookies
    # AND derives the Fernet key that encrypts stored API keys, so a
    # default/weak value is not merely "not recommended" — with a known value
    # an attacker can forge a valid admin session and decrypt every stored key.
    # Refuse to start rather than run insecurely.
    _key = SECRET_KEY.strip()
    # Every shipped placeholder (code default, Unraid template, docker-compose
    # example) begins with "change-me", so one prefix check rejects them all.
    if _key.lower().startswith("change-me"):
        raise RuntimeError(
            "SECRET_KEY is unset or still a placeholder ('change-me…'). It signs "
            "sessions and encrypts stored API keys, so a known/default value is "
            "insecure. Set a unique 32–64 character SECRET_KEY environment "
            "variable and restart. Refusing to start."
        )
    if len(_key) < 16:
        raise RuntimeError(
            f"SECRET_KEY is only {len(_key)} characters; the minimum is 16 "
            "(32–64 recommended). Set a longer SECRET_KEY and restart. "
            "Refusing to start with a weak key."
        )
    if len(_key) < 32:
        logger.warning(
            f"⚠️  SECRET_KEY is {len(_key)} characters. This meets the "
            "minimum but 32–64 characters is recommended."
        )
    elif len(_key) > 128:
        logger.warning(
            f"⚠️  SECRET_KEY is {len(_key)} characters. "
            "Values longer than 128 characters provide no additional security benefit."
        )
    else:
        logger.info(f"SECRET_KEY: {len(_key)} characters ✓")

    if NATIVE_HTTPS:
        logger.info("HTTPS: serving TLS directly (session cookie marked Secure)")
    elif SESSION_COOKIE_SECURE:
        logger.info("HTTPS: SESSION_COOKIE_SECURE=true — the UI must be reached over https://")

    init_db()
    init_scheduler()

    # One-time run-history cleanup at boot (then daily via the scheduler), so an
    # accumulated backlog is trimmed immediately after an upgrade.
    prune_run_history()

    db = SessionLocal()
    try:
        # One-time: "Mirror deletes" used to be a no-op checkbox. It now really
        # removes photos from destination albums (including ones added there
        # directly), so jobs that ticked it under the old label must opt in again.
        if _get_setting(db, "mirror_deletes_v2") is None:
            cleared = (
                db.query(SyncJob)
                .filter(SyncJob.delete_sync == True)  # noqa: E712
                .update({SyncJob.delete_sync: False}, synchronize_session=False)
            )
            db.add(Settings(key="mirror_deletes_v2", value="true"))
            if cleared:
                logger.warning(
                    f"Mirror deletes now really removes photos from destination "
                    f"albums, so it was switched off on {cleared} job(s). Re-enable "
                    "it per job if you want the destination to match the source."
                )

        # Encrypt API keys saved in plaintext by builds that predate encryption,
        # so "encrypted at rest" holds for every install, not just re-saved jobs.
        for job in db.query(SyncJob).all():
            for attr in ("source_key", "dest_key"):
                value = getattr(job, attr)
                if value and not is_encrypted(value):
                    setattr(job, attr, _encrypt_key(value))

        # Reconcile runs left mid-flight by a previous process. A row can only be
        # "running" if the process that owned it died (restart/crash/OOM) — mark
        # them failed so the UI/history are accurate and the per-job run guard
        # starts from a clean slate.
        stale = db.query(SyncRun).filter(SyncRun.status == "running").all()
        for r in stale:
            r.status = "failed"
            r.finished_at = datetime.utcnow()
            r.error_message = "Interrupted by container restart"
        if stale:
            logger.info(f"Startup: marked {len(stale)} interrupted run(s) as failed")

        # Re-schedule all enabled jobs that survived a restart
        jobs = db.query(SyncJob).filter(SyncJob.enabled == True).all()  # noqa: E712
        for job in jobs:
            try:
                schedule_job(job)
            except Exception as exc:
                logger.error(f"Could not schedule job {job.id}: {exc}")
        db.commit()
        logger.info(f"Startup: scheduled {len(jobs)} enabled sync job(s)")
    finally:
        db.close()


@app.on_event("shutdown")
async def shutdown():
    from .scheduler import scheduler

    if scheduler.running:
        scheduler.shutdown(wait=False)


# --------------------------------------------------------------------------- #
# Login rate limiting (in-memory, per client IP)
#
# Slows credential brute-forcing: after too many failures in a window the IP is
# locked out for a cooldown. State is in-memory (resets on restart), which is
# fine for a single-admin app. Behind a reverse proxy all clients may share the
# proxy IP — the thresholds are lenient enough that a user who knows their
# password will not trip them.
# --------------------------------------------------------------------------- #
_LOGIN_MAX_FAILS = 10      # failures within the window …
_LOGIN_WINDOW = 900        # … measured over 15 minutes …
_LOGIN_LOCKOUT = 300       # … trigger a 5-minute lockout.
_login_state: dict[str, dict] = {}  # ip -> {"fails", "first", "until"}
_login_last_sweep = 0.0


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _login_lock_remaining(ip: str) -> int:
    """Seconds remaining on an active lockout for *ip*, or 0 if not locked."""
    rec = _login_state.get(ip)
    if not rec:
        return 0
    remaining = int(rec.get("until", 0) - time.monotonic())
    return remaining if remaining > 0 else 0


def _record_login_failure(ip: str) -> None:
    global _login_last_sweep
    now = time.monotonic()
    # Every window, forget IPs whose window and lockout have both expired, so a
    # flood from many addresses can't grow this dict without bound.
    if now - _login_last_sweep > _LOGIN_WINDOW:
        for old_ip in [
            k for k, r in _login_state.items()
            if now - r["first"] > _LOGIN_WINDOW and r["until"] <= now
        ]:
            del _login_state[old_ip]
        _login_last_sweep = now
    rec = _login_state.get(ip)
    if not rec or now - rec["first"] > _LOGIN_WINDOW:
        rec = {"fails": 0, "first": now, "until": 0}
    rec["fails"] += 1
    if rec["fails"] >= _LOGIN_MAX_FAILS:
        rec["until"] = now + _LOGIN_LOCKOUT
        rec["fails"] = 0
        rec["first"] = now
    _login_state[ip] = rec


def _record_login_success(ip: str) -> None:
    _login_state.pop(ip, None)


def _get_admin_creds(db: Session) -> tuple[str, Optional[str]]:
    u = db.query(Settings).filter(Settings.key == "admin_username").first()
    p = db.query(Settings).filter(Settings.key == "admin_password_hash").first()
    return (u.value if u else "admin", p.value if p else None)


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


# --------------------------------------------------------------------------- #
# Auth routes
# --------------------------------------------------------------------------- #

@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request, db: Session = Depends(get_db)):
    if _logged_in(request, db):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
async def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    ip = _client_ip(request)
    locked = _login_lock_remaining(ip)
    if locked:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": f"Too many failed attempts. Try again in {locked} seconds.",
            },
            status_code=429,
        )

    admin_username, admin_hash = _get_admin_creds(db)
    # Always run bcrypt, so the response time doesn't reveal a correct username.
    pw_ok = bool(admin_hash) and _verify_password(password, admin_hash)
    if pw_ok and username == admin_username:
        _record_login_success(ip)
        request.session.clear()
        request.session["user"] = username
        request.session["epoch"] = _get_setting(db, "session_epoch") or "0"
        if _password_change_pending(db):
            return RedirectResponse("/change-password", status_code=302)
        return RedirectResponse("/", status_code=302)

    _record_login_failure(ip)
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "Invalid username or password"}
    )


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


# --------------------------------------------------------------------------- #
# Forced first-login password change
# --------------------------------------------------------------------------- #

@app.get("/change-password", response_class=HTMLResponse)
async def change_password_get(request: Request, db: Session = Depends(get_db)):
    """Standalone page displayed when the default password has never been changed."""
    if not _logged_in(request, db):
        return RedirectResponse("/login", status_code=302)
    # Only during the forced first-login change — this form skips the current
    # password; normal changes go through /settings/password, which checks it.
    if not _password_change_pending(db):
        return RedirectResponse("/settings", status_code=302)
    return templates.TemplateResponse(
        "change_password.html", {"request": request, "error": None}
    )


@app.post("/change-password")
async def change_password_post(
    request: Request,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    if not _logged_in(request, db):
        return RedirectResponse("/login", status_code=302)
    if not _password_change_pending(db):
        return RedirectResponse("/settings", status_code=302)

    error = None
    if new_password != confirm_password:
        error = "Passwords do not match."
    elif len(new_password) < 8:
        error = "Password must be at least 8 characters."
    elif new_password.strip().lower() == "admin":
        error = "You cannot reuse the default password. Please choose a unique password."

    if error:
        return templates.TemplateResponse(
            "change_password.html", {"request": request, "error": error}
        )

    _upsert_setting(db, "admin_password_hash", _hash_password(new_password))
    _upsert_setting(db, "password_changed", "true")
    _bump_session_epoch(db, request)
    logger.info("Admin password changed from default on first login")
    return RedirectResponse("/", status_code=302)


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    jobs = db.query(SyncJob).order_by(SyncJob.created_at).all()

    job_cards = []
    for job in jobs:
        last_run = (
            db.query(SyncRun)
            .filter(SyncRun.job_id == job.id)
            .order_by(SyncRun.started_at.desc())
            .first()
        )
        job_cards.append(
            {
                "job": job,
                "last_run": last_run,
                "next_run": get_next_run_time(job.id),
            }
        )

    recent_runs = (
        db.query(SyncRun)
        .order_by(SyncRun.started_at.desc())
        .limit(10)
        .all()
    )

    # Load job name for each run
    for run in recent_runs:
        run._job_name = run.job.name if run.job else "Unknown"

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "job_cards": job_cards,
            "total_jobs": len(jobs),
            "enabled_jobs": sum(1 for j in jobs if j.enabled),
            "recent_runs": recent_runs,
        },
    )


# --------------------------------------------------------------------------- #
# Run history
#
# The dashboard "Recent Runs" widget only shows the latest 10, so a frequent
# job (e.g. */5) quickly buries older failures. This page shows every run in a
# selectable window (default 30 days) filterable by job and status, with a row
# cap so a chatty schedule can't render tens of thousands of rows at once.
# --------------------------------------------------------------------------- #

_HISTORY_MAX_ROWS = 500
_HISTORY_DAY_OPTIONS = (1, 7, 30, 90, 0)  # 0 = all time
_RUN_STATUSES = ("success", "partial", "failed", "running")


@app.get("/history", response_class=HTMLResponse)
async def history(request: Request, db: Session = Depends(get_db)):
    qp = request.query_params
    try:
        days = int(qp.get("days", 30))
    except (TypeError, ValueError):
        days = 30
    if days not in _HISTORY_DAY_OPTIONS:
        days = 30

    status = qp.get("status", "all")
    if status not in _RUN_STATUSES:
        status = "all"

    job_id = None
    if qp.get("job"):
        try:
            job_id = int(qp.get("job"))
        except (TypeError, ValueError):
            job_id = None

    # Base query = time window + optional job (NOT status). Reused for the
    # summary counts and, after adding the status filter, the row list.
    base = db.query(SyncRun)
    if days > 0:
        base = base.filter(SyncRun.started_at >= datetime.utcnow() - timedelta(days=days))
    if job_id is not None:
        base = base.filter(SyncRun.job_id == job_id)

    counts = {
        "total": base.count(),
        "failed": base.filter(SyncRun.status == "failed").count(),
        "partial": base.filter(SyncRun.status == "partial").count(),
        "success": base.filter(SyncRun.status == "success").count(),
    }

    rows_q = base if status == "all" else base.filter(SyncRun.status == status)
    total_matching = rows_q.count()
    runs = rows_q.order_by(SyncRun.started_at.desc()).limit(_HISTORY_MAX_ROWS).all()

    # Attach job names via one lookup (avoids a lazy query per row).
    jobs = db.query(SyncJob).order_by(SyncJob.name).all()
    name_by_id = {j.id: j.name for j in jobs}
    for run in runs:
        run._job_name = name_by_id.get(run.job_id, "Unknown")

    return templates.TemplateResponse(
        "history.html",
        {
            "request": request,
            "runs": runs,
            "jobs": jobs,
            "counts": counts,
            "days": days,
            "status": status,
            "job_id": job_id,
            "total_matching": total_matching,
            "shown": len(runs),
            "truncated": total_matching > len(runs),
            "day_options": _HISTORY_DAY_OPTIONS,
            "statuses": _RUN_STATUSES,
        },
    )


# --------------------------------------------------------------------------- #
# Jobs — list
# --------------------------------------------------------------------------- #

@app.get("/jobs", response_class=HTMLResponse)
async def jobs_list(request: Request, db: Session = Depends(get_db)):
    jobs = db.query(SyncJob).order_by(SyncJob.created_at).all()
    job_rows = []
    for job in jobs:
        last_run = (
            db.query(SyncRun)
            .filter(SyncRun.job_id == job.id)
            .order_by(SyncRun.started_at.desc())
            .first()
        )
        job_rows.append(
            {"job": job, "last_run": last_run, "next_run": get_next_run_time(job.id)}
        )

    return templates.TemplateResponse(
        "jobs.html", {"request": request, "job_rows": job_rows}
    )


# --------------------------------------------------------------------------- #
# Jobs — create
# --------------------------------------------------------------------------- #

def _job_form(request: Request, job: Optional[SyncJob] = None, error: Optional[str] = None):
    # Never send actual key values to the template — they must not appear in HTML
    return templates.TemplateResponse(
        "job_form.html",
        {
            "request": request,
            "job": job,
            "has_source_key": bool(job and job.source_key),
            "has_dest_key": bool(job and job.dest_key),
            "has_job_webhook": bool(job and job.webhook_url),
            "job_webhook_events": list(notify.parse_events(job.webhook_events)) if job else [],
            "error": error,
        },
        status_code=400 if error else 200,
    )


def _set_job_settings(job: SyncJob, name, source_url, source_album_name, dest_url,
                      dest_album_name, schedule, delete_sync, cleanup_cache, enabled,
                      notify_override, webhook_events) -> None:
    """Copy the submitted non-secret form fields onto *job* (keys are set by the caller)."""
    job.name = name.strip()
    job.source_url = source_url.strip().rstrip("/")
    job.source_album_name = source_album_name.strip()
    job.dest_url = dest_url.strip().rstrip("/")
    job.dest_album_name = dest_album_name.strip()
    job.schedule = schedule.strip()
    job.delete_sync = delete_sync == "on"
    job.cleanup_cache = cleanup_cache == "on"
    job.enabled = enabled == "on"
    job.notify_override = notify_override.strip() or "inherit"
    job.webhook_events = notify.events_to_csv(webhook_events or [])


_SECRETS_NOT_SAVED = " The API keys / webhook URL you typed were not saved — enter them again."


def _job_input_error(schedule: str, webhook_url: str) -> Optional[str]:
    """Validate a submitted job before anything is saved, so a bad schedule
    can't be stored and leave the job silently never running."""
    try:
        cron_trigger(schedule)
    except ValueError as exc:
        return f"Invalid schedule '{schedule.strip()}': {exc}"
    if webhook_url.strip() and not notify.is_valid_webhook_url(webhook_url.strip()):
        return "Webhook URL must be an http(s):// address."
    return None


@app.get("/jobs/new", response_class=HTMLResponse)
async def job_new_get(request: Request):
    return _job_form(request)


@app.post("/jobs/new")
async def job_new_post(
    request: Request,
    name: str = Form(...),
    source_url: str = Form(...),
    source_key: str = Form(...),
    source_album_name: str = Form(...),
    dest_url: str = Form(...),
    dest_key: str = Form(...),
    dest_album_name: str = Form(...),
    schedule: str = Form("0 */6 * * *"),
    delete_sync: Optional[str] = Form(None),
    cleanup_cache: Optional[str] = Form(None),
    enabled: Optional[str] = Form(None),
    notify_override: str = Form("inherit"),
    webhook_url: str = Form(""),
    webhook_events: Optional[list] = Form(None),
    db: Session = Depends(get_db),
):
    job = SyncJob()
    _set_job_settings(job, name, source_url, source_album_name, dest_url, dest_album_name,
                      schedule, delete_sync, cleanup_cache, enabled, notify_override,
                      webhook_events)
    error = _job_input_error(schedule, webhook_url)
    if error:
        # Re-show what was typed; the unsaved job carries no secrets, so the
        # API keys must be re-entered (they are never echoed into HTML).
        return _job_form(request, job, error + _SECRETS_NOT_SAVED)

    job.source_key = _encrypt_key(source_key.strip())
    job.dest_key = _encrypt_key(dest_key.strip())
    if webhook_url.strip():
        job.webhook_url = _encrypt_key(webhook_url.strip())
    db.add(job)
    db.commit()
    db.refresh(job)

    try:
        schedule_job(job)
    except Exception as exc:
        logger.error(f"Could not schedule new job {job.id}: {exc}")

    return RedirectResponse("/jobs", status_code=302)


# --------------------------------------------------------------------------- #
# Jobs — edit
# --------------------------------------------------------------------------- #

@app.get("/jobs/{job_id}/edit", response_class=HTMLResponse)
async def job_edit_get(request: Request, job_id: int, db: Session = Depends(get_db)):
    job = db.query(SyncJob).filter(SyncJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_form(request, job)


@app.post("/jobs/{job_id}/edit")
async def job_edit_post(
    request: Request,
    job_id: int,
    name: str = Form(...),
    source_url: str = Form(...),
    source_key: str = Form(""),   # blank = keep existing encrypted key
    source_album_name: str = Form(...),
    dest_url: str = Form(...),
    dest_key: str = Form(""),     # blank = keep existing encrypted key
    dest_album_name: str = Form(...),
    schedule: str = Form("0 */6 * * *"),
    delete_sync: Optional[str] = Form(None),
    cleanup_cache: Optional[str] = Form(None),
    enabled: Optional[str] = Form(None),
    notify_override: str = Form("inherit"),
    webhook_url: str = Form(""),
    webhook_events: Optional[list] = Form(None),
    db: Session = Depends(get_db),
):
    job = db.query(SyncJob).filter(SyncJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    _set_job_settings(job, name, source_url, source_album_name, dest_url, dest_album_name,
                      schedule, delete_sync, cleanup_cache, enabled, notify_override,
                      webhook_events)
    error = _job_input_error(schedule, webhook_url)
    if error:
        # Re-show the submitted values. Nothing is committed: the request's
        # DB session rolls these in-memory changes back when it closes.
        if source_key.strip() or dest_key.strip() or webhook_url.strip():
            error += _SECRETS_NOT_SAVED  # else the "stored" badge implies they were kept
        return _job_form(request, job, error)

    # Only replace the key if a new value was provided — blank means "keep existing"
    if source_key.strip():
        job.source_key = _encrypt_key(source_key.strip())
    if dest_key.strip():
        job.dest_key = _encrypt_key(dest_key.strip())
    # Blank webhook URL means "keep existing" (same convention as API keys).
    if webhook_url.strip():
        job.webhook_url = _encrypt_key(webhook_url.strip())
    job.updated_at = datetime.utcnow()

    db.commit()

    try:
        schedule_job(job)
    except Exception as exc:
        logger.error(f"Could not reschedule job {job.id}: {exc}")

    return RedirectResponse("/jobs", status_code=302)


# --------------------------------------------------------------------------- #
# Jobs — delete
# --------------------------------------------------------------------------- #

@app.post("/jobs/{job_id}/delete")
async def job_delete(job_id: int, db: Session = Depends(get_db)):
    job = db.query(SyncJob).filter(SyncJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404)
    # Hold the job's run reservation so a sync can't be mid-upload from the
    # cache we're about to delete.
    if not try_acquire_job(job_id):
        return RedirectResponse(
            "/jobs?error=A+sync+for+this+job+is+running+-+delete+it+once+the+run+finishes.",
            status_code=302,
        )
    try:
        remove_job(job_id)
        db.delete(job)
        db.commit()
        # The cache holds this job's private originals; SQLite can hand the same
        # id to the next new job, so it must not outlive the job.
        await asyncio.to_thread(
            shutil.rmtree, Path(CACHE_BASE) / f"job_{job_id}", ignore_errors=True
        )
    finally:
        release_job(job_id)
    return RedirectResponse("/jobs", status_code=302)


# --------------------------------------------------------------------------- #
# Jobs — toggle enabled (AJAX)
# --------------------------------------------------------------------------- #

@app.post("/jobs/{job_id}/toggle")
async def job_toggle(job_id: int, db: Session = Depends(get_db)):
    job = db.query(SyncJob).filter(SyncJob.id == job_id).first()
    if not job:
        return JSONResponse({"error": "Not found"}, status_code=404)

    job.enabled = not job.enabled
    try:
        schedule_job(job)
    except Exception as exc:
        # Don't show a job as enabled that can never run (e.g. a schedule
        # saved before validation existed).
        db.rollback()
        return JSONResponse({"error": f"Invalid schedule: {exc}"}, status_code=400)
    db.commit()
    return JSONResponse({"enabled": job.enabled})


# --------------------------------------------------------------------------- #
# Jobs — run now (AJAX)
# --------------------------------------------------------------------------- #

# The event loop only keeps weak references to tasks; hold "Run now" tasks here
# so a running sync can't be garbage-collected mid-flight.
_background_runs: set = set()


@app.post("/jobs/{job_id}/run")
async def job_run_now(job_id: int, db: Session = Depends(get_db)):
    job = db.query(SyncJob).filter(SyncJob.id == job_id).first()
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)

    # Refuse to start a second concurrent run of this job (UI double-click, or a
    # click racing a scheduled fire). Released by execute_run when it finishes.
    if not try_acquire_job(job_id):
        return JSONResponse(
            {"error": "A sync for this job is already running."}, status_code=409
        )

    # Create run record immediately so the UI can follow it
    try:
        run = SyncRun(job_id=job_id, started_at=datetime.utcnow(), status="running")
        db.add(run)
        db.commit()
        db.refresh(run)
    except Exception:
        release_job(job_id)  # execute_run will never run to release it
        raise

    task = asyncio.create_task(execute_run(job_id, run.id))
    _background_runs.add(task)
    task.add_done_callback(_background_runs.discard)

    return JSONResponse({"status": "started", "run_id": run.id})


# --------------------------------------------------------------------------- #
# Logs — page + SSE stream
# --------------------------------------------------------------------------- #

@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    return templates.TemplateResponse("logs.html", {"request": request})


_LOG_TAIL_BYTES = 256 * 1024  # enough for the last 200 lines without reading the whole log


@app.get("/logs/stream")
async def logs_stream(request: Request):
    """Server-Sent Events stream of the sync log file."""

    def event(line: str) -> str:
        return f"data: {json.dumps({'line': line})}\n\n"

    async def generator():
        log_file = Path(LOG_PATH)
        try:
            # Create it on a fresh install so the viewer can follow the first
            # sync instead of erroring and reconnecting in a loop.
            if not log_file.exists():
                log_file.parent.mkdir(parents=True, exist_ok=True)
                log_file.touch()

            # Send the last 200 lines on connect — from the tail only.
            with open(log_file, "rb") as fb:
                start = max(0, fb.seek(0, os.SEEK_END) - _LOG_TAIL_BYTES)
                fb.seek(start)
                lines = fb.read().decode("utf-8", errors="replace").splitlines()
            if start:
                lines = lines[1:]  # first line is probably cut mid-way
            for line in lines[-200:]:
                yield event(line)

            # Follow new lines
            fh = open(log_file, "r", encoding="utf-8", errors="replace")
            try:
                fh.seek(0, os.SEEK_END)
                while not await request.is_disconnected():
                    line = fh.readline()
                    if line:
                        yield event(line.rstrip())
                        continue
                    await asyncio.sleep(0.4)
                    # The log rotates at 10 MB: a fresh file is smaller than our
                    # read position, so reopen it from the start.
                    try:
                        if os.stat(log_file).st_size < fh.tell():
                            fh.close()
                            fh = open(log_file, "r", encoding="utf-8", errors="replace")
                    except FileNotFoundError:
                        pass
            finally:
                fh.close()
        except Exception as exc:
            yield event(f"[stream error] {exc}")

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------- #
# Logs — support bundle download
# --------------------------------------------------------------------------- #

def _scrub_job_secrets(text: str, jobs: list) -> str:
    """Remove each job's API keys and bare server hostnames from free text.

    URL hosts and IPs are masked by notify.redact_text; this catches what it
    can't: the key values themselves and hostnames appearing without a scheme
    (e.g. in a DNS error). The lookarounds keep a Docker hostname like "immich"
    from mangling words such as "immich-go".
    """
    for job in jobs:
        for stored in (job.source_key, job.dest_key):
            key = _decrypt_key(stored) if stored else ""
            if key:
                text = text.replace(key, "[redacted-key]")
        for url in (job.source_url, job.dest_url):
            host = urlparse(url or "").hostname
            if host:
                text = re.sub(
                    rf"(?<![\w.-]){re.escape(host)}(?![\w-])",
                    "[redacted-host]", text, flags=re.IGNORECASE,
                )
    return text


@app.get("/logs/support-bundle")
def download_support_bundle(db: Session = Depends(get_db)):
    """Build and return a ZIP containing logs, sanitized job configs, run history, and system info.

    A plain def: FastAPI runs it in the threadpool, keeping the file read,
    regexes and zip off the event loop.
    """
    jobs = db.query(SyncJob).order_by(SyncJob.created_at).all()

    def redact(text: str) -> str:
        return notify.redact_text(_scrub_job_secrets(text, jobs))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:

        # 1. Sync log file (size-capped by rotation)
        log_file = Path(LOG_PATH)
        if log_file.exists():
            try:
                raw = log_file.read_text(encoding="utf-8", errors="replace")
                # Redact keys, server hosts and IPs so the bundle is safe to share
                zf.writestr("sync.log", redact(raw))
            except Exception as exc:
                zf.writestr("sync.log", f"(error reading log: {exc})\n")
        else:
            zf.writestr("sync.log", "(no log file found)\n")

        # 2. Job configurations — API keys redacted
        jobs_info = []
        for job in jobs:
            jobs_info.append({
                "id": job.id,
                "name": job.name,
                "source_url": notify.redact_text(job.source_url or ""),
                "source_key": "[redacted]",
                "source_album_name": job.source_album_name,
                "dest_url": notify.redact_text(job.dest_url or ""),
                "dest_key": "[redacted]",
                "dest_album_name": job.dest_album_name,
                "schedule": job.schedule,
                "enabled": job.enabled,
                "delete_sync": job.delete_sync,
                "cleanup_cache": job.cleanup_cache,
                "created_at": job.created_at.isoformat() if job.created_at else None,
            })
        zf.writestr("jobs.json", json.dumps(jobs_info, indent=2))

        # 3. Last 100 sync run records
        runs = (
            db.query(SyncRun)
            .order_by(SyncRun.started_at.desc())
            .limit(100)
            .all()
        )
        runs_info = []
        for run in runs:
            runs_info.append({
                "id": run.id,
                "job_id": run.job_id,
                "job_name": run.job.name if run.job else "Unknown",
                "status": run.status,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                "assets_found": run.assets_found,
                "assets_downloaded": run.assets_downloaded,
                "assets_uploaded": run.assets_uploaded,
                "assets_skipped": run.assets_skipped,
                "assets_failed": run.assets_failed,
                "error_message": redact(run.error_message) if run.error_message else None,
            })
        zf.writestr("sync_runs.json", json.dumps(runs_info, indent=2))

        # 4. System info
        immich_go_ver = "unknown"
        try:
            result = subprocess.run(
                ["immich-go", "--version"], capture_output=True, text=True, timeout=5
            )
            immich_go_ver = (result.stdout or result.stderr or "").strip().splitlines()[0]
        except Exception:
            pass

        system_info = {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "python_version": sys.version,
            "immich_go_version": immich_go_ver,
            "log_path": LOG_PATH,
            "db_path": os.getenv("DB_PATH", "/app/appdata/config.db"),
            "cache_path": os.getenv("CACHE_PATH", "/app/appdata/cache"),
            "tz": os.getenv("TZ", "(not set)"),
        }
        zf.writestr("system_info.json", json.dumps(system_info, indent=2))

    buf.seek(0)
    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    filename = f"immich-album-sync-support-{timestamp}.zip"

    return StreamingResponse(
        iter([buf.read()]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

@app.get("/settings", response_class=HTMLResponse)
async def settings_get(request: Request, db: Session = Depends(get_db)):
    settings = {s.key: s.value for s in db.query(Settings).all()}
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "settings": settings,
            "webhook_enabled": (settings.get("webhook_enabled") or "").lower() == "true",
            "webhook_configured": bool(settings.get("webhook_url")),
            "webhook_events": list(notify.parse_events(settings.get("webhook_events"))),
        },
    )


@app.post("/settings/webhook")
async def settings_webhook(
    webhook_enabled: Optional[str] = Form(None),
    webhook_url: str = Form(""),
    webhook_events: Optional[list] = Form(None),
    db: Session = Depends(get_db),
):
    url = webhook_url.strip()
    if url and not notify.is_valid_webhook_url(url):
        return RedirectResponse(
            "/settings?error=Webhook+URL+must+be+an+http(s)+address", status_code=302
        )
    _upsert_setting(db, "webhook_enabled", "true" if webhook_enabled == "on" else "false")
    _upsert_setting(db, "webhook_events", notify.events_to_csv(webhook_events or []))
    # Blank URL means "keep existing" (same convention as API keys) — only
    # replace when a new value is submitted. Encrypted at rest.
    if url:
        _upsert_setting(db, "webhook_url", _encrypt_key(url))
    return RedirectResponse("/settings?success=Notification+settings+saved", status_code=302)


@app.post("/api/test-webhook")
async def api_test_webhook(request: Request, db: Session = Depends(get_db)):
    """Send a sample notification. Body: { url?, job_id? }.

    When url is blank, fall back to the stored URL — the job's own when a
    job_id is given and it uses a custom webhook, otherwise the global one —
    so the "leave blank to keep current" fields can still be tested.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    url = (body.get("url") or "").strip()
    if not url:
        job_id = body.get("job_id")
        if job_id:
            try:
                job = db.query(SyncJob).filter(SyncJob.id == int(job_id)).first()
                if job and job.webhook_url:
                    url = _decrypt_key(job.webhook_url)
            except Exception:
                pass
        if not url:
            row = db.query(Settings).filter(Settings.key == "webhook_url").first()
            if row and row.value:
                url = _decrypt_key(row.value)
    if not url:
        return JSONResponse(
            {"error": "Enter a webhook URL to test (or save one first)."}, status_code=400
        )
    if not notify.is_valid_webhook_url(url):
        return JSONResponse(
            {"error": "Webhook URL must be an http(s):// address."}, status_code=400
        )

    ok, detail = await notify.send_test(url)
    if ok:
        return JSONResponse({"success": True})
    # detail is a sanitized error type (never the URL/token) — safe to surface.
    msg = "Test send failed — check the URL is correct and reachable."
    if detail:
        msg += f" [{detail}]"
    return JSONResponse({"success": False, "error": msg}, status_code=400)


@app.post("/settings/username")
async def settings_username(
    request: Request,
    new_username: str = Form(...),
    db: Session = Depends(get_db),
):
    name = new_username.strip()
    if not name:  # a blank username can never be typed at login — it locks the admin out
        return RedirectResponse("/settings?error=Username+cannot+be+blank", status_code=302)
    _upsert_setting(db, "admin_username", name)
    request.session["user"] = name
    _bump_session_epoch(db, request)
    return RedirectResponse("/settings?success=Username+updated", status_code=302)


@app.post("/settings/password")
async def settings_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    _, admin_hash = _get_admin_creds(db)
    if not admin_hash or not _verify_password(current_password, admin_hash):
        return RedirectResponse("/settings?error=Current+password+incorrect", status_code=302)

    if new_password != confirm_password:
        return RedirectResponse("/settings?error=New+passwords+do+not+match", status_code=302)

    if len(new_password) < 8:
        return RedirectResponse("/settings?error=Password+must+be+at+least+8+characters", status_code=302)

    _upsert_setting(db, "admin_password_hash", _hash_password(new_password))
    _upsert_setting(db, "password_changed", "true")
    _bump_session_epoch(db, request)
    return RedirectResponse("/settings?success=Password+updated+successfully", status_code=302)


# --------------------------------------------------------------------------- #
# Settings — configuration backup / restore (file format: backup.py)
# --------------------------------------------------------------------------- #

def _settings_redirect(kind: str, message: str) -> RedirectResponse:
    return RedirectResponse(f"/settings?{kind}={quote_plus(message)}", status_code=302)


@app.post("/settings/backup")
def settings_backup(
    passphrase: str = Form(""),
    confirm_passphrase: str = Form(""),
    db: Session = Depends(get_db),
):
    """Download every job plus the notification settings as one JSON file.

    A plain def: the passphrase KDF is deliberately slow, so FastAPI runs this
    in the threadpool instead of on the event loop.
    """
    if passphrase != confirm_passphrase:
        return _settings_redirect("error", "Backup passphrases do not match.")
    if passphrase and len(passphrase) < backup.MIN_PASSPHRASE:
        return _settings_redirect(
            "error", f"Backup passphrase must be at least {backup.MIN_PASSPHRASE} characters."
        )
    jobs = db.query(SyncJob).order_by(SyncJob.created_at).all()
    settings = {s.key: s.value for s in db.query(Settings).all()}
    data = backup.export_config(jobs, settings, SECRET_KEY, passphrase, APP_VERSION)
    filename = f"immich-album-sync-backup-{datetime.utcnow():%Y%m%d-%H%M%S}.json"
    return Response(
        data,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.post(_RESTORE_PATH)
async def settings_restore(request: Request, db: Session = Depends(get_db)):
    """Replace every job and the notification settings with a backup's contents.

    The form is parsed here instead of through File()/Form() parameters:
    FastAPI parses declared bodies before it runs dependencies, and this route
    accepts a much larger body — so only requests that passed the auth gate
    are parsed.
    """
    try:
        async with request.form(max_files=1, max_fields=5) as form:
            upload = form.get("backup_file")
            passphrase = form.get("passphrase")
            raw = await upload.read() if isinstance(upload, UploadFile) else b""
    except HTTPException:  # Starlette's 400 for a malformed multipart body
        return _settings_redirect("error", "The upload couldn't be read — try again.")
    if not raw:
        return _settings_redirect("error", "Choose a backup file to restore.")
    try:
        config = await asyncio.to_thread(
            backup.import_config, raw, passphrase if isinstance(passphrase, str) else ""
        )
    except backup.BackupError as exc:
        return _settings_redirect("error", str(exc))

    # Validate everything before touching the database: all or nothing.
    for j in config["jobs"]:
        error = _job_input_error(j["schedule"], j["webhook_url"])
        if error:
            return _settings_redirect("error", f"Nothing was restored — job '{j['name']}': {error}")
    notif = config["notifications"]
    if notif["webhook_url"] and not notify.is_valid_webhook_url(notif["webhook_url"]):
        return _settings_redirect("error", "Nothing was restored — the backup's webhook URL is invalid.")

    old_jobs = db.query(SyncJob).all()
    held = []
    try:
        # Hold every job's run reservation so no sync is mid-flight in a cache
        # we're about to delete, and none can start until the swap is done.
        for job in old_jobs:
            if not try_acquire_job(job.id):
                return _settings_redirect(
                    "error", "A sync is running — restore once it finishes. Nothing was changed."
                )
            held.append(job.id)

        for job in old_jobs:
            db.delete(job)  # its run history goes with it (cascade)
        new_jobs = []
        for j in config["jobs"]:
            j = dict(
                j,
                source_key=_encrypt_key(j["source_key"]),
                dest_key=_encrypt_key(j["dest_key"]),
                webhook_url=_encrypt_key(j["webhook_url"]) or None,
                webhook_events=notify.events_to_csv(j["webhook_events"]),
            )
            # A job restored without its API keys can't run — keep it paused.
            j["enabled"] = j["enabled"] and bool(j["source_key"] and j["dest_key"])
            new_jobs.append(SyncJob(**j))
        db.add_all(new_jobs)

        restored = {
            "webhook_enabled": "true" if notif["enabled"] else "false",
            "webhook_events": notify.events_to_csv(notif["events"]),
        }
        if notif["webhook_url"]:  # blank keeps the current URL, as on the Settings form
            restored["webhook_url"] = _encrypt_key(notif["webhook_url"])
        for key, value in restored.items():
            db.merge(Settings(key=key, value=value))
        db.commit()

        # Old caches hold private originals, and SQLite may hand their ids to
        # the restored jobs.
        for job_id in held:
            remove_job(job_id)
            await asyncio.to_thread(
                shutil.rmtree, Path(CACHE_BASE) / f"job_{job_id}", ignore_errors=True
            )
        for job in new_jobs:
            try:
                schedule_job(job)
            except Exception as exc:
                logger.error(f"Could not schedule restored job {job.id}: {exc}")
    finally:
        for job_id in held:
            release_job(job_id)

    count = len(new_jobs)
    logger.info(f"Configuration restored from a backup: {count} job(s)")
    message = f"Restored {count} job{'' if count == 1 else 's'} and the notification settings."
    if not config["has_secrets"]:
        message += (" The backup has no API keys, so the jobs are paused — edit each job, "
                    "enter its keys (and any custom webhook URL), then enable it.")
    return _settings_redirect("success", message)


def _upsert_setting(db: Session, key: str, value: str):
    row = db.query(Settings).filter(Settings.key == key).first()
    if row:
        row.value = value
    else:
        db.add(Settings(key=key, value=value))
    db.commit()


# --------------------------------------------------------------------------- #
# API — test connection / fetch albums
# --------------------------------------------------------------------------- #

@app.post("/api/test-connection")
async def api_test_connection(request: Request, db: Session = Depends(get_db)):
    """
    Test connectivity and check required API key permissions.

    Body: { url, api_key, role: "source"|"dest", job_id? }
    When api_key is blank and job_id is provided, the stored (encrypted) key
    for that job is used so the form never needs to re-expose the key value.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    url = (body.get("url") or "").strip().rstrip("/")
    api_key = (body.get("api_key") or "").strip()
    role = (body.get("role") or "source").strip()      # "source" or "dest"
    job_id = body.get("job_id")                        # optional — use stored key

    # If the form left the key blank (edit mode), use the stored encrypted key
    if not api_key and job_id:
        try:
            job = db.query(SyncJob).filter(SyncJob.id == int(job_id)).first()
            if job:
                stored = job.source_key if role == "source" else job.dest_key
                api_key = _decrypt_key(stored)
        except Exception:
            pass

    if not url or not api_key:
        return JSONResponse(
            {"error": "URL and API key are required. Enter a key or save the job first."},
            status_code=400,
        )

    try:
        client = ImmichClient(url, api_key)
        permissions, albums = await client.check_permissions(role)

        all_required_ok = all(
            p["ok"] is not False for p in permissions
        )

        return JSONResponse({
            "success": all_required_ok,
            "permissions": permissions,
            "albums": [
                {
                    "id": a["id"],
                    "name": a.get("albumName", ""),
                    "asset_count": a.get("assetCount", 0),
                }
                for a in albums
            ],
        })
    except httpx.ConnectError:
        logger.warning("test-connection: could not connect to %s", url)
        return JSONResponse(
            {"error": "Could not reach the server. Check the URL and that it is "
                      "reachable from the container."},
            status_code=400,
        )
    except httpx.TimeoutException:
        logger.warning("test-connection: timed out connecting to %s", url)
        return JSONResponse(
            {"error": "Connection timed out. Check the URL and your network."},
            status_code=400,
        )
    except httpx.HTTPStatusError as exc:
        logger.warning("test-connection: HTTP %s from %s", exc.response.status_code, url)
        return JSONResponse(
            {"error": f"Server returned HTTP {exc.response.status_code}."},
            status_code=400,
        )
    except Exception:
        # Log full detail server-side; return a generic message to the client.
        logger.exception("test-connection failed (role=%s)", role)
        return JSONResponse(
            {"error": "Connection test failed. Check the URL and API key."},
            status_code=400,
        )


@app.get("/api/jobs/{job_id}/status")
async def api_job_status(job_id: int, db: Session = Depends(get_db)):
    job = db.query(SyncJob).filter(SyncJob.id == job_id).first()
    if not job:
        return JSONResponse({"error": "Not found"}, status_code=404)

    last_run = (
        db.query(SyncRun)
        .filter(SyncRun.job_id == job_id)
        .order_by(SyncRun.started_at.desc())
        .first()
    )
    next_run = get_next_run_time(job_id)

    return JSONResponse(
        {
            "id": job.id,
            "name": job.name,
            "enabled": job.enabled,
            "last_run": {
                "status": last_run.status,
                "started_at": last_run.started_at.isoformat(),
                "assets_uploaded": last_run.assets_uploaded,
            }
            if last_run
            else None,
            "next_run": next_run.isoformat() if next_run else None,
        }
    )
