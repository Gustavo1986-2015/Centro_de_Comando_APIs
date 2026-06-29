"""Auth compartido para todos los routers del dashboard y admin."""
import os
import secrets
import logging
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials

logger = logging.getLogger(__name__)
security = HTTPBasic()

def verify_dashboard_auth(credentials: HTTPBasicCredentials = Depends(security)):
    """Valida HTTP Basic Auth con secrets.compare_digest (anti timing attack)."""
    correct_user = os.getenv("DASHBOARD_USER", "admin")
    correct_pass = os.getenv("DASHBOARD_PASSWORD", "changeme")
    user_ok = secrets.compare_digest(credentials.username.encode(), correct_user.encode())
    pass_ok = secrets.compare_digest(credentials.password.encode(), correct_pass.encode())
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401,
            detail="Credenciales incorrectas",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials
