from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Agent(TimestampMixin, Base):
    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(64))
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="ru")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    listings: Mapped[list[Listing]] = relationship(
        back_populates="agent", cascade="all, delete-orphan", passive_deletes=True
    )
    client_links: Mapped[list[ClientAgentLink]] = relationship(
        back_populates="agent", cascade="all, delete-orphan", passive_deletes=True
    )
    leads: Mapped[list[Lead]] = relationship(
        back_populates="agent", cascade="all, delete-orphan", passive_deletes=True
    )
    dialogs: Mapped[list[Dialog]] = relationship(back_populates="agent")


class Client(TimestampMixin, Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(255))
    first_name: Mapped[str | None] = mapped_column(String(255))
    language: Mapped[str | None] = mapped_column(String(8))

    links: Mapped[list[ClientAgentLink]] = relationship(
        back_populates="client", cascade="all, delete-orphan", passive_deletes=True
    )
    leads: Mapped[list[Lead]] = relationship(
        back_populates="client", cascade="all, delete-orphan", passive_deletes=True
    )
    dialogs: Mapped[list[Dialog]] = relationship(
        back_populates="client", cascade="all, delete-orphan", passive_deletes=True
    )


class ClientAgentLink(Base):
    __tablename__ = "client_agent_links"
    __table_args__ = (
        UniqueConstraint("client_id", "agent_id", name="uq_client_agent_links_client_agent"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    client_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    client: Mapped[Client] = relationship(back_populates="links")
    agent: Mapped[Agent] = relationship(back_populates="client_links")


class Listing(TimestampMixin, Base):
    __tablename__ = "listings"
    __table_args__ = (
        Index("ix_listings_agent_active", "agent_id", "is_active"),
        Index("ix_listings_district", "district"),
        Index("ix_listings_rooms", "rooms"),
        Index("ix_listings_price", "price"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    rooms: Mapped[int | None] = mapped_column(Integer)
    price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    area_m2: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    district: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(String(500))
    pets_allowed: Mapped[bool | None] = mapped_column(Boolean)
    amenities: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    agent: Mapped[Agent] = relationship(back_populates="listings")
    photos: Mapped[list[ListingPhoto]] = relationship(
        back_populates="listing",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ListingPhoto.position",
    )
    leads: Mapped[list[Lead]] = relationship(back_populates="listing", passive_deletes=True)


class ListingPhoto(Base):
    __tablename__ = "listing_photos"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("listings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    telegram_file_id: Mapped[str] = mapped_column(String(512), nullable=False)
    file_unique_id: Mapped[str | None] = mapped_column(String(255))
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    listing: Mapped[Listing] = relationship(back_populates="photos")


class ImportJob(Base):
    __tablename__ = "import_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agents.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    source: Mapped[str | None] = mapped_column(String(32))
    total_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    errors: Mapped[list[ImportError]] = relationship(
        back_populates="job", cascade="all, delete-orphan", passive_deletes=True
    )


class ImportError(Base):
    __tablename__ = "import_errors"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("import_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    row_number: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    job: Mapped[ImportJob] = relationship(back_populates="errors")


class Lead(TimestampMixin, Base):
    __tablename__ = "leads"
    __table_args__ = (Index("ix_leads_agent_status", "agent_id", "status"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    client_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    listing_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("listings.id", ondelete="SET NULL"), index=True
    )
    original_message: Mapped[str] = mapped_column(Text, nullable=False)
    translated_message: Mapped[str | None] = mapped_column(Text)
    original_language: Mapped[str] = mapped_column(String(8), nullable=False, default="ru")
    slots: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="new", index=True)

    client: Mapped[Client] = relationship(back_populates="leads")
    agent: Mapped[Agent] = relationship(back_populates="leads")
    listing: Mapped[Listing | None] = relationship(back_populates="leads")


class Dialog(TimestampMixin, Base):
    __tablename__ = "dialogs"
    __table_args__ = (Index("ix_dialogs_client_agent", "client_id", "agent_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    client_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agents.id", ondelete="SET NULL"), index=True
    )
    state: Mapped[str | None] = mapped_column(String(64))
    state_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    client: Mapped[Client] = relationship(back_populates="dialogs")
    agent: Mapped[Agent | None] = relationship(back_populates="dialogs")


class LlmCall(Base):
    __tablename__ = "llm_calls"
    __table_args__ = (
        Index("ix_llm_calls_purpose_created", "purpose", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agents.id", ondelete="SET NULL"), index=True
    )
    client_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("clients.id", ondelete="SET NULL"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    input_text: Mapped[str | None] = mapped_column(Text)
    output_text: Mapped[str | None] = mapped_column(Text)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


__all__ = [
    "Base",
    "Agent",
    "Client",
    "ClientAgentLink",
    "Listing",
    "ListingPhoto",
    "ImportJob",
    "ImportError",
    "Lead",
    "Dialog",
    "LlmCall",
]
