import json
import tempfile
import unittest
from pathlib import Path

from tabbybot.config import AppConfig, HuntConfig
from tabbybot.constants import TICKET_STALL_SEC
from tabbybot.hunt import HuntController, token_player_id
from tabbybot.keychain import MemoryKeychain
from tabbybot.notifier import WebhookError, WebhookNotifier, validate_webhook_url
from tabbybot.protocol import Position
from tabbybot.watchdog import StuckWatchdog, normalize_status


def make_controller(klass="ARCHER"):
    c = HuntController(AppConfig(hunt=HuntConfig(map_id=2)), klass)
    c.world_ready = True
    c.position = Position(map_id=2, x=100, y=100, revision=4)
    c.auto_farm = False
    c.plan_cluster = lambda *a, **k: None  # legacy single-target path; cluster mode has its own tests
    return c


def add_monster(c, enc="enc-1", x=350, y=100, health=250):
    c.monsters.apply_snapshot({"delta": True, "deltaVersion": 2,
                               "monsters": [{"encounterId": enc, "mapId": 2, "x": x, "y": y, "health": health}]})


class TicketTests(unittest.TestCase):
    def test_non_invalid_error_releases_ticket(self):
        c = make_controller()
        add_monster(c)
        c.tickets.accept({"id": "t1"})
        kind, _ = c.next_combat_action(0)
        self.assertEqual(kind, "attack")
        self.assertIsNone(c.tickets.available(100))
        c.on_packet({"type": "error", "action": "monsterAttack", "code": "invalid_ticket"})
        self.assertIsNotNone(c.tickets.available(100))
        self.assertEqual(c.attack_errors, {"invalid_ticket": 1})

    def test_invalid_target_locks_target(self):
        c = make_controller()
        add_monster(c)
        c.tickets.accept({"id": "t1"})
        c.next_combat_action(0)
        c.on_packet({"type": "error", "action": "monsterAttack", "code": "invalid_target"})
        self.assertTrue(c.tickets.is_invalid("enc-1"))

    def test_stalled_ticket_triggers_resync(self):
        c = make_controller()
        add_monster(c)
        c.tickets.accept({"id": "t1"})
        c.next_combat_action(0)
        start = c.tickets.busy_until_at + 0.01
        kind, _ = c.next_combat_action(start)
        self.assertEqual(kind, "wait_ticket")
        self.assertIn("等伺服器發新的", c.wait_reason)
        kind, _ = c.next_combat_action(start + TICKET_STALL_SEC + 0.1)
        self.assertEqual(kind, "resync")
        self.assertEqual(c.ticket_resyncs, 1)
        # the reservation was released so the bot can retry
        self.assertIsNotNone(c.tickets.available(start + TICKET_STALL_SEC + 0.2))

    def test_walks_toward_target_while_waiting_for_ticket(self):
        c = make_controller()
        add_monster(c, x=350, y=100)  # 250 away: already in the shoot band, wait for a ticket
        kind, packet = c.next_combat_action(1.0)
        self.assertEqual(kind, "wait_ticket")
        add_monster(c, x=500, y=100)  # 400 away, outside reach: walk in
        kind, packet = c.next_combat_action(1.1)
        self.assertEqual(kind, "chase")
        self.assertEqual(packet["action"], "sync")
        self.assertGreater(packet["x"], 100)

    def test_new_ticket_resets_wait(self):
        c = make_controller()
        add_monster(c)
        c.next_combat_action(0)
        self.assertIsNotNone(c.ticket_wait_since)
        c.on_packet({"type": "attack_ready", "nextAttack": {"id": "t2"}})
        self.assertIsNone(c.ticket_wait_since)
        self.assertEqual(c.tickets_received, 1)

    def test_sticks_to_same_target(self):
        c = make_controller()
        add_monster(c, "a", x=350)
        add_monster(c, "b", x=400)
        c.tickets.accept({"id": "t1"})
        c.next_combat_action(0)
        self.assertEqual(c.attack_target, "a")
        add_monster(c, "b", x=320)  # b now nearer but still in the shoot band
        c.tickets.accept({"id": "t2"})
        kind, payload = c.next_combat_action(c.tickets.busy_until_at + 0.01)
        self.assertEqual(payload[1][1]["encounterId"], "a")

    def test_only_own_hits_counted_and_reduce_health(self):
        c = make_controller()
        c.player_id = 7
        add_monster(c, health=100)
        c.on_packet({"type": "hit", "attacker": 9, "hit": {"monster": "enc-1", "damage": 50}})
        self.assertEqual(c.hits, 0)
        c.on_packet({"type": "hit", "attacker": 7, "hit": {"monster": "enc-1", "damage": 60}})
        self.assertEqual((c.hits, c.damage), (1, 60))
        self.assertEqual(c.monsters.monsters["enc-1"]["health"], 40)

    def test_attack_validity_at_impact(self):
        c = make_controller()
        add_monster(c, health=0)
        self.assertFalse(c.attack_still_valid("enc-1"))
        add_monster(c, "far", x=1000)
        self.assertFalse(c.attack_still_valid("far"))
        add_monster(c, "ok", x=200)
        self.assertTrue(c.attack_still_valid("ok"))

    def test_token_player_id(self):
        import base64
        body = base64.urlsafe_b64encode(json.dumps({"playerId": 42}).encode()).decode().rstrip("=")
        self.assertEqual(token_player_id(f"h.{body}.s"), 42)
        self.assertIsNone(token_player_id("garbage"))


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "alerts.log"
        self.notes = []
        self.hooks = []
        self.dog = StuckWatchdog(notify=lambda t, m: self.notes.append(m), alerts_path=self.path,
                                 hook=self.hooks.append)
        self.c = make_controller()

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_move_drops_then_relogs(self):
        self.assertIsNone(self.dog.observe(self.c, "wait_ticket", 0))
        self.assertEqual(self.dog.observe(self.c, "wait_ticket", 121), "drop")
        self.assertEqual(self.dog.alert["level"], "drop")
        self.assertEqual(len(self.notes), 1)
        self.assertIn("卡住警報", self.path.read_text("utf-8"))
        self.assertEqual(self.hooks[0]["recovered"], True)
        self.assertEqual(self.hooks[0]["map"], "北部雪地")
        self.assertIsNone(self.dog.observe(self.c, "wait_ticket", 200))
        self.assertEqual(self.dog.observe(self.c, "wait_ticket", 242), "relogin")
        self.assertEqual(self.dog.alert["level"], "relogin")
        self.assertTrue(self.hooks[-1]["relogin"])
        self.assertFalse(self.hooks[-1]["stopped"])

    def test_progress_after_recovery_clears_alert(self):
        self.dog.observe(self.c, "attack", 0)
        self.dog.observe(self.c, "attack", 121)
        self.c.self_state["experience"] = 100
        self.dog.observe(self.c, "attack", 130)
        self.assertIsNone(self.dog.alert)
        self.assertIn("恢復正常", self.path.read_text("utf-8"))

    def test_exp_keeps_it_quiet(self):
        for step in range(0, 600, 30):
            self.c.self_state["experience"] = step
            self.c.position.x = 100 + step  # also moving
            self.assertIsNone(self.dog.observe(self.c, "attack", float(step)))
        self.assertIsNone(self.dog.alert)

    def test_idle_states_not_counted(self):
        for t in range(0, 600, 30):
            self.assertIsNone(self.dog.observe(self.c, "idle", t))

    def test_normalize_status(self):
        self.assertEqual(normalize_status("攻擊 f337f210"), normalize_status("攻擊 4c310eff"))


class NotifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sent = []
        self.now = [0.0]
        self.store = MemoryKeychain()
        self.n = WebhookNotifier(self.store, settings_path=Path(self.tmp.name) / "webhook.json",
                                 sender=lambda url, body, headers, t: self.sent.append((url, json.loads(body), headers)) or 200,
                                 clock=lambda: self.now[0])

    def tearDown(self):
        self.tmp.cleanup()

    def test_https_only(self):
        with self.assertRaises(WebhookError):
            validate_webhook_url("http://example.com/hook")
        with self.assertRaises(WebhookError):
            validate_webhook_url("https://u:p@example.com/hook")
        self.assertEqual(validate_webhook_url(""), "")

    def test_save_and_send_with_bearer_and_debounce(self):
        self.n.save("https://hooks.example.com/r/1", "sekret")
        self.assertEqual(self.n.settings(), {"url": "https://hooks.example.com/r/1", "has_key": True, "last_result": ""})
        self.assertNotIn("sekret", (Path(self.tmp.name) / "webhook.json").read_text())
        self.n.send_alert({"reason": "x"}, background=False)
        url, body, headers = self.sent[0]
        self.assertEqual(headers["Authorization"], "Bearer sekret")
        self.assertEqual(body["reason"], "x")
        self.now[0] = 300
        self.assertIn("略過", self.n.send_alert({"reason": "y"}, background=False))
        self.now[0] = 601
        self.n.send_alert({"reason": "z"}, background=False)
        self.assertEqual(len(self.sent), 2)

    def test_test_button_bypasses_debounce_and_needs_url(self):
        self.assertEqual(self.n.send_test(), "尚未設定通知網址")
        self.n.save("https://hooks.example.com/r/1")
        self.n.send_alert({"reason": "x"}, background=False)
        self.assertIn("HTTP 200", self.n.send_test())
        self.assertTrue(self.sent[-1][1]["test"])
        for key in ("reason", "map", "x", "y", "status", "kills", "exp", "recovered", "stopped", "time"):
            self.assertIn(key, self.sent[-1][1])


if __name__ == "__main__":
    unittest.main()


