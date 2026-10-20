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

ASSET_NAMES = {"usdat": "USDat", "usdu": "USDu", "iusd": "iUSD", "trusd": "trUSD"}
ASSET_POOL = {"usdat": "Curve USDC/USDat", "usdu": "PancakeSwap V3 USDu/USDT", "iusd": "Curve iUSD/USDC (тонкий пул!)",
              "trusd": "Curve frxUSD/trUSD (ціна у frxUSD)"}
ASSET_ADVICE = {
    "usdat": "Погашення 1:1 через app.saturn.credit (якщо гаманець допущений) або продаж у пулі Curve USDC/USDat.",
    "usdu": "Перевірте @UnitasLabs і новини про custody/OES. Без пояснення за 2–3 год — погасити через "
            "app.unitas.so або продати на PancakeSwap.",
    "iusd": "Пул дуже тонкий — звірте з курсом погашення в протоколі (/status) і rate liUSD: чи це slashing.",
    "trusd": "Погашення напряму в Tori — лише для whitelisted; звичайний вихід — DEX (Fluid trUSD/USDC, Curve "
             "frxUSD/trUSD) або продаж PT / зняття LP на Pendle. Перевірте @tori_finance і "
             "tori.accountable.capital (proof of reserves), а також курс strUSD у /status.",
}
PROTO_NAMES = {"saturn": "Saturn", "unitas": "Unitas", "infinifi": "InfiniFi", "tori": "Tori"}
PENDLE_NAME = "Pendle trUSD"


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

    def _event(self, text: str, level: str = "alarm") -> None:
        """One-off event (no persistence, no recovery)."""
        when = self.now.strftime("%d.%m %H:%M UTC")
        self.messages.append(f"{LEVEL_TAG[level]} · {when}\n{text}")

    def _silence(self, alert_id: str) -> None:
        """Deactivate an alert without a «норма» message (e.g. the market has matured)."""
        st = (self.state.get("alerts") or {}).get(alert_id)
        if st and st.get("active"):
            st.update(active=False, level=None)

    # ------------------------------------------------------------------
    def run(self, metrics: dict, history: list[dict], errors: dict) -> list[str]:
        self._prices(metrics)
        self._tvl(metrics, history)
        self._pendle(metrics, history)
        self._maturity(metrics)
        self._new_markets()
        self._tori(metrics)
        self._infinifi(metrics)
        self._sources(errors)
        return self.messages

    # --- prices --------------------------------------------------------
    def _prices(self, m: dict) -> None:
        P = self.A["price"]
        counters = self.state.setdefault("counters", {})
        for asset in ASSET_NAMES:
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
        for proto in PROTO_NAMES:
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
                    f"Джерело: DefiLlama ({esc(self.cfg['sources']['defillama']['slugs'].get(proto, proto))})")
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

    # --- Pendle trUSD market -------------------------------------------
    def _pendle_cfg(self) -> dict:
        return self.A.get("pendle") or {}

    def _pendle(self, m: dict, history: list[dict]) -> None:
        P = self._pendle_cfg()
        days = m.get("pendle_days_to_expiry")
        if days is not None and days <= 0:      # погашено: ціна PT = 1, APY немає — глушимо все
            for a in ("pendle_pt", "pendle_apy", "pendle_liquidity"):
                self._silence(a)
            return
        alerts = self.state.get("alerts", {})
        apy = m.get("pendle_implied_apy")

        # 1) PT дешевшає: у нормі PT щодня лише дорожчає до 1 trUSD
        pt = m.get("pendle_pt_price")
        if pt is not None:
            v24 = value_ago(history, "pendle_pt_price", self.now, 24, tolerance_hours=3)
            d24 = fmt.change_pct(pt, v24)
            level = None
            if d24 is not None and d24 <= -float(P.get("pt_drop_24h_alarm_pct", 1.5)):
                level = "alarm"
            elif d24 is not None and d24 <= -float(P.get("pt_drop_24h_warn_pct", 0.5)):
                level = "warn"
            if not (d24 is None and (alerts.get("pendle_pt") or {}).get("active")):   # пропуск в історії ≠ норма
                text = (f"PT-trUSD подешевшав на <b>{fmt.pct(abs(d24) if d24 else 0, 2)}</b> за 24 год: "
                        f"{fmt.rate(v24)} → <b>{fmt.rate(pt)}</b> trUSD\n"
                        f"Implied APY: {fmt.pct(apy, 2)} · trUSD: {fmt.price(m.get('price_trusd'))} · "
                        f"ліквідність ринку: {fmt.money(m.get('pendle_liquidity'))}\n")
                if level == "alarm":
                    text += ("Ринок закладає ризик trUSD: PT погашається в 1 trUSD, тож вартість PT тримається лише разом "
                             "із trUSD. Перевірте ціну trUSD і курс strUSD (/status), @tori_finance. "
                             "Вихід — продати PT / зняти LP на app.pendle.finance, поки є ліквідність.")
                else:
                    text += ("Або продають PT (сумнів у trUSD), або активно купують YT (ажіотаж навколо поінтів Cores). "
                             "Якщо trUSD ≈ $1 і strUSD не падав — зазвичай друге.")
                self._fire("pendle_pt", "pendle", level, text,
                           f"PT-trUSD стабілізувався: <b>{fmt.rate(pt)}</b> trUSD (24 год {fmt.pct(d24, 2, signed=True)})")

        # 2) implied APY аномально високий (поза останнім тижнем до погашення)
        if apy is not None:
            counters = self.state.setdefault("counters", {})
            key = "pendle_apy_above_warn"
            warn_at = float(P.get("implied_apy_warn_pct", 30))
            noisy = days is not None and days <= float(P.get("apy_ignore_last_days", 7))
            if noisy:
                counters[key] = 0
                self._silence("pendle_apy")
            else:
                counters[key] = counters.get(key, 0) + 1 if apy > warn_at else 0
                need = int(self.A["price"].get("warn_consecutive", 2))
                level = "warn" if counters[key] >= need else None
                self._fire("pendle_apy", "pendle", level,
                           f"Implied APY ринку {PENDLE_NAME} = <b>{fmt.pct(apy, 1)}</b> (поріг {fmt.pct(warn_at, 0)}) "
                           f"на {counters[key]} перевірках поспіль\n"
                           f"PT-trUSD: {fmt.rate(pt)} trUSD · trUSD: {fmt.price(m.get('price_trusd'))}\n"
                           f"Дисконт PT росте — подивіться, хто продає PT і чи немає новин про Tori.",
                           f"Implied APY {PENDLE_NAME} повернувся до норми: <b>{fmt.pct(apy, 1)}</b>")

        # 3) ліквідність ринку тане
        liq = m.get("pendle_liquidity")
        if liq is not None:
            v24 = value_ago(history, "pendle_liquidity", self.now, 24, tolerance_hours=3)
            v7 = value_ago(history, "pendle_liquidity", self.now, 24 * 7, tolerance_hours=12)
            d24, d7 = fmt.change_pct(liq, v24), fmt.change_pct(liq, v7)
            hit24 = d24 is not None and d24 <= -float(P.get("liquidity_drop_24h_warn_pct", 25))
            hit7 = d7 is not None and d7 <= -float(P.get("liquidity_drop_7d_warn_pct", 30))
            level = "warn" if hit24 or hit7 else None
            if not (d24 is None and d7 is None and (alerts.get("pendle_liquidity") or {}).get("active")):
                self._fire("pendle_liquidity", "pendle", level,
                           f"Ліквідність ринку {PENDLE_NAME} падає: <b>{fmt.money(liq)}</b> "
                           f"(24 год {fmt.pct(d24, signed=True)}, 7 днів {fmt.pct(d7, signed=True)})\n"
                           f"LP виходять — продаж PT / зняття LP стане дорожчим (більше проковзування).",
                           f"Ліквідність ринку {PENDLE_NAME} стабілізувалась: <b>{fmt.money(liq)}</b> "
                           f"(24 год {fmt.pct(d24, signed=True)}, 7 днів {fmt.pct(d7, signed=True)})")

    def _other_markets_text(self) -> str:
        mine = (self.cfg["sources"]["ethereum"].get("pendle_trusd_market") or "").lower()
        others = [x for x in self.state.get("pendle_trusd_markets") or [] if x.get("address") != mine]
        if not others:
            return "Нового ринку trUSD на Pendle поки немає — бот повідомить, щойно з'явиться."
        lines = ["Інші активні ринки trUSD на Pendle:"]
        for x in others:
            lines.append(f"• погашення {esc(x.get('expiry'))}, implied APY {fmt.pct(x.get('implied_apy_pct'), 1)}, "
                         f"ліквідність {fmt.money(x.get('liquidity_usd'))} — <code>{esc(x.get('address'))}</code>")
        return "\n".join(lines)

    def _maturity(self, m: dict) -> None:
        days = m.get("pendle_days_to_expiry")
        market = (self.cfg["sources"]["ethereum"].get("pendle_trusd_market") or "").lower()
        if days is None or not market:
            return
        sent = self.state.setdefault("maturity_sent", {}).setdefault(market, [])
        date = (self.now + timedelta(days=days)).strftime("%d.%m.%Y")
        if days <= 0:
            if "expired" not in sent:
                self._event(f"⌛ Ринок {PENDLE_NAME} <b>погашено</b> ({date}). PT = 1 trUSD.\n"
                            f"Позиція більше нічого не заробляє (ні комісій, ні поінтів): на app.pendle.finance → "
                            f"Dashboard зніміть LP і погасіть PT у trUSD, YT — заберіть нараховане.\n"
                            f"Щоб бот стежив за новим ринком — змініть <code>pendle_trusd_market</code> і "
                            f"<code>pendle_trusd_pt</code> у config.yaml.\n" + self._other_markets_text(), level="info")
                sent.append("expired")
            return
        remind = sorted(float(d) for d in (self._pendle_cfg().get("maturity_remind_days") or [14, 7, 2]))
        crossed = [d for d in remind if days <= d and f"{d:g}" not in sent]
        if not crossed:
            return
        self._event(f"⏳ Ринок {PENDLE_NAME} погашається <b>{date}</b> — через <b>{days:.1f} дн.</b>\n"
                    f"Після погашення LP не заробляє, PT погашається 1:1 у trUSD, YT перестає нараховувати.\n"
                    f"План: PT — тримати до дати й погасити (або продати раніше, якщо ціна вже ≈ 1); "
                    f"LP — зняти після погашення; YT — забрати нараховане.\n" + self._other_markets_text(), level="info")
        sent.extend(f"{d:g}" for d in remind if days <= d and f"{d:g}" not in sent)

    def _new_markets(self) -> None:
        mine = (self.cfg["sources"]["ethereum"].get("pendle_trusd_market") or "").lower()
        known = self.state.setdefault("pendle_known_markets", [])
        for x in self.state.get("pendle_trusd_markets") or []:
            addr = x.get("address")
            if not addr or addr == mine or addr in known:
                continue
            known.append(addr)
            self._event(f"🆕 На Pendle з'явився новий ринок trUSD: погашення <b>{esc(x.get('expiry'))}</b>, "
                        f"implied APY {fmt.pct(x.get('implied_apy_pct'), 1)}, ліквідність {fmt.money(x.get('liquidity_usd'))}\n"
                        f"Адреса: <code>{esc(addr)}</code>\n"
                        f"Якщо перекладете позицію — замініть <code>pendle_trusd_market</code> у config.yaml.", level="info")

    # --- Tori ----------------------------------------------------------
    def _tori(self, m: dict) -> None:
        if "tori" in self.suppressed:
            return
        rate = m.get("strusd_rate")
        prev = (self.state.get("last_metrics") or {}).get("strusd_rate")
        tol = float((self.A.get("tori") or {}).get("rate_drop_tolerance", 1e-6))
        if rate is not None and prev not in (None, 0) and rate < prev * (1 - tol):
            self._event(f"Курс strUSD <b>впав</b>: {fmt.rate(prev)} → <b>{fmt.rate(rate)}</b> trUSD "
                        f"({fmt.pct(fmt.change_pct(rate, prev), 3, signed=True)})\n"
                        f"Tori списав збиток стратегій (reportLoss): за кодом контракту збиток спалює trUSD зі сховища "
                        f"strUSD — це буфер першого збитку для trUSD. Буфер зараз: {fmt.pct(m.get('strusd_buffer_pct'))} "
                        f"від trUSD в обігу; ліміт списання — 5% сховища на добу.\n"
                        f"trUSD: {fmt.price(m.get('price_trusd'))} · PT-trUSD: {fmt.rate(m.get('pendle_pt_price'))}. "
                        f"Перевірте @tori_finance і tori.accountable.capital; якщо збиток повториться завтра — "
                        f"це вже не разовий епізод.")

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
    for a in ASSET_NAMES:
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
    for proto in PROTO_NAMES:
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

    PP = A.get("pendle") or {}
    days = m.get("pendle_days_to_expiry")
    if days is not None and days <= 0:
        out["pendle_pt"] = out["pendle_apy"] = out["pendle_liquidity"] = "погашено"
    else:
        pt = m.get("pendle_pt_price")
        d24 = fmt.change_pct(pt, value_ago(history, "pendle_pt_price", now, 24, 3))
        level = ("alarm" if d24 is not None and d24 <= -PP.get("pt_drop_24h_alarm_pct", 1.5) else
                 "warn" if d24 is not None and d24 <= -PP.get("pt_drop_24h_warn_pct", 0.5) else None)
        out["pendle_pt"] = ("н/д" if pt is None else lvl(level)) + f" (24г {fmt.pct(d24, 2, signed=True)})"
        apy = m.get("pendle_implied_apy")
        noisy = days is not None and days <= PP.get("apy_ignore_last_days", 7)
        out["pendle_apy"] = "н/д" if apy is None else (
            "ℹ️ шумний перед погашенням" if noisy else lvl("warn" if apy > PP.get("implied_apy_warn_pct", 30) else None))
        liq = m.get("pendle_liquidity")
        l24 = fmt.change_pct(liq, value_ago(history, "pendle_liquidity", now, 24, 3))
        l7 = fmt.change_pct(liq, value_ago(history, "pendle_liquidity", now, 168, 12))
        hit = ((l24 is not None and l24 <= -PP.get("liquidity_drop_24h_warn_pct", 25))
               or (l7 is not None and l7 <= -PP.get("liquidity_drop_7d_warn_pct", 30)))
        out["pendle_liquidity"] = ("н/д" if liq is None else lvl("warn" if hit else None)) + \
            f" (24г {fmt.pct(l24, signed=True)}, 7д {fmt.pct(l7, signed=True)})"

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
