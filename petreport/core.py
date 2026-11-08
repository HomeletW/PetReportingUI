from datetime import datetime, time, timedelta, timezone
import hashlib
import json
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select, text, update
from . import db
from .config import settings


def fail(status, detail):
    raise HTTPException(status, detail)


def lock(session, key):
    number = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
    acquired = session.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": number}).scalar_one()
    if not acquired:
        fail(409, "concurrent_operation; retry with the same idempotency key")


def calendar_bounds(start, end, tz=None):
    zone = ZoneInfo(tz or settings().timezone)
    return (
        datetime.combine(start, time.min, zone).astimezone(timezone.utc),
        datetime.combine(end, time.min, zone).astimezone(timezone.utc),
    )


def lease(session, body):
    cfg = settings()
    # Serialize household writes, including overlapping boundary-date runs.
    lock(session, "household:" + cfg.household_id)
    run = (
        session.execute(select(db.runs).where(db.runs.c.id == str(body.run_id)).with_for_update())
        .mappings()
        .first()
    )
    if (
        not run
        or run["household_id"] != cfg.household_id
        or run["lease_token"] != str(body.lease_token)
        or run["lease_until"] <= db.now()
        or run["status"] != "running"
    ):
        fail(409, "stale_or_invalid_lease")
    return run


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def report_url(report_id):
    return settings().public_base_url.rstrip("/") + "/reports/" + str(report_id)


def stale_for_visits(session, ids):
    if ids:
        report_ids = select(db.report_visits.c.report_id).where(db.report_visits.c.visit_id.in_(ids))
        session.execute(
            update(db.reports)
            .where(db.reports.c.id.in_(report_ids), db.reports.c.status.in_(["published", "prepared"]))
            .values(status="stale")
        )


def template_summary(facts):
    counts = facts["counts"]
    prefix = "Incomplete collection. " if facts["collection_status"] != "complete" else ""
    return (
        prefix
        + f"{counts.get('feeder', 0)} feeder visits and {counts.get('litterbox', 0)} litterbox visits detected. "
        + "Camera coverage is unknown. Tracking bounds are not behavior duration."
    )


def date_list(start, end):
    return [start + timedelta(days=i) for i in range((end - start).days)]
