# Company deals on the JSE: buyouts, delisting offers, special payouts, rights offers, unbundlings and property-fund
# mergers, from EasyEquities' blog first and the news (SerpAPI) second, each checked against today's share price: does buying now make money or not?
# Shared by all users; cached in the market_pulse table and refreshed nightly (or on demand).
import json
import logging
import re
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import utcnow
from . import prices
from .pulse import MAX_AGE, MarketPulse, serp_news

log = logging.getLogger(__name__)

KEY = "deals"
QUERIES = ["JSE scheme of arrangement offer cents per share", "JSE delisting offer shareholders per share",
           "JSE special distribution per share last day to trade", "JSE mandatory offer minority shareholders",
           "JSE rights offer cents per share", "JSE unbundling shareholders", "JSE REIT property fund offer per share",
           "JSE property merger share swap"]
KINDS = {"buyout": "Buyout offer", "delisting": "Delisting offer", "merger": "Merger / share swap",
         "special_distribution": "Special payout", "rights_offer": "Rights offer", "unbundling": "Unbundling",
         "other": "Company change"}
DIVIDEND_TAX = 0.20
FEES = 0.01  # about what buying and selling a share costs on EasyEquities, both ways together
KIND_RE = [("special_distribution", r"special (?:dividend|distribution)|capital (?:return|distribution)|final distribution"),
           ("delisting", r"delist"), ("rights_offer", r"rights offer|rights issue"), ("unbundling", r"unbundl"),
           ("merger", r"merger|share swap|amalgamat"),
           ("buyout", r"scheme of arrangement|buyout|take.?over|acquisition|mandatory offer|offer to (?:buy|acquire)|bid for")]
AMOUNT_RE = re.compile(r"R\s?(\d+(?:[.,]\d+)?)\s*(?:a|per)\s+share|(\d+(?:[.,]\d+)?)\s*(?:cents|c)\s*(?:a|per)\s+share", re.I)
PROPERTY_RE = re.compile(r"\breit\b|property|properties|real estate|reit", re.I)


def regex_deal(article):
    """A deal from a headline without AI: the kind from keywords, the amount when it says '450 cents per share'."""
    text = f"{article['title']} {article['snippet']}"
    kind = next((k for k, rx in KIND_RE if re.search(rx, text, re.I)), None)
    if not kind:
        return None
    m = AMOUNT_RE.search(text)
    amount = None
    if m:
        amount = float((m.group(1) or "").replace(",", ".")) if m.group(1) else float(m.group(2).replace(",", ".")) / 100
    company = re.split(r"\s+(?:receives|gets|to|offers?|announces|says|plans|agrees|in|faces|bid|shareholders)\b|[:,–-]",
                       article["title"], maxsplit=1)[0].strip()
    return {"company": company[:80], "kind": kind, "amount": amount, "ldt": None, "property": bool(PROPERTY_RE.search(text))}


def ai_deals(articles):
    """Groq reads the headlines: which name a specific JSE company, what kind of deal, the rand amount per share."""
    from ..ai import llm

    heads = "\n".join(f"{i}. [{a['date'] or '?'}] {a['title']} — {a['snippet'][:700 if a['source'] == 'EasyEquities blog' else 220]}"
                      for i, a in enumerate(articles))
    system = ("You read South African business headlines. For each one about a SPECIFIC JSE-listed company having a "
              "corporate action, reply as JSON {\"deals\": [{\"i\": index, \"company\": the listed company's name, "
              "\"kind\": \"buyout|delisting|merger|special_distribution|rights_offer|unbundling|other\", "
              "\"amount\": rand per share as a number (450 cents = 4.50) or null if the headline doesn't give it, "
              "\"ldt\": \"YYYY-MM-DD\" last day to trade if given else null, \"property\": true if it's a property "
              "company or REIT}]}. Skip headlines that aren't about one company's deal. Never guess an amount.")
    data = llm.parse_json(llm.groq([{"role": "system", "content": system}, {"role": "user", "content": heads}],
                                   max_tokens=1600, json_mode=True)) or {}
    out = {}
    for d in data.get("deals", []) if isinstance(data, dict) else []:
        try:
            i = int(d.get("i"))
            amount = float(d["amount"]) if d.get("amount") not in (None, "") else None
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(articles) and d.get("company"):
            out[i] = {"company": str(d["company"])[:80], "kind": d.get("kind") if d.get("kind") in KINDS else "other",
                      "amount": amount if amount and amount > 0 else None, "ldt": _date(d.get("ldt")),
                      "property": bool(d.get("property"))}
    return out


