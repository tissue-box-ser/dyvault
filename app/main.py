"""FastAPI 服务：访问密码、Cookie 池、任务管理、文件浏览与打包下载。"""

from __future__ import annotations

import json
import mimetypes
import os
import platform
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Iterator, Optional
from urllib.parse import quote, urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core import consts
from core.auth import Auth
from core.client import AuthError, DouyinClient, DouyinError, RiskControlError
from core.config import Settings
from core.cookies import CookiePool
from core.db import Database
from core.jobs import DEFAULT_OPTIONS, LAYOUT_LABELS, STORAGE_LABELS, JobRunner
from core.parser import ParseError, parse_target
from core.zipstream import stream_zip

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent
WEB_DIR = BASE_DIR / "web"
DATA_DIR = Path(os.environ.get("DOUYIN_DATA_DIR", ROOT_DIR / "data")).expanduser()
DATA_DIR.mkdir(parents=True, exist_ok=True)

db = Database(DATA_DIR / "app.db")
settings = Settings(db, DATA_DIR)
cookies = CookiePool(db, settings)
auth = Auth(settings)
runner = JobRunner(db, settings, cookies)

PUBLIC_PATHS = {"/login", "/api/login", "/api/auth/state", "/api/auth/setup", "/favicon.ico"}

