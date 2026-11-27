"""Collect all metrics into one flat dict. A failing source sets its metrics to
None and records the error — one broken API must never stop the others."""
from __future__ import annotations

import time

import requests

from . import fmt, sources
from .evm import Chain

# Flat metric keys stored in history.csv (order matters for the CSV header).
METRIC_KEYS = [
    "tvl_saturn", "tvl_unitas", "tvl_infinifi", "tvl_tori", "tvl_fluid", "tvl_mantle",
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
            if f"tvl_{key}" not in METRIC_KEYS:
                errors[f"tvl_{key}"] = f"tvl_{key}: додайте ключ у METRIC_KEYS, щоб він потрапляв в історію"
        except Exception as e:
            errors[f"tvl_{key}"] = f"DefiLlama {slug}: {e}"

    eth = Chain(src["rpc"]["ethereum"], session=session)
    bsc = Chain(src["rpc"]["bsc"], session=session)
    E, B = src["ethereum"], src.get("bsc") or {}
    probes = src.get("probe_amounts", {})

    # --- own stablecoins: Curve pool if it is deep enough, else DefiLlama aggregate ----------
    # 27.11: LPs left Curve frxUSD/trUSD ($1.07 млн → $26 тис., 95% trUSD) — продаж 1000 trUSD у такий пул
    # дав $0.886, хоча на Fluid trUSD коштував $0.999. Тонкий пул — не ціна ринку.
    min_liq = src.get("min_pool_liquidity_usd") or {}
    coins_url = dl.get("coins_url", "https://coins.llama.fi")
    pools = [("usdat", "curve_usdat_pool", "usdat_token", "Curve USDC/USDat", None),
             ("iusd", "curve_iusd_pool", "iusd_token", "Curve iUSD/USDC", "curve_iusd_liquidity"),
             ("trusd", "curve_trusd_pool", "trusd_token", "Curve frxUSD/trUSD", "trusd_pool_liquidity")]
    for key, pool_k, tok_k, label, liq_key in pools:
        if not (E.get(pool_k) and E.get(tok_k)):
            continue
        c = None
        try:
            c = sources.curve_pool_state(eth, E[pool_k], E[tok_k], probes.get(key, 1000))
            if liq_key:
                m[liq_key] = c["liquidity_usd"]
        except Exception as e:
            errors[f"curve_{key}"] = f"{label}: {e}"
        need = float(min_liq.get(key, min_liq.get("default", 100_000)))
        if c is not None and c["liquidity_usd"] >= need:
            m[f"price_{key}"] = c["price"]
            m[f"src_{key}"] = label
            continue
        try:                                   # тонкий / недоступний пул → агрегована ціна ринку
            m[f"price_{key}"] = sources.defillama_coin_price(coins_url, "ethereum", E[tok_k], session)
            m[f"src_{key}"] = ("DefiLlama (агрегована ціна; " + (f"{label} тонкий — {fmt.money(c['liquidity_usd'], 0)}, "
                               f"там {c['price']:.4f})" if c is not None else f"{label} недоступний)"))
        except Exception as e2:
            if c is not None:                  # краще ціна тонкого пулу з поміткою, ніж нічого
                m[f"price_{key}"] = c["price"]
                m[f"src_{key}"] = f"{label} (тонкий пул {fmt.money(c['liquidity_usd'], 0)} — ціна неточна)"
            errors[f"price_{key}"] = f"DefiLlama coins {key}: {e2}"

    # --- PancakeSwap V3 USDu/USDT (лише якщо USDu у config) ------------------
    if B.get("pancake_usdu_pool") and B.get("usdu_token"):
        try:
            p = sources.v3_pool_state(bsc, B["pancake_usdu_pool"], B["usdu_token"])
            m["price_usdu"] = p["price"]
            m["usdu_pool_liquidity"] = p["liquidity_usd"]
        except Exception as e:
            errors["pancake_usdu"] = f"PancakeSwap USDu/USDT: {e}"
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

    if E.get("trusd_token"):
        # --- Tori on-chain: strUSD rate, strUSD share of trUSD, trUSD supply -----
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
