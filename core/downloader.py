"""文件下载器：断点续传、多地址回退、失败重试。"""

from __future__ import annotations

import os
import random
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

import httpx

MIN_VALID_SIZE = 1024
CHUNK = 128 * 1024


class DownloadResult(dict):
    @property
    def ok(self) -> bool:
        return self.get("status") in ("ok", "skip")


def download_file(
    urls: List[str] | str,
    dest: Path,
    referer: str = "https://www.douyin.com/",
    user_agent: str = "",
    proxy: str = "",
    retries: int = 3,
    timeout: float = 90.0,
    overwrite: bool = False,
    progress: Optional[Callable[[int, int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> DownloadResult:
    """按候选地址顺序下载，成功后原子替换为目标文件。

    已存在且体积正常的文件直接跳过，因此重复运行任务天然增量。
    """
    if isinstance(urls, str):
        urls = [urls]
    urls = [u for u in urls if u]
    if not urls:
        return DownloadResult(status="failed", error="没有可用的下载地址")

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists() and not overwrite:
        size = dest.stat().st_size
        if size >= MIN_VALID_SIZE:
            return DownloadResult(status="skip", size=size, path=str(dest))

    part = dest.with_name(dest.name + ".part")
    headers = {
        "User-Agent": user_agent
        or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Referer": referer,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Range": "",
    }

    errors: List[str] = []
    for url in urls:
        for attempt in range(retries):
            if should_stop and should_stop():
                return DownloadResult(status="failed", error="已取消")
            existing = part.stat().st_size if part.exists() else 0
            req_headers = dict(headers)
            if existing:
                req_headers["Range"] = f"bytes={existing}-"
            else:
                req_headers.pop("Range", None)
            try:
                with httpx.Client(
                    timeout=httpx.Timeout(timeout, connect=20.0),
                    follow_redirects=True,
                    proxy=proxy or None,
                ) as client:
                    with client.stream("GET", url, headers=req_headers) as resp:
                        if resp.status_code == 416:
                            # 服务端认为已下载完整
                            break
                        if resp.status_code >= 400:
                            raise httpx.HTTPStatusError(
                                f"HTTP {resp.status_code}", request=resp.request, response=resp
                            )
                        mode = "ab" if existing and resp.status_code == 206 else "wb"
                        if mode == "wb" and existing:
                            existing = 0
                        total = existing + int(resp.headers.get("content-length") or 0)
                        written = existing
                        with part.open(mode) as fp:
                            for chunk in resp.iter_bytes(CHUNK):
                                if should_stop and should_stop():
                                    return DownloadResult(status="failed", error="已取消")
                                if not chunk:
                                    continue
                                fp.write(chunk)
                                written += len(chunk)
                                if progress:
                                    progress(written, total)

                size = part.stat().st_size if part.exists() else 0
                if size < MIN_VALID_SIZE:
                    raise IOError(f"文件过小({size}B)，可能是错误页")
                os.replace(part, dest)
                return DownloadResult(status="ok", size=size, path=str(dest), url=url)
            except (httpx.HTTPError, OSError) as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
                time.sleep(min(1.5 ** attempt, 6) + random.uniform(0, 0.5))

    if part.exists():
        try:
            part.unlink()
        except OSError:
            pass
    return DownloadResult(
        status="failed",
        error="; ".join(dict.fromkeys(errors))[:300] or "下载失败",
    )


def parallel_download(
    tasks: List[Dict],
    workers: int = 4,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Dict[str, DownloadResult]:
    """批量下载，tasks 每项为 download_file 的 kwargs（必须含 key 字段做结果索引）。"""
    from concurrent.futures import ThreadPoolExecutor

    results: Dict[str, DownloadResult] = {}
    if not tasks:
        return results

    def run(item: Dict):
        key = item.pop("key")
        return key, download_file(should_stop=should_stop, **item)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for key, result in pool.map(run, tasks):
            results[key] = result
    return results
