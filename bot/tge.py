"""TGE / airdrop announcements.

Projects announce on X, which has no free API, so the bot reads what reaches the open web within hours:
  * Google News RSS search per project — crypto media and exchange-listing posts re-publish X announcements;
  * public Telegram channels through the t.me/s/<channel> web preview (default: Wu Blockchain);
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
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlsplit
from zoneinfo import ZoneInfo

import requests

from .storage import iso, parse_iso
from .telegram import esc

UA = {"User-Agent": "Mozilla/5.0 (compatible; defi-monitor-bot/1.1; +github actions)"}
TIMEOUT = 15
GNEWS_URL = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
LANGS = {"en", "es", "hi", "id", "ja", "ko", "vi", "zh", "zh-hans", "zh-hant", "zh-cn", "zh-tw", "pt", "pt-br",
         "fr", "de", "ru", "tr", "uk", "it", "pl", "ar", "th", "cn", "tw"}
DOC_WORDS = {"token", "tokens", "tokenomics", "tge", "airdrop", "claim", "claims", "season", "snapshot",
             "distribution", "rewards", "governance", "allocation", "vesting"}

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
    return re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.I)     # «$INFI» теж збігається з INFI


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
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def _hid(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# fetchers (each returns plain dicts, raises on failure)
# --------------------------------------------------------------------------
def google_news(query: str, keywords: list[str], days: int = 14, session=None) -> list[dict]:
    kw = " OR ".join(f'"{k}"' if " " in k else k for k in keywords)
    q = f"{query} ({kw}) when:{days}d"
    r = (session or requests).get(GNEWS_URL.format(q=quote_plus(q)), headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return parse_rss(r.text)


def parse_rss(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        ts = None
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(timezone.utc)
        except Exception:
            pass
        src = it.find("source")
        out.append({
            "id": "gn:" + ((it.findtext("guid") or "").strip() or _hid(title, link)),
            "title": title,
            "text": title + " " + _strip_tags(it.findtext("description") or ""),
            "link": link,
            "ts": ts,
            "via": (src.text or "").strip() if src is not None and src.text else "Google News",
        })
    return out


def telegram_channel(channel: str, session=None) -> list[dict]:
    r = (session or requests).get(f"https://t.me/s/{channel}", headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    posts = parse_tme(r.text)
    if not posts and "tgme_widget_message" not in r.text:
        raise RuntimeError(f"t.me/s/{channel}: сторінка без постів (канал приватний або змінилась розмітка)")
    return posts


def parse_tme(page: str) -> list[dict]:
    out = []
    parts = re.split(r'(?=<div class="tgme_widget_message_wrap)', page)
    for block in parts:
        m = re.search(r'data-post="([^"]+)"', block)
        if not m:
            continue
        post = m.group(1)
        tm = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        text = _strip_tags(tm.group(1)) if tm else ""
        dm = re.search(r'<time[^>]*datetime="([^"]+)"', block)
        ts = None
        if dm:
            try:
                ts = datetime.fromisoformat(dm.group(1)).astimezone(timezone.utc)
            except ValueError:
                pass
        if text:
            first = text.split("\n", 1)[0]
            out.append({"id": f"tg:{post}", "title": first[:200], "text": text, "link": f"https://t.me/{post}",
                        "ts": ts, "via": f"Telegram @{post.split('/')[0]}"})
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
        d = date(today.year, int(m.group(2)), int(m.group(1)))
        return d if d >= today - timedelta(days=1) else date(today.year + 1, d.month, d.day)
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
        n_alert = int(self.T.get("fail_alert_checks", 6))
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
    def run(self, force: bool = False) -> list[str]:
        if not projects(self.cfg):
            return []
        self.reminders()
        last = parse_iso(self.st.get("checked_at"))
        if not force and last and self.now - last < timedelta(hours=float(self.T.get("check_hours", 2))):
            return self.messages
        self.st["checked_at"] = iso(self.now)
        kws = [str(k) for k in self.T.get("keywords", [])]
        found: dict[str, list[dict]] = {}

        for key, p in projects(self.cfg).items():
            names = [p.get("name", key)] + list(p.get("aliases", []))
            # Google News: the query pins the project; title must name it and carry a keyword
            if p.get("news_query") and self.T.get("google_news", True):
                sk = f"gnews:{key}"
                items = self._guard(sk, lambda p=p: google_news(p["news_query"], self.T.get("news_keywords", kws),
                                                                int(self.T.get("news_days", 14)), self.session))
                if items is not None:
                    for it in items:
                        if hits(it["title"], names) and hits(it["title"], kws):
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
                        for path in sorted(set(paths) - known):
                            words = doc_hit(path, [p.get("ticker", "")])
                            if words:
                                found.setdefault(key, []).append({
                                    "id": f"doc:{key}:{path}", "title": f"Нова сторінка документації: {path}",
                                    "text": path, "link": paths[path], "ts": self.now, "via": "docs (sitemap)"})
                    self.st["doc_paths"][key] = sorted(known | set(paths))
                    self.st["seeded"][sk] = True

        # public Telegram channels: strict aliases (a bare «Saturn» is too ambiguous) + keyword
        for ch in self.T.get("telegram_channels", []) or []:
            sk = f"tg:{ch}"
            posts = self._guard(sk, lambda ch=ch: telegram_channel(ch, self.session))
            if posts is None:
                continue
            for it in posts:
                if not hits(it["text"], kws):
                    continue
                for key, p in projects(self.cfg).items():
                    if hits(it["text"], list(p.get("aliases", []))):
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
