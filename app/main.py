import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from passlib.context import CryptContext
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

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

app = FastAPI(title="Immich Album Sync", docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=86400)  # 24 h

# Templates directory is relative to the Python package root
_template_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_template_dir))

# Jinja2 global helpers
templates.env.globals["now"] = datetime.utcnow
templates.env.filters["datetimefmt"] = lambda dt, fmt="%Y-%m-%d %H:%M": (
    dt.strftime(fmt) if dt else "—"
)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# --------------------------------------------------------------------------- #
# Startup / shutdown
# --------------------------------------------------------------------------- #

@app.on_event("startup")
async def startup():
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
    admin_username, admin_hash = _get_admin_creds(db)
    if username == admin_username and admin_hash and pwd_context.verify(password, admin_hash):
        request.session["user"] = username
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "Invalid username or password"}
    )


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=302)

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
    return templates.TemplateResponse(
        "job_form.html", {"request": request, "job": None, "error": None}
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
        source_key=source_key.strip(),
        source_album_name=source_album_name.strip(),
        dest_url=dest_url.strip().rstrip("/"),
        dest_key=dest_key.strip(),
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
    job = db.query(SyncJob).filter(SyncJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return templates.TemplateResponse(
        "job_form.html", {"request": request, "job": job, "error": None}
    )


@app.post("/jobs/{job_id}/edit")
async def job_edit_post(
    request: Request,
    job_id: int,
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

    job = db.query(SyncJob).filter(SyncJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job.name = name.strip()
    job.source_url = source_url.strip().rstrip("/")
    job.source_key = source_key.strip()
    job.source_album_name = source_album_name.strip()
    job.dest_url = dest_url.strip().rstrip("/")
    job.dest_key = dest_key.strip()
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
# Settings
# --------------------------------------------------------------------------- #

@app.get("/settings", response_class=HTMLResponse)
async def settings_get(request: Request, db: Session = Depends(get_db)):
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=302)
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
    if not admin_hash or not pwd_context.verify(current_password, admin_hash):
        return RedirectResponse("/settings?error=Current+password+incorrect", status_code=302)

    if new_password != confirm_password:
        return RedirectResponse("/settings?error=New+passwords+do+not+match", status_code=302)

    if len(new_password) < 8:
        return RedirectResponse("/settings?error=Password+must+be+at+least+8+characters", status_code=302)

    _upsert_setting(db, "admin_password_hash", pwd_context.hash(new_password))
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
async def api_test_connection(request: Request):
    if not _logged_in(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    url = (body.get("url") or "").strip().rstrip("/")
    api_key = (body.get("api_key") or "").strip()

    if not url or not api_key:
        return JSONResponse({"error": "url and api_key are required"}, status_code=400)

    try:
        client = ImmichClient(url, api_key)
        info = await client.test_connection()
        albums = await client.get_albums()
        return JSONResponse(
            {
                "success": True,
                "server_info": info,
                "albums": [
                    {
                        "id": a["id"],
                        "name": a.get("albumName", ""),
                        "asset_count": a.get("assetCount", 0),
                    }
                    for a in albums
                ],
            }
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


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
