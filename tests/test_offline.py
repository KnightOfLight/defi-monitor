"""Offline tests — run with:  python -m unittest -v tests.test_offline"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot import alerts, commands, evm, sources, storage, tge, weekly  # noqa: E402
from bot.config import load_config  # noqa: E402
from bot.telegram import Telegram  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
class FakeChain:
    """Minimal eth_call emulator keyed by (to, selector)."""

    def __init__(self, table: dict):
        self.table = {(k[0].lower(), k[1]): v for k, v in table.items()}
        self.urls = ["fake"]

    def call(self, to, sig, *args):
        key = (to.lower(), sig + "".join(args))
        if key not in self.table:
            key = (to.lower(), sig)
        return self.table[key]

    uint = lambda self, to, sig, *a: evm.dec_uint(self.call(to, sig, *a))
    int256 = lambda self, to, sig, *a: evm.dec_int(self.call(to, sig, *a))
    addr = lambda self, to, sig, *a: evm.dec_addr(self.call(to, sig, *a))

    def decimals(self, token):
        return self.uint(token, "decimals()")

    def symbol(self, token):
        return "SYM"

    def balance_of(self, token, holder):
        return self.uint(token, "balanceOf(address)", evm.enc_addr(holder))


def word(v: int) -> str:
    return "0x" + evm.enc_uint(v)


def addr_word(a: str) -> str:
    return "0x" + evm.enc_addr(a)


class TestAbi(unittest.TestCase):
    def test_encode_decode(self):
        self.assertEqual(evm.enc_uint(13), "0" * 63 + "d")
        self.assertEqual(evm.enc_int(-1), "f" * 64)
        self.assertEqual(evm.dec_int("0x" + "f" * 64), -1)
        a = "0x3f04b65Ddbd87f9CE0A2e7Eb24d80e7fb87625b5"
        self.assertEqual(evm.dec_addr(addr_word(a)), a.lower())
        s = evm.enc_string("redeemController")
        self.assertEqual(len(s), 64 * 3)
        self.assertEqual(int(s[:64], 16), 32)
        self.assertEqual(int(s[64:128], 16), len("redeemController"))
        self.assertEqual(evm.SEL["getAddress(string)"], "0xbf40fac1")
        with self.assertRaises(evm.RpcError):
            evm.dec_uint("0x")


class TestPoolMath(unittest.TestCase):
    def test_v3_price(self):
        usdt, usdu, pool = "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20
        # price of token0 (USDT) in token1 (USDu) = 1.004 USDu per USDT -> USDu price = 0.996
        sqrt_px = int((1.004 ** 0.5) * (1 << 96))
        chain = FakeChain({
            (pool, "token0()"): addr_word(usdt), (pool, "token1()"): addr_word(usdu),
            (usdt, "decimals()"): word(18), (usdu, "decimals()"): word(18),
            (pool, "slot0()"): word(sqrt_px) + "0" * 64 * 6,
            (usdt, "balanceOf(address)"): word(470_000 * 10 ** 18),
            (usdu, "balanceOf(address)"): word(234_000 * 10 ** 18),
        })
        p = sources.v3_pool_state(chain, pool, usdu)
        self.assertAlmostEqual(p["price"], 1 / 1.004, places=4)
        self.assertAlmostEqual(p["liquidity_usd"], 470_000 + 234_000 / 1.004, delta=1)

    def test_curve_pool(self):
        usdc, usdat, pool = "0x" + "aa" * 20, "0x" + "bb" * 20, "0x" + "cc" * 20
        chain = FakeChain({
            (pool, "coins(uint256)" + evm.enc_uint(0)): addr_word(usdc),
            (pool, "coins(uint256)" + evm.enc_uint(1)): addr_word(usdat),
            (usdc, "decimals()"): word(6), (usdat, "decimals()"): word(18),
            (pool, "balances(uint256)" + evm.enc_uint(0)): word(3_200_000 * 10 ** 6),
            (pool, "balances(uint256)" + evm.enc_uint(1)): word(3_500_000 * 10 ** 18),
            # selling 1000 USDat gives 998.5 USDC
            (pool, "get_dy(int128,int128,uint256)"): word(int(998.5 * 10 ** 6)),
        })
        c = sources.curve_pool_state(chain, pool, usdat, 1000)
        self.assertAlmostEqual(c["price"], 0.9985)
        self.assertAlmostEqual(c["liquidity_usd"], 3_200_000 + 3_500_000 * 0.9985, delta=1)
        self.assertAlmostEqual(c["share_pct"], 3_500_000 * 0.9985 / (3_200_000 + 3_500_000 * 0.9985) * 100, places=3)


    def test_pendle_market_matches_api_snapshot(self):
        """Numbers from api-v2.pendle.finance for the trUSD 26NOV2026 market on 20.10.2026 11:39 UTC:
        totalPt 262 394.79, totalSy 867 604.89, impliedApy 9.987%, ptDiscount 0.94779%, liquidity 1 127 512.7 trUSD."""
        import math
        market, sy, pt, yt = "0x" + "ab" * 20, "0x" + "a1" * 20, "0x" + "a2" * 20, "0x" + "a3" * 20
        expiry = int(datetime(2026, 11, 26, tzinfo=UTC).timestamp())
        now = datetime(2026, 10, 20, 11, 39, 30, tzinfo=UTC).timestamp()
        ln = int(math.log(1.0998713034760497) * 1e18)
        st = word(int(262_394.7885067771 * 1e18)) + evm.enc_uint(int(867_604.8945807992 * 1e18)) + evm.enc_uint(ln) \
            + evm.enc_uint(5) + evm.enc_uint(10) + evm.enc_uint(10)
        chain = FakeChain({
            (market, "expiry()"): word(expiry),
            (market, "readTokens()"): addr_word(sy) + evm.enc_addr(pt) + evm.enc_addr(yt),
            (market, "_storage()"): st,
            (sy, "exchangeRate()"): word(10 ** 18),
            (sy, "decimals()"): word(18),
        })
        r = sources.pendle_market_state(chain, market, now)
        self.assertAlmostEqual(r["implied_apy_pct"], 9.98713, places=3)
        self.assertAlmostEqual(1 - r["pt_price"], 0.0094779, places=5)
        self.assertAlmostEqual(r["liquidity_asset"], 1_127_512.7, delta=5)
        self.assertAlmostEqual(r["days_to_expiry"], 36.51, places=1)
        self.assertFalse(r["expired"])
        self.assertEqual(r["pt"], pt)
        # after expiry PT = 1 trUSD
        r = sources.pendle_market_state(chain, market, expiry + 60)
        self.assertEqual(r["pt_price"], 1.0)
        self.assertTrue(r["expired"])

    def test_tori_state(self):
        trusd, strusd = "0x" + "d0" * 20, "0x" + "28" * 20
        chain = FakeChain({
            (trusd, "totalSupply()"): word(71_560_000 * 10 ** 18),
            (strusd, "convertToAssets(uint256)"): word(1_012_345_678_000_000_000),
            (strusd, "totalAssets()"): word(37_500_000 * 10 ** 18),
        })
        t = sources.tori_state(chain, trusd, strusd)
        self.assertAlmostEqual(t["strusd_rate"], 1.012345678)
        self.assertAlmostEqual(t["strusd_buffer_pct"], 37.5 / 71.56 * 100)
        self.assertEqual(t["errors"], {})


# --------------------------------------------------------------------------
def base_metrics(**over) -> dict:
    m = {k: None for k in storage.METRIC_KEYS}
    m.update({
        "tvl_saturn": 152e6, "tvl_unitas": 53e6, "tvl_infinifi": 52e6,
        "price_usdat": 0.9990, "price_usdu": 0.9980, "price_iusd": 0.9985,
        "liusd13w_rate": 1.08, "infinifi_tvl_onchain": 52.4e6, "infinifi_liquid_reserve": 4.1e6,
        "infinifi_liquid_reserve_pct": 7.8, "infinifi_queue_length": 0, "infinifi_queue_iusd": 0,
        "iusd_redeem_rate": 1.0, "infinifi_unaccrued_yield": 12.0,
        "usdu_pool_liquidity": 7e5, "curve_iusd_liquidity": 1e4,
        "tvl_tori": 71.5e6, "price_trusd": 0.9996, "trusd_pool_liquidity": 3e6,
        "pendle_pt_price": 0.99052, "pendle_pt_price_usd": 0.99015, "pendle_implied_apy": 9.99,
        "pendle_liquidity": 1.127e6, "pendle_days_to_expiry": 36.5,
        "strusd_rate": 1.0123, "strusd_buffer_pct": 52.0, "trusd_supply": 71.5e6,
    })
    m.update(over)
    return m


def fresh_state(started: datetime) -> dict:
    st = storage.load_state("/nonexistent/state.json")
    st["started_at"] = storage.iso(started)
    return st


class TestAlerts(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.cfg["alerts"]["warmup_days"] = 7

    def run_engine(self, state, metrics, now, history=(), errors=None):
        eng = alerts.AlertEngine(self.cfg, state, now)
        msgs = eng.run(metrics, list(history), errors or {})
        state["last_metrics"] = metrics
        return msgs

    def test_warmup_suppresses_tvl_but_not_prices(self):
        st = fresh_state(T0)
        hist = [{"ts": T0 - timedelta(hours=24), **base_metrics()}]
        m = base_metrics(tvl_saturn=152e6 * 0.5, price_usdat=0.98)  # -50% TVL (alarm) + price alarm
        msgs = self.run_engine(st, m, T0, hist)
        self.assertEqual(len(msgs), 1)
        self.assertIn("USDat", msgs[0])
        self.assertIn("ТРИВОГА", msgs[0])
        # after warm-up the TVL alarm fires
        later = T0 + timedelta(days=8)
        hist = [{"ts": later - timedelta(hours=24), **base_metrics()}]
        msgs = self.run_engine(st, m, later, hist)
        self.assertTrue(any("TVL Saturn" in x and "ТРИВОГА" in x for x in msgs))

    def test_price_warn_needs_two_checks_then_recovers(self):
        st = fresh_state(T0 - timedelta(days=30))
        msgs = self.run_engine(st, base_metrics(price_usdu=0.994), T0)
        self.assertEqual(msgs, [])
        msgs = self.run_engine(st, base_metrics(price_usdu=0.994), T0 + timedelta(minutes=30))
        self.assertEqual(len(msgs), 1)
        self.assertIn("УВАГА", msgs[0])
        self.assertIn("2 перевірках", msgs[0])
        # repeat only after 6h
        msgs = self.run_engine(st, base_metrics(price_usdu=0.994), T0 + timedelta(hours=1))
        self.assertEqual(msgs, [])
        msgs = self.run_engine(st, base_metrics(price_usdu=0.994), T0 + timedelta(hours=7))
        self.assertEqual(len(msgs), 1)
        self.assertIn("повтор", msgs[0])
        # escalation to alarm (USDu threshold 0.98)
        msgs = self.run_engine(st, base_metrics(price_usdu=0.975), T0 + timedelta(hours=7, minutes=30))
        self.assertEqual(len(msgs), 1)
        self.assertIn("ТРИВОГА", msgs[0])
        self.assertIn("ескалація", msgs[0])
        # recovery
        msgs = self.run_engine(st, base_metrics(price_usdu=0.999), T0 + timedelta(hours=8))
        self.assertEqual(len(msgs), 1)
        self.assertIn("НОРМА", msgs[0])

    def test_pendle_pt_drop_apy_liquidity(self):
        st = fresh_state(T0 - timedelta(days=30))
        hist = [{"ts": T0 - timedelta(hours=24), **base_metrics()},
                {"ts": T0 - timedelta(days=7), **base_metrics(pendle_liquidity=1.2e6)}]
        # PT −0.6% за 24 год → увага
        msgs = self.run_engine(st, base_metrics(pendle_pt_price=0.99052 * 0.994), T0, hist)
        self.assertEqual(len(msgs), 1)
        self.assertIn("PT-trUSD подешевшав", msgs[0])
        self.assertIn("УВАГА", msgs[0])
        # −2% → ескалація до тривоги
        msgs = self.run_engine(st, base_metrics(pendle_pt_price=0.99052 * 0.98), T0 + timedelta(minutes=30), hist)
        self.assertEqual(len(msgs), 1)
        self.assertIn("ТРИВОГА", msgs[0])
        self.assertIn("ескалація", msgs[0])
        # пропуск в історії не дає хибної «норми»
        msgs = self.run_engine(st, base_metrics(pendle_pt_price=0.97), T0 + timedelta(hours=10), hist)
        self.assertEqual(msgs, [])
        # відновлення
        msgs = self.run_engine(st, base_metrics(), T0 + timedelta(hours=10, minutes=30),
                               [{"ts": T0 - timedelta(hours=14), **base_metrics()}])
        self.assertTrue(any("НОРМА" in x and "PT-trUSD" in x for x in msgs))

        # implied APY > 30% на двох перевірках; останній тиждень — вимкнено
        st = fresh_state(T0 - timedelta(days=30))
        self.assertEqual(self.run_engine(st, base_metrics(pendle_implied_apy=45.0), T0), [])
        msgs = self.run_engine(st, base_metrics(pendle_implied_apy=45.0), T0 + timedelta(minutes=30))
        self.assertTrue(any("Implied APY" in x and "УВАГА" in x for x in msgs))
        msgs = self.run_engine(st, base_metrics(pendle_implied_apy=80.0, pendle_days_to_expiry=3), T0 + timedelta(hours=1))
        self.assertFalse(any("Implied APY" in x for x in msgs))
        self.assertFalse(st["alerts"]["pendle_apy"]["active"])

        # ліквідність ринку −30% за 24 год → увага
        st = fresh_state(T0 - timedelta(days=30))
        hist = [{"ts": T0 - timedelta(hours=24), **base_metrics(pendle_liquidity=1.6e6)}]
        msgs = self.run_engine(st, base_metrics(pendle_liquidity=1.1e6), T0, hist)
        self.assertEqual(len(msgs), 1)
        self.assertIn("Ліквідність ринку Pendle trUSD", msgs[0])

    def test_maturity_reminders_and_new_market(self):
        st = fresh_state(T0 - timedelta(days=30))
        self.assertEqual(self.run_engine(st, base_metrics(pendle_days_to_expiry=20), T0), [])
        msgs = self.run_engine(st, base_metrics(pendle_days_to_expiry=13.9), T0 + timedelta(hours=1))
        self.assertEqual(len(msgs), 1)
        self.assertIn("погашається", msgs[0])
        self.assertEqual(self.run_engine(st, base_metrics(pendle_days_to_expiry=13.8), T0 + timedelta(hours=2)), [])
        # бот лежав тиждень: 7 і 2 дні перетнуто разом → одне нагадування
        msgs = self.run_engine(st, base_metrics(pendle_days_to_expiry=1.5), T0 + timedelta(days=12))
        self.assertEqual(len(msgs), 1)
        self.assertEqual(self.run_engine(st, base_metrics(pendle_days_to_expiry=1.4), T0 + timedelta(days=12, hours=1)), [])
        # новий ринок з'явився
        st["pendle_trusd_markets"] = [
            {"address": self.cfg["sources"]["ethereum"]["pendle_trusd_market"].lower(), "expiry": "2026-11-26"},
            {"address": "0x" + "12" * 20, "expiry": "2027-02-25", "implied_apy_pct": 12.0, "liquidity_usd": 5e5}]
        msgs = self.run_engine(st, base_metrics(pendle_days_to_expiry=1.3), T0 + timedelta(days=12, hours=2))
        self.assertEqual(len(msgs), 1)
        self.assertIn("новий ринок trUSD", msgs[0])
        self.assertIn("2027-02-25", msgs[0])
        # погашення: одне повідомлення з посиланням на новий ринок, алерти Pendle замовкли
        st["alerts"]["pendle_pt"] = {"active": True, "level": "warn", "since": storage.iso(T0), "last_sent": storage.iso(T0)}
        msgs = self.run_engine(st, base_metrics(pendle_days_to_expiry=-0.01, pendle_implied_apy=None), T0 + timedelta(days=14))
        self.assertEqual(len(msgs), 1)
        self.assertIn("погашено", msgs[0])
        self.assertIn("2027-02-25", msgs[0])
        self.assertFalse(st["alerts"]["pendle_pt"]["active"])
        self.assertEqual(self.run_engine(st, base_metrics(pendle_days_to_expiry=-1), T0 + timedelta(days=15)), [])

    def test_strusd_rate_drop_and_trusd_price(self):
        st = fresh_state(T0 - timedelta(days=30))
        self.run_engine(st, base_metrics(), T0)
        msgs = self.run_engine(st, base_metrics(strusd_rate=1.0100), T0 + timedelta(minutes=30))
        self.assertEqual(len(msgs), 1)
        self.assertIn("Курс strUSD", msgs[0])
        self.assertIn("ТРИВОГА", msgs[0])
        self.assertEqual(self.run_engine(st, base_metrics(strusd_rate=1.0100), T0 + timedelta(hours=1)), [])
        # trUSD: тривога одразу нижче 0.985
        msgs = self.run_engine(st, base_metrics(strusd_rate=1.0100, price_trusd=0.98), T0 + timedelta(hours=2))
        self.assertTrue(any("trUSD = <b>$0.9800</b>" in x and "ТРИВОГА" in x for x in msgs))

    def test_infinifi_reserve_queue_rate(self):
        st = fresh_state(T0 - timedelta(days=30))
        # reserve low: no alert until 3 days
        msgs = self.run_engine(st, base_metrics(infinifi_liquid_reserve_pct=4.0, infinifi_queue_iusd=1000, infinifi_queue_length=2), T0)
        self.assertEqual(msgs, [])
        msgs = self.run_engine(st, base_metrics(infinifi_liquid_reserve_pct=4.0, infinifi_queue_iusd=1000, infinifi_queue_length=2),
                               T0 + timedelta(hours=49))
        self.assertEqual(len(msgs), 1)
        self.assertIn("Черга redemption", msgs[0])
        self.assertIn("ТРИВОГА", msgs[0])
        msgs = self.run_engine(st, base_metrics(infinifi_liquid_reserve_pct=4.0, infinifi_queue_iusd=1000, infinifi_queue_length=2),
                               T0 + timedelta(hours=73))
        self.assertTrue(any("Ліквідний резерв" in x and "УВАГА" in x for x in msgs))
        # rate drop is a one-off event
        msgs = self.run_engine(st, base_metrics(liusd13w_rate=1.07), T0 + timedelta(hours=74))
        rate_msgs = [x for x in msgs if "Exchange rate" in x]
        self.assertEqual(len(rate_msgs), 1)
        self.assertIn("1.080000 → <b>1.070000</b>", rate_msgs[0])
        msgs = self.run_engine(st, base_metrics(liusd13w_rate=1.07), T0 + timedelta(hours=75))
        self.assertFalse(any("Exchange rate" in x for x in msgs))

    def test_tvl_gap_does_not_clear_alarm(self):
        st = fresh_state(T0 - timedelta(days=30))
        hist = [{"ts": T0 - timedelta(hours=24), **base_metrics()}]
        msgs = self.run_engine(st, base_metrics(tvl_saturn=152e6 * 0.6), T0, hist)
        self.assertTrue(any("ТРИВОГА" in x for x in msgs))
        # 5 hours later the 24h-ago reading is missing (history gap) -> no spurious "НОРМА"
        msgs = self.run_engine(st, base_metrics(tvl_saturn=152e6 * 0.6), T0 + timedelta(hours=5), hist)
        self.assertEqual(msgs, [])
        self.assertTrue(st["alerts"]["tvl_saturn"]["active"])

    def test_source_errors(self):
        st = fresh_state(T0 - timedelta(days=30))
        for i in range(3):
            msgs = self.run_engine(st, base_metrics(tvl_saturn=None), T0 + timedelta(minutes=30 * i),
                                   errors={"tvl_saturn": "DefiLlama saturn: timeout"})
        self.assertEqual(len(msgs), 1)
        self.assertIn("tvl_saturn", msgs[0])
        msgs = self.run_engine(st, base_metrics(), T0 + timedelta(hours=2))
        self.assertEqual(len(msgs), 1)
        self.assertIn("знову працює", msgs[0])
        self.assertNotIn("tvl_saturn", st["source_failures"])


# --------------------------------------------------------------------------
class TestCommandsAndWeekly(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = load_config()
        self.cfg["files"]["journal"] = os.path.join(self.tmp, "journal.csv")
        self.cfg["files"]["history"] = os.path.join(self.tmp, "history.csv")
        self.cfg["files"]["state"] = os.path.join(self.tmp, "state.json")
        self.tg = Telegram("t", "123", dry_run=True)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def ctx(self, now: datetime) -> commands.Ctx:
        st = fresh_state(now - timedelta(days=30))
        c = commands.Ctx(cfg=self.cfg, state=st, tg=self.tg, now=now)
        c.metrics, c.errors, c.history = base_metrics(), {}, []
        return c

    def test_parse_points(self):
        self.assertEqual(commands.parse_points("saturn 123456 unitas 7890 infinifi 4567"),
                         {"saturn_points": 123456.0, "unitas_points": 7890.0, "infinifi_points": 4567.0})
        self.assertEqual(commands.parse_points("Saturn=1,5 inf: 10"), {"saturn_points": 1.5, "infinifi_points": 10.0})
        self.assertEqual(commands.parse_points("hello"), {})
        self.assertEqual(commands.parse_points("tori 2 500 cores"), {"tori_points": 2500.0})
        self.assertEqual(commands.parse_points("cores=12,345"), {"tori_points": 12345.0})

    def test_digest_then_log_then_reminder(self):
        friday = datetime(2026, 10, 23, 7, 10, tzinfo=UTC)  # 10:10 Kyiv (EEST)
        c = self.ctx(friday)
        self.assertEqual(weekly.run_weekly(c), "digest")
        self.assertIn("Тижневий дайджест", self.tg.sent[-1])
        self.assertIn("Pendle trUSD", self.tg.sent[-1])
        self.assertIn("tori 2500", self.tg.sent[-1])
        self.assertNotIn("Curve USDC/USDat", self.tg.sent[-1])
        rows = storage.load_journal(self.cfg["files"]["journal"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date"], "2026-10-23")
        self.assertEqual(rows[0]["saturn_points"], "")
        self.assertEqual(c.state["weekly"]["pending_points_date"], "2026-10-23")
        # same-hour second run (08:00 UTC cron) must not resend
        c2 = commands.Ctx(cfg=self.cfg, state=c.state, tg=self.tg, now=friday + timedelta(hours=1))
        c2.metrics, c2.errors, c2.history = base_metrics(), {}, []
        self.assertEqual(weekly.run_weekly(c2), "idle")
        # saturday reminder fires (10:xx Kyiv) because points are missing
        sat = commands.Ctx(cfg=self.cfg, state=c.state, tg=self.tg, now=datetime(2026, 10, 24, 7, 5, tzinfo=UTC))
        sat.metrics, sat.errors, sat.history = base_metrics(), {}, []
        self.assertEqual(weekly.run_weekly(sat), "reminder")
        self.assertIn("Нагадування", self.tg.sent[-1])
        # user logs on Sunday -> fills Friday's row
        sun = commands.Ctx(cfg=self.cfg, state=c.state, tg=self.tg, now=datetime(2026, 10, 25, 9, 0, tzinfo=UTC))
        sun.metrics, sun.errors, sun.history = base_metrics(), {}, []
        reply = commands.dispatch(sun, "/log saturn 100 unitas 200 infinifi 300 tori 400")
        self.assertIn("2026-10-23", reply)
        self.assertIn("Tori (Cores): <b>400</b>", reply)
        rows = storage.load_journal(self.cfg["files"]["journal"])
        self.assertEqual(rows[0]["saturn_points"], "100")
        self.assertEqual(rows[0]["tori_points"], "400")
        self.assertIsNone(c.state["weekly"]["pending_points_date"])
        # a later /log on a new day creates a new row and shows the delta
        nxt = commands.Ctx(cfg=self.cfg, state=c.state, tg=self.tg, now=datetime(2026, 10, 30, 9, 0, tzinfo=UTC))
        nxt.metrics, nxt.errors, nxt.history = base_metrics(), {}, []
        reply = commands.dispatch(nxt, "/log saturn 150")
        self.assertIn("+50", reply)
        self.assertEqual(len(storage.load_journal(self.cfg["files"]["journal"])), 2)
        # /note and /export
        reply = commands.dispatch(nxt, "/note перевірив дашборди")
        self.assertIn("перевірив", reply)
        reply = commands.dispatch(nxt, "/export")
        self.assertIn("Поінти Saturn", reply)
        self.assertTrue(any("[document" in s for s in self.tg.sent))
        # /status and /thresholds render without errors
        status = commands.dispatch(nxt, "/status")
        self.assertIn("Статус", status)
        self.assertIn("Tori · Pendle trUSD", status)
        self.assertIn("через 36 дн.", status)
        self.assertNotIn("Частка USDat", status)
        self.assertIn("Pendle trUSD", commands.dispatch(nxt, "/thresholds"))

    def test_winter_time_hour_selection(self):
        # January: Kyiv = UTC+2, so 07:00 UTC is 09:00 (idle) and 08:00 UTC is 10:00 (digest)
        fri7 = self.ctx(datetime(2027, 1, 15, 7, 2, tzinfo=UTC))
        self.assertEqual(weekly.run_weekly(fri7), "idle")
        fri8 = commands.Ctx(cfg=self.cfg, state=fri7.state, tg=self.tg, now=datetime(2027, 1, 15, 8, 2, tzinfo=UTC))
        fri8.metrics, fri8.errors, fri8.history = base_metrics(), {}, []
        self.assertEqual(weekly.run_weekly(fri8), "digest")

    def test_journal_keeps_old_columns(self):
        """journal.csv from v1.0 had curve_* columns: values stay, new columns are added."""
        p = self.cfg["files"]["journal"]
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write("date,saturn_points,unitas_points,infinifi_points,tvl_saturn,tvl_unitas,tvl_infinifi,"
                     "price_usdat,price_usdu,price_iusd,liusd13w_rate,infinifi_liquid_reserve,curve_usdat_share,"
                     "curve_pool_liquidity,note\n"
                     "2026-10-16,100,200,300,1,2,3,0.999,0.998,0.997,1.08,4100000,52.1,6700000.00,перший\n")
        nxt = self.ctx(datetime(2026, 10, 23, 9, 0, tzinfo=UTC))
        commands.dispatch(nxt, "/log saturn 150 tori 10")
        with open(p, encoding="utf-8") as fh:
            header = fh.readline().strip().split(",")
        self.assertIn("tori_points", header)
        self.assertIn("curve_usdat_share", header)
        rows = storage.load_journal(p)
        self.assertEqual(rows[0]["curve_usdat_share"], "52.1")
        self.assertEqual(rows[0]["note"], "перший")
        self.assertEqual(rows[1]["tori_points"], "10")
        self.assertEqual(rows[1]["curve_usdat_share"], "")
        self.assertEqual(rows[1]["price_trusd"], "0.9996")

    def test_history_roundtrip(self):
        p = self.cfg["files"]["history"]
        rows = storage.append_history(p, base_metrics(), T0 - timedelta(days=8), keep_days=10)
        rows = storage.append_history(p, base_metrics(tvl_saturn=1.0), T0, keep_days=10)
        self.assertEqual(len(rows), 2)
        self.assertEqual(storage.value_ago(rows, "tvl_saturn", T0, 24 * 7, 12), None)  # 8 days ago is out of tolerance
        self.assertEqual(storage.value_ago(rows, "tvl_saturn", T0, 24 * 8, 12), 152e6)
        rows = storage.append_history(p, base_metrics(), T0 + timedelta(days=3), keep_days=10)
        self.assertEqual(len(rows), 2)  # 11-day-old row pruned


# --------------------------------------------------------------------------
RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>q</title>
<item><title>infiniFi sets INFI TGE for November 18 - CoinGabbar</title><link>https://news.google.com/a1</link>
<guid isPermaLink="false">CBMi-a1</guid><pubDate>{d1}</pubDate><description>&lt;a&gt;infiniFi sets INFI TGE&lt;/a&gt;</description>
<source url="https://www.coingabbar.com">CoinGabbar</source></item>
<item><title>infiniFi sets INFI TGE for November 18 - TokenPost</title><link>https://news.google.com/a2</link>
<guid isPermaLink="false">CBMi-a2</guid><pubDate>{d1}</pubDate><source url="https://tokenpost.com">TokenPost</source></item>
<item><title>infiniFi Prime goes live on Blend - Blockworks</title><link>https://news.google.com/a3</link>
<guid isPermaLink="false">CBMi-a3</guid><pubDate>{d1}</pubDate><source url="https://blockworks.co">Blockworks</source></item>
<item><title>infiniFi airdrop guide from last year - Old</title><link>https://news.google.com/a4</link>
<guid isPermaLink="false">CBMi-a4</guid><pubDate>{d_old}</pubDate><source url="https://x.io">Old</source></item>
</channel></rss>"""

