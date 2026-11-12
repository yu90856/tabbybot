import unittest

from tabbybot.config import AppConfig, HuntConfig
from tabbybot.constants import PORTAL_CENTERS, TRAVEL_SYNC_WAIT_SEC
from tabbybot.hunt import HuntController, find_travel_hops, travel_reject_text
from tabbybot.protocol import Position


class RouteTests(unittest.TestCase):
    def test_adjacent_is_single_hop(self):
        self.assertEqual(find_travel_hops(1, 2), [2])
        self.assertEqual(find_travel_hops(2, 1), [1])

    def test_multihop_goes_via_town(self):
        self.assertEqual(find_travel_hops(1, 10), [2, 10])
        self.assertEqual(find_travel_hops(10, 6), [2, 1, 6])
        self.assertEqual(find_travel_hops(8, 9), [6, 1, 4, 9])

    def test_route_skips_special_maps_as_intermediate(self):
        hops = find_travel_hops(8, 9)
        self.assertNotIn(5, hops)
        self.assertNotIn(7, hops)
        self.assertEqual(find_travel_hops(1, 1), [])
        self.assertEqual(find_travel_hops(3, 10), [6, 1, 2, 10])

    def test_reject_reason_text_includes_server_code(self):
        text = travel_reject_text("distance")
        self.assertIn("距離不足", text)
        self.assertIn("distance", text)
        self.assertIn("4", travel_reject_text("distance", 4))


class HuntPlanningTests(unittest.TestCase):
    def setUp(self):
        self.controller = HuntController(AppConfig(hunt=HuntConfig(map_id=2)))
        self.controller.world_ready = True
        self.controller.position = Position(map_id=2, x=100, y=100, revision=4)

    def test_travel_walks_to_portal_instead_of_sending_immediately(self):
        self.controller.position = Position(map_id=1, x=1504.0, y=352.0, revision=2)
        kind, packet = self.controller.next_combat_action(10.0)
        # First tick after starting a route uses dt=0, so a later tick actually steps.
        kind, packet = self.controller.next_combat_action(10.1)
        self.assertEqual(kind, "portal")
        self.assertEqual(packet["action"], "sync")
        self.assertEqual(packet["animation"], "run")
        self.assertEqual(packet["map"], 1)
        self.assertNotEqual((packet["x"], packet["y"]), (1504.0, 352.0))
        portal = PORTAL_CENTERS[(1, 2)]
        # Moving toward the snowfield gate, not already sending travel.
        self.assertLess(
            ((packet["x"] - portal[0]) ** 2 + (packet["y"] - portal[1]) ** 2) ** 0.5,
            ((1504.0 - portal[0]) ** 2 + (352.0 - portal[1]) ** 2) ** 0.5,
        )
        self.assertEqual(self.controller.pending_travel, 2)
        self.assertEqual(self.controller.travel_phase, "walk")

    def test_travel_packet_fields_after_arriving_at_portal(self):
        portal = PORTAL_CENTERS[(1, 2)]
        self.controller.position = Position(map_id=1, x=portal[0], y=portal[1], revision=7)
        kind, packet = self.controller.next_combat_action(5.0)
        self.assertEqual(kind, "portal")
        self.assertEqual(packet["action"], "sync")
        self.assertEqual(packet["animation"], "idle")
        kind, packet = self.controller.next_combat_action(5.0 + TRAVEL_SYNC_WAIT_SEC / 2)
        self.assertTrue(packet is None or packet.get("action") != "travel")
        kind, packet = self.controller.next_combat_action(5.0 + TRAVEL_SYNC_WAIT_SEC + 0.05)
        self.assertEqual(kind, "travel")
        self.assertEqual(packet["action"], "travel")
        self.assertEqual(packet["map"], 2)
        self.assertEqual(packet["questId"], 0)
        self.assertEqual(packet["useTicket"], False)
        self.assertEqual(packet["movementRevision"], 7)
        self.assertEqual(
            set(packet),
            {"action", "map", "questId", "useTicket", "movementRevision"},
        )

    def test_multihop_starts_with_first_adjacent_portal(self):
        self.controller.config.hunt.map_id = 10
        self.controller.position = Position(map_id=1, x=1504.0, y=352.0, revision=3)
        self.controller.begin_travel_to(10, 20.0)
        self.assertEqual(self.controller.pending_travel, 2)
        self.assertEqual(self.controller.pending_portal, PORTAL_CENTERS[(1, 2)])
        self.assertEqual(self.controller.travel_phase, "walk")
        self.assertTrue(self.controller.travel_route)

    def test_distance_reject_does_not_resend_travel_immediately(self):
        portal = PORTAL_CENTERS[(1, 2)]
        self.controller.position = Position(map_id=1, x=portal[0], y=portal[1], revision=7)
        self.controller.next_combat_action(5.0)
        self.controller.next_combat_action(5.0 + TRAVEL_SYNC_WAIT_SEC + 0.05)
        self.controller._on_travel_result({"ok": False, "reason": "distance"})
        self.assertIn("距離不足", self.controller.status)
        self.assertIn("distance", self.controller.status)
        self.assertIsNone(self.controller.travel_phase)
        kind, packet = self.controller.next_combat_action(5.0 + TRAVEL_SYNC_WAIT_SEC + 0.1)
        self.assertTrue(packet is None or packet.get("action") != "travel")

    def test_attack_when_in_range_with_ticket(self):
        self.controller.tickets.accept({"id": "ticket-9", "critical": False})
        self.controller.monsters.apply_snapshot({
            "delta": False,
            "monsters": [{"encounterId": "enc-1", "mapId": 2, "x": 120, "y": 100, "health": 40}],
        })
        kind, payload = self.controller.next_combat_action(0)
        self.assertEqual(kind, "attack")
        self.assertEqual(payload[0][1]["animation"], "attack")
        self.assertEqual(payload[1][1]["action"], "monsterAttack")
        self.assertEqual(payload[1][1]["attackTicket"], "ticket-9")

    def test_patrol_when_no_targets(self):
        kind, _packet = self.controller.next_combat_action(0)
        self.assertEqual(kind, "patrol")



