"""Dependency-free decoder for Upstox Market Data Feed V3 (protobuf wire format).

Field numbers follow data/MarketDataFeedV3.proto. Only the fields the bot uses are read; unknown fields are skipped,
so additions on Upstox's side cannot break decoding. Replaces protoc/grpcio-tools (heavy build step on Render free).
"""
import struct
from dataclasses import dataclass
from typing import Dict, Iterator, Tuple


@dataclass
class Tick:
    ltp: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    oi: float = 0.0
    volume: float = 0.0
    iv: float = 0.0
    delta: float = 0.0
    ts: float = 0.0


def _varint(b: bytes, i: int) -> Tuple[int, int]:
    r = s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        if not x & 0x80:
            return r, i
        s += 7
        if s > 70:
            raise ValueError("varint too long")


def _fields(b: bytes) -> Iterator[Tuple[int, int, object]]:
    i, n = 0, len(b)
    while i < n:
        key, i = _varint(b, i)
        num, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        elif wt == 2:
            ln, i = _varint(b, i)
            v, i = b[i:i + ln], i + ln
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise ValueError(f"unsupported wire type {wt}")
        yield num, wt, v


def _d(v) -> float:
    return struct.unpack("<d", v)[0] if isinstance(v, (bytes, bytearray)) and len(v) == 8 else 0.0


def _ltpc(b: bytes) -> float:
    for n, wt, v in _fields(b):
        if n == 1 and wt == 1:
            return _d(v)
    return 0.0


def _quote(b: bytes) -> Tuple[float, float]:
    bid = ask = 0.0
    for n, wt, v in _fields(b):
        if n == 2 and wt == 1:
            bid = _d(v)
        elif n == 4 and wt == 1:
            ask = _d(v)
    return bid, ask


def _delta(b: bytes) -> float:
    for n, wt, v in _fields(b):
        if n == 1 and wt == 1:
            return _d(v)
    return 0.0


def _market_ff(b: bytes) -> Tick:
    t = Tick()
    for n, wt, v in _fields(b):
        if n == 1:
            t.ltp = _ltpc(v)
        elif n == 2 and t.bid == 0 and t.ask == 0:          # first bidAskQuote only
            for n2, wt2, v2 in _fields(v):
                if n2 == 1:
                    t.bid, t.ask = _quote(v2)
                    break
        elif n == 3:
            t.delta = _delta(v)
        elif n == 6 and wt == 0:
            t.volume = float(v)
        elif n == 7 and wt == 1:
            t.oi = _d(v)
        elif n == 8 and wt == 1:
            t.iv = _d(v)
    return t


def _first_level(b: bytes) -> Tick:
    t = Tick()
    for n, wt, v in _fields(b):
        if n == 1:
            t.ltp = _ltpc(v)
        elif n == 2:
            t.bid, t.ask = _quote(v)
        elif n == 3:
            t.delta = _delta(v)
        elif n == 4 and wt == 0:
            t.volume = float(v)
        elif n == 5 and wt == 1:
            t.oi = _d(v)
        elif n == 6 and wt == 1:
            t.iv = _d(v)
    return t


def _feed(b: bytes) -> Tick:
    for n, wt, v in _fields(b):
        if n == 1:
            return Tick(ltp=_ltpc(v))
        if n == 2:                                   # FullFeed
            for n2, wt2, v2 in _fields(v):
                if n2 == 1:
                    return _market_ff(v2)
                if n2 == 2:
                    for n3, wt3, v3 in _fields(v2):
                        if n3 == 1:
                            return Tick(ltp=_ltpc(v3))
        if n == 3:
            return _first_level(v)
    return Tick()


def decode(raw: bytes) -> Dict[str, Tick]:
    """FeedResponse bytes -> {instrument_key: Tick}. Frames without feeds (market info, heartbeats) give {}."""
    out: Dict[str, Tick] = {}
    for n, wt, v in _fields(raw):
        if n != 2 or wt != 2:
            continue
        key, val = "", b""
        for n2, wt2, v2 in _fields(v):
            if n2 == 1:
                key = v2.decode("utf-8", "replace")
            elif n2 == 2:
                val = v2
        if key:
            t = _feed(val)
            if t.ltp > 0:
                out[key] = t
    return out
