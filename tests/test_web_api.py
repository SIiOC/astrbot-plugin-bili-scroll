"""web_api.py 单测：用假 plugin/context 验证路由表与各端点返回结构（不联网）。"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE_DIR = os.path.dirname(PLUGIN_DIR)
if WORKSPACE_DIR not in sys.path:
    sys.path.insert(0, WORKSPACE_DIR)

# web_api 通过 astrbot.api 取日志；没有宿主源码时整组跳过。
for _env in ("ASTRBOT_SRC", "ASTRBOT_ROOT"):
    _root = os.environ.get(_env, "")
    if _root and os.path.isdir(os.path.join(_root, "astrbot")):
        if _root not in sys.path:
            sys.path.insert(0, _root)
        break


import types  # noqa: E402

if "astrbot_plugin_bili_scroll" not in sys.modules:
    _pkg = types.ModuleType("astrbot_plugin_bili_scroll")
    _pkg.__path__ = [PLUGIN_DIR]  # type: ignore[attr-defined]
    sys.modules["astrbot_plugin_bili_scroll"] = _pkg

try:
    from astrbot_plugin_bili_scroll import web_api  # noqa: E402
except ModuleNotFoundError as exc:
    if exc.name == "astrbot":
        raise unittest.SkipTest("未找到 AstrBot 源码；跳过 web_api 测试") from exc
    raise
from astrbot_plugin_bili_scroll.store import Store  # noqa: E402


class FakeContext:
    def __init__(self, has_api=True):
        self.routes = []
        if has_api:
            self.register_web_api = self._register

    def _register(self, route, handler, methods, desc):
        self.routes.append((route, handler, methods, desc))


class FakeClient:
    def __init__(self):
        self.logged_in = False
        self.state = "waiting"
        self.cancelled = False
        self.png = None
        self.self_info_calls = 0

    def has_credential(self):
        return self.logged_in

    async def self_info(self):
        self.self_info_calls += 1
        return {"name": "测试号"}

    async def qr_start(self):
        return self.png

    async def qr_poll(self):
        return self.state

    def cancel_login(self):
        self.cancelled = True

    def reload_credential(self):
        pass


class FakePlugin:
    def __init__(self, store, cfg=None):
        self.store = store
        self.cfg = cfg or {"per_day": 5, "keywords": ["DSH"], "run_at": "22:00"}
        self.client = FakeClient()
        self._busy = False
        self._umo = ""
        self._round_task = None
        self.ran = []
        self.counters = {}

    def _cfg(self, key, default=None):
        return self.cfg.get(key, default)

    def _cfg_int(self, key, default=0, *, minimum=0):
        try:
            return max(minimum, int(self.cfg.get(key, default)))
        except (TypeError, ValueError):
            return default

    async def _run_round(self, umo, source="manual", force=False):
        self.ran.append((umo, source))

    async def _persona_prompt(self, umo):
        return "测试人设"

    async def _make_comment_text(self, item, mention, one_liner, umo):
        return f"@{mention} 预览文案：{item.get('title', '')}"

    def _counter(self, name):
        return int(self.counters.get(name, 0))

    @staticmethod
    def _parse_hhmm(raw):
        try:
            hour, minute = str(raw).split(":")
            return int(hour), int(minute)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _in_valley(now):
        return True


class WebApiTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "data")
        self.plugin = FakePlugin(self.store)
        self.api = web_api.BiliScrollWebAPI(self.plugin)

    def tearDown(self):
        self._tmp.cleanup()

    def test_register_routes(self):
        ctx = FakeContext()
        count = web_api.register_web_routes(ctx, self.plugin)
        self.assertEqual(count, len(web_api.ROUTE_SPECS))
        self.assertEqual(len(ctx.routes), count)
        routes = {route for route, *_ in ctx.routes}
        self.assertIn("/astrbot_plugin_bili_scroll/status", routes)
        self.assertIn("/astrbot_plugin_bili_scroll/login/start", routes)
        self.assertIn("/astrbot_plugin_bili_scroll/login/poll", routes)
        self.assertIn("/astrbot_plugin_bili_scroll/run", routes)
        self.assertIn("/astrbot_plugin_bili_scroll/report/today", routes)

    def test_register_routes_old_context(self):
        ctx = FakeContext(has_api=False)
        self.assertEqual(web_api.register_web_routes(ctx, self.plugin), 0)

    def test_run_proactive_blocked_by_daily_cap(self):
        self.plugin.client.logged_in = True
        self.plugin.cfg["mention_name"] = "主号"
        self.plugin.cfg["proactive_max_per_day"] = 2
        today = datetime.now().strftime("%Y-%m-%d")
        self.store.save_state({"proactive_shares": {today: 2}})
        result = asyncio.run(self.api.run_proactive())
        self.assertFalse(result["accepted"])
        self.assertIn("主动分享", result["message"])

    def test_status_payload(self):
        today = datetime.now().strftime("%Y-%m-%d")
        self.store.write_report(today, "### BV1NXH661EpG\n### BV1VkgK6NEZS\n")
        status = asyncio.run(self.api.get_status())
        self.assertFalse(status["logged_in"])
        self.assertEqual(status["picked_today"], 2)
        self.assertEqual(status["per_day"], 5)
        self.assertEqual(status["keywords"], ["DSH"])
        self.assertTrue(status["report_exists"])
        self.assertFalse(status["busy"])
        self.assertEqual(status["dm_mode"], "off")
        self.assertEqual(status["sent_today"], 0)
        self.assertTrue(status["push_qq"])
        self.assertEqual(status["rounds_today"], 0)
        self.assertEqual(status["llm_calls_today"], 0)
        self.assertEqual(status["llm_budget_per_day"], 200)

    def test_login_start_and_poll(self):
        png = Path(self._tmp.name) / "qr.png"
        png.write_bytes(b"\x89PNG-fake")
        self.plugin.client.png = png
        started = asyncio.run(self.api.login_start())
        self.assertTrue(started["ok"])
        self.assertTrue(started["image"].startswith("data:image/png;base64,"))

        self.plugin.client.state = "done"
        self.plugin.client.logged_in = True
        polled = asyncio.run(self.api.login_poll())
        self.assertEqual(polled["state"], "done")
        self.assertTrue(polled["logged_in"])
        self.assertEqual(polled["user_name"], "测试号")

    def test_login_cancel_and_logout(self):
        asyncio.run(self.api.login_cancel())
        self.assertTrue(self.plugin.client.cancelled)
        self.store.save_credential({"sessdata": "s", "bili_jct": "j"})
        asyncio.run(self.api.logout())
        self.assertIsNone(self.store.load_credential())

    def test_run_round_rejects_without_login(self):
        result = asyncio.run(self.api.run_round())
        self.assertFalse(result["accepted"])
        self.plugin.client.logged_in = True
        self.plugin._umo = "aiocqhttp:FriendMessage:1"
        result = asyncio.run(self.api.run_round())
        self.assertTrue(result["accepted"])

    def test_run_round_allowed_without_push_session(self):
        self.plugin.client.logged_in = True
        self.plugin._umo = ""
        result = asyncio.run(self.api.run_round())
        self.assertTrue(result["accepted"])
        self.assertIn("查看今日日报", result["message"])

    def test_preview_comment_falls_back_to_report(self):
        today = datetime.now().strftime("%Y-%m-%d")
        self.store.write_report(
            today, "### BV1ABC\n- 标题：标题一\n- 一句话：一句话\n"
        )
        result = asyncio.run(self.api.preview_comment())
        self.assertTrue(result["ok"])
        self.assertEqual(result["bvid"], "BV1ABC")

    def test_preview_comment_throttled(self):
        self.plugin.cfg["mention_name"] = "主号"
        self.store.save_state(
            {
                "last_picked": [{"bvid": "BV1", "title": "标题一", "one_liner": "一句话"}],
                "last_picked_date": datetime.now().strftime("%Y-%m-%d"),
            }
        )
        first = asyncio.run(self.api.preview_comment())
        self.assertTrue(first["ok"])
        second = asyncio.run(self.api.preview_comment())
        self.assertFalse(second["ok"])
        self.assertIn("太频繁", second["message"])

    def test_report_and_list(self):
        today = datetime.now().strftime("%Y-%m-%d")
        self.store.write_report(today, "# 日报\n")
        report = asyncio.run(self.api.get_today_report())
        self.assertTrue(report["exists"])
        self.assertIn("日报", report["markdown"])
        listed = asyncio.run(self.api.list_reports())
        self.assertEqual(listed["items"][0]["date"], today)

    def test_preview_comment_requires_last_round(self):
        result = asyncio.run(self.api.preview_comment())
        self.assertFalse(result["ok"])
        self.assertIn("跑过一轮", result["message"])

    def test_preview_comment_uses_persona(self):
        self.plugin.cfg["mention_name"] = "主号"
        self.store.save_state(
            {
                "last_picked": [{"bvid": "BV1", "title": "标题一", "one_liner": "一句话"}],
                "last_picked_date": datetime.now().strftime("%Y-%m-%d"),
            }
        )
        result = asyncio.run(self.api.preview_comment())
        self.assertTrue(result["ok"])
        self.assertEqual(result["bvid"], "BV1")
        self.assertTrue(result["used_persona"])
        self.assertIn("预览文案", result["text"])

    # ---------- v0.4.0 页面增强 ----------
    def test_report_by_date(self):
        self.store.write_report("2026-10-20", "# 旧日报\n内容\n")
        result = asyncio.run(self.api.get_report_by_date("2026-10-20"))
        self.assertTrue(result["ok"])
        self.assertIn("旧日报", result["markdown"])
        bad = asyncio.run(self.api.get_report_by_date("../etc/passwd"))
        self.assertFalse(bad["ok"])
        self.assertIn("YYYY-MM-DD", bad["message"])

    def test_status_has_ui_fields(self):
        status = asyncio.run(self.api.get_status())
        for key in ("picked_items", "sent_recent", "next_run", "rounds_today", "llm_calls_today"):
            self.assertIn(key, status)
        self.assertIsInstance(status["picked_items"], list)
        self.assertIsInstance(status["sent_recent"], list)
        self.assertIn("config", status)
        self.assertIn("per_day", status["config"])
        self.assertIn("risk_abort_threshold", status["config"])
        self.assertIn("last_round_api_calls", status)

    def test_page_html_has_new_ui(self):
        page = (
            Path(__file__).resolve().parents[1] / "pages" / "bili-panel" / "index.html"
        ).read_text(encoding="utf-8")
        for marker in ("class=\"hero\"", "quotaBar", "llmBar", "roundBar", "btnHistory", "reportBody", "最近发送", "overviewText", "btnSaveConfig", "kwRows", "btnAddKeyword", "cfgMixKeywords", "mixBar"):
            self.assertIn(marker, page)

    # ---------- v0.5.1 审查修复 ----------
    def test_status_ignores_stale_last_picked(self):
        today = datetime.now().strftime("%Y-%m-%d")
        self.store.save_state(
            {
                "last_picked": [{"bvid": "BVOLD", "title": "昨天的"}],
                "last_picked_date": "2020-01-01",
            }
        )
        self.store.write_report(today, "### BVNEW\n- 标题：今天的\n- 一句话：一句话\n")
        status = asyncio.run(self.api.get_status())
        bvids = [item["bvid"] for item in status["picked_items"]]
        self.assertIn("BVNEW", bvids)
        self.assertNotIn("BVOLD", bvids)

    def test_run_round_blocked_by_cooldown(self):
        self.plugin.client.logged_in = True
        self.store.save_state(
            {"cooldown_until": __import__("time").time() + 600}
        )
        result = asyncio.run(self.api.run_round())
        self.assertFalse(result["accepted"])
        self.assertIn("冷却", result["message"])

    def test_run_round_busy_rejected(self):
        self.plugin.client.logged_in = True
        self.plugin._busy = True
        result = asyncio.run(self.api.run_round())
        self.assertFalse(result["accepted"])
        self.assertIn("正在跑", result["message"])

    def test_run_round_blocked_by_quota(self):
        self.plugin.client.logged_in = True
        today = datetime.now().strftime("%Y-%m-%d")
        self.plugin.cfg["per_day"] = 5
        self.store.save_state({"picked": {today: 5}})
        result = asyncio.run(self.api.run_round())
        self.assertFalse(result["accepted"])
        self.assertIn("挑满", result["message"])

    def test_run_round_blocked_by_rounds_cap(self):
        self.plugin.client.logged_in = True
        today = datetime.now().strftime("%Y-%m-%d")
        self.plugin.cfg["rounds_per_day"] = 2
        self.plugin.counters["rounds"] = 2
        self.store.save_state({"rounds": {today: 2}})
        result = asyncio.run(self.api.run_round())
        self.assertFalse(result["accepted"])
        self.assertIn("轮", result["message"])

    def test_login_name_cached_avoids_repeat_network(self):
        self.plugin.client.logged_in = True
        asyncio.run(self.api.get_status())
        first = self.plugin.client.self_info_calls
        asyncio.run(self.api.get_status())
        second = self.plugin.client.self_info_calls
        self.assertEqual(first, 1)
        self.assertEqual(second, 1)

    def test_preview_ignores_stale_last_picked(self):
        today = datetime.now().strftime("%Y-%m-%d")
        self.plugin.cfg["mention_name"] = "主号"
        self.store.save_state(
            {
                "last_picked": [{"bvid": "BVOLD", "title": "昨天的"}],
                "last_picked_date": "2020-01-01",
            }
        )
        self.store.write_report(today, "### BVNEW\n- 标题：今天的\n- 一句话：一句话\n")
        result = asyncio.run(self.api.preview_comment())
        self.assertTrue(result["ok"])
        self.assertEqual(result["bvid"], "BVNEW")

    def test_preview_comment_handles_generation_error(self):
        today = datetime.now().strftime("%Y-%m-%d")
        self.store.save_state(
            {
                "last_picked": [{"bvid": "BV1", "title": "标题一", "one_liner": "一句话"}],
                "last_picked_date": today,
            }
        )

        async def boom(*_args, **_kwargs):
            raise RuntimeError("今日模型调用预算已用尽")

        self.plugin._make_comment_text = boom
        result = asyncio.run(self.api.preview_comment())
        self.assertFalse(result["ok"])
        self.assertIn("预算", result["message"])

    def test_preview_warns_without_mention(self):
        today = datetime.now().strftime("%Y-%m-%d")
        self.store.save_state(
            {
                "last_picked": [{"bvid": "BV1", "title": "标题一", "one_liner": "一句话"}],
                "last_picked_date": today,
            }
        )
        self.plugin.cfg["mention_name"] = ""
        result = asyncio.run(self.api.preview_comment())
        self.assertTrue(result["ok"])
        self.assertIn("没填", result["warning"])

    def test_apply_config_updates_validation(self):
        result = web_api.apply_config_updates(
            self.plugin,
            {
                "per_day": 3,
                "keywords": "甲,乙，丙",
                "auto_run": True,
                "llm_budget_per_day": 999999,
                "dm_share_mode": "single",
                "dm_share_limit": 3,
                "dm_share_daily_limit": 10,
                "dm_share_interval": 5,
            },
        )
        self.assertIn("per_day", result["applied"])
        self.assertIn("keywords", result["applied"])
        self.assertIn("dm_share_mode", result["applied"])
        self.assertEqual(self.plugin.cfg["dm_share_limit"], 3)
        self.assertTrue(self.plugin.cfg["auto_run"])
        self.assertEqual(
            self.plugin.cfg["keywords"], ["甲:34", "乙:33", "丙:33"]
        )
        self.assertIn("llm_budget_per_day", " ".join(result["issues"]))

    def test_comment_guard_zero_thresholds_preserved(self):
        result = web_api.apply_config_updates(
            self.plugin,
            {"comment_min_score": 0, "comment_dedupe_pct": 0, "comment_per_round": 3},
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.plugin.cfg["comment_min_score"], 0)
        self.assertEqual(self.plugin.cfg["comment_dedupe_pct"], 0)
        self.assertEqual(self.plugin.cfg["comment_per_round"], 3)

    def test_proactive_delivery_validation(self):
        result = web_api.apply_config_updates(
            self.plugin, {"proactive_delivery": "dm"}
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.plugin.cfg["proactive_delivery"], "dm")
        bad = web_api.apply_config_updates(
            self.plugin, {"proactive_delivery": "qq"}
        )
        self.assertFalse(bad["ok"])
        self.assertIn("proactive_delivery", " ".join(bad["issues"]))

    def test_apply_config_updates_rejects_bad_values(self):
        bad = web_api.apply_config_updates(
            self.plugin, {"dm_mode": "nope", "dm_share_mode": "nope", "run_at": "25:99", "debug": True}
        )
        self.assertFalse(bad["ok"])
        joined = " ".join(bad["issues"])
        self.assertIn("dm_mode", joined)
        self.assertIn("dm_share_mode", joined)
        self.assertIn("run_at", joined)
        self.assertIn("debug", joined)

    def test_apply_config_keyword_weights_and_mix(self):
        result = web_api.apply_config_updates(
            self.plugin,
            {
                "keywords": ["数码:30", "美食:50", "AI：20"],
                "mix_keywords_pct": 60,
                "mix_recommend_pct": 30,
                "mix_popular_pct": 10,
            },
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            self.plugin.cfg["keywords"], ["数码:30", "美食:50", "AI:20"]
        )
        self.assertEqual(self.plugin.cfg["mix_keywords_pct"], 60)
        self.assertEqual(self.plugin.cfg["mix_recommend_pct"], 30)
        self.assertEqual(self.plugin.cfg["mix_popular_pct"], 10)

    def test_apply_config_rejects_bad_keyword_weight(self):
        result = web_api.apply_config_updates(self.plugin, {"keywords": ["数码:120"]})
        self.assertFalse(result["ok"])
        self.assertIn("占比", " ".join(result["issues"]))
        self.assertEqual(self.plugin.cfg.get("keywords"), ["DSH"])

    def test_apply_config_rejects_bad_mix_pct(self):
        result = web_api.apply_config_updates(self.plugin, {"mix_recommend_pct": 120})
        self.assertFalse(result["ok"])
        self.assertIn("mix_recommend_pct", " ".join(result["issues"]))


if __name__ == "__main__":
    unittest.main()
