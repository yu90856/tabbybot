"""Local 127.0.0.1 web UI. Secrets never written to logs or responses."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tabbybot.character import format_character
from tabbybot.client import (
    ClientError,
    LoginError,
    QuestError,
    accept_quest,
    deliver_quest,
    equip_item,
    fetch_equipment,
    fetch_player,
    fetch_progression,
    fetch_quest_delivery,
    fetch_quests,
    fetch_shop,
    signed_login,
    unequip_item,
)
from tabbybot.config import AppConfig, ConfigError, MarketConfig
from tabbybot.constants import MAP_BUTTONS, MAP_FILES, MAP_NAMES, MAP_TITLES, SPECIAL_ENTRY_MAPS, DEFAULT_HUNT_MAP
from tabbybot.farm import DEFAULT_FARM_LEVEL, is_farmable_map
from tabbybot.quests import (
    REPLACE_UNAVAILABLE,
    format_delivery_prompt,
    format_quest_rows,
    quest_id,
    quest_summary,
)
from tabbybot.keychain import KeychainError, KeychainStore, default_keychain
from tabbybot.loggingutil import configure_logging, redact_mapping, redact_text, short_address
from tabbybot.market import filter_listings, new_matches
from tabbybot.wallet import (
    WalletError,
    address_from_key,
    default_vault_path,
    load_vault,
    save_vault,
)

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"
MAX_LOGS = 250
MAX_MATCHES = 40


class GuiError(Exception):
    pass


class MemoryLogHandler(logging.Handler):
    def __init__(self, records: deque):
        super().__init__()
        self.records = records
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = redact_text(self.format(record))
        except Exception:
            message = "（日誌格式化失敗）"
        self.records.append({"time": time.strftime("%H:%M:%S"), "text": message})


COMBAT_DEFAULTS = {
    "elite_potion_min": 3,
    "potion_on_player_damage": False,
    "no_potion": True,
    "prefer_elite": True,
    "elite_view_range": 700,
    "auto_potion": False,
    "kite_low_potion": False,
    "overlevel_kite": False,
    "overlevel_max": 20,
    "cluster_kite": True,
    "kite_elite_only": False,
    "max_pull": 1,
}


def clean_combat_options(options: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    if "elite_potion_min" in options:
        try:
            value = int(options["elite_potion_min"])
        except (TypeError, ValueError) as exc:
            raise GuiError("精英藥水門檻必須是數字") from exc
        if value < 0 or value > 999:
            raise GuiError("精英藥水門檻必須在 0–999")
        result["elite_potion_min"] = value
    if "elite_view_range" in options:
        try:
            value = int(round(float(options["elite_view_range"])))
        except (TypeError, ValueError) as exc:
            raise GuiError("精英偵測範圍必須是數字") from exc
        if value < 200 or value > 2000:
            raise GuiError("精英偵測範圍必須在 200–2000")
        result["elite_view_range"] = value
    if "overlevel_max" in options:
        try:
            step = int(options["overlevel_max"])
        except (TypeError, ValueError) as exc:
            raise GuiError("最多越幾級必須是 10、20 或 30") from exc
        if step not in (10, 20, 30):
            raise GuiError("最多越幾級必須是 10、20 或 30")
        result["overlevel_max"] = step
    if "max_pull" in options:
        try:
            pull = int(options["max_pull"])
        except (TypeError, ValueError) as exc:
            raise GuiError("同時最多拉幾隻必須是 1–5") from exc
        result["max_pull"] = 1
    for key in ("no_potion", "potion_on_player_damage", "prefer_elite", "auto_potion", "cluster_kite", "kite_elite_only"):
        if key in options:
            result[key] = bool(options[key])
    # Simplified UI (t626): no 越級 concept, one 「風箏」 toggle. Old saved values are migrated off.
    result["max_pull"] = 1  # game allows one target at a time (t637)
    result["overlevel_kite"] = False
    result["kite_low_potion"] = False
    return result


class GuiApp:
    def __init__(
        self,
        config: AppConfig,
        vault_path: Path,
        keychain: KeychainStore | None = None,
        webhook: Any = None,
    ):
        self.config = config
        self.vault_path = vault_path
        self.keychain = keychain if keychain is not None else default_keychain()
        if webhook is None:
            from tabbybot.keychain import DarwinKeychain, MemoryKeychain, NoopKeychain, keychain_available
            from tabbybot.notifier import WebhookNotifier
            if isinstance(keychain, MemoryKeychain):
                store = MemoryKeychain()  # tests inject an in-memory keychain
            else:
                store = DarwinKeychain(account="webhook") if keychain_available() else NoopKeychain()
            webhook = WebhookNotifier(store, settings_path=vault_path.parent / "webhook.json")
        self.webhook = webhook
        self.settings_path = vault_path.parent / "settings.json"
        from tabbybot.efficiency import EfficiencyTracker
        self.efficiency = EfficiencyTracker(vault_path.parent / "efficiency.json")
        self.combat_options = dict(COMBAT_DEFAULTS)
        self._load_settings()
        self._closed = threading.Event()

        def character_ticker() -> None:
            # Keep EXP readings flowing for the efficiency panel even if no page is open.
            while not self._closed.wait(8.0):
                try:
                    self.maybe_refresh_character()
                except Exception:
                    pass

        threading.Thread(target=character_ticker, daemon=True, name="tabbybot-character-ticker").start()
        self._session_password = ""
        self.lock = threading.RLock()
        self._token_lock = threading.Lock()
        self.private_key = ""
        self.address = ""
        self.game_token = ""
        self.logs: deque = deque(maxlen=MAX_LOGS)
        self.market_matches: list[dict[str, Any]] = []
        self.market_summary = "尚未開始監控"
        self.hunt_running = False
        self.market_running = False
        self.hunt_error = ""
        self.market_error = ""
        self._hunt_stop = threading.Event()
        self._market_stop = threading.Event()
        self._hunt_thread: threading.Thread | None = None
        self._market_thread: threading.Thread | None = None
        self._holder: dict[str, Any] = {}
        self._log_handler = MemoryLogHandler(self.logs)
        logging.getLogger().addHandler(self._log_handler)
        self.quests: list[dict[str, Any]] = []
        self.quest_status = "尚未讀取任務"
        self.quest_error = ""
        self.daily_running = False
        self.quest_options = {
            "auto_quest": True,
            "task_limited": True,
            "quest_map_auto": True,
            "auto_replace": False,
            "skip_vendetta": False,
            "quest_reroll": False,
            "no_kite_level": 30,
            "auto_farm": True,
            "farm_target_level": int(getattr(getattr(config, "farm", None), "target_level", DEFAULT_FARM_LEVEL) or DEFAULT_FARM_LEVEL),
        }
        self._apply_saved_quest_options()
        self.pinned_map = self._saved_pinned_map()
        self.config.hunt.map_id = int(self.pinned_map or DEFAULT_HUNT_MAP)
        self.pending_delivery: dict[str, Any] | None = None
        self.skipped_quest_ids: set[int] = set()
        self.character: dict[str, Any] = {
            "stats": [], "equipment": [], "inventory": [],
            "error": "", "status": "尚未讀取角色資料", "updated_at": None,
        }
        self._character_lock = threading.Lock()
        self._character_inflight = False
        self._character_at = 0.0

    def close(self) -> None:
        self._closed.set()
        try:
            self.efficiency.save(time.monotonic(), force=True)
        except Exception:
            pass
        self._disconnect_hunt()
        self.stop_market()
        self.lock_wallet()
        root = logging.getLogger()
        if self._log_handler in root.handlers:
            root.removeHandler(self._log_handler)

    def has_vault(self) -> bool:
        return self.vault_path.is_file()

    def unlocked(self) -> bool:
        return bool(self.private_key)

    def _apply_unlocked(self, password: str, address: str, private_key: str) -> None:
        with self.lock:
            self.private_key = private_key
            self.address = address
            self._session_password = password

    def _remember_password(self, password: str, remember: bool) -> None:
        try:
            if remember:
                self.keychain.save(password)
                log.info("已把主密碼記住在本機鑰匙圈，下次開啟會自動解鎖。")
            else:
                self.keychain.delete()
        except KeychainError as exc:
            # Vault is already unlocked; surface a non-fatal hint.
            log.info("記住主密碼未完成：%s", exc)

    def setup(self, secret: str, password: str, confirm: str, remember: bool = True) -> str:
        if not (secret or "").strip():
            raise GuiError("請貼上私鑰或助記詞。")
        if password != confirm:
            raise GuiError("兩次輸入的主密碼不一致。")
        address = save_vault(self.vault_path, secret, password)
        wallet = load_vault(self.vault_path, password)
        self._apply_unlocked(password, address, wallet.private_key)
        self._remember_password(password, remember)
        log.info("已建立本機保險庫：%s", short_address(address))
        threading.Thread(target=self._safe_refresh_quests, daemon=True, name="tabbybot-quest-refresh").start()
        return address

    def unlock(self, password: str, remember: bool = True) -> str:
        if not self.has_vault():
            raise GuiError("還沒有保存錢包，請先做首次設定。")
        wallet = load_vault(self.vault_path, password)
        self._apply_unlocked(password, wallet.address, wallet.private_key)
        self._remember_password(password, remember)
        log.info("已解鎖錢包：%s", short_address(wallet.address))
        threading.Thread(target=self._safe_refresh_quests, daemon=True, name="tabbybot-quest-refresh").start()
        return wallet.address

    def try_auto_unlock(self) -> bool:
        if self.unlocked() or not self.has_vault():
            return False
        try:
            password = self.keychain.load()
        except KeychainError:
            log.info("無法讀取鑰匙圈，請手動輸入主密碼。")
            return False
        if not password:
            return False
        try:
            wallet = load_vault(self.vault_path, password)
        except WalletError:
            try:
                self.keychain.delete()
            except KeychainError:
                pass
            log.info("鑰匙圈裡的主密碼無法打開保險庫，請再輸入一次。")
            return False
        self._apply_unlocked(password, wallet.address, wallet.private_key)
        log.info("已用本機鑰匙圈自動解鎖：%s", short_address(wallet.address))
        threading.Thread(target=self._safe_refresh_quests, daemon=True, name="tabbybot-quest-refresh").start()
        return True

    def set_auto_unlock(self, enabled: bool) -> None:
        if enabled:
            with self.lock:
                password = self._session_password
            if not password:
                raise GuiError("目前沒有可記住的主密碼。請先鎖定，再解鎖一次並勾選「記住此電腦」。")
            try:
                self.keychain.save(password)
            except KeychainError as exc:
                raise GuiError(str(exc)) from exc
            log.info("已開啟自動解鎖。")
            return
        try:
            self.keychain.delete()
        except KeychainError as exc:
            raise GuiError(str(exc)) from exc
        log.info("已關閉自動解鎖，下次開啟需要再輸入主密碼。")

    def auto_unlock_enabled(self) -> bool:
        try:
            return self.keychain.has_password()
        except KeychainError:
            return False

    def lock_wallet(self) -> None:
        self._disconnect_hunt()
        self.stop_market()
        with self.lock:
            self.private_key = ""
            self.game_token = ""
            self.address = ""
            self._session_password = ""
            self._holder.pop("controller", None)
            self.quests = []
            self.quest_status = "尚未讀取任務"
            self.daily_running = False
            self.pending_delivery = None
            self.skipped_quest_ids = set()

    def _require_key(self) -> str:
        with self.lock:
            key = self.private_key
        if not key:
            raise GuiError("請先解鎖錢包。")
        return key

    def _ensure_token(self) -> str:
        """Reuse a cached game session, and never start two signature logins at once."""
        key = self._require_key()
        with self.lock:
            if self.game_token:
                return self.game_token
        with self._token_lock:
            with self.lock:
                if self.game_token:
                    return self.game_token
            token, address, _player = signed_login(key, self.config)
            with self.lock:
                self.game_token = token
                self.address = address or self.address
            return token

    def _session_alive(self) -> bool:
        if self._hunt_stop.is_set():
            return False
        thread = self._hunt_thread
        if thread is None or not thread.is_alive():
            return False
        controller = self._holder.get("controller")
        return controller is not None and bool(getattr(controller, "world_ready", False))

    def _resume_hunt(self, options: dict[str, Any]) -> None:
        controller = self._holder.get("controller")
        if controller is None:
            raise GuiError("目前沒有進行中的遊戲連線。")
        if "map_id" in options:
            try:
                map_id = int(options["map_id"])
            except (TypeError, ValueError) as exc:
                raise GuiError("地圖編號無效") from exc
            if map_id not in MAP_NAMES:
                raise GuiError("未知的地圖。")
            self.config.hunt.map_id = map_id
            self._remember_pin(map_id)
            controller.pin_map(map_id)
        if "elite_only" in options:
            self.config.hunt.elite_only = bool(options["elite_only"])
            controller.config.hunt.elite_only = bool(options["elite_only"])
        if "auto_combat" in options:
            controller.auto_combat = bool(options["auto_combat"])
        if "auto_return" in options:
            controller.auto_return = bool(options["auto_return"])
        controller.apply_combat_options(self.combat_options)
        self._apply_quest_flags(controller)
        controller.resume_hunt()
        self.hunt_running = True
        self.hunt_error = ""
        log.info("繼續打怪（未重新登入）。")

    def _schedule_auto_restart(self, options: dict[str, Any]) -> None:
        """程式意外出錯停掉時自動重開：30／60／120 秒後重試，一小時最多 3 次。"""
        now = time.time()
        hist = [t for t in getattr(self, "_auto_restarts", []) if now - t < 3600]
        if len(hist) >= 3:
            log.error("一小時內已自動重開 3 次，不再自動重開，請檢查紀錄。")
            self._auto_restarts = hist
            return
        delay = (30, 60, 120)[len(hist)]
        hist.append(now)
        self._auto_restarts = hist
        stop_evt = self._hunt_stop
        log.warning("打怪意外停止，%d 秒後自動重開（一小時內第 %d 次）。", delay, len(hist))

        def later() -> None:
            time.sleep(delay)
            if (stop_evt.is_set() or self.hunt_running or self._hunt_stop is not stop_evt
                    or getattr(self, "_user_stop_at", 0.0) >= now):
                return
            try:
                self.start_hunt(dict(options))
                log.info("已自動重開打怪。")
            except Exception:
                log.exception("自動重開失敗")

        threading.Thread(target=later, daemon=True, name="tabbybot-auto-restart").start()

    def start_hunt(self, options: dict[str, Any]) -> None:
        if self._session_alive():
            self._resume_hunt(options)
            return
        if self.hunt_running:
            raise GuiError("打怪已經在進行了。")
        key = self._require_key()
        try:
            if options.get("map_id") not in (None, ""):
                map_id = int(options["map_id"])
            else:
                map_id = int(self.pinned_map or DEFAULT_HUNT_MAP)
        except (TypeError, ValueError) as exc:
            raise GuiError("地圖編號無效") from exc
        if map_id not in MAP_NAMES:
            raise GuiError("未知的地圖。")
        self.config.hunt.map_id = map_id
        self._remember_pin(map_id)
        self.config.hunt.elite_only = bool(options.get("elite_only", False))
        auto_combat = bool(options.get("auto_combat", True))
        auto_return = bool(options.get("auto_return", True))
        self._hunt_stop = threading.Event()
        self._holder = {}
        self.hunt_error = ""
        self.hunt_running = True

        def worker() -> None:
            import asyncio
            from tabbybot.hunt import run_hunt

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            stop = asyncio.Event()

            def watch() -> None:
                self._hunt_stop.wait()
                loop.call_soon_threadsafe(stop.set)

            threading.Thread(target=watch, daemon=True).start()
            crashed = False
            try:
                with self.lock:
                    cached_token = self.game_token
                    cached_addr = self.address
                if not cached_token:
                    try:
                        cached_token = self._ensure_token()
                        with self.lock:
                            cached_addr = self.address
                    except Exception:
                        cached_token, cached_addr = "", cached_addr
                loop.run_until_complete(
                    run_hunt(self.config, key, stop_event=stop, controller_holder=self._holder,
                             alert_hook=self.webhook.send_alert, efficiency=self.efficiency,
                             existing_token=cached_token or None, existing_address=cached_addr or "")
                )
            except Exception as exc:
                if "Connected elsewhere" in str(exc):
                    self.hunt_error = "遊戲在別的地方登入了（官方網頁、手機或另一個外掛），這邊被踢下線。要掛機請先關掉官方遊戲，再按開始。"
                    log.error("%s", self.hunt_error)
                    try:
                        from tabbybot.watchdog import default_notify, write_alert_line
                        write_alert_line("斷線：" + self.hunt_error)
                        default_notify("Tabby 外掛", "被別處登入踢下線，外掛已停止")
                    except Exception:
                        pass
                else:
                    self.hunt_error = f"{type(exc).__name__}: {exc}"
                    log.exception("打怪結束：%s（完整錯誤如下）", type(exc).__name__)
                    crashed = True
            finally:
                self.hunt_running = False
                self._holder.pop("controller", None)
                loop.close()
                if crashed and not self._hunt_stop.is_set():
                    self._schedule_auto_restart(options)

        self._hunt_thread = threading.Thread(target=worker, daemon=True, name="tabbybot-hunt")
        self._hunt_thread.start()
        # Apply flags once the controller exists.
        def apply_flags() -> None:
            deadline = time.time() + 15
            while time.time() < deadline and self.hunt_running:
                controller = self._holder.get("controller")
                if controller is not None:
                    controller.auto_combat = auto_combat
                    controller.auto_return = auto_return
                    controller.pin_map(map_id)
                    controller.apply_combat_options(self.combat_options)
                    self._apply_quest_flags(controller)
                    return
                time.sleep(0.05)

        threading.Thread(target=apply_flags, daemon=True).start()
        log.info("開始在 %s 打怪。", MAP_NAMES.get(map_id, map_id))

    def stop_hunt(self, immediate: bool = False) -> None:
        """Soft-stop: keep the world session, finish the current target (unless immediate), then stand."""
        self._user_stop_at = time.time()
        self.daily_running = False
        controller = self._holder.get("controller")
        if controller is not None and not self._hunt_stop.is_set() and (
            getattr(controller, "world_ready", False) or self._hunt_thread is not None and self._hunt_thread.is_alive()
        ):
            controller.request_soft_stop(immediate=immediate)
            self.hunt_running = False
            return
        self._disconnect_hunt()

    def _disconnect_hunt(self) -> None:
        self.daily_running = False
        self._hunt_stop.set()
        thread = self._hunt_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)
        self.hunt_running = False

    def stop_move(self) -> None:
        controller = self._holder.get("controller")
        if controller is None:
            raise GuiError("目前沒有進行中的遊戲連線。")
        controller.pause_movement()

    def update_hunt_options(self, options: dict[str, Any]) -> None:
        if "elite_only" in options:
            self.config.hunt.elite_only = bool(options["elite_only"])
        if "map_id" in options:
            try:
                map_id = int(options["map_id"])
            except (TypeError, ValueError) as exc:
                raise GuiError("地圖編號無效") from exc
            if map_id not in MAP_NAMES:
                raise GuiError("未知的地圖。")
            self._remember_pin(map_id)
        controller = self._holder.get("controller")
        if controller is not None:
            if "auto_combat" in options:
                controller.auto_combat = bool(options["auto_combat"])
            if "auto_return" in options:
                controller.auto_return = bool(options["auto_return"])
            if "elite_only" in options:
                controller.config.hunt.elite_only = bool(options["elite_only"])
            if "map_id" in options:
                controller.config.hunt.map_id = int(options["map_id"])
            self._apply_quest_flags(controller)

    def update_quest_options(self, options: dict[str, Any]) -> None:
        for key in ("auto_quest", "task_limited", "quest_map_auto", "auto_replace", "skip_vendetta", "quest_reroll", "auto_farm"):
            if key in options:
                self.quest_options[key] = bool(options[key])
        if "no_kite_level" in options:
            try:
                self.quest_options["no_kite_level"] = max(0, min(200, int(options["no_kite_level"])))
            except (TypeError, ValueError):
                pass
        if "farm_target_level" in options:
            try:
                level = int(options["farm_target_level"])
            except (TypeError, ValueError) as exc:
                raise GuiError("練功怪物等級無效") from exc
            if level < 1 or level > 9999:
                raise GuiError("練功怪物等級必須在 1–9999")
            self.quest_options["farm_target_level"] = level
        self._save_settings()
        pend = self.pending_delivery or {}
        if self.quest_options.get("skip_vendetta") and str(pend.get("objective_type") or "").upper() == "VENDETTA" and not pend.get("already_accepted"):
            if pend.get("quest_id") is not None:
                self.skipped_quest_ids.add(int(pend["quest_id"]))
            self._set_delivery_prompt(None)
            ctl = self._holder.get("controller")
            if ctl is not None:
                ctl.pending_delivery = None
        controller = self._holder.get("controller")
        if controller is not None:
            self._apply_quest_flags(controller)

    def _apply_quest_flags(self, controller: Any | None = None) -> None:
        controller = controller if controller is not None else self._holder.get("controller")
        if controller is None:
            return
        controller.apply_quest_options({
            **self.quest_options,
            "daily_mode": self.daily_running,
            "auto_farm": self.quest_options.get("auto_farm", True),
            "farm_target_level": self.quest_options.get("farm_target_level", DEFAULT_FARM_LEVEL),
            "no_kite_level": self.quest_options.get("no_kite_level", 30),
        })
        controller.skipped_quest_ids = set(self.skipped_quest_ids)
        if self.pending_delivery and not controller.pending_delivery and not (
                self.quest_options.get("skip_vendetta") and str(self.pending_delivery.get("objective_type") or "").upper() == "VENDETTA"):
            controller.pending_delivery = self.pending_delivery

    def _safe_refresh_quests(self) -> None:
        try:
            self.refresh_quests()
        except Exception as exc:
            self.quest_error = str(exc)
            log.info("讀取任務失敗：%s", type(exc).__name__)

    def refresh_quests(self) -> list[dict[str, Any]]:
        token = self._ensure_token()
        quests = fetch_quests(self.config, token)
        with self.lock:
            self.quests = quests
            self.quest_status = quest_summary(quests)
            self.quest_error = ""
        controller = self._holder.get("controller")
        if controller is not None:
            controller.update_quests(quests)
        log.info("%s", self.quest_status)
        return quests

    def start_daily(self, options: dict[str, Any] | None = None) -> None:
        options = options or {}
        for key in ("auto_quest", "task_limited", "quest_map_auto"):
            self.quest_options[key] = bool(options.get(key, True))
        self.quest_options["auto_replace"] = bool(options.get("auto_replace", False))
        self.quest_options["skip_vendetta"] = bool(options.get("skip_vendetta", False))
        self.quest_options["quest_reroll"] = bool(options.get("quest_reroll", False))
        self.quest_options["auto_farm"] = bool(options.get("auto_farm", True))
        if "no_kite_level" in options:
            try:
                self.quest_options["no_kite_level"] = max(0, min(200, int(options["no_kite_level"])))
            except (TypeError, ValueError):
                pass
        if "farm_target_level" in options:
            try:
                level = int(options["farm_target_level"])
            except (TypeError, ValueError) as exc:
                raise GuiError("練功怪物等級無效") from exc
            if level < 1 or level > 9999:
                raise GuiError("練功怪物等級必須在 1–9999")
            self.quest_options["farm_target_level"] = level
        self._save_settings()
        self.daily_running = True
        self.quest_error = ""
        self.pending_delivery = None
        self.skipped_quest_ids = set()
        if "elite_only" not in options:
            self.config.hunt.elite_only = False
        if not self.hunt_running:
            hunt_opts = {
                "map_id": options.get("map_id", self.config.hunt.map_id),
                "elite_only": False,
                "auto_combat": True,
                "auto_return": True,
            }
            self.start_hunt(hunt_opts)
        else:
            controller = self._holder.get("controller")
            if controller is not None:
                controller.auto_combat = True
                controller.auto_return = True
                self._apply_quest_flags(controller)
        log.info("開始每日任務：打怪任務會自動接取；需要交道具或裝備的任務會先暫停請你確認。廢棄遺跡、秘寶之地不會自動進入。")
        if self.quest_options["auto_replace"]:
            log.info("%s", REPLACE_UNAVAILABLE)

    def stop_daily(self) -> None:
        self.daily_running = False
        self.quest_options["auto_quest"] = False
        self.quest_options["quest_map_auto"] = False
        self.quest_options["task_limited"] = False
        self.pending_delivery = None
        controller = self._holder.get("controller")
        if controller is not None:
            controller.pending_delivery = None
            self._apply_quest_flags(controller)
        log.info("已停止每日任務。")

    def _current_delivery_prompt(self) -> dict[str, Any] | None:
        controller = self._holder.get("controller")
        if controller is not None and getattr(controller, "pending_delivery", None):
            return controller.pending_delivery
        if controller is not None and self.pending_delivery and self.pending_delivery.get("quest_id") is not None \
                and int(self.pending_delivery["quest_id"]) in getattr(controller, "vendetta_cancelled_ids", set()):
            self.pending_delivery = None
        return self.pending_delivery

    def _set_delivery_prompt(self, prompt: dict[str, Any] | None) -> None:
        self.pending_delivery = prompt
        controller = self._holder.get("controller")
        if controller is not None:
            controller.pending_delivery = prompt
            if prompt:
                controller.quest_status = f"「{prompt.get('title', '任務')}」需要交付道具或裝備，已暫停等待你確認。"
                controller.status = controller.quest_status

    def confirm_delivery(self) -> None:
        prompt = self._current_delivery_prompt()
        if not prompt:
            raise GuiError("目前沒有等待確認的交付任務。")
        ident = prompt.get("quest_id")
        if ident is None:
            raise GuiError("任務沒有編號，無法交付。")
        token = self._ensure_token()
        if not prompt.get("already_accepted"):
            accept_quest(self.config, token, int(ident))
        preview = fetch_quest_delivery(self.config, token, int(ident))
        quest = preview.get("quest") if isinstance(preview.get("quest"), dict) else {"id": ident, "title": prompt.get("title")}
        quest.setdefault("id", ident)
        fresh = format_delivery_prompt(quest, preview=preview, already_accepted=True)
        previous_ids = [int(item) for item in (prompt.get("item_ids") or []) if item is not None]
        fresh_ids = [int(item) for item in (fresh.get("item_ids") or []) if item is not None]
        if prompt.get("previewed") and previous_ids and previous_ids != fresh_ids:
            self._set_delivery_prompt(fresh)
            raise GuiError("要交付的物品有變動，請再確認一次。")
        if not fresh.get("enough"):
            self._set_delivery_prompt(fresh)
            raise GuiError(fresh.get("note") or "可交付的物品不足，尚未提交。")
        item_ids = previous_ids if previous_ids and not prompt.get("previewed") is False and set(previous_ids).issubset(set(fresh_ids + previous_ids)) and len(previous_ids) == fresh.get("target") else fresh_ids
        # Prefer the exact items the user just confirmed if they are still in the fresh proposal or still listed.
        if previous_ids and len(previous_ids) == int(fresh.get("target") or 0):
            available = {item.get("playerItemId") for item in (preview.get("items") or []) if isinstance(item, dict)}
            if all(item in available for item in previous_ids):
                item_ids = previous_ids
        deliver_quest(self.config, token, int(ident), int(fresh.get("run_number") or 0), item_ids)
        log.info("已確認交付任務：%s（%s 件）", prompt.get("title") or ident, len(item_ids))
        self._set_delivery_prompt(None)
        self.quest_status = "伺服器已確認交付，正在重新整理任務。"
        controller = self._holder.get("controller")
        if controller is not None:
            controller._quests_dirty = True
            controller.quest_accept_checked_at = 0.0
        try:
            self.refresh_quests()
        except Exception:
            pass

    def cancel_delivery_quest(self) -> None:
        from tabbybot.client import cancel_quest
        prompt = self._current_delivery_prompt()
        if not prompt or prompt.get("quest_id") is None:
            raise GuiError("目前沒有可取消的任務。")
        cancel_quest(self.config, self._ensure_token(), int(prompt["quest_id"]))
        log.info("已取消任務：%s", prompt.get("title") or prompt.get("quest_id"))
        self._set_delivery_prompt(None)
        controller = self._holder.get("controller")
        if controller is not None:
            controller.pending_delivery = None
            controller._quests_dirty = True
            controller.quest_accept_checked_at = 0.0
        try:
            self.refresh_quests()
        except Exception:
            pass

    def skip_delivery(self) -> None:
        prompt = self._current_delivery_prompt()
        if not prompt:
            raise GuiError("目前沒有等待確認的交付任務。")
        ident = prompt.get("quest_id")
        if prompt.get("already_accepted"):
            raise GuiError("此任務已接取，遊戲沒有放棄或更換接口。請確認交付，或到官方客戶端處理。")
        if ident is not None:
            self.skipped_quest_ids.add(int(ident))
            controller = self._holder.get("controller")
            if controller is not None:
                controller.skipped_quest_ids.add(int(ident))
                controller.quest_accept_checked_at = 0.0
                controller._quests_dirty = True
        log.info("已在本機略過（未接取、未送伺服器）：%s", prompt.get("title") or ident)
        self._set_delivery_prompt(None)
        self.quest_status = f"已跳過「{prompt.get('title') or '任務'}」，繼續處理打怪任務。"

    def start_market(self, options: dict[str, Any]) -> None:
        if self.market_running:
            raise GuiError("商城監控已經在進行了。")
        name = str(options.get("name_contains") or "")
        try:
            max_price = float(options.get("max_price", self.config.market.max_price))
            min_price = float(options.get("min_price", 0))
        except (TypeError, ValueError) as exc:
            raise GuiError("價格無效") from exc
        if max_price <= min_price:
            raise GuiError("最高價必須大於最低價。")
        self.config.market.name_contains = name
        self.config.market.max_price = max_price
        self.config.market.min_price = min_price
        self._market_stop = threading.Event()
        self.market_error = ""
        self.market_running = True
        token_holder: dict[str, str] = {}

        def worker() -> None:
            try:
                token_holder["token"] = self._ensure_token()
            except Exception as exc:
                self.market_error = f"{type(exc).__name__}: {exc}"
                self.market_running = False
                log.error("商城登入失敗：%s", type(exc).__name__)
                return
            seen: set = set()
            log.info("開始監控商城（不會自動購買）。")
            while not self._market_stop.is_set():
                try:
                    products = fetch_shop(self.config, token_holder["token"])
                    matches = filter_listings(products, self.config.market)
                    fresh = new_matches(seen, matches)
                    seen = {item.dedupe_key for item in matches}
                    rows = []
                    for item in matches[:MAX_MATCHES]:
                        rows.append({
                            "id": item.product_id,
                            "price": item.price,
                            "quality": item.quality,
                            "name": item.name or ("#" + str(item.product_id)),
                            "fresh": item.dedupe_key in {row.dedupe_key for row in fresh},
                        })
                    with self.lock:
                        self.market_matches = rows
                        self.market_summary = (
                            f"掃描 {len(products)} 件，符合 {len(matches)} 件"
                            + (f"，新命中 {len(fresh)} 件" if fresh else "")
                        )
                    if fresh:
                        for item in fresh:
                            log.info(
                                "商城新命中：%s  品質 %s  價格 %s",
                                item.name or ("#" + str(item.product_id)),
                                item.quality,
                                item.price,
                            )
                except Exception as exc:
                    self.market_error = f"{type(exc).__name__}: {exc}"
                    log.error("商城掃描失敗：%s", type(exc).__name__)
                self._market_stop.wait(self.config.market.interval_sec)
            self.market_running = False

        self._market_thread = threading.Thread(target=worker, daemon=True, name="tabbybot-market")
        self._market_thread.start()

    def stop_market(self) -> None:
        self._market_stop.set()
        self.market_running = False


    def _controller(self):
        return self._holder.get("controller")

    def refresh_character(self, *, force: bool = False) -> dict[str, Any]:
        if not self.unlocked():
            raise GuiError("請先解鎖錢包。")
        now = time.monotonic()
        if not force and self._character_inflight:
            return dict(self.character)
        if not force and now - self._character_at < 8 and self.character.get("updated_at"):
            return dict(self.character)
        self._character_inflight = True
        try:
            token = self._ensure_token()
            player = fetch_player(self.config, token)
            try:
                equipment = fetch_equipment(self.config, token)
            except ClientError as exc:
                equipment = {}
                self.character["error"] = "裝備資料：%s" % exc
            else:
                self.character["error"] = ""
            progression = {}
            try:
                progression = fetch_progression(self.config, token)
            except ClientError:
                progression = {}
            controller = self._controller()
            self_state = getattr(controller, "self_state", None) if controller is not None else None
            try:
                from tabbybot.efficiency import extract_experience
                exp = extract_experience(progression) or extract_experience(player)
                if exp is not None:
                    self.efficiency.observe_experience(exp[0], exp[1], time.monotonic())
            except Exception:
                pass
            payload = format_character(player, equipment, progression, self_state)
            try:
                from tabbybot.character import character_page
                payload["page"] = character_page(player, equipment, progression, self_state)
            except Exception as exc:
                payload["page"] = None
                log.info("角色頁面資料整理失敗：%s", type(exc).__name__)
            payload["error"] = self.character.get("error") or ""
            payload["status"] = "已更新"
            payload["updated_at"] = time.time()
            self.character = payload
            self._character_at = now
            return dict(self.character)
        finally:
            self._character_inflight = False

    def maybe_refresh_character(self) -> None:
        controller = self._controller()
        if controller is None or not getattr(controller, "world_ready", False):
            return
        if self._character_inflight or time.monotonic() - self._character_at < 8:
            return

        def worker() -> None:
            try:
                self.refresh_character(force=False)
            except Exception as exc:
                self.character["error"] = str(exc)
                log.info("讀取角色資料失敗：%s", type(exc).__name__)

        threading.Thread(target=worker, daemon=True, name="tabbybot-character").start()

    def change_equipment(self, *, action: str, confirm: bool, player_item_id: object = None, slot_type: object = None) -> dict[str, Any]:
        if not confirm:
            raise GuiError("請先在介面確認後再變更裝備。")
        if action not in {"equip", "unequip"}:
            raise GuiError("不支援這種裝備操作。")
        token = self._ensure_token()
        if action == "equip":
            if player_item_id is None or player_item_id == "":
                raise GuiError("缺少物品編號，無法穿戴。")
            result = equip_item(self.config, token, player_item_id)
            log.info("已送出穿戴請求：物品 #%s", player_item_id)
        else:
            slot = str(slot_type or "").strip().upper()
            if not slot:
                raise GuiError("缺少部位，無法卸下。")
            result = unequip_item(self.config, token, slot)
            log.info("已送出卸下請求：部位 %s", slot)
        try:
            self.refresh_character(force=True)
        except Exception:
            log.info("裝備操作後重新整理角色資料未完成。")
        return {"ok": True, "character": dict(self.character), "result_ok": bool(result) or True}

    def request_travel(self, map_id: int) -> str:
        controller = self._controller()
        if controller is None or not getattr(controller, "world_ready", False):
            raise GuiError("請先連線進入遊戲，才能切圖。")
        if map_id not in MAP_NAMES:
            raise GuiError("未知的地圖。")
        if map_id in SPECIAL_ENTRY_MAPS:
            raise GuiError("%s 需要特殊道具，不會從這裡進入。" % MAP_NAMES.get(map_id, map_id))
        controller.movement_paused = False
        controller.hunt_idle = False
        controller.stop_after_kill = False
        controller.clear_manual_move()
        self._remember_pin(map_id)
        controller.pin_map(map_id)
        controller.begin_travel_to(map_id)
        if controller.travel_phase is None and controller.position and controller.position.map_id == map_id:
            return "已經在%s。" % MAP_NAMES[map_id]
        if controller.travel_phase is None:
            raise GuiError(controller.status or "目前無法切圖。")
        log.info("已開始前往 %s。", MAP_NAMES.get(map_id, map_id))
        return controller.status or ("前往%s" % MAP_NAMES[map_id])

    def request_move(self, x: float, y: float) -> str:
        controller = self._controller()
        if controller is None or not getattr(controller, "world_ready", False):
            raise GuiError("請先連線進入遊戲，才能移動。")
        try:
            controller.begin_manual_move(float(x), float(y))
        except Exception as exc:
            raise GuiError(str(exc)) from exc
        return controller.status

    def use_consumable(self, kind: str, *, confirm: bool) -> str:
        if not confirm:
            raise GuiError("請先在介面確認後再使用道具。")
        controller = self._controller()
        if controller is None or not getattr(controller, "world_ready", False):
            raise GuiError("請先連線進入遊戲，才能使用道具。")
        try:
            return controller.request_use_consumable(kind)
        except Exception as exc:
            raise GuiError(str(exc)) from exc

    def send_chat(self, message: str) -> str:
        controller = self._controller()
        if controller is None or not getattr(controller, "world_ready", False):
            raise GuiError("請先連線進入遊戲，才能聊天。")
        try:
            return controller.request_chat(message, "world")
        except Exception as exc:
            raise GuiError(str(exc)) from exc

    def _load_settings(self) -> None:
        self._saved_settings: dict[str, Any] = {}
        try:
            data = json.loads(self.settings_path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        self._saved_settings = data
        if isinstance(data.get("combat"), dict):
            try:
                self.combat_options = clean_combat_options(data["combat"], COMBAT_DEFAULTS)
            except GuiError:
                pass
        self._apply_saved_quest_options()


    def _saved_pinned_map(self) -> int | None:
        hunt = self._saved_settings.get("hunt") if isinstance(getattr(self, "_saved_settings", None), dict) else None
        if not isinstance(hunt, dict) or hunt.get("pinned_map") is None:
            return None
        try:
            mid = int(hunt["pinned_map"])
        except (TypeError, ValueError):
            return None
        from tabbybot.constants import MAP_NAMES
        return mid if mid in MAP_NAMES else None

    def _remember_pin(self, map_id: int) -> None:
        """Persist a map the user (or an explicit start/travel) picked."""
        self.pinned_map = int(map_id)
        self.config.hunt.map_id = int(map_id)
        self._save_settings()

    def _apply_saved_quest_options(self) -> None:
        if not hasattr(self, "quest_options"):
            return
        quest = self._saved_settings.get("quest") if isinstance(getattr(self, "_saved_settings", None), dict) else None
        if not isinstance(quest, dict):
            return
        for key in ("auto_quest", "task_limited", "quest_map_auto", "auto_replace", "skip_vendetta", "quest_reroll", "auto_farm"):
            if key in quest:
                self.quest_options[key] = bool(quest[key])
        if "no_kite_level" in quest:
            try:
                self.quest_options["no_kite_level"] = max(0, min(200, int(quest["no_kite_level"])))
            except (TypeError, ValueError):
                pass
        if "farm_target_level" in quest:
            try:
                level = int(quest["farm_target_level"])
            except (TypeError, ValueError):
                return
            if 1 <= level <= 9999:
                self.quest_options["farm_target_level"] = level

    def _save_settings(self) -> None:
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"combat": self.combat_options}
            if hasattr(self, "quest_options"):
                payload["quest"] = dict(self.quest_options)
            if getattr(self, "pinned_map", None):
                payload["hunt"] = {"pinned_map": int(self.pinned_map)}
            self.settings_path.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
        except OSError:
            log.info("設定檔無法寫入。")

    def update_combat_options(self, options: dict[str, Any]) -> dict[str, Any]:
        self.combat_options = clean_combat_options(options or {}, self.combat_options)
        self._save_settings()
        controller = self._holder.get("controller")
        if controller is not None:
            controller.apply_combat_options(self.combat_options)
        log.info("已更新戰鬥設定：精英藥水門檻 %s 瓶、精英偵測範圍 %s、被玩家攻擊時喝水 %s、優先打精英 %s、自動喝水 %s",
                 self.combat_options["elite_potion_min"],
                 self.combat_options.get("elite_view_range", 700),
                 "開" if self.combat_options["potion_on_player_damage"] else "關",
                 "開" if self.combat_options["prefer_elite"] else "關",
                 "開" if self.combat_options["auto_potion"] else "關")
        log.info("風箏 %s，僅風箏精英 %s",
                 "開" if self.combat_options["cluster_kite"] else "關",
                 "開" if self.combat_options.get("kite_elite_only") else "關")
        return dict(self.combat_options)

    def map_level_text(self, map_id: Any) -> str:
        """'Lv10-30' from ~/.tabbybot/map_levels.json (reloaded at most every 20 s), or '未知'."""
        from tabbybot.farm import MapLevelStore, map_level_stats
        now = time.monotonic()
        controller = self._holder.get("controller")
        store = getattr(controller, "level_store", None) if controller is not None else None
        catalog = store.catalog if store is not None and getattr(store, "path", None) is not None else None
        if catalog is None:
            if getattr(self, "_map_levels_at", 0.0) == 0.0 or now - self._map_levels_at > 20:
                self._map_levels = MapLevelStore(self.vault_path.parent / "map_levels.json").catalog
                self._map_levels_at = now
            catalog = self._map_levels
        try:
            stats = map_level_stats(catalog.get(int(map_id), []))
        except (TypeError, ValueError):
            stats = None
        if not stats:
            return "未知"
        return f"Lv{stats['min']}" if stats["min"] == stats["max"] else f"Lv{stats['min']}-{stats['max']}"

    def map_with_levels(self, map_id: Any, name: str) -> str:
        if not name:
            return name
        return f"{name} ({self.map_level_text(map_id)})"

    def _quest_rows_with_levels(self, quests: list) -> list:
        rows = format_quest_rows(quests)
        for row in rows:
            if row.get("map_id") is not None and row.get("map_name"):
                row["map_name"] = self.map_with_levels(row["map_id"], row["map_name"])
        return rows

    def current_alert(self) -> dict[str, Any] | None:
        watchdog = self._holder.get("watchdog")
        alert = getattr(watchdog, "alert", None) if watchdog is not None else None
        return dict(alert) if isinstance(alert, dict) else None

    def clear_alert(self) -> None:
        watchdog = self._holder.get("watchdog")
        if watchdog is not None:
            watchdog.alert = None

    def _session_stats(self, controller: Any) -> dict[str, Any]:
        """Totals since this hunt session started (kills, exp gained, stamina used, equipment gained)."""
        def num(v: Any) -> float | None:
            try:
                return float(str(v).rstrip("%"))
            except (TypeError, ValueError):
                return None
        if controller is None:
            return {}
        stats = {r.get("key"): r.get("value") for r in (self.character.get("stats") or []) if isinstance(r, dict)}
        lv = num(stats.get("level"))
        exp = str(stats.get("experience") or "")
        cur = mx = None
        if "/" in exp:
            cur, mx = num(exp.split("/")[0]), num(exp.split("/")[1])
        sta = num(stats.get("stamina"))
        equip = {str(i.get("id")) for i in (self.character.get("inventory") or []) if isinstance(i, dict) and i.get("slot")}
        equip |= {str(i.get("id")) for i in (self.character.get("equipment") or []) if isinstance(i, dict) and i.get("id")}
        sess = getattr(self, "_sess", None)
        if lv is None and sess is None:
            return {}
        if sess is None or sess.get("ctl") != id(controller):
            sess = {"ctl": id(controller), "lv": lv, "cur": cur, "mx": mx, "sta": sta, "exp": 0.0, "sta_used": 0.0,
                    "seen": set(equip), "new": set(), "since": time.time()}
            self._sess = sess
        if lv is not None and cur is not None and sess["lv"] is not None and sess["cur"] is not None:
            if lv > sess["lv"]:
                sess["exp"] += max(0.0, (sess["mx"] or 0) - sess["cur"]) + cur
            elif lv == sess["lv"] and cur > sess["cur"]:
                sess["exp"] += cur - sess["cur"]
        if lv is not None and cur is not None:
            sess["lv"], sess["cur"], sess["mx"] = lv, cur, mx
        if sta is not None:
            if sess["sta"] is not None and sta < sess["sta"]:
                sess["sta_used"] += sess["sta"] - sta
            sess["sta"] = sta
        if time.time() - sess["since"] < 90 and not sess["new"] and (not sess["seen"] or sess["lv"] is None):
            sess["seen"] |= equip  # character data still loading: extend baseline instead of counting
        else:
            sess["new"] |= equip - sess["seen"]
        try:
            kills = int(controller.kill_count())
        except Exception:
            kills = 0
        return {"kills": kills, "exp": int(sess["exp"]), "stamina_used": round(sess["sta_used"], 2),
                "equipment": len(sess["new"]), "minutes": int((time.time() - sess["since"]) / 60)}

    def state(self) -> dict[str, Any]:
        controller = self._holder.get("controller")
        self.maybe_refresh_character()
        hunt = controller.map_payload() if controller is not None else {
            "kills": 0, "damage": 0, "hits": 0, "potions": 0,
            "monsters": [], "peers": [], "route": [], "bounds": None,
            "status": self.hunt_error or ("打怪進行中，正在連線…" if self.hunt_running else "尚未開始打怪"),
            "activity": "",
            "world_ready": False,
            "dead": False,
            "map_id": None,
            "map_name": "",
            "map_title": "",
            "x": None,
            "y": None,
            "ticket_ready": False,
            "rtt_ms": None,
        }
        if hunt.get("farm_map") is not None and hunt.get("farm_map_name"):
            hunt["farm_map_name"] = self.map_with_levels(hunt["farm_map"], hunt["farm_map_name"])
        if hunt.get("map_id") is not None and hunt.get("map_name"):
            hunt["map_level_text"] = self.map_level_text(hunt["map_id"])
        connected = bool(controller is not None and getattr(controller, "world_ready", False))
        maps = []
        for map_id, label in MAP_BUTTONS.items():
            maps.append({
                "id": map_id,
                "label": self.map_with_levels(map_id, label),
                "title": MAP_TITLES.get(map_id, label),
                "levels": self.map_level_text(map_id),
                "special": map_id in SPECIAL_ENTRY_MAPS,
                "has_nav": map_id in MAP_FILES,
            })
        pill = "尚未解鎖"
        if self.unlocked():
            if connected:
                rtt = hunt.get("rtt_ms")
                pill = "已連線" + (f" · {rtt} ms" if rtt is not None else "")
                if getattr(controller, "stop_after_kill", False):
                    pill += " · 打完後暫停"
                elif getattr(controller, "hunt_idle", False):
                    pill += " · 待命"
            elif self.hunt_running:
                pill = "連線中…"
            else:
                pill = "已解鎖，尚未連線"
        return {
            "has_vault": self.has_vault(),
            "unlocked": self.unlocked(),
            "auto_unlock": self.auto_unlock_enabled(),
            "address": short_address(self.address) if self.address else "",
            "hunt_running": self.hunt_running,
            "hunt_paused": bool(controller is not None and (
                getattr(controller, "hunt_idle", False) or getattr(controller, "stop_after_kill", False)
            )),
            "market_running": self.market_running,
            "hunt_error": self.hunt_error,
            "alert": self.current_alert(),
            "combat": dict(self.combat_options),
            "efficiency": self.efficiency.table(),
            "webhook": self.webhook.settings(),
            "market_error": self.market_error,
            "connected": connected,
            "pill": pill,
            "hunt": hunt,
            "options": {
                "map_id": int(self.pinned_map or self.config.hunt.map_id or DEFAULT_HUNT_MAP),
                "elite_only": self.config.hunt.elite_only,
                "auto_combat": True if controller is None else bool(controller.auto_combat),
                "auto_return": True if controller is None else bool(controller.auto_return),
                "auto_potion_supported": True,
                "auto_quest": self.quest_options["auto_quest"] if controller is None else bool(controller.auto_quest),
                "task_limited": self.quest_options["task_limited"] if controller is None else bool(controller.task_limited),
                "quest_map_auto": self.quest_options["quest_map_auto"] if controller is None else bool(controller.quest_map_auto),
                "auto_replace": self.quest_options["auto_replace"] if controller is None else bool(controller.auto_replace),
                "skip_vendetta": self.quest_options.get("skip_vendetta", False) if controller is None else bool(controller.skip_vendetta),
                "quest_reroll": self.quest_options.get("quest_reroll", False) if controller is None else bool(getattr(controller, "quest_reroll", False)),
                "no_kite_level": self.quest_options.get("no_kite_level", 30) if controller is None else int(getattr(controller, "no_kite_level", 30)),
                "auto_farm": self.quest_options.get("auto_farm", True) if controller is None else bool(controller.auto_farm),
                "farm_target_level": self.quest_options.get("farm_target_level", DEFAULT_FARM_LEVEL) if controller is None else int(controller.farm_target_level),
                "farm_map": None if controller is None else getattr(controller, "farm_map", lambda: None)(),
                "name_contains": self.config.market.name_contains,
                "max_price": self.config.market.max_price,
                "min_price": self.config.market.min_price,
            },
            "quests": self._quest_rows_with_levels(controller.quests if controller is not None and controller.quests else self.quests),
            "quest_status": (controller.quest_status if controller is not None and controller.quest_status else self.quest_status),
            "quest_error": self.quest_error,
            "daily_running": self.daily_running,
            "pending_delivery": self._current_delivery_prompt(),
            "replace_note": REPLACE_UNAVAILABLE,
            "maps": maps,
            "market": {
                "summary": self.market_summary,
                "matches": list(self.market_matches),
            },
            "logs": list(self.logs)[-80:],
            "character": dict(self.character),
            "session": self._session_stats(controller),
            "chat": list(getattr(controller, "chat_log", [])) if controller is not None else [],
        }


def navgrid_payload(map_id: int) -> dict[str, Any]:
    """Decoded walkable cells + obstacles (from the game package) and learned server blocks, for the minimap."""
    from tabbybot.navigation import navigator_for
    from tabbybot import learned_nav
    out: dict[str, Any] = {"map_id": map_id, "cell_size": 0, "cells": [], "obstacles": [],
                           "learned_cell": learned_nav.CELL, "blocked": []}
    nav = navigator_for(map_id) if map_id else None
    if nav is not None:
        data = nav.data
        out["cell_size"] = data.cell_size
        out["cells"] = sorted([list(c) for c in data.cells])
        out["obstacles"] = [[round(o.x), round(o.y), round(o.rx, 1), round(o.ry, 1)] for o in data.obstacles]
    try:
        grid = learned_nav.learned_grid(map_id) if map_id else None
        if grid is not None:
            out["blocked"] = sorted([list(c) for c in grid.blocked])
    except Exception:  # noqa: BLE001 - minimap extras must never break the page
        pass
    return out


def _send_json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def _send_file(handler: BaseHTTPRequestHandler, path: Path, content_type: str) -> None:
    data = path.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def make_handler(app: GuiApp):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            # Never include request bodies; path only.
            path = self.path.split("?", 1)[0]
            log.debug("GUI %s %s", self.command, path)

        def _forbid_if_remote(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0].lower()
            if host not in {"127.0.0.1", "localhost", ""}:
                self.send_error(403, "Local access only")
                return True
            return False

        def do_GET(self) -> None:  # noqa: N802
            if self._forbid_if_remote():
                return
            path = urlparse(self.path).path
            if path in {"/", "/index.html"}:
                _send_file(self, WEB_DIR / "index.html", "text/html; charset=utf-8")
                return
            if path == "/app.css":
                _send_file(self, WEB_DIR / "app.css", "text/css; charset=utf-8")
                return
            if path == "/app.js":
                _send_file(self, WEB_DIR / "app.js", "application/javascript; charset=utf-8")
                return
            if path.startswith("/assets/"):
                name = path[len("/assets/"):]
                target = WEB_DIR / "assets" / name
                types = {".webp": "image/webp", ".png": "image/png", ".svg": "image/svg+xml"}
                if "/" in name or ".." in name or not target.is_file() or target.suffix not in types:
                    self.send_error(404, "Not found")
                    return
                _send_file(self, target, types[target.suffix])
                return
            if path == "/api/state":
                _send_json(self, 200, {"ok": True, **app.state()})
                return
            if path == "/api/update/check":
                from tabbybot import updater
                _send_json(self, 200, {"ok": True, **updater.check()})
                return
            if path == "/api/navgrid":
                from urllib.parse import parse_qs
                try:
                    map_id = int((parse_qs(urlparse(self.path).query).get("map") or ["0"])[0])
                except ValueError:
                    map_id = 0
                _send_json(self, 200, {"ok": True, **navgrid_payload(map_id)})
                return
            self.send_error(404, "Not found")

        def do_POST(self) -> None:  # noqa: N802
            if self._forbid_if_remote():
                return
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            if length > 1_000_000:
                _send_json(self, 400, {"ok": False, "error": "內容太大。"})
                return
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                _send_json(self, 400, {"ok": False, "error": "無法解析內容。"})
                return
            if not isinstance(body, dict):
                _send_json(self, 400, {"ok": False, "error": "內容必須是物件。"})
                return
            try:
                result = self._dispatch(path, body)
            except (GuiError, WalletError, ConfigError, LoginError, ClientError) as exc:
                _send_json(self, 400, {"ok": False, "error": str(exc)})
                return
            except Exception:
                log.exception("介面操作失敗")
                _send_json(self, 500, {"ok": False, "error": "發生未預期的錯誤，請查看執行視窗。"})
                return
            _send_json(self, 200, result)

        def _dispatch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
            # Drop secret fields before any incidental logging/debug.
            safe = redact_mapping({k: v for k, v in body.items() if k not in {
                "secret", "password", "confirm", "private_key", "mnemonic",
            }})
            if path == "/api/update/apply":
                from tabbybot import updater
                try:
                    how = updater.apply()
                except Exception as exc:
                    raise GuiError(f"更新失敗：{exc}") from exc
                updater.restart_later()
                return {"ok": True, "how": how}
            if path == "/api/wallet/preview":
                from tabbybot.wallet import mnemonic_addresses
                try:
                    return {"ok": True, "addresses": mnemonic_addresses(str(body.get("secret") or ""))}
                except Exception:
                    return {"ok": True, "addresses": []}
            if path == "/api/setup":
                address = app.setup(
                    str(body.get("secret") or ""),
                    str(body.get("password") or ""),
                    str(body.get("confirm") or ""),
                    remember=bool(body.get("remember", True)),
                )
                return {"ok": True, "address": short_address(address)}
            if path == "/api/unlock":
                address = app.unlock(
                    str(body.get("password") or ""),
                    remember=bool(body.get("remember", True)),
                )
                return {"ok": True, "address": short_address(address)}
            if path == "/api/auto-unlock":
                app.set_auto_unlock(bool(body.get("enabled", True)))
                return {"ok": True, "auto_unlock": app.auto_unlock_enabled()}
            if path == "/api/lock":
                app.lock_wallet()
                return {"ok": True}
            if path == "/api/hunt/start":
                app.start_hunt(body)
                return {"ok": True}
            if path == "/api/efficiency/reset":
                app.efficiency.reset()
                log.info("已重設打怪效率紀錄。")
                return {"ok": True, "efficiency": app.efficiency.table()}
            if path == "/api/combat/options":
                return {"ok": True, "combat": app.update_combat_options(body)}
            if path == "/api/webhook/save":
                from tabbybot.notifier import WebhookError
                try:
                    app.webhook.save(str(body.get("url", "")), str(body.get("key", "") or "") or None,
                                     clear_key=bool(body.get("clear_key", False)))
                except WebhookError as exc:
                    raise GuiError(str(exc)) from exc
                return {"ok": True, "webhook": app.webhook.settings()}
            if path == "/api/webhook/test":
                result = app.webhook.send_test()
                return {"ok": True, "result": result, "webhook": app.webhook.settings()}
            if path == "/api/alert/clear":
                app.clear_alert()
                return {"ok": True}
            if path == "/api/hunt/stop":
                app.stop_hunt()
                log.info("已停止打怪（不會登出）。")
                return {"ok": True}
            if path == "/api/hunt/stop-now":
                app.stop_hunt(immediate=True)
                log.info("已立刻停止打怪（不會登出）。")
                return {"ok": True}
            if path == "/api/hunt/stop-move":
                app.stop_move()
                return {"ok": True}
            if path == "/api/hunt/options":
                app.update_hunt_options(body)
                return {"ok": True}
            if path == "/api/market/start":
                app.start_market(body)
                return {"ok": True}
            if path == "/api/market/stop":
                app.stop_market()
                log.info("已停止商城監控。")
                return {"ok": True}
            if path == "/api/quests/refresh":
                quests = app.refresh_quests()
                return {"ok": True, "quests": format_quest_rows(quests), "quest_status": app.quest_status}
            if path == "/api/quests/start":
                app.start_daily(body)
                return {"ok": True}
            if path == "/api/quests/stop":
                app.stop_daily()
                return {"ok": True}
            if path == "/api/quests/options":
                app.update_quest_options(body)
                return {"ok": True}
            if path == "/api/quests/deliver":
                app.confirm_delivery()
                return {"ok": True}
            if path == "/api/quests/cancel":
                app.cancel_delivery_quest()
                return {"ok": True}
            if path == "/api/quests/skip":
                app.skip_delivery()
                return {"ok": True}
            if path == "/api/character/refresh":
                payload = app.refresh_character(force=True)
                return {"ok": True, "character": payload}
            if path == "/api/character/equip":
                return app.change_equipment(
                    action="equip",
                    confirm=bool(body.get("confirm")),
                    player_item_id=body.get("player_item_id", body.get("playerItemId")),
                )
            if path == "/api/character/unequip":
                return app.change_equipment(
                    action="unequip",
                    confirm=bool(body.get("confirm")),
                    slot_type=body.get("slot_type", body.get("slotType")),
                )
            if path == "/api/hunt/travel":
                try:
                    map_id = int(body.get("map_id"))
                except (TypeError, ValueError) as exc:
                    raise GuiError("地圖編號無效") from exc
                message = app.request_travel(map_id)
                return {"ok": True, "message": message}
            if path == "/api/hunt/move":
                try:
                    x = float(body.get("x"))
                    y = float(body.get("y"))
                except (TypeError, ValueError) as exc:
                    raise GuiError("座標無效") from exc
                message = app.request_move(x, y)
                return {"ok": True, "message": message}
            if path == "/api/item/use":
                message = app.use_consumable(str(body.get("kind") or ""), confirm=bool(body.get("confirm")))
                return {"ok": True, "message": message}
            if path == "/api/chat/send":
                message = app.send_chat(str(body.get("message") or ""))
                return {"ok": True, "message": message}
            raise GuiError("找不到這個操作。")

    return Handler


def run_gui(
    config: AppConfig,
    vault_path: Path | None = None,
    port: int = 8765,
    open_browser: bool = True,
    ready: dict[str, Any] | None = None,
    keychain: KeychainStore | None = None,
    auto_unlock: bool = True,
) -> int:
    configure_logging(logging.INFO)
    app = GuiApp(config, vault_path or default_vault_path(), keychain=keychain)
    if auto_unlock:
        app.try_auto_unlock()
    httpd = ThreadingHTTPServer(("127.0.0.1", int(port)), make_handler(app))
    actual = httpd.server_address[1]
    url = f"http://127.0.0.1:{actual}/"
    log.info("本機介面已啟動：%s（只允許這台電腦開啟）", url)
    if ready is not None:
        ready["port"] = actual
        ready["url"] = url
        ready["app"] = app
        ready["httpd"] = httpd
        event = ready.get("event")
        if event is not None:
            event.set()
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            log.info("請用瀏覽器手動開啟 %s", url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已關閉介面。")
    finally:
        httpd.server_close()
        app.close()
    return 0
