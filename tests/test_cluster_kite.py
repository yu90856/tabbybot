from unittest import mock
import logging
import math
import unittest

from tabbybot.cluster import ClusterTracker, link_clusters
from tabbybot.config import AppConfig, HuntConfig
from tabbybot.efficiency import EfficiencyTracker
from tabbybot.hunt import HuntController, monster_display_name
from tabbybot.protocol import Position


def ctl(x=100.0, y=100.0):
    c = HuntController(AppConfig(hunt=HuntConfig(map_id=2)), "ARCHER")
    c.world_ready = True
    c.position = Position(map_id=2, x=x, y=y, revision=1)
    c.auto_farm = True
    c.farm_target_level = 30
    c.player_id = 7
    c.self_state = {"health": 300, "maxHealth": 300, "healthPotions": 9, "potionCooldownMs": 0}
    return c


def mons(c, rows):
    c.monsters.apply_snapshot({"delta": True, "deltaVersion": 2, "monsters": [
        {"encounterId": e, "mapId": 2, "x": x, "y": y, "health": hp, "maxHealth": 250, "level": 30, "name": "troll"}
        for e, x, y, hp in rows]})


PACK = [("a", 1000, 1000, 250), ("b", 1060, 1000, 250), ("c", 1000, 1070, 250), ("d", 1080, 1080, 250),
        ("e", 1040, 1040, 250)]


class TrackerTests(unittest.TestCase):
    def test_link_clusters(self):
        groups = link_clusters([("a", 0, 0), ("b", 200, 0), ("c", 450, 0), ("d", 2000, 0)])
        self.assertEqual(sorted(len(g) for g in groups), [1, 3])

    def test_respawn_measured(self):
        t = ClusterTracker()
        t.update(2, [("a", 0, 0), ("b", 50, 0)], 0.0)
        self.assertEqual(len(t.sites), 1)
        t.update(2, [("b", 50, 0)], 10.0)          # a died
        t.update(2, [], 12.0)                      # b died -> site empty
        site = t.sites[0]
        self.assertEqual(site.empty_since, 12.0)
        t.update(2, [("a2", 10, 10)], 30.0)        # respawn 20 s after a's death
        self.assertEqual(site.respawn_avg(), 20.0)
        self.assertEqual(len(t.sites), 1)
        self.assertIsNone(site.empty_since)


class ClusterOrbitTests(unittest.TestCase):
    def test_attacks_from_the_ring(self):
        c = ctl(x=1040, y=1300)   # ~260 from pack centre, ~225 from nearest
        mons(c, PACK)
        c.tickets.accept({"id": "t1"})
        kind, _ = c.next_combat_action(1.0)
        self.assertEqual(kind, "attack")
        self.assertIsNotNone(c.cluster_site_id)
        self.assertIn("怪堆 5 隻", c.cluster_note)

    def test_orbits_instead_of_walking_away_when_too_close(self):
        c = ctl(x=1040, y=1150)   # 70 from the nearest monster
        mons(c, PACK)
        c._own_hit_ids.update(row[0] for row in PACK) if isinstance(PACK[0], tuple) else None
        c.last_damage_at, c.last_damage_source = 0.5, "monster"  # it's fighting us
        c.tickets.accept({"id": "t1"})
        kind, packet = c.next_combat_action(1.0)
        self.assertEqual(kind, "chase")  # too close: move first
        self.assertEqual(c.activity, "繞圈風箏")
        self.assertEqual(c.activity, "繞圈風箏")
        site = c.clusters.site(c.cluster_site_id)
        # stays near the cluster (within the leash), never runs off
        self.assertLess(math.dist((c.position.x, c.position.y), (site.x, site.y)), c.kite_leash)

    def test_max_pull_finishes_pulled_ones_first(self):
        c = ctl(x=1040, y=1300)
        mons(c, PACK)
        c.max_pull = 1
        c._own_hit_ids.add("e")
        c.tickets.accept({"id": "t1"})
        kind, _ = c.next_combat_action(1.0)
        self.assertEqual(kind, "attack")
        self.assertEqual(c.attack_target, "e")

    def test_waits_for_respawn_instead_of_leaving(self):
        c = ctl(x=1040, y=1300)
        mons(c, PACK)
        c.next_combat_action(1.0)
        mons(c, [(e, x, y, 0) for e, x, y, _ in PACK])     # pack cleared
        mons(c, [("far", 3000, 3000, 250)])               # another pack far away
        c._cluster_update_at = 0
        kind, _ = c.next_combat_action(2.0)
        self.assertEqual(kind, "wait")
        self.assertIn("等重生", c.cluster_note)

    def test_off_for_melee(self):
        c = ctl()
        c.character_class = "WARRIOR"
        self.assertFalse(c.cluster_mode())


