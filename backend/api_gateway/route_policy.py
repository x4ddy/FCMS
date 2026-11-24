"""How fresh a login check each route needs.

Anything not listed here is SENSITIVE, so a new route that nobody classified
is checked more strictly rather than less. This decides authentication
freshness only; permission checks stay in the service that owns the data.
"""

import re
from enum import Enum


class Level(Enum):
    NORMAL = "normal"        # may reuse a result from this Gateway's memory
    SENSITIVE = "sensitive"  # fresh Redis check, database if Redis has no answer
    CRITICAL = "critical"    # always the database


# One path segment. Deliberately strict: anything unusual falls through to SENSITIVE.
S = r"[A-Za-z0-9._~-]+"
H = rf"households/{S}"
CART = rf"{H}/carts/{S}"
LIST = rf"{H}/lists/{S}"

_NORMAL = {
    "GET": (
        "products", rf"products/{S}", "households", "personal-cart", "addresses", "orders", rf"orders/{S}",
        "maps/stores", rf"{H}/(?:carts|capabilities|members|addresses|lists)", CART, rf"{CART}/grants", LIST,
    ),
    "POST": (
        "maps/resolve", rf"addresses/{S}/resolve-store", rf"{H}/addresses/{S}/resolve-store",
        rf"{H}/lists", rf"{LIST}/items", rf"{LIST}/items/(?:bulk|clear-checked)", rf"{LIST}/move-to-cart",
    ),
    "PUT": (rf"{CART}/items/{S}/contributions/me",),
    "PATCH": (
        rf"personal-cart/items/{S}", rf"{CART}/items/{S}", rf"{CART}/items/{S}/contributions/me",
        LIST, rf"{LIST}/items/{S}",
    ),
    "DELETE": (rf"{CART}/items/{S}", rf"{CART}/items/{S}/contributions/{S}", rf"{LIST}/items/{S}"),
}
_CRITICAL = {
    "POST": ("checkout/orders", "auth/age"),
    "PUT": (rf"{H}/members/{S}/primary",),
    "DELETE": (rf"{H}/members/{S}",),
}


def _compile(routes: dict[str, tuple[str, ...]]) -> dict[str, re.Pattern[str]]:
    return {method: re.compile("|".join(f"(?:{route})" for route in paths)) for method, paths in routes.items()}


_NORMAL_PATTERNS = _compile(_NORMAL)
_CRITICAL_PATTERNS = _compile(_CRITICAL)


def classify(method: str, path: str) -> Level:
    critical = _CRITICAL_PATTERNS.get(method)
    if critical and critical.fullmatch(path):
        return Level.CRITICAL
    normal = _NORMAL_PATTERNS.get(method)
    if normal and normal.fullmatch(path):
        return Level.NORMAL
    return Level.SENSITIVE


def is_ambiguous(path: str) -> bool:
    """True if the forwarded URL could mean a different route than the one classified here.

    The path has already been percent-decoded, so "#" or "?" would cut it short
    and "." or ".." segments would be collapsed when the upstream URL is built.
    """
    return (
        any(character in "#?\\%" or ord(character) < 33 or ord(character) == 127 for character in path)
        or any(segment in {".", ".."} for segment in path.split("/"))
    )
