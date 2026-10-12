"""Telegram commands. Updates are read with getUpdates on every run
(GitHub Actions) or by long-polling (loop mode)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import fmt
from .alerts import status_levels
from .metrics import collect
from .storage import (POINTS_COLUMNS, journal_row_from_metrics, last_row_with_points, load_history,
                      load_journal, save_journal, to_float)
from .telegram import Telegram, esc

POINT_ALIASES = {
    "saturn": "saturn_points", "sat": "saturn_points", "strn": "saturn_points", "usdat": "saturn_points",
    "unitas": "unitas_points", "uni": "unitas_points", "up": "unitas_points", "usdu": "unitas_points",
    "infinifi": "infinifi_points", "inf": "infinifi_points", "infi": "infinifi_points", "iusd": "infinifi_points",
    "liusd": "infinifi_points",
}
POINT_LABELS = {"saturn_points": "Saturn", "unitas_points": "Unitas", "infinifi_points": "InfiniFi"}

HELP = """<b>Команди бота</b>
/status — поточні значення всіх метрик і рівень відносно порогів
/log saturn 123456 unitas 7890 infinifi 4567 — записати поінти (можна будь-якого дня; пропущені протоколи можна не вказувати)
/note текст — дописати примітку до останнього рядка журналу
/export — надіслати журнал CSV + підсумок за тиждень і місяць
/thresholds — показати пороги алертів
/help — ця довідка

