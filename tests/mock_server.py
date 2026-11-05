"""本地桩服务：模拟抖音 web 接口，用于离线端到端测试。

用法：python tests/mock_server.py [port]
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
BASE = f"http://127.0.0.1:{PORT}"
SEC_UID = "MS4wLjABAAAAtestsecuid000000000000000000000"
NICKNAME = "测试博主"

VIDEOS = [
    {
        "aweme_id": "7000000000000000001",
        "desc": "测试视频一 #测试",
        "create_time": 1700000000,
        "author": {"nickname": NICKNAME, "sec_uid": SEC_UID, "unique_id": "test_user"},
        "statistics": {"digg_count": 1200, "comment_count": 2, "collect_count": 33, "share_count": 7},
        "video": {
            "play_addr": {"url_list": [f"{BASE}/media/v1.mp4"], "uri": "v0300test1"},
            "cover": {"url_list": [f"{BASE}/media/cover1.jpg"]},
            "duration": 15000,
            "bit_rate": [],
        },
        "music": {"play_url": {"url_list": [f"{BASE}/media/m1.mp3"]}, "title": "背景音乐一"},
        "aweme_type": 0,
    },
    {
        "aweme_id": "7000000000000000002",
        "desc": "测试图集二",
        "create_time": 1700100000,
        "author": {"nickname": NICKNAME, "sec_uid": SEC_UID, "unique_id": "test_user"},
        "statistics": {"digg_count": 88, "comment_count": 0, "collect_count": 5, "share_count": 1},
        "images": [
            {"url_list": [f"{BASE}/media/img1.jpg"]},
            {"url_list": [f"{BASE}/media/img2.jpg"]},
        ],
        "music": {"play_url": {"url_list": [f"{BASE}/media/m1.mp3"]}, "title": "背景音乐二"},
        "aweme_type": 68,
    },
    {
        "aweme_id": "7000000000000000003",
        "desc": "测试视频三",
        "create_time": 1700200000,
        "author": {"nickname": NICKNAME, "sec_uid": SEC_UID, "unique_id": "test_user"},
        "statistics": {"digg_count": 5, "comment_count": 0, "collect_count": 0, "share_count": 0},
        "video": {
            "play_addr": {"url_list": [f"{BASE}/media/v3.mp4"], "uri": "v0300test3"},
            "cover": {"url_list": [f"{BASE}/media/cover3.jpg"]},
            "duration": 9000,
            "bit_rate": [],
        },
        "music": {"play_url": {"url_list": [f"{BASE}/media/m1.mp3"]}, "title": "背景音乐三"},
        "aweme_type": 0,
    },
]

COMMENTS = [
    {
        "cid": "c1",
        "text": "第一条评论",
        "create_time": 1700000500,
        "digg_count": 12,
        "reply_comment_total": 0,
        "ip_label": "广东",
        "user": {"nickname": "路人甲", "unique_id": "u1", "sec_uid": "MS4wLjABAAAAuser1", "uid": "1"},
    },
    {
        "cid": "c2",
        "text": "第二条评论",
        "create_time": 1700000600,
        "digg_count": 3,
        "reply_comment_total": 0,
        "ip_label": "北京",
        "user": {"nickname": "路人乙", "unique_id": "u2", "sec_uid": "MS4wLjABAAAAuser2", "uid": "2"},
    },
]


def blob(size: int, seed: int) -> bytes:
    return bytes((seed + i) % 251 for i in range(size))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 安静点
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("x-tt-logid", "mock-logid-0001")
        self.end_headers()
        self.wfile.write(body)

    def _binary(self, data: bytes, ctype: str = "application/octet-stream") -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/":
            body = b"<html><head><title>mock</title></head><body>douyin mock</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", "ttwid=mock-ttwid-value; Path=/")
            self.send_header("Set-Cookie", "msToken=mock-ms-token-value; Path=/")
            self.send_header("Set-Cookie", "__ac_nonce=mock-ac-nonce; Path=/")
            self.end_headers()
            self.wfile.write(body)
            return

        if path.startswith("/media/"):
            name = path.rsplit("/", 1)[-1]
            seed = sum(ord(ch) for ch in name)
            if name.endswith(".jpg"):
                self._binary(blob(2048, seed), "image/jpeg")
            elif name.endswith(".mp3"):
                self._binary(blob(2048, seed), "audio/mpeg")
            else:
                self._binary(blob(64 * 1024, seed), "video/mp4")
            return

        # 所有业务接口都必须带 a_bogus，否则视为签名缺失
        if not query.get("a_bogus"):
            self._json({"status_code": 10001, "status_msg": "missing a_bogus"})
            return
        abogus = query["a_bogus"][0]
        if len(abogus) < 100:
            self._json({"status_code": 10002, "status_msg": "bad a_bogus length"})
            return

        if path == "/aweme/v1/web/user/profile/other/":
            self._json(
                {
                    "status_code": 0,
                    "user": {
                        "nickname": NICKNAME,
                        "sec_uid": (query.get("sec_user_id") or [SEC_UID])[0],
                        "signature": "这是个测试账号",
                        "follower_count": 12345,
                        "aweme_count": len(VIDEOS),
                    },
                }
            )
            return

        if path == "/aweme/v1/web/user/profile/self/":
            self._json({"status_code": 0, "user": {"nickname": "我的账号", "sec_uid": SEC_UID}})
            return

        if path in ("/aweme/v1/web/aweme/post/", "/aweme/v1/web/aweme/favorite/", "/aweme/v1/web/aweme/collect/"):
            cursor = int((query.get("max_cursor") or ["0"])[0])
            page_size = 2
            chunk = VIDEOS[cursor : cursor + page_size]
            self._json(
                {
                    "status_code": 0,
                    "aweme_list": chunk,
                    "has_more": 1 if cursor + page_size < len(VIDEOS) else 0,
                    "max_cursor": cursor + page_size,
                }
            )
            return

        if path == "/aweme/v1/web/comment/list/":
            cursor = int((query.get("cursor") or ["0"])[0])
            chunk = [] if cursor > 0 else COMMENTS
            self._json(
                {
                    "status_code": 0,
                    "comments": chunk,
                    "has_more": 0,
                    "cursor": cursor + len(chunk),
                }
            )
            return

        if path == "/aweme/v1/web/aweme/detail/":
            aweme_id = (query.get("aweme_id") or [""])[0]
            detail = next((v for v in VIDEOS if v["aweme_id"] == aweme_id), None)
            self._json({"status_code": 0, "aweme_detail": detail})
            return

        self._json({"status_code": 404, "status_msg": f"unknown path {path}"})


def main() -> None:
    os.environ.setdefault("MOCK_READY", "1")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"mock server on {BASE}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
