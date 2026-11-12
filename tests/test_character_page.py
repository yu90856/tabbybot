import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from tabbybot.character import character_page
from tabbybot.config import load_config
from tabbybot.gui import run_gui
from tabbybot.keychain import MemoryKeychain


SWORD = {"playerItemId": 11, "itemCode": "SWORD_P1", "slotType": "WEAPON", "equipped": True,
         "qualityCode": "L2", "attack": 10}
SWORD2 = {"playerItemId": 12, "itemCode": "SWORD_P2", "slotType": "WEAPON", "equipped": False,
          "qualityCode": "L3", "attributes": [{"code": "ATTACK", "value": 15}]}
ROD = {"playerItemId": 13, "itemCode": "FISHING_ROD", "equipped": False}


class CharacterPageTests(unittest.TestCase):
    def test_page_shapes_bag_gear_and_tabs(self):
        page = character_page(
            {"name": "Neko", "characterClass": "ARCHER", "level": 31, "healthPotions": 7, "staminaPotions": 2},
            {"items": [SWORD, SWORD2, ROD]},
            {"experience": 50, "experienceNeeded": 200},
        )
        self.assertEqual(page["info"]["name"], "Neko")
        self.assertEqual(page["info"]["portrait"], "/assets/archer-portrait.png")
        self.assertEqual(page["info"]["experience_needed"], 200)
        self.assertEqual(page["info"]["stats_source"], "equipment")
        live = character_page({"name": "N"}, {"totals": {"attack": 1}}, {}, {"attack": 40, "maxHealth": 300})
        self.assertEqual(live["info"]["attack"], 40)
        self.assertEqual(live["info"]["stats_source"], "live")
        weapon = [g for g in page["gear"] if g["slot"] == "WEAPON"][0]
        self.assertEqual(weapon["item"]["id"], 11)
        self.assertEqual(len(page["gear"]), 6)
        ids = [b["id"] for b in page["bag"]]
        self.assertIn(12, ids)
        self.assertIn(13, ids)
        self.assertNotIn(11, ids)
        sword2 = [b for b in page["bag"] if b["id"] == 12][0]
        self.assertTrue(sword2["can_equip"])
        self.assertTrue(sword2["icon"].startswith("/assets/sword_p2"))
        self.assertEqual(sword2["attributes"][0]["code"], "ATTACK")
        rod = [b for b in page["bag"] if b["id"] == 13][0]
        self.assertFalse(rod["can_equip"])
        self.assertEqual(rod["category"], "OTHER")
        potion = [b for b in page["bag"] if b["virtual"] and b["use_kind"] == "health"][0]
        self.assertEqual(potion["quantity"], 7)
        tabs = {t["key"]: t["count"] for t in page["tabs"]}
        self.assertEqual(tabs["ALL"], len(page["bag"]))
        self.assertEqual(tabs["WEAPON"], 1)
        self.assertNotIn("sell", json.dumps(page))


class AssetAndMapLabelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "wallet.vault"
        (Path(self.tmp.name) / "map_levels.json").write_text(json.dumps(
            {"maps": {"3": {"samples": [10, 20, 30, 30]}}}), "utf-8")
        ready = {"event": threading.Event()}
        self.thread = threading.Thread(target=run_gui, kwargs={
            "config": load_config(), "vault_path": self.vault, "port": 0, "open_browser": False,
            "ready": ready, "keychain": MemoryKeychain()}, daemon=True)
        self.thread.start()
        self.assertTrue(ready["event"].wait(8))
        self.app = ready["app"]
        self.httpd = ready["httpd"]
        self.base = "http://127.0.0.1:%s" % ready["port"]

    def tearDown(self):
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        self.app.close()
        self.tmp.cleanup()

    def test_asset_route_serves_icons_and_blocks_traversal(self):
        with urlopen(self.base + "/assets/sword_p1.webp", timeout=8) as response:
            self.assertEqual(response.status, 200)
            self.assertTrue(len(response.read()) > 100)
        for bad in ("/assets/..%2Fgui.py", "/assets/nope.webp", "/assets/x.py"):
            with self.assertRaises(HTTPError):
                urlopen(self.base + bad, timeout=8)

    def test_map_labels_include_levels_or_unknown(self):
        self.assertEqual(self.app.map_level_text(3), "Lv10-30")
        self.assertEqual(self.app.map_level_text(2), "未知")
        self.assertTrue(self.app.map_with_levels(3, "北部雪地").endswith("(Lv10-30)"))
        with urlopen(self.base + "/api/state", timeout=8) as response:
            state = json.loads(response.read().decode("utf-8"))
        labels = {m["id"]: m["label"] for m in state["maps"]}
        self.assertTrue(labels[3].endswith("(Lv10-30)"), labels[3])
        self.assertTrue(labels[2].endswith("(未知)"), labels[2])


if __name__ == "__main__":
    unittest.main()
