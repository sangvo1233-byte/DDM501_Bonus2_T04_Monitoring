"""Authentication for tutorial operations exposed only on localhost by Compose."""
import hmac
import os

from fastapi import Header, HTTPException


def require_ops(x_ops_token: str = Header(default="")):
    if not hmac.compare_digest(x_ops_token, os.getenv("OPS_TOKEN", "tutorial-local-only")):
        raise HTTPException(status_code=403, detail="operations token required")
