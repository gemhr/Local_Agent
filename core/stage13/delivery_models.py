"""Stage13 局部交付意图与隔离 sink；不复制 Runtime 副作用表。"""

from datetime import datetime
from sqlalchemy import CheckConstraint, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from core.stage13.incident_models import GuardianBase


class DeliveryRow(GuardianBase):
    __tablename__ = "stage13_deliveries"
    delivery_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    authorization: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    token: Mapped[str | None] = mapped_column(String(36))
    epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    receipt: Mapped[dict | None] = mapped_column(JSONB)
    __table_args__ = (
        CheckConstraint(
            "status IN ('DENIED','AUTHORIZED','DELIVERING','DELIVERED','FAILED','OUTCOME_UNKNOWN')",
            name="ck_s13_delivery_status",
        ),
    )


class ControlledSinkRow(GuardianBase):
    __tablename__ = "stage13_controlled_delivery_sink"
    delivery_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    receipt: Mapped[dict] = mapped_column(JSONB, nullable=False)


DELIVERY_TABLES = (DeliveryRow.__tablename__, ControlledSinkRow.__tablename__)
