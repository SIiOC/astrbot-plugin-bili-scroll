"""BOT_GUIDE 与发布白名单的静态一致性检查。"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = re.compile(
    r"dsh-bili-scroll|c1overzz|humanizer|mnemoria|bilibili-mcp|AGI_Ananas",
    re.IGNORECASE,
)


class BotGuideTest(unittest.TestCase):
    def test_guide_exists_and_has_version(self):
        text = (ROOT / "BOT_GUIDE.md").read_text(encoding="utf-8")
        meta = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        version = re.search(r"^version:\s*(\S+)\s*$", meta, re.MULTILINE).group(1)
        self.assertIn(f"适用版本：{version}", text)
        for token in ("触发", "bili_sent_history", "auto_comment", "群聊", "不要"):
            self.assertIn(token, text)

    def test_guide_is_not_visible_with_forbidden_names(self):
        text = (ROOT / "BOT_GUIDE.md").read_text(encoding="utf-8")
        self.assertIsNone(FORBIDDEN.search(text))

    def test_release_tools_include_guide(self):
        for name in ("build_release.py", "verify_release.py"):
            source = (ROOT / "tools" / name).read_text(encoding="utf-8")
            self.assertIn("BOT_GUIDE.md", source)


if __name__ == "__main__":
    unittest.main()
