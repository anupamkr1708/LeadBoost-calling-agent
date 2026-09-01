"""The Call Service: the admission path behind `POST /v1/calls`.

auth -> tenant validation -> idempotency -> persist -> first attempt, all
inside ONE Postgres transaction (`storage.db.org_scoped_session`) — see
docs/PHASE1_DESIGN.md "Idempotency". Enqueueing into Redis happens OUTSIDE
this function, after the transaction commits — a Redis push can't be
rolled back by a Postgres abort, so it must not be able to partially
participate in one; the caller (api/endpoints/calls.py) does the enqueue,
and `orchestrator/worker_runtime.py`'s reconciliation sweep recovers the
case where the process dies in the gap between commit and enqueue.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from storage.db import org_scoped_session


@dataclass(frozen=True)
class CreatedCall:
    call_id: uuid.UUID
    status: str
    created_at: datetime
    # None when this is an idempotent replay of an existing Call — the
    # caller must NOT enqueue anything in that case (whatever attempt(s)
    # already exist for it are the queue's business, not a fresh
    # admission's; enqueueing a second time would violate "no duplicate
    # active execution").
    first_attempt_id: uuid.UUID | None
    is_new: bool


class CallService:
    def create_call(
        self,
        *,
        organization_id: int,
        lead_id: int,
        agent_config_id: uuid.UUID | None,
        campaign_id: uuid.UUID | None,
        idempotency_key: str | None,
    ) -> CreatedCall:
        with org_scoped_session(organization_id) as session:
            if idempotency_key:
                existing = self._find_existing(session, organization_id, idempotency_key)
                if existing is not None:
                    return existing

            call_row = session.execute(
                text(
                    """
                    INSERT INTO calls
                        (organization_id, lead_id, agent_config_id, campaign_id, status, idempotency_key)
                    VALUES (:org_id, :lead_id, :agent_config_id, :campaign_id, 'queued', :idempotency_key)
                    RETURNING id, created_at, status
                    """
                ),
                {
                    "org_id": organization_id,
                    "lead_id": lead_id,
                    "agent_config_id": str(agent_config_id) if agent_config_id else None,
                    "campaign_id": str(campaign_id) if campaign_id else None,
                    "idempotency_key": idempotency_key,
                },
            ).fetchone()
            if call_row is None:  # pragma: no cover - defensive; RETURNING always yields a row on success
                raise RuntimeError("INSERT ... RETURNING produced no row for calls — this should never happen.")

            if idempotency_key:
                # The real concurrent-correctness guarantee: PRIMARY KEY
                # (organization_id, idempotency_key) on call_idempotency_keys.
                # A SAVEPOINT scopes the conflict to just this insert, so a
                # losing request can clean up its own orphaned `calls` row
                # and return the winner's Call, instead of the whole
                # transaction (and the winner's work, if it's still in
                # flight) being rolled back.
                try:
                    with session.begin_nested():
                        session.execute(
                            text(
                                """
                                INSERT INTO call_idempotency_keys
                                    (organization_id, idempotency_key, call_id, call_created_at)
                                VALUES (:org_id, :key, :call_id, :call_created_at)
                                """
                            ),
                            {
                                "org_id": organization_id,
                                "key": idempotency_key,
                                "call_id": call_row.id,
                                "call_created_at": call_row.created_at,
                            },
                        )
                except IntegrityError:
                    session.execute(
                        text("DELETE FROM calls WHERE id = :id AND created_at = :created_at"),
                        {"id": call_row.id, "created_at": call_row.created_at},
                    )
                    existing = self._find_existing(session, organization_id, idempotency_key)
                    if existing is None:  # pragma: no cover - the winner's row must exist by definition
                        raise RuntimeError(
                            "idempotency key conflict but no existing row found — this should never happen."
                        ) from None
                    return existing

            attempt_row = session.execute(
                text(
                    """
                    INSERT INTO call_attempts (lead_id, organization_id, attempt_number, status, scheduled_at, call_id)
                    VALUES (:lead_id, :org_id, 1, 'pending', now(), :call_id)
                    RETURNING id
                    """
                ),
                {"lead_id": lead_id, "org_id": organization_id, "call_id": call_row.id},
            ).fetchone()
            if attempt_row is None:  # pragma: no cover - defensive
                raise RuntimeError("INSERT ... RETURNING produced no row for call_attempts.")

            return CreatedCall(
                call_id=call_row.id,
                status=call_row.status,
                created_at=call_row.created_at,
                first_attempt_id=attempt_row.id,
                is_new=True,
            )

    @staticmethod
    def _find_existing(session: Session, organization_id: int, idempotency_key: str) -> CreatedCall | None:
        row = session.execute(
            text(
                """
                SELECT c.id, c.created_at, c.status
                FROM call_idempotency_keys k
                JOIN calls c ON c.id = k.call_id AND c.created_at = k.call_created_at
                WHERE k.organization_id = :org_id AND k.idempotency_key = :key
                """
            ),
            {"org_id": organization_id, "key": idempotency_key},
        ).fetchone()
        if row is None:
            return None
        return CreatedCall(
            call_id=row.id, status=row.status, created_at=row.created_at, first_attempt_id=None, is_new=False
        )
