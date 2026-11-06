import html
import logging
import time
from datetime import datetime

import requests
from utils.market_hours import IST

from config import cfg
from models import Signal

log = logging.getLogger("telegram")


class Telegram:
    """send() succeeds only after Telegram confirms every configured chat and message chunk."""
    def __init__(self, token: str = None, chat_ids=None):
        self.token = token or cfg.TELEGRAM_BOT_TOKEN
        self.chat_ids = chat_ids or cfg.TELEGRAM_CHAT_IDS
        self.s = requests.Session()
        self.last_ok_ts = None
        self.last_error = None
        if not (self.token and self.chat_ids):
            self.last_error = "telegram credentials/chat IDs missing"
            log.warning("Telegram notifications disabled: configure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS in Render")

    def send(self, text: str) -> bool:
        if not (self.token and self.chat_ids):
            log.info("[telegram disabled] %s", text[:200].replace("\\n", " | "))
            self.last_error = "telegram credentials/chat IDs missing"
            return False
        return self._deliver(text)

    def flush(self, timeout: float = 15.0):
        return None  # send() is acknowledged synchronously

    def send_to_chat(self, text: str, chat_id: str) -> bool:
        """Deliver one outbox item to one chat so retries do not duplicate other chats."""
        if not self.token:
            self.last_error = "Telegram bot token missing"
            return False
        return self._deliver(text, chat_ids=[str(chat_id)])

    def _deliver(self, text: str, chat_ids=None) -> bool:
        ok = True
        for chat in (self.chat_ids if chat_ids is None else chat_ids):
            for chunk in [text[i:i + 3900] for i in range(0, len(text), 3900)]:
                sent = False
                for attempt in range(3):
                    try:
                        r = self.s.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                                        json={"chat_id": chat, "text": chunk, "parse_mode": "HTML",
                                              "disable_web_page_preview": True}, timeout=8)
                        if r.status_code == 429:
                            retry_after = min(5, r.json().get("parameters", {}).get("retry_after", 2))
                            self.last_error = f"Telegram HTTP 429 rate limit; retry_after={retry_after}s"
                            if attempt == 2:
                                log.warning("Telegram rate limit retries exhausted for chat %s", chat)
                                break
                            time.sleep(retry_after)
                            continue
                        payload = r.json() if r.content else {}
                        sent = bool(r.ok and payload.get("ok") is True)
                        if not sent:
                            self.last_error = f"Telegram HTTP {r.status_code}: {str(payload)[:180]}"
                            log.warning("%s", self.last_error)
                        break
                    except (requests.RequestException, ValueError) as e:
                        self.last_error = str(e)[:180]
                        log.warning("telegram send failed: %s", e)
                        time.sleep(1 + attempt)
                ok = ok and sent
        if ok:
            self.last_ok_ts = time.time()
            self.last_error = None
        return ok


def _p(x, d=2):
    return f"{x:,.{d}f}"


STRATEGY_LABEL = {"TREND": "📈 TREND", "REVERSAL": "🔄 REVERSAL (counter-trend)", "ORB": "🚀 OPENING-RANGE BREAKOUT",
                  "VWAP": "🎯 VWAP RECLAIM / REJECTION", "SQUEEZE": "💥 SQUEEZE BREAKOUT", "MOMENTUM_EXPLOSION": "🚀 MOMENTUM EXPLOSION — CONFIRMED",
                  "SIMPLE_TREND": "📈 TREND (EMA 9/21 + VWAP + RSI + ADX)",
                  "BIG_MOMENTUM": "📈 BIG MOMENTUM", "VOLUME_BREAKOUT": "📊 VOLUME-CONFIRMED BREAKOUT",
                  "BREAKOUT_RETEST": "🔁 BREAKOUT RETEST", "TREND_PULLBACK": "📈 TREND PULLBACK",
                  "VWAP_RECLAIM_REJECTION": "🎯 VWAP RECLAIM / REJECTION",
                  "MOMENTUM_REVERSAL": "🔄 MOMENTUM REVERSAL",
                  "SQUEEZE_BREAKOUT": "💥 SQUEEZE BREAKOUT",
                  "FAILED_BREAKOUT_SWEEP": "🧹 FAILED BREAKOUT / LIQUIDITY SWEEP"}


