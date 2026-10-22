"""Tiny eth_call client over JSON-RPC. No web3/eth_abi — only `requests`.

Function selectors are keccak256(signature)[:4]; they were computed once and
are hard-coded here so the bot has no keccak dependency.
"""
from __future__ import annotations

import requests

UINT256_MASK = (1 << 256) - 1

# keccak256(signature)[:4] — verified with pycryptodome keccak
SEL = {
    "balanceOf(address)": "0x70a08231",
    "decimals()": "0x313ce567",
    "totalSupply()": "0x18160ddd",
    "symbol()": "0x95d89b41",
    # Curve stableswap
    "coins(uint256)": "0xc6610657",
    "balances(uint256)": "0x4903b0d1",
    "balances(int128)": "0x065a80d8",              # дуже старі пули Curve
    "get_dy(int128,int128,uint256)": "0x5e0d443f",
    "get_dy(uint256,uint256,uint256)": "0x556d6e9f",   # twocrypto-ng (резерв)
    # Uniswap/PancakeSwap V3
    "slot0()": "0x3850c7bd",
    "token0()": "0x0dfe1681",
    "token1()": "0xd21220a7",
    # InfiniFi
    "getAddress(string)": "0xbf40fac1",
    "accounting()": "0x9624e83e",
    "totalAssetsValue()": "0xd2c0dc14",
    "totalAssetsValueOf(uint256)": "0xa58a053c",
    "queueLength()": "0xab91c7b0",
    "totalEnqueuedRedemptions()": "0x3f3b03ca",
    "exchangeRate(uint32)": "0x10821ff9",
    "shareToken(uint32)": "0xe7dda2eb",
    "receiptToAsset(uint256)": "0xf308cf65",
    "unaccruedYield()": "0xf843336c",
    "farmRegistry()": "0x87418e4a",
    "getTypeFarms(uint256)": "0xc108e2c4",
    "assets()": "0x71a97305",
    # Pendle V2 market / SY (pendle-core-v2-public, PendleMarketV7.sol)
    "expiry()": "0xe184c9be",
    "readTokens()": "0x2c8ce6bc",
    "_storage()": "0xc3fb90d6",          # (int128 totalPt, int128 totalSy, uint96 lastLnImpliedRate, uint16×3)
    "exchangeRate()": "0x3ba0b9a9",      # SY → asset, 1e18
    # ERC-4626 (Tori strUSD = StakedTrUSD.sol)
    "convertToAssets(uint256)": "0x07a2d13a",
    "totalAssets()": "0x01e1d114",
    "asset()": "0x38d52e0f",
    # security watch: pause / roles / registries (keccak перевірено)
    "paused()": "0x5c975abb",
    "owner()": "0x8da5cb5b",
    "minter()": "0x07546172",
    "getFarms()": "0x9dc4ac8a",
    "assetToken()": "0x1083f761",
}
ZERO_ADDR = "0x" + "0" * 40

# JSON-RPC error codes that mean "this endpoint is throttled", not "the call reverted"
_RETRY_CODES = {-32005, -32029, -32097, 429, -32603}


class RpcError(RuntimeError):
    pass


class RpcUnavailable(RpcError):
    """No RPC endpoint answered (transport / rate limit) — unlike a revert, says nothing about the contract."""


def scrub(text: str, urls: list[str]) -> str:
    """Remove URL paths (where API keys live) from arbitrary error text."""
    from urllib.parse import urlsplit
    for u in urls:
        try:
            path = urlsplit(u).path
        except Exception:
            continue
        if len(path.strip("/")) >= 8:
            text = text.replace(path, "/…")
    return text


def mask_url(url: str) -> str:
    """https://eth-mainnet.g.alchemy.com/v2/alch_SECRET -> https://eth-mainnet.g.alchemy.com/…
    Keys live in the path, so only scheme + host are ever printed or sent."""
    try:
        from urllib.parse import urlsplit
        u = urlsplit(url)
        return f"{u.scheme}://{u.netloc}" + ("/…" if u.path.strip("/") else "")
    except Exception:
        return "<rpc>"


# --------------------------------------------------------------------------
# ABI helpers
# --------------------------------------------------------------------------
def enc_uint(v: int) -> str:
    if v < 0:
        raise ValueError("uint must be >= 0")
    return format(v, "064x")


def enc_int(v: int) -> str:
    return format(v & UINT256_MASK, "064x")


def enc_addr(a: str) -> str:
    a = a.lower()
    if a.startswith("0x"):
        a = a[2:]
    if len(a) != 40:
        raise ValueError(f"bad address {a!r}")
    return a.rjust(64, "0")


