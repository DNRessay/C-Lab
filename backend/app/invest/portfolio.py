from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import prices
from .ee.models import EEConnection
from .ee.sync import platform_view
from .models import InvestTxn, ManualPrice, PropertyAsset

ZERO = Decimal(0)


def _f(v):
    return float(v) if v is not None else None


def holdings(txns):
    """Average-cost positions per symbol from buy/sell/dividend rows (oldest first)."""
    pos = defaultdict(lambda: {"quantity": ZERO, "cost": ZERO, "realised": ZERO, "dividends": ZERO,
                               "name": "", "asset_class": "share", "last_price": None})
    for t in txns:
        if not t.symbol:
            continue
        p = pos[t.symbol]
        p["name"] = p["name"] or t.name
        if t.kind == "buy":  # sells/dividends default to "share"; buys say what it really is
            p["asset_class"] = t.asset_class or p["asset_class"]
        qty = Decimal(t.quantity or 0)
        if t.kind == "buy":
            p["quantity"] += qty
            p["cost"] += Decimal(t.amount) + Decimal(t.fees or 0)
            p["last_price"] = t.price or p["last_price"]
        elif t.kind == "sell" and p["quantity"] > 0:
            share = min(qty / p["quantity"], Decimal(1))
            cost_out = p["cost"] * share
            p["realised"] += Decimal(t.amount) - Decimal(t.fees or 0) - cost_out
            p["cost"] -= cost_out
            p["quantity"] -= qty
            p["last_price"] = t.price or p["last_price"]
        elif t.kind == "dividend":
            p["dividends"] += Decimal(t.amount)
    return pos


def cash_balance(txns):
    """Only meaningful if deposits are recorded; otherwise None."""
    if not any(t.kind == "deposit" for t in txns):
        return None
    bal = ZERO
    for t in txns:
        amt, fees = Decimal(t.amount), Decimal(t.fees or 0)
        if t.kind in ("deposit", "sell", "dividend", "interest"):
            bal += amt - (fees if t.kind == "sell" else 0)
        elif t.kind in ("withdrawal", "fee"):
            bal -= amt
        elif t.kind == "buy":
            bal -= amt + fees
    return bal


def contributions(txns):
    """Money that went in (+) or came out (-), dated. Deposits if recorded, else buys/sells."""
    if any(t.kind == "deposit" for t in txns):
        return [(t.date, Decimal(t.amount) if t.kind == "deposit" else -Decimal(t.amount))
                for t in txns if t.kind in ("deposit", "withdrawal")]
    return [(t.date, Decimal(t.amount) + Decimal(t.fees or 0) if t.kind == "buy" else -Decimal(t.amount))
            for t in txns if t.kind in ("buy", "sell")]


def what_if(db: Session, flows, symbol):
    """Value today had each contribution bought `symbol` on the same day (price only, no dividends)."""
    row = prices.quote(db, symbol)
    if not row or row.price is None or not row.history or not flows:
        return None
    units = 0.0
    for day, amt in flows:
        p = prices.price_on(row.history, day)
        if p:
            units += float(amt) / p
    return units * float(row.price)


