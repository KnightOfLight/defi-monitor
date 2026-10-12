"""Offline tests — run with:  python -m unittest -v tests.test_offline"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot import alerts, commands, evm, sources, storage, weekly  # noqa: E402
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


# --------------------------------------------------------------------------
def base_metrics(**over) -> dict:
    m = {k: None for k in storage.METRIC_KEYS}
    m.update({
        "tvl_saturn": 152e6, "tvl_unitas": 53e6, "tvl_infinifi": 52e6,
        "price_usdat": 0.9990, "price_usdu": 0.9980, "price_iusd": 0.9985,
        "liusd13w_rate": 1.08, "infinifi_tvl_onchain": 52.4e6, "infinifi_liquid_reserve": 4.1e6,
        "infinifi_liquid_reserve_pct": 7.8, "infinifi_queue_length": 0, "infinifi_queue_iusd": 0,
        "iusd_redeem_rate": 1.0, "infinifi_unaccrued_yield": 12.0,
        "curve_usdat_share": 52.0, "curve_pool_liquidity": 6.7e6, "curve_usdc_balance": 3.2e6,
        "curve_usdat_balance": 3.5e6, "usdu_pool_liquidity": 7e5, "curve_iusd_liquidity": 1e4,
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

    def test_curve_share_and_liquidity(self):
        st = fresh_state(T0 - timedelta(days=30))
        hist = [{"ts": T0 - timedelta(days=7), **base_metrics(curve_pool_liquidity=10e6)}]
        msgs = self.run_engine(st, base_metrics(curve_usdat_share=72.0, curve_pool_liquidity=6.5e6), T0, hist)
        self.assertEqual(len(msgs), 2)
        self.assertTrue(any("Частка USDat" in x and "УВАГА" in x for x in msgs))
        self.assertTrue(any("Ліквідність пулу" in x and "35.0%" in x for x in msgs))
        msgs = self.run_engine(st, base_metrics(curve_usdat_share=81.0, curve_pool_liquidity=6.5e6), T0 + timedelta(hours=1), hist)
        self.assertEqual(len(msgs), 1)
        self.assertIn("ТРИВОГА", msgs[0])

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

    def test_digest_then_log_then_reminder(self):
        friday = datetime(2026, 10, 23, 7, 10, tzinfo=UTC)  # 10:10 Kyiv (EEST)
        c = self.ctx(friday)
        self.assertEqual(weekly.run_weekly(c), "digest")
        self.assertIn("Тижневий дайджест", self.tg.sent[-1])
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
        reply = commands.dispatch(sun, "/log saturn 100 unitas 200 infinifi 300")
        self.assertIn("2026-10-23", reply)
        rows = storage.load_journal(self.cfg["files"]["journal"])
        self.assertEqual(rows[0]["saturn_points"], "100")
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
        self.assertIn("Статус", commands.dispatch(nxt, "/status"))
        self.assertIn("Пороги", commands.dispatch(nxt, "/thresholds"))

    def test_winter_time_hour_selection(self):
        # January: Kyiv = UTC+2, so 07:00 UTC is 09:00 (idle) and 08:00 UTC is 10:00 (digest)
        fri7 = self.ctx(datetime(2027, 1, 15, 7, 2, tzinfo=UTC))
        self.assertEqual(weekly.run_weekly(fri7), "idle")
        fri8 = commands.Ctx(cfg=self.cfg, state=fri7.state, tg=self.tg, now=datetime(2027, 1, 15, 8, 2, tzinfo=UTC))
        fri8.metrics, fri8.errors, fri8.history = base_metrics(), {}, []
        self.assertEqual(weekly.run_weekly(fri8), "digest")

    def test_history_roundtrip(self):
        p = self.cfg["files"]["history"]
        rows = storage.append_history(p, base_metrics(), T0 - timedelta(days=8), keep_days=10)
        rows = storage.append_history(p, base_metrics(tvl_saturn=1.0), T0, keep_days=10)
        self.assertEqual(len(rows), 2)
        self.assertEqual(storage.value_ago(rows, "tvl_saturn", T0, 24 * 7, 12), None)  # 8 days ago is out of tolerance
        self.assertEqual(storage.value_ago(rows, "tvl_saturn", T0, 24 * 8, 12), 152e6)
        rows = storage.append_history(p, base_metrics(), T0 + timedelta(days=3), keep_days=10)
        self.assertEqual(len(rows), 2)  # 11-day-old row pruned


if __name__ == "__main__":
    unittest.main()
