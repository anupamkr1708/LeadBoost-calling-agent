"""baseline schema: organizations, calls, transcript turns, attempts, agent_configs, knowledge_chunks, usage_records, audit_log + RLS

Revision ID: e039e7d1c1f3
Revises:
Create Date: 2026-08-29 22:37:33.242604

Implements TRD Part 4 in full for Phase 0's tables:
 - pgvector + pgcrypto extensions (4.1)
 - the indexing plan (4.2), including the partial index on call_attempts
   and the HNSW vector index, both via raw SQL (see storage/models.py
   decision #4 for why)
 - monthly RANGE partitioning on calls/call_transcript_turns (4.3), with
   two partitions created up front (current month + next month) so the
   service can boot and take writes immediately; a scheduled job (not
   built yet — flagged in the Phase 0 gap statement) is responsible for
   rolling new partitions forward monthly.
 - Row-Level Security, enabled AND FORCED, on every tenant-scoped table
   (4.4) — FORCE matters because without it, the table owner role bypasses
   RLS by default, which would silently defeat the whole point.
 - an application DB role (`calling_agent_app`) that is NOT the table
   owner, so RLS actually applies to it, plus INSERT-only grants on
   audit_log (5.7's "append-only" requirement, enforced at the DB level,
   not just "please don't UPDATE this table" in application code).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql as pg

# revision identifiers, used by Alembic.
revision: str = "e039e7d1c1f3"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TENANT_TABLES_FOR_RLS = (
    "organizations",
    "calls",
    "call_transcript_turns",
    "call_attempts",
    "agent_configs",
    "knowledge_chunks",
    "usage_records",
    "audit_log",
)


def upgrade() -> None:
    # --- extensions (TRD 4.1) ---
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # --- organizations: thin local mirror (storage/models.py decision #1) ---
    op.create_table(
        "organizations",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("plan_max_concurrent_calls", sa.Integer, nullable=False, server_default="2"),
        sa.Column("plan_max_call_minutes_per_month", sa.Integer, nullable=False, server_default="500"),
        sa.Column("compliance_profile", sa.String, nullable=False, server_default="standard"),
        sa.Column("synced_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # --- calls: RANGE partitioned by created_at (TRD 4.3) ---
    op.execute(
        """
        CREATE TABLE calls (
            id UUID NOT NULL DEFAULT gen_random_uuid(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            organization_id INTEGER NOT NULL,
            lead_id INTEGER NOT NULL,
            campaign_id UUID,
            agent_config_id UUID,
            status VARCHAR NOT NULL DEFAULT 'queued',
            disposition VARCHAR,
            started_at TIMESTAMPTZ,
            ended_at TIMESTAMPTZ,
            duration_seconds INTEGER,
            recording_retained BOOLEAN NOT NULL DEFAULT false,
            summary TEXT,
            idempotency_key VARCHAR,
            PRIMARY KEY (id, created_at)
        ) PARTITION BY RANGE (created_at);
        """
    )
    # NOTE on idempotency_key: Postgres cannot enforce a UNIQUE constraint on
    # a partitioned table unless the partition key (created_at) is part of
    # it, which would only guarantee uniqueness *within the same instant*,
    # not globally — not good enough for a real idempotency guarantee. This
    # is not actually a gap: TRD Part 3.5 already specifies idempotency is
    # enforced via a Redis SETNX-style check *before* the row is inserted,
    # not via a DB constraint. This column is therefore a plain indexed
    # audit/lookup field, and Redis (storage/redis_state.py, built when
    # calls are actually dispatched in Phase 1) is the real enforcement
    # point — consistent with, not a deviation from, the TRD.
    op.execute("CREATE INDEX ix_calls_idempotency_key ON calls (idempotency_key)")
    op.execute("CREATE INDEX ix_calls_org_status ON calls (organization_id, status)")
    op.execute("CREATE INDEX ix_calls_org_created_at ON calls (organization_id, created_at)")
    op.execute("CREATE INDEX ix_calls_lead_id ON calls (lead_id)")
    _create_monthly_partitions("calls", "created_at")

    # --- call_transcript_turns: RANGE partitioned by occurred_at, real FK into calls ---
    op.execute(
        """
        CREATE TABLE call_transcript_turns (
            id UUID NOT NULL DEFAULT gen_random_uuid(),
            occurred_at TIMESTAMPTZ NOT NULL,
            organization_id INTEGER NOT NULL,
            call_id UUID NOT NULL,
            call_created_at TIMESTAMPTZ NOT NULL,
            speaker VARCHAR NOT NULL,
            text TEXT NOT NULL,
            grounding_score DOUBLE PRECISION,
            tool_calls TEXT,
            PRIMARY KEY (id, occurred_at),
            CONSTRAINT ck_turns_speaker CHECK (speaker IN ('agent', 'caller')),
            CONSTRAINT fk_turns_call FOREIGN KEY (call_id, call_created_at)
                REFERENCES calls (id, created_at)
        ) PARTITION BY RANGE (occurred_at);
        """
    )
    op.execute("CREATE INDEX ix_turns_call_id ON call_transcript_turns (call_id)")
    op.execute("CREATE INDEX ix_turns_org_id ON call_transcript_turns (organization_id)")
    _create_monthly_partitions("call_transcript_turns", "occurred_at")

    # --- call_attempts ---
    op.create_table(
        "call_attempts",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("lead_id", sa.Integer, nullable=False),
        sa.Column("organization_id", sa.Integer, nullable=False),
        sa.Column("attempt_number", sa.Integer, nullable=False),
        sa.Column("status", sa.String, nullable=False, server_default="pending"),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("call_id", pg.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_attempts_lead_scheduled", "call_attempts", ["lead_id", "scheduled_at"])
    op.create_index("ix_attempts_org_id", "call_attempts", ["organization_id"])
    # Partial index (TRD 4.2): the retry scheduler only ever scans pending rows.
    op.execute(
        "CREATE INDEX ix_attempts_pending ON call_attempts (scheduled_at) WHERE status = 'pending'"
    )

    # --- agent_configs (business logic as data, PRD/TRD Part 2.1) ---
    op.create_table(
        "agent_configs",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("persona", sa.Text, nullable=False),
        sa.Column("goal", sa.Text, nullable=False),
        sa.Column("knowledge_base_ids", pg.ARRAY(sa.String), nullable=False),
        sa.Column("tools", pg.ARRAY(sa.String), nullable=False),
        sa.Column("disposition_schema", pg.ARRAY(sa.String), nullable=False),
        sa.Column("language", sa.String, nullable=False, server_default="en-IN"),
        sa.Column("compliance_profile", sa.String, nullable=False, server_default="standard"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_agent_configs_org_id", "agent_configs", ["organization_id"])

    # --- knowledge_chunks (pgvector, TRD/roadmap Part D.5) ---
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("source_type", sa.String, nullable=False),
        sa.Column("source_ref_id", sa.String, nullable=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_kc_org_source_type", "knowledge_chunks", ["organization_id", "source_type"])
    # HNSW vector index (TRD 4.2) — mandatory, or retrieval degrades to a full scan.
    op.execute(
        "CREATE INDEX ix_kc_embedding_hnsw ON knowledge_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )

    # --- usage_records ---
    op.create_table(
        "usage_records",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("action", sa.String, nullable=False),
        sa.Column("call_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("quantity", sa.Float, nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("organization_id", "call_id", "action", name="uq_usage_idempotent"),
    )
    op.create_index("ix_usage_org_period", "usage_records", ["organization_id", "period_start"])

    # --- audit_log (append-only, TRD 5.7) ---
    op.create_table(
        "audit_log",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Integer, nullable=False),
        sa.Column("event_type", sa.String, nullable=False),
        sa.Column("subject_id", sa.String, nullable=True),
        sa.Column("detail", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_audit_org_created_at", "audit_log", ["organization_id", "created_at"])
    op.create_index("ix_audit_event_type", "audit_log", ["event_type"])

    # --- application role (non-owner, so RLS actually applies to it) ---
    # Idempotent: this migration may run against a DB where a role from a
    # prior local bootstrap already exists — the actual APP role must be
    # different from the table-owning role for FORCE ROW LEVEL SECURITY to
    # bind it (table owners bypass RLS by default even with FORCE unless
    # they're a different role than the one that ran CREATE TABLE).
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'calling_agent_app') THEN
                CREATE ROLE calling_agent_app WITH LOGIN PASSWORD 'set-a-real-password-outside-this-migration';
            END IF;
        END
        $$;
        """
    )
    for table in TENANT_TABLES_FOR_RLS:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO calling_agent_app")
    # audit_log is append-only for the app role — no UPDATE/DELETE grant (TRD 5.7).
    op.execute("REVOKE UPDATE, DELETE ON audit_log FROM calling_agent_app")
    op.execute("REVOKE UPDATE, DELETE ON audit_log FROM PUBLIC")

    # --- Row-Level Security: enable AND FORCE on every tenant-scoped table (TRD 4.4) ---
    for table in TENANT_TABLES_FOR_RLS:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # organizations' tenant key is its own `id` column, not `organization_id`
    # — give it its own policy; every other table uses `organization_id`.
    op.execute(
        """
        CREATE POLICY tenant_isolation ON organizations
            USING (id = current_setting('app.current_org_id', true)::int)
            WITH CHECK (id = current_setting('app.current_org_id', true)::int)
        """
    )
    for table in TENANT_TABLES_FOR_RLS:
        if table == "organizations":
            continue
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
                USING (organization_id = current_setting('app.current_org_id', true)::int)
                WITH CHECK (organization_id = current_setting('app.current_org_id', true)::int)
            """
        )


def downgrade() -> None:
    for table in TENANT_TABLES_FOR_RLS:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.execute("DROP TABLE IF EXISTS audit_log")
    op.execute("DROP TABLE IF EXISTS usage_records")
    op.execute("DROP TABLE IF EXISTS knowledge_chunks")
    op.execute("DROP TABLE IF EXISTS agent_configs")
    op.execute("DROP TABLE IF EXISTS call_attempts")
    op.execute("DROP TABLE IF EXISTS call_transcript_turns")
    op.execute("DROP TABLE IF EXISTS calls")
    op.execute("DROP TABLE IF EXISTS organizations")


def _create_monthly_partitions(table: str, partition_col: str) -> None:
    """Creates a partition for the current month and the next month, so the
    service can boot and take writes immediately. Rolling new partitions
    forward monthly is a scheduled-job responsibility — NOT built in Phase 0
    (flagged explicitly in docs/SYSTEM_MAP.md as 🔴, per the master prompt's
    'build the trigger or don't build the subsystem yet' rule: there's no
    live trigger for partition rollover yet, so we deliberately do not
    pretend this is a finished piece of automation)."""
    op.execute(
        f"""
        DO $$
        DECLARE
            month_start date := date_trunc('month', now());
        BEGIN
            FOR i IN 0..1 LOOP
                EXECUTE format(
                    'CREATE TABLE IF NOT EXISTS %I PARTITION OF {table} FOR VALUES FROM (%L) TO (%L)',
                    '{table}_' || to_char(month_start + (i || ' month')::interval, 'YYYY_MM'),
                    month_start + (i || ' month')::interval,
                    month_start + ((i + 1) || ' month')::interval
                );
            END LOOP;
        END
        $$;
        """
    )
