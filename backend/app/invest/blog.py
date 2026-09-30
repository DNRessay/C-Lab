# EasyEquities' public blog: the Weekly Dividends Update and the monthly Dividends List. Read nightly into
# shared tables (the same for every user); each user's view marks what they hold or watch.
import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional

from bs4 import BeautifulSoup
from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from ..db import Base
from ..models import BigId, created, pk, text, utcnow
from .prices import HEADERS, HTTP_KW, http

log = logging.getLogger(__name__)

BLOG = "https://blogs.easyequities.co.za"
LISTINGS = [f"{BLOG}/topic/dividends-update", f"{BLOG}/topic/dividends-update/page/2", f"{BLOG}/"]
SKIP_PATHS = ("/topic/", "/author/", "/page/", "/tag/", "/rss", "/all")

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DATE = r"(\d{1,2})(?:st|nd|rd|th)?[\s\-/]+([A-Za-z]{3,9})[\s\-/,]+(\d{4})|(\d{4})[-/](\d{2})[-/](\d{2})|(\d{1,2})/(\d{1,2})/(\d{4})"
LDT_RE = re.compile(r"(?:last\s+(?:day|date)\s+(?:to|of)\s+trad(?:e|ing)|last\s+trading\s+(?:day|date)|\bLDT\b)\s*[:\-•]?\s*(" + DATE + ")", re.I)
PAY_RE = re.compile(r"(?:pay(?:ment)?\s+date|paid\s+on|\bpay\b)\s*[:\-•]?\s*(" + DATE + ")", re.I)
AMOUNT_RE = re.compile(r"(?<![\w.])(R|ZAR|US\$|\$|USD|A\$|AUD|£|GBP|€|EUR)\s?(\d{1,3}(?:[ ,]\d{3})*(?:\.\d+)?|\d*\.\d+)"
                       r"|(\d+(?:\.\d+)?)\s?(c|cents|ZAc|cps)\b", re.I)
CURRENCY = {"r": "ZAR", "zar": "ZAR", "$": "USD", "us$": "USD", "usd": "USD", "a$": "AUD", "aud": "AUD", "£": "GBP",
            "gbp": "GBP", "€": "EUR", "eur": "EUR"}
ACCOUNT_RE = re.compile(r"\b(ZAR|USD|AUD|GBP|EUR|TFSA)\b[^\n]{0,20}\baccount", re.I)
NOISE = re.compile(r"[☀-➿\U0001F000-\U0001FAFF️▪•·|‍]+")
SUFFIXES = re.compile(r"\b(ltd|limited|inc|incorporated|plc|corp|corporation|co|company|holdings?|group|n\.?v|s\.?a|ag|se|"
                      r"the|class [a-z]|ordinary shares?|ord|reit|etf|ametf|feeder)\b\.?", re.I)


class BlogPost(Base):
    __tablename__ = "ee_blog_posts"
    id: Mapped[int] = pk()
    url: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    title: Mapped[str] = text(300)
    kind: Mapped[str] = text(20)  # weekly_dividends | monthly_dividends | news
    published: Mapped[Optional[date]] = mapped_column(Date, nullable=True, index=True)
    summary: Mapped[str] = text()
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = text(20)
    error: Mapped[str] = text()
    fetched_at: Mapped[datetime] = created()


class BlogDividend(Base):
    __tablename__ = "ee_blog_dividends"
    __table_args__ = (UniqueConstraint("post_id", "account", "instrument"),)
    id: Mapped[int] = pk()
    post_id: Mapped[int] = mapped_column(BigId, ForeignKey("ee_blog_posts.id", ondelete="CASCADE"), nullable=False, index=True)
    account: Mapped[str] = text(10)
    instrument: Mapped[str] = text(200)
    amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = text(3)
    ldt: Mapped[Optional[date]] = mapped_column(Date, nullable=True, index=True)
    pay_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    line: Mapped[str] = text()


def _get(url):
    r = http.get(url, headers=HEADERS, timeout=25, **HTTP_KW)
    r.raise_for_status()
    return r.text


def _date(groups) -> Optional[date]:
    g = [x for x in groups]
    try:
        if g[0]:
            month = MONTHS.get(g[1][:3].lower())
            return date(int(g[2]), month, int(g[0])) if month else None
        if g[3]:
            return date(int(g[3]), int(g[4]), int(g[5]))
        if g[6]:
            return date(int(g[8]), int(g[7]), int(g[6]))
    except ValueError:
        return None
    return None


def kind_of(title: str, url: str) -> str:
    t = f"{title} {url}".lower()
    if "dividend" in t and ("list" in t or re.search(r"(january|february|march|april|may|june|july|august|september|"
                                                     r"october|november|december) \d{4} dividends", t)):
        return "monthly_dividends"
    if "dividend" in t:
        return "weekly_dividends"
    return "news"


