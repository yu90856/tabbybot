"""Official hosts, map metadata, and combat constants."""

from __future__ import annotations

ALLOWED_HOSTS = frozenset({"game.tabby.meme"})
DEFAULT_API_BASE = "https://game.tabby.meme/api"
DEFAULT_REALTIME_URL = "wss://game.tabby.meme/api/realtime"
DEFAULT_ORIGIN = "https://game.tabby.meme"
CHAIN_ID = 56

AUTH_REQUIRED_FIELDS = (
    "action",
    "movementRevision",
    "map",
    "x",
    "y",
    "animation",
    "attackCritical",
    "flip",
    "token",
    "loginReset",
    "snapshotDelta",
)

# Official client default spawn hint used only in the first auth frame.
AUTH_DEFAULT_MAP = 1
# User-chosen hunt pin. If the user has not picked a map, stay on 失落綠洲.
DEFAULT_HUNT_MAP = 9
AUTH_DEFAULT_X = 544.0
AUTH_DEFAULT_Y = 352.0

MAP_NAMES = {
    1: "維洛鎮",
    2: "北部雪地",
    3: "石谷",
    4: "西部荒野",
    5: "廢棄遺跡",
    6: "南部森林",
    7: "秘寶之地",
    8: "黑石洞窟",
    9: "失落綠洲",
    10: "冰封洞穴",
}

# Button / header labels matching the published desktop client.
MAP_BUTTONS = {
    1: "主城",
    2: "北部雪地",
    3: "石谷",
    4: "西部荒野",
    5: "廢棄遺跡",
    6: "南部森林",
    7: "秘寶之地",
    8: "黑石洞窟",
    9: "失落綠洲",
    10: "冰封洞穴",
}
MAP_TITLES = {
    1: "Vylo Town · 維洛鎮",
    2: "Northern Snowfield · 北部雪地",
    3: "Stone Valley · 石谷",
    4: "Western Wilds · 西部荒野",
    5: "Abandoned Ruins · 廢棄遺跡",
    6: "Southern Forest · 南部森林",
    7: "Treasure Land · 秘寶之地",
    8: "Blackstone Cavern · 黑石洞窟",
    9: "Lost Oasis · 失落綠洲",
    10: "Frozen Cavern · 冰封洞穴",
}

# Adjacent portals reported by the public client. Maps 5 and 7 need special items.
MAP_ROUTES = {
    1: (2, 4, 5, 6),
    2: (1, 10),
    3: (6,),
    4: (1, 9),
    5: (1,),
    6: (1, 3, 8),
    7: (1,),
    8: (6,),
    9: (4,),
    10: (2,),
}

SPECIAL_ENTRY_MAPS = frozenset({5, 7})

MAP_FILES = {
    1: "town_navigation.json",
    2: "snowfield_navigation.json",
    7: "treasure_navigation.json",
    8: "cave_navigation.json",
    9: "oasis_navigation.json",
    10: "frozen_navigation.json",
    # Decoded from the public game package (t643): tile Ground layers + prop collision,
    # and 南部森林's authored GroundBlockers/MapBounds.
    3: "stone_navigation.json",
    4: "wilds_navigation.json",
    5: "wastes_navigation.json",
    6: "south_forest_navigation.json",
}

PLAYER_CLEARANCE = 16
# 105 * 1.15 * 1.15 * 1.2, matching published client movement scaling.
MOVEMENT_SPEED = 105.0 * 1.15 * 1.15 * 1.2
PATROL_MAX_STEP = 7.0
PATROL_SPEED_FACTOR = 0.4
WAYPOINT_ARRIVE_PATROL = 12.0
WAYPOINT_ARRIVE_CHASE = 1.0
SWEEP_SECTOR = 5  # cells of 64px → 320px sectors
RETURN_PORTAL = (3232.0, 6240.0)
RETURN_PORTAL_MAPS = frozenset({8, 9, 10})
RETURN_PORTAL_RADIUS = 220.0

# Reach used for starting an attack / impact check / chase stop.
ATTACK_REACH = {
    "ARCHER": {"prepare": 295.0, "impact": 310.0, "chase_stop": 285.0},
}
DEFAULT_REACH = {"prepare": 55.0, "impact": 60.0, "chase_stop": 45.0}

PING_INTERVAL_SEC = 4.0
PONG_TIMEOUT_SEC = 12.0
IDLE_SYNC_INTERVAL_SEC = 1.0
MOVE_TICK_SEC = 0.066
COMBAT_TICK_SEC = 0.2

