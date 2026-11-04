"""QQ Music QRC lyric decryption and conversion to enhanced LRC.

QQ Music serves its verbatim (word-timed) lyrics as QRC: a ``<Lyric_1
LyricType="1" LyricContent="…"/>`` XML payload whose content is encrypted with a
custom Triple-DES (the DES variant QQ Music uses, not standard 3DES) and then
zlib-compressed.  This module ports the decryption from LDDC / QQMusicDecoder
and turns the decrypted verbatim timeline into enhanced LRC (``[mm:ss.xx]
<mm:ss.xx>word``), the format the user keeps as ``歌曲.es.lrc``.

The DES implementation is a faithful port of the reference C code at
https://github.com/jixunmoe/qmc-decode and WXRIW/QQMusicDecoder; it is kept
self-contained so the tool has no dependency on those projects at runtime.
"""
from __future__ import annotations

import re
import zlib

# 24-byte key = three 8-byte DES keys.
QRC_KEY = b"!@#)(*$%123ZXC!@!@#)(NHL"

_QRC_CONTENT_RE = re.compile(
    r'<Lyric_1 LyricType="1" LyricContent="(?P<content>.*?)"/>', re.DOTALL
)


def qrc_decrypt(encrypted: str) -> str:
    """Decrypt a QRC lyric string returned by the QQ Music cloud API.

    ``encrypted`` is the hex-encoded ciphertext; the result is the raw XML
    string (``<Lyric_1 …/>``).  Raises :class:`ValueError` on bad input.
    """
    if not encrypted or not encrypted.strip():
        raise ValueError("没有可解密的歌词数据")
    try:
        data = bytearray.fromhex(encrypted)
    except ValueError as exc:
        raise ValueError(f"QRC 不是合法的十六进制密文: {exc}") from exc
    schedule = _tripledes_key_setup(QRC_KEY, _DECRYPT)
    out = bytearray()
    # Block cipher: operate on 8-byte blocks; the final partial block is fed
    # as-is (the reference decrypts len//8 full blocks implicitly via slicing).
    for offset in range(0, len(data), 8):
        block = data[offset : offset + 8]
        if len(block) == 8:
            out += _tripledes_crypt(block, schedule)
        else:
            out += block
    try:
        return zlib.decompress(out).decode("utf-8")
    except (zlib.error, UnicodeDecodeError) as exc:
        raise ValueError(f"QRC 解密/解压失败: {exc}") from exc


def qrc_lyric_text(decrypted: str) -> str:
    """Extract the plain verbatim timeline from a decrypted QRC XML string.

    Returns the ``LyricContent`` inner text (QRC line format).  Falls back to
    LRC when the payload is already plain LRC rather than QRC.
    """
    match = _QRC_CONTENT_RE.search(decrypted or "")
    if match and match.group("content"):
        return match.group("content")
    return (decrypted or "").strip()


def qrc_to_enhanced(qrc_text: str) -> str:
    """Convert QRC verbatim lines into enhanced LRC (``.es.lrc`` style).

    Each QRC line ``[start,dur](ws,wd)词(ws,wd)语`` becomes::

        [mm:ss.xx]<mm:ss.xx>词<mm:ss.xx>语

    Metadata tags (``[ti:…]`` etc.) are preserved verbatim.  The conversion
    itself lives in :mod:`lrc_patch.lyric_fmt` — the one place that knows how
    a native timeline maps to every target format — so this stays a thin
    compatibility wrapper instead of a second parser that can drift.
    """
    from .lyric_fmt import lines_to_a2, parse_qrc

    return lines_to_a2(parse_qrc(qrc_text))


def qrc_decrypt_to_enhanced(encrypted: str) -> str:
    """Decrypt a QRC payload and return enhanced LRC text in one step."""
    return qrc_to_enhanced(qrc_lyric_text(qrc_decrypt(encrypted)))


# ---------------------------------------------------------------------------
# Custom Triple-DES (QQ Music variant).  The S-boxes and permutation tables
# below are the standard DES tables, but the ``f`` function's expansion is the
# QQ Music layout from jixunmoe/qmc-decode; do not swap in a generic DES lib.
# ---------------------------------------------------------------------------

_ENCRYPT = 1
_DECRYPT = 0

_SBOX = (
    # sbox1
    (14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7,
     0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8,
     4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0,
     15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13),
    # sbox2
    (15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10,
     3, 13, 4, 7, 15, 2, 8, 15, 12, 0, 1, 10, 6, 9, 11, 5,
     0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15,
     13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9),
    # sbox3
    (10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8,
     13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1,
     13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7,
     1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12),
    # sbox4
    (7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15,
     13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9,
     10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4,
     3, 15, 0, 6, 10, 10, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14),
    # sbox5
    (2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9,
     14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6,
     4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14,
     11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3),
    # sbox6
    (12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11,
     10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8,
     9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6,
     4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13),
    # sbox7
    (4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1,
     13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6,
     1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2,
     6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12),
    # sbox8
    (13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7,
     1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2,
     7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8,
     2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11),
)