def post_links(html: str):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"].split("#")[0].split("?")[0].rstrip("/")
        if href.startswith("/"):
            href = BLOG + href
        if not href.startswith(BLOG + "/") or any(s in href for s in SKIP_PATHS) or href.count("/") != 3:
            continue
        if href not in out:
            out.append(href)
    return out


def content(html: str):
    """(title, published date, main-content soup)."""
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.find("meta", property="og:title") or {}).get("content") or (soup.title.string if soup.title else "")
    published = None
    for key in ("article:published_time", "og:published_time"):
        m = soup.find("meta", property=key)
        if m and m.get("content"):
            try:
                published = datetime.fromisoformat(m["content"][:10]).date()
            except ValueError:
                pass
    main = (soup.select_one("[class*=post-body]") or soup.select_one("article") or soup.select_one("main") or soup.body or soup)
    for tag in main.find_all(["script", "style", "nav", "footer", "form"]):
        tag.decompose()
    return (title or "").strip(), published, main


def lines_of(main) -> list:
    """Table rows as ' | '-joined cells, everything else as text lines (block elements split lines)."""
    out = []
    for table in main.find_all("table"):
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if any(cells):
                out.append(" | ".join(cells))
        table.replace_with(BeautifulSoup("<p></p>", "html.parser"))
    for br in main.find_all("br"):
        br.replace_with("\n")
    for block in main.find_all(["p", "li", "h1", "h2", "h3", "h4", "h5", "div"]):
        block.insert_after("\n")
    return out + [ln.strip() for ln in main.get_text("").splitlines() if ln.strip()]


def clean_name(s: str) -> str:
    s = NOISE.sub(" ", s)
    s = re.sub(r"^\s*[-–—*\d.)]+\s+", "", s)
    return re.sub(r"\s{2,}", " ", s).strip(" :-–—|,")


def parse_dividends(lines, year_hint=None):
    """Each line naming an instrument with an amount and dates. Account sections (ZAR/USD Account headings) carry over;
    table rows whose dates sit in separate cells work too."""
    account, found, header = "ZAR", [], None
    for raw in lines:
        line = NOISE.sub(" • ", raw)
        acc = ACCOUNT_RE.search(line)
        if acc and not AMOUNT_RE.search(line):
            account = acc.group(1).upper()
            continue
        cells = [c.strip() for c in raw.split(" | ")] if " | " in raw else None
        if cells and not AMOUNT_RE.search(raw) and any(re.search(r"last|ldt|pay", c, re.I) for c in cells):
            header = [c.lower() for c in cells]
            continue
        amt = AMOUNT_RE.search(line)
        if not amt:
            continue
        ldt = LDT_RE.search(line)
        pay = PAY_RE.search(line)
        ldt_d = _date(ldt.groups()[1:]) if ldt else None
        pay_d = _date(pay.groups()[1:]) if pay else None
        if cells and header and len(cells) == len(header) and not (ldt_d and pay_d):
            for h, c in zip(header, cells):
                m = re.search(DATE, c)
                if m and ("last" in h or "ldt" in h) and not ldt_d:
                    ldt_d = _date(m.groups())
                elif m and "pay" in h and not pay_d:
                    pay_d = _date(m.groups())
        if not ldt_d and not pay_d:
            continue
        name = clean_name(cells[0] if cells else line[:amt.start()])
        if len(name) < 2 or len(name) > 160:
            continue
        if amt.group(2):
            value, cur = float(amt.group(2).replace(",", "").replace(" ", "")), CURRENCY.get(amt.group(1).lower(), "")
        else:
            value, cur = round(float(amt.group(3)) / 100, 6), "ZAR"
        found.append({"account": account, "instrument": name, "amount": value, "currency": cur or
                      ("USD" if account == "USD" else "ZAR"), "ldt": ldt_d, "pay_date": pay_d, "line": raw[:500]})
    return found


def save_post(db: Session, url: str, html: str):
    title, published, main = content(html)
    post = db.scalar(select(BlogPost).where(BlogPost.url == url)) or BlogPost(url=url)
    db.add(post)
    lines = lines_of(main)
    post.title, post.kind = title[:300], kind_of(title, url)
    post.body = "\n".join(lines)
    post.summary = next((ln for ln in lines if len(ln) > 80), "")[:600]
    items = parse_dividends(lines) if post.kind != "news" else []
    if not published:
        dates = [d for i in items for d in (i["ldt"], i["pay_date"]) if d]
        published = min(dates) - timedelta(days=3) if dates else utcnow().date()
    post.published = published
    db.flush()
    db.query(BlogDividend).filter(BlogDividend.post_id == post.id).delete()
    seen = set()
    for i in items:
        key = (i["account"], i["instrument"][:200])
        if key in seen:
            continue
        seen.add(key)
        db.add(BlogDividend(post_id=post.id, account=i["account"], instrument=i["instrument"][:200], amount=i["amount"],
                            currency=i["currency"], ldt=i["ldt"], pay_date=i["pay_date"], line=i["line"]))
    post.items, post.status, post.error, post.fetched_at = len(seen), "ok", "", utcnow()
    return post


