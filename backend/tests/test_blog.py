from datetime import date

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.invest import blog
from app.main import app
from tests.test_invest import market, register  # noqa: F401

api = TestClient(app)

LISTING = """<html><body>
<a href="https://blogs.easyequities.co.za/dividend-20twentysix-september-three">Weekly Dividends Update</a>
<a href="/september-2026-dividends-list">September 2026 Dividends List</a>
<a href="https://blogs.easyequities.co.za/topic/dividends-update">topic</a>
<a href="https://blogs.easyequities.co.za/author/easyresearch">author</a>
</body></html>"""

WEEKLY = """<html><head><title>Weekly Dividends Update</title>
<meta property="article:published_time" content="2026-09-14T08:00:00Z"></head><body><nav>menu</nav>
<div class="post-body">
<p>Here are the dividends this week.</p>
<h3>\U0001F1FF\U0001F1E6 ZAR Account \U0001F1FF\U0001F1E6</h3>
<p>▪️ Prescient Income Provider Feeder AMETF • R0.05 • Last trading date 15 September 2026 • Payment date 21 September 2026</p>
<p>▪️ Sirius Real Estate Ltd • 5.90c • Last day to trade 29 September 2026 • Pay date 20 October 2026</p>
<h3>\U0001F1FA\U0001F1F8 USD Account \U0001F1FA\U0001F1F8</h3>
<p>▪️ Hewlett Packard Enterprise Company • $0.14 • Last trading date 30 September 2026 • Payment date 16 October 2026</p>
</div></body></html>"""

MONTHLY = """<html><head><meta property="og:title" content="September 2026 Dividends List"></head><body>
<div class="post-body"><p>All dividends this month.</p>
<table><tr><th>Company</th><th>Amount</th><th>Last Day to Trade</th><th>Pay Date</th></tr>
<tr><td>Growthpoint Properties Ltd (GRT)</td><td>R0.62</td><td>30/09/2026</td><td>06/10/2026</td></tr>
<tr><td>Vodacom Group</td><td>R3.10</td><td>2026-10-02</td><td>2026-10-06</td></tr></table></div></body></html>"""


def test_parse_weekly_and_monthly():
    title, published, main = blog.content(WEEKLY)
    assert (title, published) == ("Weekly Dividends Update", date(2026, 9, 14))
    rows = blog.parse_dividends(blog.lines_of(main))
    assert [(r["account"], r["instrument"], r["amount"], r["currency"], r["ldt"], r["pay_date"]) for r in rows] == [
        ("ZAR", "Prescient Income Provider Feeder AMETF", 0.05, "ZAR", date(2026, 9, 15), date(2026, 9, 21)),
        ("ZAR", "Sirius Real Estate Ltd", 0.059, "ZAR", date(2026, 9, 29), date(2026, 10, 20)),
        ("USD", "Hewlett Packard Enterprise Company", 0.14, "USD", date(2026, 9, 30), date(2026, 10, 16))]
    _, _, main = blog.content(MONTHLY)
    rows = blog.parse_dividends(blog.lines_of(main))
    assert [(r["instrument"], r["amount"], r["ldt"], r["pay_date"]) for r in rows] == [
        ("Growthpoint Properties Ltd (GRT)", 0.62, date(2026, 9, 30), date(2026, 10, 6)),
        ("Vodacom Group", 3.1, date(2026, 10, 2), date(2026, 10, 6))]
    assert blog.kind_of("September 2026 Dividends List", "") == "monthly_dividends"
    assert blog.kind_of("Weekly Dividends Update", "") == "weekly_dividends"
    assert blog.post_links(LISTING) == ["https://blogs.easyequities.co.za/dividend-20twentysix-september-three",
                                        "https://blogs.easyequities.co.za/september-2026-dividends-list"]


def test_blog_on_overview_and_watchlist(market, monkeypatch):
    pages = {blog.LISTINGS[0]: LISTING, "https://blogs.easyequities.co.za/dividend-20twentysix-september-three": WEEKLY,
             "https://blogs.easyequities.co.za/september-2026-dividends-list": MONTHLY}
    monkeypatch.setattr(blog, "_get", lambda url: pages.get(url, "<html></html>"))
    monkeypatch.setattr(blog, "utcnow", lambda: __import__("datetime").datetime(2026, 9, 25))
    db = SessionLocal()
    assert blog.refresh(db) == {"links": 2, "read": 2}
    assert blog.refresh(db)["read"] == 0  # nothing new the same day
    db.close()

    h = register("blogger")
    monkeypatch.setattr(blog, "search_symbol", lambda name, account="ZAR": "GRT.JO" if "Growthpoint" in name else None)
    r = api.post("/api/invest/blog/watch", headers=h, json={"instrument": "Growthpoint Properties Ltd (GRT)", "account": "ZAR"})
    assert r.status_code == 201 and r.json()["symbol"] == "GRT.JO"
    assert api.post("/api/invest/blog/watch", headers=h, json={"instrument": "Nothing Real"}).status_code == 404

    v = api.get("/api/invest/blog", headers=h).json()
    assert {p["kind"] for p in v["posts"]} == {"weekly_dividends", "monthly_dividends"}
    names = {u["instrument"]: u for u in v["upcoming"]}
    assert "Prescient Income Provider Feeder AMETF" not in names  # last day 15 Sept, paid 21 Sept: over by the 25th
    grt = names["Growthpoint Properties Ltd (GRT)"]
    assert (grt["mine"], grt["symbol"], grt["can_buy"]) == ("watch", "GRT.JO", True)
    s = api.get("/api/invest/summary", headers=h).json()
    assert s["blog"]["upcoming"] and s["blog"]["posts"]
