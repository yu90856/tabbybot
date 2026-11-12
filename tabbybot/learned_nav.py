"""Collision learned from the server for maps without official navigation data.

Every `invalid_position` correction tells us the step we tried was not walkable. We mark the
grid cell just ahead of the corrected (valid) position as blocked, remember the corrected
position as walkable, persist both to ~/.tabbybot/nav_<map>.json, and A* around the blocks.
Unknown cells are assumed walkable.
"""

from __future__ import annotations

import heapq
import json
import math
import time
from pathlib import Path
from typing import Optional

CELL = 16
BLOCK_TTL_SEC = 30 * 60.0  # learned walls expire (server corrections can be noisy)
SEARCH_MARGIN = 1200  # px around start/goal box
MAX_NODES = 250000

Point = tuple[float, float]
Cell = tuple[int, int]

LEARNED_DIR: Optional[Path] = None  # tests override


def learned_dir() -> Path:
    return LEARNED_DIR if LEARNED_DIR is not None else Path.home() / ".tabbybot"


def cell_of(x: float, y: float) -> Cell:
    return (math.floor(x / CELL), math.floor(y / CELL))


def center(cell: Cell) -> Point:
    return (cell[0] * CELL + CELL / 2.0, cell[1] * CELL + CELL / 2.0)


