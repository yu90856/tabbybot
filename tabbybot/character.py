"""Read-only formatting for the character, equipment, and inventory panel."""

from __future__ import annotations

from typing import Any

from tabbybot.quests import format_item_line, item_name, quality_rank, QUALITY_LABELS

CLASS_LABELS = {
    "WARRIOR": "戰士",
    "GUARD": "守衛",
    "ARCHER": "弓手",
    "LANCER": "槍兵",
    "SPEARMAN": "槍兵",
}

SLOT_LABELS = {
    "WEAPON": "武器",
    "HAT": "頭盔",
    "ARMOR": "護甲",
    "SHOES": "鞋子",
    "RING": "戒指",
    "AMULET": "護符",
}
SLOT_ORDER = ("WEAPON", "HAT", "ARMOR", "SHOES", "RING", "AMULET")

STAT_LABELS = (
    ("name", "角色", ("name", "playerName", "characterName", "displayName")),
    ("class", "職業", ()),
    ("level", "等級", ("level",)),
    ("status", "狀態", ()),
    ("health", "生命", ()),
    ("mana", "魔力", ()),
    ("experience", "經驗", ()),
    ("attack", "攻擊", ("attack", "attackPower")),
    ("defense", "防禦", ("defense", "defence")),
    ("luck", "幸運", ("luck", "luckPercent", "luckValue")),
    ("gold", "金幣", ("gold",)),
    ("move_speed", "移速加成", ("moveSpeedPercent",)),
    ("attack_speed", "攻速加成", ("attackSpeedPercent",)),
    ("stamina", "疲勞剩餘", ()),
    ("health_potions", "生命藥水", ("healthPotions",)),
    ("boss_tickets", "遺跡入場券", ("bossTickets",)),
    ("atonement_stones", "贖罪石", ("atonementStones",)),
    ("town_stones", "回城石", ("townStones",)),
    ("evil", "惡名值", ("evil",)),
)


def _as_int(value: object) -> int | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _first(sources: list[dict[str, Any]], *keys: str) -> Any:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in keys:
            if source.get(key) is not None and source.get(key) != "":
                return source[key]
    return None


def _mapping(payload: object) -> dict[str, Any]:
    return payload if isinstance(payload, dict) else {}


def _totals(*sources: dict[str, Any]) -> dict[str, Any]:
    for source in sources:
        if not isinstance(source, dict):
            continue
        equipment = source.get("equipment")
        if isinstance(equipment, dict) and isinstance(equipment.get("totals"), dict):
            return equipment["totals"]
        if isinstance(source.get("totals"), dict):
            return source["totals"]
    return {}


def class_label(value: object) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return "—"
    return CLASS_LABELS.get(raw, raw)


def slot_label(value: object) -> str:
    raw = str(value or "").strip().upper()
    return SLOT_LABELS.get(raw, raw or "其他")


def _format_ratio(current: object, maximum: object) -> str | None:
    if current is None and maximum is None:
        return None
    if maximum is None:
        return str(current)
    if current is None:
        return "—/%s" % maximum
    return "%s/%s" % (current, maximum)


def _format_gold(value: object) -> str | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return str(int(number))
    return ("%.2f" % number).rstrip("0").rstrip(".")


def extract_items(equipment: object) -> list[dict[str, Any]]:
    payload = _mapping(equipment)
    rows = payload.get("items", equipment if isinstance(equipment, list) else [])
    if not isinstance(rows, list):
        return []
    return [item for item in rows if isinstance(item, dict)]


def format_gear_item(item: dict[str, Any]) -> dict[str, Any]:
    ident = item.get("playerItemId", item.get("itemId", item.get("id")))
    slot = str(item.get("slotType", item.get("equipmentType", ""))).upper()
    rank = quality_rank(item)
    level = item.get("equipmentLevel", item.get("level"))
    return {
        "id": ident,
        "name": item_name(item),
        "label": format_item_line(item),
        "slot": slot or None,
        "slot_name": slot_label(slot) if slot else "其他",
        "quality": QUALITY_LABELS.get(rank, "未知品質"),
        "level": level,
        "equipped": bool(item.get("equipped")),
        "can_equip": ident is not None and not item.get("equipped") and not item.get("listed") and not item.get("locked"),
        "can_unequip": bool(item.get("equipped") and slot),
        "locked": bool(item.get("locked") or item.get("isLocked")),
        "listed": bool(item.get("listed") or item.get("isListed")),
        "quantity": item.get("quantity", item.get("count", 1)),
    }