def enc_string(s: str) -> str:
    b = s.encode("utf-8")
    padded_len = ((len(b) + 31) // 32) * 32
    return enc_uint(32) + enc_uint(len(b)) + b.hex().ljust(padded_len * 2, "0")


def _check(result: str) -> str:
    if not result or result == "0x":
        raise RpcError("empty return data (wrong address or function?)")
    return result[2:]


def dec_uint(result: str, word: int = 0) -> int:
    h = _check(result)
    return int(h[word * 64:(word + 1) * 64], 16)


def dec_int(result: str, word: int = 0) -> int:
    v = dec_uint(result, word)
    return v - (1 << 256) if v >= (1 << 255) else v


def dec_addr(result: str, word: int = 0) -> str:
    h = _check(result)
    return "0x" + h[word * 64:(word + 1) * 64][-40:]


def dec_string(result: str) -> str:
    h = _check(result)
    try:
        offset = int(h[0:64], 16) * 2
        length = int(h[offset:offset + 64], 16) * 2
        return bytes.fromhex(h[offset + 64:offset + 64 + length]).decode("utf-8", "replace")
    except Exception:  # some old tokens return bytes32 symbols
        return bytes.fromhex(h[:64]).rstrip(b"\x00").decode("utf-8", "replace")


# --------------------------------------------------------------------------
# JSON-RPC
# --------------------------------------------------------------------------
class Chain:
    """One chain = ordered list of RPC URLs (first that works wins)."""

    def __init__(self, urls: list[str], timeout: float = 10.0, session=None):
        if not urls:
            raise RpcError("no RPC URLs configured")
        self.urls = list(urls)
        self.timeout = timeout
        self.session = session or requests.Session()
        self.dead: set[str] = set()

    def _rpc(self, method: str, params: list):
        last_err = None
        for attempt in range(2):                      # second pass only if every URL is marked dead
            for url in self.urls:
                if url in self.dead and attempt == 0:
                    continue
                try:
                    r = self.session.post(
                        url,
                        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                        timeout=self.timeout,
                    )
                    r.raise_for_status()
                    j = r.json()
                except Exception as e:                # transport problem: demote this URL for the run
                    last_err = f"{mask_url(url)}: {scrub(str(e), self.urls)}"
                    self.dead.add(url)
                    continue
                err = j.get("error")
                if err:
                    code = err.get("code") if isinstance(err, dict) else None
                    msg = str(err)
                    if code in _RETRY_CODES or "limit" in msg.lower() or "too many" in msg.lower():
                        last_err = f"{mask_url(url)}: {msg}"
                        self.dead.add(url)
                        continue
                    raise RpcError(f"{mask_url(url)}: {msg}")   # execution error — другий RPC не допоможе
                self.dead.discard(url)
                return j.get("result")
            if not self.dead:
                break
        raise RpcUnavailable(f"усі RPC не відповіли ({', '.join(mask_url(u) for u in self.urls)}): {last_err}")

    def call(self, to: str, sig: str, *encoded_args: str) -> str:
        data = SEL[sig] + "".join(encoded_args)
        return self._rpc("eth_call", [{"to": to, "data": data}, "latest"])

    # convenience wrappers ------------------------------------------------
    def uint(self, to: str, sig: str, *args: str) -> int:
        return dec_uint(self.call(to, sig, *args))

    def int256(self, to: str, sig: str, *args: str) -> int:
        return dec_int(self.call(to, sig, *args))

    def addr(self, to: str, sig: str, *args: str) -> str:
        return dec_addr(self.call(to, sig, *args))

    def decimals(self, token: str) -> int:
        return self.uint(token, "decimals()")

    def symbol(self, token: str) -> str:
        try:
            return dec_string(self.call(token, "symbol()"))
        except Exception:
            return "?"

    def balance_of(self, token: str, holder: str) -> int:
        return self.uint(token, "balanceOf(address)", enc_addr(holder))

    def storage(self, to: str, slot: str) -> int:
        """eth_getStorageAt(latest) as an integer."""
        v = self._rpc("eth_getStorageAt", [to, slot, "latest"])
        return int(v, 16) if v and v != "0x" else 0

    def block_number(self) -> int:
        return int(self._rpc("eth_blockNumber", []), 16)


def same_addr(a: str, b: str) -> bool:
    return a.lower() == b.lower()


def is_zero(a: str | None) -> bool:
    return not a or a.lower() == ZERO_ADDR


def dec_addr_array(result: str) -> list[str]:
    """Decode `address[]` return value."""
    h = _check(result)
    offset = int(h[0:64], 16) * 2
    n = int(h[offset:offset + 64], 16)
    return ["0x" + h[offset + 64 + i * 64:offset + 64 + (i + 1) * 64][-40:] for i in range(n)]
