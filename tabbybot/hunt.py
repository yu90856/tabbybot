"""Single-account, single-map hunt and patrol state machine."""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from datetime import date
from typing import Any

from tabbybot.client import (
    ClientError,
    LoginError,
    QuestError,
    QuestLimitReached,
    RealtimeSession,
    accept_quest,
    claim_defeat,
    fetch_quest_delivery,
    fetch_quests,
    fetch_player,
    player_class,
    player_has_character,
    signed_login,
)
from tabbybot.config import AppConfig
from collections import deque

from tabbybot.constants import (
    COMBAT_TICK_SEC,
    IDLE_SYNC_INTERVAL_SEC,
    MAP_NAMES,
    DEFAULT_HUNT_MAP,
    MAP_ROUTES,
    MOVEMENT_SPEED,
    MOVE_TICK_SEC,
    PATROL_MAX_STEP,
    PATROL_SPEED_FACTOR,
    PORTAL_ARRIVE_DISTANCE,
    PORTAL_CENTERS,
    SPECIAL_ENTRY_MAPS,
    TRAVEL_REJECT_REASONS,
    TRAVEL_RESULT_TIMEOUT_SEC,
    TRAVEL_RETRY_SEC,
    TRAVEL_SYNC_WAIT_SEC,
    WAYPOINT_ARRIVE_CHASE,
    WAYPOINT_ARRIVE_PATROL,
    ATTACK_ERROR_TEXT,
    INVALID_TARGET_SKIP_SEC,
    TICKET_STALL_SEC,
    TICKET_WAIT_APPROACH_RATIO,
    AUTO_POTION_HP_RATIO,
    AUTO_POTION_RETRY_SEC,
    DAMAGE_SOURCE_MEMORY_SEC,
    ELITE_CONTEST_RADIUS,
    ELITE_POTION_MIN_DEFAULT,
    ELITE_VIEW_RANGE,
    ELITE_VIEW_RANGE_MIN,
    ELITE_VIEW_RANGE_MAX,
    MONSTER_THREAT_RADIUS,
    PLAYER_THREAT_RADIUS,
    KITE_ABORT_CONTACT_DIST,
    KITE_ABORT_CONTACTS,
    KITE_ABORT_LOCKS,
    KITE_CLEARANCE,
    KITE_HOLD_DIST,
    KITE_WALL_CLEARANCE,
    KITE_MIN_DIST,
    KITE_RETREAT_DIST,
    KITE_SHOOT_MAX,
    KITE_SHOOT_MIN,
    MONSTER_APPROACH_SPEED,
    KITE_PREDICT_HORIZON_SEC,
    MOVE_SPEED_MARGIN,
    MOVE_PAUSE_AFTER_CORRECTION_SEC,
    MOVE_PACKET_MAX_SEC,
    DEFAULT_PLAYER_DPS,
    DEFAULT_INCOMING_DPS,
    DEFAULT_REGEN_PER_SEC,
    CLUSTER_FIRST_WAIT_SEC,
    CLUSTER_ORBIT_STEP,
    CLUSTER_SITE_LOCK_SEC,
    CLUSTER_WAIT_MAX_SEC,
    KITE_LEASH_DEFAULT,
    KITE_LEASH_MIN,
    HUNT_ANCHORS,
    HUNT_ANCHOR_ARRIVE,
    HUNT_ANCHOR_RETURN,
    LEASH_RESET_LOCK_SEC,
    MAX_PULL_CHOICES,
    MAX_PULL_DEFAULT,
    ZERO_DAMAGE_STREAK,
    KITE_RETREAT_STEP,
    KITE_BURST_SHOTS,
    KITE_RETREAT_PROGRESS_SEC,
    KITE_RETREAT_MAX_SEC,
    KITE_RETREAT_PROGRESS_MIN,
    OVERLEVEL_DEFAULT,
    OVERLEVEL_HP_LOSS_RATIO,
    OVERLEVEL_LOCK_SEC,
    OVERLEVEL_NO_DAMAGE_SEC,
    OVERLEVEL_STEPS,
)
from tabbybot.farm import (
    DEFAULT_FARM_LEVEL,
    DEFAULT_FARM_MAP,
    MapLevelStore,
    choose_farm_map,
    choose_farm_map_with_reason,
    farm_status_text,
    is_farmable_map,
    level_sort_key,
    remember_monster_level,
)
from tabbybot.navigation import find_map_path, find_sweep_path, has_navigation
from tabbybot.learned_nav import learned_grid
from tabbybot.quests import (
    QUEST_ACCEPT_INTERVAL_SEC,
    QUEST_REFRESH_INTERVAL_SEC,
    QUEST_IDLE_RECHECK_SEC,
    active_quest_target,
    extract_quests,
    format_quest_rows,
    has_active_monster_quest,
    is_active_monster_quest,
    is_monster_quest,
    monster_matches_active_quest,
    format_delivery_prompt,
    next_daily_step,
    pick_next_monster_quest,
    quest_id,
    quest_map_id,
    quest_summary,
    quest_title,
    skip_reason,
    skipped_available,
)
from tabbybot.protocol import (
    MonsterCache,
    PeerCache,
    Position,
    TicketBox,
    minimap_bounds,
    apply_server_position,
    attack_elevation,
    attack_reach,
    attack_timing,
    build_monster_attack,
    build_respawn,
    build_sync,
    build_travel,
    busy_until,
    merge_self_state,
    monster_map_id,
    parse_welcome,
)

log = logging.getLogger(__name__)
combat_log = logging.getLogger("tabbybot.combat")   # human-readable lines for the GUI
debug_log = logging.getLogger("tabbybot.debug")     # raw detail -> ~/.tabbybot/debug.log only
debug_log.propagate = False

MONSTER_NAMES_ZH = {
    "skeleton": "骷髏怪", "elite_skeleton": "精英骷髏怪", "neiro_pump": "南瓜首領",
    "treasure_thief": "盜寶人", "spacedog": "太空狗", "butterfly": "逆蝶", "troll": "巨魔",
}


