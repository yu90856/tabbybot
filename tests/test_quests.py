import unittest

from tabbybot.config import AppConfig, HuntConfig
from tabbybot.constants import PORTAL_CENTERS, TRAVEL_SYNC_WAIT_SEC
from tabbybot.hunt import HuntController
from tabbybot.protocol import Position
from tabbybot.quests import (
    REPLACE_UNAVAILABLE,
    SKIP_SPECIAL_MAP,
    extract_quests,
    format_delivery_prompt,
    format_quest_row,
    has_active_monster_quest,
    is_quest_limit_response,
    monster_matches_active_quest,
    needs_consumption,
    next_daily_step,
    pick_next_monster_quest,
    propose_delivery_items,
    quest_state,
    skip_reason,
    skipped_available,
)


def quest(**fields):
    base = {
        "id": 1,
        "titleZh": "測試任務",
        "objectiveType": "MONSTER",
        "mapId": 2,
        "progress": 0,
        "target": 10,
        "available": True,
        "accepted": False,
        "completed": False,
        "rewardExperience": 10,
        "rewardGold": 5,
        "rewardPotions": 1,
    }
    base.update(fields)
    return base


class QuestHelperTests(unittest.TestCase):
    def test_extract_from_nested_payloads(self):
        rows = [quest(id=3)]
        self.assertEqual(extract_quests({"quests": rows})[0]["id"], 3)
        self.assertEqual(extract_quests({"progression": {"quests": rows}})[0]["id"], 3)
        self.assertEqual(extract_quests({"progress": {"quests": rows}})[0]["id"], 3)
        self.assertEqual(extract_quests(rows)[0]["id"], 3)
        self.assertEqual(extract_quests({"ok": True}), [])

    def test_states_and_rows(self):
        self.assertEqual(quest_state(quest(available=True)), "可接取")
        self.assertEqual(quest_state(quest(accepted=True, available=False)), "進行中")
        self.assertEqual(quest_state(quest(completed=True, accepted=True, progress=10)), "已完成")
        self.assertEqual(quest_state(quest(available=False)), "未解鎖")
        row = format_quest_row(quest(titleZh="雪地清剿"))
        self.assertEqual(row["title"], "雪地清剿")
        self.assertEqual(row["progress_text"], "0/10")
        self.assertIn("經驗", row["rewards"])

    def test_pick_prefers_current_map_and_does_not_silently_skip_delivery(self):
        delivery = quest(id=2, titleZh="交付裝備", objectiveType="EQUIP_DELIVER", mapId=1)
        relic = quest(id=3, titleZh="遺跡清剿", mapId=5)
        treasure = quest(id=4, titleZh="秘寶清剿", mapId=7)
        snow = quest(id=5, titleZh="雪地清剿", mapId=2)
        town = quest(id=6, titleZh="鎮內清剿", mapId=1)
        quests = [delivery, relic, treasure, snow, town]
        self.assertTrue(needs_consumption(delivery))
        self.assertIsNone(skip_reason(delivery))
        self.assertEqual(skip_reason(relic), SKIP_SPECIAL_MAP)
        self.assertEqual(skip_reason(treasure), SKIP_SPECIAL_MAP)
        self.assertIsNone(skip_reason(snow))
        chosen = pick_next_monster_quest(quests, current_map=1)
        self.assertEqual(chosen["id"], 6)
        chosen = pick_next_monster_quest(quests, current_map=2)
        self.assertEqual(chosen["id"], 5)
        # Ticket maps are skipped; delivery is not silently skipped.
        self.assertEqual(len(skipped_available(quests)), 2)
        active = quest(id=9, accepted=True, available=False, progress=3)
        self.assertTrue(has_active_monster_quest([active, snow]))
        self.assertIsNone(pick_next_monster_quest([active, snow], current_map=2))

    def test_monster_filter_in_limited_mode(self):
        active = quest(id=1, accepted=True, available=False, mapId=2, progress=1, target=8)
        self.assertTrue(monster_matches_active_quest([active], {"mapId": 2}, limited=False))
        self.assertTrue(monster_matches_active_quest([active], {"mapId": 2}, limited=True))
        self.assertFalse(monster_matches_active_quest([active], {"mapId": 6}, limited=True))
        self.assertFalse(monster_matches_active_quest([], {"mapId": 2}, limited=True))

    def test_daily_limit_markers(self):
        self.assertTrue(is_quest_limit_response({"message": "今日任务已达上限"}))
        self.assertTrue(is_quest_limit_response("任務領取已滿"))
        self.assertTrue(is_quest_limit_response({"code": "daily_quest_limit"}))
        self.assertFalse(is_quest_limit_response({"message": "任務不存在"}))


