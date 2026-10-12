"""All external data sources. Every function returns plain floats/dicts
and raises on failure; `metrics.py` decides what to do with errors."""
from __future__ import annotations

from fractions import Fraction

import requests

from .evm import ZERO_ADDR, Chain, dec_addr_array, enc_int, enc_string, enc_uint, is_zero, same_addr

UA = {"User-Agent": "defi-monitor-bot/1.0 (+github actions)"}
HTTP_TIMEOUT = 20


# --------------------------------------------------------------------------
# DefiLlama
# --------------------------------------------------------------------------
def defillama_tvl(base_url: str, slug: str, session: requests.Session | None = None) -> float:
    """Current TVL in USD. Uses the tiny /tvl/{slug} endpoint, falls back to /protocol/{slug}."""
    s = session or requests.Session()
    r = s.get(f"{base_url}/tvl/{slug}", headers=UA, timeout=HTTP_TIMEOUT)
    if r.ok:
        try:
            v = float(r.text.strip().strip('"'))
            if v > 0:
                return v
        except ValueError:
            pass
    r = s.get(f"{base_url}/protocol/{slug}", headers=UA, timeout=HTTP_TIMEOUT)
    if r.status_code in (400, 404):
        raise RuntimeError(f"DefiLlama не знає slug '{slug}' (HTTP {r.status_code}). "
                           f"Перевірте https://defillama.com/protocol/{slug}")
    r.raise_for_status()
    j = r.json()
    cur = j.get("currentChainTvls") or {}
    # exclude non-TVL buckets (staking, borrowed, pool2, …) the same way DefiLlama does
    total = sum(v for k, v in cur.items() if isinstance(v, (int, float)) and "-" not in k
                and k not in ("staking", "borrowed", "pool2", "vesting", "offers", "treasury"))
    if total:
        return float(total)
    hist = j.get("tvl") or []
    if hist:
        return float(hist[-1]["totalLiquidityUSD"])
    raise RuntimeError(f"DefiLlama: порожня відповідь для '{slug}'")


def defillama_protocol_info(base_url: str, slug: str) -> dict:
    """Used by self-test: name + per-chain TVL."""
    r = requests.get(f"{base_url}/protocol/{slug}", headers=UA, timeout=HTTP_TIMEOUT)
    if r.status_code in (400, 404):
        raise RuntimeError(f"slug '{slug}' не знайдено (HTTP {r.status_code})")
    r.raise_for_status()
    j = r.json()
    return {"name": j.get("name"), "chains": j.get("currentChainTvls", {})}


# --------------------------------------------------------------------------
# Curve stableswap pool (2 coins)
# --------------------------------------------------------------------------
def curve_pool_state(chain: Chain, pool: str, target_token: str, probe_amount: float) -> dict:
    """Read a 2-coin Curve pool.

    Returns price of `target_token` in units of the other coin (assumed ≈ $1 stable),
    share of target in the pool (%), balances and total liquidity in USD.
    """
    coins = [chain.addr(pool, "coins(uint256)", enc_uint(i)) for i in range(2)]
    if same_addr(coins[0], target_token):
        it, iq = 0, 1
    elif same_addr(coins[1], target_token):
        it, iq = 1, 0
    else:
        raise RuntimeError(f"Токен {target_token} не знайдено в пулі {pool} (coins={coins})")

    dec = [chain.decimals(c) for c in coins]

    def balance(i: int) -> int:
        try:
            return chain.uint(pool, "balances(uint256)", enc_uint(i))
        except Exception:
            try:
                return chain.uint(pool, "balances(int128)", enc_int(i))
            except Exception:
                return chain.balance_of(coins[i], pool)   # ERC20-баланс пулу (включає admin fees)

    bal_raw = [balance(i) for i in range(2)]
    bal = [bal_raw[i] / 10 ** dec[i] for i in range(2)]

    dx = int(round(probe_amount)) * 10 ** dec[it]
    try:
        dy = chain.uint(pool, "get_dy(int128,int128,uint256)", enc_int(it), enc_int(iq), enc_uint(dx))
    except Exception:
        dy = chain.uint(pool, "get_dy(uint256,uint256,uint256)", enc_uint(it), enc_uint(iq), enc_uint(dx))
    price = (dy / 10 ** dec[iq]) / probe_amount

    liquidity_usd = bal[iq] + bal[it] * price
    share_pct = (bal[it] * price) / liquidity_usd * 100 if liquidity_usd > 0 else 0.0
    return {
        "price": price,
        "share_pct": share_pct,
        "liquidity_usd": liquidity_usd,
        "target_balance": bal[it],
        "quote_balance": bal[iq],
        "coins": coins,
        "symbols": [chain.symbol(c) for c in coins],
    }


