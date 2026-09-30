import datetime as dt
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ...db import Base
from ...models import BigId, created, pk, text
from ..models import user_fk


class EEConnection(Base):
    """One per user: EasyEquities login (platform sync) and Gmail app password (email reader). Secrets are sealed."""

    __tablename__ = "ee_connections"
    id: Mapped[int] = pk()
    user_id: Mapped[int] = mapped_column(BigId, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)

    username: Mapped[str] = text(200)
    password: Mapped[str] = text()
    platform_status: Mapped[str] = text(20)  # "" | ok | error
    platform_error: Mapped[str] = text()
    platform_error_stage: Mapped[str] = text(40)
    platform_debug: Mapped[str] = text()  # start of the page that failed to parse
    platform_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # last success
    platform_tried_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)  # last good platform read

    mail_address: Mapped[str] = text(254)
    mail_password: Mapped[str] = text()
    mail_status: Mapped[str] = text(20)
    mail_error: Mapped[str] = text()
    mail_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    mail_last_uid: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    mail_uidvalidity: Mapped[str] = text(40)
    created_at: Mapped[datetime] = created()


class EEMail(Base):
    __tablename__ = "ee_mails"
    __table_args__ = (UniqueConstraint("user_id", "message_id"),)
    id: Mapped[int] = pk()
    user_id: Mapped[int] = user_fk()
    message_id: Mapped[str] = text(400)
    received_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    sender: Mapped[str] = text(200)
    subject: Mapped[str] = text(400)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)  # normalised text, for re-parsing

    kind: Mapped[str] = text(20)  # trade | order | deposit | withdrawal | bundle | corporate_action | notice
    parsed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    date: Mapped[Optional[dt.date]] = mapped_column(nullable=True)
    account: Mapped[str] = text(80)
    account_number: Mapped[str] = text(40)
    instrument: Mapped[str] = text(200)
    contract_code: Mapped[str] = text(40)
    side: Mapped[str] = text(10)
    quantity: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = text(3)
    value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    costs: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reference: Mapped[str] = text(40)
    done: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # corporate action dealt with
    txn_id: Mapped[Optional[int]] = mapped_column(BigId, ForeignKey("invest_transactions.id", ondelete="SET NULL"),
                                                  nullable=True)
    created_at: Mapped[datetime] = created()
