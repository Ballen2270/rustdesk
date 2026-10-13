"""Staggered layouts and fail-closed pixel boundary regressions."""
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from window_geometry import measure_title_windows
from window_targets import discover_windows


def desktop(layout=None, size=(1920, 1080), title_color='white'):
    image = Image.new('RGB', size, (15, 45, 130))
    draw = ImageDraw.Draw(image)
    rects, titles = [], []
    if layout is None:
        layout = ((10, 10, 620), (650, 10, 620), (1300, 220, 600),
                  (10, 530, 620), (650, 530, 620))
    for window in layout:
        x, y, width = window[:3]
        height = window[3] if len(window) == 4 else width*3//4
        start, end = y+30, y+30+height
        draw.rectangle((x, y, x+width-1, start-1), fill=title_color)
        draw.rectangle((x, start, x+width-1, end-1), fill=(190, 160, 95))
        rects.append([x, start, width, end-start])
        titles.append(dict(text='梦幻西游：时空', score=1.,
                           x=(x+20)/size[0], y=(y+8)/size[1],
                           w=100/size[0], h=14/size[1]))
    return image, titles, rects


class GeometryTests(unittest.TestCase):
    def test_each_column_uses_its_own_bottom(self):
        image, titles, expected = desktop()
        self.assertEqual(measure_title_windows(image, titles), expected)

    def test_moving_right_window_does_not_cut_left_windows(self):
        image, titles, expected = desktop()
        draw = ImageDraw.Draw(image)
        draw.rectangle((1300, 220, 1899, 699), fill=(15, 45, 130))
        draw.rectangle((1300, 350, 1899, 379), fill='white')
        draw.rectangle((1300, 380, 1899, 829), fill=(190, 160, 95))
        titles[2]['y'] += 130/1080
        expected[2][1] += 130
        self.assertEqual(measure_title_windows(image, titles), expected)

    def test_missing_bottom_edge_is_not_inferred_from_aspect_ratio(self):
        image, titles, _ = desktop()
        ImageDraw.Draw(image).rectangle((1300, 250, 1899, 1079), fill=(190, 160, 95))
        self.assertIsNone(measure_title_windows(image, titles))

    def test_stronger_hud_edge_does_not_replace_outer_border(self):
        image, titles, expected = desktop()
        draw = ImageDraw.Draw(image)
        for x, start, width, height in expected:
            end = start+height
            draw.rectangle((x, end-12, x+width-1, end-7), fill='white')
        self.assertEqual(measure_title_windows(image, titles), expected)

    def test_unsupported_title_bar_keeps_legacy_fallback(self):
        image, titles, _ = desktop()
        ImageDraw.Draw(image).rectangle((1300, 220, 1899, 249), fill=(100, 150, 210))
        self.assertIsNone(measure_title_windows(image, titles))

    def test_layouts_do_not_assume_count_or_side(self):
        layouts = (
            ((900, 250, 640),),
            ((20, 20, 600), (1100, 450, 700)),
            ((300, 20, 500), (300, 480, 600)),
            ((20, 50, 500), (620, 100, 600), (1350, 350, 500)),
            tuple((20+col*620, 20+row*510, 600) for row in range(2) for col in range(3)),
            ((20, 250, 600), (650, 10, 620), (1290, 10, 620),
             (650, 530, 620), (1290, 530, 620)),
            tuple((20+col*480, 20+row*400, 450) for row in range(2) for col in range(5)),
            ((20, 20, 450, 300), (650, 200, 600, 500), (1400, 20, 650, 440)),
        )
        for layout in layouts:
            for color in ('white', (35, 35, 35)):
                with self.subTest(layout=layout, title_color=color):
                    image, titles, expected = desktop(layout, (2560, 1440), color)
                    self.assertEqual(measure_title_windows(image, list(reversed(titles))),
                                     list(reversed(expected)))

    def test_recovery_requires_independent_game_anchors(self):
        image, titles, expected = desktop()
        detector = lambda *args: ([[10, 40, 620, 180]], titles[:4])
        game = [dict(text=t, score=1., x=.2+i*.1, y=.04, w=.06, h=.04)
                for i, t in enumerate(('指引', '排行'))]
        with patch('window_targets.layout_detector', return_value=detector):
            with patch('window_targets.region_ocr', side_effect=[titles]+[game]*5):
                inventory = discover_windows(image, 'pc1')
                self.assertEqual([w['rect'] for w in inventory['windows']],
                                 sorted(expected, key=lambda r: (r[1], r[0])))
            with patch('window_targets.region_ocr', side_effect=[titles, []]):
                with self.assertRaisesRegex(ValueError, 'independent'):
                    discover_windows(image, 'pc1')

    def test_zoom_does_not_discard_titles_read_at_native_scale(self):
        image, titles, _ = desktop()
        game = [dict(text=t, score=1., x=.2+i*.1, y=.04, w=.06, h=.04)
                for i, t in enumerate(('活动', '排行'))]
        detector = lambda *args: ([[10, 40, 620, 180]], titles)
        with patch('window_targets.layout_detector', return_value=detector):
            with patch('window_targets.region_ocr', side_effect=[titles[:4]]+[game]*5):
                self.assertEqual(len(discover_windows(image, 'pc1')['windows']), 5)

    def test_legacy_row_overlap_error_can_recover_independent_windows(self):
        image, titles, expected = desktop()
        game = [dict(text=t, score=1., x=.2+i*.1, y=.04, w=.06, h=.04)
                for i, t in enumerate(('活动', '排行'))]
        def detector(*args):
            raise SystemExit('windows overlap')

        with patch('window_targets.layout_detector', return_value=detector):
            with patch('window_targets.region_ocr', side_effect=[titles]+[game]*5):
                self.assertEqual([w['rect'] for w in discover_windows(image, 'pc1')['windows']],
                                 sorted(expected, key=lambda r: (r[1], r[0])))

    def test_native_and_zoom_title_duplicates_are_one_window(self):
        image, titles, expected = desktop()
        zoomed = [dict(t, y=t['y']+1/1080, h=t['h']-1/1080) for t in titles]
        self.assertEqual(measure_title_windows(image, titles+zoomed), expected)

    def test_plausible_legacy_geometry_is_checked_against_own_edges(self):
        image, titles, expected = desktop()
        incorrect = [list(r) for r in expected]
        incorrect[0][3] -= 20
        game = [dict(text=t, score=1., x=.2+i*.1, y=.04, w=.06, h=.04)
                for i, t in enumerate(('活动', '排行'))]
        with patch('window_targets.layout_detector', return_value=lambda *args: (incorrect, titles)):
            with patch('window_targets.region_ocr', side_effect=[titles]+[game]*5):
                self.assertEqual([w['rect'] for w in discover_windows(image, 'pc1')['windows']],
                                 sorted(expected, key=lambda r: (r[1], r[0])))


if __name__ == '__main__':
    unittest.main()
