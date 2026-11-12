import math
import unittest

from tabbybot.constants import (
    DEFAULT_INCOMING_DPS,
    DEFAULT_PLAYER_DPS,
    DEFAULT_REGEN_PER_SEC,
    KITE_PREDICT_HORIZON_SEC,
    KITE_RETREAT_DIST,
    KITE_SHOOT_MIN,
    MOVE_TICK_SEC,
    MOVEMENT_SPEED,
    MOVE_SPEED_MARGIN,
)
from tabbybot.hunt import REST_CORNERED_FAILS, REST_LOW_RATIO
import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).parent))
from test_selfheal_kite import add_mon, ctl


class RealKiteTests(unittest.TestCase):
    def test_moves_first_when_inside_180(self):
        c = ctl(x=100, y=100)
        add_mon(c, x=170, y=100, health=1150, max_health=1150, level=50)
        c.cluster_kite = True
        c.plan_cluster = lambda *a, **k: None
        c.tickets.accept({"id": "t1"})
        kind, packet = c.next_combat_action(1.0)
        self.assertEqual(kind, "chase")
        self.assertLess(packet["x"], 100)
        self.assertGreaterEqual(c.last_kite_distance, 60)
        self.assertIn("預測", c.kite_note)

    def test_shoots_only_in_200_300_band(self):
        c = ctl(x=100, y=100)
        add_mon(c, x=350, y=100, health=1150, max_health=1150, level=50)  # 250 away
        c.cluster_kite = True
        c.plan_cluster = lambda *a, **k: None
        c.tickets.accept({"id": "t1"})
        kind, payload = c.next_combat_action(1.0)
        self.assertEqual(kind, "attack")
        self.assertEqual(payload[1][1]["encounterId"], "enc-1")

    def test_predicted_close_moves_even_if_current_is_200(self):
        c = ctl(x=100, y=100)
        add_mon(c, x=300, y=100, health=1150, max_health=1150, level=50)  # 200 now
        # Monster is sprinting toward us; next 0.4s will put it inside 180.
        c._mon_seen["enc-1"] = (0.5, 360.0, 100.0)  # moved 60 in 0.5s → 120 u/s toward us
        c.cluster_kite = True
        c.plan_cluster = lambda *a, **k: None
        c.tickets.accept({"id": "t1"})
        kind, packet = c.next_combat_action(1.0)
        pred = 200 - 120 * KITE_PREDICT_HORIZON_SEC
        self.assertLess(pred, KITE_RETREAT_DIST)
        self.assertEqual(kind, "chase")

    def test_step_respects_server_speed_limit(self):
        c = ctl(x=100, y=100)
        add_mon(c, x=170, y=100, health=1150, max_health=1150)
        c.cluster_kite = True
        c.plan_cluster = lambda *a, **k: None
        import time as _time
        now = _time.monotonic()
        c._last_move_sent_at = now - MOVE_TICK_SEC
        c._kite_last_at = now - MOVE_TICK_SEC
        c.tickets.accept({"id": "t1"})
        kind, packet = c.next_combat_action(now)
        self.assertEqual(kind, "chase")
        travelled = math.hypot(packet["x"] - 100, packet["y"] - 100)
        allowed = MOVEMENT_SPEED * MOVE_TICK_SEC * MOVE_SPEED_MARGIN + 0.6
        self.assertLessEqual(travelled, allowed)
        self.assertGreater(travelled, 1.0)

    def test_rest_cancels_further_attacks(self):
        c = ctl(x=100, y=100)
        c.self_state.update({"health": 90, "maxHealth": 320})
        add_mon(c, x=175, y=100, health=11500, max_health=11500, elite=True)
        c._own_hit_ids.add("enc-1")
        c.tickets.accept({"id": "t1"})
        gen_before = c._attack_gen
        kind, _ = c.next_combat_action(1.0)
        self.assertTrue(c.resting)
        self.assertGreater(c._attack_gen, gen_before)
        self.assertFalse(c.attack_still_valid("enc-1"))
        c.tickets.accept({"id": "t2"})
        self.assertNotEqual(c.next_combat_action(1.2)[0], "attack")
        # Only after several failed retreats with a mob in the face may we fight.
        c._move_to = lambda *a, **k: None
        for i in range(REST_CORNERED_FAILS):
            c.plan_rest(2.0 + i)
        self.assertEqual(c._rest_retreat_fails, REST_CORNERED_FAILS)
        # still resting, attack_still_valid becomes true only once cornered
        self.assertTrue(c.attack_still_valid("enc-1"))


class EliteGateTests(unittest.TestCase):
    def test_skips_unwinnable_elite_without_potions(self):
        c = ctl(x=100, y=100)
        c.self_state.update({"health": 214, "maxHealth": 328, "healthPotions": 0})
        c.auto_potion = False
        add_mon(c, "elite", x=350, y=100, health=11500, max_health=11500, level=50, elite=True)
        # Defaults: 115s × 22 incoming = 2530 > 214 + 5×115
        self.assertFalse(c.elite_fight_safe(c.monsters.monsters["elite"]))
        with self.assertLogs("tabbybot.hunt", level="INFO") as cm:
            rows = c.candidates()
        self.assertEqual(rows, [])
        self.assertIn("跳過精英（不喝水打不完）", c.elite_note)
        self.assertTrue(any("跳過精英（不喝水打不完）" in line for line in cm.output))

    def test_allows_elite_when_estimate_is_safe(self):
        c = ctl(x=100, y=100)
        c.self_state.update({"health": 328, "maxHealth": 328, "healthPotions": 0})
        c.auto_potion = False
        add_mon(c, "elite", x=350, y=100, health=400, max_health=400, level=40, elite=True)
        # 4s × 22 = 88 < 328 + 20
        self.assertTrue(c.elite_fight_safe(c.monsters.monsters["elite"]))
        self.assertEqual([r[1] for r in c.candidates()], ["elite"])

    def test_formula_uses_measured_rates(self):
        c = ctl()
        now = 10.0
        c._dmg_dealt_events = [(now - 5, 500)]  # 100 dps over 5s when evaluated at 10 with span 5? 
        # _event_rate: span = now - first = 5, total 500 → 100 dps if we pass now=10
        # actually measured_player_dps uses time.monotonic by default. Seed and call with explicit now.
        self.assertEqual(c._event_rate(c._dmg_dealt_events, now, default=0), 100.0)
        c._dmg_taken_events = [(now - 5, 110)]
        self.assertAlmostEqual(c._event_rate(c._dmg_taken_events, now, default=0), 22.0)
        self.assertGreater(DEFAULT_PLAYER_DPS, 0)
        self.assertGreater(DEFAULT_INCOMING_DPS, 0)
        self.assertGreater(DEFAULT_REGEN_PER_SEC, 0)
        self.assertGreaterEqual(REST_LOW_RATIO, 0.3)


if __name__ == "__main__":
    unittest.main()
