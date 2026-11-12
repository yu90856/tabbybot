"""Daily quest helpers. Only auto-handles free monster-kill quests."""

from __future__ import annotations

import json
from typing import Any

from tabbybot.constants import MAP_NAMES, SPECIAL_ENTRY_MAPS

QUEST_ACCEPT_INTERVAL_SEC = 15.0
QUEST_REFRESH_INTERVAL_SEC = 20.0
# No monster quest left today: re-check at most this often (or right after the date changes).
QUEST_IDLE_RECHECK_SEC = 15 * 60.0

OBJECTIVE_MONSTER = "MONSTER"

QUEST_LIMIT_MARKERS = (
    "任務領取已滿",
    "任务领取已满",
    "領取已滿",
    "领取已满",
    "每日任務上限",
    "每日任务上限",
    "任務領取上限",
    "任务领取上限",
    "今日任務已達上限",
    "今日任务已达上限",
    "今日可領取任務已達上限",
    "今日可领取任务已达上限",
    "daily quest limit",
    "quest limit reached",
    "daily limit reached",
    "quest_accept_limit",
    "quest_acceptance_limit",
    "daily_quest_limit",
    "quest_daily_limit",
    "daily_task_limit",
    "daily_quest_cap",
)

SKIP_SPECIAL_MAP = "略過：需要特殊道具入圖，不會使用入場券"
NEEDS_DELIVERY_NOTE = "需確認後才會交付道具或裝備"
REPLACE_UNAVAILABLE = (
    "參考客戶端與公開協定裡沒有放棄、刷新或更換每日任務的接口。"
    "開啟「自動換掉」時，只會略過尚未接取的交付任務，改接打怪任務；"
    "已接取的交付任務仍需你確認交付，或到官方客戶端處理。"
)


