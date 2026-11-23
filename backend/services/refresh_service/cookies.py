import os

from fastapi import Response


REFRESH_COOKIE = "familycart_refresh"


def _secure_cookie() -> bool:
    return os.environ.get("FAMILYCART_COOKIE_SECURE", "").casefold() in {"1", "true", "yes"}


def set_refresh_cookie(response: Response, token: str, max_age: int = 30 * 24 * 60 * 60) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        secure=_secure_cookie(),
        samesite="lax",
        path="/",
    )


def clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        REFRESH_COOKIE,
        httponly=True,
        secure=_secure_cookie(),
        samesite="lax",
        path="/",
    )
