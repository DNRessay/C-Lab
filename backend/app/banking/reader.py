# Bank statements from Gmail: find statement emails with PDF attachments, read every transaction,
# keep each account's latest balance. Uses the same "Sign in with Google" token as the EasyEquities mail reader.
import base64
import hashlib
import logging
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime

import requests
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..invest.ee import gmail
from ..invest.ee.models import EEConnection, EESetting
from ..security import unseal
from . import parsers
from .models import BankAccount, BankStatement, BankTxn, Liability

log = logging.getLogger(__name__)

# sender domain -> (bank key, display name). Parsers exist for capitec, tymebank, gotyme; others use the generic one.
BANKS = {
    "capitecbank.co.za": ("capitec", "Capitec"),
    "tymebank.co.za": ("tymebank", "TymeBank"),
    "gotyme.co.za": ("gotyme", "GoTyme"),
    "fnb.co.za": ("fnb", "FNB"),
    "absa.co.za": ("absa", "Absa"),
    "standardbank.co.za": ("standardbank", "Standard Bank"),
    "nedbank.co.za": ("nedbank", "Nedbank"),
    "discovery.co.za": ("discovery", "Discovery Bank"),
    "investec.co.za": ("investec", "Investec"),
    "investec.com": ("investec", "Investec"),
    "africanbank.co.za": ("africanbank", "African Bank"),
    "bankzero.co.za": ("bankzero", "Bank Zero"),
}
NAMES = {k: v for k, v in BANKS.values()}
QUERY = "has:attachment filename:pdf (statement OR statements) from:(" + " OR ".join(BANKS) + ")"

ACCOUNT_RE = re.compile(r"(?:account|acc|card)\s*(?:number|no\.?|nr\.?|#)?\s*[:.]?\s*((?:\d[\d \-*x]{5,}\d))", re.I)
CLOSING_RE = re.compile(r"closing\s+balance[^\d\-R]{0,30}(-?R?\s?-?[\d ,]+\.\d{2})\s*(cr|dr)?", re.I)
FEE_RE = re.compile(r"\b(fee|fees|charge|charges|admin|sms notif|service fee|monthly account)\b", re.I)
CREDIT_CARD_RE = re.compile(r"credit\s+card|credit\s+limit|minimum\s+(amount\s+)?(payment\s+)?due", re.I)
LOAN_RE = re.compile(r"personal\s+loan|loan\s+account|home\s+loan|vehicle\s+finance|instal+ment\s+sale", re.I)

CATEGORIES = [
    ("Income", r"salary|payroll|wages|\bpay\b.*(ltd|pty)|income"),
    ("Transfers", r"transfer|trf|own account|goalsave|savings pocket|easyequities|investment"),
    ("Groceries", r"checkers|pick ?n ?pay|woolworths|spar\b|shoprite|food lover|boxer|makro"),
    ("Eating out", r"kfc|mcdonald|nando|steers|debonairs|uber ?eats|mr ?d\b|spur|wimpy|starbucks|restaurant|coffee"),
    ("Transport", r"uber|bolt|engen|shell|sasol|caltex|bp\b|total ?energies|fuel|petrol|gautrain|toll"),
    ("Airtime & data", r"airtime|data bundle|vodacom|mtn|cell ?c|telkom|rain\b|prepaid"),
    ("Utilities", r"electricity|prepaid elec|eskom|city of|municipal|water|dstv|netflix|showmax|spotify|apple\.com|google"),
    ("Insurance", r"insurance|assurance|sanlam|old mutual|discovery|momentum|outsurance|miway|funeral|hollard|liberty"),
    ("Debt", r"loan|credit card|repayment|instalment|finance|debicheck|rcs|edgars|truworths|mr price money"),
    ("Cash", r"atm|cash withdrawal|cash sent"),
    ("Shopping", r"takealot|amazon|mr price|pep\b|ackermans|clicks|dis-?chem|game\b|incredible|builders|temu|shein"),
    ("Interest", r"interest"),
]
KNOWN = {"Income", "Savings", "Withdrawal", "Transfer", "Payments", "Cellphone", "Investments", "Fees", "Interest"}


def refresh_token(db: Session, user_id: int) -> str:
    conn = db.scalar(select(EEConnection).where(EEConnection.user_id == user_id))
    if not conn or not conn.mail_password.startswith(gmail.PREFIX):
        return ""
    return unseal(conn.mail_password[len(gmail.PREFIX):])


