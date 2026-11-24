from math import asin, cos, isclose, radians, sin, sqrt

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from backend.shared.api import API_PREFIX
from backend.shared.maps import (
    Coordinates, CoverageResult, Deliverable, OutsideDeliveryArea, address_validation_error,
)

EARTH_RADIUS_KM = 6371.0088
DELIVERY_RADIUS_KM = 50


class Store(Coordinates):
    model_config = ConfigDict(frozen=True)

    store_id: str
    name: str
    radius_km: int = DELIVERY_RADIUS_KM


STORES = (
    Store(store_id="uae-abu-dhabi", name="Abu Dhabi", latitude=24.4539, longitude=54.3773),
    Store(store_id="uae-dubai", name="Dubai", latitude=25.2048, longitude=55.2708),
    Store(store_id="uae-al-ain", name="Al Ain", latitude=24.2075, longitude=55.7447),
    Store(store_id="uae-ras-al-khaimah", name="Ras Al Khaimah", latitude=25.7895, longitude=55.9432),
)


def distance_km(point: Coordinates, store: Store) -> float:
    lat1, lat2 = radians(point.latitude), radians(store.latitude)
    dlat = radians(store.latitude - point.latitude)
    dlon = radians(store.longitude - point.longitude)
    haversine = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(min(1.0, haversine)))


app = FastAPI(title="FamilyCart Maps")


@app.exception_handler(RequestValidationError)
async def invalid_coordinates(_: Request, error: RequestValidationError) -> JSONResponse:
    return address_validation_error(error)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get(f"{API_PREFIX}/maps/stores")
def list_stores() -> tuple[Store, ...]:
    return STORES


@app.post(f"{API_PREFIX}/maps/resolve")
def resolve(point: Coordinates) -> CoverageResult:
    eligible = []
    for store in STORES:
        distance = distance_km(point, store)
        # Allow one nanometer for floating-point error at the inclusive boundary.
        if distance <= store.radius_km or isclose(distance, store.radius_km, rel_tol=0, abs_tol=1e-12):
            eligible.append((distance, store.store_id, store))
    if not eligible:
        return OutsideDeliveryArea()
    distance, _, store = min(eligible)
    return Deliverable(store_id=store.store_id, store_name=store.name, distance_km=round(distance, 2))
