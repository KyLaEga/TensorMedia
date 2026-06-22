import unittest
from core.services.auto_selector import calculate_smart_score

class TestSmartScore(unittest.TestCase):
    def test_size_impact(self):
        item_small = {'size': 1024 * 1024} # 1 MB
        item_large = {'size': 10 * 1024 * 1024} # 10 MB
        self.assertGreater(calculate_smart_score(item_large), calculate_smart_score(item_small))

    def test_resolution_impact(self):
        item_low = {'resolution': '640x480'}
        item_high = {'resolution': '1920x1080'}
        self.assertGreater(calculate_smart_score(item_high), calculate_smart_score(item_low))

    def test_duration_impact(self):
        item_short = {'duration': 10.0}
        item_long = {'duration': 60.0}
        self.assertGreater(calculate_smart_score(item_long), calculate_smart_score(item_short))

    def test_sharpness_impact(self):
        item_blurry = {'sharpness': 10.0}
        item_sharp = {'sharpness': 100.0}
        self.assertGreater(calculate_smart_score(item_sharp), calculate_smart_score(item_blurry))

    def test_combined_score(self):
        # 4K video with small size vs 480p video with large size
        video_4k = {
            'size': 50 * 1024 * 1024, 
            'resolution': '3840x2160', 
            'duration': 10, 
            'sharpness': 100
        }
        video_480p = {
            'size': 200 * 1024 * 1024, 
            'resolution': '640x480', 
            'duration': 10, 
            'sharpness': 10
        }
        # 4K should win due to resolution and sharpness weights
        self.assertGreater(calculate_smart_score(video_4k), calculate_smart_score(video_480p))

if __name__ == '__main__':
    unittest.main()
