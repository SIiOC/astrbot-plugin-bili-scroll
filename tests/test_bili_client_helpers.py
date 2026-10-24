"""bili_client.py 的离线辅助逻辑单测（不联网）。"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
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

from astrbot_plugin_bili_scroll import bili_client  # noqa: E402
from astrbot_plugin_bili_scroll.bili_client import (  # noqa: E402
    QR_STATE_TEXT,
    BiliClient,
    _parse_set_cookie,
)
from astrbot_plugin_bili_scroll.store import Store  # noqa: E402


class TestCookieParsing(unittest.TestCase):
    def test_parse_set_cookie(self):
        jar = _parse_set_cookie(
            [
                "SESSDATA=abc%2C123; Path=/; Domain=.bilibili.com; HttpOnly",
                "bili_jct=deadbeef; Path=/",
                "DedeUserID=12345; Path=/",
                "buvid3=xyz-infoc; Path=/",
                "unrelated=1; Path=/",
            ]
        )
        self.assertEqual(jar["sessdata"], "abc%2C123")
        self.assertEqual(jar["bili_jct"], "deadbeef")
        self.assertEqual(jar["dedeuserid"], "12345")
        self.assertEqual(jar["buvid3"], "xyz-infoc")
        self.assertNotIn("unrelated", jar)

    def test_parse_set_cookie_empty(self):
        self.assertEqual(_parse_set_cookie([]), {})
        self.assertEqual(_parse_set_cookie(["no-equals-sign"]), {})

    def test_qr_state_text_covers_states(self):
        for state in ("waiting", "scanned", "expired", "done"):
            self.assertIn(state, QR_STATE_TEXT)


class TestCredentialLoading(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "data")

    def tearDown(self):
        self._tmp.cleanup()

    def test_has_credential_before_and_after_save(self):
        client = BiliClient(self.store)
        self.assertFalse(client.has_credential())
        self.store.save_credential(
            {"sessdata": "s", "bili_jct": "j", "buvid3": "b", "dedeuserid": "1"}
        )
        client.reload_credential()
        self.assertTrue(client.has_credential())
        self.assertEqual(client.credential.dedeuserid, "1")


class TestCommentResultUnwrap(unittest.TestCase):
    """Api.result 会剥掉外层 data；post_comment 必须从 data 本体取 rpid。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "data")
        self.store.save_credential(
            {"sessdata": "s", "bili_jct": "j", "buvid3": "b", "dedeuserid": "1"}
        )
        self.client = BiliClient(self.store)
        self.client.reload_credential()
        self._original = bili_client.comment.send_comment

    def tearDown(self):
        bili_client.comment.send_comment = self._original
        self._tmp.cleanup()

    def _patch(self, result):
        async def fake_send_comment(**kwargs):
            return result

        bili_client.comment.send_comment = fake_send_comment

    def test_rpid_from_unwrapped_data(self):
        self._patch({"rpid": 998877, "rpid_str": "998877"})
        result = asyncio.run(self.client.post_comment({"aid": 1}, "hi"))
        self.assertEqual(result["rpid"], 998877)

    def test_rpid_from_legacy_nested_data(self):
        self._patch({"data": {"rpid": 42}})
        result = asyncio.run(self.client.post_comment({"aid": 1}, "hi"))
        self.assertEqual(result["rpid"], 42)


class TestSendDmGuard(unittest.TestCase):
    """B 站禁止自投（21026）：给自己发私信必须在联网前就被拦下。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "data")
        self.store.save_credential(
            {"sessdata": "s", "bili_jct": "j", "buvid3": "b", "dedeuserid": "12345"}
        )
        self.client = BiliClient(self.store)
        self.client.reload_credential()

    def tearDown(self):
        self._tmp.cleanup()

    def test_self_dm_rejected_locally(self):
        with self.assertRaises(bili_client.BiliError) as ctx:
            asyncio.run(self.client.send_dm("hi", receiver_uid=12345))
        self.assertIn("21026", str(ctx.exception))
        self.assertIn("另一个账号", str(ctx.exception))

    def test_zero_receiver_falls_back_to_self_and_rejected(self):
        with self.assertRaises(bili_client.BiliError):
            asyncio.run(self.client.send_dm("hi", receiver_uid=0))


if __name__ == "__main__":
    unittest.main()
