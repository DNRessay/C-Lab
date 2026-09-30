import logging

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


for r in (auth.router, invest_router, markets.router, ee_router):
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
def handler(event, context):
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
            result["alerts_sent"] = check_all(db)
            result["snapshots"] = record_all(db)
            return result
        finally:
            db.close()
    return _asgi(event, context)
