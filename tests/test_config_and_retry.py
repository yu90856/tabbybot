import unittest

from tabbybot.client import challenge_retry_delay
from tabbybot.config import ConfigError, assert_official_url, load_config


class OfficialHostTests(unittest.TestCase):
    def test_accepts_official_https_and_wss(self):
        assert_official_url("https://game.tabby.meme/api")
        assert_official_url("wss://game.tabby.meme/api/realtime", allow_ws=True)

    def test_rejects_other_hosts(self):
        with self.assertRaises(ConfigError):
            assert_official_url("https://evil.example/api")
        with self.assertRaises(ConfigError):
            assert_official_url("wss://example.com/ws", allow_ws=True)
        with self.assertRaises(ConfigError):
            assert_official_url("http://game.tabby.meme/api")

    def test_default_config_points_at_official_hosts(self):
        config = load_config()
        self.assertIn("game.tabby.meme", config.api_base)
        self.assertIn("game.tabby.meme", config.realtime_url)
        self.assertEqual(config.hunt.map_id, 2)


class RetryDelayTests(unittest.TestCase):
    def test_honors_retry_after_ms_and_bounds(self):
        self.assertEqual(challenge_retry_delay({"retryAfterMs": 2000}, 0), 2.0)
        self.assertEqual(challenge_retry_delay({"retryAfter": 3}, 0), 3.0)
        self.assertEqual(challenge_retry_delay(None, 0), 1.0)  # 2^0 but min 0.5, actually min(1,15)=1? 2^0=1, max(1,0.5)=1
        self.assertEqual(challenge_retry_delay(None, 4), 8)  # cap of exponential is 8, then min(..., 15)
        self.assertEqual(challenge_retry_delay({"retryAfterMs": 10}, 0), 0.5)
        self.assertEqual(challenge_retry_delay({"retryAfter": 99}, 0), 15.0)


if __name__ == "__main__":
    unittest.main()