def format_signal(s: Signal) -> str:
    call = s.side == "CALL"
    icon, word = ("🟢", "BUY CALL (CE)") if call else ("🔴", "BUY PUT (PE)")
    ivr = f"{s.iv_rank:.0f}" if s.iv_rank is not None else "n/a"
    arrow_t = "↑" if call else "↓"
    # One canonical timestamp for the signal/alert. Do not show separate candle,
    # scan, queued, or delivery times that can differ and confuse the trader.
    try:
        signal_dt = datetime.fromisoformat(s.ts)
        if signal_dt.tzinfo is None:
            signal_dt = signal_dt.replace(tzinfo=IST)
        signal_time = signal_dt.astimezone(IST).strftime("%d-%b-%Y %H:%M:%S IST")
    except (TypeError, ValueError):
        signal_time = "Unavailable"
    lines = [
        f"<b>Signal / alert time:</b> {signal_time}",
        f"{icon} <b>{word} | {html.escape(s.underlying)}</b>  {'📝 PAPER' if cfg.PAPER_MODE else ''}",
        f"<b>Type:</b> {STRATEGY_LABEL.get(s.strategy, '📈 TREND')} | "
        + ("⚠️ EARLY INTRABAR — provisional; revalidate before acting"
           if str(getattr(s, "tier", "CONFIRMED")).upper() == "EARLY"
           else "✅ CLOSED-BAR CONFIRMED — score and mandatory gates passed"),
        f"<b>Contract:</b> <code>{html.escape(s.contract)}</code>",
        f"<b>Expiry:</b> {s.expiry} ({s.dte}d) | <b>Strike:</b> {s.strike:g} {s.opt_type}",
        f"<b>Lot size:</b> {s.lot_size} | <b>Cost/lot:</b> ₹{_p(s.cost_lot, 0)}",
        f"<b>Entry zone:</b> ₹{_p(s.entry_lo)} – ₹{_p(s.entry_hi)} (LTP ₹{_p(s.ltp)})",
        f"<b>Stop-loss:</b> ₹{_p(s.sl)} premium | underlying {_p(s.und_sl)}",
        f"<b>Target 1:</b> ₹{_p(s.t1)} | <b>Target 2:</b> ₹{_p(s.t2)} | <b>Target 3:</b> ₹{_p(getattr(s, 't3', 0))}  (underlying {arrow_t} {_p(s.und_t1)} / {_p(s.und_t2)} / {_p(getattr(s, 'und_t3', 0))})",
        f"<b>Trail:</b> after T1 move SL to cost (₹{_p(s.ltp)}); once premium is halfway from T1 to T2, trail SL to T1",
        f"<b>Spot:</b> {_p(s.spot)} | <b>Fut:</b> {_p(s.fut_price)}",
        f"<b>Valid till:</b> {s.valid_till} | <b>Hard exit:</b> {s.exit_by}",
        f"<b>Greeks/IV:</b> Δ {s.delta:.2f} θ {s.theta:.1f} IV {s.iv:.1f} (rank {ivr})",
        f"<b>Liquidity:</b> spread {s.spread_pct:.1f}% | OI {s.oi:,.0f} | Vol {s.volume:,.0f}",
        f"<b>Levels:</b> support {_p(s.support, 0)} | resistance {_p(s.resistance, 0)} | PCR {s.pcr:.2f}",
        f"<b>Risk:</b> loss/lot at SL ≈ ₹{_p(s.max_loss_lot, 0)} | R:R(T2) 1:{s.rr:.1f}",
        (f"<b>Rule:</b> EMA9/21 + VWAP + RSI + ADX ≥ {cfg.SIMPLE_ADX_MIN:g} | feed: {s.provider}"
         if s.strategy == "SIMPLE_TREND" else
         f"<b>Unified score:</b> {s.confidence:.1f}/10 | grade {('A+' if s.confidence >= 8.5 else 'A' if s.confidence >= 7.5 else 'B')} "
         f"(minimum {cfg.V2_MIN_SCORE / 10:.1f}/10) | feed: {s.provider}"
         if s.strategy in {"BIG_MOMENTUM", "ORB", "VOLUME_BREAKOUT", "BREAKOUT_RETEST",
                           "TREND_PULLBACK", "VWAP_RECLAIM_REJECTION", "MOMENTUM_REVERSAL",
                           "SQUEEZE_BREAKOUT", "FAILED_BREAKOUT_SWEEP"} else
         f"<b>Score:</b> {s.confidence:.1f}/10 (minimum {cfg.ALERT_SCORE_MIN:.1f}/10) | technical quality checks enabled | feed: {s.provider}"),
        "<b>Why:</b>",
    ]
    lines += [f"• {html.escape(r)}" for r in s.reasons[:6]]
    indicator_summary = next((r for r in s.reasons if all(k in r for k in ("EMA9", "EMA21", "VWAP", "RSI", "ADX"))), None)
    if indicator_summary:
        lines.append(f"• Indicator snapshot: {html.escape(indicator_summary)}")
    lines.append("<i>Telegram alert only; no order is placed. Confirm quantity, spread and stop yourself before any trade.</i>")
    return "\n".join(lines)


def format_update(s: Signal, kind: str, premium: float, note: str = "") -> str:
    icons = {"T1": "🎯", "T2": "🏆", "T3": "🚀", "SL": "🛑", "TRAIL": "🔧", "TIME": "⏰", "UND_SL": "🛑", "EXIT": "🚪"}
    head = f"{icons.get(kind, 'ℹ️')} <b>{kind}</b> | {html.escape(s.underlying)} {s.opt_type} {s.strike:g}"
    body = [head, f"Premium now ₹{_p(premium)} (entry ₹{_p(s.ltp)}) | SL now ₹{_p(s.cur_sl)}"]
    if s.status == "CLOSED":
        body.append(f"Closed: {s.result} | P&amp;L per lot: ₹{_p(s.pnl_lot, 0)}")
    if note:
        body.append(html.escape(note))
    return "\n".join(body)
