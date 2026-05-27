import os
import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

logger = logging.getLogger(__name__)

# Database path — use the appdata volume mount
DB_PATH = os.getenv("DB_PATH", "/app/appdata/config.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

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
    from . import models  # noqa: F401 — ensures models are registered

    Base.metadata.create_all(bind=engine)
    logger.info("Database initialized")

    db = SessionLocal()
    try:
        from .models import Settings
        from passlib.context import CryptContext

        pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

        # Seed default admin password (user should change this on first login)
        if not db.query(Settings).filter(Settings.key == "admin_password_hash").first():
            db.add(Settings(key="admin_password_hash", value=pwd_context.hash("admin")))
            logger.info("Default admin password set to 'admin' — please change it immediately")

        if not db.query(Settings).filter(Settings.key == "admin_username").first():
            db.add(Settings(key="admin_username", value="admin"))

        db.commit()
    finally:
        db.close()
