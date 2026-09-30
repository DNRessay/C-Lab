import io
from datetime import date

from fastapi.testclient import TestClient

from app.banking import reader
from app.db import SessionLocal
from app.invest.ee import gmail
from app.invest.ee.models import EEConnection, EESetting
from app.main import app
from app.models import User
from app.security import seal
from tests.test_invest import register

api = TestClient(app)


def capitec_pdf(lines, password=None):
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    y = 800
    for line in ["Capitec Bank", "Account Number: 1234 5678 9012", "Transaction History", *lines]:
        c.drawString(40, y, line)
        y -= 18
    c.save()
    if not password:
        return buf.getvalue()
    w = PdfWriter()
    for p in PdfReader(io.BytesIO(buf.getvalue())).pages:
        w.add_page(p)
    w.encrypt(password)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


AUG = ["01/08/2026 Payment Received ACME Salary Income 20,000.00 20,500.00",
       "03/08/2026 Checkers Sandton Groceries -1,250.00 -1.50 19,248.50",
       "05/08/2026 Monthly Account Admin Fee -7.50 19,241.00"]
SEP = ["05/08/2026 Monthly Account Admin Fee -7.50 19,241.00",  # overlaps August: must not count twice
       "02/09/2026 Uber Trip Transport -180.00 19,061.00"]


def test_bank_statements_from_gmail(monkeypatch):
    h = register("banker")
    db = SessionLocal()
    uid = db.query(User).filter(User.email == "banker@inv.example.com").one().id
    assert api.post("/api/bank/sync", headers=h).status_code == 400  # Google not signed in yet
    db.add(EEConnection(user_id=uid, snapshot={}, mail_address="b@gmail.com", mail_password=gmail.PREFIX + seal("rt")))
    db.commit()

    mails = {"m1": ("Capitec <statements@capitecbank.co.za>", "Your statement", capitec_pdf(AUG, "0101015800080")),
             "m2": ("Capitec <statements@capitecbank.co.za>", "Your statement", capitec_pdf(SEP, "0101015800080"))}
    monkeypatch.setattr(gmail, "access_token", lambda rt: "at")
    monkeypatch.setattr(reader, "search", lambda token: list(mails))
    monkeypatch.setattr(reader, "attachments", lambda token, mid: (
        {"from": mails[mid][0], "subject": mails[mid][1], "date": "Mon, 07 Sep 2026 08:00:00 +0200"},
        [(f"{mid}.pdf", mails[mid][2])]))

    r = api.post("/api/bank/sync", headers=h).json()
    assert r["status"]["locked"] == 2 and r["status"]["transactions"] == 0  # no PDF password yet

    db.add(EESetting(user_id=uid, pdf_password=seal("0101015800080")))
    db.commit()
    r = api.post("/api/bank/sync", headers=h).json()
    assert r["read_now"] == 2 and r["status"]["ok"] == 2 and r["status"]["locked"] == 0
    rows = api.get("/api/bank/transactions", headers=h).json()
    assert len(rows) == 4  # the admin fee on both statements is stored once
    checkers = next(t for t in rows if "Checkers" in t["description"])
    assert (checkers["amount"], checkers["fee"], checkers["category"]) == (-1250.0, 1.5, "Groceries")
    assert next(t for t in rows if "Admin Fee" in t["description"])["category"] == "Bank fees"
    assert next(t for t in rows if "ACME" in t["description"])["category"] == "Income"  # Capitec's "Salary Income"

    o = api.get("/api/bank", headers=h).json()
    acc = o["accounts"][0]
    assert (acc["account"], acc["kind"], acc["balance"], acc["balance_date"]) == ("Capitec ••9012", "bank", 19061.0, "2026-09-02")
    assert acc["bank_name"] == "Capitec" and acc["last"]["description"].startswith("Uber")
    assert acc["fees_12m"] == 9.0
    assert o["cash"] == 19061.0 and o["fees_12m"] == 9.0  # 1.50 on the Checkers line + 7.50 admin fee
    aug = next(m for m in o["months"] if m["month"] == "2026-08")
    assert aug["in"] == 20000.0 and aug["fees"] == 9.0
    assert o["categories"]["Groceries"] == 1250.0

    # Debt: a typed-in car loan, and a card that's switched to credit.
    assert api.post("/api/bank/liabilities", headers=h, json={"name": "Car", "kind": "vehicle", "balance": 150000}).status_code == 201
    s = api.get("/api/invest/summary", headers=h).json()
    assert s["banking"]["cash"] == 19061.0 and s["banking"]["debt"] == 150000.0
    assert round(s["net_worth"], 2) == round(s["value"] + 19061.0 - 150000.0, 2)
    assert api.patch(f"/api/bank/accounts/{acc['id']}", headers=h, json={"kind": "credit"}).json()["kind"] == "credit"
    assert api.get("/api/bank", headers=h).json()["debt"] == 169061.0

    other = register("nosy")
    assert api.get(f"/api/bank/statements/1/text", headers=other).status_code == 404
    first = next(s for s in api.get("/api/bank/statements", headers=h).json() if s["filename"] == "m1.pdf")
    assert "Checkers" in api.get(f"/api/bank/statements/{first['id']}/text", headers=h).json()["text"]
    # Other test modules expect a single EasyEquities connection/setting row.
    db.query(EEConnection).filter(EEConnection.user_id == uid).delete()
    db.query(EESetting).filter(EESetting.user_id == uid).delete()
    db.commit()
    db.close()


def test_bank_helpers():
    assert reader.bank_for("alerts@fnb.co.za") == "fnb"
    assert reader.account_kind("Credit Card Statement  Credit limit R 20 000") == "credit"
    assert reader.closing_balance("Opening balance 10.00\nClosing Balance   R 1 234.56 Dr") == -1234.56
    assert reader.categorise("POS Purchase Engen Garage") == "Transport"
    assert reader.categorise("Unknown thing", amount=50) == "Other income"
    assert date(2026, 1, 1)
