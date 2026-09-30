import json

from fastapi.testclient import TestClient

from app.ai import llm
from app.config import settings
from app.main import app
from tests.test_invest import market, register  # noqa: F401

api = TestClient(app)


def test_suggestions_and_chat(market, monkeypatch):
    h = register("aiuser")
    api.post("/api/bank/liabilities", headers=h, json={"name": "Car", "kind": "vehicle", "balance": 90000, "rate": 13})
    assert api.get("/api/ai/status", headers=h).json() == {"suggestions": False, "chat": False}
    assert api.get("/api/ai/suggestions/overview?refresh=true", headers=h).status_code == 503  # no key yet

    monkeypatch.setattr(settings, "cohere_api_key", "k")
    monkeypatch.setattr(settings, "groq_api_keys", ["g1", "g2"])
    sent = {}

    def fake_cohere(system, user, json_mode=False):
        sent["prompt"] = user
        return json.dumps({"suggestions": [
            {"title": "Pay off the car first", "detail": "13% on R90,000 costs more than your shares earn.",
             "impact": "high", "saves_or_gains_rand_per_year": 11700},
            {"title": "", "detail": "dropped: no title"}]})
    monkeypatch.setattr(llm, "cohere", fake_cohere)
    assert api.get("/api/ai/suggestions/overview", headers=h).json()["items"] == []  # nothing yet, no call made
    r = api.get("/api/ai/suggestions/overview?refresh=true", headers=h).json()
    assert r["items"] == [{"title": "Pay off the car first", "detail": "13% on R90,000 costs more than your shares earn.",
                           "impact": "high", "value": 11700}]
    assert '"owing": 90000.0' in sent["prompt"] and "Car" not in sent["prompt"]  # debt amount, not the name
    assert api.get("/api/ai/suggestions/overview", headers=h).json()["cached"] is True
    assert api.get("/api/ai/suggestions/nope", headers=h).status_code == 404

    calls = []

    def fake_post(url, **kw):
        calls.append(kw["headers"]["Authorization"])

        class R:
            status_code = 429 if len(calls) == 1 else 200
            ok = status_code == 200
            text = ""

            def json(self):
                return {"choices": [{"message": {"content": "Your net worth is ..."}}]}
        return R()
    monkeypatch.setattr(llm.requests, "post", fake_post)
    r = api.post("/api/ai/chat", headers=h, json={"messages": [{"role": "user", "content": "How am I doing?"}]})
    assert r.json() == {"reply": "Your net worth is ..."}
    assert calls[0] != calls[1]  # rate-limited key, then the next one
