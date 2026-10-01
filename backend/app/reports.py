# A finance report for any period: net worth, investments, income vs spending, categories, merchants, fees,
# dividends, debt, property and insights. Built only from data C-Lab already has (no API calls).
from collections import defaultdict
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .deps import current_user, get_db
from .models import User, utcnow

router = APIRouter(prefix="/api/reports", tags=["reports"])


def _r(v):
    return round(float(v or 0), 2)


def bank_period(db: Session, user_id: int, start: date, end: date):
    from .banking.categorize import merchant_key
    from .banking.models import BankTxn
    from .banking.reader import INVEST_RE, internal_pairs

    txns = list(db.scalars(select(BankTxn).where(BankTxn.user_id == user_id, BankTxn.date >= start - timedelta(days=4),
                                                 BankTxn.date <= end + timedelta(days=4))))
    internal = internal_pairs(txns)
    out = {"income": 0.0, "spending": 0.0, "fees": 0.0, "invested": 0.0, "from_investments": 0.0, "moved_between_own": 0.0}
    cats, merchants, months, biggest = defaultdict(float), defaultdict(float), defaultdict(lambda: [0.0, 0.0]), []
    for t in txns:
        if not start <= t.date <= end:
            continue
        inv = bool(INVEST_RE.search(t.description or ""))
        fee = t.fee or (-t.amount if t.category == "Bank fees" and t.amount < 0 else 0.0)
        out["fees"] += fee
        mk = t.date.strftime("%Y-%m")
        if t.id in internal:
            out["moved_between_own"] += abs(t.amount)
        elif t.amount >= 0:
            out["from_investments" if inv else "income"] += t.amount
            if not inv:
                months[mk][0] += t.amount
        elif inv:
            out["invested"] += -t.amount
        else:
            spend = -t.amount + (t.fee or 0)
            out["spending"] += spend
            months[mk][1] += spend
            cats["Sent to people" if t.category == "Transfers" else t.category] += -t.amount
            merchants[merchant_key(t.description) or t.description[:30].lower()] += -t.amount
            biggest.append({"date": t.date.isoformat(), "what": t.description[:80], "amount": _r(t.amount), "category": t.category})
        if t.fee:
            cats["Bank fees"] += t.fee
    out = {k: _r(v) for k, v in out.items()}
    out["saved_pct"] = round((out["income"] - out["spending"]) / out["income"] * 100, 1) if out["income"] else None
    out["categories"] = [{"name": k, "amount": _r(v)} for k, v in sorted(cats.items(), key=lambda kv: -kv[1])][:12]
    out["merchants"] = [{"name": k.title(), "amount": _r(v)} for k, v in sorted(merchants.items(), key=lambda kv: -kv[1])][:10]
    out["biggest"] = sorted(biggest, key=lambda x: x["amount"])[:8]
    out["months"] = [{"month": k, "in": _r(v[0]), "out": _r(v[1])} for k, v in sorted(months.items())]
    return out


def investments_period(db: Session, user_id: int, start: date, end: date):
    from .invest.ee.models import EEConnection
    from .invest.ee.sync import rand_rate, statement_rows
    from .invest.portfolio import month_flows

    conn = db.scalar(select(EEConnection).where(EEConnection.user_id == user_id))
    totals = defaultdict(float)
    if conn:
        rates = {}
        for r in statement_rows(db, conn):
            d = str(r.get("date") or "")[:10]
            if not d or not start.isoformat() <= d <= end.isoformat() or r.get("amount") is None:
                continue
            if r["category"] in ("dividend", "interest", "fee", "tax"):
                rate = rates.setdefault(r["currency"], rand_rate(db, r["currency"]) or 0.0)
                totals[r["category"]] += abs(r["amount"]) * rate
    put_in = sum(float(a) for d, a in month_flows(db, user_id) if start <= d <= end + timedelta(days=31))
    return {"dividends": _r(totals["dividend"]), "interest": _r(totals["interest"]), "fees": _r(totals["fee"]),
            "tax": _r(totals["tax"]), "net_put_in": _r(put_in)}


