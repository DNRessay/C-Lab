# EasyProperties: an Angular app backed by an API. Login is OAuth2 code + PKCE through EasyID (identity.openeasy.io),
# the same flow the EasyProperties site runs in the browser. Values below come from that site's public app bundle.
import base64
import hashlib
import re
import secrets
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from .platform import SESSION_KW, PlatformError, http, money

API = "https://apigateway.openeasy.io/property-client-rest/v1"
IDP = "https://identity.openeasy.io"
CLIENT_ID = "33e0a18d2e654488bfb950170d258988"
SCOPE = "openid platform profile ep_userinfo api_gateway auction_rest_api account_api properties_api referrals_api"
REDIRECT = "https://platform.easyproperties.co.za/account/authorize"
DATA_PATHS = ["/user/account", "/user/accounts/", "/property/all"]


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def login(username, password, session=None):
    """Returns (session, access_token). Raises PlatformError naming the step that failed."""
    s = session or http.Session(**SESSION_KW)
    verifier, challenge = _pkce()
    query = urlencode({"client_id": CLIENT_ID, "redirect_uri": REDIRECT, "response_type": "code", "scope": SCOPE,
                       "code_challenge": challenge, "code_challenge_method": "S256"})
    try:
        r = s.get(f"{IDP}/connect/authorize?{query}", timeout=30)
    except Exception as e:
        raise PlatformError("easyproperties login page", f"request failed ({e})")
    soup = BeautifulSoup(r.text, "html.parser")
    form = soup.find("form", id="loginForm") or soup.find("form")
    if not form:
        raise PlatformError("easyproperties login page", "no login form found", r.text)
    data = {i.get("name"): i.get("value", "") for i in form.find_all("input") if i.get("name")}
    data.update({"Username": username, "Password": password, "IsUsernameProvided": "true"})
    page_url = str(getattr(r, "url", "") or f"{IDP}/Account/Login")
    action = urljoin(page_url, form.get("action") or page_url)

    url, method, body = action, "post", data
    for _ in range(8):  # follow redirects by hand until EasyID sends us back with ?code=
        try:
            r = s.post(url, data=body, allow_redirects=False, timeout=30) if method == "post" else \
                s.get(url, allow_redirects=False, timeout=30)
        except Exception as e:
            raise PlatformError("easyproperties login", f"request failed ({e})")
        location = r.headers.get("location") or ""
        if location.startswith(REDIRECT):
            code = parse_qs(urlparse(location).query).get("code", [""])[0]
            if not code:
                raise PlatformError("easyproperties login", "EasyID returned without a code")
            break
        if r.status_code in (301, 302, 303, 307, 308) and location:
            url, method, body = urljoin(url, location), "get", None
            continue
        page = r.text or ""
        hint = "captcha or extra verification required" if re.search(r"captcha|recaptcha|one.time|otp", page, re.I) \
            else "username/password not accepted"
        raise PlatformError("easyproperties login", f"EasyID did not sign in (HTTP {r.status_code}: {hint})", page)
    else:
        raise PlatformError("easyproperties login", "too many redirects")

    try:
        r = s.post(f"{API}/user/accesstoken", json={"authorizationCode": code, "codeVerifier": verifier,
                                                    "redirectUri": REDIRECT}, timeout=30)
        tokens = r.json()
    except Exception as e:
        raise PlatformError("easyproperties token", f"token exchange failed ({e})")
    token = _first(tokens, ("accessToken", "access_token", "AccessToken", "token"))
    if not token:
        raise PlatformError("easyproperties token", f"no access token in reply (keys: {shape(tokens, 1)})")
    return s, token


def _first(d, keys):
    if isinstance(d, dict):
        for k in keys:
            if d.get(k):
                return d[k]
        for v in d.values():
            if isinstance(v, dict):
                found = _first(v, keys)
                if found:
                    return found
    return None


def shape(obj, depth=3):
    """Keys only (no values) of a JSON reply, to learn its structure from the logs."""
    if depth <= 0:
        return "…"
    if isinstance(obj, dict):
        return {k: shape(v, depth - 1) for k, v in list(obj.items())[:40]}
    if isinstance(obj, list):
        return [shape(obj[0], depth - 1), f"x{len(obj)}"] if obj else []
    return type(obj).__name__


def _num(v):
    if isinstance(v, (int, float)):
        return float(v)
    return money(v) if isinstance(v, str) else None


def _pick(d, *keys):
    for k in keys:
        for cand in (k, k[0].upper() + k[1:]):
            if isinstance(d, dict) and d.get(cand) not in (None, ""):
                return d[cand]
    return None


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def extract_holdings(*replies):
    """Find property holdings in whatever shape the API returns. Prices on EasyProperties are in cents."""
    out, seen = [], set()
    for reply in replies:
        for d in _walk(reply):
            holding = d.get("holding") if isinstance(d.get("holding"), dict) else None
            prop = d.get("property") if isinstance(d.get("property"), dict) else None
            if holding is None and prop is not None and _pick(d, "quantity", "units", "shares", "totalShares"):
                holding, source = d, prop
            elif holding is not None:
                source = prop or d
            else:
                continue
            name = _pick(source, "title", "name", "propertyName") or _pick(d, "title", "name")
            qty = _num(_pick(holding, "quantity", "units", "shares", "totalShares", "sharesHeld", "volume"))
            if not name or not qty or name in seen:
                continue
            value = _num(_pick(holding, "currentValue", "marketValue", "value", "totalValue"))
            price_c = _num(_pick(holding, "lastPrice", "currentPrice", "price") or _pick(source, "lastPrice", "currentPrice"))
            vwap_c = _num(_pick(holding, "vwap", "averagePrice", "avgPrice"))
            if value is None and price_c is not None:
                value = qty * price_c / 100
            cost = _num(_pick(holding, "purchaseValue", "costValue", "totalCost", "cost"))
            if cost is None and vwap_c is not None:
                cost = qty * vwap_c / 100
            fin = source.get("financialInfo") if isinstance(source.get("financialInfo"), dict) else {}
            yld = _num(_pick(source, "rentalYield", "dividendYield") or _pick(fin, "rentalYield", "dividendYield",
                                                                              "grossYield", "netYield"))
            seen.add(name)
            out.append({"name": str(name)[:200], "contract_code": str(_pick(source, "contractCode") or ""),
                        "shares": qty, "current_value": value, "purchase_value": cost,
                        "current_price": (value / qty) if value is not None and qty else None,
                        # Yields run 0-3%: 0.89 means 0.89%, while a fraction would be tiny (0.0089).
                        "rental_yield": (yld / 100 if yld is not None and yld >= 0.05 else yld),
                        "view_url": "", "isin": ""})
    return out


def fetch(username, password, session=None):
    """Your EasyProperties holdings, plus the reply structure (keys only) for debugging."""
    s, token = login(username, password, session)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    replies, shapes = [], {}
    for path in DATA_PATHS:
        try:
            r = s.get(API + path, headers=headers, timeout=30)
            data = r.json() if r.status_code == 200 else None
        except Exception as e:
            shapes[path] = f"error: {str(e)[:120]}"
            continue
        shapes[path] = shape(data) if data is not None else f"HTTP {r.status_code}"
        if data is not None:
            replies.append(data)
    holdings = extract_holdings(*replies)
    return {"holdings": holdings, "source": "api", "shapes": shapes}
