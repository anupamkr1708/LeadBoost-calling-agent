from __future__ import annotations

import time

import jwt

from app.config import get_settings


def mint_test_token(organization_id: int, subject: str = "test-leadboost-service", expires_in: int = 3600) -> str:
    settings = get_settings()
    private_key = settings.jwt_private_key.get_secret_value()
    payload = {
        "sub": subject,
        "organization_id": organization_id,
        "iat": int(time.time()),
        "exp": int(time.time()) + expires_in,
    }
    return jwt.encode(payload, private_key, algorithm="RS256")
