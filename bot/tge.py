"""TGE / airdrop announcements.

Projects announce on X, which has no free API, so the bot reads what reaches the open web within hours:
  * Google News and Bing News RSS search per project — crypto media (KuCoin flash, ChainCatcher, …) and
    exchange-listing posts re-publish X announcements; Bing indexes more of the small crypto sites;
  * public Telegram channels through the t.me/s/<channel> web preview: news (Wu Blockchain, Foresight News,
    BlockBeats) and exchange announcements (Binance, Bybit) — checked every run, they post fast;
  * new pages in the project docs (sitemap.xml) whose path mentions token / tge / airdrop / claim / …
A news item is reported when it names the project AND contains a TGE keyword. The first pass of every
source is silent: whatever already exists is remembered, only later additions are sent.

The user can also pin a confirmed date (/tge saturn 2026-11-20) — then the bot reminds 7 and 1 day before
and on the day itself.
"""
from __future__ import annotations

import hashlib
import html
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, unquote, urlsplit
from zoneinfo import ZoneInfo

import requests

from .storage import iso, parse_iso
from .telegram import esc

UA = {"User-Agent": "Mozilla/5.0 (compatible; defi-monitor-bot/1.1; +github actions)"}
TIMEOUT = 15
GNEWS_URL = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
BING_URL = "https://www.bing.com/news/search?q={q}&format=rss&setlang=en-US&cc=US"
LANGS = {"en", "es", "hi", "id", "ja", "ko", "vi", "zh", "zh-hans", "zh-hant", "zh-cn", "zh-tw", "pt", "pt-br",
         "fr", "de", "ru", "tr", "uk", "it", "pl", "ar", "th", "cn", "tw"}
# слова у шляху нової сторінки документації; «governance» навмисно немає — це цілі розділи
# (Saturn: /operations-and-governance/…, infiniFi: /dev-docs/governance/…), а не анонс токена
DOC_WORDS = {"token", "tokens", "tokenomics", "tge", "airdrop", "claim", "claims", "season", "snapshot",
             "distribution", "allocation", "vesting"}

_MONTHS = ("jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
           "sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?")
_DATE_RES = [
    re.compile(rf"\b(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+20\d\d)?\b", re.I),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{_MONTHS})\.?(?:,?\s+20\d\d)?\b", re.I),
    re.compile(r"\bQ[1-4]\s*20\d\d\b", re.I),
    re.compile(r"\b20\d\d-\d\d-\d\d\b"),
    re.compile(r"\b\d{1,2}\.\d{1,2}\.20\d\d\b"),
]


# --------------------------------------------------------------------------
# matching helpers
# --------------------------------------------------------------------------
def _term_re(term: str) -> re.Pattern:
    # межі слова лише по латиниці/цифрах: «$INFI» і «协议infiniFi宣布» збігаються, «INFINITY» — ні;
    # китайські ключові слова (空投 …) шукаються як підрядок
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(term) + r"(?![A-Za-z0-9_])", re.I)


def hits(text: str, terms: list[str]) -> list[str]:
    return [t for t in terms if _term_re(t).search(text or "")]


def dates_in(text: str) -> list[str]:
    out: list[str] = []
    for rx in _DATE_RES:
        for m in rx.finditer(text or ""):
            s = m.group(0).strip()
            if s.lower() not in (x.lower() for x in out):
                out.append(s)
    return out[:4]


