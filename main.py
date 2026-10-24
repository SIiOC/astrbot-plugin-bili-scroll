"""刷会儿 B 站 —— AstrBot 插件入口。

数据流：搜索关键词 → 判重 → 读字幕原文 → LLM 判断 → 写日报 → 主动推送；
可选（默认关）以用户本人账号发一条评论 @ 用户，并在发送后复核是否公开可见。

安全边界：
- 命令全部要求 ADMIN（避免群成员替主人触发评论/登录覆盖）。
- 只读工具为主；唯一写操作是评论，默认关闭且每轮限量。
- 所有落盘都在 data/plugin_data/astrbot_plugin_bili_scroll/ 下。
- 视频标题/字幕/评论里的文字一律当素材，不当作指令（见 logic.DEFAULT_JUDGE_SYSTEM）。
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import datetime

from astrbot.api import AstrBotConfig, FunctionTool
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from . import logic, vision
from .bili_client import BiliClient, NotLoggedInError
from .store import Store

PLUGIN_NAME = "astrbot_plugin_bili_scroll"
COMMENT_VERIFY_DELAY = 20  # 评论发出后等待多少秒再复核可见性（测试里可覆盖）
CANDIDATE_INTERVAL = 0.5  # 候选之间最小间隔（秒），降低 -412 风险


@dataclass
class BiliSentHistoryTool(FunctionTool):
    """LLM 工具：让 bot 自己查「我给用户发过什么」。"""

    name: str = "bili_sent_history"
    description: str = (
        "查询本插件（B站助手）最近给用户发送过的日报 / B站私信 / B站评论记录。"
        "当用户问「你给我发过什么」「今天的B站日报呢」「你评论过哪些视频」时调用。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "往前查几天，默认 7。",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回几条，默认 20。",
                },
            },
            "required": [],
        }
    )
    plugin: object = None

    async def run(self, event: AstrMessageEvent, days: int = 7, limit: int = 20) -> str:
        plugin = self.plugin
        if plugin is None:
            return "发送记录不可用（插件未就绪）。"
        # 发送记录只在主人登记的推送会话里可用，避免陌生人/群里被问出来
        if event is not None:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if not plugin._umo or umo != plugin._umo:
                return "这个工具只在主人登记的推送会话里可用，已拒绝。"
            if not plugin._is_private_event(event):
                return "这个工具只在主人私聊里可用，群聊已拒绝。"
        try:
            entries = plugin.store.recent_sent(days=int(days or 7), limit=int(limit or 20))
        except Exception as exc:  # noqa: BLE001
            return f"读取发送记录失败：{exc}"
        return logic.render_sent_history(entries) or "最近没有发送记录。"


class BiliScrollPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.cfg = config or {}
        self.store = Store(get_astrbot_plugin_data_path() + "/" + PLUGIN_NAME)
        self.client = BiliClient(self.store, proxy=self._cfg("proxy", ""))
        self._umo: str | None = None
        self._loop_task: asyncio.Task | None = None
        self._login_task: asyncio.Task | None = None
        self._round_task: asyncio.Task | None = None
        self._proactive_task: asyncio.Task | None = None
        self._busy = False

    # ---------------- 生命周期 ----------------
    async def initialize(self):
        self._umo = await self._load_umo()
        self._verdict_cache = self.store.load_verdict_cache()
        try:
            removed = vision.cleanup_stale(self.store.data_dir / "tmp", 3600)
            if removed:
                self.logger.info("[bili-scroll] 已清理抽帧临时文件 %s 个", removed)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[bili-scroll] 清理抽帧临时文件失败：%s", exc)
        if bool(self._cfg("debug", False)):
            import logging as _logging

            _logging.getLogger("astrbot_plugin_bili_scroll").setLevel(_logging.DEBUG)
            self.logger.info("[bili-scroll] debug=true，已开启调试日志")
        self._loop_task = asyncio.create_task(self._auto_loop())
        state = self.store.load_state()
        if not state.get("last_user_active_ts"):
            state["last_user_active_ts"] = datetime.now().timestamp()
            self.store.save_state(state)
        self._proactive_task = asyncio.create_task(self._proactive_loop())
        self._register_web_routes()
        self._register_llm_tools()
        self.logger.info("[bili-scroll] 已启动，数据目录：%s", self.store.data_dir)

    def _register_web_routes(self) -> None:
        """注册 WebUI 插件页面 API（旧版 AstrBot 无此能力时静默跳过）。"""
        try:
            from .web_api import register_web_routes

            count = register_web_routes(self.context, self)
            self.logger.info("[bili-scroll] 已注册页面 API：%s 个", count)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[bili-scroll] 页面 API 注册失败（不影响聊天命令）：%s", exc)

    def _register_llm_tools(self) -> None:
        """注册「查发送记录」工具（旧版框架不支持的场合静默跳过）。"""
        try:
            self.context.add_llm_tools(BiliSentHistoryTool(plugin=self))
            self.logger.info("[bili-scroll] 已注册 LLM 工具：bili_sent_history")
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[bili-scroll] 注册 LLM 工具失败（不影响其它功能）：%s", exc)

    async def terminate(self):
        for task in (self._loop_task, self._login_task, self._round_task, self._proactive_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning("[bili-scroll] 停止后台任务时出错：%s", exc)
        self.logger.info("[bili-scroll] 已停止")

    # ---------------- 配置 ----------------
    def _cfg(self, key: str, default):
        try:
            value = self.cfg.get(key, default)
        except Exception:  # noqa: BLE001
            return default
        if value is None or value == "":
            return default
        return value

    def _cfg_int(self, key: str, default: int, *, minimum: int = 0) -> int:
        try:
            return max(minimum, int(self._cfg(key, default)))
        except (TypeError, ValueError):
            return default

    # ---------------- 每日用量计数（LLM 调用 / 轮次数） ----------------
    def _counter(self, name: str) -> int:
        state = self.store.load_state()
        today = datetime.now().strftime("%Y-%m-%d")
        return int((state.get(name) or {}).get(today, 0))

    def _inc_counter(self, name: str, delta: int = 1) -> int:
        state = self.store.load_state()
        today = datetime.now().strftime("%Y-%m-%d")
        data = dict(state.get(name) or {})
        data[today] = int(data.get(today, 0)) + max(0, int(delta))
        state[name] = {key: data[key] for key in sorted(data)[-14:]}
        self.store.save_state(state)
        return data[today]

    def _llm_remaining(self) -> int:
        budget = self._cfg_int("llm_budget_per_day", 200)  # 0 = 不限
        if not budget:
            return 10**6
        return max(0, budget - self._counter("llm_calls"))

    async def _llm_generate(
        self,
        *,
        umo: str,
        prompt: str,
        system_prompt: str,
        timeout: float,
        image_urls: list[str] | None = None,
        provider_id: str = "",
    ):
        """统一 LLM 入口：先查/记每日调用预算，再调用宿主模型（本轮所有 LLM 调用都走这里）。"""
        budget = self._cfg_int("llm_budget_per_day", 200)
        used = self._counter("llm_calls")
        if budget and used >= budget:
            raise RuntimeError(f"今日模型调用已达上限（{used}/{budget} 次）")
        self._inc_counter("llm_calls")
        chat_provider_id = provider_id or await self.context.get_current_chat_provider_id(umo=umo)
        return await asyncio.wait_for(
            self.context.llm_generate(
                chat_provider_id=chat_provider_id,
                prompt=prompt,
                system_prompt=system_prompt,
                image_urls=image_urls,
            ),
            timeout=timeout,
        )

    @staticmethod
    def _parse_hhmm(raw: str) -> tuple[int, int] | None:
        try:
            hour, minute = str(raw).strip().split(":", 1)
            return max(0, min(23, int(hour))), max(0, min(59, int(minute)))
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _in_valley(now: datetime) -> bool:
        hour = now.hour
        return 12 <= hour < 14 or hour >= 18 or hour < 9

    # ---------------- 主动刷视频（会话沉寂时） ----------------
    def _touch_active(self) -> None:
        """记录主人会话最后一次活动，用于判断“沉寂”。"""
        try:
            state = self.store.load_state()
            state["last_user_active_ts"] = datetime.now().timestamp()
            self.store.save_state(state)
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("[bili-scroll] 记录活跃时间失败：%s", exc)

    def _proactive_quiet(self, now: datetime) -> bool:
        start = self._parse_hhmm(str(self._cfg("proactive_quiet_start", "23:00")))
        end = self._parse_hhmm(str(self._cfg("proactive_quiet_end", "08:00")))
        if not start or not end:
            return False
        minutes = now.hour * 60 + now.minute
        start_m = start[0] * 60 + start[1]
        end_m = end[0] * 60 + end[1]
        if start_m == end_m:
            return False
        if start_m < end_m:
            return start_m <= minutes < end_m
        return minutes >= start_m or minutes < end_m

    def _proactive_due(self, now: datetime) -> tuple[bool, str]:
        """是否该主动刷一次；返回 (是否, 原因)。"""
        if not bool(self._cfg("proactive_share", False)):
            return False, "disabled"
        if self._busy:
            return False, "busy"
        if not self._umo:
            return False, "no_session"
        if not str(self._cfg("mention_name", "") or "").strip():
            return False, "no_mention"
        if not self.client.has_credential():
            return False, "no_login"
        state = self.store.load_state()
        if float(state.get("cooldown_until") or 0) > now.timestamp():
            return False, "risk"
        today = now.strftime("%Y-%m-%d")
        max_day = self._cfg_int("proactive_max_per_day", 2)
        shares = int((state.get("proactive_shares") or {}).get(today, 0))
        if max_day and shares >= max_day:
            return False, "quota"
        gap = max(0, self._cfg_int("proactive_min_gap_minutes", 120)) * 60
        last_share = float(state.get("proactive_last_ts") or 0)
        if gap and last_share and now.timestamp() - last_share < gap:
            return False, "gap"
        if self._proactive_quiet(now):
            return False, "quiet"
        idle = max(5, self._cfg_int("proactive_idle_minutes", 30)) * 60
        last_active = float(state.get("last_user_active_ts") or 0)
        if last_active and now.timestamp() - last_active < idle:
            return False, "active"
        return True, "ok"

    async def _proactive_loop(self) -> None:
        """每 60 秒检查一次：会话沉寂够了就自己刷一会儿。"""
        await asyncio.sleep(20)
        while True:
            try:
                await asyncio.sleep(60)
                due, reason = self._proactive_due(datetime.now())
                if not due:
                    if reason not in ("disabled", "active", "quiet", "quota", "gap"):
                        self.logger.debug("[bili-scroll] 主动刷跳过：%s", reason)
                    continue
                result = await self._run_proactive()
                self.logger.info("[bili-scroll] 主动刷视频：%s", result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("[bili-scroll] 主动刷循环异常：%s", exc)

    async def _run_proactive(self, *, test: bool = False) -> str:
        """像自己刷首页一样：推荐流 + 关键词混合 → 判断 → 评论 @ 分享。"""
        if self._busy:
            return "busy"
        if not test and not bool(self._cfg("proactive_share", False)):
            return "disabled"
        mention = str(self._cfg("mention_name", "") or "").strip()
        if not mention:
            return "no_mention"
        if not self._umo:
            return "no_session"
        if not self.client.has_credential():
            return "no_login"
        now = datetime.now()
        state = self.store.load_state()
        if float(state.get("cooldown_until") or 0) > now.timestamp():
            return "risk"
        today = now.strftime("%Y-%m-%d")
        max_day = self._cfg_int("proactive_max_per_day", 2)
        shares = int((state.get("proactive_shares") or {}).get(today, 0))
        if max_day and shares >= max_day:
            return "quota"
        if not test:
            gap = max(0, self._cfg_int("proactive_min_gap_minutes", 120)) * 60
            last_share = float(state.get("proactive_last_ts") or 0)
            if gap and last_share and now.timestamp() - last_share < gap:
                return "gap"
            if self._proactive_quiet(now):
                return "quiet"
            idle = max(5, self._cfg_int("proactive_idle_minutes", 30)) * 60
            last_active = float(state.get("last_user_active_ts") or 0)
            if last_active and now.timestamp() - last_active < idle:
                return "active"
        self._busy = True
        self.client.call_count = 0
        self.client.risk_count = 0
        seen_entries: list[tuple[str, str, str]] = []
        try:
            source = str(self._cfg("proactive_source", "mixed") or "mixed").strip().lower()
            entries = logic.effective_keyword_entries(self._cfg("keywords", []))[:3]
            keywords = [name for name, _weight in entries]
            keyword_weights = [weight for _name, weight in entries]
            days = max(1, self._cfg_int("days", 7, minimum=1))
            candidates: list[dict] = []
            if source == "mixed":
                mix_keywords = self._cfg_int("mix_keywords_pct", 100)
                mix_recommend = self._cfg_int("mix_recommend_pct", 0)
                mix_popular = self._cfg_int("mix_popular_pct", 0)
                if mix_recommend or mix_popular or mix_keywords != 100:
                    budgets = logic.source_budgets(
                        8, mix_keywords, mix_recommend, mix_popular
                    )
                else:
                    # 旧行为：推荐流 + 关键词各一半
                    budgets = {"keywords": 4, "recommend": 4, "popular": 0}
                pools: dict[str, list[dict]] = {}
                if budgets.get("recommend"):
                    try:
                        pools["recommend"] = await self.client.homepage_feed(
                            limit=max(10, budgets["recommend"] * 2)
                        )
                    except Exception as exc:  # noqa: BLE001
                        self.logger.warning("[bili-scroll] 首页推荐读取失败：%s", exc)
                if budgets.get("keywords"):
                    try:
                        pools["keywords"] = await self.client.search_keywords(
                            keywords,
                            days=days,
                            limit=budgets["keywords"],
                            weights=keyword_weights,
                        )
                        for keyword_item in pools["keywords"]:
                            keyword_item.setdefault("source", "keyword")
                    except Exception as exc:  # noqa: BLE001
                        self.logger.warning("[bili-scroll] 主动刷关键词搜索失败：%s", exc)
                if budgets.get("popular"):
                    try:
                        pools["popular"] = await self.client.popular_feed(
                            limit=max(10, budgets["popular"] * 2)
                        )
                    except Exception as exc:  # noqa: BLE001
                        self.logger.warning("[bili-scroll] 主动刷热门读取失败：%s", exc)
                candidates = logic.weighted_merge(
                    pools, budgets, budgets["keywords"] + budgets["recommend"] + budgets["popular"]
                )
            elif source == "recommend":
                try:
                    candidates = await self.client.homepage_feed(limit=30)
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning("[bili-scroll] 首页推荐读取失败：%s", exc)
            else:
                try:
                    candidates = await self.client.search_keywords(
                        keywords, days=days, limit=6, weights=keyword_weights
                    )
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning("[bili-scroll] 主动刷关键词搜索失败：%s", exc)
            candidates = logic.dedupe_by_bvid(candidates)
            random.shuffle(candidates)
            seen = self.store.seen_bvids()
            unseen = [item for item in candidates if item.get("bvid") not in seen][:6]
            if not unseen:
                return "empty"

            min_chars = self._cfg_int("min_subtitle_chars", 30)
            risk_threshold = max(1, self._cfg_int("risk_abort_threshold", 3, minimum=1))
            picked: list[dict] = []
            for item in unseen:
                if self.client.risk_count >= risk_threshold:
                    break
                if self._llm_remaining() <= 0:
                    self.logger.warning("[bili-scroll] 主动刷：今日模型预算已用尽")
                    break
                try:
                    detail = await self.client.video_detail(item)
                    subtitle = await self.client.subtitle_text(detail)
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning("[bili-scroll] 主动刷跳过 %s：%s", item.get("bvid"), exc)
                    continue
                if len(subtitle) < min_chars:
                    visual, evidence = await self._fallback_evidence(detail, self._umo)
                    if not visual:
                        seen_entries.append((detail["bvid"], today, "跳过（无字幕/无语音）"))
                        await asyncio.sleep(CANDIDATE_INTERVAL)
                        continue
                    if evidence == "vision":
                        self.logger.info("[bili-scroll] 主动刷使用抽帧视觉：%s", detail["bvid"])
                else:
                    visual = ""
                verdict = await self._judge(
                    detail,
                    subtitle,
                    [],
                    visual=visual,
                    preference=logic.preference_value(detail),
                    umo=self._umo,
                    timeout=float(self._cfg("judge_timeout", 90) or 90),
                )
                await asyncio.sleep(CANDIDATE_INTERVAL)
                if not verdict["ok"] or not verdict["worth"]:
                    seen_entries.append((detail["bvid"], today, "跳过（判断不值得）"))
                    continue
                text = await self._make_comment_text(
                    detail, mention, verdict["one_liner"], self._umo, share=True
                )
                if not text:
                    continue
                result = await self.client.post_comment(detail, text)
                visible = False
                rpid = result.get("rpid") if isinstance(result, dict) else None
                if rpid:
                    try:
                        await asyncio.sleep(COMMENT_VERIFY_DELAY)
                        visible = await self.client.comment_visible(detail, rpid)
                    except Exception as exc:  # noqa: BLE001
                        self.logger.warning("[bili-scroll] 主动评论复核失败：%s", exc)
                picked.append({**detail, **verdict})
                seen_entries.append((detail["bvid"], today, "主动分享"))
                self._record_sent(
                    "comment",
                    detail["bvid"],
                    [detail],
                    extra={
                        "source": "proactive",
                        "bvid": detail.get("bvid", ""),
                        "rpid": rpid,
                        "text": text,
                        "mention": mention,
                        "visible": bool(visible),
                    },
                )
                self.logger.info(
                    "[bili-scroll] 主动分享：%s《%s》评论=%s", detail["bvid"], detail.get("title", ""), text[:60]
                )
                break

            if seen_entries:
                self.store.append_seen(seen_entries)
            if picked:
                state = self.store.load_state()
                share_map = dict(state.get("proactive_shares") or {})
                share_map[today] = int(share_map.get(today, 0)) + 1
                state["proactive_shares"] = {key: share_map[key] for key in sorted(share_map)[-14:]}
                state["proactive_last_ts"] = now.timestamp()
                self.store.save_state(state)
            if self.client.risk_count >= risk_threshold:
                cooldown_minutes = max(5, self._cfg_int("risk_cooldown_minutes", 60, minimum=5))
                state = self.store.load_state()
                state["cooldown_until"] = now.timestamp() + cooldown_minutes * 60
                self.store.save_state(state)
                self.logger.warning("[bili-scroll] 主动刷遇到连续风控，冷却 %s 分钟", cooldown_minutes)
                return "risk"
            return "shared" if picked else "nothing"
        finally:
            self._record_round_usage()
            self._busy = False

    # ---------------- 让 bot 知道自己发过什么 ----------------
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req) -> None:
        """生成前注入「最近发送过什么」，让 bot 能回答「你给我发过什么」。

        只对主人登记的推送会话生效：避免陌生人私聊/群里问一句就把 B 站日报与 BV 拿走。
        """
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "") if event is not None else ""
            if self._umo and umo == self._umo:
                self._touch_active()
            if not bool(self._cfg("inject_sent_context", True)):
                return
            if not self._umo or umo != self._umo:
                return
            # 群聊里同一会话的其它成员也能触发模型调用；发送记录只给主人私聊看
            if not self._is_private_event(event):
                return
            days = self._cfg_int("sent_context_days", 3, minimum=1)
            today = datetime.now().strftime("%Y-%m-%d")
            entries = self.store.recent_sent(days=days, limit=30)
            text = logic.build_sent_context(entries, today=today)
            if text:
                req.system_prompt = (req.system_prompt or "") + "\n\n" + text
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("[bili-scroll] 注入发送记录失败：%s", exc)

    # ---------------- 命令（全部 ADMIN） ----------------
    @filter.command("bili_login", alias={"B站登录"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def bili_login(self, event: AstrMessageEvent):
        """扫码登录 B 站（凭据只保存在本机插件数据目录）。"""
        if self._login_task and not self._login_task.done():
            yield event.plain_result("已有登录流程正在进行，请先扫码或等二维码过期。")
            return
        try:
            path = await self.client.qr_start()
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"生成二维码失败：{exc}")
            return
        self._login_task = asyncio.create_task(self._login_watch(event.unified_msg_origin))
        yield event.image_result(str(path))

    @filter.command("bili_scroll", alias={"刷b站"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def bili_scroll(self, event: AstrMessageEvent):
        """立即跑一轮，并把当前会话登记为日报推送目标。"""
        self._umo = event.unified_msg_origin
        await self._save_umo(self._umo)
        if self._busy:
            yield event.plain_result("正在跑另一轮，等它结束再试。")
            return
        self._round_task = asyncio.create_task(self._run_round(self._umo, source="manual"))
        yield event.plain_result("收到，开始刷一轮（会调判断模型，可能需要一两分钟）。")

    @filter.command("bili_force", alias={"强制刷b站"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def bili_force(self, event: AstrMessageEvent):
        """忽略今日配额，再跑一轮。"""
        self._umo = event.unified_msg_origin
        await self._save_umo(self._umo)
        if self._busy:
            yield event.plain_result("正在跑另一轮，等它结束再试。")
            return
        self._round_task = asyncio.create_task(
            self._run_round(self._umo, source="force", force=True)
        )
        yield event.plain_result("收到，忽略配额跑一轮。")

    @filter.regex(logic.NL_RUN_TRIGGER)
    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    async def bili_nl_scroll(self, event: AstrMessageEvent):
        """自然语言触发：「你去刷会视频吧」这类命令式短句（仅管理员）。"""
        if not bool(self._cfg("nl_trigger", True)):
            return
        text = event.get_message_str() if hasattr(event, "get_message_str") else ""
        if not logic.is_run_request(text):
            return

        def _stop() -> None:
            try:
                event.stop_event()  # 别再让大模型对着这句指令回一遍，避免双回复
            except Exception:  # noqa: BLE001
                pass

        self._umo = event.unified_msg_origin
        await self._save_umo(self._umo)
        if self._busy:
            _stop()
            yield event.plain_result("正在刷，等这一轮结束再说。")
            return
        cooldown_until = float(self.store.load_state().get("cooldown_until") or 0)
        now_ts = datetime.now().timestamp()
        if cooldown_until > now_ts:
            minutes = max(1, int((cooldown_until - now_ts) // 60) + 1)
            _stop()
            yield event.plain_result(f"B 站风控冷却中，约 {minutes} 分钟后再刷。")
            return
        self._round_task = asyncio.create_task(
            self._run_round(self._umo, source="natural")
        )
        _stop()
        yield event.plain_result("好，我去刷会儿视频，刷完告诉你。")

    @filter.command("bili_status", alias={"刷b站状态"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def bili_status(self, event: AstrMessageEvent):
        """查看登录状态、今日已挑条数与上次运行时间。"""
        state = self.store.load_state()
        today = datetime.now().strftime("%Y-%m-%d")
        picked = max(
            int((state.get("picked") or {}).get(today, 0)),
            logic.count_picked(self.store.read_report(today)),
        )
        per_day = self._cfg_int("per_day", 20)
        login_text = "未登录（/bili_login）"
        try:
            info = await asyncio.wait_for(self.client.self_info(), timeout=15)
            name = str(((info or {}).get("name") or "")).strip()
            login_text = f"已登录：{name}" if name else "已登录"
            if name:
                state["login_name"] = name
                state["login_name_ts"] = int(datetime.now().timestamp())
                self.store.save_state(state)
        except NotLoggedInError:
            pass
        except Exception as exc:  # noqa: BLE001
            login_text = f"登录态异常：{exc}"
        last_run = state.get("last_run_at") or "从未"
        auto_text = "开" if bool(self._cfg("auto_run", False)) else "关"
        comment_on = bool(self._cfg("auto_comment", False))
        comment_text = (
            f"开（每轮 ≤{self._cfg_int('comment_per_round', 1, minimum=1)} 条，"
            f"@ {self._cfg('mention_name', '') or '未填写'}）"
            if comment_on
            else "关"
        )
        yield event.plain_result(
            "\n".join(
                [
                    f"登录：{login_text}",
                    f"今日已挑：{picked}/{per_day if per_day else '不限'}",
                    f"上次运行：{last_run}",
                    f"自动运行：{auto_text}（{self._cfg('run_at', '22:00')}）",
                    f"自动评论：{comment_text}",
                    f"数据目录：{self.store.data_dir}",
                ]
            )
        )

    @filter.command("bili_sent", alias={"刷b站记录"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def bili_sent(self, event: AstrMessageEvent):
        """查看最近发给你的日报 / B站私信 / B站评论记录。"""
        entries = self.store.recent_sent(days=7, limit=20)
        yield event.plain_result(logic.render_sent_history(entries) or "最近没有发送记录。")

    # ---------------- 登录轮询 ----------------
    async def _login_watch(self, umo: str) -> None:
        last_state = ""
        try:
            for _ in range(36):  # 最长 3 分钟
                await asyncio.sleep(5)
                try:
                    state = await self.client.qr_poll()
                except Exception as exc:  # noqa: BLE001
                    await self._push(umo, f"登录轮询失败：{exc}")
                    return
                if state != last_state:
                    last_state = state
                    if state == "done":
                        await self._push(umo, "登录成功，可以 /bili_scroll 了。")
                        return
                    if state == "scanned":
                        await self._push(umo, "已扫码，请在手机上确认登录。")
                    elif state == "expired":
                        await self._push(umo, "二维码已过期，需要的话再 /bili_login 一次。")
                        return
                if state.startswith("error"):
                    await self._push(umo, f"登录失败：{state}")
                    return
            await self._push(umo, "二维码已过期，需要的话再 /bili_login 一次。")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[bili-scroll] 登录轮询任务异常：%s", exc)

    # ---------------- 自动运行循环 ----------------
    async def _auto_loop(self) -> None:
        await asyncio.sleep(10)
        while True:
            try:
                await asyncio.sleep(30)
                if not bool(self._cfg("auto_run", False)):
                    continue
                if not self._umo or self._busy:
                    continue
                now = datetime.now()
                if bool(self._cfg("peak_valley", False)) and not self._in_valley(now):
                    continue
                run_at = self._parse_hhmm(self._cfg("run_at", "22:00"))
                if run_at and (now.hour, now.minute) < run_at:
                    continue
                state = self.store.load_state()
                today = now.strftime("%Y-%m-%d")
                if state.get("last_auto_date") == today:
                    continue
                last_attempt = float(state.get("last_attempt_ts") or 0)
                if last_attempt and now.timestamp() - last_attempt < 1800:
                    continue
                # 风控冷却期不进入新一轮，避免每 30 秒刷一次 B 站
                if float(state.get("cooldown_until") or 0) > now.timestamp():
                    continue
                result = await self._run_round(self._umo, source="auto")
                state = self.store.load_state()
                if result in {"ok", "empty", "quota", "no_login", "limit"}:
                    state["last_auto_date"] = today
                else:
                    state["last_attempt_ts"] = now.timestamp()
                self.store.save_state(state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.logger.error("[bili-scroll] 自动循环异常：%s", exc)

    # ---------------- 一轮主流程 ----------------
    async def _run_round(self, umo: str, *, source: str = "manual", force: bool = False) -> str:
        if self._busy:
            return "busy"
        # 风控熔断冷却（自动/手动/force 都受约束）
        cooldown_until = float(self.store.load_state().get("cooldown_until") or 0)
        now_ts = datetime.now().timestamp()
        if cooldown_until and now_ts < cooldown_until:
            minutes = max(1, int((cooldown_until - now_ts) // 60) + 1)
            await self._push(umo, f"B 站风控冷却中，约 {minutes} 分钟后再试（可在页面看冷却状态）。")
            return "risk"
        rounds_cap = self._cfg_int("rounds_per_day", 12)  # 0 = 不限
        rounds_used = self._counter("rounds")
        if rounds_cap and rounds_used >= rounds_cap:
            await self._push(
                umo,
                f"今天已经跑了 {rounds_used} 轮（上限 {rounds_cap}），这是硬保护，"
                "/bili_force 也不会超过。明天再刷。",
            )
            return "limit"
        self._busy = True
        self.client.call_count = 0
        self.client.risk_count = 0
        try:
            return await self._run_round_inner(umo, source=source, force=force)
        except asyncio.CancelledError:
            raise
        except NotLoggedInError as exc:
            await self._push(umo, f"还没登录，先 /bili_login。\n（{exc}）")
            return "no_login"
        except Exception as exc:  # noqa: BLE001
            self.logger.error("[bili-scroll] 本轮失败：%s", exc)
            await self._push(umo, f"本轮失败：{exc}")
            return "error"
        finally:
            self._record_round_usage()
            self._busy = False

    def _record_round_usage(self) -> None:
        """记录本轮 B 站请求数与判断缓存（异常不影响主流程）。"""
        try:
            calls = int(getattr(self.client, "call_count", 0))
            risks = int(getattr(self.client, "risk_count", 0))
            if calls or risks:
                if calls:
                    self._inc_counter("api_calls", delta=calls)
                state = self.store.load_state()
                state["last_round_api_calls"] = calls
                state["last_round_risk"] = risks
                self.store.save_state(state)
            cache = getattr(self, "_verdict_cache", None)
            if isinstance(cache, dict):
                self.store.save_verdict_cache(cache)
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("[bili-scroll] 记录本轮用量失败：%s", exc)

    async def _run_round_inner(self, umo: str, *, source: str, force: bool) -> str:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        state = self.store.load_state()
        per_day = self._cfg_int("per_day", 20)  # 0 = 不限
        # 配额以 state 为准，同时用当天日报里的 ### BV 行数兜底（两套口径取较大值，避免漂移）
        picked_today = max(
            int((state.get("picked") or {}).get(today, 0)),
            logic.count_picked(self.store.read_report(today)),
        )
        if not force and per_day and picked_today >= per_day:
            await self._push(umo, f"今天已经挑满 {per_day} 条了（/bili_force 可强制再跑）。")
            return "quota"
        if not self.client.has_credential():
            await self._push(umo, "还没登录，先 /bili_login（读字幕需要登录态）。")
            return "no_login"
        # 真正开跑才计一轮（quota/no_login 不消耗轮次额度）
        self._inc_counter("rounds")

        remaining = 10**6 if (force or not per_day) else max(1, per_day - picked_today)
        entries = logic.effective_keyword_entries(self._cfg("keywords", []))[:10]
        keywords = [name for name, _weight in entries]
        keyword_weights = [weight for _name, weight in entries]
        display_keywords = [
            f"{name} {percent}%" for name, percent in entries
        ]
        days = max(1, min(30, self._cfg_int("days", 7, minimum=1)))
        limit = max(1, min(100, self._cfg_int("max_candidates", 30, minimum=1)))  # 硬上限 100 条候选
        min_chars = self._cfg_int("min_subtitle_chars", 30)
        judge_timeout = float(self._cfg("judge_timeout", 90) or 90)
        read_comments = bool(self._cfg("read_comments", True))

        # 来源比重：三项占比会先归一化到 100%（默认关键词 100 / 推荐 0 / 热门 0）
        mix_keywords = self._cfg_int("mix_keywords_pct", 100)
        mix_recommend = self._cfg_int("mix_recommend_pct", 0)
        mix_popular = self._cfg_int("mix_popular_pct", 0)
        budgets = logic.source_budgets(
            limit, mix_keywords, mix_recommend, mix_popular
        )
        pools: dict[str, list[dict]] = {}
        if budgets.get("keywords"):
            pools["keywords"] = await self.client.search_keywords(
                keywords, days=days, limit=budgets["keywords"], weights=keyword_weights
            )
            for keyword_item in pools["keywords"]:
                keyword_item.setdefault("source", "keyword")
        if budgets.get("recommend"):
            try:
                pools["recommend"] = await self.client.homepage_feed(
                    limit=max(10, budgets["recommend"] * 2)
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("[bili-scroll] 首页推荐来源读取失败：%s", exc)
        if budgets.get("popular"):
            try:
                pools["popular"] = await self.client.popular_feed(
                    limit=max(10, budgets["popular"] * 2)
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("[bili-scroll] 热门来源读取失败：%s", exc)
        candidates = logic.weighted_merge(pools, budgets, limit)
        seen = self.store.seen_bvids()
        unseen = [item for item in candidates if item["bvid"] not in seen]
        self.logger.info(
            "[bili-scroll] 候选 %s 条，未看过 %s 条（关键词 %s；来源 关键词%d/推荐%d/热门%d）",
            len(candidates),
            len(unseen),
            "、".join(display_keywords),
            budgets.get("keywords", 0),
            budgets.get("recommend", 0),
            budgets.get("popular", 0),
        )
        if not unseen:
            await self._push(umo, f"这一轮没有新视频（候选 {len(candidates)} 条都看过了）。")
            self._touch_run(now)
            return "empty"

        picked: list[dict] = []
        seen_entries: list[tuple[str, str, str]] = []
        digest_lines: list[str] = []
        skipped_no_subtitle = 0
        vision_used = 0
        summary_used = 0
        examined = 0
        transient_failures = 0

        risk_aborted = False
        risk_threshold = max(1, self._cfg_int("risk_abort_threshold", 3, minimum=1))
        for item in unseen:
            if len(picked) >= remaining:
                break
            if self.client.risk_count >= risk_threshold:
                risk_aborted = True
                break
            if self._llm_remaining() <= 0:
                budget = self._cfg_int("llm_budget_per_day", 200)
                self.logger.warning("[bili-scroll] 今日模型调用已达上限 %s，本轮提前结束", budget)
                await self._push(umo, f"今日模型调用预算已用尽（{budget} 次），本轮提前结束，明天再刷。")
                break
            examined += 1
            if examined > 1:
                await asyncio.sleep(CANDIDATE_INTERVAL)

            try:
                detail = await self.client.video_detail(item)
            except Exception as exc:  # noqa: BLE001
                # 瞬时失败：不写 seen、不烧候选，下一轮还能重试
                transient_failures += 1
                self.logger.warning("[bili-scroll] 跳过 %s（详情失败，不判重）：%s", item["bvid"], exc)
                continue

            try:
                subtitle = await self.client.subtitle_text(detail)
            except NotLoggedInError:
                raise
            except Exception as exc:  # noqa: BLE001
                transient_failures += 1
                self.logger.warning("[bili-scroll] 字幕失败 %s（不判重）：%s", detail["bvid"], exc)
                continue

            visual = ""
            evidence = "subtitle"
            comments: list[str] = []
            if len(subtitle) < min_chars:
                visual, evidence = await self._fallback_evidence(detail, umo)
                if evidence != "comments" and not visual:
                    skipped_no_subtitle += 1
                    seen_entries.append((detail["bvid"], today, "跳过（无字幕/无语音）"))
                    digest_lines.append(
                        f"- {today} · {detail['bvid']} · {detail['title']} · {detail['up']} · "
                        f"跳过（无字幕） · {detail['url']}"
                    )
                    continue

            if read_comments or evidence == "comments":
                try:
                    comments = await self.client.top_comments(detail, limit=6)
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning(
                        "[bili-scroll] 评论读取失败 %s：%s", detail["bvid"], exc
                    )
            if evidence == "comments" and len(comments) < 3:
                skipped_no_subtitle += 1
                seen_entries.append((detail["bvid"], today, "跳过（无字幕且评论不足）"))
                digest_lines.append(
                    f"- {today} · {detail['bvid']} · {detail['title']} · {detail['up']} · "
                    f"跳过（无字幕，评论不足） · {detail['url']}"
                )
                continue
            if evidence == "vision":
                vision_used += 1
            elif evidence == "summary":
                summary_used += 1

            verdict = await self._judge(
                detail,
                subtitle,
                comments,
                visual=visual,
                preference=logic.preference_value(detail),
                umo=umo,
                timeout=judge_timeout,
            )
            if not verdict["ok"]:
                # 判断失败同样不写 seen，避免把候选永久烧掉
                transient_failures += 1
                self.logger.warning(
                    "[bili-scroll] 判断失败 %s（不判重）：%s", detail["bvid"], verdict["reason"]
                )
                continue
            if not verdict["worth"]:
                seen_entries.append((detail["bvid"], today, "跳过（判断不值得）"))
                digest_lines.append(
                    f"- {today} · {detail['bvid']} · {detail['title']} · {detail['up']} · "
                    f"跳过（{verdict['reason'][:40]}） · {detail['url']}"
                )
                continue
            picked.append(
                {
                    **detail,
                    **verdict,
                    "comments": comments,
                    "visual": visual,
                    "evidence": evidence,
                }
            )
            seen_entries.append((detail["bvid"], today, "挑中"))
            digest_lines.append(
                f"- {today} · {detail['bvid']} · {detail['title']} · {detail['up']} · "
                f"挑中：{verdict['one_liner']}"
                + ("（无字幕·画面）" if evidence == "vision" else "")
                + " · " + detail["url"]
            )

        if vision_used or summary_used:
            self.logger.info(
                "[bili-scroll] 无字幕兜底：视觉 %s 条，AI 摘要 %s 条",
                vision_used,
                summary_used,
            )
        level = str(self._cfg("report_level", "full") or "full")
        report = logic.render_report(
            f"{today} {now.strftime('%H:%M')}",
            picked,
            skipped_no_subtitle=skipped_no_subtitle,
            total_examined=examined,
            keywords=keywords,
            level=level,
        )
        if level != "none":
            existing = self.store.read_report(today)
            if existing:
                body = "\n".join(report.splitlines()[1:]).strip()
                self.store.append_report(today, f"## 追加一轮 {now.strftime('%H:%M')}\n\n{body}")
            else:
                self.store.write_report(today, report)
        if digest_lines:
            self.store.append_digest(today[:7], digest_lines)
        self.store.append_seen(seen_entries)

        state = self.store.load_state()
        picked_map = dict(state.get("picked") or {})
        picked_map[today] = int(picked_map.get(today, 0)) + len(picked)
        state["picked"] = picked_map
        state["last_run_at"] = now.strftime("%Y-%m-%d %H:%M")
        state["last_source"] = source
        state["last_picked"] = [
            {
                key: item.get(key)
                for key in ("bvid", "title", "url", "up", "desc", "one_liner", "reason")
            }
            for item in picked[:5]
        ]
        state["last_picked_date"] = today
        self.store.save_state(state)

        if risk_aborted:
            cooldown_minutes = max(5, self._cfg_int("risk_cooldown_minutes", 60, minimum=5))
            state = self.store.load_state()
            state["cooldown_until"] = datetime.now().timestamp() + cooldown_minutes * 60
            self.store.save_state(state)
            self.logger.warning(
                "[bili-scroll] 连续 B 站风控 %s 次，熔断 %s 分钟",
                self.client.risk_count,
                cooldown_minutes,
            )
            await self._push(
                umo,
                f"B 站连续风控 {self.client.risk_count} 次，已熔断并暂停 {cooldown_minutes} 分钟。",
            )

        await self._deliver(umo, report, picked, skipped_no_subtitle, examined, level)
        if transient_failures and self._dm_mode() != "only":
            await self._push(umo, f"本轮有 {transient_failures} 条因网络/接口失败跳过，下一轮会重试。")
        if picked and bool(self._cfg("auto_comment", False)) and not risk_aborted:
            await self._maybe_comment(umo, picked)
        return "risk" if risk_aborted else "ok"

    def _touch_run(self, now: datetime) -> None:
        state = self.store.load_state()
        state["last_run_at"] = now.strftime("%Y-%m-%d %H:%M")
        self.store.save_state(state)

    # ---------------- LLM 判断 ----------------
    # ---------------- 无字幕兜底：AI 摘要 / 抽帧视觉 ----------------
    @staticmethod
    def _config_supports_image(provider_config: dict) -> bool:
        modalities = (provider_config or {}).get("modalities")
        if modalities == []:
            return True  # 宿主把空列表视为未配置
        return isinstance(modalities, list) and "image" in modalities

    async def _vision_provider_id(self, umo: str) -> str:
        configured = str(self._cfg("vision_provider_id", "") or "").strip()
        if configured:
            return configured
        manager = getattr(self.context, "provider_manager", None)
        if manager is None:
            return ""
        try:
            current = await self.context.get_current_chat_provider_id(umo=umo)
            provider = await manager.get_provider_by_id(current)
            if provider and self._config_supports_image(
                getattr(provider, "provider_config", {}) or {}
            ):
                return current
        except Exception:  # noqa: BLE001
            pass
        configs = getattr(manager, "providers_config", None) or []
        for provider_config in configs:
            if not isinstance(provider_config, dict):
                continue
            ptype = str(provider_config.get("type") or "").lower()
            if any(word in ptype for word in ("tts", "stt", "embedding", "rerank")):
                continue
            if self._config_supports_image(provider_config):
                provider_id = str(provider_config.get("id") or "").strip()
                if provider_id:
                    return provider_id
        return ""

    async def _vision_evidence(self, detail: dict, umo: str) -> str:
        if str(self._cfg("no_subtitle_policy", "skip") or "skip").strip().lower() != "vision":
            return ""
        binary = vision.ffmpeg_path(str(self._cfg("ffmpeg_path", "") or ""))
        if not binary:
            self.logger.warning("[bili-scroll] 没有 ffmpeg，无法抽帧")
            return ""
        provider_id = await self._vision_provider_id(umo)
        if not provider_id:
            self.logger.warning("[bili-scroll] 没有可用的多模态模型，无法抽帧判断")
            return ""
        max_mb = max(5, min(80, self._cfg_int("vision_max_mb", 30, minimum=5)))
        frame_count = max(2, min(8, self._cfg_int("vision_max_frames", 5, minimum=2)))
        max_seconds = max(
            10, min(300, self._cfg_int("vision_max_seconds", 120, minimum=10))
        )
        try:
            vision.cleanup_stale(self.store.data_dir / "tmp", 3600)
        except Exception:  # noqa: BLE001
            pass
        work_dir = (
            self.store.data_dir
            / "tmp"
            / f"vision-{detail['bvid']}-{datetime.now().strftime('%H%M%S%f')}"
        )
        video_path = None
        try:
            video_path = await self.client.download_video(detail, max_mb=max_mb)
            frames = await vision.extract_frames(
                video_path,
                work_dir,
                duration=float(detail.get("duration") or 0),
                count=frame_count,
                max_seconds=max_seconds,
                ffmpeg=binary,
            )
            if not frames:
                self.logger.warning("[bili-scroll] 抽帧失败：%s", detail["bvid"])
                return ""
            system, prompt = vision.build_vision_prompts(detail, len(frames))
            response = await self._llm_generate(
                umo=umo,
                prompt=prompt,
                system_prompt=system,
                timeout=float(self._cfg("vision_timeout", 120) or 120),
                image_urls=[str(path) for path in frames],
                provider_id=provider_id,
            )
            visual = vision.clean_visual(getattr(response, "completion_text", ""), 600)
            if visual:
                self.logger.info(
                    "[bili-scroll] 视觉证据 %s：%s（%s 帧，模型 %s）",
                    detail["bvid"],
                    visual[:60],
                    len(frames),
                    provider_id,
                )
            return visual
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[bili-scroll] 抽帧视觉失败 %s：%s", detail.get("bvid"), exc)
            return ""
        finally:
            try:
                if video_path:
                    video_path.unlink(missing_ok=True)
            except OSError:
                pass
            vision.remove_tree(work_dir)

    async def _fallback_evidence(self, detail: dict, umo: str) -> tuple[str, str]:
        """无字幕时的兜底证据：返回 (文字, 类型)。类型：vision / summary / comments / 空。"""
        policy = str(self._cfg("no_subtitle_policy", "skip") or "skip").strip().lower()
        if policy == "vision":
            try:
                visual = await self._vision_evidence(detail, umo)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("[bili-scroll] 视觉证据异常 %s：%s", detail.get("bvid"), exc)
                visual = ""
            if visual:
                return visual, "vision"
            policy = "summary"  # 视觉不可用时自动退到官方摘要
        if policy == "summary":
            try:
                summary = await self.client.ai_summary(detail)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("[bili-scroll] AI 摘要失败 %s：%s", detail.get("bvid"), exc)
                summary = ""
            if summary:
                return summary, "summary"
            return "", ""
        if policy == "comments":
            return "", "comments"
        return "", ""

    async def _judge(
        self,
        item: dict,
        subtitle: str,
        comments: list[str],
        *,
        umo: str,
        timeout: float,
        visual: str = "",
        preference: int = 60,
    ) -> dict:
        # 判断缓存：同一 BV 在重试/强制重跑时复用结论，省模型调用
        cache = getattr(self, "_verdict_cache", None)
        if cache is None:
            cache = self._verdict_cache = self.store.load_verdict_cache()
        ttl_days = self._cfg_int("judge_cache_days", 7)
        preference_weight = self._cfg_int("preference_weight", 70, minimum=0)
        threshold = self._cfg_int("pick_threshold", 65, minimum=0)
        key = str(item.get("bvid") or "")
        cache_key = f"{key}|p{int(preference)}|w{int(preference_weight)}|t{int(threshold)}"
        if ttl_days and key and isinstance(cache, dict):
            hit = cache.get(cache_key)
            if isinstance(hit, dict) and isinstance(hit.get("verdict"), dict):
                age = datetime.now().timestamp() - float(hit.get("ts") or 0)
                if age <= ttl_days * 86400:
                    self.logger.info("[bili-scroll] 命中判断缓存：%s", key)
                    verdict = dict(hit["verdict"])
                    verdict["cached"] = True
                    return verdict

        system_prompt, user_prompt = logic.build_judge_prompt(
            item, subtitle, comments, visual=visual
        )
        try:
            response = await self._llm_generate(
                umo=umo,
                prompt=user_prompt,
                system_prompt=system_prompt,
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            self.logger.warning("[bili-scroll] 判断超时：%s", item["bvid"])
            return logic.render_verdict(None, fallback_reason="判断超时", ok=False)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[bili-scroll] 判断失败 %s：%s", item["bvid"], exc)
            return logic.render_verdict(None, fallback_reason=f"判断失败：{exc}", ok=False)

        text = getattr(response, "completion_text", "") or ""
        data = logic.extract_json(text)
        if data is None:
            return logic.render_verdict(None, fallback_reason="模型未返回合法 JSON", ok=False)
        verdict = logic.render_verdict(data, ok=True)
        score = logic.score_candidate(preference, verdict["quality"], preference_weight)
        verdict["preference"] = int(preference)
        verdict["preference_weight"] = int(preference_weight)
        verdict["score"] = score
        verdict["threshold"] = int(threshold)
        # 最终是否入选：用户偏好权重更高，模型质量只占 (100-preference_weight)%
        verdict["worth"] = score >= int(threshold)
        if ttl_days and key and isinstance(cache, dict):
            cache[cache_key] = {"verdict": verdict, "ts": datetime.now().timestamp()}
        return verdict

    # ---------------- 人设 ----------------
    @staticmethod
    def _persona_text(persona) -> str:
        if isinstance(persona, dict):
            return str(persona.get("prompt") or persona.get("system_prompt") or "").strip()
        if persona is not None:
            return str(
                getattr(persona, "prompt", "") or getattr(persona, "system_prompt", "") or ""
            ).strip()
        return ""

    async def _persona_prompt(self, umo: str) -> str:
        """取当前会话人设的 system prompt；取不到回落 AstrBot 默认人设，再取不到返回空串。"""
        pm = getattr(self.context, "persona_manager", None)
        if pm is None:
            return ""
        # 优先：该会话选中的 persona（与 QQ 里聊天用的一致）
        try:
            conv_mgr = getattr(self.context, "conversation_manager", None)
            conv_persona_id = None
            if conv_mgr is not None and umo:
                cid = await conv_mgr.get_curr_conversation_id(umo)
                if cid:
                    conv = await conv_mgr.get_conversation(umo, cid)
                    conv_persona_id = getattr(conv, "persona_id", None) if conv else None
            if conv_persona_id:
                platform_name = umo.split(":")[0] if ":" in umo else "webchat"
                result = await pm.resolve_selected_persona(
                    umo=umo,
                    conversation_persona_id=conv_persona_id,
                    platform_name=platform_name,
                    provider_settings={},
                )
                persona = result[1] if isinstance(result, (tuple, list)) and len(result) > 1 else None
                text = self._persona_text(persona)
                if text:
                    return text
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("[bili-scroll] 读取会话人设失败：%s", exc)
        try:
            return self._persona_text(await pm.get_default_persona_v3())
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("[bili-scroll] 读取默认人设失败：%s", exc)
            return ""

    @staticmethod
    def _is_private_event(event) -> bool:
        """判断事件是否私聊；无法判断时按私聊处理（兼容旧宿主/测试替身）。"""
        checker = getattr(event, "is_private_chat", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:  # noqa: BLE001
                return False
        return True

    async def _fallback_comment(
        self,
        mention: str,
        body: str,
        limit: int,
        forbidden_terms: list[str],
        umo: str,
    ) -> str:
        """兜底文案也要过发送前复核；不通过就宁可不发。"""
        allowed, reason = await self._review_comment(
            body, forbidden_terms, umo=umo
        )
        if not allowed:
            self.logger.warning(
                "[bili-scroll] 兜底评论复核未通过（%s），已跳过：%s", reason, body[:60]
            )
            return ""
        return logic.render_comment(mention, body, limit)

    async def _make_comment_text(
        self, item: dict, mention: str, one_liner: str, umo: str, *, share: bool = False
    ) -> str:
        """生成评论正文：人设改写（默认）→ 失败回退为一句话模板。"""
        limit = max(20, min(150, self._cfg_int("comment_text_limit", 150, minimum=20)))
        forbidden_terms = logic.parse_forbidden_terms(
            self._cfg("comment_blocklist", "")
        )
        # 兜底文案同样要过安全过滤：one_liner 来自判断模型，可能被素材影响
        fallback_body = logic.clean_comment_text(one_liner, limit)
        if not logic.is_safe_comment(fallback_body, limit, forbidden_terms):
            self.logger.warning(
                "[bili-scroll] 兜底评论含可疑内容，改用中性文案：%s", fallback_body[:60]
            )
            fallback_body = "这条看着还行，先收藏了"
        fallback = logic.render_comment(mention, fallback_body, limit)
        if not bool(self._cfg("comment_use_persona", True)):
            return await self._fallback_comment(
                mention, fallback_body, limit, forbidden_terms, umo
            )
        persona = await self._persona_prompt(umo)
        if not persona:
            return await self._fallback_comment(
                mention, fallback_body, limit, forbidden_terms, umo
            )
        self.logger.info(
            "[bili-scroll] 用人设生成评论（人设 %s 字，上限 %s 字）", len(persona), limit
        )
        style_hint = str(self._cfg("comment_persona_hint", "") or "").strip()
        system, user = logic.build_comment_prompt(
            persona,
            item,
            one_liner,
            limit,
            style_hint,
            share=share,
            visual=str(item.get("visual") or ""),
        )
        try:
            response = await self._llm_generate(
                umo=umo,
                prompt=user,
                system_prompt=system,
                timeout=float(self._cfg("judge_timeout", 90) or 90),
            )
            body = logic.clean_comment_text(
                getattr(response, "completion_text", ""), limit
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[bili-scroll] 人设评论生成失败，回退一句话：%s", exc)
            return await self._fallback_comment(
                mention, fallback_body, limit, forbidden_terms, umo
            )
        if not body:
            return await self._fallback_comment(
                mention, fallback_body, limit, forbidden_terms, umo
            )
        if not logic.is_safe_comment(body, limit, forbidden_terms):
            self.logger.warning(
                "[bili-scroll] 人设评论违反评论红线，已跳过本轮评论：%s", body[:60]
            )
            return ""
        allowed, review_reason = await self._review_comment(
            body, forbidden_terms, umo=umo
        )
        if not allowed:
            self.logger.warning(
                "[bili-scroll] 评论复核未通过（%s），已跳过：%s", review_reason, body[:60]
            )
            return ""
        return logic.render_comment(mention, body, limit)

    async def _review_comment(
        self, body: str, forbidden_terms: list[str], *, umo: str
    ) -> tuple[bool, str]:
        """发送前的模型复核：查真实姓名/个人信息、拉踩攻击、引流。失败按不通过处理。"""
        if not bool(self._cfg("comment_guard_review", True)):
            return True, "未启用复核"
        system, user = logic.build_comment_review_prompt(body, forbidden_terms)
        try:
            response = await self._llm_generate(
                umo=umo,
                prompt=user,
                system_prompt=system,
                timeout=float(self._cfg("judge_timeout", 90) or 90),
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[bili-scroll] 评论复核调用失败，按不通过处理：%s", exc)
            return False, "复核失败"
        payload = logic.extract_json(getattr(response, "completion_text", ""))
        if not isinstance(payload, dict) or "safe" not in payload:
            return False, "复核未返回有效 JSON"
        # 只接受 JSON 布尔 true；字符串 "false"/"0" 一律按不通过处理（fail closed）
        return payload.get("safe") is True, str(payload.get("reason") or "")[:80]

    # ---------------- 评论（默认关） ----------------
    async def _maybe_comment(self, umo: str, picked: list[dict]) -> None:
        mention = str(self._cfg("mention_name", "") or "").strip()
        if not mention:
            await self._push(umo, "自动评论已开启但没填「要 @ 的 B 站昵称」，本轮不发评论。")
            return
        per_round = max(1, min(5, self._cfg_int("comment_per_round", 1, minimum=1)))
        state = self.store.load_state()
        commented = dict(state.get("commented") or {})
        sent = 0
        for item in picked:
            if sent >= per_round:
                break
            if item["bvid"] in commented:
                continue
            one_liner = str(item.get("one_liner") or "").strip()
            if len(one_liner) < 4:  # 避免只发一条「@昵称」
                continue
            text = await self._make_comment_text(item, mention, one_liner, umo)
            if not text:
                continue
            try:
                result = await self.client.post_comment(item, text)
            except Exception as exc:  # noqa: BLE001
                await self._push(umo, f"评论失败（{item['bvid']}）：{exc}")
                continue
            sent += 1
            commented[item["bvid"]] = {
                "rpid": result.get("rpid"),
                "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "text": text,
            }
            state["commented"] = commented
            self.store.save_state(state)
            await self._push(umo, f"已在 {item['bvid']} 下评论并 @ 你：{text}")
            visible = False
            if result.get("rpid"):
                await asyncio.sleep(COMMENT_VERIFY_DELAY)
                try:
                    visible = await self.client.comment_visible(item, result["rpid"])
                except Exception:  # noqa: BLE001
                    visible = False
                await self._push(
                    umo,
                    f"评论复核（{item['bvid']}，rpid={result['rpid']}）："
                    + ("公开可见" if visible else "暂未复核到（可能延迟，回执成功不等于公开可见）"),
                )
            self._record_sent(
                "comment",
                item["bvid"],
                [],
                extra={
                    "bvid": item["bvid"],
                    "rpid": result.get("rpid"),
                    "text": text,
                    "mention": mention,
                    "visible": bool(visible),
                },
            )

    # ---------------- 推送 ----------------
    async def _push_report(
        self,
        umo: str,
        report: str,
        picked: list[dict],
        skipped: int,
        examined: int,
        level: str,
    ) -> bool:
        if level == "none":
            head = f"本轮看了 {examined} 条，挑中 {len(picked)} 条，因无字幕跳过 {skipped} 条。"
            if picked:
                head += "\n" + "\n".join(
                    f"- {item['one_liner']} {item['url']}" for item in picked
                )
            return await self._push(umo, head)
        return await self._push(umo, report)

    async def _push(self, umo: str, text: str) -> bool:
        text = str(text or "").strip()
        if not text or not umo:
            return False
        if not bool(self._cfg("push_qq", True)):
            self.logger.debug("[bili-scroll] 已关闭聊天推送，跳过：%s", text[:60])
            return False
        limit = max(200, self._cfg_int("push_chunk_size", 1500, minimum=200))
        for chunk in logic.chunk_text(text, limit):
            try:
                sent = await self.context.send_message(umo, MessageChain([Plain(chunk)]))
            except Exception as exc:  # noqa: BLE001
                self.logger.error("[bili-scroll] 推送异常：%s", exc)
                return False
            if sent is False:
                self.logger.warning(
                    "[bili-scroll] 推送失败（平台可能不支持主动消息）：%s", umo
                )
                return False
            await asyncio.sleep(0.5)
        return True

    # ---------------- 投递：B 站私信 / 聊天推送 ----------------
    def _dm_mode(self) -> str:
        mode = str(self._cfg("dm_mode", "off") or "off").strip().lower()
        return mode if mode in ("off", "only", "both") else "off"

    async def _deliver(
        self,
        umo: str,
        report: str,
        picked: list[dict],
        skipped: int,
        examined: int,
        level: str,
    ) -> None:
        """按 dm_mode 投递：off=只推聊天；only=只发 B 站私信；both=两边都发。"""
        mode = self._dm_mode()
        if picked and mode in ("only", "both"):
            await self._send_dm(picked)
        if mode != "only":
            pushed = await self._push_report(umo, report, picked, skipped, examined, level)
            if pushed:
                self._record_sent("push", umo, picked, extra={"level": level})

    async def _send_dm(self, picked: list[dict]) -> bool:
        limit = self._cfg_int("dm_text_limit", 500, minimum=100)
        text = logic.build_dm_text(datetime.now().strftime("%Y-%m-%d"), picked, limit=limit)
        if not text:
            return False
        receiver = self._cfg_int("dm_uid", 0)
        try:
            await self.client.send_dm(text, receiver_uid=receiver)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[bili-scroll] B站私信发送失败：%s", exc)
            # 运维错误提示走聊天（即使 dm_mode=only 也提示，否则用户完全无感知）
            await self._push(self._umo or "", f"B站私信发送失败：{exc}")
            return False
        self._record_sent(
            "dm",
            f"bili:{receiver or self.client.own_uid()}",
            picked,
            extra={"text": text},
        )
        return True

    def _record_sent(self, kind: str, target: str, picked: list[dict], extra: dict | None = None) -> None:
        """把「发过什么」落到 sent.jsonl，供注入上下文与工具查询。"""
        try:
            now = datetime.now()
            entry = {
                "ts": int(now.timestamp()),
                "date": now.strftime("%Y-%m-%d"),
                "time": now.strftime("%m-%d %H:%M"),
                "kind": kind,
                "target": str(target or ""),
                "count": len(picked or []),
                "items": [
                    {
                        "bvid": item.get("bvid", ""),
                        "title": item.get("title", ""),
                        "url": item.get("url", ""),
                    }
                    for item in (picked or [])[:5]
                ],
            }
            if extra:
                entry.update(extra)
            self.store.append_sent(entry)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[bili-scroll] 记录发送日志失败：%s", exc)

    # ---------------- 推送目标持久化 ----------------
    async def _load_umo(self) -> str | None:
        try:
            umo = await self.get_kv_data("umo", None)
        except Exception:  # noqa: BLE001
            umo = None
        if umo:
            return str(umo)
        state_umo = self.store.load_state().get("umo")
        return str(state_umo) if state_umo else None

    async def _save_umo(self, umo: str) -> None:
        try:
            await self.put_kv_data("umo", umo)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[bili-scroll] KV 保存推送目标失败，改用 state.json：%s", exc)
        state = self.store.load_state()
        state["umo"] = umo
        state["last_user_active_ts"] = datetime.now().timestamp()
        self.store.save_state(state)
