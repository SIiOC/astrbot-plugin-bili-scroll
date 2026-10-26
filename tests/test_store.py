"""store.py 单测：临时目录里跑，不碰真实数据。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE_DIR = os.path.dirname(PLUGIN_DIR)
if WORKSPACE_DIR not in sys.path:
    sys.path.insert(0, WORKSPACE_DIR)


import types  # noqa: E402

if "astrbot_plugin_bili_scroll" not in sys.modules:
    _pkg = types.ModuleType("astrbot_plugin_bili_scroll")
    _pkg.__path__ = [PLUGIN_DIR]  # type: ignore[attr-defined]
    sys.modules["astrbot_plugin_bili_scroll"] = _pkg

from astrbot_plugin_bili_scroll.store import Store  # noqa: E402


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name) / "plugin_data" / "astrbot_plugin_bili_scroll"
        self.store = Store(self.data_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_creates_directories(self):
        self.assertTrue(self.store.data_dir.is_dir())
        self.assertTrue(self.store.out_dir.is_dir())
        self.assertTrue(self.store.digest_dir.is_dir())

    def test_credential_roundtrip(self):
        self.assertIsNone(self.store.load_credential())
        self.store.save_credential(
            {"sessdata": "s", "bili_jct": "j", "buvid3": "b3", "dedeuserid": "42"}
        )
        data = self.store.load_credential()
        self.assertEqual(data["sessdata"], "s")
        self.assertEqual(data["buvid3"], "b3")
        self.store.clear_credential()
        self.assertIsNone(self.store.load_credential())

    def test_seen_roundtrip(self):
        self.store.append_seen(
            [
                ("BV1NXH661EpG", "2026-10-20", "挑中"),
                ("BV1VkgK6NEZS", "2026-10-20", "跳过（无字幕）"),
            ]
        )
        self.store.append_seen([("BV1abcdefghJ", "2026-10-21", "挑中")])
        seen = self.store.seen_bvids()
        self.assertEqual(seen, {"BV1NXH661EpG", "BV1VkgK6NEZS", "BV1abcdefghJ"})
        text = self.store.seen_text()
        self.assertEqual(text.count("- BV"), 3)

    def test_update_state_merges_latest_fields(self):
        self.store.save_state({"llm_calls": {"2026-10-26": 2}, "keep": "x"})
        self.store.update_state(lambda state: state.update({"last_round_api_calls": 7}))
        state = self.store.load_state()
        self.assertEqual(state["llm_calls"]["2026-10-26"], 2)
        self.assertEqual(state["last_round_api_calls"], 7)
        self.assertEqual(state["keep"], "x")

    def test_append_sent_keeps_jsonl_append_only(self):
        today = datetime.now().strftime("%Y-%m-%d")
        for index in range(3):
            self.store.append_sent(
                {
                    "date": today,
                    "kind": "dm",
                    "items": [{"bvid": f"BV{index}"}],
                }
            )
        text = self.store.sent_file.read_text(encoding="utf-8")
        self.assertEqual(len(text.strip().splitlines()), 3)
        recent = self.store.recent_sent(days=1, limit=2)
        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[-1]["items"][0]["bvid"], "BV2")

    def test_report_write_and_append(self):
        self.store.write_report("2026-10-20", "# 第一轮\n### BV1NXH661EpG\n")
        self.store.append_report("2026-10-20", "## 追加一轮\n### BV1VkgK6NEZS\n")
        report = self.store.read_report("2026-10-20")
        self.assertIn("第一轮", report)
        self.assertIn("追加一轮", report)
        self.assertEqual(report.count("### BV"), 2)

    def test_digest_append(self):
        self.store.append_digest("2026-10", ["- 一行"])
        self.store.append_digest("2026-10", ["- 二行"])
        digest = (self.store.digest_dir / "2026-10.md").read_text(encoding="utf-8")
        self.assertIn("- 一行", digest)
        self.assertIn("- 二行", digest)

    def test_state_roundtrip(self):
        self.assertEqual(self.store.load_state(), {})
        self.store.save_state({"last_auto_date": "2026-10-20"})
        self.assertEqual(self.store.load_state()["last_auto_date"], "2026-10-20")

    def test_atomic_write_leaves_no_temp_files(self):
        self.store.save_state({"a": 1})
        leftovers = list(self.data_dir.glob(".tmp-*"))
        self.assertEqual(leftovers, [])

    def test_json_write_is_utf8(self):
        self.store.save_state({"note": "中文"})
        raw = self.store.state_file.read_text(encoding="utf-8")
        self.assertIn("中文", raw)

    # ---------- 发送记录 ----------
    def test_append_and_read_sent(self):
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        self.store.append_sent(
            {"date": today, "time": "10-20 22:10", "kind": "push", "count": 2}
        )
        self.store.append_sent(
            {"date": yesterday, "time": "10-19 22:10", "kind": "dm", "count": 1}
        )
        entries = self.store.recent_sent(days=7)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[-1]["kind"], "dm")

    def test_recent_sent_filters_old_days(self):
        today = datetime.now().strftime("%Y-%m-%d")
        self.store.append_sent({"date": "2020-01-01", "kind": "push"})
        self.store.append_sent({"date": today, "kind": "dm"})
        entries = self.store.recent_sent(days=3)
        self.assertEqual([entry["kind"] for entry in entries], ["dm"])

    def test_recent_sent_tolerates_bad_lines(self):
        today = datetime.now().strftime("%Y-%m-%d")
        self.store.sent_file.parent.mkdir(parents=True, exist_ok=True)
        self.store.sent_file.write_text(
            "不是json\n" + f'{{"date": "{today}", "kind": "dm"}}\n', encoding="utf-8"
        )
        entries = self.store.recent_sent(days=7)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "dm")

    # ---------- 判断缓存 ----------
    def test_verdict_cache_roundtrip(self):
        import time as _time

        now = _time.time()
        self.store.save_verdict_cache(
            {
                "BV1AAA": {"verdict": {"worth": True}, "ts": now},
                "BV1OLD": {"verdict": {"worth": False}, "ts": now - 30 * 86400},
            }
        )
        cache = self.store.load_verdict_cache()
        self.assertIn("BV1AAA", cache)
        self.assertNotIn("BV1OLD", cache)  # 超过 14 天被裁剪

    def test_verdict_cache_caps_entries(self):
        import time as _time

        now = _time.time()
        cache = {
            f"BV{i:06d}": {"verdict": {"worth": True}, "ts": now + i}
            for i in range(600)
        }
        self.store.save_verdict_cache(cache, max_entries=500)
        self.assertEqual(len(self.store.load_verdict_cache()), 500)


if __name__ == "__main__":
    unittest.main()
