# Unofficial EasyEquities platform client, adapted from easy-equities-client 0.5.0 (MIT, see LICENSE file here).
# Kept in the repo on purpose: when EasyEquities changes their site we patch this file instead of waiting on PyPI.
# Every failure is raised as PlatformError with the step that broke, so the UI can say exactly what stopped working.
import json
import re
import urllib.parse

from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as http
    SESSION_KW = {"impersonate": "chrome"}
except ImportError:  # pragma: no cover
    import requests as http
    SESSION_KW = {}

BASE_URL = "https://platform.easyequities.io"
SIGN_IN = "/Account/SignIn"
OVERVIEW = "/AccountOverview"
SWITCH_ACCOUNT = "/Menu/UpdateCurrency"
VALUATIONS = "/AccountOverview/GetTrustAccountValuations"
HOLDINGS = "/AccountOverview/GetHoldingsView?stockViewCategoryId=12"
TRANSACTIONS = "/TransactionHistory/GetTransactions"


class PlatformError(Exception):
    def __init__(self, stage, message, page=""):
        super().__init__(f"{stage}: {message}")
        self.stage, self.message = stage, message
        self.page = (page or "")[:50_000]  # kept for debugging a changed page layout


def money(text):
    """'R2 000.00' / '$353.61' / '-R12.30' -> float, or None."""
    if text is None:
        return None
    s = str(text).replace("\xa0", "").replace(" ", "").replace(",", "")
    m = re.search(r"(-?)[^\d-]*(\d+(?:\.\d+)?)", s)
    return float(m.group(1) + m.group(2)) if m else None


def _text(el):
    return el.get_text(" ", strip=True) if el else ""


def parse_accounts(page: str):
    soup = BeautifulSoup(page, "html.parser")
    accounts = []
    for div in soup.find_all(attrs={"id": "trust-account-types"}):
        parent = div.parent
        if parent and parent.attrs.get("data-tradingcurrencyid") and parent.attrs.get("data-id"):
            accounts.append({"id": parent["data-id"].strip(), "name": _text(div),
                             "currency_id": parent["data-tradingcurrencyid"].strip()})
    if not accounts:  # fallback: any element carrying both data attributes
        for el in soup.find_all(attrs={"data-tradingcurrencyid": True, "data-id": True}):
            accounts.append({"id": el["data-id"].strip(), "name": _text(el)[:80],
                             "currency_id": el["data-tradingcurrencyid"].strip()})
    seen, out = set(), []
    for a in accounts:
        if a["id"] not in seen:
            seen.add(a["id"])
            out.append(a)
    return out


def _cell(div, cls):
    return _text(div.find(attrs={"class": cls}))


def parse_holdings(page):
    soup = BeautifulSoup(page, "html.parser")
    out, seen = [], set()
    for div in soup.find_all(attrs={"class": "holding-inner-container"}):
        name = _cell(div, "equity-image-as-text")
        if not name or name in seen:
            continue
        img = div.find(attrs={"class": "instrument"})
        src = img.attrs.get("src", "") if img else ""
        code = re.search(r"/([A-Z0-9.]+)\.png", src)
        span = div.find(attrs={"data-detailviewurl": True})
        view = span.attrs["data-detailviewurl"] if span else ""
        seen.add(name)
        out.append({
            "name": name,
            "contract_code": code.group(1) if code else "",
            "purchase_value": money(_cell(div, "purchase-value-cell")),
            "current_value": money(_cell(div, "current-value-cell")),
            "current_price": money(_cell(div, "current-price-cell")),
            "view_url": view,
            "isin": view.split("=")[-1] if "=" in view else "",
        })
    return out


def parse_shares(page):
    """Whole shares + fractional share rights from a holding's detail page."""
    text = _text(BeautifulSoup(page, "html.parser"))
    whole = re.search(r"#\s*Shares\s*([\d ,]+)", text)
    frac = re.search(r"#\s*FSR\w*\s*(\.?\d+)", text)
    if not whole and not frac:
        return None
    w = float(whole.group(1).replace(" ", "").replace(",", "") or 0) if whole else 0.0
    f = frac.group(1) if frac else ""
    return w + (float(f if f.startswith(".") else "0." + f) if f else 0.0)


