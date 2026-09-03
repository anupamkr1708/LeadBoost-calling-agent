"""phase 1 hardening: unique constraint on call_attempts(call_id, attempt_number)

Revision ID: b3d4e5f6a7c8
Revises: a7c8d9e0f1b2
Create Date: 2026-09-02 00:00:00.000000

Found missing during the Phase 1 production-readiness hardening pass
(docs/PHASE1_AUDIT_ADDENDUM.md item A): before this migration, nothing at
the database level prevented two CallAttempt rows from sharing the same
(call_id, attempt_number) — an invariant that was previously enforced only
by application logic (each retry path computes `loaded.attempt_number + 1`
exactly once). The reaper's expired-lease sweep, before it was made atomic
in this same hardening pass, had a real (if narrow, and never catastrophic
thanks to `ux_attempts_one_running_per_call`) window where two concurrent
reapers could both decide to create a "next attempt" for the same crashed
attempt, which would have silently produced two rows with the same
attempt_number instead of failing loudly. This constraint turns that class
of bug into an immediate, visible IntegrityError instead of silently
duplicated data — real defense-in-depth alongside (not instead of) the
Redis-level atomicity fix.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3d4e5f6a7c8"
down_revision: Union[str, Sequence[str], None] = "a7c8d9e0f1b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX ux_attempts_call_id_attempt_number "
        "ON call_attempts (call_id, attempt_number)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_attempts_call_id_attempt_number")
