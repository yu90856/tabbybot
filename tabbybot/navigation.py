"""Grid pathfinding and patrol sweep points from official navigation JSON."""

from __future__ import annotations

import heapq
import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from tabbybot.constants import (
    MAP_FILES,
    PLAYER_CLEARANCE,
    RETURN_PORTAL,
    RETURN_PORTAL_MAPS,
    RETURN_PORTAL_RADIUS,
    SWEEP_SECTOR,
)


class NavigationError(Exception):
    pass


Point = tuple[float, float]
Cell = tuple[int, int]


def maps_directory() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "maps"


@dataclass(frozen=True)
class Obstacle:
    x: float
    y: float
    rx: float
    ry: float
    name: str = ""


@dataclass(frozen=True)
class MapData:
    map_id: int
    cell_size: int
    cells: frozenset[Cell]
    obstacles: tuple[Obstacle, ...]


def map_data_from_dict(payload: dict, map_id: int = 0) -> MapData:
    if not isinstance(payload, dict):
        raise NavigationError("導航資料必須是物件")
    cell_size = int(payload.get("cell_size", 64))
    cells = frozenset(tuple(cell) for cell in payload.get("cells", []))
    obstacles = []
    for raw in payload.get("obstacles", []):
        if not isinstance(raw, dict):
            continue
        obstacles.append(Obstacle(
            x=float(raw["x"]),
            y=float(raw["y"]),
            rx=float(raw["rx"]),
            ry=float(raw["ry"]),
            name=str(raw.get("name", "")),
        ))
    return MapData(map_id=map_id, cell_size=cell_size, cells=cells, obstacles=tuple(obstacles))


def load_map_file(path: Path, map_id: int = 0) -> MapData:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NavigationError(f"無法讀取導航檔 {path}") from exc
    return map_data_from_dict(payload, map_id)


EDGE_CLEARANCE = 14


