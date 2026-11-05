"""作品元数据提取、目录结构与元数据落盘。"""

from __future__ import annotations

import csv
import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
SPACE_CHARS = re.compile(r"\s+")

CSV_FIELDS = [
    "aweme_id",
    "desc",
    "create_time",
    "media_type",
    "digg_count",
    "comment_count",
    "collect_count",
    "share_count",
    "source",
    "video_file",
    "image_count",
    "comments_saved",
    "url",
]

_csv_lock = threading.Lock()


def sanitize_filename(name: str, max_bytes: int = 80) -> str:
    """去掉路径非法字符，并按 UTF-8 字节数截断（Linux 单个文件名上限 255 字节）。"""
    name = INVALID_CHARS.sub("_", (name or "").strip())
    name = SPACE_CHARS.sub(" ", name).strip(" .")
    encoded = name.encode("utf-8")
    if len(encoded) > max_bytes:
        name = encoded[:max_bytes].decode("utf-8", "ignore")
    return name or "untitled"


def format_time(ts: Optional[int]) -> str:
    if not ts:
        return "unknown"
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y%m%d")
    except (ValueError, OSError):
        return "unknown"


def _first_url(node: Optional[dict]) -> str:
    if not node:
        return ""
    urls = node.get("url_list") or []
    return urls[0] if urls else ""


def clean_video_url(url: str) -> str:
    if not url:
        return url
    url = url.replace("/playwm/", "/play/").replace("playwm", "play")
    return url


def video_candidates(aweme: dict) -> List[str]:
    """按码率从高到低给出无水印播放地址候选。"""
    video = aweme.get("video") or {}
    urls: List[str] = []

    for key in ("play_addr_h264", "play_addr"):
        url = _first_url(video.get(key))
        if url:
            urls.append(clean_video_url(url))

    bit_rates = video.get("bit_rate") or []
    try:
        bit_rates = sorted(bit_rates, key=lambda x: x.get("bit_rate") or 0, reverse=True)
    except TypeError:
        pass
    for item in bit_rates:
        url = _first_url(item.get("play_addr"))
        if url:
            urls.append(clean_video_url(url))

    uri = (video.get("play_addr") or {}).get("uri") or video.get("vid")
    if uri:
        urls.append(
            f"https://www.iesdouyin.com/aweme/v1/play/?video_id={uri}&ratio=1080p&line=0"
        )

    seen, unique = set(), []
    for url in urls:
        if url and url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def image_urls(aweme: dict) -> List[str]:
    out = []
    for image in aweme.get("images") or []:
        url = _first_url(image)
        if url:
            out.append(url)
    return out


def music_url(aweme: dict) -> str:
    music = aweme.get("music") or {}
    return _first_url(music.get("play_url"))


def cover_url(aweme: dict) -> str:
    video = aweme.get("video") or {}
    for node in (video.get("cover"), video.get("origin_cover"), video.get("dynamic_cover")):
        url = _first_url(node)
        if url:
            return url
    return ""


def media_type(aweme: dict) -> str:
    if aweme.get("images"):
        return "image"
    return "video"


def aweme_summary(aweme: dict, source: str = "post") -> dict:
    stats = aweme.get("statistics") or {}
    author = aweme.get("author") or {}
    return {
        "aweme_id": aweme.get("aweme_id"),
        "desc": (aweme.get("desc") or "").strip(),
        "create_time": aweme.get("create_time"),
        "create_date": format_time(aweme.get("create_time")),
        "media_type": media_type(aweme),
        "digg_count": stats.get("digg_count", 0),
        "comment_count": stats.get("comment_count", 0),
        "collect_count": stats.get("collect_count", 0),
        "share_count": stats.get("share_count", 0),
        "play_count": stats.get("play_count", 0),
        "source": source,
        "author": {
            "nickname": author.get("nickname"),
            "unique_id": author.get("unique_id") or author.get("short_id"),
            "sec_uid": author.get("sec_uid"),
        },
        "duration_ms": (aweme.get("video") or {}).get("duration"),
        "url": f"https://www.douyin.com/{'note' if aweme.get('images') else 'video'}/{aweme.get('aweme_id')}",
    }


def comment_summary(comment: dict) -> dict:
    user = comment.get("user") or {}
    return {
        "cid": comment.get("cid"),
        "text": comment.get("text"),
        "create_time": comment.get("create_time"),
        "digg_count": comment.get("digg_count", 0),
        "reply_comment_total": comment.get("reply_comment_total", 0),
        "ip_label": comment.get("ip_label"),
        "is_reply": bool(comment.get("reply_id") and comment.get("reply_id") != "0"),
        "user": {
            "nickname": user.get("nickname"),
            "unique_id": user.get("unique_id") or user.get("short_id"),
            "sec_uid": user.get("sec_uid"),
            "uid": user.get("uid"),
        },
    }