def format_character(
    player: object = None,
    equipment: object = None,
    progression: object = None,
    self_state: object = None,
) -> dict[str, Any]:
    """Build a UI-safe character payload from official GET /player, /equipment, /progression."""
    profile = _mapping(player)
    if isinstance(profile.get("player"), dict):
        profile = {**profile, **profile["player"]}
    progress = _mapping(progression)
    if isinstance(progress.get("progress"), dict):
        progress = {**progress, **progress["progress"]}
    if isinstance(progress.get("progression"), dict):
        progress = {**progress, **progress["progression"]}
    self_row = _mapping(self_state)
    totals = _totals(profile, progress, self_row, _mapping(equipment))
    sources = [totals, self_row, progress, profile]

    name = _first(sources, "name", "playerName", "characterName", "displayName") or "—"
    klass = _first(sources, "characterClass", "class", "playerClass")
    level = _first(sources, "level")
    experience = _first([progress, profile, self_row], "experience")
    experience_needed = _first([progress, profile, self_row], "experienceNeeded", "nextLevelExperience")
    health = _first([self_row, profile, totals], "health")
    max_health = _first([self_row, totals, profile], "maxHealth", "maximumHealth")
    mana = _first([self_row, profile, totals], "mana", "mp", "magic")
    max_mana = _first([self_row, totals, profile], "maxMana", "maximumMana", "maxMp", "mpMax")
    gold = _first(sources, "gold")
    luck = _first(sources, "luck", "luckPercent", "luckValue")
    attack = _first(sources, "attack", "attackPower")
    defense = _first(sources, "defense", "defence")
    move_speed = _first(sources, "moveSpeedPercent")
    attack_speed = _first(sources, "attackSpeedPercent")
    farming = self_row.get("farming") if isinstance(self_row.get("farming"), dict) else {}
    stamina = farming.get("staminaPercent", self_row.get("staminaPercent"))
    dead = bool(self_row.get("dead"))

    stats = [
        {"key": "name", "label": "角色", "value": str(name)},
        {"key": "class", "label": "職業", "value": class_label(klass)},
        {"key": "level", "label": "等級", "value": "—" if level is None else str(level)},
        {"key": "status", "label": "狀態", "value": "陣亡" if dead else "存活"},
        {"key": "health", "label": "生命", "value": _format_ratio(health, max_health) or "—"},
        {"key": "mana", "label": "魔力", "value": _format_ratio(mana, max_mana) or "—"},
        {"key": "experience", "label": "經驗", "value": _format_ratio(experience, experience_needed) or "—"},
        {"key": "attack", "label": "攻擊", "value": "—" if attack is None else str(attack)},
        {"key": "defense", "label": "防禦", "value": "—" if defense is None else str(defense)},
        {"key": "luck", "label": "幸運", "value": "—" if luck is None else ("%s%%" % luck)},
        {"key": "gold", "label": "金幣", "value": _format_gold(gold) or "—"},
        {"key": "move_speed", "label": "移速加成", "value": "—" if move_speed is None else ("%s%%" % move_speed)},
        {"key": "attack_speed", "label": "攻速加成", "value": "—" if attack_speed is None else ("%s%%" % attack_speed)},
        {"key": "stamina", "label": "疲勞剩餘", "value": "—" if stamina is None else (("%.2f%%" % float(stamina)) if isinstance(stamina, (int, float)) or str(stamina).replace(".", "", 1).isdigit() else ("%s%%" % stamina))},
    ]
    for key, label in (
        ("healthPotions", "生命藥水"),
        ("bossTickets", "遺跡入場券"),
        ("atonementStones", "贖罪石"),
    ):
        value = _first(sources, key)
        if value is not None:
            stats.append({"key": key, "label": label, "value": str(value)})
    town = _first(sources, "townStones")
    if isinstance(town, list):
        stats.append({"key": "townStones", "label": "回城石", "value": str(len(town))})
    elif town is not None:
        stats.append({"key": "townStones", "label": "回城石", "value": str(town)})
    evil = _first(sources, "evil")
    if evil is not None:
        stats.append({"key": "evil", "label": "惡名值", "value": str(evil)})

    items = [format_gear_item(item) for item in extract_items(equipment)]
    equipped_by_slot = {item["slot"]: item for item in items if item["equipped"] and item["slot"]}
    slots = []
    for slot in SLOT_ORDER:
        item = equipped_by_slot.get(slot)
        slots.append({
            "slot": slot,
            "slot_name": slot_label(slot),
            "item": item,
            "empty": item is None,
        })
    for item in items:
        if item["equipped"] and item["slot"] and item["slot"] not in SLOT_LABELS:
            slots.append({
                "slot": item["slot"],
                "slot_name": item["slot_name"],
                "item": item,
                "empty": False,
            })
    inventory = [item for item in items if not item["equipped"]]
    return {
        "name": str(name),
        "class_name": class_label(klass),
        "level": level,
        "stats": stats,
        "equipment": slots,
        "inventory": inventory,
        "item_count": len(items),
        "can_equip": True,
    }


