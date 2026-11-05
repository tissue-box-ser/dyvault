"""常量与浏览器指纹伪装参数。"""

from __future__ import annotations

import os
import random
import string

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
]

DEFAULT_UA = USER_AGENTS[0]

# 可通过环境变量指向自建桩服务，便于离线联调与回归测试
WEB_BASE = os.environ.get("DOUYIN_WEB_BASE", "https://www.douyin.com").rstrip("/")

# 接口路径
EP_USER_PROFILE = "/aweme/v1/web/user/profile/other/"
EP_USER_SELF = "/aweme/v1/web/user/profile/self/"
EP_AWEME_POST = "/aweme/v1/web/aweme/post/"
EP_AWEME_LIKE = "/aweme/v1/web/aweme/favorite/"
EP_AWEME_COLLECT = "/aweme/v1/web/aweme/collect/"
EP_COMMENT_LIST = "/aweme/v1/web/comment/list/"
EP_AWEME_DETAIL = "/aweme/v1/web/aweme/detail/"
EP_USER_SEARCH = "/aweme/v1/web/discover/search/"

# 作品来源
SOURCE_POST = "post"
SOURCE_LIKE = "like"
SOURCE_COLLECT = "collect"

SOURCE_ENDPOINTS = {
    SOURCE_POST: EP_AWEME_POST,
    SOURCE_LIKE: EP_AWEME_LIKE,
    SOURCE_COLLECT: EP_AWEME_COLLECT,
}

SOURCE_LABELS = {
    SOURCE_POST: "作品",
    SOURCE_LIKE: "点赞",
    SOURCE_COLLECT: "收藏",
}


def random_webid() -> str:
    """生成 19 位数字 webid，抖音 web 端会把它作为浏览器标识之一。"""
    first = random.choice("123456789")
    rest = "".join(random.choice(string.digits) for _ in range(18))
    return first + rest


def generate_ms_token(length: int = 126) -> str:
    """抖音前端的 msToken 由浏览器本地生成，服务端只做弱校验，这里模拟同格式。"""
    alphabet = string.ascii_letters + string.digits + "_-"
    return "".join(random.choice(alphabet) for _ in range(length))


def common_params(webid: str, ms_token: str = "") -> dict:
    """抖音 web 接口的公共 query 参数（不含签名）。"""
    params = {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "pc_client_type": "1",
        "version_code": "190500",
        "version_name": "19.5.0",
        "cookie_enabled": "true",
        "screen_width": "1920",
        "screen_height": "1080",
        "browser_language": "zh-CN",
        "browser_platform": "Win32",
        "browser_name": "Chrome",
        "browser_version": "130.0.0.0",
        "browser_online": "true",
        "engine_name": "Blink",
        "engine_version": "130.0.0.0",
        "os_name": "Windows",
        "os_version": "10",
        "cpu_core_num": "12",
        "device_memory": "8",
        "platform": "PC",
        "downlink": "10",
        "effective_type": "4g",
        "round_trip_time": "50",
        "webid": webid,
    }
    if ms_token:
        params["msToken"] = ms_token
    return params
