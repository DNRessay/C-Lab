from datetime import date

from app.invest import deals


def test_buyout_verdict_says_whether_buying_now_makes_money():
    tone, says = deals.verdict("buyout", 10.0, 9.0)
    assert tone == "ok" and "R0.91" in says  # R1 gap less ~1% fees
    tone, says = deals.verdict("delisting", 7.40, 8.38)
    assert tone == "warn" and "loses" in says


def test_buying_for_a_special_payout_loses_the_tax():
    tone, says = deals.verdict("special_distribution", 7.0, 8.38)
    assert tone == "warn" and "R1.48" in says  # 20% of R7 + ~1% of R8.38


def test_headline_without_ai():
    d = deals.regex_deal({"title": "Trencor receives offer of 450 cents per share", "snippet": "scheme of arrangement"})
    assert d == {"company": "Trencor", "kind": "buyout", "amount": 4.5, "ldt": None, "property": False}
    d = deals.regex_deal({"title": "Fairvest to unbundle REIT stake", "snippet": "property fund unbundling"})
    assert d["kind"] == "unbundling" and d["property"]
    assert deals.regex_deal({"title": "Rand firmer on CPI", "snippet": ""}) is None


def test_blog_posts_about_deals_come_first(monkeypatch):
    from app.db import SessionLocal
    from app.invest import blog

    db = SessionLocal()
    db.add(blog.BlogPost(url="https://blogs.easyequities.co.za/acme-buyout", title="Acme Ltd: scheme of arrangement",
                         kind="news", status="ok", published=date.today(), summary="R12.00 per share offer",
                         body="Acme shareholders will receive R12.00 per share."))
    db.add(blog.BlogPost(url="https://blogs.easyequities.co.za/markets-wrap", title="Markets wrap", kind="news", status="ok",
                         published=date.today(), summary="JSE up", body="Nothing here."))
    db.commit()
    monkeypatch.setattr(deals, "serp_news", lambda q, n=8: [])
    monkeypatch.setattr(deals.settings, "groq_api_keys", [])
    monkeypatch.setattr("app.invest.blog.search_symbol", lambda name, account="ZAR": None)
    out = deals.build(db)
    assert [d["company"] for d in out["deals"]] == ["Acme Ltd"]
    assert out["deals"][0]["amount"] == 12.0 and out["deals"][0]["source"] == "EasyEquities blog"
    db.close()
