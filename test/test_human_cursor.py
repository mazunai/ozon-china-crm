import unittest
from src.utils.human_cursor import (
    ease_in_out,
    generate_bezier_trajectory,
    HumanCursor,
)


class TestHumanCursor(unittest.TestCase):

    def test_ease_in_out(self):
        self.assertAlmostEqual(ease_in_out(0.0), 0.0)
        self.assertAlmostEqual(ease_in_out(0.5), 0.5)
        self.assertAlmostEqual(ease_in_out(1.0), 1.0)

    def test_bezier_trajectory_endpoints(self):
        start_x, start_y = 100, 200
        end_x, end_y = 500, 600
        path = generate_bezier_trajectory(start_x, start_y, end_x, end_y, steps=20)
        
        # Длина траектории steps + 1
        self.assertEqual(len(path), 21)
        # Начальная точка совпадает
        self.assertEqual(path[0], (start_x, start_y))
        # Конечная точка совпадает
        self.assertEqual(path[-1], (end_x, end_y))

    def test_cursor_tracking(self):
        cursor = HumanCursor(initial_x=50, initial_y=80)
        self.assertEqual(cursor.x, 50)
        self.assertEqual(cursor.y, 80)


if __name__ == "__main__":
    unittest.main()
