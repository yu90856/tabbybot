"""Overnight watchdog, stutter-step kiting, and low-HP retreat."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tabbybot.config import AppConfig, HuntConfig
from tabbybot.constants import KITE_RETREAT_DIST, KITE_SHOOT_MIN
from tabbybot.hunt import HuntController, REST_LOW_RATIO, REST_CORNERED_FAILS
from tabbybot.protocol import Position
from tabbybot.watchdog import StuckWatchdog


def ctl(x=100, y=100, klass="ARCHER"):
    c = HuntController(AppConfig(hunt=HuntConfig(map_id=2)), klass)
    c.world_ready = True
    c.position = Position(map_id=2, x=x, y=y, revision=4)
    c.auto_farm = False
    c.cluster_kite = False  # use single-target path unless a test turns it on
    c.auto_combat = True
    return c


def add_mon(c, enc="enc-1", x=150, y=100, health=250, max_health=250, level=40, elite=False):
    row = {"encounterId": enc, "mapId": 2, "x": x, "y": y, "health": health,
           "maxHealth": max_health, "level": level}
    if elite:
        row["monsterRank"] = "ELITE"
    c.monsters.apply_snapshot({"delta": True, "deltaVersion": 2, "monsters": [row]})
    return enc


class WatchdogSelfHealTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "alerts.log"
        self.hooks = []
        self.dog = StuckWatchdog(notify=lambda t, m: None, alerts_path=self.path, hook=self.hooks.append)
        self.c = ctl()

    def tearDown(self):
        self.tmp.cleanup()

    def test_exp_unchanged_five_minutes_drops(self):
        dog = StuckWatchdog(notify=lambda t, m: None, alerts_path=self.path,
                            no_exp_sec=300, no_move_sec=10_000)
        self.c.self_state["experience"] = 10
        self.c.position.x = 100
        self.assertIsNone(dog.observe(self.c, "attack", 0))
        self.c.position.x = 800  # moving, so only EXP matters
        self.assertIsNone(dog.observe(self.c, "attack", 299))
        self.assertEqual(dog.observe(self.c, "attack", 301), "drop")
        self.assertIn("經驗", dog.alert["reason"])

    def test_resting_does_not_trip_position_watch(self):
        dog = StuckWatchdog(notify=lambda t, m: None, alerts_path=self.path,
                            no_exp_sec=10_000, no_move_sec=120)
        self.c.resting = True
        self.c.self_state.update({"health": 80, "maxHealth": 320})
        self.assertIsNone(dog.observe(self.c, "rest", 0))
        self.assertIsNone(dog.observe(self.c, "rest", 130))
        # Frozen rest at full HP does count as stuck.
        self.c.self_state["health"] = 320
        self.assertEqual(dog.observe(self.c, "rest", 260), "drop")

    def test_stale_target_distance_and_hp(self):
        dog = StuckWatchdog(notify=lambda t, m: None, alerts_path=self.path,
                            no_exp_sec=10_000, no_move_sec=10_000, stale_target_sec=60)
        add_mon(self.c, "frozen", x=884, y=100, health=850)
        self.c.attack_target = "frozen"
        self.assertIsNone(dog.observe(self.c, "attack", 0))
        self.assertIsNone(dog.observe(self.c, "attack", 59))
        self.assertEqual(dog.observe(self.c, "attack", 61), "drop")
        self.assertIn("同一目標", dog.alert["reason"])

    def test_hp_dropping_is_not_a_stale_target(self):
        dog = StuckWatchdog(notify=lambda t, m: None, alerts_path=self.path,
                            no_exp_sec=10_000, no_move_sec=10_000, stale_target_sec=60)
        add_mon(self.c, "live", x=300, y=100, health=850)
        self.c.attack_target = "live"
        dog.observe(self.c, "attack", 0)
        add_mon(self.c, "live", x=300, y=100, health=700)
        self.assertIsNone(dog.observe(self.c, "attack", 61))

    def test_login_error_relogs_immediately(self):
        self.c.auth_rejected = True
        self.c.login_error = True
        self.assertEqual(self.dog.observe(self.c, "wait", 5), "relogin")
        self.assertIn("LoginError", self.dog.alert["reason"])
        self.assertTrue(self.hooks[-1]["relogin"])

    def test_stale_world_relogs(self):
        dog = StuckWatchdog(notify=lambda t, m: None, alerts_path=self.path, stale_world_sec=30,
                            no_exp_sec=10_000, no_move_sec=10_000)
        self.c.last_snapshot_at = 1.0
        self.c.world_ready = True
        self.assertEqual(dog.observe(self.c, "wait", 32), "relogin")
        self.assertIn("過期", dog.alert["reason"])

    def test_three_failures_escalate_and_keep_retrying(self):
        dog = StuckWatchdog(notify=lambda t, m: None, alerts_path=self.path, hook=self.hooks.append,
                            no_exp_sec=10_000, no_move_sec=60, escalate_count=3,
                            escalate_window_sec=900, escalate_retry_sec=300)
        self.assertEqual(dog.observe(self.c, "attack", 0), None)
        self.assertEqual(dog.observe(self.c, "attack", 61), "drop")
        self.assertEqual(dog.observe(self.c, "attack", 130), "relogin")
        self.assertEqual(dog.observe(self.c, "attack", 200), "relogin")
        self.assertEqual(dog.alert["level"], "escalate")
        self.assertTrue(self.hooks[-1]["escalate"])
        # Within the 5-minute retry window, stay quiet.
        self.assertIsNone(dog.observe(self.c, "attack", 400))
        # After 5 minutes, try again.
        self.assertEqual(dog.observe(self.c, "attack", 200 + 301), "relogin")

    def test_idle_hunt_is_ignored(self):
        self.c.hunt_idle = True
        self.assertIsNone(self.dog.observe(self.c, "idle", 0))
        self.assertIsNone(self.dog.observe(self.c, "idle", 400))


class StutterStepKiteTests(unittest.TestCase):
    def test_close_monster_moves_before_shot(self):
        c = ctl(x=100, y=100)
        add_mon(c, x=170, y=100, health=850)  # 70 away, inside 180
        c.cluster_kite = True
        c.plan_cluster = lambda *a, **k: None
        c.tickets.accept({"id": "t1"})
        kind, packet = c.next_combat_action(1.0)
        self.assertEqual(kind, "chase")
        self.assertEqual(packet["action"], "sync")
        self.assertLess(packet["x"], 100)

    def test_after_impact_retreats_instead_of_waiting_idle(self):
        c = ctl(x=100, y=100)
        add_mon(c, x=170, y=100, health=850)
        c.cluster_kite = True
        c.plan_cluster = lambda *a, **k: None
        c.tickets.accept({"id": "t1"})
        # Even mid-cooldown, once the shot has landed we step away.
        c._shot_move_at = 2.0
        kind, packet = c.next_combat_action(2.1)
        self.assertEqual(kind, "chase")
        self.assertEqual(packet["action"], "sync")
        self.assertLess(packet["x"], 100)

    def test_stutter_step_keeps_shot_cadence(self):
        c = ctl(x=100, y=100)
        add_mon(c, x=350, y=100, health=5000, max_health=5000)  # 250 away, in the 200-300 band
        c.cluster_kite = True
        c.plan_cluster = lambda *a, **k: None
        c.kite_low_potion = True
        c.kite_targets.add("enc-1")
        shots = 0
        now = 0.0
        for _ in range(20):
            c.tickets.accept({"id": f"t{shots}"})
            kind, payload = c.next_combat_action(now)
            if kind == "attack":
                shots += 1
                now = c.tickets.busy_until_at + 0.01
            else:
                now += 0.2
        # 20 attacks * ~0.8s would be 16s; we should still get nearly one shot per cycle.
        self.assertGreaterEqual(shots, 18)

    def test_approaches_into_shoot_band_not_melee(self):
        c = ctl(x=100, y=100)
        add_mon(c, x=700, y=100, health=850)
        c.cluster_kite = False
        c.tickets.accept({"id": "t1"})
        kind, packet = c.next_combat_action(1.0)
        self.assertEqual(kind, "chase")
        if packet is not None:
            self.assertLess(packet["x"], 700 - KITE_RETREAT_DIST)


class RestMustLeaveTests(unittest.TestCase):
    def test_low_hp_does_not_keep_attacking(self):
        c = ctl(x=100, y=100)
        c.self_state.update({"health": 90, "maxHealth": 320})  # 28%
        add_mon(c, x=175, y=100, health=850)  # 75 away, the 13:26 bug setup
        c._own_hit_ids.add("enc-1")
        c.tickets.accept({"id": "t1"})
        kind, payload = c.next_combat_action(1.0)
        self.assertTrue(c.resting)
        self.assertIn(kind, {"rest", "wait"})
        self.assertNotEqual(kind, "attack")
        self.assertIn("回血", c.activity + c.status)
        self.assertFalse(c.attack_still_valid("enc-1"))
        c.tickets.accept({"id": "t2"})
        kind2, _ = c.next_combat_action(1.2)
        self.assertNotEqual(kind2, "attack")

    def test_only_cornered_after_failed_retreats(self):
        c = ctl(x=100, y=100)
        c.self_state.update({"health": 90, "maxHealth": 320})
        add_mon(c, x=150, y=100, health=850)
        c._own_hit_ids.add("enc-1")
        # Force move failures so we eventually allow a fight.
        c._move_to = lambda *a, **k: None
        kinds = []
        for i in range(REST_CORNERED_FAILS + 1):
            result = c.plan_rest(1.0 + i)
            kinds.append(result[0] if result is not None else "combat")
        self.assertIn("combat", kinds)
        self.assertTrue(any(k == "wait" for k in kinds[:-1]))

    def test_threshold_still_starts_below_35_percent(self):
        c = ctl()
        c.self_state.update({"health": int(320 * REST_LOW_RATIO) - 1, "maxHealth": 320})
        self.assertIsNotNone(c.plan_rest(1.0))
        self.assertTrue(c.resting)


if __name__ == "__main__":
    unittest.main()
