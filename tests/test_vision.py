import os
import tempfile
import time
import unittest
from pathlib import Path

from astrbot_plugin_bili_scroll import vision


class TestVisionHelpers(unittest.TestCase):
    def test_is_safe_media_url(self):
        self.assertTrue(
            vision.is_safe_media_url(
                "https://upos-sz-mirrorcos.bilivideo.com/upgcxcode/1/2/x.mp4"
            )
        )
        self.assertTrue(
            vision.is_safe_media_url(
                "https://b-baai4ho05dolbv29i62qpe4f175e.edge.example-cdn.cn:4483/a.mp4"
            )
        )
        self.assertFalse(vision.is_safe_media_url("http://bilivideo.com/a.mp4"))
        self.assertFalse(vision.is_safe_media_url("https://localhost/a.mp4"))
        self.assertFalse(vision.is_safe_media_url("https://127.0.0.1/a.mp4"))
        self.assertFalse(vision.is_safe_media_url("https://10.0.0.8/a.mp4"))
        self.assertFalse(
            vision.is_safe_media_url("https://user@bilivideo.com/a.mp4")
        )

    def test_pick_video_stream_prefers_smallest_bandwidth(self):
        data = {
            "dash": {
                "video": [
                    {"baseUrl": "https://a.bilivideo.com/hi.mp4", "bandwidth": 900000},
                    {"baseUrl": "https://a.bilivideo.com/lo.mp4", "bandwidth": 200000},
                    {"base_url": "https://a.bilivideo.com/mid.mp4", "bandwidth": 400000},
                ]
            }
        }
        stream = vision.pick_video_stream(data)
        self.assertEqual(stream["url"], "https://a.bilivideo.com/lo.mp4")
        self.assertEqual(stream["bandwidth"], 200000)

    def test_pick_video_stream_durl_and_empty(self):
        stream = vision.pick_video_stream(
            {"durl": [{"url": "https://a.bilivideo.com/part1.mp4"}]}
        )
        self.assertEqual(stream["url"], "https://a.bilivideo.com/part1.mp4")
        self.assertIsNone(vision.pick_video_stream({}))
        self.assertIsNone(vision.pick_video_stream(None))

    def test_frame_timestamps(self):
        stamps = vision.frame_timestamps(100, 5, 60)
        self.assertEqual(len(stamps), 5)
        self.assertTrue(all(0 < stamp < 60 for stamp in stamps))
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(vision.frame_timestamps(10, 5, 120)[0], 1.0)
        self.assertEqual(vision.frame_timestamps(0, 3, 60), [10.0, 30.0, 50.0])
        self.assertEqual(len(vision.frame_timestamps(100, 20, 300)), 8)
        stamps = vision.frame_timestamps(2, 5, 60)
        self.assertGreaterEqual(len(stamps), 1)
        self.assertTrue(all(0 < stamp < 2 for stamp in stamps))

    def test_build_prompts_and_clean(self):
        system, user = vision.build_vision_prompts(
            {"title": "舞蹈练习", "up": "UP", "duration": 60, "source": "keyword"},
            5,
        )
        self.assertIn("画面", system)
        self.assertIn("舞蹈练习", user)
        self.assertIn("5 帧", user)
        self.assertEqual(vision.clean_visual("  画面   很亮  "), "画面 很亮")
        self.assertEqual(len(vision.clean_visual("字" * 900)), 500)

    def test_ffmpeg_path_returns_string(self):
        self.assertIsInstance(vision.ffmpeg_path("ffmpeg"), str)

    def test_cleanup_stale_only_removes_old_vision_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_file = root / "vision-old.mp4"
            old_dir = root / "vision-old-dir"
            new_file = root / "vision-new.mp4"
            other = root / "keep.txt"
            old_file.write_bytes(b"x" * 100)
            old_dir.mkdir()
            (old_dir / "frame.jpg").write_bytes(b"x" * 100)
            new_file.write_bytes(b"x" * 100)
            other.write_bytes(b"x" * 100)
            old_ts = time.time() - 7200
            os.utime(old_file, (old_ts, old_ts))
            os.utime(old_dir, (old_ts, old_ts))
            removed = vision.cleanup_stale(root, 3600)
            self.assertEqual(removed, 2)
            self.assertFalse(old_file.exists())
            self.assertFalse(old_dir.exists())
            self.assertTrue(new_file.exists())
            self.assertTrue(other.exists())


if __name__ == "__main__":
    unittest.main()
