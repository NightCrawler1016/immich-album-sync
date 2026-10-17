"""
End-to-end smoke test. Boots the real app in-process (Starlette TestClient)
against a throwaway database and walks through the auth gate, CSRF, the forced
password change, every page, job create/edit/toggle, the support bundle, and
configuration backup/restore. Never touches the network.

    python tests/smoke_test.py

Needs the packages in requirements.txt (uvicorn itself is not used).
"""
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import unquote_plus

TMP = Path(tempfile.mkdtemp(prefix="immich-album-sync-smoke-"))
SECRET = "smoke-test-secret-key-0123456789abcdef"
os.environ.update({
    "SECRET_KEY": SECRET,
    "DB_PATH": str(TMP / "config.db"),
    "LOG_PATH": str(TMP / "logs" / "sync.log"),
    "CACHE_PATH": str(TMP / "cache"),
})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.crypto import decrypt_secret  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import Settings, SyncJob  # noqa: E402
from app.scheduler import get_next_run_time  # noqa: E402
from app.sync import release_job, try_acquire_job  # noqa: E402

ORIGIN = {"Origin": "http://testserver"}
PASSWORD = "correct-horse-battery"
PASSWORD_2 = "staple-battery-horse"
BACKUP_PASS = "backup-passphrase-1"
SOURCE_KEY = "source-api-key-SMOKE-0001"
DEST_KEY = "dest-api-key-SMOKE-0002"
WEBHOOK = "https://hooks.example.invalid/smoke-token"
JOB_FORM = {
    "name": "Smoke job",
    "source_url": "http://immich-a.invalid:2283/",
    "source_key": SOURCE_KEY,
    "source_album_name": "Family",
    "dest_url": "http://immich-b.invalid:2283",
    "dest_key": DEST_KEY,
    "dest_album_name": "Family",
    "schedule": "0 3 * * *",
    "enabled": "on",
    "notify_override": "inherit",
}


def check(condition, what):
    if not condition:
        raise AssertionError(f"FAILED: {what}")
    print(f"  ok  {what}")


def location(resp) -> str:
    return unquote_plus(resp.headers.get("location", ""))


def jobs_in_db():
    db = SessionLocal()
    try:
        return db.query(SyncJob).order_by(SyncJob.id).all()
    finally:
        db.close()


def setting(key):
    db = SessionLocal()
    try:
        row = db.get(Settings, key)
        return row.value if row else None
    finally:
        db.close()


def secret_key_guard():
    print("startup guard")
    main.SECRET_KEY = "change-me-to-a-unique-random-32-64-char-string"
    try:
        with TestClient(main.app):
            pass
    except Exception as exc:  # the lifespan error surfaces as-is
        check("SECRET_KEY" in str(exc), "startup refuses a placeholder SECRET_KEY")
    else:
        raise AssertionError("FAILED: startup accepted a placeholder SECRET_KEY")
    finally:
        main.SECRET_KEY = SECRET


