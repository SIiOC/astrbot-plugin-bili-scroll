"""B 站客户端适配层：登录、搜索、详情、字幕、评论、复核。

把 bilibili-api-python 的所有调用收敛在这一个文件里——库若失效（该库已归档），
只需要改这一层。依赖：bilibili-api-python>=17.4.1,<18。

风控策略：只读接口遇 -412/-352 做指数退避重试；**发评论绝不重试**（避免重复发）。
代理：只作用于本插件自己的请求（Credential(proxy=...) + aiohttp proxy=），
不调用进程级的 request_settings.set_proxy，避免污染宿主里其它插件。
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
import uuid
from pathlib import Path

import aiohttp
import qrcode
from bilibili_api import Credential, comment, homepage, hot, search, session as bili_session, user, video
from bilibili_api.comment import CommentResourceType
from bilibili_api.search import SearchObjectType
from bilibili_api.utils.network import Api

from astrbot.api import logger as _LOGGER

from . import logic, vision
from .store import Store

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
PASSPORT_GENERATE = (
    "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
    "?source=main-fe-header"
)
PASSPORT_POLL = (
    "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
    "?qrcode_key={key}&source=main-fe-header"
)
FINGER_SPI = "https://api.bilibili.com/x/frontend/finger/spi"

RISK_CODES = {-412, -352, -509, 412}

QR_STATE_TEXT = {
    "waiting": "等待扫码",
    "scanned": "已扫码，请在手机上确认",
    "expired": "二维码已过期，请重新 /bili_login",
    "done": "登录成功",
    "none": "没有进行中的登录，请先 /bili_login",
}


class BiliError(RuntimeError):
    """B 站调用失败的统一异常。"""


class NotLoggedInError(BiliError):
    """需要登录态但本地没有凭据。"""


def _parse_set_cookie(raw_cookies: list[str]) -> dict:
    wanted = {
        "SESSDATA": "sessdata",
        "bili_jct": "bili_jct",
        "DedeUserID": "dedeuserid",
        "buvid3": "buvid3",
        "buvid4": "buvid4",
    }
    jar: dict[str, str] = {}
    for raw in raw_cookies or []:
        first = str(raw).split(";")[0]
        name, _, value = first.partition("=")
        key = wanted.get(name.strip())
        if key and value:
            jar[key] = value.strip()
    return jar


def _is_risk_control(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    if code in RISK_CODES:
        return True
    text = str(exc)
    return any(token in text for token in ("-412", "-352", "-509", "Precondition Failed"))


def _is_transient_network_error(exc: Exception) -> bool:
    """只读请求可重试的瞬时网络错误；不包含风控（风控另有计数与退避）。"""
    if _is_risk_control(exc):
        return False
    if isinstance(exc, (asyncio.TimeoutError, OSError, aiohttp.ClientError)):
        return True
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "getaddrinfo failed",
            "connection reset",
            "connection aborted",
            "temporary failure in name resolution",
            "timed out",
        )
    )


class BiliClient:
    """面向本插件需求的 B 站能力封装。"""

    def __init__(self, store: Store, proxy: str = "") -> None:
        self.store = store
        self.proxy = (proxy or "").strip()
        self._cred: Credential | None = None
        self._cred_loaded = False
        self._qr_key = ""
        self._qr_started_at = 0.0
        # 用量与风控计数（供上层熔断/页面展示；每轮开始时由调用方重置）
        self.call_count = 0
        self.risk_count = 0
        # 只读结果短缓存：同一进程内重复轮次不再重复打 B站接口。
        self._read_cache: dict[str, tuple[float, object]] = {}

    def _cache_get(self, key: str, ttl: float):
        hit = self._read_cache.get(key)
        if not hit:
            return None
        saved_at, value = hit
        if time.time() - saved_at > ttl:
            self._read_cache.pop(key, None)
            return None
        return value

    def _cache_set(self, key: str, value) -> None:
        self._read_cache[key] = (time.time(), value)
        if len(self._read_cache) > 2000:
            oldest = min(self._read_cache, key=lambda k: self._read_cache[k][0])
            self._read_cache.pop(oldest, None)

    def _bump(self) -> None:
        self.call_count += 1

    # ---------------- 通用：只读请求退避 ----------------
    async def _read_with_backoff(self, factory, *, attempts: int = 3, base_delay: float = 5.0):
        """只读请求：风控/瞬时网络错误指数退避重试；业务错误直接抛出。"""
        last_exc: Exception | None = None
        for attempt in range(attempts):
            self._bump()
            try:
                return await factory()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                risk = _is_risk_control(exc)
                transient = _is_transient_network_error(exc)
                if risk:
                    self.risk_count += 1
                if attempt + 1 < attempts and (risk or transient):
                    delay = (
                        base_delay * (attempt + 1)
                        if risk
                        else min(1.5, base_delay) * (attempt + 1)
                    )
                    kind = "风控" if risk else "网络"
                    _LOGGER.warning(
                        "B 站%s错误，%.1f 秒后重试（%s）：%s",
                        kind,
                        delay,
                        attempt + 1,
                        exc,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    # ---------------- 凭证 ----------------
    @property
    def credential(self) -> Credential | None:
        if not self._cred_loaded:
            self._cred_loaded = True
            data = self.store.load_credential()
            if data:
                self._cred = Credential(
                    sessdata=data.get("sessdata") or None,
                    bili_jct=data.get("bili_jct") or None,
                    buvid3=data.get("buvid3") or None,
                    buvid4=data.get("buvid4") or None,
                    dedeuserid=data.get("dedeuserid") or None,
                    ac_time_value=data.get("ac_time_value") or None,
                    proxy=self.proxy or None,
                )
        return self._cred

    def reload_credential(self) -> None:
        self._cred_loaded = False
        self._cred = None

    def has_credential(self) -> bool:
        cred = self.credential
        return bool(cred and cred.has_sessdata())

    async def self_info(self) -> dict:
        cred = self.credential
        if not cred or not cred.has_sessdata():
            raise NotLoggedInError("未登录：本地没有 SESSDATA")
        return await self._read_with_backoff(lambda: user.get_self_info(credential=cred))

    def _session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(headers=HEADERS, proxy=self.proxy or None)

    # ---------------- 扫码登录（passport 直连，凭证从 Set-Cookie 取） ----------------
    async def qr_start(self) -> Path:
        self.reload_credential()
        self._bump()
        async with self._session() as session:
            async with session.get(PASSPORT_GENERATE) as resp:
                data = await resp.json(content_type=None)
        payload = (data or {}).get("data") or {}
        key = str(payload.get("qrcode_key") or "")
        url = str(payload.get("url") or "")
        if not key or not url:
            raise BiliError(
                f"生成二维码失败：{data.get('message') if isinstance(data, dict) else data}"
            )
        self._qr_key = key
        self._qr_started_at = time.time()
        image = qrcode.make(url)
        self.store.qrcode_file.parent.mkdir(parents=True, exist_ok=True)
        image.save(str(self.store.qrcode_file))
        return self.store.qrcode_file

    async def qr_poll(self) -> str:
        """返回 waiting/scanned/expired/done/none 或 error:xxx。"""
        if not self._qr_key:
            return "none"
        self._bump()
        async with self._session() as session:
            async with session.get(
                PASSPORT_POLL.format(key=urllib.parse.quote(self._qr_key)),
                allow_redirects=False,
            ) as resp:
                body = await resp.json(content_type=None)
                raw_cookies = resp.headers.getall("Set-Cookie", [])
        data = (body or {}).get("data") or {}
        code = data.get("code")
        if code == 86101:
            return "waiting"
        if code == 86090:
            return "scanned"
        if code == 86038:
            self._qr_key = ""
            return "expired"
        if code != 0:
            self._qr_key = ""
            return f"error:{code}"

        jar = _parse_set_cookie(raw_cookies)
        if not jar.get("sessdata"):
            self._qr_key = ""
            return "error:no_sessdata"
        if not jar.get("buvid3"):
            jar.update(await self._fetch_buvid(session))
        self.store.save_credential(jar)
        self._qr_key = ""
        self.reload_credential()
        return "done"

    def cancel_login(self) -> None:
        """取消当前二维码登录流程（页面点「取消」时用）。"""
        self._qr_key = ""

    # ---------------- 私信 ----------------
    def own_uid(self) -> int:
        """凭据里记录的本人 UID（dedeuserid），读不到返回 0。"""
        try:
            return int(getattr(self.credential, "dedeuserid", 0) or 0)
        except (TypeError, ValueError):
            return 0

    async def send_dm(self, text: str, receiver_uid: int = 0) -> dict:
        """给指定 UID 发一条纯文本 B 站私信；receiver_uid=0 时发给自己。"""
        text = str(text or "").strip()
        if not text:
            raise BiliError("私信内容为空")
        uid = int(receiver_uid or 0) or self.own_uid()
        if not uid:
            raise BiliError("无法确定私信接收者：凭据里没有 dedeuserid")
        if uid == self.own_uid():
            raise BiliError(
                "B站不允许给自己发私信（21026）；请把 dm_uid 设为另一个账号的 UID，"
                "或等插件支持「用另一个账号登录后发送」。"
            )
        if not self.has_credential():
            raise NotLoggedInError("未登录，先扫码")

        async def call() -> dict:
            return await bili_session.send_msg(
                self.credential, uid, bili_session.EventType.TEXT, text
            )

        try:
            # 私信是写操作：绝不重试，避免同一条消息发多次
            self._bump()
            result = await call()
        except NotLoggedInError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BiliError(f"发送私信失败：{exc}") from exc
        payload = result if isinstance(result, dict) else {}
        code = payload.get("code")
        if code not in (None, 0):
            raise BiliError(f"发送私信失败（code={code}）：{payload.get('message') or ''}")
        return payload or {"code": 0}

    async def send_share_dm(self, item: dict, receiver_uid: int = 0) -> dict:
        """给指定 UID 发一条 B 站原生视频分享卡片（msg_type=7 / source=5）。

        说明：web 端 send_msg 文档只承诺支持 1/2/5，分享卡片可能被服务端拒绝；
        调用方应捕获 BiliError 后退回自然文字私信。
        """
        uid = int(receiver_uid or 0) or self.own_uid()
        if not uid:
            raise BiliError("无法确定私信接收者：凭据里没有 dedeuserid")
        if uid == self.own_uid():
            raise BiliError(
                "B站不允许给自己发私信（21026）；请把 dm_uid 设为另一个账号的 UID。"
            )
        if not self.has_credential():
            raise NotLoggedInError("未登录，先扫码")
        card = logic.build_dm_share_card(item)
        if not card:
            raise BiliError("分享卡片缺少 aid/bvid")
        sender_uid = self.own_uid()
        if not sender_uid:
            raise BiliError("无法确定发送者 UID：凭据里没有 dedeuserid")

        api = bili_session.API["operate"]["send_msg"]
        data = {
            "msg[sender_uid]": sender_uid,
            "msg[receiver_id]": uid,
            "msg[receiver_type]": 1,
            "msg[msg_type]": 7,
            "msg[msg_status]": 0,
            "msg[content]": json.dumps(card, ensure_ascii=False),
            "msg[dev_id]": str(uuid.uuid4()).upper(),
            "msg[new_face_version]": 0,
            "msg[timestamp]": int(time.time()),
            "from_filework": 0,
            "build": 0,
            "mobi_app": "web",
        }
        query = {"w_sender_uid": sender_uid, "w_receiver_id": uid}
        try:
            # 分享卡片是写操作：绝不重试，避免同一卡片发多次。
            self._bump()
            result = await (
                Api(**api, credential=self.credential)
                .update_params(**query)
                .update_data(**data)
                .result
            )
        except Exception as exc:  # noqa: BLE001
            raise BiliError(f"发送分享卡片失败：{exc}") from exc
        payload = result if isinstance(result, dict) else {}
        code = payload.get("code")
        if code not in (None, 0):
            raise BiliError(
                f"发送分享卡片失败（code={code}）：{payload.get('message') or ''}"
            )
        return payload or {"code": 0}

    async def _fetch_buvid(self, session: aiohttp.ClientSession) -> dict:
        try:
            async with session.get(FINGER_SPI, headers=HEADERS) as resp:
                data = await resp.json(content_type=None)
            payload = (data or {}).get("data") or {}
            return {
                "buvid3": str(payload.get("b_3") or ""),
                "buvid4": str(payload.get("b_4") or ""),
            }
        except Exception:  # noqa: BLE001
            return {}

    # ---------------- 搜索 / 详情 ----------------
    async def search_keywords(
        self,
        keywords: list[str],
        *,
        days: int = 7,
        limit: int = 24,
        weights: list[int] | None = None,
    ) -> list[dict]:
        """按关键词搜索。

        weights 为空时保持旧行为（顺序累加、凑够 limit 即停）；
        传了权重则取齐每个关键词的结果，按权重分配配额后轮转合并。
        """
        now_ts = int(time.time())
        if not weights:
            found: list[dict] = []
            for keyword in keywords:
                try:
                    raw = await self._read_with_backoff(
                        lambda kw=keyword: search.search_by_type(
                            kw,
                            search_type=SearchObjectType.VIDEO,
                            page=1,
                            page_size=42,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    raise BiliError(f"搜索「{keyword}」失败：{exc}") from exc
                items = [logic.normalize_search_item(item) for item in (raw.get("result") or [])]
                items = [item for item in items if item]
                found.extend(logic.filter_recent(items, days, now_ts))
                await asyncio.sleep(0.6)
                if len(found) >= limit:
                    break
            return logic.dedupe_by_bvid(found)[:limit]

        pools: dict[str, list[dict]] = {}
        for keyword in keywords:
            try:
                raw = await self._read_with_backoff(
                    lambda kw=keyword: search.search_by_type(
                        kw,
                        search_type=SearchObjectType.VIDEO,
                        page=1,
                        page_size=42,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                raise BiliError(f"搜索「{keyword}」失败：{exc}") from exc
            items = [logic.normalize_search_item(item) for item in (raw.get("result") or [])]
            pools[keyword] = logic.filter_recent(
                [item for item in items if item], days, now_ts
            )
            await asyncio.sleep(0.6)
        full_weights = [
            int(weights[i]) if i < len(weights) else 1 for i in range(len(keywords))
        ]
        quotas = logic.allocate_by_weights(full_weights, max(1, limit))
        budgets = {keyword: quotas[i] for i, keyword in enumerate(keywords)}
        return logic.weighted_merge(pools, budgets, limit)

    async def homepage_feed(self, limit: int = 20) -> list[dict]:
        """首页推荐流：模拟「自己刷」而不是按关键词搜。"""
        try:
            data = await self._read_with_backoff(lambda: homepage.get_videos())
        except Exception as exc:  # noqa: BLE001
            raise BiliError(f"读取首页推荐失败：{exc}") from exc
        items = [logic.normalize_homepage_item(raw) for raw in (data.get("item") or [])]
        items = [item for item in items if item]
        return logic.dedupe_by_bvid(items)[: max(1, min(30, int(limit or 20)))]

    async def popular_feed(self, limit: int = 20) -> list[dict]:
        """热门视频榜：作为「热门」来源与首页推荐、关键词混合。"""
        try:
            data = await self._read_with_backoff(
                lambda: hot.get_hot_videos(pn=1, ps=max(1, min(50, int(limit or 20))))
            )
        except Exception as exc:  # noqa: BLE001
            raise BiliError(f"读取热门视频失败：{exc}") from exc
        items = [logic.normalize_hot_item(raw) for raw in (data.get("list") or [])]
        items = [item for item in items if item]
        return logic.dedupe_by_bvid(items)[: max(1, min(50, int(limit or 20)))]

    async def video_detail(self, item: dict) -> dict:
        cache_key = f"detail:{item.get('bvid', '')}"
        cached = self._cache_get(cache_key, 900)
        if isinstance(cached, dict):
            return dict(cached)
        vid = video.Video(bvid=item["bvid"], credential=self.credential)
        try:
            info = await self._read_with_backoff(lambda: vid.get_info())
        except Exception as exc:  # noqa: BLE001
            raise BiliError(f"读取视频信息失败（{item['bvid']}）：{exc}") from exc
        detail = dict(item)
        detail["aid"] = int(info.get("aid") or item.get("aid") or 0) or None
        cid = info.get("cid") or item.get("cid")
        if not cid:
            pages = info.get("pages") or []
            if pages and isinstance(pages[0], dict):
                cid = pages[0].get("cid")  # 多 P 视频顶层 cid 可能缺
        detail["cid"] = int(cid) if cid else None
        detail["title"] = str(info.get("title") or item.get("title") or "")
        detail["cover"] = str(info.get("pic") or item.get("cover") or "")
        detail["up"] = str((info.get("owner") or {}).get("name") or item.get("up") or "")
        detail["up_uid"] = str((info.get("owner") or {}).get("mid") or item.get("up_uid") or "")
        desc = str(info.get("desc") or "").strip()
        if desc:
            detail["desc"] = desc[:1000]
        detail["duration"] = int(info.get("duration") or item.get("duration") or 0)
        detail["pubdate"] = int(info.get("pubdate") or item.get("pubdate") or 0)
        detail["play"] = int(((info.get("stat") or {}).get("view")) or item.get("play") or 0)
        self._cache_set(cache_key, detail)
        return detail

    # ---------------- 字幕 ----------------
    async def subtitle_text(self, item: dict) -> str:
        cred = self.credential
        if not cred or not cred.has_sessdata():
            raise NotLoggedInError("读取字幕需要登录态")
        if not item.get("cid"):
            return ""
        cache_key = f"subtitle:{item['bvid']}:{item['cid']}"
        cached = self._cache_get(cache_key, 3600)
        if isinstance(cached, str):
            return cached
        vid = video.Video(bvid=item["bvid"], credential=cred)
        try:
            info = await self._read_with_backoff(
                lambda: vid.get_subtitle(cid=item["cid"])
            )
        except Exception as exc:  # noqa: BLE001
            raise BiliError(f"读取字幕信息失败（{item['bvid']}）：{exc}") from exc
        track = logic.pick_subtitle_track((info or {}).get("subtitles") or [])
        if not track:
            return ""
        url = str(track.get("subtitle_url") or "")
        if url.startswith("//"):
            url = "https:" + url
        if not url:
            return ""
        # 字幕地址同样来自 B 站播放接口；只允许 https 公网地址，防止被指向内网
        if not vision.is_safe_media_url(url):
            raise BiliError("字幕地址不可用（非 https 公网地址）")
        try:
            async with self._session() as session:
                async with session.get(url) as resp:
                    body = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            raise BiliError(f"下载字幕失败（{item['bvid']}）：{exc}") from exc
        text = logic.subtitle_text_from_body(body)
        self._cache_set(cache_key, text)
        return text

    # ---------------- 无字幕兜底：官方摘要 / 抽帧 ----------------
    async def ai_summary(self, item: dict) -> str:
        """B 站官方 AI 摘要；没有就返回空串。"""
        if not item.get("cid"):
            return ""
        vid = video.Video(bvid=item["bvid"], credential=self.credential)
        try:
            data = await self._read_with_backoff(
                lambda: vid.get_ai_conclusion(cid=item["cid"])
            )
        except Exception as exc:  # noqa: BLE001
            raise BiliError(f"读取 AI 摘要失败（{item['bvid']}）：{exc}") from exc
        payload = data if isinstance(data, dict) else {}
        model_result = payload.get("model_result") or {}
        summary = (
            model_result.get("summary")
            or payload.get("summary")
            or payload.get("result")
            or ""
        )
        if isinstance(summary, dict):
            summary = summary.get("text") or ""
        return str(summary or "").strip()[:1200]

    async def download_video(self, item: dict, *, max_mb: int = 30) -> Path:
        """下载视频流的前 max_mb MB 到临时文件；拿不到地址时抛 BiliError。"""
        if not item.get("cid"):
            raise BiliError("缺少 cid，无法下载视频")
        vid = video.Video(bvid=item["bvid"], credential=self.credential)
        data = await self._read_with_backoff(
            lambda: vid.get_download_url(cid=item["cid"])
        )
        stream = vision.pick_video_stream(data)
        if not stream or not vision.is_safe_media_url(stream["url"]):
            raise BiliError("没有可用的视频流地址")
        max_bytes = max(1, int(max_mb)) * 1024 * 1024
        tmp_dir = self.store.data_dir / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        target = tmp_dir / f"vision-{item['bvid']}-{uuid.uuid4().hex[:10]}.mp4"
        headers = dict(HEADERS)
        headers["Referer"] = f"https://www.bilibili.com/video/{item['bvid']}"
        headers["Range"] = f"bytes=0-{max_bytes - 1}"
        self._bump()
        written = 0
        try:
            async with self._session() as session:
                async with session.get(
                    stream["url"],
                    headers=headers,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    if resp.status not in (200, 206):
                        raise BiliError(f"下载视频流失败：HTTP {resp.status}")
                    with target.open("wb") as handle:
                        async for chunk in resp.content.iter_chunked(256 * 1024):
                            if not chunk:
                                continue
                            remain = max_bytes - written
                            if remain <= 0:
                                break
                            handle.write(chunk[:remain])
                            written += min(len(chunk), remain)
                            if written >= max_bytes:
                                break
        except BiliError:
            target.unlink(missing_ok=True)
            raise
        except Exception as exc:  # noqa: BLE001
            target.unlink(missing_ok=True)
            raise BiliError(f"下载视频流失败：{exc}") from exc
        if written < 64 * 1024:
            target.unlink(missing_ok=True)
            raise BiliError("下载的视频片段过小")
        return target

    # ---------------- 评论（读） ----------------
    async def top_comments(self, item: dict, limit: int = 8) -> list[str]:
        if not item.get("aid"):
            return []
        try:
            data = await self._read_with_backoff(
                lambda: comment.get_comments(
                    oid=int(item["aid"]),
                    type_=CommentResourceType.VIDEO,
                    page_index=1,
                    credential=self.credential,
                )
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("读取评论失败（%s）：%s", item.get("bvid"), exc)
            raise BiliError(f"读取评论失败：{exc}") from exc
        out: list[str] = []
        for reply in (data or {}).get("replies") or []:
            message = str(((reply.get("content") or {}).get("message")) or "").strip()
            uname = str(((reply.get("member") or {}).get("uname")) or "").strip()
            if message:
                out.append(f"{uname}：{message[:200]}")
            if len(out) >= limit:
                break
        return out

    # ---------------- 评论（写，默认关；绝不重试） ----------------
    async def post_comment(self, item: dict, text: str) -> dict:
        cred = self.credential
        if not cred or not cred.has_sessdata() or not cred.has_bili_jct():
            raise NotLoggedInError("发评论需要 SESSDATA + bili_jct")
        if not item.get("aid"):
            raise BiliError("缺少 aid，无法发评论")
        self._bump()
        try:
            result = await comment.send_comment(
                text=text,
                oid=int(item["aid"]),
                type_=CommentResourceType.VIDEO,
                credential=cred,
            )
        except Exception as exc:  # noqa: BLE001
            raise BiliError(f"发评论失败：{exc}") from exc
        # bilibili_api 的 Api.result 已剥离外层 data，这里拿到的就是 data 本体；
        # 仍保留对旧版/未剥离形态的兼容。
        payload = result if isinstance(result, dict) else {}
        rpid = payload.get("rpid") or payload.get("rp_id")
        if not rpid and isinstance(payload.get("data"), dict):
            inner = payload["data"]
            rpid = inner.get("rpid") or inner.get("rp_id")
        return {"rpid": rpid, "raw": result}

    async def comment_visible(self, item: dict, rpid) -> bool:
        if not item.get("aid") or not rpid:
            return False
        try:
            data = await self._read_with_backoff(
                lambda: comment.get_comments(
                    oid=int(item["aid"]),
                    type_=CommentResourceType.VIDEO,
                    page_index=1,
                    credential=self.credential,
                )
            )
        except Exception:  # noqa: BLE001
            return False
        target = str(rpid)

        def scan(reply: dict) -> bool:
            if str(reply.get("rpid")) == target:
                return True
            return any(scan(child) for child in (reply.get("replies") or []))

        return any(scan(reply) for reply in (data or {}).get("replies") or [])
