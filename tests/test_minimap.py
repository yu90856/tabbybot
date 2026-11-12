import unittest

from tabbybot.constants import MAP_NAMES
from test_overlevel_efficiency import ctl, mon


class MinimapPayloadTests(unittest.TestCase):
    def test_portals_listed_with_destination_names(self):
        c = ctl()
        portals = c.map_payload()["portals"]
        self.assertTrue(portals)
        dests = {p["dest"]: p["name"] for p in portals}
        self.assertIn(1, dests)
        self.assertEqual(dests[1], MAP_NAMES[1])

    def test_target_line_and_kite_ring_data(self):
        c = ctl()
        mon(c, "enc-a", 300, 30)
        self.assertIsNone(c.map_payload()["target"])
        c.chase_target = "enc-a"
        tgt = c.map_payload()["target"]
        self.assertEqual(tgt["x"], 300)
        self.assertEqual(tgt["reach"], 295.0)
        self.assertEqual(tgt["kite_min"], 200.0)
        # 風箏 on: chasing an in-range target already counts as kiting.
        self.assertTrue(tgt["kiting"])
        c.cluster_kite = False
        self.assertFalse(c.map_payload()["target"]["kiting"])
        c.cluster_kite = True
        self.assertTrue(c.map_payload()["target"]["kiting"])

    def test_missing_target_monster_is_ignored(self):
        c = ctl()
        c.chase_target = "gone"
        self.assertIsNone(c.map_payload()["target"])


if __name__ == "__main__":
    unittest.main()
