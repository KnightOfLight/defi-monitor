"""Friday 10:00 (Kyiv) digest + Saturday reminder when points were not logged."""
from __future__ import annotations

from datetime import timedelta

from . import fmt, tge
from .commands import LOG_EXAMPLE, POINT_LABELS, Ctx
from .storage import (POINTS_COLUMNS, journal_row_from_metrics, last_row_with_points, load_journal, save_journal,
                      to_float, value_ago)
from .telegram import esc


def _due(ctx: Ctx, weekday: int) -> bool:
    """True when local time is <weekday> at the configured hour (or the next one —
    GitHub cron can run late, and the UTC schedule covers both DST offsets)."""
    local = ctx.local_now()
    hour = int(ctx.cfg["weekly"]["hour"])
    return local.weekday() == weekday and hour <= local.hour <= hour + 2


def run_weekly(ctx: Ctx, force: str | None = None) -> str:
    W = ctx.cfg["weekly"]
    st = ctx.state.setdefault("weekly", {})
    today = ctx.local_now().date().isoformat()

    if force == "digest" or (not force and _due(ctx, int(W["weekday"])) and st.get("digest_sent_date") != today):
        send_digest(ctx, today)
        st["digest_sent_date"] = today
        return "digest"

    pending = st.get("pending_points_date")
    if force == "reminder" or (not force and pending and _due(ctx, int(W["reminder_weekday"]))
                               and st.get("reminder_sent_date") != today):
        if pending and _still_pending(ctx, pending):
            send_reminder(ctx, pending)
            st["reminder_sent_date"] = today
            return "reminder"
        st["pending_points_date"] = None
        return "nothing-pending"
    return "idle"


def _still_pending(ctx: Ctx, date_str: str) -> bool:
    rows = load_journal(ctx.cfg["files"]["journal"])
    for r in reversed(rows):
        if r["date"] == date_str:
            return not any(r.get(c) for c in POINTS_COLUMNS)
    return False


# --------------------------------------------------------------------------
def send_digest(ctx: Ctx, today: str) -> None:
    m = ctx.get_metrics()
    h = ctx.get_history()
    path = ctx.cfg["files"]["journal"]
    rows = load_journal(path)

    # reference row for week-over-week: latest row at least 6 days old; history as fallback
    week_ago = (ctx.local_now() - timedelta(days=6)).date().isoformat()
    older = [r for r in rows if r["date"] <= week_ago]
    prev_row = older[-1] if older else None

    def wk(col: str) -> float | None:
        cur = m.get(col)
        ref = to_float(prev_row.get(col)) if prev_row else None
        if ref is None:
            ref = value_ago(h, col, ctx.now, 24 * 7, tolerance_hours=18)
        return fmt.change_pct(cur, ref)

    # append / replace today's row
    new_row = journal_row_from_metrics(today, m)
    if rows and rows[-1]["date"] == today:
        for k, v in new_row.items():
            if k not in POINTS_COLUMNS and k != "note" and v != "":
                rows[-1][k] = v
    else:
        rows.append(new_row)
    save_journal(path, rows)
    ctx.state.setdefault("weekly", {})["pending_points_date"] = today

    local = ctx.local_now().strftime("%d.%m.%Y")
    lines = [f"📅 <b>Тижневий дайджест</b> · {local}", ""]
    lines.append("<b>TVL (зміна за тиждень)</b>")
    for p, name in (("saturn", "Saturn"), ("unitas", "Unitas"), ("infinifi", "InfiniFi"), ("tori", "Tori")):
        d = wk(f"tvl_{p}")
        lines.append(f"{name}: <b>{fmt.money(m[f'tvl_{p}'])}</b> {fmt.arrow(d)} {fmt.pct(d, signed=True)}")
    lines.append("")
    lines.append("<b>Ціни</b>")
    for a, name in (("usdat", "USDat"), ("usdu", "USDu"), ("iusd", "iUSD"), ("trusd", "trUSD")):
        d = wk(f"price_{a}")
        lines.append(f"{name}: <b>{fmt.price(m[f'price_{a}'])}</b> {fmt.arrow(d)} {fmt.pct(d, 2, signed=True)}")
    lines.append("")
    d = wk("liusd13w_rate")
    lines.append(f"Rate liUSD-13w: <b>{fmt.rate(m['liusd13w_rate'])}</b> {fmt.arrow(d)} {fmt.pct(d, 3, signed=True)}")
    lines.append(f"Резерв InfiniFi: <b>{fmt.money(m['infinifi_liquid_reserve'])}</b> "
                 f"({fmt.pct(m['infinifi_liquid_reserve_pct'])} TVL); черга: {fmt.num(m['infinifi_queue_iusd'])} iUSD")
    lines.append("")
    days = m.get("pendle_days_to_expiry")
    left = "н/д" if days is None else ("погашено" if days <= 0 else f"до погашення {days:.0f} дн.")
    lines.append(f"<b>Pendle trUSD</b> ({left})")
    lines.append(f"PT-trUSD: <b>{fmt.rate(m.get('pendle_pt_price'))}</b> trUSD · implied APY "
                 f"<b>{fmt.pct(m.get('pendle_implied_apy'), 2)}</b> · ліквідність <b>{fmt.money(m.get('pendle_liquidity'))}</b> "
                 f"{fmt.pct(wk('pendle_liquidity'), signed=True)}")
    d = wk("strusd_rate")
    lines.append(f"strUSD: <b>{fmt.rate(m.get('strusd_rate'))}</b> {fmt.arrow(d)} {fmt.pct(d, 3, signed=True)} · "
                 f"у strUSD {fmt.pct(m.get('strusd_buffer_pct'))} усього trUSD")
    lines.append("")

    tl = tge.digest_lines(ctx.cfg, ctx.state, ctx.now)
    if tl:
        lines += tl + [""]

    # points: last two logged rows
    last = last_row_with_points(rows, before_index=len(rows) - 1)
    lines.append("<b>Поінти (останній запис)</b>")
    if last:
        idx = rows.index(last)
        before = last_row_with_points(rows, before_index=idx)
        for k in POINTS_COLUMNS:
            cur, ref = to_float(last.get(k)), to_float(before.get(k)) if before else None
            delta = f" ({'+' if cur - ref >= 0 else ''}{fmt.num(cur - ref)} з {esc(before['date'])})" \
                if cur is not None and ref is not None else ""
            lines.append(f"{POINT_LABELS[k]}: <b>{fmt.num(cur)}</b>{delta}")
        lines.append(f"<i>запис від {esc(last['date'])}</i>")
    else:
        lines.append("ще немає записів")
    lines.append("")
    lines.append("✍️ Запишіть сьогоднішні поінти (app.saturn.credit → Points, app.unitas.so → Points, "
                 "app.infinifi.xyz → Points, Cores — app.tori.finance):")
    lines.append(f"<code>{LOG_EXAMPLE}</code>")
    lines.append("Відповідь допишеться в рядок журналу за сьогодні. Якщо не відповісте — нагадаю в суботу.")
    if ctx.errors:
        lines.append("")
        lines.append("⚠️ Частина джерел не відповіла: " + ", ".join(f"<code>{esc(k)}</code>" for k in ctx.errors))
    ctx.tg.send("\n".join(lines))


def send_reminder(ctx: Ctx, pending_date: str) -> None:
    ctx.tg.send(
        f"🔔 <b>Нагадування</b>: поінти за {esc(pending_date)} ще не записані.\n"
        f"Відповідайте: <code>{LOG_EXAMPLE}</code>"
    )
