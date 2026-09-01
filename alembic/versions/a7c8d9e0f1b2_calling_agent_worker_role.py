"""phase 1: calling_agent_worker role -- narrow cross-org SELECT for the
worker runtime's reaper/reconciliation sweeps

Revision ID: a7c8d9e0f1b2
Revises: f1a2b3c4d5e6
Create Date: 2026-08-31 00:10:00.000000

The worker runtime routinely needs to answer "what organization does this
bare attempt_id (claimed off the GLOBAL Redis queue) belong to?" BEFORE it
can open an org_scoped_session for that org — RLS with `app.current_org_id`
unset simply returns zero rows, by design, for `calling_agent_app`. That's
correct for request-serving traffic and would be a real bug if it weren't
true. But the worker runtime is not request-serving traffic.

`calling_agent_worker` is a new, separate, narrowly-scoped role:
  - NOT superuser, NOT BYPASSRLS (storage.db's connect-time guard would
    refuse it in staging/production if it were — see docs/PHASE0_AUDIT.md
    Finding 1; this migration does not reopen that gap).
  - SELECT-only, and only on the two tables the worker runtime actually
    needs cross-org visibility into (call_attempts, conversation_sessions).
    It has no grant at all on organizations, calls, agent_configs,
    knowledge_chunks, usage_records, or audit_log.
  - Its cross-org read access comes from a dedicated permissive policy
    (`system_worker_cross_org_read`, `TO calling_agent_worker`), not from
    an RLS bypass — FORCE ROW LEVEL SECURITY stays on, and this role is
    still fully subject to it; it just happens to have a policy that says
    "any row" for SELECT specifically.

The existing `tenant_isolation` policies on these two tables previously
had no explicit `TO` clause (implicitly PUBLIC — every role, present and
future, was silently subject to them). This migration scopes them
explicitly `TO calling_agent_app`, which is a real tightening
(calling_agent_worker's access is now defined by exactly one policy this
migration creates, not by accidentally qualifying under a PUBLIC policy
meant for the app role) and changes no observable behavior for
calling_agent_app itself.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c8d9e0f1b2"
down_revision: Union[str, Sequence[str], None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CROSS_ORG_READ_TABLES = ("call_attempts", "conversation_sessions")


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'calling_agent_worker') THEN
                CREATE ROLE calling_agent_worker WITH LOGIN PASSWORD 'set-a-real-password-outside-this-migration';
            END IF;
        END
        $$;
        """
    )
    # CONNECT ON DATABASE and USAGE ON SCHEMA public are granted by
    # CI/deployment setup, exactly like calling_agent_app — the baseline
    # migration doesn't grant those for calling_agent_app either (a
    # migration doesn't know the target database's name, and schema/db
    # grants are an environment-setup concern, not a schema-definition one).

    for table in CROSS_ORG_READ_TABLES:
        op.execute(f"GRANT SELECT ON {table} TO calling_agent_worker")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
                TO calling_agent_app
                USING (organization_id = current_setting('app.current_org_id', true)::int)
                WITH CHECK (organization_id = current_setting('app.current_org_id', true)::int)
            """
        )
        op.execute(
            f"""
            CREATE POLICY system_worker_cross_org_read ON {table}
                FOR SELECT
                TO calling_agent_worker
                USING (true)
            """
        )


def downgrade() -> None:
    for table in CROSS_ORG_READ_TABLES:
        op.execute(f"DROP POLICY IF EXISTS system_worker_cross_org_read ON {table}")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
                USING (organization_id = current_setting('app.current_org_id', true)::int)
                WITH CHECK (organization_id = current_setting('app.current_org_id', true)::int)
            """
        )
        op.execute(f"REVOKE SELECT ON {table} FROM calling_agent_worker")
