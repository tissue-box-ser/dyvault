"""多账号 Cookie 池：增删改查 + 请求级轮询。

轮询策略：内存游标按「启用中」的账号顺序循环，每个请求换一个账号的前提是
调用方通过 provider() 取得的取用函数；写库只在切换账号时发生，避免频繁落盘。
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional

from .client import parse_cookie

STATUS_LABEL = {
    "ok": "正常",
    "bad": "已失效",
    "risk": "触发风控",
    "unknown": "未验证",
}


def mask_cookie(value: str) -> str:
    """只回显尾部，避免整串 Cookie 出现在页面上。"""
    value = value or ""
    if len(value) <= 24:
        return "••••••"
    names = [part.split("=", 1)[0].strip() for part in value.split(";") if "=" in part]
    tail = value[-6:]
    return f"{len(names)} 个字段 · …{tail}"


class CookiePool:
    def __init__(self, db, settings):
        self.db = db
        self.settings = settings
        self._lock = threading.Lock()
        self._cursor = 0
        self._touched: Dict[int, float] = {}
        self.migrate()

    # -------------------------------------------------------------- 兼容迁移

    def migrate(self) -> None:
        """老版本把 Cookie 存在 settings 里，升级后自动导入为一个账号（只做一次）。"""
        if self.settings.get("cookie_migrated") == "1":
            return
        legacy = self.settings.get("cookie")
        if legacy and not self.db.list_cookies():
            self.db.add_cookie("默认账号", legacy)
        self.settings.set("cookie_migrated", "1")

    # -------------------------------------------------------------- 查询

    def enabled(self) -> List[dict]:
        return self.db.list_cookies(enabled_only=True)

    def count(self, enabled_only: bool = True) -> int:
        return len(self.db.list_cookies(enabled_only=enabled_only))

    def public_list(self) -> List[dict]:
        items = []
        for row in self.db.list_cookies():
            value = row.pop("value", "")
            row["mask"] = mask_cookie(value)
            row["status_label"] = STATUS_LABEL.get(row.get("status") or "unknown", "未验证")
            row["has_login"] = bool(
                parse_cookie(value).get("sessionid")
                or parse_cookie(value).get("sessionid_ss")
                or parse_cookie(value).get("sid_tt")
            )
            items.append(row)
        return items

    def get_value(self, cookie_id: int) -> str:
        row = self.db.get_cookie(cookie_id)
        return (row or {}).get("value", "")

    # -------------------------------------------------------------- 取用

    def pick(self, exclude: Optional[set] = None) -> Optional[dict]:
        """按最久未使用挑一个账号，保证多任务之间自然轮换。"""
        exclude = exclude or set()
        rows = [r for r in self.enabled() if r["id"] not in exclude]
        if not rows:
            return None
        rows.sort(key=lambda r: (int(r.get("last_used") or 0), int(r["id"])))
        return rows[0]

    def provider(self, exclude: Optional[set] = None, log: Optional[Callable[[str], None]] = None):
        """返回给 DouyinClient 的取用函数：每次调用换下一个账号。"""
        exclude = exclude or set()

        def take() -> str:
            rows = [r for r in self.enabled() if r["id"] not in exclude]
            if not rows:
                return ""
            with self._lock:
                row = rows[self._cursor % len(rows)]
                self._cursor += 1
            now = time.time()
            if now - self._touched.get(row["id"], 0) > 60:
                self.db.touch_cookie(row["id"])
                self._touched[row["id"]] = now
            if log and len(rows) > 1:
                log(f"轮询 Cookie：{row['label']}")
            return row["value"]

        return take

    # -------------------------------------------------------------- 状态

    def mark(self, cookie_id: int, status: str, note: str = "") -> None:
        self.db.update_cookie(cookie_id, status=status, note=note[:200])
