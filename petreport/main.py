from contextlib import asynccontextmanager
from datetime import date, timedelta
import hashlib
import hmac
import secrets
import time
from urllib.parse import parse_qs, urlparse
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, select, update
from sqlalchemy.exc import SQLAlchemyError

from . import db
from .api import readiness, router
from .config import settings
from .core import fail, lock
from .retention import purge_media
from .evidence import FrigateClient, media_path
from .timeline import day_timeline

ROOT = Path(__file__).parent


@asynccontextmanager
async def lifespan(app):
    settings().media_root.mkdir(parents=True, exist_ok=True)
    app.state.frigate = FrigateClient()
    yield
    await app.state.frigate.close()


app = FastAPI(
    title="Coconut report server",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.include_router(router)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")


@app.middleware("http")
async def boundaries(request: Request, call_next):
    if request.method in ("POST", "PUT"):
        size = 0
        parts = []
        async for chunk in request.stream():
            size += len(chunk)
            if size > 2_000_000:
                return JSONResponse({"detail": "request_too_large"}, status_code=413)
            parts.append(chunk)
        request._body = b"".join(parts)
    try:
        response = await call_next(request)
    except (SQLAlchemyError, RuntimeError):
        response = JSONResponse({"detail": "database_unavailable_or_migration_required"}, status_code=503)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self'; media-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    # Public pages are checked against expiry on every request.
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.get("/api/health")
def health():
    return readiness()


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(ROOT / "static" / "favicon.png", media_type="image/png")


@app.get("/healthz")
def healthz():
    return {"status": "alive"}


@app.get("/internal/v1/openapi.json", dependencies=router.dependencies)
def openapi():
    return app.openapi()


@app.get("/", include_in_schema=False)
def home():
    return RedirectResponse("/journal", status_code=307)


@app.get("/journal", response_class=HTMLResponse)
def archive(request: Request):
    try:
        with db.sessions()() as session:
            reports = (
                session.execute(
                    select(db.reports)
                    .where(
                        db.reports.c.status.in_(["published", "stale"]),
                        db.reports.c.expires_at > db.now(),
                        db.reports.c.household_id == settings().household_id,
                        db.reports.c.timezone == settings().timezone,
                    )
                    .order_by(db.reports.c.local_end.desc())
                    .limit(100)
                )
                .mappings()
                .all()
            )
        return templates.TemplateResponse(
            request=request,
            name="archive.html",
            context={"reports": reports, "pet_name": settings().pet_name, "degraded": False},
        )
    except (SQLAlchemyError, RuntimeError):
        return templates.TemplateResponse(
            request=request,
            name="archive.html",
            context={"reports": [], "pet_name": settings().pet_name, "degraded": True},
            status_code=503,
        )


def report_path(row):
    return f"/reports/{row['kind']}/{row['local_start'].isoformat()}"


@app.get("/today", response_class=HTMLResponse)
def today_journal(request: Request):
    cfg = settings()
    today = db.now().astimezone(ZoneInfo(cfg.timezone)).date()
    yesterday = today - timedelta(days=1)
    with db.sessions()() as session:
        row = (
            session.execute(
                select(db.reports).where(
                    db.reports.c.household_id == cfg.household_id,
                    db.reports.c.timezone == cfg.timezone,
                    db.reports.c.kind == "daily",
                    db.reports.c.local_start == yesterday,
                    db.reports.c.local_end == today,
                    db.reports.c.status.in_(["published", "stale"]),
                    db.reports.c.expires_at > db.now(),
                )
            )
            .mappings()
            .first()
        )
        if row:
            return RedirectResponse(report_path(row), status_code=307)
    return templates.TemplateResponse(
        request=request,
        name="today.html",
        context={"pet_name": cfg.pet_name, "local_date": yesterday, "timezone": cfg.timezone},
    )


def available_report(session, report_id):
    row = (
        session.execute(
            select(db.reports).where(
                db.reports.c.id == str(report_id), db.reports.c.household_id == settings().household_id
            )
        )
        .mappings()
        .first()
    )
    if not row or row["status"] not in ("published", "stale", "expired"):
        fail(404, "report_not_found")
    if row["expires_at"] <= db.now() or row["status"] == "expired":
        fail(410, "report_expired")
    return row


@app.get("/reports/{report_id}", response_class=HTMLResponse)
def report_page(report_id: UUID, request: Request):
    with db.sessions()() as session:
        return RedirectResponse(report_path(available_report(session, report_id)), status_code=307)


def dated_report(kind: str, local_date: date, request: Request):
    if kind not in ("daily", "weekly"):
        fail(404, "report_not_found")
    with db.sessions()() as session:
        row = (
            session.execute(
                select(db.reports).where(
                    db.reports.c.household_id == settings().household_id,
                    db.reports.c.kind == kind,
                    db.reports.c.local_start == local_date,
                    db.reports.c.timezone == settings().timezone,
                )
            )
            .mappings()
            .first()
        )
        if not row:
            return templates.TemplateResponse(
                request=request,
                name="missing.html",
                context={
                    "pet_name": settings().pet_name,
                    "local_date": local_date,
                },
                status_code=404,
            )
        row = available_report(session, row["id"])
        daily = (
            session.execute(
                select(db.reports).where(
                    db.reports.c.household_id == row["household_id"],
                    db.reports.c.timezone == row["timezone"],
                    db.reports.c.kind == "daily",
                    db.reports.c.local_start >= row["local_start"],
                    db.reports.c.local_end <= row["local_end"],
                    db.reports.c.status.in_(["published", "stale"]),
                    db.reports.c.expires_at > db.now(),
                )
            )
            .mappings()
            .all()
        )
        neighbors = {}
        for label, comparison, ordering in [
            ("previous", db.reports.c.local_start < local_date, db.reports.c.local_start.desc()),
            ("next", db.reports.c.local_start > local_date, db.reports.c.local_start.asc()),
        ]:
            neighbor = (
                session.execute(
                    select(db.reports)
                    .where(
                        db.reports.c.household_id == row["household_id"],
                        db.reports.c.kind == kind,
                        db.reports.c.timezone == row["timezone"],
                        comparison,
                        db.reports.c.status.in_(["published", "stale"]),
                        db.reports.c.expires_at > db.now(),
                    )
                    .order_by(ordering)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            neighbors[label] = report_path(neighbor) if neighbor else None
        timeline = day_timeline(row["facts"].get("visits", []), row["local_start"], row["timezone"])
        visits = timeline["visits"]
        return templates.TemplateResponse(
            request=request,
            name="report.html",
            context={
                "report": row,
                "pet_name": settings().pet_name,
                "visits": visits,
                "day_timeline": timeline,
                "daily_links": {str(d["local_start"]): report_path(d) for d in daily},
                "neighbors": neighbors,
            },
        )


@app.get("/reports/daily/{local_date}", response_class=HTMLResponse)
def daily_report(local_date: date, request: Request):
    return dated_report("daily", local_date, request)


@app.get("/reports/weekly/{local_date}", response_class=HTMLResponse)
def weekly_report(local_date: date, request: Request):
    return dated_report("weekly", local_date, request)


# A short-lived, report/revision-bound token prevents cross-origin deletion and stale confirmations.
_fallback_key = secrets.token_bytes(32)


def deletion_token(row, expires):
    key = settings().internal_api_token.get_secret_value().encode() or _fallback_key
    value = f"delete:{row['id']}:{row['revision']}:{expires}"
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


@app.get("/reports/{report_id}/delete", response_class=HTMLResponse)
def confirm_delete(report_id: UUID, request: Request):
    with db.sessions()() as session:
        row = available_report(session, report_id)
        expires = int(time.time()) + 900
        return templates.TemplateResponse(
            request=request,
            name="delete.html",
            context={
                "report": row,
                "pet_name": settings().pet_name,
                "expires": expires,
                "delete_token": deletion_token(row, expires),
                "cancel_url": report_path(row),
            },
        )


@app.post("/reports/{report_id}/delete")
async def delete_report(report_id: UUID, request: Request):
    origin = request.headers.get("origin")
    allowed = {urlparse(settings().public_base_url).netloc, request.url.netloc}
    if request.headers.get("sec-fetch-site") == "cross-site" or (
        origin
        and (urlparse(origin).scheme not in ("http", "https") or urlparse(origin).netloc not in allowed)
    ):
        fail(403, "invalid_origin")
    if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
        fail(415, "form_required")
    try:
        form = parse_qs((await request.body()).decode(), max_num_fields=5)
        expires = int(form["expires"][0])
        token = form["token"][0]
    except (KeyError, ValueError, IndexError, UnicodeDecodeError):
        fail(403, "confirmation_required")
    with db.sessions().begin() as session:
        lock(session, "household:" + settings().household_id)
        session.execute(select(db.reports.c.id).where(db.reports.c.id == str(report_id)).with_for_update())
        row = available_report(session, report_id)
        if not int(time.time()) < expires <= int(time.time()) + 900 or not hmac.compare_digest(
            token, deletion_token(row, expires)
        ):
            fail(403, "confirmation_expired_or_changed")
        # Retain the row and delivery ledger so a deletion cannot reset notification fencing.
        session.execute(
            update(db.reports)
            .where(db.reports.c.id == str(report_id))
            .values(
                status="deleted",
                expires_at=db.now(),
                facts={},
                narrative=None,
            )
        )
        media_ids = (
            session.execute(
                select(db.report_media.c.media_id).where(db.report_media.c.report_id == str(report_id))
            )
            .scalars()
            .all()
        )
        for table in (db.report_media, db.report_visits, db.report_revisions):
            session.execute(delete(table).where(table.c.report_id == str(report_id)))
        purge_media(session, media_ids)
    return RedirectResponse("/journal?deleted=1", status_code=303)


@app.get("/media/{media_id}")
def public_media(media_id: UUID):
    with db.sessions()() as session:
        permitted = (
            select(db.report_media.c.media_id)
            .join(db.reports, db.reports.c.id == db.report_media.c.report_id)
            .where(db.reports.c.status.in_(["published", "stale"]), db.reports.c.expires_at > db.now())
        )
        row = (
            session.execute(
                select(db.media).where(
                    db.media.c.id == str(media_id), db.media.c.status == "ready", db.media.c.id.in_(permitted)
                )
            )
            .mappings()
            .first()
        )
        if not row or not media_path(row["relative_path"]).is_file():
            fail(404, "media_unavailable_or_expired")
        # Starlette FileResponse provides HTTP byte ranges for browser seeking.
        return FileResponse(media_path(row["relative_path"]), media_type=row["mime"])


templates.env.globals.update(timedelta=timedelta, report_path=report_path)