def passwords(db: Session, user_id: int):
    row = db.scalar(select(EESetting).where(EESetting.user_id == user_id))
    return (unseal(row.pdf_password),) if row and row.pdf_password else ()


def bank_for(sender: str, text: str = ""):
    sender = sender.lower()
    for domain, (key, _) in BANKS.items():
        if domain in sender:
            return key
    low = text.lower()
    for _, (key, name) in BANKS.items():
        if name.lower() in low:
            return key
    return "other"


def account_label(bank: str, text: str) -> str:
    m = ACCOUNT_RE.search(text)
    digits = re.sub(r"\D", "", m.group(1)) if m else ""
    return f"{NAMES.get(bank, bank.title())} ••{digits[-4:]}" if len(digits) >= 4 else NAMES.get(bank, bank.title())


def account_kind(text: str) -> str:
    head = text[:4000]
    if CREDIT_CARD_RE.search(head):
        return "credit"
    if LOAN_RE.search(head):
        return "loan"
    return "bank"


def categorise(description: str, parsed: str = "", amount: float = 0.0) -> str:
    parsed = next((w for w in reversed(parsed.split()) if w in KNOWN), "") if parsed else ""
    if parsed and parsed in KNOWN - {"Payments", "Withdrawal"}:
        return {"Transfer": "Transfers", "Investments": "Transfers", "Cellphone": "Airtime & data", "Savings": "Transfers",
                "Fees": "Bank fees"}.get(parsed, parsed)
    if FEE_RE.search(description):
        return "Bank fees"
    low = description.lower()
    for name, pattern in CATEGORIES:
        if re.search(pattern, low):
            return name
    return "Other income" if amount > 0 else "Other"


def closing_balance(text: str):
    m = None
    for m in CLOSING_RE.finditer(text):
        pass
    if not m:
        return None
    v = float(re.sub(r"[^\d.\-]", "", m.group(1)) or 0)
    return -abs(v) if (m.group(2) or "").lower() == "dr" else v


def _key(user_id, account, row):
    raw = f"{user_id}|{account}|{row['date']}|{row['signed']:.2f}|{row.get('balance')}|{row['description'][:120].lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:64]


def rows_from(parsed, kind):
    out = []
    for r in parsed:
        amount = r["amount"] if r["type"] == "credit" else -r["amount"]
        fee = float(r.get("fee") or 0)
        desc = r["description"]
        if not fee and "(fee)" in desc.lower():
            fee = abs(amount)
        out.append({"date": r["date"], "description": desc, "signed": amount, "fee": fee,
                    "balance": r.get("balance"), "category": categorise(desc, r.get("category") or "", amount)})
    return out


def save(db: Session, st: BankStatement, text: str, parsed):
    """Store the statement's rows (skipping ones already stored from an overlapping statement) and update the account."""
    st.kind = account_kind(text)
    st.account = account_label(st.bank, text)
    rows = rows_from(parsed, st.kind)
    db.query(BankTxn).filter(BankTxn.statement_id == st.id).delete()
    have = set(db.scalars(select(BankTxn.key).where(BankTxn.user_id == st.user_id)))
    seen_here = defaultdict(int)
    for r in rows:
        k = _key(st.user_id, st.account, r)
        seen_here[k] += 1
        if seen_here[k] > 1:  # two identical lines on one statement (two R5 airtime buys) are both real
            k = k[:58] + f"-{seen_here[k]:05d}"
        if k in have:
            continue
        have.add(k)
        db.add(BankTxn(user_id=st.user_id, statement_id=st.id, key=k, bank=st.bank, account=st.account, date=r["date"],
                       description=r["description"][:400], amount=round(r["signed"], 2), balance=r["balance"],
                       category=r["category"], fee=round(r["fee"], 2)))
    st.rows = len(rows)
    dated = [r for r in rows if r["balance"] is not None]
    last = max(dated, key=lambda r: r["date"]) if dated else None
    st.closing_balance = last["balance"] if last else closing_balance(text)
    st.closing_date = last["date"] if last else (max(r["date"] for r in rows) if rows else
                                                 (st.received_at.date() if st.received_at else None))
    if st.closing_balance is not None:
        acc = db.scalar(select(BankAccount).where(BankAccount.user_id == st.user_id, BankAccount.account == st.account))
        if not acc:
            acc = BankAccount(user_id=st.user_id, bank=st.bank, account=st.account, name=st.account, kind=st.kind)
            db.add(acc)
        if not acc.kind_set:
            acc.kind = st.kind
        if acc.balance_date is None or (st.closing_date and st.closing_date >= acc.balance_date):
            acc.balance, acc.balance_date = st.closing_balance, st.closing_date


