"""ORM models. P0 holds only infrastructure tables; domain tables arrive with their phases."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from rd_db.base import Base


class ServiceHeartbeat(Base):
    """Last-seen time per running process; feeds readiness checks and the status page."""

    __tablename__ = "service_heartbeats"

    service: Mapped[str] = mapped_column(String(64), primary_key=True)
    instance_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), server_default="member")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
