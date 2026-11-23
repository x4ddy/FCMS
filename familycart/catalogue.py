import json
from pathlib import Path

from pydantic import BaseModel, Field, TypeAdapter

from shared.tokens import Identity

CATALOGUE_PATH = Path(__file__).resolve().parents[1] / "product" / "catalog.json"


class Product(BaseModel):
    sku: str
    name: str
    category: str
    price_fils: int = Field(gt=0)
    currency: str
    min_age: int = Field(ge=0, le=18)
    image_url: str

    def visible_to(self, caller: Identity) -> bool:
        return self.min_age == 0 or caller.is_adult


def load_catalogue() -> dict[str, Product]:
    products = TypeAdapter(list[Product]).validate_python(
        json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
    )
    by_sku = {product.sku: product for product in products}
    if len(by_sku) != len(products):
        raise ValueError("Catalogue contains duplicate SKUs")
    return by_sku