TME = """<html><body><section class="tgme_channel_history js-message_history">
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="wublockchainenglish/26490" data-view="x">
<div class="tgme_widget_message_text js-message_text" dir="auto">Saturn Foundation: <b>STRN</b> TGE and Season 2 snapshot on Nov 20, 2026.<br/>Claim details soon.</div>
<div class="tgme_widget_message_footer"><a class="tgme_widget_message_date" href="https://t.me/wublockchainenglish/26490"><time datetime="2026-10-21T09:15:00+00:00" class="time">09:15</time></a></div>
</div></div>
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="wublockchainenglish/26491" data-view="y">
<div class="tgme_widget_message_text js-message_text" dir="auto">Saturn V rocket replica sold at auction; airdrop of NFTs to bidders.</div>
<a class="tgme_widget_message_date" href="#"><time datetime="2026-10-21T09:20:00+00:00" class="time">09:20</time></a>
</div></div></section></body></html>"""

BING = """<?xml version="1.0" encoding="utf-8" ?><rss version="2.0" xmlns:News="https://www.bing.com:443/news/search?q=x&amp;format=rss"><channel><title>x</title>
<item><title>infiniFi Reveals INFI Tokenomics with 45% Allocated to Ecosystem</title>
<link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;aid=&amp;tid=1&amp;url=https%3a%2f%2fwww.kucoin.com%2fnews%2fflash%2finfinifi-reveals&amp;c=1&amp;mkt=en-us</link>
<description>infiniFi published INFI tokenomics.</description><pubDate>{d}</pubDate><News:Source>kucoin</News:Source></item>
<item><title>Tokenomics 101: Building Sustainable Economic Models</title><link>http://www.bing.com/news/apiclick.aspx?url=https%3a%2f%2fforbes.com%2fa</link>
<pubDate>Thu, 12 Dec 2024 04:30:00 GMT</pubDate><News:Source>Forbes</News:Source></item>
</channel></rss>"""

