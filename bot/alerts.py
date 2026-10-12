"""Threshold evaluation with anti-spam.

One alert id per signal; its level can be "warn", "alarm" or "info".
Rules (config: alerts.*):
  * new condition      -> one message
  * level changes      -> one message
  * condition persists -> repeat at most every `repeat_hours`
  * condition clears   -> one "back to normal" message
During warm-up (first `warmup_days`) groups listed in `warmup_suppress` are silent.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from . import fmt
from .storage import history_span_hours, iso, parse_iso, value_ago
from .telegram import esc

LEVEL_TAG = {"warn": "⚠️ <b>УВАГА</b>", "alarm": "🚨 <b>ТРИВОГА</b>", "info": "ℹ️ <b>ІНФО</b>"}
LEVEL_ORDER = {None: 0, "info": 1, "warn": 2, "alarm": 3}

ASSET_NAMES = {"usdat": "USDat", "usdu": "USDu", "iusd": "iUSD"}
ASSET_POOL = {"usdat": "Curve USDC/USDat", "usdu": "PancakeSwap V3 USDu/USDT", "iusd": "Curve iUSD/USDC (тонкий пул!)"}
ASSET_ADVICE = {
    "usdat": "Перевірте пул Curve: при частці USDat > 80% — вихід одностороннім виходом в USDC "
             "або погашення 1:1 через app.saturn.credit (якщо гаманець допущений).",
    "usdu": "Перевірте @UnitasLabs і новини про custody/OES. Без пояснення за 2–3 год — погасити через "
            "app.unitas.so або продати на PancakeSwap.",
    "iusd": "Пул дуже тонкий — звірте з курсом погашення в протоколі (/status) і rate liUSD: чи це slashing.",
}
PROTO_NAMES = {"saturn": "Saturn", "unitas": "Unitas", "infinifi": "InfiniFi"}


class AlertEngine:
    def __init__(self, cfg: dict, state: dict, now: datetime):
        self.cfg = cfg
        self.A = cfg["alerts"]
        self.state = state
        self.now = now
        self.messages: list[str] = []
        started = parse_iso(state.get("started_at")) or now
        self.warmup_until = started + timedelta(days=float(self.A.get("warmup_days", 7)))
        self.in_warmup = now < self.warmup_until
        self.suppressed = set(self.A.get("warmup_suppress", [])) if self.in_warmup else set()
        self.repeat = timedelta(hours=float(self.A.get("repeat_hours", 6)))

    # ------------------------------------------------------------------
    def _fire(self, alert_id: str, group: str, level: str | None, text: str, recovery: str) -> None:
        if group in self.suppressed:
            return
        alerts = self.state.setdefault("alerts", {})
        st = alerts.get(alert_id) or {"active": False, "level": None, "since": None, "last_sent": None}
        when = self.now.strftime("%d.%m %H:%M UTC")
        if level:
            head = f"{LEVEL_TAG[level]} · {when}\n"
            if not st["active"]:
                self.messages.append(head + text)
                st.update(active=True, level=level, since=iso(self.now), last_sent=iso(self.now))
            elif st.get("level") != level:
                since = (parse_iso(st.get("since")) or self.now).strftime("%d.%m %H:%M")
                direction = "ескалація" if LEVEL_ORDER[level] > LEVEL_ORDER[st.get("level")] else "рівень знижено"
                self.messages.append(f"{head}{text}\n<i>({direction}; умова триває з {since} UTC)</i>")
                st.update(level=level, last_sent=iso(self.now))
            else:
                last = parse_iso(st.get("last_sent")) or self.now
                if self.now - last >= self.repeat:
                    since = (parse_iso(st.get("since")) or self.now).strftime("%d.%m %H:%M")
                    self.messages.append(f"{head}{text}\n<i>(повтор; умова триває з {since} UTC)</i>")
                    st["last_sent"] = iso(self.now)
        elif st["active"]:
            since = (parse_iso(st.get("since")) or self.now).strftime("%d.%m %H:%M")
            self.messages.append(f"✅ <b>НОРМА</b> · {when}\n{recovery}\n<i>(умова трималась з {since} UTC)</i>")
            st.update(active=False, level=None)
        alerts[alert_id] = st

    def _event(self, text: str) -> None:
        """One-off event (no persistence, no recovery)."""
        when = self.now.strftime("%d.%m %H:%M UTC")
        self.messages.append(f"{LEVEL_TAG['alarm']} · {when}\n{text}")

    # ------------------------------------------------------------------
    def run(self, metrics: dict, history: list[dict], errors: dict) -> list[str]:
        self._prices(metrics)
        self._tvl(metrics, history)
        self._curve(metrics, history)
        self._infinifi(metrics)
        self._sources(errors)
        return self.messages

    # --- prices --------------------------------------------------------
    def _prices(self, m: dict) -> None:
        P = self.A["price"]
        counters = self.state.setdefault("counters", {})
        for asset in ("usdat", "usdu", "iusd"):
            p = m.get(f"price_{asset}")
            if p is None:
                continue
            key = f"price_{asset}_below_warn"
            counters[key] = counters.get(key, 0) + 1 if p < P["warn_below"] else 0
            alarm_below = P["alarm_below"].get(asset, P["alarm_below"].get("usdat"))
            level = None
            if p < alarm_below:
                level = "alarm"
            elif counters[key] >= int(P.get("warn_consecutive", 2)):
                level = "warn"
            name, pool = ASSET_NAMES[asset], ASSET_POOL[asset]
            if level == "alarm":
                text = (f"{name} = <b>{fmt.price(p)}</b> — нижче порогу тривоги {fmt.price(alarm_below, 3)}\n"
                        f"Джерело: {esc(pool)}\n{ASSET_ADVICE[asset]}")
            else:
                text = (f"{name} = <b>{fmt.price(p)}</b> — нижче {fmt.price(P['warn_below'], 3)} "
                        f"на {counters[key]} перевірках поспіль\nДжерело: {esc(pool)}\n"
                        f"Перевірте причину; тривога — при {fmt.price(alarm_below, 3)}.")
            self._fire(f"price_{asset}", "price", level, text,
                       f"{name} повернувся до норми: <b>{fmt.price(p)}</b>")

    # --- TVL -----------------------------------------------------------
    def _tvl(self, m: dict, history: list[dict]) -> None:
        T = self.A["tvl"]
        for proto in ("saturn", "unitas", "infinifi"):
            cur = m.get(f"tvl_{proto}")
            if cur is None:
                continue
            key = f"tvl_{proto}"
            v7 = value_ago(history, key, self.now, 24 * 7, tolerance_hours=12)
            v24 = value_ago(history, key, self.now, 24, tolerance_hours=3)
            d7, d24 = fmt.change_pct(cur, v7), fmt.change_pct(cur, v24)
            level = None
            if d24 is not None and d24 <= -float(T["alarm_drop_24h_pct"]):
                level = "alarm"
            elif d7 is not None and d7 <= -float(T["warn_drop_7d_pct"]):
                level = "warn"
            # missing lookback data must not clear an active alert (gap in history ≠ recovery)
            current = (self.state.get("alerts", {}).get(key) or {}).get("level")
            if (current == "alarm" and d24 is None) or (current == "warn" and d7 is None and level is None):
                continue
            name = PROTO_NAMES[proto]
            text = (f"TVL {name} = <b>{fmt.money(cur)}</b>\n"
                    f"за 24 год: {fmt.pct(d24, signed=True)} · за 7 днів: {fmt.pct(d7, signed=True)}\n"
                    f"Джерело: DefiLlama ({esc(self.cfg['sources']['defillama']['slugs'][proto])})")
            if level == "alarm":
                text += "\nРізкий відтік: експлойт або паніка — нічого не підписувати до офіційної заяви протоколу."
            elif level == "warn":
                text += "\nШукати причину: кінець сезону поінтів, ротація фармерів, переоцінка — зазвичай не проблема."
            self._fire(key, "tvl", level, text,
                       f"TVL {name} стабілізувався: <b>{fmt.money(cur)}</b> "
                       f"(24 год {fmt.pct(d24, signed=True)}, 7 днів {fmt.pct(d7, signed=True)})")

        sat = m.get("tvl_saturn")
        hi = float(T.get("saturn_high_usd", 0) or 0)
        if sat is not None and hi > 0:
            level = "info" if sat > hi else None
            self._fire("tvl_saturn_high", "tvl", level,
                       f"TVL Saturn перевищив {fmt.money(hi, 0)}: <b>{fmt.money(sat)}</b>\n"
                       f"Сезон 2 закривається на $1 млрд зі snapshot-ом — позиції не чіпати.",
                       f"TVL Saturn знову нижче {fmt.money(hi, 0)}: <b>{fmt.money(sat)}</b>")

    # --- Curve pool ----------------------------------------------------
    def _curve(self, m: dict, history: list[dict]) -> None:
        C = self.A["curve_pool"]
        share = m.get("curve_usdat_share")
        if share is not None:
            level = None
            if share > float(C["usdat_share_alarm_pct"]):
                level = "alarm"
            elif share > float(C["usdat_share_warn_pct"]):
                level = "warn"
            text = (f"Частка USDat у пулі Curve USDC/USDat = <b>{fmt.pct(share)}</b> "
                    f"(USDat {fmt.num(m.get('curve_usdat_balance'))} / USDC {fmt.num(m.get('curve_usdc_balance'))})\n"
                    f"Ціна USDat у пулі: {fmt.price(m.get('price_usdat'))}")
            if level == "alarm":
                text += "\nПонад 80% — далі проковзування росте швидко; план: вихід."
            elif level == "warn":
                text += "\nЗ'ясувати: односторонні депозити (ціна в нормі) чи розпродаж (ціна < $0.995)."
            self._fire("curve_share", "curve_pool", level, text,
                       f"Частка USDat у пулі Curve повернулась до норми: <b>{fmt.pct(share)}</b>")

        liq = m.get("curve_pool_liquidity")
        if liq is not None:
            v7 = value_ago(history, "curve_pool_liquidity", self.now, 24 * 7, tolerance_hours=12)
            d7 = fmt.change_pct(liq, v7)
            level = "warn" if d7 is not None and d7 <= -float(C["liquidity_drop_7d_warn_pct"]) else None
            active = (self.state.get("alerts", {}).get("curve_liquidity") or {}).get("active")
            if d7 is None and active:
                return
            self._fire("curve_liquidity", "curve_pool", level,
                       f"Ліквідність пулу Curve USDC/USDat впала на <b>{fmt.pct(abs(d7) if d7 else 0)}</b> за 7 днів: "
                       f"{fmt.money(v7)} → <b>{fmt.money(liq)}</b>\nТонкий пул у стресі спорожнюється за хвилини.",
                       f"Ліквідність пулу Curve відновилась: <b>{fmt.money(liq)}</b> (7 днів {fmt.pct(d7, signed=True)})")

    # --- InfiniFi ------------------------------------------------------
    def _infinifi(self, m: dict) -> None:
        I = self.A["infinifi"]
        cond = self.state.setdefault("conditions", {})

        # 1) liquid reserve < X% of TVL for N days in a row
        pct = m.get("infinifi_liquid_reserve_pct")
        if pct is not None:
            if pct < float(I["reserve_warn_pct"]):
                cond.setdefault("reserve_low_since", iso(self.now))
            else:
                cond.pop("reserve_low_since", None)
            since = parse_iso(cond.get("reserve_low_since"))
            held_h = (self.now - since).total_seconds() / 3600 if since else 0
            level = "warn" if since and held_h >= float(I["reserve_warn_days"]) * 24 else None
            self._fire("infinifi_reserve", "infinifi", level,
                       f"Ліквідний резерв InfiniFi = <b>{fmt.pct(pct)}</b> TVL "
                       f"({fmt.money(m.get('infinifi_liquid_reserve'))} з {fmt.money(m.get('infinifi_tvl_onchain'))}) "
                       f"нижче {fmt.pct(float(I['reserve_warn_pct']), 0)} уже {held_h / 24:.1f} дн.\n"
                       f"Сигнал набігу: черга працює за FIFO — якщо планували виходити, стартуйте unbonding.",
                       f"Ліквідний резерв InfiniFi повернувся до норми: <b>{fmt.pct(pct)}</b> TVL")

        # 2) redemption queue non-empty for > N hours
        q_iusd, q_len = m.get("infinifi_queue_iusd"), m.get("infinifi_queue_length")
        if q_iusd is not None or q_len is not None:
            nonempty = (q_iusd or 0) > 0 or (q_len or 0) > 0
            if nonempty:
                cond.setdefault("queue_nonempty_since", iso(self.now))
            else:
                cond.pop("queue_nonempty_since", None)
            since = parse_iso(cond.get("queue_nonempty_since"))
            held_h = (self.now - since).total_seconds() / 3600 if since else 0
            level = "alarm" if since and held_h >= float(I["queue_alarm_hours"]) else None
            self._fire("infinifi_queue", "infinifi", level,
                       f"Черга redemption InfiniFi не порожня вже <b>{held_h:.0f} год</b>: "
                       f"{fmt.num(q_len)} заявок на {fmt.num(q_iusd)} iUSD\n"
                       f"Ліквідний резерв: {fmt.money(m.get('infinifi_liquid_reserve'))} ({fmt.pct(pct)} TVL)",
                       "Черга redemption InfiniFi порожня.")

        # 3) exchange rate drop vs previous reading (one-off event)
        rate = m.get("liusd13w_rate")
        prev = (self.state.get("last_metrics") or {}).get("liusd13w_rate")
        if rate is not None and prev not in (None, 0) and "infinifi" not in self.suppressed:
            if rate < prev * (1 - float(I.get("rate_drop_tolerance", 1e-6))):
                self._event(f"Exchange rate liUSD-13w <b>впав</b>: {fmt.rate(prev)} → <b>{fmt.rate(rate)}</b> "
                            f"({fmt.pct(fmt.change_pct(rate, prev), 3, signed=True)})\n"
                            f"Імовірний slashing. Перевірте stats.infinifi.xyz; "
                            f"unaccrued yield: {fmt.num(m.get('infinifi_unaccrued_yield'))} iUSD")

    # --- data sources --------------------------------------------------
    def _sources(self, errors: dict) -> None:
        fails = self.state.setdefault("source_failures", {})
        n = int(self.A.get("source_error_runs", 3))
        known = set(fails) | set(errors)
        for src in sorted(known):
            if src in errors:
                fails[src] = fails.get(src, 0) + 1
            else:
                fails[src] = 0
            level = "warn" if fails[src] >= n else None
            msg = esc(errors.get(src, "")[:300])
            self._fire(f"source_{src}", "sources", level,
                       f"Джерело даних <code>{esc(src)}</code> не відповідає {fails[src]} запусків поспіль:\n<code>{msg}</code>",
                       f"Джерело даних <code>{esc(src)}</code> знову працює.")
            if fails[src] == 0:
                fails.pop(src, None)


def warmup_note(engine: AlertEngine) -> str | None:
    if not engine.in_warmup:
        return None
    return (f"Warm-up до {engine.warmup_until.strftime('%d.%m.%Y %H:%M')} UTC: "
            f"глушаться групи {', '.join(sorted(engine.suppressed))}.")


def status_levels(cfg: dict, m: dict, history: list[dict], state: dict, now: datetime) -> dict[str, str]:
    """Human-readable level per metric for /status (does not touch state)."""
    A = cfg["alerts"]
    out: dict[str, str] = {}

    def lvl(level):
        return {"alarm": "🚨 тривога", "warn": "⚠️ увага", "info": "ℹ️", None: "✅ норма"}[level]

    P = A["price"]
    for a in ("usdat", "usdu", "iusd"):
        p = m.get(f"price_{a}")
        if p is None:
            out[f"price_{a}"] = "н/д"
            continue
        ab = P["alarm_below"].get(a, 0.985)
        out[f"price_{a}"] = lvl("alarm" if p < ab else "warn" if p < P["warn_below"] else None)
        if p < P["warn_below"] and p >= ab:
            c = state.get("counters", {}).get(f"price_{a}_below_warn", 0)
            out[f"price_{a}"] += f" ({c}/{P.get('warn_consecutive', 2)} перевірок)"

    T = A["tvl"]
    for proto in ("saturn", "unitas", "infinifi"):
        cur = m.get(f"tvl_{proto}")
        key = f"tvl_{proto}"
        d7 = fmt.change_pct(cur, value_ago(history, key, now, 168, 12))
        d24 = fmt.change_pct(cur, value_ago(history, key, now, 24, 3))
        level = None
        if d24 is not None and d24 <= -T["alarm_drop_24h_pct"]:
            level = "alarm"
        elif d7 is not None and d7 <= -T["warn_drop_7d_pct"]:
            level = "warn"
        extra = f" (24г {fmt.pct(d24, signed=True)}, 7д {fmt.pct(d7, signed=True)})"
        out[key] = (lvl(level) if cur is not None else "н/д") + extra

    C = A["curve_pool"]
    s = m.get("curve_usdat_share")
    out["curve_usdat_share"] = "н/д" if s is None else lvl(
        "alarm" if s > C["usdat_share_alarm_pct"] else "warn" if s > C["usdat_share_warn_pct"] else None)
    liq = m.get("curve_pool_liquidity")
    d7 = fmt.change_pct(liq, value_ago(history, "curve_pool_liquidity", now, 168, 12))
    out["curve_pool_liquidity"] = ("н/д" if liq is None else
                                   lvl("warn" if d7 is not None and d7 <= -C["liquidity_drop_7d_warn_pct"] else None)
                                   + f" (7д {fmt.pct(d7, signed=True)})")

    I = A["infinifi"]
    pct = m.get("infinifi_liquid_reserve_pct")
    rs = parse_iso(state.get("conditions", {}).get("reserve_low_since"))
    out["infinifi_reserve"] = "н/д" if pct is None else (
        lvl("warn" if rs and (now - rs) >= timedelta(days=I["reserve_warn_days"]) else None)
        + (f" (нижче {I['reserve_warn_pct']}% з {rs.strftime('%d.%m %H:%M')} UTC)" if rs else ""))
    qs = parse_iso(state.get("conditions", {}).get("queue_nonempty_since"))
    out["infinifi_queue"] = lvl("alarm" if qs and (now - qs) >= timedelta(hours=I["queue_alarm_hours"]) else None) \
        + (f" (не порожня з {qs.strftime('%d.%m %H:%M')} UTC)" if qs else "")
    out["_history_hours"] = f"{history_span_hours(history, now):.0f}"
    return out
