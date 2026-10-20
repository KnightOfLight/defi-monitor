"""List the deepest DEX pools for a token via GeckoTerminal.

    python tools/find_pools.py bsc 0xea953ea6634d55dac6697c436b1e81a679db5882   # USDu
    python tools/find_pools.py eth 0x23238f20b894f29041f48d88ee91131c395aaa71   # USDat
    python tools/find_pools.py eth 0x48f9e38f3070AD8945DFEae3FA70987722E3D89c   # iUSD
    python tools/find_pools.py eth 0xd0580192E98eA6CEB9c7b6191Ed2E27560911697   # trUSD (Tori)

Pick the top Curve/PancakeSwap pool against a stablecoin and put its address into config.yaml
(the bot reads Curve stableswap and Uniswap/PancakeSwap V3 pools; Fluid DEX pools are not supported).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.sources import geckoterminal_token_pools  # noqa: E402

if len(sys.argv) != 3:
    print(__doc__)
    sys.exit(1)
network, token = sys.argv[1], sys.argv[2]
for p in geckoterminal_token_pools("https://api.geckoterminal.com/api/v2", network, token)[:10]:
    print(f"{p['reserve_usd']:>14,.0f} USD  vol24h {p['volume_24h']:>12,.0f}  {p['dex']:<22} {p['name']:<24} {p['address']}")