# --------------------------------------------------------------------------
# PancakeSwap / Uniswap V3 pool
# --------------------------------------------------------------------------
def v3_pool_state(chain: Chain, pool: str, target_token: str) -> dict:
    """Marginal price of `target_token` in the other token (from slot0.sqrtPriceX96)
    plus token balances held by the pool (as a liquidity proxy)."""
    t0 = chain.addr(pool, "token0()")
    t1 = chain.addr(pool, "token1()")
    if same_addr(t0, target_token):
        target_is_0 = True
    elif same_addr(t1, target_token):
        target_is_0 = False
    else:
        raise RuntimeError(f"Токен {target_token} не знайдено в пулі {pool} (token0={t0}, token1={t1})")

    d0, d1 = chain.decimals(t0), chain.decimals(t1)
    sqrt_px = chain.uint(pool, "slot0()")  # first word = sqrtPriceX96 (uint160)
    if sqrt_px == 0:
        raise RuntimeError("slot0 повернув 0 — пул не ініціалізовано")
    # price of token0 in token1 (human units) = (sqrtP/2^96)^2 * 10^(d0-d1)
    p = Fraction(sqrt_px, 1 << 96) ** 2 * Fraction(10 ** d0, 10 ** d1)
    price = float(p) if target_is_0 else float(1 / p)

    b0 = chain.balance_of(t0, pool) / 10 ** d0
    b1 = chain.balance_of(t1, pool) / 10 ** d1
    if target_is_0:
        tb, qb = b0, b1
    else:
        tb, qb = b1, b0
    return {
        "price": price,
        "liquidity_usd": qb + tb * price,
        "target_balance": tb,
        "quote_balance": qb,
        "token0": t0,
        "token1": t1,
        "symbols": [chain.symbol(t0), chain.symbol(t1)],
    }


# --------------------------------------------------------------------------
# InfiniFi on-chain (addresses resolved from the Gateway registry)
# --------------------------------------------------------------------------
WAD = 10 ** 18
FARM_TYPE_LIQUID = 1  # FarmTypes.sol: PROTOCOL=0, LIQUID=1, MATURITY=2


def infinifi_addresses(chain: Chain, gateway: str) -> dict:
    """Resolve module addresses from the Gateway registry. Missing names are
    reported (zero address) instead of aborting everything."""
    names = ["redeemController", "lockingController", "yieldSharing", "receiptToken", "USDC"]
    out: dict = {}
    for n in names:
        try:
            out[n] = chain.addr(gateway, "getAddress(string)", enc_string(n))
        except Exception:
            out[n] = ZERO_ADDR
    if all(is_zero(out[n]) for n in names):
        raise RuntimeError(f"Gateway {gateway} не повертає адрес модулів — перевірте адресу в config.yaml "
                           f"(docs.infinifi.xyz/dev-docs/contracts.md)")
    out["accounting"] = ZERO_ADDR
    for holder in ("redeemController", "yieldSharing"):
        if not is_zero(out[holder]):
            try:
                out["accounting"] = chain.addr(out[holder], "accounting()")
                break
            except Exception:
                continue
    return out


def _liquid_via_registry(chain: Chain, accounting: str) -> float:
    """Fallback for totalAssetsValueOf(LIQUID): sum assets() of LIQUID farms.
    All liquid farms are USDC-denominated (6 decimals) per docs.infinifi.xyz/vaults."""
    registry = chain.addr(accounting, "farmRegistry()")
    farms = dec_addr_array(chain.call(registry, "getTypeFarms(uint256)", enc_uint(FARM_TYPE_LIQUID)))
    return sum(chain.uint(f, "assets()") for f in farms) / 10 ** 6


