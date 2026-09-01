# LeadBoost-side additive migration (NOT applied — no push access to that repo)

This is a standalone Alembic-style migration, written against
`LeadBoost-saas`'s existing schema conventions (as described in the
roadmap), ready for whoever owns that repository to review and apply. It
was never run against LeadBoost's actual database — I have no push/PR
access to `anupamkr1708/LeadBoost-saas`, and running an untested migration
against a repo I don't control would be worse than not running it at all.

## What this adds, and why (per roadmap Part E.2 / TRD Part 0)

- `leads.phone_verified BOOLEAN NOT NULL DEFAULT false` — the calling
  agent must never dial an unverified number; this is the flag it checks
  before scheduling any `call_attempts` row.
- `leads.consent_status VARCHAR NOT NULL DEFAULT 'unknown'` — one of
  `unknown | granted | revoked`, driven by the DNC/consent-revocation gate
  (TRD Part 2.3). This is intentionally on the LeadBoost side, not
  duplicated into the calling-agent's own schema, because LeadBoost is the
  system of record for lead data and the calling agent should never be the
  only place this fact lives.
- `leads.do_not_call BOOLEAN NOT NULL DEFAULT false` — set to `true`,
  irreversibly from the calling agent's side, the moment the DNC gate
  fires. The calling agent can only ever flip this to `true`; only a human
  in LeadBoost can flip it back.
- A `call_log` view (not a table) in LeadBoost, backed by a foreign data
  wrapper or a periodic export from the calling agent's `calls` table —
  deliberately NOT specified further here, because that's an integration
  decision (Phase 5) that depends on LeadBoost's actual replication/FDW
  posture, which I have no visibility into from outside that repo.

## Migration (Alembic-style, LeadBoost-side)

```python
"""add calling-agent integration fields to leads

Revision ID: <fill in when applied>
Revises: <fill in — depends on LeadBoost's current head revision>
"""
from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    op.add_column("leads", sa.Column("phone_verified", sa.Boolean, nullable=False, server_default="false"))
    op.add_column(
        "leads",
        sa.Column("consent_status", sa.String, nullable=False, server_default="unknown"),
    )
    op.add_column("leads", sa.Column("do_not_call", sa.Boolean, nullable=False, server_default="false"))
    op.create_check_constraint(
        "ck_leads_consent_status",
        "leads",
        "consent_status IN ('unknown', 'granted', 'revoked')",
    )
    op.create_index("ix_leads_do_not_call", "leads", ["do_not_call"])


def downgrade() -> None:
    op.drop_index("ix_leads_do_not_call", table_name="leads")
    op.drop_constraint("ck_leads_consent_status", "leads", type_="check")
    op.drop_column("leads", "do_not_call")
    op.drop_column("leads", "consent_status")
    op.drop_column("leads", "phone_verified")
```

## Before applying this against the real repo, whoever owns it should:

1. Confirm the actual current Alembic head revision in `LeadBoost-saas` and
   set `down_revision` accordingly (I could not do this without running
   migrations against that repo's real database).
2. Confirm `leads` is in fact the correct table name — I inferred it from
   the roadmap document; I did not clone and inspect the live LeadBoost
   schema against this migration before writing it.
3. Decide the `call_log` replication mechanism referenced above; it is
   deliberately left unspecified here rather than guessed at.
