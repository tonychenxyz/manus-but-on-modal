"""Webapp backend for Agent Home.

Handles:
- Google OAuth authentication
- Minting access tokens for Agent Home connection
- Ensuring Agent Home is running
- Serving the frontend
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jose import jwt
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class WebappSettings(BaseSettings):
    """Webapp settings."""

    model_config = SettingsConfigDict(
        env_prefix="WEBAPP_",
        env_file=".env",
    )

    # Google OAuth
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/auth/callback"

    # Allowed users (comma-separated emails)
    allowed_emails: str = ""

    # JWT settings
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    session_expire_hours: int = 24

    # Agent Home
    agent_home_modal_app: str = "agent-home-orchestrator"

    # Frontend
    frontend_url: str = "http://localhost:3000"

    @property
    def allowed_email_list(self) -> list[str]:
        """Get list of allowed emails."""
        if not self.allowed_emails:
            return []
        return [e.strip() for e in self.allowed_emails.split(",")]


settings = WebappSettings()

app = FastAPI(
    title="Agent Home Webapp",
    description="Web interface for Agent Home Orchestrator",
)

# CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url, "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Session management
SESSION_COOKIE = "agent_home_session"


class SessionData(BaseModel):
    """Session data stored in JWT."""

    email: str
    name: str
    picture: str | None = None
    exp: datetime


def create_session_token(email: str, name: str, picture: str | None = None) -> str:
    """Create a session JWT token.

    Args:
        email: User email
        name: User name
        picture: Profile picture URL

    Returns:
        JWT token
    """
    expire = datetime.utcnow() + timedelta(hours=settings.session_expire_hours)
    payload = {
        "email": email,
        "name": name,
        "picture": picture,
        "exp": expire,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def verify_session_token(token: str) -> SessionData | None:
    """Verify a session token.

    Args:
        token: JWT token

    Returns:
        Session data or None if invalid
    """
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
        return SessionData(**payload)
    except Exception:
        return None


def get_current_user(request: Request) -> SessionData | None:
    """Get current user from session cookie.

    Args:
        request: FastAPI request

    Returns:
        Session data or None
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return verify_session_token(token)


def require_auth(request: Request) -> SessionData:
    """Require authentication.

    Args:
        request: FastAPI request

    Returns:
        Session data

    Raises:
        HTTPException: If not authenticated
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return user


# ============================================================================
# Auth Routes
# ============================================================================


@app.get("/auth/login")
async def login() -> RedirectResponse:
    """Redirect to Google OAuth."""
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "consent",
    }
    url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    return RedirectResponse(url)


@app.get("/auth/callback")
async def auth_callback(code: str, response: Response) -> RedirectResponse:
    """Handle Google OAuth callback.

    Args:
        code: Authorization code from Google
        response: FastAPI response

    Returns:
        Redirect to frontend
    """
    # Exchange code for tokens
    async with httpx.AsyncClient() as client:
        token_response = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": settings.google_redirect_uri,
            },
        )

        if token_response.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to exchange code for token",
            )

        tokens = token_response.json()

        # Get user info
        userinfo_response = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )

        if userinfo_response.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to get user info",
            )

        userinfo = userinfo_response.json()

    email = userinfo.get("email")
    name = userinfo.get("name", email)
    picture = userinfo.get("picture")

    # Check if email is allowed
    if settings.allowed_email_list and email not in settings.allowed_email_list:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Email {email} is not authorized",
        )

    # Create session
    session_token = create_session_token(email, name, picture)

    # Redirect to frontend with session cookie
    redirect = RedirectResponse(url=settings.frontend_url, status_code=302)
    redirect.set_cookie(
        key=SESSION_COOKIE,
        value=session_token,
        httponly=True,
        secure=True,  # Set to False for local development
        samesite="lax",
        max_age=settings.session_expire_hours * 3600,
    )

    return redirect


@app.get("/auth/logout")
async def logout() -> RedirectResponse:
    """Log out by clearing session cookie."""
    redirect = RedirectResponse(url=settings.frontend_url, status_code=302)
    redirect.delete_cookie(key=SESSION_COOKIE)
    return redirect


@app.get("/auth/me")
async def get_me(request: Request) -> dict[str, Any]:
    """Get current user info.

    Args:
        request: FastAPI request

    Returns:
        User info
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    return {
        "email": user.email,
        "name": user.name,
        "picture": user.picture,
    }


# ============================================================================
# Agent Home Connection Routes
# ============================================================================


class ConnectResponse(BaseModel):
    """Response for connect endpoint."""

    agent_home_url: str
    token: str
    expires_in_seconds: int


@app.post("/api/connect")
async def connect(request: Request) -> ConnectResponse:
    """Get connection credentials for Agent Home.

    This endpoint:
    1. Ensures Agent Home is running
    2. Mints an access token for the user
    3. Returns the Agent Home URL and token

    Args:
        request: FastAPI request

    Returns:
        Connection credentials
    """
    user = require_auth(request)

    try:
        import modal

        # Ensure Agent Home is running
        ensure_fn = modal.Function.lookup(
            settings.agent_home_modal_app,
            "ensure_agent_home_running",
        )
        status_result = ensure_fn.remote()

        if status_result.get("status") == "error":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Agent Home not available: {status_result.get('message')}",
            )

        # Mint access token
        mint_fn = modal.Function.lookup(
            settings.agent_home_modal_app,
            "mint_access_token",
        )
        token_result = mint_fn.remote(user.email)

        # Get Agent Home URL from Modal
        cls = modal.Cls.lookup(settings.agent_home_modal_app, "AgentHomeSandbox")
        agent_home_url = cls.web_url

        return ConnectResponse(
            agent_home_url=agent_home_url,
            token=token_result["token"],
            expires_in_seconds=token_result["expires_in_seconds"],
        )

    except modal.exception.NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent Home Modal app not deployed",
        )
    except Exception as e:
        logger.error(f"Failed to connect to Agent Home: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to connect to Agent Home: {e}",
        )


@app.get("/api/status")
async def get_agent_home_status(request: Request) -> dict[str, Any]:
    """Get Agent Home status.

    Args:
        request: FastAPI request

    Returns:
        Status info
    """
    user = require_auth(request)

    try:
        import modal

        ensure_fn = modal.Function.lookup(
            settings.agent_home_modal_app,
            "ensure_agent_home_running",
        )
        result = ensure_fn.remote()
        return result

    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
        }


# ============================================================================
# Health Check
# ============================================================================


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "healthy"}


# ============================================================================
# Frontend Serving (for production)
# ============================================================================

# If frontend build exists, serve it
frontend_build = Path(__file__).parent.parent / "frontend" / "build"
if frontend_build.exists():
    app.mount("/static", StaticFiles(directory=frontend_build / "static"), name="static")

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str) -> HTMLResponse:
        """Serve frontend for all other routes."""
        index_file = frontend_build / "index.html"
        if index_file.exists():
            return HTMLResponse(content=index_file.read_text())
        raise HTTPException(status_code=404, detail="Not found")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
