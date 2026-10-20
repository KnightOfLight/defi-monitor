"""Collect all metrics into one flat dict. A failing source sets its metrics to
None and records the error — one broken API must never stop the others."""
from __future__ import annotations

import time

import requests

from . import sources
from .evm import Chain

# Flat metric keys stored in history.csv (order matters for the CSV header).
METRIC_KEYS = [
    "tvl_saturn", "tvl_unitas", "tvl_infinifi", "tvl_tori",
    "price_usdat", "price_usdu", "price_iusd", "price_trusd",
    "liusd13w_rate",
    "infinifi_tvl_onchain", "infinifi_liquid_reserve", "infinifi_liquid_reserve_pct",
    "infinifi_queue_length", "infinifi_queue_iusd",
    "iusd_redeem_rate", "infinifi_unaccrued_yield",
    "usdu_pool_liquidity", "curve_iusd_liquidity", "trusd_pool_liquidity",
    "pendle_pt_price", "pendle_pt_price_usd", "pendle_implied_apy", "pendle_liquidity", "pendle_days_to_expiry",
    "strusd_rate", "strusd_buffer_pct", "trusd_supply",
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

    # --- USDat: price only (Curve USDC/USDat, sell 1000 USDat) --------------
    try:
        c = sources.curve_pool_state(eth, E["curve_usdat_pool"], E["usdat_token"], probes.get("usdat", 1000))
        m["price_usdat"] = c["price"]
    except Exception as e:
        errors["curve_usdat"] = f"Curve USDC/USDat: {e}"
        try:  # price-only fallback
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

    # --- Tori: trUSD price (Curve frxUSD/trUSD), DefiLlama coins as fallback --
    if E.get("trusd_token"):
        try:
            c = sources.curve_pool_state(eth, E["curve_trusd_pool"], E["trusd_token"], probes.get("trusd", 1000))
            m["price_trusd"] = c["price"]
            m["trusd_pool_liquidity"] = c["liquidity_usd"]
        except Exception as e:
            errors["curve_trusd"] = f"Curve frxUSD/trUSD: {e}"
            try:
                m["price_trusd"] = sources.defillama_coin_price(dl.get("coins_url", "https://coins.llama.fi"),
                                                                "ethereum", E["trusd_token"], session)
                errors["curve_trusd"] += " (ціну взято з DefiLlama coins)"
            except Exception as e2:
                errors["curve_trusd"] += f"; DefiLlama coins: {e2}"

        # --- Tori on-chain: strUSD rate, first-loss buffer, trUSD supply -----
        try:
            t = sources.tori_state(eth, E["trusd_token"], E["strusd_token"])
            m["strusd_rate"] = t["strusd_rate"]
            m["strusd_buffer_pct"] = t["strusd_buffer_pct"]
            m["trusd_supply"] = t["trusd_supply"]
            if t["errors"]:
                errors["tori_partial"] = "Tori (частина полів): " + "; ".join(f"{k}: {v}" for k, v in t["errors"].items())
        except Exception as e:
            errors["tori"] = f"Tori on-chain: {e}"

    # --- Pendle trUSD market: on-chain, Pendle API as fallback ----------------
    if E.get("pendle_trusd_market"):
        trusd_usd = m["price_trusd"] if m["price_trusd"] is not None else 1.0
        try:
            pm = sources.pendle_market_state(eth, E["pendle_trusd_market"], time.time())
            m["pendle_liquidity"] = pm["liquidity_asset"] * trusd_usd
        except Exception as e:
            errors["pendle_trusd"] = f"Pendle trUSD on-chain: {e}"
            pm = None
            try:
                pm = sources.pendle_market_api(src["pendle"]["api_url"], int(src["pendle"].get("chain_id", 1)),
                                               E["pendle_trusd_market"])
                m["pendle_liquidity"] = pm["liquidity_usd"]
                errors["pendle_trusd"] += " (взято з Pendle API)"
            except Exception as e2:
                errors["pendle_trusd"] += f"; Pendle API: {e2}"
        if pm:
            m["pendle_pt_price"] = pm["pt_price"]
            m["pendle_pt_price_usd"] = pm["pt_price"] * trusd_usd if pm["pt_price"] is not None else None
            m["pendle_implied_apy"] = None if pm["expired"] else pm["implied_apy_pct"]
            m["pendle_days_to_expiry"] = pm["days_to_expiry"]

    return m, errors
