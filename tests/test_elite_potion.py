import unittest

from tabbybot.config import AppConfig, HuntConfig
from tabbybot.constants import KITE_MIN_DIST
from tabbybot.hunt import HuntController
from tabbybot.protocol import Position


def ctl(klass="WARRIOR", potions=1):
    c = HuntController(AppConfig(hunt=HuntConfig(map_id=2)), klass)
    c.world_ready = True
    c.position = Position(map_id=2, x=100, y=100, revision=1)
    c.auto_farm = False
    c.player_id = 7
    c.auto_potion = True
    c.self_state = {"health": 100, "maxHealth": 200, "healthPotions": potions, "potionCooldownMs": 0}
    c.plan_cluster = lambda *a, **k: None  # legacy single-target path
    return c


def mon(c, enc, x, y=100, elite=False, health=250, max_health=250, level=10):
    row = {"encounterId": enc, "mapId": 2, "x": x, "y": y, "health": health, "maxHealth": max_health, "level": level}
    if elite:
        row["monsterRank"] = "ELITE"
    c.monsters.apply_snapshot({"delta": True, "deltaVersion": 2, "monsters": [row]})


def peer(c, x, y=100, pid="99"):
    c.peers.apply_snapshot({"peers": [{"id": pid, "playerName": "p", "x": x, "y": y, "mapId": 2}]}, fallback_map=2)


class MeleeEliteThresholdTests(unittest.TestCase):
    def test_low_potions_skip_fresh_elite(self):
        c = ctl(potions=1)
        mon(c, "e", 130, elite=True)
        self.assertEqual(c.candidates(), [])
        self.assertIn("不主動打精英", c.elite_note)

    def test_enough_potions_allows_elite(self):
        c = ctl(potions=5)
        mon(c, "e", 130, elite=True)
        self.assertEqual([r[1] for r in c.candidates()], ["e"])

    def test_join_when_elite_already_damaged(self):
        c = ctl(potions=0)
        mon(c, "e", 130, elite=True, health=120)
        self.assertEqual([r[1] for r in c.candidates()], ["e"])
        self.assertIn("一起參與", c.elite_note)

    def test_join_when_player_next_to_elite(self):
        c = ctl(potions=0)
        mon(c, "e", 600, elite=True)
        peer(c, 650)
        self.assertEqual([r[1] for r in c.candidates()], ["e"])

    def test_threshold_setting(self):
        c = ctl(potions=2)
        c.apply_combat_options({"elite_potion_min": 2})
        mon(c, "e", 130, elite=True)
        self.assertEqual(len(c.candidates()), 1)


class EliteePriorityTests(unittest.TestCase):
    def test_switch_to_elite_only_after_current_target(self):
        c = ctl("WARRIOR", potions=9)
        mon(c, "normal", 130)
        c.tickets.accept({"id": "t1"})
        kind, payload = c.next_combat_action(1.0)
        self.assertEqual(payload[1][1]["encounterId"], "normal")
        mon(c, "elite", 140, elite=True)
        c.tickets.accept({"id": "t2"})
        kind, payload = c.next_combat_action(c.tickets.busy_until_at + 0.01)
        self.assertEqual(payload[1][1]["encounterId"], "normal")  # keep current fight
        mon(c, "normal", 130, health=0)
        c.tickets.accept({"id": "t3"})
        kind, payload = c.next_combat_action(c.tickets.busy_until_at + 0.01)
        self.assertEqual(payload[1][1]["encounterId"], "elite")

    def test_prefer_elite_off_keeps_nearest(self):
        c = ctl("WARRIOR", potions=9)
        c.apply_combat_options({"prefer_elite": False})
        mon(c, "normal", 120)
        mon(c, "elite", 150, elite=True)
        self.assertEqual(c.candidates()[0][1], "normal")

    def test_elite_first_but_only_in_chosen_band(self):
        c = ctl("WARRIOR", potions=9)
        c.auto_farm = True
        c.farm_target_level = 30
        mon(c, "n30", 140, level=30)
        mon(c, "e10", 400, elite=True, level=10)
        mon(c, "e30", 500, elite=True, level=30)
        self.assertEqual([r[1] for r in c.candidates()], ["e30", "n30"])

    def test_prefer_elite_only_within_view_range(self):
        c = ctl("WARRIOR", potions=9)
        mon(c, "normal", 130)
        mon(c, "far", 100 + 2000, elite=True)  # 2000 away, off-screen
        self.assertEqual(c.candidates()[0][1], "normal")
        mon(c, "near", 100 + 500, elite=True)  # 500 away, on screen
        self.assertEqual(c.candidates()[0][1], "near")

    def test_elite_view_range_is_configurable(self):
        c = ctl("WARRIOR", potions=9)
        mon(c, "normal", 130)
        mon(c, "mid", 100 + 500, elite=True)
        c.apply_combat_options({"elite_view_range": 300})
        self.assertEqual(c.elite_view_range, 300)
        self.assertEqual(c.candidates()[0][1], "normal")
        c.apply_combat_options({"elite_view_range": 600})
        self.assertEqual(c.candidates()[0][1], "mid")


