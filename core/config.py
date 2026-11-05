"""运行配置：默认值 + 数据库设置 + 环境变量覆盖。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Dict

DEFAULTS: Dict[str, str] = {
    "cookie": "",
    "download_root": "",
    "download_root_per_aweme": "",
    "proxy": "",
    "user_agent": "",
    "min_interval": "1.2",
    "jitter": "0.8",
    "workers": "4",
    "max_concurrent_jobs": "1",
    "auth_token": "",
}

ENV_MAP = {
    "DOUYIN_COOKIE": "cookie",
    "DOUYIN_DOWNLOAD_ROOT": "download_root",
    "DOUYIN_DOWNLOAD_ROOT_PER_AWEME": "download_root_per_aweme",
    "DOUYIN_PROXY": "proxy",
    "DOUYIN_UA": "user_agent",
    "DOUYIN_INTERVAL": "min_interval",
    "DOUYIN_JITTER": "jitter",
    "DOUYIN_WORKERS": "workers",
    "DOUYIN_MAX_JOBS": "max_concurrent_jobs",
    "DOUYIN_TOKEN": "auth_token",
}

SECRET_KEYS = {"cookie", "auth_token"}


class Settings:
    """优先级：环境变量 > 数据库 > 默认值。"""

    def __init__(self, db, data_dir: str | Path):
        self.db = db
        self.data_dir = Path(data_dir)
        self._env = {key: os.environ[env] for env, key in ENV_MAP.items() if os.environ.get(env)}

    def all(self) -> Dict[str, str]:
        values = dict(DEFAULTS)
        values.update(self.db.all_settings())
        values.update(self._env)
        return values

    def get(self, key: str, default: str = "") -> str:
        if key in self._env:
            return self._env[key]
        return self.db.get_setting(key, DEFAULTS.get(key, default))

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(float(self.get(key, str(default))))
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.get(key, str(default)))
        except (TypeError, ValueError):
            return default

    def set(self, key: str, value: str) -> None:
        self.db.set_setting(key, value)

    def _resolve(self, raw: str, fallback: Path) -> Path:
        raw = (raw or "").strip()
        path = Path(raw).expanduser() if raw else fallback
        if not path.is_absolute():
            path = self.data_dir / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    def download_root(self) -> Path:
        """合集模式的下载根目录（服务器本机绝对路径）。

        未配置时落在数据目录同级的 downloads/（Docker 下即挂载卷内的 downloads），
        配置相对路径时按数据目录解析，保证始终解析成服务器本地真实路径。
        """
        return self._resolve(self.get("download_root"), self.data_dir.parent / "downloads")

    def download_root_per_aweme(self) -> Path:
        """单作品目录模式的下载根目录，未单独配置时与合集根并列（downloads_per_aweme）。"""
        return self._resolve(
            self.get("download_root_per_aweme"),
            self.data_dir.parent / "downloads_per_aweme",
        )

    def root_for(self, layout: str = "collection") -> Path:
        return self.download_root_per_aweme() if layout == "per_aweme" else self.download_root()

    def roots(self) -> Dict[str, str]:
        return {
            "collection": str(self.download_root()),
            "per_aweme": str(self.download_root_per_aweme()),
        }

    def disk_info(self, layout: str = "collection") -> Dict[str, object]:
        """下载目录所在磁盘的容量信息。"""
        path = self.root_for(layout)
        try:
            usage = shutil.disk_usage(str(path))
            total, used, free = usage.total, usage.used, usage.free
        except OSError:
            total = used = free = 0
        percent = round(used / total * 100, 1) if total else 0.0
        return {
            "path": str(path),
            "total": total,
            "used": used,
            "free": free,
            "percent": percent,
            "writable": os.access(str(path), os.W_OK),
        }

    def public(self) -> Dict[str, str]:
        """给前端展示用，敏感字段只回显是否已配置。"""
        data = self.all()
        for key in SECRET_KEYS:
            value = data.get(key, "")
            data[key] = f"******{value[-6:]}" if value else ""
        data["cookie_set"] = "1" if self.get("cookie") else "0"
        data["_download_root_resolved"] = str(self.download_root())
        data["_download_root_per_aweme_resolved"] = str(self.download_root_per_aweme())
        return data
