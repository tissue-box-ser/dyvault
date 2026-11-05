"""命令行入口：不启动 Web 界面直接下载。

示例：
    python cli.py -u "https://www.douyin.com/user/MS4wLjABAAAA..." --limit 50 --comments 200
    python cli.py -u MS4wLjABAAAA... --source like --only-meta
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from core.client import AuthError, DouyinClient, DouyinError
from core.config import Settings
from core.cookies import CookiePool
from core.db import Database
from core.jobs import DEFAULT_OPTIONS, JobRunner
from core.parser import ParseError, parse_target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="抖音博主作品批量下载（命令行模式）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-u", "--url", required=True, help="博主主页链接 / 分享链接 / sec_uid")
    parser.add_argument(
        "-s", "--source", default="post", choices=["post", "like", "collect"],
        help="抓取来源：post 作品（默认）、like 点赞、collect 收藏",
    )
    parser.add_argument("-n", "--limit", type=int, default=0, help="最多抓取作品数，0 表示全部")
    parser.add_argument("-o", "--out", default="", help="下载根目录，默认 ~/douyin-downloads")
    parser.add_argument("--cookie", default="", help="抖音 Cookie，也可用环境变量 DOUYIN_COOKIE")
    parser.add_argument("--cookie-file", default="", help="从文件读取 Cookie")
    parser.add_argument("--comments", type=int, default=200, help="每个作品评论上限，0 表示全部")
    parser.add_argument("--replies", action="store_true", help="同时抓取二级回复")
    parser.add_argument("--no-video", action="store_true", help="不下载视频文件")
    parser.add_argument("--no-images", action="store_true", help="不下载图集")
    parser.add_argument("--cover", action="store_true", help="保存封面")
    parser.add_argument("--music", action="store_true", help="保存背景音乐")
    parser.add_argument("--only-meta", action="store_true", help="只保存元数据，不下载任何媒体")
    parser.add_argument("--raw", action="store_true", help="保存抖音原始 JSON")
    parser.add_argument("--interval", type=float, default=1.2, help="请求间隔秒数，默认 1.2")
    parser.add_argument("--workers", type=int, default=4, help="单作品并发下载数")
    parser.add_argument("--proxy", default="", help="代理地址，如 http://127.0.0.1:7890")
    parser.add_argument("--data-dir", default="", help="数据目录（默认项目下 data/）")
    parser.add_argument(
        "--layout", default="collection", choices=["collection", "per_aweme"],
        help="目录结构：collection 按类型合集（默认）、per_aweme 每个作品一个目录",
    )
    return parser


def resolve_cookie(args) -> str:
    if args.cookie_file:
        path = Path(args.cookie_file).expanduser()
        if not path.exists():
            raise SystemExit(f"Cookie 文件不存在: {path}")
        return path.read_text(encoding="utf-8").strip()
    if args.cookie:
        return args.cookie.strip()
    return os.environ.get("DOUYIN_COOKIE", "").strip()


def main() -> int:
    args = build_parser().parse_args()

    cookie = resolve_cookie(args)
    if not cookie:
        print("缺少 Cookie：用 --cookie、--cookie-file 或环境变量 DOUYIN_COOKIE 提供", file=sys.stderr)
        return 2

    try:
        target = parse_target(args.url)
    except ParseError as exc:
        print(f"链接解析失败: {exc}", file=sys.stderr)
        return 2
    if target["kind"] != "user":
        print("请提供博主主页链接，而不是单个作品链接", file=sys.stderr)
        return 2

    root = Path(args.data_dir).expanduser() if args.data_dir else Path(__file__).resolve().parent / "data"
    db = Database(root / "app.db")
    settings = Settings(db, root)
    settings.set("min_interval", str(args.interval))
    settings.set("workers", str(args.workers))
    if args.out:
        settings.set("download_root", args.out)
    if args.proxy:
        settings.set("proxy", args.proxy)

    pool = CookiePool(db, settings)
    if not pool.count(enabled_only=False):
        db.add_cookie("CLI 导入", cookie)
    elif args.cookie or args.cookie_file:
        db.add_cookie(f"CLI 导入 {time.strftime('%m-%d %H:%M')}", cookie)

    options = dict(DEFAULT_OPTIONS)
    options.update(
        {
            "limit": args.limit,
            "max_comments": args.comments,
            "include_replies": args.replies,
            "download_video": not args.no_video and not args.only_meta,
            "download_images": not args.no_images and not args.only_meta,
            "download_cover": args.cover,
            "download_music": args.music,
            "save_comments": args.comments != 0,
            "save_raw": args.raw,
            "workers": args.workers,
            "dir_layout": args.layout,
        }
    )

    # 先拿一次昵称，任务标题和目录名更直观
    nickname = ""
    client = DouyinClient(cookie=cookie, proxy=args.proxy, min_interval=args.interval)
    try:
        client.bootstrap()
        profile = client.get_user_profile(target["sec_uid"])
        nickname = profile.get("nickname") or ""
        print(f"博主：{nickname}｜作品 {profile.get('aweme_count', 0)}｜粉丝 {profile.get('follower_count', 0)}")
    except (DouyinError, AuthError) as exc:
        print(f"获取博主信息失败：{exc}", file=sys.stderr)
    finally:
        client.close()

    job_id = db.create_job(f"{nickname or target['sec_uid'][:12]} · {args.source}", target["sec_uid"], nickname, args.source, options)
    runner = JobRunner(db, settings, pool)
    runner.start(job_id)
    print(f"任务 #{job_id} 已启动，输出目录：{settings.download_root()}")

    last = ""
    try:
        while True:
            job = db.get_job(job_id)
            if not job:
                break
            line = (
                f"状态 {job['status']:<11} 作品 {job['done']}/{job['total'] or '?'} "
                f"评论 {job['comments']} 失败 {job['failed']} "
                f"体积 {job['bytes'] / 1024 / 1024:.1f}MB"
            )
            if line != last:
                print(line, flush=True)
                last = line
            if job["status"] in ("done", "error", "canceled"):
                break
            time.sleep(1.5)
    except KeyboardInterrupt:
        runner.cancel(job_id)
        print("\n已取消")
        return 130

    job = db.get_job(job_id) or {}
    if job.get("status") == "done":
        print(f"完成，文件在 {job.get('dir')}")
        return 0
    print(f"任务结束状态：{job.get('status')}｜{job.get('error') or ''}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
