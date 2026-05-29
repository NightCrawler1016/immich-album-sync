import os
import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

logger = logging.getLogger(__name__)

# Database path — use the appdata volume mount
# NOTE: do NOT call os.makedirs here at module level.
# Directory creation happens inside init_db() so any failure
# is caught by FastAPI's startup error handling and logged properly.
DB_PATH = os.getenv("DB_PATH", "/app/appdata/config.db")

DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency: yields a DB session, closes on exit."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all tables and seed default admin credentials."""
    # Ensure the appdata directory exists before SQLite tries to create the file.
    # Done here (not at module level) so failures are visible in the startup log.
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
        logger.info(f"Appdata directory ready: {db_dir}")

    from . import models  # noqa: F401 — ensures models are registered

    Base.metadata.create_all(bind=engine)
    logger.info(f"Database initialized at {DB_PATH}")

    db = SessionLocal()
    try:
        from .models import Settings
        import bcrypt

        # Seed default admin password (user should change this on first login)
        if not db.query(Settings).filter(Settings.key == "admin_password_hash").first():
            hashed = bcrypt.hashpw(b"admin", bcrypt.gensalt()).decode("utf-8")
            db.add(Settings(key="admin_password_hash", value=hashed))
            # Flag forces the password-change prompt on first login
            db.add(Settings(key="password_changed", value="false"))
            logger.info("Default admin password set to 'admin' — please change it immediately")

        if not db.query(Settings).filter(Settings.key == "admin_username").first():
            db.add(Settings(key="admin_username", value="admin"))

        db.commit()
    finally:
        db.close()
