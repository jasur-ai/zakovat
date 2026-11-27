"""The invisible state codec: game state that nobody can see or tap open.

Serverless has no memory, so a running game (or a whole tournament) travels
inside the message it belongs to. It must not be readable — a spoiler can be
tapped open and the answer is on screen — so it is written as a run of
zero-width characters that Telegram keeps verbatim but no client renders.

Two densities live side by side:

* **v2** (``ZWNJ``/``ZWJ``, one bit per character, eight characters per byte).
  Proven in production; the ordinary game keeps using it.
* **v3** (four zero-width characters, two bits per character, four characters
  per byte, body compressed with zlib). A tournament carries rosters, selections
  and finals — with v2 it would not fit inside Telegram's 4096-character limit,
  with v3 it needs roughly a third of the space.

Both start with a sentinel byte and end with a checksum, so text that happens to
contain zero-width characters of its own is never mistaken for state, and a run
that was mangled in transit is dropped instead of half-read.
"""
from __future__ import annotations

import zlib

ZW0 = "\u200c"      # zero-width non-joiner   — v2 bit 0, v3 bits 00
ZW1 = "\u200d"      # zero-width joiner       — v2 bit 1, v3 bits 01
ZWA = "\u200b"      # zero-width space        — v3 bits 10
ZWB = "\u2060"      # word joiner             — v3 bits 11

INVISIBLE = (ZW0, ZW1, ZWA, ZWB)
LEGACY_INVISIBLE = (ZW0, ZW1)

V3_BITS = {ZW0: 0, ZW1: 1, ZWA: 2, ZWB: 3}
V3_SYMBOLS = (ZW0, ZW1, ZWA, ZWB)

SENTINEL_V2 = 0xA5
SENTINEL_V3 = 0x5A
MIN_RUN_V2 = 24          # sentinel + at least one byte + checksum, eight chars each
MIN_RUN_V3 = 16          # four characters per byte


# --------------------------------------------------------------------- encode
def encode_legacy(body: str) -> str:
    """v2: one bit per character, uncompressed. The ordinary game's format."""
    data = str(body or "").encode("utf-8")
    payload = bytes([SENTINEL_V2]) + data + bytes([sum(data) & 0xFF])
    return "".join(ZW1 if bit == "1" else ZW0 for byte in payload for bit in f"{byte:08b}")


def encode_dense(body: str) -> str:
    """v3: two bits per character over the zlib-compressed body."""
    data = zlib.compress(str(body or "").encode("utf-8"), 9)
    checksum = zlib.crc32(data) & 0xFFFF
    payload = bytes([SENTINEL_V3]) + data + bytes([checksum >> 8, checksum & 0xFF])
    return "".join(V3_SYMBOLS[(byte >> shift) & 3]
                   for byte in payload for shift in (6, 4, 2, 0))


def encode(body: str, dense: bool | None = None, threshold: int = 300) -> str:
    """Pick the density: v3 when the body is big, v2 when it is short.

    ``dense`` forces one or the other. Short bodies stay in v2 because that is
    the format the running games already carry, and a state that predates this
    change must keep decoding after a deploy.
    """
    if dense is None:
        dense = len(str(body or "").encode("utf-8")) >= threshold
    return encode_dense(body) if dense else encode_legacy(body)


# --------------------------------------------------------------------- decode
def _decode_v2(run: list[str]) -> str | None:
    if len(run) < MIN_RUN_V2 or len(run) % 8:
        return None
    payload = bytearray(int("".join("1" if char == ZW1 else "0" for char in run[i:i + 8]), 2)
                        for i in range(0, len(run), 8))
    if payload[0] != SENTINEL_V2:
        return None
    data, checksum = bytes(payload[1:-1]), payload[-1]
    if not data or (sum(data) & 0xFF) != checksum:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _decode_v3(run: list[str]) -> str | None:
    if len(run) < MIN_RUN_V3 or len(run) % 4:
        return None
    payload = bytearray()
    for start in range(0, len(run), 4):
        value = 0
        for char in run[start:start + 4]:
            bits = V3_BITS.get(char)
            if bits is None:
                return None
            value = (value << 2) | bits
        payload.append(value)
    if payload[0] != SENTINEL_V3 or len(payload) < 4:
        return None
    data = bytes(payload[1:-2])
    checksum = (payload[-2] << 8) | payload[-1]
    if not data or (zlib.crc32(data) & 0xFFFF) != checksum:
        return None
    try:
        return zlib.decompress(data).decode("utf-8")
    except (zlib.error, UnicodeDecodeError):
        return None


def decode_run(run: list[str]) -> str | None:
    """Read one zero-width run, trying both densities."""
    if any(char in (ZWA, ZWB) for char in run):
        return _decode_v3(run)
    return _decode_v2(run) or _decode_v3(run)


def decode(text: str) -> str | None:
    """Read the state back out of a message, wherever it sits.

    A run only counts when its sentinel and checksum match, so ordinary text
    that contains zero-width characters is ignored. The longest valid run wins.
    """
    found: str | None = None
    longest = 0
    run: list[str] = []
    for char in str(text or "") + " ":            # the tail flushes the last run
        if char in INVISIBLE:
            run.append(char)
            continue
        if run:
            body = decode_run(run)
            if body is not None and len(run) > longest:
                found, longest = body, len(run)
            run = []
    return found


def density(text: str) -> str:
    """Which format a message carries — used by the diagnostics page."""
    seen = [char for char in str(text or "") if char in INVISIBLE]
    if any(char in (ZWA, ZWB) for char in seen):
        return "v3"
    return "v2" if seen else ""


def utf16_length(text: str) -> int:
    """Telegram counts UTF-16 code units, not Python characters."""
    return len(str(text or "").encode("utf-16-le")) // 2
