"""Legacy OI context helpers. The live scanner does not use these as signal-approval gates."""
from typing import List, Optional, Tuple

from config import cfg
from indicators.core import iv_rank  # noqa: F401  (re-exported for convenience)
from models import Candle, ChainSnapshot, Vote


def _clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def fut_buildup(fut: List[Candle], window: int = 6) -> Vote:
    """Price vs OI over the last `window` 5m candles of the current-month future."""
    if len(fut) < window + 1:
        return Vote("FUT_OI", 0, "")
    a, b = fut[-(window + 1)], fut[-1]
    if a.oi <= 0 or b.oi <= 0 or a.c <= 0:
        return Vote("FUT_OI", 0, "")
    dp = (b.c - a.c) / a.c * 100
    do = (b.oi - a.oi) / a.oi * 100
    if abs(dp) < 0.15 or abs(do) < 0.4:
        return Vote("FUT_OI", 0, "")
    mag = _clamp(0.6 + min(abs(do), 5) / 12 + min(abs(dp), 1.0) / 5, 0, 1)
    if dp > 0 and do > 0:
        return Vote("FUT_OI", mag, f"Fut price {dp:+.2f}% OI {do:+.2f}% (fresh buying, Call bias)")
    if dp < 0 and do > 0:
        return Vote("FUT_OI", -mag, f"Fut price {dp:+.2f}% OI {do:+.2f}% (fresh selling, Put bias)")
    if dp > 0 and do < 0:
        return Vote("FUT_OI", 0.4 * mag, f"Fut price {dp:+.2f}% OI {do:+.2f}% (short covering, weak Call bias)")
    return Vote("FUT_OI", -0.4 * mag, f"Fut price {dp:+.2f}% OI {do:+.2f}% (long unwinding, weak Put bias)")


def pcr_shift(cur: ChainSnapshot, earlier_pcr: Optional[float]) -> Vote:
    if not earlier_pcr or earlier_pcr <= 0 or cur.pcr <= 0:
        return Vote("PCR", 0, "")
    d = cur.pcr - earlier_pcr
    if abs(d) < cfg.PCR_MIN_CHANGE:                 # whole-chain PCR jitters; only a real shift counts
        return Vote("PCR", 0, "")
    s = _clamp(d / 0.30)
    # rising PCR with PCR>0.9 => put writing/support (Call bias); falling with PCR<1.1 => Put bias
    if d > 0 and cur.pcr >= 0.9:
        return Vote("PCR", abs(s) * 0.8, f"PCR rising {earlier_pcr:.2f}->{cur.pcr:.2f} (put writing)")
    if d < 0 and cur.pcr <= 1.1:
        return Vote("PCR", -abs(s) * 0.8, f"PCR falling {earlier_pcr:.2f}->{cur.pcr:.2f} (call writing)")
    return Vote("PCR", 0, "")


def oi_wall_break(cur: ChainSnapshot, earlier_call_wall: Optional[float], earlier_put_wall: Optional[float]) -> Vote:
    spot = cur.spot
    if earlier_call_wall and spot > earlier_call_wall * 1.0005:
        return Vote("WALL", 0.85, f"Spot above call-OI wall {earlier_call_wall:.0f}")
    if earlier_put_wall and spot < earlier_put_wall * 0.9995:
        return Vote("WALL", -0.85, f"Spot below put-OI wall {earlier_put_wall:.0f}")
    return Vote("WALL", 0, "")


def wall_room(cur: ChainSnapshot) -> Vote:
    res, sup = cur.wall("CE"), cur.wall("PE")
    if not res or not sup:
        return Vote("ROOM", 0, "")
    up, dn = res[0] - cur.spot, cur.spot - sup[0]
    if up + dn <= 0:
        return Vote("ROOM", 0, "")
    s = _clamp((up - dn) / (up + dn)) * 0.5
    side = "Call" if s > 0 else "Put"
    return Vote("ROOM", s, f"Room to wall: support {sup[0]:.0f} / resistance {res[0]:.0f} ({side} side has more room)")



def chain_votes(cur: ChainSnapshot, earlier: Optional[dict]) -> List[Vote]:
    """Optional informational OI/chain context for reports and legacy callers; not used to approve live alerts."""
    e = earlier or {}
    votes = [pcr_shift(cur, e.get("pcr")), oi_wall_break(cur, e.get("call_wall"), e.get("put_wall"))]
    return [v for v in votes if v.score != 0]


def chain_score(votes: List[Vote]) -> float:
    if not votes:
        return 0.0
    return _clamp(sum(v.score for v in votes) / max(1.5, len(votes)))