def _bitnum(data: bytes, b: int, c: int) -> int:
    return ((data[(b // 32) * 4 + 3 - (b % 32) // 8] >> (7 - b % 8)) & 1) << c


def _bitnum_intr(value: int, b: int, c: int) -> int:
    return ((value >> (31 - b)) & 1) << c


def _bitnum_intl(value: int, b: int, c: int) -> int:
    return ((value << b) & 0x80000000) >> c


def _sbox_bit(value: int) -> int:
    return (value & 32) | ((value & 31) >> 1) | ((value & 1) << 4)


def _initial_permutation(data: bytearray) -> tuple[int, int]:
    return (
        _bitnum(data, 57, 31) | _bitnum(data, 49, 30) | _bitnum(data, 41, 29) | _bitnum(data, 33, 28)
        | _bitnum(data, 25, 27) | _bitnum(data, 17, 26) | _bitnum(data, 9, 25) | _bitnum(data, 1, 24)
        | _bitnum(data, 59, 23) | _bitnum(data, 51, 22) | _bitnum(data, 43, 21) | _bitnum(data, 35, 20)
        | _bitnum(data, 27, 19) | _bitnum(data, 19, 18) | _bitnum(data, 11, 17) | _bitnum(data, 3, 16)
        | _bitnum(data, 61, 15) | _bitnum(data, 53, 14) | _bitnum(data, 45, 13) | _bitnum(data, 37, 12)
        | _bitnum(data, 29, 11) | _bitnum(data, 21, 10) | _bitnum(data, 13, 9) | _bitnum(data, 5, 8)
        | _bitnum(data, 63, 7) | _bitnum(data, 55, 6) | _bitnum(data, 47, 5) | _bitnum(data, 39, 4)
        | _bitnum(data, 31, 3) | _bitnum(data, 23, 2) | _bitnum(data, 15, 1) | _bitnum(data, 7, 0),
        _bitnum(data, 56, 31) | _bitnum(data, 48, 30) | _bitnum(data, 40, 29) | _bitnum(data, 32, 28)
        | _bitnum(data, 24, 27) | _bitnum(data, 16, 26) | _bitnum(data, 8, 25) | _bitnum(data, 0, 24)
        | _bitnum(data, 58, 23) | _bitnum(data, 50, 22) | _bitnum(data, 42, 21) | _bitnum(data, 34, 20)
        | _bitnum(data, 26, 19) | _bitnum(data, 18, 18) | _bitnum(data, 10, 17) | _bitnum(data, 2, 16)
        | _bitnum(data, 60, 15) | _bitnum(data, 52, 14) | _bitnum(data, 44, 13) | _bitnum(data, 36, 12)
        | _bitnum(data, 28, 11) | _bitnum(data, 20, 10) | _bitnum(data, 12, 9) | _bitnum(data, 4, 8)
        | _bitnum(data, 62, 7) | _bitnum(data, 54, 6) | _bitnum(data, 46, 5) | _bitnum(data, 38, 4)
        | _bitnum(data, 30, 3) | _bitnum(data, 22, 2) | _bitnum(data, 14, 1) | _bitnum(data, 6, 0),
    )


def _inverse_permutation(s0: int, s1: int) -> bytearray:
    data = bytearray(8)
    data[3] = (_bitnum_intr(s1, 7, 7) | _bitnum_intr(s0, 7, 6) | _bitnum_intr(s1, 15, 5)
               | _bitnum_intr(s0, 15, 4) | _bitnum_intr(s1, 23, 3) | _bitnum_intr(s0, 23, 2)
               | _bitnum_intr(s1, 31, 1) | _bitnum_intr(s0, 31, 0))
    data[2] = (_bitnum_intr(s1, 6, 7) | _bitnum_intr(s0, 6, 6) | _bitnum_intr(s1, 14, 5)
               | _bitnum_intr(s0, 14, 4) | _bitnum_intr(s1, 22, 3) | _bitnum_intr(s0, 22, 2)
               | _bitnum_intr(s1, 30, 1) | _bitnum_intr(s0, 30, 0))
    data[1] = (_bitnum_intr(s1, 5, 7) | _bitnum_intr(s0, 5, 6) | _bitnum_intr(s1, 13, 5)
               | _bitnum_intr(s0, 13, 4) | _bitnum_intr(s1, 21, 3) | _bitnum_intr(s0, 21, 2)
               | _bitnum_intr(s1, 29, 1) | _bitnum_intr(s0, 29, 0))
    data[0] = (_bitnum_intr(s1, 4, 7) | _bitnum_intr(s0, 4, 6) | _bitnum_intr(s1, 12, 5)
               | _bitnum_intr(s0, 12, 4) | _bitnum_intr(s1, 20, 3) | _bitnum_intr(s0, 20, 2)
               | _bitnum_intr(s1, 28, 1) | _bitnum_intr(s0, 28, 0))
    data[7] = (_bitnum_intr(s1, 3, 7) | _bitnum_intr(s0, 3, 6) | _bitnum_intr(s1, 11, 5)
               | _bitnum_intr(s0, 11, 4) | _bitnum_intr(s1, 19, 3) | _bitnum_intr(s0, 19, 2)
               | _bitnum_intr(s1, 27, 1) | _bitnum_intr(s0, 27, 0))
    data[6] = (_bitnum_intr(s1, 2, 7) | _bitnum_intr(s0, 2, 6) | _bitnum_intr(s1, 10, 5)
               | _bitnum_intr(s0, 10, 4) | _bitnum_intr(s1, 18, 3) | _bitnum_intr(s0, 18, 2)
               | _bitnum_intr(s1, 26, 1) | _bitnum_intr(s0, 26, 0))
    data[5] = (_bitnum_intr(s1, 1, 7) | _bitnum_intr(s0, 1, 6) | _bitnum_intr(s1, 9, 5)
               | _bitnum_intr(s0, 9, 4) | _bitnum_intr(s1, 17, 3) | _bitnum_intr(s0, 17, 2)
               | _bitnum_intr(s1, 25, 1) | _bitnum_intr(s0, 25, 0))
    data[4] = (_bitnum_intr(s1, 0, 7) | _bitnum_intr(s0, 0, 6) | _bitnum_intr(s1, 8, 5)
               | _bitnum_intr(s0, 8, 4) | _bitnum_intr(s1, 16, 3) | _bitnum_intr(s0, 16, 2)
               | _bitnum_intr(s1, 24, 1) | _bitnum_intr(s0, 24, 0))
    return data


def _f(state: int, key: list) -> int:
    t1 = (_bitnum_intl(state, 31, 0) | ((state & 0xF0000000) >> 1) | _bitnum_intl(state, 4, 5)
          | _bitnum_intl(state, 3, 6) | ((state & 0x0F000000) >> 3) | _bitnum_intl(state, 8, 11)
          | _bitnum_intl(state, 7, 12) | ((state & 0x00F00000) >> 5) | _bitnum_intl(state, 12, 17)
          | _bitnum_intl(state, 11, 18) | ((state & 0x000F0000) >> 7) | _bitnum_intl(state, 16, 23))
    t2 = (_bitnum_intl(state, 15, 0) | ((state & 0x0000F000) << 15) | _bitnum_intl(state, 20, 5)
          | _bitnum_intl(state, 19, 6) | ((state & 0x00000F00) << 13) | _bitnum_intl(state, 24, 11)
          | _bitnum_intl(state, 23, 12) | ((state & 0x000000F0) << 11) | _bitnum_intl(state, 28, 17)
          | _bitnum_intl(state, 27, 18) | ((state & 0x0000000F) << 9) | _bitnum_intl(state, 0, 23))
    lrgstate = (
        (t1 >> 24) & 0x000000FF, (t1 >> 16) & 0x000000FF, (t1 >> 8) & 0x000000FF,
        (t2 >> 24) & 0x000000FF, (t2 >> 16) & 0x000000FF, (t2 >> 8) & 0x000000FF,
    )
    lrgstate = [lrgstate[i] ^ key[i] for i in range(6)]
    state = (_SBOX[0][_sbox_bit(lrgstate[0] >> 2)] << 28
             | _SBOX[1][_sbox_bit(((lrgstate[0] & 0x03) << 4) | (lrgstate[1] >> 4))] << 24
             | _SBOX[2][_sbox_bit(((lrgstate[1] & 0x0F) << 2) | (lrgstate[2] >> 6))] << 20
             | _SBOX[3][_sbox_bit(lrgstate[2] & 0x3F)] << 16
             | _SBOX[4][_sbox_bit(lrgstate[3] >> 2)] << 12
             | _SBOX[5][_sbox_bit(((lrgstate[3] & 0x03) << 4) | (lrgstate[4] >> 4))] << 8
             | _SBOX[6][_sbox_bit(((lrgstate[4] & 0x0F) << 2) | (lrgstate[5] >> 6))] << 4
             | _SBOX[7][_sbox_bit(lrgstate[5] & 0x3F)])
    return (_bitnum_intl(state, 15, 0) | _bitnum_intl(state, 6, 1) | _bitnum_intl(state, 19, 2)
            | _bitnum_intl(state, 20, 3) | _bitnum_intl(state, 28, 4) | _bitnum_intl(state, 11, 5)
            | _bitnum_intl(state, 27, 6) | _bitnum_intl(state, 16, 7) | _bitnum_intl(state, 0, 8)
            | _bitnum_intl(state, 14, 9) | _bitnum_intl(state, 22, 10) | _bitnum_intl(state, 25, 11)
            | _bitnum_intl(state, 4, 12) | _bitnum_intl(state, 17, 13) | _bitnum_intl(state, 30, 14)
            | _bitnum_intl(state, 9, 15) | _bitnum_intl(state, 1, 16) | _bitnum_intl(state, 7, 17)
            | _bitnum_intl(state, 23, 18) | _bitnum_intl(state, 13, 19) | _bitnum_intl(state, 31, 20)
            | _bitnum_intl(state, 26, 21) | _bitnum_intl(state, 2, 22) | _bitnum_intl(state, 8, 23)
            | _bitnum_intl(state, 18, 24) | _bitnum_intl(state, 12, 25) | _bitnum_intl(state, 29, 26)
            | _bitnum_intl(state, 5, 27) | _bitnum_intl(state, 21, 28) | _bitnum_intl(state, 10, 29)
            | _bitnum_intl(state, 3, 30) | _bitnum_intl(state, 24, 31))


def _crypt(data: bytearray, key: list) -> bytearray:
    s0, s1 = _initial_permutation(data)
    for idx in range(15):
        previous_s1 = s1
        s1 = _f(s1, key[idx]) ^ s0
        s0 = previous_s1
    s0 = _f(s1, key[15]) ^ s0
    return _inverse_permutation(s0, s1)


_KEY_RND_SHIFT = (1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1)
_KEY_PERM_C = (56, 48, 40, 32, 24, 16, 8, 0, 57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18, 10, 2, 59, 51, 43, 35)
_KEY_PERM_D = (62, 54, 46, 38, 30, 22, 14, 6, 61, 53, 45, 37, 29, 21, 13, 5, 60, 52, 44, 36, 28, 20, 12, 4, 27, 19, 11, 3)
_KEY_COMPRESSION = (13, 16, 10, 23, 0, 4, 2, 27, 14, 5, 20, 9, 22, 18, 11, 3, 25, 7, 15, 6, 26, 19, 12, 1, 40, 51, 30, 36,
                    46, 54, 29, 39, 50, 44, 32, 47, 43, 48, 38, 55, 33, 52, 45, 41, 49, 35, 28, 31)


def _key_schedule(key: bytes, mode: int) -> list[list[int]]:
    schedule = [[0] * 6 for _ in range(16)]
    c = sum(_bitnum(key, _KEY_PERM_C[i], 31 - i) for i in range(28))
    d = sum(_bitnum(key, _KEY_PERM_D[i], 31 - i) for i in range(28))
    for i in range(16):
        c = ((c << _KEY_RND_SHIFT[i]) | (c >> (28 - _KEY_RND_SHIFT[i]))) & 0xFFFFFFF0
        d = ((d << _KEY_RND_SHIFT[i]) | (d >> (28 - _KEY_RND_SHIFT[i]))) & 0xFFFFFFF0
        togen = 15 - i if mode == _DECRYPT else i
        for j in range(6):
            schedule[togen][j] = 0
        for j in range(24):
            schedule[togen][j // 8] |= _bitnum_intr(c, _KEY_COMPRESSION[j], 7 - (j % 8))
        for j in range(24, 48):
            schedule[togen][j // 8] |= _bitnum_intr(d, _KEY_COMPRESSION[j] - 27, 7 - (j % 8))
    return schedule


def _tripledes_key_setup(key: bytes, mode: int) -> list[list[list[int]]]:
    if mode == _ENCRYPT:
        return [_key_schedule(key[0:8], _ENCRYPT), _key_schedule(key[8:16], _DECRYPT), _key_schedule(key[16:24], _ENCRYPT)]
    return [_key_schedule(key[16:24], _DECRYPT), _key_schedule(key[8:16], _ENCRYPT), _key_schedule(key[0:8], _DECRYPT)]


def _tripledes_crypt(data: bytearray, key: list) -> bytearray:
    for i in range(3):
        data = _crypt(data, key[i])
    return data
