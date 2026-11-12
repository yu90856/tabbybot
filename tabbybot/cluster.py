"""Monster cluster ("怪堆") tracking: spawn sites, membership and measured respawn time."""

from __future__ import annotations

import math
from collections import deque
from typing import Any, Iterable

CLUSTER_LINK_DIST = 300.0     # monsters closer than this belong to the same cluster
CLUSTER_MATCH_DIST = 350.0    # a new cluster this close to a known site is that site
RESPAWN_MAX_SAMPLE = 600.0    # ignore "respawns" slower than this (we probably left)
SITE_FORGET_SEC = 900.0
MAX_SITES = 60


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def link_clusters(points: list[tuple[str, float, float]], link: float = CLUSTER_LINK_DIST) -> list[list[tuple[str, float, float]]]:
    """Single-linkage grouping: points within `link` of each other share a cluster."""
    parent = list(range(len(points)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            if math.hypot(points[i][1] - points[j][1], points[i][2] - points[j][2]) <= link:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri
    groups: dict[int, list[tuple[str, float, float]]] = {}
    for i, pt in enumerate(points):
        groups.setdefault(find(i), []).append(pt)
    return list(groups.values())


class Site:
    __slots__ = ("id", "map_id", "x", "y", "spread", "members", "alive", "deaths", "respawns",
                 "last_active", "empty_since", "positions")

    def __init__(self, site_id: int, map_id: int, x: float, y: float, now: float) -> None:
        self.id = site_id
        self.map_id = map_id
        self.x = x
        self.y = y
        self.spread = 0.0
        self.members: set[str] = set()   # every encounter id ever assigned here
        self.alive: set[str] = set()
        self.deaths: deque = deque(maxlen=20)  # death times waiting for a respawn
        self.respawns: deque = deque(maxlen=12)
        self.last_active = now
        self.empty_since: float | None = None
        self.positions: dict[str, tuple[float, float]] = {}

    def respawn_avg(self) -> float | None:
        return round(_median(list(self.respawns)), 1) if self.respawns else None

    def distance(self, x: float, y: float) -> float:
        return math.hypot(self.x - x, self.y - y)


class ClusterTracker:
    def __init__(self) -> None:
        self.sites: list[Site] = []
        self.by_id: dict[str, Site] = {}
        self._next = 1
        self.map_id: int | None = None

    def reset(self) -> None:
        self.sites = []
        self.by_id = {}
        self.map_id = None

    def site(self, site_id: int | None) -> Site | None:
        for site in self.sites:
            if site.id == site_id:
                return site
        return None

    def update(self, map_id: int, living: Iterable[tuple[str, float, float]], now: float,
               engaged: set[str] | None = None) -> None:
        """`living`: (encounter_id, x, y) of every living monster on the current map."""
        if map_id != self.map_id:
            self.reset()
            self.map_id = map_id
        engaged = engaged or set()
        alive_now = {eid: (x, y) for eid, x, y in living}
        # 1) deaths: members that were alive and are now gone / dead
        for site in self.sites:
            gone = [eid for eid in site.alive if eid not in alive_now]
            for eid in gone:
                site.alive.discard(eid)
                site.positions.pop(eid, None)
                site.deaths.append(now)
            if not site.alive and site.empty_since is None and gone:
                site.empty_since = now
        # 2) known ids keep their site (an aggro'd monster chasing us stays in its site)
        fresh: list[tuple[str, float, float]] = []
        for eid, (x, y) in alive_now.items():
            site = self.by_id.get(eid)
            if site is not None:
                site.alive.add(eid)
                site.positions[eid] = (x, y)
            else:
                fresh.append((eid, x, y))
        # 3) new ids: join the nearest site, else form new sites
        leftovers = []
        for eid, x, y in fresh:
            best = min(self.sites, key=lambda s: s.distance(x, y), default=None)
            if best is not None and best.distance(x, y) <= CLUSTER_MATCH_DIST:
                self._join(best, eid, x, y, now, respawn=True)
            else:
                leftovers.append((eid, x, y))
        for group in link_clusters(leftovers):
            cx = sum(p[1] for p in group) / len(group)
            cy = sum(p[2] for p in group) / len(group)
            site = Site(self._next, map_id, cx, cy, now)
            self._next += 1
            self.sites.append(site)
            for eid, x, y in group:
                self._join(site, eid, x, y, now, respawn=False)
        # 4) centre / spread from idle members (not ones we've pulled)
        for site in self.sites:
            idle = [pos for eid, pos in site.positions.items() if eid not in engaged]
            if idle:
                cx = sum(p[0] for p in idle) / len(idle)
                cy = sum(p[1] for p in idle) / len(idle)
                site.x += (cx - site.x) * 0.3
                site.y += (cy - site.y) * 0.3
                site.spread = max(math.hypot(p[0] - site.x, p[1] - site.y) for p in idle)
            if site.alive:
                site.last_active = now
                site.empty_since = None
        self.sites = [s for s in self.sites if now - s.last_active < SITE_FORGET_SEC][-MAX_SITES:]
        live_ids = {s.id for s in self.sites}
        if len(self.by_id) > 20000 or any(s.id not in live_ids for s in self.by_id.values()):
            self.by_id = {eid: s for eid, s in self.by_id.items() if s.id in live_ids}

    def _join(self, site: Site, eid: str, x: float, y: float, now: float, *, respawn: bool) -> None:
        if respawn and site.deaths:
            gap = now - site.deaths.popleft()
            if 0 < gap <= RESPAWN_MAX_SAMPLE:
                site.respawns.append(gap)
        site.members.add(eid)
        site.alive.add(eid)
        site.positions[eid] = (x, y)
        self.by_id[eid] = site

    def payload(self, current_id: int | None) -> list[dict[str, Any]]:
        return [{
            "id": s.id, "x": round(s.x, 1), "y": round(s.y, 1), "r": round(max(s.spread, 60.0), 1),
            "alive": len(s.alive), "respawn": s.respawn_avg(), "samples": len(s.respawns),
            "current": s.id == current_id,
        } for s in self.sites]
