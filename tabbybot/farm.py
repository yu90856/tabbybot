"""Pick a farm map from observed monster levels. Never auto-enter special maps."""

from __future__ import annotations

from typing import Any, Iterable, Mapping, MutableMapping, Sequence

from tabbybot.constants import MAP_NAMES, SPECIAL_ENTRY_MAPS

# Town (1) is not a grinding map. Maps 5 and 7 need special entry items.
FARMABLE_MAPS = frozenset({2, 3, 4, 6, 8, 9, 10})
DEFAULT_FARM_MAP = 2
DEFAULT_FARM_LEVEL = 30
MAX_LEVEL_SAMPLES = 200
NEAR_LEVEL_TOLERANCE = 5

# Mapping evidence (no official per-map spawn table exists in the reference
# repo, navigation JSONs, or public docs):
# - The published desktop client defaults farmAfterQuestsMap to 2 (北部雪地).
# - tabbybot's default hunt.map_id is also 2, which has navigation data.
# - When live monster levels have been observed, choose_farm_map() prefers
#   the farmable map whose median observed level is closest to the target.
# - With no observations, stay on the current farmable map if already in one,
#   otherwise fall back to map 2.

def is_farmable_map(map_id: int | None) -> bool:
    try:
        return int(map_id) in FARMABLE_MAPS  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def remember_monster_level(
    catalog: MutableMapping[int, list[int]],
    map_id: int | None,
    level: int | None,
    *,
    limit: int = MAX_LEVEL_SAMPLES,
) -> None:
    if level is None or not is_farmable_map(map_id):
        return
    try:
        value = int(level)
        mid = int(map_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return
    if value < 1:
        return
    rows = catalog.setdefault(mid, [])
    rows.append(value)
    if len(rows) > limit:
        del rows[: len(rows) - limit]


def _median(levels: Sequence[int]) -> int:
    ordered = sorted(levels)
    return ordered[len(ordered) // 2]


def map_level_stats(levels: Iterable[int]) -> dict[str, int] | None:
    rows = [int(item) for item in levels]
    if not rows:
        return None
    return {
        "count": len(rows),
        "min": min(rows),
        "max": max(rows),
        "median": _median(rows),
    }


def map_depths() -> dict[int, int]:
    """Hops from town for every farmable map (deeper maps are generally harder).

    Derived from the official portal graph (MAP_ROUTES, mirrored from the
    reference client); special-entry maps are never used as stepping stones.
    """
    from tabbybot.constants import MAP_ROUTES

    depth = {1: 0}
    queue = [1]
    while queue:
        current = queue.pop(0)
        for nxt in MAP_ROUTES.get(current, ()):
            if nxt in depth or nxt in SPECIAL_ENTRY_MAPS:
                continue
            depth[nxt] = depth[current] + 1
            queue.append(nxt)
    return {map_id: value for map_id, value in depth.items() if is_farmable_map(map_id)}


def map_hops(start: int | None) -> dict[int, int]:
    """Portal hops from start to every map (special-entry maps are not passed through)."""
    from tabbybot.constants import MAP_ROUTES

    if start is None:
        return {}
    dist = {int(start): 0}
    queue = [int(start)]
    while queue:
        cur = queue.pop(0)
        for nxt in MAP_ROUTES.get(cur, ()):
            if nxt in dist or nxt in SPECIAL_ENTRY_MAPS:
                continue
            dist[nxt] = dist[cur] + 1
            queue.append(nxt)
    return dist


def nav_reachable(start: int | None, goal: int) -> bool:
    """goal reachable from start passing only through maps with official navigation data."""
    from tabbybot.constants import MAP_FILES, MAP_ROUTES

    if start is None or start == goal:
        return True
    if int(start) not in MAP_FILES and int(start) != 1:
        return False  # walking to any portal here is itself unreliable
    seen = {int(start)}
    queue = [int(start)]
    while queue:
        cur = queue.pop(0)
        if cur != int(start) and cur not in MAP_FILES:
            continue  # can't walk across a map without nav data reliably
        for nxt in MAP_ROUTES.get(cur, ()):
            if nxt == goal:
                return True
            if nxt in seen or nxt in SPECIAL_ENTRY_MAPS:
                continue
            seen.add(nxt)
            queue.append(nxt)
    return False


SPOT_MERGE_DIST = 350.0
MAX_SPOTS_PER_BAND = 40


def _band(level: int) -> int:
    return max(10, (int(level) // 10) * 10)


def _clean_levels(rows: Iterable[Any] | None) -> list[int]:
    levels = []
    for item in rows or []:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value >= 1:
            levels.append(value)
    return levels


def choose_farm_map_with_reason(
    target_level: int,
    catalog: Mapping[int, Sequence[int]] | None = None,
    current_map: int | None = None,
) -> tuple[int, str]:
    """Return (map_id, Chinese reason) for the farmable map best matching target_level."""
    try:
        target = int(target_level)
    except (TypeError, ValueError):
        target = DEFAULT_FARM_LEVEL
    if target < 1:
        target = DEFAULT_FARM_LEVEL
    depths = map_depths()
    stats: dict[int, dict[str, int]] = {}
    for map_id, rows in (catalog or {}).items():
        try:
            mid = int(map_id)
        except (TypeError, ValueError):
            continue
        if not is_farmable_map(mid):
            continue
        st = map_level_stats(_clean_levels(rows))
        if st is not None:
            stats[mid] = st
    unknown = sorted((mid for mid in depths if mid not in stats), key=lambda mid: (depths[mid], mid))

    def near(mid: int) -> int:
        return min(abs(level - target) for level in _clean_levels((catalog or {}).get(mid, [])) or [10**6])

    # Every map spawns a spread of levels; stay put if the current map already
    # has monsters close to the target level.
    if current_map in stats and near(int(current_map)) <= NEAR_LEVEL_TOLERANCE:
        st = stats[int(current_map)]
        return int(current_map), f"目前地圖 {farm_map_label(int(current_map))} 有接近 Lv{target} 的怪（實測 Lv{st['min']}–{st['max']}），留在這裡"
    close = [mid for mid in stats if near(mid) <= NEAR_LEVEL_TOLERANCE]
    if close:
        # Prefer maps we can reach without crossing a map that has no navigation data.
        best = min(close, key=lambda mid: (near(mid), 0 if nav_reachable(current_map, mid) else 1,
                                           -sum(1 for lv in (catalog or {}).get(mid, []) if abs(int(lv) - target) <= NEAR_LEVEL_TOLERANCE),
                                           depths.get(mid, 9), mid))
        st = stats[best]
        return best, f"{farm_map_label(best)} 有接近 Lv{target} 的怪（實測 Lv{st['min']}–{st['max']}）"
    covering = [mid for mid, st in stats.items() if st["min"] <= target <= st["max"]]
    if covering:
        best = min(covering, key=lambda mid: (abs(stats[mid]["median"] - target), 0 if mid == current_map else 1, mid))
        st = stats[best]
        return best, f"{farm_map_label(best)} 實測怪物 Lv{st['min']}–{st['max']}，涵蓋目標 Lv{target}"

    if stats and unknown and max(st["max"] for st in stats.values()) >= target:
        # Recorded maps don't have this level: explore the closest map we haven't recorded yet
        # (shallow maps first for Lv<=30, deeper ones for higher targets).
        hops = map_hops(current_map)
        tier = 1 if target <= 30 else 2
        choice = min(unknown, key=lambda mid: (abs(depths[mid] - tier) if target <= 30 else -depths[mid],
                                               hops.get(mid, 99), mid))
        known = "、".join(f"{farm_map_label(mid)} Lv{st['min']}–{st['max']}" for mid, st in sorted(stats.items()))
        return choice, f"已記錄的地圖都沒有 Lv{target}（{known}）；去還沒記錄的 {farm_map_label(choice)} 找"

    if target <= 30 and not stats:
        # Nothing recorded yet anywhere; shallow maps usually have Lv10–30.
        if is_farmable_map(current_map):
            return int(current_map), f"每張圖都有 Lv10–30 的怪，在目前的 {farm_map_label(int(current_map))} 找 Lv{target}"
        shallow = sorted(depths, key=lambda mid: (depths[mid], mid))
        if shallow:
            return shallow[0], f"每張圖都有 Lv10–30 的怪，先去 {farm_map_label(shallow[0])} 找 Lv{target}"

    highest = max((st["max"] for st in stats.values()), default=None)
    if unknown and (highest is None or highest < target):
        # Known maps are all too weak (or nothing known yet): try the nearest
        # unexplored map that is deeper than the explored weak ones.
        weak_depth = max((depths.get(mid, 0) for mid in stats), default=0)
        deeper = [mid for mid in unknown if depths[mid] > weak_depth] or unknown
        if highest is None:
            # Nothing observed. Every map spawns Lv10/20/30, deeper ones add 40/50+.
            if is_farmable_map(current_map) and (target <= 30 or depths.get(int(current_map), 0) >= 2):
                return int(current_map), f"還沒有實測資料；每張圖都有 Lv10–30 的怪，先在目前的 {farm_map_label(int(current_map))} 找"
            tier = 1 if target <= 30 else 2
            pool = [mid for mid in unknown if depths[mid] == tier] or unknown
            choice = pool[0]
            return choice, (f"還沒有實測等級資料；依地圖深度（離城 {depths[choice]} 站）先去 {farm_map_label(choice)} 探索")
        choice = deeper[0]
        return choice, (f"已知地圖最高只有 Lv{highest}，低於目標 Lv{target}；"
                        f"改去還沒探索、離城較遠的 {farm_map_label(choice)}（離城 {depths[choice]} 站）")

    if stats:
        best = min(stats, key=lambda mid: (abs(stats[mid]["median"] - target), 0 if mid == current_map else 1, mid))
        st = stats[best]
        return best, f"{farm_map_label(best)} 實測 Lv{st['min']}–{st['max']}，是已知最接近目標 Lv{target} 的地圖"
    return DEFAULT_FARM_MAP, "沒有任何地圖資料，先去北部雪地"


def choose_farm_map(
    target_level: int,
    catalog: Mapping[int, Sequence[int]] | None = None,
    current_map: int | None = None,
) -> int:
    """Return the farmable map that best matches target_level."""
    return choose_farm_map_with_reason(target_level, catalog, current_map)[0]


class MapLevelStore:
    """Persist observed per-map monster levels (one sample per encounter)."""

    def __init__(self, path: Any = None) -> None:
        from pathlib import Path

        self.path = Path(path) if path is not None else None
        self.catalog: dict[int, list[int]] = {}
        # Where each level band spawns: {map: {band: [[x, y, count], ...]}}
        self.spots: dict[int, dict[int, list[list[float]]]] = {}
        self.seen: set[str] = set()
        self.dirty = False
        self.saved_at = 0.0
        self.load()

    def load(self) -> None:
        import json

        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        for key, row in (data.get("maps") or {}).items() if isinstance(data, dict) else []:
            try:
                mid = int(key)
            except (TypeError, ValueError):
                continue
            levels = _clean_levels(row.get("samples") if isinstance(row, dict) else row)
            if levels and is_farmable_map(mid):
                self.catalog[mid] = levels[-MAX_LEVEL_SAMPLES:]
        for key, bands in (data.get("spots") or {}).items() if isinstance(data, dict) else []:
            try:
                mid = int(key)
                self.spots[mid] = {int(b): [[float(r[0]), float(r[1]), float(r[2])] for r in rows if len(r) >= 3]
                                   for b, rows in (bands or {}).items()}
            except (TypeError, ValueError, AttributeError):
                continue

    def observe(self, encounter_id: str, map_id: int | None, level: int | None,
                x: float | None = None, y: float | None = None) -> None:
        if not encounter_id or encounter_id in self.seen or level is None or not is_farmable_map(map_id):
            return
        self.seen.add(encounter_id)
        if len(self.seen) > 20000:
            self.seen.clear()
        remember_monster_level(self.catalog, map_id, level)
        if x is not None and y is not None:
            self.add_spot(int(map_id), int(level), float(x), float(y))
        self.dirty = True

    def add_spot(self, map_id: int, level: int, x: float, y: float) -> None:
        rows = self.spots.setdefault(map_id, {}).setdefault(_band(level), [])
        for row in rows:
            if ((row[0] - x) ** 2 + (row[1] - y) ** 2) ** 0.5 <= SPOT_MERGE_DIST:
                n = row[2]
                row[0] = round((row[0] * n + x) / (n + 1), 1)
                row[1] = round((row[1] * n + y) / (n + 1), 1)
                row[2] = min(n + 1, 999)
                return
        rows.append([round(x, 1), round(y, 1), 1])
        if len(rows) > MAX_SPOTS_PER_BAND:
            rows.sort(key=lambda r: -r[2])
            del rows[MAX_SPOTS_PER_BAND:]

    def spots_for(self, map_id: int, level: int) -> list[tuple[float, float, int]]:
        return [(r[0], r[1], int(r[2])) for r in self.spots.get(int(map_id), {}).get(_band(level), [])]

    def save(self, now: float, *, force: bool = False, interval: float = 30.0) -> None:
        import json
        import time as _time

        if self.path is None or not self.dirty or (not force and now - self.saved_at < interval):
            return
        maps = {}
        for mid, levels in sorted(self.catalog.items()):
            st = map_level_stats(levels)
            if st is None:
                continue
            maps[str(mid)] = {"name": farm_map_label(mid), **st, "samples": levels}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            spots = {str(mid): {str(b): rows for b, rows in sorted(bands.items())}
                     for mid, bands in sorted(self.spots.items())}
            tmp.write_text(json.dumps({"updated": _time.strftime("%Y-%m-%d %H:%M:%S"), "maps": maps, "spots": spots},
                                      ensure_ascii=False, indent=1), "utf-8")
            tmp.replace(self.path)
            self.dirty = False
            self.saved_at = now
        except OSError:
            pass

    def table(self) -> list[dict[str, Any]]:
        depths = map_depths()
        rows = []
        for mid in sorted(depths, key=lambda m: (depths[m], m)):
            st = map_level_stats(self.catalog.get(mid, []))
            rows.append({"map_id": mid, "name": farm_map_label(mid), "depth": depths[mid],
                         "min": st["min"] if st else None, "max": st["max"] if st else None,
                         "median": st["median"] if st else None, "count": st["count"] if st else 0})
        return rows


def farm_map_label(map_id: int) -> str:
    return MAP_NAMES.get(map_id, str(map_id))


def level_sort_key(level: int | None, target_level: int, distance: float) -> tuple[int, int, float]:
    """Prefer monsters near the target level, then nearer distance."""
    if level is None:
        return (1, 0, distance)
    return (0, abs(int(level) - int(target_level)), distance)


def assert_not_special(map_id: int) -> bool:
    return map_id not in SPECIAL_ENTRY_MAPS


def farm_status_text(
    map_id: int,
    target_level: int,
    *,
    waiting_delivery: bool = False,
    title: str = "",
) -> str:
    where = farm_map_label(map_id)
    if waiting_delivery:
        quest = f"「{title}」" if title else "任務"
        return f"{quest}等待你確認交付，同時前往{where}練功（目標 {target_level} 級）。"
    return f"沒有可自動進行的任務，前往{where}練功（目標 {target_level} 級）。"
