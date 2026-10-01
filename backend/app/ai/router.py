import json
from datetime import datetime, timedelta
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import JSON, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from ..config import settings
from ..db import Base
from ..deps import current_user, get_db
from ..invest.models import user_fk
from ..models import User, created, pk, text, utcnow
from . import context, llm

router = APIRouter(prefix="/api/ai", tags=["ai"])

SECTIONS = {
    "overview": "the whole picture: how their banking (income, spending, fees, debt, cash) and their investments "
                "(EasyEquities, property) fit together, and what to change so more money ends up growing",
    "banking": "their bank accounts: spending by category, bank fees, debt, cash buffer, what's left each month",
    "holdings": "their share/ETF holdings: concentration, gains and losses, dividends, diversification",
    "transactions": "costs of investing: fees and tax compared with dividends and interest, and how to pay less",
    "property": "property: EasyProperties yields and gains, own property equity and bond",
    "watchlist": "dividends coming up from EasyEquities' updates and their watchlist: which look worth a closer look",
}
CACHE = timedelta(hours=30)  # nightly warm-up keeps these fresh
MIN_REFRESH = timedelta(hours=6)  # a manual refresh at most this often per tab
CHAT_CACHE = timedelta(hours=12)


class AIChatCache(Base):
    """Answers to one-question chats (the starter questions), so tapping them again doesn't use the API."""

    __tablename__ = "ai_chat_cache"
    __table_args__ = (UniqueConstraint("user_id", "question"),)
    id: Mapped[int] = pk()
    user_id: Mapped[int] = user_fk()
    question: Mapped[str] = text(300)
    reply: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = created()


class AISuggestion(Base):
    __tablename__ = "ai_suggestions"
    __table_args__ = (UniqueConstraint("user_id", "section"),)
    id: Mapped[int] = pk()
    user_id: Mapped[int] = user_fk()
    section: Mapped[str] = text(20)
    items: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = created()


@router.get("/status")
def status(user: User = Depends(current_user)):
    return {"suggestions": bool(settings.cohere_api_key), "chat": bool(settings.groq_api_keys)}


def suggest(db: Session, user_id: int, section: str):
    data = context.build(db, user_id)
    system = (llm.NOTE + " Reply with JSON only: {\"suggestions\": [{\"title\": short, \"detail\": 1-3 sentences with "
              "their numbers, \"impact\": \"high\"|\"medium\"|\"low\", \"saves_or_gains_rand_per_year\": number or null}]}. "
              "3 to 5 suggestions, most useful first. Only suggest what their data supports.")
    prompt = f"Focus: {SECTIONS[section]}.\n\nTheir numbers (JSON):\n{json.dumps(data, default=str)}"
    out = llm.parse_json(llm.cohere(system, prompt, json_mode=True)) or {}
    items = out.get("suggestions", out) if isinstance(out, dict) else out
    clean = []
    for i in items if isinstance(items, list) else []:
        if isinstance(i, dict) and i.get("title"):
            clean.append({"title": str(i["title"])[:140], "detail": str(i.get("detail", ""))[:700],
                          "impact": i.get("impact") if i.get("impact") in ("high", "medium", "low") else "medium",
                          "value": i.get("saves_or_gains_rand_per_year") if isinstance(i.get("saves_or_gains_rand_per_year"), (int, float)) else None})
    return clean[:6]


def _out(row):
    return {"items": row.items, "created_at": row.created_at.isoformat(), "cached": True,
            "next_refresh_at": (row.created_at + MIN_REFRESH).isoformat()}


def save_suggestions(db: Session, user_id: int, section: str):
    row = db.scalar(select(AISuggestion).where(AISuggestion.user_id == user_id, AISuggestion.section == section))
    items = suggest(db, user_id, section)
    row = row or AISuggestion(user_id=user_id, section=section)
    row.items, row.created_at = items, utcnow()
    db.add(row)
    db.commit()
    return row


@router.get("/suggestions/{section}")
def suggestions(section: str, refresh: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Cached suggestions (made nightly). A refresh calls Cohere at most once every 6 hours per tab."""
    if section not in SECTIONS:
        raise HTTPException(404, "Unknown section.")
    row = db.scalar(select(AISuggestion).where(AISuggestion.user_id == user.id, AISuggestion.section == section))
    if row and (not refresh or utcnow() - row.created_at < MIN_REFRESH):
        return _out(row)
    if not refresh:
        return {"items": [], "created_at": None, "cached": False, "available": bool(settings.cohere_api_key)}
    try:
        row = save_suggestions(db, user.id, section)
    except llm.AIError as e:
        raise HTTPException(503, str(e))
    return {**_out(row), "cached": False}


def warm_all(db: Session, deadline=None):
    """Nightly: fresh suggestions for every tab of every user with data, so pages never wait on the API."""
    from ..invest.ee.models import EEConnection

    if not settings.cohere_api_key:
        return 0
    import time

    made = 0
    for uid in list(db.scalars(select(EEConnection.user_id))):
        rows = {r.section: r for r in db.scalars(select(AISuggestion).where(AISuggestion.user_id == uid))}
        for section in sorted(SECTIONS, key=lambda k: rows[k].created_at if k in rows else datetime.min):  # oldest first
            if deadline and time.time() > deadline:
                return made  # out of time tonight; the rest go first tomorrow
            row = rows.get(section)
            if row and utcnow() - row.created_at < timedelta(hours=20):
                continue
            try:
                save_suggestions(db, uid, section)
                made += 1
            except llm.AIError:
                db.rollback()
                return made  # out of quota for now; the rest stay cached from before
            except Exception:
                db.rollback()
    return made


class Msg(BaseModel):
    role: str
    content: str = Field(max_length=4000)


class ChatIn(BaseModel):
    messages: List[Msg] = Field(min_length=1, max_length=30)


@router.post("/chat")
def chat(body: ChatIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    single = len(body.messages) == 1
    q = body.messages[-1].content.strip()[:300]
    if single:
        hit = db.scalar(select(AIChatCache).where(AIChatCache.user_id == user.id, AIChatCache.question == q))
        if hit and utcnow() - hit.created_at < CHAT_CACHE:
            return {"reply": hit.reply, "cached": True}
    data = context.build(db, user.id)
    system = (llm.NOTE + " Answer in plain, short language (use lists when helpful). Here are their latest numbers "
              f"from C-Lab (JSON), use them:\n{json.dumps(data, default=str)}")
    history = [{"role": m.role if m.role in ("user", "assistant") else "user", "content": m.content} for m in body.messages[-12:]]
    try:
        reply = llm.groq([{"role": "system", "content": system}, *history])
    except llm.AIError as e:
        raise HTTPException(503, str(e))
    if single:
        hit = db.scalar(select(AIChatCache).where(AIChatCache.user_id == user.id, AIChatCache.question == q))
        hit = hit or AIChatCache(user_id=user.id, question=q)
        hit.reply, hit.created_at = reply, utcnow()
        db.add(hit)
        db.commit()
    return {"reply": reply}
