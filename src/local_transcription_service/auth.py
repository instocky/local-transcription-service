"""Auth dependency for the HTTP API.

Single shared token via the `X-Auth-Token` header (HLD-001 §14).
`secrets.compare_digest` is used so response time does not leak
the token length. The `/health` and `/ready` endpoints do NOT
include this dependency and are reachable without auth.
"""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, status


def require_token(request: Request) -> None:
    """Validate the `X-Auth-Token` header against the configured token.

    Reads the token from `request.app.state.settings` (set during
    `create_app`). Returns 401 with `WWW-Authenticate: Token` on
    missing or mismatched header.
    """
    settings = request.app.state.settings
    presented = request.headers.get("X-Auth-Token")
    if presented is None or not secrets.compare_digest(presented, settings.auth_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "Invalid or missing X-Auth-Token"},
            headers={"WWW-Authenticate": "Token"},
        )
