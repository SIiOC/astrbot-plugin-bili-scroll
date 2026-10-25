"""公开评论写网关回归；全部使用 fake client 和临时 Store，无真实写操作。"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
# 未配置 AstrBot 源码时此模块也由 unittest 标记为 skipped。
from test_main_flow import FakeClient, make_item, make_plugin, main


class CommentGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.delay = patch.object(main, "COMMENT_VERIFY_DELAY", 0)
        self.delay.start()
        self.cfg = {
            "auto_comment": True, "mention_name": "主 号", "push_qq": False,
            "comment_daily_limit": 5, "comment_min_interval_seconds": 30,
            "comment_per_up_daily": 1, "comment_min_score": 75,
        }
        self.client = FakeClient([], {}, {})
        self.plugin = make_plugin(self.tmp.name, self.cfg, self.client, {})
        self.item = {**make_item("BV1NXH661EpG", "转场编排"), "quality": 90,
                     "up_uid": "42", "one_liner": "这段转场和配乐衔接得很自然"}
        self.body = "这段转场和配乐衔接得很自然"

        async def generate(item, mention, one_liner, umo, **kwargs):
            return main.logic.render_comment(mention, self.body)
        self.plugin._make_comment_text = generate

    def tearDown(self):
        self.delay.stop()
        self.tmp.cleanup()

    def send(self, item=None, **kwargs):
        return asyncio.run(self.plugin._post_guarded_comment("test:FriendMessage:1", item or self.item, **kwargs))

    def seed(self, **guard):
        state = self.plugin.store.load_state()
        state["comment_guard"] = {
            "date": datetime.now().strftime("%Y-%m-%d"), "count": 0,
            "last_ts": 0, "up": {}, "texts": [], **guard,
        }
        self.plugin.store.save_state(state)

    def test_daily_limit_and_hard_cap(self):
        self.seed(count=5)
        self.assertEqual(self.send()[0], "daily_limit")
        self.cfg["comment_daily_limit"] = 1000
        self.seed(count=20)
        self.assertEqual(self.send()[0], "daily_limit")
        self.assertEqual(self.client.posted, [])

    def test_interval_survives_gateway_calls(self):
        self.assertEqual(self.send()[0], "posted")
        other = {**self.item, "bvid": "BV1NXH661E02", "up_uid": "43"}
        self.assertEqual(self.send(other)[0], "interval")
        self.assertEqual(len(self.client.posted), 1)

    def test_up_uid_not_renamed_nickname_is_limit_key(self):
        self.seed(up={"uid:42": 1})
        self.assertEqual(self.send({**self.item, "up": "改过的昵称"})[0], "up_limit")
        self.assertFalse(self.client.posted)

    def test_keyword_preference_cannot_inflate_comment_quality(self):
        self.assertEqual(self.send({**self.item, "score": 100, "quality": 17})[0], "quality")
        self.assertFalse(self.client.posted)

    def test_quality_boundary(self):
        self.assertEqual(self.send({**self.item, "quality": 74})[0], "quality")
        self.assertEqual(self.send({**self.item, "quality": 75})[0], "posted")

    def test_spaced_mention_does_not_make_generic_body_specific(self):
        self.body = "支持"
        self.assertEqual(self.send()[0], "generic")
        self.assertFalse(self.client.posted)

    def test_duplicate_body_ignores_recipient_prefix(self):
        self.seed(texts=[self.body])
        self.assertEqual(self.send()[0], "duplicate_text")
        self.assertFalse(self.client.posted)

    def test_legacy_history_backfills_quota(self):
        now = datetime.now()
        self.plugin.store.save_state({"commented": {"old": {
            "date": now.isoformat(), "text": "历史评论", "up_key": "uid:88"}}})
        self.cfg["comment_daily_limit"] = 1
        self.assertEqual(self.send()[0], "daily_limit")

    def test_legacy_missing_up_pauses_for_day(self):
        self.plugin.store.save_state({"commented": {"old": {
            "date": datetime.now().isoformat(), "text": "历史评论"}}})
        self.assertEqual(self.send()[0], "legacy_history_unattributed")

    def test_bad_state_is_fail_closed_and_cannot_be_washed_by_save(self):
        self.plugin.store.state_file.write_text("{bad", encoding="utf-8")
        self.assertEqual(self.send()[0], "state_error")
        self.assertEqual(self.plugin.store.load_state(), {})
        with self.assertRaises(ValueError):
            self.plugin.store.save_state({})
        self.assertEqual(self.plugin.store.state_file.read_text(encoding="utf-8"), "{bad")
        self.assertFalse(self.client.posted)

    def test_wrong_state_shape_is_fail_closed(self):
        self.plugin.store.state_file.write_text("[]", encoding="utf-8")
        self.assertEqual(self.send()[0], "state_error")
        self.assertFalse(self.client.posted)

    def test_reservation_save_failure_never_calls_post(self):
        def fail(_state):
            raise OSError("disk full")
        self.plugin.store.save_state = fail
        self.assertEqual(self.send()[0], "state_error")
        self.assertFalse(self.client.posted)

    def test_timeout_keeps_reservation_and_cools_down(self):
        async def uncertain(item, text):
            self.client.posted.append((item["bvid"], text))
            raise asyncio.TimeoutError("result unknown")
        self.client.post_comment = uncertain
        self.assertEqual(self.send()[0], "post_failed")
        state = self.plugin.store.load_state()
        self.assertEqual(state["comment_guard"]["count"], 1)
        self.assertEqual(state["commented"][self.item["bvid"]]["status"], "unknown")
        self.assertGreater(state["comment_cooldown_until"], datetime.now().timestamp())
        self.assertEqual(self.send()[0], "cooldown")
        self.assertEqual(len(self.client.posted), 1)

    def test_cancel_during_post_preserves_pending_attempt(self):
        async def cancelled(item, text):
            self.client.posted.append((item["bvid"], text))
            raise asyncio.CancelledError()
        self.client.post_comment = cancelled
        with self.assertRaises(asyncio.CancelledError):
            self.send()
        state = self.plugin.store.load_state()
        self.assertEqual(state["comment_guard"]["count"], 1)
        self.assertIn(self.item["bvid"], state["commented"])

    def test_cancel_during_visibility_check_does_not_erase_submitted(self):
        async def cancel_verify(*args):
            raise asyncio.CancelledError()
        self.client.comment_visible = cancel_verify
        with self.assertRaises(asyncio.CancelledError):
            self.send()
        state = self.plugin.store.load_state()
        self.assertEqual(state["commented"][self.item["bvid"]]["status"], "submitted")
        self.assertEqual(state["comment_guard"]["count"], 1)

    def test_concurrent_calls_post_same_video_once(self):
        async def run():
            return await asyncio.gather(*[
                self.plugin._post_guarded_comment("owner", self.item) for _ in range(2)])
        statuses = [r[0] for r in asyncio.run(run())]
        self.assertEqual(statuses.count("posted"), 1)
        self.assertEqual(len(self.client.posted), 1)

    def test_generation_state_changes_and_llm_budget_not_overwritten(self):
        async def generate(*args, **kwargs):
            self.plugin._inc_counter("llm_calls", 2)
            return main.logic.render_comment(self.cfg["mention_name"], self.body)
        self.plugin._make_comment_text = generate
        self.assertEqual(self.send()[0], "posted")
        today = datetime.now().strftime("%Y-%m-%d")
        self.assertEqual(self.plugin.store.load_state()["llm_calls"][today], 2)

    def test_midnight_generation_reserves_new_day(self):
        clock = [datetime(2026, 10, 24, 23, 59, 59)]
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0]
        async def generate(*args, **kwargs):
            clock[0] = datetime(2026, 10, 25, 0, 0, 1)
            return main.logic.render_comment(self.cfg["mention_name"], self.body)
        self.plugin._make_comment_text = generate
        with patch.object(main, "datetime", Clock):
            self.assertEqual(self.send()[0], "posted")
        state = self.plugin.store.load_state()
        self.assertEqual(state["comment_guard"]["date"], "2026-10-25")
        self.assertEqual(state["comment_guard"]["count"], 1)

    def test_switch_disabled_during_generation_blocks_post(self):
        async def generate(*args, **kwargs):
            self.cfg["auto_comment"] = False
            return main.logic.render_comment(self.cfg["mention_name"], self.body)
        self.plugin._make_comment_text = generate
        self.assertEqual(self.send()[0], "disabled")
        self.assertFalse(self.client.posted)

    def test_proactive_and_test_cannot_bypass_public_master_switch(self):
        self.cfg["auto_comment"] = False
        self.cfg["proactive_share"] = True
        self.assertEqual(asyncio.run(self.plugin._run_proactive(test=True)), "comments_disabled")
        self.assertFalse(self.client.posted)

    def test_proactive_test_cannot_bypass_comment_daily_limit(self):
        self.cfg.update({"proactive_share": True, "min_subtitle_chars": 5})
        self.client.items = [self.item]
        self.client.home_feed_items = [self.item]
        self.client.subtitles = {self.item["bvid"]: "字幕" * 20}
        self.seed(count=5)
        self.assertEqual(asyncio.run(self.plugin._run_proactive(test=True)), "nothing")
        self.assertFalse(self.client.posted)

    def test_status_query_preserves_comments_written_during_self_info(self):
        async def self_info():
            state = self.plugin.store.load_state()
            state["comment_guard"] = {"date": "test", "count": 4}
            self.plugin.store.save_state(state)
            return {"name": "测试登录"}
        self.client.self_info = self_info
        event = type("Event", (), {"plain_result": lambda self, text: text})()
        async def read_status():
            return [x async for x in self.plugin.bili_status(event)]
        asyncio.run(read_status())
        self.assertEqual(self.plugin.store.load_state()["comment_guard"]["count"], 4)


if __name__ == "__main__":
    unittest.main()
