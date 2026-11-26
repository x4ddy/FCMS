from fastapi import Response
from pydantic import BaseModel

from backend.shared.tokens import is_child_account
from .cookies import set_refresh_cookie
from .service import SessionTokens


class SignedInUser(BaseModel):
    id: str
    display_name: str
    is_adult: bool
    age_setup_required: bool
    is_child_account: bool = False
    email: str | None = None


class RefreshResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: SignedInUser


def signed_in_response(response: Response, tokens: SessionTokens) -> RefreshResponse:
    set_refresh_cookie(response, tokens.refresh_token)
    response.headers["Cache-Control"] = "no-store"
    return RefreshResponse(
        access_token=tokens.access_token,
        expires_in=tokens.expires_in,
        user=SignedInUser(id=tokens.user_id, display_name=tokens.display_name, is_adult=tokens.is_adult,
                          age_setup_required=tokens.age_setup_required, is_child_account=is_child_account(tokens.user_id),
                          email=tokens.email),
    )
