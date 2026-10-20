"""File-backed state: state.json, history.csv (metric lookback), journal.csv."""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .metrics import METRIC_KEYS

JOURNAL_COLUMNS = [
    "date", "saturn_points", "unitas_points", "infinifi_points", "tori_points",
    "tvl_saturn", "tvl_unitas", "tvl_infinifi", "tvl_tori",
    "price_usdat", "price_usdu", "price_iusd", "price_trusd",
    "liusd13w_rate", "infinifi_liquid_reserve",
    "strusd_rate", "pendle_pt_price", "pendle_implied_apy", "pendle_liquidity",
    "note",
]
POINTS_COLUMNS = ["saturn_points", "unitas_points", "infinifi_points", "tori_points"]
MONEY_COLUMNS = ("infinifi_liquid_reserve", "pendle_liquidity")
RATE_COLUMNS = ("liusd13w_rate", "strusd_rate", "pendle_pt_price")   # тут важливий 7-й знак


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.10g}"
    return str(v)


# --------------------------------------------------------------------------
# state.json
# --------------------------------------------------------------------------
DEFAULT_STATE = {
    "version": 1,
    "started_at": None,          # first run — warm-up starts here
    "telegram_offset": 0,
    "alerts": {},                # alert_id -> {active, since, last_sent, level}
    "counters": {},              # e.g. consecutive price breaches
    "conditions": {},            # e.g. reserve_low_since, queue_nonempty_since
    "source_failures": {},       # source -> consecutive failure count
    "liusd_rate_watermark": None,
    "last_metrics": {},
    "last_run": None,
    "maturity_sent": {},         # pendle market -> ["14", "7", "2", "expired"]
    "pendle_trusd_markets": [],  # active trUSD markets from the Pendle API (refreshed daily)
    "pendle_known_markets": [],  # markets already announced as «new»
    "pendle_markets_checked_at": None,
    "pendle_markets_seeded": False,
    "weekly": {"digest_sent_date": None, "reminder_sent_date": None, "pending_points_date": None},
}


def load_state(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return json.loads(json.dumps(DEFAULT_STATE))
    with open(p, "r", encoding="utf-8") as fh:
        st = json.load(fh)
    for k, v in DEFAULT_STATE.items():
        st.setdefault(k, json.loads(json.dumps(v)))
    return st


def save_state(path: str, state: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# history.csv — one row per monitor run, pruned to N days
# --------------------------------------------------------------------------
HISTORY_COLUMNS = ["ts"] + METRIC_KEYS


def load_history(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    rows = []
    with open(p, "r", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            ts = parse_iso(r.get("ts"))
            if not ts:
                continue
            row = {"ts": ts}
            for k in METRIC_KEYS:
                v = r.get(k, "")
                row[k] = float(v) if v not in ("", None) else None
            rows.append(row)
    rows.sort(key=lambda r: r["ts"])
    return rows


def append_history(path: str, metrics: dict, now: datetime, keep_days: int) -> list[dict]:
    rows = load_history(path)
    rows.append({"ts": now, **{k: metrics.get(k) for k in METRIC_KEYS}})
    cutoff = now - timedelta(days=keep_days)
    rows = [r for r in rows if r["ts"] >= cutoff]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(HISTORY_COLUMNS)
        for r in rows:
            w.writerow([iso(r["ts"])] + [_fmt(r.get(k)) for k in METRIC_KEYS])
    return rows


def value_ago(history: list[dict], key: str, now: datetime, hours: float, tolerance_hours: float) -> float | None:
    """Metric value from the record closest to `hours` ago (within tolerance)."""
    target = now - timedelta(hours=hours)
    best, best_dt = None, None
    for r in history:
        if r.get(key) is None:
            continue
        d = abs((r["ts"] - target).total_seconds()) / 3600
        if d <= tolerance_hours and (best_dt is None or d < best_dt):
            best, best_dt = r[key], d
    return best


def history_span_hours(history: list[dict], now: datetime) -> float:
    return (now - history[0]["ts"]).total_seconds() / 3600 if history else 0.0


# --------------------------------------------------------------------------
# journal.csv — the user's weekly log
# --------------------------------------------------------------------------
def load_journal(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    with open(p, "r", encoding="utf-8", newline="") as fh:
        rows = [dict(r) for r in csv.DictReader(fh)]
    for r in rows:
        r.pop(None, None)                 # хвости рядків без заголовка
        for c in JOURNAL_COLUMNS:
            r.setdefault(c, "")
    return rows


def journal_fieldnames(rows: list[dict]) -> list[str]:
    """Current columns + any old ones still present in the file (e.g. curve_usdat_share from
    before v1.1) — dropped metrics stop being filled, but past values are never deleted."""
    extra: list[str] = []
    for r in rows:
        for k in r:
            if k and k not in JOURNAL_COLUMNS and k not in extra and any(x.get(k) for x in rows):
                extra.append(k)
    return JOURNAL_COLUMNS + extra


def save_journal(path: str, rows: list[dict]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fields = journal_fieldnames(rows)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in fields})


def journal_row_from_metrics(date_str: str, metrics: dict) -> dict:
    row = {c: "" for c in JOURNAL_COLUMNS}
    row["date"] = date_str
    for c in JOURNAL_COLUMNS:
        if c in metrics and metrics[c] is not None:
            v = metrics[c]
            row[c] = (f"{v:.2f}" if c.startswith("tvl_") or c in MONEY_COLUMNS
                      else f"{v:.8g}" if c in RATE_COLUMNS else f"{v:.6g}")
    return row


def last_row_with_points(rows: list[dict], before_index: int | None = None) -> dict | None:
    rng = rows[:before_index] if before_index is not None else rows
    for r in reversed(rng):
        if any(r.get(c) not in ("", None) for c in POINTS_COLUMNS):
            return r
    return None


def to_float(v) -> float | None:
    try:
        return float(v) if v not in ("", None) else None
    except (TypeError, ValueError):
        return None