class HuntQuestTests(unittest.TestCase):
    def setUp(self):
        self.controller = HuntController(AppConfig(hunt=HuntConfig(map_id=2)))
        self.controller.world_ready = True
        self.controller.position = Position(map_id=1, x=100, y=100, revision=3)
        self.controller.auto_quest = True
        self.controller.task_limited = True
        self.controller.quest_map_auto = True
        self.controller.daily_mode = True

    def test_snapshot_progress_updates_quests(self):
        self.controller.on_packet({
            "type": "snapshot",
            "self": {"health": 80, "maxHealth": 100},
            "progress": {"quests": [quest(id=11, accepted=True, available=False, progress=4, target=10)]},
        })
        self.assertTrue(has_active_monster_quest(self.controller.quests))
        self.assertEqual(self.controller.quest_map_target, 2)
        self.assertEqual(self.controller.quest_map_quest_id, 11)

    def test_travel_includes_quest_id(self):
        self.controller.update_quests([quest(id=21, accepted=True, available=False, mapId=2, progress=0, target=5)])
        portal = PORTAL_CENTERS[(1, 2)]
        self.controller.position = Position(map_id=1, x=portal[0], y=portal[1], revision=8)
        self.controller.next_combat_action(4.0)
        kind, packet = self.controller.next_combat_action(4.0 + TRAVEL_SYNC_WAIT_SEC + 0.05)
        self.assertEqual(kind, "travel")
        self.assertEqual(packet["map"], 2)
        self.assertEqual(packet["questId"], 21)
        self.assertEqual(packet["useTicket"], False)

    def test_limited_mode_waits_when_no_active_quest(self):
        self.controller.auto_farm = False
        self.controller.position = Position(map_id=2, x=100, y=100, revision=4)
        self.controller.update_quests([quest(id=1, completed=True, accepted=True, progress=10)])
        kind, packet = self.controller.next_combat_action(20.0)
        self.assertEqual(kind, "idle")
        self.assertIsNone(packet)

    def test_limited_mode_ignores_off_map_monsters(self):
        self.controller.position = Position(map_id=2, x=100, y=100, revision=4)
        self.controller.update_quests([quest(id=21, accepted=True, available=False, mapId=6, progress=0, target=5)])
        # Already on snowfield, but the quest is for map 6, so snowfield monsters are ignored.
        self.assertFalse(self.controller.monster_allowed({"mapId": 2, "x": 100, "y": 120}))
        self.assertTrue(self.controller.monster_allowed({"mapId": 6, "x": 100, "y": 120}))

    def test_skips_special_map_as_travel_target(self):
        self.controller.update_quests([quest(id=7, accepted=True, available=False, mapId=5, progress=0, target=5)])
        target, ident = self.controller.desired_map()
        self.assertIsNone(target)
        self.assertEqual(ident, 0)



