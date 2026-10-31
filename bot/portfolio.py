"""What the user actually holds — read from config, so dropping or adding a project is a config edit.

* prices: own stablecoins whose source is configured (USDat — Curve pool, USDu — PancakeSwap, …);
* TVL: protocols listed in sources.defillama.slugs (a slug, a list of slugs to sum, or "chain:<Name>");
* points: weekly.points — projects whose points are logged with /log.
"""
from __future__ import annotations

ASSETS = {  # key: (display name, config section, config key that enables it)
    "usdat": ("USDat", "ethereum", "curve_usdat_pool"),
    "usdu": ("USDu", "bsc", "pancake_usdu_pool"),
    "iusd": ("iUSD", "ethereum", "curve_iusd_pool"),
    "trusd": ("trUSD", "ethereum", "curve_trusd_pool"),
}
POINTS = {  # project: (journal column, label, where to look)
    "saturn": ("saturn_points", "Saturn", "app.saturn.credit → Points"),
    "unitas": ("unitas_points", "Unitas", "app.unitas.so → Points"),
    "infinifi": ("infinifi_points", "InfiniFi", "app.infinifi.xyz → Points"),
    "tori": ("tori_points", "Tori (Cores)", "Cores — app.tori.finance"),
}
DEFAULT_NAMES = {"saturn": "Saturn", "unitas": "Unitas", "infinifi": "InfiniFi", "tori": "Tori",
                 "fluid": "Fluid", "mantle": "Mantle"}


def assets(cfg: dict) -> dict[str, str]:
    src = cfg.get("sources") or {}
    return {k: name for k, (name, sec, key) in ASSETS.items() if (src.get(sec) or {}).get(key)}


def protocols(cfg: dict) -> dict[str, str]:
    dl = (cfg.get("sources") or {}).get("defillama") or {}
    names = dl.get("names") or {}
    return {k: names.get(k, DEFAULT_NAMES.get(k, k.capitalize())) for k in (dl.get("slugs") or {})}


def points(cfg: dict) -> list[str]:
    """Journal columns of the projects whose points are logged now."""
    keys = (cfg.get("weekly") or {}).get("points") or list(POINTS)
    return [POINTS[k][0] for k in keys if k in POINTS]


def log_example(cfg: dict) -> str:
    demo = {"saturn": "123456", "unitas": "7890", "infinifi": "4567", "tori": "2500"}
    keys = (cfg.get("weekly") or {}).get("points") or list(POINTS)
    return "/log " + " ".join(f"{k} {demo[k]}" for k in keys if k in demo)


def points_where(cfg: dict) -> str:
    keys = (cfg.get("weekly") or {}).get("points") or list(POINTS)
    return ", ".join(POINTS[k][2] for k in keys if k in POINTS)
