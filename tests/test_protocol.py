import unittest

from tabbybot.protocol import (
    AttackTicket,
    MonsterCache,
    Position,
    TicketBox,
    apply_server_position,
    attach_seq_life,
    build_auth_frame,
    build_monster_attack,
    build_sync,
    merge_self_state,
    parse_welcome,
)


class AuthFrameTests(unittest.TestCase):
    def test_required_fields_and_token_placement(self):
        frame = build_auth_frame("secret-token", map_id=2, x=10, y=20, movement_revision=3)
        self.assertEqual(frame["action"], "auth")
        self.assertEqual(frame["token"], "secret-token")
        self.assertEqual(frame["map"], 2)
        self.assertEqual(frame["movementRevision"], 3)
        keys = list(frame)
        self.assertEqual(keys[keys.index("token") + 1], "loginReset")
        self.assertEqual(keys[keys.index("loginReset") + 1], "snapshotDelta")
        self.assertTrue(frame["loginReset"])
        self.assertEqual(frame["snapshotDelta"], 2)

    def test_attach_seq_life(self):
        packet = attach_seq_life({"action": "ping"}, 4, 9)
        self.assertEqual(packet["seq"], 4)
        self.assertEqual(packet["life"], 9)
        self.assertEqual(packet["action"], "ping")


class SnapshotTests(unittest.TestCase):
    def test_parse_welcome(self):
        parsed = parse_welcome({"type": "welcome", "position": {"map": 2, "x": 1.5, "y": 8.0, "revision": 11}})
        self.assertEqual(parsed, (2, 1.5, 8.0, 11))
        self.assertIsNone(parse_welcome({"type": "snapshot"}))

    def test_full_snapshot_replaces_monsters(self):
        cache = MonsterCache()
        cache.apply_snapshot({
            "delta": False,
            "monsters": [
                {"encounterId": "a", "health": 10, "mapId": 2, "x": 1, "y": 1},
                {"encounterId": "b", "health": 5, "mapId": 2, "x": 2, "y": 2},
            ],
        })
        cache.apply_snapshot({
            "delta": False,
            "monsters": [
                {"encounterId": "c", "health": 3, "mapId": 2, "x": 3, "y": 3},
            ],
        })
        self.assertEqual(set(cache.monsters), {"c"})

    def test_delta_version_2_merges_and_keeps_unmentioned(self):
        cache = MonsterCache()
        cache.apply_snapshot({
            "delta": False,
            "monsters": [
                {"encounterId": "a", "health": 10, "mapId": 2},
                {"encounterId": "b", "health": 5, "mapId": 2},
            ],
        })
        cache.apply_snapshot({
            "delta": True,
            "deltaVersion": 2,
            "monsters": [{"encounterId": "a", "health": 4}],
            "removedMonsters": [],
        })
        self.assertEqual(cache.monsters["a"]["health"], 4)
        self.assertIn("b", cache.monsters)

    def test_legacy_delta_prunes_missing_ids(self):
        cache = MonsterCache()
        cache.apply_snapshot({
            "delta": False,
            "monsters": [
                {"encounterId": "a", "health": 10, "mapId": 2},
                {"encounterId": "b", "health": 5, "mapId": 2},
            ],
        })
        cache.apply_snapshot({
            "delta": True,
            "deltaVersion": 0,
            "monsters": [{"encounterId": "a", "health": 1, "mapId": 2}],
        })
        self.assertEqual(set(cache.monsters), {"a"})

    def test_removed_monsters(self):
        cache = MonsterCache()
        cache.apply_snapshot({
            "delta": False,
            "deltaVersion": 2,
            "monsters": [
                {"encounterId": "a", "health": 10, "mapId": 2},
                {"encounterId": "b", "health": 5, "mapId": 2},
            ],
        })
        cache.apply_snapshot({
            "delta": True,
            "deltaVersion": 2,
            "monsters": [],
            "removedMonsters": ["a"],
        })
        self.assertEqual(set(cache.monsters), {"b"})

    def test_merge_self_delta_and_respawn(self):
        first = merge_self_state({}, {"delta": False, "self": {"life": 3, "health": 10, "dead": True}})
        self.assertEqual(first["life"], 3)
        second = merge_self_state(first, {"delta": True, "self": {"health": 2}})
        self.assertEqual(second["life"], 3)
        self.assertEqual(second["health"], 2)
        third = merge_self_state(second, {"delta": True, "action": "respawn", "self": {}})
        self.assertFalse(third["dead"])


class TicketTests(unittest.TestCase):
    def test_accept_and_consume(self):
        box = TicketBox()
        box.accept({"id": "t1", "critical": True})
        ticket = box.available(now=0)
        self.assertIsNotNone(ticket)
        self.assertTrue(ticket.critical)
        box.consume("t1", busy_at=10)
        self.assertIsNone(box.available(now=0))
        self.assertIsNone(box.available(now=9))
        # After busy window the same used ticket is still not reusable.
        self.assertIsNone(box.available(now=11))

    def test_invalid_target_unconsumes(self):
        box = TicketBox()
        box.accept({"id": "t2"})
        box.consume("t2", busy_at=0)
        box.on_monster_attack_error("invalid_target", {}, now=1)
        self.assertIsNotNone(box.available(now=1))
        self.assertEqual(box.available(now=1).id, "t2")

    def test_attack_cooldown_keeps_ticket_but_waits(self):
        box = TicketBox()
        box.accept({"id": "t3"})
        box.on_monster_attack_error("attack_cooldown", {}, now=5)
        self.assertIsNone(box.available(now=5.1))
        self.assertIsNotNone(box.available(now=5 + 0.7))


class MovementCorrectionTests(unittest.TestCase):
    def test_stale_revision_is_ignored(self):
        current = Position(map_id=2, x=10, y=10, revision=8)
        updated, applied = apply_server_position(current, 2, 1, 1, 7)
        self.assertFalse(applied)
        self.assertEqual(updated.x, 10)
        self.assertEqual(updated.revision, 8)

    def test_equal_or_newer_revision_is_applied(self):
        current = Position(map_id=2, x=10, y=10, revision=8)
        updated, applied = apply_server_position(current, 2, 4, 5, 8)
        self.assertTrue(applied)
        self.assertEqual((updated.x, updated.y, updated.revision), (4, 5, 8))
        newer, ok = apply_server_position(updated, 2, 9, 9, 12)
        self.assertTrue(ok)
        self.assertEqual(newer.revision, 12)

    def test_sync_and_attack_packets(self):
        sync = build_sync(movement_revision=3, map_id=2, x=1.239, y=4.5, animation="run", flip=True)
        self.assertEqual(sync["x"], 1.24)
        self.assertEqual(sync["action"], "sync")
        attack = build_monster_attack("enc-1", "ticket-1", queued_at_ms=99)
        self.assertEqual(attack["encounterId"], "enc-1")
        self.assertEqual(attack["attackTicket"], "ticket-1")
        self.assertEqual(attack["queuedAt"], 99)


if __name__ == "__main__":
    unittest.main()