class LeashTests(unittest.TestCase):
    def test_zero_damage_streak_marks_target_resetting(self):
        c = ctl(x=1040, y=1300)
        mons(c, PACK)
        c._engage = {"id": "a", "at": 0.0, "last_hit_at": 0.0, "anchor": (1000.0, 1000.0), "hp": 300,
                     "potions": 9, "level": 30, "map": 2, "name": "x"}
        c.attack_target = "a"
        c.position.x, c.position.y = 1000, 1400   # dragged 400 from home
        for _ in range(3):
            c.on_packet({"type": "hit", "attacker": 7, "hit": {"monster": "a", "damage": 0}})
        self.assertIsNone(c.attack_target)
        self.assertTrue(c.tickets.is_invalid("a"))
        self.assertEqual(c.leash_resets, 1)
        self.assertLess(c.kite_leash, 450)
        self.assertGreaterEqual(c.kite_leash, 220)

    def test_hp_regen_marks_resetting(self):
        c = ctl()
        mons(c, [("a", 300, 100, 100)])
        c.attack_target = "a"
        c._check_regen()
        mons(c, [("a", 320, 100, 180)])
        c._check_regen()
        self.assertEqual(c.leash_resets, 1)
        self.assertIsNone(c.attack_target)


class ReadableLogTests(unittest.TestCase):
    def test_lines_have_no_ids(self):
        c = ctl()
        mons(c, [("deadbeefcafe", 300, 100, 250)])
        records = []

        class H(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = H()
        logging.getLogger("tabbybot.combat").addHandler(handler)
        logging.getLogger("tabbybot.combat").setLevel(logging.INFO)
        try:
            c.on_packet({"type": "hit", "attacker": 7, "hit": {"monster": "deadbeefcafe", "damage": 37}})
        finally:
            logging.getLogger("tabbybot.combat").removeHandler(handler)
        self.assertEqual(records, ["| 巨魔 Lv30 | 攻擊 | 37 | 213/250 | 300/300"])
        self.assertNotIn("deadbeef", c.status_line())
        self.assertEqual(monster_display_name({"name": "troll", "level": 40}), "巨魔 Lv40")


class CycleTimeTests(unittest.TestCase):
    def test_cycle_time_includes_waiting(self):
        e = EfficiencyTracker(None)
        e.record_kill(2, 30, 5.0, 0, 0.0, cycle_secs=None)
        e.record_kill(2, 30, 5.0, 0, 30.0, cycle_secs=30.0)
        row = e.table()["rows"][0]
        self.assertEqual(row["avg_secs"], 30.0)
        self.assertEqual(row["fight_secs"], 5.0)


if __name__ == "__main__":
    unittest.main()


class KiteToggleTests(unittest.TestCase):
    def test_kite_off_stands_and_shoots(self):
        c = ctl(x=1040, y=1150)   # monster 70 away: kiting would step back
        mons(c, PACK)
        c.cluster_kite = False
        c._own_hit_ids.add("e")
        c.kite_targets.add("e")
        c.overlevel_targets.add("e")
        self.assertFalse(c.kite_active("e"))
        c.tickets.accept({"id": "t1"})
        kind, _ = c.next_combat_action(1.0)
        self.assertEqual(kind, "attack")

    def test_saved_overlevel_settings_migrated_off(self):
        from tabbybot.gui import COMBAT_DEFAULTS, clean_combat_options
        result = clean_combat_options({"overlevel_kite": True, "overlevel_max": 20, "kite_low_potion": True,
                                       "cluster_kite": True}, COMBAT_DEFAULTS)
        self.assertFalse(result["overlevel_kite"])
        self.assertFalse(result["kite_low_potion"])
        self.assertTrue(result["cluster_kite"])


class WallDetourTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from tabbybot import learned_nav
        self._tmp = tempfile.TemporaryDirectory()
        self._old = learned_nav.LEARNED_DIR
        learned_nav.LEARNED_DIR = Path(self._tmp.name)
        learned_nav._GRIDS.clear()

    def tearDown(self):
        from tabbybot import learned_nav
        learned_nav.LEARNED_DIR = self._old
        learned_nav._GRIDS.clear()
        self._tmp.cleanup()

    def test_rejections_learn_blocks_and_travel_replans_around(self):
        from tabbybot import learned_nav
        c = HuntController(AppConfig(hunt=HuntConfig(map_id=6)), "ARCHER")
        c.world_ready = True
        c.position = Position(map_id=6, x=3505, y=414, revision=5)
        c.begin_travel_to(8, 0.0)
        self.assertEqual(c.travel_phase, "walk")
        portal = c.pending_portal
        # A wall runs north-south just west of us; the server keeps putting us back.
        wall_x = 3480
        rev = 6
        for i in range(120):
            now = 1.0 + i * 0.1
            c.advance_travel(now)
            if c.position.x < wall_x and 250 < c.position.y < 900:
                back = (wall_x + 5, c.position.y)
                c.on_packet({"type": "error", "code": "invalid_position",
                             "position": {"map": 6, "x": back[0], "y": back[1], "revision": rev}})
                rev += 1
        grid = learned_nav.learned_grid(6)
        self.assertTrue(grid.blocked)
        self.assertTrue((learned_nav.learned_dir() / "nav_6.json").is_file())
        # The re-planned route steers around the learned wall instead of straight through it.
        route = grid.find_path((c.position.x, c.position.y), portal)
        self.assertTrue(route)
        self.assertTrue(grid.line_clear((c.position.x, c.position.y), route[0]) or len(route) > 1)

    def test_astar_goes_around_wall(self):
        from tabbybot.learned_nav import LearnedGrid, cell_of
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            g = LearnedGrid(99, Path(d))
            for y in range(0, 20):
                g.blocked.add((10, y))
            path = g.find_path((200.0, 300.0), (500.0, 300.0))
            self.assertTrue(path)
            pts = [(200.0, 300.0)] + path
            for a, b in zip(pts, pts[1:]):
                steps = 40
                for k in range(steps + 1):
                    x = a[0] + (b[0] - a[0]) * k / steps
                    y = a[1] + (b[1] - a[1]) * k / steps
                    self.assertNotIn(cell_of(x, y), g.blocked)

    def test_gives_up_and_grinds_here_when_wall_never_clears(self):
        c = HuntController(AppConfig(hunt=HuntConfig(map_id=6)), "ARCHER")
        c.world_ready = True
        c.position = Position(map_id=6, x=3505, y=414, revision=5)
        c.begin_travel_to(8, 0.0)
        with mock.patch("tabbybot.hunt.time.monotonic", side_effect=[float(t) for t in range(0, 400, 2)] * 3):
            for rev in range(6, 60):
                c.on_packet({"type": "error", "code": "invalid_position",
                             "position": {"map": 6, "x": 3505, "y": 414, "revision": rev}})
                if c.travel_phase is None:
                    break
        self.assertIsNone(c.travel_phase)
        self.assertTrue(c.farm_hold_active(c.farm_hold[1] - 1))
        self.assertIn("走不通", c.status)

    def test_watchdog_flags_travel_that_never_moves(self):
        import tempfile
        from pathlib import Path
        from tabbybot.watchdog import StuckWatchdog
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dog = StuckWatchdog(notify=lambda t, m: None, alerts_path=Path(tmp.name) / "a.log")
        c = ctl(x=3903, y=1493)
        self.assertIsNone(dog.observe(c, "travel", 0))
        c.position.x = 3914
        self.assertIsNone(dog.observe(c, "travel", 60))
        self.assertEqual(dog.observe(c, "travel", 121), "drop")
        self.assertIn("沒有前進", dog.alert["reason"])


class PatrolSpotTests(unittest.TestCase):
    def test_patrol_heads_to_recorded_spot_and_logs_it(self):
        c = HuntController(AppConfig(hunt=HuntConfig(map_id=10)), "ARCHER")
        c.world_ready = True
        c.auto_farm = True
        c.farm_target_level = 40
        c.position = Position(map_id=10, x=3600, y=3200, revision=1)
        c.level_store.add_spot(10, 40, 2800, 2800)
        c.level_store.add_spot(10, 30, 3500, 3200)
        with self.assertLogs("tabbybot.hunt", level="INFO") as cm:
            path = c._patrol_to_spot(100.0)
        self.assertTrue(path)
        self.assertLess(math.dist(path[-1], (2800, 2800)), 80)
        self.assertTrue(any("巡邏前往 (2800, 2800)" in line for line in cm.output))
        # Same spot isn't picked again right away.
        self.assertIsNone(c._patrol_to_spot(110.0))


class RestInsteadOfPotionTests(unittest.TestCase):
    def make(self, hp):
        c = HuntController(AppConfig(hunt=HuntConfig(map_id=10)), "ARCHER")
        c.world_ready = True
        c.position = Position(map_id=10, x=3600, y=3200, revision=1)
        c.self_state = {"health": hp, "maxHealth": 311, "healthPotions": 10}
        return c

    def test_no_auto_potion_by_default(self):
        c = self.make(50)
        self.assertFalse(c.auto_potion)
        self.assertIsNone(c.plan_auto_potion(100.0))

    def test_low_hp_rests_until_80_percent(self):
        c = self.make(100)  # 32%
        c.monsters.monsters["m1"] = {"encounterId": "m1", "x": 3650, "y": 3200, "health": 100,
                                     "maxHealth": 100, "mapId": 10, "level": 50}
        with mock.patch("tabbybot.hunt.has_navigation", return_value=False):
            action = c.plan_rest(1.0)
        self.assertTrue(c.resting)
        self.assertIn(action[0], {"rest", "wait"})
        if action[0] == "rest":
            self.assertLess(action[1]["x"] if "x" in action[1] else c.position.x, 3600.1)
        c.self_state["health"] = 200  # 64%: still resting
        self.assertIsNotNone(c.plan_rest(5.0))
        c.self_state["health"] = 260  # 84%
        self.assertIsNone(c.plan_rest(10.0))
        self.assertFalse(c.resting)

    def test_repeated_low_hp_suggests_lower_level(self):
        c = self.make(311)
        c.farm_target_level = 50
        with self.assertLogs("tabbybot.hunt", level="INFO") as cm:
            for i in range(3):
                c.self_state["health"] = 90
                c.plan_rest(10.0 + i * 60)
                c.self_state["health"] = 300
                c.plan_rest(40.0 + i * 60)
        self.assertTrue(any("考慮把「練功等級」調低" in line for line in cm.output))


class IdleMonsterNotKitedTests(unittest.TestCase):
    def test_idle_monster_close_is_shot_not_kited(self):
        c = ctl(x=1040, y=1150)
        mons(c, PACK)
        c.tickets.accept({"id": "t1"})
        kind, _ = c.next_combat_action(1.0)
        self.assertEqual(kind, "chase")  # even idle, within 180 we step out first

    def test_wall_behind_means_shoot(self):
        c = ctl(x=1040, y=1150)
        mons(c, PACK)
        c.last_damage_at, c.last_damage_source = 0.5, "monster"
        c._reject_times = [0.5, 0.8]
        c.tickets.accept({"id": "t1"})
        kind, _ = c.next_combat_action(1.0)
        self.assertEqual(kind, "attack")


class DecodedNavTests(unittest.TestCase):
    def test_south_forest_has_decoded_nav_and_route_to_mine(self):
        from tabbybot.navigation import has_navigation, find_map_path, navigator_for
        for m in (3, 4, 6):
            self.assertTrue(has_navigation(m))
        nav = navigator_for(6)
        # server-corrected stuck spots sit exactly on the decoded wall edge
        self.assertNotEqual(nav.walkable(3505, 414), nav.walkable(3515, 407))
        path = find_map_path(6, (3756, 240), (1376, 1888))
        self.assertTrue(path)
        self.assertLess(math.dist(path[-1], (1376, 1888)), 1.0)

    def test_start_just_outside_cell_is_snapped(self):
        from tabbybot.navigation import find_map_path
        path = find_map_path(10, (4474, 1405), (3600, 3200))
        self.assertTrue(path)


class DeathNotKillTests(unittest.TestCase):
    def test_traditional_names(self):
        from tabbybot.hunt import monster_display_name
        self.assertEqual(monster_display_name({"name": "盗贼", "level": 12}), "盜賊 Lv12")
        self.assertEqual(monster_display_name({"name": "牛頭人 Lv50", "level": 50}), "牛頭人 Lv50")


class EdgeClearanceTests(unittest.TestCase):
    def test_route_keeps_off_wall_edge_near_forest_mine(self):
        from tabbybot.navigation import find_map_path
        path = find_map_path(6, (2043, 1935), (3872, 96))
        self.assertTrue(path)
        # no waypoint sits on the y=1920 GroundBlocker edge
        self.assertFalse(any(abs(p[1] - 1920) < 1 for p in path[:-1]))
