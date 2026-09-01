"""ORM models for the calling-agent service's own database.

Design decisions worth stating explicitly (fuller reasoning in
docs/ARCHITECTURE_DECISIONS.md):

1. `organizations` is a THIN, READ-MOSTLY LOCAL MIRROR of LeadBoost's
   organization concept (id + the two fields this service needs at low
   latency for admission control: plan_max_concurrent_calls,
   compliance_profile). LeadBoost remains the source of truth; this table
   is kept in sync via the integration layer (Phase 5, not built yet). This
   resolves an apparent tension between the two source documents: the
   roadmap's E.2 says `Call.organization_id`/`lead_id` are "FK by reference,
   not FK constraint (cross-service)", while the TRD's Part 4.6 SQL writes
   `agent_configs.organization_id REFERENCES organizations(id)`. A thin
   local mirror lets both be true: cross-service business entities (leads)
   stay soft-referenced, while `organization_id` gets a real, DB-enforced FK
   + RLS policy on the tables added in this phase, because organization_id
   is the one thing every query in this service is scoped by. `calls` /
   `call_transcript_turns` / `call_attempts` keep organization_id as a soft
   (indexed, non-FK) reference for consistency with the roadmap's explicit
   instruction on those specific tables.

2. `calls` and `call_transcript_turns` are RANGE-partitioned by month (TRD
   Part 4.3). Postgres requires a partitioned table's PRIMARY KEY/UNIQUE
   constraints to include the partition key, so `calls`' primary key is
   `(id, created_at)`, not just `id`. `call_transcript_turns` — the single
   highest-volume, tightest-coupled child of `calls` — carries the parent's
   `created_at` alongside `call_id` (`call_created_at`) so it can hold a
   REAL composite foreign key into `calls`, not just a soft reference.
   `call_attempts.call_id` is intentionally a SOFT reference (plain indexed
   UUID, no FK) — a scheduled attempt can exist before the call it becomes
   even has a row, so a hard FK doesn't fit that lifecycle.

3. Every tenant-scoped table carries `organization_id` directly (never only
   inferred through a join), per the master prompt's non-negotiable rule #2
   — this is what the RLS policies (alembic migration) key off of.

4. The partial index on `call_attempts(status) WHERE status = 'pending'`
   (TRD Part 4.2) and the HNSW vector index on `knowledge_chunks.embedding`
   are declared as raw SQL in the Alembic migration, not here — SQLAlchemy's
   declarative `Index(..., postgresql_where=...)` needs a resolved Column
   object, and hnsw index syntax isn't yet a first-class SQLAlchemy
   construct; raw SQL in the migration is the more honest, less-magic
   choice for both.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Organization(Base):
    """Thin local mirror of LeadBoost's organization — see module docstring
    decision #1. NOT the source of truth for org identity/billing."""

    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    plan_max_concurrent_calls: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    plan_max_call_minutes_per_month: Mapped[int] = mapped_column(Integer, default=500, nullable=False)
    compliance_profile: Mapped[str] = mapped_column(String, default="standard", nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Call(Base):
    __tablename__ = "calls"
    __table_args__ = (
        UniqueConstraint("id", "created_at", name="uq_calls_id_created_at"),
        # idempotency_key is NOT a DB-level unique constraint — Postgres
        # can't enforce global uniqueness on a partitioned table without
        # including the partition key, which wouldn't give a real guarantee
        # anyway. Real idempotency enforcement is a Redis SETNX-style check
        # before insert (TRD Part 3.5); this index is for lookup/audit only.
        Index("ix_calls_idempotency_key", "idempotency_key"),
        Index("ix_calls_org_status", "organization_id", "status"),
        Index("ix_calls_org_created_at", "organization_id", "created_at"),
        Index("ix_calls_lead_id", "lead_id"),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    # Composite primary key (id, created_at) — required by Postgres native
    # partitioning (decision #2 above). `id` alone is NOT globally unique to
    # Postgres's knowledge (it's practically unique via uuid4, but the DB
    # can only enforce uniqueness including the partition key).
    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now()
    )
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)  # soft ref, see decision #1
    lead_id: Mapped[int] = mapped_column(Integer, nullable=False)  # soft ref, cross-service (roadmap E.2)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    agent_config_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String, default="queued", nullable=False)
    disposition: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recording_retained: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String, nullable=True)  # TRD Part 3.5


class CallTranscriptTurn(Base):
    __tablename__ = "call_transcript_turns"
    __table_args__ = (
        ForeignKeyConstraint(
            ["call_id", "call_created_at"],
            ["calls.id", "calls.created_at"],
            name="fk_turns_call",
        ),
        CheckConstraint("speaker IN ('agent', 'caller')", name="ck_turns_speaker"),
        Index("ix_turns_call_id", "call_id"),
        Index("ix_turns_org_id", "organization_id"),
        {"postgresql_partition_by": "RANGE (occurred_at)"},
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, nullable=False)
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)  # denormalized for direct RLS
    call_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    call_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    speaker: Mapped[str] = mapped_column(String, nullable=False)  # agent | caller
    text: Mapped[str] = mapped_column(Text, nullable=False)
    grounding_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    tool_calls: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON


class CallAttempt(Base):
    __tablename__ = "call_attempts"
    __table_args__ = (
        Index("ix_attempts_lead_scheduled", "lead_id", "scheduled_at"),
        Index("ix_attempts_org_id", "organization_id"),
        # Partial index WHERE status = 'pending' (TRD Part 4.2) is created
        # as raw SQL in the alembic migration — see decision #4.
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_id: Mapped[int] = mapped_column(Integer, nullable=False)  # soft ref, cross-service
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, default="pending", nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    call_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)  # soft ref


class AgentConfig(Base):
    """Business logic AS DATA (PRD/TRD Part 2.1) — one row per campaign
    config, interpreted at runtime by one general orchestrator."""

    __tablename__ = "agent_configs"
    __table_args__ = (Index("ix_agent_configs_org_id", "organization_id"),)

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)  # real FK, added in migration
    persona: Mapped[str] = mapped_column(Text, nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    knowledge_base_ids: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    tools: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    disposition_schema: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    language: Mapped[str] = mapped_column(String, default="en-IN", nullable=False)
    compliance_profile: Mapped[str] = mapped_column(String, default="standard", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (Index("ix_kc_org_source_type", "organization_id", "source_type"),)

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)  # real FK, added in migration
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    source_ref_id: Mapped[str | None] = mapped_column(String, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UsageRecord(Base):
    __tablename__ = "usage_records"
    __table_args__ = (
        Index("ix_usage_org_period", "organization_id", "period_start"),
        UniqueConstraint("organization_id", "call_id", "action", name="uq_usage_idempotent"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)  # real FK, added in migration
    action: Mapped[str] = mapped_column(String, nullable=False)  # e.g. "call_minutes_used"
    call_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)  # TRD 3.5 idempotency
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLogEntry(Base):
    """Append-only (INSERT-only grant enforced at the DB role level in the
    migration, TRD Part 5.7) — auth events, call lifecycle transitions,
    agent_config changes, and DNC/consent events specifically."""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_org_created_at", "organization_id", "created_at"),
        Index("ix_audit_event_type", "event_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)  # real FK, added in migration
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    subject_id: Mapped[str | None] = mapped_column(String, nullable=True)  # e.g. call_id, lead_id
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