CDN_HOSTS = (
    "douyin.com",
    "iesdouyin.com",
    "douyinpic.com",
    "douyinvod.com",
    "douyinstatic.com",
    "byteimg.com",
    "snssdk.com",
    "zjcdn.com",
    "pstatp.com",
    "amemv.com",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    interrupted = db.reset_running_jobs()
    if interrupted:
        db.add_log(None, "warn", f"服务重启，{interrupted} 个中断任务已标记为 interrupted")
    db.trim_logs(5000)
    yield


app = FastAPI(title="DyVault · 抖音批量下载", version="2.1.0", lifespan=lifespan)


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static") or path in PUBLIC_PATHS:
        return await call_next(request)

    token = settings.get("auth_token")
    provided = request.headers.get("x-token") or request.query_params.get("token")
    token_ok = bool(token) and provided == token

    if not auth.password_set():
        if path.startswith("/api"):
            return JSONResponse(
                {"detail": "尚未设置访问密码", "need_setup": True}, status_code=401
            )
        return RedirectResponse("/login?setup=1", status_code=302)

    if token_ok or auth.verify_session(request.cookies.get(auth.SESSION_COOKIE)):
        return await call_next(request)

    if path.startswith("/api"):
        return JSONResponse({"detail": "未登录或登录已过期", "need_login": True}, status_code=401)
    return RedirectResponse("/login", status_code=302)


# ------------------------------------------------------------------ 模型


class SettingsPayload(BaseModel):
    values: Dict[str, str] = Field(default_factory=dict)


class JobCreate(BaseModel):
    target: str
    source: str = consts.SOURCE_POST
    options: Dict = Field(default_factory=dict)


class ParsePayload(BaseModel):
    target: str


class LoginPayload(BaseModel):
    password: str = ""
    remember: bool = True


class PasswordPayload(BaseModel):
    old: str = ""
    new: str = ""


class CookiePayload(BaseModel):
    label: str = ""
    value: str = ""
    enabled: Optional[bool] = None


# ------------------------------------------------------------------ 登录


@app.get("/api/auth/state")
def auth_state(request: Request):
    return {
        "password_set": auth.password_set(),
        "authed": auth.verify_session(request.cookies.get(auth.SESSION_COOKIE)),
    }


@app.post("/api/login")
def login(payload: LoginPayload, request: Request):
    ip = request.client.host if request.client else "?"
    remain = auth.locked(ip)
    if remain:
        raise HTTPException(429, f"密码错误次数过多，请 {remain} 秒后再试")
    if not auth.password_set():
        raise HTTPException(400, "尚未设置访问密码")
    if not auth.verify(payload.password):
        auth.fail(ip)
        raise HTTPException(401, "密码不正确")
    auth.reset(ip)
    token = auth.issue()
    response = JSONResponse({"ok": True})
    response.set_cookie(
        auth.SESSION_COOKIE,
        token,
        max_age=30 * 86400,
        httponly=True,
        samesite="lax",
        path="/",
    )
    db.add_log(None, "info", f"登录成功：{ip}")
    return response


@app.post("/api/logout")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


@app.post("/api/auth/setup")
def setup_password(payload: LoginPayload):
    if auth.password_set():
        raise HTTPException(400, "访问密码已设置，请直接登录")
    if len(payload.password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    auth.set_password(payload.password)
    db.add_log(None, "info", "已设置访问密码")
    response = JSONResponse({"ok": True})
    response.set_cookie(
        auth.SESSION_COOKIE, auth.issue(), max_age=30 * 86400, httponly=True, samesite="lax", path="/"
    )
    return response


@app.post("/api/auth/password")
def change_password(payload: PasswordPayload):
    if not auth.verify(payload.old):
        raise HTTPException(400, "原密码不正确")
    if len(payload.new) < 6:
        raise HTTPException(400, "新密码至少 6 位")
    auth.set_password(payload.new)
    db.add_log(None, "info", "访问密码已修改")
    response = JSONResponse({"ok": True})
    response.set_cookie(
        auth.SESSION_COOKIE, auth.issue(), max_age=30 * 86400, httponly=True, samesite="lax", path="/"
    )
    return response


# ------------------------------------------------------------------ 基础


@app.get("/api/health")
def health():
    return {"ok": True, "version": "2.1.0", "running": runner.running_ids()}


@app.get("/api/state")
def state():
    jobs = db.list_jobs(100)
    for job in jobs:
        job["running"] = runner.is_running(job["id"])
    total_bytes = sum(int(j.get("bytes") or 0) for j in jobs)
    total_awemes = sum(int(j.get("done") or 0) for j in jobs)
    enabled = cookies.count()
    return {
        "jobs": jobs,
        "settings": settings.public(),
        "sources": consts.SOURCE_LABELS,
        "defaults": DEFAULT_OPTIONS,
        "storage": STORAGE_LABELS,
        "layouts": LAYOUT_LABELS,
        "cookies": {"total": cookies.count(enabled_only=False), "enabled": enabled},
        "stats": {
            "jobs": len(jobs),
            "awemes": total_awemes,
            "bytes": total_bytes,
            "download_root": str(settings.download_root()),
            "roots": settings.roots(),
        },
        "version": "2.1.0",
    }


@app.get("/api/logs")
def logs(job_id: Optional[int] = None, limit: int = 200):
    return {"logs": db.get_logs(job_id, min(limit, 1000))}


@app.get("/api/system")
def system_info():
    disk = settings.disk_info("collection")
    disks = {"collection": disk, "per_aweme": settings.disk_info("per_aweme")}
    return {
        "ok": True,
        "version": "2.1.0",
        "hostname": platform.node(),
        "platform": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "download_root": disk["path"],
        "disk": disk,
        "roots": settings.roots(),
        "disks": disks,
        "running": runner.running_ids(),
        "concurrency": settings.get_int("max_concurrent_jobs", 1),
        "cookies": {"total": cookies.count(enabled_only=False), "enabled": cookies.count()},
        "storage": STORAGE_LABELS,
        "layouts": LAYOUT_LABELS,
    }


# ------------------------------------------------------------------ 设置


ALLOWED_SETTINGS = {
    "download_root",
    "download_root_per_aweme",
    "proxy",
    "user_agent",
    "min_interval",
    "jitter",
    "workers",
    "max_concurrent_jobs",
    "auth_token",
}


@app.post("/api/settings")
def save_settings(payload: SettingsPayload):
    saved = []
    for key, value in payload.values.items():
        if key not in ALLOWED_SETTINGS:
            continue
        if key == "auth_token" and value.startswith("******"):
            continue
        settings.set(key, str(value))
        saved.append(key)
    if "download_root" in saved or "download_root_per_aweme" in saved:
        settings.roots()
    db.add_log(None, "info", f"设置已更新：{', '.join(saved) or '无变化'}")
    return {"ok": True, "saved": saved, "settings": settings.public()}


# ------------------------------------------------------------------ Cookie 池


@app.get("/api/cookies")
def list_cookies():
    return {
        "items": cookies.public_list(),
        "enabled": cookies.count(),
        "total": cookies.count(enabled_only=False),
    }


@app.post("/api/cookies")
def add_cookie(payload: CookiePayload):
    value = (payload.value or "").strip()
    if "=" not in value or len(value) < 20:
        raise HTTPException(400, "Cookie 格式不对，请粘贴整串 key=value; key=value 内容")
    label = (payload.label or "").strip() or f"账号 {cookies.count(enabled_only=False) + 1}"
    cookie_id = db.add_cookie(label, value)
    db.add_log(None, "info", f"新增 Cookie 账号：{label}")
    return {"ok": True, "id": cookie_id, "items": cookies.public_list()}


@app.patch("/api/cookies/{cookie_id}")
def update_cookie(cookie_id: int, payload: CookiePayload):
    row = db.get_cookie(cookie_id)
    if not row:
        raise HTTPException(404, "账号不存在")
    fields: Dict[str, object] = {}
    if payload.label:
        fields["label"] = payload.label.strip()
    if payload.value:
        if "=" not in payload.value:
            raise HTTPException(400, "Cookie 格式不对")
        fields["value"] = payload.value.strip()
        fields["status"] = "unknown"
    if payload.enabled is not None:
        fields["enabled"] = 1 if payload.enabled else 0
    db.update_cookie(cookie_id, **fields)
    return {"ok": True, "items": cookies.public_list()}


@app.delete("/api/cookies/{cookie_id}")
def delete_cookie(cookie_id: int):
    if not db.get_cookie(cookie_id):
        raise HTTPException(404, "账号不存在")
    db.delete_cookie(cookie_id)
    return {"ok": True, "items": cookies.public_list()}


@app.post("/api/cookies/{cookie_id}/test")
def test_cookie(cookie_id: int):
    row = db.get_cookie(cookie_id)
    if not row:
        raise HTTPException(404, "账号不存在")
    client = DouyinClient(
        cookie=row["value"],
        user_agent=settings.get("user_agent"),
        proxy=settings.get("proxy"),
        min_interval=0.6,
    )
    try:
        report = client.diagnose()
        cookies.mark(cookie_id, "ok" if report.get("ok") else "bad", report.get("message", ""))
        return {"ok": bool(report.get("ok")), "report": report, "items": cookies.public_list()}
    finally:
        client.close()


# ------------------------------------------------------------------ 解析


def _pick_cookie() -> Optional[dict]:
    return cookies.pick()


@app.post("/api/parse")
def parse_one(payload: ParsePayload):
    try:
        target = parse_target(payload.target)
    except ParseError as exc:
        raise HTTPException(400, str(exc)) from exc

    if target["kind"] != "user":
        return {
            "ok": False,
            "kind": target["kind"],
            "message": "这是单个作品链接。批量下载请粘贴博主主页链接（douyin.com/user/...）",
        }

    result = {"ok": True, "kind": "user", "sec_uid": target["sec_uid"], "nickname": ""}
    row = _pick_cookie()
    if not row:
        result["warning"] = "还没有配置 Cookie，无法预读博主资料"
        return result
    try:
        client = DouyinClient(
            cookie=row["value"],
            user_agent=settings.get("user_agent"),
            proxy=settings.get("proxy"),
            min_interval=0.5,
        )
        client.bootstrap()
        profile = client.get_user_profile(target["sec_uid"])
        result.update(
            {
                "nickname": profile.get("nickname"),
                "signature": profile.get("signature"),
                "follower_count": profile.get("follower_count"),
                "aweme_count": profile.get("aweme_count"),
            }
        )
        client.close()
    except (DouyinError, AuthError, RiskControlError) as exc:
        result["warning"] = f"已解析 sec_uid，但获取资料失败：{exc}"
    return result


# ------------------------------------------------------------------ 任务


@app.post("/api/jobs")
def create_job(payload: JobCreate):
    try:
        target = parse_target(payload.target)
    except ParseError as exc:
        raise HTTPException(400, str(exc)) from exc
    if target["kind"] != "user":
        raise HTTPException(400, "请粘贴博主主页链接，而不是单个作品链接")
    row = _pick_cookie()
    if not row:
        raise HTTPException(400, "请先在左侧「设置 → Cookie 账号池」添加至少一个 Cookie")

    source = payload.source if payload.source in consts.SOURCE_ENDPOINTS else consts.SOURCE_POST
    options = dict(DEFAULT_OPTIONS)
    options.update({k: v for k, v in (payload.options or {}).items() if k in DEFAULT_OPTIONS})

    nickname = ""
    try:
        client = DouyinClient(
            cookie=row["value"],
            user_agent=settings.get("user_agent"),
            proxy=settings.get("proxy"),
            min_interval=0.5,
        )
        client.bootstrap()
        profile = client.get_user_profile(target["sec_uid"])
        nickname = profile.get("nickname") or ""
        client.close()
    except (DouyinError, AuthError, RiskControlError) as exc:
        db.add_log(None, "warn", f"创建任务时获取博主信息失败：{exc}")

    title = f"{nickname or target['sec_uid'][:12]} · {consts.SOURCE_LABELS[source]}"
    job_id = db.create_job(title, target["sec_uid"], nickname, source, options)
    db.add_log(job_id, "info", f"任务已创建：{title}")
    runner.start(job_id)
    return {"ok": True, "job_id": job_id, "job": db.get_job(job_id)}


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: int):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    job["running"] = runner.is_running(job_id)
    job["aweme_count"] = db.count_awemes(job_id)
    return job


@app.get("/api/jobs/{job_id}/awemes")
def job_awemes(job_id: int, limit: int = 200, offset: int = 0):
    return {"items": db.list_awemes(job_id, min(limit, 500), offset)}


@app.post("/api/jobs/{job_id}/pause")
def pause_job(job_id: int):
    if not runner.pause(job_id):
        raise HTTPException(400, "任务未在运行")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: int):
    runner.resume(job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: int):
    runner.cancel(job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/rerun")
def rerun_job(job_id: int):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    if runner.is_running(job_id):
        raise HTTPException(400, "任务正在运行")
    db.update_job(job_id, status="pending", error="")
    runner.start(job_id)
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: int):
    if runner.is_running(job_id):
        runner.cancel(job_id)
    db.delete_job(job_id)
    return {"ok": True}


