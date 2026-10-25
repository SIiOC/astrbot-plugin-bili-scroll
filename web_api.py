"""Plugin Page 后端 API：扫码登录、状态、跑一轮、日报。

通过 AstrBot v4.24.2+ 的 ``context.register_web_api`` 注册；前端页面在
``pages/bili-panel/index.html``，经 WebUI 的插件页面桥接调用。

路由前缀 ``/astrbot_plugin_bili_scroll/...``，前端用
``AstrBotPluginPage.apiGet("status")`` 这类相对端点即可。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from datetime import datetime, timedelta

from . import logic
from .bili_client import NotLoggedInError

PLUGIN_NAME = "astrbot_plugin_bili_scroll"
_LOGGER = logging.getLogger(__name__)

ROUTE_SPECS: tuple[tuple[str, str, list[str], str], ...] = (
    ("status", "get_status", ["GET"], "登录状态与今日统计"),
    ("login/start", "login_start", ["POST"], "生成 B 站登录二维码"),
    ("login/poll", "login_poll", ["GET"], "轮询扫码状态"),
    ("login/cancel", "login_cancel", ["POST"], "取消登录"),
    ("logout", "logout", ["POST"], "清除本机登录凭据"),
    ("run", "run_round", ["POST"], "立即跑一轮"),
    ("proactive", "run_proactive", ["POST"], "主动刷一条并分享（测试）"),
    ("preview/comment", "preview_comment", ["POST"], "用人设预览一条评论（不发送）"),
    ("config", "save_config", ["POST"], "快捷修改配置"),
    ("report/today", "get_today_report", ["GET"], "今日日报"),
    ("report/<date>", "get_report_by_date", ["GET"], "按日期读取日报"),
    ("reports", "list_reports", ["GET"], "历史日报列表"),
)

# 页面「快捷设置」允许修改的键（有范围校验；敏感/只读项不在内）
_BOOL_KEYS = {
    "auto_run",
    "peak_valley",
    "auto_comment",
    "comment_guard_review",
    "comment_use_persona",
    "push_qq",
    "read_comments",
    "inject_sent_context",
    "nl_trigger",
    "proactive_share",
}
_INT_KEYS: dict[str, tuple[int, int]] = {
    "per_day": (0, 999),
    "days": (1, 30),
    "max_candidates": (1, 100),
    "comment_per_round": (1, 3),
    "comment_text_limit": (20, 150),
    "comment_daily_limit": (1, 20),
    "comment_per_up_daily": (1, 3),
    "comment_min_score": (0, 100),
    "comment_min_interval_seconds": (30, 3600),
    "comment_min_chars": (2, 80),
    "comment_dedupe_pct": (0, 100),
    "llm_budget_per_day": (0, 100000),
    "rounds_per_day": (0, 100),
    "risk_abort_threshold": (1, 20),
    "risk_cooldown_minutes": (5, 1440),
    "judge_cache_days": (0, 30),
    "sent_context_days": (1, 30),
    "dm_uid": (0, 10**12),
    "dm_text_limit": (100, 500),
    "dm_share_limit": (1, 10),
    "dm_share_daily_limit": (1, 50),
    "dm_share_interval": (2, 60),
    "proactive_idle_minutes": (5, 1440),
    "proactive_max_per_day": (0, 10),
    "proactive_min_gap_minutes": (0, 1440),
    "mix_keywords_pct": (0, 100),
    "mix_recommend_pct": (0, 100),
    "mix_popular_pct": (0, 100),
    "vision_max_frames": (2, 8),
    "vision_max_seconds": (10, 300),
    "vision_max_mb": (5, 80),
    "vision_timeout": (30, 300),
    "preference_weight": (0, 100),
    "pick_threshold": (0, 100),
}
_STR_KEYS = {
    "run_at",
    "mention_name",
    "comment_persona_hint",
    "comment_blocklist",
    "dm_mode",
    "dm_share_mode",
    "proactive_quiet_start",
    "proactive_quiet_end",
    "proactive_source",
    "no_subtitle_policy",
    "vision_provider_id",
    "ffmpeg_path",
}
_DM_MODES = {"off", "only", "both"}
_DM_SHARE_MODES = {"single", "digest"}
_NO_SUBTITLE_POLICIES = {"skip", "summary", "comments", "vision"}


def apply_config_updates(plugin, payload: dict) -> dict:
    """把页面提交的键值写进插件配置（带类型/范围校验，纯函数便于单测）。"""
    cfg = getattr(plugin, "cfg", None)
    if cfg is None:
        return {"ok": False, "message": "插件配置不可用", "applied": [], "issues": []}
    applied: list[str] = []
    issues: list[str] = []
    mix_touched = False
    for key, value in (payload or {}).items():
        if key in _BOOL_KEYS:
            cfg[key] = bool(value)
            applied.append(key)
            continue
        if key in _INT_KEYS:
            low, high = _INT_KEYS[key]
            try:
                number = int(value)
            except (TypeError, ValueError):
                issues.append(f"{key}: 需要整数")
                continue
            if not low <= number <= high:
                issues.append(f"{key}: 取值范围 {low}–{high}")
                continue
            cfg[key] = number
            applied.append(key)
            if key in ("mix_keywords_pct", "mix_recommend_pct", "mix_popular_pct"):
                mix_touched = True
            continue
        if key == "keywords":
            if isinstance(value, str):
                items = [x.strip() for x in re.split(r"[\n,，;；]+", value) if x.strip()]
            elif isinstance(value, (list, tuple)):
                items = [str(x).strip() for x in value if str(x).strip()]
            else:
                issues.append("keywords: 需要列表或逗号分隔字符串")
                continue
            invalid: list[str] = []
            merged: dict[str, int | None] = {}
            for raw in items[:10]:
                tail_match = re.search(r"[:：]\s*(\d+)\s*%?\s*$", raw)
                if tail_match and not (0 <= int(tail_match.group(1)) <= 100):
                    invalid.append(raw)
                    continue
                parsed = logic.parse_keyword_entry(raw)
                if not parsed:
                    continue
                name, percent = parsed
                if name not in merged:
                    merged[name] = percent
                elif percent is not None and (
                    merged[name] is None or percent > merged[name]
                ):
                    merged[name] = percent
            if invalid:
                issues.append(f"keywords: 占比写法应为 关键词:0~100（{invalid[0]}）")
                continue
            names = list(merged)
            percents = logic.normalize_percent_values(list(merged.values()))
            cfg[key] = [f"{name}:{percent}" for name, percent in zip(names, percents)]
            applied.append(key)
            continue
        if key in _STR_KEYS:
            text = str(value or "").strip()
            if key == "dm_mode" and text not in _DM_MODES:
                issues.append("dm_mode: 只能是 off / only / both")
                continue
            if key == "dm_share_mode" and text not in _DM_SHARE_MODES:
                issues.append("dm_share_mode: 只能是 single / digest")
                continue
            if key == "no_subtitle_policy" and text not in _NO_SUBTITLE_POLICIES:
                issues.append("no_subtitle_policy: 只能是 skip / summary / comments / vision")
                continue
            if key == "run_at":
                matched = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
                if not matched or int(matched.group(1)) > 23 or int(matched.group(2)) > 59:
                    issues.append("run_at: 格式应为 HH:MM（00:00–23:59）")
                    continue
            cfg[key] = text
            applied.append(key)
            continue
        issues.append(f"{key}: 不允许修改")
    if mix_touched:
        values = [
            int(cfg.get("mix_keywords_pct", 100) or 0),
            int(cfg.get("mix_recommend_pct", 0) or 0),
            int(cfg.get("mix_popular_pct", 0) or 0),
        ]
        values = [max(0, min(100, value)) for value in values]
        (
            cfg["mix_keywords_pct"],
            cfg["mix_recommend_pct"],
            cfg["mix_popular_pct"],
        ) = logic.normalized_source_percentages(*values)
    return {
        "ok": not issues,
        "applied": applied,
        "issues": issues,
        "message": "已保存" if not issues else "部分字段未保存",
    }


def register_web_routes(context, plugin) -> int:
    """注册页面 API；旧版 AstrBot 没有 register_web_api 时静默跳过。"""
    if not hasattr(context, "register_web_api"):
        return 0
    api = BiliScrollWebAPI(plugin)
    registered = 0
    for suffix, attr, methods, description in ROUTE_SPECS:
        handler = getattr(api, attr, None)
        if not callable(handler):
            _LOGGER.error("页面 API 缺少处理方法：%s（/%s 未注册）", attr, suffix)
            continue
        try:
            context.register_web_api(
                f"/{PLUGIN_NAME}/{suffix}", handler, methods, description
            )
            registered += 1
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("注册页面 API /%s 失败：%s", suffix, exc)
    return registered


class BiliScrollWebAPI:
    """每个方法对应一个前端端点，返回可 JSON 序列化的 dict。"""

    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self._user_cache: tuple[float, str] = (0.0, "")

    # ---------------- 内部工具 ----------------
    def _next_run_text(self) -> str:
        """给页面显示「下次自动运行」的人话文本。"""
        plugin = self.plugin
        if not bool(plugin._cfg("auto_run", False)):
            return "关"
        parsed = plugin._parse_hhmm(str(plugin._cfg("run_at", "22:00")))
        if not parsed:
            return "时间格式无效"
        hour, minute = parsed
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        day = "今天"
        if target <= now:
            target += timedelta(days=1)
            day = "明天"
        text = f"{day} {hour:02d}:{minute:02d}"
        if bool(plugin._cfg("peak_valley", False)) and not plugin._in_valley(target):
            text += "（谷时窗限制）"
        return text
    async def _login_name(self, timeout: float = 8.0) -> tuple[bool, str]:
        """登录名缓存：避免页面每 20 秒轮询都打一次 B 站接口（防风控/省请求）。"""
        client = self.plugin.client
        if not client.has_credential():
            return False, ""
        state = self.plugin.store.load_state()
        cached_name = str(state.get("login_name") or "").strip()
        if cached_name:
            return True, cached_name
        try:
            info = await asyncio.wait_for(client.self_info(), timeout=timeout)
            name = str((info or {}).get("name") or "").strip()
            if name:
                self._save_login_name(name)
            return True, name
        except NotLoggedInError:
            return False, ""
        except Exception:  # noqa: BLE001
            # 有凭据但探测失败（网络/风控）：仍按已登录处理，名字留空
            return True, ""

    def _save_login_name(self, name: str) -> None:
        try:
            state = self.plugin.store.load_state()
            state["login_name"] = str(name or "")
            state["login_name_ts"] = int(time.time())
            self.plugin.store.save_state(state)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- GET /status ----------------
    async def get_status(self) -> dict:
        plugin = self.plugin
        state = plugin.store.load_state()
        today = datetime.now().strftime("%Y-%m-%d")
        report_text = plugin.store.read_report(today)
        picked = max(
            int((state.get("picked") or {}).get(today, 0)),
            logic.count_picked(report_text),
        )
        logged_in, name = await self._login_name()
        picked_items = state.get("last_picked") if state.get("last_picked_date") == today else []
        picked_items = picked_items or logic.parse_report_items(report_text)[:5]
        picked_items = [
            {
                "bvid": item.get("bvid", ""),
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "one_liner": item.get("one_liner", ""),
            }
            for item in picked_items[:5]
        ]
        sent_recent: list[str] = []
        try:
            sent_recent = [
                logic.sent_entry_line(entry)
                for entry in plugin.store.recent_sent(days=3, limit=5)
            ]
        except Exception:  # noqa: BLE001
            pass
        sent_today = 0
        last_sent = ""
        try:
            sent_entries = plugin.store.recent_sent(days=1, limit=50)
            sent_today = sum(
                1 for entry in sent_entries if str(entry.get("date") or "")[:10] == today
            )
            if sent_entries:
                last_sent = str(sent_entries[-1].get("time") or "")
        except Exception:  # noqa: BLE001
            pass
        return {
            "logged_in": logged_in,
            "user_name": name,
            "today": today,
            "picked_today": picked,
            "per_day": plugin._cfg_int("per_day", 20),
            "busy": bool(plugin._busy),
            "auto_run": bool(plugin._cfg("auto_run", False)),
            "run_at": str(plugin._cfg("run_at", "22:00")),
            "auto_comment": bool(plugin._cfg("auto_comment", False)),
            "comment_use_persona": bool(plugin._cfg("comment_use_persona", True)),
            "comment_guard_review": bool(plugin._cfg("comment_guard_review", True)),
            "comment_blocklist": str(plugin._cfg("comment_blocklist", "") or ""),
            "push_qq": bool(plugin._cfg("push_qq", True)),
            "mention_name": str(plugin._cfg("mention_name", "") or ""),
            "keywords": logic.effective_keywords(plugin._cfg("keywords", [])),
            "last_run": state.get("last_run_at") or "",
            "next_run": self._next_run_text(),
            "picked_items": picked_items,
            "sent_recent": sent_recent,
            "has_push_session": bool(plugin._umo),
            "report_exists": bool(report_text),
            "report_chars": len(report_text),
            "data_dir": str(plugin.store.data_dir),
            "dm_mode": str(plugin._cfg("dm_mode", "off") or "off"),
            "dm_share_mode": str(plugin._cfg("dm_share_mode", "single") or "single"),
            "dm_share_limit": plugin._cfg_int("dm_share_limit", 3, minimum=1),
            "dm_share_daily_limit": plugin._cfg_int(
                "dm_share_daily_limit", 10, minimum=1
            ),
            "dm_share_interval": plugin._cfg_int("dm_share_interval", 5, minimum=2),
            "dm_uid": plugin._cfg_int("dm_uid", 0),
            "dm_text_limit": plugin._cfg_int("dm_text_limit", 500, minimum=100),
            "sent_today": sent_today,
            "last_sent": last_sent,
            "inject_sent_context": bool(plugin._cfg("inject_sent_context", True)),
            "proactive_share": bool(plugin._cfg("proactive_share", False)),
            "proactive_idle_minutes": plugin._cfg_int("proactive_idle_minutes", 30, minimum=5),
            "proactive_max_per_day": plugin._cfg_int("proactive_max_per_day", 2),
            "proactive_min_gap_minutes": plugin._cfg_int(
                "proactive_min_gap_minutes", 120
            ),
            "proactive_quiet_start": str(
                plugin._cfg("proactive_quiet_start", "23:00") or "23:00"
            ),
            "proactive_quiet_end": str(
                plugin._cfg("proactive_quiet_end", "08:00") or "08:00"
            ),
            "proactive_source": str(plugin._cfg("proactive_source", "mixed") or "mixed"),
            "proactive_today": int(
                (state.get("proactive_shares") or {}).get(today, 0)
            ),
            "proactive_last_share": float(state.get("proactive_last_ts") or 0),
            "rounds_today": plugin._counter("rounds"),
            "rounds_per_day": plugin._cfg_int("rounds_per_day", 12),
            "llm_calls_today": plugin._counter("llm_calls"),
            "llm_budget_per_day": plugin._cfg_int("llm_budget_per_day", 200),
            "llm_remaining": max(
                0,
                plugin._cfg_int("llm_budget_per_day", 200)
                - plugin._counter("llm_calls"),
            )
            if plugin._cfg_int("llm_budget_per_day", 200)
            else -1,
            "api_calls_today": plugin._counter("api_calls"),
            "last_round_api_calls": int(state.get("last_round_api_calls") or 0),
            "last_round_risk": int(state.get("last_round_risk") or 0),
            "cooldown_remaining_min": max(
                0,
                int(
                    (
                        float(state.get("cooldown_until") or 0)
                        - datetime.now().timestamp()
                    )
                    // 60
                )
                + 1,
            )
            if float(state.get("cooldown_until") or 0) > datetime.now().timestamp()
            else 0,
            "config": {
                "keywords": logic.effective_keywords(plugin._cfg("keywords", [])),
                "keyword_weights": [
                    {"keyword": name, "percent": percent}
                    for name, percent in logic.effective_keyword_entries(
                        plugin._cfg("keywords", [])
                    )
                ],
                "mix_keywords_pct": logic.normalized_source_percentages(
                    plugin._cfg_int("mix_keywords_pct", 100),
                    plugin._cfg_int("mix_recommend_pct", 0),
                    plugin._cfg_int("mix_popular_pct", 0),
                )[0],
                "mix_recommend_pct": logic.normalized_source_percentages(
                    plugin._cfg_int("mix_keywords_pct", 100),
                    plugin._cfg_int("mix_recommend_pct", 0),
                    plugin._cfg_int("mix_popular_pct", 0),
                )[1],
                "mix_popular_pct": logic.normalized_source_percentages(
                    plugin._cfg_int("mix_keywords_pct", 100),
                    plugin._cfg_int("mix_recommend_pct", 0),
                    plugin._cfg_int("mix_popular_pct", 0),
                )[2],
                "per_day": plugin._cfg_int("per_day", 20),
                "days": plugin._cfg_int("days", 7, minimum=1),
                "max_candidates": plugin._cfg_int("max_candidates", 30, minimum=1),
                "run_at": str(plugin._cfg("run_at", "22:00")),
                "peak_valley": bool(plugin._cfg("peak_valley", False)),
                "auto_run": bool(plugin._cfg("auto_run", False)),
                "auto_comment": bool(plugin._cfg("auto_comment", False)),
                "nl_trigger": bool(plugin._cfg("nl_trigger", True)),
                "mention_name": str(plugin._cfg("mention_name", "") or ""),
                "comment_use_persona": bool(plugin._cfg("comment_use_persona", True)),
                "comment_guard_review": bool(plugin._cfg("comment_guard_review", True)),
                "comment_blocklist": str(plugin._cfg("comment_blocklist", "") or ""),
                "comment_persona_hint": str(plugin._cfg("comment_persona_hint", "") or ""),
                "comment_text_limit": plugin._cfg_int(
                    "comment_text_limit", 150, minimum=20
                ),
                "comment_per_round": min(3, plugin._cfg_int("comment_per_round", 1, minimum=1)),
                "comment_daily_limit": plugin._cfg_int(
                    "comment_daily_limit", 5, minimum=1
                ),
                "comment_per_up_daily": plugin._cfg_int(
                    "comment_per_up_daily", 1, minimum=1
                ),
                "comment_min_score": plugin._cfg_int(
                    "comment_min_score", 75, minimum=0
                ),
                "comment_min_interval_seconds": plugin._cfg_int(
                    "comment_min_interval_seconds", 120, minimum=30
                ),
                "comment_min_chars": plugin._cfg_int(
                    "comment_min_chars", 4, minimum=2
                ),
                "comment_dedupe_pct": plugin._cfg_int(
                    "comment_dedupe_pct", 85, minimum=0
                ),
                "push_qq": bool(plugin._cfg("push_qq", True)),
                "dm_mode": str(plugin._cfg("dm_mode", "off") or "off"),
                "dm_share_mode": str(plugin._cfg("dm_share_mode", "single") or "single"),
                "dm_share_limit": plugin._cfg_int("dm_share_limit", 3, minimum=1),
                "dm_share_daily_limit": plugin._cfg_int(
                    "dm_share_daily_limit", 10, minimum=1
                ),
                "dm_share_interval": plugin._cfg_int(
                    "dm_share_interval", 5, minimum=2
                ),
                "dm_uid": plugin._cfg_int("dm_uid", 0),
                "dm_text_limit": plugin._cfg_int("dm_text_limit", 500, minimum=100),
                "llm_budget_per_day": plugin._cfg_int("llm_budget_per_day", 200),
                "rounds_per_day": plugin._cfg_int("rounds_per_day", 12),
                "risk_abort_threshold": plugin._cfg_int(
                    "risk_abort_threshold", 3, minimum=1
                ),
                "risk_cooldown_minutes": plugin._cfg_int(
                    "risk_cooldown_minutes", 60, minimum=5
                ),
                "judge_cache_days": plugin._cfg_int("judge_cache_days", 7),
                "read_comments": bool(plugin._cfg("read_comments", True)),
                "no_subtitle_policy": str(
                    plugin._cfg("no_subtitle_policy", "skip") or "skip"
                ),
                "vision_provider_id": str(plugin._cfg("vision_provider_id", "") or ""),
                "vision_max_frames": plugin._cfg_int(
                    "vision_max_frames", 5, minimum=2
                ),
                "vision_max_seconds": plugin._cfg_int(
                    "vision_max_seconds", 120, minimum=10
                ),
                "vision_max_mb": plugin._cfg_int("vision_max_mb", 30, minimum=5),
                "vision_timeout": plugin._cfg_int("vision_timeout", 120, minimum=30),
                "ffmpeg_path": str(plugin._cfg("ffmpeg_path", "ffmpeg") or "ffmpeg"),
                "preference_weight": plugin._cfg_int(
                    "preference_weight", 70, minimum=0
                ),
                "pick_threshold": plugin._cfg_int("pick_threshold", 65, minimum=0),
                "inject_sent_context": bool(plugin._cfg("inject_sent_context", True)),
                "proactive_share": bool(plugin._cfg("proactive_share", False)),
                "proactive_idle_minutes": plugin._cfg_int(
                    "proactive_idle_minutes", 30, minimum=5
                ),
                "proactive_max_per_day": plugin._cfg_int("proactive_max_per_day", 2),
                "proactive_min_gap_minutes": plugin._cfg_int(
                    "proactive_min_gap_minutes", 120
                ),
                "proactive_quiet_start": str(
                    plugin._cfg("proactive_quiet_start", "23:00") or "23:00"
                ),
                "proactive_quiet_end": str(
                    plugin._cfg("proactive_quiet_end", "08:00") or "08:00"
                ),
                "proactive_source": str(
                    plugin._cfg("proactive_source", "mixed") or "mixed"
                ),
            },
        }

    # ---------------- POST /login/start ----------------
    async def login_start(self) -> dict:
        try:
            png = await self.plugin.client.qr_start()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"生成二维码失败：{exc}"}
        try:
            body = base64.b64encode(png.read_bytes()).decode("ascii")
        except OSError as exc:
            return {"ok": False, "message": f"读取二维码失败：{exc}"}
        return {"ok": True, "image": "data:image/png;base64," + body}

    # ---------------- GET /login/poll ----------------
    async def login_poll(self) -> dict:
        try:
            state = await self.plugin.client.qr_poll()
        except Exception as exc:  # noqa: BLE001
            return {"state": "error", "message": str(exc)}
        payload: dict = {"state": state}
        if state == "done":
            self._user_cache = (0.0, "")
            logged_in, name = await self._login_name()
            payload.update({"logged_in": logged_in, "user_name": name})
        return payload

    # ---------------- POST /login/cancel ----------------
    async def login_cancel(self) -> dict:
        self.plugin.client.cancel_login()
        return {"ok": True}

    # ---------------- POST /logout ----------------
    async def logout(self) -> dict:
        self.plugin.store.clear_credential()
        self.plugin.client.reload_credential()
        self._user_cache = (0.0, "")
        try:
            state = self.plugin.store.load_state()
            state.pop("login_name", None)
            state.pop("login_name_ts", None)
            self.plugin.store.save_state(state)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True}

    # ---------------- POST /config ----------------
    async def save_config(self) -> dict:
        """页面快捷设置：读取请求体并写回插件配置（会同步保存到配置页）。"""
        try:
            from astrbot.api.web import request as web_request

            payload = await web_request.json() or {}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"读取请求体失败：{exc}", "applied": [], "issues": []}
        if not isinstance(payload, dict):
            return {"ok": False, "message": "请求体需要 JSON 对象", "applied": [], "issues": []}
        result = apply_config_updates(self.plugin, payload)
        if result.get("applied"):
            try:
                save_async = getattr(self.plugin.cfg, "save_config_async", None)
                if callable(save_async):
                    await save_async()
                else:
                    save_sync = getattr(self.plugin.cfg, "save_config", None)
                    if callable(save_sync):
                        save_sync()
            except Exception as exc:  # noqa: BLE001
                result["ok"] = False
                result["message"] = f"已写入内存，但保存到配置页失败：{exc}"
        return result

    # ---------------- POST /run ----------------
    async def run_round(self) -> dict:
        plugin = self.plugin
        if plugin._busy:
            return {"accepted": False, "message": "正在跑上一轮，等它结束再试"}
        if not plugin.client.has_credential():
            return {"accepted": False, "message": "还没登录，先扫码"}
        now_ts = time.time()
        state = plugin.store.load_state()
        cooldown_until = float(state.get("cooldown_until") or 0)
        if cooldown_until > now_ts:
            minutes = int((cooldown_until - now_ts) // 60) + 1
            return {"accepted": False, "message": f"B站风控冷却中，约 {minutes} 分钟后再试"}
        # 页面 /run 不接受 force：今日挑满时提前拒绝，避免“已开始”却秒结束
        per_day = plugin._cfg_int("per_day", 20)
        if per_day:
            today = datetime.now().strftime("%Y-%m-%d")
            picked = max(
                int((state.get("picked") or {}).get(today, 0)),
                logic.count_picked(plugin.store.read_report(today)),
            )
            if picked >= per_day:
                return {
                    "accepted": False,
                    "message": (
                        f"今天已挑满 {picked}/{per_day} 条，页面不提供强制；"
                        "需要再跑请发 /bili_force 或调高「每日挑中」"
                    ),
                }
        rounds_cap = plugin._cfg_int("rounds_per_day", 12)
        rounds_used = plugin._counter("rounds")
        if rounds_cap and rounds_used >= rounds_cap:
            return {"accepted": False, "message": f"今天已跑 {rounds_used}/{rounds_cap} 轮，明天再刷"}
        # 没有登记推送会话也允许跑：结果照写日报，在页面里看；聊天推送需先 /bili_scroll 登记
        plugin._round_task = asyncio.create_task(
            plugin._run_round(plugin._umo or "", source="web")
        )
        if plugin._umo:
            return {"accepted": True, "message": "已开始，1–2 分钟后回来看日报"}
        return {
            "accepted": True,
            "message": "已开始（还没有推送会话，结果在页面「查看今日日报」里看；想在聊天里收日报，先发一次 /bili_scroll）",
        }

    # ---------------- POST /proactive ----------------
    async def run_proactive(self) -> dict:
        """页面手动触发一次「主动刷视频」（忽略沉寂/安静时段，仍受每日次数限制）。"""
        plugin = self.plugin
        if plugin._busy:
            return {"ok": False, "accepted": False, "message": "正在跑另一轮，等它结束再试"}
        if not plugin.client.has_credential():
            return {"ok": False, "accepted": False, "message": "还没登录，先扫码"}
        if not bool(plugin._cfg("auto_comment", False)):
            return {
                "ok": False,
                "accepted": False,
                "message": "公开评论总开关 auto_comment 未开；主动分享也走评论通道，开启后才会发",
            }
        if not str(plugin._cfg("mention_name", "") or "").strip():
            return {
                "ok": False,
                "accepted": False,
                "message": "先去配置里填「要 @ 的 B 站昵称」，主动分享靠评论 @ 通知",
            }
        state = plugin.store.load_state()
        today = datetime.now().strftime("%Y-%m-%d")
        max_day = plugin._cfg_int("proactive_max_per_day", 2)
        shares = int((state.get("proactive_shares") or {}).get(today, 0))
        if max_day and shares >= max_day:
            return {
                "ok": False,
                "accepted": False,
                "message": f"今天已经主动分享 {shares}/{max_day} 次了，明天再刷或调高「每天最多主动分享」",
            }
        plugin._proactive_task = asyncio.create_task(plugin._run_proactive(test=True))
        return {
            "ok": True,
            "accepted": True,
            "message": "开始刷了：读几条首页推荐，挑中会在 B 站评论 @ 你（约 10–40 秒）",
        }

    # ---------------- POST /preview/comment ----------------
    async def preview_comment(self) -> dict:
        """用人设生成一条评论预览（不发送），便于确认口吻。"""
        plugin = self.plugin
        now = time.time()
        last = float(getattr(plugin, "_last_preview_ts", 0) or 0)
        if now - last < 15:
            return {"ok": False, "message": "预览太频繁（15 秒一次），稍后再试"}
        plugin._last_preview_ts = now
        state = plugin.store.load_state()
        today = datetime.now().strftime("%Y-%m-%d")
        items = state.get("last_picked") if state.get("last_picked_date") == today else []
        if not items:
            # 当天没跑过或旧版本没存 last_picked：从今天的日报兜底解析
            items = logic.parse_report_items(plugin.store.read_report(today))[:5]
        if not items:
            return {"ok": False, "message": "还没有跑过一轮，先点「现在跑一轮」再来预览"}
        item = items[0]
        mention = str(plugin._cfg("mention_name", "") or "").strip()
        umo = plugin._umo or ""
        persona = ""
        try:
            persona = await plugin._persona_prompt(umo)
        except Exception:  # noqa: BLE001
            persona = ""
        try:
            text = await plugin._make_comment_text(
                item, mention, str(item.get("one_liner") or ""), umo
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"预览失败：{exc}"}
        if not text:
            return {"ok": False, "message": "没有生成可用的评论（检查模型调用预算/配置）"}
        return {
            "ok": True,
            "bvid": item.get("bvid", ""),
            "title": item.get("title", ""),
            "text": text,
            "persona_len": len(persona),
            "used_persona": bool(persona)
            and bool(plugin._cfg("comment_use_persona", True)),
            "warning": "" if mention else "没填「要 @ 的昵称」，真正的自动评论会跳过",
        }

    # ---------------- GET /report/today ----------------
    async def get_today_report(self) -> dict:
        today = datetime.now().strftime("%Y-%m-%d")
        text = self.plugin.store.read_report(today)
        return {"date": today, "exists": bool(text), "markdown": text}

    # ---------------- GET /report/<date> ----------------
    async def get_report_by_date(self, date: str = "") -> dict:
        date = str(date or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            return {"ok": False, "message": "日期格式应为 YYYY-MM-DD"}
        text = self.plugin.store.read_report(date)
        return {"ok": True, "date": date, "exists": bool(text), "markdown": text}

    # ---------------- GET /reports ----------------
    async def list_reports(self) -> dict:
        out_dir = self.plugin.store.out_dir
        items: list[dict] = []
        try:
            for path in sorted(out_dir.glob("*.md"), reverse=True)[:30]:
                items.append({"date": path.stem, "size": path.stat().st_size})
        except OSError as exc:
            _LOGGER.warning("列日报失败：%s", exc)
        return {"items": items}
