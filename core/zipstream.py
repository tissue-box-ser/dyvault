"""流式 ZIP：边读边写，不落临时文件，适合把整个任务打包给浏览器下载。

使用 STORED（不压缩）+ 数据描述符，因此无需预先知道文件大小；
视频本身已压缩，再压一遍只会浪费 CPU。
"""

from __future__ import annotations

import struct
import time
import zlib
from typing import Iterable, Iterator, Tuple

FLAG_DATA_DESCRIPTOR = 0x0008
FLAG_UTF8 = 0x0800


def _dos_datetime() -> Tuple[int, int]:
    now = time.localtime()
    year = max(1980, now.tm_year)
    date = ((year - 1980) << 9) | (now.tm_mon << 5) | now.tm_mday
    clock = (now.tm_hour << 11) | (now.tm_min << 5) | (now.tm_sec // 2)
    return clock, date


def stream_zip(entries: Iterable[Tuple[str, Iterator[bytes]]]) -> Iterator[bytes]:
    """entries 为 (压缩包内路径, 字节块迭代器) 序列。"""
    offset = 0
    central: list[tuple[bytes, int, int, int]] = []
    clock, date = _dos_datetime()
    flags = FLAG_DATA_DESCRIPTOR | FLAG_UTF8

    for name, chunks in entries:
        raw = name.encode("utf-8")
        local_offset = offset
        header = struct.pack(
            "<IHHHHHIIIHH",
            0x04034B50, 20, flags, 0, clock, date, 0, 0, 0, len(raw), 0,
        ) + raw
        yield header
        offset += len(header)

        crc = 0
        size = 0
        for chunk in chunks:
            if not chunk:
                continue
            crc = zlib.crc32(chunk, crc)
            size += len(chunk)
            offset += len(chunk)
            yield chunk

        crc &= 0xFFFFFFFF
        descriptor = struct.pack("<IIII", 0x08074B50, crc, size, size)
        yield descriptor
        offset += len(descriptor)
        central.append((raw, crc, size, local_offset))

    cd_start = offset
    for raw, crc, size, local_offset in central:
        entry = struct.pack(
            "<IHHHHHHIIIHHHHHII",
            0x02014B50, 20, 20, flags, 0, clock, date, crc, size, size,
            len(raw), 0, 0, 0, 0, 0, local_offset,
        ) + raw
        yield entry
        offset += len(entry)

    cd_size = offset - cd_start
    eocd = struct.pack(
        "<IHHHHIIH", 0x06054B50, 0, 0, len(central), len(central), cd_size, cd_start, 0
    )
    yield eocd