# ---- in-game style character page -------------------------------------------
import json as _json
import re as _re
from pathlib import Path as _Path

CATEGORY_TABS = (
    ("ALL", "全部"), ("WEAPON", "武器"), ("HAT", "頭盔"), ("ARMOR", "胸甲"),
    ("SHOES", "靴子"), ("RING", "戒指"), ("AMULET", "護符"), ("OTHER", "其他"),
)
ATTRIBUTE_NAMES = {
    "ATTACK": "攻擊", "DEFENSE": "防禦", "HEALTH": "生命", "MAX_HEALTH": "生命",
    "HEALTH_REGEN": "每秒回血", "ATTACK_SPEED": "攻擊速度", "ATTACK_SPEED_PERCENT": "攻擊速度",
    "MOVE_SPEED": "移動速度", "MOVE_SPEED_PERCENT": "移動速度", "CRITICAL": "暴擊",
    "CRITICAL_CHANCE": "暴擊率", "CRITICAL_CHANCE_PERCENT": "暴擊率", "LUCK": "幸運",
}
PERCENT_CODES = {"LUCK", "CRITICAL_CHANCE", "CRITICAL_CHANCE_PERCENT", "ATTACK_SPEED",
                 "ATTACK_SPEED_PERCENT", "MOVE_SPEED", "MOVE_SPEED_PERCENT"}
TOP_LEVEL_STATS = (
    ("attack", "ATTACK"), ("defense", "DEFENSE"), ("maxHealth", "HEALTH"), ("healthRegen", "HEALTH_REGEN"),
    ("attackSpeedPercent", "ATTACK_SPEED_PERCENT"), ("moveSpeedPercent", "MOVE_SPEED_PERCENT"),
    ("criticalChancePercent", "CRITICAL_CHANCE_PERCENT"), ("luck", "LUCK"),
)
# Consumables shown in the bag from player counters. "use" is the realtime action
# from the reference client; None means the use action is not confirmed.
COUNTER_ITEMS = (
    ("healthPotions", "HEALTH_POTION", "生命藥水", "恢復生命。", "health"),
    ("staminaPotions", "STAMINA_POTION", "體力藥水", "恢復疲勞（體力）。", "stamina"),
    ("atonementStones", "ATONEMENT_STONE", "贖罪之石", "消耗一顆贖罪之石，減少惡名值。", None),
    ("townStones", "TOWN_STONE", "回城石", "回到維洛鎮。", None),
    ("bossTickets", "BOSS_TICKET", "遺跡入場券", "進入廢棄遺跡時使用。", None),
)
CONSUMABLE_EMOJI = {"HEALTH_POTION": "🧪", "STAMINA_POTION": "🍵", "ATONEMENT_STONE": "🪨",
                    "TOWN_STONE": "🏠", "BOSS_TICKET": "🎟️", "WOODEN_FISHING_ROD": "🎣",
                    "TREASURE_STONE": "💎"}
ACTIVITY_ITEM_ACTIONS = {"WOODEN_FISHING_ROD": "useFishingRod", "TREASURE_STONE": "useTreasureStone",
                         "STAMINA_POTION": "useStaminaPotion"}

_NAMES: dict[str, str] | None = None


def item_names_zh() -> dict[str, str]:
    global _NAMES
    if _NAMES is None:
        try:
            rows = _json.loads((_Path(__file__).with_name("item_names_zh.json")).read_text("utf-8"))
            _NAMES = {str(r["itemCode"]).upper(): str(r["nameZh"]) for r in rows if r.get("itemCode") and r.get("nameZh")}
        except (OSError, ValueError, KeyError, TypeError):
            _NAMES = {}
    return _NAMES


