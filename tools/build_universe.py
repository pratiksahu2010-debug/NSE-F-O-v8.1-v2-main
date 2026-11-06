"""Rebuild the top-50 most-traded F&O stock list now (ranks by 20-day average turnover)."""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import cfg  # noqa: E402
from data.instruments import Master, build_underlyings, download_master  # noqa: E402
from data.universe import rank_stocks  # noqa: E402
from data.upstox_client import UpstoxProvider  # noqa: E402

if __name__ == "__main__":
    m = Master(download_master())
    unds = build_underlyings(m, date.today())
    print("Indices:", [u.name for u in unds if u.kind == "INDEX"])
    names = rank_stocks(UpstoxProvider(), [u for u in unds if u.kind == "STOCK"], cfg.TOP_STOCKS, date.today())
    print(f"Top {len(names)} stocks:", names)