class AuthorStore:
    """一个博主对应一个根目录。

    layout = collection  按类型归档：video/ image/ cover/ music/ data/ comments/
    layout = per_aweme   每个作品一个目录：works/<日期>_<id>_<描述>/{video.mp4, images/, meta.json …}
    """

    def __init__(
        self,
        root: str | Path,
        nickname: str,
        sec_uid: str,
        layout: str = "collection",
    ):
        base_name = sanitize_filename(nickname or "unknown", 40)
        suffix = (sec_uid or "")[-8:]
        self.root = Path(root).expanduser().resolve() / f"{base_name}_{suffix}"
        self.layout = layout if layout in ("collection", "per_aweme") else "collection"
        self.video_dir = self.root / "video"
        self.image_dir = self.root / "image"
        self.cover_dir = self.root / "cover"
        self.music_dir = self.root / "music"
        self.data_dir = self.root / "data"
        self.comment_dir = self.root / "comments"
        self.index_file = self.root / "index.csv"

    def ensure(self) -> None:
        if self.layout == "per_aweme":
            self.root.mkdir(parents=True, exist_ok=True)
        else:
            for path in (
                self.video_dir,
                self.image_dir,
                self.cover_dir,
                self.music_dir,
                self.data_dir,
                self.comment_dir,
            ):
                path.mkdir(parents=True, exist_ok=True)
        if not self.index_file.exists():
            with _csv_lock:
                if not self.index_file.exists():
                    with self.index_file.open("w", newline="", encoding="utf-8-sig") as fp:
                        csv.writer(fp).writerow(CSV_FIELDS)

    # -------------------------------------------------------------- 目录结构

    def aweme_dir(self, summary: dict) -> Path:
        date = summary.get("create_date") or "unknown"
        desc = sanitize_filename(summary.get("desc") or "untitled", 50)
        return self.root / "works" / f"{date}_{summary['aweme_id']}_{desc}"

    # -------------------------------------------------------------- 文件命名

    def video_path(self, summary: dict) -> Path:
        if self.layout == "per_aweme":
            return self.aweme_dir(summary) / "video.mp4"
        date = summary.get("create_date") or "unknown"
        desc = sanitize_filename(summary.get("desc") or "video", 60)
        return self.video_dir / f"{date}_{summary['aweme_id']}_{desc}.mp4"

    def image_dir_for(self, summary) -> Path:
        if self.layout == "per_aweme" and isinstance(summary, dict):
            return self.aweme_dir(summary) / "images"
        aweme_id = summary.get("aweme_id") if isinstance(summary, dict) else summary
        return self.image_dir / str(aweme_id)

    def cover_path(self, aweme_id: str, summary: Optional[dict] = None) -> Path:
        if self.layout == "per_aweme" and summary:
            return self.aweme_dir(summary) / "cover.jpg"
        return self.cover_dir / f"{aweme_id}.jpg"

    def music_path(self, aweme_id: str, summary: Optional[dict] = None) -> Path:
        if self.layout == "per_aweme" and summary:
            return self.aweme_dir(summary) / "music.mp3"
        return self.music_dir / f"{aweme_id}.mp3"

    def data_path(self, aweme_id: str, summary: Optional[dict] = None) -> Path:
        if self.layout == "per_aweme" and summary:
            return self.aweme_dir(summary) / "meta.json"
        return self.data_dir / f"{aweme_id}.json"

    def comment_path(self, aweme_id: str, summary: Optional[dict] = None) -> Path:
        if self.layout == "per_aweme" and summary:
            return self.aweme_dir(summary) / "comments.jsonl"
        return self.comment_dir / f"{aweme_id}.jsonl"

    # -------------------------------------------------------------- 落盘

    def write_meta(self, summary: dict, extra: Optional[dict] = None) -> None:
        payload = dict(summary)
        if extra:
            payload.update(extra)
        path = self.data_path(summary["aweme_id"], summary)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def append_comment(self, aweme_id: str, comment: dict, summary: Optional[dict] = None) -> None:
        path = self.comment_path(aweme_id, summary)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(comment, ensure_ascii=False) + "\n")

    def append_index(self, row: Dict) -> None:
        with _csv_lock:
            with self.index_file.open("a", newline="", encoding="utf-8-sig") as fp:
                writer = csv.DictWriter(fp, fieldnames=CSV_FIELDS, extrasaction="ignore")
                writer.writerow(row)


class RootIndex:
    """全局汇总表，记录每个任务抓了哪些作品（便于跨任务去重与检索）。"""

    def __init__(self, root: str | Path):
        self.path = Path(root).expanduser().resolve() / "all_awemes.csv"
        self._lock = threading.Lock()

    def append(self, row: Dict) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            exists = self.path.exists()
            with self.path.open("a", newline="", encoding="utf-8-sig") as fp:
                writer = csv.DictWriter(fp, fieldnames=CSV_FIELDS, extrasaction="ignore")
                if not exists:
                    writer.writeheader()
                writer.writerow(row)


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.1f}{unit}" if unit != "B" else f"{int(num)}B"
        num /= 1024
    return f"{num:.1f}TB"


def now_ts() -> int:
    return int(time.time())