def insights(s, bank, inv, money):
    out = []
    if bank["income"]:
        rate = bank["saved_pct"]
        out.append(f"You kept {rate}% of the money that came in." if rate is not None and rate >= 0 else
                   f"You spent more than came in, by R{abs(bank['income'] - bank['spending']):,.2f}.")
    if bank["categories"]:
        top = bank["categories"][0]
        share = top["amount"] / bank["spending"] * 100 if bank["spending"] else 0
        out.append(f"{top['name']} was your biggest spend: R{top['amount']:,.2f} ({share:.0f}% of spending).")
    if bank["fees"]:
        out.append(f"Bank fees cost R{bank['fees']:,.2f} this period.")
    if inv["fees"] + inv["tax"] > inv["dividends"] + inv["interest"] and inv["fees"]:
        out.append(f"Investment fees and tax (R{inv['fees'] + inv['tax']:,.2f}) were more than dividends and interest "
                   f"(R{inv['dividends'] + inv['interest']:,.2f}).")
    hold = [h for h in s["holdings"] if h.get("value")]
    if hold:
        total = sum(h["value"] for h in hold)
        big = max(hold, key=lambda h: h["value"])
        if total and big["value"] / total > 0.3:
            out.append(f"{big.get('name') or big['symbol']} is {big['value'] / total * 100:.0f}% of your investments: "
                       "a big bet on one name.")
    months_spend = bank["spending"] / max(1, len(bank["months"])) if bank["months"] else 0
    cash = (s.get("banking") or {}).get("cash") or 0
    if months_spend:
        out.append(f"Your bank balance covers about {cash / months_spend:.1f} month(s) of spending.")
    debt = (s.get("banking") or {}).get("debt") or 0
    if debt:
        out.append(f"Debt of R{debt:,.2f} comes off your net worth.")
    return out


@router.get("")
def report(start: str = "", end: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    from .ai.router import AISuggestion
    from .invest import portfolio

    try:
        end_d = date.fromisoformat(end) if end else utcnow().date()
        start_d = date.fromisoformat(start) if start else end_d.replace(day=1)
    except ValueError:
        raise HTTPException(400, "Dates are YYYY-MM-DD.")
    if start_d > end_d:
        start_d, end_d = end_d, start_d
    s = portfolio.summary(db, user.id)
    months = max(1, (end_d.year - start_d.year) * 12 + end_d.month - start_d.month + 1)
    money = portfolio.money_picture(db, user.id, months=min(36, months + 13))
    rows = {r["month"]: r for r in money["months"]}
    first, last = rows.get(start_d.strftime("%Y-%m")), rows.get(end_d.strftime("%Y-%m"))
    bank = bank_period(db, user.id, start_d, end_d)
    inv = investments_period(db, user.id, start_d, end_d)
    ai = db.scalar(select(AISuggestion).where(AISuggestion.user_id == user.id, AISuggestion.section == "overview"))
    total = sum(h["value"] for h in s["holdings"] if h.get("value")) or 1
    return {
        "period": {"start": start_d.isoformat(), "end": end_d.isoformat(), "months": months},
        "generated_at": utcnow().isoformat(), "name": f"{user.first_name} {user.last_name}".strip(),
        "net_worth": {"now": _r(s["net_worth"]), "start_month": first and first["net_worth"], "end_month": last and last["net_worth"],
                      "parts": {"investments": _r(s["value"]), "own_property": _r(s["property_equity"] - (s.get("easyproperties_value") or 0)),
                                "bank": _r((s.get("banking") or {}).get("cash")), "debt": _r((s.get("banking") or {}).get("debt"))},
                      "series": [{k: r[k] for k in ("month", "net_worth", "investments", "bank", "debt")}
                                 for r in money["months"] if start_d.strftime("%Y-%m") <= r["month"] <= end_d.strftime("%Y-%m")]},
        "investments": {"worth": _r(s["value"]), "money_put_in_all_time": _r(s["invested"]), "gain_all_time": _r(s["gain"]),
                        "return_pct": round((s["return_pct"] or 0) * 100, 1), **inv,
                        "top_holdings": [{"name": h.get("name") or h["symbol"], "value": _r(h["value"]),
                                          "weight": round(h["value"] / total * 100, 1), "gain_pct": round((h.get("gain_pct") or 0) * 100, 1)}
                                         for h in sorted(s["holdings"], key=lambda h: -h["value"])[:10]],
                        "allocation": {k: _r(v) for k, v in s["allocation"].items()}},
        "banking": bank,
        "debt": {"total": _r((s.get("banking") or {}).get("debt"))},
        "property": {"equity": _r(s["property_equity"]), "easyproperties": _r(s.get("easyproperties_value")),
                     "own": [{"name": p["name"], "worth": _r(p["valuation"]), "bond": _r(p["bond_balance"])} for p in s["properties"]]},
        "insights": insights(s, bank, inv, money),
        "ai": [{"title": i["title"], "detail": i["detail"]} for i in (ai.items if ai else [])][:5],
    }