def refresh(db: Session, limit=12):
    """New posts from the listings (plus re-reading the newest dividend post, which EasyEquities sometimes updates)."""
    links = []
    for listing in LISTINGS:
        try:
            for u in post_links(_get(listing)):
                if u not in links:
                    links.append(u)
        except Exception as e:
            log.warning("Blog listing %s failed: %s", listing, e)
    known = {u: (s, f) for u, s, f in db.execute(select(BlogPost.url, BlogPost.status, BlogPost.fetched_at))}
    day_ago = utcnow() - timedelta(days=1)
    newest_div = next((u for u in links if "dividend" in u.lower()), None)
    todo = [u for u in links if u not in known or (known[u][1] < day_ago and (known[u][0] != "ok" or u == newest_div))]
    done = 0
    for url in todo[:limit]:
        try:
            save_post(db, url, _get(url))
            done += 1
        except Exception as e:
            post = db.scalar(select(BlogPost).where(BlogPost.url == url)) or BlogPost(url=url, title=url.rsplit("/", 1)[-1])
            post.status, post.error, post.fetched_at = "error", f"{type(e).__name__}: {e}"[:400], utcnow()
            db.add(post)
        db.commit()
    log.info("EasyEquities blog: %d links, %d read", len(links), done)
    return {"links": len(links), "read": done}


def norm(s: str) -> str:
    s = SUFFIXES.sub(" ", (s or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def matcher(names_and_symbols):
    """[(kind, symbol, name)] -> function(instrument) -> (kind, symbol) or None."""
    keys = []
    for kind, symbol, name in names_and_symbols:
        n = norm(name)
        base = (symbol or "").upper().split(".")[0]
        keys.append((kind, symbol, n, base))

    def match(instrument):
        n = norm(instrument)
        tokens = set(re.findall(r"\b[A-Z0-9]{2,6}\b", instrument))
        for kind, symbol, key, base in keys:
            if key and len(key) >= 4 and (n == key or n.startswith(key + " ") or key.startswith(n + " ") and len(n) >= 6):
                return kind, symbol
            if base and len(base) >= 3 and base in tokens:
                return kind, symbol
        return None
    return match


def view(db: Session, holdings, watch):
    """Latest posts and the dividends still to come (or just paid), each marked if you hold or watch it."""
    match = matcher([("hold", h.get("symbol"), h.get("name")) for h in holdings] +
                    [("watch", w.symbol, w.name) for w in watch])
    today = utcnow().date()
    posts = list(db.scalars(select(BlogPost).where(BlogPost.status == "ok").order_by(BlogPost.published.desc()).limit(12)))
    rows = db.execute(select(BlogDividend, BlogPost).join(BlogPost, BlogDividend.post_id == BlogPost.id)
                      .where((BlogDividend.ldt >= today - timedelta(days=7)) | (BlogDividend.pay_date >= today))
                      .order_by(BlogDividend.ldt)).all()
    seen, upcoming = set(), []
    for d, p in rows:
        key = (d.account, norm(d.instrument), d.ldt)
        if key in seen:
            continue
        seen.add(key)
        m = match(d.instrument)
        upcoming.append({"instrument": d.instrument, "account": d.account, "amount": d.amount, "currency": d.currency,
                         "ldt": d.ldt.isoformat() if d.ldt else None, "pay_date": d.pay_date.isoformat() if d.pay_date else None,
                         "mine": m[0] if m else "", "symbol": m[1] if m else "", "post": p.url,
                         "can_buy": bool(d.ldt and d.ldt >= today)})
    return {"posts": [{"title": p.title, "url": p.url, "kind": p.kind, "published": p.published.isoformat() if p.published else None,
                       "summary": p.summary, "items": p.items} for p in posts],
            "upcoming": upcoming}


def search_symbol(name: str, account: str = "ZAR"):
    """Yahoo's search: best ticker for a name, preferring the JSE for ZAR-account shares."""
    try:
        r = http.get("https://query2.finance.yahoo.com/v1/finance/search", headers=HEADERS, timeout=15,
                     params={"q": re.sub(r"\s+", " ", name)[:60], "quotesCount": 8, "newsCount": 0}, **HTTP_KW)
        quotes = [q for q in r.json().get("quotes", []) if q.get("symbol")]
    except Exception:
        return None
    if account == "ZAR":
        jse = [q for q in quotes if q["symbol"].endswith(".JO")]
        quotes = jse or quotes
    elif account in ("USD", "AUD", "GBP", "EUR"):
        suffix = {"USD": "", "AUD": ".AX", "GBP": ".L", "EUR": ""}[account]
        pref = [q for q in quotes if (q["symbol"].endswith(suffix) if suffix else "." not in q["symbol"])]
        quotes = pref or quotes
    return quotes[0]["symbol"] if quotes else None
