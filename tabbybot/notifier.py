"""Optional user-configured webhook for stuck alerts (e.g. a Grok Bot routine).

The URL lives in ~/.tabbybot/webhook.json; the sender key lives in macOS Keychain
(service tabbybot, account webhook). The key is never logged or returned to the UI.
Only https URLs are accepted, and only the configured URL is ever contacted.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from tabbybot.constants import WEBHOOK_DEBOUNCE_SEC
from tabbybot.keychain import KeychainError, KeychainStore

log = logging.getLogger(__name__)

WEBHOOK_SETTINGS = Path.home() / ".tabbybot" / "webhook.json"


class WebhookError(Exception):
    pass


def validate_webhook_url(url: str) -> str:
    url = str(url or "").strip()
    if not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise WebhookError("通知網址必須是 https:// 開頭的完整網址。")
    if parsed.username or parsed.password:
        raise WebhookError("通知網址不能包含帳號密碼，請把密鑰填在「密鑰」欄位。")
    return url


def _default_sender(url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


class WebhookNotifier:
    def __init__(
        self,
        keystore: KeychainStore,
        settings_path: Path | None = None,
        sender: Optional[Callable[[str, bytes, dict[str, str], float], int]] = None,
        clock: Callable[[], float] = time.monotonic,
        debounce_sec: float = WEBHOOK_DEBOUNCE_SEC,
    ) -> None:
        self.keystore = keystore
        self.settings_path = settings_path or WEBHOOK_SETTINGS
        self.sender = sender or _default_sender
        self.clock = clock
        self.debounce_sec = debounce_sec
        self.last_sent_at: float | None = None
        self.last_result = ""
        self._lock = threading.Lock()

    # settings -------------------------------------------------------------
    def url(self) -> str:
        try:
            data = json.loads(self.settings_path.read_text("utf-8"))
            return validate_webhook_url(data.get("url", "")) if isinstance(data, dict) else ""
        except (OSError, ValueError, WebhookError):
            return ""

    def has_key(self) -> bool:
        try:
            return bool(self.keystore.load())
        except KeychainError:
            return False

    def settings(self) -> dict[str, Any]:
        return {"url": self.url(), "has_key": self.has_key(), "last_result": self.last_result}

    def save(self, url: str, key: str | None = None, clear_key: bool = False) -> None:
        clean = validate_webhook_url(url)
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps({"url": clean}), "utf-8")
        try:
            self.settings_path.chmod(0o600)
        except OSError:
            pass
        try:
            if clear_key:
                self.keystore.delete()
            elif key:
                self.keystore.save(str(key).strip())
        except KeychainError as exc:
            raise WebhookError("密鑰無法存進鑰匙圈。") from exc
        log.info("已儲存 Grok Bot 通知設定（%s）。", urllib.parse.urlparse(clean).hostname if clean else "未設定")

    # sending --------------------------------------------------------------
    def _post(self, payload: dict[str, Any]) -> str:
        url = self.url()
        if not url:
            return "尚未設定通知網址"
        headers = {"Content-Type": "application/json", "User-Agent": "tabbybot-watchdog"}
        try:
            key = self.keystore.load()
        except KeychainError:
            key = None
        if key:
            headers["Authorization"] = "Bearer " + key
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            status = self.sender(url, body, headers, 10.0)
        except Exception as exc:  # never include headers (key) in messages
            return f"通知送出失敗：{type(exc).__name__}"
        if 200 <= status < 300:
            return f"通知已送出（HTTP {status}）"
        return f"通知被拒絕（HTTP {status}）"

    def send_alert(self, payload: dict[str, Any], *, force: bool = False, background: bool = True) -> str:
        with self._lock:
            now = self.clock()
            if not force and self.last_sent_at is not None and now - self.last_sent_at < self.debounce_sec:
                return "10 分鐘內已通知過，這次略過"
            if not self.url():
                return "尚未設定通知網址"
            self.last_sent_at = now

        def run() -> None:
            self.last_result = self._post(payload)
            log.info("Grok Bot 通知：%s", self.last_result)

        if background:
            threading.Thread(target=run, daemon=True, name="tabbybot-webhook").start()
            return "通知送出中"
        run()
        return self.last_result

    def send_test(self) -> str:
        payload = {
            "reason": "測試通知", "map": "", "x": None, "y": None, "status": "這是一則測試通知",
            "kills": 0, "exp": None, "recovered": False, "stopped": False, "test": True,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        return self.send_alert(payload, force=True, background=False)
