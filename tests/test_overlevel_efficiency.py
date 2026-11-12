import tempfile
import unittest
from pathlib import Path

from tabbybot.config import AppConfig, HuntConfig
from tabbybot.efficiency import EfficiencyTracker, extract_experience, level_band
from tabbybot.hunt import HuntController
from tabbybot.protocol import Position


def ctl(klass="ARCHER"):
    c = HuntController(AppConfig(hunt=HuntConfig(map_id=2)), klass)
    c.world_ready = True
    c.position = Position(map_id=2, x=100, y=100, revision=1)
    c.auto_farm = True
    c.farm_target_level = 30
    c.player_id = 7
    c.self_state = {"health": 200, "maxHealth": 200, "healthPotions": 9, "potionCooldownMs": 0}
    c.plan_cluster = lambda *a, **k: None  # legacy single-target path; cluster mode has its own tests
    return c


def mon(c, enc, x, level, health=250):
    c.monsters.apply_snapshot({"delta": True, "deltaVersion": 2, "monsters": [
        {"encounterId": enc, "mapId": 2, "x": x, "y": 100, "health": health, "maxHealth": 250, "level": level}]})


class OverLevelTests(unittest.TestCase):
    def test_off_by_default_keeps_target_band(self):
        c = ctl()
        mon(c, "a30", 300, 30)
        mon(c, "b50", 200, 50)
        self.assertEqual([r[1] for r in c.candidates()], ["a30"])

    def test_on_targets_higher_band_and_kites(self):
        c = ctl()
        c.apply_combat_options({"overlevel_kite": True, "overlevel_max": 20})
        mon(c, "a30", 300, 30)
        mon(c, "b50", 250, 50)
        mon(c, "c40", 260, 40)
        mon(c, "d60", 120, 60)  # beyond +20
        self.assertEqual([r[1] for r in c.candidates()], ["c40", "b50"])
        self.assertIn("Lv40–50", c.level_filter_text)
        self.assertTrue(c.kite_active("c40"))

    def test_melee_never_overlevels(self):
        c = ctl("WARRIOR")
        c.apply_combat_options({"overlevel_kite": True})
        mon(c, "a30", 130, 30)
        mon(c, "b40", 120, 40)
        self.assertEqual([r[1] for r in c.candidates()], ["a30"])

    def test_abort_on_hp_loss_locks_band_and_falls_back(self):
        c = ctl()
        c.apply_combat_options({"overlevel_kite": True})
        mon(c, "a30", 360, 30)
        mon(c, "c40", 350, 40)
        c.tickets.accept({"id": "t1"})
        kind, payload = c.next_combat_action(1.0)
        self.assertEqual(payload[1][1]["encounterId"], "c40")
        c.self_state["health"] = 120  # lost 40%
        c.check_overlevel_abort(2.0)
        self.assertIn("c40", c.kite_failed)
        self.assertIn("生命掉了超過", c.overlevel_note)
        self.assertEqual([r[1] for r in c.candidates()], ["a30"])
        self.assertEqual(c.efficiency.rows["2:40"]["aborts"], 1)

    def test_abort_when_no_damage_for_20s(self):
        c = ctl()
        c.apply_combat_options({"overlevel_kite": True})
        mon(c, "c40", 350, 40)
        c.tickets.accept({"id": "t1"})
        c.next_combat_action(1.0)
        c.check_overlevel_abort(10.0)
        self.assertNotIn("c40", c.kite_failed)
        c.check_overlevel_abort(21.5)
        self.assertIn("20 秒沒有打出傷害", c.overlevel_note)


class EfficiencyTests(unittest.TestCase):
    def test_level_band(self):
        self.assertEqual(level_band(37), 30)
        self.assertEqual(level_band(5), 10)
        self.assertIsNone(level_band(None))

    def test_extract_experience(self):
        self.assertEqual(extract_experience({"progression": {"level": 24, "experience": 10500}}), (24, 10500))
        self.assertIsNone(extract_experience({"foo": 1}))

    def test_engaged_kill_records_time_potions_and_exp(self):
        c = ctl()
        mon(c, "m", 350, 30, health=60)
        c.tickets.accept({"id": "t1"})
        import time
        c.next_combat_action(time.monotonic())
        c.efficiency.observe_experience(24, 1000, time.monotonic())
        c.self_state["healthPotions"] = 8
        c.on_packet({"type": "hit", "attacker": 7, "hit": {"monster": "m", "damage": 60}})
        row = c.efficiency.rows["2:30"]
        self.assertEqual(row["kills"], 1)
        self.assertEqual(row["potions"], 1)
        self.assertEqual(row["timed_kills"], 1)
        c.efficiency.observe_experience(24, 1050, c.efficiency._pending[0][0] + 1)
        self.assertEqual((row["exp"], row["exp_kills"]), (50, 1))

    def test_kill_without_own_hit_not_counted(self):
        c = ctl()
        mon(c, "m", 350, 30)
        c.tickets.accept({"id": "t1"})
        c.next_combat_action(1.0)
        mon(c, "m", 200, 30, health=0)  # someone else finished it
        self.assertEqual(c.efficiency.rows, {})

    def test_mixed_bands_exp_not_split(self):
        t = EfficiencyTracker(None)
        t.observe_experience(10, 100, 0.0)
        t.record_kill(2, 30, 10, 0, 1.0)
        t.record_kill(2, 40, 12, 0, 2.0)
        t.observe_experience(10, 300, 3.0)
        self.assertEqual(t.rows["2:30"]["exp_kills"], 0)
        self.assertEqual(t.rows["2:40"]["exp_kills"], 0)

    def test_level_up_skips_exp(self):
        t = EfficiencyTracker(None)
        t.observe_experience(10, 900, 0.0)
        t.record_kill(2, 30, 10, 0, 1.0)
        t.observe_experience(11, 20, 2.0)
        self.assertEqual(t.rows["2:30"]["exp_kills"], 0)

    def test_table_sorting_best_and_samples_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "efficiency.json"
            t = EfficiencyTracker(path)
            now = 0.0
            exp = 0
            t.observe_experience(10, exp, now)
            for _ in range(5):  # Lv30: 50 exp / 10 s => 300/min
                now += 20
                t.record_kill(6, 30, 10.0, 0, now)
                exp += 50
                t.observe_experience(10, exp, now + 1)
            for _ in range(2):  # Lv40: 100 exp / 15 s => 400/min but only 2 samples
                now += 20
                t.record_kill(6, 40, 15.0, 1, now)
                exp += 100
                t.observe_experience(10, exp, now + 1)
            table = t.table()
            first = table["rows"][0]
            self.assertEqual(first["band"], 40)
            self.assertFalse(first["enough"])
            self.assertFalse(first["best"])
            lv30 = [r for r in table["rows"] if r["band"] == 30][0]
            self.assertTrue(lv30["best"])
            self.assertEqual((lv30["avg_exp"], lv30["avg_secs"], lv30["exp_per_min"]), (50.0, 10.0, 300.0))
            t.save(now, force=True)
            again = EfficiencyTracker(path)
            self.assertEqual(again.rows["6:30"]["kills"], 5)
            again.reset()
            self.assertEqual(EfficiencyTracker(path).rows, {})


if __name__ == "__main__":
    unittest.main()
