# Setup that must be green before the app opens: SA ID number (checked, stored sealed, reused as the statement
# PDF password), EasyEquities login working, Google (Gmail) connected. Nothing is asked twice.
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import Date, DateTime, ForeignKey, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .db import Base
from .deps import current_user, get_db
from .models import BigId, User, pk, text, utcnow
from .security import seal, unseal

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


class UserProfile(Base):
    __tablename__ = "user_profiles"
    id: Mapped[int] = pk()
    user_id: Mapped[int] = mapped_column(BigId, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    id_number: Mapped[str] = text()  # sealed
    birth_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    citizen: Mapped[str] = text(12)  # citizen | resident
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


def luhn_ok(digits: str) -> bool:
    """Luhn checksum: from the right, double every second digit (minus 9 if over 9); the total must end in 0."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
    return total % 10 == 0


def check_sa_id(raw: str, today: Optional[date] = None):
    """(digits, info) for a valid South African ID number, else raises ValueError with the reason.
    YYMMDD birth date, SSSS gender sequence, C citizenship (0 citizen, 1 permanent resident), A, Z = Luhn check."""
    today = today or date.today()
    n = "".join(ch for ch in str(raw) if ch.isdigit())
    if len(n) != 13:
        raise ValueError("An SA ID number has 13 digits.")
    yy, mm, dd = int(n[0:2]), int(n[2:4]), int(n[4:6])
    year = 2000 + yy if 2000 + yy <= today.year else 1900 + yy
    try:
        born = date(year, mm, dd)
    except ValueError:
        raise ValueError("The first 6 digits must be a real birth date (YYMMDD).")
    if born > today:
        raise ValueError("The birth date in this ID is in the future.")
    if n[10] not in "01":
        raise ValueError("The 11th digit must be 0 (citizen) or 1 (permanent resident).")
    if not luhn_ok(n):
        raise ValueError("This ID number's check digit doesn't add up. Check for a typo.")
    return n, {"birth_date": born, "gender": "female" if int(n[6:10]) < 5000 else "male",
               "citizen": "citizen" if n[10] == "0" else "resident"}


def save_id(db: Session, user_id: int, raw: str):
    from .invest.ee.models import EESetting

    n, info = check_sa_id(raw)
    prof = db.scalar(select(UserProfile).where(UserProfile.user_id == user_id)) or UserProfile(user_id=user_id)
    prof.id_number, prof.birth_date, prof.citizen, prof.verified_at = seal(n), info["birth_date"], info["citizen"], utcnow()
    db.add(prof)
    # The same number opens EasyEquities and bank statement PDFs: store it there too, so it's never asked again.
    s = db.scalar(select(EESetting).where(EESetting.user_id == user_id)) or EESetting(user_id=user_id)
    s.pdf_password = seal(n)
    db.add(s)
    db.commit()
    return prof


def status(db: Session, user: User):
    from .invest.ee import gmail
    from .invest.ee.models import EEConnection, EESetting

    prof = db.scalar(select(UserProfile).where(UserProfile.user_id == user.id))
    if not (prof and prof.verified_at):
        # Already gave it as the statement password? Use that instead of asking again.
        s = db.scalar(select(EESetting).where(EESetting.user_id == user.id))
        try:
            if s and s.pdf_password:
                prof = save_id(db, user.id, unseal(s.pdf_password))
        except ValueError:
            pass
    conn = db.scalar(select(EEConnection).where(EEConnection.user_id == user.id))
    ee_ok = bool(conn and conn.username and conn.password and conn.platform_status == "ok")
    google_ok = bool(conn and conn.mail_password.startswith(gmail.PREFIX) and conn.mail_address and conn.mail_status != "error")
    from sqlalchemy import func

    from .banking.models import BankStatement
    from .invest.ee.models import EEStatementDoc

    def count(model, *conds):
        return db.scalar(select(func.count()).select_from(model).where(model.user_id == user.id, *conds)) or 0

    opened = count(EEStatementDoc, EEStatementDoc.status == "ok") + count(BankStatement, BankStatement.status == "ok")
    locked = count(EEStatementDoc, EEStatementDoc.error.contains("decrypt")) + count(BankStatement, BankStatement.status == "locked")
    steps = {
        "id": {"done": bool(prof and prof.verified_at),
               "detail": (f"Born {prof.birth_date:%d %b %Y} · SA {prof.citizen}" if prof and prof.birth_date else "")},
        "easyequities": {"done": ee_ok, "detail": (conn.username if ee_ok else (conn.platform_error if conn and conn.username else ""))},
        "google": {"done": google_ok, "detail": conn.mail_address if google_ok else ""},
        # Proof it all works: at least one statement found and opened with the ID.
        "statements": {"done": opened > 0,
                       "detail": (f"{opened} statement(s) opened with your ID" if opened else
                                  f"Found {locked}, but your ID didn't open them. Check the ID number." if locked else "")},
    }
    return {"steps": steps, "complete": all(v["done"] for v in steps.values()), "name": user.first_name}


@router.get("")
def get_status(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return status(db, user)


class IdIn(BaseModel):
    id_number: str


@router.put("/id")
def set_id(body: IdIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        save_id(db, user.id, body.id_number)
    except ValueError as e:
        raise HTTPException(400, {"id_number": [str(e)]})
    return status(db, user)


@router.post("/statements")
def find_statements(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Try one EasyEquities statement and one bank statement from Gmail, opened with the ID number."""
    from .banking import reader as bank_reader
    from .invest.ee import reader as ee_reader
    from .invest.ee.models import EEConnection

    conn = db.scalar(select(EEConnection).where(EEConnection.user_id == user.id))
    tried = {}
    if conn and conn.username and conn.platform_status == "ok":
        try:
            tried["easyequities"] = ee_reader.read_batch(db, conn, limit=1)
        except Exception as e:  # report, don't fail the whole check
            db.rollback()
            tried["easyequities"] = {"error": str(e)[:200]}
    try:
        tried["bank"] = bank_reader.read_batch(db, user.id, limit=2)
    except Exception as e:
        db.rollback()
        tried["bank"] = {"error": str(e)[:200]}
    return {**status(db, user), "tried": tried}
