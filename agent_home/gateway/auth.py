"""Authentication utilities for the Gateway API.

Handles JWT token verification for browser connections.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel

from shared.config import get_settings

security = HTTPBearer(auto_error=False)


class TokenData(BaseModel):
    """Data stored in access tokens."""

    sub: str  # User email
    exp: datetime
    iat: datetime
    jti: str  # Token ID for revocation


class TokenPayload(BaseModel):
    """Decoded token payload."""

    email: str
    token_id: str
    issued_at: datetime
    expires_at: datetime


def create_access_token(
    email: str,
    token_id: str,
    expires_delta: timedelta | None = None,
) -> str:
    """Create a JWT access token.

    Args:
        email: The user's email address
        token_id: Unique identifier for this token
        expires_delta: Optional custom expiration time

    Returns:
        Encoded JWT token
    """
    settings = get_settings()

    if expires_delta is None:
        expires_delta = timedelta(minutes=settings.access_token_expire_minutes)

    now = datetime.utcnow()
    expire = now + expires_delta

    to_encode: dict[str, Any] = {
        "sub": email,
        "exp": expire,
        "iat": now,
        "jti": token_id,
    }

    return jwt.encode(
        to_encode,
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def verify_token(token: str) -> TokenPayload:
    """Verify and decode a JWT token.

    Args:
        token: The JWT token to verify

    Returns:
        Decoded token payload

    Raises:
        ValueError: If the token is invalid or expired
    """
    settings = get_settings()

    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError as e:
        raise ValueError(f"Invalid token: {e}") from e

    # Extract fields
    email = payload.get("sub")
    token_id = payload.get("jti")
    exp = payload.get("exp")
    iat = payload.get("iat")

    if not all([email, token_id, exp, iat]):
        raise ValueError("Token missing required fields")

    # Check if email is allowed
    if settings.allowed_emails and email not in settings.allowed_emails:
        raise ValueError(f"Email {email} not in allowed list")

    return TokenPayload(
        email=email,
        token_id=token_id,
        issued_at=datetime.fromtimestamp(iat),
        expires_at=datetime.fromtimestamp(exp),
    )


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Security(security),
) -> TokenPayload:
    """FastAPI dependency to get the current authenticated user.

    Args:
        credentials: The HTTP Authorization header

    Returns:
        The authenticated user's token payload

    Raises:
        HTTPException: If authentication fails
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        return verify_token(credentials.credentials)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


def verify_websocket_token(token: str) -> TokenPayload:
    """Verify a token for WebSocket connections.

    Same as verify_token but named explicitly for WebSocket use.

    Args:
        token: The JWT token

    Returns:
        Decoded token payload

    Raises:
        ValueError: If verification fails
    """
    return verify_token(token)
