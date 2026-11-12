import math
import unittest
from unittest import mock

from tabbybot.config import AppConfig, HuntConfig
from tabbybot.constants import KITE_CLEARANCE, KITE_WALL_CLEARANCE
from tabbybot.hunt import (
    HuntController,
    arc_waypoints,
    path_clears_point,
    segment_min_dist,
)
from tabbybot.protocol import Position


def ctl(x=100.0, y=100.0, map_id=2):
    c = HuntController(AppConfig(hunt=HuntConfig(map_id=map_id)), "ARCHER")
    c.world_ready = True
    c.position = Position(map_id=map_id, x=x, y=y, revision=1)
    c.auto_farm = True
    c.auto_quest = False
    c.farm_target_level = 40
    c.player_id = 7
    c.self_state = {"health": 300, "maxHealth": 300, "healthPotions": 9, "potionCooldownMs": 0}
    return c


def mons(c, rows, rank="NORMAL"):
    c.monsters.apply_snapshot({"delta": True, "deltaVersion": 2, "monsters": [
        {"encounterId": e, "mapId": c.position.map_id, "x": x, "y": y, "health": hp,
         "maxHealth": 250, "level": 40, "name": "troll", "monsterRank": rank}
        for e, x, y, hp in rows]})


class GeometryTests(unittest.TestCase):
    def test_path_does_not_cross_monster(self):
        threat = (0.0, 0.0)
        self.assertFalse(path_clears_point((-200, 0), [(200, 0)], threat, 120))
        self.assertTrue(path_clears_point((-200, 0), [(-200, 200), (200, 200), (200, 0)], threat, 120))
        self.assertGreaterEqual(segment_min_dist((-200, 200), (200, 200), threat), 200)

    def test_arc_stays_off_center(self):
        pts = arc_waypoints((0, 0), (200, 0), (0, 200), 1.0, 200, count=6)
        for pt in pts:
            self.assertGreater(math.dist(pt, (0, 0)), 190)


class SoftStopTests(unittest.TestCase):
    def test_finishes_current_target_then_stands(self):
        c = ctl(x=1040, y=1300)
        mons(c, [("a", 1040, 1200, 200)])
        c.attack_target = "a"
        c.tickets.accept({"id": "t1"})
        c.request_soft_stop()
        self.assertTrue(c.stop_after_kill)
        self.assertFalse(c.hunt_idle)
        kind, _ = c.next_combat_action(1.0)
        self.assertEqual(kind, "attack")
        self.assertEqual(c.attack_target, "a")
        mons(c, [("a", 1040, 1200, 0), ("b", 1100, 1300, 200)])
        kind, _ = c.next_combat_action(2.0)
        self.assertEqual(kind, "idle")
        self.assertTrue(c.hunt_idle)
        # A new monster in range must not be chased or attacked.
        c.tickets.accept({"id": "t2"})
        kind, _ = c.next_combat_action(3.0)
        self.assertEqual(kind, "idle")
        self.assertNotEqual(c.attack_target, "b")

    def test_idle_defends_without_chasing(self):
        c = ctl(x=1040, y=1300)
        mons(c, [("near", 1040, 1200, 200), ("far", 3000, 3000, 200)])
        c._enter_hunt_idle("已停止打怪，原地待命（未登出）")
        c.last_damage_at, c.last_damage_source = 2.8, "monster"
        c.tickets.accept({"id": "t1"})
        kind, _ = c.next_combat_action(3.0)
        self.assertEqual(kind, "attack")
        self.assertEqual(c.attack_target, "near")
        # Far monster after the nearby one is gone: stand, do not walk over.
        mons(c, [("near", 1040, 1200, 0), ("far", 3000, 3000, 200)])
        c.last_damage_at, c.last_damage_source = 3.1, "monster"
        kind, _ = c.next_combat_action(3.2)
        self.assertIn(kind, {"idle", "wait"})
        self.assertNotEqual(getattr(c, "activity", ""), "追擊")

    def test_immediate_stop_does_not_keep_fighting(self):
        c = ctl(x=1040, y=1300)
        mons(c, [("a", 1040, 1200, 200)])
        c.attack_target = "a"
        c.tickets.accept({"id": "t1"})
        c.request_soft_stop(immediate=True)
        self.assertTrue(c.hunt_idle)
        self.assertFalse(c.stop_after_kill)
        kind, _ = c.next_combat_action(1.0)
        self.assertEqual(kind, "idle")

    def test_resume_clears_idle(self):
        c = ctl()
        c._enter_hunt_idle("待命")
        c.resume_hunt()
        self.assertFalse(c.hunt_idle)
        self.assertFalse(c.movement_paused)