def _strip_tags(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _norm_title(t: str) -> str:
    t = re.sub(r"\s+-\s+[^-]{2,60}$", "", t or "")          # Google News appends " - Source"
    return re.sub(r"[\W_]+", " ", t.lower()).strip()       # \W з Unicode: китайські заголовки не злипаються в ""


def hid_title(t: str) -> str:
    return _hid(_norm_title(t))


def _hid(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# fetchers (each returns plain dicts, raises on failure)
# --------------------------------------------------------------------------
def _kw_group(keywords: list[str]) -> str:
    return " OR ".join(f'"{k}"' if " " in k else k for k in keywords if k.isascii())


def google_news(query: str, keywords: list[str], days: int = 14, session=None) -> list[dict]:
    q = f"{query} ({_kw_group(keywords)}) when:{days}d"
    r = (session or requests).get(GNEWS_URL.format(q=quote_plus(q)), headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return parse_rss(r.text, "gn")


def bing_news(query: str, session=None) -> list[dict]:
    """Bing News RSS. Only a plain query works: with «( … OR … )» Bing returns an empty feed, so keywords
    are applied locally. No date operator — stale items are dropped later by max_age_days."""
    r = (session or requests).get(BING_URL.format(q=quote_plus(query)), headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    if not r.text.lstrip().startswith("<?xml") and "<rss" not in r.text[:500]:
        raise RuntimeError("Bing повернув не RSS (можливо, капча)")
    return parse_rss(r.text, "bn")


def _unwrap(link: str) -> str:
    """Bing wraps links: …/apiclick.aspx?…&url=https%3a%2f%2fsite%2fpage&… → the real URL."""
    m = re.search(r"[?&]url=([^&]+)", link or "")
    return unquote(m.group(1)) if m and "bing.com" in link else link


def parse_rss(xml_text: str, prefix: str = "gn") -> list[dict]:
    root = ET.fromstring(xml_text)
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = _unwrap((it.findtext("link") or "").strip())
        ts = None
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(timezone.utc)
        except Exception:
            pass
        src = next((c for c in it if c.tag == "source" or c.tag.endswith("}Source")), None)   # Bing: News:Source
        out.append({
            "id": f"{prefix}:" + ((it.findtext("guid") or "").strip() or _hid(title, link)),
            "title": title,
            "text": title + " " + _strip_tags(it.findtext("description") or ""),
            "link": link,
            "ts": ts,
            "via": (src.text or "").strip() if src is not None and src.text else
                   ("Google News" if prefix == "gn" else "Bing News"),
        })
    return out


def telegram_channel(channel: str, session=None) -> list[dict]:
    r = (session or requests).get(f"https://t.me/s/{channel}", headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    posts = parse_tme(r.text)
    if not posts and "tgme_widget_message" not in r.text:
        raise RuntimeError(f"t.me/s/{channel}: сторінка без постів (канал приватний або змінилась розмітка)")
    return posts


class _TmeParser(HTMLParser):
    """t.me/s/<channel> → posts. Text is taken only from the post's own .tgme_widget_message_text:
    a reply quotes the original post inside .tgme_widget_message_reply with the same class (RSSHub
    selects `.tgme_widget_message_bubble > .tgme_widget_message_text` for the same reason)."""
    VOID = {"br", "img", "meta", "link", "input", "hr", "source", "wbr", "area", "col", "embed", "param", "track"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, set]] = []
        self.posts: list[dict] = []
        self.cur: dict | None = None
        self.text_depth: int | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = set((a.get("class") or "").split())
        if tag == "br" and self.text_depth is not None:
            self.cur["text"].append("\n")
        if tag in self.VOID:
            return
        self.stack.append((tag, cls))
        if "tgme_widget_message" in cls and a.get("data-post"):
            self.cur = {"post": a["data-post"], "text": [], "ts": None, "has_text": False}
            self.posts.append(self.cur)
        elif self.cur is not None:
            in_reply = any("tgme_widget_message_reply" in c for _, c in self.stack[:-1])
            if "tgme_widget_message_text" in cls and self.text_depth is None and not in_reply and not self.cur["has_text"]:
                self.text_depth = len(self.stack)
                self.cur["has_text"] = True
            if tag == "time" and a.get("datetime") and self.cur["ts"] is None:
                self.cur["ts"] = a["datetime"]

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        while self.stack:                        # tolerate unclosed tags
            t, _ = self.stack.pop()
            if self.text_depth is not None and len(self.stack) < self.text_depth:
                self.text_depth = None
            if t == tag:
                break

    def handle_data(self, data):
        if self.text_depth is not None and self.cur is not None:
            self.cur["text"].append(data)


def parse_tme(page: str) -> list[dict]:
    p = _TmeParser()
    p.feed(page)
    out = []
    for x in p.posts:
        text = re.sub(r"[ \t]+\n", "\n", "".join(x["text"])).strip()
        ts = None
        if x["ts"]:
            try:
                ts = datetime.fromisoformat(x["ts"]).astimezone(timezone.utc)
            except ValueError:
                pass
        if text:
            out.append({"id": f"tg:{x['post']}", "title": text.split("\n", 1)[0][:200], "text": text,
                        "link": f"https://t.me/{x['post']}", "ts": ts, "via": f"Telegram @{x['post'].split('/')[0]}"})
    return out


def sitemap_urls(url: str, session=None, depth: int = 0) -> set[str]:
    r = (session or requests).get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    locs = [(e.text or "").strip() for e in root.iter() if e.tag.endswith("loc")]
    if root.tag.endswith("sitemapindex"):
        if depth >= 1:
            return set()
        out: set[str] = set()
        for child in locs[:10]:
            out |= sitemap_urls(child, session, depth + 1)
        return out
    return {u for u in locs if u}


def doc_path(url: str) -> str:
    """Path without the language prefix, so /es/resources/cores == /resources/cores."""
    segs = [s for s in urlsplit(url).path.split("/") if s]
    if len(segs) > 1 and segs[0].lower() in LANGS:
        segs = segs[1:]
    return "/" + "/".join(segs)


def doc_hit(path: str, extra: list[str]) -> list[str]:
    words = set(re.split(r"[^a-z0-9]+", path.lower()))
    return sorted(words & (DOC_WORDS | {e.lower() for e in extra if e}))


# --------------------------------------------------------------------------
# state helpers
# --------------------------------------------------------------------------
def _st(state: dict) -> dict:
    st = state.setdefault("tge", {})
    for k, v in (("seen", {}), ("seeded", {}), ("items", []), ("dates", {}), ("reminded", {}),
                 ("fails", {}), ("fail_alerted", {}), ("doc_paths", {}), ("checked_at", None)):
        st.setdefault(k, v)
    return st


def projects(cfg: dict) -> dict:
    return (cfg.get("tge") or {}).get("projects") or {}


def news_names(p: dict, key: str = "") -> list[str]:
    """Names a news headline must contain. strict_title: only aliases — a bare «Tori» is also Tori Dunlap."""
    aliases = list(p.get("aliases", []))
    return aliases if p.get("strict_title") else [p.get("name", key)] + aliases


def resolve_project(cfg: dict, word: str) -> str | None:
    w = (word or "").lower().lstrip("$#/")
    for key, p in projects(cfg).items():
        names = {key, str(p.get("name", "")).lower(), str(p.get("ticker", "")).lower()}
        names |= {str(a).lower().lstrip("$") for a in p.get("short", [])}
        if w in names - {""}:
            return key
    return None


def parse_date(s: str, today: date) -> date | None:
    s = s.strip()
    for f in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})", s)
    if m:
        try:
            d = date(today.year, int(m.group(2)), int(m.group(1)))
            return d if d >= today - timedelta(days=1) else date(today.year + 1, d.month, d.day)
        except ValueError:                       # 31.02, 29.02 у невисокосний рік
            return None
    return None


# --------------------------------------------------------------------------
# main pass
# --------------------------------------------------------------------------
class Watcher:
    def __init__(self, cfg: dict, state: dict, now: datetime, session=None):
        self.cfg, self.now = cfg, now
        self.T = cfg.get("tge") or {}
        self.st = _st(state)
        self.session = session or requests.Session()
        self.tz = ZoneInfo(cfg.get("timezone", "Europe/Kyiv"))
        self.messages: list[str] = []

    # ---- reminders for dates the user pinned ------------------------------
    def reminders(self) -> None:
        today = self.now.astimezone(self.tz).date()
        remind = sorted((int(x) for x in self.T.get("remind_days", [7, 1])), reverse=True)
        for key, d in list(self.st["dates"].items()):
            p = projects(self.cfg).get(key, {"name": key})
            try:
                day = date.fromisoformat(d)
            except ValueError:
                continue
            left = (day - today).days
            sent = self.st["reminded"].setdefault(key, [])
            hint = f"\n{esc(p['hint'])}" if p.get("hint") else ""
            if left == 0 and "0" not in sent:
                self.messages.append(
                    f"🚀 <b>Сьогодні TGE {esc(p['name'])}</b> ({day:%d.%m.%Y}).{hint}\n"
                    f"Claim — лише за посиланням з офіційного X/Discord проєкту: у день TGE багато фішингових "
                    f"«claim»-сайтів. Нічого не підписуйте з посилань у коментарях і DM.")
                sent.append("0")
            elif left > 0:
                crossed = [r for r in remind if left <= r and str(r) not in sent]
                if crossed:
                    self.messages.append(f"⏰ TGE {esc(p['name'])} — через <b>{left} дн.</b> ({day:%d.%m.%Y}).{hint}")
                    sent.extend(str(r) for r in remind if left <= r and str(r) not in sent)

    # ---- one source -------------------------------------------------------
    def _guard(self, key: str, fn):
        """Run a fetcher; count consecutive failures, report once after N, report recovery."""
        n_alert = int(self.T.get("telegram_fail_alert_checks", 12) if key.startswith("tg:")
                      else self.T.get("fail_alert_checks", 6))
        try:
            res = fn()
        except Exception as e:
            self.st["fails"][key] = self.st["fails"].get(key, 0) + 1
            if self.st["fails"][key] >= n_alert and not self.st["fail_alerted"].get(key):
                self.messages.append(f"⚠️ <b>УВАГА</b>\nДжерело TGE-новин <code>{esc(key)}</code> не відповідає "
                                     f"{self.st['fails'][key]} перевірок поспіль:\n<code>{esc(str(e)[:250])}</code>")
                self.st["fail_alerted"][key] = True
            print(f"[tge] {key}: {str(e)[:200]}")
            return None
        if self.st["fail_alerted"].pop(key, None):
            self.messages.append(f"✅ Джерело TGE-новин <code>{esc(key)}</code> знову працює.")
        self.st["fails"].pop(key, None)
        return res

    def _accept(self, key: str, item: dict, project: str, found: dict) -> None:
        tid = "t:" + _hid(project, _norm_title(item["title"]))
        seen = self.st["seen"]
        new = item["id"] not in seen and tid not in seen
        seen[item["id"]] = seen[tid] = iso(self.now)
        if not new or not self.st["seeded"].get(key):
            return
        max_age = timedelta(days=float(self.T.get("max_age_days", 7)))
        if item.get("ts") and self.now - item["ts"] > max_age:
            return
        found.setdefault(project, []).append(item)

    # ---- full pass --------------------------------------------------------
    def _due(self, stamp: str, hours: float, force: bool) -> bool:
        last = parse_iso(self.st.get(stamp))
        if force or not last or self.now - last >= timedelta(hours=hours):
            self.st[stamp] = iso(self.now)
            return True
        return False

    def _relevant(self, p: dict, text: str) -> bool:
        return not hits(text, list(p.get("exclude", [])))

    def run(self, force: bool = False) -> list[str]:
        if not projects(self.cfg):
            return []
        self.reminders()
        news_due = self._due("checked_at", float(self.T.get("check_hours", 2)), force)
        # Telegram-канали пишуть швидко (Foresight News ~11 постів/год, а t.me/s віддає лише 20 останніх) — частіше
        tg_due = self._due("tg_checked_at", float(self.T.get("telegram_check_hours", 0.25)), force)
        if not (news_due or tg_due):
            return self.messages
        kws = [str(k) for k in self.T.get("keywords", [])]
        news_kws = self.T.get("news_keywords", kws)
        found: dict[str, list[dict]] = {}

        for key, p in projects(self.cfg).items() if news_due else ():
            names = news_names(p, key)
            # Google News + Bing News: the query pins the project; the title must name it and carry a keyword
            engines = []
            if p.get("news_query"):
                engines.append(("gnews", lambda p=p: google_news(p["news_query"], news_kws,
                                                                 int(self.T.get("news_days", 14)), self.session)))
            for i, bq in enumerate(p.get("bing_queries") or []):
                engines.append((f"bing{i or ''}", lambda bq=bq: bing_news(bq, self.session)))
            for eng, fn in engines:
                if eng.rstrip("0123456789") not in (self.T.get("news_engines") or ["gnews", "bing"]):
                    continue
                sk = f"{eng}:{key}"
                items = self._guard(sk, fn)
                if items is not None:
                    for it in items:
                        if hits(it["title"], names) and hits(it["title"], kws) and self._relevant(p, it["title"]):
                            self._accept(sk, it, key, found)
                    self.st["seeded"][sk] = True
            # docs: new pages whose path looks token-related
            if p.get("docs_sitemap"):
                sk = f"docs:{key}"
                urls = self._guard(sk, lambda p=p: sitemap_urls(p["docs_sitemap"], self.session))
                if urls is not None:
                    paths = {doc_path(u): u for u in sorted(urls)}
                    known = set(self.st["doc_paths"].get(key, []))
                    if self.st["seeded"].get(sk):
                        ignore = tuple(p.get("docs_ignore") or ())
                        for path in sorted(set(paths) - known):
                            if ignore and path.startswith(ignore):
                                continue
                            words = doc_hit(path, [p.get("ticker", "")])
                            if words:
                                found.setdefault(key, []).append({
                                    "id": f"doc:{key}:{path}", "title": f"Нова сторінка документації: {path}",
                                    "text": path, "link": paths[path], "ts": self.now, "via": "docs (sitemap)"})
                    self.st["doc_paths"][key] = sorted(known | set(paths))
                    self.st["seeded"][sk] = True

        # public Telegram channels: strict aliases (a bare «Saturn» is too ambiguous) + keyword
        for ch in (self.T.get("telegram_channels", []) or []) if tg_due else ():
            sk = f"tg:{ch}"
            posts = self._guard(sk, lambda ch=ch: telegram_channel(ch, self.session))
            if posts is None:
                continue
            for it in posts:
                if not hits(it["text"], kws):
                    continue
                for key, p in projects(self.cfg).items():
                    if hits(it["text"], list(p.get("aliases", []))) and self._relevant(p, it["text"]):
                        self._accept(sk, it, key, found)
            self.st["seeded"][sk] = True

        for key, items in found.items():
            self.messages.append(self._report(key, items))
            for it in items:
                self.st["items"].append({"project": key, "title": it["title"][:200], "link": it["link"],
                                         "ts": iso(it["ts"] or self.now), "via": it["via"],
                                         "dates": dates_in(it.get("text", ""))})
        self._prune()
        return self.messages

    def _report(self, key: str, items: list[dict]) -> str:
        p = projects(self.cfg).get(key, {"name": key})
        lines = [f"📣 <b>TGE / airdrop · {esc(p['name'])}</b>"]
        for it in items[:5]:
            when = it["ts"].astimezone(self.tz).strftime("%d.%m %H:%M") if it.get("ts") else ""
            ds = dates_in(it.get("text", ""))
            lines.append(f"• {esc(it['title'][:220])}\n  <i>{esc(it['via'])}{' · ' + when if when else ''}</i>"
                         + (f" · 📅 у тексті: <b>{esc(', '.join(ds))}</b>" if ds else "")
                         + f"\n  {esc(it['link'])}")
        if len(items) > 5:
            lines.append(f"… і ще {len(items) - 5}")
        if p.get("status"):
            lines.append(f"Що було відомо: {esc(p['status'])}")
        if p.get("hint"):
            lines.append(esc(p["hint"]))
        lines.append(f"Якщо дата підтверджена — <code>/tge {esc(key)} РРРР-ММ-ДД</code>, і я нагадаю заздалегідь.")
        return "\n".join(lines)

    def _prune(self) -> None:
        cutoff = self.now - timedelta(days=60)
        self.st["seen"] = {k: v for k, v in self.st["seen"].items() if (parse_iso(v) or self.now) >= cutoff}
        self.st["items"] = self.st["items"][-40:]


def run_tge(cfg: dict, state: dict, now: datetime, force: bool = False) -> list[str]:
    return Watcher(cfg, state, now).run(force=force)


# --------------------------------------------------------------------------
# text for /tge and the weekly digest
# --------------------------------------------------------------------------
def status_text(cfg: dict, state: dict, now: datetime) -> str:
    st = _st(state)
    tz = ZoneInfo(cfg.get("timezone", "Europe/Kyiv"))
    today = now.astimezone(tz).date()
    lines = ["🪙 <b>TGE / airdrop</b>", ""]
    for key, p in projects(cfg).items():
        lines.append(f"<b>{esc(p.get('name', key))}</b> — {esc(p.get('status', ''))}")
        d = st["dates"].get(key)
        if d:
            left = (date.fromisoformat(d) - today).days
            lines.append(f"📅 ваша дата: <b>{date.fromisoformat(d):%d.%m.%Y}</b> "
                         + (f"(через {left} дн.)" if left > 0 else "(сьогодні)" if left == 0 else "(минула)"))
        mine = [x for x in st["items"] if x.get("project") == key][-3:]
        for x in reversed(mine):
            ts = parse_iso(x.get("ts"))
            lines.append(f"• {ts.astimezone(tz):%d.%m} · {esc(x['title'][:140])}"
                         + (f" (📅 {esc(', '.join(x['dates']))})" if x.get("dates") else "")
                         + f"\n  {esc(x['link'])}")
        lines.append("")
    T = cfg.get("tge") or {}
    chk = parse_iso(st.get("checked_at"))
    srcs = ["Google News"] + [f"Telegram @{c}" for c in T.get("telegram_channels", []) or []] + ["docs (sitemap)"]
    lines.append(f"<i>Остання перевірка: {chk.astimezone(tz):%d.%m %H:%M} (Київ)</i>" if chk else
                 "<i>Перевірки ще не було — перша пройде при найближчому запуску (тихо: лише запам'ятає наявне).</i>")
    lines.append(f"<i>Джерела: {esc(', '.join(srcs))}. X (Twitter) напряму не читається — анонси звідти "
                 f"приходять через передруки, зазвичай за кілька годин.</i>")
    lines.append("Задати дату: <code>/tge saturn 2026-11-20</code> · прибрати: <code>/tge saturn off</code>")
    return "\n".join(lines)


def digest_lines(cfg: dict, state: dict, now: datetime) -> list[str]:
    st = _st(state)
    if not projects(cfg):
        return []
    tz = ZoneInfo(cfg.get("timezone", "Europe/Kyiv"))
    today = now.astimezone(tz).date()
    week = now - timedelta(days=7)
    lines = ["<b>TGE / airdrop</b>"]
    for key, p in projects(cfg).items():
        d = st["dates"].get(key)
        if d:
            left = (date.fromisoformat(d) - today).days
            when = f"TGE <b>{date.fromisoformat(d):%d.%m}</b>" + (f" (через {left} дн.)" if left > 0 else "")
        else:
            when = esc(p.get("expected", "дата не оголошена"))
        n = sum(1 for x in st["items"] if x.get("project") == key and (parse_iso(x.get("ts")) or now) >= week)
        lines.append(f"{esc(p.get('name', key))}: {when}" + (f" · нових згадок за тиждень: {n}" if n else ""))
    lines.append("Деталі й останні новини: /tge")
    return lines


def cmd_tge(cfg: dict, state: dict, now: datetime, arg: str) -> str:
    st = _st(state)
    parts = arg.split()
    if not parts:
        return status_text(cfg, state, now)
    key = resolve_project(cfg, parts[0])
    if not key:
        return f"Не знаю проєкту «{esc(parts[0])}». Доступні: {', '.join(projects(cfg))}."
    name = projects(cfg)[key].get("name", key)
    if len(parts) == 1:
        d = st["dates"].get(key)
        return f"{esc(name)}: дата TGE {'— ' + d if d else 'не задана'}. Формат: <code>/tge {key} 2026-11-20</code>"
    if parts[1].lower() in ("off", "-", "clear", "none", "нема", "видалити"):
        st["dates"].pop(key, None)
        st["reminded"].pop(key, None)
        return f"Дату TGE {esc(name)} прибрано."
    today = now.astimezone(ZoneInfo(cfg.get("timezone", "Europe/Kyiv"))).date()
    d = parse_date(parts[1], today)
    if not d:
        return "Не розпізнав дату. Формат: <code>2026-11-20</code> або <code>20.11.2026</code>."
    st["dates"][key] = d.isoformat()
    left = (d - today).days
    days = [int(x) for x in (cfg.get("tge") or {}).get("remind_days", [7, 1])]
    st["reminded"][key] = [str(r) for r in days if left <= r]     # уже минулі пороги не дублюємо
    remind = ", ".join(str(x) for x in days)
    return (f"✅ TGE {esc(name)}: <b>{d:%d.%m.%Y}</b> (через {left} дн.). Нагадаю за {remind} дн. і в день TGE."
            + (f"\n{esc(projects(cfg)[key]['hint'])}" if projects(cfg)[key].get("hint") else ""))
