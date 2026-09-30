from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from datetime import timedelta

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..deps import current_user, get_db
from ..invest.ee import mail
from ..models import User, utcnow
from . import reader
from .models import BankAccount, BankStatement, BankTxn, Liability

router = APIRouter(prefix="/api/bank", tags=["banking"])


def _account(a: BankAccount, db: Session = None):
    extra = {}
    if db is not None:
        last = db.scalar(select(BankTxn).where(BankTxn.user_id == a.user_id, BankTxn.account == a.account)
                         .order_by(BankTxn.date.desc(), BankTxn.id.desc()))
        since = utcnow().date() - timedelta(days=365)
        row = db.execute(select(func.coalesce(func.sum(case((BankTxn.amount > 0, BankTxn.amount), else_=0)), 0),
                                func.coalesce(func.sum(case((BankTxn.amount < 0, -BankTxn.amount), else_=0)), 0),
                                func.coalesce(func.sum(BankTxn.fee), 0), func.count(),
                                func.coalesce(func.sum(case(((BankTxn.category == "Bank fees") & (BankTxn.fee == 0) &
                                                             (BankTxn.amount < 0), -BankTxn.amount), else_=0)), 0))
                         .where(BankTxn.user_id == a.user_id, BankTxn.account == a.account, BankTxn.date >= since)).one()
        extra = {"bank_name": reader.NAMES.get(a.bank, a.bank.title() or "Other"),
                 "last": {"date": last.date.isoformat(), "description": last.description, "amount": last.amount,
                          "balance": last.balance} if last else None,
                 "in_12m": round(row[0], 2), "out_12m": round(row[1] + row[2], 2), "fees_12m": round(row[2] + row[4], 2), "count_12m": row[3]}
    return {**extra, "id": a.id, "bank": a.bank, "account": a.account, "name": a.name, "kind": a.kind, "hidden": a.hidden,
            "balance": a.balance, "balance_date": a.balance_date.isoformat() if a.balance_date else None}


def _liability(m: Liability):
    return {"id": m.id, "name": m.name, "kind": m.kind, "balance": m.balance, "rate": m.rate, "monthly": m.monthly,
            "notes": m.notes, "updated_at": m.updated_at.isoformat()}


@router.get("")
def overview(user: User = Depends(current_user), db: Session = Depends(get_db)):
    pos = reader.position(db, user.id)
    return {"status": reader.status(db, user.id), "cash": pos["cash"], "debt": pos["debt"], "fees_12m": pos["fees_12m"],
            "accounts": [_account(a, db) for a in pos["accounts"]], "liabilities": [_liability(m) for m in pos["liabilities"]],
            **reader.charts(db, user.id)}


@router.post("/sync")
def sync(user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        result = reader.read_batch(db, user.id)
    except mail.MailError as e:
        raise HTTPException(502, str(e))
    if not result["ok"]:
        raise HTTPException(400, result["error"])
    return {**result, "status": reader.status(db, user.id)}


@router.post("/reparse")
def reparse(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return reader.reparse(db, user.id)


@router.get("/transactions")
def transactions(limit: int = 3000, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(BankTxn).where(BankTxn.user_id == user.id)
                      .order_by(BankTxn.date.desc(), BankTxn.id.desc()).limit(min(limit, 10000)))
    return [{"id": t.id, "date": t.date.isoformat(), "account": t.account, "description": t.description, "amount": t.amount,
             "fee": t.fee, "balance": t.balance, "category": t.category} for t in rows]


@router.get("/statements")
def statements(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(BankStatement).where(BankStatement.user_id == user.id)
                      .order_by(BankStatement.received_at.desc().nulls_last()))
    return [{"id": s.id, "bank": reader.NAMES.get(s.bank, s.bank), "account": s.account, "kind": s.kind,
             "filename": s.filename, "subject": s.subject, "received_at": s.received_at.isoformat() if s.received_at else None,
             "status": s.status, "error": s.error, "rows": s.rows, "closing_balance": s.closing_balance,
             "closing_date": s.closing_date.isoformat() if s.closing_date else None} for s in rows]


@router.get("/statements/{sid}/text")
def statement_text(sid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = db.get(BankStatement, sid)
    if not s or s.user_id != user.id:
        raise HTTPException(404, "Not found.")
    return {"name": s.filename, "text": s.body}


class AccountPatch(BaseModel):
    name: Optional[str] = None
    kind: Optional[str] = None
    hidden: Optional[bool] = None
    balance: Optional[float] = None


@router.patch("/accounts/{aid}")
def update_account(aid: int, body: AccountPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    a = db.get(BankAccount, aid)
    if not a or a.user_id != user.id:
        raise HTTPException(404, "Not found.")
    if body.kind is not None:
        if body.kind not in ("bank", "credit", "loan"):
            raise HTTPException(400, "kind must be bank, credit or loan.")
        a.kind, a.kind_set = body.kind, True
    if body.name is not None:
        a.name = body.name.strip()[:80] or a.account
    if body.hidden is not None:
        a.hidden = body.hidden
    if body.balance is not None:
        a.balance, a.balance_date = body.balance, utcnow().date()
    db.commit()
    return _account(a)


class LiabilityIn(BaseModel):
    name: str
    kind: str = "loan"
    balance: float
    rate: Optional[float] = None
    monthly: Optional[float] = None
    notes: str = ""


@router.get("/liabilities")
def liabilities(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return [_liability(m) for m in reader.position(db, user.id)["liabilities"]]


@router.post("/liabilities", status_code=201)
def add_liability(body: LiabilityIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not body.name.strip():
        raise HTTPException(400, "Give it a name.")
    m = Liability(user_id=user.id, name=body.name.strip()[:120], kind=body.kind[:20], balance=abs(body.balance),
                  rate=body.rate, monthly=body.monthly, notes=body.notes)
    db.add(m)
    db.commit()
    return _liability(m)


@router.put("/liabilities/{lid}")
def update_liability(lid: int, body: LiabilityIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    m = db.get(Liability, lid)
    if not m or m.user_id != user.id:
        raise HTTPException(404, "Not found.")
    m.name, m.kind, m.balance = body.name.strip()[:120] or m.name, body.kind[:20], abs(body.balance)
    m.rate, m.monthly, m.notes, m.updated_at = body.rate, body.monthly, body.notes, utcnow()
    db.commit()
    return _liability(m)


@router.delete("/liabilities/{lid}", status_code=204)
def delete_liability(lid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    m = db.get(Liability, lid)
    if not m or m.user_id != user.id:
        raise HTTPException(404, "Not found.")
    db.delete(m)
    db.commit()
