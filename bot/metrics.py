"""Collect all metrics into one flat dict. A failing source sets its metrics to
None and records the error — one broken API must never stop the others."""
from __future__ import annotations

import requests

from . import sources
from .evm import Chain

# Flat metric keys stored in history.csv (order matters for the CSV header).
METRIC_KEYS = [
    "tvl_saturn", "tvl_unitas", "tvl_infinifi",
    "price_usdat", "price_usdu", "price_iusd",
    "liusd13w_rate",
    "infinifi_tvl_onchain", "infinifi_liquid_reserve", "infinifi_liquid_reserve_pct",
    "infinifi_queue_length", "infinifi_queue_iusd",
    "iusd_redeem_rate", "infinifi_unaccrued_yield",
    "curve_usdat_share", "curve_pool_liquidity", "curve_usdc_balance", "curve_usdat_balance",
    "usdu_pool_liquidity", "curve_iusd_liquidity",
]


def collect(cfg: dict) -> tuple[dict, dict]:
    """Returns (metrics, errors). metrics[k] is float|int|None."""
    src = cfg["sources"]
    m: dict = {k: None for k in METRIC_KEYS}
    errors: dict[str, str] = {}
    session = requests.Session()

    # --- DefiLlama TVL -----------------------------------------------------
    dl = src["defillama"]
    for key, slug in dl["slugs"].items():
        try:
            m[f"tvl_{key}"] = sources.defillama_tvl(dl["base_url"], slug, session)
        except Exception as e:
            errors[f"tvl_{key}"] = f"DefiLlama {slug}: {e}"

    eth = Chain(src["rpc"]["ethereum"], session=session)
    bsc = Chain(src["rpc"]["bsc"], session=session)
    E, B = src["ethereum"], src["bsc"]
    probes = src.get("probe_amounts", {})

    # --- Curve USDC/USDat --------------------------------------------------
    try:
        c = sources.curve_pool_state(eth, E["curve_usdat_pool"], E["usdat_token"], probes.get("usdat", 1000))
        m["price_usdat"] = c["price"]
        m["curve_usdat_share"] = c["share_pct"]
        m["curve_pool_liquidity"] = c["liquidity_usd"]
        m["curve_usdat_balance"] = c["target_balance"]
        m["curve_usdc_balance"] = c["quote_balance"]
    except Exception as e:
        errors["curve_usdat"] = f"Curve USDC/USDat: {e}"
        try:  # price-only fallback (share/liquidity stay None)
            g = sources.geckoterminal_pool(src["geckoterminal"]["base_url"], "eth",
                                           E["curve_usdat_pool"], E["usdat_token"])
            m["price_usdat"] = g["price"]
            errors["curve_usdat"] += " (ціну взято з GeckoTerminal)"
        except Exception as e2:
            errors["curve_usdat"] += f"; GeckoTerminal: {e2}"

    # --- Curve iUSD/USDC (thin pool — price is indicative only) ------------
    try:
        c = sources.curve_pool_state(eth, E["curve_iusd_pool"], E["iusd_token"], probes.get("iusd", 100))
        m["price_iusd"] = c["price"]
        m["curve_iusd_liquidity"] = c["liquidity_usd"]
    except Exception as e:
        errors["curve_iusd"] = f"Curve iUSD/USDC: {e}"

    # --- PancakeSwap V3 USDu/USDT -------------------------------------------
    try:
        p = sources.v3_pool_state(bsc, B["pancake_usdu_pool"], B["usdu_token"])
        m["price_usdu"] = p["price"]
        m["usdu_pool_liquidity"] = p["liquidity_usd"]
    except Exception as e:
        errors["pancake_usdu"] = f"PancakeSwap USDu/USDT: {e}"
        # fallback: GeckoTerminal
        try:
            g = sources.geckoterminal_pool(src["geckoterminal"]["base_url"], "bsc",
                                           B["pancake_usdu_pool"], B["usdu_token"])
            m["price_usdu"] = g["price"]
            m["usdu_pool_liquidity"] = g["liquidity_usd"]
            errors["pancake_usdu"] += " (взято з GeckoTerminal)"
        except Exception as e2:
            errors["pancake_usdu"] += f"; GeckoTerminal: {e2}"

    # --- InfiniFi on-chain --------------------------------------------------
    try:
        i = sources.infinifi_state(eth, E["infinifi_gateway"], int(E.get("liusd_bucket_weeks", 13)))
        m["liusd13w_rate"] = i["liusd_rate"]
        m["infinifi_tvl_onchain"] = i["tvl_onchain"]
        m["infinifi_liquid_reserve"] = i["liquid_reserve"]
        m["infinifi_liquid_reserve_pct"] = i["liquid_reserve_pct"]
        m["infinifi_queue_length"] = i["queue_length"]
        m["infinifi_queue_iusd"] = i["queue_iusd"]
        m["iusd_redeem_rate"] = i["iusd_redeem_rate"]
        m["infinifi_unaccrued_yield"] = i["unaccrued_yield"]
        if i["errors"]:
            errors["infinifi_partial"] = "InfiniFi (частина полів): " + "; ".join(f"{k}: {v}" for k, v in i["errors"].items())
    except Exception as e:
        errors["infinifi"] = f"InfiniFi on-chain: {e}"

    # reserve % needs a denominator: on-chain TVL, else DefiLlama TVL
    if m["infinifi_liquid_reserve_pct"] is None and m["infinifi_liquid_reserve"] is not None and m["tvl_infinifi"]:
        m["infinifi_liquid_reserve_pct"] = m["infinifi_liquid_reserve"] / m["tvl_infinifi"] * 100

    return m, errors
