"""Formatting helpers for Telegram messages."""
from __future__ import annotations


def money(v: float | None, digits: int = 2) -> str:
    if v is None:
        return "н/д"
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e9:
        return f"{sign}${a / 1e9:.{digits}f} млрд"
    if a >= 1e6:
        return f"{sign}${a / 1e6:.{digits}f} млн"
    if a >= 1e3:
        return f"{sign}${a / 1e3:.{max(digits - 1, 0)}f} тис."
    return f"{sign}${a:.0f}"


def price(v: float | None, digits: int = 4) -> str:
    return "н/д" if v is None else f"${v:.{digits}f}"


def pct(v: float | None, digits: int = 1, signed: bool = False) -> str:
    if v is None:
        return "н/д"
    s = f"{v:+.{digits}f}%" if signed else f"{v:.{digits}f}%"
    return s


def num(v: float | None, digits: int = 0) -> str:
    if v is None:
        return "н/д"
    s = f"{v:,.{digits}f}".replace(",", " ")
    return s


def rate(v: float | None) -> str:
    return "н/д" if v is None else f"{v:.6f}"


def change_pct(now: float | None, before: float | None) -> float | None:
    if now is None or before in (None, 0):
        return None
    return (now / before - 1) * 100


def arrow(v: float | None) -> str:
    if v is None:
        return ""
    return "▲" if v > 0 else "▼" if v < 0 else "■"
