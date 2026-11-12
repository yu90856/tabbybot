"""Pure protocol helpers: frames, tickets, snapshots, movement corrections."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from tabbybot.constants import (
    ATTACK_BUSY_PAD,
    ATTACK_REACH,
    AUTH_DEFAULT_MAP,
    AUTH_DEFAULT_X,
    AUTH_DEFAULT_Y,
    AUTH_REQUIRED_FIELDS,
    DEFAULT_REACH,
    INVALID_TARGET_LOCK_SEC,
    TICKET_COOLDOWN_FLOOR,
)


class ProtocolError(Exception):
    pass


def build_auth_frame(
    token: str,
    *,
    map_id: int = AUTH_DEFAULT_MAP,
    x: float = AUTH_DEFAULT_X,
    y: float = AUTH_DEFAULT_Y,
    movement_revision: int = 0,
) -> dict[str, Any]:
    """Build the first official-client login frame. Token sits before loginReset."""
    if not token:
        raise ProtocolError("缺少遊戲會話 token")
    frame = {
        "action": "auth",
        "movementRevision": int(movement_revision),
        "map": int(map_id),
        "x": float(x),
        "y": float(y),
        "animation": "idle",
        "attackCritical": False,
        "flip": False,
        "token": token,
        "loginReset": True,
        "snapshotDelta": 2,
    }
    missing = [name for name in AUTH_REQUIRED_FIELDS if name not in frame]
    if missing:
        raise ProtocolError("登入幀缺少欄位：" + ", ".join(missing))
    keys = list(frame)
    if keys[keys.index("token") + 1] != "loginReset" or keys[keys.index("loginReset") + 1] != "snapshotDelta":
        raise ProtocolError("token 必須放在 loginReset、snapshotDelta 之前")
    return frame


def attach_seq_life(packet: dict[str, Any], seq: int, life: int) -> dict[str, Any]:
    payload = dict(packet)
    payload["seq"] = int(seq)
    payload["life"] = int(life)
    return payload


def build_sync(
    *,
    movement_revision: int,
    map_id: int,
    x: float,
    y: float,
    animation: str = "idle",
    attack_critical: bool = False,
    flip: bool = False,
    attack_elevation: int | None = None,
) -> dict[str, Any]:
    packet: dict[str, Any] = {
        "action": "sync",
        "movementRevision": int(movement_revision),
        "map": int(map_id),
        "x": round(float(x), 2),
        "y": round(float(y), 2),
        "animation": animation,
        "attackCritical": bool(attack_critical),
        "flip": bool(flip),
    }
    if attack_elevation is not None:
        packet["attackElevation"] = int(attack_elevation)
    return packet


def build_monster_attack(encounter_id: str, ticket: str, queued_at_ms: int | None = None) -> dict[str, Any]:
    if queued_at_ms is None:
        queued_at_ms = int(time.monotonic() * 1000)
    return {
        "action": "monsterAttack",
        "encounterId": encounter_id,
        "attackTicket": ticket,
        "queuedAt": int(queued_at_ms),
    }


def build_travel(destination: int, movement_revision: int, *, quest_id: int = 0, use_ticket: bool = False) -> dict[str, Any]:
    return {
        "action": "travel",
        "map": int(destination),
        "questId": int(quest_id),
        "useTicket": bool(use_ticket),
        "movementRevision": int(movement_revision),
    }


def build_ping(sent_at_ms: int | None = None) -> dict[str, Any]:
    if sent_at_ms is None:
        sent_at_ms = int(time.monotonic() * 1000)
    return {"action": "ping", "sentAt": int(sent_at_ms)}


def build_respawn() -> dict[str, Any]:
    return {"action": "respawn"}


def parse_welcome(packet: dict[str, Any]) -> tuple[int, float, float, int] | None:
    if packet.get("type") != "welcome":
        return None
    position = packet.get("position")
    if not isinstance(position, dict):
        return None
    return (
        int(position.get("map", 1)),
        float(position["x"]),
        float(position["y"]),
        int(position.get("revision", 0)),
    )


def attack_elevation(player_x: float, player_y: float, target_x: float, target_y: float) -> int:
    dx = target_x - player_x
    dy = target_y - player_y
    if abs(dy) > max(12.0, abs(dx) * 0.4142):
        return -1 if dy < 0 else 1
    return 0


def attack_timing(character_class: str, attack_speed_percent: float, elevation: int) -> tuple[int, float, float]:
    speed_multiplier = 1.2 * min(2.0, max(1.0, 1.0 + float(attack_speed_percent) / 100.0))
    frame_duration = (0.88 / 12.0) / speed_multiplier
    klass = character_class.upper()
    if klass == "ARCHER":
        impact_frame = 5 if elevation == 1 else 7
    else:
        impact_frame = 5
    impact_delay_ms = round(frame_duration * impact_frame * 1000)
    animation_duration = frame_duration * 12
    return impact_delay_ms, animation_duration, speed_multiplier


def attack_reach(character_class: str, kind: str = "prepare") -> float:
    table = ATTACK_REACH.get(character_class.upper(), DEFAULT_REACH)
    return table[kind]


def busy_until(animation_duration: float, speed_multiplier: float, now: float | None = None) -> float:
    now = time.monotonic() if now is None else now
    return now + max(TICKET_COOLDOWN_FLOOR / speed_multiplier, animation_duration) + ATTACK_BUSY_PAD


@dataclass
class AttackTicket:
    id: str
    critical: bool = False


@dataclass
class TicketBox:
    current: AttackTicket | None = None
    used: set[str] = field(default_factory=set)
    last_ticket: str | None = None
    busy_until_at: float = 0.0
    invalid_until: dict[str, float] = field(default_factory=dict)

    def accept(self, raw: dict[str, Any] | None) -> AttackTicket | None:
        if not isinstance(raw, dict) or not raw.get("id"):
            return None
        ticket = AttackTicket(id=str(raw["id"]), critical=bool(raw.get("critical", False)))
        self.current = ticket
        return ticket

    def available(self, now: float | None = None) -> AttackTicket | None:
        now = time.monotonic() if now is None else now
        ticket = self.current
        if ticket is None or ticket.id in self.used or now < self.busy_until_at:
            return None
        return ticket

    def consume(self, ticket_id: str, busy_at: float) -> None:
        self.used.add(ticket_id)
        self.last_ticket = ticket_id
        self.busy_until_at = busy_at

    def unconsume(self, ticket_id: str | None = None) -> None:
        target = ticket_id or self.last_ticket
        if not target:
            return
        self.used.discard(target)
        if self.last_ticket == target:
            self.last_ticket = None

    def on_monster_attack_error(self, code: str, packet: dict[str, Any], now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if code == "invalid_target":
            self.unconsume()
        elif code == "attack_cooldown":
            self.busy_until_at = max(self.busy_until_at, now + TICKET_COOLDOWN_FLOOR)
        nxt = packet.get("nextAttack")
        if isinstance(nxt, dict):
            self.accept(nxt)

    def lock_invalid(self, encounter_id: str, now: float | None = None, duration: float = INVALID_TARGET_LOCK_SEC) -> None:
        now = time.monotonic() if now is None else now
        self.invalid_until[encounter_id] = now + duration

    def is_invalid(self, encounter_id: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return now < self.invalid_until.get(encounter_id, 0.0)


def monster_map_id(monster: dict[str, Any], fallback: int | None = None) -> int:
    try:
        return int(monster.get("mapId", fallback if fallback is not None else -1))
    except (TypeError, ValueError):
        return fallback if fallback is not None else -1


class MonsterCache:
    def __init__(self) -> None:
        self.monsters: dict[str, dict[str, Any]] = {}

    def apply_snapshot(self, packet: dict[str, Any], fallback_map: int | None = None) -> None:
        rows = packet.get("monsters", [])
        if not isinstance(rows, list):
            return
        compact = int(packet.get("deltaVersion", 0) or 0) == 2
        is_delta = bool(packet.get("delta", False))
        cache = self.monsters if is_delta else {}
        for removed in packet.get("removedMonsters", []) or []:
            cache.pop(str(removed), None)
        present: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or not row.get("encounterId"):
                continue
            encounter_id = str(row["encounterId"])
            present.add(encounter_id)
            merged = dict(cache.get(encounter_id, {}))
            merged.update(row)
            cache[encounter_id] = merged
        if not compact:
            cache = {key: value for key, value in cache.items() if key in present}
        self.monsters = cache

    def on_map(self, map_id: int) -> dict[str, dict[str, Any]]:
        return {
            key: monster
            for key, monster in self.monsters.items()
            if isinstance(monster, dict) and monster_map_id(monster, map_id) == map_id
        }

    def living_on_map(self, map_id: int) -> Iterable[tuple[str, dict[str, Any]]]:
        for encounter_id, monster in self.on_map(map_id).items():
            try:
                if int(monster.get("health", 0)) > 0:
                    yield encounter_id, monster
            except (TypeError, ValueError):
                continue


@dataclass
class Position:
    map_id: int
    x: float
    y: float
    revision: int
    estimated: bool = False


def apply_server_position(
    current: Position | None,
    map_id: int,
    x: float,
    y: float,
    revision: int,
    *,
    ignore_stale: bool = True,
) -> tuple[Position, bool]:
    """Adopt a server position. Older revisions are ignored when local is ahead."""
    if current is not None and ignore_stale and revision < current.revision:
        return current, False
    return Position(map_id=int(map_id), x=float(x), y=float(y), revision=int(revision), estimated=False), True


def merge_self_state(previous: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any]:
    incoming = packet.get("self")
    if not isinstance(incoming, dict):
        return previous
    if not bool(packet.get("delta", False)):
        merged = dict(incoming)
    else:
        merged = dict(previous)
        merged.update(incoming)
    if str(packet.get("action", "")) == "respawn":
        merged["dead"] = False
    return merged


def product_display_name(product: dict[str, Any]) -> str:
    for key in ("name", "itemName", "title", "displayName", "item_name"):
        value = product.get(key)
        if isinstance(value, str) and value.strip():
            return value
    item = product.get("item")
    if isinstance(item, dict):
        for key in ("name", "itemName", "title"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return ""



NEARBY_UNIT_TTL_SEC = 15.0


def _unit_point(row: dict[str, Any]) -> tuple[float, float] | None:
    position = row.get("position") if isinstance(row.get("position"), dict) else {}
    for source in (row, position):
        if source.get("x") is None or source.get("y") is None:
            continue
        try:
            return float(source["x"]), float(source["y"])
        except (TypeError, ValueError):
            continue
    return None


def _unit_map_id(row: dict[str, Any], fallback: int | None = None) -> int | None:
    position = row.get("position") if isinstance(row.get("position"), dict) else {}
    for source in (row, position):
        for key in ("mapId", "map"):
            if source.get(key) is None:
                continue
            try:
                return int(source[key])
            except (TypeError, ValueError):
                continue
    return fallback


def _unit_name(row: dict[str, Any], fallback: str = "") -> str:
    for key in ("playerName", "npcName", "displayName", "name", "title"):
        value = row.get(key)
        if value:
            return str(value)
    ident = row.get("id", row.get("playerId", row.get("npcId")))
    return fallback or (f"#{ident}" if ident is not None else "")


def _unit_kind(row: dict[str, Any], default: str = "player") -> str:
    if row.get("npc") or row.get("isNpc") or row.get("isNPC"):
        return "npc"
    raw = str(row.get("kind") or row.get("type") or row.get("unitType") or "").strip().lower()
    if raw in {"npc", "npcs"}:
        return "npc"
    if raw in {"player", "peer", "players", "nearbyplayer"}:
        return "player"
    return default


def _unit_key(row: dict[str, Any], index: int, kind: str) -> str:
    ident = row.get("id", row.get("playerId", row.get("npcId")))
    if ident is not None:
        return f"{kind}:{ident}"
    name = _unit_name(row).strip()
    if name:
        return f"{kind}:name:{name}"
    point = _unit_point(row)
    if point is not None:
        return f"{kind}:pos:{point[0]}:{point[1]}:{index}"
    return f"{kind}:idx:{index}"


class PeerCache:
    """Nearby players and NPCs from snapshot peer/npc lists, with a short TTL."""

    def __init__(self) -> None:
        self.units: dict[str, dict[str, Any]] = {}
        self._seen: dict[str, float] = {}

    def apply_snapshot(
        self,
        packet: dict[str, Any],
        fallback_map: int | None = None,
        now: float | None = None,
    ) -> None:
        now = time.monotonic() if now is None else now
        rows: list[tuple[dict[str, Any], str]] = []
        for key, default_kind in (
            ("peers", "player"),
            ("players", "player"),
            ("nearbyPlayers", "player"),
            ("npcs", "npc"),
            ("NPC", "npc"),
            ("nearbyNpcs", "npc"),
        ):
            raw = packet.get(key)
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict):
                        rows.append((item, default_kind))
        for index, (item, default_kind) in enumerate(rows):
            kind = _unit_kind(item, default_kind)
            key = _unit_key(item, index, kind)
            point = _unit_point(item)
            previous = self.units.get(key, {})
            x, y = point if point is not None else (previous.get("x"), previous.get("y"))
            map_id = _unit_map_id(item, previous.get("map_id", fallback_map))
            self.units[key] = {
                "id": str(item.get("id", item.get("playerId", item.get("npcId", key)))),
                "name": _unit_name(item, previous.get("name") or ("NPC" if kind == "npc" else "玩家")),
                "kind": kind,
                "x": x,
                "y": y,
                "map_id": map_id,
            }
            self._seen[key] = now
        stale = [key for key, seen in self._seen.items() if now - seen > NEARBY_UNIT_TTL_SEC]
        for key in stale:
            self._seen.pop(key, None)
            self.units.pop(key, None)

    def on_map(self, map_id: int) -> list[dict[str, Any]]:
        rows = []
        for unit in self.units.values():
            if unit.get("map_id") is not None and unit.get("map_id") != map_id:
                continue
            if unit.get("x") is None or unit.get("y") is None:
                continue
            rows.append({
                "id": unit.get("id"),
                "name": unit.get("name") or "",
                "kind": unit.get("kind") or "player",
                "x": unit.get("x"),
                "y": unit.get("y"),
            })
        return rows


def minimap_bounds(
    nav_bounds: dict[str, float] | None,
    points: Iterable[tuple[float, float]],
) -> dict[str, float] | None:
    """Prefer navigation bounds, but always expand to include visible units."""
    xs: list[float] = []
    ys: list[float] = []
    for point in points:
        try:
            xs.append(float(point[0]))
            ys.append(float(point[1]))
        except (TypeError, ValueError, IndexError):
            continue
    unit_bounds = None
    if xs and ys:
        span_x = max(xs) - min(xs)
        span_y = max(ys) - min(ys)
        pad = max(400.0, span_x * 0.15, span_y * 0.15)
        unit_bounds = {
            "x0": min(xs) - pad,
            "y0": min(ys) - pad,
            "x1": max(xs) + pad,
            "y1": max(ys) + pad,
        }
    if nav_bounds and unit_bounds:
        return {
            "x0": min(float(nav_bounds["x0"]), unit_bounds["x0"]),
            "y0": min(float(nav_bounds["y0"]), unit_bounds["y0"]),
            "x1": max(float(nav_bounds["x1"]), unit_bounds["x1"]),
            "y1": max(float(nav_bounds["y1"]), unit_bounds["y1"]),
        }
    return nav_bounds or unit_bounds
