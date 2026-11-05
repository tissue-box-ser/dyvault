"""抖音 Web API 客户端：Cookie 管理、a_bogus 签名、限速重试、分页抓取。"""

from __future__ import annotations

import json
import random
import threading
import time
from typing import Callable, Dict, Iterator, List, Optional
from urllib.parse import quote, urlencode

import httpx

from . import consts
from .sign import ABogus


class DouyinError(Exception):
    """基础异常。"""


class ApiError(DouyinError):
    def __init__(self, code, message, path: str = ""):
        self.code = code
        self.message = message
        self.path = path
        super().__init__(f"[{code}] {message} ({path})")


class AuthError(DouyinError):
    """Cookie 失效 / 未登录。"""


class RiskControlError(DouyinError):
    """被风控拦截（空响应、验证码）。"""


# Cookie 里这几个字段决定能力边界
KEY_COOKIES = ("ttwid", "msToken", "odin_tt", "sessionid", "sessionid_ss", "sid_tt", "webid")


def parse_cookie(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not text:
        return out
    for part in text.replace("\n", ";").replace("\r", ";").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key:
            out[key] = value.strip()
    return out


class DouyinClient:
    """一个实例对应一个 Cookie 身份，内部限速，可安全跨线程使用。"""

    def __init__(
        self,
        cookie: str = "",
        user_agent: str = "",
        proxy: str = "",
        timeout: float = 25.0,
        min_interval: float = 1.2,
        jitter: float = 1.0,
        log: Optional[Callable[[str], None]] = None,
        cookie_provider: Optional[Callable[[], str]] = None,
        rotate_every: int = 25,
    ):
        self.user_agent = user_agent or consts.DEFAULT_UA
        self.min_interval = max(0.0, min_interval)
        self.jitter = max(0.0, jitter)
        self._log = log or (lambda msg: None)
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._abogus = ABogus(user_agent=self.user_agent)
        self.last_logid = ""
        self._bootstrapped = False
        self._cookie_provider = cookie_provider
        self._rotate_every = max(1, rotate_every)
        self._request_count = 0

        self._cookies = parse_cookie(cookie)
        self.webid = self._cookies.get("webid") or consts.random_webid()

        self.http = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            proxy=proxy or None,
            headers=self._base_headers(),
        )

    # ---------------------------------------------------------------- 基础

    def _base_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": consts.WEB_BASE + "/",
            "sec-ch-ua": '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

    def _page_headers(self) -> Dict[str, str]:
        headers = self._base_headers()
        headers.update(
            {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            }
        )
        return headers

    def _absorb(self, resp: httpx.Response) -> None:
        """手动收敛 Set-Cookie，避免 httpx 自带 cookie jar 与显式 header 冲突。"""
        changed = False
        for key, value in resp.cookies.items():
            if self._cookies.get(key) != value:
                self._cookies[key] = value
                changed = True
        if changed and self.http.cookies:
            self.http.cookies.clear()

    def cookie_string(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self._cookies.items())

    def cookie_dict(self) -> Dict[str, str]:
        return dict(self._cookies)

    def close(self) -> None:
        self.http.close()

    def _maybe_rotate(self) -> None:
        """按请求次数轮换账号：每 N 次请求换下一个 Cookie。"""
        if not self._cookie_provider:
            return
        self._request_count += 1
        if self._request_count % self._rotate_every != 0:
            return
        try:
            fresh = self._cookie_provider()
        except Exception as exc:  # noqa: BLE001
            self._log(f"切换 Cookie 失败：{exc}")
            return
        parsed = parse_cookie(fresh or "")
        if not parsed:
            return
        self._cookies = parsed
        self.webid = parsed.get("webid") or self.webid
        self.last_logid = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def has_login(self) -> bool:
        return bool(
            self._cookies.get("sessionid")
            or self._cookies.get("sessionid_ss")
            or self._cookies.get("sid_tt")
        )

    # ---------------------------------------------------------------- 签名

    def _query(self, params: Dict[str, str]) -> str:
        merged = consts.common_params(self.webid, self._cookies.get("msToken", ""))
        merged.update({k: str(v) for k, v in params.items()})
        return urlencode(list(merged.items()))

    def _request(
        self,
        path: str,
        params: Dict[str, str],
        referer: str = "",
        retries: int = 3,
    ) -> dict:
        self._maybe_rotate()
        last_error: Optional[Exception] = None
        for attempt in range(retries):
            query = self._query(params)
            try:
                abogus = self._abogus.generate_abogus(query)[1]
            except Exception as exc:  # 签名失败重试无意义
                raise DouyinError(f"签名失败: {exc}") from exc

            url = f"{consts.WEB_BASE}{path}?{query}&a_bogus={quote(abogus, safe='')}"
            self._throttle()
            headers = {}
            if referer:
                headers["Referer"] = referer
            if self._cookies:
                headers["Cookie"] = self.cookie_string()

            try:
                resp = self.http.get(url, headers=headers)
            except httpx.HTTPError as exc:
                last_error = exc
                self._backoff(attempt)
                continue

            self._absorb(resp)
            self.last_logid = resp.headers.get("x-tt-logid", "")
            text = resp.text or ""

            if resp.status_code in (401, 403):
                raise AuthError(f"HTTP {resp.status_code}，Cookie 可能已失效")
            if resp.status_code != 200:
                last_error = DouyinError(f"HTTP {resp.status_code}")
                self._backoff(attempt)
                continue
            if not text.strip():
                last_error = RiskControlError(
                    f"接口返回空内容（logid {self.last_logid or '无'}），签名被风控拦截"
                )
                self._backoff(attempt)
                continue

            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                last_error = RiskControlError("返回内容不是 JSON，可能被验证码拦截")
                self._backoff(attempt)
                continue

            code = data.get("status_code", 0)
            if code in (0, None):
                return data
            message = data.get("status_msg") or data.get("message") or "未知错误"
            if code in (8, 2, 2154, 2053):
                raise RiskControlError(f"风控拦截: {message}")
            if code in (2096, 2191):
                raise AuthError(f"需要登录或 Cookie 失效: {message}")
            last_error = ApiError(code, message, path)
            self._backoff(attempt)

        raise last_error or DouyinError(f"请求失败: {path}")

    def _throttle(self) -> None:
        with self._lock:
            now = time.time()
            wait = self._last_request + self.min_interval - now
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.time()
        if self.jitter:
            time.sleep(random.uniform(0, self.jitter))

    def _backoff(self, attempt: int) -> None:
        time.sleep(min(2 ** attempt, 10) + random.uniform(0, 1))

    # ---------------------------------------------------------------- 初始化

    def bootstrap(self, force: bool = False) -> dict:
        """预热会话：补齐 ttwid / msToken，刷新 __ac_nonce。"""
        if self._bootstrapped and not force:
            return {"ok": True, "login": self.has_login}

        try:
            resp = self.http.get(
                consts.WEB_BASE + "/",
                headers={**self._page_headers(), "Cookie": self.cookie_string()}
                if self._cookies
                else self._page_headers(),
            )
            self._absorb(resp)
            self._log(f"首页预热 {resp.status_code}，cookie: {', '.join(self._cookies.keys())}")
        except httpx.HTTPError as exc:
            self._log(f"首页预热失败: {exc}")

        if not self._cookies.get("ttwid"):
            self._register_ttwid()
        if not self._cookies.get("msToken"):
            if not self._fetch_mstoken():
                # 抖音前端的 msToken 由本地 JS 生成，服务端只做弱校验
                self._cookies["msToken"] = consts.generate_ms_token()
                self._log("本地生成 msToken")
        self._bootstrapped = True
        return {"ok": True, "login": self.has_login, "cookie": self.cookie_string()}

    def _register_ttwid(self) -> None:
        payload = {
            "region": "cn",
            "aid": 1768,
            "needFid": False,
            "service": "www.ixigua.com",
            "migrate_info": {"ticket": "", "source": "node"},
            "cbUrlProtocol": "https",
            "union": True,
        }
        try:
            resp = httpx.post(
                "https://ttwid.bytedance.com/ttwid/union/register/",
                json=payload,
                headers={"User-Agent": self.user_agent, "Content-Type": "application/json"},
                timeout=15,
                follow_redirects=True,
            )
            for key, value in resp.cookies.items():
                self._cookies[key] = value
            if self._cookies.get("ttwid"):
                self._log("已获取 ttwid")
        except httpx.HTTPError as exc:
            self._log(f"获取 ttwid 失败: {exc}")

    def _fetch_mstoken(self) -> bool:
        try:
            resp = self.http.get(
                "https://mssdk.bytedance.com/web/report",
                params={"msToken": ""},
                headers={"User-Agent": self.user_agent, "Referer": consts.WEB_BASE + "/"},
            )
            self._absorb(resp)
            if self._cookies.get("msToken"):
                self._log("已获取 msToken")
                return True
        except httpx.HTTPError as exc:
            self._log(f"获取 msToken 失败: {exc}")
        return False

    # ---------------------------------------------------------------- 诊断

    def diagnose(self) -> dict:
        """给前端用的体检：说明当前 Cookie 能做什么、卡在哪一步。"""
        report = {
            "ok": False,
            "login": self.has_login,
            "cookies": sorted(self._cookies.keys()),
            "missing": [k for k in KEY_COOKIES if not self._cookies.get(k)],
            "steps": [],
            "message": "",
            "hint": "",
        }

        def step(name: str, ok: bool, detail: str) -> None:
            report["steps"].append({"name": name, "ok": ok, "detail": detail})

        try:
            self.bootstrap(force=True)
        except DouyinError as exc:
            step("会话预热", False, str(exc))

        step(
            "首页预热",
            bool(self._cookies.get("ttwid")),
            f"cookie: {', '.join(report['cookies']) or '无'}",
        )

        try:
            data = self._request(consts.EP_USER_SELF, {}, referer=consts.WEB_BASE + "/")
            user = data.get("user") or {}
            step("登录态校验", bool(user), user.get("nickname") or "未返回用户信息")
            if user:
                report["ok"] = True
                report["nickname"] = user.get("nickname")
                report["message"] = f"Cookie 有效：{user.get('nickname')}"
                return report
        except RiskControlError as exc:
            step("登录态校验", False, f"被风控：{exc}")
        except AuthError as exc:
            step("登录态校验", False, f"鉴权失败：{exc}")
        except DouyinError as exc:
            step("登录态校验", False, str(exc))

        if not self.has_login:
            report["hint"] = (
                "Cookie 里没有 sessionid，当前是游客态。游客态只能看公开主页，且极易被风控。"
                "请在已登录 douyin.com 的浏览器里复制完整 Cookie。"
            )
            report["message"] = "未登录（缺少 sessionid）"
        elif not self._cookies.get("__ac_signature"):
            report["hint"] = (
                "缺少 __ac_signature，抖音可能要求完成一次人机验证。"
                "请用浏览器打开 douyin.com 正常浏览几个页面后再复制 Cookie。"
            )
            report["message"] = "Cookie 不完整或被风控"
        else:
            report["hint"] = "Cookie 可能已过期，请重新登录抖音网页版后复制。"
            report["message"] = "Cookie 失效或被风控"
        return report

    # ---------------------------------------------------------------- 业务

    def get_user_profile(self, sec_uid: str) -> dict:
        data = self._request(
            consts.EP_USER_PROFILE,
            {"sec_user_id": sec_uid, "publish_video_strategy_type": "2"},
            referer=f"{consts.WEB_BASE}/user/{sec_uid}",
        )
        return data.get("user") or {}

    def get_self_profile(self) -> dict:
        data = self._request(consts.EP_USER_SELF, {}, referer=consts.WEB_BASE + "/")
        return data.get("user") or {}

    def iter_user_awemes(
        self,
        sec_uid: str,
        source: str = consts.SOURCE_POST,
        limit: int = 0,
        on_page: Optional[Callable[[int, bool], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> Iterator[dict]:
        """遍历某个博主的作品 / 点赞 / 收藏列表。

        limit=0 表示不限制；on_page(已抓数量, 是否还有更多) 用于上报进度。
        """
        path = consts.SOURCE_ENDPOINTS.get(source)
        if not path:
            raise DouyinError(f"未知来源类型: {source}")

        cursor = 0
        fetched = 0
        page = 0
        while True:
            if should_stop and should_stop():
                return
            data = self._request(
                path,
                {
                    "sec_user_id": sec_uid,
                    "max_cursor": str(cursor),
                    "count": "20",
                    "locate_query": "false",
                    "show_live_replay_strategy": "1",
                    "need_time_list": "1",
                    "time_list_query": "0",
                    "whale_cut_token": "",
                    "cut_version": "1",
                    "publish_video_strategy_type": "2",
                },
                referer=f"{consts.WEB_BASE}/user/{sec_uid}",
            )
            items = data.get("aweme_list") or []
            for item in items:
                if should_stop and should_stop():
                    return
                yield item
                fetched += 1
                if limit and fetched >= limit:
                    if on_page:
                        on_page(fetched, False)
                    return

            page += 1
            has_more = bool(data.get("has_more"))
            cursor = data.get("max_cursor") or 0
            if on_page:
                on_page(fetched, has_more)
            self._log(f"{consts.SOURCE_LABELS.get(source, source)}第 {page} 页，累计 {fetched} 条")
            if not has_more or not items or not cursor:
                return

    def get_aweme_detail(self, aweme_id: str) -> dict:
        data = self._request(
            consts.EP_AWEME_DETAIL,
            {"aweme_id": aweme_id},
            referer=f"{consts.WEB_BASE}/video/{aweme_id}",
        )
        return data.get("aweme_detail") or {}

    def iter_comments(
        self,
        aweme_id: str,
        limit: int = 0,
        include_replies: bool = False,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> Iterator[dict]:
        """抓取一级评论；include_replies=True 时顺带展开二级回复。"""
        cursor = 0
        fetched = 0
        while True:
            if should_stop and should_stop():
                return
            data = self._request(
                consts.EP_COMMENT_LIST,
                {
                    "aweme_id": aweme_id,
                    "cursor": str(cursor),
                    "count": "20",
                    "item_type": "0",
                },
                referer=f"{consts.WEB_BASE}/video/{aweme_id}",
            )
            comments = data.get("comments") or []
            for comment in comments:
                yield comment
                fetched += 1
                if include_replies and comment.get("reply_comment_total"):
                    for reply in self.iter_replies(aweme_id, comment.get("cid", ""), should_stop):
                        yield reply
                        fetched += 1
                        if limit and fetched >= limit:
                            return
                if limit and fetched >= limit:
                    return
            if not data.get("has_more") or not comments:
                return
            cursor = data.get("cursor") or 0
            if not cursor:
                return

    def iter_replies(
        self,
        aweme_id: str,
        comment_id: str,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> Iterator[dict]:
        cursor = 0
        while True:
            if should_stop and should_stop():
                return
            data = self._request(
                "/aweme/v1/web/comment/list/reply/",
                {
                    "item_id": aweme_id,
                    "comment_id": comment_id,
                    "cursor": str(cursor),
                    "count": "20",
                    "item_type": "0",
                },
                referer=f"{consts.WEB_BASE}/video/{aweme_id}",
            )
            replies = data.get("comments") or []
            for reply in replies:
                yield reply
            if not data.get("has_more") or not replies:
                return
            cursor = data.get("cursor") or 0
            if not cursor:
                return

    def search_user(self, keyword: str) -> List[dict]:
        data = self._request(
            consts.EP_USER_SEARCH,
            {
                "keyword": keyword,
                "search_channel": "aweme_user_web",
                "search_source": "normal_search",
                "query_correct_type": "1",
                "is_filter_search": "0",
                "offset": "0",
                "count": "10",
            },
            referer=consts.WEB_BASE + "/",
        )
        users = []
        for entry in data.get("user_list") or []:
            info = entry.get("user_info") or {}
            if info.get("sec_uid"):
                users.append(
                    {
                        "sec_uid": info.get("sec_uid"),
                        "nickname": info.get("nickname"),
                        "unique_id": info.get("unique_id") or info.get("short_id"),
                        "avatar": (info.get("avatar_thumb") or {}).get("url_list", [""])[0],
                        "follower_count": info.get("follower_count"),
                    }
                )
        return users