def _walk(part):
    yield part
    for p in part.get("parts", []) or []:
        yield from _walk(p)


def search(token: str, limit_ids=500):
    headers = {"Authorization": f"Bearer {token}"}
    ids, page = [], None
    while len(ids) < limit_ids:
        r = requests.get(f"{gmail.API}/messages", headers=headers, timeout=30,
                         params={"q": QUERY, "maxResults": 100, **({"pageToken": page} if page else {})})
        if not r.ok:
            raise gmail.mail.MailError(f"Gmail search failed (HTTP {r.status_code})")
        data = r.json()
        ids += [m["id"] for m in data.get("messages", [])]
        page = data.get("nextPageToken")
        if not page:
            break
    return ids


def attachments(token: str, msg_id: str):
    """(headers dict, [(filename, bytes)]) for every PDF attached to the message."""
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(f"{gmail.API}/messages/{msg_id}", headers=headers, params={"format": "full"}, timeout=30)
    r.raise_for_status()
    msg = r.json()
    head = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    out = []
    for part in _walk(msg.get("payload", {})):
        name = part.get("filename") or ""
        if not name.lower().endswith(".pdf"):
            continue
        body = part.get("body", {})
        data = body.get("data")
        if not data and body.get("attachmentId"):
            a = requests.get(f"{gmail.API}/messages/{msg_id}/attachments/{body['attachmentId']}", headers=headers, timeout=60)
            a.raise_for_status()
            data = a.json().get("data")
        if data:
            out.append((name, base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))))
    return head, out


def read_batch(db: Session, user_id: int, limit=15):
    """Read up to `limit` new statement emails. Locked ones are retried (the PDF password may have been set since)."""
    rt = refresh_token(db, user_id)
    if not rt:
        return {"ok": False, "error": "Sign in with Google first (Account ▸ Google).", "read_now": 0, "left": 0}
    token = gmail.access_token(rt)
    ids = search(token)
    done = set(db.scalars(select(BankStatement.gmail_id).where(BankStatement.user_id == user_id,
                                                               BankStatement.status.in_(("ok", "error")))))
    todo = [i for i in ids if i not in done]
    pws = passwords(db, user_id)
    read_now = rows_now = 0
    for msg_id in todo[:limit]:
        try:
            head, files = attachments(token, msg_id)
        except Exception as e:
            log.warning("Bank statement %s download failed: %s", msg_id, e)
            continue
        try:
            received = parsedate_to_datetime(head.get("date")).astimezone().replace(tzinfo=None)
        except Exception:
            received = None
        for filename, data in files:
            st = db.scalar(select(BankStatement).where(BankStatement.user_id == user_id, BankStatement.gmail_id == msg_id,
                                                       BankStatement.filename == filename[:300]))
            if not st:
                st = BankStatement(user_id=user_id, gmail_id=msg_id, filename=filename[:300])
                db.add(st)
            st.subject, st.received_at = head.get("subject", "")[:400], received
            st.bank = bank_for(head.get("from", ""))
            db.flush()
            try:
                parsed, text = parsers.parse_pdf(data, st.bank, pws)
                st.bank = st.bank if st.bank != "other" else bank_for("", text)
                st.body = text
                save(db, st, text, parsed)
                st.status, st.error = "ok", ""
                rows_now += st.rows
            except ValueError as e:
                st.status, st.error = ("locked" if "password" in str(e) else "error"), str(e)[:500]
            except Exception as e:
                log.exception("Bank statement %s (%s) failed", msg_id, filename)
                st.status, st.error = "error", f"{type(e).__name__}: {e}"[:500]
            db.commit()
        read_now += 1
    left = max(0, len(todo) - read_now)
    log.info("Bank reader user %s: read %d emails, %d rows, %d left", user_id, read_now, rows_now, left)
    return {"ok": True, "found": len(ids), "read_now": read_now, "rows_now": rows_now, "left": left}


def reparse(db: Session, user_id: int):
    n = 0
    for st in db.scalars(select(BankStatement).where(BankStatement.user_id == user_id, BankStatement.status == "ok")):
        text = st.body
        if st.bank == "gotyme":
            continue  # GoTyme needs the PDF's word positions; its rows stay as first read
        from .parsers.capitec import CapitecParser
        from .parsers.generic import GenericParser
        from .parsers.tymebank import TymeBankLegacyParser

        parser = {"capitec": CapitecParser, "tymebank": TymeBankLegacyParser}.get(st.bank, GenericParser)()
        save(db, st, text, parser.parse(text))
        n += 1
    db.commit()
    return {"reparsed": n}