Бот запускається за розкладом, тому відповідь на команду приходить при наступному запуску (до 30 хв на GitHub Actions)."""


@dataclass
class Ctx:
    cfg: dict
    state: dict
    tg: Telegram
    now: datetime
    metrics: dict | None = None
    errors: dict = field(default_factory=dict)
    history: list | None = None

    # lazy metrics ------------------------------------------------------
    def get_metrics(self) -> dict:
        if self.metrics is None:
            self.metrics, self.errors = collect(self.cfg)
        return self.metrics

    def get_history(self) -> list:
        if self.history is None:
            self.history = load_history(self.cfg["files"]["history"])
        return self.history

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.cfg.get("timezone", "Europe/Kyiv"))

    def local_now(self) -> datetime:
        return self.now.astimezone(self.tz)


# --------------------------------------------------------------------------
# Dispatcher
# --------------------------------------------------------------------------
def handle_updates(ctx: Ctx, long_poll_seconds: int = 0) -> int:
    """Fetch new updates, run commands from the allowed chat, advance offset.
    Returns number of commands handled."""
    offset = int(ctx.state.get("telegram_offset") or 0)
    updates = ctx.tg.get_updates(offset, timeout=long_poll_seconds)
    handled = 0
    for u in updates:
        ctx.state["telegram_offset"] = max(ctx.state.get("telegram_offset", 0), u["update_id"] + 1)
        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if not text or chat_id != ctx.tg.chat_id:
            continue  # ignore strangers silently
        try:
            reply = dispatch(ctx, text)
        except Exception as e:  # never let one bad command kill the run
            reply = f"❌ Помилка під час виконання команди: <code>{esc(e)}</code>"
        if reply:
            ctx.tg.send(reply)
        handled += 1
    return handled


def dispatch(ctx: Ctx, text: str) -> str | None:
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower().split("@", 1)[0]
    arg = parts[1].strip() if len(parts) > 1 else ""
    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/status":
        return status_text(ctx)
    if cmd == "/log":
        return cmd_log(ctx, arg)
    if cmd == "/note":
        return cmd_note(ctx, arg)
    if cmd == "/export":
        return cmd_export(ctx)
    if cmd == "/thresholds":
        return thresholds_text(ctx.cfg)
    if cmd.startswith("/"):
        return f"Невідома команда {esc(cmd)}.\n\n{HELP}"
    if parse_points(text):            # "saturn 123 unitas 45" без /log — теж приймаємо
        return cmd_log(ctx, text)
    return None


# --------------------------------------------------------------------------
# /status
# --------------------------------------------------------------------------
def status_text(ctx: Ctx) -> str:
    m = ctx.get_metrics()
    h = ctx.get_history()
    lv = status_levels(ctx.cfg, m, h, ctx.state, ctx.now)
    local = ctx.local_now().strftime("%d.%m.%Y %H:%M")
    lines = [f"📊 <b>Статус</b> · {local} (Київ)", ""]
    lines.append("<b>Ціни (найглибший пул)</b>")
    lines.append(f"USDat: <b>{fmt.price(m['price_usdat'])}</b> — {lv['price_usdat']}")
    lines.append(f"USDu: <b>{fmt.price(m['price_usdu'])}</b> — {lv['price_usdu']}")
    lines.append(f"iUSD: <b>{fmt.price(m['price_iusd'])}</b> — {lv['price_iusd']} "
                 f"(пул ~{fmt.money(m.get('curve_iusd_liquidity'), 0)}; курс погашення в протоколі "
                 f"{fmt.price(m.get('iusd_redeem_rate'))})")
    lines.append("")
    lines.append("<b>TVL (DefiLlama)</b>")
    for p, name in (("saturn", "Saturn"), ("unitas", "Unitas"), ("infinifi", "InfiniFi")):
        lines.append(f"{name}: <b>{fmt.money(m[f'tvl_{p}'])}</b> — {lv[f'tvl_{p}']}")
    lines.append("")
    lines.append("<b>Пул Curve USDC/USDat</b>")
    lines.append(f"Частка USDat: <b>{fmt.pct(m['curve_usdat_share'])}</b> — {lv['curve_usdat_share']}")
    lines.append(f"Ліквідність: <b>{fmt.money(m['curve_pool_liquidity'])}</b> — {lv['curve_pool_liquidity']}")
    lines.append(f"USDu/USDT (PancakeSwap V3): ліквідність {fmt.money(m.get('usdu_pool_liquidity'))}")
    lines.append("")
    lines.append("<b>InfiniFi (on-chain)</b>")
    lines.append(f"Rate liUSD-13w: <b>{fmt.rate(m['liusd13w_rate'])}</b>")
    lines.append(f"Ліквідний резерв: <b>{fmt.money(m['infinifi_liquid_reserve'])}</b> "
                 f"({fmt.pct(m['infinifi_liquid_reserve_pct'])} з {fmt.money(m['infinifi_tvl_onchain'])}) — {lv['infinifi_reserve']}")
    lines.append(f"Черга redemption: <b>{fmt.num(m['infinifi_queue_length'])}</b> заявок, "
                 f"{fmt.num(m['infinifi_queue_iusd'])} iUSD — {lv['infinifi_queue']}")
    uy = m.get("infinifi_unaccrued_yield")
    if uy is not None:
        lines.append(f"Unaccrued yield: {fmt.num(uy)} iUSD" + (" ⚠️ (від'ємний = незараховані збитки)" if uy < 0 else ""))
    lines.append("")
    lines.append(f"<i>Історія для порівнянь: {lv['_history_hours']} год.</i>")
    if ctx.errors:
        lines.append("")
        lines.append("<b>Помилки джерел</b>")
        for k, v in ctx.errors.items():
            lines.append(f"• <code>{esc(k)}</code>: {esc(v[:200])}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# /thresholds
# --------------------------------------------------------------------------
def thresholds_text(cfg: dict) -> str:
    A = cfg["alerts"]
    P, T, C, I = A["price"], A["tvl"], A["curve_pool"], A["infinifi"]
    ab = P["alarm_below"]
    return "\n".join([
        "<b>Пороги (config.yaml)</b>",
        f"Ціни: увага &lt; {P['warn_below']} на {P.get('warn_consecutive', 2)} перевірках поспіль; "
        f"тривога одразу: USDat &lt; {ab.get('usdat')}, USDu &lt; {ab.get('usdu')}, iUSD &lt; {ab.get('iusd')}",
        f"TVL: увага −{T['warn_drop_7d_pct']}% за 7 дн; тривога −{T['alarm_drop_24h_pct']}% за 24 год; "
        f"інфо Saturn &gt; {fmt.money(T['saturn_high_usd'], 0)}",
        f"Пул Curve: частка USDat увага &gt; {C['usdat_share_warn_pct']}%, тривога &gt; {C['usdat_share_alarm_pct']}%; "
        f"ліквідність −{C['liquidity_drop_7d_warn_pct']}% за 7 дн → увага",
        f"InfiniFi: резерв &lt; {I['reserve_warn_pct']}% TVL {I['reserve_warn_days']} дн поспіль → увага; "
        f"черга redemption &gt; {I['queue_alarm_hours']} год → тривога; будь-яке падіння rate liUSD-13w → тривога",
        f"Антиспам: повтор раз на {A['repeat_hours']} год; warm-up {A['warmup_days']} дн "
        f"(глушаться: {', '.join(A.get('warmup_suppress', []))})",
    ])


# --------------------------------------------------------------------------
# /log
# --------------------------------------------------------------------------
_NUM = re.compile(r"^[+-]?\d+(?:[.,]\d+)?$")
_THOUSANDS = re.compile(r"^[+-]?\d{1,3}(?:[,.\u00a0 ]\d{3})+$")   # 123,456 / 1.234.567 / 1 234


def _to_number(val: str) -> float | None:
    v = val.replace("_", "").replace("\u00a0", " ").strip()
    if _THOUSANDS.match(v):                      # роздільники тисяч
        return float(re.sub(r"[,. ]", "", v))
    v = v.replace(" ", "")
    if _NUM.match(v):                            # десятковий дріб: 1.5 або 1,5
        return float(v.replace(",", "."))
    return None


def parse_points(arg: str) -> dict[str, float]:
    tokens = re.split(r"[\s=:]+", arg.strip())
    out: dict[str, float] = {}
    i = 0
    while i < len(tokens) - 1:
        name = tokens[i].lower().lstrip("#")
        if name in POINT_ALIASES:
            raw = tokens[i + 1]
            j = i + 2
            # "7 890 123" набране з пробілами: доклеюємо групи рівно по 3 цифри
            while j < len(tokens) and re.fullmatch(r"\d{3}", tokens[j]) and re.fullmatch(r"[+-]?\d+", raw):
                raw += tokens[j]
                j += 1
            num = _to_number(raw)
            if num is not None:
                out[POINT_ALIASES[name]] = num
                i = j
                continue
        i += 1
    return out


def cmd_log(ctx: Ctx, arg: str) -> str:
    pts = parse_points(arg)
    if not pts:
        return ("Формат: <code>/log saturn 123456 unitas 7890 infinifi 4567</code>\n"
                "Можна вказати лише частину протоколів.")
    path = ctx.cfg["files"]["journal"]
    rows = load_journal(path)
    today = ctx.local_now().date().isoformat()
    pending = (ctx.state.get("weekly") or {}).get("pending_points_date")

    target_idx = None
    if pending:
        for i in range(len(rows) - 1, -1, -1):
            if rows[i]["date"] == pending and not any(rows[i].get(c) for c in POINTS_COLUMNS):
                target_idx = i
                break
    if target_idx is None:
        for i in range(len(rows) - 1, -1, -1):
            if rows[i]["date"] == today:
                target_idx = i
                break
    if target_idx is None:
        rows.append(journal_row_from_metrics(today, ctx.get_metrics()))
        target_idx = len(rows) - 1

    prev = last_row_with_points(rows, before_index=target_idx)
    row = rows[target_idx]
    for k, v in pts.items():
        row[k] = f"{v:.0f}" if float(v).is_integer() else f"{v}"
    save_journal(path, rows)
    ctx.state.setdefault("weekly", {})["pending_points_date"] = None

    lines = [f"✅ Поінти записано в рядок <b>{esc(row['date'])}</b>:"]
    for k in POINTS_COLUMNS:
        if k in pts:
            delta = ""
            if prev and to_float(prev.get(k)) is not None:
                d = pts[k] - to_float(prev[k])
                delta = f" ({'+' if d >= 0 else ''}{fmt.num(d)} з {esc(prev['date'])})"
            lines.append(f"• {POINT_LABELS[k]}: <b>{fmt.num(pts[k])}</b>{delta}")
    missing = [POINT_LABELS[k] for k in POINTS_COLUMNS if not row.get(k)]
    if missing:
        lines.append(f"Ще не вказано: {', '.join(missing)} — можна дописати тією ж командою.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# /note
# --------------------------------------------------------------------------
def cmd_note(ctx: Ctx, arg: str) -> str:
    if not arg:
        return "Формат: <code>/note текст примітки</code>"
    path = ctx.cfg["files"]["journal"]
    rows = load_journal(path)
    if not rows:
        rows.append(journal_row_from_metrics(ctx.local_now().date().isoformat(), ctx.get_metrics()))
    row = rows[-1]
    note = arg.replace("\n", " ").replace(",", ";").strip()
    row["note"] = f"{row['note']}; {note}" if row.get("note") else note
    save_journal(path, rows)
    return f"📝 Примітку дописано до рядка <b>{esc(row['date'])}</b>:\n<i>{esc(row['note'])}</i>"


# --------------------------------------------------------------------------
# /export
# --------------------------------------------------------------------------
def _row_on_or_before(rows: list[dict], date_str: str) -> dict | None:
    cands = [r for r in rows if r["date"] <= date_str]
    return cands[-1] if cands else None


def cmd_export(ctx: Ctx) -> str:
    path = ctx.cfg["files"]["journal"]
    rows = load_journal(path)
    if not rows:
        return "Журнал поки порожній — перший рядок з'явиться після п'ятничного дайджесту або /log."
    latest = rows[-1]
    d_latest = datetime.fromisoformat(latest["date"])
    week = _row_on_or_before(rows, (d_latest - timedelta(days=7)).date().isoformat())
    month = _row_on_or_before(rows, (d_latest - timedelta(days=30)).date().isoformat())
    if week is latest:
        week = None
    if month is latest:
        month = None

    def line(label: str, col: str, kind: str) -> str:
        cur = to_float(latest.get(col))
        f = (lambda v: fmt.money(v)) if kind == "money" else (lambda v: fmt.price(v)) if kind == "price" \
            else (lambda v: fmt.rate(v)) if kind == "rate" else (lambda v: fmt.pct(v)) if kind == "pct" \
            else (lambda v: fmt.num(v))
        parts = [f"{label}: {f(cur)}"]
        for tag, ref in (("тижд", week), ("міс", month)):
            if ref is not None:
                rv = to_float(ref.get(col))
                if kind in ("money", "price", "rate"):
                    parts.append(f"{tag} {fmt.pct(fmt.change_pct(cur, rv), 2 if kind != 'money' else 1, signed=True)}")
                elif cur is not None and rv is not None:
                    parts.append(f"{tag} {'+' if cur - rv >= 0 else ''}{fmt.num(cur - rv)}")
        return " · ".join(parts)

    lines = [f"📤 Журнал: {len(rows)} рядків, {esc(rows[0]['date'])} → {esc(latest['date'])}",
             f"Порівняння: тиждень vs {esc(week['date']) if week else '—'}, місяць vs {esc(month['date']) if month else '—'}",
             "",
             line("Поінти Saturn", "saturn_points", "num"),
             line("Поінти Unitas", "unitas_points", "num"),
             line("Поінти InfiniFi", "infinifi_points", "num"),
             line("TVL Saturn", "tvl_saturn", "money"),
             line("TVL Unitas", "tvl_unitas", "money"),
             line("TVL InfiniFi", "tvl_infinifi", "money"),
             line("USDat", "price_usdat", "price"),
             line("USDu", "price_usdu", "price"),
             line("iUSD", "price_iusd", "price"),
             line("Rate liUSD-13w", "liusd13w_rate", "rate"),
             line("Резерв InfiniFi", "infinifi_liquid_reserve", "money"),
             line("Частка USDat у Curve", "curve_usdat_share", "pct"),
             line("Ліквідність Curve", "curve_pool_liquidity", "money"),
             ]
    notes = [f"{r['date']}: {r['note']}" for r in rows[-8:] if r.get("note")]
    if notes:
        lines += ["", "<b>Останні примітки</b>"] + [esc(n) for n in notes]
    summary = "\n".join(lines)
    ctx.tg.send_document(path, caption="journal.csv — завантажте у Claude для аналізу", filename="journal.csv")
    return summary