# ------------------------------------------------------------------ 打包 / 取回


def _job_items(job: dict) -> list[dict]:
    """统一成 [{name, url, kind}]：浏览器模式取直链，服务器模式取本地文件。"""
    layout = (job.get("options") or {}).get("dir_layout") or "collection"
    base = "per_aweme" if layout == "per_aweme" else "collection"
    root = settings.root_for(base).resolve()
    job_dir = Path(job["dir"]) if job.get("dir") else root
    items: list[dict] = []
    for row in db.list_aweme_media(job["id"]):
        if row["media"]:
            for media in row["media"]:
                items.append(
                    {
                        "name": media.get("name") or "media",
                        "url": "/api/fetch?u=" + quote(media.get("url", ""), safe=""),
                        "kind": media.get("kind", ""),
                    }
                )
            continue
        for key, rel in (row["files"] or {}).items():
            if key == "comments" or not rel:
                continue
            absolute = (job_dir / rel).resolve()
            try:
                api_path = os.path.relpath(absolute, root).replace(os.sep, "/")
            except ValueError:
                continue
            items.append(
                {
                    "name": rel.replace(os.sep, "/"),
                    "url": f"/api/file?base={base}&path=" + quote(api_path, safe=""),
                    "kind": key.split("_")[0],
                }
            )
    return items