class FarmAndMinimapHuntTests(unittest.TestCase):
    def setUp(self):
        self.controller = HuntController(AppConfig(hunt=HuntConfig(map_id=2)))
        self.controller.world_ready = True
        self.controller.auto_farm = True
        self.controller.farm_target_level = 30
        self.controller.daily_mode = True
        self.controller.auto_quest = True
        self.controller.task_limited = True
        self.controller.auto_combat = True
        self.controller.auto_return = True
        self.controller.position = Position(map_id=1, x=544.0, y=352.0, revision=3)

    def test_map_payload_includes_units_without_nav(self):
        self.controller.position = Position(map_id=4, x=8479.67, y=2363.06, revision=8)
        self.controller.monsters.apply_snapshot({
            "delta": False,
            "monsters": [
                {"encounterId": "alive-1", "mapId": 4, "x": 8356.65, "y": 2278.10, "health": 183, "level": 30, "name": "skeleton"},
                {"encounterId": "dead-1", "mapId": 4, "x": 8592.86, "y": 2583.21, "health": 0, "name": "skeleton"},
            ],
        })
        self.controller.peers.apply_snapshot({
            "peers": [{"id": "p1", "playerName": "路人甲", "x": 8400, "y": 2300, "mapId": 4}],
        }, fallback_map=4, now=1.0)
        payload = self.controller.map_payload()
        self.assertEqual(payload["map_id"], 4)
        self.assertIsNotNone(payload["bounds"])
        self.assertLess(payload["bounds"]["x0"], min(8479.67, 8356.65))
        self.assertGreater(payload["bounds"]["x1"], max(8479.67, 8356.65))
        alive = [row for row in payload["monsters"] if row["alive"]]
        self.assertEqual(alive[0]["level"], 30)
        self.assertEqual(payload["peers"][0]["name"], "路人甲")

    def test_farms_while_waiting_for_delivery_and_keeps_prompt(self):
        self.controller.pending_delivery = {"title": "交付裝備", "quest_id": 9}
        kind, packet = self.controller.next_combat_action(20.0)
        kind, packet = self.controller.next_combat_action(20.1)
        self.assertIsNotNone(self.controller.pending_delivery)
        self.assertNotEqual(kind, "idle")
        self.assertEqual(self.controller.desired_map()[0], 2)
        self.assertIn("等待你確認", self.controller.status)

    def test_active_quest_has_priority_over_farm(self):
        self.controller.quest_map_auto = True
        self.controller.quests = [{
            "id": 505, "titleZh": "西部荒野讨伐", "objectiveType": "MONSTER",
            "mapId": 4, "progress": 29, "target": 90, "accepted": True, "completed": False, "available": False,
        }]
        self.controller.refresh_quest_map_target()
        self.assertEqual(self.controller.desired_map()[0], 4)
        self.assertFalse(self.controller.should_auto_farm())

    def test_prefers_near_level_monsters_when_farming(self):
        self.controller.position = Position(map_id=4, x=100, y=100, revision=2)
        self.controller.level_catalog[4] = [28, 30, 32]
        self.controller.monsters.apply_snapshot({
            "delta": False,
            "monsters": [
                {"encounterId": "far-ok", "mapId": 4, "x": 400, "y": 100, "health": 40, "level": 30},
                {"encounterId": "near-low", "mapId": 4, "x": 120, "y": 100, "health": 40, "level": 8},
            ],
        })
        # daily done / no active quest -> farming
        chosen = self.controller.candidates()
        self.assertEqual(chosen[0][1], "far-ok")

    def test_manual_move_and_user_packets(self):
        self.controller.begin_manual_move(600, 400)
        self.assertEqual(self.controller.manual_goal, (600.0, 400.0))
        kind, packet = self.controller.next_combat_action(30.0)
        self.assertEqual(kind, "manual")
        self.assertEqual(packet["action"], "sync")
        self.controller.self_state["healthPotions"] = 3
        self.controller.request_use_consumable("health")
        self.assertEqual(self.controller.pop_user_packets()[0]["action"], "useHealthPotion")
        self.controller.request_chat("大家好")
        chat = self.controller.pop_user_packets()[0]
        self.assertEqual(chat, {"action": "chat", "channel": "world", "message": "大家好"})
        self.controller.self_state["healthPotions"] = 0
        with self.assertRaises(RuntimeError):
            self.controller.request_use_consumable("health")


if __name__ == "__main__":

    unittest.main()
