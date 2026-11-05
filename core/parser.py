"""解析用户输入：主页链接、分享短链、作品链接、纯 sec_uid。"""

from __future__ import annotations

import re
from typing import Optional

import httpx

URL_PATTERN = re.compile(r"https?://[^\s，。、；;\"'<>()【】]+")
SEC_UID_PATTERN = re.compile(r"MS4wLjABAAAA[\w\-]{10,}")
AWEME_ID_PATTERN = re.compile(r"\b(\d{15,20})\b")
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


class ParseError(Exception):
    pass


def extract_urls(text: str) -> list[str]:
    return URL_PATTERN.findall(text or "")


def expand_short_url(url: str, timeout: float = 15.0) -> str:
    """跟随 v.douyin.com 短链跳转，拿到真实地址。"""
    if "v.douyin.com" not in url and "iesdouyin.com/share" not in url:
        return url
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": MOBILE_UA},
        ) as client:
            resp = client.get(url)
            return str(resp.url)
    except httpx.HTTPError as exc:
        raise ParseError(f"短链解析失败: {exc}") from exc


def parse_target(text: str) -> dict:
    """把用户输入解析成 {'kind': 'user'|'aweme', 'sec_uid':..., 'aweme_id':...}。"""
    raw = (text or "").strip()
    if not raw:
        raise ParseError("输入为空")

    urls = extract_urls(raw)
    candidate = urls[0] if urls else raw

    if candidate.startswith("http"):
        candidate = expand_short_url(candidate)

        match = re.search(r"/user/([^/?#]+)", candidate)
        if match and match.group(1).startswith("MS4w"):
            return {"kind": "user", "sec_uid": match.group(1), "url": candidate}

        match = re.search(r"[?&]sec_uid=([^&#]+)", candidate)
        if match:
            return {"kind": "user", "sec_uid": match.group(1), "url": candidate}

        match = re.search(r"/share/user/([^/?#]+)", candidate)
        if match:
            return {"kind": "user", "sec_uid": match.group(1), "url": candidate}

        match = re.search(r"/video/(\d{15,20})", candidate)
        if match:
            return {"kind": "aweme", "aweme_id": match.group(1), "url": candidate}

        match = re.search(r"[?&]modal_id=(\d{15,20})", candidate)
        if match:
            return {"kind": "aweme", "aweme_id": match.group(1), "url": candidate}

        match = re.search(r"/note/(\d{15,20})", candidate)
        if match:
            return {"kind": "aweme", "aweme_id": match.group(1), "url": candidate}

        raise ParseError("无法从链接中识别博主或作品，请使用 douyin.com/user/... 形式的主页链接")

    sec = SEC_UID_PATTERN.search(raw)
    if sec:
        return {"kind": "user", "sec_uid": sec.group(0)}

    if raw.startswith("MS4w"):
        return {"kind": "user", "sec_uid": raw}

    digits = AWEME_ID_PATTERN.search(raw)
    if digits:
        return {"kind": "aweme", "aweme_id": digits.group(1)}

    raise ParseError("无法识别的输入，可粘贴主页链接、分享链接或 sec_uid")


def normalize_sec_uid(value: str) -> str:
    value = (value or "").strip()
    sec = SEC_UID_PATTERN.search(value)
    return sec.group(0) if sec else value
