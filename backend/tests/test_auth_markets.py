from fastapi.testclient import TestClient

from app.invest import prices
from app.main import app, handler
from tests.test_invest import fake_fetch

api = TestClient(app)


def test_health():
    assert api.get("/api/health").json() == {"status": "ok", "database": True}


def test_signup_closed_to_strangers_after_owner():
    # test_invest registers the owner first ("investor"); make sure one exists regardless of order
    api.post("/api/auth/register", json={"first_name": "investor", "email": "investor@inv.example.com", "password": "Str0ng-pass!"})
    r = api.post("/api/auth/register", json={"first_name": "x", "email": "stranger@example.com", "password": "Str0ng-pass!"})
    assert r.status_code == 403


def test_login_refresh_me_password():
    r = api.post("/api/auth/login", json={"email": "INVESTOR@inv.example.com", "password": "Str0ng-pass!"})
    assert r.status_code == 200
    tok = r.json()
    h = {"Authorization": f"Bearer {tok['access']}"}
    assert api.get("/api/auth/me", headers=h).json()["email"] == "investor@inv.example.com"
    assert "access" in api.post("/api/auth/token/refresh", json={"refresh": tok["refresh"]}).json()
    assert api.post("/api/auth/login", json={"email": "investor@inv.example.com", "password": "nope"}).status_code == 401
    assert api.post("/api/auth/change-password", headers=h,
                    json={"old_password": "wrong", "new_password": "An0ther-pass!"}).status_code == 400


def test_markets_board(monkeypatch):
    monkeypatch.setattr(prices, "fetch", lambda s: fake_fetch(s) if s in ("ZAR=X", "STX40.JO", "STXPRO.JO") else (_ for _ in ()).throw(ValueError("x")))
    h = {"Authorization": "Bearer " + api.post("/api/auth/login", json={"email": "investor@inv.example.com", "password": "Str0ng-pass!"}).json()["access"]}
    m = {x["symbol"]: x for x in api.get("/api/markets", headers=h).json()["markets"]}
    assert m["ZAR=X"]["label"] == "USD/ZAR" and m["ZAR=X"]["price"] == 18.0
    assert m["STX40.JO"]["price"] == 90.0  # cents converted to rand
    assert m["^GSPC"]["price"] is None     # feed failure shows as missing, not a crash


def test_schedule_event_runs_alerts(monkeypatch):
    import app.invest.alerts as alerts

    monkeypatch.setattr(alerts, "check_all", lambda db: 3)
    assert handler({"source": "aws.events", "detail-type": "Scheduled Event"}, None) == {"alerts_sent": 3}
