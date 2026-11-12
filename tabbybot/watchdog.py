"""Local stuck watchdog: drop stale targets, re-login in-process, keep hunting overnight.

Alerts go to ~/.tabbybot/alerts.log, a macOS notification, and the optional webhook.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from tabbybot.constants import (
    WATCHDOG_ESCALATE_COUNT,
    WATCHDOG_ESCALATE_RETRY_SEC,
    WATCHDOG_ESCALATE_WINDOW_SEC,
    WATCHDOG_NO_EXP_SEC,
    WATCHDOG_NO_ATTACK_SEC,
    WATCHDOG_NO_MOVE_SEC,
    WATCHDOG_STALE_TARGET_SEC,
    WATCHDOG_STALE_WORLD_SEC,
    WATCHDOG_RELOGIN_MIN_SEC,
)

log = logging.getLogger(__name__)

ALERTS_LOG = Path.home() / ".tabbybot" / "alerts.log"
# Soft-stop / user-idle kinds are ignored. Travel still checks movement.
HUNTING_KINDS = {
    "attack", "wait_ticket", "chase", "patrol", "resync",
    "wait", "rest", "travel", "portal", "manual",
}
MOVE_KINDS = {"travel", "portal", "manual"}


def default_notify(title: str, message: str) -> None:
    if sys.platform != "darwin":
        return
    def esc(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{esc(message)}" with title "{esc(title)}" sound name "Glass"'
    try:
        subprocess.Popen(["/usr/bin/osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def write_alert_line(text: str, path: Path | None = None) -> None:
    target = path or ALERTS_LOG
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
    except OSError:
        pass


def normalize_status(text: str) -> str:
    text = re.sub(r"\b[0-9a-f]{8}\b", "#", str(text or ""))
    return re.sub(r"\d+", "N", text)


def _exp_key(controller: Any) -> tuple:
    state = getattr(controller, "self_state", {}) or {}
    exp = state.get("experience", state.get("exp"))
    level = state.get("level") or state.get("monsterLevel")
    efficiency = getattr(controller, "efficiency", None)
    polled = getattr(efficiency, "_last_exp", None) if efficiency is not None else None
    return (exp, polled, level)


def _is_hunting(controller: Any, kind: str) -> bool:
    if getattr(controller, "hunt_idle", False):
        return False
    if getattr(controller, "is_dead", False):
        return False
    if not bool(getattr(controller, "auto_combat", True)):
        return False
    return kind in HUNTING_KINDS


def _is_resting(controller: Any) -> bool:
    if not bool(getattr(controller, "resting", False)):
        return False
    ratio = None
    if hasattr(controller, "hp_ratio"):
        try:
            ratio = controller.hp_ratio()
        except Exception:
            ratio = None
    # Frozen "回血中" at full HP is not a real rest.
    if ratio is not None and ratio >= 0.76:
        return False
    return True


class StuckWatchdog:
    def __init__(
        self,
        *,
        notify: Callable[[str, str], None] | None = None,
        hook: Callable[[dict[str, Any]], Any] | None = None,
        alerts_path: Path | None = None,
        no_exp_sec: float = WATCHDOG_NO_EXP_SEC,
        no_attack_sec: float = WATCHDOG_NO_ATTACK_SEC,
        no_move_sec: float = WATCHDOG_NO_MOVE_SEC,
        stale_target_sec: float = WATCHDOG_STALE_TARGET_SEC,
        stale_world_sec: float = WATCHDOG_STALE_WORLD_SEC,
        relogin_min_sec: float = WATCHDOG_RELOGIN_MIN_SEC,
        escalate_count: int = WATCHDOG_ESCALATE_COUNT,
        escalate_window_sec: float = WATCHDOG_ESCALATE_WINDOW_SEC,
        escalate_retry_sec: float = WATCHDOG_ESCALATE_RETRY_SEC,
        # older names kept so existing callers don't break
        no_progress_sec: float | None = None,
        repeat_sec: float | None = None,
        grace_sec: float | None = None,
    ) -> None:
        self.notify = notify or default_notify
        self.hook = hook
        self._controller: Any = None
        self.alerts_path = alerts_path
        self.no_exp_sec = float(no_progress_sec if no_progress_sec is not None else no_exp_sec)
        self.no_attack_sec = float(no_attack_sec)
        self.no_move_sec = no_move_sec
        self.stale_target_sec = stale_target_sec
        self.stale_world_sec = stale_world_sec
        self.relogin_min_sec = relogin_min_sec
        self.escalate_count = escalate_count
        self.escalate_window_sec = escalate_window_sec
        self.escalate_retry_sec = escalate_retry_sec
        self.alert: dict[str, Any] | None = None
        self.recovered_at: float | None = None
        self.stopped = False  # never auto-stops overnight; kept for payload compatibility
        self._exp_key: tuple | None = None
        self._exp_at: float | None = None
        self._pos: tuple[int, int, int] | None = None
        self._pos_at: float | None = None
        self._target_key: tuple | None = None
        self._target_at: float | None = None
        self._attempts: list[float] = []
        self._last_action: str | None = None
        self._last_action_at: float = 0.0
        self._last_relogin_at: float = 0.0
        self._escalated = False
        self._dmg_at: float = 0.0
        self._dmg_drop_at: float = 0.0

    def _reset_baselines(self, now: float) -> None:
        self._exp_at = now
        self._pos_at = now
        self._target_at = now

    def _on_progress(self, now: float) -> None:
        if self.alert:
            write_alert_line("恢復正常：經驗或戰鬥已繼續", self.alerts_path)
            log.info("卡住警報解除：經驗或戰鬥已繼續。")
        self.alert = None
        self.recovered_at = None
        self._last_action = None
        self._escalated = False
        self._attempts = [t for t in self._attempts if now - t < self.escalate_window_sec]

    def observe(self, controller: Any, kind: str, now: float | None = None) -> str | None:
        """Return None, "drop", or "relogin"."""
        now = time.monotonic() if now is None else now
        self._controller = controller

        exp_key = _exp_key(controller)
        if exp_key != self._exp_key or self._exp_at is None:
            if self._exp_key is not None and exp_key != self._exp_key:
                self._on_progress(now)
            self._exp_key = exp_key
            self._exp_at = now

        pos = controller.position
        pos_key = (pos.map_id, int(pos.x // 40), int(pos.y // 40)) if pos is not None else None
        if pos_key != self._pos or self._pos_at is None:
            self._pos = pos_key
            self._pos_at = now

        target_key = self._target_snapshot(controller)
        if target_key != self._target_key or self._target_at is None:
            self._target_key = target_key
            self._target_at = now

        login_error = bool(getattr(controller, "auth_rejected", False)) or bool(
            getattr(controller, "login_error", False)
        )
        stale_world = self._stale_world(controller, now)
        hunting = _is_hunting(controller, kind)

        if not hunting and not login_error and not stale_world:
            self._pos_at = now
            self._target_at = now
            self._exp_at = now
            return None

        # No damage dealt for 10 s while hunting with mobs around -> retarget; 40 s -> re-login.
        hit_at = float(getattr(controller, "last_own_hit_at", 0.0) or 0.0)
        if not self._dmg_at or not hunting or kind in MOVE_KINDS or _is_resting(controller) or hit_at > self._dmg_at:
            self._dmg_at = max(now if not hunting or kind in MOVE_KINDS or _is_resting(controller) else hit_at, hit_at)
            self._dmg_drop_at = 0.0
        elif self._dmg_at and now - self._dmg_at >= 10.0 and not login_error:
            try:
                has_mobs = bool(controller.candidates())
            except Exception:
                has_mobs = True
            if has_mobs:
                if now - self._dmg_at >= 40.0:
                    self._dmg_at = now
                    return self._act("40 秒沒有造成傷害", now, style="relogin")
                if now - (self._dmg_drop_at or 0.0) >= 10.0 and not _chasing_hit_elite(controller):
                    self._dmg_drop_at = now
                    return self._act("10 秒沒有造成傷害，換目標", now, style="drop")

        reason = None
        style = "drop"  # drop = resync; relogin = no EXP; hard = LoginError
        if login_error:
            reason = "登入失敗或會話已失效（LoginError）"
            style = "hard"
        elif hunting and now - (now if self._exp_at is None else self._exp_at) >= self.no_exp_sec:
            mins = int((now - self._exp_at) // 60)
            reason = f"{mins} 分鐘經驗沒有增加"
            style = "relogin"
        elif stale_world:
            reason = "世界狀態過期（一段時間沒有收到地圖快照）"
            style = "drop"
        elif hunting and kind in MOVE_KINDS and now - (now if self._pos_at is None else self._pos_at) >= self.no_move_sec:
            mins = int((now - self._pos_at) // 60)
            reason = f"移動中但 {mins} 分鐘沒有前進（可能卡在牆邊）"
        elif hunting and not _is_resting(controller) and now - (now if self._pos_at is None else self._pos_at) >= self.no_move_sec:
            mins = int((now - self._pos_at) // 60)
            reason = f"位置 {mins} 分鐘沒變（非回血）"
        elif hunting and self._moving_no_attack(controller, kind, now):
            reason = "移動中 60 秒沒有出手也沒有經驗"
            # First time: drop and retarget. If it happens again, re-login.
            if self._last_action == "drop" and (now - self._last_action_at) >= self.no_attack_sec:
                style = "relogin"
            else:
                style = "drop"
        elif hunting and self._frozen_target(now):
            reason = "同一目標距離或血量 60 秒沒變，疑似過期目標"
            if self._last_action == "drop" and (now - self._last_action_at) >= self.no_attack_sec:
                style = "relogin"

        if reason is None:
            return None
        return self._act(reason, now, style=style)

    def _stale_world(self, controller: Any, now: float) -> bool:
        if not getattr(controller, "world_ready", False) and not getattr(controller, "last_snapshot_at", 0):
            return False
        stamped = float(getattr(controller, "last_snapshot_at", 0.0) or 0.0)
        if stamped <= 0:
            return False
        return (now - stamped) >= self.stale_world_sec

    def _target_snapshot(self, controller: Any) -> tuple | None:
        eid = getattr(controller, "attack_target", None) or getattr(controller, "chase_target", None)
        if not eid:
            return None
        mon = {}
        cache = getattr(controller, "monsters", None)
        if cache is not None:
            mon = (getattr(cache, "monsters", {}) or {}).get(eid) or {}
        try:
            hp = int(mon.get("health", 0) or 0)
        except (TypeError, ValueError):
            hp = 0
        dist_bucket = None
        pos = getattr(controller, "position", None)
        if pos is not None and mon.get("x") is not None and mon.get("y") is not None:
            try:
                dist = ((float(mon["x"]) - pos.x) ** 2 + (float(mon["y"]) - pos.y) ** 2) ** 0.5
                dist_bucket = int(dist // 20)
            except (TypeError, ValueError):
                dist_bucket = None
        return (str(eid), hp, dist_bucket)


    def _in_attack_reach(self, controller: Any) -> bool:
        pos = getattr(controller, "position", None)
        cache = getattr(controller, "monsters", None)
        if pos is None or cache is None:
            return False
        reach = 320.0
        living = getattr(cache, "living_on_map", None)
        rows = living(pos.map_id) if callable(living) else []
        for _eid, monster in rows:
            try:
                dist = ((float(monster["x"]) - pos.x) ** 2 + (float(monster["y"]) - pos.y) ** 2) ** 0.5
            except (KeyError, TypeError, ValueError):
                continue
            if dist <= reach:
                return True
        return False

    def _moving_no_attack(self, controller: Any, kind: str, now: float) -> bool:
        if kind not in {"chase", "patrol"}:
            return False
        if _is_resting(controller):
            return False
        if not self._in_attack_reach(controller):
            return False
        last_atk = float(getattr(controller, "last_attack_at", 0.0) or 0.0)
        if last_atk <= 0:
            # Session start: require the full window from first observe.
            last_atk = self._exp_at if self._exp_at is not None else now
        if now - last_atk < self.no_attack_sec:
            return False
        exp_at = now if self._exp_at is None else self._exp_at
        return (now - exp_at) >= self.no_attack_sec

    def _frozen_target(self, now: float) -> bool:
        if self._target_key is None or self._target_at is None:
            return False
        if now - self._target_at < self.stale_target_sec:
            return False
        # Frozen means the same id + same HP + same distance bucket for the whole window.
        eid, hp, dist = self._target_key
        return bool(eid) and hp is not None and dist is not None

    def _recent_attempts(self, now: float) -> list[float]:
        self._attempts = [t for t in self._attempts if now - t < self.escalate_window_sec]
        return self._attempts

    def _act(self, reason: str, now: float, *, style: str) -> str | None:
        """style: hard (LoginError, always re-login), relogin (no EXP, rate-limited), drop (resync only)."""
        if style != "hard" and self._last_action is not None and now - self._last_action_at < 2.0:
            return None  # debounce the same observe ticks
        if style == "drop":
            self._raise(reason, "drop", "已放棄過期目標並改打其他怪")
            self._last_action = "drop"
            self._last_action_at = now
            self.recovered_at = now
            self._reset_baselines(now)
            return "drop"

        # Re-login: LoginError is immediate; no-EXP is at most once per 5 minutes.
        # "移動中 60 秒沒有出手" may relogin 60s after the retarget drop.
        if style != "hard":
            if self._escalated and now - self._last_action_at < self.escalate_retry_sec:
                return None
            relogin_floor = self.no_attack_sec if "沒有出手也沒有經驗" in reason else self.relogin_min_sec
            if (not self._escalated and self._last_relogin_at
                    and now - self._last_relogin_at < relogin_floor):
                return None
            recent = self._recent_attempts(now)
            if len(recent) + 1 >= self.escalate_count and not self._escalated:
                self._escalated = True
                self._raise(reason, "escalate", f"15 分鐘內已恢復失敗 {len(recent) + 1} 次，立刻重試登入")
                self._attempts.append(now)
                self._last_action = "relogin"
                self._last_action_at = now
                self._last_relogin_at = now
                self._reset_baselines(now)
                return "relogin"

        action = "已自動重新登入並準備回原地圖"
        if self._escalated:
            action = "嚴重卡住，正在重新登入（立刻重試）"
        self._raise(reason, "relogin", action)
        self._attempts.append(now)
        self._last_action = "relogin"
        self._last_action_at = now
        self._last_relogin_at = now
        self.recovered_at = now
        self._reset_baselines(now)
        return "relogin"

    def _raise(self, reason: str, level: str, action: str) -> None:
        prefix = "嚴重卡住警報" if level == "escalate" else "卡住警報"
        text = f"{prefix}：{reason}；{action}"
        self.alert = {"level": level, "reason": reason, "action": action, "text": text,
                      "time": datetime.now().strftime("%H:%M:%S")}
        log.warning("%s", text)
        write_alert_line(text, self.alerts_path)
        try:
            title = "Tabby 外掛（嚴重）" if level == "escalate" else "Tabby 外掛"
            self.notify(title, text)
        except Exception:
            pass
        if self.hook is not None:
            try:
                self.hook(self.webhook_payload(reason, level))
            except Exception:
                log.info("Grok Bot 通知送出失敗。")

    def webhook_payload(self, reason: str, level: str) -> dict[str, Any]:
        from tabbybot.constants import MAP_NAMES
        c = self._controller
        pos = getattr(c, "position", None)
        state = getattr(c, "self_state", {}) or {}
        return {
            "reason": reason,
            "map": MAP_NAMES.get(pos.map_id, str(pos.map_id)) if pos is not None else "",
            "x": round(pos.x) if pos is not None else None,
            "y": round(pos.y) if pos is not None else None,
            "status": str(getattr(c, "status", "")),
            "kills": int(getattr(c, "kills", 0) or 0),
            "exp": state.get("experience", state.get("exp")),
            "recovered": level in {"drop", "relogin"},
            "stopped": False,
            "relogin": level in {"relogin", "escalate"},
            "escalate": level == "escalate",
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }


def _chasing_hit_elite(controller) -> bool:
    """An elite we already damaged is still alive: keep after it until it dies (owner's rule)."""
    try:
        tid = getattr(controller, "attack_target", None) or getattr(controller, "chase_target", None)
        if not tid or tid not in getattr(controller, "_own_hit_ids", ()):
            return False
        mon = controller.monsters.monsters.get(tid) or {}
        return int(mon.get("health", 0)) > 0 and bool(controller.is_elite(mon))
    except Exception:
        return False
