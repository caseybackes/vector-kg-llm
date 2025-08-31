# services/agent-gateway/gateway/auth.py
# FastAPI dependency to require an API key for non-health endpoints.

import os
from fastapi import Header, HTTPException, status

_GATEWAY_KEY = os.getenv("GATEWAY_API_KEY", "")

def require_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    # If key not set in env, allow all (home/dev); if set, enforce.
    if _GATEWAY_KEY and x_api_key != _GATEWAY_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key")
