"""CLI entry point.

  python -m bot.run monitor            # one monitoring pass (GitHub Actions every 30 min)
  python -m bot.run weekly [--force digest|reminder]
  python -m bot.run selftest           # check every data source, print + send to Telegram
  python -m bot.run status             # print /status to the console (no Telegram needed)
  python -m bot.run loop               # long-running daemon (VPS alternative)
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import traceback

from . import __version__, fmt, sources
from .alerts import AlertEngine, warmup_note
from .commands import Ctx, handle_updates, status_text
from .config import load_config, require_telegram
from .evm import Chain, mask_url, same_addr
from .metrics import collect
from .storage import append_history, iso, load_history, load_state, save_state, utcnow
from .telegram import Telegram
from .weekly import run_weekly


def make_ctx(cfg: dict, dry_run: bool = False) -> Ctx:
    state = load_state(cfg["files"]["state"])
    tg = Telegram(cfg["telegram"]["token"], cfg["telegram"]["chat_id"], dry_run=dry_run)
    return Ctx(cfg=cfg, state=state, tg=tg, now=utcnow())


# --------------------------------------------------------------------------
def monitor_pass(ctx: Ctx, process_commands: bool = True) -> None:
    cfg, state = ctx.cfg, ctx.state
    first_run = not state.get("started_at")
    if first_run:
        state["started_at"] = iso(ctx.now)

    ctx.metrics, ctx.errors = collect(cfg)
    ctx.history = load_history(cfg["files"]["history"])
    engine = AlertEngine(cfg, state, ctx.now)
    messages = engine.run(ctx.metrics, ctx.history, ctx.errors)

    if process_commands:
        try:
            handle_updates(ctx)
        except Exception as e:
            print(f"[warn] getUpdates failed: {e}", file=sys.stderr)

    if first_run:
        note = warmup_note(engine) or ""
        ctx.tg.send(f"🤖 Бот запущено (v{__version__}). {note}\n\n" + status_text(ctx))
    for msg in messages:
        ctx.tg.send(msg)

    ctx.history = append_history(cfg["files"]["history"], ctx.metrics, ctx.now,
                                 int(cfg["files"].get("history_keep_days", 10)))
    state["last_metrics"] = ctx.metrics
    state["last_run"] = iso(ctx.now)
    print(f"[monitor] {iso(ctx.now)} metrics={sum(v is not None for v in ctx.metrics.values())}/"
          f"{len(ctx.metrics)} alerts={len(messages)} errors={list(ctx.errors)}")


def cmd_monitor(args) -> int:
    cfg = load_config()
    require_telegram(cfg)
    ctx = make_ctx(cfg)
    monitor_pass(ctx)
    save_state(cfg["files"]["state"], ctx.state)
    return 0


def cmd_weekly(args) -> int:
    cfg = load_config()
    require_telegram(cfg)
    ctx = make_ctx(cfg)
    force = args.force if args.force in ("digest", "reminder") else None
    result = run_weekly(ctx, force=force)
    try:
        handle_updates(ctx)
    except Exception as e:
        print(f"[warn] getUpdates failed: {e}", file=sys.stderr)
    if ctx.metrics is not None:
        ctx.state["last_metrics"] = ctx.metrics
    save_state(cfg["files"]["state"], ctx.state)
    print(f"[weekly] {iso(ctx.now)} local={ctx.local_now():%a %H:%M} result={result}")
    return 0


def cmd_status(args) -> int:
    cfg = load_config()
    ctx = make_ctx(cfg, dry_run=True)
    import html
    text = status_text(ctx)
    print(html.unescape(re.sub(r"<[^>]+>", "", text)))
    return 0


# --------------------------------------------------------------------------
def cmd_selftest(args) -> int:
    cfg = load_config()
    src = cfg["sources"]
    lines: list[str] = [f"🧪 Self-test defi-monitor v{__version__}"]
    ok_all = True

    def check(label, fn):
        nonlocal ok_all
        try:
            res = fn()
            lines.append(f"✅ {label}: {res}")
        except Exception as e:
            ok_all = False
            lines.append(f"❌ {label}: {e}")

    dl = src["defillama"]
    for key, slug in dl["slugs"].items():
        def _dl(slug=slug):
            info = sources.defillama_protocol_info(dl["base_url"], slug)
            tvl = sources.defillama_tvl(dl["base_url"], slug)
            chains = {k: round(v / 1e6, 2) for k, v in info["chains"].items() if isinstance(v, (int, float))}
            return f"{info['name']} TVL ${tvl / 1e6:.2f}M, по мережах (M$): {chains}"
        check(f"DefiLlama {key} (slug={slug})", _dl)

    eth = Chain(src["rpc"]["ethereum"])
    bsc = Chain(src["rpc"]["bsc"])
    E, B = src["ethereum"], src["bsc"]
    check("Ethereum RPC", lambda: f"блок {eth.block_number()} через {mask_url(eth.urls[0])}")
    check("BSC RPC", lambda: f"блок {bsc.block_number()} через {mask_url(bsc.urls[0])}")

    def _curve():
        c = sources.curve_pool_state(eth, E["curve_usdat_pool"], E["usdat_token"], src["probe_amounts"]["usdat"])
        return (f"{'/'.join(c['symbols'])}: ціна USDat {c['price']:.5f}, частка USDat {c['share_pct']:.1f}%, "
                f"ліквідність ${c['liquidity_usd'] / 1e6:.2f}M")
    check("Curve USDC/USDat", _curve)

    def _curve_iusd():
        c = sources.curve_pool_state(eth, E["curve_iusd_pool"], E["iusd_token"], src["probe_amounts"]["iusd"])
        return f"{'/'.join(c['symbols'])}: ціна iUSD {c['price']:.5f}, ліквідність ${c['liquidity_usd']:,.0f}"
    check("Curve iUSD/USDC", _curve_iusd)

    def _pcs():
        p = sources.v3_pool_state(bsc, B["pancake_usdu_pool"], B["usdu_token"])
        return f"{'/'.join(p['symbols'])}: ціна USDu {p['price']:.5f}, ліквідність ${p['liquidity_usd']:,.0f}"
    check("PancakeSwap V3 USDu/USDT", _pcs)

    def _gt():
        g = sources.geckoterminal_pool(src["geckoterminal"]["base_url"], "bsc", B["pancake_usdu_pool"], B["usdu_token"])
        return f"{g['name']}: ціна {g['price']:.5f}, reserve ${g['liquidity_usd']:,.0f} (резервне джерело)"
    check("GeckoTerminal USDu", _gt)

    def _inf():
        i = sources.infinifi_state(eth, E["infinifi_gateway"], int(E.get("liusd_bucket_weeks", 13)))
        tok_ok = i.get("liusd_share_token") and same_addr(i["liusd_share_token"], E["liusd13w_token"])
        res = (f"TVL on-chain {fmt.money(i['tvl_onchain'])}, ліквідний резерв {fmt.money(i['liquid_reserve'])} "
               f"({fmt.pct(i['liquid_reserve_pct'])}), черга {fmt.num(i['queue_length'])} заявок / "
               f"{fmt.num(i['queue_iusd'])} iUSD, rate liUSD-13w {fmt.rate(i['liusd_rate'])}, "
               f"курс погашення iUSD {fmt.price(i['iusd_redeem_rate'], 5)}, unaccrued {fmt.num(i['unaccrued_yield'])} iUSD; "
               f"shareToken(13) == liUSD-13w з docs: {'так' if tok_ok else 'НІ — перевірте адреси'}")
        if i["errors"]:
            res += " | не прочитано: " + "; ".join(f"{k}: {v}" for k, v in i["errors"].items())
        return res
    check("InfiniFi on-chain", _inf)

    tg_ok = bool(cfg["telegram"]["token"] and cfg["telegram"]["chat_id"])
    if tg_ok:
        tg = Telegram(cfg["telegram"]["token"], cfg["telegram"]["chat_id"])
        check("Telegram getMe (токен)", lambda: f"бот @{tg.get_me().get('username')}")
        check("Telegram sendMessage (chat id)", lambda: (tg.send("🧪 Self-test: перевірка chat id — ок", silent=True), "надіслано")[1])
    else:
        lines.append("⚠️ Telegram: секрети не задані — повідомлення не надсилаю")

    lines.append("РЕЗУЛЬТАТ: " + ("усе працює ✅" if ok_all else "є помилки ❌ — див. вище"))
    report = "\n".join(lines)
    # console copy hides the bot username: Actions logs of a public repo are visible to everyone
    print(re.sub(r"бот @\S+", "бот @…", report))
    if tg_ok:
        try:
            Telegram(cfg["telegram"]["token"], cfg["telegram"]["chat_id"]).send(
                report.replace("<", "&lt;").replace(">", "&gt;"))
        except Exception as e:
            print(f"[warn] Telegram send failed: {e}")
    return 0 if ok_all else 1


# --------------------------------------------------------------------------
def cmd_loop(args) -> int:
    """Daemon mode for a VPS: instant command replies, monitor every N minutes."""
    cfg = load_config()
    require_telegram(cfg)
    interval = float(args.interval) * 60
    last_monitor = 0.0
    while True:
        try:
            ctx = make_ctx(cfg)
            if time.time() - last_monitor >= interval:
                monitor_pass(ctx, process_commands=False)
                last_monitor = time.time()
            run_weekly(ctx)
            handle_updates(ctx, long_poll_seconds=25)
            save_state(cfg["files"]["state"], ctx.state)
        except KeyboardInterrupt:
            return 0
        except Exception:
            traceback.print_exc()
            time.sleep(30)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m bot.run")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("monitor").set_defaults(fn=cmd_monitor)
    w = sub.add_parser("weekly")
    w.add_argument("--force", default="", help="digest | reminder")
    w.set_defaults(fn=cmd_weekly)
    sub.add_parser("selftest").set_defaults(fn=cmd_selftest)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    lp = sub.add_parser("loop")
    lp.add_argument("--interval", default="30", help="хвилин між перевірками")
    lp.set_defaults(fn=cmd_loop)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
