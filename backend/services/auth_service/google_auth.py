import os
from dataclasses import dataclass

from google.auth.exceptions import GoogleAuthError
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token


class InvalidGoogleCredential(ValueError):
    pass


@dataclass(frozen=True)
class GoogleIdentity:
    subject: str
    email: str
    display_name: str
    # Google does not provide age. Production verification therefore leaves this
    # false until a separate age-assurance flow is implemented.
    is_adult: bool = False


def google_client_id() -> str:
    client_id = os.environ.get("FAMILYCART_GOOGLE_CLIENT_ID", "").strip()
    if not client_id:
        raise RuntimeError("FAMILYCART_GOOGLE_CLIENT_ID is required")
    return client_id


def verify_google_credential(credential: str, client_id: str) -> GoogleIdentity:
    try:
        claims = id_token.verify_oauth2_token(
            credential,
            google_requests.Request(),
            client_id,
        )
    except (GoogleAuthError, ValueError):
        raise InvalidGoogleCredential("Google sign-in could not be verified") from None

    subject = str(claims.get("sub", "")).strip()
    email = str(claims.get("email", "")).strip().casefold()
    name = str(claims.get("name", "")).strip()
    if not subject or not email or claims.get("email_verified") is not True:
        raise InvalidGoogleCredential("Google sign-in could not be verified")
    if not email.endswith("@gmail.com"):
        raise InvalidGoogleCredential("Please use a Gmail account")
    return GoogleIdentity(subject=subject, email=email, display_name=name or email.split("@", 1)[0])