SITEMAP_INDEX = """<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://docs.x.io/sitemap-pages.xml</loc></sitemap></sitemapindex>"""
SITEMAP_PAGES = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://docs.x.io/overview</loc></url><url><loc>https://docs.x.io/es/overview</loc></url>{extra}</urlset>"""


class FakeResp:
    def __init__(self, text):
        self.text, self.content, self.status_code = text, text.encode(), 200

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, pages):
        self.pages = pages

    def get(self, url, **kw):
        for k, v in self.pages.items():
            if url.startswith(k):
                if isinstance(v, Exception):
                    raise v
                return FakeResp(v() if callable(v) else v)
        raise RuntimeError(f"unexpected url {url}")


class TestTge(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.cfg["tge"]["projects"] = {
            "infinifi": dict(self.cfg["tge"]["projects"]["infinifi"], docs_sitemap="https://docs.x.io/sitemap.xml"),
            "saturn": dict(self.cfg["tge"]["projects"]["saturn"], docs_sitemap=None, news_query=None, bing_queries=None),
        }
        self.cfg["tge"]["telegram_channels"] = ["wublockchainenglish"]

    def test_parsers_and_helpers(self):
        from email.utils import format_datetime
        items = tge.parse_rss(RSS.format(d1=format_datetime(T0), d_old=format_datetime(T0 - timedelta(days=200))))
        self.assertEqual(len(items), 4)
        self.assertEqual(items[0]["via"], "CoinGabbar")
        self.assertEqual(items[0]["ts"], T0)
        posts = tge.parse_tme(TME)
        self.assertEqual([p["id"] for p in posts], ["tg:wublockchainenglish/26490", "tg:wublockchainenglish/26491"])
        self.assertIn("STRN TGE", posts[0]["text"])
        self.assertEqual(posts[0]["link"], "https://t.me/wublockchainenglish/26490")
        self.assertEqual(tge.dates_in("TGE on November 18, Q4 2026; claim 2026-11-20"), ["November 18", "Q4 2026", "2026-11-20"])
        self.assertEqual(tge.dates_in("tokens may launch soon"), [])
        self.assertEqual(tge.hits("New $INFI listing", ["INFI"]), ["INFI"])
        self.assertEqual(tge.hits("INFINITY airdrop", ["INFI"]), [])
        self.assertEqual(tge.doc_path("https://docs.tori.finance/es/resources/cores"), "/resources/cores")
        self.assertEqual(tge.doc_path("https://docs.unitas.so/overview/up"), "/overview/up")
        self.assertEqual(tge.doc_hit("/overview/up/tokenomics", ["UP"]), ["tokenomics", "up"])
        self.assertEqual(tge.doc_hit("/points/get-susdu-lp", ["UP"]), [])
        self.assertEqual(tge.doc_hit("/saturn-docs/operations-and-governance/new-page", ["STRN"]), [])
        # китайський текст: латинська назва без пробілів і китайські ключові слова
        self.assertEqual(tge.hits("加密协议infiniFi公布代币经济学", ["infiniFi"]), ["infiniFi"])
        self.assertEqual(tge.hits("infiniFi宣布空投快照时间", ["空投", "TGE"]), ["空投"])
        self.assertEqual(tge.hits("Bybit: New Crypto Perpetual Listing: INFIUSDT", ["INFI", "INFIUSDT"]), ["INFIUSDT"])
        self.assertNotEqual(tge.hid_title("Saturn 宣布 TGE"), tge.hid_title("Tori 宣布空投"))
        seen_urls = []

        class Rec:
            def get(self, url, **kw):
                seen_urls.append(url)
                return FakeResp(BING.format(d="Tue, 13 Oct 2026 18:33:00 GMT"))
        tge.bing_news("Saturn STRN", Rec())
        self.assertIn("q=Saturn+STRN&format=rss", seen_urls[0])      # без «( … OR … )» — з ними Bing порожній
        b = tge.parse_rss(BING.format(d="Tue, 13 Oct 2026 18:33:00 GMT"), "bn")
        self.assertEqual(b[0]["link"], "https://www.kucoin.com/news/flash/infinifi-reveals")
        self.assertEqual(b[0]["via"], "kucoin")
        self.assertTrue(b[0]["id"].startswith("bn:"))
        sess = FakeSession({"https://docs.x.io/sitemap.xml": SITEMAP_INDEX,
                            "https://docs.x.io/sitemap-pages.xml": SITEMAP_PAGES.format(extra="")})
        self.assertEqual(tge.sitemap_urls("https://docs.x.io/sitemap.xml", sess),
                         {"https://docs.x.io/overview", "https://docs.x.io/es/overview"})

    def test_tme_reply_quote_is_not_the_post(self):
        page = """<div class="tgme_widget_message_wrap js-widget_message_wrap">
<div class="tgme_widget_message js-widget_message" data-post="wublockchainenglish/7">
<div class="tgme_widget_message_bubble">
<a class="tgme_widget_message_reply" href="https://t.me/wublockchainenglish/6">
<div class="tgme_widget_message_author accent_color"><span class="tgme_widget_message_author_name">Wu</span></div>
<div class="tgme_widget_message_text js-message_text" dir="auto">Old: infiniFi TGE <a href="https://x.com/a">rumor</a></div></a>
<div class="tgme_widget_message_text js-message_text" dir="auto">Update: <b>Saturn</b> Foundation sets <i class="emoji"><b>🚀</b></i> STRN TGE<br/>for Nov 20</div>
<div class="tgme_widget_message_footer"><a class="tgme_widget_message_date" href="https://t.me/wublockchainenglish/7"><time datetime="2026-10-21T09:15:00+00:00" class="time">09:15</time></a></div>
</div></div></div>
<div class="tgme_widget_message_wrap js-widget_message_wrap">
<div class="tgme_widget_message js-widget_message" data-post="wublockchainenglish/8"><div class="tgme_widget_message_bubble">
<a class="tgme_widget_message_reply" href="#"><div class="tgme_widget_message_text js-message_text">infiniFi airdrop quoted</div></a>
<a class="tgme_widget_message_photo_wrap" href="#"></a>
<a class="tgme_widget_message_date" href="#"><time datetime="2026-10-21T09:16:00+00:00">09:16</time></a></div></div></div>"""
        posts = tge.parse_tme(page)
        self.assertEqual(len(posts), 1)                    # media-only reply has no own text → skipped
        self.assertEqual(posts[0]["text"], "Update: Saturn Foundation sets 🚀 STRN TGE\nfor Nov 20")
        self.assertNotIn("infiniFi", posts[0]["text"])
        self.assertEqual(posts[0]["ts"], datetime(2026, 10, 21, 9, 15, tzinfo=UTC))
        today = date(2026, 10, 20)
        self.assertEqual(tge.parse_date("20.11", today), date(2026, 11, 20))
        self.assertEqual(tge.parse_date("05.01", today), date(2027, 1, 5))
        self.assertIsNone(tge.parse_date("31.02", today))
        self.assertIsNone(tge.parse_date("завтра", today))

    def test_watcher_seed_then_report(self):
        from email.utils import format_datetime
        extra = {"v": ""}
        rss = {"v": RSS.format(d1=format_datetime(T0 - timedelta(days=30)), d_old=format_datetime(T0 - timedelta(days=200)))}
        tme = {"v": "<html>" + TME.split('<div class="tgme_widget_message_wrap')[0] + "</html>"}  # no posts yet
        bing = {"v": BING.format(d=format_datetime(T0 - timedelta(days=30)))}
        sess = FakeSession({"https://news.google.com/": lambda: rss["v"], "https://t.me/s/": lambda: tme["v"] + "tgme_widget_message",
                            "https://www.bing.com/": lambda: bing["v"],
                            "https://docs.x.io/sitemap.xml": SITEMAP_INDEX,
                            "https://docs.x.io/sitemap-pages.xml": lambda: SITEMAP_PAGES.format(extra=extra["v"])})
        st = fresh_state(T0 - timedelta(days=30))
        # 1) first pass is silent: everything that exists now is remembered
        msgs = tge.Watcher(self.cfg, st, T0, sess).run()
        self.assertEqual(msgs, [])
        self.assertTrue(st["tge"]["seeded"]["gnews:infinifi"] and st["tge"]["seeded"]["docs:infinifi"]
                        and st["tge"]["seeded"]["bing:infinifi"])
        # 2) within check_hours nothing is fetched
        rss["v"] = RSS.format(d1=format_datetime(T0 + timedelta(hours=1)), d_old=format_datetime(T0 - timedelta(days=200)))
        self.assertEqual(tge.Watcher(self.cfg, st, T0 + timedelta(hours=1), sess).run(), [])
        # 3) new items: same-story duplicates collapse, non-TGE and stale items are dropped
        rss["v"] = RSS.format(d1=format_datetime(T0 + timedelta(hours=2)), d_old=format_datetime(T0 - timedelta(days=200))) \
            .replace("CBMi-a1", "CBMi-b1").replace("CBMi-a2", "CBMi-b2").replace("CBMi-a4", "CBMi-b4") \
            .replace("sets INFI TGE for November 18", "confirms INFI TGE date: November 18")
        tme["v"] = TME
        bing["v"] = BING.format(d=format_datetime(T0 + timedelta(hours=2))).replace("Reveals INFI", "Confirms INFI TGE and")
        extra["v"] = ("<url><loc>https://docs.x.io/infi/tokenomics</loc></url><url><loc>https://docs.x.io/vaults</loc></url>"
                      "<url><loc>https://docs.x.io/dev-docs/tokens/new-receipt-token</loc></url>")
        msgs = tge.Watcher(self.cfg, st, T0 + timedelta(hours=3), sess).run()
        self.assertEqual(len(msgs), 2)    # infiniFi (news + docs) and Saturn (Telegram)
        m = next(x for x in msgs if "infiniFi" in x)
        self.assertIn("TGE / airdrop · infiniFi", m)
        self.assertEqual(m.count("confirms INFI TGE date"), 1)
        self.assertIn("📅 у тексті: <b>November 18</b>", m)
        self.assertIn("/infi/tokenomics", m)
        self.assertIn("https://www.kucoin.com/news/flash/infinifi-reveals", m)   # Bing, посилання розгорнуте
        self.assertNotIn("Tokenomics 101", m)
        self.assertNotIn("/vaults", m)
        self.assertNotIn("new-receipt-token", m)                      # docs_ignore: /dev-docs/
        self.assertNotIn("Blend", m)
        self.assertNotIn("last year", m)
        # Telegram: strict aliases — «Saturn V rocket» is not the Saturn protocol
        sat = next(x for x in msgs if "· Saturn" in x)
        self.assertIn("Nov 20, 2026", sat)
        self.assertNotIn("rocket", sat)
        self.assertIn("Snapshot сезону 2", sat)
        # 4) the same story re-posted under new ids is not sent again
        tme["v"] = TME.replace("26490", "26500").replace("26491", "26501")
        self.assertEqual(tge.Watcher(self.cfg, st, T0 + timedelta(hours=6), sess).run(), [])
        # /tge shows the findings
        txt = tge.status_text(self.cfg, st, T0 + timedelta(hours=6))
        self.assertIn("confirms INFI TGE date", txt)
        self.assertIn("STRN", txt)

    def test_telegram_runs_more_often_and_exclude(self):
        cfg = load_config()
        cfg["tge"]["projects"] = {"unitas": dict(cfg["tge"]["projects"]["unitas"], docs_sitemap=None)}
        cfg["tge"]["telegram_channels"] = ["ch"]
        calls = {"news": 0, "tg": 0}
        post = {"v": ""}

        def news():
            calls["news"] += 1
            return '<?xml version="1.0"?><rss><channel></channel></rss>'

        def tg():
            calls["tg"] += 1
            return post["v"] + "tgme_widget_message"
        sess = FakeSession({"https://news.google.com/": news, "https://www.bing.com/": news, "https://t.me/s/": tg})
        st = fresh_state(T0)
        tge.Watcher(cfg, st, T0, sess).run()
        tge.Watcher(cfg, st, T0 + timedelta(minutes=15), sess).run()
        tge.Watcher(cfg, st, T0 + timedelta(minutes=30), sess).run()
        self.assertEqual(calls, {"news": 3, "tg": 3})        # новини раз на 2 год (Google + 2 Bing), Telegram щозапуску
        post["v"] = TME.replace("Saturn Foundation: <b>STRN</b> TGE and Season 2 snapshot on Nov 20, 2026.",
                                "Johnny Unitas jersey airdrop to Colts fans")
        self.assertEqual(tge.Watcher(cfg, st, T0 + timedelta(minutes=45), sess).run(), [])
        post["v"] = TME.replace("Saturn Foundation: <b>STRN</b> TGE and Season 2 snapshot on Nov 20, 2026.",
                                "Unitas Labs 宣布 $UP 第二季空投快照").replace("26490", "26600")
        msgs = tge.Watcher(cfg, st, T0 + timedelta(hours=1), sess).run()
        self.assertEqual(len(msgs), 1)
        self.assertIn("Unitas Labs 宣布", msgs[0])

    def test_source_failure_alert_once_and_recovery(self):
        self.cfg["tge"]["projects"] = {"tori": dict(load_config()["tge"]["projects"]["tori"], news_query=None,
                                                    bing_queries=None)}
        self.cfg["tge"]["telegram_channels"] = []
        st = fresh_state(T0)
        bad = FakeSession({"https://docs.tori.finance/": RuntimeError("HTTP 503")})
        out = []
        for i in range(7):
            out += tge.Watcher(self.cfg, st, T0 + timedelta(hours=2 * i), bad).run()
        self.assertEqual(len(out), 1)
        self.assertIn("docs:tori", out[0])
        good = FakeSession({"https://docs.tori.finance/": SITEMAP_PAGES.format(extra="")})
        out = tge.Watcher(self.cfg, st, T0 + timedelta(hours=20), good).run()
        self.assertEqual(len(out), 1)
        self.assertIn("знову працює", out[0])

    def test_tge_command_and_reminders(self):
        self.cfg["tge"]["projects"] = {k: dict(v, news_query=None, docs_sitemap=None, bing_queries=None)
                                       for k, v in load_config()["tge"]["projects"].items()}
        self.cfg["tge"]["telegram_channels"] = []
        st = fresh_state(T0)
        now = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)   # 11:00 Kyiv
        reply = tge.cmd_tge(self.cfg, st, now, "strn 20.11.2026")
        self.assertIn("20.11.2026", reply)
        self.assertIn("через 19 дн.", reply)
        self.assertEqual(st["tge"]["dates"]["saturn"], "2026-11-20")
        self.assertIn("Не знаю", tge.cmd_tge(self.cfg, st, now, "solana 2026-11-01"))
        run = lambda d: tge.Watcher(self.cfg, st, d, FakeSession({})).run()
        self.assertEqual(run(now), [])
        r = run(datetime(2026, 11, 13, 9, 0, tzinfo=UTC))
        self.assertEqual(len(r), 1)
        self.assertIn("через <b>7 дн.</b>", r[0])
        self.assertEqual(run(datetime(2026, 11, 13, 12, 0, tzinfo=UTC)), [])
        self.assertIn("через <b>1 дн.</b>", run(datetime(2026, 11, 19, 9, 0, tzinfo=UTC))[0])
        r = run(datetime(2026, 11, 20, 6, 0, tzinfo=UTC))
        self.assertIn("Сьогодні TGE Saturn", r[0])
        self.assertIn("фішинг", r[0])
        self.assertEqual(run(datetime(2026, 11, 20, 9, 0, tzinfo=UTC)), [])
        # a date set 3 days ahead doesn't produce a «7 days» reminder afterwards
        tge.cmd_tge(self.cfg, st, now, "infinifi 2026-11-04")
        self.assertEqual(run(datetime(2026, 11, 1, 12, 0, tzinfo=UTC)), [])
        # digest + clearing
        lines = "\n".join(tge.digest_lines(self.cfg, st, now))
        self.assertIn("Saturn: TGE <b>20.11</b> (через 19 дн.)", lines)
        self.assertIn("Unitas: TGE відбувся 13.03.2026", lines)
        self.assertIn("прибрано", tge.cmd_tge(self.cfg, st, now, "saturn off"))
        self.assertNotIn("saturn", st["tge"]["dates"])
        self.assertIn("TGE / airdrop", commands.dispatch(
            commands.Ctx(cfg=self.cfg, state=st, tg=Telegram("t", "1", dry_run=True), now=now), "/tge"))


if __name__ == "__main__":
    unittest.main()
