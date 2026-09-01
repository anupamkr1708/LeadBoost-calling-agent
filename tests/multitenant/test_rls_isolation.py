"""The single most important test suite in Phase 0 (TRD Part 6.4).

This does NOT go through the ORM or any application filtering code — it
connects directly as `calling_agent_app` (the RLS-bound, non-owner DB role)
and proves that even a bare `SELECT * FROM calls` with no WHERE clause at
all returns only the current tenant's rows. This is deliberately the
strongest possible test: it proves the database physically cannot return
another tenant's data, independent of whether application code remembers to
filter correctly.
"""
from __future__ import annotations

import uuid

import psycopg
import pytest

ORG_A = 9001
ORG_B = 9002


def _set_org(conn: psycopg.Connection, org_id: int) -> None:
    # SET does not support query parameters ($1) in Postgres — it's parsed
    # like DDL, not a normal statement. Safe here because org_id is always
    # an int from our own test constants, never user input.
    assert isinstance(org_id, int)
    with conn.cursor() as cur:
        cur.execute(f"SET app.current_org_id = '{org_id}'")


@pytest.fixture()
def seeded_two_orgs(clean_db, app_settings):
    """Seeds two organizations, one call each, one audit_log entry each.

    IMPORTANT CORRECTION from the first draft of this fixture: I originally
    assumed the owner role (`calling_agent`) bypasses RLS for seeding. That's
    wrong — FORCE ROW LEVEL SECURITY (which the migration deliberately sets,
    see decision note in the migration file) applies RLS to the table owner
    too, which is the whole point of FORCE. So seeding must set
    `app.current_org_id` per insert just like any other write. This is
    actually the more honest fixture: it proves RLS holds even for the role
    that created the tables, not just for a deliberately-restricted app role.
    """
    import psycopg

    dsn = app_settings.database_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        for org_id in (ORG_A, ORG_B):
            cur.execute(f"SET app.current_org_id = '{org_id}'")
            cur.execute(
                "INSERT INTO organizations (id, plan_max_concurrent_calls) VALUES (%s, 2)",
                (org_id,),
            )
            call_id = uuid.uuid4()
            cur.execute(
                "INSERT INTO calls (id, organization_id, lead_id, status) VALUES (%s, %s, %s, 'queued')",
                (call_id, org_id, org_id * 10),
            )
            cur.execute(
                "INSERT INTO audit_log (organization_id, event_type, subject_id) VALUES (%s, %s, %s)",
                (org_id, "test_seed", str(call_id)),
            )
    return {"org_a": ORG_A, "org_b": ORG_B}


def test_bare_select_never_returns_other_tenants_rows(seeded_two_orgs, app_role_dsn):
    """The adversarial case the TRD calls out explicitly: a query with NO
    tenant filter at all must still only return the caller's own org."""
    with psycopg.connect(app_role_dsn, autocommit=True) as conn:
        _set_org(conn, ORG_A)
        with conn.cursor() as cur:
            cur.execute("SELECT organization_id FROM calls")  # deliberately no WHERE clause
            rows = cur.fetchall()
    org_ids_seen = {r[0] for r in rows}
    assert org_ids_seen == {ORG_A}, (
        f"RLS FAILED: org A's unfiltered query saw organization_ids {org_ids_seen}, "
        f"expected only {{ORG_A}}"
    )


def test_switching_org_context_switches_visible_rows(seeded_two_orgs, app_role_dsn):
    with psycopg.connect(app_role_dsn, autocommit=True) as conn:
        _set_org(conn, ORG_A)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM calls")
            count_a = cur.fetchone()[0]

        _set_org(conn, ORG_B)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM calls")
            count_b = cur.fetchone()[0]

    assert count_a == 1
    assert count_b == 1


def test_no_org_context_set_returns_zero_rows_not_all_rows(seeded_two_orgs, app_role_dsn):
    """Fail-closed check: if the application forgets to SET app.current_org_id
    at all, the policy's current_setting(..., true) returns NULL, and
    `organization_id = NULL` is never true in SQL — so the safe failure mode
    is 'see nothing', not 'see everything'. This is the case that matters
    most: a bug that forgets to set org context must not leak all tenants'
    data, it must leak none."""
    with psycopg.connect(app_role_dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM calls")
        count = cur.fetchone()[0]
    assert count == 0, (
        "SAFETY FAILURE: querying with no org context set returned rows — "
        "the fail-closed guarantee (missing context => see nothing) is broken."
    )


def test_cross_tenant_write_is_rejected(seeded_two_orgs, app_role_dsn):
    """WITH CHECK on the policy must block org A's session from inserting a
    row claiming to belong to org B."""
    with psycopg.connect(app_role_dsn, autocommit=True) as conn:
        _set_org(conn, ORG_A)
        with conn.cursor() as cur, pytest.raises(psycopg.errors.Error):
            cur.execute(
                "INSERT INTO calls (id, organization_id, lead_id, status) VALUES (%s, %s, %s, 'queued')",
                (uuid.uuid4(), ORG_B, 1),
            )


def test_audit_log_is_append_only_for_app_role(seeded_two_orgs, app_role_dsn):
    """TRD Part 5.7: audit_log must reject UPDATE/DELETE from the app role
    entirely, at the grant level — not just 'application code doesn't call
    UPDATE', which would be trivially bypassable by a bug or a compromised
    credential."""
    with psycopg.connect(app_role_dsn, autocommit=True) as conn:
        _set_org(conn, ORG_A)
        with conn.cursor() as cur, pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("UPDATE audit_log SET event_type = 'tampered' WHERE organization_id = %s", (ORG_A,))
