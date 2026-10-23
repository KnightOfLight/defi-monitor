"""Security watch: what each project depends on, and early signs that something broke.

Three layers, from the fastest and most reliable to the broadest:

1. On-chain, own contracts (every run, ~15 min). For every contract in `security.contracts`:
   * EIP-1967 proxy slots (implementation / admin / beacon) and owner() / minter() — a change means the code
     or the keys behind it changed. Planned upgrades trigger it too, but a silent upgrade is the classic way
     a stolen admin key turns into a drained protocol;
   * paused() — protocols pause first when something goes wrong;
   * totalSupply() of the stablecoins — a jump is the signature of an unbacked-mint exploit,
     a fast drop is a run on the protocol.
2. Dependencies, measured: prices of the stablecoins the projects stand on (DefiLlama coins) and TVL of
   the DeFi protocols they use (DefiLlama, hourly); live allocation of infiniFi farms read on-chain.
3. News: hack keywords + the name of a project or a dependency, in public Telegram channels, crypto media RSS,
   Google News and Bing News. The same machinery as the TGE watcher; first pass of each source is silent.

Nothing here pretends to be real time: a professional monitor (Hypernative, Forta, …) sees a hack in the
mempool. This bot sees it ~15–30 min after it lands on-chain, and minutes–hours after the first post on X.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from . import fmt, tge
from .evm import Chain, RpcUnavailable, dec_addr_array, is_zero, same_addr
from .storage import iso, parse_iso
from .telegram import esc

# EIP-1967 slots = keccak256("eip1967.proxy.<name>") − 1 (computed, not copied)
SLOTS = {
    "impl": "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc",
    "admin": "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103",
    "beacon": "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50",
}
ADDR_PROBES = ("impl", "admin", "beacon", "owner", "minter")
PROBE_LABEL = {"impl": "реалізацію (proxy implementation)", "admin": "адміна proxy", "beacon": "beacon proxy",
               "owner": "власника (owner)", "minter": "мінтера (minter)"}
UA = {"User-Agent": "Mozilla/5.0 (compatible; defi-monitor-bot/1.2; +github actions)"}
TIMEOUT = 15


def short(a: str | None) -> str:
    return f"{a[:6]}…{a[-4:]}" if a and len(a) > 12 else str(a)


def S(cfg: dict) -> dict:
    return cfg.get("security") or {}


def _st(state: dict) -> dict:
    st = state.setdefault("security", {})
    for k, v in (("contracts", {}), ("tvl", {}), ("farms", {}), ("farm_names", {}), ("news", {}), ("log", []),
                 ("scanned_at", None), ("tvl_at", None), ("farms_at", None), ("names_at", None)):
        st.setdefault(k, v)
    n = st["news"]
    for k, v in (("seen", {}), ("seeded", {}), ("items", []), ("cooldown", {}), ("fails", {}), ("fail_alerted", {}),
                 ("checked", {})):
        n.setdefault(k, v)
    return st


def _due(st: dict, key: str, hours: float, now: datetime, force: bool = False) -> bool:
    last = parse_iso(st.get(key))
    if force or not last or now - last >= timedelta(hours=hours):
        st[key] = iso(now)
        return True
    return False


class CachedSession:
    """One fetch per URL per run: the TGE and the security watchers read the same Telegram pages."""

    def __init__(self, session=None):
        self.s = session or requests.Session()
        self.cache: dict = {}

    def get(self, url, **kw):
        key = (url, json.dumps(kw.get("params"), sort_keys=True))
        if key not in self.cache:
            r = self.s.get(url, **kw)
            if getattr(r, "status_code", 200) != 200:
                return r                                   # не кешуємо помилки
            self.cache[key] = r
        return self.cache[key]


# --------------------------------------------------------------------------
# dependency registry
# --------------------------------------------------------------------------
def deps(cfg: dict) -> dict:
    return S(cfg).get("deps") or {}


def projects(cfg: dict) -> dict:
    return S(cfg).get("projects") or {}


def farm_deps(name: str, rules: list) -> list[str]:
    """FARM_PENDLEV3_RUSD_30OCT25 → [reservoir, pendle]: every rule whose token starts a name part,
    in rule order (base asset first, the venue after)."""
    parts = [p for p in name.upper().split("_") if p and p != "FARM"]
    out: list[str] = []
    for token, dep in rules:
        t = str(token).upper()
        if dep not in out and any(p.startswith(t) for p in parts):
            out.append(dep)
    return out


def farm_dep(name: str, rules: list) -> str | None:
    d = farm_deps(name, rules)
    return d[0] if d else None


def dependents(cfg: dict, state: dict, dep_key: str) -> list[tuple[str, str]]:
    """[(project name, why)] — who stands on this dependency."""
    out = []
    for pk, p in projects(cfg).items():
        why = (p.get("deps") or {}).get(dep_key)
        if pk == "infinifi":
            share = farm_shares(state).get(dep_key)
            if share and share >= 0.5:
                why = (why + "; " if why else "") + f"фарми infiniFi: {share:.1f}% активів"
        if why:
            out.append((p.get("name", pk), why))
    return out


def farm_shares(state: dict) -> dict[str, float]:
    f = (state.get("security") or {}).get("farms") or {}
    total = f.get("total") or 0
    out: dict[str, float] = {}
    for it in f.get("items") or []:
        for d in it.get("deps") or ([it["dep"]] if it.get("dep") else []):
            if total:
                out[d] = out.get(d, 0) + it["usd"] / total * 100
    return out


def news_targets(cfg: dict, state: dict) -> list[dict]:
    """Everything worth matching in hack news: own projects first, then dependencies in use."""
    out = []
    for pk, p in projects(cfg).items():
        out.append({"key": f"own:{pk}", "own": True, "name": p.get("name", pk), "aliases": p.get("aliases", []),
                    "strict": False, "exclude": p.get("exclude", []), "project": pk})
    used = set()
    for p in projects(cfg).values():
        used |= set((p.get("deps") or {}).keys())
    used |= {k for k, v in farm_shares(state).items() if v >= 0.5}
    for dk in sorted(used):
        d = deps(cfg).get(dk)
        if d and d.get("aliases"):
            out.append({"key": f"dep:{dk}", "own": False, "name": d.get("name", dk), "aliases": d["aliases"],
                        "strict": bool(d.get("strict")), "exclude": d.get("exclude", []), "dep": dk,
                        "extra_keywords": d.get("extra_keywords", [])})
    return out


# --------------------------------------------------------------------------
# 1. on-chain: own contracts
# --------------------------------------------------------------------------
def probe(chain: Chain, addr: str, kind: str):
    """Value of one probe, or raises. A revert / empty answer means «not applicable» for this contract."""
    if kind in SLOTS:
        v = chain.storage(addr, SLOTS[kind])
        return ("0x" + format(v, "064x")[-40:]).lower() if v else None
    if kind in ("owner", "minter"):
        a = chain.addr(addr, f"{kind}()").lower()
        return None if is_zero(a) else a
    if kind == "paused":
        return bool(chain.uint(addr, "paused()"))
    if kind == "supply":
        return chain.uint(addr, "totalSupply()") / 10 ** chain.decimals(addr)
    raise ValueError(kind)


def contract_list(cfg: dict) -> list[dict]:
    out = []
    for pk, p in projects(cfg).items():
        for c in p.get("contracts") or []:
            out.append(dict(c, project=pk, project_name=p.get("name", pk)))
    return out


class Onchain:
    def __init__(self, cfg: dict, state: dict, now: datetime, chains: dict[str, Chain]):
        self.cfg, self.now, self.chains = cfg, now, chains
        self.st = _st(state)
        self.T = S(cfg)
        self.findings: list[dict] = []
        self.errors: dict[str, str] = {}

    def _resolve(self, c: dict, chain: Chain) -> str | None:
        if c.get("address"):
            return c["address"]
        r = c.get("resolve") or {}
        if r.get("from") and r.get("getter"):
            a = chain.addr(r["from"], r["getter"])
            return None if is_zero(a) else a
        return None

    def _err(self, chain_name: str, label: str, msg: str) -> None:
        """One error key per chain: when RPC is down, one alert — not one per contract."""
        k = f"sec:onchain:{chain_name}"
        prev = self.errors.get(k)
        self.errors[k] = (prev + "; " if prev else "") + f"{label}: {msg[:160]}"

    def run(self) -> None:
        full = _due(self.st, "scanned_at", float(self.T.get("rescan_hours", 24)), self.now)
        down: set[str] = set()
        for c in contract_list(self.cfg):
            cname = c.get("chain", "ethereum")
            chain = self.chains.get(cname)
            label = f"{c['project_name']} · {c['label']}"
            if chain is None or cname in down:
                continue
            try:
                addr = self._resolve(c, chain)
            except RpcUnavailable as e:
                self._err(cname, label, str(e))
                down.add(cname)
                continue
            except Exception as e:
                self._err(cname, label, f"адресу не знайдено ({e})")
                continue
            if not addr:
                continue
            key = f"{c.get('chain', 'ethereum')}:{addr.lower()}"
            rec = self.st["contracts"].get(key)
            first = rec is None
            rec = rec or {"label": label, "kinds": [], "values": {}, "supply_hist": []}
            rec["label"], rec["project"] = label, c["project"]
            kinds = (list(ADDR_PROBES) + ["paused"] + (["supply"] if c.get("supply") else [])) \
                if (first or full) else rec["kinds"]
            found, values, failed = [], {}, None
            for k in kinds:
                try:
                    v = probe(chain, addr, k)
                except RpcUnavailable as e:
                    failed = str(e)
                    break
                except Exception:
                    continue                      # revert → не застосовно до цього контракту
                if v is None and k in ADDR_PROBES:
                    continue
                found.append(k)
                values[k] = v
            if failed:
                self._err(cname, label, failed)
                down.add(cname)
                if not first:                     # перший скан не вдався — повторимо наступного запуску
                    self.st["contracts"][key] = rec
                continue
            if first or full:
                rec["kinds"] = found
            self._compare(c, label, addr, rec, values, first)
            rec["values"].update(values)
            for k in list(rec["values"]):
                if k not in rec["kinds"]:
                    rec["values"].pop(k)
            rec["checked_at"] = iso(self.now)
            self.st["contracts"][key] = rec

    def _compare(self, c: dict, label: str, addr: str, rec: dict, values: dict, first: bool) -> None:
        old = rec["values"]
        note = f"\n{esc(c['note'])}" if c.get("note") else ""
        link = f"https://{'bscscan.com' if c.get('chain') == 'bsc' else 'etherscan.io'}/address/{addr}"
        # address-like probes: any change is an event
        for k in ADDR_PROBES:
            if k in values and k in old and old[k] and values[k] != old[k]:
                self.findings.append({"type": "event", "level": "alarm", "text":
                    f"🛡 <b>{esc(label)}</b>: змінено {PROBE_LABEL[k]}\n"
                    f"<code>{esc(short(old[k]))}</code> → <code>{esc(short(values[k]))}</code>\n"
                    f"Планові оновлення теж так виглядають, але тихе оновлення — типовий шлях, яким викрадений "
                    f"ключ адміна перетворюється на злам. Перевірте X/Discord проєкту; поки немає пояснення — "
                    f"нічого не вносьте й не підписуйте.{note}\n{link}"})
        # pause: a condition with recovery
        if "paused" in values:
            p = values["paused"]
            self.findings.append({"type": "cond", "id": f"sec_paused_{c['project']}_{c['label']}",
                                  "level": "alarm" if p else None,
                                  "text": f"🛡 <b>{esc(label)}</b> поставлено на <b>паузу</b> (paused() = true).\n"
                                          f"Протоколи ставлять паузу, коли щось іде не так: злам, помилка оракула, "
                                          f"атака на пул. Шукайте пояснення в каналах проєкту.{note}\n{link}",
                                  "recovery": f"🛡 {esc(label)}: паузу знято."})
        # supply: jump per run (event), drop over 24 h (condition)
        if "supply" in values:
            cur = values["supply"]
            hist = [h for h in rec.get("supply_hist") or [] if (parse_iso(h[0]) or self.now) >= self.now - timedelta(hours=48)]
            prev = old.get("supply")
            T = self.T.get("supply") or {}
            if prev and not first:
                jump = (cur / prev - 1) * 100
                lvl = ("alarm" if jump >= float(T.get("jump_alarm_pct", 50)) else
                       "warn" if jump >= float(T.get("jump_warn_pct", 10)) else None)
                if lvl:
                    self.findings.append({"type": "event", "level": lvl, "text":
                        f"🛡 <b>{esc(label)}</b>: емісія різко зросла на <b>{jump:.1f}%</b> за один запуск: "
                        f"{fmt.num(prev)} → <b>{fmt.num(cur)}</b>\n"
                        f"Так виглядає безконтрольний мінт (злам мінтера). Буває й легітимно — великий депозит. "
                        f"Звірте з ціною (/status) і каналами проєкту.{note}\n{link}"})
            day = [h[1] for h in hist if (parse_iso(h[0]) or self.now) >= self.now - timedelta(hours=24)]
            peak = max(day) if day else None
            drop = (1 - cur / peak) * 100 if peak else None
            lvl = None
            if drop is not None:
                lvl = ("alarm" if drop >= float(T.get("drop_alarm_pct", 30)) else
                       "warn" if drop >= float(T.get("drop_warn_pct", 15)) else None)
            self.findings.append({"type": "cond", "id": f"sec_supply_drop_{c['project']}_{c['label']}", "level": lvl,
                                  "text": f"🛡 <b>{esc(label)}</b>: емісія впала на <b>{fmt.pct(drop)}</b> за 24 год "
                                          f"({fmt.num(peak)} → {fmt.num(cur)}) — масове погашення / вихід.{note}",
                                  "recovery": f"🛡 {esc(label)}: емісія стабілізувалась ({fmt.num(cur)})."})
            hist.append([iso(self.now), cur])
            rec["supply_hist"] = hist[-200:]


# --------------------------------------------------------------------------
# 2. dependencies, measured
# --------------------------------------------------------------------------
def stable_prices(cfg: dict, session) -> dict[str, float]:
    coins = {k: d["coin"] for k, d in deps(cfg).items() if d.get("coin")}
    if not coins:
        return {}
    url = (S(cfg).get("coins_url") or "https://coins.llama.fi") + "/prices/current/" + ",".join(coins.values())
    r = session.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    got = {k.lower(): v for k, v in (r.json().get("coins") or {}).items()}
    out = {}
    for k, c in coins.items():
        v = got.get(c.lower())
        if v and v.get("price"):
            out[k] = float(v["price"])
    return out


def llama_tvl(cfg: dict, slug: str, session) -> float:
    base = (cfg.get("sources", {}).get("defillama") or {}).get("base_url", "https://api.llama.fi")
    r = session.get(f"{base}/tvl/{slug}", headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    v = float(r.text.strip().strip('"'))
    if v <= 0:
        raise RuntimeError(f"DefiLlama: TVL {slug} = 0")
    return v


def infinifi_farms(chain: Chain, gateway: str) -> list[dict]:
    from .sources import infinifi_addresses
    a = infinifi_addresses(chain, gateway)
    registry = chain.addr(a["accounting"], "farmRegistry()")
    farms = dec_addr_array(chain.call(registry, "getFarms()"))
    dec_cache: dict[str, int] = {}
    out = []
    for f in farms:
        try:
            amount = chain.uint(f, "assets()")
            tok = chain.addr(f, "assetToken()").lower()
            if tok not in dec_cache:
                dec_cache[tok] = chain.decimals(tok)
            out.append({"addr": f.lower(), "usd": amount / 10 ** dec_cache[tok]})
        except RpcUnavailable:
            raise
        except Exception:
            continue
    return out


def farm_names(url: str, session) -> dict[str, str]:
    r = session.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return {x["addr"].lower(): x["name"] for x in r.json() if x.get("addr") and x.get("name")}


class Measured:
    def __init__(self, cfg: dict, state: dict, now: datetime, session, chains: dict[str, Chain]):
        self.cfg, self.now, self.session, self.chains = cfg, now, session, chains
        self.st = _st(state)
        self.state = state
        self.T = S(cfg)
        self.findings: list[dict] = []
        self.errors: dict[str, str] = {}
        self.prices: dict[str, float] = {}

    def _who(self, dep_key: str) -> str:
        who = dependents(self.cfg, self.state, dep_key)
        return "\n".join(f"• {esc(n)}: {esc(w)}" for n, w in who) or "• (прямих залежностей не знайдено)"

    def run(self) -> None:
        # stablecoins every run — one request
        try:
            self.prices = stable_prices(self.cfg, self.session)
            self.st["prices"] = {k: round(v, 6) for k, v in self.prices.items()}
        except Exception as e:
            self.errors["sec:stablecoins"] = f"DefiLlama coins: {e}"
        P = self.T.get("stables") or {}
        for k, price in self.prices.items():
            d = deps(self.cfg).get(k, {})
            lvl = ("alarm" if price < float(P.get("alarm_below", 0.97)) else
                   "warn" if price < float(P.get("warn_below", 0.99)) else None)
            self.findings.append({"type": "cond", "id": f"sec_stable_{k}", "level": lvl,
                                  "text": f"🛡 Залежний стейблкоїн <b>{esc(d.get('name', k))}</b> = <b>{fmt.price(price)}</b>\n"
                                          f"На ньому стоять:\n{self._who(k)}",
                                  "recovery": f"🛡 {esc(d.get('name', k))} повернувся до {fmt.price(price)}."})
        # DeFi protocols TVL — hourly; errors are kept until the next check so that the
        # «source not responding» counter (per run) sees a persistent failure as persistent
        if _due(self.st, "tvl_at", float(self.T.get("tvl_check_hours", 1)), self.now):
            bad = []
            for k, d in deps(self.cfg).items():
                if not d.get("llama"):
                    continue
                try:
                    v = llama_tvl(self.cfg, d["llama"], self.session)
                except Exception as e:
                    bad.append(f"{d['llama']}: {str(e)[:80]}")
                    continue
                hist = [h for h in self.st["tvl"].get(k, []) if (parse_iso(h[0]) or self.now) >= self.now - timedelta(hours=48)]
                hist.append([iso(self.now), v])
                self.st["tvl"][k] = hist[-60:]
            self.st["tvl_error"] = ("DefiLlama TVL залежностей: " + "; ".join(bad)) if bad else None
        if self.st.get("tvl_error"):
            self.errors["sec:dep_tvl"] = self.st["tvl_error"]
        V = self.T.get("dep_tvl") or {}
        window = timedelta(hours=float(V.get("window_hours", 6)))
        for k, hist in self.st["tvl"].items():
            if k not in deps(self.cfg) or not hist:
                continue
            recent = [h[1] for h in hist if (parse_iso(h[0]) or self.now) >= self.now - window]
            cur = hist[-1][1]
            peak = max(recent) if recent else None
            drop = (1 - cur / peak) * 100 if peak else None
            lvl = None if drop is None else ("alarm" if drop >= float(V.get("alarm_drop_pct", 40)) else
                                             "warn" if drop >= float(V.get("warn_drop_pct", 25)) else None)
            name = deps(self.cfg)[k].get("name", k)
            self.findings.append({"type": "cond", "id": f"sec_tvl_{k}", "level": lvl,
                                  "text": f"🛡 TVL залежності <b>{esc(name)}</b> впав на <b>{fmt.pct(drop)}</b> за "
                                          f"{window.total_seconds() / 3600:.0f} год: {fmt.money(peak)} → <b>{fmt.money(cur)}</b>\n"
                                          f"Різкий відтік — часто перший кількісний слід зламу або паніки.\n"
                                          f"На ньому стоять:\n{self._who(k)}",
                                  "recovery": f"🛡 TVL {esc(name)} стабілізувався: {fmt.money(cur)}."})
        # infiniFi farms — hourly, names from the project's own deployment file (daily)
        F = self.T.get("infinifi_farms") or {}
        gw = self.cfg["sources"]["ethereum"].get("infinifi_gateway")
        if F.get("enabled", True) and gw and _due(self.st, "farms_at", float(F.get("check_hours", 1)), self.now):
            try:
                items = infinifi_farms(self.chains["ethereum"], gw)
                known = self.st["farm_names"]
                unknown = any(it["addr"] not in known for it in items)
                if F.get("names_url") and (_due(self.st, "names_at", 24, self.now) or
                                           (unknown and _due(self.st, "names_retry_at", 6, self.now))):
                    try:                          # назви фармів — з deployment-файлу самого infiniFi на GitHub
                        names = farm_names(F["names_url"], self.session)
                        for it in items:
                            if it["addr"] in names:
                                known[it["addr"]] = names[it["addr"]]
                    except Exception as e:
                        print(f"[security] infiniFi farm names: {e}")
                rules = [tuple(x) for x in F.get("rules") or []]
                for it in items:
                    it["name"] = known.get(it["addr"], short(it["addr"]))
                    it["deps"] = farm_deps(it["name"], rules) if it["addr"] in known else []
                    it["dep"] = it["deps"][0] if it["deps"] else None
                items = [it for it in items if it["usd"] >= 1]
                items.sort(key=lambda x: -x["usd"])
                self.st["farms"] = {"at": iso(self.now), "items": items, "total": sum(x["usd"] for x in items)}
                self.st["farms_error"] = None
            except Exception as e:
                self.st["farms_error"] = f"infiniFi farms: {str(e)[:200]}"
        if self.st.get("farms_error"):
            self.errors["sec:infinifi_farms"] = self.st["farms_error"]


# --------------------------------------------------------------------------
# 3. news: hack keywords × project / dependency names
# --------------------------------------------------------------------------
def _strict_hit(text: str, alias: str, strong: list[str]) -> bool:
    """«Bybit hacked», «Bybit suffers $1.5B exploit», «Bybit被盗» — but not «hacker sends funds to Bybit»."""
    for kw in strong:
        gap = 12 if not kw.isascii() else 45
        rx = r"(?<![A-Za-z0-9_])" + re.escape(alias) + r"(?![A-Za-z0-9_]).{0,%d}?" % gap + \
            (re.escape(kw) if not kw.isascii() else r"(?<![A-Za-z0-9_])" + re.escape(kw))
        if re.search(rx, text, re.I | re.S):
            return True
    return False


_SENT = re.compile(r"[。！？!?；;\n]+|(?<=[a-z0-9\)])\.\s+")


def sentences(text: str) -> list[str]:
    return [x.strip() for x in _SENT.split(text or "") if x.strip()]


def _hit_in(s: str, t: dict, kws: list[str], strong: list[str]) -> bool:
    extra = list(t.get("extra_keywords") or [])
    if not (tge.hits(s, kws) or (extra and tge.hits(s, extra))):
        return False
    if t["strict"]:
        return any(_strict_hit(s, a, strong + extra) for a in t["aliases"])
    return bool(tge.hits(s, t["aliases"]))


def match(item: dict, targets: list[dict], T: dict, scope_full: bool) -> list[dict]:
    """Where the words must meet:
    * a dependency — only in the headline (RSS title / first line of a Telegram post): a long post that mentions
      «Morpho» in one sentence and «黑客攻击» as a hypothetical in another is not about a Morpho hack;
    * your own project — in the headline or in one sentence of the post body (missing a real incident costs more).
    A headline about protection (bug bounty, «防范黑客», «protect against exploits») is skipped."""
    head = item["title"]
    if tge.hits(head, [str(k) for k in T.get("ignore_keywords", [])]):
        return []
    kws = [str(k) for k in T.get("hack_keywords", [])]
    strong = [str(k) for k in T.get("strong_keywords", [])]
    body = sentences(item["text"]) if scope_full else []
    out = []
    for t in targets:
        scopes = [head] + (body if t["own"] else [])
        scopes = [x for x in scopes if not tge.hits(x, list(t.get("exclude") or []))]
        if any(_hit_in(x, t, kws, strong) for x in scopes):
            out.append(t)
    own = [t for t in out if t["own"]]
    return own or out


class HackNews:
    def __init__(self, cfg: dict, state: dict, now: datetime, session):
        self.cfg, self.state, self.now, self.session = cfg, state, now, session
        self.st = _st(state)["news"]
        self.T = S(cfg).get("news") or {}
        self.tz = ZoneInfo(cfg.get("timezone", "Europe/Kyiv"))
        self.messages: list[str] = []

    def _guard(self, key: str, fn):
        n_alert = int(self.T.get("fail_alert_checks", 12))
        try:
            res = fn()
        except Exception as e:
            self.st["fails"][key] = self.st["fails"].get(key, 0) + 1
            if self.st["fails"][key] >= n_alert and not self.st["fail_alerted"].get(key):
                self.messages.append(f"⚠️ <b>УВАГА</b>\nДжерело новин про хаки <code>{esc(key)}</code> не відповідає "
                                     f"{self.st['fails'][key]} перевірок поспіль:\n<code>{esc(str(e)[:250])}</code>")
                self.st["fail_alerted"][key] = True
            print(f"[security] {key}: {str(e)[:200]}")
            return None
        if self.st["fail_alerted"].pop(key, None):
            self.messages.append(f"✅ Джерело новин про хаки <code>{esc(key)}</code> знову працює.")
        self.st["fails"].pop(key, None)
        return res

    def _fetch_rss(self, url: str) -> list[dict]:
        r = self.session.get(url, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        return tge.parse_rss(r.text, "rss")

    def sources(self) -> list[tuple[str, object, bool]]:
        """[(source key, fetcher, scope_full)] due on this run."""
        T, out = self.T, []
        every = float(T.get("check_hours", 0.2))
        slow = float(T.get("search_check_hours", 1))
        tgc = (self.cfg.get("tge") or {}).get("telegram_channels", []) or []
        for ch in T.get("telegram_channels") or tgc:
            out.append((f"tg:{ch}", lambda ch=ch: tge.telegram_channel(ch, self.session), True, every))
        for url in T.get("rss") or []:
            out.append((f"rss:{url.split('/')[2]}", lambda url=url: self._fetch_rss(url), False, every))
        ascii_kws = [k for k in T.get("search_keywords", []) if str(k).isascii()]
        for i, q in enumerate(T.get("gnews_queries") or []):
            out.append((f"gnews:{i}", lambda q=q: tge.google_news(q, ascii_kws, 1, self.session), False, every))
        for i, q in enumerate(T.get("bing_queries") or []):
            out.append((f"bing:{i}", lambda q=q: tge.bing_news(q, self.session), False, slow))
        for pk, p in ((self.cfg.get("tge") or {}).get("projects") or {}).items():
            if p.get("news_query"):
                out.append((f"gnews:own:{pk}", lambda p=p: tge.google_news(p["news_query"], ascii_kws, 2, self.session),
                            False, slow))
        return [(k, fn, full) for k, fn, full, hours in out if _due(self.st["checked"], k, hours, self.now)]

    def run(self) -> list[str]:
        targets = news_targets(self.cfg, self.state)
        if not targets:
            return []
        max_age = timedelta(hours=float(self.T.get("max_age_hours", 48)))
        cool = timedelta(hours=float(self.T.get("cooldown_hours", 12)))
        found: dict[str, tuple[dict, list[dict]]] = {}
        for key, fn, full in self.sources():
            items = self._guard(key, fn)
            if items is None:
                continue
            seeded = self.st["seeded"].get(key)
            for it in items:
                hits = match(it, targets, S(self.cfg), full)
                if not hits:
                    continue
                for t in hits:
                    tid = "t:" + tge._hid(t["key"], tge._norm_title(it["title"]))
                    new = it["id"] + t["key"] not in self.st["seen"] and tid not in self.st["seen"]
                    self.st["seen"][it["id"] + t["key"]] = self.st["seen"][tid] = iso(self.now)
                    if not new or not seeded:
                        continue
                    if it.get("ts") and self.now - it["ts"] > max_age:
                        continue
                    found.setdefault(t["key"], (t, []))[1].append(it)
            self.st["seeded"][key] = True
        for tkey, (t, items) in found.items():
            last = parse_iso(self.st["cooldown"].get(tkey))
            for it in items:
                self.st["items"].append({"target": tkey, "name": t["name"], "title": it["title"][:200],
                                         "link": it["link"], "via": it["via"], "ts": iso(it.get("ts") or self.now)})
            if last and self.now - last < cool:
                continue                                   # той самий сюжет з інших джерел — у /security
            self.st["cooldown"][tkey] = iso(self.now)
            self.messages.append(self._report(t, items))
        cutoff = self.now - timedelta(days=30)
        self.st["seen"] = {k: v for k, v in self.st["seen"].items() if (parse_iso(v) or self.now) >= cutoff}
        self.st["items"] = self.st["items"][-60:]
        return self.messages

    def _report(self, t: dict, items: list[dict]) -> str:
        if t["own"]:
            head = (f"🚨 <b>ТРИВОГА · можливий злам: {esc(t['name'])}</b>\n"
                    f"Новини пов'язують ваш протокол зі зламом / атакою:")
            tail = ("До офіційної заяви: нічого не вносьте й не підписуйте, не переходьте за «claim/withdraw»-лінками "
                    "з коментарів. On-chain стан контрактів — /security, ціни — /status.")
        else:
            who = dependents(self.cfg, self.state, t["dep"])
            head = (f"⚠️ <b>Можливий злам залежності: {esc(t['name'])}</b>\n"
                    + "\n".join(f"Стосується {esc(n)}: {esc(w)}" for n, w in who))
            tail = "Перевірте, чи це справді про ваш протокол, — бот шукає лише збіг назви і слова «hack/exploit»."
        lines = [head]
        for it in items[:4]:
            when = it["ts"].astimezone(self.tz).strftime("%d.%m %H:%M") if it.get("ts") else ""
            lines.append(f"• {esc(it['title'][:220])}\n  <i>{esc(it['via'])}{' · ' + when if when else ''}</i>\n  {esc(it['link'])}")
        if len(items) > 4:
            lines.append(f"… і ще {len(items) - 4}")
        lines.append(tail)
        return "\n".join(lines)


# --------------------------------------------------------------------------
# glue
# --------------------------------------------------------------------------
def collect(cfg: dict, state: dict, now: datetime, session, chains: dict[str, Chain]) -> tuple[list[dict], dict]:
    """On-chain + measured dependencies. Never raises."""
    if not S(cfg).get("enabled", True):
        return [], {}
    findings, errors = [], {}
    for part in (Onchain(cfg, state, now, chains), Measured(cfg, state, now, session, chains)):
        try:
            part.run()
        except Exception as e:
            errors[f"sec:{type(part).__name__.lower()}"] = str(e)
        findings += part.findings
        errors.update(part.errors)
    return findings, errors


def apply(engine, findings: list[dict], state: dict) -> None:
    log = _st(state)["log"]
    before = len(engine.messages)
    for f in findings:
        if f["type"] == "event":
            engine._event(f["text"], level=f["level"])
        else:
            engine._fire(f["id"], "security", f["level"], f["text"], f["recovery"])
    for m in engine.messages[before:]:
        log.append([iso(engine.now), re.sub(r"<[^>]+>", "", m).split("\n")[1][:160] if "\n" in m else m[:160]])
    del log[:-50]


def run_news(cfg: dict, state: dict, now: datetime, session) -> list[str]:
    if not S(cfg).get("enabled", True):
        return []
    msgs = HackNews(cfg, state, now, session).run()
    log = _st(state)["log"]
    for m in msgs:
        log.append([iso(now), re.sub(r"<[^>]+>", "", m).split("\n")[0][:160]])
    del log[:-50]
    return msgs


# --------------------------------------------------------------------------
# /deps and /security
# --------------------------------------------------------------------------
def deps_text(cfg: dict, state: dict, now: datetime, which: str = "") -> str:
    st = _st(state)
    tz = ZoneInfo(cfg.get("timezone", "Europe/Kyiv"))
    prices = st.get("prices") or {}
    sel = {k: p for k, p in projects(cfg).items() if not which or which in (k, str(p.get("name", "")).lower())}
    if not sel:
        return f"Не знаю проєкту «{esc(which)}». Доступні: {', '.join(projects(cfg))}."
    lines = ["🧩 <b>Від чого залежать ваші проєкти</b>" if not which else ""]
    for pk, p in sel.items():
        lines.append(f"\n<b>{esc(p.get('name', pk))}</b>")
        for dk, why in (p.get("deps") or {}).items():
            d = deps(cfg).get(dk, {"name": dk})
            extra = ""
            if dk in prices:
                extra = f" · {fmt.price(prices[dk])}"
            elif st["tvl"].get(dk):
                extra = f" · TVL {fmt.money(st['tvl'][dk][-1][1], 1)}"
            lines.append(f"• <b>{esc(d.get('name', dk))}</b> — {esc(why)}{extra}")
        if pk == "infinifi" and (st.get("farms") or {}).get("items"):
            f = st["farms"]
            at = parse_iso(f.get("at"))
            lines.append(f"<i>Розподіл активів infiniFi on-chain ({fmt.money(f['total'], 1)}, "
                         f"{at.astimezone(tz):%d.%m %H:%M}):</i>")
            agg: dict[str, float] = {}
            via: dict[str, set] = {}
            for it in f["items"]:
                k = it.get("dep") or it["name"]
                agg[k] = agg.get(k, 0) + it["usd"]
                via.setdefault(k, set()).update((it.get("deps") or [])[1:])
            for k, v in sorted(agg.items(), key=lambda x: -x[1])[:12]:
                name = deps(cfg).get(k, {}).get("name", k)
                also = ", ".join(deps(cfg).get(x, {}).get("name", x) for x in sorted(via.get(k) or []))
                lines.append(f"  {v / f['total'] * 100:5.1f}%  {esc(name)}{' через ' + esc(also) if also else ''} "
                             f"({fmt.money(v, 1)})")
        cs = [c for c in contract_list(cfg) if c["project"] == pk]
        if cs:
            watched = []
            for c in cs:
                rec = next((r for r in st["contracts"].values() if r.get("label") == f"{p.get('name', pk)} · {c['label']}"), None)
                kinds = ", ".join(rec["kinds"]) if rec else "ще не перевірено"
                watched.append(f"{esc(c['label'])} ({esc(kinds)})")
            lines.append("🛡 On-chain нагляд: " + "; ".join(watched))
    if not which:
        lines.append("\n<i>Один проєкт: /deps tori · Стан і останні інциденти: /security</i>")
    return "\n".join(x for x in lines if x is not None).strip()


def security_text(cfg: dict, state: dict, now: datetime) -> str:
    st = _st(state)
    tz = ZoneInfo(cfg.get("timezone", "Europe/Kyiv"))
    lines = ["🛡 <b>Безпека</b>", ""]
    alerts = state.get("alerts") or {}
    active = [k for k, v in alerts.items() if k.startswith("sec_") and v.get("active")]
    lines.append("Активні тривоги безпеки: " + (", ".join(esc(a) for a in active) if active else "немає ✅"))
    lines.append("")
    lines.append("<b>Контракти під наглядом</b>")
    for key, r in st["contracts"].items():
        v = r.get("values") or {}
        bits = []
        if "impl" in v:
            bits.append(f"impl {short(v['impl'])}")
        if "paused" in v:
            bits.append("⏸ ПАУЗА" if v["paused"] else "пауза: ні")
        if "supply" in v:
            bits.append(f"емісія {fmt.num(v['supply'])}")
        if "owner" in v:
            bits.append(f"owner {short(v['owner'])}")
        lines.append(f"• {esc(r.get('label', key))}: {esc(', '.join(bits) or 'без proxy/паузи')}")
    if st.get("prices"):
        lines.append("")
        lines.append("<b>Стейблкоїни-залежності</b>: " + ", ".join(
            f"{esc(deps(cfg).get(k, {}).get('name', k).split(' ')[0])} {fmt.price(v)}" for k, v in st["prices"].items()))
    items = st["news"]["items"][-5:]
    if items:
        lines.append("")
        lines.append("<b>Останні новини про хаки зі збігом</b>")
        for x in reversed(items):
            ts = parse_iso(x.get("ts"))
            lines.append(f"• {ts.astimezone(tz):%d.%m %H:%M} · {esc(x['name'])}: {esc(x['title'][:120])}\n  {esc(x['link'])}")
    chk = parse_iso(st.get("scanned_at"))
    lines.append("")
    lines.append(f"<i>Повна перевірка контрактів: {chk.astimezone(tz):%d.%m %H:%M}; далі — кожен запуск.</i>" if chk else
                 "<i>Перевірки ще не було — перша пройде при найближчому запуску.</i>")
    lines.append("<i>Залежності: /deps. Затримка: on-chain — до ~30 хв, новини — від хвилин до годин після X.</i>")
    return "\n".join(lines)


def status_line(cfg: dict, state: dict) -> str:
    st = _st(state)
    active = [k for k, v in (state.get("alerts") or {}).items() if k.startswith("sec_") and v.get("active")]
    paused = [r["label"] for r in st["contracts"].values() if (r.get("values") or {}).get("paused")]
    head = f"🛡 Безпека: {'⚠️ активних тривог ' + str(len(active)) if active else 'тривог немає ✅'}"
    if paused:
        head += " · на паузі: " + ", ".join(esc(p) for p in paused)
    return head + f" (контрактів під наглядом: {len(st['contracts'])}) — /security, /deps"


def digest_line(state: dict, now: datetime) -> str | None:
    st = _st(state)
    week = now - timedelta(days=7)
    n = sum(1 for ts, _ in st["log"] if (parse_iso(ts) or now) >= week)
    return f"🛡 Безпека: подій за тиждень — {n}" + (" (деталі: /security)" if n else " ✅")