class Platform:
    def __init__(self, base_url=BASE_URL, session=None):
        self.base_url = base_url
        self.s = session or http.Session(**SESSION_KW)
        self.current = None

    def _url(self, path):
        return self.base_url + path

    def _get(self, stage, path):
        try:
            r = self.s.get(self._url(path), timeout=30)
        except Exception as e:
            raise PlatformError(stage, f"request failed ({e})")
        if r.status_code != 200:
            raise PlatformError(stage, f"HTTP {r.status_code}", r.text)
        return r

    def login(self, username, password):
        data = (f"UserIdentifier={urllib.parse.quote(username)}&Password={urllib.parse.quote(password)}"
                "&ReturnUrl=&OneSignalGameId=&IsUsingNewLayoutSatrixOrEasyEquitiesMobileApp=False")
        try:
            r = self.s.post(self._url(SIGN_IN), data=data, allow_redirects=False, timeout=30,
                            headers={"Content-Type": "application/x-www-form-urlencoded",
                                     "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
        except Exception as e:
            raise PlatformError("login", f"request failed ({e})")
        if r.status_code != 302:
            hint = "wrong username/password, or EasyEquities now asks for a one-time PIN"
            raise PlatformError("login", f"not accepted (HTTP {r.status_code}: {hint})", r.text)
        location = r.headers.get("location", "") or ""
        if "signin" in location.lower() or "otp" in location.lower() or "verify" in location.lower():
            raise PlatformError("login", f"redirected to {location} (password rejected or extra verification needed)")

    def accounts(self):
        r = self._get("accounts", OVERVIEW)
        accounts = parse_accounts(r.text)
        if not accounts:
            raise PlatformError("accounts", "no accounts found on the overview page (layout changed?)", r.text)
        return accounts

    def switch(self, account_id):
        if self.current == account_id:
            return
        try:
            r = self.s.post(self._url(SWITCH_ACCOUNT), data={"trustAccountId": account_id}, timeout=30)
        except Exception as e:
            raise PlatformError("switch account", f"request failed ({e})")
        if r.status_code != 200:
            raise PlatformError("switch account", f"HTTP {r.status_code}", r.text)
        self.current = account_id

    def valuations(self, account_id):
        self.switch(account_id)
        r = self._get("valuations", VALUATIONS)
        try:
            data = r.json()
            return json.loads(data) if isinstance(data, str) else data
        except ValueError:
            raise PlatformError("valuations", "response is not JSON", r.text)

    def holdings(self, account_id, with_shares=True):
        self.switch(account_id)
        r = self._get("holdings", HOLDINGS)
        rows = parse_holdings(r.text)
        if not rows and "holding-inner-container" not in r.text and "No holdings" not in r.text:
            # Either an empty account or a changed layout; keep the page so we can tell which.
            raise PlatformError("holdings", "could not read the holdings table (layout changed?)", r.text)
        if with_shares:
            for h in rows:
                if not h["view_url"]:
                    continue
                try:
                    h["shares"] = parse_shares(self._get("holding detail", h["view_url"]).text)
                except PlatformError:
                    h["shares"] = None  # value still comes through; units are a nice-to-have
        return rows

    def transactions(self, account_id):
        self.switch(account_id)
        r = self._get("transactions", TRANSACTIONS)
        try:
            return r.json()
        except ValueError:
            raise PlatformError("transactions", "response is not JSON", r.text)


def valuation_total(valuation):
    """Best effort 'account value' from the valuations JSON."""
    if not isinstance(valuation, dict):
        return None
    top = valuation.get("TopSummary") or {}
    for key in ("AccountValue", "TotalValue", "AccountValueNumeric"):
        if key in top:
            v = money(top[key])
            if v is not None:
                return v
    for item in top.get("AccountValues", []) if isinstance(top.get("AccountValues"), list) else []:
        v = money(item.get("Value"))
        if v is not None:
            return v
    return None


def snapshot(username, password, base_url=BASE_URL, client=None):
    """Log in and read every account. Raises PlatformError; returns a JSON-safe dict."""
    p = client or Platform(base_url)
    p.login(username, password)
    out = []
    for acc in p.accounts():
        item = {**acc, "holdings": [], "valuation": None, "transactions": [], "warnings": []}
        item["holdings"] = p.holdings(acc["id"])
        for stage, fn in (("valuation", p.valuations), ("transactions", p.transactions)):
            try:
                item[stage] = fn(acc["id"])
            except PlatformError as e:
                item["warnings"].append(str(e))
        holdings_value = sum(h["current_value"] or 0 for h in item["holdings"])
        item["value"] = valuation_total(item["valuation"]) or holdings_value
        item["holdings_value"] = holdings_value
        item["purchase_value"] = sum(h["purchase_value"] or 0 for h in item["holdings"])
        out.append(item)
    return {"accounts": out}
