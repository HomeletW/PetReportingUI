from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from . import db
from .api import router
from .config import settings
from .core import fail
from .evidence import FrigateClient, media_path

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
        "default-src 'self'; img-src 'self'; media-src 'self'; style-src 'self'; script-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    )
    # Public pages are checked against expiry on every request.
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.get("/healthz")
def healthz():
    return {"status": "alive"}


@app.get("/internal/v1/openapi.json", dependencies=router.dependencies)
def openapi():
    return app.openapi()


@app.get("/", response_class=HTMLResponse)
def archive(request: Request):
    try:
        with db.sessions()() as session:
            reports = (
                session.execute(
                    select(db.reports)
                    .where(
                        db.reports.c.status.in_(["published", "stale"]), db.reports.c.expires_at > db.now()
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


@app.get("/reports/{report_id}", response_class=HTMLResponse)
def report_page(report_id: UUID, request: Request):
    with db.sessions()() as session:
        row = session.execute(select(db.reports).where(db.reports.c.id == str(report_id))).mappings().first()
        if not row or row["status"] not in ("published", "stale", "expired"):
            fail(404, "report_not_found")
        if row["expires_at"] <= db.now() or row["status"] == "expired":
            fail(410, "report_expired")
        daily = (
            session.execute(
                select(db.reports).where(
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
        return templates.TemplateResponse(
            request=request,
            name="report.html",
            context={
                "report": row,
                "pet_name": settings().pet_name,
                "daily_links": {str(d["local_start"]): d["id"] for d in daily},
            },
        )


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


templates.env.globals["timedelta"] = timedelta
