import unittest

from tabbybot.character import format_character
from tabbybot.protocol import PeerCache, minimap_bounds


class CharacterFormatTests(unittest.TestCase):
    def test_splits_stats_equipment_and_inventory(self):
        player = {"name": "小幫手", "characterClass": "ARCHER", "level": 18, "gold": 120}
        equipment = {"items": [
            {"playerItemId": 11, "name": "木弓", "slotType": "WEAPON", "equipped": True, "qualityCode": "L1", "equipmentLevel": 1},
            {"playerItemId": 22, "name": "備用帽", "slotType": "HAT", "equipped": False, "qualityCode": "L2", "equipmentLevel": 5},
        ]}
        progression = {"experience": 40, "experienceNeeded": 100}
        self_state = {"health": 80, "maxHealth": 100, "dead": False, "attack": 12, "defense": 8}
        payload = format_character(player, equipment, progression, self_state)
        labels = {row["label"]: row["value"] for row in payload["stats"]}
        self.assertEqual(payload["name"], "小幫手")
        self.assertEqual(labels["職業"], "弓手")
        self.assertEqual(labels["等級"], "18")
        self.assertEqual(labels["生命"], "80/100")
        weapon = next(slot for slot in payload["equipment"] if slot["slot"] == "WEAPON")
        self.assertFalse(weapon["empty"])
        self.assertTrue(weapon["item"]["can_unequip"])
        self.assertEqual(payload["inventory"][0]["id"], 22)
        self.assertTrue(payload["inventory"][0]["can_equip"])


class MinimapHelperTests(unittest.TestCase):
    def test_bounds_fallback_keeps_units_apart(self):
        bounds = minimap_bounds(None, [(8479.0, 2363.0), (8356.0, 2278.0), (7989.0, 1727.0)])
        self.assertIsNotNone(bounds)
        self.assertLess(bounds["x0"], 7989.0)
        self.assertGreater(bounds["x1"], 8479.0)
        self.assertLess(bounds["y0"], 1727.0)
        self.assertGreater(bounds["y1"], 2363.0)

    def test_peer_cache_keeps_players_and_npcs(self):
        cache = PeerCache()
        cache.apply_snapshot({
            "peers": [{"id": "p1", "playerName": "路人", "x": 10, "y": 20, "mapId": 4}],
            "npcs": [{"id": "n1", "name": "任務NPC", "x": 30, "y": 40, "mapId": 4}],
        }, fallback_map=4, now=100.0)
        rows = {row["kind"]: row for row in cache.on_map(4)}
        self.assertEqual(rows["player"]["name"], "路人")
        self.assertEqual(rows["npc"]["name"], "任務NPC")


if __name__ == "__main__":
    unittest.main()
