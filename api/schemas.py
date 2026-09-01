from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class CreateCallRequest(BaseModel):
    lead_id: int = Field(..., gt=0)
    agent_config_id: uuid.UUID | None = None
    campaign_id: uuid.UUID | None = None
    idempotency_key: str | None = Field(
        default=None,
        description="Client-supplied idempotency key. Enforced via a real "
        "database-unique constraint (call_idempotency_keys, Phase 1) — not "
        "a Redis SETNX, since Redis alone wouldn't survive an eviction or "
        "restart and this system treats Postgres as durable business truth "
        "(see docs/PHASE1_DESIGN.md \"Idempotency\").",
    )


class CreateCallResponse(BaseModel):
    call_id: uuid.UUID
    status: str
    created_at: datetime