def infinifi_state(chain: Chain, gateway: str, bucket_weeks: int = 13) -> dict:
    """Every value is read independently: one missing getter must not hide the rest.
    `out["errors"]` lists the fields that failed."""
    a = infinifi_addresses(chain, gateway)
    rc, lc, ys, acc = a["redeemController"], a["lockingController"], a["yieldSharing"], a["accounting"]
    out: dict = {"addresses": a, "errors": {}}

    def need(addr: str, name: str) -> str:
        if is_zero(addr):
            raise RuntimeError(f"{name}: адресу не знайдено в Gateway")
        return addr

    def read(key, fn, optional=False):
        try:
            out[key] = fn()
        except Exception as e:
            out[key] = None
            if not optional:
                out["errors"][key] = str(e)

    def liquid():
        try:
            return chain.uint(need(acc, "accounting"), "totalAssetsValueOf(uint256)", enc_uint(FARM_TYPE_LIQUID)) / WAD
        except Exception:
            return _liquid_via_registry(chain, need(acc, "accounting"))

    def usdc_decimals() -> int:
        try:
            return chain.decimals(need(a["USDC"], "USDC"))
        except Exception:
            return 6

    read("tvl_onchain", lambda: chain.uint(need(acc, "accounting"), "totalAssetsValue()") / WAD)
    read("liquid_reserve", liquid)
    read("queue_length", lambda: chain.uint(need(rc, "redeemController"), "queueLength()"))
    read("queue_iusd", lambda: chain.uint(need(rc, "redeemController"), "totalEnqueuedRedemptions()") / WAD)
    read("liusd_rate", lambda: chain.uint(need(lc, "lockingController"), "exchangeRate(uint32)", enc_uint(bucket_weeks)) / WAD)
    read("liusd_share_token", lambda: chain.addr(need(lc, "lockingController"), "shareToken(uint32)", enc_uint(bucket_weeks)))
    read("iusd_redeem_rate", lambda: chain.uint(need(rc, "redeemController"), "receiptToAsset(uint256)", enc_uint(WAD))
         / 10 ** usdc_decimals())
    read("unaccrued_yield", lambda: chain.int256(need(ys, "yieldSharing"), "unaccruedYield()") / WAD, optional=True)

    tvl, liq = out.get("tvl_onchain"), out.get("liquid_reserve")
    out["liquid_reserve_pct"] = (liq / tvl * 100) if tvl and liq is not None else None
    if all(out.get(k) is None for k in ("tvl_onchain", "queue_length", "liusd_rate")):
        raise RuntimeError("жоден виклик не вдався: " + "; ".join(f"{k}: {v}" for k, v in out["errors"].items()))
    return out


# --------------------------------------------------------------------------
# GeckoTerminal (fallback price source + pool discovery)
# --------------------------------------------------------------------------
def geckoterminal_pool(base_url: str, network: str, pool: str, target_token: str | None = None) -> dict:
    r = requests.get(f"{base_url}/networks/{network}/pools/{pool.lower()}",
                     headers={**UA, "Accept": "application/json"}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    d = r.json()["data"]
    at = d["attributes"]
    rel = d.get("relationships", {})
    base_id = (rel.get("base_token", {}).get("data") or {}).get("id", "")
    base_addr = base_id.split("_", 1)[-1] if base_id else ""
    quote_usd = float(at.get("quote_token_price_usd") or 1.0)
    base_usd = float(at.get("base_token_price_usd") or 1.0)
    bpq = float(at.get("base_token_price_quote_token") or 0) or None
    qpb = float(at.get("quote_token_price_base_token") or 0) or None

    if target_token and base_addr and not same_addr(base_addr, target_token):
        price = (qpb * base_usd) if qpb else quote_usd   # target is the quote token
    else:
        price = (bpq * quote_usd) if bpq else base_usd    # target is the base token
    return {
        "price": price,
        "liquidity_usd": float(at.get("reserve_in_usd") or 0),
        "name": at.get("name"),
        "raw": at,
    }


def geckoterminal_token_pools(base_url: str, network: str, token: str) -> list[dict]:
    """Top pools for a token, sorted by USD reserve (desc). Used by tools/find_pools.py."""
    r = requests.get(f"{base_url}/networks/{network}/tokens/{token.lower()}/pools",
                     params={"page": 1, "include": "dex"},
                     headers={**UA, "Accept": "application/json"}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    j = r.json()
    dex_names = {i["id"]: i["attributes"].get("name") for i in j.get("included", []) if i.get("type") == "dex"}
    pools = []
    for d in j.get("data", []):
        at = d["attributes"]
        dex_id = (d.get("relationships", {}).get("dex", {}).get("data") or {}).get("id")
        pools.append({
            "address": at.get("address"),
            "name": at.get("name"),
            "dex": dex_names.get(dex_id, dex_id),
            "reserve_usd": float(at.get("reserve_in_usd") or 0),
            "volume_24h": float((at.get("volume_usd") or {}).get("h24") or 0),
        })
    pools.sort(key=lambda p: p["reserve_usd"], reverse=True)
    return pools
