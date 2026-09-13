"""phase 2: conversation_turns -- durable semantic turn record

Revision ID: c4d5e6f7a8b9
Revises: b3d4e5f6a7c8
Create Date: 2026-09-05 00:00:00.000000

Adds the ONE new table Phase 2's semantic layer needs
(docs/PHASE2_DESIGN.md "Observability" / master prompt §28): a durable
record of every semantic turn, distinct from Phase 1's
`call_attempt_events` (a bare lifecycle event log with no concept of
state-before/after or model metadata). RLS-enabled/forced exactly like
every other tenant table, granted to `calling_agent_app` only —
`conversation_turns` is always written from within an already-known
organization's `org_scoped_session` (the worker has already resolved the
org by the time semantic turns run), so it does NOT need the narrow
`calling_agent_worker` cross-org grant that `call_attempts` and
`conversation_sessions` needed for the reaper's bare-attempt-id lookup
problem (docs/PHASE1_DESIGN.md / the Phase 1 hardening pass) — there is
no equivalent chicken-and-egg problem here.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

# revision identifiers, used by Alembic.
revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, Sequence[str], None] = "b3d4e5f6a7c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "conversation_turns",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.Integer, nullable=False),
        sa.Column("call_attempt_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_number", sa.Integer, nullable=False),
        sa.Column("speaker", sa.String, nullable=False),
        sa.Column("transcript", sa.Text, nullable=False),
        sa.Column("state_before", sa.Text, nullable=False),
        sa.Column("state_after", sa.Text, nullable=False),
        sa.Column("interpretation", sa.Text, nullable=False),
        sa.Column("plan", sa.Text, nullable=False),
        sa.Column("guardrail_result", sa.Text, nullable=False),
        sa.Column("final_action", sa.Text, nullable=False),
        sa.Column("response_text", sa.Text, nullable=True),
        sa.Column("interpretation_model", sa.String, nullable=False),
        sa.Column("interpretation_provider", sa.String, nullable=False),
        sa.Column("interpretation_prompt_version", sa.String, nullable=False),
        sa.Column("interpretation_policy_version", sa.String, nullable=False),
        sa.Column("interpretation_context_version", sa.Integer, nullable=False),
        sa.Column("interpretation_latency_ms", sa.Float, nullable=False),
        sa.Column("planner_model", sa.String, nullable=False),
        sa.Column("planner_prompt_version", sa.String, nullable=False),
        sa.Column("planner_latency_ms", sa.Float, nullable=False),
        sa.Column("response_model", sa.String, nullable=True),
        sa.Column("response_latency_ms", sa.Float, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("session_id", "turn_number", name="uq_conversation_turn_number"),
    )
    op.create_index("ix_conversation_turns_org_id", "conversation_turns", ["organization_id"])
    op.create_index("ix_conversation_turns_session_id", "conversation_turns", ["session_id"])
    op.create_index("ix_conversation_turns_attempt_id", "conversation_turns", ["call_attempt_id"])

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON conversation_turns TO calling_agent_app")
    op.execute("ALTER TABLE conversation_turns ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE conversation_turns FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON conversation_turns
            TO calling_agent_app
            USING (organization_id = current_setting('app.current_org_id', true)::int)
            WITH CHECK (organization_id = current_setting('app.current_org_id', true)::int)
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON conversation_turns")
    op.execute("DROP TABLE IF EXISTS conversation_turns")
