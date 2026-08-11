"""
Google sign-in and session cookies.

The browser runs Google Identity Services, which hands it a signed ID token
("credential"). It POSTs that to /auth/google; we verify it against Google's keys,
upsert the user, and reply with an HttpOnly cookie holding a signed session blob.
Every subsequent request carries the cookie, and current_user turns it back into a
User row — or a 401.
"""

import os
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, Response
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import get_db
from models import User

COOKIE_NAME = "overhand_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")

_DEV_SECRET = "dev-insecure-secret"
SESSION_SECRET = os.environ.get("SESSION_SECRET", _DEV_SECRET)

# A known secret means anyone can mint a session for any account. Convenient
# locally, fatal in production — so refuse to start rather than run forgeable.
if SESSION_SECRET == _DEV_SECRET and os.environ.get("APP_ENV", "dev") != "dev":
    raise RuntimeError(
        "SESSION_SECRET must be set when APP_ENV is not 'dev'. "
        'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(32))"'
    )

_serializer = URLSafeTimedSerializer(SESSION_SECRET, salt="overhand-session")

_UNAUTHENTICATED = HTTPException(status_code=401, detail="Sign in to continue")


def _is_dev() -> bool:
    return os.environ.get("APP_ENV", "dev") == "dev"


def verify_google_token(credential: str) -> dict:
    """Validate a Google ID token and return its claims. Raises HTTPException(401)."""
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="GOOGLE_CLIENT_ID is not configured")

    try:
        claims = google_id_token.verify_oauth2_token(
            credential, google_requests.Request(), GOOGLE_CLIENT_ID
        )
    except ValueError as exc:  # bad signature, wrong audience, expired
        raise HTTPException(status_code=401, detail=f"Google sign-in failed: {exc}") from exc

    if not claims.get("email_verified"):
        raise HTTPException(status_code=401, detail="That Google account has no verified email")

    return claims


def upsert_user(db: Session, claims: dict) -> User:
    """Find the user by Google subject id, creating or refreshing the row."""
    sub = claims["sub"]
    user = db.scalar(select(User).where(User.google_sub == sub))

    if user is None:
        # An existing row can hold this email under a different subject — a deleted
        # and recreated Google account, or a Workspace transfer. Inserting would
        # trip the unique index, so re-point the old row instead. Safe because
        # verify_google_token has already required a Google-verified email.
        user = db.scalar(select(User).where(User.email == claims["email"]))
        if user is not None:
            user.google_sub = sub

    if user is None:
        user = User(google_sub=sub, email=claims["email"])
        db.add(user)

    # Email, name and avatar can all change on Google's side; sub cannot.
    user.email = claims["email"]
    user.name = claims.get("name")
    user.picture = claims.get("picture")
    user.last_seen_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(user)
    return user


def issue_session(response: Response, user: User) -> None:
    """Attach the signed session cookie to a response."""
    token = _serializer.dumps({"uid": user.id, "sub": user.google_sub})
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=not _is_dev(),
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """The dependency every protected route depends on. Fails closed."""
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        raise _UNAUTHENTICATED

    try:
        data = _serializer.loads(raw, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired) as exc:
        raise _UNAUTHENTICATED from exc

    user = db.get(User, data.get("uid"))
    # The subject check means a reused row id cannot resurrect someone else's session.
    if user is None or user.google_sub != data.get("sub"):
        raise _UNAUTHENTICATED

    return user


def public_user(user: User, page_limit: int) -> dict:
    """The user shape the frontend consumes."""
    return {
        "email": user.email,
        "name": user.name,
        "picture": user.picture,
        "pages_used": user.pages_used,
        "page_limit": page_limit,
    }