def _as_int(value: object) -> int | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        if value is None or isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def extract_quests(payload: object) -> list[dict[str, Any]]:
    """Pull a quest list out of a /quests, progression, or snapshot payload."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("quests",):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    for nested_key in ("progression", "progress", "data", "payload"):
        nested = payload.get(nested_key)
        if nested is not None and nested is not payload:
            rows = extract_quests(nested)
            if rows:
                return rows
    return []


def quest_id(quest: dict[str, Any]) -> int | None:
    return _as_int(quest.get("id", quest.get("questId")))


def quest_title(quest: dict[str, Any]) -> str:
    title = quest.get("titleZh") or quest.get("title") or quest.get("nameZh") or quest.get("name")
    ident = quest_id(quest)
    if title:
        return str(title)
    return f"任務 {ident}" if ident is not None else "未命名任務"


def quest_map_id(quest: dict[str, Any]) -> int | None:
    return _as_int(quest.get("mapId", quest.get("map")))


def quest_map_name(quest: dict[str, Any]) -> str:
    map_id = quest_map_id(quest)
    if map_id is None:
        return "—"
    return MAP_NAMES.get(map_id, str(map_id))


def objective_type(quest: dict[str, Any]) -> str:
    return str(quest.get("objectiveType") or quest.get("type") or "").strip().upper()


def is_monster_quest(quest: dict[str, Any]) -> bool:
    return objective_type(quest) == OBJECTIVE_MONSTER


def is_completed(quest: dict[str, Any]) -> bool:
    if quest.get("completed"):
        return True
    target = _as_float(quest.get("target"), default=float("inf"))
    progress = _as_float(quest.get("progress"), default=0.0)
    return target != float("inf") and progress >= target and target > 0


def is_accepted(quest: dict[str, Any]) -> bool:
    return bool(quest.get("accepted")) and not is_completed(quest)


def is_available(quest: dict[str, Any]) -> bool:
    return bool(quest.get("available")) and not bool(quest.get("accepted")) and not is_completed(quest)


def quest_state(quest: dict[str, Any]) -> str:
    if is_completed(quest):
        return "已完成"
    if quest.get("accepted"):
        return "進行中"
    if quest.get("available"):
        return "可接取"
    return "未解鎖"


def needs_consumption(quest: dict[str, Any]) -> bool:
    """True for equipment delivery or item-consumption quests, not kill quests."""
    if is_completed(quest):
        return False
    return not is_monster_quest(quest)


def skip_reason(quest: dict[str, Any]) -> str | None:
    """Why this quest must not be auto-accepted or auto-travelled."""
    if needs_consumption(quest):
        return None
    map_id = quest_map_id(quest)
    if map_id in SPECIAL_ENTRY_MAPS:
        return SKIP_SPECIAL_MAP
    return None


def is_auto_eligible(quest: dict[str, Any]) -> bool:
    return skip_reason(quest) is None


def is_active_monster_quest(quest: dict[str, Any]) -> bool:
    if not is_monster_quest(quest) or not quest.get("accepted") or is_completed(quest):
        return False
    progress = _as_float(quest.get("progress"), default=0.0)
    target = _as_float(quest.get("target"), default=float("inf"))
    return progress < target


def has_active_monster_quest(quests: list[dict[str, Any]]) -> bool:
    return any(is_active_monster_quest(quest) for quest in quests)


def monster_matches_active_quest(
    quests: list[dict[str, Any]],
    monster: dict[str, Any],
    *,
    limited: bool,
) -> bool:
    if not limited:
        return True
    monster_map = _as_int(monster.get("mapId", monster.get("map")))
    for quest in quests:
        if not is_active_monster_quest(quest):
            continue
        quest_map = quest_map_id(quest)
        if quest_map is None or (monster_map is not None and quest_map == monster_map):
            return True
    return False


def pick_next_monster_quest(
    quests: list[dict[str, Any]],
    current_map: int | None = None,
) -> dict[str, Any] | None:
    """First available monster quest we can auto-run. Prefers the current map."""
    if has_active_monster_quest(quests):
        return None
    candidates = [
        quest for quest in quests
        if is_available(quest) and is_monster_quest(quest) and is_auto_eligible(quest)
    ]
    if not candidates:
        return None
    if current_map is not None:
        candidates.sort(key=lambda quest: quest_map_id(quest) != current_map)
    return candidates[0]


def skipped_available(quests: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str]]:
    rows = []
    for quest in quests:
        if not is_available(quest):
            continue
        reason = skip_reason(quest)
        if reason:
            rows.append((quest, reason))
    return rows


def active_quest_target(quests: list[dict[str, Any]]) -> tuple[int | None, int]:
    """Map and quest id to travel toward, preferring an active monster quest."""
    active = [
        quest for quest in quests
        if quest.get("accepted") and not is_completed(quest) and quest_map_id(quest) is not None
    ]
    if not active:
        return None, 0
    active = [quest for quest in active if is_monster_quest(quest) and skip_reason(quest) is None]
    if not active:
        return None, 0
    chosen = active[0]
    ident = quest_id(chosen) or 0
    return quest_map_id(chosen), ident


def quest_error_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("message", "error", "reason", "code", "detail"):
            item = value.get(key)
            if isinstance(item, (str, int, float)) and str(item).strip():
                return str(item)
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value) if value is not None else ""


def is_quest_limit_response(value: object) -> bool:
    if isinstance(value, dict):
        text = " ".join(str(item) for item in value.values()).casefold()
    elif isinstance(value, (list, tuple)):
        text = " ".join(str(item) for item in value).casefold()
    else:
        text = quest_error_text(value).casefold()
    return any(marker.casefold() in text for marker in QUEST_LIMIT_MARKERS)


def format_quest_row(quest: dict[str, Any]) -> dict[str, Any]:
    progress = quest.get("progress", 0)
    target = quest.get("target", "—")
    if is_completed(quest):
        reason = None
    elif needs_consumption(quest):
        reason = NEEDS_DELIVERY_NOTE
    else:
        reason = skip_reason(quest)
    ident = quest_id(quest)
    return {
        "id": ident,
        "title": quest_title(quest),
        "map_id": quest_map_id(quest),
        "map_name": quest_map_name(quest),
        "progress": progress,
        "target": target,
        "progress_text": f"{progress}/{target}",
        "state": quest_state(quest),
        "objective_type": objective_type(quest) or "—",
        "monster": is_monster_quest(quest),
        "accepted": bool(quest.get("accepted")),
        "completed": is_completed(quest),
        "available": bool(quest.get("available")),
        "skip": reason,
        "rewards": (
            f"經驗 {quest.get('rewardExperience', 0)}"
            f" · 金幣 {quest.get('rewardGold', 0)}"
            f" · 藥水 {quest.get('rewardPotions', 0)}"
        ),
    }


def format_quest_rows(quests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [format_quest_row(quest) for quest in quests]


def quest_summary(quests: list[dict[str, Any]]) -> str:
    if not quests:
        return "尚未讀取任務。請先解鎖並按「重新整理任務」。"
    total = len(quests)
    done = sum(1 for quest in quests if is_completed(quest))
    active = sum(1 for quest in quests if is_active_monster_quest(quest))
    available = sum(1 for quest in quests if is_available(quest) and is_auto_eligible(quest))
    skipped = len(skipped_available(quests))
    return (
        f"今日任務 {total} 條 · 已完成 {done} · 進行中 {active}"
        f" · 可自動接取 {available}"
        + (f" · 已略過 {skipped}" if skipped else "")
    )


QUALITY_RANK = {
    "L1": 1, "COMMON": 1, "NORMAL": 1, "普通": 1,
    "L2": 2, "UNCOMMON": 2, "RARE": 2, "優秀": 2, "优秀": 2, "稀有": 2,
    "L3": 3, "EPIC": 3, "史詩": 3, "史诗": 3,
    "L4": 4, "LEGENDARY": 4, "傳說": 4, "传说": 4,
}
QUALITY_LABELS = {
    1: "普通", 2: "稀有", 3: "史詩", 4: "傳說",
}


def quality_rank(item: dict[str, Any]) -> int:
    raw = str(item.get("qualityCode") or item.get("quality") or item.get("rarity") or "").strip()
    return QUALITY_RANK.get(raw.upper(), QUALITY_RANK.get(raw, 2))


def item_is_blocked(item: dict[str, Any]) -> bool:
    return any(item.get(flag) for flag in ("equipped", "locked", "listed", "isLocked", "isListed"))


def item_name(item: dict[str, Any]) -> str:
    for key in ("nameZh", "displayNameZh", "titleZh", "name", "displayName", "nameEn", "itemCode"):
        value = item.get(key)
        if value:
            return str(value)
    ident = item.get("playerItemId", item.get("itemId", item.get("id")))
    return f"物品 #{ident}" if ident is not None else "未命名物品"


def item_quantity(item: dict[str, Any]) -> int:
    qty = _as_int(item.get("quantity", item.get("count", 1)))
    return 1 if qty is None or qty <= 0 else qty


def format_item_line(item: dict[str, Any]) -> str:
    rank = quality_rank(item)
    quality = QUALITY_LABELS.get(rank, "未知品質")
    level = item.get("equipmentLevel", item.get("level"))
    ident = item.get("playerItemId", item.get("itemId", item.get("id")))
    qty = item_quantity(item)
    parts = [quality, item_name(item)]
    if level is not None and str(level) != "":
        parts.append(f"等級 {level}")
    if qty > 1:
        parts.append(f"×{qty}")
    if ident is not None:
        parts.append(f"#{ident}")
    return " · ".join(str(part) for part in parts)


def format_item_rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for item in items:
        ident = item.get("playerItemId", item.get("itemId", item.get("id")))
        rows.append({
            "id": ident,
            "name": item_name(item),
            "label": format_item_line(item),
            "quantity": item_quantity(item),
            "quality": QUALITY_LABELS.get(quality_rank(item), "未知品質"),
            "level": item.get("equipmentLevel", item.get("level")),
        })
    return rows


def deliverable_items(items: object) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    rows = []
    for item in items:
        if not isinstance(item, dict) or item.get("playerItemId") is None:
            continue
        if item_is_blocked(item):
            continue
        rows.append(item)
    return rows


def propose_delivery_items(items: object, target: int) -> list[dict[str, Any]]:
    """Pick the cheapest eligible items, never equipped / locked / listed ones."""
    eligible = deliverable_items(items)
    eligible.sort(key=lambda item: (
        quality_rank(item),
        _as_int(item.get("equipmentLevel", item.get("level"))) or 0,
        _as_int(item.get("playerItemId")) or 0,
    ))
    need = max(0, int(target or 0))
    return eligible[:need]


def required_delivery_count(quest: dict[str, Any], preview: dict[str, Any] | None = None) -> int:
    if preview and isinstance(preview.get("quest"), dict):
        count = _as_int(preview["quest"].get("target", preview["quest"].get("quantity")))
        if count:
            return count
    count = _as_int(quest.get("target", quest.get("quantity", quest.get("requiredCount"))))
    return count or 0


def delivery_run_number(quest: dict[str, Any], preview: dict[str, Any] | None = None) -> int:
    if preview and isinstance(preview.get("quest"), dict):
        value = _as_int(preview["quest"].get("runNumber", preview["quest"].get("run")))
        if value is not None:
            return value
    value = _as_int(quest.get("runNumber", quest.get("run")))
    return value or 0


def consumption_quests(quests: list[dict[str, Any]], *, accepted: bool | None = None) -> list[dict[str, Any]]:
    rows = []
    for quest in quests:
        if not needs_consumption(quest):
            continue
        if accepted is True and not (quest.get("accepted") and not is_completed(quest)):
            continue
        if accepted is False and not is_available(quest):
            continue
        rows.append(quest)
    return rows


def next_daily_step(
    quests: list[dict[str, Any]],
    current_map: int | None = None,
    *,
    auto_replace: bool = False,
    skipped_ids: set[int] | None = None,
) -> dict[str, Any]:
    """Decide the next daily-quest action. Never silently delivers items."""
    skipped_ids = skipped_ids or set()

    def _skipped(quest: dict[str, Any]) -> bool:
        ident = quest_id(quest)
        return ident is not None and ident in skipped_ids

    accepted_delivery = [quest for quest in consumption_quests(quests, accepted=True) if not _skipped(quest)]
    if accepted_delivery:
        return {"kind": "delivery", "quest": accepted_delivery[0], "already_accepted": True, "replace_available": False}
    if has_active_monster_quest(quests):
        return {"kind": "hunt"}
    available_delivery = [quest for quest in consumption_quests(quests, accepted=False) if not _skipped(quest)]
    monster = pick_next_monster_quest(quests, current_map)
    if auto_replace:
        if monster:
            return {
                "kind": "hunt_accept",
                "quest": monster,
                "replaced": available_delivery,
                "replace_available": False,
            }
        if available_delivery:
            return {"kind": "replaced_done", "replaced": available_delivery, "replace_available": False}
        return {"kind": "done"}
    if monster:
        return {"kind": "hunt_accept", "quest": monster}
    if available_delivery:
        return {"kind": "delivery", "quest": available_delivery[0], "already_accepted": False, "replace_available": False}
    return {"kind": "done"}


def format_delivery_prompt(
    quest: dict[str, Any],
    *,
    preview: dict[str, Any] | None = None,
    already_accepted: bool = False,
    error: str = "",
) -> dict[str, Any]:
    items = deliverable_items((preview or {}).get("items"))
    target = required_delivery_count(quest, preview)
    proposed = propose_delivery_items(items, target)
    enough = bool(target and len(proposed) >= target)
    note = ""
    if not preview or not items:
        if already_accepted:
            note = "已接取此任務，但還沒列出可交付物品。按確認後會再向伺服器查一次。"
        else:
            note = "伺服器要先接取才能列出確切物品。按「確認交付」會先接取，再自動選出最低品質的候選並交付；數量不足時會中止，不會亂交。"
    elif not enough:
        note = f"背包裡可交付的物品不足（需要 {target} 件，目前可用 {len(items)} 件）。請先整理背包再確認，或按跳過。"
    return {
        "quest_id": quest_id(quest),
        "title": quest_title(quest),
        "map_name": quest_map_name(quest),
        "objective_type": objective_type(quest) or "交付",
        "target": target,
        "run_number": delivery_run_number(quest, preview),
        "already_accepted": already_accepted,
        "proposed": format_item_rows(proposed),
        "item_ids": [item.get("playerItemId") for item in proposed],
        "available_count": len(items),
        "enough": enough,
        "previewed": bool(preview),
        "replace_available": False,
        "replace_note": REPLACE_UNAVAILABLE,
        "note": error or note,
        "error": error,
    }
