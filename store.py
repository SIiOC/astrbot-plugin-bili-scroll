"""本地状态层：凭证、判重表、日报、归档、运行状态。

数据全部落在 AstrBot 的 data/plugin_data/<插件名>/ 下，方便备份与迁移。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

_BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")


class Store:
    """插件数据目录的薄封装（纯标准库，可单测）。"""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.out_dir = self.data_dir / "out"
        self.digest_dir = self.data_dir / "digest"
        self.credential_file = self.data_dir / "bili_credential.json"
        self.seen_file = self.data_dir / "seen.md"
        self.state_file = self.data_dir / "state.json"
        self.sent_file = self.data_dir / "sent.jsonl"
        self.verdict_cache_file = self.data_dir / "verdict_cache.json"
        self.qrcode_file = self.data_dir / "qrcode_login.png"
        self.ensure()

    def ensure(self) -> None:
        for path in (self.data_dir, self.out_dir, self.digest_dir):
            path.mkdir(parents=True, exist_ok=True)

    # ---------- 通用 ----------
    @staticmethod
    def _read_json(path: Path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    @staticmethod
    def _write_text_atomic(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            os.replace(tmp, path)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _write_json(self, path: Path, data) -> None:
        self._write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    # ---------- 凭证 ----------
    def load_credential(self) -> dict | None:
        data = self._read_json(self.credential_file, None)
        return data if isinstance(data, dict) and data.get("sessdata") else None

    def save_credential(self, credential: dict) -> Path:
        payload = {
            "sessdata": str(credential.get("sessdata") or ""),
            "bili_jct": str(credential.get("bili_jct") or ""),
            "buvid3": str(credential.get("buvid3") or ""),
            "buvid4": str(credential.get("buvid4") or ""),
            "dedeuserid": str(credential.get("dedeuserid") or ""),
            "ac_time_value": str(credential.get("ac_time_value") or ""),
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._write_json(self.credential_file, payload)
        try:
            os.chmod(self.credential_file, 0o600)
        except OSError:
            pass
        return self.credential_file

    def clear_credential(self) -> None:
        try:
            self.credential_file.unlink()
        except OSError:
            pass

    # ---------- 运行状态 ----------
    def load_state(self) -> dict:
        data = self._read_json(self.state_file, {})
        return data if isinstance(data, dict) else {}

    def save_state(self, state: dict) -> None:
        self._write_json(self.state_file, state)

    # ---------- 判重表 ----------
    def seen_text(self) -> str:
        try:
            return self.seen_file.read_text(encoding="utf-8")
        except OSError:
            return ""

    def seen_bvids(self) -> set[str]:
        return set(_BV_RE.findall(self.seen_text()))

    def append_seen(self, entries: list[tuple[str, str, str]]) -> None:
        """entries: [(bvid, 日期, 结论)]，追加一行一条。"""
        if not entries:
            return
        lines = [f"- {bvid} | {date} | {note}" for bvid, date, note in entries]
        existing = self.seen_text()
        all_lines = (existing.rstrip("\n").splitlines() if existing else []) + lines
        # 判重表只保留最近 2 万条，避免多年运行后文件无限增长（仍然覆盖长期去重）
        all_lines = all_lines[-20000:]
        self._write_text_atomic(self.seen_file, "\n".join(all_lines) + "\n")

    # ---------- 日报 ----------
    def report_path(self, date_str: str) -> Path:
        return self.out_dir / f"{date_str}.md"

    def read_report(self, date_str: str) -> str:
        try:
            return self.report_path(date_str).read_text(encoding="utf-8")
        except OSError:
            return ""

    def write_report(self, date_str: str, text: str) -> Path:
        self._write_text_atomic(self.report_path(date_str), text)
        return self.report_path(date_str)

    def append_report(self, date_str: str, text: str) -> Path:
        existing = self.read_report(date_str).rstrip()
        merged = f"{existing}\n\n{text.strip()}\n" if existing else text.strip() + "\n"
        return self.write_report(date_str, merged)

    # ---------- 已发送记录（给 bot 回忆用） ----------
    def append_sent(self, entry: dict) -> None:
        """追加一条发送记录（JSONL，一行一条；append-only，保留最近 5000 条）。"""
        if not isinstance(entry, dict) or not entry:
            return
        line = json.dumps(entry, ensure_ascii=False)
        try:
            existing = self.sent_file.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        lines = (existing.rstrip("\n").splitlines() if existing else []) + [line]
        self._write_text_atomic(self.sent_file, "\n".join(lines[-5000:]) + "\n")

    def recent_sent(self, days: int = 7, limit: int = 50) -> list[dict]:
        """按天过滤最近发送记录（新的在后，取最后 limit 条）。"""
        try:
            text = self.sent_file.read_text(encoding="utf-8")
        except OSError:
            return []
        cutoff = (datetime.now() - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d")
        items: list[dict] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            if str(entry.get("date") or "")[:10] < cutoff:
                continue
            items.append(entry)
        return items[-max(1, int(limit)):]

    # ---------- 判断结果缓存（跨轮复用，省模型调用） ----------
    def load_verdict_cache(self) -> dict:
        data = self._read_json(self.verdict_cache_file, {})
        return data if isinstance(data, dict) else {}

    def save_verdict_cache(self, cache: dict, *, keep_days: int = 14, max_entries: int = 500) -> None:
        if not isinstance(cache, dict):
            return
        cutoff = (datetime.now() - timedelta(days=max(1, int(keep_days)))).timestamp()
        cleaned = {
            str(key): value
            for key, value in cache.items()
            if isinstance(value, dict) and float(value.get("ts") or 0) >= cutoff
        }
        if len(cleaned) > max_entries:
            newest = sorted(cleaned.items(), key=lambda kv: float(kv[1].get("ts") or 0))
            cleaned = dict(newest[-max_entries:])
        self._write_json(self.verdict_cache_file, cleaned)

    # ---------- 归档 ----------
    def append_digest(self, month_str: str, lines: list[str]) -> None:
        if not lines:
            return
        path = self.digest_dir / f"{month_str}.md"
        existing = ""
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            pass
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        self._write_text_atomic(path, existing + prefix + "\n".join(lines) + "\n")
