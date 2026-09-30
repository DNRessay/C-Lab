from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...deps import current_user, get_db
from ...models import User
from ...security import seal
from . import sync
from .models import EEConnection, EEMail

router = APIRouter(prefix="/api/ee", tags=["easyequities"])


class PlatformIn(BaseModel):
    username: str
    password: str


class MailIn(BaseModel):
    address: str
    app_password: str


class MailPatch(BaseModel):
    done: Optional[bool] = None


def connection(db: Session, user: User, create=False) -> Optional[EEConnection]:
    conn = db.scalar(select(EEConnection).where(EEConnection.user_id == user.id))
    if not conn and create:
        conn = EEConnection(user_id=user.id, snapshot={})
        db.add(conn)
        db.flush()
    return conn


def _dt(v):
    return v.isoformat(timespec="seconds") if v else None


def status(db: Session, conn: Optional[EEConnection]):
    view, _ = sync.platform_view(db, conn) if conn else ({"accounts": [], "value_zar": 0.0, "taken_at": None}, {})
    return {
        "platform": {
            "connected": bool(conn and conn.username),
            "username": conn.username if conn else "",
            "status": conn.platform_status if conn else "",
            "error": conn.platform_error if conn else "",
            "error_stage": conn.platform_error_stage if conn else "",
            "synced_at": _dt(conn.platform_synced_at) if conn else None,
            "tried_at": _dt(conn.platform_tried_at) if conn else None,
        },
        "mail": {
            "connected": bool(conn and conn.mail_address),
            "address": conn.mail_address if conn else "",
            "status": conn.mail_status if conn else "",
            "error": conn.mail_error if conn else "",
            "synced_at": _dt(conn.mail_synced_at) if conn else None,
        },
        **view,
    }


@router.get("")
def get_status(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return status(db, connection(db, user))


@router.put("/platform")
def connect_platform(body: PlatformIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not body.username.strip() or not body.password:
        raise HTTPException(400, "Enter your EasyEquities username and password.")
    conn = connection(db, user, create=True)
    conn.username, conn.password = body.username.strip(), seal(body.password)
    db.commit()
    sync.sync_platform(db, conn)
    return status(db, conn)


@router.delete("/platform")
def disconnect_platform(user: User = Depends(current_user), db: Session = Depends(get_db)):
    conn = connection(db, user)
    if conn:
        conn.username = conn.password = conn.platform_status = conn.platform_error = conn.platform_debug = ""
        conn.snapshot = {}
        db.commit()
    return status(db, conn)


@router.put("/mail")
def connect_mail(body: MailIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if "@" not in body.address or not body.app_password.strip():
        raise HTTPException(400, "Enter the Gmail address and an app password.")
    conn = connection(db, user, create=True)
    if conn.mail_address.lower() != body.address.strip().lower():
        conn.mail_last_uid, conn.mail_uidvalidity = 0, ""
    conn.mail_address, conn.mail_password = body.address.strip(), seal(body.app_password.strip())
    db.commit()
    sync.sync_mail(db, conn)
    return status(db, conn)


@router.delete("/mail")
def disconnect_mail(user: User = Depends(current_user), db: Session = Depends(get_db)):
    conn = connection(db, user)
    if conn:
        conn.mail_address = conn.mail_password = conn.mail_status = conn.mail_error = ""
        db.commit()
    return status(db, conn)


@router.post("/sync")
def sync_now(user: User = Depends(current_user), db: Session = Depends(get_db)):
    conn = connection(db, user)
    if not conn:
        raise HTTPException(400, "Connect EasyEquities or Gmail first.")
    added = 0
    if conn.username and conn.password:
        sync.sync_platform(db, conn)
    if conn.mail_address and conn.mail_password:
        added = sync.sync_mail(db, conn)
    return {**status(db, conn), "new_emails": added}


@router.post("/reparse")
def reparse(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return sync.reparse(db, user.id)


def mail_out(m: EEMail, body=False):
    out = {k: getattr(m, k) for k in ("id", "kind", "parsed", "account", "instrument", "contract_code", "side",
                                      "quantity", "price", "currency", "value", "costs", "total", "reference",
                                      "done", "txn_id", "subject", "sender")}
    out["date"] = m.date.isoformat() if m.date else None
    out["received_at"] = _dt(m.received_at)
    if body:
        out["body"] = m.body
    return out


@router.get("/mails")
def mails(kind: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = select(EEMail).where(EEMail.user_id == user.id)
    if kind:
        q = q.where(EEMail.kind == kind)
    return [mail_out(m) for m in db.scalars(q.order_by(EEMail.received_at.desc(), EEMail.id.desc()).limit(500))]


def own_mail(db, mail_id, user):
    m = db.get(EEMail, mail_id)
    if not m or m.user_id != user.id:
        raise HTTPException(404, "Not found.")
    return m


@router.get("/mails/{mail_id}")
def mail_detail(mail_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return mail_out(own_mail(db, mail_id, user), body=True)


@router.patch("/mails/{mail_id}")
def update_mail(mail_id: int, body: MailPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    m = own_mail(db, mail_id, user)
    if body.done is not None:
        m.done = body.done
    db.commit()
    return mail_out(m)
