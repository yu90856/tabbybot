import unittest
from datetime import date, timedelta
from unittest import mock

from tabbybot.config import AppConfig, HuntConfig
from tabbybot.hunt import HuntController
from tabbybot.quests import QUEST_IDLE_RECHECK_SEC


class QuestIdleTests(unittest.TestCase):
    def make(self):
        c = HuntController(AppConfig(hunt=HuntConfig(map_id=2)), "ARCHER")
        c.auto_quest = True
        c.daily_mode = True
        c.world_ready = True
        c._claim_token = "x"
        c.quests = []
        return c

    def test_no_quest_logged_once_and_polling_slows(self):
        c = self.make()
        c.status = "攻擊中"
        with self.assertLogs("tabbybot.hunt", level="INFO") as cm:
            for _ in range(5):
                c._accept_result = {"none": True, "quests": []}
                c.apply_quest_results()
        lines = [l for l in cm.output if "沒有可自動接取" in l]
        self.assertEqual(len(lines), 1)
        self.assertIn("練功等級", lines[0])
        self.assertEqual(c.status, "攻擊中")  # no status takeover
        now = c._quest_idle_at
        c.quest_refresh_at = now
        c.quest_accept_checked_at = now
        started = []
        with mock.patch("tabbybot.hunt.threading.Thread") as th:
            th.return_value.start = lambda: started.append(1)
            c.maybe_accept_quest(now + 60)
            c.maybe_refresh_quests(now + 60)
            self.assertEqual(started, [])
            c.maybe_accept_quest(now + QUEST_IDLE_RECHECK_SEC + 1)
            self.assertEqual(len(started), 1)

    def test_new_day_rechecks_immediately(self):
        c = self.make()
        c._accept_result = {"none": True, "quests": []}
        c.apply_quest_results()
        c._quest_idle_date = date.today() - timedelta(days=1)
        self.assertFalse(c.quest_idle(c._quest_idle_at + 10))

    def test_grinds_farm_map_when_no_quest(self):
        c = self.make()
        c.auto_farm = True
        c._accept_result = {"none": True, "quests": []}
        c.apply_quest_results()
        self.assertTrue(c.should_auto_farm())


if __name__ == "__main__":
    unittest.main()