def position(db: Session, user_id: int):
    """Money in banks, debt (bank credit/loan accounts + typed-in liabilities) and fees, for net worth."""
    accounts = list(db.scalars(select(BankAccount).where(BankAccount.user_id == user_id).order_by(BankAccount.account)))
    manual = list(db.scalars(select(Liability).where(Liability.user_id == user_id).order_by(Liability.name)))
    cash = debt = 0.0
    for a in accounts:
        if a.hidden or a.balance is None:
            continue
        if a.kind == "bank" and a.balance >= 0:
            cash += a.balance
        else:
            debt += abs(a.balance)  # overdrawn bank account, card or loan balance
    debt += sum(max(0.0, m.balance) for m in manual)
    since = date.today() - timedelta(days=365)
    fees = db.scalar(select(func.coalesce(func.sum(BankTxn.fee), 0.0)).where(BankTxn.user_id == user_id,
                                                                            BankTxn.date >= since)) or 0.0
    fee_lines = db.scalar(select(func.coalesce(func.sum(-BankTxn.amount), 0.0)).where(
        BankTxn.user_id == user_id, BankTxn.date >= since, BankTxn.category == "Bank fees", BankTxn.fee == 0,
        BankTxn.amount < 0)) or 0.0
    return {"cash": round(cash, 2), "debt": round(debt, 2), "fees_12m": round(fees + fee_lines, 2),
            "accounts": accounts, "liabilities": manual}


def status(db: Session, user_id: int):
    rows = db.execute(select(BankStatement.status, func.count()).where(BankStatement.user_id == user_id)
                      .group_by(BankStatement.status)).all()
    counts = {s: n for s, n in rows}
    last = db.scalar(select(BankStatement).where(BankStatement.user_id == user_id, BankStatement.status != "ok")
                     .order_by(BankStatement.read_at.desc()))
    return {"google": bool(refresh_token(db, user_id)), "statements": sum(counts.values()), "ok": counts.get("ok", 0),
            "locked": counts.get("locked", 0), "failed": counts.get("error", 0),
            "transactions": db.scalar(select(func.count()).select_from(BankTxn).where(BankTxn.user_id == user_id)),
            "last_error": last.error if last else "", "pdf_password_set": bool(passwords(db, user_id)),
            "last_read": (db.scalar(select(func.max(BankStatement.read_at)).where(BankStatement.user_id == user_id)) or
                          None)}


def charts(db: Session, user_id: int, months=24):
    """Per month: money in, money out (spending, excl. transfers), fees; spending by category (last 12 months)."""
    start = (date.today().replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)
    txns = list(db.scalars(select(BankTxn).where(BankTxn.user_id == user_id, BankTxn.date >= start)))
    by_month = defaultdict(lambda: {"in": 0.0, "out": 0.0, "fees": 0.0, "transfers_in": 0.0, "transfers_out": 0.0})
    cats = defaultdict(float)
    year_ago = date.today() - timedelta(days=365)
    for t in txns:
        m = by_month[t.date.strftime("%Y-%m")]
        transfer = t.category == "Transfers"
        if t.amount >= 0:
            m["transfers_in" if transfer else "in"] += t.amount
        else:
            m["transfers_out" if transfer else "out"] += -t.amount
            if t.date >= year_ago and not transfer:
                cats[t.category] += -t.amount
        fee = t.fee or (-t.amount if t.category == "Bank fees" and t.amount < 0 else 0.0)
        m["fees"] += fee
        if t.fee:
            m["out"] += t.fee
            if t.date >= year_ago:
                cats["Bank fees"] += t.fee
    return {"months": [{"month": k, **{f: round(v, 2) for f, v in by_month[k].items()}} for k in sorted(by_month)],
            "categories": {k: round(v, 2) for k, v in sorted(cats.items(), key=lambda kv: -kv[1])}}


def read_all(db: Session):
    """Nightly: every user signed in with Google."""
    n = 0
    for conn in db.scalars(select(EEConnection).where(EEConnection.mail_password.startswith(gmail.PREFIX))):
        try:
            read_batch(db, conn.user_id, limit=25)
            n += 1
        except Exception:
            db.rollback()
            log.exception("Bank reader failed for user %s", conn.user_id)
    return n
