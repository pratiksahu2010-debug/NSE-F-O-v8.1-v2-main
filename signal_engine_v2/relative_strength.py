"""Relative-strength helpers. Inputs must be timestamp-aligned returns."""
from __future__ import annotations


def relative_strength(underlying_return_pct, index_return_pct):
    if underlying_return_pct is None or index_return_pct is None:
        return None
    return round(float(underlying_return_pct) - float(index_return_pct), 4)


def sector_relative_strength(underlying_return_pct, sector_return_pct):
    if underlying_return_pct is None or sector_return_pct is None:
        return None
    return round(float(underlying_return_pct) - float(sector_return_pct), 4)
