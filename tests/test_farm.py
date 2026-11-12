import unittest

from tabbybot.farm import (
    DEFAULT_FARM_MAP,
    MapLevelStore,
    choose_farm_map,
    choose_farm_map_with_reason,
    is_farmable_map,
    level_sort_key,
    remember_monster_level,
)
from tabbybot.constants import SPECIAL_ENTRY_MAPS


class FarmMapTests(unittest.TestCase):
    def test_never_selects_special_or_town(self):
        catalog = {1: [30, 30], 5: [30, 30], 7: [30, 31], 4: [12, 13]}
        chosen = choose_farm_map(30, catalog)
        self.assertTrue(is_farmable_map(chosen))
        self.assertNotIn(chosen, SPECIAL_ENTRY_MAPS)
        self.assertNotEqual(chosen, 1)

    def test_picks_closest_median_to_target(self):
        catalog = {2: [8, 9, 10], 4: [28, 30, 32], 8: [40, 42]}
        self.assertEqual(choose_farm_map(30, catalog), 4)
        self.assertEqual(choose_farm_map(9, catalog), 2)
        self.assertEqual(choose_farm_map(41, catalog), 8)

    def test_fallback_without_observations(self):
        self.assertEqual(choose_farm_map(30, {}), DEFAULT_FARM_MAP)
        self.assertEqual(choose_farm_map(30, {}, current_map=4), 4)
        self.assertEqual(choose_farm_map(30, {}, current_map=1), DEFAULT_FARM_MAP)
        # 40+ with nothing known: go one tier deeper instead of map 2
        self.assertIn(choose_farm_map(45, {}, current_map=2), {3, 8, 9, 10})

    def test_stays_on_current_map_when_it_has_target_level(self):
        catalog = {6: [10, 20, 30], 4: [28, 30, 32]}
        chosen, reason = choose_farm_map_with_reason(30, catalog, current_map=6)
        self.assertEqual(chosen, 6)
        self.assertIn("留在這裡", reason)

    def test_leaves_map_when_known_maps_too_weak(self):
        catalog = {2: [10, 20, 30], 6: [10, 20, 30]}
        chosen, reason = choose_farm_map_with_reason(50, catalog, current_map=2)
        self.assertIn(chosen, {3, 8, 9, 10})
        self.assertIn("Lv30", reason)
        catalog[8] = [40, 50, 52]
        self.assertEqual(choose_farm_map(50, catalog, current_map=2), 8)

    def test_level_store_persists_one_sample_per_encounter(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "map_levels.json"
            store = MapLevelStore(path)
            store.observe("a", 6, 30)
            store.observe("a", 6, 30)
            store.observe("b", 6, 20)
            store.observe("c", 5, 50)  # special map ignored
            store.save(100.0, force=True)
            again = MapLevelStore(path)
            self.assertEqual(sorted(again.catalog[6]), [20, 30])
            self.assertNotIn(5, again.catalog)
            row = [r for r in again.table() if r["map_id"] == 6][0]
            self.assertEqual((row["min"], row["max"]), (20, 30))

    def test_remember_ignores_special_maps(self):
        catalog = {}
        remember_monster_level(catalog, 5, 30)
        remember_monster_level(catalog, 4, 29)
        remember_monster_level(catalog, 4, None)
        self.assertNotIn(5, catalog)
        self.assertEqual(catalog[4], [29])

    def test_level_sort_prefers_near_target(self):
        near = level_sort_key(30, 30, 80)
        mid = level_sort_key(28, 30, 10)
        unknown = level_sort_key(None, 30, 5)
        self.assertLess(near, mid)
        self.assertLess(mid, unknown)


if __name__ == "__main__":
    unittest.main()


class RecordedLevelsTests(unittest.TestCase):
    def test_lv30_not_recorded_anywhere_explores_unrecorded_map(self):
        from tabbybot.farm import choose_farm_map_with_reason
        catalog = {2: [10] * 20 + [20] * 8, 6: [10] * 50, 8: [40] * 30, 10: [40] * 100 + [50] * 10}
        mid, reason = choose_farm_map_with_reason(30, catalog, 10)
        self.assertIn(mid, {3, 4, 9})
        self.assertIn("沒有 Lv30", reason)
        self.assertIn("冰封洞穴 Lv40–50", reason)

    def test_stays_on_ice_cave_for_lv40(self):
        from tabbybot.farm import choose_farm_map_with_reason
        catalog = {2: [10] * 20, 8: [40] * 30, 10: [40] * 100 + [50] * 10}
        self.assertEqual(choose_farm_map_with_reason(40, catalog, 10)[0], 10)

    def test_spots_recorded_and_persisted(self):
        import tempfile
        from pathlib import Path
        from tabbybot.farm import MapLevelStore
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "map_levels.json"
            store = MapLevelStore(path)
            store.observe("a", 10, 40, 1000, 1000)
            store.observe("b", 10, 41, 1100, 1050)
            store.observe("c", 10, 40, 3000, 500)
            store.observe("d", 10, 50, 3000, 500)
            store.save(0.0, force=True)
            again = MapLevelStore(path)
            spots = again.spots_for(10, 40)
            self.assertEqual(len(spots), 2)
            self.assertEqual(max(s[2] for s in spots), 2)
            self.assertEqual(len(again.spots_for(10, 50)), 1)
