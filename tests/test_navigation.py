import math
import unittest

from tabbybot.navigation import (
    MapData,
    Navigator,
    Obstacle,
    first_walkable_points,
    navigator_for,
    path_is_walkable,
)


def grid_map(width=8, height=8, blocked=(), obstacles=(), map_id=99, cell_size=64):
    cells = frozenset((x, y) for x in range(width) for y in range(height) if (x, y) not in set(blocked))
    data = MapData(map_id=map_id, cell_size=cell_size, cells=cells, obstacles=tuple(obstacles))
    return Navigator(data, clearance=8, grid_step=16)


class SyntheticNavigationTests(unittest.TestCase):
    def test_walkable_rejects_outside_and_ellipse(self):
        obstacle = Obstacle(x=200, y=200, rx=40, ry=30, name="rock")
        nav = grid_map(obstacles=(obstacle,))
        self.assertTrue(nav.walkable(32, 32))
        self.assertFalse(nav.walkable(-10, 32))
        self.assertFalse(nav.walkable(200, 200))
        # Just outside the expanded ellipse should be fine if still in a cell.
        self.assertTrue(nav.walkable(32, 200))

    def test_straight_path_when_clear(self):
        nav = grid_map()
        path = nav.find_path((32, 32), (200, 32))
        self.assertEqual(path, [(200, 32)])

    def test_astar_goes_around_obstacle(self):
        # Wall-like tall ellipse blocking the straight line at x=200 between y=0..300
        wall = Obstacle(x=200, y=160, rx=20, ry=140, name="wall")
        nav = grid_map(width=10, height=8, obstacles=(wall,))
        start, goal = (64, 160), (320, 160)
        self.assertTrue(nav.walkable(*start))
        self.assertTrue(nav.walkable(*goal))
        self.assertFalse(nav.line_clear(start, goal))
        path = nav.find_path(start, goal)
        self.assertTrue(path)
        self.assertEqual(path[-1], goal)
        self.assertTrue(path_is_walkable(nav, start, path))
        # Path should not be a single straight segment.
        self.assertGreaterEqual(len(path), 2)

    def test_sweep_order_is_deterministic(self):
        nav = grid_map(width=12, height=6)
        first = nav.sweep_points()
        second = nav.sweep_points()
        self.assertEqual(first, second)
        self.assertTrue(first)
        # Snake order: even sy increases sx, odd sy decreases sx among first row pair.
        # Just assert stability and that all points are walkable.
        for point in first:
            self.assertTrue(nav.walkable(*point))


class RealMapNavigationTests(unittest.TestCase):
    def test_town_path_between_walkable_cells(self):
        nav = navigator_for(1)
        self.assertIsNotNone(nav)
        points = first_walkable_points(1, count=12)
        self.assertGreaterEqual(len(points), 2)
        start, goal = points[0], points[-1]
        if math.dist(start, goal) < 80:
            goal = points[min(6, len(points) - 1)]
        path = nav.find_path(start, goal)
        self.assertTrue(path)
        self.assertTrue(path_is_walkable(nav, start, path))
        self.assertEqual(path[-1], goal)

    def test_snowfield_has_sweep_points(self):
        nav = navigator_for(2)
        self.assertIsNotNone(nav)
        points = nav.sweep_points()
        self.assertGreater(len(points), 3)
        start = first_walkable_points(2, count=1)[0]
        path = nav.find_sweep_path(start, 0)
        self.assertTrue(path)
        self.assertTrue(path_is_walkable(nav, start, path))


if __name__ == "__main__":
    unittest.main()