class DeliveryPromptTests(unittest.TestCase):
    def test_propose_lowest_quality_and_skip_blocked(self):
        items = [
            {"playerItemId": 1, "nameZh": "史詩劍", "qualityCode": "L3", "equipmentLevel": 2},
            {"playerItemId": 2, "nameZh": "普通劍", "qualityCode": "L1", "equipmentLevel": 1},
            {"playerItemId": 3, "nameZh": "普通盾", "qualityCode": "COMMON", "equipmentLevel": 3},
            {"playerItemId": 4, "nameZh": "穿著中", "qualityCode": "L1", "equipped": True},
            {"playerItemId": 5, "nameZh": "上架中", "qualityCode": "L1", "listed": True},
        ]
        proposed = propose_delivery_items(items, 2)
        self.assertEqual([item["playerItemId"] for item in proposed], [2, 3])
        prompt = format_delivery_prompt(
            quest(id=31, titleZh="交付兩件裝備", objectiveType="EQUIP_DELIVER", target=2, accepted=True, available=False),
            preview={"quest": {"target": 2, "runNumber": 7}, "items": items},
            already_accepted=True,
        )
        self.assertEqual(prompt["title"], "交付兩件裝備")
        self.assertEqual(prompt["target"], 2)
        self.assertEqual(prompt["run_number"], 7)
        self.assertEqual(prompt["item_ids"], [2, 3])
        self.assertTrue(prompt["enough"])
        self.assertIn("普通劍", prompt["proposed"][0]["label"])
        self.assertFalse(prompt["replace_available"])
        self.assertIn("沒有放棄", REPLACE_UNAVAILABLE)

    def test_not_enough_items_does_not_propose_shortfall(self):
        items = [{"playerItemId": 8, "nameZh": "木棒", "qualityCode": "L1"}]
        prompt = format_delivery_prompt(
            quest(id=4, titleZh="交三件", objectiveType="ITEM", target=3, accepted=True, available=False),
            preview={"quest": {"target": 3, "runNumber": 1}, "items": items},
            already_accepted=True,
        )
        self.assertFalse(prompt["enough"])
        self.assertEqual(len(prompt["item_ids"]), 1)
        self.assertIn("不足", prompt["note"])

    def test_default_pauses_for_accepted_delivery(self):
        delivery = quest(id=2, titleZh="交付裝備", objectiveType="EQUIP_DELIVER", accepted=True, available=False)
        snow = quest(id=5, titleZh="雪地清剿", available=True)
        step = next_daily_step([delivery, snow], current_map=2, auto_replace=False)
        self.assertEqual(step["kind"], "delivery")
        self.assertEqual(step["quest"]["id"], 2)
        self.assertTrue(step["already_accepted"])
        self.assertFalse(step["replace_available"])

    def test_default_asks_after_monster_quests_are_gone(self):
        delivery = quest(id=2, titleZh="交付裝備", objectiveType="EQUIP_DELIVER", available=True)
        snow = quest(id=5, titleZh="雪地清剿", available=True, mapId=2)
        step = next_daily_step([delivery, snow], current_map=2, auto_replace=False)
        self.assertEqual(step["kind"], "hunt_accept")
        self.assertEqual(step["quest"]["id"], 5)
        step = next_daily_step([delivery], current_map=1, auto_replace=False)
        self.assertEqual(step["kind"], "delivery")
        self.assertFalse(step["already_accepted"])

    def test_auto_replace_skips_unaccepted_delivery_but_cannot_drop_accepted(self):
        delivery = quest(id=2, titleZh="交付裝備", objectiveType="EQUIP_DELIVER", available=True)
        snow = quest(id=5, titleZh="雪地清剿", available=True, mapId=2)
        step = next_daily_step([delivery, snow], current_map=2, auto_replace=True)
        self.assertEqual(step["kind"], "hunt_accept")
        self.assertEqual(step["quest"]["id"], 5)
        self.assertEqual(step["replaced"][0]["id"], 2)
        self.assertFalse(step["replace_available"])
        accepted = quest(id=2, titleZh="交付裝備", objectiveType="EQUIP_DELIVER", accepted=True, available=False)
        step = next_daily_step([accepted, snow], auto_replace=True)
        self.assertEqual(step["kind"], "delivery")
        self.assertTrue(step["already_accepted"])

    def test_skip_ids_avoid_reprompting_unaccepted_delivery(self):
        delivery = quest(id=2, titleZh="交付裝備", objectiveType="EQUIP_DELIVER", available=True)
        step = next_daily_step([delivery], skipped_ids={2})
        self.assertEqual(step["kind"], "done")


class HuntDeliveryPauseTests(unittest.TestCase):
    def test_pending_delivery_pauses_combat(self):
        controller = HuntController(AppConfig(hunt=HuntConfig(map_id=2)))
        controller.world_ready = True
        controller.position = Position(map_id=2, x=100, y=100, revision=4)
        controller.auto_combat = True
        controller.auto_farm = False
        controller.pending_delivery = {"title": "交付裝備", "quest_id": 2}
        kind, packet = controller.next_combat_action(20.0)
        self.assertEqual(kind, "idle")
        self.assertIsNone(packet)
        self.assertIn("確認", controller.status)


if __name__ == "__main__":
    unittest.main()
