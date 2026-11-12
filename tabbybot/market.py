"""Shop listing filters. No purchase side effects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tabbybot.config import MarketConfig
from tabbybot.protocol import product_display_name


@dataclass(frozen=True)
class ListingMatch:
    product_id: int
    price: float
    quality: str
    name: str
    raw: dict[str, Any]

    @property
    def dedupe_key(self) -> tuple[int, float]:
        return (self.product_id, self.price)


def iter_valid_products(products: Any) -> list[dict[str, Any]]:
    if not isinstance(products, list):
        return []
    rows = []
    for item in products:
        if isinstance(item, dict):
            rows.append(item)
    return rows


def filter_listings(products: Any, config: MarketConfig) -> list[ListingMatch]:
    matches: list[ListingMatch] = []
    needle = (config.name_contains or "").strip().lower()
    for product in iter_valid_products(products):
        try:
            product_id = product.get("id")
            if isinstance(product_id, bool) or int(product_id) <= 0:
                continue
            price = float(product.get("price"))
            quality = str(product.get("qualityCode", ""))
            quality_ok = "*" in config.qualities or quality in config.qualities
            if not quality_ok:
                continue
            if product.get("state") != "ACTIVE":
                continue
            if product.get("market") is False:
                continue
            if product.get("reserved"):
                continue
            if not (config.min_price <= price < config.max_price):
                continue
            name = product_display_name(product)
            if needle and needle not in name.lower():
                continue
            matches.append(ListingMatch(
                product_id=int(product_id),
                price=price,
                quality=quality,
                name=name,
                raw=product,
            ))
        except (TypeError, ValueError, OverflowError):
            continue
    matches.sort(key=lambda item: (item.price, -item.product_id))
    return matches


def new_matches(previous: set[tuple[int, float]], current: list[ListingMatch]) -> list[ListingMatch]:
    return [item for item in current if item.dedupe_key not in previous]
