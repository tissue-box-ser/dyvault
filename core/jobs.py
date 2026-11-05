"""任务执行器：编排抓取、下载、评论采集，支持暂停 / 取消 / 续跑。

两种落盘方式：
- storage=server   媒体下载到当前服务器的下载根目录（默认）
- storage=browser  只抓元数据与直链，媒体由浏览器保存到操作者本机
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import consts
from .client import AuthError, DouyinClient, DouyinError, RiskControlError
from .config import Settings
from .cookies import CookiePool
from .db import Database
from .downloader import parallel_download
from .store import (
    AuthorStore,
    RootIndex,
    aweme_summary,
    comment_summary,
    cover_url,
    image_urls,
    music_url,
    video_candidates,
)

DEFAULT_OPTIONS: Dict[str, object] = {
    "limit": 0,
    "download_video": True,
    "download_images": True,
    "download_cover": False,
    "download_music": False,
    "save_comments": True,
    "max_comments": 200,
    "include_replies": False,
    "save_raw": False,
    "overwrite": False,
    "workers": 4,
    "storage": "server",       # server | browser
    "dir_layout": "collection",  # collection | per_aweme
}

STORAGE_LABELS = {"server": "服务器本地", "browser": "浏览器本机"}
LAYOUT_LABELS = {"collection": "按类型合集", "per_aweme": "单作品目录"}

IMAGE_EXT = (".jpeg", ".jpg", ".webp", ".png", ".heic")


class JobCanceled(Exception):
    pass


class JobFailed(Exception):
    pass


def guess_ext(url: str, default: str = ".jpg") -> str:
    path = (url or "").split("?")[0].lower()
    for ext in IMAGE_EXT:
        if path.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return default


class JobRunner:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        cookies: CookiePool,
        on_event: Optional[Callable] = None,
    ):
        self.db = db
        self.settings = settings
        self.cookies = cookies
        self.on_event = on_event
        self._threads: Dict[int, threading.Thread] = {}
        self._stops: Dict[int, threading.Event] = {}
        self._pauses: Dict[int, threading.Event] = {}
        self._lock = threading.Lock()
        self._sem = threading.Semaphore(max(1, settings.get_int("max_concurrent_jobs", 1)))
        self._log_index = RootIndex(settings.download_root())

    # ---------------------------------------------------------------- 控制

    def start(self, job_id: int) -> bool:
        with self._lock:
            thread = self._threads.get(job_id)
            if thread and thread.is_alive():
                return False
            job = self.db.get_job(job_id)
            if not job:
                return False
            self._stops[job_id] = threading.Event()
            self._pauses[job_id] = threading.Event()
            thread = threading.Thread(
                target=self._run, args=(job_id,), name=f"job-{job_id}", daemon=True
            )
            self._threads[job_id] = thread
            thread.start()
            return True

    def pause(self, job_id: int) -> bool:
        pause = self._pauses.get(job_id)
        thread = self._threads.get(job_id)
        if not pause or not thread or not thread.is_alive():
            return False
        pause.set()
        self.db.update_job(job_id, status="paused")
        self.db.add_log(job_id, "info", "任务已暂停")
        return True

    def resume(self, job_id: int) -> bool:
        pause = self._pauses.get(job_id)
        thread = self._threads.get(job_id)
        if pause and thread and thread.is_alive():
            pause.clear()
            self.db.update_job(job_id, status="running")
            self.db.add_log(job_id, "info", "任务已继续")
            return True
        return self.start(job_id)

    def cancel(self, job_id: int) -> bool:
        stop = self._stops.get(job_id)
        pause = self._pauses.get(job_id)
        if pause:
            pause.clear()
        if stop:
            stop.set()
        thread = self._threads.get(job_id)
        if not thread or not thread.is_alive():
            self.db.update_job(job_id, status="canceled")
        return True

    def is_running(self, job_id: int) -> bool:
        thread = self._threads.get(job_id)
        return bool(thread and thread.is_alive())

    def running_ids(self) -> List[int]:
        return [jid for jid, t in self._threads.items() if t.is_alive()]

    # ---------------------------------------------------------------- 内部

    def _log(self, job_id: int, message: str, level: str = "info") -> None:
        self.db.add_log(job_id, level, message)
        print(f"[job {job_id}] {message}", flush=True)
        if self.on_event:
            try:
                self.on_event({"type": "log", "job_id": job_id, "message": message, "level": level})
            except Exception:
                pass

    def _checkpoint(self, job_id: int) -> None:
        stop = self._stops.get(job_id)
        pause = self._pauses.get(job_id)
        if stop and stop.is_set():
            raise JobCanceled()
        while pause and pause.is_set():
            if stop and stop.is_set():
                raise JobCanceled()
            time.sleep(0.5)

    def _run(self, job_id: int) -> None:
        self._sem.acquire()
        try:
            self._execute(job_id)
        except JobCanceled:
            self.db.update_job(job_id, status="canceled")
            self._log(job_id, "任务已取消", "warn")
        except JobFailed as exc:
            self.db.update_job(job_id, status="error", error=str(exc))
            self._log(job_id, f"任务失败：{exc}", "error")
        except Exception as exc:  # noqa: BLE001
            self.db.update_job(job_id, status="error", error=f"{type(exc).__name__}: {exc}")
            self._log(job_id, f"任务异常：{type(exc).__name__}: {exc}", "error")
        finally:
            self._sem.release()
            with self._lock:
                self._stops.pop(job_id, None)
                self._pauses.pop(job_id, None)

    def _execute(self, job_id: int) -> None:
        job = self.db.get_job(job_id)
        if not job:
            return
        opts = dict(DEFAULT_OPTIONS)
        opts.update(job.get("options") or {})

        if not self.cookies.count():
            raise JobFailed("还没有可用的 Cookie，请在左侧「设置 → Cookie 账号池」添加")

        # 一个任务内遇到失效账号时自动换下一个，最多尝试全部账号
        tried: set = set()
        last_error: Optional[Exception] = None
        while True:
            row = self.cookies.pick(exclude=tried)
            if not row:
                raise JobFailed(
                    "所有 Cookie 都不可用，请在「设置 → Cookie 账号池」里补充或更新账号"
                    + (f"（最后一次错误：{last_error}）" if last_error else "")
                )
            try:
                self._run_attempt(job_id, job, opts, row)
                return
            except (AuthError, RiskControlError) as exc:
                last_error = exc
                tried.add(row["id"])
                status = "bad" if isinstance(exc, AuthError) else "risk"
                self.cookies.mark(row["id"], status, str(exc))
                remaining = [c for c in self.cookies.enabled() if c["id"] not in tried]
                self._log(
                    job_id,
                    f"账号「{row['label']}」不可用（{exc}），"
                    + (f"切换到下一个账号重试" if remaining else "已无可用账号"),
                    "warn",
                )
                if not remaining:
                    raise JobFailed(
                        f"所有 Cookie 都失败了，最后一次：{exc}。"
                        "建议换一个新 Cookie，并把请求间隔调到 2 秒以上"
                    ) from exc

    def _run_attempt(self, job_id: int, job: dict, opts: dict, cookie_row: dict) -> None:
        storage = opts.get("storage") or "server"
        layout = opts.get("dir_layout") or "collection"
        proxy = self.settings.get("proxy")
        provider = self.cookies.provider(log=lambda msg: self._log(job_id, msg, "debug")) \
            if self.cookies.count() > 1 else None

        client = DouyinClient(
            cookie=cookie_row["value"],
            user_agent=self.settings.get("user_agent"),
            proxy=proxy,
            min_interval=self.settings.get_float("min_interval", 1.2),
            jitter=self.settings.get_float("jitter", 0.8),
            log=lambda msg: self._log(job_id, msg, "debug"),
            cookie_provider=provider,
        )

        self.db.update_job(job_id, status="running", error="")
        self._log(
            job_id,
            f"开始任务：{job['title']}（来源 {consts.SOURCE_LABELS.get(job['source'], job['source'])}"
            f"｜保存到{STORAGE_LABELS.get(storage, storage)}｜{LAYOUT_LABELS.get(layout, layout)}"
            f"｜账号 {cookie_row['label']}）",
        )

        try:
            client.bootstrap()
            self.cookies.mark(cookie_row["id"], "ok", "")
            if job["source"] in (consts.SOURCE_LIKE, consts.SOURCE_COLLECT) and not client.has_login:
                raise JobFailed("点赞 / 收藏列表需要登录态 Cookie（缺少 sessionid）")

            profile = client.get_user_profile(job["sec_uid"])
            nickname = profile.get("nickname") or job.get("nickname") or "unknown"
            self._log(
                job_id,
                f"博主：{nickname}｜粉丝 {profile.get('follower_count', 0)}｜作品 {profile.get('aweme_count', 0)}",
            )

            store = AuthorStore(self.settings.root_for(layout), nickname, job["sec_uid"], layout)
            store.ensure()
            self.db.update_job(job_id, nickname=nickname, dir=str(store.root), status="running")
            self._log(job_id, f"输出目录：{store.root}")

            stat = {"done": 0, "failed": 0, "comments": 0, "bytes": 0, "seen": 0}
            limit = int(opts.get("limit") or 0)

            def on_page(fetched: int, has_more: bool) -> None:
                self.db.update_job(job_id, total=fetched)

            iterator = client.iter_user_awemes(
                job["sec_uid"],
                source=job["source"],
                limit=limit,
                on_page=on_page,
                should_stop=lambda: self._stops.get(job_id, threading.Event()).is_set(),
            )
            for aweme in iterator:
                self._checkpoint(job_id)
                stat["seen"] += 1
                self._handle_aweme(job_id, aweme, store, opts, client, stat, job["source"], proxy)
                self.db.update_job(
                    job_id,
                    total=stat["seen"],
                    done=stat["done"],
                    failed=stat["failed"],
                    comments=stat["comments"],
                    bytes=stat["bytes"],
                )

            if stat["seen"] == 0:
                hint = ""
                if job["source"] == consts.SOURCE_COLLECT:
                    hint = "（收藏列表只能抓取自己账号的数据，且需要登录 Cookie）"
                elif job["source"] == consts.SOURCE_LIKE:
                    hint = "（对方可能关闭了点赞列表，或需要登录 Cookie）"
                raise JobFailed(f"没有抓到任何作品{hint}")

            self.db.update_job(job_id, status="done", error="")
            finished = "已生成下载清单，可在任务卡点「下载到本机」" if storage == "browser" else ""
            self._log(
                job_id,
                f"完成：作品 {stat['done']} 成功 / {stat['failed']} 失败，评论 {stat['comments']} 条，"
                f"新增 {stat['bytes'] / 1024 / 1024:.1f} MB。{finished}",
                "success",
            )
        finally:
            client.close()

    def _handle_aweme(
        self,
        job_id: int,
        aweme: dict,
        store: AuthorStore,
        opts: dict,
        client: DouyinClient,
        stat: dict,
        source: str,
        proxy: str,
    ) -> None:
        summary = aweme_summary(aweme, source)
        aweme_id = summary["aweme_id"]
        if not aweme_id:
            return

        browser = opts.get("storage") == "browser"
        files: Dict[str, str] = {}
        media: List[dict] = []
        errors: List[str] = []
        added_bytes = 0

        def add_media(kind: str, url: str, path: Path) -> None:
            media.append(
                {
                    "kind": kind,
                    "name": str(path.relative_to(store.root)).replace(os.sep, "/"),
                    "url": url,
                }
            )

        tasks = []
        common = {
            "user_agent": client.user_agent,
            "proxy": proxy,
            "overwrite": bool(opts.get("overwrite")),
            "referer": summary["url"],
            "retries": 3,
        }

        if summary["media_type"] == "video" and opts.get("download_video"):
            urls = video_candidates(aweme)
            dest = store.video_path(summary)
            if browser:
                if urls:
                    add_media("video", urls[0], dest)
                else:
                    errors.append("没有可用的视频地址")
            elif urls:
                tasks.append({"key": "video", "urls": urls, "dest": dest, **common})
            else:
                errors.append("没有可用的视频地址")

        if summary["media_type"] == "image" and opts.get("download_images"):
            urls = image_urls(aweme)
            folder = store.image_dir_for(summary)
            for index, url in enumerate(urls, start=1):
                dest = folder / f"{index:02d}{guess_ext(url)}"
                if browser:
                    add_media("image", url, dest)
                else:
                    tasks.append({"key": f"image_{index}", "urls": [url], "dest": dest, **common})

        if opts.get("download_cover"):
            url = cover_url(aweme)
            if url:
                dest = store.cover_path(aweme_id, summary)
                if browser:
                    add_media("cover", url, dest)
                else:
                    tasks.append({"key": "cover", "urls": [url], "dest": dest, **common})

        if opts.get("download_music"):
            url = music_url(aweme)
            if url:
                dest = store.music_path(aweme_id, summary)
                if browser:
                    add_media("music", url, dest)
                else:
                    tasks.append({"key": "music", "urls": [url], "dest": dest, **common})

        if tasks:
            results = parallel_download(
                tasks,
                workers=max(1, int(opts.get("workers") or 4)),
                should_stop=lambda: self._stops.get(job_id, threading.Event()).is_set(),
            )
            for key, result in results.items():
                if result.get("status") == "ok":
                    added_bytes += int(result.get("size") or 0)
                    files[key] = os.path.relpath(result["path"], store.root)
                elif result.get("status") == "skip":
                    files[key] = os.path.relpath(result["path"], store.root)
                else:
                    errors.append(f"{key}: {result.get('error')}")

        # 评论（两种存储模式都落在服务器，体量很小且便于检索）
        saved_comments = 0
        if opts.get("save_comments") and summary["comment_count"]:
            limit = int(opts.get("max_comments") or 0)
            cpath = store.comment_path(aweme_id, summary)
            if cpath.exists() and limit == 0 and not opts.get("overwrite"):
                saved_comments = sum(1 for _ in cpath.open(encoding="utf-8"))
            else:
                if opts.get("overwrite") and cpath.exists():
                    cpath.unlink()
                try:
                    for comment in client.iter_comments(
                        aweme_id,
                        limit=limit,
                        include_replies=bool(opts.get("include_replies")),
                        should_stop=lambda: self._stops.get(job_id, threading.Event()).is_set(),
                    ):
                        self._checkpoint(job_id)
                        store.append_comment(aweme_id, comment_summary(comment), summary)
                        saved_comments += 1
                except RiskControlError:
                    raise
                except (DouyinError, AuthError) as exc:
                    errors.append(f"comments: {exc}")

        if browser:
            summary["_files"] = {}
            summary["_media"] = media
            summary["_status"] = "ok" if media else "failed"
        else:
            files["comments"] = saved_comments if saved_comments else ""
            summary["_files"] = {k: v for k, v in files.items() if v != ""}
            summary["_media"] = []
            expected_video = summary["media_type"] == "video" and opts.get("download_video")
            if expected_video and "video" not in summary["_files"]:
                summary["_status"] = "failed"
            elif errors:
                summary["_status"] = "partial"
            else:
                summary["_status"] = "ok"

        summary["_error"] = "; ".join(errors)[:500]
        payload = {"files": summary["_files"], "status": summary["_status"]}
        if browser:
            payload["media"] = summary["_media"]
        if opts.get("save_raw"):
            payload["raw"] = aweme
        store.write_meta(summary, payload)
        store.append_index(
            {
                "aweme_id": aweme_id,
                "desc": summary["desc"][:200],
                "create_time": summary["create_date"],
                "media_type": summary["media_type"],
                "digg_count": summary["digg_count"],
                "comment_count": summary["comment_count"],
                "collect_count": summary["collect_count"],
                "share_count": summary["share_count"],
                "source": consts.SOURCE_LABELS.get(source, source),
                "video_file": summary["_files"].get("video", ""),
                "image_count": sum(1 for k in summary["_files"] if k.startswith("image_")),
                "comments_saved": saved_comments,
                "url": summary["url"],
            }
        )
        self.db.upsert_aweme(job_id, summary)

        stat["done"] += 1 if summary["_status"] != "failed" else 0
        stat["failed"] += 1 if summary["_status"] == "failed" else 0
        stat["comments"] += saved_comments
        stat["bytes"] += added_bytes

        if summary["_status"] == "failed":
            self._log(job_id, f"作品 {aweme_id} 处理失败：{summary['_error']}", "warn")
        elif stat["done"] % 20 == 0:
            self._log(job_id, f"进度：已处理 {stat['seen']} 个作品")