def setup_debug_log(path: Any = None) -> None:
    """Attach a rotating file handler for raw diagnostics (once)."""
    from logging.handlers import RotatingFileHandler
    from pathlib import Path as _P

    target = _P(path) if path is not None else _P.home() / ".tabbybot" / "debug.log"
    for handler in debug_log.handlers:
        if getattr(handler, "baseFilename", None) == str(target):
            return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(str(target), maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    debug_log.addHandler(handler)
    debug_log.setLevel(logging.DEBUG)
    # Keep the human-readable combat lines in the same file for context.
    if not any(getattr(h, "baseFilename", None) == str(target) for h in combat_log.handlers):
        combat_log.addHandler(handler)


_MONSTER_S2T = str.maketrans("盗贼髅莱蝎骑头树兽龙灵战卫恶鸟虫鱼亚尸狱钢铁变魇蛮猎鹰伤风雾狮将军长师术杀剑枪护队冻圣领蚁鳄鲨鲸龟猪驴马鸦鸡鹅鸭凤飞岭坟宝贪机铠诅萨满斗侠强夺游荡叶尘烬灾难惧愤疯泽岛云电闪击坚软粘统帅枭鹫蝾兰鲁纳尔达罗维迈顿觉无尽终极远时实体态质层阶级门关", "盜賊髏萊蠍騎頭樹獸龍靈戰衛惡鳥蟲魚亞屍獄鋼鐵變魘蠻獵鷹傷風霧獅將軍長師術殺劍槍護隊凍聖領蟻鱷鯊鯨龜豬驢馬鴉雞鵝鴨鳳飛嶺墳寶貪機鎧詛薩滿鬥俠強奪遊蕩葉塵燼災難懼憤瘋澤島雲電閃擊堅軟黏統帥梟鷲蠑蘭魯納爾達羅維邁頓覺無盡終極遠時實體態質層階級門關")


def monster_display_name(monster: dict[str, Any] | None) -> str:
    if not isinstance(monster, dict):
        return "—"
    name = ""
    for key in ("nameZh", "monsterNameZh", "displayNameZh"):
        if monster.get(key):
            name = str(monster[key])
            break
    if not name:
        for raw in (monster.get("monsterKey"), monster.get("name"), monster.get("displayName")):
            if not raw:
                continue
            key = str(raw).strip().casefold().replace(" ", "_").replace("-", "_")
            if key.startswith("monster."):
                key = key[len("monster."):]
            if key == "skeleton" and str(monster.get("monsterRank", "")).upper() == "ELITE":
                key = "elite_skeleton"
            name = MONSTER_NAMES_ZH.get(key, str(raw))
            break
    if not name:
        name = "精英怪" if str(monster.get("monsterRank", "")).upper() == "ELITE" else "怪物"
    name = name.translate(_MONSTER_S2T)
    level = monster_level(monster)
    if level is not None and name.endswith(f"Lv{level}"):
        return name  # already a display name (e.g. from an engagement record)
    return f"{name} Lv{level}" if level is not None else name


def maps_are_adjacent(current: int, target: int) -> bool:
    return target in MAP_ROUTES.get(current, ())


def find_travel_hops(start: int, goal: int, *, avoid_special: bool = True) -> list[int]:
    """Shortest portal route from start to goal, as a list of map ids to enter."""
    if start == goal:
        return []
    queue = deque([(start, [])])
    visited = {start}
    while queue:
        current, hops = queue.popleft()
        for neighbor in MAP_ROUTES.get(current, ()):
            if neighbor in visited:
                continue
            if avoid_special and neighbor in SPECIAL_ENTRY_MAPS and neighbor != goal:
                continue
            next_hops = hops + [neighbor]
            if neighbor == goal:
                return next_hops
            visited.add(neighbor)
            queue.append((neighbor, next_hops))
    return []


def travel_reject_text(reason: object, retry_after: object = None) -> str:
    code = str(reason or "未知原因")
    label = TRAVEL_REJECT_REASONS.get(code, code)
    text = f"切圖被拒：{label}"
    if code not in TRAVEL_REJECT_REASONS and code != "未知原因":
        text = f"切圖被拒：{label}（伺服器原因：{code}）"
    elif code in TRAVEL_REJECT_REASONS:
        text = f"切圖被拒：{label}（伺服器原因：{code}）"
    try:
        if retry_after is not None and float(retry_after) > 0:
            text += f"；{retry_after} 秒後可再試"
    except (TypeError, ValueError):
        pass
    return text


def monster_distance(pos: Position, monster: dict[str, Any]) -> float:
    try:
        return math.hypot(float(monster["x"]) - pos.x, float(monster["y"]) - pos.y)
    except (KeyError, TypeError, ValueError):
        return math.inf


def monster_level(monster: dict[str, Any]) -> int | None:
    for key in ("level", "monsterLevel", "requiredLevel", "lv"):
        if key in monster:
            try:
                return int(monster[key])
            except (TypeError, ValueError):
                return None
    return None


_NAV_CACHE: dict = {}


def _cached_navigator(map_id: int):
    if map_id not in _NAV_CACHE:
        from tabbybot.navigation import navigator_for
        _NAV_CACHE[map_id] = navigator_for(map_id)
    return _NAV_CACHE[map_id]


def segment_min_dist(a: tuple[float, float], b: tuple[float, float], point: tuple[float, float]) -> float:
    """Closest distance from `point` to the segment a→b."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 <= 1e-9:
        return math.dist(a, point)
    t = max(0.0, min(1.0, ((point[0] - ax) * dx + (point[1] - ay) * dy) / length2))
    return math.dist((ax + t * dx, ay + t * dy), point)


def path_clears_point(
    start: tuple[float, float],
    path: list[tuple[float, float]],
    point: tuple[float, float],
    min_dist: float,
) -> bool:
    """True if every segment of start→path stays at least min_dist from point."""
    prev = start
    for dest in path:
        if segment_min_dist(prev, dest, point) < min_dist:
            return False
        prev = dest
    return True


def arc_waypoints(
    center: tuple[float, float],
    start: tuple[float, float],
    goal: tuple[float, float],
    sign: float,
    radius: float,
    count: int = 5,
) -> list[tuple[float, float]]:
    """Points along the circle from start to goal in the given sign (1=ccw, -1=cw)."""
    a0 = math.atan2(start[1] - center[1], start[0] - center[0])
    a1 = math.atan2(goal[1] - center[1], goal[0] - center[0])
    delta = a1 - a0
    if sign >= 0 and delta <= 0:
        delta += 2 * math.pi
    elif sign < 0 and delta >= 0:
        delta -= 2 * math.pi
    radius = max(radius, 1.0)
    count = max(2, count)
    points = []
    for i in range(1, count + 1):
        ang = a0 + delta * (i / count)
        points.append((center[0] + radius * math.cos(ang), center[1] + radius * math.sin(ang)))
    return points


def step_towards(
    pos: Position,
    target: tuple[float, float],
    *,
    dt: float,
    max_step: float,
    speed_factor: float = 1.0,
) -> tuple[float, float, float, float]:
    dx = target[0] - pos.x
    dy = target[1] - pos.y
    distance = math.hypot(dx, dy)
    if distance <= 0:
        return pos.x, pos.y, 0.0, 0.0
    step = min(distance, MOVEMENT_SPEED * dt * speed_factor, max_step)
    nx = round(pos.x + dx / distance * step, 2)
    ny = round(pos.y + dy / distance * step, 2)
    return nx, ny, dx, dy


REST_LOW_RATIO = 0.35            # start resting below this HP (must actually leave)
REST_RESUME_RATIO = 0.80         # fight again at this HP (natural regen ~10/s)
REST_SAFE_DIST = 380.0           # back away while a monster is this close
REST_CORNERED_FAILS = 3          # only shoot while resting after this many failed retreats
REST_HINT_COUNT = 3              # this many low-HP rests ...
REST_HINT_WINDOW_SEC = 600.0     # ... within 10 min -> suggest a lower 練功等級
SPOT_REVISIT_SEC = 90.0          # don't re-check the same spawn spot more often than this
TRAVEL_STUCK_RADIUS = 200.0      # px: less movement than this counts as "same spot"
TRAVEL_STUCK_GIVEUP_SEC = 75.0   # give up a blocked portal walk before the 2-min watchdog
FARM_HOLD_SEC = 900.0            # then grind on the current map for 15 min


KITE_NO_KITE_MAX_LEVEL = 30  # normal mobs at/below this level are not kited


class HuntController:
    def __init__(self, config: AppConfig, character_class: str = "WARRIOR"):
        self.config = config
        self.character_class = character_class.upper()
        self.position: Position | None = None
        self.self_state: dict[str, Any] = {}
        self.monsters = MonsterCache()
        self.tickets = TicketBox()
        self.world_ready = False
        self.auth_rejected = False
        self.login_error = False
        self.last_snapshot_at = 0.0
        self._shot_move_at = 0.0
        self._attack_gen = 0
        self._rest_retreat_fails = 0
        self._last_move_sent_at = 0.0
        self._last_accepted_at = 0.0
        self._last_accepted_pos: tuple[float, float] | None = None
        self._move_pause_until = 0.0
        self._move_speed_scale = MOVE_SPEED_MARGIN
        self.too_fast_count = 0
        self._mon_seen: dict[str, tuple[float, float, float]] = {}
        self._dmg_dealt_events: list[tuple[float, float]] = []
        self._dmg_taken_events: list[tuple[float, float]] = []
        self._regen_events: list[tuple[float, float]] = []
        self._kite_dist_log: list[tuple[float, float, float]] = []
        self.last_kite_distance = 0.0
        self.chase_route: list[tuple[float, float]] = []
        self.chase_target: str | None = None
        self.patrol_route: list[tuple[float, float]] = []
        self.patrol_index = 0
        self.last_sync_at = 0.0
        self.claimed: set[str] = set()
        self.pending_travel: int | None = None
        self.travel_requested_at = 0.0
        self.status = "等待世界資料"
        self.attack_speed_percent = 0.0
        self._respawn_sent = False
        self.last_move_at = 0.0
        self.patrol_last_at = 0.0
        self.chase_last_at = 0.0
        self.session: RealtimeSession | None = None
        self._claim_token = ""
        self.kills = 0
        # "No quest today" is logged once per day; quest polling then slows to every 15 min.
        self._quest_idle_date: date | None = None
        self._quest_idle_at = 0.0
        self._quest_idle_logged: date | None = None
        self.own_kills = 0  # own-hit monsters that died without a rewards[] entry
        # Maps without navigation data (石谷/西部荒野/南部森林): walk around walls on repeated rejections.
        self._invalid_moves: list[float] = []
        self._last_move_goal: tuple[float, float] | None = None
        self._last_step_to: tuple[float, float] | None = None
        self._nav_replan = False
        self.learned_blocks = 0
        self._travel_stuck_anchor: tuple[float, float] | None = None
        self._travel_stuck_since = 0.0
        self.farm_hold: tuple[int, float] | None = None
        self._spot_visits: dict[tuple[int, int, int], float] = {}
        self._patrol_note: tuple | None = None  # (map, until): grind here, travel blocked
        self.detours = 0
        self._combat_last: dict[str, float] = {}
        self._last_kill_at: float | None = None
        self._last_kill_map: int | None = None
        # Leash / reset detection (t624)
        self.kite_leash = KITE_LEASH_DEFAULT
        self.leash_resets = 0
        self.hunt_anchor: tuple[float, float] | None = None
        self.hunt_anchor_clear = 0.0
        self.hunt_anchor_leash = 0.0
        self._anchor_route: list[tuple[float, float]] = []
        self.zero_damage_hits = 0
        self._zero_streak: dict[str, int] = {}
        self._server_hp: dict[str, int] = {}
        # Cluster orbit kiting (t621)
        from tabbybot.cluster import ClusterTracker
        self.clusters = ClusterTracker()
        self.cluster_kite = True
        self.max_pull = 1  # game only allows attacking one target at a time
        self.cluster_site_id: int | None = None
        self.cluster_note = ""
        self._cluster_update_at = 0.0
        self._site_lock: dict[int, float] = {}
        self._own_counted: set[str] = set()
        self.hits = 0
        self.hits_taken = 0
        self.damage = 0
        self.potions = 0
        self.auto_combat = True
        self.movement_paused = False
        self.hunt_idle = False
        self.stop_after_kill = False
        self.pinned_map: int | None = None
        self.kite_elite_only = False
        self.auto_return = True
        self.last_rtt_ms = None
        self.activity = "待命"
        self.pending_portal = None
        self.travel_route: list[tuple[float, float]] = []
        self.travel_phase: str | None = None
        self.travel_attempt = 0
        self.travel_sync_at = 0.0
        self.travel_sent_at = 0.0
        self.travel_last_at = 0.0
        self.travel_quest_id = 0
        self.travel_retry_after = 0.0
        self.travel_final_target: int | None = None
        self.travel_quest_id = 0
        self.quests: list[dict[str, Any]] = []
        self.auto_quest = False
        self.task_limited = False
        self.quest_map_auto = False
        self.daily_mode = False
        self.quest_map_target: int | None = None
        self.quest_map_quest_id = 0
        self.quest_status = "尚未讀取任務"
        self.quest_accept_blocked_date = None
        self.quest_accept_inflight = False
        self.quest_accept_checked_at = 0.0
        self.quest_refresh_at = 0.0
        self.quest_refresh_inflight = False
        self._quests_dirty = False
        self._accept_result: dict[str, Any] | None = None
        self._refresh_result: list[dict[str, Any]] | None = None
        self._quest_completion_state: dict[str, bool] = {}
        self.auto_replace = False
        self.skip_vendetta = False
        self.pending_delivery: dict[str, Any] | None = None
        self.skipped_quest_ids: set[int] = set()
        self.auto_farm = True
        self.farm_target_level = DEFAULT_FARM_LEVEL
        if getattr(config, "farm", None) is not None:
            self.auto_farm = bool(config.farm.enabled)
            self.farm_target_level = int(config.farm.target_level or DEFAULT_FARM_LEVEL)
        self.level_store = MapLevelStore(None)
        self.level_catalog: dict[int, list[int]] = self.level_store.catalog
        self.quest_first = True
        self.farm_reason = ""
        self.level_filter_text = ""
        self.peers = PeerCache()
        self.manual_goal: tuple[float, float] | None = None
        self.manual_route: list[tuple[float, float]] = []
        self.manual_last_at = 0.0
        self.user_packets: deque = deque()
        self.chat_log: deque = deque(maxlen=80)
        self.player_id: int | None = None
        self.attack_target: str | None = None
        self.attacks_sent = 0
        self.tickets_received = 0
        self.attack_errors: dict[str, int] = {}
        self.ticket_wait_since: float | None = None
        self.ticket_resyncs = 0
        self._last_error_log: dict[str, float] = {}
        self.wait_reason = ""
        self.packet_types: dict[str, int] = {}
        # Elite / potion settings (persisted by the GUI).
        self.elite_potion_min = ELITE_POTION_MIN_DEFAULT
        self.potion_on_player_damage = False
        self.prefer_elite = True
        self.elite_view_range = float(ELITE_VIEW_RANGE)
        self.auto_potion = False  # t634: no auto potions while grinding; rest instead
        self.resting = False
        self._rest_started = 0.0
        self._low_hp_events: list[float] = []
        self._rest_hint_at = -1e9
        self.auto_potion_ratio = AUTO_POTION_HP_RATIO
        self.last_damage_source = ""
        self.last_damage_at = 0.0
        self._last_hp: int | None = None
        self._potion_requested_at = 0.0
        self._player_hit_at = 0.0
        self.potion_note = ""
        self.elite_note = ""
        self.kite_low_potion = False
        self.kite_targets: set[str] = set()
        self.kite_failed: set[str] = set()
        self._kite_locks = 0
        self._kite_contacts = 0
        self._kite_route: list[tuple[float, float]] = []
        self._kite_last_at = 0.0
        self._retreat_started_at = 0.0
        self._retreat_start_dist = 0.0
        self._retreat_start_hp: int | None = None
        self._retreat_tried_straight = False
        self._kite_burst_left = 0
        self.last_attack_at = 0.0
        self._retarget_now = False
        self.kite_note = ""
        self.overlevel_kite = False
        self.overlevel_max = OVERLEVEL_DEFAULT
        self.overlevel_targets: set[str] = set()
        self.overlevel_band_lock: dict[int, float] = {}
        self.overlevel_note = ""
        from tabbybot.efficiency import EfficiencyTracker
        self.efficiency = EfficiencyTracker(None)
        self._engage: dict[str, Any] | None = None
        self._own_hit_ids: set[str] = set()
        self._recorded_kills: set[str] = set()
        self.quest_kills = 0
        self._quest_progress: dict[Any, int] = {}

    @property
    def hunt_map(self) -> int:
        return self.config.hunt.map_id

    def on_packet(self, packet: dict[str, Any]) -> None:
        kind = packet.get("type")
        key = str(kind)
        if key not in self.packet_types and len(self.packet_types) < 60:
            self.packet_types[key] = 0
        if key in self.packet_types:
            self.packet_types[key] += 1
        if kind != "snapshot" and isinstance(packet.get("rewards"), list):
            self._ingest_rewards(packet["rewards"])
        if kind == "error" and packet.get("code") == "login_required":
            self.auth_rejected = True
            self.login_error = True
            self.status = "伺服器拒絕會話"
            return
        if kind == "pong":
            sent = packet.get("sentAt")
            if sent is not None:
                try:
                    self.last_rtt_ms = max(0, int(time.monotonic() * 1000) - int(sent))
                except (TypeError, ValueError):
                    pass
            return
        welcome = parse_welcome(packet)
        if welcome:
            map_id, x, y, revision = welcome
            self.position, _ = apply_server_position(None, map_id, x, y, revision)
            self._note_accepted_position(time.monotonic())
            return
        if kind == "snapshot":
            self._on_snapshot(packet)
            return
        if kind in {"chat", "scoped_chat", "chat_ack"}:
            self._ingest_chat(packet)
            return
        if kind == "hit" and isinstance(packet.get("hit"), dict):
            self._on_hit(packet["hit"], packet.get("attacker"))
            return
        if kind == "attack_ready" and isinstance(packet.get("nextAttack"), dict):
            self._accept_ticket(packet["nextAttack"])
            return
        if kind == "error" and packet.get("action") == "monsterAttack":
            self._on_attack_error(packet)
            return
        if kind == "error" and packet.get("code") in {"movement_too_fast", "invalid_position", "combat_locked"}:
            if packet.get("code") == "combat_locked" and self.attack_target and self.attack_target in self.kite_targets:
                self._kite_locks += 1
                if self._kite_locks >= KITE_ABORT_LOCKS:
                    self.abort_kite(self.attack_target, "伺服器在戰鬥中鎖定移動（combat_locked）")
            self._on_movement_error(packet)
            return
        if kind == "travel_result":
            self._on_travel_result(packet)

    def _on_snapshot(self, packet: dict[str, Any]) -> None:
        self.last_snapshot_at = time.monotonic()
        previous_potions = self.self_state.get("healthPotions")
        self.self_state = merge_self_state(self.self_state, packet)
        self._track_damage(time.monotonic())
        if isinstance(packet.get("self"), dict) and ("experience" in packet["self"] or "exp" in packet["self"]):
            from tabbybot.efficiency import extract_experience
            exp = extract_experience(self.self_state)
            if exp is not None:
                self.efficiency.observe_experience(exp[0], exp[1], time.monotonic())
        current_potions = self.self_state.get("healthPotions")
        try:
            if isinstance(previous_potions, (int, float)) and isinstance(current_potions, (int, float)):
                used = max(0, int(previous_potions) - int(current_potions))
                if used:
                    self.potions += used
        except (TypeError, ValueError):
            pass
        if "life" in self.self_state:
            if self.session is not None:
                try:
                    self.session.life = int(self.self_state["life"])
                except (TypeError, ValueError):
                    pass
        if "characterClass" in self.self_state:
            self.character_class = str(self.self_state["characterClass"]).upper()
        totals = {}
        equipment = self.self_state.get("equipment")
        if isinstance(equipment, dict) and isinstance(equipment.get("totals"), dict):
            totals = equipment["totals"]
        elif isinstance(self.self_state.get("totals"), dict):
            totals = self.self_state["totals"]
        try:
            self.attack_speed_percent = float(totals.get("attackSpeedPercent", self.attack_speed_percent) or 0)
        except (TypeError, ValueError):
            pass
        if isinstance(self.self_state.get("nextAttack"), dict):
            self._accept_ticket(self.self_state["nextAttack"])
        if str(packet.get("action", "")) == "respawn" and isinstance(packet.get("position"), dict):
            pos = packet["position"]
            current = self.position
            adopted, _ = apply_server_position(
                current,
                int(pos.get("map", current.map_id if current else 1)),
                float(pos.get("x", current.x if current else 0)),
                float(pos.get("y", current.y if current else 0)),
                int(pos.get("revision", current.revision if current else 0)),
            )
            self.position = adopted
            self._note_accepted_position(time.monotonic())
            self._respawn_sent = False
            self.tickets.used.clear()
            self.tickets.busy_until_at = 0.0
            self.stop_routes()
        self.monsters.apply_snapshot(packet, self.position.map_id if self.position else None)
        self.check_engaged_death(time.monotonic())
        self._check_regen()
        self.peers.apply_snapshot(packet, self.position.map_id if self.position else None)
        self.record_monster_levels()
        self._ingest_chat(packet)
        self._ingest_rewards(packet.get("rewards") or [])
        progress = packet.get("progress")
        if isinstance(progress, dict):
            quests = extract_quests(progress)
            if quests:
                self.update_quests(quests)
        if not self.world_ready:
            self.world_ready = True
            self.status = "世界已就緒"
            self._quests_dirty = True
        if self.is_dead and not self._respawn_sent:
            self.status = "角色陣亡，準備復活"

    def _ingest_rewards(self, rewards: list) -> None:
        for reward in rewards:
            if not isinstance(reward, dict):
                continue
            raw_id = reward.get("EncounterID") or reward.get("encounterId") or reward.get("encounter_id")
            if raw_id:
                encounter_id = str(raw_id)
                fresh = encounter_id not in self.claimed
                if fresh:
                    self.claimed.add(encounter_id)
                    self.kills += 1
                    if encounter_id in self._own_counted:  # already counted from HP reaching 0
                        self._own_counted.discard(encounter_id)
                        self.own_kills = max(0, self.own_kills - 1)
                if self._engage and self._engage.get("id") == encounter_id:
                    self._record_engaged_kill(time.monotonic())
                if fresh:
                    if self._claim_token:
                        try:
                            claimed = claim_defeat(self.config, self._claim_token, encounter_id)
                        except Exception:
                            log.warning("領取擊敗獎勵時發生錯誤")
                            claimed = None
                        if claimed:
                            quests = extract_quests(claimed)
                            if quests:
                                self.update_quests(quests)
                            else:
                                self._quests_dirty = True

    def _on_movement_error(self, packet: dict[str, Any]) -> None:
        position = packet.get("position")
        if not isinstance(position, dict) or self.position is None:
            return
        try:
            revision = int(position.get("revision", self.position.revision))
            adopted, applied = apply_server_position(
                self.position,
                int(position.get("map", self.position.map_id)),
                float(position.get("x", self.position.x)),
                float(position.get("y", self.position.y)),
                revision,
            )
        except (TypeError, ValueError):
            return
        if not applied:
            log.info("忽略過期移動糾正：服務端修訂 %s，本地 %s", revision, self.position.revision)
            return
        self.position = adopted
        self.stop_routes()
        now = time.monotonic()
        self.status = f"服務端糾正位置（{packet.get('code')}）"
        self._note_accepted_position(now)
        self._move_pause_until = now + MOVE_PAUSE_AFTER_CORRECTION_SEC
        if str(packet.get("code")) == "movement_too_fast":
            self.too_fast_count += 1
            self._move_speed_scale = max(0.70, self._move_speed_scale * 0.90)
            log.info("移動過快，已對齊伺服器位置並暫停移動（第 %d 次）", self.too_fast_count)
        if str(packet.get("code")) == "invalid_position":
            self._note_invalid_move(now)

    def _note_invalid_move(self, now: float) -> None:
        """Server rejected a step. On maps without official navigation data, learn the blocked
        cell ahead of the corrected (valid) position, then re-plan around it with A*."""
        if self.position is None:
            return
        map_id = self.position.map_id
        here = (float(self.position.x), float(self.position.y))
        self._invalid_moves = [t for t in self._invalid_moves if now - t < 8.0] + [now]
        if self.travel_phase == "walk":
            if self._travel_stuck_anchor is None or math.dist(here, self._travel_stuck_anchor) > TRAVEL_STUCK_RADIUS:
                self._travel_stuck_anchor = here
                self._travel_stuck_since = now
            elif now - self._travel_stuck_since >= TRAVEL_STUCK_GIVEUP_SEC:
                self.abandon_travel(now, "一直被伺服器擋回原地")
                return
        self._reject_times = [t for t in getattr(self, "_reject_times", []) if now - t < 3.0] + [now]
        toward = self._last_step_to
        if toward is None or math.dist(here, toward) < 3.0:
            toward = self.pending_portal if self.travel_phase == "walk" and self.pending_portal else self._last_move_goal
        if toward is None:
            return
        grid = learned_grid(map_id)
        added = grid.learn_rejection(here, toward)
        if added:
            self.learned_blocks += added
        self._nav_replan = True
        self.detours += 1
        self.status = "前方有障礙，記下並繞路"
        self.combat_event("移動（繞過障礙）", None, throttle=5.0, key="detour")
        debug_log.debug("blocked map=%s at=%s toward=%s new=%s total=%s", map_id, here, toward, added,
                        len(grid.blocked))

    def abandon_travel(self, now: float, why: str) -> None:
        """Can't reach the next portal: grind on this map for a while instead of standing still."""
        if self.position is None:
            return
        dest = self.travel_final_target or self.pending_travel
        name = MAP_NAMES.get(dest, dest) if dest else "目標地圖"
        here = MAP_NAMES.get(self.position.map_id, self.position.map_id)
        self._clear_travel()
        self._travel_stuck_anchor = None
        self.travel_retry_after = now + FARM_HOLD_SEC
        self.farm_hold = (self.position.map_id, now + FARM_HOLD_SEC)
        learned_grid(self.position.map_id).save(force=True)
        self.status = (f"前往 {name} 的路走不通（{why}），先在 {here} 打最接近 Lv{self.farm_target_level} 的怪，"
                       f"{int(FARM_HOLD_SEC // 60)} 分鐘後再試")
        log.info("%s", self.status)

    def farm_hold_active(self, now: float | None = None) -> bool:
        if self.farm_hold is None or self.position is None:
            return False
        now = time.monotonic() if now is None else now
        if now >= self.farm_hold[1] or self.farm_hold[0] != self.position.map_id:
            self.farm_hold = None
            return False
        return True

    def _learned_route(self, goal: tuple[float, float]) -> list[tuple[float, float]] | None:
        if self.position is None:
            return None
        path = learned_grid(self.position.map_id).find_path((self.position.x, self.position.y), goal)
        return path or None

    def _on_travel_result(self, packet: dict[str, Any]) -> None:
        destination = self.pending_travel
        if packet.get("ok"):
            if self.position is None:
                self._clear_travel()
                return
            map_id = int(destination or self.position.map_id)
            x = float(packet.get("x", self.position.x))
            y = float(packet.get("y", self.position.y))
            revision = int(packet.get("revision", self.position.revision))
            self.position, _ = apply_server_position(self.position, map_id, x, y, revision, ignore_stale=False)
            self._note_accepted_position(time.monotonic())
            arrived = MAP_NAMES.get(map_id, map_id)
            self.status = f"已進入 {arrived}"
            self.activity = f"已進入 {arrived}"
            log.info("已進入 %s。", arrived)
            self.stop_routes()
            self._clear_travel()
            return
        reason = packet.get("reason", "未知原因")
        message = travel_reject_text(reason, packet.get("retryAfter"))
        self.status = message
        self.activity = message
        log.info("%s", message)
        position = packet.get("position")
        if isinstance(position, dict) and self.position is not None:
            try:
                if int(position.get("map", self.position.map_id)) == self.position.map_id:
                    adopted, applied = apply_server_position(
                        self.position,
                        self.position.map_id,
                        float(position.get("x", self.position.x)),
                        float(position.get("y", self.position.y)),
                        int(position.get("revision", self.position.revision)),
                    )
                    if applied:
                        self.position = adopted
            except (TypeError, ValueError):
                pass
        retry = TRAVEL_RETRY_SEC
        try:
            if packet.get("retryAfter") is not None:
                retry = max(TRAVEL_RETRY_SEC, float(packet["retryAfter"]))
        except (TypeError, ValueError):
            pass
        self._clear_travel()
        self.travel_retry_after = time.monotonic() + retry

    @property
    def is_dead(self) -> bool:
        return bool(self.self_state.get("dead"))

    def stop_routes(self) -> None:
        self.chase_route = []
        self.chase_target = None
        self.patrol_route = []

    def pause_movement(self) -> None:
        self.movement_paused = True
        self.stop_routes()
        self.clear_manual_move()
        self._clear_travel()
        self.status = "已停止移動"
        log.info("已停止移動。")

    def pin_map(self, map_id: int) -> None:
        """Remember a map the user picked; auto-farm must not override it."""
        self.pinned_map = int(map_id)
        self.config.hunt.map_id = int(map_id)
        if self.travel_final_target != self.pinned_map:
            self._clear_travel()
        self.apply_hunt_anchor(int(map_id))

    def apply_hunt_anchor(self, map_id: int | None = None) -> None:
        """Use the recorded open pocket for this map, or clear it if none."""
        mid = int(map_id if map_id is not None else (self.position.map_id if self.position else 0))
        row = HUNT_ANCHORS.get(mid)
        if not row:
            if self.hunt_anchor is not None:
                self.hunt_anchor = None
                self.hunt_anchor_clear = 0.0
                self.hunt_anchor_leash = 0.0
                self._anchor_route = []
            return
        x, y, clear, leash = row
        self.hunt_anchor = (float(x), float(y))
        self.hunt_anchor_clear = float(clear)
        self.hunt_anchor_leash = float(leash)

    def _anchor_active(self) -> bool:
        mid = int(self.position.map_id) if self.position is not None else 0
        if mid in HUNT_ANCHORS:
            if self.hunt_anchor != tuple(HUNT_ANCHORS[mid][:2]):
                self.apply_hunt_anchor(mid)
            return self.hunt_anchor is not None
        return False

    def _in_anchor_leash(self, x: float, y: float) -> bool:
        if not self._anchor_active() or self.hunt_anchor is None:
            return True
        return math.hypot(x - self.hunt_anchor[0], y - self.hunt_anchor[1]) <= self.hunt_anchor_leash

    def _monster_in_anchor(self, encounter_id: str, monster: dict) -> bool:
        # On a map with a farm pocket, never fall open and fight the corridors.
        mid = int(self.position.map_id) if self.position is not None else 0
        if mid in HUNT_ANCHORS and not self._anchor_active():
            self.apply_hunt_anchor(mid)
        if not self._anchor_active():
            return mid not in HUNT_ANCHORS
        try:
            mx, my = float(monster["x"]), float(monster["y"])
        except (KeyError, TypeError, ValueError):
            return False
        if self._in_anchor_leash(mx, my):
            return True
        # Only finish a pulled fight while we are still inside the pocket.
        if self.position is not None and self._in_anchor_leash(self.position.x, self.position.y):
            if encounter_id in {self.attack_target, self.chase_target} or encounter_id in getattr(self, "_own_hit_ids", ()):
                return True
        return False

    def plan_anchor_return(self, now: float) -> dict[str, Any] | None:
        """Walk back to the open pocket when we have drifted and are not mid-fight."""
        if self.position is None or not self._anchor_active() or self.hunt_anchor is None:
            return None
        if self.movement_paused:
            return None
        here = (self.position.x, self.position.y)
        dist = math.dist(here, self.hunt_anchor)
        if dist <= HUNT_ANCHOR_ARRIVE:
            self._anchor_route = []
            return None
        if not self._anchor_route or math.dist(self._anchor_route[-1], self.hunt_anchor) > 30:
            path = find_map_path(self.position.map_id, here, self.hunt_anchor) if has_navigation(self.position.map_id) else [self.hunt_anchor]
            self._anchor_route = list(path or [self.hunt_anchor])
        self._anchor_route, packet = self._advance_route(
            self._anchor_route, HUNT_ANCHOR_ARRIVE, dt=MOVE_TICK_SEC,
            max_step=MOVEMENT_SPEED / 10.0, speed_factor=1.0, now=now,
        )
        if packet is not None:
            self.status = (f"返回空曠練功點 ({self.hunt_anchor[0]:.0f}, {self.hunt_anchor[1]:.0f})"
                           f"，淨空半徑 {self.hunt_anchor_clear:.0f}")
            self.activity = "返回空曠練功點"
        return packet


    def live_combat_target(self) -> str | None:
        for encounter_id in (self.attack_target, self.chase_target):
            if not encounter_id:
                continue
            monster = self.monsters.monsters.get(encounter_id)
            try:
                if isinstance(monster, dict) and int(monster.get("health", 0) or 0) > 0:
                    return encounter_id
            except (TypeError, ValueError):
                continue
        return None

    def under_attack(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return bool(self.last_damage_at and now - self.last_damage_at <= 3.0 and self.last_damage_source == "monster")

    def request_soft_stop(self, immediate: bool = False) -> None:
        """Stay logged in. Finish the current target unless immediate, then stand still."""
        self.clear_manual_move()
        self._clear_travel()
        self.patrol_route = []
        if immediate:
            self.stop_routes()
            self.attack_target = None
            self.chase_target = None
            self._enter_hunt_idle("已立刻停止打怪，原地待命（未登出）")
            return
        if self.live_combat_target():
            self.stop_after_kill = True
            self.hunt_idle = False
            self.movement_paused = False
            self.status = "打完目前目標後暫停（不會登出）"
            self.activity = self.status
            log.info("%s", self.status)
            return
        self._enter_hunt_idle("已停止打怪，原地待命（未登出）")

    def _enter_hunt_idle(self, message: str) -> None:
        self.stop_after_kill = False
        self.hunt_idle = True
        self.movement_paused = True
        self.attack_target = None
        self.chase_target = None
        self.stop_routes()
        self.clear_manual_move()
        self._clear_travel()
        self.status = message
        self.activity = "原地待命"
        log.info("%s", message)

    def resume_hunt(self) -> None:
        self.hunt_idle = False
        self.stop_after_kill = False
        self.movement_paused = False
        self.status = "繼續打怪"
        self.activity = self.status
        log.info("%s", self.status)

    def _nearest_attacker(self, now: float) -> str | None:
        if self.position is None:
            return None
        me = (self.position.x, self.position.y)
        best: tuple[float, str] | None = None
        for encounter_id, monster in self.monsters.living_on_map(self.position.map_id):
            try:
                dist = math.dist(me, (float(monster["x"]), float(monster["y"])))
            except (KeyError, TypeError, ValueError):
                continue
            if dist <= MONSTER_THREAT_RADIUS or encounter_id in self._own_hit_ids:
                if best is None or dist < best[0]:
                    best = (dist, encounter_id)
        return best[1] if best else None

    def _engage_one(self, encounter_id: str, now: float, *, may_approach: bool, may_kite: bool) -> tuple[str, Any]:
        if self.position is None:
            return ("idle", None)
        monster = self.monsters.monsters.get(encounter_id)
        if not isinstance(monster, dict):
            return ("idle", None)
        try:
            distance = math.dist((self.position.x, self.position.y), (float(monster["x"]), float(monster["y"])))
        except (KeyError, TypeError, ValueError):
            return ("idle", None)
        from tabbybot.protocol import attack_reach
        reach = attack_reach(self.character_class, "prepare")
        self.attack_target = encounter_id
        if distance <= reach:
            planned = self.plan_attack(encounter_id, now)
            if planned:
                return ("attack", planned)
        if may_kite and self.kite_active(encounter_id) and self._can_stutter_move(now) and distance < KITE_SHOOT_MIN:
            step = self.plan_kite_step(encounter_id, distance, now)
            if step is not None:
                self.activity = "風箏精英" if self.is_elite(monster) else "風箏"
                return ("chase", step)
        if distance <= reach:
            return self._wait_for_ticket(encounter_id, [(distance, encounter_id, monster)], now, approach=False)
        if may_approach:
            self.chase_target = encounter_id
            packet = self.plan_chase_step(now)
            if packet is None:
                # Committed target unreachable/stale: drop it and chase the nearest valid one.
                if self.attack_target == self.chase_target:
                    self.attack_target = None
                self.chase_target = candidates[0][1]
                self.chase_route = []
                packet = self.plan_chase_step(now)
            self.activity = "追擊"
            return ("chase", packet)
        return ("wait", None)

    def _paused_combat_action(self, now: float) -> tuple[str, Any]:
        if self.stop_after_kill and not self.hunt_idle:
            target = self.live_combat_target()
            if not target:
                self._enter_hunt_idle("目標已擊殺，原地待命（未登出）")
                return ("idle", None)
            return self._engage_one(target, now, may_approach=True, may_kite=True)
        if self.under_attack(now):
            attacker = self._nearest_attacker(now)
            if attacker:
                return self._engage_one(attacker, now, may_approach=False, may_kite=False)
        self.activity = "原地待命"
        if "待命" not in (self.status or ""):
            self.status = "已停止打怪，原地待命（未登出）"
        return ("idle", None)

    def _accept_ticket(self, raw: dict[str, Any]) -> None:
        before = self.tickets.current.id if self.tickets.current else None
        ticket = self.tickets.accept(raw)
        if ticket is not None and ticket.id != before:
            self.tickets_received += 1
            self.ticket_wait_since = None

    def _on_attack_error(self, packet: dict[str, Any], now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        code = str(packet.get("code", "") or "unknown")
        self.attack_errors[code] = self.attack_errors.get(code, 0) + 1
        target = self.attack_target
        self.tickets.on_monster_attack_error(code, packet, now)
        if code not in {"invalid_target", "attack_cooldown"}:
            # Server rejected without consuming: release the local reservation
            # so the same ticket can be retried (matches the official client).
            self.tickets.unconsume()
        if code == "invalid_target" and target:
            self.tickets.lock_invalid(target, now, INVALID_TARGET_SKIP_SEC)
            if self.attack_target == target:
                self.attack_target = None
            if self.chase_target == target:
                self.chase_target = None
                self.chase_route = []
            self._clear_retreat()
            self._kite_route = []
            self._retarget_now = True
        text = ATTACK_ERROR_TEXT.get(code, f"攻擊被伺服器拒絕（{code}）")
        self.status = text
        if now - self._last_error_log.get(code, 0.0) >= 10:
            self._last_error_log[code] = now
            log.info("%s", text)

    def _on_hit(self, hit: dict[str, Any], attacker: Any = None) -> None:
        victim = hit.get("player", hit.get("target", hit.get("targetPlayer", hit.get("victim"))))
        if self.player_id is not None and victim is not None and "monster" not in hit:
            try:
                if int(victim) == int(self.player_id) and attacker is not None and int(attacker) != int(self.player_id):
                    self._player_hit_at = time.monotonic()
                    return
            except (TypeError, ValueError):
                pass
        if self.player_id is not None and attacker is not None:
            try:
                if int(attacker) != int(self.player_id):
                    return
            except (TypeError, ValueError):
                return
        try:
            amount = max(0, int(hit.get("damage", hit.get("amount", hit.get("value", 0))) or 0))
        except (TypeError, ValueError):
            amount = 0
        self.damage += amount
        self.hits += 1
        now = time.monotonic()
        if amount:
            self._dmg_dealt_events.append((now, float(amount)))
            self._trim_rate(self._dmg_dealt_events, now)
        hit_id = str(hit.get("monster", ""))
        if hit_id:
            self._own_hit_ids.add(hit_id)
            self.last_own_hit_at = time.monotonic()
            if self._engage and self._engage.get("id") == hit_id:
                self._engage["last_hit_at"] = time.monotonic()
        monster = self.monsters.monsters.get(hit_id)
        if monster is not None and "health" in monster:
            try:
                monster["health"] = max(0, int(monster["health"]) - amount)
            except (TypeError, ValueError):
                pass
        debug_log.debug("hit monster=%s damage=%s hp=%s/%s raw=%s", hit_id[:8], amount,
                        (monster or {}).get("health"), (monster or {}).get("maxHealth"), hit)
        self.combat_event("攻擊", monster, amount)
        if hit_id:
            if amount <= 0:
                self.zero_damage_hits += 1
                streak = self._zero_streak.get(hit_id, 0) + 1
                self._zero_streak[hit_id] = streak
                if streak >= ZERO_DAMAGE_STREAK:
                    self.mark_resetting(hit_id, f"連續 {streak} 下 0 傷害")
            else:
                self._zero_streak.pop(hit_id, None)
        self.check_engaged_death(time.monotonic())

    def combat_event(self, action: str, monster: dict[str, Any] | None = None, damage: Any = None, *,
                     throttle: float = 0.0, key: str | None = None) -> None:
        """One readable line: 目標 | 動作 | 傷害 | 目標生命 | 我的生命 (time is added by the handler)."""
        now = time.monotonic()
        k = key or action
        if throttle and now - self._combat_last.get(k, -1e9) < throttle:
            return
        self._combat_last[k] = now
        if len(self._combat_last) > 500:
            self._combat_last.clear()
        target = monster_display_name(monster) if monster else "—"
        mhp = "—"
        if isinstance(monster, dict) and monster.get("health") is not None:
            mhp = f"{monster.get('health')}/{monster.get('maxHealth', '?')}"
        hp, max_hp = self.self_state.get("health"), self.self_state.get("maxHealth")
        mine = f"{hp}/{max_hp}" if hp is not None else "—"
        dmg = "—" if damage is None else str(damage)
        combat_log.info("| %s | %s | %s | %s | %s", target, action, dmg, mhp, mine)

    def note_action(self, kind: str, now: float) -> None:
        """Movement lines for the GUI log (throttled)."""
        if kind == "chase":
            target_id = self.chase_target or self.attack_target
            monster = self.monsters.monsters.get(target_id) if target_id else None
            if self.activity in {"風箏精英", "繞圈風箏"}:
                return  # retreat lines are logged where they happen
            self.combat_event("移動", monster, throttle=6.0, key=f"move:{target_id}")
        elif kind == "patrol":
            self.combat_event("移動（巡邏找怪）", None, throttle=20.0, key="patrol")

    def kill_count(self) -> int:
        return max(self.kills + self.own_kills, self.quest_kills)

    def map_payload(self) -> dict[str, Any]:
        from tabbybot.constants import MAP_TITLES
        from tabbybot.navigation import navigator_for

        pos = self.position
        monsters = []
        if pos is not None:
            for encounter_id, monster in self.monsters.on_map(pos.map_id).items():
                if not isinstance(monster, dict):
                    continue
                try:
                    health = int(monster.get("health", 0) or 0)
                except (TypeError, ValueError):
                    health = 0
                level = monster_level(monster)
                monsters.append({
                    "id": encounter_id[:8],
                    "x": monster.get("x"),
                    "y": monster.get("y"),
                    "health": health,
                    "alive": health > 0,
                    "elite": str(monster.get("monsterRank", "")).upper() == "ELITE",
                    "name": str(monster.get("name") or monster.get("monsterKey") or monster.get("type") or ""),
                    "level": level,
                })
        peers = []
        nav_bounds = None
        if pos is not None:
            peers = self.peers.on_map(pos.map_id)
            nav = navigator_for(pos.map_id)
            if nav is not None and nav.data.cells:
                size = nav.data.cell_size
                xs = [cell[0] for cell in nav.data.cells]
                ys = [cell[1] for cell in nav.data.cells]
                nav_bounds = {
                    "x0": min(xs) * size,
                    "y0": min(ys) * size,
                    "x1": (max(xs) + 1) * size,
                    "y1": (max(ys) + 1) * size,
                }
        points = []
        if pos is not None:
            points.append((pos.x, pos.y))
        for row in monsters:
            if row.get("x") is not None and row.get("y") is not None:
                points.append((row["x"], row["y"]))
        for row in peers:
            points.append((row["x"], row["y"]))
        if self.manual_goal is not None:
            points.append(self.manual_goal)
        bounds = minimap_bounds(nav_bounds, points)
        ticket = self.tickets.available()
        return {
            "map_id": pos.map_id if pos else None,
            "map_name": MAP_NAMES.get(pos.map_id, str(pos.map_id)) if pos else "",
            "map_title": MAP_TITLES.get(pos.map_id, MAP_NAMES.get(pos.map_id, "")) if pos else "",
            "x": None if pos is None else round(pos.x, 2),
            "y": None if pos is None else round(pos.y, 2),
            "estimated": bool(pos.estimated) if pos else False,
            "health": self.self_state.get("health"),
            "max_health": self.self_state.get("maxHealth"),
            "world_ready": self.world_ready,
            "dead": self.is_dead,
            "status": self.status,
            "activity": self.activity or self.status,
            "status_line": self.status_line(),
            "kills": self.kill_count(),
            "damage": self.damage,
            "hits": self.hits,
            "hits_taken": self.hits_taken,
            "too_fast_count": self.too_fast_count,
            "kite_distance": round(self.last_kite_distance, 1),
            "potions": self.potions,
            "ticket_ready": ticket is not None,
            "wait_reason": self.wait_reason,
            "attacks_sent": self.attacks_sent,
            "tickets_received": self.tickets_received,
            "attack_errors": dict(self.attack_errors),
            "ticket_resyncs": self.ticket_resyncs,
            "quest_kills": self.quest_kills,
            "packet_types": dict(self.packet_types),
            "player_id_known": self.player_id is not None,
            "rtt_ms": self.last_rtt_ms,
            "monsters": monsters,
            "peers": peers,
            "route": list(self.chase_route or self.patrol_route or self.travel_route or self.manual_route or []),
            "manual_goal": list(self.manual_goal) if self.manual_goal else None,
            "auto_farm": self.auto_farm,
            "farm_target_level": self.farm_target_level,
            "farm_map": self.farm_map(),
            "farm_map_name": MAP_NAMES.get(self.farm_map(), ""),
            "farm_reason": self.farm_reason,
            "level_filter": self.level_filter_text,
            "elite_note": self.elite_note,
            "potion_note": self.potion_note,
            "last_damage_source": self.last_damage_source,
            "combat_options": {
                "elite_potion_min": self.elite_potion_min,
                "potion_on_player_damage": self.potion_on_player_damage,
                "prefer_elite": self.prefer_elite,
                "elite_view_range": self.elite_view_range,
                "auto_potion": self.auto_potion,
                "kite_low_potion": self.kite_low_potion,
                "overlevel_kite": self.overlevel_kite,
                "overlevel_max": self.overlevel_max,
                "cluster_kite": self.cluster_kite,
                "kite_elite_only": self.kite_elite_only,
                "max_pull": self.max_pull,
            },
            "hunt_idle": self.hunt_idle,
            "stop_after_kill": self.stop_after_kill,
            "pinned_map": self.pinned_map,
            "pinned_map_name": MAP_NAMES.get(self.pinned_map, "") if self.pinned_map else "",
            "clusters": self.clusters.payload(self.cluster_site_id) if self.cluster_mode() else [],
            "cluster_note": self.cluster_note if self.cluster_mode() else "",
            "kite_leash": round(self.kite_leash),
            "leash_resets": self.leash_resets,
            "hunt_anchor": list(self.hunt_anchor) if self.hunt_anchor else None,
            "hunt_anchor_clear": round(self.hunt_anchor_clear),
            "hunt_anchor_leash": round(self.hunt_anchor_leash),
            "zero_damage_hits": self.zero_damage_hits,
            "overlevel_note": self.overlevel_note,
            "kite_note": self.kite_note,
            "quest_map_target": self.quest_map_target,
            "map_levels": self.level_store.table(),
            "farming": self.should_auto_farm(),
            "health_potions": self.self_state.get("healthPotions"),
            "stamina_potions": self.self_state.get("staminaPotions"),
            "potion_cooldown_ms": self.self_state.get("potionCooldownMs"),
            "chat": list(self.chat_log),
            "portal": list(self.pending_portal) if self.pending_portal else None,
            "travel_phase": self.travel_phase,
            "bounds": bounds,
            "map_bounds": nav_bounds,
            "portals": self.minimap_portals(),
            "target": self.minimap_target(),
            "hunt_map": self.hunt_map,
            "elite_only": bool(self.config.hunt.elite_only),
            "auto_combat": self.auto_combat,
            "movement_paused": self.movement_paused,
            "auto_return": self.auto_return,
            "quests": format_quest_rows(self.quests),
            "quest_status": self.quest_status,
            "auto_quest": self.auto_quest,
            "task_limited": self.task_limited,
            "quest_map_auto": self.quest_map_auto,
            "daily_mode": self.daily_mode,
            "quest_map": self.quest_map_target,
            "auto_replace": self.auto_replace,
            "skip_vendetta": self.skip_vendetta,
            "pending_delivery": self.pending_delivery,
        }

    def minimap_portals(self) -> list[dict[str, Any]]:
        pos = self.position
        if pos is None:
            return []
        rows = []
        for (src, dest), (x, y) in PORTAL_CENTERS.items():
            if src == pos.map_id:
                rows.append({"x": x, "y": y, "dest": dest, "name": MAP_NAMES.get(dest, str(dest))})
        return rows

    def minimap_target(self) -> dict[str, Any] | None:
        """Current chase/attack target for the minimap line + kite ring (display only)."""
        encounter_id = self.chase_target or self.attack_target
        if not encounter_id:
            return None
        monster = self.monsters.monsters.get(encounter_id)
        if not isinstance(monster, dict) or monster.get("x") is None:
            return None
        try:
            kiting = bool(self.kite_active(encounter_id))
        except Exception:
            kiting = False
        return {
            "id": str(encounter_id)[:8],
            "x": monster.get("x"),
            "y": monster.get("y"),
            "kiting": kiting,
            "reach": attack_reach(self.character_class, "prepare"),
            "kite_min": KITE_MIN_DIST,
        }

    def monster_allowed(self, monster: dict[str, Any]) -> bool:
        level = monster_level(monster)
        if level is not None and not (self.config.hunt.level_min <= level <= self.config.hunt.level_max):
            return False
        if self.config.hunt.elite_only and str(monster.get("monsterRank", "")).upper() != "ELITE":
            return False
        limited = (self.task_limited or self.quest_first) and has_active_monster_quest(self.quests)
        if not monster_matches_active_quest(self.quests, monster, limited=limited):
            return False
        return True

    def candidates(self) -> list[tuple[float, str, dict[str, Any]]]:
        if self.position is None:
            return []
        rows = []
        self.elite_note = ""
        for encounter_id, monster in self.monsters.living_on_map(self.position.map_id):
            if self.tickets.is_invalid(encounter_id):
                continue
            if not self.monster_allowed(monster):
                continue
            if not self.elite_allowed(encounter_id, monster):
                continue
            if not self._monster_in_anchor(encounter_id, monster):
                continue
            if self._in_locked_site(encounter_id):
                continue
            if getattr(self, "_drop_skip", {}).get(encounter_id, 0) > time.monotonic():
                continue
            distance = monster_distance(self.position, monster)
            if math.isfinite(distance):
                rows.append((distance, encounter_id, monster))
        if self.auto_farm and rows and self.overlevel_kite and self.is_ranged():
            target = self.farm_target_level
            now_t = time.monotonic()
            from tabbybot.efficiency import level_band as _band
            over = []
            for item in rows:
                lvl = monster_level(item[2])
                if lvl is None or not (target < lvl <= target + self.overlevel_max):
                    continue
                if item[1] in self.kite_failed:
                    continue
                if now_t < self.overlevel_band_lock.get(_band(lvl) or 0, 0.0):
                    continue
                over.append(item)
            if over:
                over.sort(key=lambda item: (monster_level(item[2]) or 0, item[0]))
                for item in over:
                    self.overlevel_targets.add(item[1])
                self.level_filter_text = f"越級風箏：Lv{target + 10}–{target + self.overlevel_max}"
                if self.prefer_elite:
                    elites = [item for item in rows if self.is_elite(item[2]) and item[0] <= self.elite_view_range and item not in over]
                    over = elites + over
                return over
        if self.auto_farm and rows:
            target = self.farm_target_level
            rows.sort(key=lambda item: level_sort_key(monster_level(item[2]), target, item[0]))
            diffs = [abs(lvl - target) for lvl in (monster_level(item[2]) for item in rows) if lvl is not None]
            quest_active = (self.task_limited or self.quest_first) and has_active_monster_quest(self.quests)
            if diffs and not quest_active and not self.farm_hold_active():
                # 練功等級 is exact: only that Lv band (Lv30 -> 30–39), elites included only if in band.
                from tabbybot.efficiency import level_band as _band
                rows = [item for item in rows if _band(monster_level(item[2])) == _band(target)]
                self.level_filter_text = (f"只打 Lv{target}" if rows
                                          else f"附近沒有 Lv{target} 的怪，繼續巡邏尋找")
            elif diffs:
                best = min(diffs)
                # Only the closest level band: never chase much weaker monsters
                # while monsters at (or nearest to) the target level are visible.
                rows = [item for item in rows
                        if monster_level(item[2]) is None or abs(monster_level(item[2]) - target) <= best
                        or (self.prefer_elite and self.is_elite(item[2]) and item[0] <= self.elite_view_range)]
                levels = sorted({monster_level(item[2]) for item in rows if monster_level(item[2]) is not None})
                self.level_filter_text = (f"只打 Lv{target}" if best == 0
                                          else f"附近沒有 Lv{target}，改打最接近的 Lv{'/'.join(map(str, levels))}")
            else:
                self.level_filter_text = f"目標 Lv{target}（怪物沒有等級資料）"
        else:
            rows.sort(key=lambda item: item[0])
            self.level_filter_text = "不篩等級"
        if self.prefer_elite:
            elites = [item for item in rows if self.is_elite(item[2]) and item[0] <= self.elite_view_range]
            if elites:
                rest = [item for item in rows if item not in elites]
                rows = elites + rest
        return rows

    def status_line(self, debug: bool = False) -> str:
        pos = self.position
        map_name = MAP_NAMES.get(pos.map_id, str(pos.map_id)) if pos else "未知"
        hp = self.self_state.get("health")
        max_hp = self.self_state.get("maxHealth")
        hp_text = f"{hp}/{max_hp}" if hp is not None and max_hp is not None else "—"
        if self.tickets.available():
            ticket = "可攻擊"
        elif time.monotonic() < self.tickets.busy_until_at:
            ticket = "攻擊冷卻"
        else:
            ticket = "等待票據" + (f"（{self.wait_reason}）" if self.wait_reason else "")
        nearest = self.candidates()[:1]
        if nearest:
            ident = f"{nearest[0][1][:8]} " if debug else ""
            target = f"{ident}{monster_display_name(nearest[0][2])} {nearest[0][0]:.0f} 單位"
        else:
            target = "無目標"
        where = f"{map_name} ({pos.x:.0f},{pos.y:.0f})" if pos else "未定位"
        return f"{where}  HP {hp_text}  {ticket}  {target}  擊殺 {self.kill_count()}  出手 {self.attacks_sent}  {self.status}"

    def _clear_travel(self) -> None:
        self.pending_travel = None
        self.pending_portal = None
        self.travel_route = []
        self.travel_phase = None
        self.travel_sync_at = 0.0
        self.travel_sent_at = 0.0
        self.travel_last_at = 0.0

    def begin_travel_to(self, target_map: int, now: float | None = None, *, quest_id: int = 0) -> None:
        """Start walking to the next portal on the route toward target_map."""
        if self.position is None:
            return
        if self.travel_phase is not None and self.travel_final_target == target_map:
            return
        if self.travel_phase is not None:
            self._clear_travel()
        now = time.monotonic() if now is None else now
        if now < self.travel_retry_after:
            return
        if self.position.map_id == target_map:
            self._clear_travel()
            return
        if target_map in SPECIAL_ENTRY_MAPS:
            name = MAP_NAMES.get(target_map, target_map)
            self.status = f"{name} 需要特殊道具，請先在官方客戶端進入該地圖。"
            log.info("%s", self.status)
            self.travel_retry_after = now + 10
            return
        hops = find_travel_hops(self.position.map_id, target_map)
        if not hops:
            self.status = (
                f"找不到通往 {MAP_NAMES.get(target_map, target_map)} 的入口路線。"
            )
            log.info("%s", self.status)
            self.travel_retry_after = now + 10
            return
        if any(hop in SPECIAL_ENTRY_MAPS for hop in hops):
            self.status = "這條路線需要經過需要特殊道具的地圖，請先在官方客戶端處理。"
            log.info("%s", self.status)
            self.travel_retry_after = now + 10
            return
        next_hop = hops[0]
        portal = PORTAL_CENTERS.get((self.position.map_id, next_hop))
        self.stop_routes()
        self.pending_travel = next_hop
        self.pending_portal = portal
        self.travel_final_target = target_map
        self.travel_quest_id = int(quest_id or 0)
        self.travel_attempt += 1
        self.travel_last_at = now
        hop_name = MAP_NAMES.get(next_hop, next_hop)
        if len(hops) > 1:
            via = " → ".join(MAP_NAMES.get(item, str(item)) for item in hops)
            log.info("前往 %s，路線：%s；先進入 %s。", MAP_NAMES.get(target_map, target_map), via, hop_name)
        if portal is None:
            self.travel_phase = "sync"
            self.travel_sync_at = now - TRAVEL_SYNC_WAIT_SEC
            self.status = f"目前地圖沒有通往 {hop_name} 的入口座標；改由目前位置請求切圖。"
            log.info("%s", self.status)
            return
        start = (float(self.position.x), float(self.position.y))
        if math.dist(start, portal) <= PORTAL_ARRIVE_DISTANCE:
            self.travel_route = []
            self.travel_phase = "sync"
            self.travel_sync_at = now
            self.status = f"已在 {hop_name} 入口，補發位置同步並等待確認…"
            log.info("已在入口 (%.0f, %.0f)，先同步位置再請求進入 %s。", portal[0], portal[1], hop_name)
            return
        if has_navigation(self.position.map_id):
            path = find_map_path(self.position.map_id, start, portal)
            if not path:
                self.status = "無法從目前位置找到通往入口的路；已取消切圖。"
                log.info("%s", self.status)
                self._clear_travel()
                self.travel_retry_after = now + 10
                return
            self.travel_route = list(path)
        else:
            self.travel_route = self._learned_route(portal) or [portal]
        self._travel_stuck_anchor = None
        self._nav_replan = False
        self.travel_phase = "walk"
        self.status = (
            f"正沿 {len(self.travel_route)} 段路線前往 {hop_name} 入口 "
            f"({portal[0]:.0f}, {portal[1]:.0f})"
        )
        self.activity = "前往地圖入口"
        log.info("%s", self.status)

    def advance_travel(self, now: float) -> dict[str, Any] | None:
        if self.position is None or self.travel_phase is None:
            return None
        if self.travel_phase == "wait_result":
            if self.travel_sent_at and now - self.travel_sent_at >= TRAVEL_RESULT_TIMEOUT_SEC:
                self.status = "6 秒內沒有收到切圖結果，已停止等待以免卡住。"
                log.info("%s", self.status)
                self._clear_travel()
                self.travel_retry_after = now + TRAVEL_RETRY_SEC
            return None
        if self.travel_phase == "sync":
            if now - self.travel_sync_at < TRAVEL_SYNC_WAIT_SEC:
                if now - self.travel_sync_at < 0.0001:
                    packet = build_sync(
                        movement_revision=self.position.revision,
                        map_id=self.position.map_id,
                        x=self.position.x,
                        y=self.position.y,
                        animation="idle",
                    )
                    self.note_sync_sent(packet, now)
                    return packet
                return None
            destination = self.pending_travel
            if destination is None:
                self._clear_travel()
                return None
            self.travel_phase = "wait_result"
            self.travel_sent_at = now
            name = MAP_NAMES.get(destination, destination)
            self.status = f"正在請求進入 {name}…"
            log.info("已到入口並完成位置同步，正在請求進入 %s。", name)
            return build_travel(
                int(destination),
                self.position.revision,
                quest_id=int(self.travel_quest_id or 0),
                use_ticket=False,
            )
        if self.travel_phase != "walk":
            return None
        self.travel_last_at = now
        while self.travel_route and math.dist(
            (float(self.position.x), float(self.position.y)), self.travel_route[0]
        ) <= PORTAL_ARRIVE_DISTANCE:
            self.travel_route.pop(0)
        if not self.travel_route:
            self.travel_phase = "sync"
            self.travel_sync_at = now
            self.status = "已到入口座標，補發位置同步並等待確認…"
            log.info("%s", self.status)
            packet = build_sync(
                movement_revision=self.position.revision,
                map_id=self.position.map_id,
                x=self.position.x,
                y=self.position.y,
                animation="idle",
            )
            self.note_sync_sent(packet, now)
            return packet
        if self._nav_replan and self.pending_portal and not has_navigation(self.position.map_id):
            self._nav_replan = False
            better = self._learned_route(self.pending_portal)
            if better:
                self.travel_route = better
            else:
                self.abandon_travel(now, "已知的障礙把路堵死了")
                return None
        if (self._travel_stuck_anchor is not None
                and math.dist((self.position.x, self.position.y), self._travel_stuck_anchor) > TRAVEL_STUCK_RADIUS):
            self._travel_stuck_anchor = None  # made real progress
        target = self.travel_route[0]
        point = self._capped_step(target, now)
        if point is None:
            return None
        remaining = math.dist((self.position.x, self.position.y), target)
        # Same per-packet cap as chase (speed/10), on top of the shared limiter.
        dx = point[0] - self.position.x
        dy = point[1] - self.position.y
        travelled = math.hypot(dx, dy)
        cap = MOVEMENT_SPEED / 10.0
        if travelled > cap + 1e-6 and travelled > 0:
            scale = cap / travelled
            point = (round(self.position.x + dx * scale, 2), round(self.position.y + dy * scale, 2))
            travelled = cap
        packet = self._emit_move(point[0], point[1], now, flip=dx < 0)
        if remaining - travelled < 1:
            self.status = "即將抵達入口"
        return packet

    def plan_travel(self) -> dict[str, Any] | None:
        """Compatibility wrapper used by tests and older call sites."""
        if self.position is None:
            return None
        clock = time.monotonic()
        if self.travel_phase is None:
            self.begin_travel_to(self.hunt_map, clock)
        return self.advance_travel(clock)

    def plan_respawn(self) -> dict[str, Any] | None:
        if not self.is_dead or self._respawn_sent:
            return None
        self._respawn_sent = True
        self.stop_routes()
        return build_respawn()

    def _advance_route(
        self,
        route: list[tuple[float, float]],
        arrive: float,
        *,
        dt: float,
        max_step: float,
        speed_factor: float,
        now: float | None = None,
    ) -> tuple[list[tuple[float, float]], dict[str, Any] | None]:
        if self.position is None or not route:
            return route, None
        while route and math.dist((self.position.x, self.position.y), route[0]) <= arrive:
            route = route[1:]
        if not route:
            return route, None
        self._last_move_goal = route[-1]
        if self._nav_replan and not has_navigation(self.position.map_id):
            self._nav_replan = False
            better = self._learned_route(route[-1])
            if better:
                route = better
        elif self._nav_replan:
            self._nav_replan = False
            if self.retreat_blocked(time.monotonic()):
                # Official nav data says walkable but the server keeps refusing: give up this route.
                if self.chase_target:
                    self.tickets.lock_invalid(self.chase_target, duration=30.0)
                    log.info("追擊路線被牆擋住，30 秒內改打別隻")
                    self.chase_target = None
                else:
                    self.patrol_index += 1
                return [], None
        target = route[0]
        clock = time.monotonic() if now is None else now
        # Official-style per-tick cap still applies, on top of the shared 90% limiter.
        point = self._capped_step(target, clock, speed_factor=speed_factor)
        if point is None:
            return route, None
        # Honour caller max_step (patrol 7, chase speed/10) without bypassing the limiter.
        dx = point[0] - self.position.x
        dy = point[1] - self.position.y
        travelled = math.hypot(dx, dy)
        if travelled > max_step + 1e-6 and travelled > 0:
            scale = max_step / travelled
            point = (round(self.position.x + dx * scale, 2), round(self.position.y + dy * scale, 2))
        packet = self._emit_move(point[0], point[1], clock, flip=dx < 0)
        return route, packet

    def plan_patrol_step(self, now: float) -> dict[str, Any] | None:
        if self.position is None:
            return None
        if not self.patrol_route:
            spot_path = self._patrol_to_spot(now)
            if spot_path:
                self.patrol_route = spot_path
                self.patrol_last_at = now
        if not self.patrol_route:
            if not has_navigation(self.position.map_id):
                self.status = "此地圖沒有導航資料，改為原地等待怪物。"
                return None
            if self._patrol_note != ("sweep", self.position.map_id, self.farm_target_level):
                self._patrol_note = ("sweep", self.position.map_id, self.farm_target_level)
                log.info("巡邏：%s 還沒有 Lv%s 的出怪紀錄，沿路線掃圖找怪",
                         MAP_NAMES.get(self.position.map_id, self.position.map_id), self.farm_target_level)
            path = find_sweep_path(self.position.map_id, (self.position.x, self.position.y), self.patrol_index)
            self.patrol_index += 1
            if not path:
                self.status = "找不到巡邏路徑"
                return None
            self.patrol_route = path
            self.status = "巡邏搜怪中"
            self.patrol_last_at = now
        dt = max(0.0, now - (self.patrol_last_at or now))
        self.patrol_last_at = now
        self.patrol_route, packet = self._advance_route(
            self.patrol_route,
            WAYPOINT_ARRIVE_PATROL,
            dt=dt,
            max_step=PATROL_MAX_STEP,
            speed_factor=PATROL_SPEED_FACTOR,
        )
        return packet

    def _patrol_to_spot(self, now: float) -> list[tuple[float, float]] | None:
        """Head to the nearest recorded spawn spot of the chosen level that we haven't checked lately."""
        if self.position is None or not self.auto_farm:
            return None
        map_id = self.position.map_id
        spots = self.level_store.spots_for(map_id, self.farm_target_level)
        if not spots:
            return None
        here = (self.position.x, self.position.y)
        fresh = [s for s in spots if now - self._spot_visits.get((map_id, round(s[0]), round(s[1])), -1e9) >= SPOT_REVISIT_SEC
                 and math.dist(here, (s[0], s[1])) > WAYPOINT_ARRIVE_PATROL * 2]
        if not fresh:
            return None
        # Busy spots first, then distance.
        spot = min(fresh, key=lambda s: math.dist(here, (s[0], s[1])) / (1.0 + min(s[2], 20) / 5.0))
        self._spot_visits[(map_id, round(spot[0]), round(spot[1]))] = now
        goal = (spot[0], spot[1])
        if has_navigation(map_id):
            path = find_map_path(map_id, here, goal)
        else:
            path = self._learned_route(goal) or [goal]
        if not path:
            return None
        self.status = f"巡邏前往 Lv{self.farm_target_level} 出怪點"
        log.info("巡邏前往 (%.0f, %.0f)：之前在這裡看過 %s 次 Lv%s 的怪（距離 %.0f）",
                 spot[0], spot[1], spot[2], self.farm_target_level, math.dist(here, goal))
        return list(path)

    def plan_chase_step(self, now: float) -> dict[str, Any] | None:
        if self.position is None or not self.chase_target:
            return None
        monster = self.monsters.monsters.get(self.chase_target)
        if not monster or int(monster.get("health", 0)) <= 0:
            self.chase_target = None
            self.chase_route = []
            return None
        if monster_distance(self.position, monster) <= attack_reach(self.character_class, "chase_stop"):
            self.chase_route = []
            self.chase_target = None
            return None
        if not self.chase_route:
            goal = (float(monster["x"]), float(monster["y"]))
            start = (self.position.x, self.position.y)
            if has_navigation(self.position.map_id):
                path = find_map_path(self.position.map_id, start, goal)
            else:
                path = [goal]
            remain = attack_reach(self.character_class, "chase_stop") - 5
            self.chase_route = _trim_approach(start, path, remain) if path else [goal]
            self.status = f"追擊 {monster_display_name(monster)}"
        dt = min(0.1, max(0.0, now - (self.chase_last_at or 0.0))) or MOVE_TICK_SEC
        self.chase_last_at = now
        self.chase_route, packet = self._advance_route(
            self.chase_route,
            WAYPOINT_ARRIVE_CHASE,
            dt=dt,
            max_step=MOVEMENT_SPEED / 10.0,
            now=now,
            speed_factor=1.0,
        )
        return packet

    def plan_attack(self, encounter_id: str, now: float) -> list[tuple[float, dict[str, Any]]]:
        """Return timed outgoing packets: (delay_sec, packet). First is immediate sync."""
        if self.position is None:
            return []
        monster = self.monsters.monsters.get(encounter_id)
        ticket = self.tickets.available(now)
        if monster is None or ticket is None:
            return []
        distance = monster_distance(self.position, monster)
        if distance > attack_reach(self.character_class, "prepare"):
            return []
        mx, my = float(monster["x"]), float(monster["y"])
        elevation = attack_elevation(self.position.x, self.position.y, mx, my)
        delay_ms, anim, speed = attack_timing(self.character_class, self.attack_speed_percent, elevation)
        busy = busy_until(anim, speed, now)
        self.tickets.consume(ticket.id, busy)
        sync = build_sync(
            movement_revision=self.position.revision,
            map_id=self.position.map_id,
            x=self.position.x,
            y=self.position.y,
            animation="attack",
            attack_critical=ticket.critical,
            flip=mx < self.position.x,
            attack_elevation=elevation,
        )
        attack = build_monster_attack(encounter_id, ticket.id)
        if not self._engage or self._engage.get("id") != encounter_id:
            self._engage = {
                "id": encounter_id, "at": now, "last_hit_at": now,
                "anchor": (float(monster.get("x", self.position.x)), float(monster.get("y", self.position.y))),
                "name": monster_display_name(monster),
                "hp": self._hp_value(), "potions": self.health_potion_count(),
                "level": monster_level(monster), "map": self.position.map_id,
            }
        self.attack_target = encounter_id
        self.attacks_sent += 1
        self.last_attack_at = now
        self.ticket_wait_since = None
        self._clear_retreat()
        if self._kite_burst_left <= 0:
            self._kite_burst_left = KITE_BURST_SHOTS
        self._kite_burst_left = max(0, self._kite_burst_left - 1)
        self.wait_reason = ""
        idle = build_sync(
            movement_revision=self.position.revision,
            map_id=self.position.map_id,
            x=self.position.x,
            y=self.position.y,
            animation="idle",
        )
        self.note_sync_sent(sync, now)
        self.status = f"攻擊 {monster_display_name(monster)}"
        self.activity = "自動戰鬥"
        # After the projectile goes out, stutter-step is allowed even if the
        # attack animation has not finished. Skip the trailing idle sync when
        # we will use that window to move, so it cannot snap us back.
        self._shot_move_at = now + delay_ms / 1000.0 + 0.05
        packets = [(0.0, sync), (delay_ms / 1000.0, attack)]
        will_step = (
            self.character_class == "ARCHER"
            and self.kite_active(encounter_id)
            and distance < KITE_SHOOT_MIN + 50.0
        )
        if not will_step:
            packets.append((anim, idle))
        return packets

    def plan_idle_sync(self, now: float, force: bool = False) -> dict[str, Any] | None:
        if self.position is None:
            return None
        if not force and now - self.last_sync_at < IDLE_SYNC_INTERVAL_SEC:
            return None
        if self.chase_target or self.patrol_route or self.pending_travel:
            return None
        packet = build_sync(
            movement_revision=self.position.revision,
            map_id=self.position.map_id,
            x=self.position.x,
            y=self.position.y,
            animation="idle",
        )
        self.note_sync_sent(packet, now)
        return packet


    def update_quests(self, quests: list[dict[str, Any]], *, request_next: bool = True) -> None:
        rows = [quest for quest in quests if isinstance(quest, dict)]
        previous = self._quest_completion_state
        current = {}
        for quest in rows:
            ident = quest_id(quest)
            if ident is None:
                continue
            done = bool(quest.get("completed"))
            current[str(ident)] = done
            if done and not previous.get(str(ident), False):
                log.info("任務完成：%s（地圖 %s）", quest_title(quest), MAP_NAMES.get(quest_map_id(quest) or 0, quest_map_id(quest) or "—"))
        self._quest_completion_state.update(current)
        for quest in rows:
            ident = quest_id(quest)
            if ident is None or not is_monster_quest(quest):
                continue
            try:
                value = int(quest.get("progress", 0) or 0)
            except (TypeError, ValueError):
                continue
            before = self._quest_progress.get(ident)
            if before is not None and value > before:
                self.quest_kills += value - before
            self._quest_progress[ident] = value
        self.quests = rows
        self.quest_status = quest_summary(rows)
        self.refresh_quest_map_target()
        if request_next and self.auto_quest and not has_active_monster_quest(rows):
            self._quests_dirty = True

    def refresh_quest_map_target(self) -> None:
        if not (self.quest_map_auto or self.quest_first):
            self.quest_map_target = None
            self.quest_map_quest_id = 0
            return
        target, ident = active_quest_target(self.quests)
        self.quest_map_target = target
        self.quest_map_quest_id = ident


    def record_monster_levels(self) -> None:
        for monster in self.monsters.monsters.values():
            if not isinstance(monster, dict):
                continue
            try:
                map_id = int(monster.get("mapId", self.position.map_id if self.position else 0) or 0)
            except (TypeError, ValueError):
                continue
            encounter_id = str(monster.get("encounterId", "") or "")
            if encounter_id:
                try:
                    mx, my = float(monster.get("x")), float(monster.get("y"))
                except (TypeError, ValueError):
                    mx = my = None
                self.level_store.observe(encounter_id, map_id, monster_level(monster), mx, my)
            else:
                remember_monster_level(self.level_catalog, map_id, monster_level(monster))
        self.level_store.save(time.monotonic())

    def _ingest_chat(self, packet: dict[str, Any]) -> None:
        kind = str(packet.get("type") or "")
        if kind == "chat_ack":
            code = packet.get("code") or packet.get("status") or packet.get("message")
            if code:
                self.chat_log.append({"channel": "系統", "sender": "系統", "text": "訊息狀態：%s" % code, "self": False})
            return
        rows = packet.get("messages")
        if isinstance(packet.get("message"), dict):
            rows = [packet.get("message")]
        elif isinstance(packet.get("message"), str) and kind in {"chat", "scoped_chat"}:
            rows = [{"body": packet.get("message"), "sender": packet.get("sender") or packet.get("Sender")}]
        if not isinstance(rows, list):
            return
        channel = str(packet.get("channel") or ("世界" if kind == "chat" else "附近"))
        for item in rows:
            if not isinstance(item, dict):
                continue
            sender = item.get("Sender") or item.get("sender") or item.get("playerName") or "系統"
            body = item.get("Body") or item.get("message") or item.get("body") or item.get("text") or ""
            if body:
                self.chat_log.append({
                    "channel": channel,
                    "sender": str(sender),
                    "text": str(body),
                    "self": False,
                })

    def should_auto_farm(self) -> bool:
        if not self.auto_farm:
            return False
        if has_active_monster_quest(self.quests):
            return False
        return bool(self.daily_mode or self.auto_quest or self.pending_delivery)

    def farm_map(self) -> int:
        current = self.position.map_id if self.position else None
        if self.pinned_map is not None:
            if current is not None and self.farm_hold_active():
                return current
            reason = "你指定的地圖，不會自動改去別張"
            if reason != self.farm_reason:
                self.farm_reason = reason
                log.info("練功地圖：%s（%s）", MAP_NAMES.get(self.pinned_map, self.pinned_map), reason)
            return self.pinned_map
        if current is not None and self.farm_hold_active():
            return current
        chosen, reason = choose_farm_map_with_reason(self.farm_target_level, self.level_catalog, current)
        if not is_farmable_map(chosen):
            chosen, reason = DEFAULT_FARM_MAP, "預設北部雪地"
        if reason != self.farm_reason:
            self.farm_reason = reason
            log.info("練功地圖：%s（%s）", MAP_NAMES.get(chosen, chosen), reason)
        return chosen

    def queue_user_packet(self, packet: dict[str, Any]) -> None:
        if not isinstance(packet, dict) or not packet.get("action"):
            raise ValueError("無效的操作")
        self.user_packets.append(dict(packet))

    def pop_user_packets(self) -> list[dict[str, Any]]:
        rows = []
        while self.user_packets:
            rows.append(self.user_packets.popleft())
        return rows

    def begin_manual_move(self, x: float, y: float) -> None:
        if self.position is None or not self.world_ready:
            raise RuntimeError("尚未進入遊戲世界，不能移動。")
        goal = (float(x), float(y))
        start = (self.position.x, self.position.y)
        self.movement_paused = False
        self.manual_goal = goal
        if has_navigation(self.position.map_id):
            path = find_map_path(self.position.map_id, start, goal)
            self.manual_route = path or [goal]
        else:
            self.manual_route = [goal]
        self.manual_last_at = time.monotonic()
        self.chase_target = None
        self.chase_route = []
        self.patrol_route = []
        self.status = "前往手動指定座標（%.0f, %.0f）" % goal
        self.activity = self.status
        log.info("%s", self.status)

    def clear_manual_move(self) -> None:
        self.manual_goal = None
        self.manual_route = []
        self.manual_last_at = 0.0

    def plan_manual_step(self, now: float) -> dict[str, Any] | None:
        if self.position is None or self.manual_goal is None:
            return None
        if math.hypot(self.position.x - self.manual_goal[0], self.position.y - self.manual_goal[1]) <= WAYPOINT_ARRIVE_PATROL:
            self.status = "已到達指定座標"
            self.activity = self.status
            self.clear_manual_move()
            return None
        if not self.manual_route:
            if has_navigation(self.position.map_id):
                path = find_map_path(self.position.map_id, (self.position.x, self.position.y), self.manual_goal)
                self.manual_route = path or [self.manual_goal]
            else:
                self.manual_route = [self.manual_goal]
        dt = min(0.1, max(0.0, now - (self.manual_last_at or now)))
        self.manual_last_at = now
        self.manual_route, packet = self._advance_route(
            self.manual_route,
            WAYPOINT_ARRIVE_PATROL,
            dt=dt,
            max_step=MOVEMENT_SPEED / 10.0,
            speed_factor=1.0,
        )
        if not self.manual_route:
            self.status = "已到達指定座標"
            self.activity = self.status
            self.clear_manual_move()
        return packet

    def request_use_consumable(self, kind: str) -> str:
        """Queue a user-initiated consumable packet. Returns a status message."""
        if not self.world_ready or self.position is None:
            raise RuntimeError("尚未進入遊戲世界。")
        if self.is_dead:
            raise RuntimeError("角色已陣亡，無法使用道具。")
        action_map = {
            "health": ("useHealthPotion", "healthPotions", "生命藥水"),
            "stamina": ("useStaminaPotion", "staminaPotions", "體力恢復藥水"),
        }
        if kind not in action_map:
            raise RuntimeError("不支援這種道具。")
        action, field, label = action_map[kind]
        try:
            count = int(self.self_state.get(field, 0) or 0)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            raise RuntimeError("沒有可使用的%s。" % label)
        if kind == "health":
            try:
                cooldown = int(self.self_state.get("potionCooldownMs", 0) or 0)
            except (TypeError, ValueError):
                cooldown = 0
            if cooldown > 0:
                raise RuntimeError("生命藥水還在冷卻中。")
        self.queue_user_packet({"action": action})
        message = "已送出使用%s的請求，等待伺服器確認。" % label
        self.status = message
        log.info("%s", message)
        return message

    def request_chat(self, message: str, channel: str = "world") -> str:
        text = (message or "").strip()
        if not text:
            raise RuntimeError("請先輸入要送出的訊息。")
        if len(text) > 200:
            raise RuntimeError("訊息太長，請縮短到 200 字以內。")
        if channel != "world":
            raise RuntimeError("目前只支援世界頻道。")
        if not self.world_ready:
            raise RuntimeError("尚未進入遊戲世界，不能送出聊天。")
        self.queue_user_packet({"action": "chat", "channel": "world", "message": text})
        self.chat_log.append({"channel": "世界", "sender": "我", "text": text, "self": True})
        log.info("已送出世界聊天訊息（%s 字）。", len(text))
        return "已送出。"

    def desired_map(self) -> tuple[int | None, int]:
        self.refresh_quest_map_target()
        if (self.quest_map_auto or self.quest_first) and self.quest_map_target:
            return self.quest_map_target, int(self.quest_map_quest_id or 0)
        if has_active_monster_quest(self.quests) and self.quest_map_auto and not self.quest_map_target:
            return None, 0
        if self.pinned_map is not None:
            current = self.position.map_id if self.position else None
            if current is not None and self.farm_hold_active() and current != self.pinned_map:
                return current, 0
            return self.pinned_map, 0
        if self.should_auto_farm():
            return self.farm_map(), 0
        if self.task_limited and not has_active_monster_quest(self.quests):
            return None, 0
        if (self.daily_mode or self.quest_map_auto) and has_active_monster_quest(self.quests) and not self.quest_map_target:
            return None, 0
        if self.auto_return:
            return self.hunt_map, 0
        return None, 0

    def apply_quest_results(self) -> None:
        refresh = self._refresh_result
        if refresh is not None:
            self._refresh_result = None
            self.update_quests(refresh, request_next=True)
        result = self._accept_result
        if result is None:
            return
        self._accept_result = None
        self.quest_accept_inflight = False
        if result.get("quests"):
            self.update_quests(result["quests"], request_next=False)
        if result.get("limit"):
            self.quest_accept_blocked_date = date.today()
            detail = result["limit"]
            self.quest_status = "今日任務領取已滿。" + (f" 伺服器：{detail}" if detail else "")
            log.info("%s", self.quest_status)
            return
        if result.get("error"):
            log.info("自動接取任務未完成：%s", result["error"])
            return
        if result.get("delivery_prompt"):
            self.pending_delivery = result["delivery_prompt"]
            self.quest_status = f"「{self.pending_delivery.get('title', '任務')}」需要交付道具或裝備，已暫停等待你確認。"
            self.status = self.quest_status
            log.info("%s", self.quest_status)
            return
        if result.get("replaced"):
            titles = "、".join(quest_title(row) for row in result["replaced"][:5])
            log.info("已略過尚未接取的交付任務並改接打怪任務（遊戲沒有更換接口）：%s", titles)
        accepted = result.get("accepted")
        if accepted:
            self._quest_idle_date = None
            log.info("已自動接取打怪任務：%s（%s）", quest_title(accepted), MAP_NAMES.get(quest_map_id(accepted) or 0, quest_map_id(accepted) or "未指定地圖"))
            self.refresh_quest_map_target()
            return
        if result.get("replaced_done") or result.get("none"):
            if result.get("replaced_done") or result.get("replaced"):
                self.quest_status = "打怪任務已處理完；已略過尚未接取的交付任務（遊戲沒有更換或放棄接口）。"
            else:
                self.quest_status = "今日沒有可自動接取的打怪任務了。"
            today = date.today()
            self._quest_idle_date = today
            self._quest_idle_at = time.monotonic()
            if self._quest_idle_logged != today:
                self._quest_idle_logged = today
                log.info("%s 改直接打 練功等級 Lv%s；之後每 15 分鐘或換日再查一次任務。",
                         self.quest_status, self.farm_target_level)

    def quest_idle(self, now: float) -> bool:
        """True while today's quests are exhausted and the 15-minute re-check isn't due yet."""
        if self._quest_idle_date is None:
            return False
        if self._quest_idle_date != date.today():
            self._quest_idle_date = None  # new day: check right away
            return False
        return now - self._quest_idle_at < QUEST_IDLE_RECHECK_SEC

    def maybe_refresh_quests(self, now: float) -> None:
        if self.quest_refresh_inflight or not self._claim_token:
            return
        interval = QUEST_IDLE_RECHECK_SEC if self.quest_idle(now) else QUEST_REFRESH_INTERVAL_SEC
        due = self._quests_dirty or (self.auto_quest and now - self.quest_refresh_at >= interval)
        if not due or now - self.quest_refresh_at < 2.0:
            return
        self.quest_refresh_inflight = True
        self.quest_refresh_at = now
        self._quests_dirty = False
        token = self._claim_token
        config = self.config

        def worker() -> None:
            try:
                self._refresh_result = fetch_quests(config, token)
            except Exception as exc:
                log.info("重新整理任務失敗：%s：%s", type(exc).__name__, exc)
                # Don't hammer the quest API (or treat this as a session failure).
                self.quest_refresh_at = time.monotonic() + 90.0
            finally:
                self.quest_refresh_inflight = False

        threading.Thread(target=worker, daemon=True, name="tabbybot-quests").start()

    def maybe_accept_quest(self, now: float) -> None:
        if not self.auto_quest or not self.world_ready or not self._claim_token:
            return
        today = date.today()
        if self.quest_accept_blocked_date is not None and self.quest_accept_blocked_date != today:
            self.quest_accept_blocked_date = None
        if self.quest_accept_blocked_date == today:
            return
        if self.pending_delivery:
            return
        if has_active_monster_quest(self.quests):
            return
        if self.quest_idle(now):
            return
        if self.quest_accept_inflight or now - self.quest_accept_checked_at < QUEST_ACCEPT_INTERVAL_SEC:
            return
        self.quest_accept_inflight = True
        self.quest_accept_checked_at = now
        token = self._claim_token
        config = self.config
        current_map = self.position.map_id if self.position else None
        auto_replace = self.auto_replace
        skip_vendetta = self.skip_vendetta
        skipped_ids = set(self.skipped_quest_ids)

        def worker() -> None:
            result: dict[str, Any] = {}
            try:
                quests = fetch_quests(config, token)
                result["quests"] = quests
                if skip_vendetta:
                    from tabbybot.quests import objective_type as _ot, quest_id as _qid
                    for _q in quests:
                        if str(_ot(_q) or "").upper() == "VENDETTA" and _qid(_q) is not None:
                            skipped_ids.add(int(_qid(_q)))
                            self.skipped_quest_ids.add(int(_qid(_q)))
                step = next_daily_step(quests, current_map, auto_replace=auto_replace, skipped_ids=skipped_ids)
                kind = step.get("kind")
                if kind == "hunt":
                    result["already_active"] = True
                elif kind == "hunt_accept":
                    chosen = step["quest"]
                    ident = quest_id(chosen)
                    if ident is None:
                        result["error"] = "任務沒有編號"
                    else:
                        accepted = accept_quest(config, token, ident)
                        result["accepted"] = chosen
                        result["replaced"] = step.get("replaced") or []
                        refreshed = extract_quests(accepted)
                        result["quests"] = refreshed or fetch_quests(config, token)
                elif kind == "delivery":
                    quest = step["quest"]
                    preview = None
                    error = ""
                    ident = quest_id(quest)
                    if ident is not None:
                        try:
                            preview = fetch_quest_delivery(config, token, ident)
                        except QuestError as exc:
                            error = str(exc)
                    result["delivery_prompt"] = format_delivery_prompt(
                        quest,
                        preview=preview,
                        already_accepted=bool(step.get("already_accepted")),
                        error=error if preview is None else "",
                    )
                elif kind == "replaced_done":
                    result["replaced_done"] = True
                    result["replaced"] = step.get("replaced") or []
                else:
                    result["none"] = True
                    result["skipped"] = skipped_available(quests)
            except QuestLimitReached as exc:
                result["limit"] = str(exc)
            except (QuestError, Exception) as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"
            self._accept_result = result

        threading.Thread(target=worker, daemon=True, name="tabbybot-quest-accept").start()

    def apply_quest_options(self, options: dict[str, Any]) -> None:
        if "auto_quest" in options:
            self.auto_quest = bool(options["auto_quest"])
        if "task_limited" in options:
            self.task_limited = bool(options["task_limited"])
        if "quest_map_auto" in options:
            self.quest_map_auto = bool(options["quest_map_auto"])
        if "daily_mode" in options:
            self.daily_mode = bool(options["daily_mode"])
        if "auto_replace" in options:
            self.auto_replace = bool(options["auto_replace"])
        if "skip_vendetta" in options:
            self.skip_vendetta = bool(options["skip_vendetta"])
        if "auto_farm" in options:
            self.auto_farm = bool(options["auto_farm"])
        if "farm_target_level" in options:
            try:
                level = int(options["farm_target_level"])
            except (TypeError, ValueError):
                level = DEFAULT_FARM_LEVEL
            self.farm_target_level = max(1, min(9999, level))
        if self.auto_quest or self.daily_mode:
            self._quests_dirty = True
        self.refresh_quest_map_target()

    def next_combat_action(self, now: float) -> tuple[str, Any]:
        if not self.world_ready or self.position is None or self.is_dead:
            return ("idle", None)
        self.apply_quest_results()
        self.maybe_refresh_quests(now)
        self.maybe_accept_quest(now)
        if self.pending_delivery and not self.should_auto_farm():
            title = self.pending_delivery.get("title") or "任務"
            self.status = f"「{title}」需要交付道具或裝備，已暫停等待你確認。"
            self.activity = self.status
            return ("idle", None)
        if self.pending_delivery and self.should_auto_farm():
            title = self.pending_delivery.get("title") or "任務"
            self.status = farm_status_text(self.farm_map(), self.farm_target_level, waiting_delivery=True, title=title)
            self.activity = self.status
        if self.stop_after_kill or self.hunt_idle:
            return self._paused_combat_action(now)
        if self.manual_goal is not None:
            if self.movement_paused:
                return ("wait", None)
            if self.travel_phase is not None:
                pass
            else:
                packet = self.plan_manual_step(now)
                if packet is None:
                    return ("wait", None)
                return ("manual", packet)
        if self.travel_phase is not None:
            if self.movement_paused:
                return ("wait", None)
            packet = self.advance_travel(now)
            if packet is None:
                return ("wait", None)
            kind = "travel" if packet.get("action") == "travel" else "portal"
            return (kind, packet)
        target_map, travel_quest_id = self.desired_map()
        if target_map is not None and self.position.map_id != target_map:
            if self.movement_paused:
                return ("wait", None)
            if not self.auto_return and not (self.quest_map_auto and travel_quest_id) and not self.should_auto_farm():
                return ("wait", None)
            self.begin_travel_to(target_map, now, quest_id=travel_quest_id)
            packet = self.advance_travel(now) if self.travel_phase is not None else None
            if packet is None:
                return ("wait", None)
            kind = "travel" if packet.get("action") == "travel" else "portal"
            return (kind, packet)
        if self.pending_travel:
            return ("wait", None)
        if self.task_limited and not has_active_monster_quest(self.quests) and not self.should_auto_farm():
            if self.quest_status and "領取已滿" not in self.quest_status and "沒有可自動" not in self.quest_status:
                self.status = "任務有限模式：等待接取下一個打怪任務"
            else:
                self.status = self.quest_status or "任務有限模式：目前沒有進行中的打怪任務"
            self.activity = self.status
            return ("idle", None)
        if self.should_auto_farm() and self.position is not None:
            target = self.farm_map()
            if self.position.map_id == target and not self.pending_delivery:
                self.activity = farm_status_text(target, self.farm_target_level)
        if self.quest_map_auto and has_active_monster_quest(self.quests) and not self.quest_map_target:
            self.status = "進行中的任務需要特殊道具入圖，已略過自動進圖。"
            self.activity = self.status
            return ("idle", None)
        if not self.auto_combat:
            return ("idle", None)
        rest = self.plan_rest(now)
        if rest is not None:
            return rest
        if self.position is not None:
            self.apply_hunt_anchor(self.position.map_id)
        self.check_overlevel_abort(now)
        candidates = self.candidates()
        if self.cluster_mode():
            action = self.plan_cluster(candidates, now)
            if action is not None:
                return action
        reach = attack_reach(self.character_class, "prepare")
        nearby = [row for row in candidates if row[0] <= reach]
        if (self._anchor_active() and self.position is not None
                and not self._in_anchor_leash(self.position.x, self.position.y)
                and not nearby):
            packet = self.plan_anchor_return(now)
            if packet is not None:
                return ("chase", packet)
        if nearby:
            self.patrol_route = []
            encounter_id = nearby[0][1]
            # Keep hitting the same monster until it dies or leaves reach so
            # damage accumulates instead of being spread over many targets.
            committed = self.attack_target or self.chase_target
            if committed and any(row[1] == committed for row in nearby):
                encounter_id = committed
            distance = next(row[0] for row in nearby if row[1] == encounter_id)
            mon = self.monsters.monsters.get(encounter_id) or {}
            # After an invalid_target skip, shoot the nearest valid mob right
            # away instead of only kiting (which can jitter between packed mobs
            # and never fire).
            if self._retarget_now:
                self._retarget_now = False
                self.attack_target = encounter_id
                planned = self.plan_attack(encounter_id, now)
                if planned:
                    self.chase_target = None
                    self.chase_route = []
                    return ("attack", planned)
            if self.kite_active(encounter_id):
                _pred, must_move, may_shoot = self.kite_spacing(encounter_id, mon, distance, now)
                if must_move:
                    if self._can_stutter_move(now):
                        self.attack_target = encounter_id
                        step = self.plan_kite_step(encounter_id, distance, now)
                        if step is not None:
                            self.activity = "風箏精英" if self.is_elite(mon) else "風箏"
                            return ("chase", step)
                    return self._wait_for_ticket(encounter_id, nearby, now, approach=False)
                if not may_shoot:
                    return self._wait_for_ticket(encounter_id, nearby, now, approach=False)
                self._kite_contacts = 0
            planned = self.plan_attack(encounter_id, now)
            if planned:
                self.chase_target = None
                self.chase_route = []
                return ("attack", planned)
            return self._wait_for_ticket(encounter_id, nearby, now)
        if self.movement_paused:
            return ("wait", None)
        if candidates:
            self.patrol_route = []
            committed = self.attack_target or self.chase_target
            if committed and any(row[1] == committed for row in candidates):
                if self.chase_target != committed:
                    self.chase_target = committed
                    self.chase_route = []
            elif self.chase_target != candidates[0][1]:
                self.chase_target = candidates[0][1]
                self.chase_route = []
            packet = self.plan_chase_step(now)
            self.activity = "追擊"
            return ("chase", packet)
        self.chase_target = None
        self.chase_route = []
        if self._anchor_active():
            packet = self.plan_anchor_return(now)
            if packet is not None:
                return ("chase", packet)
            self.status = (f"在空曠練功點等怪 ({self.hunt_anchor[0]:.0f}, {self.hunt_anchor[1]:.0f})"
                           if self.hunt_anchor else "在空曠練功點等怪")
            self.activity = "等待出怪"
            return ("wait", None)
        packet = self.plan_patrol_step(now)
        self.activity = "巡邏搜尋目標"
        return ("patrol", packet)


    def _can_stutter_move(self, now: float) -> bool:
        """Move only after the current shot has been sent (post-impact), never before."""
        if self.movement_paused:
            return False
        return now >= float(getattr(self, "_shot_move_at", 0.0) or 0.0)

    def _wait_for_ticket(self, encounter_id: str, nearby: list, now: float, *, approach: bool = True) -> tuple[str, Any]:
        busy = now < self.tickets.busy_until_at
        if busy:
            self.wait_reason = "攻擊動作冷卻中"
            if self.kite_active(encounter_id) and self._can_stutter_move(now):
                distance = nearby[0][0]
                for row in nearby:
                    if row[1] == encounter_id:
                        distance = row[0]
                if distance < KITE_SHOOT_MIN:
                    step = self.plan_kite_step(encounter_id, distance, now)
                    if step is not None:
                        mon = self.monsters.monsters.get(encounter_id) or {}
                        self.activity = "風箏精英" if self.is_elite(mon) else "風箏"
                        return ("chase", step)
            return ("wait_ticket", None)
        if self.ticket_wait_since is None:
            self.ticket_wait_since = now
        waited = now - self.ticket_wait_since
        if self.tickets.current is None:
            self.wait_reason = "還沒收到伺服器的攻擊票據"
        else:
            self.wait_reason = "上一張攻擊票據已用掉，等伺服器發新的"
        if waited >= TICKET_STALL_SEC:
            self.ticket_resyncs += 1
            self.ticket_wait_since = now
            # Release the reservation: if the server never consumed it we can
            # reuse it; if it did, the server answers with a fresh nextAttack.
            self.tickets.used.clear()
            self.tickets.busy_until_at = 0.0
            self.attack_target = None
            self.chase_target = None
            self.chase_route = []
            self.last_sync_at = 0.0
            log.info("超過 %d 秒沒有拿到攻擊票據（%s），重新同步並重試。", int(TICKET_STALL_SEC), self.wait_reason)
            self.status = "票據逾時，重新同步"
            return ("resync", None)
        self.status = f"等待票據：{self.wait_reason}"
        # Use the wait to close distance to the target (never during an attack animation).
        distance = nearby[0][0]
        for row in nearby:
            if row[1] == encounter_id:
                distance = row[0]
        approach_limit = attack_reach(self.character_class, "prepare") * TICKET_WAIT_APPROACH_RATIO
        if self.kite_active(encounter_id):
            approach_limit = max(approach_limit, KITE_SHOOT_MAX - 10.0)
        if approach and not self.movement_paused and distance > approach_limit:
            if self.chase_target != encounter_id:
                self.chase_target = encounter_id
                self.chase_route = []
            packet = self._approach_step(encounter_id, now)
            if packet is not None:
                return ("chase", packet)
        return ("wait_ticket", None)

    def _approach_step(self, encounter_id: str, now: float) -> dict[str, Any] | None:
        if self.position is None:
            return None
        monster = self.monsters.monsters.get(encounter_id)
        if not monster:
            return None
        if not self.chase_route:
            goal = (float(monster["x"]), float(monster["y"]))
            start = (self.position.x, self.position.y)
            path = find_map_path(self.position.map_id, start, goal) if has_navigation(self.position.map_id) else [goal]
            remain = attack_reach(self.character_class, "prepare") * TICKET_WAIT_APPROACH_RATIO * 0.8
            self.chase_route = _trim_approach(start, path, remain) if path else [goal]
        dt = min(0.1, max(0.0, now - (self.chase_last_at or 0.0))) or MOVE_TICK_SEC
        self.chase_last_at = now
        self.chase_route, packet = self._advance_route(
            self.chase_route,
            WAYPOINT_ARRIVE_CHASE,
            dt=dt,
            max_step=MOVEMENT_SPEED / 10.0,
            speed_factor=1.0,
            now=now,
        )
        return packet


    # ---- damage source / potions -------------------------------------------
    def classify_damage_source(self, now: float) -> str:
        """Best-effort: 'player' or 'monster'. The protocol has no explicit attacker
        field for damage taken, so we use explicit player hit packets when present,
        otherwise proximity of monsters versus other players."""
        if now - self._player_hit_at <= DAMAGE_SOURCE_MEMORY_SEC:
            return "player"
        if self.position is None:
            return "monster"
        me = (self.position.x, self.position.y)
        for _eid, monster in self.monsters.living_on_map(self.position.map_id):
            try:
                if math.dist(me, (float(monster["x"]), float(monster["y"]))) <= MONSTER_THREAT_RADIUS:
                    return "monster"
            except (KeyError, TypeError, ValueError):
                continue
        for peer in self.peers.on_map(self.position.map_id):
            if peer.get("kind") != "player":
                continue
            if self.player_id is not None and str(peer.get("id")) == str(self.player_id):
                continue
            try:
                if math.dist(me, (float(peer["x"]), float(peer["y"]))) <= PLAYER_THREAT_RADIUS:
                    return "player"
            except (KeyError, TypeError, ValueError):
                continue
        return "monster"

    def _track_damage(self, now: float) -> None:
        try:
            hp = int(self.self_state.get("health"))
        except (TypeError, ValueError):
            return
        if self._last_hp is not None and hp < self._last_hp and not self.is_dead:
            lost = self._last_hp - hp
            self.last_damage_source = self.classify_damage_source(now)
            self.last_damage_at = now
            self.hits_taken += 1
            self._dmg_taken_events.append((now, float(lost)))
            self._trim_rate(self._dmg_taken_events, now)
        elif self._last_hp is not None and hp > self._last_hp and not self.is_dead:
            if now - (self.last_damage_at or 0.0) > 1.5:
                self._regen_events.append((now, float(hp - self._last_hp)))
                self._trim_rate(self._regen_events, now)
        self._last_hp = hp

    def health_potion_count(self) -> int:
        try:
            return int(self.self_state.get("healthPotions", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def hp_ratio(self) -> float | None:
        try:
            hp = float(self.self_state.get("health", 0))
            max_hp = float(self.self_state.get("maxHealth", 0))
        except (TypeError, ValueError):
            return None
        return hp / max_hp if max_hp > 0 else None

    def plan_rest(self, now: float) -> tuple[str, Any] | None:
        """Low HP (< 35%): stop fighting, back away from nearby monsters and wait for natural regen
        to ~80% before engaging again. Repeated low-HP rests -> suggest lowering 練功等級."""
        ratio = self.hp_ratio()
        if ratio is None or self.position is None:
            return None
        if not self.resting:
            if ratio >= REST_LOW_RATIO:
                return None
            self.resting = True
            self._rest_started = now
            self._rest_retreat_fails = 0
            self._attack_gen += 1
            self.attack_target = None
            self.chase_target = None
            self.chase_route = []
            self.patrol_route = []
            self._kite_route = []
            self._low_hp_events = [t for t in self._low_hp_events if now - t < REST_HINT_WINDOW_SEC] + [now]
            self.combat_event("生命過低，撤退回血", None)
            log.info("生命 %d%%，先撤退等自然回血到 %d%% 再打（不喝水）", int(ratio * 100), int(REST_RESUME_RATIO * 100))
            if len(self._low_hp_events) >= REST_HINT_COUNT and now - self._rest_hint_at > REST_HINT_WINDOW_SEC:
                self._rest_hint_at = now
                log.info("建議：%d 分鐘內已經 %d 次血量過低，Lv%s 可能太難，考慮把「練功等級」調低一級。",
                         int(REST_HINT_WINDOW_SEC // 60), len(self._low_hp_events), self.farm_target_level)
        elif ratio >= REST_RESUME_RATIO:
            self.resting = False
            self.combat_event("回血完成，繼續打", None)
            log.info("生命回到 %d%%（休息 %d 秒），繼續打怪", int(ratio * 100), int(now - self._rest_started))
            return None
        me = (self.position.x, self.position.y)
        self.status = f"回血中（{int(ratio * 100)}% → {int(REST_RESUME_RATIO * 100)}%）"
        self.activity = "撤退回血"
        self.attack_target = None
        self.chase_target = None
        self.chase_route = []
        threats = []
        close = []
        for _eid, monster in self.monsters.living_on_map(self.position.map_id):
            try:
                pos = (float(monster["x"]), float(monster["y"]))
            except (KeyError, TypeError, ValueError):
                continue
            d = math.dist(me, pos)
            if d <= REST_SAFE_DIST:
                threats.append(pos)
            if d <= 120.0:
                close.append(pos)
        if self.movement_paused:
            return ("wait", None)
        if not threats:
            self._rest_retreat_fails = 0
            return ("wait", None)
        cx = sum(p[0] for p in threats) / len(threats)
        cy = sum(p[1] for p in threats) / len(threats)
        dx, dy = me[0] - cx, me[1] - cy
        norm = math.hypot(dx, dy) or 1.0
        candidates = [(me[0] + dx / norm * REST_SAFE_DIST, me[1] + dy / norm * REST_SAFE_DIST)]
        # Try sides and slightly shorter steps before giving up.
        for sign in (1.0, -1.0):
            candidates.append((me[0] - dy / norm * REST_SAFE_DIST * sign, me[1] + dx / norm * REST_SAFE_DIST * sign))
        for dist in (300.0, 220.0, 180.0, 120.0, 80.0):
            candidates.append((me[0] + dx / norm * dist, me[1] + dy / norm * dist))
        packet = None
        for goal in candidates:
            packet = self._move_to(goal, now)
            if packet is not None:
                break
        if packet is not None:
            self._rest_retreat_fails = 0
            return ("rest", packet)
        self._rest_retreat_fails += 1
        # Only stand and shoot when truly boxed in after several failed retreats.
        if close and self._rest_retreat_fails >= REST_CORNERED_FAILS:
            self.status = f"生命 {int(ratio * 100)}%，四面被卡住，只好還擊"
            return None
        return ("wait", None)

    def plan_auto_potion(self, now: float) -> dict[str, Any] | None:
        if not self.auto_potion or not self.world_ready or self.is_dead:
            return None
        try:
            hp = float(self.self_state.get("health", 0))
            max_hp = float(self.self_state.get("maxHealth", 0))
            cooldown = int(self.self_state.get("potionCooldownMs", 0) or 0)
        except (TypeError, ValueError):
            return None
        if max_hp <= 0 or hp / max_hp > self.auto_potion_ratio:
            return None
        if self.health_potion_count() <= 0 or cooldown > 0:
            return None
        if now - self._potion_requested_at < AUTO_POTION_RETRY_SEC:
            return None
        recent = now - self.last_damage_at <= DAMAGE_SOURCE_MEMORY_SEC
        if recent and self.last_damage_source == "player" and not self.potion_on_player_damage:
            note = "被玩家攻擊，依設定不自動喝水（避免被惡意消耗）"
            if self.potion_note != note:
                log.info("%s", note)
            self.potion_note = note
            return None
        self._potion_requested_at = now
        self.potion_note = f"生命 {int(hp)}/{int(max_hp)}，自動喝水"
        self.combat_event("喝水", None)
        return {"action": "useHealthPotion"}

    # ---- elites ---------------------------------------------------------------
    @staticmethod
    def is_elite(monster: dict[str, Any]) -> bool:
        return str(monster.get("monsterRank", "")).upper() == "ELITE" or monster.get("elite") is True

    def elite_contested(self, monster: dict[str, Any]) -> bool:
        """Someone else is already fighting it: it has lost HP or a player stands next to it."""
        try:
            if "maxHealth" in monster and int(monster.get("health", 0)) < int(monster["maxHealth"]):
                return True
        except (TypeError, ValueError):
            pass
        if self.position is None:
            return False
        try:
            mx, my = float(monster["x"]), float(monster["y"])
        except (KeyError, TypeError, ValueError):
            return False
        for peer in self.peers.on_map(self.position.map_id):
            if peer.get("kind") != "player":
                continue
            if self.player_id is not None and str(peer.get("id")) == str(self.player_id):
                continue
            try:
                if math.dist((mx, my), (float(peer["x"]), float(peer["y"]))) <= ELITE_CONTEST_RADIUS:
                    return True
            except (KeyError, TypeError, ValueError):
                continue
        return False

    def is_ranged(self) -> bool:
        if self.character_class == "ARCHER":
            return True
        equipment = self.self_state.get("equipment")
        weapon = None
        if isinstance(equipment, dict):
            weapon = equipment.get("WEAPON") or equipment.get("weapon")
        name = ""
        if isinstance(weapon, dict):
            name = str(weapon.get("name") or weapon.get("itemName") or weapon.get("code") or "")
        return "BOW" in name.upper()

    def elite_allowed(self, encounter_id: str, monster: dict[str, Any]) -> bool:
        if not self.is_elite(monster):
            return True
        if encounter_id == self.attack_target:
            return True  # never abandon a fight already started
        potions = self.health_potion_count()
        low = potions < int(self.elite_potion_min or 0)
        if self.is_ranged():
            # Ranged: always fight elites by kiting, regardless of potions.
            self.kite_targets.add(encounter_id)
            self.elite_note = "弓手：精英一律風箏打（不看藥水）"
            return True
        if not low:
            return True
        if self.elite_contested(monster):
            self.elite_note = f"近戰：藥水 {potions} 瓶低於 {self.elite_potion_min}，但附近有人在打精英，一起參與"
            return True
        self.elite_note = f"近戰：藥水 {potions} 瓶低於 {self.elite_potion_min}，不主動打精英"
        return False

    def apply_combat_options(self, options: dict[str, Any]) -> None:
        if "elite_potion_min" in options:
            try:
                self.elite_potion_min = max(0, min(999, int(options["elite_potion_min"])))
            except (TypeError, ValueError):
                pass
        if "potion_on_player_damage" in options:
            self.potion_on_player_damage = bool(options["potion_on_player_damage"])
        if "prefer_elite" in options:
            self.prefer_elite = bool(options["prefer_elite"])
        if "elite_view_range" in options:
            try:
                value = float(options["elite_view_range"])
            except (TypeError, ValueError):
                value = float(ELITE_VIEW_RANGE)
            self.elite_view_range = max(ELITE_VIEW_RANGE_MIN, min(ELITE_VIEW_RANGE_MAX, value))
        if "auto_potion" in options:
            self.auto_potion = bool(options["auto_potion"])
        if "kite_low_potion" in options:
            self.kite_low_potion = bool(options["kite_low_potion"])
        if "overlevel_kite" in options:
            self.overlevel_kite = bool(options["overlevel_kite"])
        if "overlevel_max" in options:
            try:
                value = int(options["overlevel_max"])
            except (TypeError, ValueError):
                value = OVERLEVEL_DEFAULT
            self.overlevel_max = value if value in OVERLEVEL_STEPS else OVERLEVEL_DEFAULT
        if "cluster_kite" in options:
            self.cluster_kite = bool(options["cluster_kite"])
        if "kite_elite_only" in options:
            self.kite_elite_only = bool(options["kite_elite_only"])
        if "max_pull" in options:
            try:
                value = int(options["max_pull"])
            except (TypeError, ValueError):
                value = MAX_PULL_DEFAULT
            self.max_pull = 1  # one target at a time (t637)


    def _trim_rate(self, store: list, now: float, window: float = 20.0) -> None:
        cutoff = now - window
        store[:] = [row for row in store if row[0] >= cutoff]

    def _event_rate(self, store: list, now: float, window: float = 12.0, default: float = 0.0) -> float:
        self._trim_rate(store, now, window)
        if not store:
            return default
        total = sum(amount for _, amount in store)
        span = max(now - store[0][0], 1.0)
        return total / span

    def measured_player_dps(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        rate = self._event_rate(self._dmg_dealt_events, now, default=0.0)
        return rate if rate > 0 else DEFAULT_PLAYER_DPS

    def measured_incoming_dps(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        rate = self._event_rate(self._dmg_taken_events, now, default=0.0)
        return rate if rate > 0 else DEFAULT_INCOMING_DPS

    def measured_regen(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        rate = self._event_rate(self._regen_events, now, window=20.0, default=0.0)
        return rate if rate > 0 else DEFAULT_REGEN_PER_SEC

    def elite_fight_safe(self, monster: dict[str, Any]) -> bool:
        """(elite HP / our DPS) × incoming DPS vs current HP + regen over the fight."""
        try:
            hp = float(monster.get("health") or monster.get("maxHealth") or 0)
        except (TypeError, ValueError):
            hp = 0.0
        if hp <= 0:
            return True
        now = time.monotonic()
        our = max(self.measured_player_dps(now), 1.0)
        incoming = self.measured_incoming_dps(now)
        regen = self.measured_regen(now)
        try:
            current = float(self.self_state.get("health") or 0)
        except (TypeError, ValueError):
            current = 0.0
        duration = hp / our
        expected = duration * incoming
        available = current + regen * duration
        return expected < available

    def _note_accepted_position(self, now: float | None = None) -> None:
        """Treat the current local position as the last one the server has accepted."""
        now = time.monotonic() if now is None else now
        if self.position is None:
            return
        self._last_accepted_at = now
        self._last_accepted_pos = (float(self.position.x), float(self.position.y))
        self._last_move_sent_at = now
        self.last_sync_at = now

    def note_sync_sent(self, packet: dict[str, Any], now: float | None = None) -> None:
        """Every outgoing sync (move, idle, attack) updates the server's last accepted sample."""
        if packet.get("action") != "sync" or self.position is None:
            return
        now = time.monotonic() if now is None else now
        try:
            x, y = float(packet["x"]), float(packet["y"])
        except (KeyError, TypeError, ValueError):
            return
        prev = self._last_accepted_pos
        moved = prev is None or math.hypot(x - prev[0], y - prev[1]) > 0.05
        self._last_accepted_at = now
        self._last_accepted_pos = (x, y)
        self.last_sync_at = now
        if moved:
            self._last_move_sent_at = now

    def _move_paused(self, now: float) -> bool:
        return now < float(self._move_pause_until or 0.0)

    def _move_budget(self, now: float, *, speed_factor: float = 1.0) -> float:
        """Max distance for one packet: 90% of allowed speed × elapsed since last accepted position.

        Elapsed is capped per packet so a long pause does not become a teleport, but the
        speed check itself always uses time since the last accepted sync (not an average).
        """
        last = float(self._last_accepted_at or 0.0)
        elapsed = (now - last) if last else MOVE_TICK_SEC
        elapsed = max(0.0, elapsed)
        step_elapsed = min(elapsed, MOVE_PACKET_MAX_SEC)
        scale = float(self._move_speed_scale or MOVE_SPEED_MARGIN)
        return max(0.0, MOVEMENT_SPEED * step_elapsed * scale * float(speed_factor or 1.0))

    def _capped_step(self, target: tuple[float, float], now: float, *, speed_factor: float = 1.0) -> tuple[float, float] | None:
        """Next (x, y) toward target, or None if we must wait (throttle / post-correction pause)."""
        if self.position is None or self._move_paused(now):
            return None
        last_sent = float(self._last_move_sent_at or 0.0)
        if last_sent and now - last_sent < MOVE_TICK_SEC - 0.005:
            return None  # throttle move packets
        dx = target[0] - self.position.x
        dy = target[1] - self.position.y
        distance = math.hypot(dx, dy)
        if distance <= 0.05:
            return None
        budget = self._move_budget(now, speed_factor=speed_factor)
        if budget < 0.25:
            return None
        step = min(distance, budget)
        return (round(self.position.x + dx / distance * step, 2),
                round(self.position.y + dy / distance * step, 2))

    def _emit_move(self, x: float, y: float, now: float, *, flip: bool | None = None,
                   animation: str = "run") -> dict[str, Any] | None:
        if self.position is None:
            return None
        if flip is None:
            flip = x < self.position.x
        packet = build_sync(
            movement_revision=self.position.revision,
            map_id=self.position.map_id,
            x=x,
            y=y,
            animation=animation,
            flip=bool(flip),
        )
        self.position.x = x
        self.position.y = y
        self.position.estimated = True
        self._last_step_to = (x, y)
        self.note_sync_sent(packet, now)
        return packet

    def predicted_monster_distance(self, encounter_id: str, monster: dict[str, Any], now: float) -> float:
        if self.position is None or not monster:
            return 0.0
        try:
            mx, my = float(monster["x"]), float(monster["y"])
        except (KeyError, TypeError, ValueError):
            return monster_distance(self.position, monster)
        prev = self._mon_seen.get(encounter_id)
        used_hist = False
        vx = vy = 0.0
        if prev is not None:
            t0, x0, y0 = prev
            dt = now - t0
            if 0.05 < dt <= 3.0:
                vx, vy = (mx - x0) / dt, (my - y0) / dt
                used_hist = math.hypot(vx, vy) >= 5.0
        # Record for the next tick; do not overwrite a fresh sample in the same now.
        if prev is None or now - prev[0] >= 0.04:
            self._mon_seen[encounter_id] = (now, mx, my)
        horizon = KITE_PREDICT_HORIZON_SEC
        if used_hist:
            px, py = mx + vx * horizon, my + vy * horizon
        else:
            dx, dy = self.position.x - mx, self.position.y - my
            norm = math.hypot(dx, dy) or 1.0
            px = mx + dx / norm * MONSTER_APPROACH_SPEED * horizon
            py = my + dy / norm * MONSTER_APPROACH_SPEED * horizon
        return math.hypot(self.position.x - px, self.position.y - py)

    def note_kite_distance(self, distance: float, predicted: float) -> None:
        self.last_kite_distance = float(distance)
        now = time.monotonic()
        self._kite_dist_log.append((now, float(distance), float(predicted)))
        if len(self._kite_dist_log) > 200:
            self._kite_dist_log = self._kite_dist_log[-200:]
        self.kite_note = f"風箏：距離 {distance:.0f}（預測 {predicted:.0f}）"

    def kite_spacing(self, encounter_id: str, monster: dict[str, Any], distance: float, now: float) -> tuple[float, bool, bool]:
        """Returns (predicted_distance, must_move, may_shoot).

        Back off until the gap is ~260 (band 230–300). Once in the band, fire a
        burst of shots before moving again. If retreat is getting nowhere, shoot.
        """
        pred = self.predicted_monster_distance(encounter_id, monster, now)
        self.note_kite_distance(distance, pred)
        in_band = distance >= KITE_SHOOT_MIN and pred >= KITE_RETREAT_DIST
        give_up = self._kite_retreat_give_up(distance, now)
        if in_band:
            self._clear_retreat()
            must_move = False
            may_shoot = True
        elif give_up:
            must_move = False
            may_shoot = True  # 3 s without a shot, or no progress / HP falling
        else:
            must_move = True
            may_shoot = False
        return pred, must_move, may_shoot

    def _clear_retreat(self) -> None:
        self._retreat_started_at = 0.0
        self._retreat_tried_straight = False

    def _mark_retreat(self, distance: float, now: float) -> None:
        if self._retreat_started_at and now - self._retreat_started_at < 8.0:
            return
        self._retreat_started_at = now
        self._retreat_start_dist = float(distance)
        self._retreat_start_hp = self._hp_value()
        self._retreat_tried_straight = False
        self._kite_burst_left = 0

    def _kite_retreat_give_up(self, distance: float, now: float) -> bool:
        started = float(self._retreat_started_at or 0.0)
        if started <= 0:
            return False
        elapsed = now - started
        if elapsed >= KITE_RETREAT_MAX_SEC:
            return True
        if elapsed >= KITE_RETREAT_PROGRESS_SEC:
            if distance < self._retreat_start_dist + KITE_RETREAT_PROGRESS_MIN:
                return True
            hp = self._hp_value()
            if hp is not None and self._retreat_start_hp is not None and hp < self._retreat_start_hp:
                return True
        return False

    def _radial_retreat_goal(self, threat: tuple[float, float], distance: float, near_center: tuple[float, float] | None = None) -> tuple[float, float] | None:
        if self.position is None:
            return None
        hold_dist = KITE_SHOOT_MIN if near_center else KITE_HOLD_DIST
        me = (self.position.x, self.position.y)
        dx, dy = me[0] - threat[0], me[1] - threat[1]
        base = math.atan2(dy, dx)
        need = max(KITE_HOLD_DIST - distance, 160.0)
        need = min(need, KITE_RETREAT_STEP)
        candidates = []
        # Sweep a wide fan (including due-side) so a wall in a straight line
        # doesn't collapse the goal into a tiny hop. _move_to will path around.
        offsets = (0.0, 0.3, -0.3, 0.6, -0.6, 0.9, -0.9, 1.2, -1.2, 1.57, -1.57, 2.0, -2.0, 2.5, -2.5)
        for off in offsets:
            ang = base + off
            for dist in (need, KITE_RETREAT_STEP, 220.0, 160.0):
                raw = (me[0] + math.cos(ang) * dist, me[1] + math.sin(ang) * dist)
                goal = raw
                if not self._point_walkable(goal):
                    opened = self.open_point(raw, prefer=me)
                    # Don't accept an open_point that shrinks a long retreat into a short hop.
                    if math.dist(me, opened) < 160.0 and math.dist(opened, threat) < max(distance + 80.0, KITE_SHOOT_MIN):
                        continue
                    goal = opened
                if not self._point_walkable(goal):
                    continue
                gap = math.dist(goal, threat)
                step = math.dist(me, goal)
                hold = 1 if gap >= hold_dist else 0
                band = 1 if gap >= KITE_SHOOT_MIN else 0
                score = (
                    hold * 2000.0 + band * 800.0 + gap + 0.2 * step
                    + 0.3 * self.wall_distance(goal) - 40.0 * abs(off)
                )
                if near_center:  # normal mobs: circle close to the pile, not away from it
                    dc = math.dist(goal, near_center)
                    score -= 8.0 * max(0.0, dc - 200.0) + (0.0 if gap < 240.0 else gap - 240.0)
                candidates.append((score, goal))
        if candidates:
            candidates.sort(key=lambda row: -row[0])
            return candidates[0][1]
        norm = math.hypot(dx, dy) or 1.0
        return (me[0] + dx / norm * need, me[1] + dy / norm * need)

    def kite_active(self, encounter_id: str) -> bool:
        if not self.is_ranged() or not self.cluster_kite:
            return False  # 「風箏」 off: stand and shoot
        monster = self.monsters.monsters.get(encounter_id) or {}
        if self.kite_elite_only and not self.is_elite(monster):
            return False
        lv = monster_level(monster)
        if not self.is_elite(monster) and lv is not None and lv <= KITE_NO_KITE_MAX_LEVEL:
            return False  # low-level normal mobs: stand and shoot
        if encounter_id in self.kite_failed:
            return False
        return True

    def abort_kite(self, encounter_id: str, reason: str) -> None:
        monster = self.monsters.monsters.get(encounter_id) or {}
        level = monster_level(monster)
        if self.position is not None:
            self.efficiency.record_abort(self.position.map_id, level)
        if encounter_id in self.overlevel_targets:
            from tabbybot.efficiency import level_band
            band = level_band(level)
            if band is not None:
                self.overlevel_band_lock[band] = time.monotonic() + OVERLEVEL_LOCK_SEC
            self.overlevel_targets.discard(encounter_id)
            self.overlevel_note = f"越級放棄 Lv{level if level is not None else '?'}：{reason}；60 秒內改打一般等級"
            log.info("%s", self.overlevel_note)
        if self._engage and self._engage.get("id") == encounter_id:
            self._engage = None
        self.kite_failed.add(encounter_id)
        self.kite_targets.discard(encounter_id)
        self._kite_route = []
        self.kite_note = f"風箏中止：{reason}"
        log.info("%s", self.kite_note)
        if self.attack_target == encounter_id:
            self.attack_target = None
        self.tickets.lock_invalid(encounter_id, duration=60.0)

    def plan_kite_step(self, encounter_id: str, distance: float, now: float) -> dict[str, Any] | None:
        """Step straight away until the gap is ~260 (band 230–300)."""
        if self.position is None or not self._can_stutter_move(now):
            return None  # never move before impact / during pre-shot windup
        monster = self.monsters.monsters.get(encounter_id)
        if not monster:
            return None
        if distance <= KITE_ABORT_CONTACT_DIST:
            self._kite_contacts += 1
            if self._kite_contacts >= KITE_ABORT_CONTACTS:
                self.abort_kite(encounter_id, "精英一直貼身，拉不開距離")
                return None
        mx, my = float(monster["x"]), float(monster["y"])
        self._mark_retreat(distance, now)
        site = self.clusters.site(self.cluster_site_id) if not self.is_elite(monster) else None
        goal = self._radial_retreat_goal((mx, my), distance, (site.x, site.y) if site else None)
        anchor = (self._engage or {}).get("anchor") if (self._engage or {}).get("id") == encounter_id else None
        if goal is None or self.retreat_blocked(now):
            self.kite_note = "後面是牆，原地射擊"
            return None
        packet = self._move_to(goal, now, threat=(mx, my), center=anchor or (mx, my))
        if packet is None:
            self.kite_note = "退路被牆擋住，原地射擊"
            return None
        pred = getattr(self, "last_kite_distance", distance)
        # last_kite_distance is current; predicted is already in kite_note if set.
        base = self.kite_note if "預測" in (self.kite_note or "") else f"風箏：距離 {distance:.0f}"
        self.kite_note = f"{base}，後撤拉開"
        self.status = self.kite_note
        self.combat_event("後退", monster, throttle=3.0, key="retreat")
        return packet


    # ---- leash / reset detection (t624) ------------------------------------------
    def _check_regen(self) -> None:
        """A monster we are fighting whose server HP goes UP has lost aggro and is resetting."""
        watch = {self.attack_target, (self._engage or {}).get("id")} - {None}
        for encounter_id in list(self._server_hp):
            if encounter_id not in watch:
                self._server_hp.pop(encounter_id, None)
        for encounter_id in watch:
            monster = self.monsters.monsters.get(encounter_id)
            if not isinstance(monster, dict):
                continue
            try:
                hp = int(monster.get("health", 0))
                max_hp = int(monster.get("maxHealth", 0) or 0)
            except (TypeError, ValueError):
                continue
            previous = self._server_hp.get(encounter_id)
            self._server_hp[encounter_id] = hp
            if previous is not None and hp > 0 and hp - previous > max(1, int(max_hp * 0.02)):
                self.mark_resetting(encounter_id, f"怪物回血 {previous}→{hp}")

    def mark_resetting(self, encounter_id: str, reason: str) -> None:
        """Target lost aggro (walking home / regen / 0 damage): stop shooting it, shrink leash."""
        now = time.monotonic()
        monster = self.monsters.monsters.get(encounter_id) or {}
        anchor = (self._engage or {}).get("anchor") if (self._engage or {}).get("id") == encounter_id else None
        if anchor is None and self.position is not None:
            site = self.clusters.by_id.get(encounter_id)
            if site is not None:
                anchor = (site.x, site.y)
        if anchor is not None and self.position is not None:
            dragged = math.dist((self.position.x, self.position.y), anchor)
            if dragged > KITE_MIN_DIST:
                self.kite_leash = max(KITE_LEASH_MIN, min(self.kite_leash, dragged * 0.85))
        self.leash_resets += 1
        self._zero_streak.pop(encounter_id, None)
        self._server_hp.pop(encounter_id, None)
        if self.position is not None:
            self.efficiency.record_abort(self.position.map_id, monster_level(monster))
        if self._engage and self._engage.get("id") == encounter_id:
            self._engage = None
        if self.attack_target == encounter_id:
            self.attack_target = None
        if self.chase_target == encounter_id:
            self.chase_target = None
            self.chase_route = []
        self.tickets.lock_invalid(encounter_id, now, LEASH_RESET_LOCK_SEC)
        self.kite_note = f"目標脫離仇恨（{reason}），改打別隻；風箏範圍調整為 {self.kite_leash:.0f}"
        log.info("%s", self.kite_note)
        self.combat_event("放棄（脫離仇恨）", monster or None)

    def wall_distance(self, point: tuple[float, float], limit: float = 80.0) -> float:
        """How far this point is from a wall (official nav + learned blocks). 0 if not walkable."""
        if self.position is None:
            return limit
        map_id = self.position.map_id
        best = float(limit)
        nav = _cached_navigator(map_id) if has_navigation(map_id) else None
        if nav is not None:
            best = min(best, nav.wall_distance(point[0], point[1], limit))
            if best <= 0:
                return 0.0
        grid = learned_grid(map_id)
        if grid.blocked:
            from tabbybot.learned_nav import CELL, cell_of
            cx, cy = cell_of(point[0], point[1])
            reach = max(1, int(math.ceil(limit / CELL)))
            for dx in range(-reach, reach + 1):
                for dy in range(-reach, reach + 1):
                    if (cx + dx, cy + dy) in grid.blocked:
                        dist = math.hypot((cx + dx + 0.5) * CELL - point[0], (cy + dy + 0.5) * CELL - point[1])
                        best = min(best, dist)
                        if best <= 0:
                            return 0.0
        return best

    def kite_wall_need(self, start: tuple[float, float]) -> float:
        """Required wall clearance along a kite step: at least 40, and don't walk closer to a wall."""
        room = self.wall_distance(start)
        if room >= KITE_WALL_CLEARANCE:
            return max(KITE_WALL_CLEARANCE, room - 8.0)
        return KITE_WALL_CLEARANCE

    def open_point(self, point: tuple[float, float], prefer: tuple[float, float] | None = None,
                   min_clear: float = KITE_WALL_CLEARANCE) -> tuple[float, float]:
        """Nudge a point (orbit centre, retreat goal) away from walls into more open space."""
        here = (self.position.x, self.position.y) if self.position is not None else point
        need = max(min_clear, self.kite_wall_need(prefer or here))
        if self.wall_distance(point) >= need and self._point_walkable(point):
            return point
        best = None
        for dist in (20.0, 40.0, 60.0, 80.0, 100.0):
            for i in range(16):
                ang = i * (2 * math.pi / 16)
                cand = (point[0] + math.cos(ang) * dist, point[1] + math.sin(ang) * dist)
                if not self._point_walkable(cand):
                    continue
                room = self.wall_distance(cand)
                score = room - 0.05 * math.dist(cand, prefer or here)
                if best is None or score > best[0]:
                    best = (score, cand)
            if best is not None and best[0] >= need:
                return best[1]
        return best[1] if best is not None else point

    def _point_walkable(self, point: tuple[float, float]) -> bool:
        if self.position is None:
            return False
        map_id = self.position.map_id
        if has_navigation(map_id):
            nav = _cached_navigator(map_id)
            if nav is not None and not nav.walkable(*point):
                return False
        grid = learned_grid(map_id)
        if grid.blocked:
            from tabbybot.learned_nav import cell_of
            if cell_of(*point) in grid.blocked:
                return False
        return True

    def segment_open(self, start: tuple[float, float], end: tuple[float, float],
                     min_clear: float | None = None) -> bool:
        """Whole segment stays walkable and at least `min_clear` from walls (not just the endpoint)."""
        if min_clear is None:
            min_clear = min(KITE_WALL_CLEARANCE, max(self.wall_distance(start), 4.0))
        if not self._point_walkable(end):
            return False
        map_id = self.position.map_id if self.position is not None else 0
        nav = _cached_navigator(map_id) if has_navigation(map_id) else None
        if nav is not None and not nav.line_clear(start, end):
            return False
        grid = learned_grid(map_id)
        distance = math.dist(start, end)
        steps = max(1, int(math.ceil(distance / 8.0)))
        start_room = self.wall_distance(start) if min_clear > 0 else 0.0
        for i in range(1, steps + 1):
            t = i / steps
            x = start[0] + (end[0] - start[0]) * t
            y = start[1] + (end[1] - start[1]) * t
            if not self._point_walkable((x, y)):
                return False
            if grid.blocked:
                from tabbybot.learned_nav import cell_of
                if cell_of(x, y) in grid.blocked:
                    return False
            if min_clear <= 0:
                continue
            room = self.wall_distance((x, y))
            need = min_clear if distance * t > 16.0 else min(min_clear, start_room)
            if room < need - 0.5:
                return False
        return True

    def move_ok(self, goal: tuple[float, float]) -> bool:
        """Goal is walkable AND the straight line there doesn't cross a wall or a learned block."""
        if self.position is None:
            return False
        me = (self.position.x, self.position.y)
        return self._point_walkable(goal) and self.segment_open(me, goal, min_clear=0.0)

    def kite_goal_ok(self, goal: tuple[float, float], threat: tuple[float, float] | None = None) -> bool:
        """Kite/orbit candidate: open of walls, and the chord stays off the target monster."""
        if self.position is None or not self._point_walkable(goal):
            return False
        me = (self.position.x, self.position.y)
        if not self.segment_open(me, goal, min_clear=self.kite_wall_need(me)):
            return False
        if threat is not None:
            clearance = min(KITE_CLEARANCE, max(math.dist(me, threat), 1.0))
            if segment_min_dist(me, goal, threat) < clearance:
                return False
        return True

    def move_ok_away(self, monster: dict[str, Any]) -> bool:
        """Can we step ~200 directly away from this monster (and not be blocked lately)?"""
        if self.position is None or self.retreat_blocked(time.monotonic()):
            return False
        mx, my = float(monster["x"]), float(monster["y"])
        dx, dy = self.position.x - mx, self.position.y - my
        norm = math.hypot(dx, dy) or 1.0
        return self.move_ok((self.position.x + dx / norm * 200.0, self.position.y + dy / norm * 200.0))

    def retreat_blocked(self, now: float) -> bool:
        """Two+ server rejections in the last 3 s: stop trying to walk, just shoot."""
        return len([t for t in getattr(self, "_reject_times", []) if now - t < 3.0]) >= 2

    def _orbit_goal(self, center: tuple[float, float], radius: float, threat: tuple[float, float] | None) -> tuple[float, float] | None:
        """Next open point on a circle around `center`, away from walls and from `threat`."""
        me = (self.position.x, self.position.y)
        center = self.open_point(center, prefer=me)
        angle = math.atan2(me[1] - center[1], me[0] - center[0])
        options = []
        for scale in (1.0, 1.15, 0.85, 1.3):
            r = max(radius * scale, KITE_CLEARANCE)
            step = CLUSTER_ORBIT_STEP / max(r, 1.0)
            for sign in (1.0, -1.0):
                a = angle + sign * step
                goal = (center[0] + r * math.cos(a), center[1] + r * math.sin(a))
                goal = self.open_point(goal, prefer=me)
                if not self.kite_goal_ok(goal, threat):
                    continue
                room = self.wall_distance(goal)
                score = room * 3.0
                if threat is not None:
                    score += math.dist(goal, threat)
                if sign == getattr(self, "_orbit_sign", 1.0):
                    score += 25.0  # keep circling the same way unless the other side is clearly safer
                if scale != 1.0:
                    score -= 20.0
                if room < KITE_WALL_CLEARANCE:
                    score -= 200.0
                options.append((score, sign, goal, room))
        if not options:
            return None
        options.sort(key=lambda row: -row[0])
        # Prefer a point with wall clearance if any exist; otherwise stand (caller gets None).
        open_ones = [row for row in options if row[3] >= KITE_WALL_CLEARANCE]
        chosen = (open_ones or options)[0]
        if chosen[3] < min(KITE_WALL_CLEARANCE, self.wall_distance(me) + 4.0) and not open_ones:
            return None
        self._orbit_sign = chosen[1]
        return chosen[2]

    def _path_usable(self, start: tuple[float, float], path: list[tuple[float, float]],
                     threat: tuple[float, float] | None, min_clear: float | None = None) -> bool:
        prev = start
        clearance = KITE_CLEARANCE
        if threat is not None:
            clearance = min(KITE_CLEARANCE, max(math.dist(start, threat), 1.0))
        need = self.kite_wall_need(start) if min_clear is None else min_clear
        for dest in path:
            if not self.segment_open(prev, dest, min_clear=need):
                return False
            if threat is not None and segment_min_dist(prev, dest, threat) < clearance:
                return False
            prev = dest
        return True

    def _move_to(self, goal: tuple[float, float] | None, now: float,
                 threat: tuple[float, float] | None = None,
                 center: tuple[float, float] | None = None) -> dict[str, Any] | None:
        """Walk to goal along a path that stays off walls and off the kite target. None = stand and shoot."""
        if goal is None or self.position is None:
            return None
        start = (self.position.x, self.position.y)
        if threat is not None:
            clearance = min(KITE_CLEARANCE, max(math.dist(start, threat), 1.0))
        else:
            clearance = KITE_CLEARANCE
        goal = self.open_point(goal, prefer=start)
        path = None
        nav_map = has_navigation(self.position.map_id)
        if nav_map:
            raw = find_map_path(self.position.map_id, start, goal)
            # Prefer a roomy path; if the only route is a bit tighter than ideal,
            # still take it rather than standing still in melee. Never walk into a wall.
            loose = max(16.0, min(self.kite_wall_need(start), self.wall_distance(start) - 16.0))
            if raw and (self._path_usable(start, raw, threat)
                        or self._path_usable(start, raw, threat, min_clear=loose)):
                path = list(raw)
        if path is None and center is not None:
            radius = max(math.dist(start, center), math.dist(goal, center), KITE_CLEARANCE)
            sign0 = getattr(self, "_orbit_sign", 1.0)
            for sign in (sign0, -sign0):
                arc = arc_waypoints(center, start, goal, sign, radius, count=6)
                if self._path_usable(start, arc, threat) and all(self._point_walkable(pt) for pt in arc):
                    path = arc
                    self._orbit_sign = sign
                    break
        if path is None and threat is not None:
            radius = max(math.dist(start, threat), KITE_CLEARANCE)
            sign0 = getattr(self, "_orbit_sign", 1.0)
            for sign in (sign0, -sign0):
                arc = arc_waypoints(threat, start, goal, sign, radius, count=6)
                if self._path_usable(start, arc, threat) and all(self._point_walkable(pt) for pt in arc):
                    path = arc
                    self._orbit_sign = sign
                    break
        if path is None and self.kite_goal_ok(goal, threat):
            path = [goal]
        loose = max(16.0, min(self.kite_wall_need(start), self.wall_distance(start) - 16.0))
        if path is None and threat is not None and self._point_walkable(goal):
            if (self.segment_open(start, goal, min_clear=loose)
                    and segment_min_dist(start, goal, threat) >= math.dist(start, threat) - 8.0):
                path = [goal]
        if path is None and threat is not None:
            # Last resort: one walkable step away that doesn't close on a wall.
            dx, dy = start[0] - threat[0], start[1] - threat[1]
            norm = math.hypot(dx, dy) or 1.0
            start_room = self.wall_distance(start)
            for step in (160.0, 80.0, 40.0):
                mid = (start[0] + dx / norm * step, start[1] + dy / norm * step)
                if (self._point_walkable(mid)
                        and self.wall_distance(mid) >= start_room - 8.0
                        and self.segment_open(start, mid, min_clear=loose)):
                    path = [mid]
                    break
        if not path:
            return None
        self._kite_route = list(path)
        dt = min(0.1, max(0.0, now - (self._kite_last_at or now))) or 0.1
        self._kite_last_at = now
        self._kite_route, packet = self._advance_route(
            self._kite_route, WAYPOINT_ARRIVE_CHASE, dt=dt, max_step=MOVEMENT_SPEED / 10.0, speed_factor=1.0, now=now,
        )
        return packet

    # ---- cluster orbit kiting (t621) ---------------------------------------------
    def cluster_mode(self) -> bool:
        return bool(self.cluster_kite) and self.is_ranged()

    def update_clusters(self, now: float) -> None:
        if self.position is None or now - self._cluster_update_at < 0.3:
            return
        self._cluster_update_at = now
        living = []
        for encounter_id, monster in self.monsters.living_on_map(self.position.map_id):
            try:
                living.append((encounter_id, float(monster["x"]), float(monster["y"])))
            except (KeyError, TypeError, ValueError):
                continue
        self.clusters.update(self.position.map_id, living, now, engaged=self._own_hit_ids)

    def _site_targets(self, site: Any, allowed: dict[str, tuple]) -> list[tuple]:
        return [allowed[eid] for eid in site.alive if eid in allowed]

    def respawn_wait_ok(self, site: Any, now: float) -> bool:
        if site is None or site.empty_since is None or site.alive:
            return False
        avg = site.respawn_avg()
        if avg is not None and avg > CLUSTER_WAIT_MAX_SEC:
            return False
        limit = avg * 1.5 + 5.0 if avg is not None else CLUSTER_FIRST_WAIT_SEC
        return now - site.empty_since < limit

    def _in_locked_site(self, encounter_id: str) -> bool:
        if encounter_id == self.attack_target:
            return False
        mon = self.monsters.monsters.get(encounter_id) or {}
        try:
            if int(mon.get("health", 0)) >= int(mon.get("maxHealth", 1)):
                return False  # full HP = nobody hitting it; free to take even in a locked pile
        except (TypeError, ValueError):
            pass
        now = time.monotonic()
        for site in getattr(self.clusters, "sites", ()):
            if now < self._site_lock.get(site.id, 0.0) and encounter_id in getattr(site, "alive", ()):
                return True
        return False

    def _site_contested(self, site: Any) -> bool:
        """Another player is near this 怪堆's centre, or its mobs are being hit by someone else."""
        if self.position is None:
            return False
        for eid in getattr(site, "alive", ()):
            mon = self.monsters.monsters.get(eid) or {}
            try:
                if eid not in self._own_hit_ids and int(mon.get("health", 0)) < int(mon.get("maxHealth", 0)):
                    return True
            except (TypeError, ValueError):
                pass
        radius = self.cluster_ring(site) + 150.0
        for peer in self.peers.on_map(self.position.map_id):
            if peer.get("kind") != "player":
                continue
            if self.player_id is not None and str(peer.get("id")) == str(self.player_id):
                continue
            try:
                if math.dist((site.x, site.y), (float(peer["x"]), float(peer["y"]))) <= radius:
                    return True
            except (KeyError, TypeError, ValueError):
                continue
        return False

    def pick_cluster(self, allowed: dict[str, tuple], now: float) -> Any:
        """Look at the 5 nearest 怪堆; prefer one nobody else is fighting, else join a busy one."""
        rows = []
        for site in self.clusters.sites:
            if now < self._site_lock.get(site.id, 0.0):
                continue
            targets = self._site_targets(site, allowed)
            if not targets:
                continue
            dist = math.hypot(site.x - self.position.x, site.y - self.position.y)
            rows.append((dist, site, targets))
        rows.sort(key=lambda r: r[0])
        best = None
        for dist, site, targets in rows[:5]:
            score = len(targets) * 400.0 - dist + (300.0 if site.id == self.cluster_site_id else 0.0)
            if self._site_contested(site):
                score -= 100000.0  # only if all 5 are taken
            if best is None or score > best[0]:
                best = (score, site)
        return best[1] if best else None

    def cluster_ring(self, site: Any) -> float:
        ring = max(site.spread + KITE_MIN_DIST, KITE_MIN_DIST + 60.0)
        return min(ring, max(KITE_MIN_DIST + 40.0, self.kite_leash * 0.85),
                   attack_reach(self.character_class, "prepare") + site.spread - 20.0)

    def plan_cluster(self, candidates: list, now: float) -> tuple[str, Any] | None:
        """Archer cluster orbit: circle the 怪堆 at a safe radius, shoot the next one right after a kill,
        wait there for the respawn when it is quick. Returns None to fall back to the normal logic."""
        if self.position is None:
            return None
        self.update_clusters(now)
        allowed = {row[1]: row for row in candidates}
        site = self.clusters.site(self.cluster_site_id)
        if site is not None and now < self._site_lock.get(site.id, 0.0):
            site = None
        if site is None or (not self._site_targets(site, allowed) and not self.respawn_wait_ok(site, now)):
            site = self.pick_cluster(allowed, now)
            self.cluster_site_id = site.id if site is not None else None
        if site is not None and now - getattr(self, "_contest_check_at", 0.0) >= 3.0:
            self._contest_check_at = now
            if self._site_contested(site):
                alt = self.pick_cluster(allowed, now)
                if alt is not None and alt.id != site.id and not self._site_contested(alt):
                    log.info("這堆有其他玩家在打，換到沒人打的怪堆")
                    site = alt
                    self.cluster_site_id = alt.id
                    if self.attack_target and self.attack_target not in getattr(alt, "alive", ()):
                        self.attack_target = None
                elif not (self.attack_target and self.attack_target in getattr(site, "alive", ())
                          and self.attack_target in self._own_hit_ids):
                    # No free pile known yet: leave this one for 90 s and go find another.
                    self._site_lock[site.id] = now + 90.0
                    self.cluster_site_id = None
                    self.attack_target = None
                    self.chase_target = None
                    log.info("這堆有人在打、附近還沒找到空的怪堆，先離開去找")
                    return None
        if site is None:
            self.cluster_note = "附近沒有符合等級的怪堆"
            return None
        center = self.open_point((site.x, site.y), prefer=(self.position.x, self.position.y))
        me = (self.position.x, self.position.y)
        ring = self.cluster_ring(site)
        avg = site.respawn_avg()
        respawn_text = f"重生約 {avg:.0f} 秒" if avg is not None else "重生時間測量中"
        targets = self._site_targets(site, allowed)
        if not targets and allowed:
            # Site empty but other mobs exist: never stand idle waiting for respawn.
            self._site_lock[site.id] = now + 15.0
            nxt = self.pick_cluster(allowed, now)
            if nxt is not None and nxt.id != site.id:
                self.cluster_site_id = nxt.id
                self.cluster_note = "怪堆清空，換到附近另一堆"
                return self.plan_cluster(candidates, now)
            self.cluster_site_id = None
            self.cluster_note = "怪堆清空，先打附近其他怪"
            return None
        if not targets:
            waited = now - (site.empty_since or now)
            self.cluster_note = f"怪堆清空，原地等重生（{respawn_text}，已等 {waited:.0f} 秒）"
            self.status = self.cluster_note
            self.activity = "等怪堆重生"
            if math.dist(me, center) > ring + 120.0 and not self.movement_paused:
                packet = self._move_to(center, now, center=center)
                if packet is not None:
                    return ("chase", packet)
            return ("wait", None)
        pulled = [eid for eid in site.alive if eid in self._own_hit_ids]
        hit_rows = [row for row in targets if row[1] in self._own_hit_ids]
        if self.attack_target and self.attack_target in allowed and self.attack_target in site.alive:
            target = allowed[self.attack_target]
        elif hit_rows:
            target = min(hit_rows, key=lambda row: row[0])
        elif len(pulled) < 1:  # game allows one target at a time
            target = min(targets, key=lambda row: row[0])
        else:
            # Already pulling the max: finish one of those (even if it's off-level) before touching a fresh one.
            rows = []
            for eid in pulled:
                mon = self.monsters.monsters.get(eid)
                if isinstance(mon, dict) and mon.get("x") is not None:
                    rows.append((monster_distance(self.position, mon), eid, mon))
            target = min(rows, key=lambda row: row[0]) if rows else min(targets, key=lambda row: row[0])
        self.cluster_note = (f"怪堆 {len(site.alive)} 隻（可打 {len(targets)}），已拉 {len(pulled)}，"
                             f"{respawn_text}，繞圈半徑 {ring:.0f}")
        distance, encounter_id, monster = target
        if self.kite_elite_only and not self.is_elite(monster):
            return None  # 僅風箏精英：普通怪改由一般邏輯站著射
        reach = attack_reach(self.character_class, "prepare")
        # Nearest threat = any living monster of this cluster (they may all aggro).
        threat, threat_dist = None, float("inf")
        recently_hit = now - self.last_damage_at < 3.0 and self.last_damage_source == "monster"
        for eid in site.alive:
            mon = self.monsters.monsters.get(eid)
            if not isinstance(mon, dict) or mon.get("x") is None:
                continue
            # Only monsters that are fighting us are threats; an idle one at full HP just gets shot.
            if eid not in self._own_hit_ids and not recently_hit:
                continue
            d = math.hypot(float(mon["x"]) - me[0], float(mon["y"]) - me[1])
            if d < threat_dist:
                threat, threat_dist = (float(mon["x"]), float(mon["y"])), d
        spacing_dist = threat_dist if threat is not None and threat_dist < distance else distance
        spacing_mon = monster
        if threat is not None and threat_dist <= distance:
            # Use the nearest fighting monster for spacing.
            for eid in site.alive:
                mon = self.monsters.monsters.get(eid)
                if not isinstance(mon, dict):
                    continue
                d = math.hypot(float(mon["x"]) - me[0], float(mon["y"]) - me[1])
                if abs(d - threat_dist) < 1.0:
                    spacing_mon = mon
                    break
        _pred, must_move, may_shoot = self.kite_spacing(encounter_id, spacing_mon, spacing_dist, now)
        if must_move and self._can_stutter_move(now) and not self.retreat_blocked(now):
            close = True
            threat_dist = spacing_dist
            if threat is None and spacing_mon is not None:
                try:
                    threat = (float(spacing_mon["x"]), float(spacing_mon["y"]))
                except (KeyError, TypeError, ValueError):
                    threat = None
        else:
            close = False
        if close:
            if threat_dist <= KITE_ABORT_CONTACT_DIST:
                self._kite_contacts += 1
                if self._kite_contacts >= KITE_ABORT_CONTACTS * 2 and not (
                        self.attack_target and self.attack_target in self._own_hit_ids):
                    self._site_lock[site.id] = now + CLUSTER_SITE_LOCK_SEC
                    self.cluster_site_id = None
                    self._kite_contacts = 0
                    self.kite_note = "怪堆一直貼身，換一堆打（60 秒內不回來）"
                    log.info("%s", self.kite_note)
                    return None
            # Straight radial retreat to ~260 — never orbit in melee (the gap never opens).
            self._mark_retreat(spacing_dist, now)
            threat_pt = threat or (float(monster["x"]), float(monster["y"]))
            goal = self._radial_retreat_goal(threat_pt, spacing_dist)
            packet = self._move_to(goal, now, threat=threat_pt) if goal is not None else None
            if packet is not None:
                self.activity = "拉開距離"
                self.status = f"拉開距離：最近的怪 {threat_dist:.0f} → {int(KITE_HOLD_DIST)}"
                self.combat_event("後退", monster, throttle=3.0, key="retreat")
                return ("chase", packet)
        elif threat_dist > KITE_SHOOT_MIN:
            self._kite_contacts = 0
        if distance > reach:
            if self.movement_paused:
                return ("wait", None)
            # Approach on the ring, on the target's side, instead of walking into the pack.
            mx, my = float(monster["x"]), float(monster["y"])
            vx, vy = mx - center[0], my - center[1]
            norm = math.hypot(vx, vy) or 1.0
            goal = (center[0] + vx / norm * ring, center[1] + vy / norm * ring)
            if math.dist(goal, (mx, my)) > reach - 20.0 or math.dist(me, goal) < 30.0:
                # Stay off the monster; never walk onto it.
                vx, vy = me[0] - mx, me[1] - my
                norm = math.hypot(vx, vy) or 1.0
                stay = max(KITE_SHOOT_MIN, min(distance, min(reach - 15.0, KITE_HOLD_DIST)))
                goal = (mx + vx / norm * stay, my + vy / norm * stay)
            self.chase_target = encounter_id
            packet = self._move_to(goal, now, threat=(mx, my), center=center)
            if packet is not None:
                self.activity = "追擊"
                return ("chase", packet)
            return None  # ring approach blocked: fall back to normal path-finding chase
        self.patrol_route = []
        if must_move and not may_shoot and not self.retreat_blocked(now):
            return ("wait", None)
        if not may_shoot and not self.retreat_blocked(now):
            return self._wait_for_ticket(encounter_id, [target], now, approach=False)
        planned = self.plan_attack(encounter_id, now)
        if planned:
            self.chase_target = None
            self.chase_route = []
            return ("attack", planned)
        return self._wait_for_ticket(encounter_id, [target], now, approach=False)

    def cluster_payload_note(self) -> str:
        return self.cluster_note

    # ---- efficiency / over-level ---------------------------------------------
    def _hp_value(self) -> int | None:
        try:
            return int(self.self_state.get("health"))
        except (TypeError, ValueError):
            return None

    def _record_engaged_kill(self, now: float) -> None:
        engage = self._engage
        if not engage:
            return
        encounter_id = engage["id"]
        self._engage = None
        if encounter_id in self._recorded_kills or encounter_id not in self._own_hit_ids:
            return
        self._recorded_kills.add(encounter_id)
        # Count it as a kill even when the server sends no rewards[] entry for it
        # (seen 11:32–11:36 in 黑石洞窟: 11 kills, 1300 EXP, kill counter stayed 0).
        if encounter_id not in self.claimed:
            self.own_kills += 1
            self._own_counted.add(encounter_id)
            if len(self._own_counted) > 5000:
                self._own_counted.clear()
        if len(self._recorded_kills) > 5000:
            self._recorded_kills.clear()
        potions = max(0, int(engage.get("potions") or 0) - self.health_potion_count())
        cycle = None
        if self._last_kill_at is not None and self._last_kill_map == engage.get("map"):
            cycle = now - self._last_kill_at
        self._last_kill_at, self._last_kill_map = now, engage.get("map")
        self.efficiency.record_kill(engage.get("map"), engage.get("level"), now - float(engage["at"]), potions, now,
                                    cycle_secs=cycle)
        self._zero_streak.pop(encounter_id, None)
        self._server_hp.pop(encounter_id, None)
        dead = self.monsters.monsters.get(encounter_id) or {"name": engage.get("name"), "level": engage.get("level")}
        self.combat_event("擊殺", dead if isinstance(dead, dict) else None)
        self.overlevel_targets.discard(encounter_id)
        exp = None
        from tabbybot.efficiency import extract_experience
        exp = extract_experience(self.self_state)
        if exp is not None:
            self.efficiency.observe_experience(exp[0], exp[1], now)

    def check_engaged_death(self, now: float) -> None:
        engage = self._engage
        if not engage:
            return
        hp = self._hp_value()
        moved_map = self.position is not None and engage.get("map") is not None and engage.get("map") != self.position.map_id
        if self.is_dead or (hp is not None and hp <= 0) or moved_map:
            # We died (or were sent back to town): the monster vanishing from view is not a kill.
            self._engage = None
            if self.is_dead or (hp is not None and hp <= 0):
                self.combat_event("陣亡（不算擊殺）", self.monsters.monsters.get(engage["id"]) or {"name": engage.get("name"), "level": engage.get("level")})
            return
        monster = self.monsters.monsters.get(engage["id"])
        dead = monster is None
        if monster is not None:
            try:
                dead = int(monster.get("health", 0)) <= 0
            except (TypeError, ValueError):
                dead = False
        if dead:
            self._record_engaged_kill(now)

    def check_overlevel_abort(self, now: float) -> None:
        engage = self._engage
        if not engage or engage["id"] not in self.overlevel_targets:
            return
        start_hp = engage.get("hp")
        hp = self._hp_value()
        try:
            max_hp = float(self.self_state.get("maxHealth", 0) or 0)
        except (TypeError, ValueError):
            max_hp = 0.0
        if start_hp is not None and hp is not None and max_hp > 0 and (start_hp - hp) / max_hp > OVERLEVEL_HP_LOSS_RATIO:
            self.abort_kite(engage["id"], f"生命掉了超過 {int(OVERLEVEL_HP_LOSS_RATIO * 100)}%")
            return
        if now - float(engage.get("last_hit_at", engage["at"])) >= OVERLEVEL_NO_DAMAGE_SEC:
            self.abort_kite(engage["id"], f"{int(OVERLEVEL_NO_DAMAGE_SEC)} 秒沒有打出傷害")

    def drop_stale_target(self, reason: str = "") -> None:
        _sk = getattr(self, "_drop_skip", None)
        if _sk is None:
            _sk = self._drop_skip = {}
        _now = time.monotonic()
        for _t in (self.attack_target, self.chase_target):
            if _t:
                _sk[_t] = _now + 30.0
        self.attack_target = None
        self.chase_target = None
        self.chase_route = []
        self._engage = None
        self._clear_retreat()
        self._kite_route = []
        self._retarget_now = True
        # Do not add the current id to the skip list — that was leaving the
        # bot circling a pack it had just locked out.
        self.tickets.invalid_until.clear()
        self.stop_routes()
        self.ticket_wait_since = None
        self.status = "卡住恢復：放棄過期目標" + (f"（{reason}）" if reason else "")
        log.warning("%s", self.status)

    def recover_from_stuck(self) -> None:
        self.tickets.used.clear()
        self.tickets.busy_until_at = 0.0
        self.tickets.invalid_until.clear()
        self.attack_target = None
        self.ticket_wait_since = None
        self.stop_routes()
        self.last_sync_at = 0.0
        self._quests_dirty = True
        self.status = "卡住自動恢復：重新同步位置與攻擊票據"
        log.info("%s", self.status)

    def prepare_for_relogin(self) -> None:
        self.world_ready = False
        self.auth_rejected = False
        self.login_error = False
        self.last_snapshot_at = 0.0
        self.recover_from_stuck()
        self.status = "卡住恢復：重新登入並回到原地圖"
        log.warning("%s", self.status)

    def attack_still_valid(self, encounter_id: str) -> bool:
        if self.position is None or self.is_dead:
            return False
        if self.resting and self._rest_retreat_fails < REST_CORNERED_FAILS:
            return False
        monster = self.monsters.monsters.get(encounter_id)
        if not monster:
            return False
        try:
            if int(monster.get("health", 0)) <= 0:
                return False
        except (TypeError, ValueError):
            return False
        if monster_map_id(monster, self.position.map_id) != self.position.map_id:
            return False
        return monster_distance(self.position, monster) <= attack_reach(self.character_class, "impact")

    def cancel_attack(self, encounter_id: str, ticket_id: str) -> None:
        self.tickets.unconsume(ticket_id)
        self.tickets.busy_until_at = 0.0
        self.tickets.lock_invalid(encounter_id)
        if self.attack_target == encounter_id:
            self.attack_target = None


def token_player_id(token: str) -> int | None:
    import base64
    import json as _json
    try:
        part = str(token).split(".")[1]
        claims = _json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        if isinstance(claims, dict) and claims.get("playerId") is not None:
            return int(claims["playerId"])
    except Exception:
        return None
    return None


def _trim_approach(start: tuple[float, float], path: list[tuple[float, float]], remaining: float) -> list[tuple[float, float]]:
    points = [start, *path]
    approach: list[tuple[float, float]] = []
    for index in range(len(points) - 1, 0, -1):
        segment = math.dist(points[index - 1], points[index])
        if segment >= remaining:
            ratio = remaining / segment
            stop = (
                points[index][0] + (points[index - 1][0] - points[index][0]) * ratio,
                points[index][1] + (points[index - 1][1] - points[index][1]) * ratio,
            )
            approach = [*points[1:index], stop]
            break
        remaining -= segment
    return approach or path



async def run_hunt(
    config: AppConfig,
    private_key: str,
    stop_event: asyncio.Event | None = None,
    controller_holder: dict | None = None,
    alert_hook=None,
    efficiency=None,
    existing_token: str | None = None,
    existing_address: str = "",
) -> int:
    token = None
    _address = existing_address
    player = None
    if existing_token:
        try:
            player = fetch_player(config, existing_token)
            token = existing_token
            log.info("沿用既有登入會話，不再重新簽名。")
        except (LoginError, ClientError) as exc:
            log.info("既有登入會話無法使用（%s），改重新簽名。", type(exc).__name__)
            token, player = None, None
    if not token:
        token, _address, player = signed_login(private_key, config)
    if not player_has_character(player):
        raise RuntimeError("這個錢包還沒有角色。請先到官方客戶端建立角色。")
    klass = player_class(player)
    controller = HuntController(config, klass)
    controller._claim_token = token
    controller.player_id = token_player_id(token)
    if efficiency is not None:
        controller.efficiency = efficiency
    from pathlib import Path as _Path
    setup_debug_log()
    controller.level_store = MapLevelStore(_Path.home() / ".tabbybot" / "map_levels.json")
    controller.level_catalog = controller.level_store.catalog
    from tabbybot.watchdog import StuckWatchdog
    from tabbybot.client import ping_watchdog
    watchdog = StuckWatchdog(hook=alert_hook)
    if controller_holder is not None:
        controller_holder["controller"] = controller
        controller_holder["address"] = _address
        controller_holder["watchdog"] = watchdog
    stop = stop_event if stop_event is not None else asyncio.Event()

    async def close_session(session, recv_task, ping_task) -> None:
        if session is not None:
            try:
                await session.close()
            except Exception:
                pass
        for task in (recv_task, ping_task):
            if task is not None:
                task.cancel()

    def fresh_token():
        new_token, address, _player = signed_login(private_key, config)
        controller._claim_token = new_token
        controller.player_id = token_player_id(new_token)
        if controller_holder is not None:
            controller_holder["address"] = address
        return new_token

    exit_code = 0
    try:
        while not stop.is_set():
            controller.prepare_for_relogin() if controller.world_ready or controller.auth_rejected or controller.login_error else None
            session = RealtimeSession(config, token, controller.on_packet)
            controller.session = session
            current = session

            async def dispatch(packet: dict[str, Any], delay: float, gen: int | None = None) -> None:
                if delay:
                    await asyncio.sleep(delay)
                if current.closed:
                    return
                if packet.get("action") == "monsterAttack":
                    encounter_id = str(packet.get("encounterId", ""))
                    if gen is not None and gen != controller._attack_gen:
                        controller.cancel_attack(encounter_id, str(packet.get("attackTicket", "")))
                        return
                    if not controller.attack_still_valid(encounter_id):
                        controller.cancel_attack(encounter_id, str(packet.get("attackTicket", "")))
                        return
                try:
                    await current.send_game(packet)
                    controller.note_sync_sent(packet)
                except Exception:
                    return

            recv_task = ping_task = None
            relogin = False
            relogin_wait = 2.0
            try:
                await session.connect()
                recv_task = asyncio.create_task(session.recv_loop())
                ping_task = asyncio.create_task(ping_watchdog(session, stop))
                deadline = time.monotonic() + 20
                while not controller.world_ready and not controller.auth_rejected and not session.closed and not stop.is_set():
                    if time.monotonic() > deadline:
                        log.error("等待世界資料逾時，將重新登入。")
                        controller.login_error = True
                        relogin = True
                        break
                    await asyncio.sleep(0.05)
                if stop.is_set():
                    break
                if relogin or controller.auth_rejected or session.closed:
                    relogin = True
                else:
                    idle = controller.plan_idle_sync(time.monotonic(), force=True)
                    if idle:
                        await session.send_game(idle)
                    debug_log.debug("%s", controller.status_line(debug=True))
                    last_status = 0.0
                    last_kind, last_kind_at = "", 0.0
                    while not session.closed and not stop.is_set():
                        now = time.monotonic()
                        if controller.auth_rejected or controller.login_error:
                            relogin = True
                            break
                        respawn = controller.plan_respawn()
                        if respawn:
                            await session.send_game(respawn)
                            await asyncio.sleep(0.2)
                            continue
                        for user_packet in controller.pop_user_packets():
                            await session.send_game(user_packet)
                        potion = controller.plan_auto_potion(now)
                        if potion:
                            await session.send_game(potion)
                        kind, payload = controller.next_combat_action(now)
                        controller.note_action(kind, now)
                        if last_kind_at:
                            controller.efficiency.add_time(last_kind, now - last_kind_at)
                        last_kind, last_kind_at = kind, now
                        controller.efficiency.save(now)
                        verdict = watchdog.observe(controller, kind, now)
                        if verdict == "drop":
                            controller.drop_stale_target((watchdog.alert or {}).get("reason", ""))
                            kind, payload = "resync", None
                        elif verdict == "relogin":
                            relogin = True
                            break
                        elif verdict == "recover":
                            controller.recover_from_stuck()
                            kind, payload = "resync", None
                        if kind == "attack" and payload:
                            gen = controller._attack_gen
                            first_delay, first_packet = payload[0]
                            await dispatch(first_packet, first_delay, gen)
                            for delay, packet in payload[1:]:
                                asyncio.create_task(dispatch(packet, delay, gen))
                            await asyncio.sleep(COMBAT_TICK_SEC)
                        elif kind == "resync":
                            idle = controller.plan_idle_sync(now, force=True)
                            if idle:
                                await session.send_game(idle)
                            await asyncio.sleep(COMBAT_TICK_SEC)
                        elif kind in {"chase", "patrol", "travel", "portal", "manual", "rest"} and payload:
                            await session.send_game(payload)
                            await asyncio.sleep(MOVE_TICK_SEC)
                        else:
                            idle = controller.plan_idle_sync(now)
                            if idle:
                                await session.send_game(idle)
                            await asyncio.sleep(COMBAT_TICK_SEC)
                        if now - last_status >= 2:
                            debug_log.debug("%s", controller.status_line(debug=True))
                            last_status = now
                    if session.closed and not stop.is_set() and not relogin:
                        reason = str(getattr(session, "close_reason", "") or "")
                        if "Connected elsewhere" in reason:
                            log.warning("被別處登入踢下線，5 分鐘後自動重試登入。")
                            relogin_wait = 300.0
                        else:
                            log.warning("連線已關閉（%s），將重新登入。", reason or "未說明原因")
                        relogin = True
                    elif relogin and (controller.auth_rejected or controller.login_error):
                        log.warning(
                            "登入狀態異常（login_error=%s auth_rejected=%s），將重新登入。",
                            controller.login_error, controller.auth_rejected,
                        )
            finally:
                await close_session(session, recv_task, ping_task)
                controller.session = None

            if stop.is_set() or not relogin:
                break
            delay = relogin_wait
            if watchdog._escalated:
                delay = max(delay, watchdog.escalate_retry_sec)
            log.warning("重新登入（%.0f 秒後）…", delay)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
                break
            except asyncio.TimeoutError:
                pass
            if stop.is_set():
                break
            controller.prepare_for_relogin()
            try:
                token = fresh_token()
            except LoginError as exc:
                controller.login_error = True
                log.warning("重新登入失敗：%s", exc)
                watchdog.observe(controller, "resync", time.monotonic())
                token = token  # retry with old token after another wait
                # Loop again; observe may ask for another relogin after backoff.
    except asyncio.CancelledError:
        raise
    finally:
        try:
            controller.level_store.save(time.monotonic(), force=True)
            controller.efficiency.save(time.monotonic(), force=True)
        except Exception:
            pass
        if stop_event is None:
            stop.set()
    return exit_code
