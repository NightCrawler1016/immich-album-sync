from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from .database import Base


class SyncJob(Base):
    __tablename__ = "sync_jobs"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)

    # Source — Immich A (private)
    source_url = Column(String(500), nullable=False)
    source_key = Column(String(500), nullable=False)
    source_album_name = Column(String(255), nullable=False)

    # Destination — Immich B (public)
    dest_url = Column(String(500), nullable=False)
    dest_key = Column(String(500), nullable=False)
    dest_album_name = Column(String(255), nullable=False)

    # Schedule (cron expression)
    schedule = Column(String(100), default="0 */6 * * *")

    # Behavior flags
    delete_sync = Column(Boolean, default=False)   # Mirror deletes from A to B
    cleanup_cache = Column(Boolean, default=False) # Delete cache after upload
    enabled = Column(Boolean, default=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_run_at = Column(DateTime, nullable=True)
    next_run_at = Column(DateTime, nullable=True)

    # Relationships
    runs = relationship("SyncRun", back_populates="job", cascade="all, delete-orphan")


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("sync_jobs.id"), nullable=False)

    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String(50), default="running")  # running | success | partial | failed

    assets_found = Column(Integer, default=0)
    assets_downloaded = Column(Integer, default=0)
    assets_uploaded = Column(Integer, default=0)
    assets_skipped = Column(Integer, default=0)
    assets_failed = Column(Integer, default=0)

    error_message = Column(Text, nullable=True)

    job = relationship("SyncJob", back_populates="runs")

    @property
    def duration_seconds(self):
        if self.finished_at and self.started_at:
            return int((self.finished_at - self.started_at).total_seconds())
        return None


class Settings(Base):
    __tablename__ = "settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=True)
