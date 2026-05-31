import asyncio
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
import bcrypt as _bcrypt
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .crypto import decrypt_secret as _decrypt_raw, encrypt_secret as _encrypt_raw
from .database import get_db, init_db
from .immich_client import ImmichClient
from .models import Settings, SyncJob, SyncRun
from .scheduler import (
    get_next_run_time,
    init_scheduler,
    remove_job,
    schedule_job,
)
from .sync import LOG_PATH, run_sync_job

# --------------------------------------------------------------------------- #
# App setup
# --------------------------------------------------------------------------- #

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
logger = logging.getLogger(__name__)

SECRET_KEY = os.getenv("SECRET_KEY", "change-me-to-something-random-and-long")
# App version. Overridable at build/run time (e.g. baked from a git tag) via
# the APP_VERSION env var; falls back to this default otherwise.
APP_VERSION = os.getenv("APP_VERSION", "1.0.0")

app = FastAPI(title="Immich Album Sync", docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=86400)  # 24 h

# --------------------------------------------------------------------------- #
# CSRF protection
#
# State-changing requests must originate from this app's own pages. We compare
# the browser-set Origin (falling back to Referer) host against the Host header
# and reject cross-site requests. Modern browsers always send Origin on POST,
# so legitimate same-origin form posts and fetch() calls pass; a malicious
# third-party page cannot forge a matching Origin. This complements the
# SameSite=Lax session cookie set above (which already blocks cross-site
# cookie-bearing POSTs) without needing a token in every form.
# --------------------------------------------------------------------------- #
_CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


@app.middleware("http")
async def csrf_protect(request: Request, call_next):
    if request.method not in _CSRF_SAFE_METHODS:
        host = request.headers.get("host", "")
        source = request.headers.get("origin") or request.headers.get("referer")
        if source:
            netloc = urlparse(source).netloc
            # Block only on a clear cross-origin mismatch. When neither header
            # is present (rare for browsers) the SameSite cookie is the backstop.
            if netloc and netloc != host:
                return JSONResponse(
                    {"error": "CSRF validation failed — request origin mismatch."},
                    status_code=403,
                )
    return await call_next(request)

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
templates.env.filters["datetimefmt"] = lambda dt, fmt="%Y-%m-%d %H:%M": (
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
    # Validate SECRET_KEY before anything else
    _default_key = "change-me-to-something-random-and-long"
    if SECRET_KEY == _default_key:
        logger.warning(
            "⚠️  SECRET_KEY is the default value — this is INSECURE. "
            "Set a unique key in your environment variables."
        )
    elif len(SECRET_KEY) < 16:
        logger.warning(
            f"⚠️  SECRET_KEY is only {len(SECRET_KEY)} characters. "
            "Minimum recommended length is 32 characters."
        )
    elif len(SECRET_KEY) > 128:
        logger.warning(
            f"⚠️  SECRET_KEY is {len(SECRET_KEY)} characters. "
            "Values longer than 128 characters provide no additional security benefit."
        )
    else:
        logger.info(f"SECRET_KEY: {len(SECRET_KEY)} characters ✓")

    init_db()
    init_scheduler()

    # Re-schedule all enabled jobs that survived a restart
    from .database import SessionLocal

    db = SessionLocal()
    try:
        jobs = db.query(SyncJob).filter(SyncJob.enabled == True).all()  # noqa: E712
        for job in jobs:
            try:
                next_run = schedule_job(job)
                if next_run:
                    job.next_run_at = next_run
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
# Auth helpers
# --------------------------------------------------------------------------- #

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
    now = time.monotonic()
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


def _logged_in(request: Request) -> bool:
    return bool(request.session.get("user"))


def _require_login(request: Request):
    """Raise a redirect if the session has no user."""
    if not _logged_in(request):
        raise HTTPException(status_code=302, headers={"Location": "/login"})


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
async def login_get(request: Request):
    if _logged_in(request):
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
    if username == admin_username and admin_hash and _verify_password(password, admin_hash):
        _record_login_success(ip)
        request.session["user"] = username
        # Check if first-login password change is still required
        pw_row = db.query(Settings).filter(Settings.key == "password_changed").first()
        if pw_row and pw_row.value == "false":
            request.session["must_change_password"] = True
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
async def change_password_get(request: Request):
    """Standalone page displayed when the default password has never been changed."""
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=302)
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
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=302)

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
    request.session.pop("must_change_password", None)
    logger.info("Admin password changed from default on first login")
    return RedirectResponse("/", status_code=302)


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=302)
    if request.session.get("must_change_password"):
        return RedirectResponse("/change-password", status_code=302)

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
# Jobs — list
# --------------------------------------------------------------------------- #

