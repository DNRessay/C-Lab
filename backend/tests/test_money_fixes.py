from datetime import date
from types import SimpleNamespace as T

from app.banking import reader
from app.banking.parsers.tymebank import TymeBankLegacyParser


def txn(i, d, amount, desc, account="A", category="Other"):
    return T(id=i, date=d, amount=amount, description=desc, account=account, category=category)


def test_pocket_moves_are_internal_and_people_are_named():
    txns = [
        txn(1, date(2026, 5, 1), -100, "Money transferred in for GoalSave 123"),
        txn(2, date(2026, 5, 1), 100, "Money transferred in for GoalSave 123"),
        txn(3, date(2026, 5, 2), -50, "Transfer to Current account"),
        txn(4, date(2026, 5, 3), 346.71, "PayShap - Pay by ShapID, M NYOBOL", category="Transfers"),
        txn(5, date(2026, 5, 4), 200, "Transfer from Capitec", account="B"),
        txn(6, date(2026, 5, 4), -200, "Send money", account="C"),
    ]
    internal = reader.internal_pairs(txns)
    assert {1, 2, 3, 5, 6} <= internal and 4 not in internal
    assert reader.income_category(txns[3]) == "Received from people"
    assert reader.income_category(txn(7, date(2026, 5, 5), 9.0, "Interest_applied", category="Interest")) == "Interest"


def test_statement_summary_lines_are_not_payments():
    rows = reader.rows_from([
        {"date": date(2026, 10, 1), "description": "Summary Opening balance", "amount": 668.34, "type": "credit"},
        {"date": date(2026, 10, 1), "description": "Closing Balance", "amount": 10.0, "type": "credit"},
        {"date": date(2026, 10, 2), "description": "Purchase at Boxer", "amount": 50.0, "type": "debit"},
    ], "bank")
    assert [r["description"] for r in rows] == ["Purchase at Boxer"]


def test_fee_lines_are_bank_fees_whatever_the_merchant_rules_say():
    rules = [("mabopane square", "Shopping")]
    assert reader.categorise("Fee: ATM Withdrawal at MABOPANE SQUARE 2 PRETORIA ZA", amount=-10, rules=rules) == "Bank fees"
    assert reader.categorise("Purchase at MABOPANE SQUARE", amount=-50, rules=rules) == "Shopping"


def test_tymebank_ignores_reference_numbers_posing_as_amounts():
    text = "\n".join([
        "18 Mar 2026 Purchase at S2SSlovosupermarket5 Pretoria ZA - 4",
        "19 Mar 2026 Purchase at MAMA PRINCESS TUCK SHOP Winterveld ZA",
        "- 54.00 - 1,234.56",
        "20 Mar 2026 PayShap - Pay by ShapID, M NYOBOL - - 346.71 1,581.27",
    ])
    rows = TymeBankLegacyParser().parse(text)
    assert [(r["description"][:20], r["amount"], r["type"]) for r in rows] == [
        ("Purchase at MAMA PRI", 54.0, "debit"), ("PayShap - Pay by Sha", 346.71, "credit")]


def test_easyequities_cash_never_shows_a_wallet_hundreds_overdrawn():
    from app.invest.ee import sync

    conn = T(snapshot={"taken_at": "2026-10-02", "accounts": [
        {"id": 1, "name": "EasyEquities ZAR", "value": 14.24, "holdings": [
            {"contract_code": "STX40", "name": "Satrix 40", "current_value": 125.0, "purchase_value": 100.0, "current_price": 1.0}]},
        {"id": 2, "name": "TFSA", "value": 3.18, "holdings": [
            {"contract_code": "STXNDQ", "name": "Satrix Nasdaq", "current_value": 3.04, "purchase_value": 2.0, "current_price": 1.0}]},
    ]})
    original = sync.rand_rate
    sync.rand_rate = lambda db, cur: 1.0
    try:
        view, _ = sync.platform_view(None, conn)
    finally:
        sync.rand_rate = original
    cash = {a["name"]: round(a["cash_zar"], 2) for a in view["accounts"]}
    assert cash == {"EasyEquities ZAR": 0.0, "TFSA": 0.14}
