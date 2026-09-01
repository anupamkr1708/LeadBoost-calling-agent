"""POST /v1/calls — Phase 0 scope, deliberately limited.

This writes a real row to the real, RLS-protected `calls` table and returns
a real 202, but it does NOT dispatch a call — there is no telephony
adapter, no orchestrator, nothing to dispatch TO yet (those are Phase 1+).
Per the master prompt's integration rule, we are NOT pretending this is
more finished than it is: the endpoint's own docstring and the response
status are the honest signal ("queued", not "dialing" or "in_progress").
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy import text

from api.auth import AuthContext, require_auth
from api.rate_limit import enforce_rate_limit
from api.schemas import CreateCallRequest, CreateCallResponse
from storage.db import org_scoped_session

router = APIRouter(prefix="/v1", tags=["calls"])


@router.post("/calls", response_model=CreateCallResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_call(
    body: CreateCallRequest,
    auth: AuthContext = Depends(require_auth),  # noqa: B008 - this is FastAPI's actual DI idiom, not a bug
) -> CreateCallResponse:
    enforce_rate_limit(auth)

    with org_scoped_session(auth.organization_id) as session:
        result = session.execute(
            text(
                """
                INSERT INTO calls (organization_id, lead_id, agent_config_id, campaign_id, status, idempotency_key)
                VALUES (:org_id, :lead_id, :agent_config_id, :campaign_id, 'queued', :idempotency_key)
                RETURNING id, created_at, status
                """
            ),
            {
                "org_id": auth.organization_id,
                "lead_id": body.lead_id,
                "agent_config_id": str(body.agent_config_id) if body.agent_config_id else None,
                "campaign_id": str(body.campaign_id) if body.campaign_id else None,
                "idempotency_key": body.idempotency_key,
            },
        )
        row = result.fetchone()

    if row is None:  # pragma: no cover - RETURNING should always yield a row on success;
        # this is a defensive fail-closed check, not expected to be reachable in practice.
        raise RuntimeError("INSERT ... RETURNING produced no row — this should never happen.")

    return CreateCallResponse(call_id=row.id, status=row.status, created_at=row.created_at)
