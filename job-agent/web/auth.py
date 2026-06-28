"""Session-based authentication helpers for the web app."""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import bcrypt
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from db.database import Database


logger = logging.getLogger(__name__)

SESSION_USER_ID_KEY = "user_id"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

PUBLIC_PATHS = frozenset({"/", "/login", "/signup", "/register", "/logout", "/about"})
PUBLIC_PREFIXES = ("/static/",)


def get_session_secret() -> str:
    """Return SESSION_SECRET from env, with a dev fallback and warning."""
    secret = os.environ.get("SESSION_SECRET", "").strip()
    if secret:
        return secret
    logger.warning(
        "SESSION_SECRET is not set in .env — using an insecure dev default. "
        "Set SESSION_SECRET to a long random string before deploying."
    )
    return "dev-insecure-change-me-in-production"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email))


def validate_password(password: str) -> str | None:
    if len(password) < 8:
        return "Password must be at least 8 characters."
    return None


def get_current_user(request: Request) -> dict[str, Any] | None:
    user_id = request.session.get(SESSION_USER_ID_KEY)
    if not user_id:
        return None
    user = Database().get_user_by_id(int(user_id))
    if not user:
        request.session.pop(SESSION_USER_ID_KEY, None)
        return None
    return user


def login_user(request: Request, user: dict[str, Any]) -> None:
    request.session[SESSION_USER_ID_KEY] = int(user["id"])


def logout_user(request: Request) -> None:
    request.session.clear()


def is_public_path(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


class AuthMiddleware(BaseHTTPMiddleware):
    """Redirect unauthenticated users to /login; protect API with 401."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path

        if is_public_path(path):
            if path in {"/login", "/signup", "/register"} and get_current_user(request):
                return RedirectResponse(url="/home", status_code=303)
            return await call_next(request)

        if get_current_user(request):
            return await call_next(request)

        if path.startswith("/api/"):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        next_url = path
        if request.url.query:
            next_url = f"{path}?{request.url.query}"
        return RedirectResponse(url=f"/login?next={next_url}", status_code=303)