@app.get("/api/jobs/{job_id}/manifest")
def job_manifest(job_id: int):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    items = _job_items(job)
    return {
        "ok": True,
        "job": {"id": job["id"], "title": job["title"], "nickname": job.get("nickname")},
        "count": len(items),
        "items": items,
    }


def _check_cdn(url: str) -> None:
    host = (urlparse(url).hostname or "").lower()
    if not host or not any(host == h or host.endswith("." + h) for h in CDN_HOSTS):
        raise HTTPException(400, "只允许代理抖音 CDN 地址")


@app.get("/api/fetch")
def fetch_media(u: str, name: str = ""):
    """服务端转发抖音 CDN：直链带 Referer 才能稳定下载。"""
    _check_cdn(u)
    client = httpx.Client(timeout=60.0, follow_redirects=True)
    try:
        resp = client.send(
            client.build_request(
                "GET",
                u,
                headers={
                    "User-Agent": consts.DEFAULT_UA,
                    "Referer": "https://www.douyin.com/",
                    "Accept": "*/*",
                },
            ),
            stream=True,
        )
    except httpx.HTTPError as exc:
        client.close()
        raise HTTPException(502, f"拉取失败：{exc}") from exc

    if resp.status_code != 200:
        resp.close()
        client.close()
        raise HTTPException(502, f"抖音 CDN 返回 {resp.status_code}")

    media_type = resp.headers.get("content-type", "application/octet-stream")
    headers = {}
    size = resp.headers.get("content-length")
    if size:
        headers["Content-Length"] = size
    if name:
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(name)}"

    def chunks() -> Iterator[bytes]:
        try:
            for chunk in resp.iter_bytes(64 * 1024):
                yield chunk
        finally:
            resp.close()
            client.close()

    return StreamingResponse(chunks(), media_type=media_type, headers=headers)