TICKET_COOLDOWN_FLOOR = 0.62
ATTACK_BUSY_PAD = 0.06
INVALID_TARGET_LOCK_SEC = 3.0

# Official client portal centers: (current_map, destination) -> (x, y).
PORTAL_CENTERS = {
    (1, 2): (928.0, -224.0), (1, 4): (-544.0, 928.0),
    (1, 5): (2464.0, 928.0), (1, 6): (928.0, 1952.0),
    (2, 1): (3872.0, 7616.0), (3, 6): (608.0, 608.0),
    (4, 1): (9136.0, 1568.0), (5, 1): (48.0, 2528.0),
    (6, 1): (3872.0, 96.0), (6, 3): (7264.0, 4320.0),
    (7, 1): (3872.0, 7616.0),
    (6, 8): (1376.0, 1888.0), (8, 6): (3232.0, 6240.0),
    (4, 9): (5088.0, -32.0), (9, 4): (3232.0, 6240.0),
    (2, 10): (96.0, 4704.0), (10, 2): (3232.0, 6240.0),
}
PORTAL_ARRIVE_DISTANCE = 1.0
TRAVEL_SYNC_WAIT_SEC = 0.9
TRAVEL_RESULT_TIMEOUT_SEC = 6.5
TRAVEL_RETRY_SEC = 3.0

# Human-readable server travel_result reasons.
TRAVEL_REJECT_REASONS = {
    "distance": "距離不足（還沒走到入口）",
    "too_far": "距離不足（還沒走到入口）",
    "invalid_position": "位置無效",
    "ticket": "需要入場券或特殊道具",
    "useTicket": "需要入場券或特殊道具",
    "no_ticket": "需要入場券或特殊道具",
    "cooldown": "切圖冷卻中",
    "busy": "目前無法切圖",
    "combat_locked": "戰鬥中無法切圖",
}

# Attack ticket handling
TICKET_STALL_SEC = 10.0
TICKET_WAIT_APPROACH_RATIO = 0.6
INVALID_TARGET_SKIP_SEC = 15.0
ATTACK_ERROR_TEXT = {
    "invalid_target": "目標已被伺服器判定無效（可能剛死或超出範圍），暫時跳過 15 秒",
    "attack_cooldown": "伺服器還在攻擊冷卻，保留票據稍後再試",
    "invalid_ticket": "攻擊票據無效，等伺服器發新票據",
    "ticket_expired": "攻擊票據已過期，等伺服器發新票據",
    "out_of_range": "距離太遠，伺服器拒絕攻擊",
    "combat_locked": "目前無法戰鬥（戰鬥鎖定）",
}

# Stuck watchdog (overnight self-heal)
WATCHDOG_NO_EXP_SEC = 120.0          # EXP unchanged while hunting
WATCHDOG_NO_ATTACK_SEC = 60.0        # moving near a mob with no shot and no EXP
WATCHDOG_NO_MOVE_SEC = 120.0         # position unchanged, and not resting
WATCHDOG_STALE_TARGET_SEC = 60.0     # same target, distance and HP frozen
WATCHDOG_STALE_WORLD_SEC = 300.0     # no snapshot: drop/resync, not an instant re-login
WATCHDOG_RELOGIN_MIN_SEC = 60.0     # at most one watchdog re-login per 5 min (except hard disconnect)
WATCHDOG_ESCALATE_COUNT = 3
WATCHDOG_ESCALATE_WINDOW_SEC = 900.0 # 15 min
WATCHDOG_ESCALATE_RETRY_SEC = 10.0  # then retry every 5 min
WATCHDOG_NO_PROGRESS_SEC = 300.0     # alias of WATCHDOG_NO_EXP_SEC (older tests/docs)
WATCHDOG_REPEAT_SEC = 120.0
WATCHDOG_RECOVERY_GRACE_SEC = 120.0
WEBHOOK_DEBOUNCE_SEC = 600.0

# Elites / potions
ELITE_POTION_MIN_DEFAULT = 3
AUTO_POTION_HP_RATIO = 0.5
AUTO_POTION_RETRY_SEC = 2.5
ELITE_CONTEST_RADIUS = 180.0
ELITE_VIEW_RANGE = 700.0  # default 「精英偵測範圍」; visible on screen (~600–800)
ELITE_VIEW_RANGE_MIN = 200.0
ELITE_VIEW_RANGE_MAX = 2000.0
MONSTER_THREAT_RADIUS = 150.0
PLAYER_THREAT_RADIUS = 320.0
DAMAGE_SOURCE_MEMORY_SEC = 6.0

