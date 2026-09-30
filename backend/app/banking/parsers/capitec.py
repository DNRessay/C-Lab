import logging
import re
from datetime import datetime

from .common import AMOUNT, make_txn, to_float

log = logging.getLogger(__name__)

CATEGORY_WORDS = {
    "Income", "Savings", "Withdrawal", "Transfer", "Payments", "Cellphone",
    "Uncategorised", "Investments", "Fees", "Interest",
}
CREDIT_HINTS = ("payment received", "received", "deposit", "interest received", "transfer received", "refund")
DEBIT_HINTS = ("payment:", "sent", "cash sent", "withdrawal", "purchase", "transfer to", "prepaid", "voucher", "debicheck")

ROW = re.compile(r"^(\d{2}/\d{2}/\d{4})\s+(.+)$")
THREE = re.compile(rf"^(.+?)\s+({AMOUNT})\s+({AMOUNT})\s+({AMOUNT})\s*$")
TWO = re.compile(rf"^(.+?)\s+({AMOUNT})\s+({AMOUNT})\s*$")
AMOUNTS_ONLY = re.compile(rf"^({AMOUNT})\s+({AMOUNT})\s*$")
SKIP = ("Transaction History", "Money In", "Money Out")


def split_category(text):
    """Capitec prints the category after the description, e.g. 'Checkers Sandton Groceries'."""
    words = text.split()
    for i in range(len(words) - 1, -1, -1):
        if words[i] in CATEGORY_WORDS:
            two_word = i > 0 and words[i - 1] not in CATEGORY_WORDS
            category = f"{words[i - 1]} {words[i]}" if two_word else words[i]
            return " ".join(words[: i - 1] if two_word else words[:i]), category
    return text, None


class CapitecParser:
    def parse(self, text):
        out = []
        lines = text.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            m = ROW.match(line) if line and not any(s in line for s in SKIP) else None
            if not m:
                i += 1
                continue
            try:
                day = datetime.strptime(m.group(1), "%d/%m/%Y").date()
                rest = m.group(2).strip()
                fee = 0.0
                if (m3 := THREE.match(rest)):
                    body, amount, fee, balance = m3.group(1), to_float(m3.group(2)), to_float(m3.group(3)), to_float(m3.group(4))
                elif (m2 := TWO.match(rest)):
                    body, amount, balance = m2.group(1), to_float(m2.group(2)), to_float(m2.group(3))
                elif i + 1 < len(lines) and (mn := AMOUNTS_ONLY.match(lines[i + 1].strip())):
                    body, amount, balance = rest, to_float(mn.group(1)), to_float(mn.group(2))
                    i += 1
                else:
                    i += 1
                    continue

                description, category = split_category(body.strip())
                low = description.lower()
                if any(k in low for k in CREDIT_HINTS):
                    is_credit = True
                elif any(k in low for k in DEBIT_HINTS):
                    is_credit = False
                elif amount < 0:
                    is_credit = False
                else:
                    cat = (category or "").lower()
                    is_credit = "income" in cat or "received" in cat

                if abs(amount) > 0 and len(description) >= 3:
                    out.append(make_txn(
                        day, description, amount, "credit" if is_credit else "debit", "CAP", len(out),
                        category=category, fee=abs(fee), balance=balance,
                    ))
            except (ValueError, IndexError) as e:
                log.warning("Capitec row skipped: %s", e)
            i += 1
        log.info("Capitec: %d transactions", len(out))
        return out
