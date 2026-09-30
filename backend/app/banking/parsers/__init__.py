# SA bank statement parsers (shared with C.T.H.A.I). Each returns rows of
# {date, description, amount (positive), type: credit|debit, reference, [category, fee, balance]}.
import io
import logging

from .capitec import CapitecParser
from .generic import GenericParser
from .gotyme import GoTymeParser, is_gotyme
from .tymebank import TymeBankLegacyParser

log = logging.getLogger(__name__)

__all__ = ["parse_pdf", "extract_text", "open_pdf"]


def open_pdf(pdf_bytes, passwords=()):
    """(PdfReader, password that worked). Tries no password, blank, then each saved password."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    if not reader.is_encrypted:
        return reader, None
    for pw in ("", *passwords):
        try:
            if reader.decrypt(pw):
                return reader, pw
        except Exception:
            continue
    raise ValueError("statement is password-protected and none of the saved passwords opened it")


def extract_text(pdf_bytes, passwords=()):
    reader, pw = open_pdf(pdf_bytes, passwords)
    return "\n".join(page.extract_text() or "" for page in reader.pages), pw


def parse_pdf(pdf_bytes, bank_name, passwords=()):
    """(rows, text)."""
    text, pw = extract_text(pdf_bytes, passwords)
    if bank_name == "gotyme" or is_gotyme(text):
        return GoTymeParser().parse(pdf_bytes, pw), text
    if bank_name == "tymebank":
        return TymeBankLegacyParser().parse(text), text
    if bank_name == "capitec":
        return CapitecParser().parse(text), text
    return GenericParser().parse(text), text