@app.get("/jobs", response_class=HTMLResponse)
async def jobs_list(request: Request, db: Session = Depends(get_db)):
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=302)
    if request.session.get("must_change_password"):
        return RedirectResponse("/change-password", status_code=302)

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

@app.get("/jobs/new", response_class=HTMLResponse)
async def job_new_get(request: Request):
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=302)
    if request.session.get("must_change_password"):
        return RedirectResponse("/change-password", status_code=302)
    return templates.TemplateResponse(
        "job_form.html", {
            "request": request,
            "job": None,
            "has_source_key": False,
            "has_dest_key": False,
            "error": None,
        }
    )


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
    db: Session = Depends(get_db),
):
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=302)

    job = SyncJob(
        name=name.strip(),
        source_url=source_url.strip().rstrip("/"),
        source_key=_encrypt_key(source_key.strip()),
        source_album_name=source_album_name.strip(),
        dest_url=dest_url.strip().rstrip("/"),
        dest_key=_encrypt_key(dest_key.strip()),
        dest_album_name=dest_album_name.strip(),
        schedule=schedule.strip(),
        delete_sync=delete_sync == "on",
        cleanup_cache=cleanup_cache == "on",
        enabled=enabled == "on",
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    if job.enabled:
        try:
            next_run = schedule_job(job)
            if next_run:
                job.next_run_at = next_run
                db.commit()
        except Exception as exc:
            logger.error(f"Could not schedule new job {job.id}: {exc}")

    return RedirectResponse("/jobs", status_code=302)


# --------------------------------------------------------------------------- #
# Jobs — edit
# --------------------------------------------------------------------------- #

@app.get("/jobs/{job_id}/edit", response_class=HTMLResponse)
async def job_edit_get(request: Request, job_id: int, db: Session = Depends(get_db)):
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=302)
    if request.session.get("must_change_password"):
        return RedirectResponse("/change-password", status_code=302)
    job = db.query(SyncJob).filter(SyncJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    # Never send actual key values to the template — they must not appear in HTML
    return templates.TemplateResponse(
        "job_form.html", {
            "request": request,
            "job": job,
            "has_source_key": bool(job.source_key),
            "has_dest_key": bool(job.dest_key),
            "error": None,
        }
    )


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
    db: Session = Depends(get_db),
):
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=302)

    job = db.query(SyncJob).filter(SyncJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job.name = name.strip()
    job.source_url = source_url.strip().rstrip("/")
    # Only replace the key if a new value was provided — blank means "keep existing"
    if source_key.strip():
        job.source_key = _encrypt_key(source_key.strip())
    job.source_album_name = source_album_name.strip()
    job.dest_url = dest_url.strip().rstrip("/")
    if dest_key.strip():
        job.dest_key = _encrypt_key(dest_key.strip())
    job.dest_album_name = dest_album_name.strip()
    job.schedule = schedule.strip()
    job.delete_sync = delete_sync == "on"
    job.cleanup_cache = cleanup_cache == "on"
    job.enabled = enabled == "on"
    job.updated_at = datetime.utcnow()

    db.commit()

    remove_job(job_id)
    if job.enabled:
        try:
            next_run = schedule_job(job)
            if next_run:
                job.next_run_at = next_run
                db.commit()
        except Exception as exc:
            logger.error(f"Could not reschedule job {job.id}: {exc}")

    return RedirectResponse("/jobs", status_code=302)


# --------------------------------------------------------------------------- #
# Jobs — delete
# --------------------------------------------------------------------------- #

@app.post("/jobs/{job_id}/delete")
async def job_delete(request: Request, job_id: int, db: Session = Depends(get_db)):
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=302)
    job = db.query(SyncJob).filter(SyncJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404)
    remove_job(job_id)
    db.delete(job)
    db.commit()
    return RedirectResponse("/jobs", status_code=302)