class KillCountTests(unittest.TestCase):
    def test_quest_progress_counts_kills_and_feeds_watchdog(self):
        c = make_controller()
        q = {"id": 5, "objectiveType": "MONSTER", "progress": 10, "target": 80, "accepted": True}
        c.update_quests([q], request_next=False)
        c.update_quests([dict(q, progress=13)], request_next=False)
        self.assertEqual(c.quest_kills, 3)
        self.assertEqual(c.map_payload()["kills"], 3)

    def test_lowercase_reward_key_counts(self):
        c = make_controller()
        c.on_packet({"type": "snapshot", "delta": True, "rewards": [{"encounterId": "e1"}]})
        self.assertEqual(c.kills, 1)
        c.on_packet({"type": "reward", "rewards": [{"EncounterID": "e2"}]})
        self.assertEqual(c.kills, 2)


class QuestFirstAndLevelTests(unittest.TestCase):
    def test_accepted_quest_wins_even_with_toggles_off(self):
        c = make_controller()
        c.auto_farm = True
        c.quest_map_auto = False
        c.auto_quest = False
        c.update_quests([{"id": 6, "objectiveType": "MONSTER", "mapId": 6, "progress": 57,
                          "target": 80, "accepted": True, "completed": False}], request_next=False)
        self.assertEqual(c.desired_map()[0], 6)
        add_monster(c, "here", x=150)  # on map 2, not the quest map
        self.assertEqual(c.candidates(), [])

    def test_level_filter_skips_lower_when_target_visible(self):
        c = make_controller()
        c.auto_farm = True
        c.farm_target_level = 30
        c.monsters.apply_snapshot({"delta": False, "monsters": [
            {"encounterId": "lv10", "mapId": 2, "x": 110, "y": 100, "health": 50, "level": 10},
            {"encounterId": "lv30", "mapId": 2, "x": 900, "y": 100, "health": 50, "level": 30},
        ]})
        ids = [row[1] for row in c.candidates()]
        self.assertEqual(ids, ["lv30"])
        self.assertIn("只打 Lv30", c.level_filter_text)
        c.tickets.accept({"id": "t"})
        kind, _ = c.next_combat_action(1.0)
        self.assertEqual(kind, "chase")  # chases the Lv30 instead of hitting the adjacent Lv10

    def test_level_filter_falls_back_to_nearest_level(self):
        c = make_controller()
        c.auto_farm = True
        c.farm_target_level = 30
        c.monsters.apply_snapshot({"delta": False, "monsters": [
            {"encounterId": "lv10", "mapId": 2, "x": 110, "y": 100, "health": 50, "level": 10},
            {"encounterId": "lv20", "mapId": 2, "x": 300, "y": 100, "health": 50, "level": 20},
        ]})
        # 練功等級 is exact now: no fallback to other levels, keep patrolling for Lv30.
        self.assertEqual([row[1] for row in c.candidates()], [])
        self.assertIn("附近沒有 Lv30", c.level_filter_text)
        c.monsters.apply_snapshot({"delta": True, "monsters": [
            {"encounterId": "lv30", "mapId": 2, "x": 500, "y": 100, "health": 50, "level": 30}]})
        self.assertEqual([row[1] for row in c.candidates()], ["lv30"])


class OwnKillAndDamageProgressTests(unittest.TestCase):
    def test_own_hit_death_counts_as_kill_without_rewards(self):
        c = make_controller()
        c.player_id = 7
        add_monster(c, health=100)
        c._engage = {"id": "enc-1", "at": 0.0, "last_hit_at": 0.0, "hp": 300, "potions": 0, "level": 40, "map": 2}
        c.on_packet({"type": "hit", "attacker": 7, "hit": {"monster": "enc-1", "damage": 100}})
        self.assertEqual(c.own_kills, 1)
        self.assertEqual(c.map_payload()["kills"], 1)
        # A later rewards[] entry for the same monster must not double count.
        c.on_packet({"type": "snapshot", "delta": True, "rewards": [{"EncounterID": "enc-1"}]})
        self.assertEqual(c.map_payload()["kills"], 1)

    def test_player_death_is_not_a_kill(self):
        c = make_controller()
        c.player_id = 7
        add_monster(c, health=900)
        c._engage = {"id": "enc-1", "at": 0.0, "last_hit_at": 0.0, "hp": 300, "potions": 0, "level": 50, "map": 2}
        c._own_hit_ids.add("enc-1")
        c.self_state.update({"health": 0, "dead": True})
        c.monsters.monsters.pop("enc-1", None)
        c.check_engaged_death(10.0)
        self.assertEqual(c.own_kills, 0)
        self.assertEqual(c.map_payload()["kills"], 0)
        self.assertIsNone(c._engage)

    def test_damage_without_exp_or_movement_still_trips(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dog = StuckWatchdog(notify=lambda t, m: None, alerts_path=Path(tmp.name) / "alerts.log")
        c = make_controller()
        for step in range(0, 100, 20):
            c.damage += 10
            c.hits += 1
            self.assertIsNone(dog.observe(c, "attack", float(step)))
        # Standing still (even while dealing damage) for 2 minutes is stuck.
        self.assertEqual(dog.observe(c, "attack", 121), "drop")
