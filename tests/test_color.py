import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import hue_core

class ColorTests(unittest.TestCase):
    def test_black_keeps_alpha_brightness_without_switching_power(self):
        body = hue_core.color_body((0, 0, 0, 1), 1)
        self.assertNotIn('on', body)
        self.assertNotIn('color', body)
        self.assertEqual(body['dimming']['brightness'], 100)

    def test_alpha_alone_sets_brightness(self):
        bright = hue_core.color_body((1, 0, 0, 1), 1)
        dark = hue_core.color_body((.2, 0, 0, 1), 1)
        half = hue_core.color_body((1, 0, 0, .5), 1)
        self.assertEqual(bright['dimming']['brightness'], 100)
        self.assertEqual(dark['dimming']['brightness'], 100)
        self.assertEqual(half['dimming']['brightness'], 50)
        self.assertEqual(bright['dynamics']['duration'], 1000)

    def test_invalid_input_rejected(self):
        with self.assertRaises(ValueError):
            hue_core.color_body((float('nan'), 0, 0, 1), 1)

    def test_gamut_clipped_inside_triangle(self):
        gamut = {'red': {'x': .6, 'y': .3}, 'green': {'x': .3, 'y': .6}, 'blue': {'x': .1, 'y': .1}}
        b = hue_core.color_body((1, 0, 0, 1), 1, gamut=gamut)
        xy = b['color']['xy']
        self.assertLessEqual(xy['x'], .6)
        self.assertGreaterEqual(xy['y'], .1)

if __name__ == '__main__':
    unittest.main()