# Kiting (archer only). Player ~167 u/s, monsters ~50-60 u/s; their hit range
# ~75, ours prepare/impact = 295/310 (wiki 320).
# Back off in a straight line until the gap is 230–260 (hold 260, near 300),
# then fire several shots in a row. Do not take tiny back-off steps or orbit
# in melee — that lets the monster keep up at ~80. If a retreat does not
# open the gap within ~2 s, or HP is falling, stop circling and either run
# straight into open space or stand and shoot. Never retreat more than ~3 s
# without firing.
KITE_SHOOT_MIN = 160.0
KITE_SHOOT_MAX = 300.0
KITE_HOLD_DIST = 200.0
KITE_RETREAT_DIST = 180.0
KITE_MIN_DIST = 230.0
MONSTER_APPROACH_SPEED = 55.0   # observed farm mobs, median ~50 max ~63
KITE_PREDICT_HORIZON_SEC = 0.4  # one archer impact window
# Server checks each sync packet independently: distance from the last accepted
# position divided by elapsed time since that accept, against MOVEMENT_SPEED.
# It is not a long-window average, so two close packets look like a speed hack.
# We cap every step at 90% of that budget and never emit a move while the
# remaining budget is tiny (throttle). After a correction, snap and pause.
MOVE_SPEED_MARGIN = 0.90
MOVE_PAUSE_AFTER_CORRECTION_SEC = 0.25
MOVE_PACKET_MAX_SEC = 0.12  # one packet never carries more travel than this
DEFAULT_PLAYER_DPS = 100.0      # ~82-148 every ~0.8s
DEFAULT_INCOMING_DPS = 22.0     # 牛頭人 melee ~21-22 / s (death log)
DEFAULT_REGEN_PER_SEC = 5.0     # observed ~5/s out of combat
KITE_RETREAT_STEP = 280.0       # long radial goal, not a short hop
KITE_BURST_SHOTS = 3
KITE_RETREAT_PROGRESS_SEC = 2.0
KITE_RETREAT_MAX_SEC = 3.0
KITE_RETREAT_PROGRESS_MIN = 30.0
# Never let a kite/orbit path come closer than this to the current target.
KITE_CLEARANCE = 120.0
# Keep kite/orbit steps this far from walls; endpoints that only sit on a walkable cell still hug edges.
KITE_WALL_CLEARANCE = 40.0
KITE_ABORT_LOCKS = 2
KITE_ABORT_CONTACT_DIST = 85.0
KITE_ABORT_CONTACTS = 4

# Over-level kiting
OVERLEVEL_STEPS = (10, 20, 30)
OVERLEVEL_DEFAULT = 20
OVERLEVEL_HP_LOSS_RATIO = 0.30
OVERLEVEL_NO_DAMAGE_SEC = 20.0
OVERLEVEL_LOCK_SEC = 60.0

# Cluster ("怪堆") orbit kiting — archers. Monsters spawn in groups of ~5.
CLUSTER_ORBIT_STEP = 160.0       # arc length per orbit re-plan
CLUSTER_FIRST_WAIT_SEC = 40.0    # wait this long for the first respawn sample
CLUSTER_WAIT_MAX_SEC = 60.0      # don't wait for clusters slower than this
CLUSTER_SITE_LOCK_SEC = 60.0
EXPLORE_EVERY_SEC = 480.0      # 每 8 分鐘探索一次沒去過的區域
EXPLORE_MAX_SEC = 180.0        # 單次探索最多走 3 分鐘
EXPLORE_CELL = 500.0
EXPLORE_FORGET_SEC = 1800.0    # 30 分鐘內去過的區域算已探索
MAX_PULL_DEFAULT = 2
MAX_PULL_CHOICES = (1, 2, 3, 4, 5)
# Leash: monsters dragged too far from home reset (walk back, regen, take 0 damage).
# Not in the protocol; start here and shrink when a reset is observed.
KITE_LEASH_DEFAULT = 450.0
KITE_LEASH_MIN = 220.0
# Open farm pockets: map -> (x, y, wall-free radius, target leash).
# Map 8: west cluster around the densest Lv40 pack; measured clearance ~264
# (the cave's most open pocket that still has good density). Oasis spots are ~400.
HUNT_ANCHORS = {
    8: (2488.0, 5112.0, 264.0, 480.0),
}
HUNT_ANCHOR_ARRIVE = 80.0
HUNT_ANCHOR_RETURN = 160.0
ZERO_DAMAGE_STREAK = 3
LEASH_RESET_LOCK_SEC = 25.0