class PinnedMapTests(unittest.TestCase):
    def test_manual_map_not_overridden_in_west_wilds(self):
        c = ctl(x=100, y=100, map_id=4)
        c.auto_quest = True
        c.level_catalog = {4: [20, 30], 9: [40], 10: [40, 50], 8: [40]}
        c.pin_map(10)  # 冰封洞穴
        self.assertEqual(c.farm_map(), 10)
        self.assertEqual(c.desired_map()[0], 10)

    def test_travel_retargets_to_pinned_map(self):
        c = ctl(x=100, y=100, map_id=4)
        c.pin_map(9)
        c.begin_travel_to(9)
        first = c.travel_final_target
        c.pin_map(10)
        c.begin_travel_to(10)
        self.assertEqual(c.pinned_map, 10)
        # Either travelling to 10, or already cleared because path setup failed; destination must not stay 9.
        self.assertNotEqual(c.travel_final_target, 9)
        if c.travel_final_target is not None:
            self.assertEqual(c.travel_final_target, 10)
        self.assertEqual(first, 9)


class KiteEliteOnlyTests(unittest.TestCase):
    def test_normal_mobs_are_shot_standing(self):
        c = ctl(x=1040, y=1150)
        mons(c, [("a", 1040, 1040, 250), ("b", 1060, 1000, 250)], rank="NORMAL")
        c.kite_elite_only = True
        c.cluster_kite = True
        c._own_hit_ids.update(("a", "b"))
        c.last_damage_at, c.last_damage_source = 0.5, "monster"
        c.tickets.accept({"id": "t1"})
        kind, _ = c.next_combat_action(1.0)
        self.assertNotEqual(c.activity, "繞圈風箏")
        self.assertIn(kind, {"attack", "wait"})

    def test_elites_still_kite(self):
        c = ctl(x=1040, y=1150)
        mons(c, [("a", 1040, 1040, 250), ("b", 1060, 1000, 250)], rank="ELITE")
        c.kite_elite_only = True
        c.cluster_kite = True
        c._own_hit_ids.update(("a", "b"))
        c.last_damage_at, c.last_damage_source = 0.5, "monster"
        c.tickets.accept({"id": "t1"})
        kind, _ = c.next_combat_action(1.0)
        self.assertEqual(kind, "chase")  # too close: move first
        self.assertEqual(c.activity, "繞圈風箏")


class WallClearanceTests(unittest.TestCase):
    def test_segment_through_monster_is_rejected(self):
        c = ctl(x=-200, y=0)
        with mock.patch.object(HuntController, "_point_walkable", return_value=True), \
             mock.patch.object(HuntController, "wall_distance", return_value=80.0), \
             mock.patch("tabbybot.hunt.has_navigation", return_value=False):
            self.assertFalse(c.kite_goal_ok((200, 0), threat=(0, 0)))
            self.assertTrue(c.kite_goal_ok((0, 200), threat=(0, 0)))

    def test_wall_hugging_goal_is_rejected(self):
        c = ctl(x=80, y=100)

        def room(point, limit=80.0):
            # A wall along x=0: distance is the x coordinate.
            return max(0.0, float(point[0]))

        with mock.patch.object(HuntController, "_point_walkable", return_value=True), \
             mock.patch.object(HuntController, "wall_distance", side_effect=room), \
             mock.patch("tabbybot.hunt.has_navigation", return_value=False):
            self.assertFalse(c.segment_open((80, 100), (10, 100), min_clear=c.kite_wall_need((80, 100))))
            self.assertTrue(c.segment_open((80, 100), (120, 100), min_clear=c.kite_wall_need((80, 100))))
            toward_wall = c._move_to((10, 100), 1.0, threat=(200, 100))
            if toward_wall is not None:
                self.assertGreaterEqual(toward_wall["x"], 80.0 - 1.0)
            # Sideways into open space, away from both the wall and the monster.
            packet = c._move_to((80, 220), 1.0, threat=(200, 100))
            self.assertIsNotNone(packet)
            self.assertGreaterEqual(packet["x"], 80.0 - 1.0)
            if c._kite_route:
                self.assertTrue(path_clears_point((80, 100), c._kite_route, (200, 100),
                                                  min(KITE_CLEARANCE, math.dist((80, 100), (200, 100)))))

    def test_orbit_prefers_open_side(self):
        c = ctl(x=100, y=0)

        def room(point, limit=80.0):
            return max(0.0, float(point[0]))  # wall at x=0

        with mock.patch.object(HuntController, "_point_walkable", return_value=True), \
             mock.patch.object(HuntController, "wall_distance", side_effect=room), \
             mock.patch.object(HuntController, "segment_open", return_value=True), \
             mock.patch("tabbybot.hunt.has_navigation", return_value=False):
            goal = c._orbit_goal((200, 0), 200.0, (200, 0))
            self.assertIsNotNone(goal)
            self.assertGreaterEqual(c.wall_distance(goal), KITE_WALL_CLEARANCE)


if __name__ == "__main__":
    unittest.main()