class LearnedGrid:
    def __init__(self, map_id: int, directory: Optional[Path] = None):
        self.map_id = int(map_id)
        self.path = (directory or learned_dir()) / f"nav_{self.map_id}.json"
        self.blocked: set[Cell] = set()
        self.blocked_at: dict[Cell, float] = {}
        self.ok: set[Cell] = set()
        self._dirty = False
        self._saved_at = -1e9
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if int(data.get("cell", CELL)) != CELL:
            return
        now = time.time()
        stamps = data.get("blocked_at") or {}
        self.blocked = set()
        self.blocked_at: dict[Cell, float] = {}
        for c in data.get("blocked", []):
            if isinstance(c, list) and len(c) == 2:
                at = float(stamps.get(f"{c[0]},{c[1]}", 0.0))
                if now - at < BLOCK_TTL_SEC:
                    self.blocked.add(tuple(c))
                    self.blocked_at[tuple(c)] = at
        self.ok = {tuple(c) for c in data.get("ok", []) if isinstance(c, list) and len(c) == 2}
        self.blocked -= self.ok

    def save(self, force: bool = False) -> None:
        if not self._dirty:
            return
        now = time.monotonic()
        if not force and now - self._saved_at < 5.0:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"map": self.map_id, "cell": CELL,
                                       "blocked": sorted(list(c) for c in self.blocked),
                                       "blocked_at": {f"{c[0]},{c[1]}": self.blocked_at.get(c, time.time())
                                                      for c in self.blocked},
                                       "ok": sorted(list(c) for c in self.ok)}), encoding="utf-8")
            tmp.replace(self.path)
            self._dirty = False
            self._saved_at = now
        except OSError:
            pass

    def mark_ok(self, x: float, y: float) -> None:
        cell = cell_of(x, y)
        if cell not in self.ok:
            self.ok.add(cell)
            self.blocked.discard(cell)
            self._dirty = True

    def mark_blocked(self, x: float, y: float) -> bool:
        cell = cell_of(x, y)
        if cell in self.ok or cell in self.blocked:
            return False
        self.blocked.add(cell)
        self.blocked_at[cell] = time.time()
        self._dirty = True
        return True

    def expire(self) -> None:
        now = time.time()
        old = [c for c in self.blocked if now - self.blocked_at.get(c, now) >= BLOCK_TTL_SEC]
        for c in old:
            self.blocked.discard(c)
            self.blocked_at.pop(c, None)
        if old:
            self._dirty = True

    def clear_near(self, points: list, radius: float) -> int:
        drop = [c for c in self.blocked
                if any(math.dist(center(c), p) <= radius for p in points)]
        for c in drop:
            self.blocked.discard(c)
            self.blocked_at.pop(c, None)
        if drop:
            self._dirty = True
        return len(drop)

    def learn_rejection(self, valid: Point, toward: Point) -> int:
        """Server put us back at `valid` while we were heading to `toward`."""
        self.mark_ok(*valid)
        dx, dy = toward[0] - valid[0], toward[1] - valid[1]
        dist = math.hypot(dx, dy)
        if dist < 1e-6:
            return 0
        ux, uy = dx / dist, dy / dist
        start = cell_of(*valid)
        added = 0
        for ahead in (10.0, 18.0):  # exactly one fine cell, right where the step was refused
            px, py = valid[0] + ux * ahead, valid[1] + uy * ahead
            if cell_of(px, py) == start:
                continue
            if self.mark_blocked(px, py):
                added += 1
            break
        self.save()
        return added

    def _cost(self, cell: Cell) -> float:
        # Stay a cell away from known walls where possible.
        x, y = cell
        for nx in (x - 1, x, x + 1):
            for ny in (y - 1, y, y + 1):
                if (nx, ny) in self.blocked:
                    return 3.0
        return 1.0

    def line_clear(self, a: Point, b: Point) -> bool:
        dist = math.dist(a, b)
        steps = max(1, int(dist / (CELL / 3.0)))
        for i in range(steps + 1):
            t = i / steps
            c = cell_of(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            if c in self.blocked or self._cost(c) > 1.0 and c not in self.ok:
                return False
        return True

    def find_path(self, start: Point, goal: Point) -> list[Point]:
        """Waypoints around learned blocks. If they seal the way, forget the blocks near us and the
        straight line once and retry (corrections are noisy); [] only if still impossible."""
        self.expire()
        path = self._find_path(start, goal)
        if path:
            return path
        line = [(start[0] + (goal[0] - start[0]) * t / 10.0, start[1] + (goal[1] - start[1]) * t / 10.0)
                for t in range(11)]
        if self.clear_near([start], 250.0) + self.clear_near(line, 48.0):
            self.save(force=True)
            return self._find_path(start, goal)
        return []

    def _find_path(self, start: Point, goal: Point) -> list[Point]:
        if not self.blocked or self.line_clear(start, goal):
            return [goal]
        s, g = cell_of(*start), cell_of(*goal)
        x0 = math.floor((min(start[0], goal[0]) - SEARCH_MARGIN) / CELL)
        x1 = math.floor((max(start[0], goal[0]) + SEARCH_MARGIN) / CELL)
        y0 = math.floor((max(0.0, min(start[1], goal[1]) - SEARCH_MARGIN)) / CELL)
        y1 = math.floor((max(start[1], goal[1]) + SEARCH_MARGIN) / CELL)
        x0 = max(x0, 0)
        frontier = [(0.0, 0.0, s)]
        came: dict[Cell, Cell] = {}
        cost = {s: 0.0}
        seen = 0
        while frontier:
            _, gcost, cur = heapq.heappop(frontier)
            if cur == g:
                break
            if gcost > cost.get(cur, math.inf):
                continue
            seen += 1
            if seen > MAX_NODES:
                return []
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
                nxt = (cur[0] + dx, cur[1] + dy)
                if not (x0 <= nxt[0] <= x1 and y0 <= nxt[1] <= y1) or nxt in self.blocked:
                    continue
                if dx and dy and ((cur[0] + dx, cur[1]) in self.blocked or (cur[0], cur[1] + dy) in self.blocked):
                    continue  # no corner cutting
                step = (1.4142 if dx and dy else 1.0) * self._cost(nxt)
                ng = gcost + step
                if ng < cost.get(nxt, math.inf):
                    cost[nxt] = ng
                    came[nxt] = cur
                    h = math.hypot(nxt[0] - g[0], nxt[1] - g[1])
                    heapq.heappush(frontier, (ng + h, ng, nxt))
        if g not in came and g != s:
            return []
        cells = [g]
        while cells[-1] != s:
            cells.append(came[cells[-1]])
        cells.reverse()
        points = [center(c) for c in cells[1:-1]] + [goal]
        # Line-of-sight smoothing.
        out: list[Point] = []
        anchor = start
        i = 0
        while i < len(points):
            j = len(points) - 1
            while j > i and not self.line_clear(anchor, points[j]):
                j -= 1
            out.append(points[j])
            anchor = points[j]
            i = j + 1
        return out


_GRIDS: dict[int, LearnedGrid] = {}


def learned_grid(map_id: int) -> LearnedGrid:
    grid = _GRIDS.get(int(map_id))
    if grid is None or grid.path.parent != learned_dir():
        grid = LearnedGrid(map_id)
        _GRIDS[int(map_id)] = grid
    return grid
