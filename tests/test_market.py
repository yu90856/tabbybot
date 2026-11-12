import unittest

from tabbybot.config import MarketConfig
from tabbybot.market import filter_listings, new_matches


def product(**overrides):
    row = {
        "id": 1,
        "price": 12,
        "qualityCode": "L3",
        "state": "ACTIVE",
        "market": True,
        "name": "Iron Sword",
    }
    row.update(overrides)
    return row


class MarketFilterTests(unittest.TestCase):
    def setUp(self):
        self.config = MarketConfig(qualities=["L3", "L4"], min_price=10, max_price=50, name_contains="")

    def test_quality_and_price_window(self):
        rows = [
            product(id=1, price=10, qualityCode="L3"),   # min inclusive
            product(id=2, price=49.9, qualityCode="L4"),
            product(id=3, price=50, qualityCode="L3"),   # max exclusive
            product(id=4, price=12, qualityCode="L2"),
            product(id=5, price=9.9, qualityCode="L3"),
        ]
        matches = filter_listings(rows, self.config)
        self.assertEqual([item.product_id for item in matches], [1, 2])

    def test_star_quality(self):
        config = MarketConfig(qualities=["*"], min_price=0, max_price=100)
        matches = filter_listings([product(qualityCode="L1"), product(qualityCode="L4")], config)
        self.assertEqual(len(matches), 2)

    def test_reserved_state_and_market_flags(self):
        rows = [
            product(id=1, reserved=True),
            product(id=2, state="SOLD"),
            product(id=3, market=False),
            product(id=4),
        ]
        matches = filter_listings(rows, self.config)
        self.assertEqual([item.product_id for item in matches], [4])

    def test_name_substring(self):
        config = MarketConfig(qualities=["*"], min_price=0, max_price=100, name_contains="sword")
        rows = [product(id=1, name="Iron Sword"), product(id=2, name="Wooden Shield")]
        matches = filter_listings(rows, config)
        self.assertEqual([item.product_id for item in matches], [1])

    def test_invalid_rows_skipped(self):
        rows = [
            "nope",
            {"id": "x", "price": 12, "qualityCode": "L3", "state": "ACTIVE"},
            {"id": 0, "price": 12, "qualityCode": "L3", "state": "ACTIVE"},
            {"id": True, "price": 12, "qualityCode": "L3", "state": "ACTIVE"},
            product(id=8),
        ]
        matches = filter_listings(rows, self.config)
        self.assertEqual([item.product_id for item in matches], [8])

    def test_new_matches_dedupes_id_and_price(self):
        first = filter_listings([product(id=1, price=12), product(id=2, price=20)], self.config)
        seen = {item.dedupe_key for item in first}
        second = filter_listings([
            product(id=1, price=12),
            product(id=1, price=15),
            product(id=2, price=20),
        ], self.config)
        fresh = new_matches(seen, second)
        self.assertEqual([item.dedupe_key for item in fresh], [(1, 15)])


if __name__ == "__main__":
    unittest.main()
