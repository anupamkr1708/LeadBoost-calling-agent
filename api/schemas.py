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
        description="Client-supplied idempotency key. Real dedup enforcement "
        "is a Redis SETNX check added in Phase 1 when calls are actually "
        "dispatched — Phase 0 only records it (TRD Part 3.5).",
    )


class CreateCallResponse(BaseModel):
    call_id: uuid.UUID
    status: str
    created_at: datetime