# --------------------------------------------------------------------------- #
# Jobs — toggle enabled (AJAX)
# --------------------------------------------------------------------------- #

@app.post("/jobs/{job_id}/toggle")
async def job_toggle(request: Request, job_id: int, db: Session = Depends(get_db)):
    if not _logged_in(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    job = db.query(SyncJob).filter(SyncJob.id == job_id).first()
    if not job:
        return JSONResponse({"error": "Not found"}, status_code=404)

    job.enabled = not job.enabled
    db.commit()

    if job.enabled:
        try:
            next_run = schedule_job(job)
            if next_run:
                job.next_run_at = next_run
                db.commit()
        except Exception:
            pass
    else:
        remove_job(job_id)

    return JSONResponse({"enabled": job.enabled})


# --------------------------------------------------------------------------- #
# Jobs — run now (AJAX)
# --------------------------------------------------------------------------- #

@app.post("/jobs/{job_id}/run")
async def job_run_now(request: Request, job_id: int, db: Session = Depends(get_db)):
    if not _logged_in(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    job = db.query(SyncJob).filter(SyncJob.id == job_id).first()
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)

    # Create run record immediately so the UI can follow it
    run = SyncRun(job_id=job_id, started_at=datetime.utcnow(), status="running")
    db.add(run)
    db.commit()
    db.refresh(run)
    run_id = run.id

    asyncio.create_task(_run_background(job_id, run_id))

    return JSONResponse({"status": "started", "run_id": run_id})


async def _run_background(job_id: int, run_id: int):
    from .database import SessionLocal

    db = SessionLocal()
    try:
        job = db.query(SyncJob).filter(SyncJob.id == job_id).first()
        if not job:
            return

        results = await run_sync_job(job)

        run = db.query(SyncRun).filter(SyncRun.id == run_id).first()
        if run:
            run.finished_at = datetime.utcnow()
            run.status = results["status"]
            run.assets_found = results["assets_found"]
            run.assets_downloaded = results["assets_downloaded"]
            run.assets_uploaded = results["assets_uploaded"]
            run.assets_skipped = results["assets_skipped"]
            run.assets_failed = results["assets_failed"]
            run.error_message = results.get("error_message")

        job.last_run_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        logger.error(f"Background run error (job={job_id}): {exc}")
        try:
            run = db.query(SyncRun).filter(SyncRun.id == run_id).first()
            if run:
                run.status = "failed"
                run.finished_at = datetime.utcnow()
                run.error_message = str(exc)
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Logs — page + SSE stream
# --------------------------------------------------------------------------- #

@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=302)
    if request.session.get("must_change_password"):
        return RedirectResponse("/change-password", status_code=302)
    return templates.TemplateResponse("logs.html", {"request": request})


