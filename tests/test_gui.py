import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tabbybot.config import load_config
from tabbybot.gui import run_gui
from tabbybot.keychain import MemoryKeychain
from tabbybot.loggingutil import redact_text

TEST_KEY = "ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


def html_text(base):
    with urlopen(base + "/", timeout=8) as response:
        return response.read().decode("utf-8")


class GuiRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "wallet.vault"
        ready = {"event": threading.Event()}
        self.keychain = MemoryKeychain()
        self.thread = threading.Thread(
            target=run_gui,
            kwargs={
                "config": load_config(),
                "vault_path": self.vault,
                "port": 0,
                "open_browser": False,
                "ready": ready,
                "keychain": self.keychain,
            },
            daemon=True,
        )
        self.thread.start()
        self.assertTrue(ready["event"].wait(8), "GUI server did not start")
        self.port = ready["port"]
        self.app = ready["app"]
        self.httpd = ready["httpd"]
        self.base = "http://127.0.0.1:%s" % self.port

    def tearDown(self):
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        self.app.close()
        self.tmp.cleanup()

    def _json(self, path, body=None, method=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.base + path, data=data, headers=headers, method=method or ("GET" if data is None else "POST"))
        try:
            with urlopen(request, timeout=8) as response:
                payload = json.loads(response.read().decode("utf-8"))
                return response.status, payload
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"raw": raw}
            return exc.code, payload

    def test_combat_options_persist(self):
        status, state = self._json("/api/state")
        self.assertEqual(state["combat"], {"elite_potion_min": 3, "potion_on_player_damage": False,
                                           "prefer_elite": True, "elite_view_range": 700, "auto_potion": False, "kite_low_potion": False,
                                           "overlevel_kite": False, "overlevel_max": 20,
                                           "cluster_kite": True, "kite_elite_only": False, "max_pull": 1})
        self.assertIn("rows", state["efficiency"])
        page = html_text(self.base)
        for label in ("風箏（弓手）", "僅風箏精英", "立刻停止", "練功等級", "打怪效率", "經驗/分鐘", "放棄次數"):
            self.assertIn(label, page)
        for gone in ("越級風箏", "最多越幾級", "弓手一律打帶跑風箏精英"):
            self.assertNotIn(gone, page)
        status, _ = self._json("/api/combat/options", {"overlevel_max": 15})
        self.assertEqual(status, 400)
        status, payload = self._json("/api/combat/options", {"overlevel_kite": True, "overlevel_max": 30,
                                                             "kite_low_potion": True})
        self.assertFalse(payload["combat"]["overlevel_kite"])    # 越級 removed (t626)
        self.assertFalse(payload["combat"]["kite_low_potion"])
        status, payload = self._json("/api/efficiency/reset", {})
        self.assertEqual(status, 200)
        self.assertTrue((self.vault.parent / "efficiency.json").exists())
        with urlopen(self.base + "/", timeout=8) as response:
            html = response.read().decode("utf-8")
        for label in ("近戰職業：水低於 N 瓶不打精英", "被玩家攻擊時自動喝水", "附近有精英時優先打精英", "精英偵測範圍", "風箏（弓手）"):
            self.assertIn(label, html)
        status, payload = self._json("/api/combat/options", {"elite_potion_min": 5, "potion_on_player_damage": True})
        self.assertEqual(status, 200)
        self.assertEqual(payload["combat"]["elite_potion_min"], 5)
        saved = json.loads((self.vault.parent / "settings.json").read_text("utf-8"))
        self.assertTrue(saved["combat"]["potion_on_player_damage"])
        status, _ = self._json("/api/combat/options", {"elite_potion_min": -1})
        self.assertEqual(status, 400)
        status, payload = self._json("/api/combat/options", {"elite_view_range": 800})
        self.assertEqual(status, 200)
        self.assertEqual(payload["combat"]["elite_view_range"], 800)
        status, _ = self._json("/api/combat/options", {"elite_view_range": 50})
        self.assertEqual(status, 400)
        self.assertIn("風箏（弓手）", html)
        self.assertNotIn("同時最多拉幾隻", html)
        status, payload = self._json("/api/combat/options", {"cluster_kite": False, "max_pull": 3})
        self.assertEqual((payload["combat"]["cluster_kite"], payload["combat"]["max_pull"]), (False, 1))

    def test_webhook_settings_and_alert_routes(self):
        with urlopen(self.base + "/", timeout=8) as response:
            html = response.read().decode("utf-8")
        self.assertIn("Grok Bot 通知網址", html)
        self.assertIn("測試通知", html)
        self.assertIn("alert-banner", html)
        status, payload = self._json("/api/webhook/save", {"url": "http://insecure.example/x", "key": "k"})
        self.assertEqual(status, 400)
        status, payload = self._json("/api/webhook/save", {"url": "https://hooks.example.com/r", "key": "topsecret"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["webhook"]["url"], "https://hooks.example.com/r")
        self.assertTrue(payload["webhook"]["has_key"])
        status, state = self._json("/api/state")
        self.assertNotIn("topsecret", json.dumps(state))
        self.assertIsNone(state["alert"])
        self.assertNotIn("topsecret", json.dumps(list(self.app.logs), ensure_ascii=False))
        self.assertTrue((self.vault.parent / "webhook.json").exists())
        status, _ = self._json("/api/alert/clear", {})
        self.assertEqual(status, 200)

    def test_index_and_state_before_setup(self):
        with urlopen(self.base + "/", timeout=8) as response:
            html = response.read().decode("utf-8")
        self.assertIn("遊戲控制台", html)
        self.assertIn("商城與監控", html)
        self.assertIn("實時小地圖", html)
        self.assertIn("開始每日任務", html)
        self.assertIn("只打任務怪", html)
        self.assertIn("按任務地圖自動進圖", html)
        self.assertIn("需要交道具或裝備的任務自動換掉", html)
        self.assertIn("確認交付", html)
        self.assertIn("記住此電腦", html)
        self.assertIn("開啟時自動解鎖", html)
        self.assertIn("角色", html)
        self.assertIn("沒任務或等待確認時自動練功", html)
        self.assertIn("練功等級", html)
        self.assertIn("背包物品", html)
        self.assertIn("btn-mini-simple", html)
        self.assertIn("btn-mini-detail", html)
        self.assertIn("btn-mini-in", html)
        self.assertIn("btn-mini-out", html)
        self.assertIn("物品詳情", html)
        self.assertNotIn("上架</button>", html)
        self.assertNotIn(">丟棄<", html)
        self.assertIn("世界聊天", html)
        self.assertIn("使用生命藥水", html)
        self.assertIn("卸下", html)
        status, payload = self._json("/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["has_vault"])
        self.assertFalse(payload["unlocked"])
        self.assertFalse(payload["auto_unlock"])
        self.assertIn("quests", payload)
        self.assertFalse(payload["daily_running"])
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")
        status, payload = self._json("/api/quests/start", {"auto_quest": True})
        self.assertEqual(status, 400)

    def test_equip_requires_confirm_and_character_payload_exists(self):
        status, payload = self._json("/api/state")
        self.assertEqual(status, 200)
        self.assertIn("character", payload)
        status, payload = self._json("/api/character/equip", {"player_item_id": 1, "confirm": False})
        self.assertEqual(status, 400)
        status, payload = self._json("/api/item/use", {"kind": "health", "confirm": False})
        self.assertEqual(status, 400)
        status, payload = self._json("/api/chat/send", {"message": "hi"})
        self.assertEqual(status, 400)

    def test_setup_unlock_and_secrets_stay_hidden(self):

        status, payload = self._json("/api/setup", {
            "secret": TEST_KEY,
            "password": "mom-password",
            "confirm": "mismatch",
        })
        self.assertEqual(status, 400)

        status, payload = self._json("/api/setup", {
            "secret": TEST_KEY,
            "password": "mom-password",
            "confirm": "mom-password",
        })
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        dumped = json.dumps(payload)
        self.assertNotIn(TEST_KEY, dumped)
        self.assertNotIn("mom-password", dumped)
        self.assertTrue(self.vault.is_file())

        status, payload = self._json("/api/unlock", {"password": "wrong-pass"})
        self.assertEqual(status, 400)

        status, payload = self._json("/api/unlock", {"password": "mom-password"})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["unlocked"] if "unlocked" in payload else payload["ok"])
        state_status, state = self._json("/api/state")
        self.assertEqual(state_status, 200)
        self.assertTrue(state["unlocked"])
        blob = json.dumps(state) + json.dumps(payload)
        self.assertNotIn(TEST_KEY, blob)
        self.assertNotIn("mom-password", blob)
        for row in state.get("logs", []):
            self.assertNotIn(TEST_KEY, row.get("text", ""))
            self.assertNotIn("mom-password", row.get("text", ""))

        status, payload = self._json("/api/hunt/stop-move", {})
        self.assertEqual(status, 400)
        status, payload = self._json("/api/hunt/stop", {})
        self.assertEqual(status, 200)
        status, payload = self._json("/api/market/stop", {})
        self.assertEqual(status, 200)
        status, payload = self._json("/api/lock", {})
        self.assertEqual(status, 200)
        _, state = self._json("/api/state")
        self.assertFalse(state["unlocked"])

    def test_remember_unlocks_next_start_without_logging_secrets(self):
        status, payload = self._json("/api/setup", {
            "secret": TEST_KEY,
            "password": "mom-password",
            "confirm": "mom-password",
            "remember": True,
        })
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.keychain.load(), "mom-password")
        status, payload = self._json("/api/lock", {})
        self.assertEqual(status, 200)
        _, state = self._json("/api/state")
        self.assertFalse(state["unlocked"])
        self.assertTrue(state["auto_unlock"])
        self.assertTrue(self.app.try_auto_unlock())
        _, state = self._json("/api/state")
        self.assertTrue(state["unlocked"])
        self.assertTrue(state["auto_unlock"])
        blob = json.dumps(state)
        self.assertNotIn(TEST_KEY, blob)
        self.assertNotIn("mom-password", blob)
        for row in state.get("logs", []):
            self.assertNotIn("mom-password", row.get("text", ""))
            self.assertNotIn(TEST_KEY, row.get("text", ""))

        status, payload = self._json("/api/lock", {})
        self.assertEqual(status, 200)
        self.keychain.save("wrong-pass")
        self.assertFalse(self.app.try_auto_unlock())
        self.assertIsNone(self.keychain.load())
        _, state = self._json("/api/state")
        self.assertFalse(state["unlocked"])
        self.assertNotIn("wrong-pass", json.dumps(state))
        for row in state.get("logs", []):
            self.assertNotIn("wrong-pass", row.get("text", ""))
            self.assertNotIn("mom-password", row.get("text", ""))

    def test_redact_helper_covers_password_words(self):
        self.assertIn("<redacted-hex>", redact_text("key=" + TEST_KEY))
        self.assertIn("Bearer <redacted>", redact_text("Authorization: Bearer abc.def.ghi"))


    def test_quest_options_and_kite_elite_persist(self):
        status, payload = self._json("/api/quests/options", {
            "auto_quest": False, "task_limited": False, "quest_map_auto": False,
            "auto_farm": True, "farm_target_level": 40,
        })
        self.assertEqual(status, 200, payload)
        status, payload = self._json("/api/combat/options", {"kite_elite_only": True, "cluster_kite": True})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["combat"]["kite_elite_only"])
        saved = json.loads((self.vault.parent / "settings.json").read_text("utf-8"))
        self.assertEqual(saved["quest"]["farm_target_level"], 40)
        self.assertFalse(saved["quest"]["auto_quest"])
        self.assertTrue(saved["combat"]["kite_elite_only"])
        from tabbybot.config import load_config
        from tabbybot.gui import GuiApp
        from tabbybot.keychain import MemoryKeychain
        app2 = GuiApp(load_config(), self.vault, keychain=MemoryKeychain())
        try:
            self.assertEqual(app2.quest_options["farm_target_level"], 40)
            self.assertFalse(app2.quest_options["auto_quest"])
            self.assertTrue(app2.combat_options["kite_elite_only"])
        finally:
            app2.close()

    def test_stop_now_route_exists(self):
        page = html_text(self.base)
        self.assertIn("btn-hunt-stop-now", page)
        self.assertIn("僅風箏精英", page)
        self.assertIn("不會登出", page)
        status, payload = self._json("/api/hunt/stop-now", {})
        self.assertEqual(status, 200, payload)


if __name__ == "__main__":
    unittest.main()


class NavGridPayloadTests(unittest.TestCase):
    def test_decoded_maps_have_terrain(self):
        from tabbybot.gui import navgrid_payload
        for m in (2, 3, 4, 6, 8, 9, 10):
            d = navgrid_payload(m)
            self.assertEqual(d["cell_size"], 64, m)
            self.assertTrue(d["cells"], m)
        self.assertEqual(navgrid_payload(0)["cells"], [])
