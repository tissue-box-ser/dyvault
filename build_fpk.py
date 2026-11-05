#!/usr/bin/env python
"""打包飞牛 fnOS 应用（.fpk）。

用法：
    python build_fpk.py                 # 默认 1.0.0 / x86_64
    python build_fpk.py -v 1.0.1 -a arm64

产物：dist/douyin-dl_<版本>_<架构>.fpk
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import tarfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / ".fpk_build"
DIST = ROOT / "dist"
APPNAME = "douyin-dl"

INCLUDE = ["core", "app", "cli.py", "requirements.txt"]
EXCLUDE_DIRS = {"__pycache__", ".venv", "data", "downloads", "dist", ".git", ".tmp", "tests", ".fpk_build"}


# ------------------------------------------------------------------ 图标

def _round_rect(x: float, y: float, size: float, radius: float) -> bool:
    dx = max(abs(x - size / 2) - (size / 2 - radius), 0.0)
    dy = max(abs(y - size / 2) - (size / 2 - radius), 0.0)
    return dx * dx + dy * dy <= radius * radius


def _in_triangle(x: float, y: float, size: float) -> bool:
    ax, ay = size * 0.38, size * 0.28
    bx, by = size * 0.38, size * 0.72
    cx, cy = size * 0.72, size * 0.50
    d1 = (x - bx) * (ay - by) - (ax - bx) * (y - by)
    d2 = (x - cx) * (by - cy) - (bx - cx) * (y - cy)
    d3 = (x - ax) * (cy - ay) - (cx - ax) * (y - ay)
    has_neg = d1 < 0 or d2 < 0 or d3 < 0
    has_pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (has_neg and has_pos)


def make_icon(path: Path, size: int) -> None:
    raw = bytearray()
    radius = size * 0.23
    for y in range(size):
        raw.append(0)  # filter: none
        for x in range(size):
            if not _round_rect(x + 0.5, y + 0.5, size, radius):
                raw += b"\x00\x00\x00\x00"
                continue
            t = y / max(1, size - 1)
            r = int(47 + (122 - 47) * t)
            g = int(111 + (162 - 111) * t)
            b = int(237 + (255 - 237) * t)
            if _in_triangle(x + 0.5, y + 0.5, size):
                r, g, b = 255, 255, 255
            raw += bytes((r, g, b, 255))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


# ------------------------------------------------------------------ 打包

def build_app_tgz(dest: Path) -> None:
    with tarfile.open(dest, "w:gz") as tar:
        for name in INCLUDE:
            src = ROOT / name
            if not src.exists():
                raise SystemExit(f"缺少必要文件: {src}")
            if src.is_file():
                tar.add(src, arcname=name)
                continue
            for path in sorted(src.rglob("*")):
                if any(part in EXCLUDE_DIRS for part in path.parts):
                    continue
                if path.is_dir():
                    continue
                tar.add(path, arcname=str(path.relative_to(ROOT)).replace("\\", "/"))


def build_fpk(version: str, arch: str, os_min: str, author: str) -> Path:
    BUILD.mkdir(exist_ok=True)
    DIST.mkdir(exist_ok=True)
    for item in BUILD.iterdir():
        if item.is_file():
            item.unlink()

    make_icon(BUILD / "ICON.PNG", 64)
    make_icon(BUILD / "ICON_256.PNG", 256)

    app_tgz = BUILD / "app.tgz"
    build_app_tgz(app_tgz)
    checksum = hashlib.sha256(app_tgz.read_bytes()).hexdigest()

    manifest = {
        "appname": APPNAME,
        "version": version,
        "display_name": "DyVault",
        "desc": "按博主主页批量下载抖音视频与图集，可同时采集评论、点赞数、收藏数，提供网页管理界面。",
        "changelog": "首个版本：博主作品/点赞/收藏批量抓取，评论采集，断点续传，Web 管理界面。",
        "author": author,
        "platform": arch,
        "source": "https://github.com/tissue-box-ser/dyvault",
        "checksum": checksum,
        "maintainer": author,
        "distributor": author,
        "os_min_version": os_min,
        "service_port": "8080",
        "install_type": "bin",
        "ctl_script": "cmd/main",
        "disable_authorization": True,
    }
    (BUILD / "manifest").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=4), encoding="utf-8"
    )

    cmd_dir = BUILD / "cmd"
    cmd_dir.mkdir(exist_ok=True)
    main_script = (ROOT / "fpk" / "cmd" / "main").read_text(encoding="utf-8")
    target = cmd_dir / "main"
    target.write_text(main_script, encoding="utf-8", newline="\n")

    out = DIST / f"{APPNAME}_{version}_{arch}.fpk"
    with tarfile.open(out, "w:gz") as tar:
        for name in ("manifest", "ICON.PNG", "ICON_256.PNG", "app.tgz"):
            tar.add(BUILD / name, arcname=name)
        info = tar.gettarinfo(str(target), arcname="cmd/main")
        info.mode = 0o755
        with target.open("rb") as fp:
            tar.addfile(info, fp)

    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="构建飞牛 fpk 安装包")
    parser.add_argument("-v", "--version", default="1.0.0")
    parser.add_argument("-a", "--arch", default="x86_64", choices=["x86_64", "arm64"])
    parser.add_argument("--os-min", default="0.9.0", help="最低 fnOS 版本")
    parser.add_argument("--author", default="tissue-box-ser")
    args = parser.parse_args()

    out = build_fpk(args.version, args.arch, args.os_min, args.author)
    data = out.read_bytes()
    print(f"产物: {out}")
    print(f"大小: {len(data) / 1024:.1f} KB")
    print(f"sha256: {hashlib.sha256(data).hexdigest()}")
    print(f"md5:    {hashlib.md5(data).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