@app.get("/api/jobs/{job_id}/archive")
def job_archive(job_id: int):
    """把整个任务打包成一个 ZIP 流式下发（不占服务器临时空间）。"""
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    items = _job_items(job)
    if not items:
        raise HTTPException(400, "这个任务没有可打包的文件")

    layout = (job.get("options") or {}).get("dir_layout") or "collection"
    base = "per_aweme" if layout == "per_aweme" else "collection"
    root = settings.root_for(base).resolve()
    job_dir = Path(job["dir"]) if job.get("dir") else root
    safe_title = (job.get("nickname") or f"job-{job_id}").replace("/", "_")

    def local_chunks(path: Path) -> Iterator[bytes]:
        with path.open("rb") as fp:
            while True:
                block = fp.read(256 * 1024)
                if not block:
                    break
                yield block

    def remote_chunks(url: str) -> Iterator[bytes]:
        client = httpx.Client(timeout=90.0, follow_redirects=True)
        try:
            resp = client.send(
                client.build_request(
                    "GET",
                    url,
                    headers={
                        "User-Agent": consts.DEFAULT_UA,
                        "Referer": "https://www.douyin.com/",
                        "Accept": "*/*",
                    },
                ),
                stream=True,
            )
            try:
                if resp.status_code != 200:
                    return
                for chunk in resp.iter_bytes(64 * 1024):
                    yield chunk
            finally:
                resp.close()
        finally:
            client.close()

    def build():
        from urllib.parse import parse_qs, unquote

        for item in items:
            url = item["url"]
            arcname = f"{safe_title}/{item['name']}"
            if url.startswith("/api/file?"):
                query = parse_qs(url.split("?", 1)[1])
                rel = (query.get("path") or [""])[0]
                path = (root / unquote(rel)).resolve()
                if not path.is_file():
                    continue
                yield arcname, local_chunks(path)
            else:
                raw = url.split("u=", 1)[1]
                yield arcname, remote_chunks(unquote(raw))

    filename = quote(f"{safe_title}.zip")
    return StreamingResponse(
        stream_zip(build()),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


# ------------------------------------------------------------------ 文件


@app.get("/api/browse")
def browse(path: str = "", base: str = "collection"):
    root = settings.root_for(base).resolve()
    target = (root / path).resolve() if path else root
    if root not in target.parents and target != root:
        raise HTTPException(400, "路径越界")
    if not target.exists():
        raise HTTPException(404, "目录不存在")
    entries = []
    if target.is_dir():
        for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name)):
            try:
                stat = item.stat()
            except OSError:
                continue
            entries.append(
                {
                    "name": item.name,
                    "is_dir": item.is_dir(),
                    "size": 0 if item.is_dir() else stat.st_size,
                    "mtime": int(stat.st_mtime),
                }
            )
    return {
        "root": str(root),
        "path": str(target.relative_to(root)) if target != root else "",
        "entries": entries[:500],
    }


@app.get("/api/file")
def read_file(path: str, base: str = "collection"):
    """把服务器本地的成品文件取回浏览器（只读，限定在下载根目录内）。"""
    root = settings.root_for(base).resolve()
    target = (root / path).resolve()
    if root not in target.parents and target != root:
        raise HTTPException(400, "路径越界")
    if not target.is_file():
        raise HTTPException(404, "文件不存在")
    media_type, _ = mimetypes.guess_type(target.name)
    return FileResponse(
        str(target),
        filename=target.name,
        media_type=media_type or "application/octet-stream",
    )


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/login")
def login_page():
    return FileResponse(WEB_DIR / "login.html")