def _date(s):
    try:
        return date.fromisoformat(str(s)[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def verdict(kind, amount, price):
    """(tone, what it means for someone buying today). tone: ok = buying can make money, warn = it loses, info = depends."""
    if kind in ("buyout", "delisting") and amount and price:
        gap = amount - price
        net = gap - price * FEES
        if net > 0:
            return "ok", (f"The offer is R{amount:.2f} a share and it trades at R{price:.2f}: about R{net:.2f} a share "
                          f"({net / price:+.1%}) after fees if the deal goes through. If it fails or is delayed, the price "
                          "usually falls back below where it is now.")
        return "warn", (f"It trades at R{price:.2f}, at or above the R{amount:.2f} offer: buying now loses about "
                        f"R{-net:.2f} a share after fees even if the deal goes through.")
    if kind == "special_distribution" and amount:
        lost = amount * DIVIDEND_TAX + (price or 0) * FEES
        return "warn", (f"After the last day to trade the price drops by about the R{amount:.2f} payout, and the payout is "
                        f"taxed {DIVIDEND_TAX:.0%}. Buying just to get it loses about R{lost:.2f} a share.")
    if kind == "special_distribution":
        return "warn", ("The price drops by about the payout after the last day to trade, and the payout is taxed "
                        f"{DIVIDEND_TAX:.0%}: buying just to get it loses money.")
    if kind == "rights_offer":
        return "info", ("Shareholders on the record date may buy new shares, usually below the market price. More shares "
                        "usually push the price down; only worth it if you'd want more of the company anyway.")
    if kind == "unbundling":
        return "info", ("Shareholders get shares in the company being split off and the price drops by about their value. "
                        "No free money; you end up holding two companies.")
    if kind == "merger":
        return "info", ("Shareholders get shares in the merged company. What it's worth depends on the swap ratio and "
                        "on the other company's price.")
    if kind in ("buyout", "delisting"):
        return "info", ("No price per share given yet. Compare the offer with the share price before buying: if the "
                        "price is already at or above the offer, there's nothing left to make.")
    return "info", "Read the announcement before buying: company changes can move the price either way."


def blog_articles(db: Session, days=120):
    """EasyEquities' blog posts about company deals (it announces buyouts, payouts and unbundlings to its clients)."""
    from .blog import BlogPost

    since = date.today() - timedelta(days=days)
    out = []
    for p in db.scalars(select(BlogPost).where(BlogPost.status == "ok", BlogPost.kind == "news", BlogPost.published >= since)):
        text = f"{p.title}\n{p.summary}\n{p.body[:3000]}"
        if any(re.search(rx, text, re.I) for _, rx in KIND_RE):
            out.append({"title": p.title, "snippet": re.sub(r"\s+", " ", f"{p.summary} {p.body[:1200]}")[:900], "link": p.url,
                        "source": "EasyEquities blog", "date": p.published.isoformat() if p.published else None})
    return out


def build(db: Session):
    from ..ai import llm
    from .blog import search_symbol

    articles = blog_articles(db)
    seen = {a["link"] for a in articles}
    for q in QUERIES:
        for a in serp_news(q, 8):
            if a["link"] not in seen and (not a["date"] or a["date"] >= (date.today() - timedelta(days=120)).isoformat()):
                seen.add(a["link"])
                articles.append(a)
    found = {}
    if settings.groq_api_keys and articles:
        try:
            found = ai_deals(articles[:50])
        except llm.AIError as e:
            log.warning("Deals: AI skipped: %s", e)
    if not found:
        found = {i: d for i, a in enumerate(articles) if (d := regex_deal(a))}

    deals, done = [], set()
    blog_first = lambda kv: (articles[kv[0]]["source"] == "EasyEquities blog", articles[kv[0]]["date"] or "")
    for i, d in sorted(found.items(), key=blog_first, reverse=True):
        key = (d["company"].lower(), d["kind"])
        if key in done:
            continue  # the newest article about a deal wins
        done.add(key)
        a = articles[i]
        symbol = search_symbol(d["company"], "ZAR")
        row = prices.quote(db, symbol) if symbol and symbol.endswith(".JO") else None
        price = float(row.price) if row and row.price is not None else None
        tone, says = verdict(d["kind"], d["amount"], price)
        gap = round(d["amount"] - price, 2) if d["amount"] and price and d["kind"] in ("buyout", "delisting") else None
        deals.append({"company": d["company"], "symbol": symbol if row else None, "kind": d["kind"], "kind_label": KINDS[d["kind"]],
                      "property": d["property"], "amount": d["amount"], "price": price, "gap": gap,
                      "gap_pct": round(gap / price, 4) if gap is not None else None, "tone": tone, "verdict": says,
                      "ldt": d["ldt"], "title": a["title"], "link": a["link"], "source": a["source"], "date": a["date"]})
    order = {"ok": 0, "warn": 1, "info": 2}
    deals.sort(key=lambda x: (order[x["tone"]], -(len(x["date"] or "") and int((x["date"] or "0").replace("-", "")))))
    return {"built_at": utcnow().isoformat(), "deals": deals[:25],
            "sources": {"news": bool(settings.serpapi_keys), "ai": bool(settings.groq_api_keys)}}


def get(db: Session, refresh=False):
    row = db.scalar(select(MarketPulse).where(MarketPulse.key == KEY).order_by(MarketPulse.id.desc()))
    if row and not refresh and utcnow() - row.created_at < timedelta(hours=26):
        return row.data
    if row and refresh and utcnow() - row.created_at < MAX_AGE and row.data.get("sources", {}).get("news") == bool(settings.serpapi_keys):
        return row.data  # at most one rebuild every few hours
    data = build(db)
    row = row or MarketPulse(key=KEY)
    row.data, row.created_at = json.loads(json.dumps(data)), utcnow()
    db.add(row)
    db.commit()
    return data
