"""端到端测试：桩服务 + 完整任务流程，验证签名拼接、分页、下载、评论、落盘。

运行：python tests/e2e_test.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8099
BASE = f"http://127.0.0.1:{PORT}"
SEC_UID = "MS4wLjABAAAAtestsecuid000000000000000000000"

os.environ["DOUYIN_WEB_BASE"] = BASE
os.environ["DOUYIN_COOKIE"] = "sessionid=fake-session; ttwid=mock; msToken=mock"

sys.path.insert(0, str(ROOT))


def wait_ready(timeout: float = 20.0) -> None:
    import httpx

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(BASE + "/", timeout=1).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise SystemExit("mock server 未能就绪")


def main() -> int:
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "tests" / "mock_server.py"), str(PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    failures = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        print(("  OK   " if condition else "  FAIL ") + name + (f"  <- {detail}" if not condition and detail else ""))
        if not condition:
            failures.append(name)

    try:
        wait_ready()

        from core.config import Settings
        from core.cookies import CookiePool
        from core.db import Database
        from core.jobs import JobRunner

        workdir = Path(tempfile.mkdtemp(prefix="douyin-e2e-"))
        db = Database(workdir / "app.db")
        settings = Settings(db, workdir)
        settings.set("download_root", str(workdir / "downloads"))
        settings.set("min_interval", "0")
        settings.set("jitter", "0")

        options = {
            "limit": 0,
            "download_video": True,
            "download_images": True,
            "download_cover": True,
            "download_music": True,
            "save_comments": True,
            "max_comments": 50,
            "include_replies": False,
            "save_raw": False,
            "overwrite": False,
            "workers": 3,
        }

        pool = CookiePool(db, settings)
        db.add_cookie("测试账号 A", "sessionid=fake-session; ttwid=mock; msToken=mock")
        db.add_cookie("测试账号 B", "sessionid=fake-session-b; ttwid=mock; msToken=mock")

        job_id = db.create_job("测试博主 · 作品", SEC_UID, "测试博主", "post", options)
        runner = JobRunner(db, settings, pool)
        runner.start(job_id)

        deadline = time.time() + 60
        while runner.is_running(job_id) and time.time() < deadline:
            time.sleep(0.3)

        job = db.get_job(job_id)
        print(f"\n任务状态: {job['status']}  作品 {job['done']}/{job['total']}  评论 {job['comments']}  失败 {job['failed']}")
        if job.get("error"):
            print("错误:", job["error"])

        check("任务状态为 done", job["status"] == "done", job.get("error", ""))
        check("抓到 3 个作品", job["total"] == 3, str(job["total"]))
        check("成功 3 个", job["done"] == 3, str(job["done"]))
        check("无失败", job["failed"] == 0, str(job["failed"]))

        root = Path(job["dir"])
        check("输出目录存在", root.exists(), str(root))

        videos = list((root / "video").glob("*.mp4"))
        check("视频文件 2 个", len(videos) == 2, str([p.name for p in videos]))
        if videos:
            check("视频体积正常", videos[0].stat().st_size > 4096, str(videos[0].stat().st_size))
            check("文件名含作品 ID", "7000000000000000001" in videos[0].name, videos[0].name)

        images = list((root / "image").rglob("*.jpg"))
        check("图集图片 2 张", len(images) == 2, str([p.name for p in images]))

        covers = list((root / "cover").glob("*.jpg"))
        check("封面 2 张（图集无封面）", len(covers) == 2, str([p.name for p in covers]))

        music = list((root / "music").glob("*.mp3"))
        check("背景音乐 3 个", len(music) == 3, str([p.name for p in music]))

        meta = list((root / "data").glob("*.json"))
        check("元数据 3 份", len(meta) == 3, str(len(meta)))

        comment_file = root / "comments" / "7000000000000000001.jsonl"
        check("评论文件存在", comment_file.exists(), str(comment_file))
        if comment_file.exists():
            lines = [line for line in comment_file.read_text(encoding="utf-8").splitlines() if line.strip()]
            check("评论 2 条", len(lines) == 2, str(len(lines)))
            check("评论内容可解析", "第一条评论" in comment_file.read_text(encoding="utf-8"))

        index = root / "index.csv"
        check("index.csv 存在", index.exists(), str(index))
        if index.exists():
            rows = index.read_text(encoding="utf-8-sig").strip().splitlines()
            check("index.csv 3 行数据", len(rows) == 4, str(len(rows)))

        awemes = db.list_awemes(job_id)
        check("数据库记录 3 条", len(awemes) == 3, str(len(awemes)))
        if awemes:
            one = next((a for a in awemes if a["aweme_id"] == "7000000000000000001"), None)
            check("点赞数入库", bool(one) and one["digg"] == 1200, str(one and one["digg"]))

        # 幂等性：重跑不应重复下载
        db.update_job(job_id, status="pending", error="")
        runner2 = JobRunner(db, settings, pool)
        runner2.start(job_id)
        deadline = time.time() + 60
        while runner2.is_running(job_id) and time.time() < deadline:
            time.sleep(0.3)
        job2 = db.get_job(job_id)
        check("重跑仍然成功", job2["status"] == "done", job2.get("error", ""))
        check("重跑不产生额外体积", job2["bytes"] == 0, str(job2["bytes"]))
        check("视频数量不变", len(list((root / "video").glob("*.mp4"))) == 2)

        # 签名必须带在请求上（桩服务会拒绝缺失 a_bogus 的请求）
        from core import consts
        from core.client import DouyinClient

        client = DouyinClient(cookie="sessionid=x; ttwid=mock; msToken=mock", min_interval=0)
        client.bootstrap()
        profile = client.get_user_profile(SEC_UID)
        check("签名请求拿到博主资料", profile.get("nickname") == "测试博主", str(profile)[:120])

        items = list(client.iter_user_awemes(SEC_UID, limit=0))
        check("分页迭代拿到 3 条", len(items) == 3, str(len(items)))
        client.close()

        # 多账号轮询：provider 每次返回不同账号
        seen = {pool.provider()() for _ in range(4)}
        check("Cookie 池轮询到多个账号", len(seen) == 2, str(len(seen)))
        # 环境变量 DOUYIN_COOKIE 会被自动导入为第一个账号（老配置迁移路径）
        check("Cookie 池统计正确（含环境变量导入）", pool.count(enabled_only=False) == 3,
              str(pool.count(enabled_only=False)))

        # 单作品目录布局
        layout_opts = dict(options)
        layout_opts["dir_layout"] = "per_aweme"
        layout_job = db.create_job("测试博主 · 单作品目录", SEC_UID, "测试博主", "post", layout_opts)
        runner3 = JobRunner(db, settings, pool)
        runner3.start(layout_job)
        deadline = time.time() + 60
        while runner3.is_running(layout_job) and time.time() < deadline:
            time.sleep(0.3)
        job3 = db.get_job(layout_job)
        check("单作品目录任务完成", job3["status"] == "done", job3.get("error", ""))
        check("单作品目录落在独立根路径", "per_aweme" in job3["dir"], job3["dir"])
        works = list((Path(job3["dir"]) / "works").glob("*/video.mp4"))
        check("每个作品独立目录", len(works) == 2, str([p.parent.name for p in works]))
        check("作品目录含 meta", len(list((Path(job3["dir"]) / "works").glob("*/meta.json"))) == 3)

        # 浏览器本机模式：只出清单，不落媒体
        browser_opts = dict(options)
        browser_opts["storage"] = "browser"
        browser_opts["dir_layout"] = "collection"
        browser_job = db.create_job("测试博主 · 下载到本机", SEC_UID, "测试博主", "post", browser_opts)
        runner4 = JobRunner(db, settings, pool)
        runner4.start(browser_job)
        deadline = time.time() + 60
        while runner4.is_running(browser_job) and time.time() < deadline:
            time.sleep(0.3)
        job4 = db.get_job(browser_job)
        check("本机模式任务完成", job4["status"] == "done", job4.get("error", ""))
        media_rows = db.list_aweme_media(browser_job)
        media_count = sum(len(row["media"]) for row in media_rows)
        check("本机模式生成媒体清单", media_count >= 6, str(media_count))
        check("本机模式不落媒体文件", all(not row["files"] for row in media_rows), "files 应为空")
        check("本机模式清单含直链", all("url" in m for row in media_rows for m in row["media"]), "missing url")

        print("\n结果:", "全部通过" if not failures else f"{len(failures)} 项失败 -> {failures}")
        return 0 if not failures else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