class Navigator:
    def __init__(self, data: MapData, clearance: float = PLAYER_CLEARANCE, grid_step: int | None = None):
        self.data = data
        self.clearance = clearance
        self.grid_step = grid_step if grid_step is not None else (16 if data.map_id == 1 else 32)
        self._bins = self._build_bins()

    def _build_bins(self) -> dict[Cell, list[Obstacle]]:
        bins: dict[Cell, list[Obstacle]] = {}
        for obstacle in self.data.obstacles:
            rx = obstacle.rx + self.clearance
            ry = obstacle.ry + self.clearance
            x0 = math.floor((obstacle.x - rx) / 128)
            x1 = math.floor((obstacle.x + rx) / 128)
            y0 = math.floor((obstacle.y - ry) / 128)
            y1 = math.floor((obstacle.y + ry) / 128)
            for bx in range(x0, x1 + 1):
                for by in range(y0, y1 + 1):
                    bins.setdefault((bx, by), []).append(obstacle)
        return bins

    # Optional extra blocker (learned server rejections), set by the bot: f(x, y) -> True if blocked.
    extra_blocked = None

    def walkable(self, x: float, y: float) -> bool:
        cell = (math.floor(x / self.data.cell_size), math.floor(y / self.data.cell_size))
        if cell not in self.data.cells:
            return False
        if self.extra_blocked is not None and self.extra_blocked(x, y):
            return False
        bucket = self._bins.get((math.floor(x / 128), math.floor(y / 128)), ())
        for obstacle in bucket:
            rx = obstacle.rx + self.clearance
            ry = obstacle.ry + self.clearance
            dx = x - obstacle.x
            dy = y - obstacle.y
            if abs(dx) < rx and abs(dy) < ry and (dx / rx) ** 2 + (dy / ry) ** 2 < 1:
                return False
        return True

    def line_clear(self, start: Point, end: Point) -> bool:
        distance = math.dist(start, end)
        steps = max(1, math.ceil(distance / 4))
        x0, y0 = start
        x1, y1 = end
        for index in range(steps + 1):
            t = index / steps
            x, y = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
            if not self.walkable(x, y):
                return False
            # Keep the body off wall/cliff edges mid-segment (the server collides the player's shape,
            # not its centre); the end points may still hug a wall.
            if EDGE_CLEARANCE and distance * t > 20 and distance * (1 - t) > 20 and not self._edge_room(x, y):
                return False
        return True

    def _edge_room(self, x: float, y: float) -> bool:
        cells = self.data.cells
        size = self.data.cell_size
        c = EDGE_CLEARANCE
        for px, py in ((x - c, y), (x + c, y), (x, y - c), (x, y + c)):
            if (math.floor(px / size), math.floor(py / size)) not in cells:
                return False
        return True

    def wall_distance(self, x: float, y: float, limit: float = 80.0) -> float:
        """Approximate distance to the nearest unwalkable point, capped at `limit`. 0 if not walkable."""
        if not self.walkable(x, y):
            return 0.0
        best = float(limit)
        step = 4.0
        rays = 16
        n = max(1, int(limit / step))
        for i in range(rays):
            ang = i * (2 * math.pi / rays)
            dx, dy = math.cos(ang), math.sin(ang)
            for k in range(1, n + 1):
                dist = k * step
                if not self.walkable(x + dx * dist, y + dy * dist):
                    best = min(best, dist)
                    break
            if best <= step:
                return best
        return best

    def segment_clearance(self, start: Point, end: Point, limit: float = 80.0, sample: float = 8.0) -> float:
        """Minimum wall_distance along the segment, or 0 if the line is blocked."""
        if not self.line_clear(start, end):
            return 0.0
        distance = math.dist(start, end)
        steps = max(1, math.ceil(distance / max(sample, 1.0)))
        best = min(self.wall_distance(*start, limit), self.wall_distance(*end, limit))
        for i in range(1, steps):
            t = i / steps
            x = start[0] + (end[0] - start[0]) * t
            y = start[1] + (end[1] - start[1]) * t
            best = min(best, self.wall_distance(x, y, limit))
            if best <= 0:
                return 0.0
        return best

    def _nearest_grid(self, point: Point) -> Cell | None:
        step = self.grid_step
        gx, gy = round(point[0] / step), round(point[1] / step)
        for radius in range(5):
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    if max(abs(dx), abs(dy)) != radius:
                        continue
                    node = (gx + dx, gy + dy)
                    world = (node[0] * step, node[1] * step)
                    if self.walkable(*world) and self.line_clear(point, world):
                        return node
        return None

    def find_path(self, start: Point, goal: Point, max_distance: float | None = None) -> list[Point]:
        if max_distance is not None and math.dist(start, goal) > max_distance:
            return []
        # Server-corrected positions often sit a pixel past a cell edge: snap to the nearest walkable spot.
        snapped_start = self.nearest_walkable(start, 72.0)
        snapped_goal = self.nearest_walkable(goal, 96.0)
        if snapped_start is None or snapped_goal is None:
            return []
        if snapped_start != start:
            rest = self.find_path(snapped_start, snapped_goal, max_distance)
            return [snapped_start] + rest if rest or snapped_start == snapped_goal else []
        goal = snapped_goal
        if self.line_clear(start, goal):
            return [goal]
        first = self._nearest_grid(start)
        last = self._nearest_grid(goal)
        if first is None or last is None:
            return []
        step = self.grid_step
        frontier: list[tuple[float, Cell]] = [(0.0, first)]
        costs = {first: 0.0}
        parents: dict[Cell, Cell | None] = {first: None}
        directions = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy]
        while frontier:
            _, current = heapq.heappop(frontier)
            if current == last:
                break
            for dx, dy in directions:
                neighbor = (current[0] + dx, current[1] + dy)
                world = (neighbor[0] * step, neighbor[1] * step)
                if max_distance is not None and math.dist(world, start) > max_distance + 150:
                    continue
                current_world = (current[0] * step, current[1] * step)
                if EDGE_CLEARANCE and neighbor != last and not self._edge_room(*world):
                    continue
                if not self.line_clear(current_world, world):
                    continue
                cost = costs[current] + math.hypot(dx, dy)
                if cost >= costs.get(neighbor, math.inf):
                    continue
                costs[neighbor] = cost
                parents[neighbor] = current
                heapq.heappush(frontier, (cost + math.dist(neighbor, last), neighbor))
        if last not in parents:
            return []
        nodes: list[Point] = []
        node: Cell | None = last
        while node is not None:
            nodes.append((node[0] * step, node[1] * step))
            node = parents[node]
        nodes.reverse()
        points = [start, *nodes, goal]
        return self._simplify(points)

    def _simplify(self, points: list[Point]) -> list[Point]:
        if len(points) < 2:
            return points[1:] if points else []
        result: list[Point] = []
        index = 0
        while index < len(points) - 1:
            farthest = index + 1
            for candidate in range(len(points) - 1, farthest, -1):
                if self.line_clear(points[index], points[candidate]):
                    farthest = candidate
                    break
            result.append(points[farthest])
            index = farthest
        return result

    def nearest_walkable(self, point: Point, radius: float) -> Point | None:
        if self.walkable(*point):
            return point
        best = None
        for r in range(8, int(radius) + 1, 8):
            for k in range(16):
                a = k * math.pi / 8
                cand = (point[0] + r * math.cos(a), point[1] + r * math.sin(a))
                if self.walkable(*cand) and (best is None or math.dist(cand, point) < math.dist(best, point)):
                    best = cand
            if best is not None:
                return best
        return None

    def sweep_points(self) -> list[Point]:
        sectors: dict[Cell, list[Point]] = {}
        size = self.data.cell_size
        for cx, cy in sorted(self.data.cells):
            point = (cx * size + size / 2, cy * size + size / 2)
            if not self.walkable(*point):
                continue
            key = (cx // SWEEP_SECTOR, cy // SWEEP_SECTOR)
            sectors.setdefault(key, []).append(point)
        points: list[Point] = []
        ordered = sorted(sectors.items(), key=lambda row: (row[0][1], row[0][0] * (-1 if row[0][1] % 2 else 1)))
        for (sx, sy), candidates in ordered:
            center = (sx * (SWEEP_SECTOR * size) + (SWEEP_SECTOR * size) / 2,
                      sy * (SWEEP_SECTOR * size) + (SWEEP_SECTOR * size) / 2)
            point = min(candidates, key=lambda item: math.dist(item, center))
            if self.data.map_id in RETURN_PORTAL_MAPS and math.dist(point, RETURN_PORTAL) < RETURN_PORTAL_RADIUS:
                continue
            points.append(point)
        return points

    def find_sweep_path(self, start: Point, index: int) -> list[Point]:
        points = self.sweep_points()
        if not points:
            return []
        goal = points[index % len(points)]
        return self.find_path(start, goal)


@lru_cache(maxsize=8)
def navigator_for(map_id: int) -> Navigator | None:
    filename = MAP_FILES.get(map_id)
    if not filename:
        return None
    path = maps_directory() / filename
    if not path.is_file():
        return None
    nav = Navigator(load_map_file(path, map_id))

    def _learned(x: float, y: float, _map: int = map_id) -> bool:
        from tabbybot import learned_nav
        grid = learned_nav._GRIDS.get(_map)
        return bool(grid is not None and grid.blocked and learned_nav.cell_of(x, y) in grid.blocked)

    nav.extra_blocked = _learned
    return nav


def find_map_path(map_id: int, start: Point, goal: Point) -> list[Point]:
    nav = navigator_for(map_id)
    if nav is None:
        return []
    return nav.find_path(start, goal)


def find_sweep_path(map_id: int, start: Point, index: int) -> list[Point]:
    nav = navigator_for(map_id)
    if nav is None:
        return []
    return nav.find_sweep_path(start, index)


def has_navigation(map_id: int) -> bool:
    return navigator_for(map_id) is not None


def first_walkable_points(map_id: int, count: int = 8) -> list[Point]:
    nav = navigator_for(map_id)
    if nav is None:
        return []
    found: list[Point] = []
    size = nav.data.cell_size
    for cx, cy in sorted(nav.data.cells):
        point = (cx * size + size / 2, cy * size + size / 2)
        if nav.walkable(*point):
            found.append(point)
            if len(found) >= count:
                break
    return found


def path_is_walkable(nav: Navigator, start: Point, path: Iterable[Point]) -> bool:
    cursor = start
    for point in path:
        if not nav.line_clear(cursor, point):
            return False
        cursor = point
    return True