def item_icon(item: dict[str, Any]) -> str | None:
    """Local asset path (extracted game art from the reference repo), or None."""
    code = str(item.get("itemCode", item.get("code", "")) or "").upper()
    slot = str(item.get("slotType", item.get("equipmentType", "")) or "").upper()
    if "BOW" in code:
        return "/assets/bow_p1.png"
    stem = {"WEAPON": "sword", "HAT": "helmet", "ARMOR": "armor", "SHOES": "boots",
            "RING": "ring", "AMULET": "amulet"}.get(slot)
    if not stem:
        return None
    level = item.get("equipmentLevel")
    if level is None:
        match = _re.search(r"P([1-6])$", code)
        level = match.group(1) if match else 1
    try:
        level = min(2, max(1, int(level or 1)))
    except (TypeError, ValueError):
        level = 1
    return f"/assets/{stem}_p{level}.webp"


def item_attributes(item: dict[str, Any]) -> list[dict[str, Any]]:
    """[{code, name, value, suffix, text}] from attributes[] plus top-level stat fields."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    attributes = item.get("attributes", [])
    if isinstance(attributes, list):
        for attribute in attributes:
            if not isinstance(attribute, dict):
                continue
            code = str(attribute.get("code", "")).upper()
            try:
                value = float(attribute.get("value", 0))
            except (TypeError, ValueError):
                continue
            canon = "HEALTH" if code == "MAX_HEALTH" else code.replace("_PERCENT", "")
            seen.add(canon)
            rows.append(_attr_row(code, value, attribute.get("displayNameZh"), bool(attribute.get("percentage"))))
    for key, code in TOP_LEVEL_STATS:
        canon = code.replace("_PERCENT", "")
        if canon in seen:
            continue
        try:
            value = float(item.get(key, 0) or 0)
        except (TypeError, ValueError):
            continue
        if value:
            rows.append(_attr_row(code, value, None, False))
    return rows


def _attr_row(code: str, value: float, name: Any, percentage: bool) -> dict[str, Any]:
    suffix = "/秒" if code == "HEALTH_REGEN" else ("%" if percentage or code in PERCENT_CODES else "")
    shown = str(int(value)) if float(value).is_integer() else f"{value:g}"
    label = str(name or ATTRIBUTE_NAMES.get(code) or code or "屬性")
    canon = "HEALTH" if code == "MAX_HEALTH" else code.replace("_PERCENT", "")
    return {"code": canon, "name": label, "value": value, "suffix": suffix, "text": f"{label} +{shown}{suffix}"}


def item_category(item: dict[str, Any]) -> str:
    slot = str(item.get("slotType", item.get("equipmentType", "")) or "").upper()
    return slot if slot in {"WEAPON", "HAT", "ARMOR", "SHOES", "RING", "AMULET"} else "OTHER"


_S2T = str.maketrans("兽护头链剑铁钢银战龙灵风级药宝强韧坚贵晶环项锋盾斗华丽纹",
                     "獸護頭鏈劍鐵鋼銀戰龍靈風級藥寶強韌堅貴晶環項鋒盾鬥華麗紋")
_CODE_NAME_FALLBACK = {"GOLDEN_LUCKY_RING": "黃金幸運戒指"}


def bag_item(item: dict[str, Any]) -> dict[str, Any]:
    row = format_gear_item(item)
    code = str(item.get("itemCode", item.get("code", "")) or "").upper()
    mapped = item_names_zh().get(code)
    if mapped and (not item.get("nameZh")):
        row["name"] = mapped
    if row.get("name") == code and code in _CODE_NAME_FALLBACK:
        row["name"] = _CODE_NAME_FALLBACK[code]
    row["name"] = str(row.get("name") or "").translate(_S2T)
    row["code"] = code
    row["quality_code"] = f"L{quality_rank(item)}"
    row["category"] = item_category(item)
    if row["category"] == "OTHER":
        row["can_equip"] = False
    row["icon"] = item_icon(item)
    row["emoji"] = CONSUMABLE_EMOJI.get(code, "📦") if row["icon"] is None else None
    row["description"] = str(item.get("descriptionZh") or item.get("description") or "")
    row["attributes"] = item_attributes(item)
    action = ACTIVITY_ITEM_ACTIONS.get(code)
    row["use_kind"] = "stamina" if action == "useStaminaPotion" else None
    row["use_note"] = "" if row["use_kind"] else ("" if row["category"] != "OTHER" else "這個道具的使用方式尚未確認，請在遊戲內使用")
    row["virtual"] = False
    return row


def character_page(player: object = None, equipment: object = None, progression: object = None,
                   self_state: object = None) -> dict[str, Any]:
    """Extra fields for the in-game style page: info card, bag grid, tabs."""
    profile = _mapping(player)
    if isinstance(profile.get("player"), dict):
        profile = {**profile, **profile["player"]}
    progress = _mapping(progression)
    for key in ("progress", "progression"):
        if isinstance(progress.get(key), dict):
            progress = {**progress, **progress[key]}
    self_row = _mapping(self_state)
    totals = _totals(profile, progress, self_row, _mapping(equipment))
    sources = [totals, self_row, progress, profile]

    def num(*keys: str, src: list[dict[str, Any]] | None = None) -> Any:
        return _first(src or sources, *keys)

    klass = str(num("characterClass", "class", "playerClass") or "").upper()
    info = {
        "name": str(num("name", "playerName", "characterName", "displayName") or "—"),
        "class_name": class_label(klass),
        "class_code": klass,
        "portrait": {"ARCHER": "/assets/archer-portrait.png", "GUARD": "/assets/guard-portrait.png"}.get(klass, "/assets/warrior-idle.webp"),
        "level": num("level"),
        "experience": num("experience", src=[progress, profile, self_row]),
        "experience_needed": num("experienceNeeded", "nextLevelExperience", src=[progress, profile, self_row]),
        "health": num("health", src=[self_row, profile, totals]),
        "max_health": num("maxHealth", "maximumHealth", src=[self_row, totals, profile]),
        "attack": num("attack", "attackPower", src=[self_row, totals, profile, progress]),
        "defense": num("defense", "defence", src=[self_row, totals, profile, progress]),
        "attack_speed": num("attackSpeedPercent"),
        "move_speed": num("moveSpeedPercent"),
        "combat_power": num("combatPower", "battlePower", "power", "fightPower"),
        "evil": num("evil"),
        "evil_max": num("maxEvil", "evilMax", "evilLimit"),
        "health_regen": num("healthRegen", "healthRegenPerSecond"),
    }
    live = any(self_row.get(key) is not None for key in ("attack", "maxHealth", "defense"))
    info["stats_source"] = "live" if live else "equipment"
    raw_items = extract_items(equipment)
    items = [bag_item(item) for item in raw_items]
    equipped = [item for item in items if item["equipped"]]
    bag = [item for item in items if not item["equipped"]]
    codes = {item["code"] for item in bag}
    for key, code, name, desc, kind in COUNTER_ITEMS:
        count = _first(sources, key)
        if isinstance(count, list):
            count = len(count)
        try:
            count = int(count or 0)
        except (TypeError, ValueError):
            count = 0
        if count <= 0 or code in codes:
            continue
        bag.append({
            "id": f"virtual:{code}", "name": name, "label": name, "code": code, "category": "OTHER",
            "slot": None, "slot_name": "其他", "quality": "—", "level": None, "equipped": False,
            "can_equip": False, "can_unequip": False, "locked": False, "listed": False,
            "quantity": count, "icon": None, "emoji": CONSUMABLE_EMOJI.get(code, "📦"),
            "description": desc, "attributes": [], "use_kind": kind,
            "use_note": "" if kind else "這個道具的使用方式尚未確認，請在遊戲內使用", "virtual": True,
        })
    counts = {key: 0 for key, _label in CATEGORY_TABS}
    for item in bag:
        counts["ALL"] += 1
        counts[item["category"]] = counts.get(item["category"], 0) + 1
    gear = []
    by_slot = {item["slot"]: item for item in equipped if item.get("slot")}
    for slot in SLOT_ORDER:
        gear.append({"slot": slot, "slot_name": slot_label(slot), "item": by_slot.get(slot)})
    return {
        "info": info,
        "gear": gear,
        "bag": bag,
        "tabs": [{"key": key, "label": label, "count": counts.get(key, 0)} for key, label in CATEGORY_TABS],
    }
