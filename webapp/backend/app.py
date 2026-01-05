"""Webapp backend for Agent Home.

Handles:
- Email/password authentication
- Minting access tokens for Agent Home connection
- Ensuring Agent Home is running
- Serving the frontend
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jose import jwt
from pydantic import BaseModel, EmailStr
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class WebappSettings(BaseSettings):
    """Webapp settings."""

    model_config = SettingsConfigDict(
        env_prefix="WEBAPP_",
        env_file=".env",
    )

    # Admin user credentials (set these in .env)
    admin_email: str = "admin@example.com"
    admin_password: str = "changeme"  # Change this!

    # JWT settings
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    session_expire_hours: int = 24

    # Cookie settings
    cookie_secure: bool = False  # Set True in production with HTTPS

    # Agent Home
    agent_home_modal_app: str = "agent-home-orchestrator"

    # Frontend
    frontend_url: str = "http://localhost:3000"


settings = WebappSettings()

# Generate JWT secret if not set
if not settings.jwt_secret:
    settings.jwt_secret = secrets.token_hex(32)
    logger.warning("JWT_SECRET not set, using randomly generated secret (sessions won't persist across restarts)")

app = FastAPI(
    title="Agent Home Webapp",
    description="Web interface for Agent Home Orchestrator",
)

# CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url, "http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Session management
SESSION_COOKIE = "agent_home_session"


def hash_password(password: str) -> str:
    """Hash a password using SHA-256."""
    return hashlib.sha256(password.encode()).hexdigest()


class SessionData(BaseModel):
    """Session data stored in JWT."""

    email: str
    name: str
    exp: datetime


class LoginRequest(BaseModel):
    """Login request body."""

    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    """Login response."""

    success: bool
    message: str
    user: dict[str, Any] | None = None


def create_session_token(email: str, name: str) -> str:
    """Create a session JWT token.

    Args:
        email: User email
        name: User name

    Returns:
        JWT token
    """
    expire = datetime.utcnow() + timedelta(hours=settings.session_expire_hours)
    payload = {
        "email": email,
        "name": name,
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


@app.post("/auth/login")
async def login(request: LoginRequest) -> JSONResponse:
    """Login with email and password.

    Args:
        request: Login request with email and password

    Returns:
        JSON response with session cookie
    """
    # Check credentials against admin user
    if (
        request.email == settings.admin_email
        and request.password == settings.admin_password
    ):
        # Create session token
        session_token = create_session_token(
            email=request.email,
            name=request.email.split("@")[0],  # Use part before @ as name
        )

        # Create response with cookie
        response = JSONResponse(
            content={
                "success": True,
                "message": "Login successful",
                "user": {
                    "email": request.email,
                    "name": request.email.split("@")[0],
                },
            }
        )
        response.set_cookie(
            key=SESSION_COOKIE,
            value=session_token,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            max_age=settings.session_expire_hours * 3600,
        )

        return response

    # Invalid credentials
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid email or password",
    )


@app.post("/auth/logout")
async def logout() -> JSONResponse:
    """Log out by clearing session cookie."""
    response = JSONResponse(content={"success": True, "message": "Logged out"})
    response.delete_cookie(key=SESSION_COOKIE)
    return response


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

# If frontend build exists, serve it (Vite outputs to 'dist')
frontend_build = Path(__file__).parent.parent / "frontend" / "dist"
if frontend_build.exists():
    # Serve static assets
    assets_dir = frontend_build / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

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
