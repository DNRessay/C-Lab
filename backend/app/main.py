import logging
import re

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from mangum import Mangum
from sqlalchemy import text

from . import auth
from .config import settings
from .db import SessionLocal, init_db
from .invest import markets
from .banking.router import router as bank_router
from .invest.ee.router import router as ee_router
from .invest.router import router as invest_router

logging.getLogger().setLevel(logging.INFO)

if settings.auto_create_tables:
    init_db()

app = FastAPI(title="C-Lab API", redirect_slashes=False, docs_url="/docs" if settings.debug else None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_origin_regex=settings.cors_origin_regex,
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def strip_trailing_slash(request: Request, call_next):
    path = request.scope["path"]
    if len(path) > 1 and path.endswith("/"):
        request.scope["path"] = path.rstrip("/")
    return await call_next(request)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    errors = {}
    for e in exc.errors():
        field = ".".join(str(p) for p in e["loc"] if p not in ("body", "query", "path", "form"))
        errors.setdefault(field or "non_field_errors", []).append(e["msg"])
    return JSONResponse({"detail": errors}, status_code=400)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        raise exc
    logging.exception("Unhandled error on %s", request.url.path)
    return JSONResponse({"detail": "Internal server error."}, status_code=500)


for r in (auth.router, invest_router, markets.router, ee_router, bank_router):
    app.include_router(r)


@app.get("/api/health")
def health():
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        ok = True
    except Exception:
        ok = False
    finally:
        db.close()
    return {"status": "ok" if ok else "degraded", "database": ok}


_asgi = Mangum(app, lifespan="off")


# Lambda entrypoint: app.main.handler. The nightly EventBridge schedule calls it too:
# EasyEquities sync first (so prices are fresh), then price alerts.
def admin(event):
    """Direct Lambda invokes only (AWS credentials needed; a Function URL request can't produce this event shape)."""
    from sqlalchemy import func, select

    from .invest.ee import reader
    from .invest.ee.models import EESetting
    from .models import User
    from .security import seal

    db = SessionLocal()
    try:
        user = db.get(User, int(event["user_id"])) if event.get("user_id") else \
            db.scalar(select(User).where(func.lower(User.email) == str(event.get("email", "")).lower()))
        if not user:
            return {"ok": False, "error": "no such user"}
        if event.get("action") == "set_pdf_password":
            row = db.scalar(select(EESetting).where(EESetting.user_id == user.id)) or EESetting(user_id=user.id)
            row.pdf_password = seal(str(event.get("password", "")).strip())
            db.add(row)
            db.commit()
            return {"ok": True, **{k: v for k, v in reader.status(db, user.id).items() if k != "last_read"}}
        if event.get("action") == "reparse_statements":
            return {"ok": True, **reader.reparse(db, user.id)}
        if event.get("action") == "read_statements":
            from .invest.ee.models import EEConnection

            conn = db.scalar(select(EEConnection).where(EEConnection.user_id == user.id))
            return {"ok": True, **reader.read_batch(db, conn, limit=int(event.get("limit", 25)))}
        if event.get("action") == "statement_shape":
            # Layout only (letters -> a, digits -> 9) of a stored statement, plus which lines the parser took.
            from .invest.ee.models import EEStatementDoc

            docs = list(db.scalars(select(EEStatementDoc).where(EEStatementDoc.user_id == user.id,
                                                                EEStatementDoc.status == "ok").order_by(EEStatementDoc.id)))
            kind = event.get("kind")
            docs = [d for d in docs if not kind or d.kind == kind]
            if not docs:
                return {"ok": False, "error": "no statements read"}
            doc = docs[min(int(event.get("index", 0)), len(docs) - 1)]
            lines = [ln.rstrip() for ln in doc.body.splitlines() if ln.strip()]
            start, count = int(event.get("start", 0)), int(event.get("count", 120))
            taken = {r["line_no"] for r in reader.parse_lines(doc.body)}
            numbered = [n for n, ln in enumerate(doc.body.splitlines()) if ln.strip()]
            # Table column titles only: lines with no digits, no @ or dots (no emails/urls) and 3+ spaced columns.
            headers = [re.sub(r"\s{2,}", " | ", ln.strip()) for ln in lines
                       if not re.search(r"[\d@.]", ln) and len(re.split(r"\s{3,}", ln.strip())) >= 3]
            return {"ok": True, "kind": doc.kind, "period": doc.period, "total_lines": len(lines), "headers": headers,
                    "lines_found": doc.lines_found,
                    "shape": [("* " if numbered[i] in taken else "  ") + s for i, s in
                              enumerate(reader.shape(doc.body, max_lines=10_000))][start:start + count]}
        if event.get("action") == "blog_shape":
            # What the blog reader sees: listing links, and the newest dividend post's lines with what was parsed.
            from .invest import blog

            links = []
            for listing in blog.LISTINGS:
                try:
                    links += [u for u in blog.post_links(blog._get(listing)) if u not in links]
                except Exception as e:
                    links.append(f"error {listing}: {e}")
            url = event.get("url") or next((u for u in links if "dividend" in u.lower()), None)
            if not url:
                return {"ok": False, "links": links}
            title, published, main = blog.content(blog._get(url))
            lines = blog.lines_of(main)
            start, count = int(event.get("start", 0)), int(event.get("count", 80))
            return {"ok": True, "links": links[:40], "url": url, "title": title, "published": str(published),
                    "lines": lines[start:start + count], "total_lines": len(lines),
                    "parsed": [{**i, "ldt": str(i["ldt"]), "pay_date": str(i["pay_date"])} for i in blog.parse_dividends(lines)][:30]}
        if event.get("action") == "refresh_blog":
            from .invest import blog

            if event.get("reread_empty"):
                db.query(blog.BlogPost).filter(blog.BlogPost.items == 0, blog.BlogPost.kind != "news").delete()
                db.commit()
            return {"ok": True, **blog.refresh(db, limit=int(event.get("limit", 12)))}
        if event.get("action") == "bank_shape":
            from .banking import reader as bank_reader

            return {"ok": True, **bank_reader.shape(db, user.id, int(event.get("index", 0)), int(event.get("start", 0)),
                                                    int(event.get("count", 120)))}
        if event.get("action") == "reparse_bank":
            from .banking import reader as bank_reader

            return {"ok": True, **bank_reader.reparse(db, user.id, only_empty=bool(event.get("only_empty")))}
        if event.get("action") == "read_bank":
            from .banking import reader as bank_reader

            return bank_reader.read_batch(db, user.id, limit=int(event.get("limit", 15)))
        return {"ok": False, "error": "unknown action"}
    finally:
        db.close()


def handler(event, context):
    if isinstance(event, dict) and event.get("source") == "clab.admin":
        return admin(event)
    if isinstance(event, dict) and event.get("source") == "aws.events":
        from .invest.alerts import check_all
        from .invest.ee.sync import sync_all
        from .invest.portfolio import record_all

        db = SessionLocal()
        try:
            result = {}
            try:
                result["easyequities_synced"] = sync_all(db)
            except Exception:
                logging.exception("Nightly EasyEquities sync failed")
                db.rollback()
            try:
                from .banking.reader import read_all

                result["bank_statements"] = read_all(db)
            except Exception:
                logging.exception("Nightly bank statement read failed")
                db.rollback()
            try:
                from .invest.blog import refresh

                result["blog"] = refresh(db)
            except Exception:
                logging.exception("Nightly blog read failed")
                db.rollback()
            result["alerts_sent"] = check_all(db)
            result["snapshots"] = record_all(db)
            return result
        finally:
            db.close()
    return _asgi(event, context)
