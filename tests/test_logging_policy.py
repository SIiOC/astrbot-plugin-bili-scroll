"""插件日志来源与上下文注入策略的静态回归。"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ["main.py", "bili_client.py", "web_api.py", "vision.py", "logic.py", "store.py"]


class LoggingPolicyTest(unittest.TestCase):
    def test_no_stdlib_logging_in_runtime_or_tests(self):
        paths = [ROOT / name for name in RUNTIME]
        paths += [
            path
            for path in (ROOT / "tests").glob("*.py")
            if path.name != Path(__file__).name
        ]
        bad = []
        for path in paths:
            text = path.read_text(encoding="utf-8")
            if re.search(r"(?m)^\s*import\s+logging\b", text) or re.search(
                r"\blogging\.getLogger\s*\(", text
            ):
                bad.append(str(path.relative_to(ROOT)))
        self.assertEqual(bad, [], "插件代码只能使用 astrbot.api.logger")
        for name in ("bili_client.py", "web_api.py"):
            self.assertIn(
                "from astrbot.api import logger",
                (ROOT / name).read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