@app.get("/logs/stream")
async def logs_stream(request: Request):
    """Server-Sent Events stream of the sync log file."""
    if not _logged_in(request):
        raise HTTPException(status_code=401)

    async def generator():
        log_file = Path(LOG_PATH)

        # Send last 200 lines on connect
        if log_file.exists():
            try:
                with open(log_file, "r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.readlines()
                for line in lines[-200:]:
                    payload = json.dumps({"line": line.rstrip()})
                    yield f"data: {payload}\n\n"
            except Exception as exc:
                yield f"data: {json.dumps({'line': f'[stream] Could not read log: {exc}'})}\n\n"

        # Follow new lines
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(0, 2)  # jump to EOF
                while True:
                    if await request.is_disconnected():
                        break
                    line = fh.readline()
                    if line:
                        yield f"data: {json.dumps({'line': line.rstrip()})}\n\n"
                    else:
                        await asyncio.sleep(0.4)
        except Exception as exc:
            yield f"data: {json.dumps({'line': f'[stream error] {exc}'})}\n\n"

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------- #
# Logs — support bundle download
# --------------------------------------------------------------------------- #

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_URL_HOST_RE = re.compile(r"(https?://)([^/\s\"']+)")


def _mask_url_host(url: Optional[str]) -> Optional[str]:
    """Mask the host[:port] of a URL while keeping the scheme (http/https)."""
    if not url:
        return url
    return _URL_HOST_RE.sub(lambda m: m.group(1) + "[redacted-host]", url)


def _redact_text(text: str) -> str:
    """Mask URL hosts and bare IPv4 addresses so a bundle can be shared safely."""
    text = _URL_HOST_RE.sub(lambda m: m.group(1) + "[redacted-host]", text)
    text = _IPV4_RE.sub("[redacted-ip]", text)
    return text


@app.get("/logs/support-bundle")
async def download_support_bundle(request: Request, db: Session = Depends(get_db)):
    """Build and return a ZIP containing logs, sanitized job configs, run history, and system info."""
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=302)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:

        # 1. Sync log file
        log_file = Path(LOG_PATH)
        if log_file.exists():
            try:
                raw = log_file.read_text(encoding="utf-8", errors="replace")
                # Redact server hosts/IPs so the bundle is safe to share
                zf.writestr("sync.log", _redact_text(raw))
            except Exception as exc:
                zf.writestr("sync.log", f"(error reading log: {exc})\n")
        else:
            zf.writestr("sync.log", "(no log file found)\n")

        # 2. Job configurations — API keys redacted
        jobs = db.query(SyncJob).order_by(SyncJob.created_at).all()
        jobs_info = []
        for job in jobs:
            jobs_info.append({
                "id": job.id,
                "name": job.name,
                "source_url": _mask_url_host(job.source_url),
                "source_key": "[redacted]",
                "source_album_name": job.source_album_name,
                "dest_url": _mask_url_host(job.dest_url),
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
                "error_message": run.error_message,
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
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=302)
    if request.session.get("must_change_password"):
        return RedirectResponse("/change-password", status_code=302)
    settings = {s.key: s.value for s in db.query(Settings).all()}
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "settings": settings,
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
        },
    )


@app.post("/settings/username")
async def settings_username(
    request: Request,
    new_username: str = Form(...),
    db: Session = Depends(get_db),
):
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=302)
    _upsert_setting(db, "admin_username", new_username.strip())
    request.session["user"] = new_username.strip()
    return RedirectResponse("/settings?success=Username+updated", status_code=302)


@app.post("/settings/password")
async def settings_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=302)

    _, admin_hash = _get_admin_creds(db)
    if not admin_hash or not _verify_password(current_password, admin_hash):
        return RedirectResponse("/settings?error=Current+password+incorrect", status_code=302)

    if new_password != confirm_password:
        return RedirectResponse("/settings?error=New+passwords+do+not+match", status_code=302)

    if len(new_password) < 8:
        return RedirectResponse("/settings?error=Password+must+be+at+least+8+characters", status_code=302)

    _upsert_setting(db, "admin_password_hash", _hash_password(new_password))
    _upsert_setting(db, "password_changed", "true")
    request.session.pop("must_change_password", None)
    return RedirectResponse("/settings?success=Password+updated+successfully", status_code=302)


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
    if not _logged_in(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

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
async def api_job_status(request: Request, job_id: int, db: Session = Depends(get_db)):
    if not _logged_in(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

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
