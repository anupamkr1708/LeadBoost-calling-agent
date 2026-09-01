"""phase 1: execution runtime -- conversation_sessions, call_attempt_events,
call_idempotency_keys, call_attempts execution columns + one-running-per-call

Revision ID: f1a2b3c4d5e6
Revises: e039e7d1c1f3
Create Date: 2026-08-31 00:00:00.000000

Implements docs/PHASE1_DESIGN.md's "Domain model" and "Idempotency"
sections:
 - `call_attempts` gets the execution-lifecycle columns the runtime needs
   (session_id, worker_id, started_at, ended_at, failure_category,
   failure_detail, updated_at) plus the ONE real database-enforced
   correctness invariant behind "the same call can never run twice": a
   partial unique index on `call_attempts(call_id) WHERE status =
   'running'`.
 - `conversation_sessions` and `call_attempt_events` are new tenant-scoped
   tables, RLS-enabled/forced exactly like Phase 0's tables (same
   `calling_agent_app` role, same policy shape).
 - `call_idempotency_keys` is the real enforcement point for
   idempotent call creation — deliberately NOT partitioned (see
   docs/PHASE1_DESIGN.md "Idempotency" for why `calls.idempotency_key`
   itself can't carry this constraint).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "e039e7d1c1f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NEW_TENANT_TABLES_FOR_RLS = (
    "conversation_sessions",
    "call_attempt_events",
    "call_idempotency_keys",
)


def upgrade() -> None:
    # --- call_attempts: execution-lifecycle columns ---
    op.add_column("call_attempts", sa.Column("session_id", pg.UUID(as_uuid=True), nullable=True))
    op.add_column("call_attempts", sa.Column("worker_id", sa.String, nullable=True))
    op.add_column("call_attempts", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("call_attempts", sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("call_attempts", sa.Column("failure_category", sa.String, nullable=True))
    op.add_column("call_attempts", sa.Column("failure_detail", sa.Text, nullable=True))
    op.add_column(
        "call_attempts",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_attempts_call_id", "call_attempts", ["call_id"])
    # THE real "same call cannot be concurrently executed twice" guarantee
    # (docs/PHASE1_DESIGN.md "Concurrency / worker acquisition") — enforced
    # by Postgres regardless of any race in the Redis claim layer above it.
    op.execute(
        "CREATE UNIQUE INDEX ux_attempts_one_running_per_call "
        "ON call_attempts (call_id) WHERE status = 'running'"
    )

    # --- conversation_sessions ---
    op.create_table(
        "conversation_sessions",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Integer, nullable=False),
        sa.Column("call_attempt_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_id", sa.String, nullable=False),
        sa.Column("state", sa.String, nullable=False, server_default="started"),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_sessions_org_id", "conversation_sessions", ["organization_id"])
    op.create_index("ix_sessions_attempt_id", "conversation_sessions", ["call_attempt_id"])

    # --- call_attempt_events: ordered durable execution record ---
    op.create_table(
        "call_attempt_events",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Integer, nullable=False),
        sa.Column("call_attempt_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence_number", sa.Integer, nullable=False),
        sa.Column("event_type", sa.String, nullable=False),
        sa.Column("detail", sa.Text, nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("call_attempt_id", "sequence_number", name="uq_attempt_event_sequence"),
    )
    op.create_index("ix_attempt_events_org_id", "call_attempt_events", ["organization_id"])
    op.create_index("ix_attempt_events_attempt_id", "call_attempt_events", ["call_attempt_id"])

    # --- call_idempotency_keys: the real idempotency enforcement point ---
    op.create_table(
        "call_idempotency_keys",
        sa.Column("organization_id", sa.Integer, primary_key=True),
        sa.Column("idempotency_key", sa.String, primary_key=True),
        sa.Column("call_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("call_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["call_id", "call_created_at"], ["calls.id", "calls.created_at"], name="fk_idempotency_call"
        ),
    )

    # --- grants + RLS, same shape as the baseline migration ---
    for table in NEW_TENANT_TABLES_FOR_RLS:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO calling_agent_app")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
                USING (organization_id = current_setting('app.current_org_id', true)::int)
                WITH CHECK (organization_id = current_setting('app.current_org_id', true)::int)
            """
        )


def downgrade() -> None:
    for table in NEW_TENANT_TABLES_FOR_RLS:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.execute("DROP TABLE IF EXISTS call_idempotency_keys")
    op.execute("DROP TABLE IF EXISTS call_attempt_events")
    op.execute("DROP TABLE IF EXISTS conversation_sessions")
    op.execute("DROP INDEX IF EXISTS ux_attempts_one_running_per_call")
    op.drop_index("ix_attempts_call_id", table_name="call_attempts")
    op.drop_column("call_attempts", "updated_at")
    op.drop_column("call_attempts", "failure_detail")
    op.drop_column("call_attempts", "failure_category")
    op.drop_column("call_attempts", "ended_at")
    op.drop_column("call_attempts", "started_at")
    op.drop_column("call_attempts", "worker_id")
    op.drop_column("call_attempts", "session_id")
