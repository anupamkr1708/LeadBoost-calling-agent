"""JWT bearer authentication (TRD Part 5.3/5.4): LeadBoost calls this
service with an RS256-signed service token carrying an `organization_id`
claim. We verify against our own public key (asymmetric — LeadBoost holds
the private key for tokens it issues to us, or we hold it for tokens we
issue in response, depending on direction; Phase 0 only needs verification
of inbound tokens, so only the public key is used here).

No fallback secret, no "trust the header if present" shortcut — this is the
same fail-closed posture as app/config.py, applied to request auth.
"""
from __future__ import annotations

import jwt
from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from api.errors import AuthError
from app.config import get_settings

_bearer_scheme = HTTPBearer(auto_error=False)


class AuthContext:
    __slots__ = ("organization_id", "subject")

    def __init__(self, organization_id: int, subject: str) -> None:
        self.organization_id = organization_id
        self.subject = subject


async def require_auth(request: Request) -> AuthContext:
    credentials: HTTPAuthorizationCredentials | None = await _bearer_scheme(request)
    if credentials is None or not credentials.credentials:
        raise AuthError("Missing bearer token.")

    settings = get_settings()
    public_key = settings.jwt_public_key.get_secret_value()

    try:
        payload = jwt.decode(
            credentials.credentials,
            public_key,
            algorithms=["RS256"],
            options={"require": ["exp", "organization_id", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Token expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError(f"Invalid token: {exc}") from exc

    org_id = payload.get("organization_id")
    if not isinstance(org_id, int) or org_id <= 0:
        raise AuthError("Token missing a valid organization_id claim.")

    return AuthContext(organization_id=org_id, subject=str(payload.get("sub")))