def summary(db: Session, user_id: int):
    txns = list(db.scalars(select(InvestTxn).where(InvestTxn.user_id == user_id)
                           .order_by(InvestTxn.date, InvestTxn.id)))
    manual = {m.symbol: m for m in db.scalars(select(ManualPrice).where(ManualPrice.user_id == user_id))}
    conn = db.scalar(select(EEConnection).where(EEConnection.user_id == user_id))
    ee_view, ee_prices = platform_view(db, conn) if conn and conn.snapshot else (None, {})

    rows, total_value, total_cost = [], 0.0, 0.0
    for symbol, p in holdings(txns).items():
        if p["quantity"] <= Decimal("0.000001") and not p["realised"] and not p["dividends"]:
            continue
        source, price = "none", None
        if symbol in manual:
            price, source = float(manual[symbol].price), f"manual ({manual[symbol].as_of})"
        elif symbol in ee_prices:
            price, source = ee_prices[symbol], f"EasyEquities ({ee_view['taken_at'][:10]})"
        elif not symbol.startswith("EE:"):  # EE: symbols (EasyProperties etc.) aren't on Yahoo
            q = prices.quote(db, symbol)
            if q and q.price is not None:
                price, source = float(q.price), "market"
                p["name"] = p["name"] or q.name
        if price is None and p["last_price"] is not None:
            price, source = float(p["last_price"]), "last trade"
        qty, cost = float(p["quantity"]), float(p["cost"])
        value = qty * price if price is not None else 0.0
        total_value += value
        total_cost += cost
        rows.append({
            "symbol": symbol, "name": p["name"], "asset_class": p["asset_class"], "quantity": qty,
            "avg_cost": cost / qty if qty else None, "cost": cost, "price": price, "price_source": source,
            "value": value, "gain": value - cost if qty else 0.0, "gain_pct": (value / cost - 1) if cost else None,
            "realised": float(p["realised"]), "dividends": float(p["dividends"]),
        })
    rows.sort(key=lambda r: -r["value"])

    cash = cash_balance(txns)
    flows = contributions(txns)
    invested = float(sum((a for _, a in flows), ZERO))
    portfolio_value = total_value + (float(cash) if cash is not None else 0.0)
    # Without a cash record, sells already reduce `invested`; only dividends left the portfolio as cash.
    received = sum(r["dividends"] for r in rows) if cash is None else 0.0
    put_in = float(sum((a for _, a in flows if a > 0), ZERO))
    benchmarks = []
    for symbol, label in prices.BENCHMARKS:
        v = what_if(db, flows, symbol)
        if v is not None:
            benchmarks.append({"symbol": symbol, "label": label, "value": v,
                               "gain": v - invested, "return_pct": (v - invested) / put_in if put_in > 0 else None})

    allocation = defaultdict(float)
    for r in rows:
        allocation[r["asset_class"]] += r["value"]
    if cash:
        allocation["cash"] += float(cash)

    props = [property_view(p) for p in db.scalars(select(PropertyAsset).where(PropertyAsset.user_id == user_id)
                                                  .order_by(PropertyAsset.name))]
    return {
        "holdings": rows,
        "cash": _f(cash),
        "invested": invested,
        "value": portfolio_value,
        "gain": portfolio_value + received - invested,
        "return_pct": (portfolio_value + received - invested) / put_in if put_in > 0 else None,
        "since": txns[0].date.isoformat() if txns else None,
        "benchmarks": benchmarks,
        "allocation": {k: v for k, v in sorted(allocation.items(), key=lambda kv: -kv[1]) if v},
        "properties": props,
        "property_equity": sum(p["equity"] for p in props),
        "net_worth": portfolio_value + sum(p["equity"] for p in props),
        "easyequities": {"value": ee_view["value_zar"], "taken_at": ee_view["taken_at"],
                         "accounts": [{"name": a["name"], "value_zar": a["value_zar"]} for a in ee_view["accounts"]]}
        if ee_view else None,
    }


def property_view(p: PropertyAsset):
    val, bond = float(p.valuation or 0), float(p.bond_balance or 0)
    rent, costs, bond_pay = float(p.monthly_rent or 0), float(p.monthly_costs or 0), float(p.monthly_bond_payment or 0)
    buy = float(p.purchase_price or 0)
    years = ((date.today() - p.purchase_date).days / 365.25) if p.purchase_date else None
    growth = (val / buy - 1) if buy and val else None
    return {
        "id": p.id, "name": p.name, "kind": p.kind, "purchase_date": p.purchase_date, "purchase_price": buy,
        "valuation": val, "valuation_date": p.valuation_date, "bond_balance": bond,
        "monthly_bond_payment": bond_pay, "monthly_rent": rent, "monthly_costs": costs, "notes": p.notes,
        "equity": val - bond,
        "loan_to_value": bond / val if val else None,
        "gross_yield": rent * 12 / val if val and rent else None,
        "net_yield": (rent - costs) * 12 / val if val and rent else None,
        "monthly_cashflow": rent - costs - bond_pay,
        "growth": growth,
        "growth_per_year": ((1 + growth) ** (1 / years) - 1) if growth is not None and years and years >= 1 else None,
    }
