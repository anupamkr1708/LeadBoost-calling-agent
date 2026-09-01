"""POST /v1/calls — Phase 1: real admission through the execution runtime.

auth -> tenant validation -> idempotency -> persist (all inside
`CallService.create_call`'s one transaction) -> enqueue (only for a
genuinely NEW call; an idempotent replay must not enqueue a second time).
See docs/PHASE1_DESIGN.md "Idempotency" for why enqueue happens outside
that transaction, and orchestrator/worker_runtime.py's reconciliation
sweep for how a lost enqueue (process dies between commit and this push)
is recovered.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request, status

from api.auth import AuthContext, require_auth
from api.rate_limit import enforce_rate_limit
from api.schemas import CreateCallRequest, CreateCallResponse
from orchestrator.call_service import CallService

router = APIRouter(prefix="/v1", tags=["calls"])
_call_service = CallService()


@router.post("/calls", response_model=CreateCallResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_call(
    body: CreateCallRequest,
    request: Request,
    auth: AuthContext = Depends(require_auth),  # noqa: B008 - this is FastAPI's actual DI idiom, not a bug
) -> CreateCallResponse:
    enforce_rate_limit(auth)

    created = _call_service.create_call(
        organization_id=auth.organization_id,
        lead_id=body.lead_id,
        agent_config_id=body.agent_config_id,
        campaign_id=body.campaign_id,
        idempotency_key=body.idempotency_key,
    )

    if created.is_new:
        assert created.first_attempt_id is not None
        # The Queue instance is constructed once by the composition root
        # (app/main.py's lifespan) and handed to every request via
        # app.state — this endpoint never constructs a Redis client or a
        # Queue itself (docs/PHASE1_DESIGN.md "Composition root").
        await request.app.state.queue.enqueue(str(created.first_attempt_id), ready_at=time.time())

    return CreateCallResponse(call_id=created.call_id, status=created.status, created_at=created.created_at)

