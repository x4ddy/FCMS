import os
from typing import Annotated, Literal

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

Latitude = Annotated[float, Field(strict=True, allow_inf_nan=False, ge=-90, le=90)]
Longitude = Annotated[float, Field(strict=True, allow_inf_nan=False, ge=-180, le=180)]
MAPS_UNAVAILABLE = "MAPS_UNAVAILABLE"


class Coordinates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latitude: Latitude
    longitude: Longitude


class Deliverable(BaseModel):
    status: Literal["DELIVERABLE"] = "DELIVERABLE"
    store_id: str
    store_name: str
    distance_km: Annotated[float, Field(ge=0, allow_inf_nan=False)]


class OutsideDeliveryArea(BaseModel):
    status: Literal["OUTSIDE_DELIVERY_AREA"] = "OUTSIDE_DELIVERY_AREA"
    store_id: None = None
    store_name: None = None
    distance_km: None = None


CoverageResult = Annotated[Deliverable | OutsideDeliveryArea, Field(discriminator="status")]
coverage_result = TypeAdapter(CoverageResult)


def maps_service_url() -> str:
    return os.environ.get("FAMILYCART_MAPS_URL", "http://127.0.0.1:8004").rstrip("/")


def address_validation_error(error: RequestValidationError) -> JSONResponse:
    # Never echo address text or non-JSON values such as NaN in these errors.
    details = [{key: issue[key] for key in ("loc", "msg", "type")} for issue in error.errors()]
    return JSONResponse(status_code=422, content={"detail": details})
