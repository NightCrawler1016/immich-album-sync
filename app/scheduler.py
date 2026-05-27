import logging
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")


def init_scheduler():
    """Start the APScheduler instance and attach an event listener."""
    scheduler.add_listener(_on_job_event, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    if not scheduler.running:
        scheduler.start()
        logger.info("APScheduler started (UTC timezone)")


def _on_job_event(event):
    if event.exception:
        logger.error(f"Scheduled job {event.job_id} raised an exception: {event.exception}")
    else:
        logger.info(f"Scheduled job {event.job_id} completed successfully")


# --------------------------------------------------------------------------- #
# Per-job management helpers
# --------------------------------------------------------------------------- #

def _aps_id(job_id: int) -> str:
    return f"sync_job_{job_id}"


def schedule_job(job) -> Optional[datetime]:
    """
    Register (or re-register) *job* with APScheduler.
    Returns the next scheduled run time, or None if job is disabled.
    """
    aps_id = _aps_id(job.id)

    # Always remove stale entry first
    if scheduler.get_job(aps_id):
        scheduler.remove_job(aps_id)

    if not job.enabled:
        return None

    try:
        parts = job.schedule.strip().split()
        if len(parts) != 5:
            raise ValueError(f"Expected 5-part cron expression, got: '{job.schedule}'")

        trigger = CronTrigger(
            minute=parts[0],
            hour=parts[1],
            day=parts[2],
            month=parts[3],
            day_of_week=parts[4],
            timezone="UTC",
        )

        scheduler.add_job(
            _scheduled_sync_runner,
            trigger=trigger,
            args=[job.id],
            id=aps_id,
            name=f"Sync: {job.name}",
            replace_existing=True,
            misfire_grace_time=3600,  # allow up to 1 h late start
            coalesce=True,
        )

        aps_job = scheduler.get_job(aps_id)
        if aps_job and aps_job.next_run_time:
            return aps_job.next_run_time

    except Exception as exc:
        logger.error(f"Failed to schedule job {job.id} ('{job.name}'): {exc}")
        raise

    return None


def remove_job(job_id: int):
    """Remove a job from the scheduler (no-op if not present)."""
    aps_id = _aps_id(job_id)
    if scheduler.get_job(aps_id):
        scheduler.remove_job(aps_id)
        logger.info(f"Removed scheduled job {aps_id}")


def get_next_run_time(job_id: int) -> Optional[datetime]:
    """Return the next scheduled fire time for a job, or None."""
    aps_job = scheduler.get_job(_aps_id(job_id))
    if aps_job and aps_job.next_run_time:
        return aps_job.next_run_time
    return None


# --------------------------------------------------------------------------- #
# Async runner called by APScheduler
# --------------------------------------------------------------------------- #

async def _scheduled_sync_runner(job_id: int):
    """APScheduler calls this coroutine at each scheduled time."""
    from .database import SessionLocal
    from .models import SyncJob, SyncRun
    from .sync import run_sync_job

    db = SessionLocal()
    run = None
    try:
        job = db.query(SyncJob).filter(
            SyncJob.id == job_id, SyncJob.enabled == True  # noqa: E712
        ).first()

        if not job:
            logger.warning(f"Scheduled job {job_id} not found or disabled — skipping")
            return

        logger.info(f"Scheduled run starting for job {job_id} '{job.name}'")

        # Create a SyncRun record
        run = SyncRun(job_id=job_id, started_at=datetime.utcnow(), status="running")
        db.add(run)
        db.commit()
        db.refresh(run)

        results = await run_sync_job(job)

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

        logger.info(f"Scheduled job {job_id} finished — status={results['status']}")

    except Exception as exc:
        logger.error(f"Unhandled error in scheduled job {job_id}: {exc}")
        if run:
            run.status = "failed"
            run.finished_at = datetime.utcnow()
            run.error_message = str(exc)
            try:
                db.commit()
            except Exception:
                pass
    finally:
        db.close()
