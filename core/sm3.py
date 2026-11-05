"""纯 Python SM3 实现（GB/T 32905-2016），零第三方依赖。

抖音 a_bogus 签名内部使用 SM3 摘要，这里自行实现以避免引入 gmssl。
"""

MASK = 0xFFFFFFFF

IV = (
    0x7380166F,
    0x4914B2B9,
    0x172442D7,
    0xDA8A0600,
    0xA96F30BC,
    0x163138AA,
    0xE38DEE4D,
    0xB0FB0E4E,
)


def _rotl(x: int, n: int) -> int:
    n &= 31
    return ((x << n) | (x >> (32 - n))) & MASK


def _p0(x: int) -> int:
    return x ^ _rotl(x, 9) ^ _rotl(x, 17)


def _p1(x: int) -> int:
    return x ^ _rotl(x, 15) ^ _rotl(x, 23)


def sm3_digest(data: bytes) -> bytes:
    bit_len = len(data) * 8
    padded = bytearray(data)
    padded.append(0x80)
    while len(padded) % 64 != 56:
        padded.append(0x00)
    padded += bit_len.to_bytes(8, "big")

    v = list(IV)
    for offset in range(0, len(padded), 64):
        block = padded[offset : offset + 64]
        w = [int.from_bytes(block[i * 4 : i * 4 + 4], "big") for i in range(16)]
        for j in range(16, 68):
            w.append(
                _p1(w[j - 16] ^ w[j - 9] ^ _rotl(w[j - 3], 15))
                ^ _rotl(w[j - 13], 7)
                ^ w[j - 6]
            )
        w1 = [w[j] ^ w[j + 4] for j in range(64)]

        a, b, c, d, e, f, g, h = v
        for j in range(64):
            if j < 16:
                ff = a ^ b ^ c
                gg = e ^ f ^ g
                t = 0x79CC4519
            else:
                ff = (a & b) | (a & c) | (b & c)
                gg = (e & f) | ((~e & MASK) & g)
                t = 0x7A879D8A
            ss1 = _rotl((_rotl(a, 12) + e + _rotl(t, j)) & MASK, 7)
            ss2 = ss1 ^ _rotl(a, 12)
            tt1 = (ff + d + ss2 + w1[j]) & MASK
            tt2 = (gg + h + ss1 + w[j]) & MASK
            d = c
            c = _rotl(b, 9)
            b = a
            a = tt1
            h = g
            g = _rotl(f, 19)
            f = e
            e = _p0(tt2)

        v = [
            v[0] ^ a,
            v[1] ^ b,
            v[2] ^ c,
            v[3] ^ d,
            v[4] ^ e,
            v[5] ^ f,
            v[6] ^ g,
            v[7] ^ h,
        ]

    return b"".join(x.to_bytes(4, "big") for x in v)


def sm3_hash(data: bytes) -> str:
    """返回 64 位小写十六进制摘要。"""
    return sm3_digest(data).hex()


def sm3_hash_list(values) -> str:
    """兼容 gmssl 的 sm3_hash(list[int]) 调用方式。"""
    return sm3_hash(bytes(bytearray(v & 0xFF for v in values)))


if __name__ == "__main__":
    vectors = [
        (b"abc", "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0"),
        (
            b"abcd" * 16,
            "debe9ff92275b8a138604889c18e5a4d6fdb70e5387e5765293dcba39c0c5732",
        ),
        (b"", "1ab21d8355cfa17f8e61194831e81a8f22bec8c728fefb747ed035eb5082aa2b"),
    ]
    ok = True
    for msg, expect in vectors:
        got = sm3_hash(msg)
        flag = "OK " if got == expect else "FAIL"
        if got != expect:
            ok = False
        print(f"{flag} len={len(msg):3d} {got}")
    print("ALL PASS" if ok else "MISMATCH")