class PotionTests(unittest.TestCase):
    def test_drinks_on_monster_damage(self):
        c = ctl(potions=3)
        mon(c, "m", 140)
        c._last_hp = 150
        c._track_damage(10.0)
        self.assertEqual(c.last_damage_source, "monster")
        self.assertEqual(c.plan_auto_potion(10.0), {"action": "useHealthPotion"})
        self.assertIsNone(c.plan_auto_potion(10.5))  # retry guard

    def test_player_damage_respects_toggle(self):
        c = ctl(potions=3)
        peer(c, 200)
        c._last_hp = 150
        c._track_damage(10.0)
        self.assertEqual(c.last_damage_source, "player")
        self.assertIsNone(c.plan_auto_potion(10.0))
        self.assertIn("被玩家攻擊", c.potion_note)
        c.apply_combat_options({"potion_on_player_damage": True})
        self.assertEqual(c.plan_auto_potion(10.1), {"action": "useHealthPotion"})

    def test_explicit_player_hit_packet(self):
        c = ctl(potions=3)
        c.on_packet({"type": "hit", "attacker": 99, "hit": {"player": 7, "damage": 30}})
        self.assertEqual(c.classify_damage_source(c._player_hit_at + 1), "player")
        self.assertEqual(c.hits, 0)

    def test_no_drink_when_healthy_or_cooldown(self):
        c = ctl(potions=3)
        c.self_state["health"] = 180
        self.assertIsNone(c.plan_auto_potion(1.0))
        c.self_state.update(health=50, potionCooldownMs=500)
        self.assertIsNone(c.plan_auto_potion(1.0))
        c.apply_combat_options({"auto_potion": False})
        c.self_state["potionCooldownMs"] = 0
        self.assertIsNone(c.plan_auto_potion(1.0))


class KiteTests(unittest.TestCase):
    def test_archer_ignores_threshold_and_kites_when_close(self):
        c = ctl("ARCHER", potions=0)
        mon(c, "e", 100 + KITE_MIN_DIST - 50, elite=True)
        self.assertEqual([r[1] for r in c.candidates()], ["e"])
        c.tickets.accept({"id": "t1"})
        kind, packet = c.next_combat_action(1.0)
        self.assertEqual(kind, "chase")  # too close: move first
        self.assertLess(packet["x"], 100)  # stepping away from the elite (which is to the east)
        self.assertIn("風箏", c.kite_note)

    def test_archer_shoots_when_far_enough(self):
        c = ctl("ARCHER", potions=0)
        mon(c, "e", 100 + 250, elite=True)
        c.tickets.accept({"id": "t1"})
        kind, payload = c.next_combat_action(1.0)
        self.assertEqual(kind, "attack")

    def test_archer_with_potions_does_not_kite_unless_always(self):
        c = ctl("ARCHER", potions=9)
        mon(c, "e", 150, elite=True)
        c.tickets.accept({"id": "t1"})
        self.assertEqual(c.next_combat_action(1.0)[0], "chase")
        c2 = ctl("ARCHER", potions=9)
        c2.apply_combat_options({"kite_low_potion": True})
        mon(c2, "e", 150, elite=True)
        c2.tickets.accept({"id": "t1"})
        self.assertEqual(c2.next_combat_action(1.0)[0], "chase")

    def test_kite_aborts_on_combat_lock(self):
        c = ctl("ARCHER", potions=0)
        mon(c, "e", 150, elite=True)
        c.candidates()
        c.attack_target = "e"
        for _ in range(2):
            c.on_packet({"type": "error", "code": "combat_locked"})
        self.assertIn("e", c.kite_failed)
        self.assertIn("combat_locked", c.kite_note)

    def test_melee_never_kites(self):
        c = ctl("WARRIOR", potions=9)
        c.apply_combat_options({"kite_low_potion": True})
        mon(c, "e", 130, elite=True)
        c.tickets.accept({"id": "t1"})
        self.assertEqual(c.next_combat_action(1.0)[0], "attack")


if __name__ == "__main__":
    unittest.main()