def walkthrough(c: TestClient):
    print("auth gate")
    check(c.get("/health").status_code == 200, "/health is public")
    r = c.get("/")
    check(location(r) == "/login", "anonymous page request → /login")
    check(r.headers.get("x-frame-options") == "DENY", "anti-framing header set")
    check(c.get("/api/jobs/1/status").status_code == 401, "anonymous API request → 401")
    r = c.get("/jobs", headers={"host": "x/static?"})
    check(location(r) == "/login", "Host-header path confusion doesn't bypass the gate")

    print("CSRF + body cap")
    creds = {"username": "admin", "password": "admin"}
    check(c.post("/login", data=creds).status_code == 403, "POST without Origin rejected")
    r = c.post("/login", data=creds, headers={"Origin": "http://evil.example"})
    check(r.status_code == 403, "cross-origin POST rejected")
    r = c.post("/login", content=b"username=" + b"a" * 70_000,
               headers={**ORIGIN, "Content-Type": "application/x-www-form-urlencoded"})
    check(r.status_code in (400, 413), "oversized body rejected")

    print("login + forced password change")
    r = c.post("/login", data={**creds, "password": "wrong"}, headers=ORIGIN)
    check("Invalid username or password" in r.text, "wrong password rejected")
    r = c.post("/login", data=creds, headers=ORIGIN)
    check(location(r) == "/change-password", "default login → forced password change")
    for path in ("/", "/jobs", "/jobs/new", "/history", "/logs", "/settings"):
        check(location(c.get(path)) == "/change-password", f"GET {path} gated until the change")
    r = c.post("/jobs/new", data=JOB_FORM, headers=ORIGIN)
    check(location(r) == "/change-password" and not jobs_in_db(), "POST /jobs/new gated")
    check(c.post("/api/test-webhook", json={}, headers=ORIGIN).status_code == 403, "API gated (403)")
    check(c.get("/change-password").status_code == 200, "change-password page renders")
    r = c.post("/change-password", data={"new_password": "  Admin  ", "confirm_password": "  Admin  "},
               headers=ORIGIN)
    check("cannot reuse" in r.text, "default password can't be reused")
    r = c.post("/change-password", data={"new_password": PASSWORD, "confirm_password": PASSWORD},
               headers=ORIGIN)
    check(location(r) == "/" and setting("password_changed") == "true", "password changed")

    print("pages")
    for path in ("/", "/jobs", "/jobs/new", "/history", "/history?days=0&status=failed",
                 "/logs", "/settings"):
        check(c.get(path).status_code == 200, f"GET {path}")
    check(location(c.get("/change-password")) == "/settings", "forced-change form closed afterwards")

    print("jobs")
    r = c.post("/jobs/new", data={**JOB_FORM, "schedule": "not a cron"}, headers=ORIGIN)
    check(r.status_code == 400 and "Invalid schedule" in r.text and not jobs_in_db(),
          "invalid schedule rejected, nothing saved")
    r = c.post("/jobs/new", data=JOB_FORM, headers=ORIGIN)
    jobs = jobs_in_db()
    check(location(r) == "/jobs" and len(jobs) == 1, "job created")
    job = jobs[0]
    check(job.source_key.startswith("gAAAAA") and SOURCE_KEY not in job.source_key,
          "API key encrypted at rest")
    r = c.get(f"/jobs/{job.id}/edit")
    check(r.status_code == 200 and SOURCE_KEY not in r.text and DEST_KEY not in r.text,
          "edit page never renders the keys")
    check(c.post(f"/jobs/{job.id}/toggle", headers=ORIGIN).json() == {"enabled": False},
          "toggle pauses the job")
    check(c.get(f"/api/jobs/{job.id}/status").json()["enabled"] is False, "status API")
    r = c.post("/settings/webhook",
               data={"webhook_enabled": "on", "webhook_url": WEBHOOK, "webhook_events": ["failed"]},
               headers=ORIGIN)
    check("success=" in location(r), "global webhook saved")

    print("support bundle")
    r = c.get("/logs/support-bundle")
    bundle = zipfile.ZipFile(io.BytesIO(r.content))
    blob = b"".join(bundle.read(name) for name in bundle.namelist())
    check(r.status_code == 200 and SOURCE_KEY.encode() not in blob
          and b"immich-a.invalid" not in blob, "bundle has no API keys or server hosts")

    print("backup")
    r = c.post("/settings/backup", data={"passphrase": "short", "confirm_passphrase": "short"},
               headers=ORIGIN)
    check("at least" in location(r), "short passphrase rejected")
    r = c.post("/settings/backup", data={"passphrase": BACKUP_PASS, "confirm_passphrase": "x"},
               headers=ORIGIN)
    check("do not match" in location(r), "mismatched passphrases rejected")
    check(c.get(r.headers["location"]).text.count("do not match") == 1, "flash message shown once")
    r = c.post("/settings/backup", data={"passphrase": BACKUP_PASS, "confirm_passphrase": BACKUP_PASS},
               headers=ORIGIN)
    protected = r.content
    doc = json.loads(protected)
    check(r.status_code == 200 and doc["format"] == "immich-album-sync-backup"
          and doc["format_version"] == 1 and len(doc["jobs"]) == 1, "backup downloads")
    check(all(s.encode() not in protected for s in (SOURCE_KEY, DEST_KEY, WEBHOOK)),
          "backup holds no plaintext secrets")
    check(bool(doc["jobs"][0]["source"]["api_key"]) and bool(doc["notifications"]["webhook_url"]),
          "backup carries the encrypted secrets")

    print("restore")

    def restore(content: bytes, passphrase: str = ""):
        return c.post("/settings/restore", headers=ORIGIN, data={"passphrase": passphrase},
                      files={"backup_file": ("backup.json", content, "application/json")})

    check("Wrong passphrase" in location(restore(protected, "wrong-passphrase")),
          "wrong passphrase rejected")
    check("protected" in location(restore(protected)), "missing passphrase rejected")
    check("isn't an Immich Album Sync backup" in location(restore(b"not json")),
          "non-backup file rejected")
    newer = json.dumps({**doc, "format_version": 99}).encode()
    check("newer version" in location(restore(newer, BACKUP_PASS)), "newer backup format rejected")
    huge = b"{" + b" " * (3 * 1024 * 1024) + b"}"
    check("too large" in location(restore(huge)), "file over 2 MiB rejected cleanly")
    check("not valid JSON" in location(restore(b"[" * 200_000)), "deeply nested JSON rejected cleanly")
    bad_text = json.dumps({**doc, "jobs": [{**doc["jobs"][0], "name": "\ud800"}]}).encode()
    check("not valid text" in location(restore(bad_text, BACKUP_PASS)), "unencodable text rejected cleanly")
    check(len(jobs_in_db()) == 1, "rejected restores changed nothing")

    c.post("/jobs/new", data={**JOB_FORM, "name": "Added after the backup"}, headers=ORIGIN)
    old_ids = [j.id for j in jobs_in_db()]
    check(len(old_ids) == 2, "a second job added after the backup")
    try_acquire_job(old_ids[0])
    try:
        r = restore(protected, BACKUP_PASS)
    finally:
        release_job(old_ids[0])
    check("sync is running" in location(r) and len(jobs_in_db()) == 2,
          "restore refused while a sync runs")
    old_cache = TMP / "cache" / f"job_{old_ids[1]}" / "files"
    old_cache.mkdir(parents=True)
    (old_cache / "private.jpg").write_bytes(b"x")
    # Fields a future release might add must be ignored, not fatal.
    future = {**doc, "added_later": {"x": 1},
              "jobs": [{**doc["jobs"][0], "new_option": True}]}
    r = restore(json.dumps(future).encode(), BACKUP_PASS)
    jobs = jobs_in_db()
    check("Restored 1 job" in location(r) and [j.name for j in jobs] == ["Smoke job"],
          "restore replaces all jobs (unknown fields ignored)")
    check(not old_cache.parent.exists(), "replaced jobs' cached originals deleted")
    check(decrypt_secret(jobs[0].source_key, SECRET) == SOURCE_KEY
          and decrypt_secret(jobs[0].dest_key, SECRET) == DEST_KEY,
          "restored keys re-encrypted with this install's SECRET_KEY")
    check(decrypt_secret(setting("webhook_url"), SECRET) == WEBHOOK
          and setting("webhook_events") == "failed", "webhook settings restored")
    check(jobs[0].enabled is False, "restored job keeps its paused state")

    c.post(f"/jobs/{jobs[0].id}/toggle", headers=ORIGIN)  # enable it for the next two restores
    r = c.post("/settings/backup", data={"passphrase": BACKUP_PASS, "confirm_passphrase": BACKUP_PASS},
               headers=ORIGIN)
    enabled_doc = json.loads(r.content)
    c.post("/jobs/new", data={**JOB_FORM, "name": "Replaced by the restore"}, headers=ORIGIN)
    old_ids = [j.id for j in jobs_in_db()]
    check(all(get_next_run_time(i) for i in old_ids), "jobs to be replaced are scheduled")
    padded = json.dumps({**enabled_doc, "padding": "x" * 100_000}).encode()
    r = restore(padded, BACKUP_PASS)
    jobs = jobs_in_db()
    check("Restored 1 job" in location(r), "a restore bigger than the 64 KiB form cap is accepted")
    check(jobs[0].enabled is True and get_next_run_time(jobs[0].id) is not None,
          "restored enabled job is scheduled")
    check(all(get_next_run_time(i) is None for i in old_ids), "replaced jobs are unscheduled")

    r = c.post("/settings/backup", data={"passphrase": "", "confirm_passphrase": ""}, headers=ORIGIN)
    bare = json.loads(r.content)
    check(bare["encryption"] is None and bare["jobs"][0]["source"]["api_key"] is None
          and bare["jobs"][0]["enabled"] is True, "backup without a passphrase leaves secrets out")
    r = restore(r.content)
    jobs = jobs_in_db()
    check("paused" in location(r) and jobs[0].enabled is False and jobs[0].source_key == "",
          "keyless restore leaves the job paused")

    print("sessions")
    stale = dict(c.cookies)
    r = c.post("/settings/password", headers=ORIGIN, data={
        "current_password": PASSWORD, "new_password": PASSWORD_2, "confirm_password": PASSWORD_2})
    check("success=" in location(r), "password changed in Settings")
    check(c.get("/").status_code == 200, "this session stays signed in")
    other = TestClient(main.app, follow_redirects=False, cookies=stale)
    check(location(other.get("/")) == "/login", "every other session is signed out")
    c.get("/logout")
    check(location(c.get("/")) == "/login", "logout ends the session")


if __name__ == "__main__":
    try:
        secret_key_guard()
        with TestClient(main.app, follow_redirects=False) as client:
            walkthrough(client)
        print("\nALL SMOKE TESTS PASSED")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
